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

    def run(self, change_name: str) -> ClaudeResult:
        """Run Claude with instructions to implement the given change.

        Reads the openspec tasks file and asks Claude to implement remaining tasks.
        """
        tasks_file = self._work_dir / "openspec" / "changes" / change_name / "tasks.md"
        if tasks_file.exists():
            tasks_content = tasks_file.read_text()
            prompt = (
                f"Implement the OpenSpec change '{change_name}'. "
                f"Here are the tasks from tasks.md:\n\n{tasks_content}\n\n"
                "Complete all incomplete tasks (marked with '- [ ]'). "
                "Check off each task as you complete it."
            )
        else:
            prompt = f"Implement the OpenSpec change '{change_name}'. Complete all tasks."
        return self._execute(prompt)

    def run_with_context(
        self,
        change_name: str,
        remaining_tasks: list[str],
        previous_errors: list[str] | None = None,
    ) -> ClaudeResult:
        """Run Claude with additional context about remaining work."""
        tasks_file = self._work_dir / "openspec" / "changes" / change_name / "tasks.md"
        parts = [f"Continue implementing the OpenSpec change '{change_name}'."]

        if tasks_file.exists():
            tasks_content = tasks_file.read_text()
            parts.append(f"Here are the tasks from tasks.md:\n\n{tasks_content}")

        if remaining_tasks:
            tasks_str = "\n".join(f"  - {t}" for t in remaining_tasks[:10])
            parts.append(f"These tasks remain incomplete:\n{tasks_str}")

        if previous_errors:
            errors_str = "\n".join(f"  - {e}" for e in previous_errors[:5])
            parts.append(f"Previous errors to avoid:\n{errors_str}")

        parts.append("Complete as many remaining tasks as possible.")
        prompt = "\n\n".join(parts)
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
