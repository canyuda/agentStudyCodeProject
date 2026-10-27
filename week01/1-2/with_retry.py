import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

def call_with_retry(
    operation: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    total_deadline: float = 20.0,
) -> T:
    started = time.monotonic()
    last_error: ModelCallError | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as raw_exc:
            error = normalize_exception(raw_exc)
            last_error = error

            deadline_exceeded = time.monotonic() - started >= total_deadline
            if not error.retryable or attempt == max_attempts or deadline_exceeded:
                raise error from raw_exc

            exponential = min(max_delay, base_delay * 2 ** (attempt - 1))
            jitter = random.uniform(0, exponential * 0.25)
            time.sleep(exponential + jitter)

    assert last_error is not None
    raise last_error