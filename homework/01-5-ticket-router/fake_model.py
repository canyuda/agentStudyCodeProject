"""Scripted fake model: replays canned outputs, zero SDK dependency.

The last output repeats, so a broken output at the end keeps failing —
exactly what the "failed" scenario needs.
"""

from collections.abc import Callable

ModelCall = Callable[[list[dict[str, str]], dict[str, object]], str]


def scripted_model(outputs: list[str]) -> ModelCall:
    state = {"index": 0}

    def call(messages: list[dict[str, str]], schema: dict[str, object]) -> str:
        output = outputs[min(state["index"], len(outputs) - 1)]
        state["index"] += 1
        return output

    return call
