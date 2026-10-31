"""请求级脱敏会话：敏感值 -> 占位符（去程）与占位符 -> 原值（回程）。

【教学定位：脱敏的第 2 层（映射）】
rules.py 负责"识别"，本模块负责"替换与还原"，两个关注点分离。

【占位符设计（需求 10.4）——为什么是 ⟦TYPE_N⟧】
格式：⟦类型_序号⟧，如 ⟦EMAIL_1⟧、⟦PHONE_2⟧（⟦⟧ 是 U+27E6/U+27E7
数学白方括号）。四条硬要求与对应的字符选择理由：
1. 不易被模型改写 —— ⟦⟧ 在自然语料中几乎不出现，模型倾向原样保留
2. 不与正常文本冲突 —— 用户输入里出现 ⟦⟧ 的概率可忽略
3. UTF-8 安全 —— 三个字节的标准多字节编码，不会截断成乱码
4. 能穿越 SSE 分块 —— 纯文本，不依赖任何传输层转义

【映射的两个不变式】
- 同一原值在一次请求内【始终】映射到同一占位符（模型看到的上下文
  保持一致：同一个 ⟦EMAIL_1⟧ 指同一个人）
- 不同值分配不同占位符（序号递增保证）

【隐私红线（需求 10.4/10.7）】
双向映射表只存在于本会话对象（请求内存作用域），请求结束随对象
一起被回收——绝不写日志、绝不进账本、绝不持久化。账本只拿 hits
（按类型计数），连内容长度都不留。
"""

from __future__ import annotations

import re

from .rules import MaskRule, build_rules

PLACEHOLDER_TEMPLATE = "⟦{type}_{n}⟧"  # 如 ⟦EMAIL_1⟧
PLACEHOLDER_RE = re.compile(r"⟦([A-Z]+)_\d+⟧")


class MaskSession:
    """一次请求的脱敏会话（pipeline 在 _prepare 中创建，挂在 CallState 上）。"""

    def __init__(self, rules: list[MaskRule] | None = None, enabled_map: dict[str, bool] | None = None) -> None:
        # 规则表既可直接传入（pipeline 用），也可按启停映射构建（单测用）
        self._rules = rules if rules is not None else build_rules(enabled_map)
        self._to_placeholder: dict[str, str] = {}  # 原值 -> 占位符
        self._to_original: dict[str, str] = {}  # 占位符 -> 原值（回程用）
        self._counters: dict[str, int] = {}  # 各类型已发放的序号
        self.hits: dict[str, int] = {}  # 按类型计数的命中数（仅计数，绝无内容）

    # ---------------------------------------------------------------- 去程

    def mask_text(self, text: str) -> str:
        """把文本中所有敏感片段替换为稳定占位符。

        算法分三步（刻意在"原文坐标"上工作，避免边替换边偏移的坑）：
        1. 按规则顺序（长/具体在前）收集所有命中区间，记录已占用区间
        2. 与已占用区间重叠的后续命中直接丢弃（身份证已占住整段，
           其内部的手机号形态子串不再单独处理——长短规则协同的关键）
        3. 区间按位置排序后一次性拼接输出，同时登记占位符映射

        已替换出的占位符（⟦EMAIL_1⟧ 等）不可能再命中任何数字/邮箱
        正则，因此后续规则不会破坏先前的替换结果。
        """
        spans: list[tuple[int, int, str, MaskRule]] = []  # (起, 止, 原文, 规则)
        taken: list[tuple[int, int]] = []  # 已被更长匹配占用的区间
        for rule in self._rules:  # longest/most-specific first
            for match in rule.iter_matches(text):
                start, end = match.span()
                if any(start < e and s < end for s, e in taken):
                    continue  # 与更长匹配重叠：放弃（避免短规则截断长号码）
                spans.append((start, end, match.group(0), rule))
                taken.append((start, end))
        if not spans:
            return text  # 快路径：无命中直接返回原文
        spans.sort(key=lambda s: s[0])  # 按出现位置排序
        parts: list[str] = []
        cursor = 0
        for start, end, original, rule in spans:
            parts.append(text[cursor:start])  # 命中前的普通文本
            parts.append(self._placeholder_for(rule.placeholder_type, original))
            # 命中数按"出现次数"计（同一值出现 2 次 = 2 次命中）
            self.hits[rule.placeholder_type] = self.hits.get(rule.placeholder_type, 0) + 1
            cursor = end
        parts.append(text[cursor:])  # 最后一段普通文本
        return "".join(parts)

    def _placeholder_for(self, ptype: str, original: str) -> str:
        """为原值取（或首次创建）占位符——两个不变式都在这里保证。"""
        existing = self._to_placeholder.get(original)
        if existing is not None:
            return existing  # 不变式 1：同值同占位符
        n = self._counters.get(ptype, 0) + 1  # 不变式 2：不同值序号递增
        self._counters[ptype] = n
        token = PLACEHOLDER_TEMPLATE.format(type=ptype, n=n)
        self._to_placeholder[original] = token
        self._to_original[token] = original
        return token

    # ---------------------------------------------------------------- 回程

    def restore_text(self, text: str) -> str:
        """把文本中的已知占位符替换回原值。

        未知的占位符（模型改写/编造的，如 [EMAIL_1]）原样保留——
        【绝不】把原值重新发给模型修补（需求 10.6 的红线）。
        dict.get 的默认值=匹配原文本身，一行实现"认识就换、不认识保留"。
        """
        return PLACEHOLDER_RE.sub(lambda m: self._to_original.get(m.group(0), m.group(0)), text)

    def known(self) -> dict[str, str]:
        """当前映射表视图（仅供调试/测试检查，勿用于日志输出）。"""
        return dict(self._to_original)
