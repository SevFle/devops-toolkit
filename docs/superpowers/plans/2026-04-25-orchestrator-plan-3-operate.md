# Orchestrator Plan 3 — Operate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the orchestrator operable in production: admin REST + minimal HTML UI gated by GitHub OAuth, Prometheus metrics, Caddy front, Strato VPS deployment via existing `deploy-production.yml`, daily pg backups, container hardening, full E2E + load + chaos test suites, and the migration phases that decommission the legacy OpenSpec workflows.

**Architecture:** Same image; new admin endpoints under `/admin/*` use a session cookie issued by GitHub OAuth (the App's OAuth flow). Prom counters/histograms emit on every transition / job / cli_run. Caddy in front terminates TLS, rate-limits webhook + auth, proxies to api. The CI workflow `deploy-orchestrator.yml` calls the repo's existing `deploy-production.yml` reusable workflow with the orchestrator compose file. A staging deploy lives on Strato under a separate App install for 7-day burn-in.

**Tech Stack:** FastAPI sessions + Jinja2 + htmx, prometheus-client, Caddy 2.x, Docker Compose, k6 (load), Bash chaos scripts, the repo's existing `deploy-production.yml`.

**Reference spec:** `docs/superpowers/specs/2026-04-25-glm-claude-orchestrator-design.md`
**Tracking issue:** `#35`
**Working branch:** `feat/orchestrator-operate` (off `main`, after Plan 2 PR merges)
**Depends on:** Plan 2 (#34) merged.

---

## Task 1: Add session + OAuth deps

- [ ] **Step 1: Branch**

```bash
git checkout main
git pull
git checkout -b feat/orchestrator-operate
```

- [ ] **Step 2: Add deps** to `orchestrator/pyproject.toml` `[project] dependencies`:

```toml
"itsdangerous>=2.2",
"jinja2>=3.1",
```

- [ ] **Step 3: Reinstall**

```bash
make install
```

- [ ] **Step 4: Commit**

```bash
git add orchestrator/pyproject.toml
git commit -m "chore(orchestrator): itsdangerous + jinja2 deps for admin UI"
```

---

## Task 2: GitHub OAuth login + session (TDD)

**Files:** `orchestrator/src/orchestrator/api/auth.py`, `orchestrator/tests/integration/test_admin_auth.py`

- [ ] **Step 1: Failing test**

```python
# orchestrator/tests/integration/test_admin_auth.py
from __future__ import annotations

import pytest
import pytest_asyncio
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from orchestrator.api.app import create_app
from orchestrator.db.base import Base


@pytest_asyncio.fixture
async def app(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setenv("SESSION_SIGNING_KEY", "a" * 44)
    monkeypatch.setenv("ADMIN_LOGINS", "alice")
    monkeypatch.setenv("GH_APP_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GH_APP_OAUTH_CLIENT_SECRET", "csec")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://test")
    return create_app()


@pytest.mark.asyncio
async def test_login_redirects_to_github(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cli:
        r = await cli.get("/admin/auth/login", follow_redirects=False)
    assert r.status_code == 302
    assert "github.com/login/oauth/authorize" in r.headers["location"]


@pytest.mark.asyncio
async def test_callback_admin_login_grants_session(app) -> None:
    with respx.mock(assert_all_called=False) as rx:
        rx.post("https://github.com/login/oauth/access_token").mock(
            return_value=Response(200, json={"access_token": "ghu"})
        )
        rx.get("https://api.github.com/user").mock(
            return_value=Response(200, json={"login": "alice"})
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cli:
            r = await cli.get("/admin/auth/login", follow_redirects=False)
            state = r.headers["location"].split("state=")[1].split("&")[0]
            r2 = await cli.get(f"/admin/auth/callback?code=abc&state={state}",
                               follow_redirects=False)
        assert r2.status_code == 302
        assert any("admin_session=" in c.split(";")[0] for c in r2.headers.get_list("set-cookie"))


@pytest.mark.asyncio
async def test_callback_non_admin_login_rejected(app) -> None:
    with respx.mock(assert_all_called=False) as rx:
        rx.post("https://github.com/login/oauth/access_token").mock(
            return_value=Response(200, json={"access_token": "ghu"})
        )
        rx.get("https://api.github.com/user").mock(
            return_value=Response(200, json={"login": "outsider"})
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cli:
            r = await cli.get("/admin/auth/login", follow_redirects=False)
            state = r.headers["location"].split("state=")[1].split("&")[0]
            r2 = await cli.get(f"/admin/auth/callback?code=abc&state={state}",
                               follow_redirects=False)
        assert r2.status_code == 403
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/api/auth.py`

```python
from __future__ import annotations

import secrets
from urllib.parse import urlencode

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer

from orchestrator.api.deps import settings_dep
from orchestrator.settings import Settings

router = APIRouter(prefix="/admin/auth")
log = structlog.get_logger()
COOKIE = "admin_session"


def _serializer(key: str) -> URLSafeSerializer:
    return URLSafeSerializer(key, salt="orchestrator-admin")


@router.get("/login")
async def login(request: Request, settings: Settings = Depends(settings_dep)) -> Response:
    if not settings.gh_app_oauth_client_id:
        raise HTTPException(status_code=501, detail="OAuth not configured")
    state = secrets.token_urlsafe(24)
    qs = urlencode({
        "client_id": settings.gh_app_oauth_client_id,
        "redirect_uri": f"{settings.public_base_url}/admin/auth/callback",
        "scope": "read:user",
        "state": state,
    })
    resp = RedirectResponse(f"https://github.com/login/oauth/authorize?{qs}", status_code=302)
    resp.set_cookie("oauth_state", state, httponly=True, secure=True,
                    samesite="lax", max_age=600)
    return resp


@router.get("/callback")
async def callback(
    request: Request,
    code: str,
    state: str,
    settings: Settings = Depends(settings_dep),
) -> Response:
    if request.cookies.get("oauth_state") != state:
        raise HTTPException(status_code=400, detail="state mismatch")

    async with httpx.AsyncClient(timeout=15) as cli:
        tok = await cli.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": settings.gh_app_oauth_client_id,
                "client_secret": settings.gh_app_oauth_client_secret,
                "code": code,
                "redirect_uri": f"{settings.public_base_url}/admin/auth/callback",
            },
            headers={"Accept": "application/json"},
        )
        tok.raise_for_status()
        access = tok.json().get("access_token")
        if not access:
            raise HTTPException(status_code=400, detail="no access token")
        user = await cli.get("https://api.github.com/user",
                             headers={"Authorization": f"Bearer {access}"})
        user.raise_for_status()
        login_name = user.json().get("login")

    if login_name not in settings.admin_logins:
        log.warning("admin_login_denied", login=login_name)
        raise HTTPException(status_code=403, detail="login not in admin allowlist")

    sig = _serializer(settings.session_signing_key).dumps({"login": login_name})
    resp = RedirectResponse("/admin/runs", status_code=302)
    resp.delete_cookie("oauth_state")
    resp.set_cookie(
        COOKIE, sig, httponly=True, secure=True, samesite="lax",
        max_age=7 * 24 * 3600,
    )
    return resp


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(COOKIE)
    return {"status": "logged_out"}


def current_user(request: Request, settings: Settings) -> str:
    cookie = request.cookies.get(COOKIE)
    if not cookie:
        raise HTTPException(status_code=401)
    try:
        data = _serializer(settings.session_signing_key).loads(cookie)
    except BadSignature as e:
        raise HTTPException(status_code=401) from e
    return str(data["login"])
```

- [ ] **Step 4: Wire router** in `orchestrator/src/orchestrator/api/app.py`

```python
from orchestrator.api.auth import router as auth_router
# inside create_app:
app.include_router(auth_router)
```

- [ ] **Step 5: Run, expect PASS**

- [ ] **Step 6: Commit**

```bash
git add orchestrator/src/orchestrator/api/auth.py orchestrator/src/orchestrator/api/app.py orchestrator/tests/integration/test_admin_auth.py
git commit -m "feat(orchestrator): admin GitHub OAuth login + signed session cookie"
```

---

## Task 3: `require_admin` dep + runs list (TDD)

**Files:** `orchestrator/src/orchestrator/api/admin/{__init__,deps,runs}.py`, `orchestrator/tests/integration/test_admin_runs.py`

- [ ] **Step 1: Failing test**

```python
# orchestrator/tests/integration/test_admin_runs.py
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from orchestrator.api.app import create_app
from orchestrator.db.base import Base
from orchestrator.db.models import Repo, Run, RunState


@pytest_asyncio.fixture
async def app(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setenv("SESSION_SIGNING_KEY", "a" * 44)
    monkeypatch.setenv("ADMIN_LOGINS", "alice")
    return create_app()


def _admin_cookie() -> dict[str, str]:
    from itsdangerous import URLSafeSerializer
    sig = URLSafeSerializer("a" * 44, salt="orchestrator-admin").dumps({"login": "alice"})
    return {"admin_session": sig}


@pytest.mark.asyncio
async def test_runs_list_requires_session(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cli:
        r = await cli.get("/admin/runs")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_runs_list_returns_rows(app, session: AsyncSession) -> None:
    session.add(repo := Repo(full_name="o/r", installation_id=42))
    await session.flush()
    session.add(Run(repo_id=repo.id, issue_number=1, state=RunState.NEW))
    await session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t",
                           cookies=_admin_cookie()) as cli:
        r = await cli.get("/admin/runs", headers={"Accept": "application/json"})
    assert r.status_code == 200
    rows = r.json()["items"]
    assert len(rows) == 1
    assert rows[0]["repo"] == "o/r"
    assert rows[0]["state"] == "NEW"
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement deps** at `orchestrator/src/orchestrator/api/admin/__init__.py` (empty) and `orchestrator/src/orchestrator/api/admin/deps.py`

```python
from __future__ import annotations

from fastapi import Depends, Request

from orchestrator.api.auth import current_user
from orchestrator.api.deps import settings_dep
from orchestrator.settings import Settings


def require_admin(request: Request, settings: Settings = Depends(settings_dep)) -> str:
    return current_user(request, settings)
```

- [ ] **Step 4: Implement runs router** at `orchestrator/src/orchestrator/api/admin/runs.py`

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.admin.deps import require_admin
from orchestrator.api.deps import session_dep
from orchestrator.db.models import Repo, Run

router = APIRouter(prefix="/admin")


@router.get("/runs")
async def list_runs(
    state: str | None = None,
    repo: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(session_dep),
    _user: str = Depends(require_admin),
) -> dict:
    stmt = select(Run, Repo).join(Repo, Run.repo_id == Repo.id).order_by(Run.id.desc()).limit(limit)
    if state:
        stmt = stmt.where(Run.state == state)
    if repo:
        stmt = stmt.where(Repo.full_name == repo)
    rows = (await session.execute(stmt)).all()
    items = [
        {
            "id": r.Run.id,
            "repo": r.Repo.full_name,
            "issue": r.Run.issue_number,
            "pr": r.Run.pr_number,
            "state": r.Run.state.value,
            "cycle_count": r.Run.cycle_count,
            "updated_at": r.Run.state_updated_at.isoformat(),
        }
        for r in rows
    ]
    return {"items": items}
```

- [ ] **Step 5: Wire** in `orchestrator/src/orchestrator/api/app.py`

```python
from orchestrator.api.admin.runs import router as admin_runs
app.include_router(admin_runs)
```

- [ ] **Step 6: Run, expect PASS**

- [ ] **Step 7: Commit**

```bash
git add orchestrator/src/orchestrator/api/admin/ orchestrator/src/orchestrator/api/app.py orchestrator/tests/integration/test_admin_runs.py
git commit -m "feat(orchestrator): /admin/runs list with session auth"
```

---

## Task 4: Run detail + abort + resume + override-gate

**Files:** extend `orchestrator/src/orchestrator/api/admin/runs.py`, create `orchestrator/tests/integration/test_admin_run_actions.py`

- [ ] **Step 1: Implement** — append to `runs.py`

```python
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import update

from orchestrator.db.models import Job, JobKind, JobStatus, PolicyDecision, RunEvent, RunState
from orchestrator.db.queue import enqueue


@router.get("/runs/{run_id}")
async def get_run(
    run_id: int,
    session: AsyncSession = Depends(session_dep),
    _user: str = Depends(require_admin),
) -> dict:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404)
    repo = await session.get(Repo, run.repo_id)
    events = (await session.execute(
        select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id.desc()).limit(50)
    )).scalars().all()
    jobs = (await session.execute(
        select(Job).where(Job.run_id == run_id).order_by(Job.id.desc())
    )).scalars().all()
    return {
        "run": {
            "id": run.id, "repo": repo.full_name, "issue": run.issue_number,
            "pr": run.pr_number, "state": run.state.value,
            "cycle_count": run.cycle_count,
        },
        "events": [
            {"ts": e.ts.isoformat(), "type": e.type, "data": e.data_jsonb} for e in events
        ],
        "jobs": [
            {"id": j.id, "kind": j.kind.value, "status": j.status.value,
             "attempts": j.attempts, "result": j.result_jsonb}
            for j in jobs
        ],
    }


@router.post("/runs/{run_id}/abort")
async def abort_run(
    run_id: int,
    session: AsyncSession = Depends(session_dep),
    user: str = Depends(require_admin),
) -> dict:
    run = await session.get(Run, run_id, with_for_update=True)
    if run is None:
        raise HTTPException(status_code=404)
    prev = run.state.value
    run.state = RunState.FAILED
    run.state_updated_at = datetime.now(timezone.utc)
    session.add(RunEvent(
        run_id=run_id, type="admin.abort",
        data_jsonb={"actor": user, "from": prev, "to": "FAILED"},
    ))
    await session.execute(
        update(Job).where(Job.run_id == run_id, Job.status == JobStatus.ready)
        .values(status=JobStatus.failed, result_jsonb={"reason": "aborted-by-admin"})
    )
    await session.commit()
    return {"status": "aborted"}


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: int,
    session: AsyncSession = Depends(session_dep),
    user: str = Depends(require_admin),
) -> dict:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404)
    kind_map = {
        RunState.NEW: "interview_turn", RunState.INTERVIEWING: "interview_turn",
        RunState.READY: "implement", RunState.PR_OPEN: "review",
        RunState.UNDER_REVIEW: "review", RunState.REMEDIATING: "remediate",
        RunState.APPROVED: "merge", RunState.MERGEABLE: "merge",
    }
    kind_str = kind_map.get(run.state)
    if kind_str is None:
        raise HTTPException(status_code=409, detail=f"cannot resume from {run.state.value}")
    await enqueue(session, run_id=run.id, kind=JobKind(kind_str))
    session.add(RunEvent(run_id=run_id, type="admin.resume",
                         data_jsonb={"actor": user, "kind": kind_str}))
    await session.commit()
    return {"status": "enqueued", "kind": kind_str}


@router.post("/runs/{run_id}/override-gate")
async def override_gate(
    run_id: int,
    session: AsyncSession = Depends(session_dep),
    user: str = Depends(require_admin),
) -> dict:
    run = await session.get(Run, run_id, with_for_update=True)
    if run is None:
        raise HTTPException(status_code=404)
    if run.state != RunState.HUMAN_REQUIRED:
        raise HTTPException(status_code=409, detail=f"not in HUMAN_REQUIRED: {run.state.value}")
    session.add(PolicyDecision(
        run_id=run_id, gate="human_required",
        verdict="override", reason="admin override", actor=user,
    ))
    run.state = RunState.APPROVED
    run.state_updated_at = datetime.now(timezone.utc)
    session.add(RunEvent(
        run_id=run_id, type="admin.override_gate",
        data_jsonb={"actor": user, "from": "HUMAN_REQUIRED", "to": "APPROVED"},
    ))
    await session.commit()
    return {"status": "overridden"}
```

- [ ] **Step 2: Test** at `orchestrator/tests/integration/test_admin_run_actions.py` covering:

- abort flips state to `FAILED` and writes `RunEvent(type='admin.abort')`
- resume from `READY` enqueues `implement` job
- override-gate from `HUMAN_REQUIRED` writes `PolicyDecision` and flips to `APPROVED`
- override-gate from non-`HUMAN_REQUIRED` returns 409

(Use the same `_admin_cookie()` helper as Task 3.)

- [ ] **Step 3: Run, expect PASS**

- [ ] **Step 4: Commit**

```bash
git add orchestrator/src/orchestrator/api/admin/runs.py orchestrator/tests/integration/test_admin_run_actions.py
git commit -m "feat(orchestrator): /admin/runs/{id}/(abort|resume|override-gate)"
```

---

## Task 5: Repos + jobs admin endpoints

**Files:** `orchestrator/src/orchestrator/api/admin/{repos,jobs}.py`

- [ ] **Step 1: repos.py**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.admin.deps import require_admin
from orchestrator.api.deps import session_dep
from orchestrator.db.models import Repo
from orchestrator.github.app import mint_app_jwt
from orchestrator.github.tokens import get_installation_token
from orchestrator.policy.loader import load_and_snapshot
from orchestrator.settings import get_settings

router = APIRouter(prefix="/admin")


@router.get("/repos")
async def list_repos(
    session: AsyncSession = Depends(session_dep),
    _user: str = Depends(require_admin),
) -> dict:
    rows = (await session.execute(select(Repo).order_by(Repo.full_name))).scalars().all()
    return {
        "items": [
            {"id": r.id, "full_name": r.full_name,
             "installation_id": r.installation_id,
             "policy_yaml_sha": r.policy_yaml_sha,
             "policy_loaded": r.policy_snapshot_jsonb is not None}
            for r in rows
        ]
    }


@router.post("/repos/{repo_id}/refresh-policy")
async def refresh_policy(
    repo_id: int,
    session: AsyncSession = Depends(session_dep),
    _user: str = Depends(require_admin),
) -> dict:
    repo = await session.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(status_code=404)
    s = get_settings()
    token = await get_installation_token(
        session, installation_id=repo.installation_id,
        mint_app_jwt=lambda: mint_app_jwt(
            app_id=s.gh_app_id, private_key_path=s.gh_app_private_key_path,
        ),
    )
    p = await load_and_snapshot(session, repo_row=repo, token=token)
    return {"loaded": p is not None, "sha": repo.policy_yaml_sha}
```

- [ ] **Step 2: jobs.py**

```python
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.admin.deps import require_admin
from orchestrator.api.deps import session_dep
from orchestrator.db.models import Job, JobStatus

router = APIRouter(prefix="/admin")


@router.get("/jobs/queue")
async def queue_state(
    session: AsyncSession = Depends(session_dep),
    _user: str = Depends(require_admin),
) -> dict:
    by_status = (await session.execute(
        select(Job.status, func.count(Job.id)).group_by(Job.status)
    )).all()
    leases = (await session.execute(
        select(Job.id, Job.kind, Job.locked_by, Job.locked_until)
        .where(Job.status == JobStatus.running).order_by(Job.id.desc()).limit(50)
    )).all()
    now = datetime.now(timezone.utc)
    return {
        "depth": {s.value: int(c) for s, c in by_status},
        "active_leases": [
            {"job_id": j.id, "kind": j.kind.value, "worker": j.locked_by,
             "expires_in_s": int((j.locked_until - now).total_seconds()) if j.locked_until else None}
            for j in leases
        ],
    }
```

- [ ] **Step 3: Wire** in `orchestrator/src/orchestrator/api/app.py`

```python
from orchestrator.api.admin.repos import router as admin_repos
from orchestrator.api.admin.jobs import router as admin_jobs
app.include_router(admin_repos)
app.include_router(admin_jobs)
```

- [ ] **Step 4: Smoke**

```bash
curl -sb cookies.txt http://localhost:8000/admin/repos
curl -sb cookies.txt http://localhost:8000/admin/jobs/queue
```

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/api/admin/repos.py orchestrator/src/orchestrator/api/admin/jobs.py orchestrator/src/orchestrator/api/app.py
git commit -m "feat(orchestrator): /admin/repos + /admin/jobs/queue"
```

---

## Task 6: Jinja templates + htmx

**Files:** `orchestrator/src/orchestrator/api/templates/{base,runs_list,run_detail,repos_list}.html`, `orchestrator/src/orchestrator/api/admin/_render.py`, modify routers to render HTML when `Accept: text/html`.

- [ ] **Step 1: base.html**

```html
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{% block title %}orchestrator{% endblock %}</title>
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<style>
body { font: 14px/1.4 system-ui, sans-serif; max-width: 1100px; margin: 1.5rem auto; padding: 0 1rem; }
nav a { margin-right: 1rem; }
table { border-collapse: collapse; width: 100%; }
td, th { border-bottom: 1px solid #eee; padding: .4rem .6rem; text-align: left; }
.state { font-family: ui-monospace, monospace; }
.state.UNDER_REVIEW { color: #b06000; }
.state.HUMAN_REQUIRED { color: #b00020; font-weight: 600; }
.state.MERGED { color: #008060; }
form.inline { display: inline; }
button { font: inherit; padding: .2rem .5rem; }
</style>
</head>
<body>
<nav><a href="/admin/runs">Runs</a><a href="/admin/repos">Repos</a><a href="/admin/jobs/queue">Queue</a></nav>
{% block content %}{% endblock %}
</body>
</html>
```

- [ ] **Step 2: runs_list.html**

```html
{% extends "base.html" %}
{% block title %}Runs{% endblock %}
{% block content %}
<h1>Runs</h1>
<form method="get" hx-get="/admin/runs" hx-target="#tbl" hx-swap="outerHTML">
  <label>State <select name="state"><option value="">any</option>
    {% for s in ["NEW","INTERVIEWING","READY","IMPLEMENTING","PR_OPEN","UNDER_REVIEW","REMEDIATING","APPROVED","MERGEABLE","MERGED","FAILED","HUMAN_REQUIRED"] %}
    <option {% if s == state %}selected{% endif %}>{{ s }}</option>
    {% endfor %}
  </select></label>
  <button>Filter</button>
</form>
<table id="tbl">
  <tr><th>id</th><th>repo</th><th>issue</th><th>pr</th><th>state</th><th>cycles</th><th>updated</th></tr>
  {% for r in items %}
  <tr>
    <td><a href="/admin/runs/{{ r.id }}">{{ r.id }}</a></td>
    <td>{{ r.repo }}</td><td>#{{ r.issue }}</td><td>{{ r.pr or "" }}</td>
    <td class="state {{ r.state }}">{{ r.state }}</td>
    <td>{{ r.cycle_count }}</td>
    <td>{{ r.updated_at }}</td>
  </tr>
  {% endfor %}
</table>
{% endblock %}
```

- [ ] **Step 3: run_detail.html**

```html
{% extends "base.html" %}
{% block title %}Run {{ run.id }}{% endblock %}
{% block content %}
<h1>Run {{ run.id }} — {{ run.repo }} #{{ run.issue }}</h1>
<p>State: <span class="state {{ run.state }}">{{ run.state }}</span> ·
   PR: {{ run.pr or "—" }} · cycles: {{ run.cycle_count }}</p>

<form class="inline" hx-post="/admin/runs/{{ run.id }}/abort" hx-confirm="Abort run?">
  <button>Abort</button>
</form>
<form class="inline" hx-post="/admin/runs/{{ run.id }}/resume" hx-confirm="Resume run?">
  <button>Resume</button>
</form>
{% if run.state == "HUMAN_REQUIRED" %}
<form class="inline" hx-post="/admin/runs/{{ run.id }}/override-gate"
      hx-confirm="Override HUMAN_REQUIRED gate?">
  <button>Override gate</button>
</form>
{% endif %}

<h2>Jobs</h2>
<table>
  <tr><th>id</th><th>kind</th><th>status</th><th>attempts</th></tr>
  {% for j in jobs %}
  <tr><td>{{ j.id }}</td><td>{{ j.kind }}</td><td>{{ j.status }}</td><td>{{ j.attempts }}</td></tr>
  {% endfor %}
</table>

<h2>Events</h2>
<table>
  <tr><th>ts</th><th>type</th><th>data</th></tr>
  {% for e in events %}
  <tr><td>{{ e.ts }}</td><td>{{ e.type }}</td><td><code>{{ e.data | tojson }}</code></td></tr>
  {% endfor %}
</table>
{% endblock %}
```

- [ ] **Step 4: repos_list.html** — same shape as runs_list, table of repos.

```html
{% extends "base.html" %}
{% block title %}Repos{% endblock %}
{% block content %}
<h1>Repos</h1>
<table>
  <tr><th>id</th><th>full_name</th><th>install</th><th>policy</th><th>actions</th></tr>
  {% for r in items %}
  <tr>
    <td>{{ r.id }}</td><td>{{ r.full_name }}</td><td>{{ r.installation_id }}</td>
    <td>{{ "yes" if r.policy_loaded else "no" }} ({{ r.policy_yaml_sha or "-" }})</td>
    <td>
      <form class="inline" hx-post="/admin/repos/{{ r.id }}/refresh-policy">
        <button>Refresh</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>
{% endblock %}
```

- [ ] **Step 5: `_render.py`**

```python
# orchestrator/src/orchestrator/api/admin/_render.py
from __future__ import annotations

from importlib import resources
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape


def _env() -> Environment:
    tpl_dir = resources.files("orchestrator.api.templates")
    return Environment(
        loader=FileSystemLoader(str(tpl_dir)),
        autoescape=select_autoescape(["html"]),
    )


def render(request: Request, template: str, ctx: dict[str, Any]) -> JSONResponse | HTMLResponse:
    if "text/html" in (request.headers.get("accept") or ""):
        body = _env().get_template(template).render(**ctx)
        return HTMLResponse(body)
    return JSONResponse(ctx)
```

- [ ] **Step 6: Update routers to accept `Request` and call `render(...)`** in `runs.py`, `repos.py`. Tests passing JSON via `Accept: application/json` continue to work.

- [ ] **Step 7: Smoke**

```bash
curl -s -H "Accept: text/html" -b admin_session=<sig> http://localhost:8000/admin/runs > /tmp/runs.html
open /tmp/runs.html
```

- [ ] **Step 8: Commit**

```bash
git add orchestrator/src/orchestrator/api/templates/ orchestrator/src/orchestrator/api/admin/
git commit -m "feat(orchestrator): server-rendered Jinja + htmx admin UI"
```

---

## Task 7: Prometheus metrics

**Files:** `orchestrator/src/orchestrator/obs/metrics.py`, `orchestrator/src/orchestrator/api/admin/metrics.py`, `orchestrator/tests/unit/test_metrics.py`

- [ ] **Step 1: Failing test**

```python
# orchestrator/tests/unit/test_metrics.py
from __future__ import annotations

from prometheus_client import REGISTRY

from orchestrator.obs import metrics as M


def test_counters_registered() -> None:
    M.WEBHOOKS_TOTAL.labels(event="issues", status="ok").inc()
    v = REGISTRY.get_sample_value("orch_webhooks_total", {"event": "issues", "status": "ok"})
    assert v == 1.0


def test_state_transition_observed() -> None:
    M.RUNS_TRANSITIONS.labels(state_transition="NEW->INTERVIEWING").inc()
    v = REGISTRY.get_sample_value(
        "orch_runs_total", {"state_transition": "NEW->INTERVIEWING"},
    )
    assert v == 1.0
```

- [ ] **Step 2: Implement** at `orchestrator/src/orchestrator/obs/metrics.py`

```python
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

WEBHOOKS_TOTAL = Counter(
    "orch_webhooks_total", "Webhooks received", labelnames=["event", "status"],
)
RUNS_TRANSITIONS = Counter(
    "orch_runs_total", "State transitions", labelnames=["state_transition"],
)
JOBS_TOTAL = Counter(
    "orch_jobs_total", "Jobs by kind+outcome", labelnames=["kind", "outcome"],
)
CLI_RUNS_TOTAL = Counter(
    "orch_cli_runs_total", "CLI runs", labelnames=["profile", "exit"],
)
POLICY_DECISIONS = Counter(
    "orch_policy_decisions_total", "Policy gate decisions",
    labelnames=["gate", "verdict"],
)
HUMAN_REQUIRED_TOTAL = Counter(
    "orch_human_required_total", "Runs that hit HUMAN_REQUIRED", labelnames=["reason"],
)

JOB_DURATION = Histogram(
    "orch_job_duration_seconds", "Job runtime",
    labelnames=["kind"], buckets=(1, 5, 15, 60, 300, 900, 1800, 3600),
)
CLI_DURATION = Histogram(
    "orch_cli_duration_seconds", "CLI subprocess runtime",
    labelnames=["profile"], buckets=(5, 30, 120, 300, 900, 1800),
)

QUEUE_DEPTH = Gauge(
    "orch_queue_depth", "Ready jobs by kind", labelnames=["kind"],
)
ACTIVE_WORKERS = Gauge("orch_active_workers", "Running jobs (proxy for workers)")
OLDEST_PENDING_AGE = Gauge(
    "orch_oldest_pending_age_seconds", "Age of oldest ready job",
)
WORKDIR_FREE_BYTES = Gauge(
    "orch_workdir_free_bytes", "Free space on /var/lib/orchestrator/work",
)
```

- [ ] **Step 3: Wire emission points** in:

- `state/transitions.py` — `RUNS_TRANSITIONS.labels(state_transition=f"{prev}->{new.value}").inc()` after commit
- `api/webhooks.py` — `WEBHOOKS_TOTAL.labels(event=event, status="ok").inc()` after persist
- `worker/loop.py` — increment `JOBS_TOTAL.labels(kind, outcome)` and observe `JOB_DURATION` per job
- `cli/runner.py` — increment `CLI_RUNS_TOTAL.labels(profile=profile.name, exit=str(exit_code))` and observe `CLI_DURATION`

- [ ] **Step 4: Implement `/admin/metrics`** at `orchestrator/src/orchestrator/api/admin/metrics.py`

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from orchestrator.api.deps import settings_dep
from orchestrator.settings import Settings

router = APIRouter(prefix="/admin")


@router.get("/metrics")
async def metrics(
    request: Request, settings: Settings = Depends(settings_dep),
) -> Response:
    expected = settings.prometheus_bearer
    if not expected:
        raise HTTPException(status_code=501, detail="prometheus bearer not configured")
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") != expected:
        raise HTTPException(status_code=401)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

Wire in `orchestrator/src/orchestrator/api/app.py`:

```python
from orchestrator.api.admin.metrics import router as admin_metrics
app.include_router(admin_metrics)
```

- [ ] **Step 5: Run tests, expect PASS**

- [ ] **Step 6: Commit**

```bash
git add orchestrator/src/orchestrator/obs/metrics.py \
        orchestrator/src/orchestrator/api/admin/metrics.py \
        orchestrator/src/orchestrator/api/app.py \
        orchestrator/src/orchestrator/state/transitions.py \
        orchestrator/src/orchestrator/api/webhooks.py \
        orchestrator/src/orchestrator/cli/runner.py \
        orchestrator/src/orchestrator/worker/loop.py \
        orchestrator/tests/unit/test_metrics.py
git commit -m "feat(orchestrator): prometheus metrics + bearer-protected /admin/metrics"
```

---

## Task 8: Caddyfile + production compose

**Files:** `deploy/compose/Caddyfile`, modify `deploy/compose/docker-compose.yml`

- [ ] **Step 1: Caddyfile**

```caddyfile
{
  email ops@example.com
}

orchestrator.example.com {
  encode zstd gzip

  header {
    Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
    X-Content-Type-Options "nosniff"
    X-Frame-Options "DENY"
    Referrer-Policy "strict-origin-when-cross-origin"
    Permissions-Policy "camera=(), microphone=(), geolocation=()"
  }

  @webhook path /gh/webhook
  rate_limit @webhook 50r/s

  @auth path /admin/auth/*
  rate_limit @auth 10r/m {
    by_ip
  }

  reverse_proxy api:8000
}
```

- [ ] **Step 2: Compose** — add Caddy + worker replicas

```yaml
# append to services:
  caddy:
    image: caddy:2-alpine
    depends_on:
      api:
        condition: service_healthy
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    restart: unless-stopped

  worker:
    image: orchestrator:dev
    depends_on:
      migrator:
        condition: service_completed_successfully
    env_file: .env
    environment:
      ROLE: worker
    volumes:
      - ./gh-app.pem:/secrets/gh-app.pem:ro
      - workdir:/var/lib/orchestrator/work
    deploy:
      replicas: 2
    restart: unless-stopped
```

Add to `volumes:`: `caddy-data:`, `caddy-config:`.

- [ ] **Step 3: Smoke locally** (caddy will fail TLS but service starts)

```bash
docker compose -f deploy/compose/docker-compose.yml up -d
docker compose -f deploy/compose/docker-compose.yml logs caddy | tail
```

- [ ] **Step 4: Commit**

```bash
git add deploy/compose/Caddyfile deploy/compose/docker-compose.yml
git commit -m "feat(orchestrator): Caddy TLS front + worker replicas in compose"
```

---

## Task 9: pg_dump backup sidecar

**Files:** `deploy/compose/pgbackup.sh`, modify `deploy/compose/docker-compose.yml`

- [ ] **Step 1: pgbackup.sh**

```bash
#!/usr/bin/env sh
set -eu
DEST="${BACKUP_DEST:-/backups}"
mkdir -p "$DEST"
ts=$(date -u +%Y%m%dT%H%M%SZ)
out="$DEST/orch-$ts.sql.gz"
pg_dump --no-owner --no-acl -d "$DATABASE_URL_PG" | gzip > "$out"
echo "wrote $out"
ls -1t "$DEST"/orch-*.sql.gz | tail -n +31 | xargs -r rm -f
if [ -n "${OFFSITE_S3_BUCKET:-}" ]; then
  aws s3 cp "$out" "s3://${OFFSITE_S3_BUCKET}/orchestrator/$(basename "$out")"
fi
```

`chmod +x deploy/compose/pgbackup.sh`.

- [ ] **Step 2: Compose** — add sidecar

```yaml
  pgbackup:
    image: postgres:16-alpine
    depends_on:
      postgres:
        condition: service_healthy
    volumes:
      - ./pgbackup.sh:/pgbackup.sh:ro
      - pgbackups:/backups
    environment:
      DATABASE_URL_PG: "postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}"
      OFFSITE_S3_BUCKET: "${OFFSITE_S3_BUCKET:-}"
    entrypoint: ["/bin/sh", "-c", "while :; do /pgbackup.sh; sleep 86400; done"]
    restart: unless-stopped
```

Add `pgbackups:` to `volumes:`.

- [ ] **Step 3: Test once**

```bash
docker compose -f deploy/compose/docker-compose.yml run --rm pgbackup /pgbackup.sh
docker compose -f deploy/compose/docker-compose.yml exec pgbackup ls /backups
```

- [ ] **Step 4: Commit**

```bash
git add deploy/compose/pgbackup.sh deploy/compose/docker-compose.yml
git commit -m "feat(orchestrator): daily pg_dump sidecar with 30-day retention"
```

---

## Task 10: Container hardening

**Files:** modify `deploy/compose/docker-compose.yml`

- [ ] **Step 1: Apply to `api` and `worker`**

```yaml
    cap_drop: ["ALL"]
    read_only: true
    tmpfs:
      - /tmp:size=64m
    security_opt:
      - "no-new-privileges:true"
```

For `worker`, the `workdir:/var/lib/orchestrator/work` bind already provides the only writable path; ensure the Dockerfile creates `/var/lib/orchestrator/home` (Plan 1 already does).

- [ ] **Step 2: Bring stack up + smoke**

```bash
docker compose -f deploy/compose/docker-compose.yml up -d
sleep 10
curl -s http://localhost:8000/admin/healthz
```

- [ ] **Step 3: Commit**

```bash
git add deploy/compose/docker-compose.yml
git commit -m "feat(orchestrator): cap-drop=ALL + read-only rootfs + no-new-privileges"
```

---

## Task 11: Strato deploy scripts + workflow

**Files:** `deploy/strato/{deploy,pre,post,healthcheck}.sh`, `.github/workflows/deploy-orchestrator.yml`

- [ ] **Step 1: deploy.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
SERVICE="${SERVICE:-orchestrator}"
DIR="/srv/${SERVICE}"
COMPOSE="$DIR/docker-compose.yml"
mkdir -p "$DIR"
docker compose -f "$COMPOSE" pull
docker compose -f "$COMPOSE" up -d --remove-orphans
```

- [ ] **Step 2: pre.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
DIR="/srv/${SERVICE:-orchestrator}"
docker compose -f "$DIR/docker-compose.yml" run --rm migrator
```

- [ ] **Step 3: post.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
URL="${1:?healthz URL required}"
for i in $(seq 1 30); do
  if curl -fsS "$URL" >/dev/null; then
    echo "healthz ok"
    exit 0
  fi
  sleep 5
done
echo "healthz never reached"
exit 1
```

- [ ] **Step 4: healthcheck.sh**

```bash
#!/usr/bin/env bash
exec curl -fsS "${1:-http://localhost:8000/admin/healthz}"
```

- [ ] **Step 5: `chmod +x deploy/strato/*.sh`**

- [ ] **Step 6: Workflow** at `.github/workflows/deploy-orchestrator.yml`

```yaml
name: deploy-orchestrator
on:
  push:
    branches: [main]
    paths:
      - 'orchestrator/**'
      - 'deploy/**'
      - '.github/workflows/deploy-orchestrator.yml'
  workflow_dispatch:
    inputs:
      env:
        description: "staging | production"
        default: "staging"

jobs:
  deploy:
    uses: ./.github/workflows/deploy-production.yml
    with:
      service: orchestrator
      compose_file: deploy/compose/docker-compose.yml
      ssh_host: ${{ vars.STRATO_HOST }}
      health_url: https://orchestrator.${{ vars.PUBLIC_DOMAIN }}/admin/healthz
      pre_hook: deploy/strato/pre.sh
      post_hook: deploy/strato/post.sh
    secrets:
      ssh_private_key: ${{ secrets.STRATO_SSH_KEY }}
      env_file: ${{ secrets.ORCHESTRATOR_ENV }}
```

If the existing `deploy-production.yml` has a different signature, adapt to its actual inputs — verify with `cat .github/workflows/deploy-production.yml` before running.

- [ ] **Step 7: Commit**

```bash
git add deploy/strato/ .github/workflows/deploy-orchestrator.yml
git commit -m "feat(orchestrator): Strato VPS deploy scripts + caller workflow"
```

---

## Task 12: Staging deploy + 7-day burn-in

- [ ] **Step 1: Configure staging GH App** — separate App `orchestrator-staging`, OAuth callback `https://orchestrator-staging.<domain>/admin/auth/callback`, webhook URL `https://orchestrator-staging.<domain>/gh/webhook`. Install only on `SevFle/orchestrator-e2e-fixture`.

- [ ] **Step 2: Configure GH repo Actions vars + secrets**

```bash
gh variable set STRATO_HOST -b "<strato-host>" -R SevFle/devops-toolkit
gh variable set PUBLIC_DOMAIN -b "<your-domain>" -R SevFle/devops-toolkit
gh secret set STRATO_SSH_KEY < ~/.ssh/strato.key -R SevFle/devops-toolkit
gh secret set ORCHESTRATOR_ENV < deploy/compose/.env.staging -R SevFle/devops-toolkit
```

- [ ] **Step 3: Trigger staging deploy**

```bash
gh workflow run deploy-orchestrator.yml -f env=staging -R SevFle/devops-toolkit
```

- [ ] **Step 4: Smoke**

```bash
curl -s https://orchestrator-staging.<domain>/admin/healthz
```

- [ ] **Step 5: Run E2E suite (Tasks 13–14) against staging install for 7 days; revisit before promoting to production.**

- [ ] **Step 6: Document burn-in dates and observations** in `orchestrator/README.md`. Commit.

---

## Task 13: E2E suite

**Files:** `orchestrator/tests/e2e/{conftest,test_golden_path,test_remediation_cycle,test_risk_glob_human_required,test_abort_resume}.py`

- [ ] **Step 1: e2e conftest**

```python
# orchestrator/tests/e2e/conftest.py
from __future__ import annotations

import os
from collections.abc import Generator

import httpx
import pytest


@pytest.fixture(scope="session")
def base_url() -> str:
    url = os.environ.get("E2E_BASE_URL")
    if not url:
        pytest.skip("E2E_BASE_URL not set")
    return url


@pytest.fixture(scope="session")
def gh_token() -> str:
    t = os.environ.get("E2E_GH_TOKEN")
    if not t:
        pytest.skip("E2E_GH_TOKEN not set")
    return t


@pytest.fixture
def gh(gh_token: str) -> Generator[httpx.Client, None, None]:
    with httpx.Client(
        base_url="https://api.github.com",
        headers={"Authorization": f"Bearer {gh_token}"},
        timeout=30,
    ) as cli:
        yield cli
```

- [ ] **Step 2: Golden-path** at `orchestrator/tests/e2e/test_golden_path.py`

```python
from __future__ import annotations

import time

import httpx
import pytest

REPO = "SevFle/orchestrator-e2e-fixture"


def _wait_label(gh: httpx.Client, issue: int, expected: list[str], timeout_s: int = 600) -> str:
    end = time.time() + timeout_s
    while time.time() < end:
        r = gh.get(f"/repos/{REPO}/issues/{issue}")
        r.raise_for_status()
        labels = {l["name"] for l in r.json()["labels"]}
        if any(s in labels for s in expected):
            return next(s for s in expected if s in labels)
        time.sleep(5)
    pytest.fail(f"timed out waiting for issue {issue} to reach {expected}")


def test_golden_path(gh: httpx.Client) -> None:
    body = "Goal: also export subtract(a,b)->int. Tests in tests/test_subtract.py."
    r = gh.post(f"/repos/{REPO}/issues",
                json={"title": "E2E golden", "labels": ["ai-triage"], "body": body})
    r.raise_for_status()
    issue = r.json()["number"]

    _wait_label(gh, issue, ["ai-implement"], timeout_s=300)

    deadline = time.time() + 900
    pr = None
    while time.time() < deadline:
        prs = gh.get(f"/repos/{REPO}/pulls", params={"state": "open"}).json()
        for p in prs:
            if "Run " in (p.get("body") or "") and p["title"].startswith("feat:"):
                pr = p
                break
        if pr:
            break
        time.sleep(5)
    assert pr is not None

    gh.post(f"/repos/{REPO}/pulls/{pr['number']}/reviews",
            json={"event": "APPROVE", "body": "lgtm"}).raise_for_status()

    deadline = time.time() + 300
    while time.time() < deadline:
        s = gh.get(f"/repos/{REPO}/pulls/{pr['number']}").json()
        if s.get("merged"):
            return
        time.sleep(5)
    pytest.fail("PR never merged")
```

- [ ] **Step 3: Remediation cycle** at `orchestrator/tests/e2e/test_remediation_cycle.py` — open issue with body that intentionally requires multiple turns; assert at least one PR review comment is posted (verdict `request_changes` or `blocking`) before the eventual approve.

- [ ] **Step 4: Risk-glob HUMAN_REQUIRED** at `orchestrator/tests/e2e/test_risk_glob_human_required.py` — open issue whose acceptance forces touching `migrations/`. Assert PR opens, then issue receives `Human approval required: risk_path:` comment.

- [ ] **Step 5: Abort/resume** at `orchestrator/tests/e2e/test_abort_resume.py` — use admin API: open issue, find run via `/admin/runs`, `POST .../abort`, assert `state=FAILED`; then `POST .../resume` from a non-terminal state and assert a job is enqueued.

- [ ] **Step 6: Run E2E**

```bash
E2E_BASE_URL=https://orchestrator-staging.<domain> \
E2E_GH_TOKEN=$(gh auth token) \
pytest orchestrator/tests/e2e -v
```

- [ ] **Step 7: Commit**

```bash
git add orchestrator/tests/e2e/
git commit -m "test(orchestrator): E2E suite (golden, remediation, risk-glob, abort/resume)"
```

---

## Task 14: Load test (k6)

**Files:** `load/webhook_spike.k6.js`

- [ ] **Step 1: Script**

```js
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '30s', target: 10 },
    { duration: '1m',  target: 50 },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    http_req_failed:   ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
  },
};

const URL = __ENV.URL || 'http://localhost:8000/gh/webhook';

export default function () {
  const body = JSON.stringify({ zen: 'load' });
  const res = http.post(URL, body, {
    headers: {
      'Content-Type': 'application/json',
      'X-GitHub-Event': 'ping',
      'X-GitHub-Delivery': `k6-${__VU}-${__ITER}`,
      'X-Hub-Signature-256': 'sha256=deadbeef',
    },
  });
  check(res, { '401': r => r.status === 401 });
  sleep(0.05);
}
```

(Signature is intentionally invalid — 401 is the expected status. Test measures intake throughput, not full pipeline.)

- [ ] **Step 2: Run**

```bash
URL=https://orchestrator-staging.<domain>/gh/webhook k6 run load/webhook_spike.k6.js
```

Expect `http_req_failed < 1%`, `p95 < 500ms`.

- [ ] **Step 3: Document expected results** in `orchestrator/README.md` "Load smoke" section. Commit.

---

## Task 15: Chaos scripts

**Files:** `chaos/{kill_worker,pg_restart,rotate_secrets}.sh`

- [ ] **Step 1: kill_worker.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
echo "killing worker container..."
docker compose -f deploy/compose/docker-compose.yml kill worker
sleep 5
docker compose -f deploy/compose/docker-compose.yml up -d worker
echo "expected: reaper recovers any leased job within 30s grace"
```

- [ ] **Step 2: pg_restart.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
docker compose -f deploy/compose/docker-compose.yml restart postgres
sleep 5
curl -fsS http://localhost:8000/admin/healthz
echo "expected: api reconnects via pool_pre_ping; healthz returns db:ok"
```

- [ ] **Step 3: rotate_secrets.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail
echo "rotating ANTHROPIC_API_KEY in .env..."
sed -i.bak 's/^ANTHROPIC_API_KEY=.*/ANTHROPIC_API_KEY=sk-ant-ROTATED/' deploy/compose/.env
docker compose -f deploy/compose/docker-compose.yml up -d --force-recreate api worker
sleep 10
curl -fsS http://localhost:8000/admin/healthz
echo "expected: api/worker pick up new env without DB downtime"
```

- [ ] **Step 4: Run each, capture before/after metric snapshots, document.**

```bash
chmod +x chaos/*.sh
bash chaos/kill_worker.sh
```

- [ ] **Step 5: Commit**

```bash
git add chaos/
git commit -m "test(orchestrator): chaos scripts (kill-worker, pg-restart, rotate-secrets)"
```

---

## Task 16: Mark legacy workflows deprecated (no-op by default)

**Files:** `.github/workflows/openspec-{interview,propose,orchestrate}.yml`

- [ ] **Step 1: Prepend deprecation banner + gate jobs behind `legacy_compatibility: false` opt-in**

For each of `openspec-interview.yml`, `openspec-propose.yml`, `openspec-orchestrate.yml`:

```yaml
# DEPRECATED: superseded by the orchestrator service.
# See docs/superpowers/specs/2026-04-25-glm-claude-orchestrator-design.md.
# Will be deleted after 30 days of stable production. Tracks #34, #35.

on:
  workflow_call:
    inputs:
      legacy_compatibility:
        type: boolean
        default: false
      # …existing inputs preserved…

jobs:
  noop:
    if: ${{ ! inputs.legacy_compatibility }}
    runs-on: ubuntu-latest
    steps:
      - run: |
          echo "Workflow deprecated; orchestrator service now drives this flow."
          echo "Set legacy_compatibility=true to opt in to the legacy path."
  legacy:
    if: ${{ inputs.legacy_compatibility }}
    # …existing job config preserved unchanged…
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/openspec-*.yml
git commit -m "chore(orchestrator): mark openspec-* workflows deprecated, no-op by default"
```

---

## Task 17: Production rollout (post-merge ops, separate run)

- [ ] **Step 1: Verify 7-day staging burn-in** completed clean (no unexplained `FAILED` runs, expected `HUMAN_REQUIRED` triggered exactly when intended, all chaos scenarios recovered).

- [ ] **Step 2: Configure production GH App** — install on **one low-risk repo** to start.

- [ ] **Step 3: Trigger production deploy**

```bash
gh workflow run deploy-orchestrator.yml -f env=production -R SevFle/devops-toolkit
```

- [ ] **Step 4: Smoke**

```bash
curl -s https://orchestrator.<domain>/admin/healthz
```

- [ ] **Step 5: Open one canary issue** in the production-installed repo with `ai-triage` label. Watch the loop. Approve manually as codeowner.

- [ ] **Step 6: Document** rollout date, repos onboarded, any policy adjustments in `orchestrator/README.md`. Commit.

---

## Task 18: Decommission legacy after 30 days stable (post-merge ops)

(Run only after production has been stable for 30 days.)

- [ ] **Step 1: Delete `_legacy/`**

```bash
git checkout -b chore/orchestrator-decommission-legacy
git rm -r orchestrator/_legacy
```

- [ ] **Step 2: Delete deprecated workflows**

```bash
git rm .github/workflows/openspec-interview.yml \
       .github/workflows/openspec-propose.yml \
       .github/workflows/openspec-orchestrate.yml
```

- [ ] **Step 3: Commit + PR**

```bash
git commit -m "chore(orchestrator): delete legacy openspec workflows + _legacy/

Service stable in production for 30 days. The webhook-driven orchestrator
fully replaces openspec-{interview,propose,orchestrate}.yml. Refs #33, #34, #35."
git push -u origin chore/orchestrator-decommission-legacy
gh pr create --fill
```

---

## Task 19: Open Plan 3 PR

(Tasks 1–16 above are merged via this PR. Tasks 17–18 are post-merge ops in separate PRs.)

- [ ] **Step 1: Push**

```bash
git push -u origin feat/orchestrator-operate
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "feat(orchestrator): Plan 3 — Operate" --body "$(cat <<'EOF'
Implements Plan 3 of the orchestrator rebuild.

Closes #35 once merged.

## Summary

- GitHub OAuth login, signed session cookies, allowlisted admin logins
- /admin/runs (list + detail + abort + resume + override-gate)
- /admin/repos + refresh-policy
- /admin/jobs/queue
- Server-rendered Jinja + htmx UI
- Prometheus metrics + bearer-protected /admin/metrics
- Caddy TLS front + worker replicas in compose
- pg_dump backup sidecar (30-day retention, optional S3 offsite)
- Container hardening (cap-drop=ALL, read-only rootfs, no-new-privileges)
- Strato deploy scripts + caller workflow deploy-orchestrator.yml
- E2E suite: golden, remediation, risk-glob HUMAN_REQUIRED, abort/resume
- k6 webhook-spike load test
- Chaos scripts: kill-worker, pg-restart, rotate-secrets
- Migration: legacy openspec-* workflows marked deprecated, no-op by default

## Test plan

- [ ] make test green (unit + integration)
- [ ] Admin endpoints work behind OAuth
- [ ] Prometheus scrape returns expected counters/histograms
- [ ] 7-day staging burn-in completed clean
- [ ] Production deploy successful, one canary repo onboarded
- [ ] Chaos scenarios recover within documented bounds

Refs: spec PR #32, Plan 1, Plan 2.
EOF
)"
```

- [ ] **Step 3: Verify CI** + iterate.

- [ ] **Step 4: After merge + 7-day burn-in + 30-day stability** run Tasks 17 (production rollout) and 18 (decommission) as separate PRs.

---

## Self-review

- **Spec coverage** — Plan 3 implements §10 admin surface, §11.1 OAuth, §12 observability, §13 deploy, §14 hardening, §15 testing (E2E/load/chaos), §16 migration phases. Combined with Plans 1 + 2, the spec is fully covered.
- **Placeholder scan** — `<your-domain>`, `<strato-host>`, `<sig>` are intentional ops placeholders that the operator fills via Actions vars / `.env` at deploy time. No `TODO`/`TBD` in code.
- **Type consistency** — admin cookie name `admin_session` consistent across `auth.py`, `current_user`, tests, Jinja templates. `RunState` values in `runs_list.html` filter dropdown match the enum exactly. `Profile.name` strings used as Prometheus label values match the registry keys (`interviewer`, `glm-implementer`, `anthropic-reviewer`). Compose service names (`api`, `worker`, `postgres`, `migrator`, `caddy`, `pgbackup`) consistent across `docker-compose.yml`, Caddyfile reverse proxy target, and Strato scripts.
