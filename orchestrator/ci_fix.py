"""Self-healing CI: fetch failure logs, run Claude to fix, push the result."""

from __future__ import annotations

import argparse
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from lib.git_ops import comment_on_pr, push_branch, GitError
from lib.log import StructuredLogger


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CIFailure:
    """A single CI job failure with its error log."""
    job_name: str
    step_name: str
    log: str


@dataclass(frozen=True)
class FixResult:
    """Result of a Claude fix attempt."""
    exit_code: int
    duration_seconds: float
    timed_out: bool


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Self-healing CI: fetch failure logs and run Claude to fix.",
    )
    parser.add_argument(
        "run_id",
        help="GitHub Actions run ID that failed",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Repository in owner/name format",
    )
    parser.add_argument(
        "--branch",
        required=True,
        help="Branch name to fix (must already be checked out)",
    )
    parser.add_argument(
        "--pr-url",
        default=None,
        help="PR URL to post comments to (optional)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Max fix attempts before giving up (default: 3)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Timeout for each Claude invocation in seconds (default: 900)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Fetch CI failure logs
# ---------------------------------------------------------------------------

def fetch_failed_jobs(run_id: str, repo: str, logger: StructuredLogger) -> list[CIFailure]:
    """Fetch failure logs from a GitHub Actions run."""
    # Get failed job details
    result = subprocess.run(
        [
            "gh", "run", "view", run_id,
            "--repo", repo,
            "--json", "jobs",
            "--jq", '.jobs[] | select(.conclusion == "failure") | {name, steps: [.steps[] | select(.conclusion == "failure") | {name, number}]}',
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        logger.error("Failed to fetch run details", stderr=result.stderr[:500])
        return []

    # Get the failed log output
    log_result = subprocess.run(
        ["gh", "run", "view", run_id, "--repo", repo, "--log-failed"],
        capture_output=True,
        text=True,
    )

    if log_result.returncode != 0:
        logger.error("Failed to fetch failure logs", stderr=log_result.stderr[:500])
        return []

    raw_log = log_result.stdout

    # Parse the log into per-job failures
    # gh log-failed format: "JobName\tStepName\tTimestamp Message"
    failures: dict[str, list[str]] = {}

    for line in raw_log.splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 3:
            job_name, step_name = parts[0], parts[1]
            msg = parts[2]
            key = f"{job_name}::{step_name}"
            if key not in failures:
                failures[key] = []
            failures[key].append(msg)

    result_failures = []
    for key, lines in failures.items():
        job_name, step_name = key.split("::", 1)
        # Keep last 100 lines per job (most relevant errors are at the end)
        log_text = "\n".join(lines[-100:])
        result_failures.append(CIFailure(
            job_name=job_name,
            step_name=step_name,
            log=log_text,
        ))

    logger.info(
        "Fetched CI failures",
        run_id=run_id,
        failed_jobs=len(result_failures),
    )
    return result_failures


# ---------------------------------------------------------------------------
# Build fix prompt
# ---------------------------------------------------------------------------

def build_fix_prompt(failures: list[CIFailure]) -> str:
    """Build a prompt for Claude to fix CI failures."""
    parts = [
        "The CI pipeline failed. Fix ALL of the following errors. "
        "Do NOT skip any failure — every job listed below must pass after your fix.\n"
    ]

    for i, failure in enumerate(failures, 1):
        parts.append(
            f"## Failure {i}: {failure.job_name} / {failure.step_name}\n"
            f"```\n{failure.log}\n```\n"
        )

    parts.append(
        "## Instructions\n"
        "1. Read the error messages carefully — fix the root cause, not symptoms.\n"
        "2. If a test fails, fix the implementation (not the test) unless the test is wrong.\n"
        "3. If a type error occurs, fix the types to match the actual usage.\n"
        "4. If a build fails, check imports, missing dependencies, and environment variables.\n"
        "5. Keep changes minimal — only touch what's needed to fix the CI errors.\n"
        "6. Run the relevant check locally if possible (e.g., `npm run type-check`, `npm run lint`, `npm test`).\n"
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Run Claude fix
# ---------------------------------------------------------------------------

def run_claude_fix(
    prompt: str,
    work_dir: Path,
    timeout: int,
    logger: StructuredLogger,
) -> FixResult:
    """Run Claude with the fix prompt."""
    cmd = [
        "claude", "-p", prompt,
        "--model", "claude-sonnet-4-5-20250514",
        "--max-turns", "30",
        "--output-format", "text",
    ]

    logger.info("Starting Claude CI fix", prompt_length=len(prompt))
    start = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(work_dir),
        )
        duration = time.monotonic() - start

        logger.info(
            "Claude fix finished",
            exit_code=result.returncode,
            duration_seconds=round(duration, 1),
        )

        return FixResult(
            exit_code=result.returncode,
            duration_seconds=duration,
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        logger.warning("Claude fix timed out", timeout=timeout)
        return FixResult(
            exit_code=-1,
            duration_seconds=duration,
            timed_out=True,
        )


# ---------------------------------------------------------------------------
# Commit and push
# ---------------------------------------------------------------------------

def commit_fix(
    work_dir: Path,
    attempt: int,
    logger: StructuredLogger,
) -> bool:
    """Stage and commit CI fix changes. Returns True if committed."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=str(work_dir),
    )
    if not result.stdout.strip():
        logger.info("No changes to commit after fix attempt")
        return False

    add_result = subprocess.run(
        ["git", "add", "-A"],
        capture_output=True,
        text=True,
        cwd=str(work_dir),
    )
    if add_result.returncode != 0:
        logger.error("git add failed", stderr=add_result.stderr[:500])
        return False

    message = f"fix: CI auto-heal attempt {attempt}"
    commit_result = subprocess.run(
        ["git", "commit", "-m", message],
        capture_output=True,
        text=True,
        cwd=str(work_dir),
    )
    if commit_result.returncode != 0:
        logger.error("git commit failed", stderr=commit_result.stderr[:500])
        return False

    logger.info("Committed CI fix", attempt=attempt)
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    logger = StructuredLogger(f"ci-fix-{args.run_id}")
    work_dir = Path.cwd()

    logger.info(
        "Starting CI self-heal",
        run_id=args.run_id,
        repo=args.repo,
        branch=args.branch,
    )

    # Fetch failure details
    failures = fetch_failed_jobs(args.run_id, args.repo, logger)
    if not failures:
        logger.error("No failure logs found — nothing to fix")
        return 1

    # Filter out the CI Status meta-job (it just reports other failures)
    failures = [f for f in failures if f.job_name != "CI Status"]
    if not failures:
        logger.error("Only CI Status job failed — no actionable failures")
        return 1

    # Post initial comment
    if args.pr_url:
        job_list = "\n".join(f"- {f.job_name} / {f.step_name}" for f in failures)
        comment_on_pr(
            args.pr_url,
            f"### CI Auto-Heal Triggered\n\n"
            f"**Run:** [{args.run_id}](https://github.com/{args.repo}/actions/runs/{args.run_id})\n"
            f"**Failed jobs:**\n{job_list}\n\n"
            f"Attempting to fix automatically (max {args.max_attempts} attempts)...",
            work_dir,
            logger,
        )

    prompt = build_fix_prompt(failures)

    for attempt in range(1, args.max_attempts + 1):
        logger.info(f"Fix attempt {attempt}/{args.max_attempts}")

        fix_result = run_claude_fix(prompt, work_dir, args.timeout, logger)

        committed = commit_fix(work_dir, attempt, logger)
        if committed:
            try:
                push_branch(args.branch, work_dir, logger)
            except GitError as exc:
                logger.warning(f"Push failed: {exc}")

            if args.pr_url:
                status = "timed out" if fix_result.timed_out else "completed"
                comment_on_pr(
                    args.pr_url,
                    f"### CI Fix Attempt {attempt}/{args.max_attempts}\n\n"
                    f"| Metric | Value |\n"
                    f"|--------|-------|\n"
                    f"| Status | {status} |\n"
                    f"| Duration | {fix_result.duration_seconds:.0f}s |\n"
                    f"| Changes | pushed |\n\n"
                    f"CI will re-run automatically on the new push.",
                    work_dir,
                    logger,
                )

            logger.info(
                "Fix pushed — CI will re-run",
                attempt=attempt,
                duration=round(fix_result.duration_seconds, 1),
            )
            return 0
        else:
            logger.warning(
                "Claude made no changes",
                attempt=attempt,
            )

            if attempt < args.max_attempts:
                # Add a single retry prefix (replace if already present)
                retry_prefix = (
                    "The previous fix attempt produced NO changes. "
                    "The CI is still failing. You MUST modify files to fix these errors.\n\n"
                )
                if not prompt.startswith(retry_prefix):
                    prompt = retry_prefix + prompt

    # All attempts exhausted
    if args.pr_url:
        comment_on_pr(
            args.pr_url,
            f"### CI Auto-Heal Failed\n\n"
            f"Exhausted {args.max_attempts} fix attempts without producing a working fix.\n"
            f"Manual intervention required.",
            work_dir,
            logger,
        )

    logger.error(f"All {args.max_attempts} fix attempts exhausted")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
