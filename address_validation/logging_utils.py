from __future__ import annotations

import sys
from datetime import datetime, timezone


def log_info(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{timestamp} INFO] {message}", flush=True)


def log_warn(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{timestamp} WARN] {message}", file=sys.stderr, flush=True)


def log_error(message: str) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{timestamp} ERROR] {message}", file=sys.stderr, flush=True)
