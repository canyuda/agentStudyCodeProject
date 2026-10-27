"""第 2 步的回归测试：SSE 编解码。"""

import pytest

from gateway.events import RunEvent
from gateway.sse import SSEDecoder, encode_heartbeat, encode_sse, parse_last_event_id


def decode_block(block: str) -> RunEvent | None:
    """把一个完整 SSE 块喂给解码器（含结尾空行触发组包）。"""
    decoder = SSEDecoder()
    result = None
    for line in block.split("\n")[:-1]:  # split 会多出一个空串，去掉后手动喂空行
        event = decoder.feed_line(line)
        if event is not None:
            result = event
    return result


def test_encode_decode_roundtrip_with_chinese():
    """中文 delta 往返不丢、不乱码。"""
    event = RunEvent("run_x", 3, "text.delta", 1700000000.0, {"delta": "你好，世界"})
    assert decode_block(encode_sse(event)) == event


def test_newline_inside_data_survives():
    """正文里的真实换行经 JSON 转义后仍为单行 data，解码后还原。"""
    event = RunEvent("run_x", 4, "run.completed", 0.0, {"text": "第一行\n第二行"})
    assert decode_block(encode_sse(event)) == event


def test_heartbeat_is_ignored_and_pollutes_nothing():
    """心跳块（注释行）被忽略，且不影响后续事件的解析。"""
    decoder = SSEDecoder()
    assert decoder.feed_line(": keep-alive") is None
    assert decoder.feed_line("") is None  # 空行组包：无 data -> 仍是 None
    # 紧接着解析一个正常事件，状态没有被心跳污染
    event = RunEvent("run_x", 0, "run.started", 0.0, {})
    for line in encode_sse(event).split("\n")[:-1]:
        got = decoder.feed_line(line)
    assert got == event
    assert decoder.last_event_id == 0


def test_multi_data_lines_are_joined():
    """按 SSE 规范手写多行 data，解码端用 \\n 拼接后仍能还原 JSON。"""
    import json
    payload = json.dumps({"run_id": "run_x", "seq": 1, "type": "run.started",
                          "ts": 0.0, "data": {}}, ensure_ascii=False)
    half = len(payload) // 2
    block = f"id: 1\nevent: run.started\ndata: {payload[:half]}\ndata: {payload[half:]}\n\n"
    assert decode_block(block) == RunEvent("run_x", 1, "run.started", 0.0, {})


def test_last_event_id_parsing():
    """Last-Event-ID 解析：None/空 -> -1，合法 -> int，非法 -> ValueError。"""
    assert parse_last_event_id(None) == -1
    assert parse_last_event_id("") == -1
    assert parse_last_event_id(" 7 ") == 7
    with pytest.raises(ValueError):
        parse_last_event_id("abc")  # fail fast，路由层转 400


def test_heartbeat_block_format():
    """心跳块格式固定：注释行 + 空行。"""
    assert encode_heartbeat() == ": keep-alive\n\n"
