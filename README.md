# devops-toolkit

Unified reusable GitHub workflows, composite actions, and AI-powered CI/CD orchestration.

## Quick Start

```bash
# In your project root:
curl -fsSL https://raw.githubusercontent.com/SevFle/devops-toolkit/main/init/init.sh | bash
```

The init script detects your project type (Node.js, Go, Python) and scaffolds thin caller workflows (~10 lines each). Run `init.sh --validate` to verify scaffolded workflows.

---

## Workflows

All workflows are reusable via `workflow_call`. Caller repos invoke them with thin wrappers.

### CI / CD

| Workflow | Description | Trigger |
|----------|-------------|---------|
| `ci.yml` | Runtime-aware CI for Node.js, Python, and Go with path-based short-circuiting, optional E2E execution, flaky-test retry detection, and coverage enforcement via the `test-report` composite action. | `workflow_call` |
| `e2e.yml` | Playwright E2E tests with configurable browser matrix, custom start command, and artifact uploads. | `workflow_call` |
| `release.yml` | Semantic version bump, run the full verification suite, create git tag + GitHub Release, and support dry runs plus optional AI-generated release notes. | `workflow_call` |

### Deploy

| Workflow | Description | Trigger |
|----------|-------------|---------|
| `deploy-staging.yml` | Build Docker image, transfer to VPS via SSH, deploy with docker-compose, run optional pre/post hooks, and health-check with retries. Auto-increments beta version tags and supports environment protection. | `workflow_call` |
| `deploy-production.yml` | Production deploy to VPS from a published release or manual dispatch using immutable image references, optional migration hooks, environment protection, and webhook notifications. | `workflow_call` |
| `deploy-k8s.yml` | Kubernetes-native deploy with provenance attestation, optional Cosign signing, Helm/Kustomize or kubectl rollout strategies, rollout verification, webhook notifications, and auto-rollback on failure. | `workflow_call` |
| `rollback.yml` | Rollback staging or production to a previous Docker image tag on VPS. | `workflow_call` |

### Security & Quality

| Workflow | Description | Trigger |
|----------|-------------|---------|
| `security.yml` | Secret detection, dependency audit, optional container scanning, CycloneDX SBOM generation, SARIF uploads, path ignores, and webhook notifications. | `workflow_call` |
| `pr-quality.yml` | PR hygiene gate: diff size limits, conventional commit title validation, optional changelog and linked-issue enforcement, required labels, risky-file checklists, and branch naming patterns. Posts a pass/fail summary comment. | `workflow_call` |
| `codeql.yml` | GitHub CodeQL analysis with auto or explicit language selection, custom build hooks, and Security tab upload for semantic code scanning. | `workflow_call` |

### Self-Healing CI

| Workflow | Description | Trigger |
|----------|-------------|---------|
| `ci-heal.yml` | Monitors CI failures on PR branches. Automatically checks out the failing branch, runs Claude CLI to diagnose and fix errors, commits the fix, and pushes. Respects a per-branch heal attempt limit. | `workflow_call` |

### Housekeeping

| Workflow | Description | Trigger |
|----------|-------------|---------|
| `housekeeping.yml` | Weekly maintenance: labels/closes stale issues, closes abandoned `openspec/*` draft PRs, deletes merged branches (excluding protected). Supports dry-run mode. | `workflow_call` |

### OpenSpec Pipeline

AI-driven feature implementation from issue to PR:

| Workflow | Description | Trigger |
|----------|-------------|---------|
| `openspec-interview.yml` | Multi-turn interview bot on GitHub issues. Claude asks clarifying questions, tracks state in base64 comment markers, and determines when the interview is complete. | `workflow_call` |
| `openspec-propose.yml` | Converts a `/propose` issue comment into an OpenSpec proposal branch, draft artifacts, and follow-up PR workflow state. | `workflow_call` |
| `openspec-orchestrate.yml` | Drives Claude CLI in a retry loop to implement an OpenSpec change: creates branch, runs implementation attempts, reviews code, commits progress, and opens a PR. | `workflow_call` |

### AI-Powered Analysis

All AI workflows invoke an AI CLI with specialized prompts and parse structured JSON output. By default they use OpenCode (`opencode run`), but can be switched to Claude CLI (`claude -p`) via the `ai_cli` input on any workflow.

| Workflow | Description | Trigger |
|----------|-------------|---------|
| `ai-owasp-audit.yml` | Audits the full codebase against the OWASP Top 20 security categories. Traces data flow from inputs to sinks. Creates a GitHub Issue with findings grouped by severity, or posts inline PR review comments. | `workflow_call` |
| `ai-code-review.yml` | Automated PR code review: bugs, security issues, performance problems, code quality, and test coverage gaps. Posts a GitHub PR review with inline comments per finding. Can auto-approve clean PRs. | `workflow_call` |
| `ai-test-gaps.yml` | Analyzes coverage reports to identify untested code paths, ranks them by risk, and generates runnable test implementations. Creates a PR with the generated tests. | `workflow_call` |
| `ai-perf-audit.yml` | Static performance audit: N+1 queries, memory leaks, blocking I/O, unnecessary re-renders, algorithmic inefficiencies. Creates a GitHub Issue with findings sorted by estimated impact. | `workflow_call` |
| `ai-dead-code.yml` | Deep dead code analysis: unused exports, orphaned files, unused dependencies, unreachable code. Can auto-create a cleanup PR or report findings as an Issue. | `workflow_call` |
| `ai-release-notes.yml` | Generates human-readable release notes from commit history and merged PRs. Groups changes by category, highlights breaking changes with migration instructions. | `workflow_call` |
| `ai-api-compat.yml` | Detects API-breaking changes between base and head branches: removed endpoints, changed schemas, altered function signatures, destructive DB migrations. Posts PR comment and optionally blocks merge. | `workflow_call` |
| `ai-tech-debt.yml` | Comprehensive tech debt assessment: complexity hotspots, code smells, outdated patterns, TODO/FIXME audit, dependency freshness. Creates a GitHub Issue formatted as an actionable backlog with effort estimates. | `workflow_call` |

---

## Composite Actions

### `actions/claude-run`

Wraps `claude -p` with structured output parsing for OpenSpec markers (`OPENSPEC_QUESTION_STEP`, `OPENSPEC_INTERVIEW_COMPLETE`, `OPENSPEC_CHANGE_ID`). Handles prompt preparation, base64 encoding, and response truncation.

```yaml
- uses: SevFle/devops-toolkit/actions/claude-run@main
  with:
    prompt: 'Review this code for security issues'
    model: 'zai-coding-plan/glm-5'
```

### `actions/docker-build`

Docker buildx with GHA caching.

```yaml
- uses: SevFle/devops-toolkit/actions/docker-build@main
  with:
    push: false
    tags: myapp:latest
```

### `actions/test-report`

Parses test results (JUnit XML, pytest JSON) and coverage reports (lcov, cobertura, coverage.json). Posts a PR comment with a results summary table and coverage delta against the base branch.

```yaml
- uses: SevFle/devops-toolkit/actions/test-report@main
  with:
    test_results_path: test-results/junit.xml
    coverage_path: coverage/lcov.info
```

---

## Orchestrator

The `orchestrator/` directory contains the Python backend that drives AI-powered workflows.

| Module | Description |
|--------|-------------|
| `orchestrate.py` | 4-phase implementation loop: setup, implement (with retry), review, finalize. Creates branches, runs Claude, commits progress, opens PRs. |
| `ci_fix.py` | Self-healing CI: fetches failure logs and runs Claude to diagnose and fix them. |
| `lib/claude_runner.py` | Claude CLI subprocess wrapper with timeout and error handling. |
| `lib/reviewer.py` | Code review via Claude CLI with structured finding output. |
| `lib/progress.py` | Multi-signal progress detection: openspec CLI status, tasks.md parsing, git diff analysis. |
| `lib/git_ops.py` | Git operations: branch, commit, push, PR creation/update, comments. |
| `lib/complexity.py` | Complexity scoring for adaptive time budgets. Analyzes task count and keywords to classify changes as low/medium/high and recommend attempts, timeouts, and budgets. |
| `lib/config.py` | Immutable configuration from environment variables. Supports adaptive budgeting toggle. |
| `lib/history.py` | SQLite-based run history for tracking orchestration attempts and outcomes. |
| `lib/log.py` | Structured JSON logging for orchestration phases. |

Requirements: Python 3.11+, stdlib only (no external dependencies).

---

## Prompts

AI workflow prompts in `prompts/`:

| Prompt | Used By |
|--------|---------|
| `openspec-auto-interview.prompt.md` | `openspec-interview.yml` |
| `owasp-audit.prompt.md` | `ai-owasp-audit.yml` |
| `ai-code-review.prompt.md` | `ai-code-review.yml` |
| `test-gap-analysis.prompt.md` | `ai-test-gaps.yml` |
| `perf-audit.prompt.md` | `ai-perf-audit.yml` |
| `dead-code-analysis.prompt.md` | `ai-dead-code.yml` |
| `release-notes.prompt.md` | `ai-release-notes.yml` |
| `api-compat.prompt.md` | `ai-api-compat.yml` |
| `tech-debt-scan.prompt.md` | `ai-tech-debt.yml` |

Additional `opsx-*.prompt.md` prompts support the OpenSpec CLI artifact workflow.

---

## Usage Examples

### CI Pipeline

```yaml
name: CI
on:
  pull_request:
  push:
    branches: [main]
jobs:
  ci:
    uses: SevFle/devops-toolkit/.github/workflows/ci.yml@main
    with:
      docker_build: true
      test_report: true
    secrets: inherit
```

### CI with Custom Commands

```yaml
jobs:
  ci:
    uses: SevFle/devops-toolkit/.github/workflows/ci.yml@main
    with:
      node_version: '20'
      test_command: 'npx vitest run'
      lint_command: 'npx eslint .'
      typecheck_command: 'npx tsc --noEmit'
      coverage_threshold: 80
    secrets: inherit
```

### E2E Tests (Nightly)

```yaml
name: E2E Tests
on:
  schedule:
    - cron: '0 1 * * *'
jobs:
  e2e:
    uses: SevFle/devops-toolkit/.github/workflows/e2e.yml@main
    with:
      browsers: '["chromium", "firefox"]'
      build_command: 'npm run build'
      start_command: 'PORT=4173 node .next/standalone/server.js'
      base_url: 'http://localhost:4173'
    secrets: inherit
```

### Deploy to Staging (VPS)

```yaml
name: Deploy Staging
on:
  push:
    branches: [main]
jobs:
  deploy:
    uses: SevFle/devops-toolkit/.github/workflows/deploy-staging.yml@main
    with:
      app_name: myapp-beta
      deploy_dir: /opt/services/apps/myapp-beta
      image_name: ghcr.io/myorg/myapp
    secrets: inherit
```

### Deploy to Kubernetes

```yaml
name: Deploy K8s
on:
  push:
    branches: [main]
jobs:
  deploy:
    uses: SevFle/devops-toolkit/.github/workflows/deploy-k8s.yml@main
    with:
      image_name: ghcr.io/myorg/myapp
      deployment_name: myapp
      namespace: production
      use_helm: true
      helm_chart: ./charts/myapp
    secrets: inherit
```

### Security Scanning

```yaml
name: Security
on:
  pull_request:
  schedule:
    - cron: '0 3 * * 1'
jobs:
  security:
    uses: SevFle/devops-toolkit/.github/workflows/security.yml@main
    with:
      scan_docker: true
      generate_sbom: true
      severity_threshold: high
    secrets: inherit
```

### CodeQL Analysis

```yaml
name: CodeQL
on:
  pull_request:
  push:
    branches: [main]
jobs:
  analyze:
    uses: SevFle/devops-toolkit/.github/workflows/codeql.yml@main
    with:
      language: auto
      query_suite: security-and-quality
    secrets: inherit
```

### PR Quality Gate

```yaml
name: PR Quality
on:
  pull_request:
    types: [opened, synchronize, edited]
jobs:
  quality:
    uses: SevFle/devops-toolkit/.github/workflows/pr-quality.yml@main
    with:
      max_lines_warn: 400
      max_lines_block: 800
    secrets: inherit
```

### CI Auto-Heal

```yaml
name: CI Auto-Heal
on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]
jobs:
  heal:
    uses: SevFle/devops-toolkit/.github/workflows/ci-heal.yml@main
    secrets: inherit
```

### AI Code Review

```yaml
name: AI Code Review
on:
  pull_request:
    types: [opened, synchronize]
jobs:
  review:
    uses: SevFle/devops-toolkit/.github/workflows/ai-code-review.yml@main
    with:
      severity_threshold: medium
      auto_approve: false
    secrets: inherit
```

### AI Code Review (with OpenCode)

```yaml
jobs:
  review:
    uses: SevFle/devops-toolkit/.github/workflows/ai-code-review.yml@main
    with:
      ai_cli: opencode
      ai_model: 'your-model-id'
    secrets: inherit
```

### OWASP Security Audit

```yaml
name: OWASP Audit
on:
  schedule:
    - cron: '0 5 * * 1'
  workflow_dispatch:
jobs:
  audit:
    uses: SevFle/devops-toolkit/.github/workflows/ai-owasp-audit.yml@main
    with:
      categories: all
      severity_filter: medium
    secrets: inherit
```

### OpenSpec Pipeline

```yaml
# Interview bot (triggered by labels/comments on issues)
name: OpenSpec Interview
on:
  issues:
    types: [labeled]
  issue_comment:
    types: [created]
jobs:
  interview:
    uses: SevFle/devops-toolkit/.github/workflows/openspec-interview.yml@main
    secrets: inherit
```

```yaml
# Proposal generator (triggered by /propose comments)
name: OpenSpec Propose
on:
  issue_comment:
    types: [created]
jobs:
  propose:
    uses: SevFle/devops-toolkit/.github/workflows/openspec-propose.yml@main
    with:
      base_branch: main
    secrets: inherit
```

### Housekeeping

```yaml
name: Housekeeping
on:
  schedule:
    - cron: '0 4 * * 0'
jobs:
  housekeeping:
    uses: SevFle/devops-toolkit/.github/workflows/housekeeping.yml@main
    with:
      stale_days: 30
      dry_run: false
    secrets: inherit
```

---

## Required Secrets

| Secret | Used By | Description |
|--------|---------|-------------|
| `PAT_TOKEN` | CI-Heal, OpenSpec | GitHub PAT with repo + workflow permissions |
| `DEPLOY_HOST` | VPS Deploy | VPS hostname |
| `DEPLOY_USER` | VPS Deploy | SSH username |
| `DEPLOY_SSH_KEY` | VPS Deploy | SSH private key |
| `DEPLOY_PORT` | VPS Deploy | SSH port (default: 22) |
| `KUBECONFIG` | K8s Deploy | Kubernetes config for kubectl/helm |
| `REGISTRY_USERNAME` | K8s Deploy | Container registry username |
| `REGISTRY_PASSWORD` | K8s Deploy | Container registry password |

---

## Project Structure

```
.github/workflows/          # Reusable workflows (workflow_call)
  ci.yml                    # CI pipeline with test reporting
  e2e.yml                   # Playwright E2E tests
  release.yml               # Semantic versioning + release
  deploy-staging.yml        # Docker deploy to VPS (staging)
  deploy-production.yml     # Docker deploy to VPS (production)
  deploy-k8s.yml            # Kubernetes deploy (Helm/Kustomize/kubectl)
  rollback.yml              # VPS rollback
  security.yml              # Secret detection + dep audit + container scan
  pr-quality.yml            # PR size, commit format, changelog checks
  codeql.yml                # Semantic security scanning via CodeQL
  ci-heal.yml               # Auto-fix CI failures with Claude
  housekeeping.yml          # Stale issue/PR/branch cleanup
  openspec-interview.yml    # AI interview bot on issues
  openspec-propose.yml      # Proposal generation from /propose comments
  openspec-orchestrate.yml  # AI-driven implementation loop
  ai-owasp-audit.yml        # OWASP Top 20 security audit
  ai-code-review.yml        # AI PR code review
  ai-test-gaps.yml          # Test gap analysis + generation
  ai-perf-audit.yml         # Performance audit
  ai-dead-code.yml          # Dead code & dependency analysis
  ai-release-notes.yml      # AI release notes generator
  ai-api-compat.yml         # API breaking change detection
  ai-tech-debt.yml          # Tech debt scanner
actions/
  claude-run/               # Composite action: wraps claude -p
  docker-build/             # Composite action: Docker buildx + cache
  test-report/              # Composite action: test results + coverage PR comments
orchestrator/               # Python orchestration backend
  lib/                      # Core modules (config, complexity, git, progress, etc.)
  tests/                    # Test suite
prompts/                    # AI workflow prompt files
init/                       # CLI init script + templates
  init.sh                   # Project scaffolder (Node/Go/Python)
  templates/                # Caller workflow templates
```
