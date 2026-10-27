import asyncio
import random
from collections.abc import AsyncIterator, Callable


class RetryableStreamError(Exception):
    pass


async def open_stream_with_retry(
    open_stream: Callable[[], AsyncIterator[ModelStreamEvent]],
    *,
    max_attempts: int = 3,
) -> AsyncIterator[ModelStreamEvent]:
    emitted_business_event = False

    for attempt in range(1, max_attempts + 1):
        try:
            async for event in open_stream():
                emitted_business_event = True
                yield event
            return

        except RetryableStreamError:
            # 已向下游发过内容后，不自动重新生成。
            if emitted_business_event or attempt == max_attempts:
                raise

            base = min(0.5 * (2 ** (attempt - 1)), 4.0)
            delay = random.uniform(0, base)
            await asyncio.sleep(delay)