"""步骤 7：HTTP 接口 —— 创建 / 订阅 / 取消，把前六步组装成完整服务。

三个核心端点（外加一个状态查询）：

  POST /v1/runs                创建 Run：登记 -> run.created 入账 -> 启动 Agent Loop
  GET  /v1/runs/{id}/events    SSE 订阅：Last-Event-ID 断线补发 + 心跳保活
  POST /v1/runs/{id}/cancel    取消：协作式信号 + 强制式 task.cancel 双保险
  GET  /v1/runs/{id}           状态查询（审计视角：状态/事件数/checkpoint）

两个关键设计：
1. create_app 工厂 + 依赖注入（store / model 从外面传进来）：
   第 9 步测试注入 FakeModel 就能离线跑全链路，不需要端口和网络。
2. "断线不重跑"的实现位置就在订阅生成器里：
   客户端断开只让 subscribe 生成器 return，Agent Loop 的 Task 感知不到，
   继续跑完并写终态；客户端带 Last-Event-ID 重连后从账本补发即可。

启动方式（详见 README）：
  cd agentStudyCodeProject/homework/streamingAgentGateway
  ../../.venv/bin/python -m uvicorn gateway.server:app --port 8300
  环境变量 GATEWAY_MODEL=deepseek 切换真实模型（默认 fake）
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from dotenv import load_dotenv

from .deepseek_adapter import DEFAULT_ENV_PATH, build_deepseek_model
from .events import is_terminal_event
from .fake_model import FakeStreamingModel
from .loop import execute_run
from .model import StreamingModel
from .sse import encode_heartbeat, encode_sse, parse_last_event_id
from .sqlite_store import SqliteRunStore
from .store import Run, RunStore

# Web 首页（第 8 步的浏览器客户端）
_INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"

# 服务启动时统一从 agentStudyCodeProject/.env 加载配置（GATEWAY_* / DEEPSEEK_*）。
# override=False：命令行已 export 的环境变量优先于 .env，便于临时覆盖。
load_dotenv(DEFAULT_ENV_PATH)

# 订阅空闲超过该秒数就发一次心跳，防止中间层（代理/浏览器）掐掉空闲连接
HEARTBEAT_INTERVAL = 15.0


class CreateRunRequest(BaseModel):
    prompt: str = Field(min_length=1, description="发送给模型的用户提示词")


def pick_model() -> StreamingModel:
    """按环境变量 GATEWAY_MODEL 选择模型实现：fake（默认）/ deepseek。

    默认 fake 是刻意的：整个服务不依赖模型 SDK、不花 token 就能完整演示。
    """
    choice = os.environ.get("GATEWAY_MODEL", "fake").lower()
    if choice == "deepseek":
        return build_deepseek_model()
    return FakeStreamingModel()


def build_store() -> RunStore:
    """按环境变量 GATEWAY_DB 决定存储实现（第 10 步）：

    - 未设置（默认）：纯内存 RunStore，重启即空，演示最轻量；
    - 设置为文件路径：SqliteRunStore 写穿持久化，启动时自动恢复历史 Run
      （已终态的可回放续传，非终态的孤儿补 run.failed 回收）。
    """
    db_path = os.environ.get("GATEWAY_DB")
    if db_path:
        store = SqliteRunStore(db_path)
        store.restore_all()
        return store
    return RunStore()


def create_app(
    store: RunStore | None = None,
    model: StreamingModel | None = None,
) -> FastAPI:
    """组装应用。store/model 允许注入，测试时用内存版和假模型。"""
    store = store or build_store()
    model = model or pick_model()
    # 正在执行的 Agent Loop Task：取消接口需要拿它做强制取消
    tasks: dict[str, asyncio.Task[None]] = {}

    app = FastAPI(title="Streaming Agent Gateway")

    # ------------------------------------------------------------- 首页 ----
    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """Web 客户端（gateway/static/index.html）。"""
        return HTMLResponse(_INDEX_HTML.read_text(encoding="utf-8"))

    # ------------------------------------------------------------- 创建 ----
    @app.post("/v1/runs")
    async def create_run(body: CreateRunRequest) -> dict[str, str]:
        run = store.create_run(body.prompt)
        # run.created 先入账：此时状态仍是 created，"创建"与"开始执行"分离
        await store.append_event(
            run, "run.created", {"prompt": body.prompt, "status": run.status}
        )
        # Agent Loop 跑在独立 Task 上：订阅断开与否完全不影响它
        task = asyncio.create_task(execute_run(store, run, model))
        tasks[run.run_id] = task
        task.add_done_callback(lambda _: tasks.pop(run.run_id, None))
        return {"run_id": run.run_id, "status": run.status}

    # ------------------------------------------------------------- 查询 ----
    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "run_id": run.run_id,
            "status": run.status,
            "event_count": len(run.events),
            "checkpoint": run.checkpoint,
        }

    # ------------------------------------------------------------- 订阅 ----
    async def subscribe(request: Request, run: Run, after_seq: int) -> AsyncIterator[str]:
        """SSE 订阅生成器：从 after_seq+1 开始补发，追上后等新事件。

        三条退出路径：
        - 补发/追平过程中遇到终态事件：正常收尾；
        - 客户端断开（is_disconnected）：只结束订阅，Run 继续跑（断线不重跑）；
        - 等待超时：发心跳续命，循环继续。
        """
        cursor = after_seq
        while True:
            # 1) 先把账本里已有的事件补出去（断线恢复 / 历史回放）
            for event in store.events_after(run, cursor):
                cursor = event.seq
                yield encode_sse(event)
                if is_terminal_event(event):
                    return

            # 2) 追平了：客户端还在吗？不在就安静退出
            if await request.is_disconnected():
                return

            # 3) 等新事件；超时就发心跳防止连接被中间层回收
            if not await store.wait_for_events(run, HEARTBEAT_INTERVAL):
                yield encode_heartbeat()

    @app.get("/v1/runs/{run_id}/events")
    async def stream_events(
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        after_seq_query: int | None = Query(default=None, alias="after_seq"),
    ) -> StreamingResponse:
        """SSE 订阅端点。续传位置三个来源，优先级从高到低：

        1. Last-Event-ID 请求头：浏览器 EventSource 自动重连时原生携带；
        2. ?after_seq=N 查询参数：浏览器 EventSource 无法自定义请求头，
           手动重连时（重新 new EventSource）只能走 query 传断点；
        3. 都没有：从头回放（after_seq=-1）。
        """
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")

        if last_event_id is not None:
            try:
                after_seq = parse_last_event_id(last_event_id)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="invalid Last-Event-ID"
                ) from None
        elif after_seq_query is not None:
            after_seq = after_seq_query
        else:
            after_seq = -1

        return StreamingResponse(
            subscribe(request, run, after_seq),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",  # 告诉 nginx 之类反向代理不要缓冲 SSE
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------- 取消 ----
    @app.post("/v1/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")

        # 第一层：协作式信号（Loop 在下一个模型事件处检查并优雅收尾）
        accepted = store.request_cancel(run)
        # 第二层：强制式 task.cancel（哪怕模型卡死在网络 IO 也能打断）
        task = tasks.get(run_id)
        if accepted and task is not None:
            task.cancel()

        return {"run_id": run_id, "status": run.status, "accepted": accepted}

    return app


# 模块级默认实例：uvicorn gateway.server:app 直接可用
app = create_app()
