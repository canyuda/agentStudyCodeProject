"""第 7/8 步的回归测试：HTTP 接口全链路（ASGITransport 直打，无需端口）。

重点覆盖两个验收级异常场景：
- 断线恢复：订阅中断后带 Last-Event-ID 重连，事件续传且模型绝不重跑；
- 取消：HTTP 取消后 SSE 流收到终态，重复取消幂等。
"""

import asyncio

from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from gateway.fake_model import FakeStreamingModel
from gateway.server import create_app
from gateway.sse import SSEDecoder

TERMINALS = {"run.completed", "run.cancelled", "run.failed"}


class CountingModel:
    """包装任意模型，统计 stream() 被调用的次数 —— "断线不重跑"的裁判。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    async def stream(self, messages):
        self.calls += 1
        async for event in self.inner.stream(messages):
            yield event


@pytest_asyncio.fixture
async def ctx():
    """ctx.client：注入计数假模型的应用客户端；ctx.model：调用计数裁判。"""
    model = CountingModel(FakeStreamingModel(
        text="流" * 40, chunk_size=1,
        first_token_delay=0.05, inter_chunk_delay=0.05))
    app = create_app(model=model)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield SimpleNamespace(client=c, model=model)


async def read_sse(client, url, headers=None, stop_after=None, until_terminal=False):
    """订阅并解析 SSE：读 N 个事件断开（stop_after）或读到终态。"""
    decoder, events = SSEDecoder(), []
    async with client.stream("GET", url, headers=headers or {}) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            event = decoder.feed_line(line)
            if event is None:
                continue
            events.append(event)
            if until_terminal and event.type in TERMINALS:
                break
            if stop_after and len(events) >= stop_after:
                break
    return events, decoder.last_event_id


async def create_run(ctx, prompt="测试") -> str:
    response = await ctx.client.post("/v1/runs", json={"prompt": prompt})
    assert response.status_code == 200
    return response.json()["run_id"]


# ---------------------------------------------------------------- 正常链路 --
async def test_create_then_subscribe_to_completion(ctx):
    """创建后订阅：完整收到 created->started->deltas->metrics->completed。"""
    run_id = await create_run(ctx)
    events, _ = await read_sse(ctx.client, f"/v1/runs/{run_id}/events", until_terminal=True)
    types = [e.type for e in events]
    assert types[0] == "run.created"
    assert "run.started" in types
    assert types.count("text.delta") == 40
    assert types[-1] == "run.completed"
    assert [e.seq for e in events] == list(range(len(events)))  # seq 连续
    assert ctx.model.calls == 1


async def test_empty_prompt_rejected(ctx):
    """异常场景：空 prompt 被请求校验拦下（422），不产生 Run。"""
    response = await ctx.client.post("/v1/runs", json={"prompt": ""})
    assert response.status_code == 422


# --------------------------------------------------------- 断线恢复不重跑 --
async def test_reconnect_resumes_without_rerunning_model(ctx):
    """核心验收：中断订阅后重连，从断点续传，模型调用次数仍为 1。"""
    run_id = await create_run(ctx)
    await asyncio.sleep(0.2)

    first, last_seq = await read_sse(
        ctx.client, f"/v1/runs/{run_id}/events", stop_after=3)
    assert last_seq == 2
    await asyncio.sleep(0.2)  # 断线期间 Run 继续跑

    second, _ = await read_sse(
        ctx.client, f"/v1/runs/{run_id}/events",
        headers={"Last-Event-ID": str(last_seq)}, until_terminal=True)
    assert second[0].seq == last_seq + 1          # 从断点的下一个开始
    assert second[-1].type == "run.completed"
    seqs = [e.seq for e in first] + [e.seq for e in second]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))  # 拼接无重复无空洞
    assert ctx.model.calls == 1          # 重连绝不重新调用模型


async def test_after_seq_query_param_channel(ctx):
    """浏览器手动重连通道：?after_seq=N 与请求头效果一致。"""
    run_id = await create_run(ctx)
    await asyncio.sleep(0.2)
    events, _ = await read_sse(
        ctx.client, f"/v1/runs/{run_id}/events?after_seq=2", until_terminal=True)
    assert events[0].seq == 3
    assert ctx.model.calls == 1


# ------------------------------------------------------------------- 取消 --
async def test_cancel_terminates_stream_and_is_idempotent(ctx):
    """取消后订阅收到 run.cancelled 终态；重复取消 accepted=false 且状态不变。"""
    run_id = await create_run(ctx)
    await asyncio.sleep(0.25)

    response = await ctx.client.post(f"/v1/runs/{run_id}/cancel")
    assert response.json()["accepted"] is True

    events, _ = await read_sse(ctx.client, f"/v1/runs/{run_id}/events", until_terminal=True)
    assert events[-1].type == "run.cancelled"

    again = await ctx.client.post(f"/v1/runs/{run_id}/cancel")
    assert again.json() == {"run_id": run_id, "status": "cancelled", "accepted": False}


async def test_cancel_mid_stream_stops_deltas(ctx):
    """订阅中途取消：Run 提前进入终态，delta 数远小于全量（真停止）。"""
    run_id = await create_run(ctx)
    await asyncio.sleep(0.25)
    await ctx.client.post(f"/v1/runs/{run_id}/cancel")

    # 取消是异步生效的：轮询等终态事件落账（教学口径：以账本为准，而非响应为准）
    for _ in range(20):
        body = (await ctx.client.get(f"/v1/runs/{run_id}")).json()
        if body["status"] == "cancelled":
            break
        await asyncio.sleep(0.05)
    # 全量应为 40 delta + created/started/metrics/cancelled 等 5 个框架事件
    assert body["status"] == "cancelled"
    assert 0 < body["event_count"] < 40 + 5


# --------------------------------------------------------------- 404/400 --
async def test_unknown_run_404_and_bad_header_400(ctx):
    await create_run(ctx)
    response = await ctx.client.get("/v1/runs/run_none/events")
    assert response.status_code == 404
    ok = await create_run(ctx)
    response = await ctx.client.get(f"/v1/runs/{ok}/events",
                                headers={"Last-Event-ID": "abc"})
    assert response.status_code == 400
