# devops-toolkit

Unified reusable GitHub workflows, composite actions, and AI-powered CI/CD orchestration.

## Quick Start

```bash
# In your project root:
curl -fsSL https://raw.githubusercontent.com/SevFle/devops-toolkit/main/init/init.sh | bash
```

The init script detects your project type and scaffolds thin caller workflows (~10 lines each).

## Workflows

| Workflow | Description | Trigger |
|----------|-------------|---------|
| `ci.yml` | Lint, type-check, test, build, Docker validation | `workflow_call` |
| `e2e.yml` | Playwright E2E tests with browser matrix | `workflow_call` |
| `deploy-staging.yml` | Docker build + VPS deploy to staging | `workflow_call` |
| `deploy-production.yml` | Docker build + VPS deploy to production | `workflow_call` |
| `rollback.yml` | Rollback staging or production | `workflow_call` |
| `release.yml` | Semantic version bump + GitHub Release | `workflow_call` |
| `ci-heal.yml` | Auto-fix failing CI with Claude CLI | `workflow_call` |
| `openspec-interview.yml` | Multi-turn interview bot on GitHub issues | `workflow_call` |
| `openspec-propose.yml` | Generate OpenSpec artifacts from interview | `workflow_call` |
| `openspec-orchestrate.yml` | Implement OpenSpec change with retry loop | `workflow_call` |

## Usage Examples

### CI Pipeline

```yaml
# .github/workflows/ci.yml
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

### Deploy to Staging

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

## Composite Actions

### `actions/claude-run`

Wraps `claude -p` with structured output parsing for OpenSpec markers.

```yaml
- uses: SevFle/devops-toolkit/actions/claude-run@main
  with:
    prompt: 'Review this code for security issues'
    model: 'claude-sonnet-4-5-20250514'
```

### `actions/docker-build`

Docker buildx with GHA caching.

```yaml
- uses: SevFle/devops-toolkit/actions/docker-build@main
  with:
    push: false
    tags: myapp:latest
```

## Orchestrator

The `orchestrator/` directory contains the Python backend that drives AI-powered workflows:

- **orchestrate.py** — 4-phase implementation loop (setup, implement, review, finalize)
- **ci_fix.py** — Self-healing CI that fetches failure logs and runs Claude to fix them
- **lib/claude_runner.py** — Claude CLI subprocess wrapper
- **lib/reviewer.py** — Code review via Claude CLI
- **lib/progress.py** — Multi-signal progress detection (openspec CLI, tasks.md, git diff)
- **lib/git_ops.py** — Git operations (branch, commit, push, PR creation)

Requirements: Python 3.11+, stdlib only (no external dependencies).

## Required Secrets

| Secret | Used By | Description |
|--------|---------|-------------|
| `ANTHROPIC_API_KEY` | OpenSpec, CI-Heal | Claude API key |
| `PAT_TOKEN` | CI-Heal | GitHub PAT with repo + workflow permissions |
| `OPENSPEC_GH_TOKEN` | OpenSpec | GitHub PAT for branch/PR creation |
| `DEPLOY_HOST` | Deploy | VPS hostname |
| `DEPLOY_USER` | Deploy | SSH username |
| `DEPLOY_SSH_KEY` | Deploy | SSH private key |
| `DEPLOY_PORT` | Deploy | SSH port (default: 22) |

## Project Structure

```
.github/workflows/     # Reusable workflows (workflow_call)
actions/
  claude-run/          # Composite action: wraps claude -p
  docker-build/        # Composite action: Docker buildx + cache
orchestrator/          # Python orchestration backend
  lib/                 # Core modules
  tests/               # Test suite
prompts/               # OpenSpec prompt files
init/                  # CLI init script + templates
```
