"""第 1 步的回归测试：RunEvent 统一事件模型。"""

import json

import pytest

from gateway.events import (
    TERMINAL_EVENT_TYPES,
    RunEvent,
    is_terminal_event,
)


def test_event_roundtrip_keeps_all_fields():
    """事件经过 dict -> JSON 字符串 -> dict 往返后完全相等（SSE/SQLite 依赖此性质）。"""
    event = RunEvent(run_id="run_x", seq=3, type="text.delta",
                     ts=1700000000.5, data={"delta": "你好"})
    restored = RunEvent.from_dict(json.loads(json.dumps(event.to_dict())))
    assert restored == event


def test_event_is_immutable():
    """frozen=True：事件是历史事实，产生后不许改（审计前提）。"""
    event = RunEvent(run_id="run_x", seq=0, type="run.started")
    with pytest.raises(Exception):
        event.seq = 99  # type: ignore[misc]


def test_terminal_event_detection():
    """只有三种终态事件会被判定为终态。"""
    assert is_terminal_event(RunEvent("r", 9, "run.completed", 0, {}))
    assert is_terminal_event(RunEvent("r", 9, "run.cancelled", 0, {}))
    assert is_terminal_event(RunEvent("r", 9, "run.failed", 0, {}))
    assert not is_terminal_event(RunEvent("r", 1, "text.delta", 0, {}))
    assert not is_terminal_event(RunEvent("r", 2, "checkpoint.saved", 0, {}))
    assert TERMINAL_EVENT_TYPES == {"run.completed", "run.cancelled", "run.failed"}


def test_from_dict_requires_run_id():
    """缺少关键字段时 fail fast（显式 KeyError，而不是静默造出残缺事件）。"""
    with pytest.raises(KeyError):
        RunEvent.from_dict({"seq": 0, "type": "run.started"})
