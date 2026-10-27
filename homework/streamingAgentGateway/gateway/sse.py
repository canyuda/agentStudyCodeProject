"""步骤 2：SSE 编解码 —— RunEvent 与 text/event-stream 之间的双向转换。

SSE（Server-Sent Events）协议速记：
- 一个事件块 = 若干属性行 + 一个空行，例如：
      id: 3
      event: text.delta
      data: {"run_id": "run_x", "seq": 3, ...}

      （结尾空行才算块结束）
- 注释行以 ":" 开头，接收端直接忽略 —— 心跳就用它实现（: keep-alive）。
- "data:" 可以出现多行，接收端用 "\\n" 拼接后再解析。
- 断线重连时，浏览器 EventSource 自动带上上次收到的 id（Last-Event-ID 头）。

本模块职责：
- encode：RunEvent -> SSE 文本块（服务端推送用）；
- decode：SSE 文本块 -> RunEvent（CLI 客户端 / 测试用）；
- parse_last_event_id：解析重连头，算出该从哪个 seq 之后开始补发。

编解码只依赖 events.py，不涉及任何模型 SDK —— 这也是验收标准
"不依赖模型 SDK"的第一层含义：传输层自包含、可离线测试。
"""

from __future__ import annotations

from .events import RunEvent

# 换行符固定用 \n：SSE 规范要求，跨平台一致
_SSE_NEWLINE = "\n"
_BLOCK_END = _SSE_NEWLINE * 2  # 一个事件块以空行（两个换行）结尾


# ---------------------------------------------------------------------- 编码 --
def encode_sse(event: RunEvent) -> str:
    """把一个 RunEvent 编码成一个完整的 SSE 事件块。

    保证：json.dumps 会把中文正常输出（ensure_ascii=False），
    且字符串内部换行会被转义成 \\n，因此 data 恒为单行，解码端无需猜测。
    """
    import json

    payload = json.dumps(event.to_dict(), ensure_ascii=False)
    return (
        f"id: {event.seq}{_SSE_NEWLINE}"
        f"event: {event.type}{_SSE_NEWLINE}"
        f"data: {payload}{_BLOCK_END}"
    )


def encode_heartbeat() -> str:
    """心跳块：注释行 + 空行。用于订阅长时间无事件时保持连接不被中间层掐断。"""
    return f": keep-alive{_SSE_NEWLINE * 2}"


# ---------------------------------------------------------------------- 解码 --
class SSEDecoder:
    """增量式 SSE 解码器：一行一行喂，遇到空行就"组包"出一个 RunEvent。

    使用方式（第 8 步 CLI 客户端会这样用）：

        decoder = SSEDecoder()
        for line in response.iter_lines():
            event = decoder.feed_line(line)
            if event is not None:
                handle(event)   # 心跳等纯注释块会返回 None，跳过即可

    为什么做成"按行喂"而不是"整块喂"：
    真实网络流是任意切割的字节，事件块的边界（空行）可能落在任意位置，
    按行缓冲是最贴近浏览器 EventSource 行为的最小实现。
    """

    def __init__(self) -> None:
        self._data_lines: list[str] = []
        self._event_type: str | None = None
        self._last_event_id: int | None = None

    @property
    def last_event_id(self) -> int | None:
        """最近一次完整解析到的 id，客户端断线时可以拿它做恢复游标。"""
        return self._last_event_id

    def feed_line(self, line: str) -> RunEvent | None:
        """喂入一行（不含换行符）。

        返回：
        - RunEvent：一个完整事件块刚刚结束；
        - None：块还没结束，或者这是一个纯心跳/注释块。
        """
        # 空行 = 事件块结束，触发组包
        if line == "":
            return self._flush()

        # 注释行（心跳）：直接忽略
        if line.startswith(":"):
            return None

        # 属性行统一是 "field: value"；按规范，冒号后只剥掉一个前导空格
        if line.startswith("data:"):
            self._data_lines.append(line[5:].removeprefix(" "))
        elif line.startswith("event:"):
            self._event_type = line[6:].strip()
        elif line.startswith("id:"):
            raw = line[3:].strip()
            # id 非法时按规范忽略该字段（浏览器行为），不中断整条流
            try:
                self._last_event_id = int(raw)
            except ValueError:
                pass
        # 其余字段（retry: 等）教学项目不处理，静默忽略
        return None

    def _flush(self) -> RunEvent | None:
        """组包：把缓存的 data 行拼成 JSON 还原 RunEvent。"""
        if not self._data_lines:
            # 没有 data 的块（比如心跳）不需要组包，但状态要复位
            self._reset()
            return None

        import json

        raw = json.loads("\n".join(self._data_lines))
        event = RunEvent.from_dict(raw)
        self._reset()
        return event

    def _reset(self) -> None:
        self._data_lines = []
        self._event_type = None


def parse_last_event_id(header_value: str | None) -> int:
    """解析 Last-Event-ID 请求头，返回"已经收到的最大 seq"。

    约定：返回 -1 表示"什么都没收到过"，服务端应从 seq=0 开始补发。
    非法值（非整数）在这里就 fail fast，由路由层转成 400。
    """
    if header_value is None or header_value.strip() == "":
        return -1
    return int(header_value.strip())  # 非法值直接抛 ValueError，调用方负责兜住
