"""Build the repair feedback message fed back to the model."""

import json


def build_repair_message(issues: list[dict[str, object]]) -> str:
    """Answer the three questions a repair turn must answer:
    where it went wrong, why, and what the model may change.
    """
    return (
        "上一份输出未通过 TicketDecision 校验。\n"
        # default=str: ctx may carry the raw ValueError raised by a
        # model_validator, which json.dumps cannot serialize natively.
        f"校验错误：{json.dumps(issues, ensure_ascii=False, default=str)}\n"
        "请保持原任务含义不变，只修正 JSON 语法、字段、"
        "类型或字段组合。只返回修正后的 JSON。"
    )
