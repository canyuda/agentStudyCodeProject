from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

ResultKind = Literal["text", "structured", "tool_calls", "refusal"]

@dataclass(frozen=True)
class ModelCapabilities:
    chat_completions: bool
    responses: bool
    structured_output: Literal["native_schema", "json_mode", "prompt_only"]
    tool_calling: bool
    supports_temperature: bool
    supports_top_p: bool

@dataclass(frozen=True)
class ModelRequest:
    system: str
    user: str
    max_output_tokens: int = 1024
    temperature: float | None = None
    top_p: float | None = None
    output_schema: dict[str, Any] | None = None

@dataclass
class ModelResult:
    kind: ResultKind
    text: str | None = None
    data: dict[str, Any] | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: str | None = None

class ModelAdapter(ABC):
    name: str
    capabilities: ModelCapabilities

    @abstractmethod
    def generate(self, request: ModelRequest) -> ModelResult:
        raise NotImplementedError