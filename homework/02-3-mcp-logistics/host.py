"""订单查询 Agent Host（课件 2.3 §7.3 复刻）。

MCP Adapter 把远端工具适配成统一的 ToolDefinition，ToolRuntime 保持唯一
执行入口（校验 → handler → 归一化），Agent Loop 只面向 Tool Result——
Server 从本地函数换成远端 MCP 进程，主链一行不改。

依赖（锁定版本，见课件 §7.1）：
    Python 3.12 / mcp==2.0.0 / jsonschema==4.26.0 / openai==1.109.1

运行方式：
    python host.py            # stdio 传输：自动把 server.py 作为子进程启动
    python host.py --http     # Streamable HTTP：连接已启动的 server
    不配密钥：只跑确定性 MCP 契约测试（成功 / 拒绝 / 失败）。
    配置 DEEPSEEK_API_KEY（可放在 agentStudyCodeProject/.env，经
    python-dotenv 加载；或导出 LLM_BASE_URL / LLM_MODEL 指向自建网关）
    后追加真实模型驱动的 Agent Loop 验收。

单独验证 Streamable HTTP 传输：先 `python server.py --http`，
再 `python host.py --http`（URL 可用 MCP_SERVER_URL 覆盖，
默认 http://127.0.0.1:8000/mcp）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from jsonschema import ValidationError, validate
from mcp import Client, StdioServerParameters, stdio_client
from mcp.client.streamable_http import TransportStreams, streamable_http_client
from openai import AsyncOpenAI


def _load_env_if_present() -> None:
    """Optionally load agentStudyCodeProject/.env (gitignored) if dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.is_file():
        load_dotenv(env_path)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    text: str | None = None
    tool_call: ToolCall | None = None


class LLMProvider(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply: ...


class DeepSeekProvider:
    """复用 OpenAI-compatible 接口；生产项目可替换为阶段一网关。"""

    def __init__(self) -> None:
        self.model = os.getenv("LLM_MODEL", "deepseek-chat")
        self.client = AsyncOpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        message = response.choices[0].message
        if message.tool_calls:
            call = message.tool_calls[0]
            return ModelReply(
                tool_call=ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=json.loads(call.function.arguments or "{}"),
                )
            )
        return ModelReply(text=message.content or "")


ToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    @property
    def model_name(self) -> str:
        """Model-visible projection: FC providers reject '.' in function names
        (pattern ^[a-zA-Z0-9_-]+$), so the qualified name needs an alias."""
        return self.name.replace(".", "_")

    def to_model_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.model_name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolRuntime:
    async def execute(self, call: ToolCall, tools: dict[str, ToolDefinition]) -> dict[str, Any]:
        tool = tools.get(call.name)
        if tool is None:
            # Resolve the model-visible alias (dots replaced) back to the
            # qualified name — the qualified name stays the internal id.
            tool = next((t for t in tools.values() if t.model_name == call.name), None)
        if not tool:
            return {"ok": False, "code": "TOOL_NOT_FOUND"}
        try:
            validate(instance=call.arguments, schema=tool.input_schema)
        except ValidationError as exc:
            # Rejected BEFORE any network call reaches the server.
            return {"ok": False, "code": "INVALID_ARGUMENT", "message": exc.message}
        return await tool.handler(call.id, call.arguments)


def result_payload(result: Any, max_chars: int = 2_000) -> dict[str, Any]:
    if result.is_error:
        text = getattr(result.content[0], "text", "MCP_TOOL_ERROR")
        return {"ok": False, "code": "MCP_TOOL_ERROR", "message": text[:max_chars]}
    value = result.structured_content
    if value is None:
        text = getattr(result.content[0], "text", "")
        value = {"text": text}
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) > max_chars:
        return {"ok": False, "code": "RESULT_TOO_LARGE"}
    return dict(value)


async def discover_tools(client: Client, server_id: str) -> dict[str, ToolDefinition]:
    result = await client.list_tools()
    definitions: dict[str, ToolDefinition] = {}
    for remote in result.tools:
        qualified_name = f"{server_id}.{remote.name}"

        async def handler(
            tool_call_id: str,
            arguments: dict[str, Any],
            remote_name: str = remote.name,
        ) -> dict[str, Any]:
            response = await client.call_tool(remote_name, arguments)
            payload = result_payload(response)
            return {"tool_call_id": tool_call_id, **payload}

        definitions[qualified_name] = ToolDefinition(
            name=qualified_name,
            description=remote.description or "",
            input_schema=remote.input_schema,
            handler=handler,
        )
    return definitions


async def run_agent(
    provider: LLMProvider,
    runtime: ToolRuntime,
    tools: dict[str, ToolDefinition],
    user_input: str,
    max_rounds: int = 4,
) -> str:
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_input}]
    model_tools = [tool.to_model_tool() for tool in tools.values()]
    for round_no in range(1, max_rounds + 1):
        reply = await provider.complete(messages, model_tools)
        if reply.text is not None:
            print("LOOP", round_no, "final")
            return reply.text
        if reply.tool_call is None:
            raise RuntimeError("MODEL_REPLY_INVALID")
        print("LOOP", round_no, "tool_call", reply.tool_call.name)
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": reply.tool_call.id,
                        "type": "function",
                        "function": {
                            "name": reply.tool_call.name,
                            "arguments": json.dumps(
                                reply.tool_call.arguments, ensure_ascii=False
                            ),
                        },
                    }
                ],
            }
        )
        tool_result = await runtime.execute(reply.tool_call, tools)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": reply.tool_call.id,
                "content": json.dumps(tool_result, ensure_ascii=False),
            }
        )
    raise RuntimeError("MAX_ROUNDS_EXCEEDED")


def _build_transport(base_dir: Path) -> AbstractAsyncContextManager[TransportStreams]:
    """Mirror server.py's CLI switch: --http attaches to an already running
    server, otherwise server.py is spawned locally over stdio."""
    if "--http" in sys.argv:
        url = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
        return streamable_http_client(url)
    return stdio_client(
        StdioServerParameters(
            command=sys.executable,
            args=[str(base_dir / "server.py")],
            cwd=base_dir,
        )
    )


async def main() -> None:
    _load_env_if_present()
    base_dir = Path(__file__).resolve().parent
    async with Client(_build_transport(base_dir)) as client:
        tools = await discover_tools(client, "orders")
        resources = await client.list_resources()
        prompts = await client.list_prompts()
        print("PROTOCOL", client.protocol_version)
        print("TOOLS", sorted(tools))
        print("RESOURCES", [str(item.uri) for item in resources.resources])
        print("PROMPTS", [item.name for item in prompts.prompts])

        runtime = ToolRuntime()

        success = await runtime.execute(
            ToolCall("call_success", "orders.get_order", {"order_id": "ord_1001"}),
            tools,
        )
        assert success["status"] == "shipped"
        print("CONTRACT SUCCESS", success["status"])

        rejected = await runtime.execute(
            ToolCall("call_reject", "orders.get_order", {}), tools
        )
        assert rejected["code"] == "INVALID_ARGUMENT"
        print("REJECT", rejected["code"])

        failed = await runtime.execute(
            ToolCall(
                "call_fail", "orders.get_order", {"order_id": "ord_missing"}
            ),
            tools,
        )
        assert failed["code"] == "MCP_TOOL_ERROR"
        print("FAIL", failed["code"])

        # --- 10.1 exercise: get_logistics contract paths (same Runtime, no
        # special-casing — the tool simply flows through discover_tools). ---

        logistics_ok = await runtime.execute(
            ToolCall(
                "call_logistics_ok", "orders.get_logistics", {"order_id": "ord_1001"}
            ),
            tools,
        )
        assert logistics_ok["carrier"] == "SF Express"
        print("LOGISTICS SUCCESS", logistics_ok["carrier"])

        logistics_missing = await runtime.execute(
            ToolCall(
                "call_logistics_missing",
                "orders.get_logistics",
                {"order_id": "ord_1002"},
            ),
            tools,
        )
        # Pending order → stable business error, not an unhandled exception.
        assert logistics_missing["code"] == "MCP_TOOL_ERROR"
        assert "LOGISTICS_NOT_FOUND" in logistics_missing["message"]
        print("LOGISTICS MISSING", logistics_missing["message"][:40])

        logistics_huge = await runtime.execute(
            ToolCall(
                "call_logistics_huge",
                "orders.get_logistics",
                {"order_id": "ord_1003"},
            ),
            tools,
        )
        # Oversized payload normalized by result_payload, never reaches the model.
        assert logistics_huge["code"] == "RESULT_TOO_LARGE"
        print("LOGISTICS TOO_LARGE", logistics_huge["code"])

        if "DEEPSEEK_API_KEY" not in os.environ:
            print("LIVE SKIP：设置 DEEPSEEK_API_KEY 后运行真实 Agent Loop")
            return

        answer = await run_agent(
            provider=DeepSeekProvider(),
            runtime=runtime,
            tools=tools,
            max_rounds=8,
            user_input=(
                "依次检查订单 ord_1001 和 ord_1002：对每个订单先查询状态，"
                "只有已发货（status=shipped）的订单才查询它的物流轨迹。"
                "最后告诉我每个订单的状态，以及已发货订单的物流是否已签收。"
            ),
        )
        print("LIVE", answer)


if __name__ == "__main__":
    asyncio.run(main())
