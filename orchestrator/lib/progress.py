"""Multi-signal progress detection for OpenSpec task completion."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.log import StructuredLogger


@dataclass(frozen=True)
class OpenSpecProgress:
    """Progress from the openspec CLI."""

    state: str  # "all_done", "blocked", "ready", etc.
    completed: int
    total: int
    remaining: int
    tasks_file: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class TasksMdProgress:
    """Progress from parsing tasks.md checkboxes."""

    completed: int
    total: int
    file_path: str | None = None


@dataclass(frozen=True)
class GitDiffResult:
    """Result of git diff analysis."""

    has_changes: bool
    has_meaningful_changes: bool  # excludes whitespace-only
    files_changed: int


@dataclass(frozen=True)
class ProgressAssessment:
    """Combined assessment from all signals."""

    is_complete: bool
    completed: int
    total: int
    has_meaningful_diff: bool
    is_stuck: bool
    openspec: OpenSpecProgress | None
    tasks_md: TasksMdProgress | None
    git_diff: GitDiffResult | None


class ProgressDetector:
    """Combines openspec CLI, tasks.md parsing, and git diff to assess progress."""

    def __init__(
        self,
        change_name: str,
        work_dir: Path,
        logger: StructuredLogger,
        max_consecutive_no_progress: int = 3,
    ) -> None:
        self._change_name = change_name
        self._work_dir = work_dir
        self._log = logger
        self._max_consecutive_no_progress = max_consecutive_no_progress
        self._consecutive_no_progress = 0
        self._last_completed: int | None = None

    @property
    def consecutive_no_progress(self) -> int:
        return self._consecutive_no_progress

    def check_openspec_cli(self) -> OpenSpecProgress | None:
        """Run openspec CLI to get structured progress data.

        The openspec CLI is optional. If unavailable, returns None and the
        orchestrator falls back to tasks.md checkbox parsing.
        """
        try:
            result = subprocess.run(
                [
                    "openspec", "instructions", "apply",
                    "--change", self._change_name,
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self._work_dir),
            )
            if result.returncode != 0:
                self._log.warning(
                    "openspec CLI failed",
                    exit_code=result.returncode,
                    stderr=result.stderr[:500],
                )
                return None

            data = json.loads(result.stdout)
            progress = data.get("progress", {})
            total = progress.get("total", 0)
            complete = progress.get("complete", 0)
            remaining = progress.get("remaining", total - complete)

            # Find tasks file from contextFiles
            tasks_file = None
            context_files = data.get("contextFiles", {})
            if isinstance(context_files, dict):
                tasks_file = context_files.get("tasks")
            elif isinstance(context_files, list):
                for ctx in context_files:
                    if isinstance(ctx, str) and "tasks" in ctx.lower():
                        tasks_file = ctx
                        break

            return OpenSpecProgress(
                state=data.get("state", "unknown"),
                completed=complete,
                total=total,
                remaining=remaining,
                tasks_file=tasks_file,
                raw=data,
            )
        except FileNotFoundError:
            self._log.warning("openspec CLI not found, falling back to tasks.md parsing")
            return None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as exc:
            self._log.warning(f"openspec CLI error: {exc}")
            return None

    def check_tasks_md(self, tasks_file: str | None = None) -> TasksMdProgress | None:
        """Parse tasks markdown file for checkbox counts."""
        if tasks_file is None:
            # Try default location
            candidates = [
                c for c in self._work_dir.glob("**/tasks.md")
                if "node_modules" not in c.parts
            ]
            openspec_candidates = [
                c for c in candidates
                if "openspec" in str(c) and self._change_name in str(c)
            ]
            if openspec_candidates:
                tasks_file = str(openspec_candidates[0])
            elif candidates:
                self._log.warning("Using fallback tasks.md search (no openspec match)")
                tasks_file = str(candidates[0])
            else:
                self._log.warning("No tasks.md file found")
                return None

        file_path = Path(tasks_file)
        if not file_path.is_absolute():
            file_path = self._work_dir / file_path

        if not file_path.is_file():
            self._log.warning(f"Tasks file not found or not a file: {file_path}")
            return None

        content = file_path.read_text()
        completed = len(re.findall(r"- \[x\]", content, re.IGNORECASE))
        incomplete = len(re.findall(r"- \[ \]", content))
        total = completed + incomplete

        return TasksMdProgress(
            completed=completed,
            total=total,
            file_path=str(file_path),
        )

    def check_git_diff(self) -> GitDiffResult:
        """Check git diff for meaningful code changes."""
        try:
            # Full diff
            full = subprocess.run(
                ["git", "diff", "--stat"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(self._work_dir),
            )
            has_changes = bool(full.stdout.strip())

            # Count files from stat output
            files_changed = 0
            if has_changes:
                lines = full.stdout.strip().split("\n")
                # Last line is summary like " 3 files changed, 10 insertions(+)"
                for line in lines[:-1]:
                    if "|" in line:
                        files_changed += 1

            # Whitespace-ignored diff
            ws_ignored = subprocess.run(
                ["git", "diff", "-w", "--stat"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=str(self._work_dir),
            )
            has_meaningful = bool(ws_ignored.stdout.strip())

            return GitDiffResult(
                has_changes=has_changes,
                has_meaningful_changes=has_meaningful,
                files_changed=files_changed,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            # If git is unavailable, assume changes exist to avoid false negatives
            return GitDiffResult(
                has_changes=True,
                has_meaningful_changes=True,
                files_changed=0,
            )

    def assess_progress(self) -> ProgressAssessment:
        """Combine all signals into a single progress assessment."""
        openspec = self.check_openspec_cli()
        git_diff = self.check_git_diff()

        # Get tasks file from openspec if available
        tasks_file = openspec.tasks_file if openspec else None
        tasks_md = self.check_tasks_md(tasks_file)

        # Determine completion — trust openspec CLI as authoritative
        if openspec is not None:
            is_complete = openspec.state == "all_done"
            completed = openspec.completed
            total = openspec.total
        elif tasks_md is not None:
            is_complete = tasks_md.completed == tasks_md.total and tasks_md.total > 0
            completed = tasks_md.completed
            total = tasks_md.total
        else:
            is_complete = False
            completed = 0
            total = 0

        # Log discrepancies
        if openspec and tasks_md and openspec.total > 0:
            if openspec.completed != tasks_md.completed:
                self._log.warning(
                    "Progress signal mismatch",
                    openspec_completed=openspec.completed,
                    tasks_md_completed=tasks_md.completed,
                )

        # Track no-progress streak based on actual task completion increase
        has_meaningful = git_diff.has_meaningful_changes
        tasks_increased = (
            self._last_completed is not None
            and completed > self._last_completed
        )

        if self._last_completed is None:
            # First assessment — no stuck check yet
            made_progress = True
        elif tasks_increased:
            made_progress = True
        elif has_meaningful:
            # Files changed but no new tasks completed — count as partial
            # (allows 1 grace attempt but not infinite)
            made_progress = False
        else:
            made_progress = False

        self._last_completed = completed

        if made_progress:
            self._consecutive_no_progress = 0
        else:
            self._consecutive_no_progress += 1

        is_stuck = self._consecutive_no_progress >= self._max_consecutive_no_progress

        return ProgressAssessment(
            is_complete=is_complete,
            completed=completed,
            total=total,
            has_meaningful_diff=has_meaningful,
            is_stuck=is_stuck,
            openspec=openspec,
            tasks_md=tasks_md,
            git_diff=git_diff,
        )
