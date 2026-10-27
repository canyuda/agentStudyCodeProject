"""步骤 6：Agent Loop —— 网关的心脏。

职责一句话：消费 ModelStreamEvent 流，补上身份信息变成 RunEvent 入账，
并全程维护"取消、观测（TTFT）、checkpoint、终态"四件事。

                  model.stream()                store.append_event()
    [Fake/DeepSeek] ──ModelStreamEvent──▶ [Agent Loop] ──RunEvent──▶ [账本/SSE/SQLite]

与课件 1-3 单文件版的差异（本作业的四点增强）：
1. TTFT 观测：首个 delta 与 run.started 的时间差，随 run.metrics 事件发布；
2. 周期 checkpoint：每 N 个 delta 存一次进度快照并广播 checkpoint.saved；
3. run.metrics 在终态事件之前发布（订阅端见到终态就会关流，指标必须先行）；
4. 强制取消路径里用 asyncio.shield 保护终态事件写入（见下方注释）。

取消的两层机制（验收标准"取消真停止"）：
- 协作式：每个模型事件到达时检查 cancel_event，发现取消就 break 出
  async for —— 模型生成器随之关闭，后续 token 不再被消费、不再计费；
- 强制式：HTTP 层对 asyncio.Task 调 task.cancel()，在下一个 await 点
  抛 CancelledError，哪怕模型卡死在网络 IO 上也能被打断。
"""

from __future__ import annotations

import asyncio
import time

from .events import (
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
)
from .model import StreamingModel
from .store import Run, RunStore

DEFAULT_SYSTEM_PROMPT = "你是简洁的中文助手，直接回答用户问题。"
CHECKPOINT_EVERY = 10  # 每累计多少个 delta 保存一次进度快照


async def execute_run(
    store: RunStore,
    run: Run,
    model: StreamingModel,
    *,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    checkpoint_every: int = CHECKPOINT_EVERY,
) -> None:
    """执行一次 Run 的完整生命周期，直到进入某个终态才返回。"""
    started_at = time.monotonic()
    first_delta_at: float | None = None

    store.set_status(run, RUN_STATUS_RUNNING)
    await store.append_event(run, "run.started", {"status": run.status})

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": run.prompt},
    ]

    text_parts: list[str] = []
    usage: dict = {}
    finish_reason: str | None = None
    delta_count = 0

    try:
        async for model_event in model.stream(messages):
            # ---- 协作式取消检查点：每个模型事件都先看一眼 ----
            if run.cancel_event.is_set():
                partial = "".join(text_parts)
                store.set_status(run, RUN_STATUS_CANCELLED)
                await store.append_event(
                    run,
                    "run.cancelled",
                    {"reason": "user_requested", "partial_text": partial},
                )
                return  # return 跳出 async for：生成器被关闭，模型停止吐字

            if model_event.type == "text.delta":
                delta = model_event.data["delta"]
                if first_delta_at is None:
                    first_delta_at = time.monotonic()  # TTFT 计时终点
                text_parts.append(delta)
                delta_count += 1
                await store.append_event(run, "text.delta", {"delta": delta})

                # ---- 周期 checkpoint：进度快照 + 广播 ----
                if delta_count % checkpoint_every == 0:
                    partial = "".join(text_parts)
                    store.save_checkpoint(
                        run,
                        {"text_so_far": partial, "deltas": delta_count},
                    )
                    await store.append_event(
                        run,
                        "checkpoint.saved",
                        {"deltas": delta_count, "chars": len(partial)},
                    )

            elif model_event.type == "model.usage":
                usage = dict(model_event.data)

            elif model_event.type == "model.finished":
                finish_reason = model_event.data.get("finish_reason")

        # ---- 模型流自然耗尽：先看是否被截断 ----
        full_text = "".join(text_parts)

        if finish_reason == "length":
            store.set_status(run, RUN_STATUS_FAILED)
            await store.append_event(
                run,
                "run.failed",
                {
                    "code": "OUTPUT_TRUNCATED",
                    "retryable": False,
                    "partial_text": full_text,
                },
            )
            return

        # ---- 正常完成：指标先行，终态殿后 ----
        store.set_status(run, RUN_STATUS_COMPLETED)
        await store.append_event(
            run,
            "run.metrics",
            {
                "ttft_ms": round((first_delta_at - started_at) * 1000, 1)
                if first_delta_at is not None
                else None,
                "total_ms": round((time.monotonic() - started_at) * 1000, 1),
                "deltas": delta_count,
                "usage": usage,
            },
        )
        await store.append_event(
            run,
            "run.completed",
            {"text": full_text, "usage": usage, "finish_reason": finish_reason},
        )

    except asyncio.CancelledError:
        # ---- 强制取消路径：task.cancel() 在某个 await 点打断了我们 ----
        # 若协作式路径已经写过终态事件，这里幂等跳过（账本守卫也会拦）。
        partial = "".join(text_parts)
        already_terminal = run.events and run.events[-1].type in (
            "run.completed", "run.cancelled", "run.failed"
        )
        if not already_terminal:
            store.set_status(run, RUN_STATUS_CANCELLED)
            # shield：本 Task 已收到取消信号，此刻再裸 await 可能立刻再次
            # 抛 CancelledError，导致 run.cancelled 事件丢失、客户端永远等不到
            # 终态。shield 让"写终态事件"这个小节不被二次取消打断。
            await asyncio.shield(
                store.append_event(
                    run,
                    "run.cancelled",
                    {"reason": "task_cancelled", "partial_text": partial},
                )
            )
        raise  # 必须重新抛出：取消信号不允许被吞掉，否则上层无法感知

    except Exception as exc:
        # ---- 模型/网络层任何异常 -> 统一转 run.failed 终态（fail fast，不吞） ----
        partial = "".join(text_parts)
        store.set_status(run, RUN_STATUS_FAILED)
        await store.append_event(
            run,
            "run.failed",
            {
                "code": "MODEL_STREAM_FAILED",
                "retryable": True,
                "error": str(exc),
                "partial_text": partial,
            },
        )
