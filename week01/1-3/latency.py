from dataclasses import dataclass
import os
from time import perf_counter
from typing import Optional


@dataclass
class StreamMetrics:
    started_at: float
    first_event_at: Optional[float] = None
    first_token_at: Optional[float] = None
    completed_at: Optional[float] = None

    def mark_first_event(self) -> None:
        if self.first_event_at is None:
            self.first_event_at = perf_counter()

    def mark_text_delta(self, text_delta: Optional[str]) -> None:
        # 首个供应商事件不一定带有可显示文本，因此 TTFT 必须等到
        # 第一个非空 text.delta 到达后才开始记录。
        self.mark_first_event()
        if self.first_token_at is None and text_delta:
            self.first_token_at = perf_counter()

    def mark_completed(self) -> None:
        if self.first_event_at is None:
            self.mark_first_event()
        self.completed_at = perf_counter()

    def snapshot(self) -> dict[str, Optional[float]]:
        return {
            "first_event_seconds": (
                None
                if self.first_event_at is None
                else self.first_event_at - self.started_at
            ),
            "ttft_seconds": (
                None
                if self.first_token_at is None
                else self.first_token_at - self.started_at
            ),
            "generation_seconds": (
                None
                if self.first_token_at is None or self.completed_at is None
                else self.completed_at - self.first_token_at
            ),
            "total_seconds": (
                None
                if self.completed_at is None
                else self.completed_at - self.started_at
            ),
        }


def build_client():
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:
        raise RuntimeError("请先安装 openai：pip install openai") from exc

    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("请先设置环境变量 DEEPSEEK_API_KEY")

    # DeepSeek 客户端。这里沿用 OpenAI SDK 的兼容调用方式。
    return OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    )


def stream_model_response(metrics: StreamMetrics) -> str:
    client = build_client()
    response = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {
                "role": "user",
                "content": "请用一句中文短句介绍 TTFT 是什么，不要列点。",
            },
        ],
        stream=True,
        extra_body={"thinking": {"type": "disabled"}},
    )

    chunks: list[str] = []
    saw_first_event = False

    for chunk in response:
        if not saw_first_event:
            saw_first_event = True
            print("收到首个供应商事件")
            metrics.mark_first_event()

        choices = getattr(chunk, "choices", None)
        if not choices:
            continue

        text_delta = getattr(choices[0].delta, "content", None)
        if text_delta is None:
            print("收到非文本事件: delta.content = None")
            continue

        if text_delta == "":
            print("收到空 text.delta，不计入用户可感知 TTFT")
            metrics.mark_text_delta(text_delta)
            continue

        if metrics.first_token_at is None:
            print(f"收到首个非空 text.delta: {text_delta!r}")

        metrics.mark_text_delta(text_delta)
        print(text_delta, end="", flush=True)
        chunks.append(text_delta)

    metrics.mark_completed()
    return "".join(chunks)


def main() -> None:
    metrics = StreamMetrics(started_at=perf_counter())
    print("开始请求模型...")
    try:
        full_text = stream_model_response(metrics)
    except RuntimeError as exc:
        print(f"运行失败: {exc}")
        return

    print("\n流式输出结束")
    print(f"完整文本: {full_text!r}")

    print("\n计时结果:")
    for name, value in metrics.snapshot().items():
        if value is None:
            print(f"- {name}: None")
        else:
            print(f"- {name}: {value:.3f}s")


if __name__ == "__main__":
    main()
