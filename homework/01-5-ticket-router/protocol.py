"""Ticket classification protocol.

One Pydantic model defines the contract, exports the JSON Schema,
validates raw model output, and turns it back into a Python object.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RouteAction = Literal["route_to_team", "ask_user"]


class TicketDecision(BaseModel):
    """The only decision a ticket agent is allowed to make.

    extra="forbid"   -> unknown fields are rejected instead of silently dropped
    strict=True      -> no implicit coercion (e.g. "5" stays a string)
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    action: RouteAction = Field(
        description="转交团队，或向用户追问补充信息"
    )
    team: str | None = Field(
        ...,
        min_length=1,
        max_length=50,
        description="action=route_to_team 时必须给出；ask_user 时为 null",
    )
    question: str | None = Field(
        ...,
        min_length=1,
        max_length=500,
        description="action=ask_user 时必须给出；route_to_team 时为 null",
    )

    @model_validator(mode="after")
    def check_action_fields(self) -> "TicketDecision":
        # Cross-field business rule: each action carries exactly its own payload.
        if self.action == "route_to_team":
            if not self.team or self.question is not None:
                raise ValueError(
                    "route_to_team requires team and question must be null"
                )

        if self.action == "ask_user":
            if not self.question or self.team is not None:
                raise ValueError(
                    "ask_user requires question and team must be null"
                )

        return self


class TicketResult(BaseModel):
    """Terminal state of one classification run.

    decision is None on failure: the caller must treat it as
    "no safe decision", never fall back to a guessed team.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "failed"]
    decision: TicketDecision | None
    calls: int = Field(description="model calls consumed, including repairs")
    message: str | None = None
