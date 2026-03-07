"""Git operations: clone, branch, commit, push, and PR creation."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from lib.log import StructuredLogger


class GitError(Exception):
    """Raised when a git operation fails."""


def _run_git(
    args: list[str],
    cwd: str | Path,
    *,
    check: bool = True,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    """Run a git command and return the result."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd),
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def clone_repo(
    repo: str,
    dest: Path,
    token: str,
    logger: StructuredLogger,
    *,
    shallow: bool = True,
) -> Path:
    """Clone a repository using PAT authentication.

    Args:
        repo: Repository in 'owner/name' or full URL format.
        dest: Destination directory.
        token: GitHub PAT for authentication.
        logger: Logger instance.
        shallow: Use --depth 1 for faster clones.

    Returns:
        Path to the cloned repo directory.
    """
    if repo.startswith("http"):
        clone_url = repo
    else:
        clone_url = f"https://github.com/{repo}.git"

    # Pass token via http.extraheader to avoid embedding it in the URL
    # (which could leak in logs or .git/config)
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    auth_header = f"Authorization: Basic {credentials}"

    cmd = ["-c", f"http.extraheader={auth_header}", "clone"]
    if shallow:
        cmd.extend(["--depth", "1"])
    cmd.extend([clone_url, str(dest)])

    logger.info("Cloning repository", repo=repo, shallow=shallow)
    _run_git(cmd, cwd=".", timeout=300)
    logger.info("Clone complete", dest=str(dest))
    return dest


def create_branch(
    branch_name: str,
    work_dir: Path,
    logger: StructuredLogger,
    *,
    base_branch: str = "main",
) -> None:
    """Create and checkout a branch. If it already exists, check it out.

    Args:
        branch_name: Branch to create (e.g., 'openspec/my-feature').
        work_dir: Repository working directory.
        logger: Logger instance.
        base_branch: Branch to create from.
    """
    # Check if branch already exists locally
    result = _run_git(
        ["branch", "--list", branch_name],
        cwd=work_dir,
        check=False,
    )
    if result.stdout.strip():
        logger.info("Branch already exists, checking out", branch=branch_name)
        _run_git(["checkout", branch_name], cwd=work_dir)
        return

    # Check if branch exists on remote
    result = _run_git(
        ["ls-remote", "--heads", "origin", branch_name],
        cwd=work_dir,
        check=False,
    )
    if result.stdout.strip():
        logger.info("Branch exists on remote, fetching and checking out", branch=branch_name)
        _run_git(["fetch", "origin", branch_name], cwd=work_dir)
        _run_git(["checkout", "-b", branch_name, f"origin/{branch_name}"], cwd=work_dir)
        return

    # Create new branch from base
    logger.info("Creating new branch", branch=branch_name, base=base_branch)
    _run_git(["checkout", "-b", branch_name, base_branch], cwd=work_dir)


def has_uncommitted_changes(work_dir: Path) -> bool:
    """Check if there are uncommitted changes (staged or unstaged)."""
    result = _run_git(["status", "--porcelain"], cwd=work_dir, check=False)
    return bool(result.stdout.strip())


def commit_progress(
    change_name: str,
    completed: int,
    total: int,
    work_dir: Path,
    logger: StructuredLogger,
) -> bool:
    """Stage all changes and commit with progress message.

    Returns True if a commit was made, False if there was nothing to commit.
    """
    if not has_uncommitted_changes(work_dir):
        logger.info("No changes to commit")
        return False

    # Intentional: git add -A stages all changes, relying on .gitignore
    # to exclude build artifacts, secrets, and other unwanted files.
    _run_git(["add", "-A"], cwd=work_dir)

    message = f"wip: {change_name} - {completed}/{total} tasks complete"
    logger.info(f"Committing progress: {message}")
    _run_git(["commit", "-m", message], cwd=work_dir)
    return True


def get_full_diff(work_dir: Path, base_branch: str = "main") -> str:
    """Get the full diff of the current branch vs base branch."""
    # Try merge-base diff first
    result = _run_git(
        ["diff", f"{base_branch}...HEAD"],
        cwd=work_dir,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout

    # Fallback: diff against base branch directly
    result = _run_git(
        ["diff", base_branch],
        cwd=work_dir,
        check=False,
    )
    return result.stdout


def push_branch(
    branch_name: str,
    work_dir: Path,
    logger: StructuredLogger,
) -> None:
    """Push branch to remote. Pulls and rebases on rejection, retries once."""
    logger.info("Pushing branch", branch=branch_name)
    try:
        _run_git(["push", "-u", "origin", branch_name], cwd=work_dir, timeout=120)
    except GitError:
        logger.warning("Push rejected, attempting pull --rebase then retry")
        _run_git(
            ["pull", "--rebase", "origin", branch_name],
            cwd=work_dir,
            timeout=120,
        )
        _run_git(["push", "-u", "origin", branch_name], cwd=work_dir, timeout=120)


def create_draft_pr(
    branch_name: str,
    change_name: str,
    work_dir: Path,
    logger: StructuredLogger,
    *,
    base_branch: str = "main",
) -> str:
    """Create a draft PR and return its URL.

    If a PR already exists for this branch, returns the existing URL.
    """
    import json as _json

    # Check if PR already exists
    existing = subprocess.run(
        ["gh", "pr", "view", branch_name, "--json", "url"],
        capture_output=True,
        text=True,
        cwd=str(work_dir),
    )
    if existing.returncode == 0:
        pr_data = _json.loads(existing.stdout)
        pr_url = pr_data.get("url", "")
        logger.info("PR already exists", pr_url=pr_url)
        return pr_url

    body = (
        f"## OpenSpec Change: `{change_name}`\n\n"
        "**Status:** In progress\n\n"
        "---\n*Automated by devops-toolkit*"
    )

    title = f"feat: implement {change_name}"
    logger.info("Creating draft PR", title=title)

    result = subprocess.run(
        [
            "gh", "pr", "create",
            "--title", title,
            "--body", body,
            "--base", base_branch,
            "--head", branch_name,
            "--draft",
        ],
        capture_output=True,
        text=True,
        cwd=str(work_dir),
    )

    if result.returncode != 0:
        raise GitError(f"Failed to create draft PR: {result.stderr.strip()}")

    pr_url = result.stdout.strip()
    logger.info("Draft PR created", pr_url=pr_url)
    return pr_url


def comment_on_pr(
    pr_url: str,
    body: str,
    work_dir: Path,
    logger: StructuredLogger,
) -> None:
    """Post a comment on an existing PR."""
    logger.info("Posting PR comment", pr_url=pr_url, body_length=len(body))
    result = subprocess.run(
        ["gh", "pr", "comment", pr_url, "--body", body],
        capture_output=True,
        text=True,
        cwd=str(work_dir),
    )
    if result.returncode != 0:
        logger.warning(
            "Failed to post PR comment",
            error=result.stderr.strip(),
            exit_code=result.returncode,
            pr_url=pr_url,
        )
    else:
        logger.info("Posted PR comment")


def mark_pr_ready(
    pr_url: str,
    work_dir: Path,
    logger: StructuredLogger,
) -> None:
    """Mark a draft PR as ready for review."""
    result = subprocess.run(
        ["gh", "pr", "ready", pr_url],
        capture_output=True,
        text=True,
        cwd=str(work_dir),
    )
    if result.returncode != 0:
        logger.warning("Failed to mark PR ready", error=result.stderr.strip())
    else:
        logger.info("PR marked as ready for review")


def update_pr_body(
    pr_url: str,
    body: str,
    work_dir: Path,
    logger: StructuredLogger,
) -> None:
    """Update the body/description of an existing PR."""
    result = subprocess.run(
        ["gh", "pr", "edit", pr_url, "--body", body],
        capture_output=True,
        text=True,
        cwd=str(work_dir),
    )
    if result.returncode != 0:
        logger.warning("Failed to update PR body", error=result.stderr.strip())


def push_and_create_pr(
    branch_name: str,
    change_name: str,
    pr_body: str,
    work_dir: Path,
    logger: StructuredLogger,
    *,
    base_branch: str = "main",
) -> str:
    """Push branch and create PR. Returns PR URL.

    If PR already exists, updates it instead.
    Kept for backwards compatibility — new code should use the
    individual push_branch / create_draft_pr / mark_pr_ready helpers.
    """
    push_branch(branch_name, work_dir, logger)

    import json as _json

    # Check if PR already exists
    existing = subprocess.run(
        ["gh", "pr", "view", branch_name, "--json", "url"],
        capture_output=True,
        text=True,
        cwd=str(work_dir),
    )

    if existing.returncode == 0:
        pr_data = _json.loads(existing.stdout)
        pr_url = pr_data.get("url", "")
        logger.info("PR already exists, updating", pr_url=pr_url)
        update_pr_body(pr_url, pr_body, work_dir, logger)
        return pr_url

    # Create new PR
    title = f"feat: implement {change_name}"
    logger.info("Creating PR", title=title)

    result = subprocess.run(
        [
            "gh", "pr", "create",
            "--title", title,
            "--body", pr_body,
            "--base", base_branch,
            "--head", branch_name,
        ],
        capture_output=True,
        text=True,
        cwd=str(work_dir),
    )

    if result.returncode != 0:
        raise GitError(f"Failed to create PR: {result.stderr.strip()}")

    pr_url = result.stdout.strip()
    logger.info("PR created", pr_url=pr_url)
    return pr_url
