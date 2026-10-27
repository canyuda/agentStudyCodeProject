class DecisionResult(BaseModel):
    status: Literal["ok", "degraded", "failed"]
    source: Literal["model", "cache", "none"]
    decision: AgentDecision | None
    message: str | None
    repair_attempts: int = 0


def failed_result(attempts: int) -> DecisionResult:
    return DecisionResult(
        status="failed",
        source="none",
        decision=None,
        message="模型未能生成可验证的决策，请稍后重试。",
        repair_attempts=attempts,
    )