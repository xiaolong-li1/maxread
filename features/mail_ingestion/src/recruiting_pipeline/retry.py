from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


class PermanentPipelineError(RuntimeError):
    pass


def retry_call(
    operation: Callable[[], T],
    *,
    attempts: int,
    base_seconds: float,
    retryable: Callable[[BaseException], bool] | None = None,
) -> T:
    last: BaseException | None = None
    for index in range(max(1, attempts)):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - boundary wrapper records the failure
            last = exc
            if retryable is not None and not retryable(exc):
                raise
            if index + 1 >= max(1, attempts):
                raise
            delay = min(60.0, base_seconds * (2**index)) + random.uniform(0, base_seconds)
            time.sleep(delay)
    assert last is not None
    raise last


def is_transient_error(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    text = str(exc).lower()
    return any(token in text for token in ("timeout", "temporarily", "connection reset", "503", "502", "500", "429", "rate limit"))
