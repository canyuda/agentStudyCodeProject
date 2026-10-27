"""步骤 8b：CLI 客户端 —— 用命令行消费网关的 SSE 流。

用法（服务先启动，见 README）：

  # 创建一个新 Run 并流式打印，直到终态
  python -m gateway.cli "用一段话解释 SSE"

  # 断点续读已有 Run（比如上次 Ctrl+C 断开后），不创建新调用
  python -m gateway.cli --resume run_ab12cd34ef56 --after 12

  # 指定服务地址
  python -m gateway.cli "你好" --url http://127.0.0.1:8300

设计要点：
- 正文（text.delta）打到 stdout，元信息（事件/指标）打到 stderr：
  这样 `python -m gateway.cli "..." > out.txt` 能得到纯净的模型输出；
- Ctrl+C 只断开本地订阅，不取消服务端的 Run（断线 != 取消），
  退出前打印下一条续读命令（断点就是已收到的最大 seq）；
- SSE 解析完全复用第 2 步的 SSEDecoder —— 编解码同一份实现，不会两头不一致。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from .events import TERMINAL_EVENT_TYPES
from .sse import SSEDecoder

DEFAULT_BASE_URL = "http://127.0.0.1:8300"


def note(message: str) -> None:
    """元信息走 stderr，保持 stdout 只有模型正文。"""
    print(message, file=sys.stderr)


async def consume_stream(
    client: httpx.AsyncClient,
    base_url: str,
    run_id: str,
    after_seq: int,
) -> None:
    """订阅一个 Run 的 SSE 流并打印，直到终态事件或用户 Ctrl+C。"""
    decoder = SSEDecoder()
    headers = {"Last-Event-ID": str(after_seq)} if after_seq >= 0 else {}

    try:
        async with client.stream(
            "GET", f"{base_url}/v1/runs/{run_id}/events", headers=headers
        ) as response:
            response.raise_for_status()
            note(f"[event] 已连接，从 seq={after_seq + 1} 开始接收")

            async for line in response.aiter_lines():
                event = decoder.feed_line(line)
                if event is None:
                    continue

                if event.type == "text.delta":
                    sys.stdout.write(event.data["delta"])
                    sys.stdout.flush()
                elif event.type == "run.metrics":
                    note(
                        f"[event] metrics: TTFT={event.data['ttft_ms']}ms "
                        f"total={event.data['total_ms']}ms deltas={event.data['deltas']}"
                    )
                elif event.type == "run.completed":
                    sys.stdout.write("\n")
                    note(f"[终态] completed usage={event.data.get('usage')}")
                elif event.type == "run.cancelled":
                    sys.stdout.write("\n")
                    note(f"[终态] cancelled reason={event.data.get('reason')}")
                elif event.type == "run.failed":
                    sys.stdout.write("\n")
                    note(
                        f"[终态] failed code={event.data.get('code')} "
                        f"retryable={event.data.get('retryable')} error={event.data.get('error')}"
                    )
                else:
                    # run.created / run.started / checkpoint.saved 等过程事件
                    note(f"[event] #{event.seq} {event.type} {event.data}")

                if event.type in TERMINAL_EVENT_TYPES:
                    return  # 正常收尾：主动关闭流

    except asyncio.CancelledError:
        # Ctrl+C：Runner 取消了本协程。只断开订阅，服务端 Run 不受影响。
        sys.stdout.write("\n")
        note(f"[断开] 订阅已断开（Run 未取消，仍在服务端运行）。续读命令：")
        note(
            f"        python -m gateway.cli --resume {run_id} "
            f"--after {decoder.last_event_id if decoder.last_event_id is not None else after_seq}"
        )
        raise  # 取消信号不吞


async def run(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient(
        # SSE 长连接：连接/发送要快，读取不能设超时（否则中途会被掐断）
        timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)
    ) as client:
        # ---- 模式一：带 prompt，先创建新 Run ----
        if args.prompt:
            response = await client.post(
                f"{args.url}/v1/runs", json={"prompt": args.prompt}
            )
            if response.status_code != 200:
                note(f"[错误] 创建 Run 失败：HTTP {response.status_code} {response.text}")
                raise SystemExit(1)
            run_id = response.json()["run_id"]
            note(f"[event] 已创建 Run: {run_id}")
        # ---- 模式二：--resume，续读已有 Run ----
        else:
            run_id = args.resume

        try:
            await consume_stream(client, args.url, run_id, args.after)
        except (httpx.HTTPStatusError, httpx.ConnectError) as exc:
            note(f"[错误] 订阅失败：{exc}")
            raise SystemExit(1) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Streaming Agent Gateway 的 CLI 客户端")
    parser.add_argument("prompt", nargs="?", default=None,
                        help="要发送的提示词；提供时创建新 Run")
    parser.add_argument("--resume", default=None, metavar="RUN_ID",
                        help="续读已有 Run（与 prompt 二选一）")
    parser.add_argument("--after", type=int, default=-1, metavar="SEQ",
                        help="续读起点：只接收 seq 大于该值的事件，默认 -1 从头回放")
    parser.add_argument("--url", default=DEFAULT_BASE_URL,
                        help=f"网关地址，默认 {DEFAULT_BASE_URL}")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if not args.prompt and not args.resume:
        note("[错误] 需要提供 prompt（创建模式）或 --resume RUN_ID（续读模式）")
        raise SystemExit(2)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        # 旧版 Python 的 Ctrl+C 走这里；新版已在 CancelledError 分支打印续读提示
        pass


if __name__ == "__main__":
    main()
