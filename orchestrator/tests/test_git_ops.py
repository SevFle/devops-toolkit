"""Tests for git operations module."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.git_ops import (
    GitError,
    clone_repo,
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
        subprocess.run(["git", "init", "-b", "main"], cwd=str(repo), capture_output=True)
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

    def test_creates_branch_from_base_branch(self, git_repo, logger):
        """BUG-6: create_branch must create from specified base_branch, not HEAD."""
        # Create a 'develop' branch with an extra commit
        subprocess.run(
            ["git", "checkout", "-b", "develop"],
            cwd=str(git_repo), capture_output=True,
        )
        (git_repo / "develop_file.py").write_text("develop = True")
        subprocess.run(["git", "add", "-A"], cwd=str(git_repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "develop commit"],
            cwd=str(git_repo), capture_output=True,
        )
        # Get the develop commit hash
        develop_hash = subprocess.run(
            ["git", "rev-parse", "develop"],
            cwd=str(git_repo), capture_output=True, text=True,
        ).stdout.strip()
        # Get the main commit hash
        main_hash = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(git_repo), capture_output=True, text=True,
        ).stdout.strip()

        # Stay on develop (HEAD != main). create_branch should create from
        # base_branch="main", NOT from current HEAD (develop).
        create_branch("openspec/from-main", git_repo, logger, base_branch="main")

        # The new branch should point to the main commit, not develop
        new_branch_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(git_repo), capture_output=True, text=True,
        ).stdout.strip()
        assert new_branch_hash == main_hash
        assert new_branch_hash != develop_hash

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


class TestCloneRepo:
    @patch("lib.git_ops._run_git")
    def test_clone_does_not_embed_token_in_url(self, mock_run_git, logger, tmp_path):
        """BUG-5: clone_repo must not embed PAT in the clone URL."""
        clone_repo("owner/repo", tmp_path / "dest", "ghp_secret123", logger)

        # Get the clone command that was called
        clone_call = mock_run_git.call_args_list[0]
        clone_args = clone_call[0][0]  # first positional arg is the args list

        # The token should NOT appear in any argument
        for arg in clone_args:
            assert "ghp_secret123" not in arg, f"Token leaked in clone arg: {arg}"


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
