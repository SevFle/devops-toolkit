"""Tests for progress detection module."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.log import StructuredLogger
from lib.progress import (
    GitDiffResult,
    OpenSpecProgress,
    ProgressDetector,
    TasksMdProgress,
)


@pytest.fixture
def logger():
    return StructuredLogger("test-change")


@pytest.fixture
def tmp_work_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class TestCheckTasksMd:
    def test_counts_checkboxes(self, tmp_work_dir, logger):
        tasks_file = tmp_work_dir / "tasks.md"
        tasks_file.write_text(
            "## 1. Setup\n"
            "- [x] 1.1 Done task\n"
            "- [x] 1.2 Another done\n"
            "- [ ] 1.3 Not done\n"
            "## 2. More\n"
            "- [ ] 2.1 Pending\n"
            "- [ ] 2.2 Also pending\n"
        )

        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        result = detector.check_tasks_md(str(tasks_file))

        assert result is not None
        assert result.completed == 2
        assert result.total == 5

    def test_all_complete(self, tmp_work_dir, logger):
        tasks_file = tmp_work_dir / "tasks.md"
        tasks_file.write_text(
            "- [x] 1.1 Done\n"
            "- [x] 1.2 Done\n"
        )

        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        result = detector.check_tasks_md(str(tasks_file))

        assert result is not None
        assert result.completed == 2
        assert result.total == 2

    def test_file_not_found(self, tmp_work_dir, logger):
        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        result = detector.check_tasks_md("/nonexistent/tasks.md")

        assert result is None

    def test_case_insensitive_x(self, tmp_work_dir, logger):
        tasks_file = tmp_work_dir / "tasks.md"
        tasks_file.write_text(
            "- [x] lowercase\n"
            "- [X] uppercase\n"
            "- [ ] unchecked\n"
        )

        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        result = detector.check_tasks_md(str(tasks_file))

        assert result is not None
        assert result.completed == 2
        assert result.total == 3

    def test_empty_file(self, tmp_work_dir, logger):
        tasks_file = tmp_work_dir / "tasks.md"
        tasks_file.write_text("# No tasks here\n")

        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        result = detector.check_tasks_md(str(tasks_file))

        assert result is not None
        assert result.completed == 0
        assert result.total == 0


class TestCheckGitDiff:
    @patch("lib.progress.subprocess.run")
    def test_detects_changes(self, mock_run, tmp_work_dir, logger):
        # Full diff shows changes
        mock_run.side_effect = [
            MagicMock(stdout=" file1.py | 10 ++++\n 1 file changed", returncode=0),
            MagicMock(stdout=" file1.py | 10 ++++\n 1 file changed", returncode=0),
        ]

        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        result = detector.check_git_diff()

        assert result.has_changes is True
        assert result.has_meaningful_changes is True

    @patch("lib.progress.subprocess.run")
    def test_no_changes(self, mock_run, tmp_work_dir, logger):
        mock_run.side_effect = [
            MagicMock(stdout="", returncode=0),
            MagicMock(stdout="", returncode=0),
        ]

        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        result = detector.check_git_diff()

        assert result.has_changes is False
        assert result.has_meaningful_changes is False

    @patch("lib.progress.subprocess.run")
    def test_whitespace_only(self, mock_run, tmp_work_dir, logger):
        mock_run.side_effect = [
            MagicMock(stdout=" file.py | 2 +-\n 1 file changed", returncode=0),
            MagicMock(stdout="", returncode=0),  # whitespace-ignored shows nothing
        ]

        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        result = detector.check_git_diff()

        assert result.has_changes is True
        assert result.has_meaningful_changes is False


class TestAssessProgress:
    @patch.object(ProgressDetector, "check_openspec_cli")
    @patch.object(ProgressDetector, "check_tasks_md")
    @patch.object(ProgressDetector, "check_git_diff")
    def test_complete_all_signals(self, mock_diff, mock_tasks, mock_openspec, tmp_work_dir, logger):
        mock_openspec.return_value = OpenSpecProgress(
            state="all_done", completed=5, total=5, remaining=0,
        )
        mock_tasks.return_value = TasksMdProgress(completed=5, total=5)
        mock_diff.return_value = GitDiffResult(
            has_changes=True, has_meaningful_changes=True, files_changed=3,
        )

        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        assessment = detector.assess_progress()

        assert assessment.is_complete is True
        assert assessment.completed == 5
        assert assessment.total == 5
        assert assessment.is_stuck is False

    @patch.object(ProgressDetector, "check_openspec_cli")
    @patch.object(ProgressDetector, "check_tasks_md")
    @patch.object(ProgressDetector, "check_git_diff")
    def test_partial_progress(self, mock_diff, mock_tasks, mock_openspec, tmp_work_dir, logger):
        mock_openspec.return_value = OpenSpecProgress(
            state="ready", completed=3, total=7, remaining=4,
        )
        mock_tasks.return_value = TasksMdProgress(completed=3, total=7)
        mock_diff.return_value = GitDiffResult(
            has_changes=True, has_meaningful_changes=True, files_changed=2,
        )

        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        assessment = detector.assess_progress()

        assert assessment.is_complete is False
        assert assessment.completed == 3
        assert assessment.total == 7
        assert assessment.is_stuck is False

    @patch.object(ProgressDetector, "check_openspec_cli")
    @patch.object(ProgressDetector, "check_tasks_md")
    @patch.object(ProgressDetector, "check_git_diff")
    def test_stuck_detection(self, mock_diff, mock_tasks, mock_openspec, tmp_work_dir, logger):
        mock_openspec.return_value = OpenSpecProgress(
            state="ready", completed=0, total=5, remaining=5,
        )
        mock_tasks.return_value = TasksMdProgress(completed=0, total=5)
        mock_diff.return_value = GitDiffResult(
            has_changes=False, has_meaningful_changes=False, files_changed=0,
        )

        detector = ProgressDetector("test-change", tmp_work_dir, logger)

        # First run — establishes baseline, never stuck
        a1 = detector.assess_progress()
        assert a1.is_stuck is False
        assert detector.consecutive_no_progress == 0

        # Second run — no progress (completed still 0), counter starts
        a2 = detector.assess_progress()
        assert a2.is_stuck is False
        assert detector.consecutive_no_progress == 1

        # Third run — still no progress
        a3 = detector.assess_progress()
        assert a3.is_stuck is False
        assert detector.consecutive_no_progress == 2

        # Fourth run — 3 consecutive no-progress, now stuck
        a4 = detector.assess_progress()
        assert a4.is_stuck is True
        assert detector.consecutive_no_progress == 3

    @patch.object(ProgressDetector, "check_openspec_cli")
    @patch.object(ProgressDetector, "check_tasks_md")
    @patch.object(ProgressDetector, "check_git_diff")
    def test_openspec_authoritative_over_tasks_md(
        self, mock_diff, mock_tasks, mock_openspec, tmp_work_dir, logger,
    ):
        # openspec says not done, tasks.md says done
        mock_openspec.return_value = OpenSpecProgress(
            state="ready", completed=4, total=5, remaining=1,
        )
        mock_tasks.return_value = TasksMdProgress(completed=5, total=5)
        mock_diff.return_value = GitDiffResult(
            has_changes=True, has_meaningful_changes=True, files_changed=1,
        )

        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        assessment = detector.assess_progress()

        # openspec wins
        assert assessment.is_complete is False
        assert assessment.completed == 4

    @patch.object(ProgressDetector, "check_openspec_cli")
    @patch.object(ProgressDetector, "check_tasks_md")
    @patch.object(ProgressDetector, "check_git_diff")
    def test_fallback_to_tasks_md_when_no_openspec(
        self, mock_diff, mock_tasks, mock_openspec, tmp_work_dir, logger,
    ):
        mock_openspec.return_value = None  # CLI unavailable
        mock_tasks.return_value = TasksMdProgress(completed=5, total=5)
        mock_diff.return_value = GitDiffResult(
            has_changes=True, has_meaningful_changes=True, files_changed=1,
        )

        detector = ProgressDetector("test-change", tmp_work_dir, logger)
        assessment = detector.assess_progress()

        assert assessment.is_complete is True
        assert assessment.completed == 5
