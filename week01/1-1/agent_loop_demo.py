import asyncio
import json
import os
import re
import sys
import time

from openai import OpenAI

from sandbox_runner import run_python_in_sandbox


# DeepSeek 客户端。这里沿用 OpenAI SDK 的兼容调用方式。
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)

# 这段系统提示词就是“工具调用规则”。
# 它明确告诉模型：问天气时，如果还没有结果，就先输出一段 tool_call JSON。
SYSTEM = (
    "你是会使用工具的助手。"
    '问天气时，若还没有结果，只输出 JSON：{"type":"tool_call","name":"get_weather","arguments":{"city":"城市名"}}；'
    '当用户要求执行一小段 Python 代码、做计算或验证结果时，若还没有结果，只输出 JSON：{"type":"tool_call","name":"run_python","arguments":{"code":"Python代码"}}；'
    "拿到工具结果后再自然语言回答。"
)


async def get_weather(args: dict) -> dict:
    """一个假的天气工具，用来演示 tool execution。"""
    city = args.get("city", "北京")
    await asyncio.sleep(1)
    return {
        "content": [{"type": "text", "text": f"{city}当前天气晴，27C，微风。"}],
        "details": {
            "city": city,
            "weather": "晴",
            "temperature_c": 27,
        },
    }


async def run_python_tool(args: dict) -> dict:
    """把一小段 Python 代码交给最小 sandbox 执行。"""
    code = args.get("code", "")
    result = await run_python_in_sandbox(code)
    stdout_text = result["stdout"].strip()
    stderr_text = result["stderr"].strip()
    if result["ok"]:
        output_text = (
            "Python 执行成功。\n"
            f"输出:\n{stdout_text or 'No output.'}"
        )
    else:
        detail_text = stderr_text or "No stderr."
        output_text = (
            f"Python 执行失败（{result['error_type']}）。\n"
            f"说明: {result['error_message']}\n"
            f"错误详情:\n{detail_text}"
        )
    return {
        "content": [{"type": "text", "text": output_text}],
        "details": result,
    }


def render(message: dict) -> str:
    """把内部 message 结构渲染成可读文本。"""
    parts = []
    for block in message["content"]:
        if block["type"] == "text":
            parts.append(block["text"])
        elif block["type"] == "toolCall":
            parts.append(
                f'[toolCall] {block["name"]}({json.dumps(block["arguments"], ensure_ascii=False)})'
            )
    return "\n".join(parts)


def to_llm(messages: list[dict]) -> list[dict]:
    """把内部消息格式转成 LLM 输入格式。"""
    out = []
    for message in messages:
        if message["role"] in {"user", "assistant"}:
            out.append({"role": message["role"], "content": render(message)})
        elif message["role"] == "toolResult":
            out.append(
                {
                    "role": "user",
                    "content": (
                        f'Tool result for {message["tool_name"]}: '
                        f'{json.dumps(message["details"], ensure_ascii=False)}'
                    ),
                }
            )
    return out


def parse_assistant(text: str) -> dict:
    """
    把模型输出解析成统一的 assistant message。

    这里是“代码识别模型是否决定调用工具”的地方：
    - 如果模型输出的是 {"type":"tool_call", ...}
    - 我们就把它转成内部的 toolCall block
    - 否则就按普通文本回答处理
    """
    try:
        match = re.search(r"\{.*\}", text, re.S)
        payload = json.loads(match.group(0)) if match else {}
        if payload.get("type") == "tool_call":
            return {
                "role": "assistant",
                "content": [
                    {
                        "type": "toolCall",
                        "id": "call_001",
                        "name": payload["name"],
                        "arguments": payload["arguments"],
                    }
                ],
            }
    except Exception:
        pass

    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def extract_response_text(response, on_text_delta=None) -> str:
    """兼容普通响应与 stream=True 的分片响应，并支持边收边消费文本分片。"""
    if hasattr(response, "choices"):
        text = response.choices[0].message.content or ""
        if text and on_text_delta:
            on_text_delta(text)
        return text

    chunks: list[str] = []
    for chunk in response:
        if not getattr(chunk, "choices", None):
            continue

        delta = chunk.choices[0].delta
        if delta and delta.content:
            chunks.append(delta.content)
            if on_text_delta:
                on_text_delta(delta.content)

    return "".join(chunks)


def stream_to_terminal(text: str) -> None:
    """把模型分片即时写到终端。"""
    sys.stdout.write(text)
    sys.stdout.flush()


async def llm_complete(messages: list[dict], on_text_delta=None) -> str:
    """
    调用 DeepSeek。

    这里是“模型真正做决策”的地方：
    模型会根据 system prompt + 当前上下文，
    决定这次是直接回答，还是先返回 tool_call JSON。
    """

    def _call() -> str:
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=[{"role": "system", "content": SYSTEM}, *to_llm(messages)],
            stream=True,
            reasoning_effort="low",
            extra_body={"thinking": {"type": "disabled"}},
        )
        return extract_response_text(response, on_text_delta=on_text_delta)

    # OpenAI SDK 是阻塞式的，所以放在线程里跑，避免卡住 asyncio。
    return await asyncio.to_thread(_call)


async def run_tool_call(call: dict, tools: dict) -> dict:
    """统一执行工具，方便后续接入 sandbox 和其他 runtime。"""
    tool = tools[call["name"]]
    return await tool["handler"](call["arguments"])


async def agent_loop(user_text: str, verbose: bool = True) -> dict:
    """
    一个最小可运行的 Agent Loop。

    主流程：
    1. 用户消息进入上下文
    2. 调模型生成 assistant message
    3. 如果 assistant 里有 toolCall，就执行工具
    4. 把 tool result 回填到上下文
    5. 再调下一轮模型，直到 assistant 不再请求工具
    """
    started_at = time.perf_counter()
    executed_calls = []
    tool_results = []
    ctx = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": user_text}]}],
        "tools": {
            "get_weather": {
                "handler": get_weather,
                "sandboxed": False,
            },
            "run_python": {
                "handler": run_python_tool,
                "sandboxed": True,
            },
        },
    }

    if verbose:
        print("[agent_start]")
        print("[turn_start]")

    while True:
        # Step 1. 调 LLM，让模型决定当前是直接回答还是先调工具。
        on_text_delta = stream_to_terminal if verbose else None
        if verbose:
            print("[assistant_stream]", end=" ", flush=True)
        raw_text = await llm_complete(ctx["messages"], on_text_delta=on_text_delta)
        if verbose:
            print()
        assistant = parse_assistant(raw_text)
        ctx["messages"].append(assistant)
        calls = [block for block in assistant["content"] if block["type"] == "toolCall"]
        if verbose:
            if calls:
                print("[message_end]", render(assistant))
            else:
                print("[message_end] assistant streamed")

        # Step 2. 从 assistant message 里找出 toolCall。
        if not calls:
            break

        # Step 3. 真正执行工具。
        for call in calls:
            tool_meta = ctx["tools"][call["name"]]
            executed_calls.append(
                {
                    "id": call["id"],
                    "name": call["name"],
                    "arguments": call["arguments"],
                    "sandboxed": tool_meta["sandboxed"],
                }
            )
            if verbose:
                print("[tool_start]", call["name"], call["arguments"])
            result = await run_tool_call(call, ctx["tools"])
            tool_results.append(
                {
                    "tool_call_id": call["id"],
                    "tool_name": call["name"],
                    "details": result["details"],
                }
            )
            if verbose:
                print("[tool_end]", result["details"])

            # Step 4. 把 tool result 回填到上下文，供下一轮模型继续使用。
            tool_result = {
                "role": "toolResult",
                "tool_call_id": call["id"],
                "tool_name": call["name"],
                "content": result["content"],
                "details": result["details"],
                "is_error": not result["details"].get("ok", True),
            }
            ctx["messages"].append(tool_result)
            if verbose:
                print("[message_end]", render(tool_result))

        if verbose:
            print("[turn_end]")
            print("[turn_start]")

    final_answer = render(ctx["messages"][-1])
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    result = {
        "final_answer": final_answer,
        "messages": ctx["messages"],
        "tool_calls": executed_calls,
        "tool_results": tool_results,
        "duration_ms": duration_ms,
    }

    if verbose:
        print("[turn_end]")
        print("[agent_end]")
        print("\nFinal answer:")
        print(final_answer)

    return result


if __name__ == "__main__":
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise RuntimeError("请先设置 DEEPSEEK_API_KEY")

    asyncio.run(agent_loop("今天星期几？北京什么天气？"))
