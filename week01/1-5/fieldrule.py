from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentDecision(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    action: Literal["search_docs", "finish"] = Field(
        description="下一步是搜索资料还是结束回答"
    )
    query: str | None = Field(
        ...,
        min_length=1,
        max_length=200,
        description="搜索时使用；结束时为 null",
    )
    answer: str | None = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="结束时使用；搜索时为 null",
    )

    @model_validator(mode="after")
    def check_action_fields(self) -> "AgentDecision":
        if self.action == "search_docs":
            if not self.query or self.answer is not None:
                raise ValueError(
                    "search_docs requires query and answer must be null"
                )

        if self.action == "finish":
            if not self.answer or self.query is not None:
                raise ValueError(
                    "finish requires answer and query must be null"
                )

        return self


AgentDecision.model_json_schema()

raw_output = """
{
    "action": "search_docs",
    "query": "你好",
    "answer": null
}
"""


decision = AgentDecision.model_validate_json(raw_output)

print(decision.action)
print(decision.query)
print(decision.model_dump())

