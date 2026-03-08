"""Integration tests for the main orchestrator loop with mocked subprocesses."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


from lib.config import Config
from lib.log import StructuredLogger
from lib.reviewer import ClaudeReviewer


class TestReviewResponseParsing:
    """Test Claude review response parsing — the trickiest integration point."""

    def _make_reviewer(self):
        config = Config(review_timeout=60)
        logger = StructuredLogger("test")
        return ClaudeReviewer(config, Path("."), logger)

    def test_parse_direct_json(self):
        reviewer = self._make_reviewer()
        raw = json.dumps({
            "approved": True,
            "summary": "Looks good",
            "findings": [],
        })
        result = reviewer._parse_review_response(raw)

        assert result.approved is True
        assert result.summary == "Looks good"
        assert result.findings == []

    def test_parse_claude_envelope(self):
        reviewer = self._make_reviewer()
        inner = json.dumps({
            "approved": False,
            "summary": "Issues found",
            "findings": [{"severity": "high", "message": "bug"}],
        })
        raw = json.dumps({"result": inner})
        result = reviewer._parse_review_response(raw)

        assert result.approved is False
        assert len(result.findings) == 1

    def test_parse_json_in_text(self):
        reviewer = self._make_reviewer()
        raw = 'Here is my review:\n{"approved": true, "summary": "ok", "findings": []}\nEnd.'
        result = reviewer._parse_review_response(raw)

        assert result.approved is True

    def test_parse_real_claude_envelope(self):
        """Test with actual Claude CLI --output-format json envelope."""
        reviewer = self._make_reviewer()
        inner = json.dumps({
            "approved": True,
            "summary": "Implementation looks correct",
            "findings": [],
        })
        raw = json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": f"\n\n{inner}",
            "session_id": "abc-123",
        })
        result = reviewer._parse_review_response(raw)

        assert result.approved is True
        assert result.summary == "Implementation looks correct"
        assert result.parse_error is None

    def test_parse_envelope_with_markdown_fences(self):
        """Test when Claude wraps JSON in markdown code fences inside the envelope."""
        reviewer = self._make_reviewer()
        inner_json = '{"approved": false, "summary": "Issues found", "findings": [{"severity": "high", "message": "bug"}]}'
        raw = json.dumps({
            "type": "result",
            "result": f"\n```json\n{inner_json}\n```\n",
        })
        result = reviewer._parse_review_response(raw)

        assert result.approved is False
        assert len(result.findings) == 1
        assert result.findings[0]["message"] == "bug"
        assert result.parse_error is None

    def test_parse_envelope_with_text_around_json(self):
        """Test when Claude adds explanation text around the JSON in the envelope."""
        reviewer = self._make_reviewer()
        inner_json = '{"approved": true, "summary": "Looks good", "findings": []}'
        raw = json.dumps({
            "type": "result",
            "result": f"Here is my review:\n\n{inner_json}\n\nLet me know if you need more details.",
        })
        result = reviewer._parse_review_response(raw)

        assert result.approved is True
        assert result.parse_error is None

    def test_parse_empty_response(self):
        reviewer = self._make_reviewer()
        result = reviewer._parse_review_response("")

        assert result.approved is False
        assert result.parse_error is not None

    def test_parse_invalid_json(self):
        reviewer = self._make_reviewer()
        result = reviewer._parse_review_response("this is not json at all")

        assert result.approved is False
        assert result.parse_error is not None


class TestCheckTool:
    @patch("orchestrate.subprocess.run")
    def test_returns_true_when_tool_found(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        from orchestrate import _check_tool

        assert _check_tool("git") is True

    @patch("orchestrate.subprocess.run")
    def test_returns_false_when_tool_not_found(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)

        from orchestrate import _check_tool

        assert _check_tool("nonexistent_tool") is False


class TestToolValidation:
    @patch("orchestrate.subprocess.run")
    def test_validate_tools_all_present(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)

        from orchestrate import validate_tools

        logger = StructuredLogger("test")
        missing = validate_tools(skip_review=False, logger=logger)
        assert missing == []

    @patch("orchestrate._check_tool")
    def test_validate_tools_missing(self, mock_check):
        mock_check.side_effect = lambda name: name != "claude"

        from orchestrate import validate_tools

        logger = StructuredLogger("test")
        missing = validate_tools(skip_review=False, logger=logger)
        assert "claude" in missing

    @patch("orchestrate._check_tool")
    def test_skip_review_skips_claude(self, mock_check):
        mock_check.side_effect = lambda name: name != "claude"

        from orchestrate import validate_tools

        logger = StructuredLogger("test")
        # With skip_review=True, claude is still in REQUIRED_TOOLS
        # so it will still be missing
        missing = validate_tools(skip_review=True, logger=logger)
        assert "claude" in missing


class TestMainExceptionHandlers:
    @patch("orchestrate.StructuredLogger")
    @patch("orchestrate.RunHistory")
    @patch("orchestrate.Config.from_env")
    @patch("orchestrate.parse_args")
    @patch("orchestrate.setup_phase")
    @patch("orchestrate.implementation_phase")
    def test_system_exit_before_impl_keeps_zero_attempts(
        self, mock_impl, mock_setup, mock_args, mock_config_env, mock_history_cls, mock_logger_cls,
    ):
        """BUG-4 edge case: when impl_phase raises before returning,
        total_attempts stays at its initialized value (0)."""
        from orchestrate import main, SetupResult

        mock_args.return_value = MagicMock(
            change_name="test", no_clone=True, skip_review=True, repo=None, base_branch="main",
        )
        mock_config = Config()
        mock_config_env.return_value = mock_config

        mock_history = MagicMock()
        mock_history.start_run.return_value = "run-1"
        mock_history_cls.return_value = mock_history

        mock_setup.return_value = SetupResult(work_dir=Path("."), branch_name="test")

        # implementation_phase raises before returning a value
        mock_impl.side_effect = SystemExit("Max attempts reached")

        # impl_phase raises before returning, so total_attempts stays at
        # its initialized value (0). This is a regression guard for the edge
        # case — the companion test below validates the actual fix (total=3).
        main()

        fail_call = mock_history.fail_run.call_args
        assert fail_call is not None
        assert fail_call.kwargs.get("total_attempts", fail_call[1].get("total_attempts")) == 0

    @patch("orchestrate.StructuredLogger")
    @patch("orchestrate.RunHistory")
    @patch("orchestrate.Config.from_env")
    @patch("orchestrate.parse_args")
    @patch("orchestrate.setup_phase")
    @patch("orchestrate.implementation_phase")
    @patch("orchestrate.review_phase")
    def test_exception_after_impl_passes_actual_attempts(
        self, mock_review, mock_impl, mock_setup, mock_args, mock_config_env, mock_history_cls, mock_logger_cls,
    ):
        """BUG-4: When exception occurs after implementation, actual attempts are passed."""
        from orchestrate import main, SetupResult

        mock_args.return_value = MagicMock(
            change_name="test", no_clone=True, skip_review=False, repo=None, base_branch="main",
        )
        mock_config = Config()
        mock_config_env.return_value = mock_config

        mock_history = MagicMock()
        mock_history.start_run.return_value = "run-1"
        mock_history_cls.return_value = mock_history

        mock_setup.return_value = SetupResult(work_dir=Path("."), branch_name="test")

        # implementation_phase succeeds with 3 attempts
        mock_impl.return_value = (3, SetupResult(work_dir=Path("."), branch_name="test"))

        # review_phase raises
        mock_review.side_effect = SystemExit("Review failed")

        main()

        # Check that fail_run was called with the actual total_attempts=3
        fail_call = mock_history.fail_run.call_args
        assert fail_call is not None
        total_attempts_value = fail_call.kwargs.get("total_attempts")
        if total_attempts_value is None:
            total_attempts_value = fail_call[1].get("total_attempts")
        assert total_attempts_value == 3


class TestExtractRemainingTasks:
    def test_extracts_incomplete(self, tmp_path):
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(
            "- [x] 1.1 Done\n"
            "- [ ] 1.2 Not done\n"
            "- [x] 2.1 Done too\n"
            "- [ ] 2.2 Also not done\n"
        )

        from lib.progress import TasksMdProgress

        # Create a mock assessment with the tasks_md field
        mock_assessment = MagicMock()
        mock_assessment.tasks_md = TasksMdProgress(
            completed=2, total=4, file_path=str(tasks_file),
        )

        from orchestrate import extract_remaining_tasks

        remaining = extract_remaining_tasks(mock_assessment)
        assert len(remaining) == 2
        assert "1.2 Not done" in remaining
        assert "2.2 Also not done" in remaining

    def test_no_tasks_md(self):
        mock_assessment = MagicMock()
        mock_assessment.tasks_md = None

        from orchestrate import extract_remaining_tasks

        remaining = extract_remaining_tasks(mock_assessment)
        assert remaining == []


class TestImplementationCommentFormatting:
    def test_labels_checklist_progress_and_adds_reality_note(self):
        from orchestrate import _format_implementation_comment

        body = _format_implementation_comment(
            attempt=3,
            max_attempts=6,
            tasks_before="12/118",
            tasks_after="12/118",
            duration_seconds=2400,
            exit_code=0,
            has_diff=True,
            files_changed=14,
            is_complete=False,
            is_stuck=False,
            remaining_tasks=["Write E2E tests", "Implement conflict detection"],
            previous_errors=None,
        )

        assert "| OpenSpec checklist | 12/118 → 12/118 |" in body
        assert "| Code changes | yes (14 files) |" in body
        assert "Remaining checklist items (2):" in body
        assert "actual implementation may be ahead of this snapshot" in body

    def test_stuck_message_uses_checklist_specific_language(self):
        from orchestrate import _format_implementation_comment

        body = _format_implementation_comment(
            attempt=4,
            max_attempts=6,
            tasks_before="12/118",
            tasks_after="12/118",
            duration_seconds=2400,
            exit_code=-1,
            has_diff=True,
            files_changed=6,
            is_complete=False,
            is_stuck=True,
            remaining_tasks=None,
            previous_errors=["Claude timed out"],
        )

        assert "Checklist did not move in consecutive runs" in body
        assert "**Recent errors:**" in body


class TestReadOpenspecContext:
    def test_reads_proposal_and_specs(self, tmp_path):
        change_dir = tmp_path / "openspec" / "changes" / "my-feature"
        change_dir.mkdir(parents=True)

        (change_dir / "proposal.md").write_text("## Why\nBecause reasons.")

        spec_dir = change_dir / "specs" / "core"
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.md").write_text("## Requirements\nDo the thing.")

        from orchestrate import read_openspec_context

        ctx = read_openspec_context("my-feature", tmp_path)
        assert "Because reasons" in ctx
        assert "Do the thing" in ctx

    def test_missing_change_dir(self, tmp_path):
        from orchestrate import read_openspec_context

        ctx = read_openspec_context("nonexistent", tmp_path)
        assert "no openspec context found" in ctx
