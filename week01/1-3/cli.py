import argparse
import json
import os
import sys
from typing import Any
from dotenv import load_dotenv
load_dotenv()

import httpx


DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_SYSTEM_PROMPT = "你是代码审查助手，只报告有证据的问题。"


def print_status(message: str) -> None:
    print(message, file=sys.stderr)


def get_api_key(cli_value: str | None) -> str:
    api_key = cli_value or os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("请先通过 --api-key 或环境变量 DEEPSEEK_API_KEY 提供 DeepSeek API Key")
    return api_key


def build_chat_payload(
    prompt: str,
    system_prompt: str,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "max_tokens": max_tokens,
        "stream_options": {"include_usage": True},
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def handle_chunk_payload(
    chunk: dict[str, Any],
    *,
    state: dict[str, Any],
) -> bool:
    choices = chunk.get("choices") or []
    choice = choices[0] if choices else None
    delta = None
    finish_reason = None

    if choice is not None:
        delta = (choice.get("delta") or {}).get("content")
        finish_reason = choice.get("finish_reason")

    if delta:
        sys.stdout.write(delta)
        sys.stdout.flush()
        state["saw_text_delta"] = True

    if finish_reason:
        state["finish_reason"] = finish_reason

    usage = chunk.get("usage")
    if usage is not None:
        state["usage"] = usage

    return finish_reason is not None


def stream_deepseek_sse(
    *,
    api_key: str,
    base_url: str,
    prompt: str,
    system_prompt: str,
    model: str,
    max_tokens: int,
) -> None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload = build_chat_payload(prompt, system_prompt, model, max_tokens)

    print_status("[status] created")

    state: dict[str, Any] = {
        "saw_text_delta": False,
        "finish_reason": None,
        "usage": None,
    }

    try:
        with httpx.Client(timeout=None) as client:
            with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                response.raise_for_status()
                print_status("[status] running")

                data_lines: list[str] = []

                for line in response.iter_lines():
                    if line == "":
                        if data_lines:
                            data = "\n".join(data_lines)
                            data_lines = []

                            if data == "[DONE]":
                                break

                            chunk = json.loads(data)
                            is_terminal = handle_chunk_payload(chunk, state=state)
                            if is_terminal:
                                break
                        continue

                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())

        if state["saw_text_delta"]:
            print()

        if state["finish_reason"] == "length":
            print_status("[status] failed code=OUTPUT_TRUNCATED retryable=False")
            return

        usage = state["usage"] or {}
        print_status(
            f"[status] completed finish_reason={state['finish_reason']!r} usage={json.dumps(usage, ensure_ascii=False)}"
        )
    except KeyboardInterrupt:
        # CLI 收到 Ctrl+C 时，把本地执行视图切换到 cancelling/cancelled。
        print_status("[status] cancelling")
        print_status("[status] cancelled reason=user_interrupted")
    except httpx.HTTPStatusError as exc:
        body = exc.response.text
        print_status(f"[status] failed code=HTTP_{exc.response.status_code} body={body}")
    except Exception as exc:
        print_status(f"[status] failed code=STREAM_ERROR error={exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="直接请求 DeepSeek，并通过 CLI 消费原始 SSE 增量流。",
    )
    parser.add_argument("prompt", help="发送给 DeepSeek 的用户提示词")
    parser.add_argument(
        "--api-key",
        default=None,
        help="DeepSeek API Key；未传时从 DEEPSEEK_API_KEY 读取",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"DeepSeek OpenAI 兼容接口地址，默认是 {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"模型名，默认是 {DEFAULT_MODEL}",
    )
    parser.add_argument(
        "--system-prompt",
        default=DEFAULT_SYSTEM_PROMPT,
        help="system prompt 文本",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1024,
        help="生成的最大 token 数，默认是 1024",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        api_key = get_api_key(args.api_key)
    except RuntimeError as exc:
        print_status(f"[status] failed code=MISSING_API_KEY error={exc}")
        raise SystemExit(1) from exc

    stream_deepseek_sse(
        api_key=api_key,
        base_url=args.base_url.rstrip("/"),
        prompt=args.prompt,
        system_prompt=args.system_prompt,
        model=args.model,
        max_tokens=args.max_tokens,
    )


if __name__ == "__main__":
    main()
