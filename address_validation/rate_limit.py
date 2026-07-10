from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any


RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504, 403}


@dataclass
class PerformanceSettings:
    workers: int = 6
    requests_per_second: float = 4.0
    max_retries: int = 5
    retry_backoff_seconds: float = 1.5
    retry_status_codes: set[int] = field(default_factory=lambda: set(RETRYABLE_STATUS_CODES))
    progress_every: int = 100
    batch_save_size: int = 1


def get_performance_settings(config: dict[str, Any]) -> PerformanceSettings:
    performance = config.get("performance") or {}
    retry_codes = performance.get("retry_status_codes")
    return PerformanceSettings(
        workers=max(1, int(performance.get("workers", 6))),
        requests_per_second=max(0.1, float(performance.get("requests_per_second", 4.0))),
        max_retries=max(0, int(performance.get("max_retries", 5))),
        retry_backoff_seconds=max(0.1, float(performance.get("retry_backoff_seconds", 1.5))),
        retry_status_codes=(
            set(int(code) for code in retry_codes)
            if retry_codes
            else set(RETRYABLE_STATUS_CODES)
        ),
        progress_every=max(1, int(performance.get("progress_every", 100))),
    batch_save_size=max(1, int(performance.get("batch_save_size", 1))),
)


def get_endpoint_rps(endpoint: dict[str, Any], defaults: PerformanceSettings) -> float:
    rate_limit = endpoint.get("rate_limit") or {}
    if "requests_per_second" in rate_limit:
        return max(0.1, float(rate_limit["requests_per_second"]))
    return defaults.requests_per_second


class RateLimiter:
    """Simple thread-safe token bucket for requests-per-second control."""

    def __init__(self, requests_per_second: float) -> None:
        self.interval = 1.0 / requests_per_second
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                delay = self._next_allowed - now
                self._next_allowed += self.interval
            else:
                delay = 0.0
                self._next_allowed = now + self.interval

        if delay > 0:
            time.sleep(delay)


def compute_backoff_seconds(
    attempt: int,
    base_seconds: float,
    *,
    retry_after: float | None = None,
) -> float:
    if retry_after is not None and retry_after > 0:
        return retry_after + random.uniform(0.0, 0.5)
    # Exponential backoff with jitter: 1.5, 3, 6, ...
    return (base_seconds * (2 ** max(0, attempt))) + random.uniform(0.0, 0.5)


def parse_retry_after(header_value: str | None) -> float | None:
    if not header_value:
        return None
    try:
        return float(header_value)
    except ValueError:
        return None
