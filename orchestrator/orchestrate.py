"""OpenSpec Orchestrator - drives Claude CLI to implement spec-driven changes."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from lib.config import Config
from lib.git_ops import (
    GitError,
    clone_repo,
    comment_on_pr,
    commit_progress,
    create_branch,
    create_draft_pr,
    get_full_diff,
    mark_pr_ready,
    push_branch,
    update_pr_body,
)
from lib.history import RunHistory
from lib.log import StructuredLogger
from lib.claude_runner import ClaudeRunner
from lib.progress import ProgressDetector
from lib.reviewer import ClaudeReviewer


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Orchestrate Claude CLI to implement an OpenSpec change.",
    )
    parser.add_argument(
        "change_name",
        help="Name of the OpenSpec change to implement",
    )
    parser.add_argument(
        "--no-clone",
        action="store_true",
        help="Skip repo cloning; work in current directory",
    )
    parser.add_argument(
        "--skip-review",
        action="store_true",
        help="Skip Claude code review phase",
    )
    parser.add_argument(
        "--repo",
        help="Repository to clone (owner/name or URL)",
    )
    parser.add_argument(
        "--base-branch",
        default="main",
        help="Base branch to create the feature branch from (default: main)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Tool validation
# ---------------------------------------------------------------------------

REQUIRED_TOOLS = ["git", "claude"]
REVIEW_TOOLS = ["claude"]
PR_TOOLS = ["gh"]


def _check_tool(name: str) -> bool:
    """Check if a CLI tool is available on PATH."""
    try:
        subprocess.run(
            ["which", name],
            capture_output=True,
            timeout=5,
        )
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def validate_tools(skip_review: bool, logger: StructuredLogger) -> list[str]:
    """Return list of missing required tools."""
    needed = list(REQUIRED_TOOLS)
    if not skip_review:
        needed.extend(REVIEW_TOOLS)
    needed.extend(PR_TOOLS)

    missing = [t for t in needed if not _check_tool(t)]
    for tool in missing:
        logger.error(f"Required tool not found: {tool}")
    return missing


# ---------------------------------------------------------------------------
# Change validation
# ---------------------------------------------------------------------------


def validate_change(change_name: str, work_dir: Path, logger: StructuredLogger) -> bool:
    """Verify the OpenSpec change exists by checking the filesystem.

    Looks for openspec/changes/<change_name>/ with a tasks.md file.
    """
    change_dir = work_dir / "openspec" / "changes" / change_name
    if not change_dir.is_dir():
        logger.error(f"Change directory not found: {change_dir}")
        return False

    tasks_file = change_dir / "tasks.md"
    if not tasks_file.exists():
        logger.error(f"No tasks.md found in {change_dir}")
        return False

    logger.info("Change validated", change=change_name, tasks_file=str(tasks_file))
    return True


# ---------------------------------------------------------------------------
# OpenSpec context reader
# ---------------------------------------------------------------------------


def read_openspec_context(change_name: str, work_dir: Path) -> str:
    """Read proposal + spec files to provide review context."""
    change_dir = work_dir / "openspec" / "changes" / change_name
    parts: list[str] = []

    # Read proposal
    proposal = change_dir / "proposal.md"
    if proposal.exists():
        parts.append(f"## Proposal\n{proposal.read_text()}")

    # Read spec files
    specs_dir = change_dir / "specs"
    if specs_dir.exists():
        for spec_file in sorted(specs_dir.rglob("*.md")):
            parts.append(f"## Spec: {spec_file.stem}\n{spec_file.read_text()}")

    return "\n\n---\n\n".join(parts) if parts else "(no openspec context found)"


# ---------------------------------------------------------------------------
# Remaining tasks extractor
# ---------------------------------------------------------------------------


def extract_remaining_tasks(tasks_md) -> list[str]:
    """Extract incomplete task descriptions from tasks.md.

    Accepts a TasksMdProgress or a ProgressAssessment (reads its .tasks_md).
    """
    # Accept either TasksMdProgress directly or ProgressAssessment
    if hasattr(tasks_md, "tasks_md"):
        tasks_md = tasks_md.tasks_md
    if tasks_md is None or tasks_md.file_path is None:
        return []

    file_path = Path(tasks_md.file_path)
    if not file_path.exists():
        return []

    remaining: list[str] = []
    for line in file_path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            remaining.append(stripped[5:].strip())
    return remaining


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SetupResult:
    work_dir: Path
    branch_name: str
    pr_url: str | None = None


def setup_phase(
    args: argparse.Namespace,
    config: Config,
    logger: StructuredLogger,
) -> SetupResult:
    """Phase 1: Validate tools, validate change, set up repo and branch."""
    logger.phase("setup", "Starting setup phase")

    # Validate tools
    missing = validate_tools(args.skip_review, logger)
    if missing:
        raise SystemExit(f"Missing required tools: {', '.join(missing)}")

    # Determine working directory
    if args.no_clone:
        work_dir = Path.cwd()
        logger.info("Using current directory (--no-clone)", cwd=str(work_dir))
    elif args.repo:
        work_dir = Path.cwd() / args.change_name
        clone_repo(args.repo, work_dir, config.github_token, logger)
    else:
        work_dir = Path.cwd()
        logger.info("No --repo specified, working in current directory")

    # Validate change exists
    if not validate_change(args.change_name, work_dir, logger):
        raise SystemExit(f"OpenSpec change '{args.change_name}' not found or invalid")

    # Create branch
    branch_name = f"openspec/{args.change_name}"
    try:
        create_branch(branch_name, work_dir, logger, base_branch=args.base_branch)
    except GitError as exc:
        logger.warning(f"Branch creation issue (may already exist): {exc}")

    logger.phase("setup", "Setup complete", branch=branch_name)
    return SetupResult(work_dir=work_dir, branch_name=branch_name, pr_url=None)


def _post_implementation_comment(
    setup: SetupResult,
    logger: StructuredLogger,
    *,
    attempt: int,
    max_attempts: int,
    tasks_before: str,
    tasks_after: str,
    duration_seconds: float,
    exit_code: int,
    has_diff: bool,
    is_complete: bool,
    is_stuck: bool,
    remaining_tasks: list[str] | None = None,
    previous_errors: list[str] | None = None,
) -> None:
    """Post a PR comment summarizing an implementation attempt."""
    if not setup.pr_url:
        return

    status = "completed" if exit_code == 0 else f"exited with code {exit_code}"

    # Determine what happens next
    if is_complete:
        next_step = ":white_check_mark: All tasks complete — moving to review phase"
    elif is_stuck:
        next_step = ":x: Stuck — no progress in consecutive runs, aborting"
    elif attempt >= max_attempts:
        next_step = ":x: Max attempts reached, aborting"
    else:
        next_step = f":repeat: Retrying (attempt {attempt + 1}/{max_attempts})"

    lines = [
        f"### Implementation attempt {attempt}/{max_attempts}\n",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Status | {status} |",
        f"| Tasks | {tasks_before} → {tasks_after} |",
        f"| Duration | {duration_seconds:.0f}s |",
        f"| Changes | {'yes' if has_diff else 'no new changes'} |",
        f"| Next | {next_step} |",
    ]

    if remaining_tasks:
        lines.append(f"\n**Remaining tasks ({len(remaining_tasks)}):**")
        for task in remaining_tasks[:10]:
            lines.append(f"- [ ] {task}")
        if len(remaining_tasks) > 10:
            lines.append(f"- ... and {len(remaining_tasks) - 10} more")

    if previous_errors:
        lines.append("\n**Recent errors:**")
        for err in previous_errors:
            lines.append(f"```\n{err}\n```")

    comment_on_pr(setup.pr_url, "\n".join(lines), setup.work_dir, logger)


def _ensure_pr(
    setup: SetupResult,
    args: argparse.Namespace,
    logger: StructuredLogger,
) -> SetupResult:
    """Create draft PR after first push if not yet created. Returns updated SetupResult."""
    if setup.pr_url:
        return setup

    try:
        pr_url = create_draft_pr(
            setup.branch_name, args.change_name, setup.work_dir, logger,
            base_branch=args.base_branch,
        )
        return SetupResult(
            work_dir=setup.work_dir,
            branch_name=setup.branch_name,
            pr_url=pr_url,
        )
    except (GitError, Exception) as exc:
        logger.warning(f"Could not create draft PR: {exc}")
        return setup


def _check_time_budget(
    start_time: float,
    config: Config,
    logger: StructuredLogger,
    phase: str,
) -> None:
    """Abort if we've exceeded the time budget."""
    elapsed = time.monotonic() - start_time
    if elapsed >= config.time_budget_seconds:
        remaining = config.time_budget_seconds - elapsed
        raise SystemExit(
            f"Time budget exhausted during {phase} phase "
            f"({elapsed:.0f}s elapsed, budget={config.time_budget_seconds}s). "
            "Aborting to avoid GHA timeout."
        )


def implementation_phase(
    args: argparse.Namespace,
    config: Config,
    setup: SetupResult,
    logger: StructuredLogger,
    history: RunHistory,
    run_id: str,
    start_time: float,
) -> tuple[int, SetupResult]:
    """Phase 2: Run Claude in a retry loop until all tasks complete.

    Returns (total_attempts, updated_setup with pr_url).
    """
    logger.phase("implementation", "Starting implementation phase")

    runner = ClaudeRunner(config, setup.work_dir, logger)
    detector = ProgressDetector(args.change_name, setup.work_dir, logger)
    total_attempts = 0
    previous_errors: list[str] = []
    consecutive_push_failures = 0

    for attempt in range(1, config.max_implementation_attempts + 1):
        _check_time_budget(start_time, config, logger, "implementation")
        total_attempts = attempt
        logger.info(f"Implementation attempt {attempt}/{config.max_implementation_attempts}")

        # Check current progress before running (lightweight, no stuck tracking)
        pre_tasks = detector.check_tasks_md()
        tasks_before = f"{pre_tasks.completed}/{pre_tasks.total}" if pre_tasks else "0/0"

        # Log attempt
        attempt_id = history.log_attempt(
            run_id, attempt, "claude", tasks_before=tasks_before,
        )

        # Compute remaining tasks (used for retry prompt and PR comment)
        remaining = extract_remaining_tasks(pre_tasks) if pre_tasks else []

        # Run Claude
        if attempt == 1:
            result = runner.run(args.change_name)
        else:
            result = runner.run_with_context(
                args.change_name,
                remaining_tasks=remaining,
                previous_errors=previous_errors if previous_errors else None,
            )

        # Track errors from this run
        if result.exit_code != 0 and result.stderr:
            previous_errors.append(result.stderr[:200])
            # Keep only last 3 errors
            previous_errors = previous_errors[-3:]

        # Check progress after run
        post_progress = detector.assess_progress()
        tasks_after = f"{post_progress.completed}/{post_progress.total}"
        remaining_after = extract_remaining_tasks(post_progress)

        # Update attempt record
        history.update_attempt(
            attempt_id,
            exit_code=result.exit_code,
            duration_seconds=result.duration_seconds,
            tasks_after=tasks_after,
            has_diff=post_progress.has_meaningful_diff,
        )

        # Commit any changes
        committed = commit_progress(
            args.change_name,
            post_progress.completed,
            post_progress.total,
            setup.work_dir,
            logger,
        )

        # Push and create draft PR on first commit
        if committed:
            try:
                push_branch(setup.branch_name, setup.work_dir, logger)
                setup = _ensure_pr(setup, args, logger)
                consecutive_push_failures = 0
            except GitError as exc:
                consecutive_push_failures += 1
                logger.warning(
                    f"Push failed ({consecutive_push_failures}x consecutive): {exc}",
                )
                if consecutive_push_failures >= 3:
                    raise SystemExit(
                        f"Aborting: {consecutive_push_failures} consecutive push failures. "
                        "Remote branch has diverged beyond recovery."
                    )

        _post_implementation_comment(
            setup, logger,
            attempt=attempt,
            max_attempts=config.max_implementation_attempts,
            tasks_before=tasks_before,
            tasks_after=tasks_after,
            duration_seconds=result.duration_seconds,
            exit_code=result.exit_code,
            has_diff=post_progress.has_meaningful_diff,
            is_complete=post_progress.is_complete,
            is_stuck=post_progress.is_stuck,
            remaining_tasks=remaining_after if remaining_after else None,
            previous_errors=previous_errors if previous_errors else None,
        )

        # Log progress
        logger.progress(
            attempt=attempt,
            completed=post_progress.completed,
            total=post_progress.total,
            has_diff=post_progress.has_meaningful_diff,
        )

        # Check completion
        if post_progress.is_complete:
            logger.phase("implementation", "All tasks complete!")
            return total_attempts, setup

        # Check if stuck
        if post_progress.is_stuck:
            raise SystemExit(
                f"Stuck: {detector.consecutive_no_progress} consecutive runs with no progress"
            )

    raise SystemExit(
        f"Max implementation attempts ({config.max_implementation_attempts}) reached. "
        f"Progress: {tasks_after}"
    )


def _format_review_comment(
    cycle: int,
    max_cycles: int,
    review: "ReviewResult",
) -> str:
    """Format a review result as a PR comment."""
    status = "Approved" if review.approved else "Changes requested"
    icon = "white_check_mark" if review.approved else "x"

    lines = [
        f"### Review cycle {cycle}/{max_cycles} — :{icon}: {status}\n",
        f"**Summary:** {review.summary}\n",
    ]

    if review.findings:
        lines.append(f"**Findings ({len(review.findings)}):**\n")
        for f in review.findings:
            severity = f.get("severity", "?").upper()
            category = f.get("category", "")
            msg = f.get("message", "")
            file_path = f.get("file", "")
            line_num = f.get("line")
            suggestion = f.get("suggestion", "")
            location = f"`{file_path}"
            if line_num:
                location += f":{line_num}"
            location += "`"
            cat_label = f" ({category})" if category else ""
            lines.append(f"- **[{severity}]**{cat_label} {location}: {msg}")
            if suggestion:
                lines.append(f"  - *Suggestion:* {suggestion}")

    if review.parse_error:
        lines.append(f"\n> **Parse warning:** {review.parse_error}")

    return "\n".join(lines)


def review_phase(
    args: argparse.Namespace,
    config: Config,
    setup: SetupResult,
    logger: StructuredLogger,
    history: RunHistory,
    run_id: str,
    impl_attempts: int,
    start_time: float,
) -> tuple[int, bool]:
    """Phase 3: Claude reviews, Claude fixes if needed.

    Returns (total_review_cycles, was_approved).
    """
    logger.phase("review", "Starting review phase")

    reviewer = ClaudeReviewer(config, setup.work_dir, logger)
    runner = ClaudeRunner(config, setup.work_dir, logger)
    detector = ProgressDetector(args.change_name, setup.work_dir, logger)

    openspec_context = read_openspec_context(args.change_name, setup.work_dir)
    total_cycles = 0
    consecutive_push_failures = 0

    for cycle in range(1, config.max_review_cycles + 1):
        _check_time_budget(start_time, config, logger, "review")
        total_cycles = cycle
        logger.info(f"Review cycle {cycle}/{config.max_review_cycles}")

        # Get diff for review
        git_diff = get_full_diff(setup.work_dir, args.base_branch)

        # Log review attempt
        attempt_id = history.log_attempt(run_id, impl_attempts + cycle, "claude_review")

        # Run review
        review = reviewer.review(args.change_name, openspec_context, git_diff)

        # Update attempt record
        history.update_attempt(
            attempt_id,
            approved=review.approved,
            findings_count=len(review.findings),
        )

        logger.review_result(
            cycle=cycle,
            approved=review.approved,
            findings_count=len(review.findings),
        )

        # Post review results as PR comment
        if setup.pr_url:
            review_comment = _format_review_comment(cycle, config.max_review_cycles, review)
            comment_on_pr(setup.pr_url, review_comment, setup.work_dir, logger)

        if review.approved:
            logger.phase("review", "Review approved!")
            return total_cycles, True

        # If rejected but no findings, something went wrong with parsing
        if not review.findings:
            if review.parse_error:
                logger.warning(
                    "Review parse error with no findings, skipping fix cycle",
                    parse_error=review.parse_error,
                )
                continue
            # Rejected with no findings and no parse error = treat as approved
            logger.info("Review rejected but no findings reported, treating as approved")
            return total_cycles, True

        # Not approved with actual findings — run Claude with fix instructions
        logger.info("Review rejected, running fixes", findings_count=len(review.findings))

        fix_attempt_id = history.log_attempt(
            run_id, impl_attempts + cycle, "claude_fix",
        )

        fix_result = runner.run_with_fixes(args.change_name, review.findings)

        # Check for changes and commit
        post_fix = detector.assess_progress()
        history.update_attempt(
            fix_attempt_id,
            exit_code=fix_result.exit_code,
            duration_seconds=fix_result.duration_seconds,
            has_diff=post_fix.has_meaningful_diff,
        )

        committed = commit_progress(
            args.change_name,
            post_fix.completed,
            post_fix.total,
            setup.work_dir,
            logger,
        )

        # Push fix commits and post comment
        if committed:
            try:
                push_branch(setup.branch_name, setup.work_dir, logger)
                consecutive_push_failures = 0
            except GitError as exc:
                consecutive_push_failures += 1
                logger.warning(
                    f"Push failed ({consecutive_push_failures}x consecutive): {exc}",
                )
                if consecutive_push_failures >= 3:
                    logger.warning("Aborting review: repeated push failures")
                    break

        if setup.pr_url:
            fix_body = (
                f"### Fix cycle {cycle} complete\n\n"
                f"| Metric | Value |\n"
                f"|--------|-------|\n"
                f"| Duration | {fix_result.duration_seconds:.0f}s |\n"
                f"| Changes | {'yes' if post_fix.has_meaningful_diff else 'no new changes'} |\n"
                f"| Tasks | {post_fix.completed}/{post_fix.total} |"
            )
            comment_on_pr(setup.pr_url, fix_body, setup.work_dir, logger)

    logger.warning(f"Max review cycles ({config.max_review_cycles}) reached without full approval")
    return total_cycles, False


def finalize_phase(
    args: argparse.Namespace,
    setup: SetupResult,
    logger: StructuredLogger,
    was_approved: bool,
    review_findings: str | None = None,
) -> str:
    """Phase 4: Final push, update PR body, and mark ready. Returns PR URL."""
    logger.phase("finalize", "Starting finalize phase")

    # Build final PR body
    body_parts = [f"## OpenSpec Change: `{args.change_name}`\n"]

    if was_approved:
        body_parts.append("**Review:** :white_check_mark: Approved by Claude\n")
    else:
        body_parts.append("**Review:** :warning: Max review cycles reached (not fully approved)\n")
        if review_findings:
            body_parts.append(f"### Remaining Findings\n{review_findings}\n")

    body_parts.append("---\n*Automated by devops-toolkit*")
    pr_body = "\n".join(body_parts)

    # Final push
    try:
        push_branch(setup.branch_name, setup.work_dir, logger)
    except GitError as exc:
        logger.warning(f"Final push failed: {exc}")

    if setup.pr_url:
        # Update the existing draft PR body and mark it ready
        update_pr_body(setup.pr_url, pr_body, setup.work_dir, logger)
        mark_pr_ready(setup.pr_url, setup.work_dir, logger)
        logger.phase("finalize", "PR marked ready", pr_url=setup.pr_url)
        return setup.pr_url

    # Fallback: no draft PR was created earlier, create one now
    from lib.git_ops import push_and_create_pr
    pr_url = push_and_create_pr(
        branch_name=setup.branch_name,
        change_name=args.change_name,
        pr_body=pr_body,
        work_dir=setup.work_dir,
        logger=logger,
        base_branch=args.base_branch,
    )
    logger.phase("finalize", "PR created", pr_url=pr_url)
    return pr_url


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> int:
    args = parse_args()
    config = Config.from_env()

    # Validate config
    config_errors = config.validate()
    if config_errors:
        for err in config_errors:
            print(f"Config error: {err}", file=sys.stderr)
        return 1

    logger = StructuredLogger(args.change_name)
    history = RunHistory(config.db_path)

    run_id = history.start_run(
        change_name=args.change_name,
        repo=args.repo,
        branch=f"openspec/{args.change_name}",
        config={
            "max_implementation_attempts": config.max_implementation_attempts,
            "max_review_cycles": config.max_review_cycles,
            "skip_review": args.skip_review,
        },
    )

    start_time = time.monotonic()

    try:
        # Phase 1: Setup
        setup = setup_phase(args, config, logger)

        # Phase 2: Implementation
        impl_attempts, setup = implementation_phase(
            args, config, setup, logger, history, run_id, start_time,
        )

        # Phase 3: Review (unless skipped)
        was_approved = True
        total_review_cycles = 0
        if not args.skip_review:
            total_review_cycles, was_approved = review_phase(
                args, config, setup, logger, history, run_id, impl_attempts,
                start_time,
            )
        else:
            logger.info("Skipping review phase (--skip-review)")

        # Phase 4: Finalize
        pr_url = finalize_phase(args, setup, logger, was_approved)

        # Record success
        history.complete_run(
            run_id,
            pr_url=pr_url,
            total_attempts=impl_attempts,
            total_review_cycles=total_review_cycles,
        )
        logger.info("Orchestration complete", pr_url=pr_url)
        return 0

    except SystemExit as exc:
        error_msg = str(exc)
        logger.fatal(error_msg)
        history.fail_run(run_id, error_msg, total_attempts=0)
        return 1
    except Exception as exc:
        error_msg = f"Unexpected error: {exc}"
        logger.fatal(error_msg)
        history.fail_run(run_id, error_msg, total_attempts=0)
        return 1
    finally:
        history.close()


if __name__ == "__main__":
    raise SystemExit(main())
