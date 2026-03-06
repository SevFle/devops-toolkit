#!/usr/bin/env bash
# devops-toolkit init — scaffold caller workflows for a project
# Usage: curl -fsSL https://raw.githubusercontent.com/SevFle/devops-toolkit/main/init/init.sh | bash

set -euo pipefail

TOOLKIT_REPO="SevFle/devops-toolkit"
TOOLKIT_REF="main"
RAW_BASE="https://raw.githubusercontent.com/${TOOLKIT_REPO}/${TOOLKIT_REF}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[info]${NC}  $*"; }
ok()    { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[warn]${NC}  $*"; }
err()   { echo -e "${RED}[error]${NC} $*"; }

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

validate_scaffolded() {
  local has_errors=false

  info "Validating generated workflow files..."

  for f in .github/workflows/*.yml; do
    [ -f "$f" ] || continue

    # Check YAML syntax
    if command -v python3 >/dev/null 2>&1; then
      if python3 -c "import yaml; yaml.safe_load(open('$f'))" 2>/dev/null; then
        ok "YAML valid: $f"
      else
        # Fallback: PyYAML might not be installed
        if python3 -c "import yaml" 2>/dev/null; then
          warn "YAML syntax error: $f"
          has_errors=true
        else
          warn "PyYAML not installed, skipping syntax check for $f"
        fi
      fi
    else
      warn "python3 not found, skipping YAML syntax check"
    fi

    # Check for remaining template variables
    if grep -q '{{[A-Z_]*}}' "$f" 2>/dev/null; then
      warn "Unresolved template variables in $f:"
      grep -n '{{[A-Z_]*}}' "$f" | while read -r line; do
        echo "       $line"
      done
      has_errors=true
    fi
  done

  if [ "$has_errors" = "true" ]; then
    warn "Validation completed with warnings"
    return 1
  else
    ok "All workflow files validated successfully"
    return 0
  fi
}

# ---------------------------------------------------------------------------
# Detect project type
# ---------------------------------------------------------------------------

detect_project() {
  local project_type="unknown"
  local package_manager="npm"
  local has_docker=false
  local base_branch="main"

  if [ -f "package.json" ]; then
    project_type="node"
    if [ -f "pnpm-lock.yaml" ]; then
      package_manager="pnpm"
    fi
  elif [ -f "pyproject.toml" ] || [ -f "setup.py" ]; then
    project_type="python"
  elif [ -f "go.mod" ]; then
    project_type="go"
  fi

  if [ -f "Dockerfile" ]; then
    has_docker=true
  fi

  # Check if develop branch exists
  if git rev-parse --verify develop >/dev/null 2>&1 || \
     git ls-remote --heads origin develop 2>/dev/null | grep -q develop; then
    base_branch="develop"
  fi

  echo "$project_type|$package_manager|$has_docker|$base_branch"
}

# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

ask_yes_no() {
  local prompt="$1"
  local default="${2:-y}"
  local yn
  if [ "$default" = "y" ]; then
    read -rp "$prompt [Y/n]: " yn
    yn="${yn:-y}"
  else
    read -rp "$prompt [y/N]: " yn
    yn="${yn:-n}"
  fi
  [[ "$yn" =~ ^[Yy] ]]
}

# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

render_template() {
  local template="$1"
  local output="$2"
  shift 2

  # Download template
  local tmp
  tmp=$(mktemp)
  if ! curl -fsSL "${RAW_BASE}/init/templates/${template}" -o "$tmp" 2>/dev/null; then
    err "Failed to download template: $template"
    rm -f "$tmp"
    return 1
  fi

  # Replace variables
  local content
  content=$(cat "$tmp")
  rm -f "$tmp"

  while [ $# -gt 0 ]; do
    local key="$1"
    local value="$2"
    content="${content//\{\{${key}\}\}/${value}}"
    shift 2
  done

  # Remove conditional blocks for disabled features
  # {{#FEATURE}}...{{/FEATURE}} blocks
  content=$(echo "$content" | sed '/{{#/,/{{\//{/{{#\|{{\//{d}}')

  mkdir -p "$(dirname "$output")"
  echo "$content" > "$output"
  ok "Created $output"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
  # Handle --validate flag
  if [ "${1:-}" = "--validate" ]; then
    validate_scaffolded
    exit $?
  fi

  echo ""
  echo -e "${BLUE}=== devops-toolkit init ===${NC}"
  echo ""

  # Check we're in a git repo
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    err "Not a git repository. Run this from your project root."
    exit 1
  fi

  # Detect project
  IFS='|' read -r project_type package_manager has_docker base_branch <<< "$(detect_project)"
  info "Detected: type=$project_type, pkg=$package_manager, docker=$has_docker, base=$base_branch"

  # Create workflows directory
  mkdir -p .github/workflows

  # Ask which workflows to enable
  echo ""
  info "Which workflows would you like to enable?"
  echo ""

  local enable_ci=false
  local enable_e2e=false
  local enable_deploy_staging=false
  local enable_deploy_prod=false
  local enable_ci_heal=false
  local enable_openspec=false

  ask_yes_no "  CI Pipeline (lint, type-check, test, build)" "y" && enable_ci=true
  ask_yes_no "  E2E Tests (Playwright)" "n" && enable_e2e=true

  if [ "$has_docker" = "true" ]; then
    ask_yes_no "  Deploy to Staging (Docker + VPS)" "n" && enable_deploy_staging=true
    ask_yes_no "  Deploy to Production (Docker + VPS)" "n" && enable_deploy_prod=true
  fi

  ask_yes_no "  CI Auto-Heal (Claude fixes CI failures)" "n" && enable_ci_heal=true
  ask_yes_no "  OpenSpec Pipeline (interview + propose + orchestrate)" "n" && enable_openspec=true

  echo ""

  # Generate caller workflows
  if [ "$enable_ci" = "true" ]; then
    local docker_flag="false"
    [ "$has_docker" = "true" ] && docker_flag="true"

    if [ "$project_type" = "go" ]; then
      # Go-specific CI: prompt for optional lint tool
      local go_lint_cmd="golangci-lint run"
      if ask_yes_no "    Use golangci-lint for Go linting?" "y"; then
        go_lint_cmd="golangci-lint run"
      fi

      cat > .github/workflows/ci.yml << CIEOF
name: CI
on:
  pull_request:
  push:
    branches: [${base_branch}]
concurrency:
  group: ci-\${{ github.ref }}
  cancel-in-progress: true
jobs:
  ci:
    uses: ${TOOLKIT_REPO}/.github/workflows/ci.yml@${TOOLKIT_REF}
    with:
      install_command: 'go mod download'
      test_command: 'go test ./...'
      build_command: 'go build ./...'
      lint_command: '${go_lint_cmd}'
      typecheck_command: 'go vet ./...'
      docker_build: ${docker_flag}
    secrets: inherit
CIEOF
      ok "Created .github/workflows/ci.yml (Go)"

    elif [ "$project_type" = "python" ]; then
      # Python-specific CI: prompt for tooling preferences
      local py_test_cmd="pytest"
      local py_lint_cmd="ruff check ."
      local py_typecheck_cmd="mypy ."
      local py_install_cmd='pip install -e ".[dev]"'

      if ask_yes_no "    Use ruff for Python linting?" "y"; then
        py_lint_cmd="ruff check ."
      else
        py_lint_cmd="flake8"
      fi
      if ask_yes_no "    Use mypy for type checking?" "y"; then
        py_typecheck_cmd="mypy ."
      else
        py_typecheck_cmd="echo 'No type check'"
      fi

      cat > .github/workflows/ci.yml << CIEOF
name: CI
on:
  pull_request:
  push:
    branches: [${base_branch}]
concurrency:
  group: ci-\${{ github.ref }}
  cancel-in-progress: true
jobs:
  ci:
    uses: ${TOOLKIT_REPO}/.github/workflows/ci.yml@${TOOLKIT_REF}
    with:
      install_command: '${py_install_cmd}'
      test_command: '${py_test_cmd}'
      build_command: 'echo "No build step"'
      lint_command: '${py_lint_cmd}'
      typecheck_command: '${py_typecheck_cmd}'
      docker_build: ${docker_flag}
    secrets: inherit
CIEOF
      ok "Created .github/workflows/ci.yml (Python)"

    else
      # Node.js default
      cat > .github/workflows/ci.yml << CIEOF
name: CI
on:
  pull_request:
  push:
    branches: [${base_branch}]
concurrency:
  group: ci-\${{ github.ref }}
  cancel-in-progress: true
jobs:
  ci:
    uses: ${TOOLKIT_REPO}/.github/workflows/ci.yml@${TOOLKIT_REF}
    with:
      docker_build: ${docker_flag}
    secrets: inherit
CIEOF
      ok "Created .github/workflows/ci.yml"
    fi
  fi

  if [ "$enable_e2e" = "true" ]; then
    cat > .github/workflows/e2e.yml << E2EEOF
name: E2E Tests
on:
  schedule:
    - cron: '0 1 * * *'
  workflow_dispatch:
jobs:
  e2e:
    uses: ${TOOLKIT_REPO}/.github/workflows/e2e.yml@${TOOLKIT_REF}
    secrets: inherit
E2EEOF
    ok "Created .github/workflows/e2e.yml"
  fi

  if [ "$enable_deploy_staging" = "true" ]; then
    local app_name
    read -rp "  App name for staging (e.g., myapp-beta): " app_name
    app_name="${app_name:-app-beta}"

    cat > .github/workflows/deploy-staging.yml << DSEOF
name: Deploy Staging
on:
  push:
    branches: [main]
  workflow_dispatch:
jobs:
  deploy:
    uses: ${TOOLKIT_REPO}/.github/workflows/deploy-staging.yml@${TOOLKIT_REF}
    with:
      app_name: ${app_name}
      deploy_dir: /opt/services/apps/${app_name}
      image_name: ghcr.io/\${{ github.repository_owner }}/\${{ github.event.repository.name }}
    secrets: inherit
DSEOF
    ok "Created .github/workflows/deploy-staging.yml"
  fi

  if [ "$enable_deploy_prod" = "true" ]; then
    local app_name_prod
    read -rp "  App name for production (e.g., myapp-prod): " app_name_prod
    app_name_prod="${app_name_prod:-app-prod}"

    cat > .github/workflows/deploy-production.yml << DPEOF
name: Deploy Production
on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      version:
        description: 'Version to deploy'
        required: true
jobs:
  deploy:
    uses: ${TOOLKIT_REPO}/.github/workflows/deploy-production.yml@${TOOLKIT_REF}
    with:
      app_name: ${app_name_prod}
      deploy_dir: /opt/services/apps/${app_name_prod}
      image_name: ghcr.io/\${{ github.repository_owner }}/\${{ github.event.repository.name }}
      version: \${{ inputs.version || github.event.release.tag_name }}
    secrets: inherit
DPEOF
    ok "Created .github/workflows/deploy-production.yml"
  fi

  if [ "$enable_ci_heal" = "true" ]; then
    cat > .github/workflows/ci-heal.yml << CHEOF
name: CI Auto-Heal
on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]
jobs:
  heal:
    uses: ${TOOLKIT_REPO}/.github/workflows/ci-heal.yml@${TOOLKIT_REF}
    secrets: inherit
CHEOF
    ok "Created .github/workflows/ci-heal.yml"
  fi

  if [ "$enable_openspec" = "true" ]; then
    cat > .github/workflows/openspec-interview.yml << OIEOF
name: OpenSpec Interview
on:
  issues:
    types: [labeled]
  issue_comment:
    types: [created]
jobs:
  interview:
    uses: ${TOOLKIT_REPO}/.github/workflows/openspec-interview.yml@${TOOLKIT_REF}
    secrets: inherit
OIEOF
    ok "Created .github/workflows/openspec-interview.yml"

    cat > .github/workflows/openspec-propose.yml << OPEOF
name: OpenSpec Propose
on:
  issue_comment:
    types: [created]
jobs:
  propose:
    uses: ${TOOLKIT_REPO}/.github/workflows/openspec-propose.yml@${TOOLKIT_REF}
    with:
      base_branch: ${base_branch}
    secrets: inherit
OPEOF
    ok "Created .github/workflows/openspec-propose.yml"

    cat > .github/workflows/openspec-orchestrate.yml << OOEOF
name: OpenSpec Orchestrate
on:
  workflow_dispatch:
    inputs:
      change_name:
        description: 'OpenSpec change name'
        required: true
jobs:
  orchestrate:
    uses: ${TOOLKIT_REPO}/.github/workflows/openspec-orchestrate.yml@${TOOLKIT_REF}
    with:
      change_name: \${{ inputs.change_name }}
      base_branch: ${base_branch}
    secrets: inherit
OOEOF
    ok "Created .github/workflows/openspec-orchestrate.yml"
  fi

  # Print next steps
  echo ""
  echo -e "${GREEN}=== Setup complete! ===${NC}"
  echo ""
  info "Next steps:"
  echo ""
  echo "  1. Review the generated workflows in .github/workflows/"
  echo "  2. Configure required secrets in GitHub Settings > Secrets:"
  echo ""

  if [ "$enable_ci_heal" = "true" ]; then
    echo "     - PAT_TOKEN (GitHub PAT with repo + workflow permissions)"
  fi
  if [ "$enable_deploy_staging" = "true" ] || [ "$enable_deploy_prod" = "true" ]; then
    echo "     - DEPLOY_HOST, DEPLOY_USER, DEPLOY_SSH_KEY, DEPLOY_PORT"
  fi
  if [ "$enable_openspec" = "true" ]; then
    echo "     - ANTHROPIC_API_KEY (Claude API key)"
    echo "     - OPENSPEC_GH_TOKEN (GitHub PAT for branch/PR creation)"
  fi

  echo ""
  echo "  3. Commit and push the workflow files"
  echo ""

  # Validate generated files
  validate_scaffolded
}

main "$@"
