"""结构化输出的本地校验（纵深防御）+ 修复指令生成。

【教学定位：为什么网关还要"再校验一次"？】
上游（如 Responses 协议）声称原生支持 JSON Schema 约束，但：
- 模型不是确定性程序，"严格模式"仍有小概率跑偏
- 协议透传链路上任何一环（代理、网关、适配器翻译）都可能引入偏差
所以需求 5.2 明确要求纵深防御：无论上游怎么说，网关本地用标准
jsonschema 校验器做二次校验，不通过就进修复闭环。

【修复闭环的关键：错误要"精确到可行动"】
修复指令里给模型的错误不是笼统的"格式不对"，而是：
    schema violation at /items/0/qty: 'x' is not of type 'integer'
      ^ 出错路径（JSON Pointer 风格）         ^ 具体原因
配上完整目标 Schema 一起回填，模型才知道往哪改、改成什么样。
实验证明：带精确错误的修复成功率远高于"请重试"式指令。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import jsonschema

# 匹配"整体被一对 markdown 代码围栏包裹"的输出：
# ```json\n{...}\n``` 或 ```\n{...}\n```（语言标记可选）。
# 模型爱用围栏包 JSON 是普遍习惯，先剥掉再校验（需求 5.2 的解析容错）。
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def extract_json_text(text: str) -> str:
    """剥掉一层 markdown 代码围栏；不是围栏包裹则原样返回。"""
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


@dataclass
class ValidationResult:
    """校验结果：ok=False 时 error 是"路径 + 原因"的精确描述。

    json_text 始终携带"剥围栏后的候选 JSON 文本"——修复成功时
    pipeline 用它替换模型原始输出，客户端拿到干净的纯 JSON。
    """

    ok: bool
    value: Any = None  # 解析成功时的 Python 对象
    error: str | None = None  # 失败原因（含出错路径）
    json_text: str = ""


def validate_json_schema(text: str, schema: dict[str, Any]) -> ValidationResult:
    """文本 -> JSON 解析 -> jsonschema 校验，两段各自产出精确错误。

    第一段（语法）：json.JSONDecodeError 自带行列号，
      如 "invalid JSON at line 1 column 9: Expecting ',' delimiter"
    第二段（语义）：iter_errors 枚举全部违规，按路径排序取最靠前的一个，
      路径拼接成 JSON Pointer 形式（/items/0/qty），根级违规用 "/"。
    """
    candidate = extract_json_text(text.strip())
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return ValidationResult(
            ok=False,
            error=f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}",
            json_text=candidate,
        )
    validator = jsonschema.Draft202012Validator(schema)  # 最新草案校验器
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        # absolute_path 是路径段列表（["items", 0, "qty"]），拼成 /items/0/qty
        path = "/" + "/".join(str(p) for p in first.absolute_path) if first.absolute_path else "/"
        return ValidationResult(
            ok=False,
            error=f"schema violation at {path}: {first.message}",
            value=value,
            json_text=candidate,
        )
    return ValidationResult(ok=True, value=value, json_text=candidate)


def repair_instruction(error: str, schema: dict[str, Any]) -> str:
    """生成修复指令（需求 5.2 第 2 步）：精确校验错误 + 完整目标 Schema。

    ensure_ascii=False + indent=2：Schema 以可读的中文原样呈现，
    帮助模型准确理解嵌套结构。结尾明确"只输出 JSON、无围栏"，
    降低下一轮再犯同样格式问题的概率。
    """
    return (
        "Your previous reply failed JSON Schema validation.\n"
        f"Validation error: {error}\n"
        "Required JSON Schema:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        "Reply again with ONLY the corrected JSON value. No prose, no markdown fences."
    )
