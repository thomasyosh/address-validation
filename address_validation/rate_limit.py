from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any


RETRYABLE_STATUS_CODES = {429, 403, 408, 500, 502, 503}
# 504 Gateway Timeout is usually a proxy/upstream timeout; retrying often wastes time.
NO_RETRY_STATUS_CODES = {504}


@dataclass
class PerformanceSettings:
    workers: int = 40
    sequential: bool = False
    requests_per_second: float = 4.0
    max_retries: int = 1
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 8.0
    retry_status_codes: set[int] = field(default_factory=lambda: set(RETRYABLE_STATUS_CODES))
    no_retry_status_codes: set[int] = field(default_factory=lambda: set(NO_RETRY_STATUS_CODES))
    progress_every: int = 50
    batch_save_size: int = 50


def get_performance_settings(config: dict[str, Any]) -> PerformanceSettings:
    performance = config.get("performance") or {}
    retry_codes = performance.get("retry_status_codes")
    no_retry_codes = performance.get("no_retry_status_codes")
    sequential = bool(performance.get("sequential", False))
    workers = 1 if sequential else max(1, int(performance.get("workers", 40)))
    return PerformanceSettings(
        workers=workers,
        sequential=sequential,
        requests_per_second=max(0.0, float(performance.get("requests_per_second", 4.0))),
        max_retries=max(0, int(performance.get("max_retries", 1))),
        retry_backoff_seconds=max(0.1, float(performance.get("retry_backoff_seconds", 1.0))),
        max_retry_backoff_seconds=max(
            0.1,
            float(performance.get("max_retry_backoff_seconds", 8.0)),
        ),
        retry_status_codes=(
            set(int(code) for code in retry_codes)
            if retry_codes is not None
            else set(RETRYABLE_STATUS_CODES)
        ),
        no_retry_status_codes=(
            set(int(code) for code in no_retry_codes)
            if no_retry_codes is not None
            else set(NO_RETRY_STATUS_CODES)
        ),
        progress_every=max(1, int(performance.get("progress_every", 50))),
        batch_save_size=max(1, int(performance.get("batch_save_size", 50))),
    )


def get_endpoint_rps(endpoint: dict[str, Any], defaults: PerformanceSettings) -> float | None:
    """
    Return max HTTP requests per second for an endpoint.

    None means unlimited (no throttling). Endpoint rate_limit wins over the
    global performance.requests_per_second default.
    """
    rate_limit = endpoint.get("rate_limit") or {}
    if "requests_per_second" in rate_limit:
        value = float(rate_limit["requests_per_second"])
        if value <= 0:
            return None
        return max(0.1, value)
    return defaults.requests_per_second if defaults.requests_per_second > 0 else None


def get_endpoint_max_workers(endpoint: dict[str, Any], global_workers: int) -> int:
    """
    Max in-flight HTTP requests for one endpoint.

    Use max_workers: 1 on a downsized intranet server so the client never sends
    parallel requests to that host, even when performance.workers is higher.
    """
    limit = endpoint.get("max_workers")
    if limit is None:
        return max(1, global_workers)
    return max(1, min(global_workers, int(limit)))


class RateLimiter:
    """Simple thread-safe spacing for HTTP requests-per-second control."""

    def __init__(self, requests_per_second: float | None) -> None:
        self.unlimited = requests_per_second is None
        self.interval = 0.0 if self.unlimited else 1.0 / requests_per_second
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if self.unlimited:
            return
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
    max_seconds: float = 8.0,
) -> float:
    if retry_after is not None and retry_after > 0:
        return min(retry_after + random.uniform(0.0, 0.5), max_seconds)
    delay = (base_seconds * (2 ** max(0, attempt))) + random.uniform(0.0, 0.5)
    return min(delay, max_seconds)


def should_retry_status(status_code: int | None, settings: PerformanceSettings, attempts: int) -> bool:
    if status_code is None:
        return False
    if status_code in settings.no_retry_status_codes:
        return False
    if status_code not in settings.retry_status_codes:
        return False
    # attempts is 1-based; allow max_retries extra tries after the first.
    return attempts <= settings.max_retries


def parse_retry_after(header_value: str | None) -> float | None:
    if not header_value:
        return None
    try:
        return float(header_value)
    except ValueError:
        return None
