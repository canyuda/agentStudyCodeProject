from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PromptContext:
    task: str
    workspace_root: Path
    allowed_tools: tuple[str, ...]
    project_rules: str
    run_summary: str
    observations: tuple[str, ...]
    remaining_steps: int
    remaining_tokens: int

    def validate(self) -> None:
        if not self.task.strip():
            raise ValueError("task must not be empty")
        if self.remaining_steps < 0 or self.remaining_tokens < 0:
            raise ValueError("budget must not be negative")
        if not self.allowed_tools:
            raise ValueError("allowed_tools must not be empty")