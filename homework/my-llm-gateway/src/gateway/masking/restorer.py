"""流式还原器：带"待定尾部缓冲"的占位符还原（需求 10.6，场景 13）。

【教学定位：流式脱敏最难的一块】
非流式响应可以等全文到齐再整体还原；流式却是一个个 SSE 增量到达，
一个占位符完全可能被切在两个块里：

    块1: "联系 ⟦EMA"      块2: "IL_1⟧ 处理"

如果见到增量就立刻转发，客户端会看到占位符碎片 ⟦EMA（既泄漏了
"这里有个占位符"的结构，还原也无从做起）。

【解法：待定尾部缓冲（pending tail buffer）】
每收到一个块：
1. 追加到缓冲区
2. 检查缓冲区【末尾】是否是"可能还在长成中的占位符前缀"
   （正则 ⟦[A-Z0-9_]*$ 能匹配 "⟦"、"⟦EM"、"⟦EMAIL_1"…）
3. 前缀之前的全部内容 = 证明安全，还原后立即下发；
   前缀本身留在缓冲区，等下一个块拼接后继续判断
4. 流结束时 flush() 冲出残留缓冲（此时不可能再有后续块）

效果：客户端收到的永远是完整还原后的文本，占位符碎片零泄漏。

【丢失占位符的处理】模型改写/丢弃占位符时（如 [EMAIL_1]），
restore_text 认不出的占位符原样保留——绝不把原值重新发给模型修补。
"""

from __future__ import annotations

import re

from .masker import PLACEHOLDER_RE, MaskSession

# 锚定结尾的前缀模式：缓冲区末尾若匹配，说明一个占位符可能"还没到齐"。
# 字符类 [A-Z0-9_]* 覆盖类型名、下划线和数字（⟦ 单独出现也算前缀）。
_PARTIAL_PLACEHOLDER = re.compile(r"⟦[A-Z0-9_]*$")


class StreamRestorer:
    """有状态的流式还原器：每路流一个实例（pipeline.stream 中创建）。"""

    def __init__(self, session: MaskSession) -> None:
        self._session = session  # 与去程共享同一个会话（映射表一致）
        self._buffer = ""  # 待定尾部缓冲

    def feed(self, chunk: str) -> str:
        """喂入一个增量块，返回"现在可以安全下发"的已还原文本。

        返回可能为空串（比如整块都拼进了未完成的占位符前缀）——
        pipeline 会跳过空串不 yield，客户端无感知。
        """
        self._buffer += chunk
        match = _PARTIAL_PLACEHOLDER.search(self._buffer)
        if match is None:
            # 末尾不可能构成占位符前缀：全部安全，清空缓冲
            safe, self._buffer = self._buffer, ""
        else:
            # 切成两段：前缀之前的安全下发，前缀起留在缓冲等下一块
            safe, self._buffer = self._buffer[: match.start()], self._buffer[match.start() :]
        return self._session.restore_text(safe)

    def flush(self) -> str:
        """流结束时冲出残留缓冲（残留要么是完整占位符，要么是普通文本）。"""
        rest, self._buffer = self._buffer, ""
        return self._session.restore_text(rest)


def complete_placeholders_in(text: str) -> int:
    """统计文本中完整占位符的个数（测试辅助函数）。"""
    return len(PLACEHOLDER_RE.findall(text))
