"""Structured JSON-line logging to stdout for GitHub Actions capture."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any


class StructuredLogger:
    """Emits JSON-line log entries to stdout."""

    def __init__(self, change_name: str | None = None) -> None:
        self._change_name = change_name

    def _emit(self, level: str, message: str, **extra: Any) -> None:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
        }
        if self._change_name:
            entry["change_name"] = self._change_name
        for key, value in extra.items():
            if value is not None:
                entry[key] = value
        print(json.dumps(entry, default=str), flush=True)

    def info(self, message: str, **extra: Any) -> None:
        self._emit("info", message, **extra)

    def warning(self, message: str, **extra: Any) -> None:
        self._emit("warning", message, **extra)

    def error(self, message: str, **extra: Any) -> None:
        self._emit("error", message, **extra)

    def phase(self, phase_name: str, message: str, **extra: Any) -> None:
        self._emit("info", message, phase=phase_name, **extra)

    def progress(
        self,
        attempt: int,
        completed: int,
        total: int,
        has_diff: bool,
    ) -> None:
        self._emit(
            "info",
            "Claude attempt complete",
            attempt=attempt,
            progress=f"{completed}/{total}",
            has_diff=has_diff,
        )

    def review_result(
        self,
        cycle: int,
        approved: bool,
        findings_count: int,
    ) -> None:
        self._emit(
            "info",
            "Claude review complete",
            cycle=cycle,
            approved=approved,
            findings_count=findings_count,
        )

    def fatal(self, message: str, **extra: Any) -> None:
        """Log error and write to stderr for visibility."""
        self._emit("error", message, **extra)
        print(f"FATAL: {message}", file=sys.stderr)
