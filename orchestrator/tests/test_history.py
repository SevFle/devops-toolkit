"""Tests for SQLite run history module."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from lib.history import RunHistory


@pytest.fixture
def history():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test.db"
        h = RunHistory(db_path)
        yield h
        h.close()


@pytest.fixture
def history_with_run(history):
    run_id = history.start_run(
        "test-change",
        repo="owner/repo",
        branch="openspec/test-change",
        config={"max_attempts": 5},
    )
    return history, run_id


class TestDatabaseInit:
    def test_creates_db_file(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "sub" / "dir" / "test.db"
            h = RunHistory(db_path)
            h.start_run("test")
            assert db_path.exists()
            h.close()

    def test_wal_mode(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test.db"
            h = RunHistory(db_path)
            h.start_run("test")

            conn = sqlite3.connect(str(db_path))
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()
            h.close()

            assert mode == "wal"

    def test_tables_created(self, history):
        history.start_run("test")
        conn = history._connect()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {row[0] for row in tables}

        assert "runs" in table_names
        assert "attempts" in table_names
        assert "schema_meta" in table_names


class TestRunLifecycle:
    def test_start_run(self, history):
        run_id = history.start_run(
            "my-change",
            repo="owner/repo",
            branch="openspec/my-change",
            config={"max_attempts": 5},
        )
        assert run_id is not None
        assert len(run_id) == 36  # UUID length

        conn = history._connect()
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        assert row is not None

    def test_complete_run(self, history_with_run):
        history, run_id = history_with_run

        history.complete_run(
            run_id,
            pr_url="https://github.com/pr/1",
            total_attempts=3,
            total_review_cycles=1,
        )

        conn = history._connect()
        row = conn.execute(
            "SELECT status, pr_url, total_attempts FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()

        assert row[0] == "completed"
        assert row[1] == "https://github.com/pr/1"
        assert row[2] == 3

    def test_fail_run(self, history_with_run):
        history, run_id = history_with_run

        history.fail_run(run_id, "Max attempts reached", total_attempts=5)

        conn = history._connect()
        row = conn.execute(
            "SELECT status, error_message FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()

        assert row[0] == "failed"
        assert row[1] == "Max attempts reached"


class TestAttemptLifecycle:
    def test_log_and_update_attempt(self, history_with_run):
        history, run_id = history_with_run

        attempt_id = history.log_attempt(
            run_id, 1, "claude", tasks_before="0/5",
        )
        assert attempt_id is not None

        history.update_attempt(
            attempt_id,
            exit_code=0,
            duration_seconds=120.5,
            tasks_after="3/5",
            has_diff=True,
        )

        conn = history._connect()
        row = conn.execute(
            "SELECT exit_code, duration_seconds, tasks_after, has_diff FROM attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()

        assert row[0] == 0
        assert row[1] == 120.5
        assert row[2] == "3/5"
        assert row[3] == 1  # True stored as 1

    def test_review_attempt(self, history_with_run):
        history, run_id = history_with_run

        attempt_id = history.log_attempt(run_id, 1, "claude_review")
        history.update_attempt(
            attempt_id,
            approved=False,
            findings_count=3,
            duration_seconds=45.0,
        )

        conn = history._connect()
        row = conn.execute(
            "SELECT approved, findings_count FROM attempts WHERE id = ?",
            (attempt_id,),
        ).fetchone()

        assert row[0] == 0  # False
        assert row[1] == 3

    def test_multiple_attempts(self, history_with_run):
        history, run_id = history_with_run

        history.log_attempt(run_id, 1, "claude")
        history.log_attempt(run_id, 2, "claude")
        history.log_attempt(run_id, 3, "claude_review")

        conn = history._connect()
        count = conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]

        assert count == 3
