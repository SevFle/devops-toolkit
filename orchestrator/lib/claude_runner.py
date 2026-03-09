"""Claude CLI subprocess runner for one-shot non-interactive invocations."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from lib.config import Config
from lib.log import StructuredLogger


@dataclass(frozen=True)
class ClaudeResult:
    """Immutable result of a Claude CLI invocation."""

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


class ClaudeRunner:
    """Spawns Claude CLI in non-interactive mode and captures results."""

    def __init__(
        self,
        config: Config,
        work_dir: Path,
        logger: StructuredLogger,
        model: str = "zai-coding-plan/glm-5",
    ) -> None:
        self._config = config
        self._work_dir = work_dir
        self._log = logger
        self._model = model

    def _tasks_file_path(self, change_name: str) -> Path:
        """Return the canonical tasks.md path for a change."""
        return self._work_dir / "openspec" / "changes" / change_name / "tasks.md"

    def _checklist_sync_instructions(self, change_name: str) -> str:
        """Return strict checklist-sync instructions for the implementation agent."""
        tasks_path = self._tasks_file_path(change_name)
        relative_path = tasks_path.relative_to(self._work_dir)
        return (
            "Checklist sync is mandatory.\n"
            f"- Keep `{relative_path}` synchronized with the real code state during this run.\n"
            "- As soon as you fully complete a task, immediately change its checkbox from `- [ ]` to `- [x]` before moving to the next task.\n"
            "- Do not batch checkbox updates at the end of the run.\n"
            "- Do not mark partial work as complete.\n"
            "- Before finishing, re-open the tasks file and verify every completed task is checked off and every incomplete task is still unchecked."
        )

    def _build_run_prompt(self, change_name: str, tasks_content: str | None) -> str:
        """Build the initial implementation prompt."""
        parts = [f"Implement the OpenSpec change '{change_name}'."]

        if tasks_content:
            parts.append(f"Here are the tasks from tasks.md:\n\n{tasks_content}")
            parts.append("Complete all incomplete tasks (marked with '- [ ]').")
        else:
            parts.append("Complete all tasks.")

        parts.append(self._checklist_sync_instructions(change_name))
        return "\n\n".join(parts)

    def _build_retry_prompt(
        self,
        change_name: str,
        tasks_content: str | None,
        remaining_tasks: list[str],
        previous_errors: list[str] | None,
    ) -> str:
        """Build a retry prompt with explicit checklist resynchronization."""
        parts = [f"Continue implementing the OpenSpec change '{change_name}'."]

        if tasks_content:
            parts.append(f"Here are the tasks from tasks.md:\n\n{tasks_content}")

        parts.append(
            "First, sync the checklist with the current repository state: if code already completes a task, check it off in tasks.md before starting new work."
        )

        if remaining_tasks:
            tasks_str = "\n".join(f"  - {t}" for t in remaining_tasks[:10])
            parts.append(f"These tasks remain incomplete:\n{tasks_str}")

        if previous_errors:
            errors_str = "\n".join(f"  - {e}" for e in previous_errors[:5])
            parts.append(f"Previous errors to avoid:\n{errors_str}")

        parts.append(self._checklist_sync_instructions(change_name))
        parts.append("Complete as many remaining tasks as possible.")
        return "\n\n".join(parts)

    def run(self, change_name: str) -> ClaudeResult:
        """Run Claude with instructions to implement the given change.

        Reads the openspec tasks file and asks Claude to implement remaining tasks.
        """
        tasks_file = self._tasks_file_path(change_name)
        tasks_content = tasks_file.read_text() if tasks_file.exists() else None
        prompt = self._build_run_prompt(change_name, tasks_content)
        return self._execute(prompt)

    def run_with_context(
        self,
        change_name: str,
        remaining_tasks: list[str],
        previous_errors: list[str] | None = None,
    ) -> ClaudeResult:
        """Run Claude with additional context about remaining work."""
        tasks_file = self._tasks_file_path(change_name)
        tasks_content = tasks_file.read_text() if tasks_file.exists() else None
        prompt = self._build_retry_prompt(
            change_name,
            tasks_content,
            remaining_tasks,
            previous_errors,
        )
        return self._execute(prompt)

    def run_with_fixes(
        self,
        change_name: str,
        findings: list[dict],
    ) -> ClaudeResult:
        """Run Claude with review findings to fix issues."""
        parts = [f"Fix the following review findings for OpenSpec change '{change_name}':"]

        for i, finding in enumerate(findings[:10], 1):
            severity = finding.get("severity", "medium")
            message = finding.get("message", "No description")
            file_path = finding.get("file", "")
            suggestion = finding.get("suggestion", "")

            entry = f"{i}. [{severity}] {message}"
            if file_path:
                entry += f"\n   File: {file_path}"
            if suggestion:
                entry += f"\n   Suggestion: {suggestion}"
            parts.append(entry)

        parts.append(
            "Keep the OpenSpec checklist accurate while applying fixes: if a fix completes work for an unchecked task, update tasks.md immediately; do not batch checkbox updates."
        )
        parts.append(self._checklist_sync_instructions(change_name))
        parts.append("\nFix all issues listed above. Keep changes minimal and focused.")
        prompt = "\n\n".join(parts)
        return self._execute(prompt)

    def _execute(self, prompt: str) -> ClaudeResult:
        """Execute Claude CLI in non-interactive mode."""
        cmd = [
            "opencode", "run",
            "--model", self._model,
            prompt,
        ]

        self._log.info("Starting Claude", prompt_preview=prompt[:200])
        start = time.monotonic()

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._config.claude_timeout,
                cwd=str(self._work_dir),
            )
            duration = time.monotonic() - start

            log_kwargs = {
                "exit_code": result.returncode,
                "duration_seconds": round(duration, 1),
            }
            if result.returncode != 0 and result.stderr:
                log_kwargs["stderr"] = result.stderr[:500]
            self._log.info("Claude finished", **log_kwargs)

            return ClaudeResult(
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                duration_seconds=duration,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            self._log.warning(
                "Claude timed out",
                timeout=self._config.claude_timeout,
                duration_seconds=round(duration, 1),
            )

            return ClaudeResult(
                exit_code=-1,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
                duration_seconds=duration,
                timed_out=True,
            )
