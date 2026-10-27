import json
from collections.abc import AsyncIterator

import httpx

import gateway


class FakeProvider:
    def __init__(self, content: str = "正常结果", failures: int = 0) -> None:
        self.content = content
        self.failures = failures
        self.complete_calls: list[str] = []

    async def complete(self, config, messages, timeout_seconds, response_schema):
        self.complete_calls.append(config.provider_model)
        if self.failures:
            self.failures -= 1
            raise TimeoutError("temporary upstream timeout")
        return self.content, gateway.Usage(input_tokens=11, output_tokens=7)

    async def stream(self, config, messages, timeout_seconds) -> AsyncIterator[str]:
        yield "第"
        yield "一块"


async def request(method: str, path: str, payload: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=gateway.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, json=payload)


async def main() -> None:
    original_provider = gateway.provider
    gateway.CALL_TRACES.clear()
    try:
        fake = FakeProvider()
        gateway.provider = fake
        response = await request("POST", "/v1/llm", {
            "model": "general-primary",
            "messages": [{"role": "user", "content": "hello"}],
            "prompt": {
                "name": "knowledge_decision",
                "version": "v1",
                "variables": {"product_name": "测试助手"},
            },
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["content"] == "正常结果"
        assert body["usage"] == {"input_tokens": 11, "output_tokens": 7}
        assert body["attempts"] == 1

        structured = await request("POST", "/v1/llm", {
            "model": "general-primary",
            "messages": [{"role": "user", "content": "hello"}],
            "response_schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        })
        assert structured.status_code == 502
        assert structured.json()["detail"]["code"] == "invalid_json"

        gateway.provider = FakeProvider(content=json.dumps({"answer": "ok"}))
        structured = await request("POST", "/v1/llm", {
            "model": "general-primary",
            "messages": [{"role": "user", "content": "hello"}],
            "response_schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        })
        assert structured.status_code == 200, structured.text
        assert structured.json()["parsed"] == {"answer": "ok"}

        retry_provider = FakeProvider(failures=2)
        gateway.provider = retry_provider
        retried = await request("POST", "/v1/llm", {
            "model": "general-primary",
            "messages": [{"role": "user", "content": "retry"}],
        })
        assert retried.status_code == 200, retried.text
        assert retried.json()["model"] == "general-backup"
        assert retried.json()["attempts"] == 3

        gateway.provider = FakeProvider()
        streamed = await request("POST", "/v1/llm/stream", {
            "model": "general-primary",
            "messages": [{"role": "user", "content": "stream"}],
        })
        assert streamed.status_code == 200
        assert streamed.headers["content-type"].startswith("text/event-stream")
        assert '"type": "content.delta"' in streamed.text
        assert '"type": "response.completed"' in streamed.text

        invalid = await request("POST", "/v1/llm", {
            "model": "general-primary",
            "messages": [{"role": "user", "content": "invalid"}],
            "unexpected": True,
        })
        assert invalid.status_code == 422

        traces = await request("GET", "/v1/traces")
        assert traces.status_code == 200
        assert any(trace["status"] == "success" for trace in traces.json())
        print("Gateway verification passed")
    finally:
        gateway.provider = original_provider


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
