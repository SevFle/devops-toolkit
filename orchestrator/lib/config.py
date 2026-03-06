"""Configuration loaded from environment variables with sensible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Immutable orchestrator configuration."""

    max_implementation_attempts: int = 5
    max_review_cycles: int = 3
    claude_timeout: int = 1800  # 30 minutes
    review_timeout: int = 600  # 10 minutes
    db_path: Path = Path.home() / ".orchestrator" / "history.db"
    github_token: str = ""
    max_consecutive_no_progress: int = 2
    time_budget_seconds: int = 6600  # 110 minutes (leaves 10 min buffer for 2h GHA timeout)
    adaptive_budget: bool = True

    @classmethod
    def from_env(cls) -> Config:
        """Create Config from environment variables with defaults."""

        def _int_env(key: str, default: str) -> int:
            """Parse env var as int, tolerating float strings like '5.0'."""
            raw = os.environ.get(key, default)
            return int(float(raw))

        return cls(
            max_implementation_attempts=_int_env("ORCHESTRATOR_MAX_ATTEMPTS", "5"),
            max_review_cycles=_int_env("ORCHESTRATOR_MAX_REVIEW_CYCLES", "3"),
            claude_timeout=_int_env("ORCHESTRATOR_CLAUDE_TIMEOUT", "1800"),
            review_timeout=_int_env("ORCHESTRATOR_REVIEW_TIMEOUT", "600"),
            db_path=Path(
                os.environ.get(
                    "ORCHESTRATOR_DB_PATH",
                    str(Path.home() / ".orchestrator" / "history.db"),
                )
            ),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            max_consecutive_no_progress=_int_env("ORCHESTRATOR_MAX_NO_PROGRESS", "2"),
            time_budget_seconds=_int_env("ORCHESTRATOR_TIME_BUDGET", "6600"),
            adaptive_budget=os.environ.get(
                "ORCHESTRATOR_ADAPTIVE_BUDGET", "true"
            ).lower() in ("true", "1", "yes"),
        )

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if valid."""
        errors: list[str] = []
        if self.max_implementation_attempts < 1:
            errors.append("max_implementation_attempts must be >= 1")
        if self.max_review_cycles < 1:
            errors.append("max_review_cycles must be >= 1")
        if self.claude_timeout < 60:
            errors.append("claude_timeout must be >= 60 seconds")
        if self.review_timeout < 30:
            errors.append("review_timeout must be >= 30 seconds")
        return errors
