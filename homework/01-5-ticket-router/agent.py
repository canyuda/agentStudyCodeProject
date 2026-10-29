"""Ticket classification agent: validate, repair at most twice, fail fast.

Run the demo:
    cd agentStudyCodeProject/homework/01-5-ticket-router
    python agent.py          # any interpreter with pydantic v2 works
"""

from pydantic import ValidationError

from fake_model import ModelCall, scripted_model
from protocol import TicketDecision, TicketResult
from repair import build_repair_message

SYSTEM_PROMPT = (
    "你是工单分类助手。只输出符合 TicketDecision 协议的 JSON："
    "action 只能是 route_to_team 或 ask_user；"
    "转交时必须给 team 且 question 为 null；"
    "追问时必须给 question 且 team 为 null；禁止任何额外字段。"
)

MAX_REPAIRS = 2


def classify_ticket(
    ticket: str,
    call_model: ModelCall,
    max_repairs: int = MAX_REPAIRS,
) -> TicketResult:
    """Model call -> validate -> repair loop -> ok/failed terminal state.

    The repair loop only regenerates the decision; no tool is ever
    invoked here, so retries carry no business side effects.
    """
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": ticket},
    ]
    schema = TicketDecision.model_json_schema()
    calls = 0

    for attempt in range(max_repairs + 1):
        raw_output = call_model(messages, schema)
        calls += 1

        try:
            decision = TicketDecision.model_validate_json(raw_output)
        except ValidationError as exc:
            issues = exc.errors(include_url=False, include_input=False)

            if attempt == max_repairs:
                break

            # Feed the exact validation errors back as new context.
            messages.extend(
                [
                    {"role": "assistant", "content": raw_output[:2000]},
                    {"role": "user", "content": build_repair_message(issues)},
                ]
            )
            continue

        return TicketResult(status="ok", decision=decision, calls=calls)

    # No safe decision: never guess a team, never invoke routing.
    return TicketResult(
        status="failed",
        decision=None,
        calls=calls,
        message=(
            f"structured output failed after {calls} calls; "
            "decision stays None so routing is never triggered"
        ),
    )


def main() -> None:
    scenarios: dict[str, list[str]] = {
        # ok: valid routing on the first call
        "ok_first_call": [
            '{"action": "route_to_team", "team": "billing", "question": null}',
        ],
        # repaired: first call misses team (business combination error)
        "ok_after_repair": [
            '{"action": "route_to_team", "team": null, "question": null}',
            '{"action": "route_to_team", "team": "billing", "question": null}',
        ],
        # ok: valid ask_user
        "ok_ask_user": [
            '{"action": "ask_user", "team": null, '
            '"question": "请提供订单号和发票抬头"}',
        ],
        # failed: broken output repeats through the whole repair budget
        "failed_no_guess": [
            '{"action": "route_to_team", "team": null, "question": null}',
        ],
    }

    ticket = "工单：客户反馈上个月的发票金额不对，要求重开。"

    for name, outputs in scenarios.items():
        result = classify_ticket(ticket, scripted_model(outputs))
        print(f"[{name}] status={result.status} calls={result.calls}")
        print(f"  decision={result.decision}")
        if result.message:
            print(f"  message={result.message}")


if __name__ == "__main__":
    main()
