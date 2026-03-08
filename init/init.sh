#!/usr/bin/env bash
# devops-toolkit init — scaffold caller workflows for a project
# Usage: curl -fsSL https://raw.githubusercontent.com/SevFle/devops-toolkit/main/init/init.sh | bash

set -euo pipefail

TOOLKIT_REPO="SevFle/devops-toolkit"
TOOLKIT_REF="main"
RAW_BASE="https://raw.githubusercontent.com/${TOOLKIT_REPO}/${TOOLKIT_REF}"
LOCAL_TEMPLATE_ROOT="${DEVOPS_TOOLKIT_LOCAL_ROOT:-}"

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
      if python3 -c "import yaml, sys; yaml.safe_load(open(sys.argv[1]))" "$f" 2>/dev/null; then
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

  local tmp
  tmp=$(mktemp)
  if [ -n "$LOCAL_TEMPLATE_ROOT" ] && [ -f "$LOCAL_TEMPLATE_ROOT/init/templates/${template}" ]; then
    cp "$LOCAL_TEMPLATE_ROOT/init/templates/${template}" "$tmp"
  else
    if ! curl -fsSL "${RAW_BASE}/init/templates/${template}" -o "$tmp" 2>/dev/null; then
      err "Failed to download template: $template"
      rm -f "$tmp"
      return 1
    fi
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
  # NOTE: Nested blocks are still not supported, but this Python fallback is
  # portable across GNU/BSD environments and does not rely on sed dialects.
  content=$(CONTENT="$content" python3 -c "import os, re; print(re.sub(r'\\n?\\{\\{#[A-Z_]+\\}\\}[\\s\\S]*?\\{\\{/[A-Z_]+\\}\\}\\n?', '\\n', os.environ['CONTENT']), end='')")

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
  local enable_security=false
  local enable_pr_quality=false
  local enable_codeql=false
  local enable_ai_code_review=false
  local enable_deploy_staging=false
  local enable_deploy_prod=false
  local enable_deploy_k8s=false
  local enable_ci_heal=false
  local enable_housekeeping=false
  local enable_openspec=false
  local enable_owasp_audit=false
  local enable_perf_audit=false
  local enable_test_gaps=false
  local enable_dead_code=false
  local enable_api_compat=false
  local enable_tech_debt=false

  ask_yes_no "  CI Pipeline (lint, type-check, test, build)" "y" && enable_ci=true
  ask_yes_no "  E2E Tests (Playwright)" "n" && enable_e2e=true
  ask_yes_no "  Security Scans (secrets, deps, containers)" "y" && enable_security=true
  ask_yes_no "  PR Quality Gate" "y" && enable_pr_quality=true
  ask_yes_no "  CodeQL Analysis" "n" && enable_codeql=true
  ask_yes_no "  AI Code Review" "n" && enable_ai_code_review=true

  if [ "$has_docker" = "true" ]; then
    ask_yes_no "  Deploy to Staging (Docker + VPS)" "n" && enable_deploy_staging=true
    ask_yes_no "  Deploy to Production (Docker + VPS)" "n" && enable_deploy_prod=true
    ask_yes_no "  Deploy to Kubernetes" "n" && enable_deploy_k8s=true
  fi

  ask_yes_no "  CI Auto-Heal (Claude fixes CI failures)" "n" && enable_ci_heal=true
  ask_yes_no "  Housekeeping (stale issues, branch cleanup)" "n" && enable_housekeeping=true
  ask_yes_no "  OpenSpec Pipeline (interview + propose + orchestrate)" "n" && enable_openspec=true
  ask_yes_no "  Weekly OWASP Audit" "n" && enable_owasp_audit=true
  ask_yes_no "  Weekly Performance Audit" "n" && enable_perf_audit=true
  ask_yes_no "  Weekly Test Gap Analysis" "n" && enable_test_gaps=true
  ask_yes_no "  Weekly Dead Code Analysis" "n" && enable_dead_code=true
  ask_yes_no "  PR API Compatibility Check" "n" && enable_api_compat=true
  ask_yes_no "  Weekly Tech Debt Scan" "n" && enable_tech_debt=true

  echo ""

  # Generate caller workflows
  if [ "$enable_ci" = "true" ]; then
    local docker_flag="false"
    [ "$has_docker" = "true" ] && docker_flag="true"

    if [ "$project_type" = "go" ]; then
      render_template \
        "ci-go.yml.tmpl" \
        ".github/workflows/ci.yml" \
        BASE_BRANCH "$base_branch" \
        DOCKER_BUILD "$docker_flag"

    elif [ "$project_type" = "python" ]; then
      render_template \
        "ci-python.yml.tmpl" \
        ".github/workflows/ci.yml" \
        BASE_BRANCH "$base_branch" \
        DOCKER_BUILD "$docker_flag"

    else
      render_template \
        "ci.yml.tmpl" \
        ".github/workflows/ci.yml" \
        BASE_BRANCH "$base_branch" \
        DOCKER_BUILD "$docker_flag"
    fi
  fi

  if [ "$enable_e2e" = "true" ]; then
    render_template "e2e.yml.tmpl" ".github/workflows/e2e.yml"
  fi

  if [ "$enable_security" = "true" ]; then
    render_template \
      "security.yml.tmpl" \
      ".github/workflows/security.yml" \
      SCAN_DOCKER "$has_docker"
  fi

  if [ "$enable_pr_quality" = "true" ]; then
    render_template "pr-quality.yml.tmpl" ".github/workflows/pr-quality.yml"
  fi

  if [ "$enable_codeql" = "true" ]; then
    render_template \
      "codeql.yml.tmpl" \
      ".github/workflows/codeql.yml" \
      BASE_BRANCH "$base_branch"
  fi

  if [ "$enable_ai_code_review" = "true" ]; then
    render_template "ai-code-review.yml.tmpl" ".github/workflows/ai-code-review.yml"
  fi

  if [ "$enable_deploy_staging" = "true" ]; then
    local app_name
    read -rp "  App name for staging (e.g., myapp): " app_name
    app_name="${app_name:-app}"

    render_template \
      "deploy-staging.yml.tmpl" \
      ".github/workflows/deploy-staging.yml" \
      BASE_BRANCH "$base_branch" \
      APP_NAME "$app_name"
  fi

  if [ "$enable_deploy_prod" = "true" ]; then
    local app_name_prod
    read -rp "  App name for production (e.g., myapp): " app_name_prod
    app_name_prod="${app_name_prod:-app}"

    render_template \
      "deploy-production.yml.tmpl" \
      ".github/workflows/deploy-production.yml" \
      APP_NAME "$app_name_prod"
  fi

  if [ "$enable_deploy_k8s" = "true" ]; then
    local namespace
    read -rp "  Kubernetes namespace: " namespace
    namespace="${namespace:-default}"
    local app_name_k8s
    read -rp "  Kubernetes deployment name: " app_name_k8s
    app_name_k8s="${app_name_k8s:-app}"

    render_template \
      "deploy-k8s.yml.tmpl" \
      ".github/workflows/deploy-k8s.yml" \
      BASE_BRANCH "$base_branch" \
      APP_NAME "$app_name_k8s" \
      NAMESPACE "$namespace"
  fi

  if [ "$enable_ci_heal" = "true" ]; then
    render_template "ci-heal.yml.tmpl" ".github/workflows/ci-heal.yml"
  fi

  if [ "$enable_housekeeping" = "true" ]; then
    render_template "housekeeping.yml.tmpl" ".github/workflows/housekeeping.yml"
  fi

  if [ "$enable_openspec" = "true" ]; then
    render_template "openspec-interview.yml.tmpl" ".github/workflows/openspec-interview.yml"
    render_template \
      "openspec-propose.yml.tmpl" \
      ".github/workflows/openspec-propose.yml" \
      BASE_BRANCH "$base_branch"
    render_template \
      "openspec-orchestrate.yml.tmpl" \
      ".github/workflows/openspec-orchestrate.yml" \
      BASE_BRANCH "$base_branch"
  fi

  if [ "$enable_owasp_audit" = "true" ]; then
    render_template "ai-owasp-audit.yml.tmpl" ".github/workflows/ai-owasp-audit.yml"
  fi

  if [ "$enable_perf_audit" = "true" ]; then
    render_template "ai-perf-audit.yml.tmpl" ".github/workflows/ai-perf-audit.yml"
  fi

  if [ "$enable_test_gaps" = "true" ]; then
    render_template "ai-test-gaps.yml.tmpl" ".github/workflows/ai-test-gaps.yml"
  fi

  if [ "$enable_dead_code" = "true" ]; then
    render_template "ai-dead-code.yml.tmpl" ".github/workflows/ai-dead-code.yml"
  fi

  if [ "$enable_api_compat" = "true" ]; then
    render_template "ai-api-compat.yml.tmpl" ".github/workflows/ai-api-compat.yml"
  fi

  if [ "$enable_tech_debt" = "true" ]; then
    render_template "ai-tech-debt.yml.tmpl" ".github/workflows/ai-tech-debt.yml"
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

  if [ "$enable_ci_heal" = "true" ] || [ "$enable_openspec" = "true" ]; then
    echo "     - PAT_TOKEN (GitHub PAT with repo + workflow permissions)"
  fi
  if [ "$enable_deploy_staging" = "true" ] || [ "$enable_deploy_prod" = "true" ]; then
    echo "     - DEPLOY_HOST, DEPLOY_USER, DEPLOY_SSH_KEY, DEPLOY_PORT"
  fi
  if [ "$enable_deploy_k8s" = "true" ]; then
    echo "     - REGISTRY_USERNAME, REGISTRY_PASSWORD, KUBECONFIG"
  fi

  echo ""
  echo "  3. Commit and push the workflow files"
  echo ""

  # Validate generated files
  validate_scaffolded
}

main "$@"
