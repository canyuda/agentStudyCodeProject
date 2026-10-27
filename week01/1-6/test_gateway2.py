import asyncio
import os
from dataclasses import replace
from typing import Any

import httpx

import gateway


async def request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> httpx.Response:
    return await client.request(method, path, json=payload)


def expect(condition: bool, title: str, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{title} 失败: {detail}")
    print(f"通过: {title}")


async def main() -> None:
    primary_key = os.getenv("DEEPSEEK_API_KEY")
    if not primary_key:
        raise RuntimeError("未检测到 DEEPSEEK_API_KEY，无法执行真实 DeepSeek 集成测试")

    original_configs = gateway.MODEL_CONFIGS.copy()
    original_traces = gateway.CALL_TRACES.copy()
    original_backup_key = os.getenv("DEEPSEEK_BACKUP_API_KEY")
    gateway.CALL_TRACES.clear()
    os.environ.setdefault("DEEPSEEK_BACKUP_API_KEY", primary_key)

    transport = httpx.ASGITransport(app=gateway.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://gateway-test", timeout=90) as client:
            normal = await request(client, "POST", "/v1/llm", {
                "model": "general-primary",
                "messages": [{"role": "user", "content": "只回复 PONG，不要解释。"}],
                "prompt": {
                    "name": "knowledge_decision",
                    "version": "v1",
                    "variables": {"product_name": "Gateway 集成测试"},
                },
            })
            expect(normal.status_code == 200, "FastAPI 统一非流式入口", normal.text)
            normal_body = normal.json()
            expect(bool(normal_body["request_id"]), "统一响应包含 request_id")
            expect(normal_body["model"] == "general-primary", "平台模型名映射为统一响应")
            expect(normal_body["usage"]["input_tokens"] >= 0 and normal_body["usage"]["output_tokens"] >= 0, "返回 Token 用量")
            expect(normal_body["latency_ms"] >= 0 and normal_body["attempts"] >= 1, "返回耗时和尝试次数")
            print(f"模型输出（普通调用）: {normal_body['content']}")

            structured = await request(client, "POST", "/v1/llm", {
                "model": "general-primary",
                "messages": [{"role": "user", "content": "返回 JSON：{\"city\": \"Beijing\"}。"}],
                "response_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string", "const": "Beijing"}},
                    "required": ["city"],
                    "additionalProperties": False,
                },
            })
            expect(structured.status_code == 200, "Structured Output 请求与出口 Schema 校验", structured.text)
            expect(structured.json()["parsed"] == {"city": "Beijing"}, "Structured Output 返回已解析对象")
            print(f"模型输出（结构化原文）: {structured.json()['content']}")
            print(f"模型输出（结构化解析）: {structured.json()['parsed']}")

            streamed = await request(client, "POST", "/v1/llm/stream", {
                "model": "general-primary",
                "messages": [{"role": "user", "content": "只回复 STREAM_OK。"}],
            })
            expect(streamed.status_code == 200, "Streaming HTTP 入口", streamed.text)
            expect(streamed.headers["content-type"].startswith("text/event-stream"), "SSE 响应类型")
            expect('"type": "content.delta"' in streamed.text, "逐块转发 content.delta")
            expect('"type": "response.completed"' in streamed.text, "流式完成事件")
            print("模型输出（流式 SSE）:")
            print(streamed.text.rstrip())

            invalid_request = await request(client, "POST", "/v1/llm", {
                "model": "general-primary",
                "messages": [{"role": "user", "content": "校验"}],
                "unknown_field": True,
            })
            expect(invalid_request.status_code == 422, "Pydantic 请求入口禁止未知字段", invalid_request.text)

            invalid_combination = await request(client, "POST", "/v1/llm", {
                "model": "general-primary",
                "stream": True,
                "messages": [{"role": "user", "content": "校验"}],
                "response_schema": {"type": "object"},
            })
            expect(invalid_combination.status_code == 422, "禁止 Streaming 与 Structured Output 混用", invalid_combination.text)

            missing_variable = await request(client, "POST", "/v1/llm", {
                "model": "general-primary",
                "messages": [{"role": "user", "content": "模板校验"}],
                "prompt": {"name": "knowledge_decision", "version": "v1", "variables": {}},
            })
            expect(missing_variable.status_code == 400, "Prompt 模板缺失变量被拦截", missing_variable.text)
            expect(missing_variable.json()["detail"]["code"] == "missing_prompt_variable", "Prompt 返回稳定错误码")

            unknown_model = await request(client, "POST", "/v1/llm", {
                "model": "not-allowed",
                "messages": [{"role": "user", "content": "模型校验"}],
            })
            expect(unknown_model.status_code == 400, "模型白名单校验", unknown_model.text)

            gateway.MODEL_CONFIGS["general-primary"] = replace(
                original_configs["general-primary"],
                base_url="http://127.0.0.1:1/v1",
            )
            fallback = await request(client, "POST", "/v1/llm", {
                "model": "general-primary",
                "messages": [{"role": "user", "content": "只回复 FALLBACK_OK。"}],
                "timeout_seconds": 5,
            })
            expect(fallback.status_code == 200, "超时/连接失败后自动 fallback", fallback.text)
            expect(fallback.json()["model"] == "general-backup", "响应标识实际备用模型")
            expect(fallback.json()["attempts"] == 3, "主模型重试一次后切换备用模型")
            print(f"模型输出（备用模型）: {fallback.json()['content']}")
            gateway.MODEL_CONFIGS.clear()
            gateway.MODEL_CONFIGS.update(original_configs)

            traces = await request(client, "GET", "/v1/traces")
            expect(traces.status_code == 200, "调用审计查询入口", traces.text)
            trace_items = traces.json()
            expect(any(item["prompt_name"] == "knowledge_decision" for item in trace_items), "审计记录 Prompt 名称和版本")
            expect(any(item["actual_model"] == "general-backup" and item["attempts"] == 3 for item in trace_items), "审计记录实际模型与 fallback 尝试次数")
            expect(all("content" not in item for item in trace_items), "审计记录不保存模型回答")
            expect(any(item["cost_usd"] >= 0 and item["latency_ms"] >= 0 for item in trace_items), "审计记录成本和延迟")
    finally:
        gateway.MODEL_CONFIGS.clear()
        gateway.MODEL_CONFIGS.update(original_configs)
        gateway.CALL_TRACES.clear()
        gateway.CALL_TRACES.extend(original_traces)
        if original_backup_key is None:
            os.environ.pop("DEEPSEEK_BACKUP_API_KEY", None)
        else:
            os.environ["DEEPSEEK_BACKUP_API_KEY"] = original_backup_key

    print("真实 DeepSeek Gateway 集成测试全部通过")


if __name__ == "__main__":
    asyncio.run(main())
