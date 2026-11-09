"""Bounded retry decisions shared by buffered and streaming async requests."""

import math
import random
import time
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

NETWORK_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
STATUSES = frozenset({408, 429, 500, 502, 503, 504})
METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
_clock = time.time


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 2
    base_delay: float = 0.5
    max_delay: float = 30.0

    def __post_init__(self):
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer.")
        for value in (self.base_delay, self.max_delay):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("Retry delays must be finite, non-negative numbers.")

    def permits(self, method: str, attempt: int, replayable: bool = True) -> bool:
        return replayable and method.upper() in METHODS and attempt < self.max_retries

    def delay(self, attempt: int, response: httpx.Response | None = None) -> float | None:
        hint = response.headers.get("Retry-After") if response is not None else None
        if hint:
            seconds = None
            try:
                seconds = float(hint)
            except ValueError:
                try:
                    date = parsedate_to_datetime(hint)
                    if date.tzinfo is None:
                        date = date.replace(tzinfo=timezone.utc)
                    seconds = max(0.0, date.timestamp() - _clock())
                except (ValueError, TypeError, OverflowError):
                    pass
            if seconds is not None and math.isfinite(seconds) and seconds >= 0:
                return seconds if seconds <= self.max_delay else None
        try:
            ceiling = min(self.max_delay, math.ldexp(self.base_delay, attempt))
        except OverflowError:
            ceiling = self.max_delay
        return random.uniform(0, ceiling)


def replayable(request: httpx.Request) -> bool:
    try:
        _ = request.content
    except httpx.RequestNotRead:
        return False
    return True
