# GLM + Claude PR Orchestrator — Design Spec

- **Date**: 2026-04-25
- **Author**: SevFle (interview-driven)
- **Status**: Draft (awaiting user review)
- **Repo**: `SevFle/devops-toolkit`
- **Replaces**: existing `orchestrator/` Python (OpenSpec pipeline) and `.github/workflows/openspec-*.yml`
- **Deploy target**: Strato VPS (SSH-deployed Docker Compose stack)

---

## 1. Goal

A self-hosted orchestration service that drives an AI-implementation + AI-review loop on GitHub repositories.

Roles:

- **Implementer** — GLM-5.1 (Z.AI), invoked through the standard `claude` CLI binary by overriding `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` to Z.AI's Anthropic-compatible endpoint. Owns code generation and remediation.
- **Reviewer** — `claude-opus-4-7` (real Anthropic) on PR diffs. Owns review verdicts, structured findings, approval signal.
- **Interviewer** — `claude-sonnet-4-6` (real Anthropic) on issues. Owns multi-turn issue interview to fill the implementation template.

The service receives GitHub webhooks, runs a state machine per issue, dispatches stage-specific jobs to workers, posts results back through GitHub's PR review and merge surfaces, and enforces policy gates (risk paths, remediation cycle limits, codeowner-required merges).

## 2. Non-goals

- Multi-tenant SaaS (single-org, single VPS, allowlisted admin logins).
- Cost kill switch (costs are logged for audit, never used to abort).
- Kubernetes deployment.
- Pluggable implementer providers beyond Z.AI/GLM in MVP (interface designed for future plug-ins).
- Web SPA admin UI (server-rendered Jinja + htmx is enough).
- Cross-PR or project-wide memory / "learning" features.
- Replay of historical issues on enrollment (forward-looking only).

## 3. System topology

```
GitHub ──webhook (App)──▶ Caddy (TLS) ──▶ api (FastAPI)
                                          │
                                          │ INSERT job + NOTIFY
                                          ▼
                                      postgres
                                          ▲
                                          │ SKIP LOCKED pull
                                          │
                        workers (N replicas) ──shell──▶ claude CLI
                                          │              (env-scoped profile:
                                          │               glm-implementer | anthropic-reviewer | interviewer)
                                          │
                                          └──git push / gh pr / gh review──▶ GitHub
```

### 3.1 Containers

One image, multi-entrypoint. Entry script dispatches on `$ROLE`: `api`, `worker`, `migrate`.

| Service | Replicas | Purpose |
|---|---|---|
| `caddy` | 1 | TLS termination, reverse proxy, rate-limit `/gh/webhook` and `/admin/auth/*` |
| `api` | 1 | FastAPI: webhook receiver, admin REST + minimal HTML UI, OAuth |
| `worker` | N (default 2) | Job runner; subprocess `claude` CLI per profile |
| `postgres` | 1 | State, audit, queue, OAuth sessions |
| `migrator` | 1 (one-shot) | `alembic upgrade head` then exit |

**Why split api/worker** — CLI runs are minutes-long. The API must respond within GitHub's webhook SLO (~10 s before retry).

## 4. State machine

States per run (issue lifecycle):

```
NEW ──label ai-triage──▶ INTERVIEWING ──template complete──▶ READY
READY ──label ai-implement──▶ IMPLEMENTING ──push+PR──▶ PR_OPEN
PR_OPEN ──review.submitted──▶ UNDER_REVIEW
UNDER_REVIEW ──verdict=blocking──▶ REMEDIATING ──push──▶ UNDER_REVIEW   (cycle, max N)
UNDER_REVIEW ──verdict=approve──▶ APPROVED
APPROVED ──codeowner approval──▶ MERGEABLE
MERGEABLE ──auto-merge──▶ MERGED
any ──timeout/limit/abort/risk──▶ FAILED | HUMAN_REQUIRED
```

Policy gates intercept transitions:

- Paths matching risk globs → force `HUMAN_REQUIRED` before `MERGEABLE`.
- Remediation cycles ≥ limit → `HUMAN_REQUIRED`.
- Review finding category `security` + severity `blocking` → `HUMAN_REQUIRED`.

Every transition is written inside the same transaction as a `run_events` audit row. No state change without an audit row.

## 5. Data model (Postgres)

| Table | Purpose | Key columns |
|---|---|---|
| `repos` | enrolled repos | `id`, `full_name`, `installation_id`, `policy_yaml_sha`, `policy_snapshot_jsonb` |
| `runs` | one per issue lifecycle | `id`, `repo_id`, `issue_number`, `pr_number?`, `state`, `state_updated_at`, `cycle_count`, `codemap_md` |
| `jobs` | unit of worker work | `id`, `run_id`, `kind` (`interview_turn`\|`implement`\|`review`\|`remediate`\|`merge`), `profile`, `status`, `attempts`, `priority`, `locked_by`, `locked_until`, `payload_jsonb`, `result_jsonb` |
| `run_events` | append-only audit | `id`, `run_id`, `job_id?`, `ts`, `level`, `type`, `data_jsonb` |
| `cli_runs` | per-subprocess record | `id`, `job_id`, `profile`, `model`, `started_at`, `ended_at`, `exit_code`, `token_in`, `token_out`, `usd_est`, `log_path` |
| `gh_webhooks` | raw webhook capture | `id`, `delivery_id` (unique), `event`, `received_at`, `payload_jsonb` |
| `policy_decisions` | gate evaluations | `id`, `run_id`, `gate`, `verdict`, `reason`, `ts`, `actor?` |
| `installation_tokens` | cached App tokens | `installation_id`, `token`, `expires_at` |
| `oauth_sessions` | admin UI auth | `id`, `user_login`, `token_hash`, `expires_at` |

**Queue semantics**

```sql
SELECT … FROM jobs
WHERE status='ready' AND locked_until < now()
ORDER BY priority DESC, id ASC
FOR UPDATE SKIP LOCKED LIMIT 1;
UPDATE jobs SET locked_by=$worker, locked_until=now()+lease WHERE id=$id;
```

Wakeup via `LISTEN jobs_ready`; workers also poll every 5 s as a safety net.

**Concurrency safeguards**

- Per-repo lock via `pg_advisory_xact_lock(hashtext('repo:'||full_name))` while claiming an `implement` or `remediate` job — prevents two PRs racing the same branch.
- Per-run lock via `pg_advisory_xact_lock(hashtext('run:'||run_id))` — interview turns serialize naturally.

## 6. Repo layout

```
orchestrator/                       # service root (replaces old python in-place)
├── pyproject.toml                  # FastAPI, asyncpg, sqlalchemy[asyncio], alembic,
│                                   # httpx, pyjwt (GH App), uvicorn, structlog,
│                                   # prometheus-client, pydantic-settings, pyyaml
├── alembic/
│   └── versions/
├── src/orchestrator/
│   ├── __main__.py                 # entrypoint dispatcher: api|worker|migrate|repl
│   ├── settings.py                 # pydantic-settings, env-driven
│   ├── api/
│   │   ├── app.py
│   │   ├── webhooks.py             # /gh/webhook — verify HMAC, persist, dispatch
│   │   ├── admin/
│   │   ├── auth.py                 # GH App JWT + install token cache + OAuth flow
│   │   └── deps.py
│   ├── worker/
│   │   ├── loop.py                 # poll + LISTEN
│   │   ├── leases.py               # SKIP LOCKED claim, heartbeat, reaper
│   │   └── handlers/
│   │       ├── interview.py
│   │       ├── implement.py
│   │       ├── review.py
│   │       ├── remediate.py
│   │       └── merge.py
│   ├── cli/
│   │   ├── profile.py              # registry of CLI profiles (§7)
│   │   ├── runner.py               # subprocess.run with explicit env=, stream capture
│   │   └── parsers.py              # structured JSON tail extraction per kind
│   ├── github/
│   │   ├── app.py                  # JWT, install token mint
│   │   ├── client.py               # thin httpx wrapper
│   │   ├── pr.py                   # create/update/comment/review/merge
│   │   └── codeowners.py
│   ├── policy/
│   │   ├── loader.py               # fetch .devops/orchestrator.yml from default branch
│   │   ├── schema.py               # pydantic policy model
│   │   └── gates.py                # paths, cycle limit, security findings
│   ├── state/
│   │   ├── machine.py              # transitions table + guards
│   │   └── transitions.py          # tx-wrapped transition + audit row
│   ├── db/
│   │   ├── models.py
│   │   ├── session.py
│   │   └── queue.py
│   ├── sandbox/
│   │   ├── workdir.py              # /var/lib/orchestrator/work/<run_id>
│   │   └── git.py                  # clone, checkout, branch, push (token-scoped)
│   ├── prompts/
│   │   ├── interview/
│   │   ├── implement/
│   │   ├── review/
│   │   └── remediate/
│   ├── obs/
│   │   ├── log.py                  # structlog JSON, secret-redaction allowlist
│   │   └── metrics.py              # prometheus counters / histograms / gauges
│   └── util/
│       └── ids.py
├── tests/
│   ├── unit/
│   ├── integration/                # testcontainers-pg + fake-gh server
│   └── e2e/
└── README.md

deploy/
├── docker/
│   ├── Dockerfile                  # multi-stage; bakes claude CLI binary
│   └── entrypoint.sh
├── compose/
│   ├── docker-compose.yml
│   ├── Caddyfile
│   └── .env.example
└── strato/
    ├── deploy.sh
    └── healthcheck.sh

docs/superpowers/specs/
└── 2026-04-25-glm-claude-orchestrator-design.md   # this spec

.github/workflows/
└── deploy-orchestrator.yml         # caller of repo's existing deploy-production.yml

config/
└── orchestrator.example.yml        # per-repo policy template
```

Old `orchestrator/{ci_fix.py,monitor.py,orchestrate.py,lib/}` and `.github/workflows/openspec-*.yml` are deprecated and removed in this change. `ci-heal.yml` is left standalone (different trigger surface — CI failure, not issue intake). `prompts/` at repo root: orchestrator-specific prompts move into the service tree; non-orchestrator prompts stay where they are.

## 7. CLI profile system

Single `claude` binary, three profiles. Never share `os.environ` — always explicit `env=` dict in `subprocess.run`.

```python
@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    base_url: str | None     # None = real Anthropic
    auth_env_var: str
    permission_mode: str     # "default" | "acceptEdits" | "plan"
    extra_env: dict[str, str]
    output_format: str       # "json" | "stream-json" | "text"
    max_turns: int | None
    timeout_s: int

PROFILES = {
    "interviewer": Profile(
        name="interviewer", model="claude-sonnet-4-6",
        base_url=None, auth_env_var="ANTHROPIC_API_KEY",
        permission_mode="plan", extra_env={},
        output_format="json", max_turns=12, timeout_s=300,
    ),
    "glm-implementer": Profile(
        name="glm-implementer", model="glm-5.1",
        base_url="https://api.z.ai/api/anthropic",
        auth_env_var="ZAI_API_KEY",
        permission_mode="acceptEdits", extra_env={"DISABLE_TELEMETRY": "1"},
        output_format="stream-json", max_turns=None, timeout_s=1800,
    ),
    "anthropic-reviewer": Profile(
        name="anthropic-reviewer", model="claude-opus-4-7",
        base_url=None, auth_env_var="ANTHROPIC_API_KEY",
        permission_mode="plan", extra_env={},
        output_format="json", max_turns=4, timeout_s=600,
    ),
}
```

The runner builds env from a tight allowlist (`PATH`, `HOME`, `LANG`, `TZ`) plus `GH_TOKEN` for the install token plus profile-specific Anthropic keys/URLs. `HOME` per run = `<sandbox>/.home` so `~/.claude/*` config never bleeds across profiles or runs. Settings file written there per job (allowedTools, MCP config, etc.).

Streams: stdout/stderr go to `cli_runs.log_path` rolling file in the sandbox plus tee to `run_events`. `stream-json` is parsed line-by-line for token usage and step events. Final structured tail is extracted with strict pydantic schemas per kind; malformed output is recorded raw and retried once with a stricter prompt before failing.

Cost telemetry is recorded per `cli_run` (`token_in`, `token_out`, `usd_est`). Audit only — no kill switch.

Sandboxes live at `/var/lib/orchestrator/work/<run_id>/` (`repo/`, `.home/`, `logs/`, `tmp/`). Reaper deletes terminal sandboxes after 7 days.

## 8. Core flows

### 8.1 Interview (NEW → READY)

```
GitHub: issues.opened with label `ai-triage`
  → POST /gh/webhook
  → verify HMAC(GitHub App secret), persist gh_webhooks
  → tx: upsert run(state=NEW), insert job(kind=interview_turn, payload={trigger:opened})
  → NOTIFY jobs_ready
worker.handlers.interview:
  → claim job, transition NEW→INTERVIEWING (audit row)
  → ensure sandbox: clone repo (App install token), persist /var/lib/orchestrator/work/<run_id>/repo
  → render prompt: interview/system.md + issue body + prior interview comments + repo codemap
  → cli.runner: claude --profile=interviewer --model=claude-sonnet-4-6
                  --cwd=<sandbox> --output-format=json
  → parse: { kind: "ask" | "complete", question?, template? }
  → if ask: post issue comment with question + base64 state marker; job done
  → if complete: post template comment, remove `ai-triage`, add `ai-implement`,
                 transition INTERVIEWING→READY
GitHub: issue_comment.created (author = original reporter, run state=INTERVIEWING)
  → enqueue interview_turn(payload={trigger:reply})
```

**Codemap for interviewer** — at sandbox creation, generate a lightweight repo summary (top-level tree, per-dir purpose hints from `graphify` if available, else `git ls-files | head -200` plus root READMEs). Stored once as `runs.codemap_md`, reused across turns.

### 8.2 Implement (READY → PR_OPEN)

```
GitHub: issues.labeled (label `ai-implement`, run state=READY)
  → enqueue job(kind=implement)
worker.handlers.implement:
  → transition READY→IMPLEMENTING
  → sandbox: ensure clone fresh, create branch ai/<run_id>-<slug>
  → render prompt: implement/system.md + issue template + AC + files + test command
  → cli.runner: claude --profile=glm-implementer
                  env: ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic,
                       ANTHROPIC_AUTH_TOKEN=$ZAI_KEY,
                       ANTHROPIC_MODEL=glm-5.1
                  --cwd=<sandbox> --permission-mode=acceptEdits
  → after CLI: git diff --stat, run policy.test_command — capture pass/fail
  → if no changes: → FAILED, comment, stop
  → git push (App install token), open PR via gh.pr.create
                  body=template + run_id + costs + risk notes
  → transition IMPLEMENTING→PR_OPEN
```

### 8.3 Review (PR_OPEN → UNDER_REVIEW → APPROVED|REMEDIATING)

```
GitHub: pull_request.opened by app user (state=PR_OPEN)
  → enqueue job(kind=review)
worker.handlers.review:
  → transition PR_OPEN→UNDER_REVIEW
  → fetch PR diff via gh, fetch repo HEAD into sandbox/review (separate clone, ro)
  → render prompt: review/system.md + diff + relevant files + AC from issue + remediation_history
  → cli.runner: claude --profile=anthropic-reviewer --model=claude-opus-4-7
                  --cwd=<sandbox-ro> --output-format=json
  → parse:
       {
         verdict: "approve" | "request_changes" | "blocking",
         findings: [
           { category: "bug"|"security"|"perf"|"tests_missing"|"style",
             severity: "blocking"|"non_blocking",
             path, line?, message, suggested_fix? }
         ]
       }
  → post pull_request_review with inline comments per finding
  → policy gate: any (security & blocking) OR cycle_count >= max → HUMAN_REQUIRED
  → if verdict=approve: UNDER_REVIEW→APPROVED
  → else: UNDER_REVIEW→REMEDIATING, enqueue remediate
```

### 8.4 Remediate (REMEDIATING → UNDER_REVIEW)

```
worker.handlers.remediate:
  → cycle_count += 1; if > policy.max_cycles → HUMAN_REQUIRED, stop
  → render prompt: remediate/system.md + structured findings + diff so far + AC
  → cli.runner: claude --profile=glm-implementer
  → push to same branch
  → on push, GH fires pull_request.synchronize → enqueue review job
```

### 8.5 Merge (APPROVED → MERGEABLE → MERGED)

```
APPROVED set:
  → policy gate: paths in risk-globs OR codeowner mismatch OR explicit human_required
       → HUMAN_REQUIRED, comment requesting human approval, stop
  → else if policy.auto_merge=true and required PR checks green:
       → wait for codeowner approval (pull_request_review.submitted state=approved by codeowner)
       → APPROVED→MERGEABLE → gh.pr.merge(squash) → MERGED
  → else: comment "ready to merge", stop
GitHub: pull_request_review.submitted (state=approved)
  → if reviewer is codeowner & run.state=APPROVED → enqueue job(kind=merge)
```

### 8.6 Failure paths

- CLI exit ≠ 0 → retry with backoff up to `attempts < 3`, then `FAILED`, post issue comment with run_id and link to last 50 log lines.
- Subprocess timeout (per kind, default 30 m impl / 10 m review / 5 m interview) → SIGTERM, mark job `failed_timeout`.
- Webhook signature mismatch → 401, log, no DB write.
- Stuck job (lease expired, worker crashed) → reaper cron job re-claims after `locked_until + grace`.

## 9. Per-repo policy

`.devops/orchestrator.yml` lives in each enrolled repo on the default branch. Loaded fresh at every webhook by SHA, snapshotted into `repos.policy_snapshot_jsonb` for run-time use.

```yaml
version: 1

intake:
  triage_label: ai-triage
  implement_label: ai-implement
  required_template_fields: [goal, acceptance_criteria, files, test_command, risk_level]

models:
  interviewer: claude-sonnet-4-6
  implementer: glm-5.1
  reviewer: claude-opus-4-7

limits:
  max_remediation_cycles: 3
  diff_max_files: 40
  diff_max_lines: 2000
  per_run_timeout_h: 4

risk_globs:
  - "migrations/**"
  - "**/migrations/**"
  - "auth/**"
  - "**/auth/**"
  - "payments/**"
  - "infra/**"
  - "deploy/**"
  - ".github/workflows/**"

human_required_when:
  - any_path_matches: risk_globs
  - cycle_at_or_above: 3
  - finding:
      category: security
      severity: blocking

merge:
  strategy: squash
  auto_merge: true
  required_codeowner_approval: true
  required_checks: [ci, security]

review:
  comment_style: inline                 # inline | summary
  ignore_categories: [style]
  approve_when_no_findings: true

test_command: "make test"

prompts:
  implement_overlay: ".devops/prompts/implement-overlay.md"
  review_overlay: ".devops/prompts/review-overlay.md"

ignore:
  paths: ["docs/**", "**/*.md"]
```

Validation: pydantic schema on load. Invalid YAML → comment on the triggering issue with location and reason; no run created.

### 9.1 Onboarding

1. Install GitHub App on repo → `installation.created` webhook.
2. Orchestrator inserts a `repos` row, posts an onboarding issue with the policy template.
3. On first PR adding `.devops/orchestrator.yml`, orchestrator validates and updates the snapshot.
4. From then on, normal flow.

### 9.2 Codeowner enforcement

`github/codeowners.py` parses `CODEOWNERS` (root, `.github/`, `docs/`). On `pull_request_review.submitted` (state=approved), match the reviewer login against owners of every changed path. The merge gate is satisfied only when all owned paths are covered by approving reviewers. Partial coverage → comment listing uncovered paths.

## 10. Admin surface

Auth: GitHub OAuth via the App's OAuth flow. Authorized = login matches `settings.admin_logins[]`. Session cookie is signed, httpOnly, secure, samesite=lax, 7-day TTL, persisted to `oauth_sessions`. CSRF: state on login + double-submit on POST.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/runs` | list runs, filter by repo/state |
| `GET` | `/admin/runs/{id}` | run detail: events, jobs, cli_runs, costs, policy snapshot |
| `POST` | `/admin/runs/{id}/abort` | force `FAILED` |
| `POST` | `/admin/runs/{id}/resume` | re-enqueue from current state (idempotent) |
| `POST` | `/admin/runs/{id}/override-gate` | bypass `HUMAN_REQUIRED` for one transition (writes `policy_decisions` row with operator login) |
| `GET` | `/admin/repos` | enrolled repos + policy snapshots |
| `POST` | `/admin/repos/{id}/refresh-policy` | force pull `.devops/orchestrator.yml` |
| `GET` | `/admin/jobs/queue` | live job queue depth + leases |
| `GET` | `/admin/healthz` | unauth, basic up + db ping |
| `GET` | `/admin/metrics` | prometheus, bearer-protected |

UI: server-rendered Jinja + htmx for live updates. No SPA. Every admin mutation writes a `run_events` row with `data.actor=<login>`.

## 11. Auth & secrets

### 11.1 GitHub App

Permissions (least privilege):

| Permission | Level | Why |
|---|---|---|
| Contents | read & write | clone, push, branch |
| Pull requests | read & write | open, comment, review, merge |
| Issues | read & write | interview comments, labels |
| Metadata | read | required default |
| Checks | read | wait for required checks |
| Members | read | codeowner team resolution |
| Workflows | read | observe CI |

Events: `issues`, `issue_comment`, `pull_request`, `pull_request_review`, `pull_request_review_comment`, `installation`, `installation_repositories`, `check_run` (terminal status only).

Webhook is HMAC-SHA256 verified with the App webhook secret. Reject and log on mismatch; persist raw payload to `gh_webhooks` for replay.

Tokens:

- App JWT minted from RS256 private key, 9-minute TTL.
- Installation tokens cached in `installation_tokens`, 50-minute TTL, refreshed lazily.
- Tokens never logged; structlog redaction filter on emit.

Outbound auth in CLI subprocesses: install token in `GH_TOKEN` env, scoped per-process. `git push` uses `https://x-access-token:$GH_TOKEN@github.com/...` form, scrubbed in logs.

### 11.2 Secrets

Stored in `.env` on the VPS, owned by the `orchestrator` system user (mode 600). Loaded via pydantic-settings.

```
DATABASE_URL=postgresql+asyncpg://...
GH_APP_ID=...
GH_APP_PRIVATE_KEY_PATH=/etc/orchestrator/gh-app.pem
GH_APP_WEBHOOK_SECRET=...
GH_APP_OAUTH_CLIENT_ID=...
GH_APP_OAUTH_CLIENT_SECRET=...
ANTHROPIC_API_KEY=...
ZAI_API_KEY=...
SESSION_SIGNING_KEY=...                       # 32 bytes, base64
ADMIN_LOGINS=SevFle,...
PROMETHEUS_BEARER=...
PUBLIC_BASE_URL=https://orchestrator.<your-domain>
```

Rotation: `.env` swap + `docker compose up -d --force-recreate api worker`. No secret in image. No secret in compose file.

## 12. Observability

### 12.1 Logging

`structlog` JSON to stdout. Fields: `ts`, `level`, `event`, `run_id`, `job_id`, `repo`, `pr`, `actor`, `kind`, `model`, `latency_ms`, `tokens_in`, `tokens_out`, `usd_est`, optional `error.type`/`error.msg`. Secrets/tokens filtered by allowlist on emit.

Docker log driver: json-file with rotation `max-size=100m`, `max-file=10`. Forwarding to Loki/Grafana is a future option, not blocking MVP.

### 12.2 Metrics (`prometheus-client`)

Counters:

- `orch_webhooks_total{event,status}`
- `orch_runs_total{state_transition}`
- `orch_jobs_total{kind,outcome}`
- `orch_cli_runs_total{profile,exit}`
- `orch_policy_decisions_total{gate,verdict}`
- `orch_human_required_total{reason}`

Histograms:

- `orch_job_duration_seconds{kind}`
- `orch_cli_duration_seconds{profile}`
- `orch_review_findings{category,severity}`

Gauges:

- `orch_queue_depth{kind}`
- `orch_active_workers`
- `orch_oldest_pending_age_seconds`
- `orch_workdir_free_bytes`

`/admin/metrics` is bearer-protected (separate from OAuth, for the Prometheus scraper).

## 13. Deploy (Strato VPS)

The orchestrator deploys via the repo's existing `deploy-production.yml` reusable workflow. New caller `.github/workflows/deploy-orchestrator.yml`:

```yaml
on:
  push:
    branches: [main]
    paths: ['orchestrator/**', 'deploy/**']
  workflow_dispatch:
jobs:
  deploy:
    uses: ./.github/workflows/deploy-production.yml
    with:
      service: orchestrator
      compose_file: deploy/compose/docker-compose.yml
      ssh_host: ${{ vars.STRATO_HOST }}
      health_url: https://orchestrator.<domain>/admin/healthz
      pre_hook: deploy/strato/pre.sh        # alembic upgrade head
      post_hook: deploy/strato/post.sh      # warm caches, smoke test webhook signature
    secrets:
      ssh_private_key: ${{ secrets.STRATO_SSH_KEY }}
      env_file: ${{ secrets.ORCHESTRATOR_ENV }}
```

**Compose** sketch:

```yaml
services:
  caddy:        # TLS, reverse proxy
  api:          # 1 replica, 8000
  worker:       # 2 replicas (compose deploy.replicas)
  postgres:     # 16, named volume, daily pg_dump cron sidecar
  migrator:     # one-shot
networks: { default: }
volumes: { pgdata, work, caddy-data, caddy-config }
```

Healthcheck: `GET /admin/healthz` returns `{db: ok, queue_depth: N, workers: N}`. Compose `healthcheck` blocks `api`/`worker` from "ready" until pg is up.

Backups: daily `pg_dump` to off-VPS S3-compatible bucket (Hetzner or Backblaze), 30-day retention. Sandbox dirs are not backed up.

## 14. Hardening

- Caddy: `Strict-Transport-Security`; rate-limit `/gh/webhook` (50/s) and `/admin/auth/*` (10/min/IP).
- Postgres bound to docker network only — no host port published.
- VPS firewall: 22, 80, 443 only; SSH key-only; fail2ban.
- `claude` CLI subprocesses run as a dedicated UID inside the container. No docker socket access. `--cap-drop=ALL`. Read-only rootfs except `/var/lib/orchestrator/work` and `/tmp`.

## 15. Testing

| Layer | Tooling | Targets |
|---|---|---|
| Unit | pytest, pytest-asyncio | `state/machine`, `policy/gates`, `cli/profile`, `github/codeowners`, `cli/parsers` |
| Integration | testcontainers-pg + `respx` (httpx mock) + fake-gh server | webhook→job→state, OAuth flow, install token cache, codeowner enforcement, advisory locks |
| CLI contract | `pytest-recording` cassettes per profile | parser robustness, malformed-output handling, both endpoints |
| E2E | dedicated test repo (`SevFle/orchestrator-e2e-fixture`) + staging GH App install + ephemeral compose | golden-path; remediation cycle; HUMAN_REQUIRED on risk-glob; abort/resume |
| Load | k6 against `/gh/webhook` | 50 rps spike, 100 concurrent runs, queue not stalled |
| Chaos | manual scripts | kill worker mid-CLI → reaper claims → idempotent resume; pg restart → reconnect; secret rotation hot-swap |

Coverage target ≥ 80%. CI runs unit + integration on every PR; E2E runs on `main` post-merge gated by deploy-staging.

## 16. Migration plan

**Phase 0 — branch off, replace in place.** New code lands in `orchestrator/`. Old files moved to `orchestrator/_legacy/` for one release window, marked deprecated.

**Phase 1 — port valuable surface area.**

- `lib/git_ops.py` → split into `sandbox/git.py` + `github/pr.py`.
- `lib/log.py` → `obs/log.py`.
- `lib/complexity.py` → drop (replaced by per-kind static timeouts).
- `lib/history.py` → drop (replaced by `runs` + `run_events`).
- `lib/progress.py` → fold into `cli/parsers.py` for stream-json events.
- `lib/reviewer.py` → reference for new `worker/handlers/review.py`, but rewritten against profile system.
- `lib/claude_runner.py` → reference for new `cli/runner.py`, rewritten with strict env + profiles.
- Top-level `prompts/` → orchestrator-specific prompts move into the service tree.

**Phase 2 — workflows.**

- `.github/workflows/openspec-interview.yml`, `openspec-propose.yml`, `openspec-orchestrate.yml` → marked deprecated, replaced by webhook-driven service. Kept one release cycle, then deleted.
- `ci-heal.yml` (self-healing CI) → keeps separate identity (different trigger surface). Future option: optional consolidation as `kind=ci_heal` job, out of MVP scope.
- AI analysis workflows (`ai-code-review.yml`, `ai-test-gaps.yml`, etc.) → unchanged, complementary.

**Phase 3 — rollout.**

1. Deploy to Strato as `staging` first (separate App install on test repo). Run E2E for 7 days.
2. Promote to production install on real repos — start with one low-risk repo, expand.
3. Delete `orchestrator/_legacy/` and deprecated workflows after 30 days stable.

## 17. Risks & mitigations

| Risk | Mitigation |
|---|---|
| GLM tool-use parity gaps via Anthropic-compat | E2E cassette tests detect malformed tool calls; fallback prompt strategy in `prompts/implement/glm-fallback.md` triggered on parse failure |
| Z.AI endpoint downtime | Per-profile circuit breaker (5 fails / 60 s → 5 min open); pause new impl jobs, retain webhook persistence; runs queue rather than fail |
| Two PRs racing the same repo | Per-repo advisory lock (§5) |
| Webhook storms | Idempotent on `delivery_id`; rate-limit at Caddy; jobs deduped by `(run_id, kind, payload_hash)` for 60 s |
| Sandbox disk fill | Reaper deletes terminal sandboxes after 7 d; `df` watchdog metric `orch_workdir_free_bytes`, alert threshold |
| Secret leakage in logs | structlog redaction allowlist; snapshot tests against fixture log lines containing tokens |
| Policy YAML breaking change | `version:` field; loader rejects unknown versions; deprecation window for v1 → v2 |

## 18. Open items deferred to plan

- Exact prompt content for each phase (interview/system, implement/system, review/system, remediate/system) — drafted during implementation, validated against the test fixture repo.
- Token price table for `usd_est` — config file in `config/prices.yml`, updated per-model.
- Caddy site config specifics (domain, ACME email).
- Backup target bucket choice (Hetzner vs Backblaze) — ops detail.
- Exact `glm-5.1` model ID exposed by Z.AI's Anthropic-compat endpoint at deploy time — verified via a live probe during initial deploy; falls back to whatever GLM model ID Z.AI documents.

---

## Decision log (interview answers)

| # | Question | Answer |
|---|---|---|
| 1 | Scope | B — Full orchestrator (state machine, retries, audit, policy, gates) |
| 2 | Reuse vs greenfield | C — Replace existing `orchestrator/` |
| 3 | Language | A — Python |
| 4 | GLM integration | Use `claude` CLI with Z.AI Anthropic-compat endpoint |
| 5 | State storage | B — Postgres |
| 6 | Deploy + queue | A (Docker Compose) + pg-only queue (LISTEN/NOTIFY + SKIP LOCKED) |
| 7 | Policy + gates | C — YAML + DB snapshot, codeowner approval as human gate |
| 8 | Auth + observability | GitHub App / OAuth via App / JSON logs + Prometheus metrics |
| F1 | Issue intake | B — Keep interview phase, port into service |
| F2 | Models + cost cap | Sonnet 4.6 interviewer (with repo context), GLM-5.1 implementer, Opus 4.7 reviewer (with repo context), no kill switch |
| Topology | Service split | B — api + worker, pg as bus |
