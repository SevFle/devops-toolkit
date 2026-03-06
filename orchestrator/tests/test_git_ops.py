"""Tests for git operations module."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.git_ops import (
    GitError,
    commit_progress,
    create_branch,
    has_uncommitted_changes,
)
from lib.log import StructuredLogger


@pytest.fixture
def logger():
    return StructuredLogger("test-change")


@pytest.fixture
def git_repo():
    """Create a temporary git repo for testing."""
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo), capture_output=True,
        )
        # Create initial commit
        (repo / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(repo), capture_output=True,
        )
        yield repo


class TestCreateBranch:
    def test_creates_new_branch(self, git_repo, logger):
        create_branch("openspec/test", git_repo, logger)

        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "openspec/test"

    def test_checkout_existing_branch(self, git_repo, logger):
        # Create the branch first
        subprocess.run(
            ["git", "checkout", "-b", "openspec/existing"],
            cwd=str(git_repo), capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(git_repo), capture_output=True,
        )

        # Should checkout without error
        create_branch("openspec/existing", git_repo, logger)

        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "openspec/existing"


class TestHasUncommittedChanges:
    def test_clean_repo(self, git_repo):
        assert has_uncommitted_changes(git_repo) is False

    def test_with_changes(self, git_repo):
        (git_repo / "new_file.py").write_text("print('hello')")
        assert has_uncommitted_changes(git_repo) is True

    def test_with_staged_changes(self, git_repo):
        (git_repo / "staged.py").write_text("x = 1")
        subprocess.run(["git", "add", "staged.py"], cwd=str(git_repo), capture_output=True)
        assert has_uncommitted_changes(git_repo) is True


class TestCommitProgress:
    def test_commits_changes(self, git_repo, logger):
        (git_repo / "feature.py").write_text("def feature(): pass")

        committed = commit_progress("my-change", 3, 7, git_repo, logger)

        assert committed is True

        # Verify commit message
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        )
        assert "wip: my-change - 3/7 tasks complete" in result.stdout

    def test_no_changes_skips_commit(self, git_repo, logger):
        committed = commit_progress("my-change", 0, 5, git_repo, logger)
        assert committed is False

    def test_stages_all_files(self, git_repo, logger):
        (git_repo / "a.py").write_text("a = 1")
        (git_repo / "b.py").write_text("b = 2")

        commit_progress("my-change", 2, 5, git_repo, logger)

        # Verify both files are committed
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1"],
            cwd=str(git_repo),
            capture_output=True,
            text=True,
        )
        assert "a.py" in result.stdout
        assert "b.py" in result.stdout


class TestPushAndCreatePr:
    @patch("lib.git_ops.subprocess.run")
    @patch("lib.git_ops._run_git")
    def test_creates_new_pr(self, mock_git, mock_run, logger):
        # First call: gh pr view (not found)
        # Second call: gh pr create (success)
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout=""),  # PR doesn't exist
            MagicMock(returncode=0, stdout="https://github.com/owner/repo/pull/42\n"),
        ]

        from lib.git_ops import push_and_create_pr

        pr_url = push_and_create_pr(
            "openspec/test", "test-change", "PR body", Path("."), logger,
        )
        assert pr_url == "https://github.com/owner/repo/pull/42"
