from datetime import datetime, timedelta, timezone
from hashlib import sha256


SCHEMA_VERSION = "agent-decision-v1"


class CachedAnswer(BaseModel):
    task_key: str
    answer: str = Field(min_length=1, max_length=4000)
    created_at: datetime
    schema_version: str


def make_task_key(task: str) -> str:
    normalized = " ".join(task.split()).strip().lower()
    return sha256(normalized.encode("utf-8")).hexdigest()


def use_cache_if_fresh(
    cached: CachedAnswer | None,
    task: str,
    max_age: timedelta = timedelta(minutes=5),
) -> AgentDecision | None:
    if cached is None:
        return None
    if cached.task_key != make_task_key(task):
        return None
    if cached.schema_version != SCHEMA_VERSION:
        return None
    if cached.created_at.tzinfo is None:
        return None

    age = datetime.now(timezone.utc) - cached.created_at
    if age < timedelta(0) or age > max_age:
        return None

    return AgentDecision(
        action="finish",
        query=None,
        answer=cached.answer,
    )