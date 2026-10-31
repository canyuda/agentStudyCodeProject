"""表驱动的敏感信息识别规则（数据脱敏的第 1 层：识别）。

【教学定位】
"表驱动"是本模块的核心思想：规则是【数据】（一个列表），不是散落在
代码里的 if-else。带来两个直接好处：
1. 每条规则可独立启停（配置 masking.rules.xxx.enabled: false）
2. 扩展新规则（如"订单号"）只需往 _RULES 追加一项，零逻辑改动

【执行顺序：更具体/更长的规则优先】（需求 10.3）
顺序为：密钥 -> 银行卡 -> 身份证 -> 手机号 -> 邮箱
原因：短规则会"截断"长号码——大陆手机号（11 位）是身份证号（18 位）
的子串；若手机号规则先跑，一个身份证会被拆成"手机段+残段"两个占位符，
既破坏数据又给模型错误暗示。长规则先匹配、占住整段，短规则自然跳过。

【降误报：校验器】
光靠"长得像"会大量误伤普通数字串。身份证加 GB11643 校验码验证、
银行卡加 Luhn 校验，两者都是纯数学校验，把误报率压到实用水平——
这是"正则粗筛 + 语义校验"的经典组合拳。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

# ---------------------------------------------------------------- 校验算法


def luhn_ok(digits: str) -> bool:
    """Luhn 算法（银行卡标准校验）。

    从右往左：偶数位(0-based 奇数下标)乘 2 超 9 减 9，求和后模 10 应为 0。
    用途：13-19 位数字串只有通过 Luhn 才被认定为银行卡——
    否则订单号、时间戳都会被误伤。
    """
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:  # 从右数第 2、4、6... 位
            d *= 2
            if d > 9:
                d -= 9  # 乘 2 后 10~18 折叠回 1~9
        total += d
    return total % 10 == 0


# 中国大陆身份证（GB 11643-1999）校验码算法：
# 前 17 位分别乘以固定权重，求和模 11，查表得到第 18 位（校验码，可为 X）
_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CHECK = "10X98765432"  # 模 11 的余数 0~10 对应的校验字符


def id_card_ok(text: str) -> bool:
    """18 位身份证校验码验证（末位 X 大小写均可）。"""
    body, check = text[:17], text[17].upper()
    total = sum(int(ch) * w for ch, w in zip(body, _ID_WEIGHTS, strict=False))
    return _ID_CHECK[total % 11] == check


# ---------------------------------------------------------------- 规则表


@dataclass(frozen=True)
class MaskRule:
    """一条识别规则 = 名称 + 占位符类型 + 正则 + 可选校验器。

    validator 的语义：正则命中后【再】调用它做语义校验，
    返回 False 则该命中不算敏感信息（降误报的关键机制）。
    """

    name: str  # 配置里的开关键名（如 "email"）
    placeholder_type: str  # 生成的占位符类型段（如 "EMAIL" -> ⟦EMAIL_1⟧）
    pattern: re.Pattern[str]
    validator: Callable[[str], bool] | None = None
    enabled: bool = True

    def iter_matches(self, text: str) -> Iterator[re.Match[str]]:
        """产出所有"通过校验"的匹配（生成器：调用方按需消费）。"""
        for match in self.pattern.finditer(text):
            if self.validator is None or self.validator(match.group(0)):
                yield match


# 规则表：顺序即执行顺序（长/具体在前，见模块 docstring）。
# 正则细节：
#   secret_key  sk-/sk_ 前缀 8~64 位令牌串，覆盖主流大模型厂商密钥风格；
#               \b 词边界防止匹配到更长串中间
#   bank_card   13-19 位数字、容忍空格/短横线分组；(?<![\d-])/(?![\d-])
#               否定环视保证不在更长数字串内部；Luhn 校验兜底
#   id_card     17 位数字 + 末位数字/X/x，前后不能是数字；校验码兜底
#   phone       大陆手机号 1[3-9] 开头恰好 11 位，前后需非数字边界
#               （否则 18 位身份证中间 11 位也会被当成手机号）
#   email       通用邮箱正则
_RULES: list[MaskRule] = [
    MaskRule(
        name="secret_key",
        placeholder_type="SECRET",
        pattern=re.compile(r"\bsk[-_][A-Za-z0-9_-]{8,64}\b"),
    ),
    MaskRule(
        name="bank_card",
        placeholder_type="BANK",
        pattern=re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])"),
        # 校验器：剥掉分组符后长度须在 13~19 且通过 Luhn
        validator=lambda m: (
            (13 <= len(digits := re.sub(r"[ -]", "", m)) <= 19) and luhn_ok(digits)
        ),
    ),
    MaskRule(
        name="id_card",
        placeholder_type="IDCARD",
        pattern=re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
        validator=id_card_ok,
    ),
    MaskRule(
        name="phone",
        placeholder_type="PHONE",
        pattern=re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ),
    MaskRule(
        name="email",
        placeholder_type="EMAIL",
        pattern=re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    ),
]


def build_rules(enabled_map: dict[str, bool] | None = None) -> list[MaskRule]:
    """按配置构建生效规则表：enabled_map 里显式 False 的被剔除，
    未提及的默认启用（新增规则无需改配置就生效，向后兼容）。"""
    enabled_map = enabled_map or {}
    return [r for r in _RULES if enabled_map.get(r.name, True)]


RULE_ORDER = [r.name for r in _RULES]  # 供文档/测试引用的执行顺序
