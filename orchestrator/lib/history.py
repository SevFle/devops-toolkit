"""SQLite-based run history for orchestration audit trail."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    change_name TEXT NOT NULL,
    repo TEXT,
    branch TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    completed_at TEXT,
    pr_url TEXT,
    error_message TEXT,
    config_json TEXT,
    total_attempts INTEGER DEFAULT 0,
    total_review_cycles INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    attempt_number INTEGER NOT NULL,
    type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    exit_code INTEGER,
    duration_seconds REAL,
    tasks_before TEXT,
    tasks_after TEXT,
    has_diff INTEGER,
    approved INTEGER,
    findings_count INTEGER
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class AttemptRecord:
    """Immutable snapshot of a logged attempt."""

    id: str
    run_id: str
    attempt_number: int
    type: str
    started_at: str
    completed_at: str | None = None
    exit_code: int | None = None
    duration_seconds: float | None = None
    tasks_before: str | None = None
    tasks_after: str | None = None
    has_diff: bool | None = None
    approved: bool | None = None
    findings_count: int | None = None


class RunHistory:
    """Manages SQLite run history database."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    def _ensure_dir(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._ensure_dir()
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._init_schema()
        return self._conn

    def _init_schema(self) -> None:
        conn = self._conn
        assert conn is not None
        conn.executescript(_SCHEMA_SQL)

        # Check schema version
        cursor = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        )
        row = cursor.fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES (?, ?)",
                ("version", str(_SCHEMA_VERSION)),
            )
            conn.commit()

    def start_run(
        self,
        change_name: str,
        repo: str | None = None,
        branch: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> str:
        """Insert a new run record. Returns run ID."""
        conn = self._connect()
        run_id = _new_id()
        conn.execute(
            """INSERT INTO runs (id, change_name, repo, branch, status, started_at, config_json)
               VALUES (?, ?, ?, ?, 'running', ?, ?)""",
            (
                run_id,
                change_name,
                repo,
                branch,
                _now_iso(),
                json.dumps(config) if config else None,
            ),
        )
        conn.commit()
        return run_id

    def complete_run(
        self,
        run_id: str,
        pr_url: str,
        total_attempts: int,
        total_review_cycles: int,
    ) -> None:
        """Mark a run as completed with PR URL."""
        conn = self._connect()
        conn.execute(
            """UPDATE runs
               SET status = 'completed',
                   completed_at = ?,
                   pr_url = ?,
                   total_attempts = ?,
                   total_review_cycles = ?
               WHERE id = ?""",
            (_now_iso(), pr_url, total_attempts, total_review_cycles, run_id),
        )
        conn.commit()

    def fail_run(
        self,
        run_id: str,
        error_message: str,
        total_attempts: int,
    ) -> None:
        """Mark a run as failed."""
        conn = self._connect()
        conn.execute(
            """UPDATE runs
               SET status = 'failed',
                   completed_at = ?,
                   error_message = ?,
                   total_attempts = ?
               WHERE id = ?""",
            (_now_iso(), error_message, total_attempts, run_id),
        )
        conn.commit()

    def log_attempt(
        self,
        run_id: str,
        attempt_number: int,
        attempt_type: str,
        tasks_before: str | None = None,
    ) -> str:
        """Insert a new attempt record. Returns attempt ID."""
        conn = self._connect()
        attempt_id = _new_id()
        conn.execute(
            """INSERT INTO attempts (id, run_id, attempt_number, type, started_at, tasks_before)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (attempt_id, run_id, attempt_number, attempt_type, _now_iso(), tasks_before),
        )
        conn.commit()
        return attempt_id

    def update_attempt(
        self,
        attempt_id: str,
        *,
        exit_code: int | None = None,
        duration_seconds: float | None = None,
        tasks_after: str | None = None,
        has_diff: bool | None = None,
        approved: bool | None = None,
        findings_count: int | None = None,
    ) -> None:
        """Update an attempt record after completion."""
        conn = self._connect()
        conn.execute(
            """UPDATE attempts
               SET completed_at = ?,
                   exit_code = ?,
                   duration_seconds = ?,
                   tasks_after = ?,
                   has_diff = ?,
                   approved = ?,
                   findings_count = ?
               WHERE id = ?""",
            (
                _now_iso(),
                exit_code,
                duration_seconds,
                tasks_after,
                1 if has_diff else (0 if has_diff is not None else None),
                1 if approved else (0 if approved is not None else None),
                findings_count,
                attempt_id,
            ),
        )
        conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
