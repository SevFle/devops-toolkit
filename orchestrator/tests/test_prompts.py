"""Static contract tests for AI prompt files."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"
ANALYSIS_PROMPTS = {
    "ai-code-review.prompt.md",
    "api-compat.prompt.md",
    "dead-code-analysis.prompt.md",
    "owasp-audit.prompt.md",
    "perf-audit.prompt.md",
    "tech-debt-scan.prompt.md",
}


def _read_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


class TestPromptContracts:
    def test_analysis_prompts_require_repo_relative_paths_and_confidence(self):
        missing = []

        for prompt_name in sorted(ANALYSIS_PROMPTS):
            content = _read_prompt(prompt_name)
            if "repo-relative" not in content or "confidence" not in content:
                missing.append(prompt_name)

        assert missing == []

    def test_ai_code_review_prompt_preserves_parser_fields(self):
        content = _read_prompt("ai-code-review.prompt.md")

        assert '"summary"' in content
        assert '"findings"' in content
        assert '"message"' in content
        assert '"suggestion"' in content
        assert "```json" in content

    def test_analysis_prompts_preserve_parser_critical_fields(self):
        expected_tokens = {
            "api-compat.prompt.md": ['"breaking_changes"', '"compatible_changes"', '"summary"'],
            "dead-code-analysis.prompt.md": [
                '"dead_exports"',
                '"orphaned_files"',
                '"unused_deps"',
                '"unreachable"',
                '"deprecated"',
                '"summary"',
            ],
            "owasp-audit.prompt.md": ['"category"', '"description"', '"recommendation"'],
            "perf-audit.prompt.md": ['"findings"', '"estimated_impact"', '"description"', '"suggestion"'],
            "tech-debt-scan.prompt.md": ['"hotspots"', '"smells"', '"outdated"', '"todos"', '"deps"', '"summary"'],
        }

        for prompt_name, tokens in expected_tokens.items():
            content = _read_prompt(prompt_name)
            for token in tokens:
                assert token in content, f"Missing {token} in {prompt_name}"

    def test_api_compat_prompt_requires_base_and_head_evidence(self):
        content = _read_prompt("api-compat.prompt.md")

        assert "base_evidence" in content
        assert "head_evidence" in content
        assert "unclear_changes" in content
        assert "consumer_impact" in content

    def test_perf_prompt_requires_trigger_conditions_and_evidence_type(self):
        content = _read_prompt("perf-audit.prompt.md")

        assert "trigger_conditions" in content
        assert "evidence_type" in content
        assert "do not report" in content.lower()

    def test_test_gap_prompt_preserves_automation_markers(self):
        content = _read_prompt("test-gap-analysis.prompt.md")

        assert "Preserve the `RATIONALE` blocks" in content
        assert "Prefer extending existing test files" in content
        assert "no brittle tests" in content.lower()
        assert "RATIONALE for <test-file-path>:" in content
        assert "SUMMARY:" in content

    def test_release_notes_prompt_forbids_invented_content(self):
        content = _read_prompt("release-notes.prompt.md")

        assert "Do not invent" in content
        assert "If the source material is insufficient" in content
        assert "Do NOT wrap the output in code fences" in content
