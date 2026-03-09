"""Static contract tests for reusable workflows, templates, and scaffolding."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TEMPLATES_DIR = REPO_ROOT / "init" / "templates"
README_PATH = REPO_ROOT / "README.md"
INIT_SCRIPT_PATH = REPO_ROOT / "init" / "init.sh"
THIRD_PARTY_BRANCH_REFS = {"main", "master", "develop", "trunk"}
RUNNERS_EXPRESSION = "fromJSON(startsWith(inputs.runners, '[') && inputs.runners || format('[\"{0}\"]', inputs.runners))"
CI_TEMPLATE_EXCEPTIONS = {"ci-go.yml.tmpl", "ci-python.yml.tmpl"}
PUBLIC_WORKFLOW_EXCEPTIONS = {"_self-test.yml"}


def _public_workflow_paths() -> list[Path]:
	return sorted(
		path
		for path in WORKFLOWS_DIR.glob("*.yml")
		if path.name not in PUBLIC_WORKFLOW_EXCEPTIONS
	)


def _workflow_inputs(path: Path) -> set[str]:
	lines = path.read_text(encoding="utf-8").splitlines()
	in_workflow_call = False
	in_inputs = False
	workflow_indent = -1
	inputs_indent = -1
	inputs: set[str] = set()

	for line in lines:
		stripped = line.strip()
		indent = len(line) - len(line.lstrip(" "))

		if stripped == "workflow_call:":
			in_workflow_call = True
			workflow_indent = indent
			in_inputs = False
			continue

		if in_workflow_call and indent <= workflow_indent and stripped:
			in_workflow_call = False
			in_inputs = False

		if not in_workflow_call:
			continue

		if stripped == "inputs:":
			in_inputs = True
			inputs_indent = indent
			continue

		if in_inputs and indent <= inputs_indent and stripped:
			in_inputs = False

		if in_inputs and indent == inputs_indent + 2 and stripped.endswith(":"):
			inputs.add(stripped[:-1])

	return inputs


def _template_target(path: Path) -> str | None:
	match = re.search(
		r"uses:\s*SevFle/devops-toolkit/\.github/workflows/([^\s@]+)@main",
		path.read_text(encoding="utf-8"),
	)
	return match.group(1) if match else None


def _template_with_keys(path: Path) -> set[str]:
	lines = path.read_text(encoding="utf-8").splitlines()
	with_keys: set[str] = set()
	in_with = False
	with_indent = -1

	for line in lines:
		stripped = line.strip()
		indent = len(line) - len(line.lstrip(" "))

		if stripped == "with:":
			in_with = True
			with_indent = indent
			continue

		if in_with and indent <= with_indent and stripped:
			in_with = False

		if in_with and indent == with_indent + 2 and stripped.endswith(":"):
			with_keys.add(stripped[:-1])

	return with_keys


def _third_party_uses_references() -> list[tuple[Path, str]]:
	references: list[tuple[Path, str]] = []
	file_paths = list(WORKFLOWS_DIR.glob("*.yml")) + list((REPO_ROOT / "actions").glob("*/action.yml"))

	for path in sorted(file_paths):
		for match in re.finditer(r"uses:\s*([^\s]+)", path.read_text(encoding="utf-8")):
			value = match.group(1)
			if value.startswith("./"):
				continue
			if value.startswith("SevFle/devops-toolkit/"):
				continue
			references.append((path, value))

	return references


class TestWorkflowCatalog:
	def test_readme_lists_all_public_reusable_workflows(self):
		readme = README_PATH.read_text(encoding="utf-8")
		missing = [path.name for path in _public_workflow_paths() if f"`{path.name}`" not in readme]

		assert missing == []

	def test_every_public_reusable_workflow_has_template_or_explicit_exception(self):
		workflow_to_template = {
			"ci.yml": "ci.yml.tmpl",
			**{
				path.name: f"{path.stem}.yml.tmpl"
				for path in _public_workflow_paths()
				if path.name != "ci.yml"
			},
		}
		missing = [
			f"{workflow_name} -> {template_name}"
			for workflow_name, template_name in sorted(workflow_to_template.items())
			if not (TEMPLATES_DIR / template_name).exists()
		]

		assert missing == []

	def test_no_unmapped_templates_exist(self):
		mapped = {f"{path.stem}.yml.tmpl" for path in _public_workflow_paths()}
		mapped.remove("ci.yml.tmpl")
		mapped.add("ci.yml.tmpl")
		allowed = mapped | CI_TEMPLATE_EXCEPTIONS

		unexpected = sorted(path.name for path in TEMPLATES_DIR.glob("*.tmpl") if path.name not in allowed)

		assert unexpected == []


class TestActionPinning:
	def test_external_actions_are_not_pinned_to_branches(self):
		branch_pinned = []

		for path, reference in _third_party_uses_references():
			if "@" not in reference:
				continue
			ref = reference.rsplit("@", 1)[1]
			if ref in THIRD_PARTY_BRANCH_REFS:
				branch_pinned.append(f"{path.relative_to(REPO_ROOT)} -> {reference}")

		assert branch_pinned == []


class TestTemplateContracts:
	def test_template_wrapper_targets_existing_workflow(self):
		missing_targets = []

		for template_path in sorted(TEMPLATES_DIR.glob("*.tmpl")):
			target = _template_target(template_path)
			if target is None:
				continue
			if not (WORKFLOWS_DIR / target).exists():
				missing_targets.append(f"{template_path.name} -> {target}")

		assert missing_targets == []

	def test_template_with_keys_are_subset_of_target_workflow_inputs(self):
		invalid = []

		for template_path in sorted(TEMPLATES_DIR.glob("*.tmpl")):
			target = _template_target(template_path)
			if target is None:
				continue

			with_keys = _template_with_keys(template_path)
			if not with_keys:
				continue

			workflow_inputs = _workflow_inputs(WORKFLOWS_DIR / target)
			unknown = sorted(with_keys - workflow_inputs)
			if unknown:
				invalid.append(f"{template_path.name} -> {target}: {', '.join(unknown)}")

		assert invalid == []

	def test_reusable_workflows_using_runners_input_apply_consistent_runs_on_expression(self):
		inconsistent = []

		for workflow_path in _public_workflow_paths():
			content = workflow_path.read_text(encoding="utf-8")
			if "runners:" not in content:
				continue
			if RUNNERS_EXPRESSION not in content:
				inconsistent.append(workflow_path.name)

		assert inconsistent == []

	def test_pr_quality_passes_changed_files_to_risky_path_check(self):
		content = (WORKFLOWS_DIR / "pr-quality.yml").read_text(encoding="utf-8")

		assert "CHANGED_FILES=$(gh pr diff \"$PR_NUMBER\" --name-only)" in content
		assert (
			"CHANGED_FILES=\"$CHANGED_FILES\" python3 - <<'PY'" in content
			or "export CHANGED_FILES" in content
		)
		assert "(close[sd]?|fix(e[sd])?|resolve[sd]?)?\\s*#?[0-9]+" not in content

	def test_ci_enforces_coverage_even_when_report_is_zero(self):
		content = (WORKFLOWS_DIR / "ci.yml").read_text(encoding="utf-8")

		assert "steps.report.outputs.coverage_pct != ''" in content
		assert "steps.report.outputs.coverage_pct != '0'" not in content
		assert "*.md" in content
		assert 'bash -lc "${{ inputs.install_command }}"' not in content
		assert 'bash -lc "${{ inputs.build_command }}"' not in content
		assert 'continue-on-error: true' in content

	def test_security_status_treats_sbom_failure_as_failure_when_enabled(self):
		content = (WORKFLOWS_DIR / "security.yml").read_text(encoding="utf-8")

		assert "needs.sbom.result" in content
		assert "SBOM generation failed" in content

	def test_reusable_workflows_do_not_reference_secrets_in_if_expressions(self):
		workflow_names = [
			"deploy-k8s.yml",
			"deploy-production.yml",
			"deploy-staging.yml",
			"security.yml",
		]

		violations = []
		for name in workflow_names:
			content = (WORKFLOWS_DIR / name).read_text(encoding="utf-8")
			if "if: always() && secrets.NOTIFY_WEBHOOK_URL != ''" in content:
				violations.append(name)

		assert violations == []

	def test_self_test_yaml_lint_uses_inline_python_validation(self):
		content = (WORKFLOWS_DIR / "_self-test.yml").read_text(encoding="utf-8")

		assert 'python3 -c "import pathlib, sys, yaml;' in content
		assert 'python3 - "$f" <<\'PY\'' not in content

	def test_codeql_auto_detection_is_not_hardcoded(self):
		content = (WORKFLOWS_DIR / "codeql.yml").read_text(encoding="utf-8")

		assert "detect-languages:" in content
		assert "needs.detect-languages.outputs.languages" in content
		assert '[\"javascript-typescript\",\"python\",\"go\"]' not in content
		assert "python3 -c \"import json, sys;" in content

	def test_deploy_k8s_strategy_does_not_force_container_name_for_canary_or_blue_green(self):
		content = (WORKFLOWS_DIR / "deploy-k8s.yml").read_text(encoding="utf-8")

		assert 'target_container="$target_deployment"' not in content
		assert "if [ '${{ inputs.deployment_strategy }}' = 'rolling' ]; then" in content

	def test_ai_test_gaps_counts_new_tests_without_duplicate_zero_output(self):
		content = (WORKFLOWS_DIR / "ai-test-gaps.yml").read_text(encoding="utf-8")

		assert "awk '/^\\?\\?/ {count++} END {print count+0}'" in content
		assert "grep -c '^\\?' || echo 0" not in content

	def test_rollback_template_targets_environment_specific_app_names(self):
		content = (TEMPLATES_DIR / "rollback.yml.tmpl").read_text(encoding="utf-8")

		assert "format('{0}-beta', '{{APP_NAME}}')" in content
		assert "format('{0}-prod', '{{APP_NAME}}')" in content


class TestInitScaffolding:
	ANSWERS = "y\nn\ny\ny\nn\nn\nn\nn\nn\nn\nn\nn\nn\nn\nn\n"

	def test_init_script_scaffolds_node_python_go_ci_security_pr_quality_without_placeholders(
		self, tmp_path: Path,
	):
		cases = [
			("node", "package.json", '{"name":"demo","version":"1.0.0"}', "runtime: 'node'"),
			(
				"python",
				"pyproject.toml",
				'[project]\nname = "demo"\nversion = "0.1.0"\n',
				"runtime: 'python'",
			),
			("go", "go.mod", "module example.com/demo\n\ngo 1.22\n", "runtime: 'go'"),
		]

		for project_name, marker_file, marker_content, expected_runtime in cases:
			project_dir = tmp_path / project_name
			project_dir.mkdir()
			subprocess.run(["git", "init", "-b", "main"], cwd=project_dir, check=True, capture_output=True)
			(project_dir / marker_file).write_text(marker_content, encoding="utf-8")

			subprocess.run(
				["bash", str(INIT_SCRIPT_PATH)],
				cwd=project_dir,
				input=self.ANSWERS,
				text=True,
				check=True,
				env={**os.environ, "DEVOPS_TOOLKIT_LOCAL_ROOT": str(REPO_ROOT)},
				capture_output=True,
			)

			ci_path = project_dir / ".github" / "workflows" / "ci.yml"
			security_path = project_dir / ".github" / "workflows" / "security.yml"
			quality_path = project_dir / ".github" / "workflows" / "pr-quality.yml"

			assert ci_path.exists()
			assert security_path.exists()
			assert quality_path.exists()
			assert expected_runtime in ci_path.read_text(encoding="utf-8")

			generated_files = list((project_dir / ".github" / "workflows").glob("*.yml"))
			assert generated_files
			for file_path in generated_files:
				content = file_path.read_text(encoding="utf-8")
				assert re.search(r"\{\{[A-Z_][A-Z_]*\}\}", content) is None
