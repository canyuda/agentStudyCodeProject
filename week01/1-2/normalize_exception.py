from dataclasses import dataclass
from typing import Literal

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    RateLimitError,
)
from pydantic import ValidationError

ErrorCode = Literal[
    "authentication",
    "invalid_request",
    "rate_limited",
    "overloaded",
    "timeout",
    "network",
    "refusal",
    "truncated",
    "schema_invalid",
    "unknown",
]

@dataclass
class ModelCallError(Exception):
    code: ErrorCode
    message: str
    retryable: bool
    status_code: int | None = None
    request_id: str | None = None


def normalize_exception(exc: Exception) -> ModelCallError:
    if isinstance(exc, AuthenticationError):
        return ModelCallError("authentication", str(exc), False, 401)

    if isinstance(exc, BadRequestError):
        return ModelCallError(
            "invalid_request",
            str(exc),
            False,
            getattr(exc, "status_code", None),
        )

    if isinstance(exc, RateLimitError):
        return ModelCallError("rate_limited", str(exc), True, 429)

    if isinstance(exc, APITimeoutError):
        return ModelCallError("timeout", str(exc), True)

    if isinstance(exc, APIConnectionError):
        return ModelCallError("network", str(exc), True)

    if isinstance(exc, InternalServerError):
        status = getattr(exc, "status_code", None)
        code: ErrorCode = "overloaded" if status == 503 else "unknown"
        return ModelCallError(code, str(exc), True, status)

    if isinstance(exc, ValidationError):
        return ModelCallError("schema_invalid", str(exc), False)

    return ModelCallError("unknown", str(exc), False)




def parse_response(response) -> AgentAction:
    if response.status == "incomplete":
        reason = getattr(response.incomplete_details, "reason", "unknown")
        if reason == "content_filter":
            raise ModelCallError("refusal", "内容被安全策略拒绝", False)
        raise ModelCallError("truncated", f"输出不完整: {reason}", False)

    if response.status == "failed":
        raise ModelCallError("unknown", str(response.error), True)

    if not response.output_text:
        raise ModelCallError("schema_invalid", "模型返回空内容", False)

    try:
        return AgentAction.model_validate_json(response.output_text)
    except ValidationError as exc:
        raise ModelCallError("schema_invalid", str(exc), False) from exc