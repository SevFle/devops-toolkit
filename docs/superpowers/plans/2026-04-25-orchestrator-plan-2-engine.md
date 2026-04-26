# Orchestrator Plan 2 — Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the engine on top of Plan 1's foundation: state machine, pg-only job queue, worker loop, sandboxing, CLI profile system, all five handlers, policy + gates + codeowners. End state: end-to-end issue → interview → impl → review → remediate → approve → merge in fixture repo.

**Architecture:** Worker process consumes jobs from `jobs` table via `SELECT … FOR UPDATE SKIP LOCKED`, woken by Postgres `LISTEN jobs_ready`. Handlers shell out to `claude` CLI with strict per-profile env. State transitions are tx-wrapped with mandatory `run_events` audit row. Per-repo + per-run advisory locks guard concurrent jobs. Policy is loaded fresh per webhook from each repo's `.devops/orchestrator.yml` and snapshotted into `repos.policy_snapshot_jsonb`.

**Tech Stack:** Python 3.12, SQLAlchemy 2.x async, asyncpg, structlog, httpx, pyyaml, pydantic 2 (for policy schema), pathspec (for glob matching), pytest-asyncio, testcontainers, respx, pytest-recording.

**Reference spec:** `docs/superpowers/specs/2026-04-25-glm-claude-orchestrator-design.md`
**Tracking issue:** `#34`
**Working branch:** `feat/orchestrator-engine` (off `main`, after Plan 1 PR merges)
**Depends on:** Plan 1 (#33) merged.

---

## Task 1: State machine — transitions table + guards (TDD)

**Files:** `orchestrator/src/orchestrator/state/{__init__,machine}.py`, `orchestrator/tests/unit/test_state_machine.py`

- [ ] **Step 1: Branch**

```bash
git checkout main
git pull
git checkout -b feat/orchestrator-engine
```

- [ ] **Step 2: Failing test** at `orchestrator/tests/unit/test_state_machine.py`

```python
from __future__ import annotations

import pytest

from orchestrator.db.models import RunState as S
from orchestrator.state.machine import InvalidTransitionError, is_terminal, next_state


def test_label_triage_transitions_new_to_interviewing() -> None:
    assert next_state(S.NEW, "label.ai-triage") == S.INTERVIEWING


def test_interview_complete_advances_to_ready() -> None:
    assert next_state(S.INTERVIEWING, "interview.complete") == S.READY


def test_label_implement_advances_ready_to_implementing() -> None:
    assert next_state(S.READY, "label.ai-implement") == S.IMPLEMENTING


def test_pr_open_advances() -> None:
    assert next_state(S.IMPLEMENTING, "pr.opened") == S.PR_OPEN


def test_review_blocking_to_remediating() -> None:
    assert next_state(S.UNDER_REVIEW, "review.blocking") == S.REMEDIATING


def test_review_approve_to_approved() -> None:
    assert next_state(S.UNDER_REVIEW, "review.approve") == S.APPROVED


def test_codeowner_approval_advances() -> None:
    assert next_state(S.APPROVED, "codeowner.approved") == S.MERGEABLE


def test_merge_advances_to_merged() -> None:
    assert next_state(S.MERGEABLE, "merge.success") == S.MERGED


def test_invalid_transition_raises() -> None:
    with pytest.raises(InvalidTransitionError):
        next_state(S.NEW, "merge.success")


def test_human_required_from_any_non_terminal() -> None:
    for s in [S.NEW, S.INTERVIEWING, S.READY, S.UNDER_REVIEW, S.APPROVED]:
        assert next_state(s, "gate.human_required") == S.HUMAN_REQUIRED


def test_terminal_states() -> None:
    assert is_terminal(S.MERGED)
    assert is_terminal(S.FAILED)
    assert is_terminal(S.HUMAN_REQUIRED)
    assert not is_terminal(S.UNDER_REVIEW)
```

- [ ] **Step 3: Run, expect FAIL**

- [ ] **Step 4: Implement** at `orchestrator/src/orchestrator/state/machine.py`

```python
from __future__ import annotations

from orchestrator.db.models import RunState as S


class InvalidTransitionError(ValueError):
    pass


TRANSITIONS: dict[tuple[S, str], S] = {
    (S.NEW, "label.ai-triage"): S.INTERVIEWING,
    (S.NEW, "label.ai-implement"): S.IMPLEMENTING,
    (S.INTERVIEWING, "interview.turn"): S.INTERVIEWING,
    (S.INTERVIEWING, "interview.complete"): S.READY,
    (S.READY, "label.ai-implement"): S.IMPLEMENTING,
    (S.IMPLEMENTING, "pr.opened"): S.PR_OPEN,
    (S.PR_OPEN, "review.start"): S.UNDER_REVIEW,
    (S.UNDER_REVIEW, "review.blocking"): S.REMEDIATING,
    (S.UNDER_REVIEW, "review.request_changes"): S.REMEDIATING,
    (S.UNDER_REVIEW, "review.approve"): S.APPROVED,
    (S.REMEDIATING, "remediate.pushed"): S.UNDER_REVIEW,
    (S.APPROVED, "codeowner.approved"): S.MERGEABLE,
    (S.MERGEABLE, "merge.success"): S.MERGED,
}

TERMINAL: frozenset[S] = frozenset({S.MERGED, S.FAILED, S.HUMAN_REQUIRED})


def next_state(state: S, event: str) -> S:
    if event == "gate.human_required" and state not in TERMINAL:
        return S.HUMAN_REQUIRED
    if event == "fail" and state not in TERMINAL:
        return S.FAILED
    try:
        return TRANSITIONS[(state, event)]
    except KeyError as e:
        raise InvalidTransitionError(f"no transition for ({state.value}, {event})") from e


def is_terminal(state: S) -> bool:
    return state in TERMINAL
```

- [ ] **Step 5: Run, expect PASS**

- [ ] **Step 6: Commit**

```bash
git add orchestrator/src/orchestrator/state/ orchestrator/tests/unit/test_state_machine.py
git commit -m "feat(orchestrator): state machine transition table + guards"
```

---

## Task 2: tx-wrapped transition + audit row helper (TDD)

**Files:** `orchestrator/src/orchestrator/state/transitions.py`, `orchestrator/tests/integration/test_transitions.py`

- [ ] **Step 1: Failing test** at `orchestrator/tests/integration/test_transitions.py`

```python
from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.base import Base
from orchestrator.db.models import Repo, Run, RunEvent, RunState
from orchestrator.state.machine import InvalidTransitionError
from orchestrator.state.transitions import transition


@pytest.mark.asyncio
async def test_transition_writes_state_and_audit_in_one_tx(session: AsyncSession) -> None:
    async with session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session.add(repo := Repo(full_name="o/r", installation_id=1))
    await session.flush()
    session.add(run := Run(repo_id=repo.id, issue_number=1, state=RunState.NEW))
    await session.commit()

    await transition(session, run_id=run.id, event="label.ai-triage", data={"by": "test"})

    refreshed = await session.get(Run, run.id)
    assert refreshed.state == RunState.INTERVIEWING
    events = (await session.execute(
        select(RunEvent).where(RunEvent.run_id == run.id)
    )).scalars().all()
    assert len(events) == 1
    assert events[0].type == "transition"
    assert events[0].data_jsonb == {
        "from": "NEW", "to": "INTERVIEWING", "event": "label.ai-triage", "by": "test",
    }


@pytest.mark.asyncio
async def test_transition_invalid_rolls_back(session: AsyncSession) -> None:
    async with session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session.add(repo := Repo(full_name="o/r2", installation_id=1))
    await session.flush()
    session.add(run := Run(repo_id=repo.id, issue_number=2, state=RunState.NEW))
    await session.commit()

    with pytest.raises(InvalidTransitionError):
        await transition(session, run_id=run.id, event="merge.success")

    refreshed = await session.get(Run, run.id)
    assert refreshed.state == RunState.NEW
    events = (await session.execute(
        select(RunEvent).where(RunEvent.run_id == run.id)
    )).scalars().all()
    assert events == []
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/state/transitions.py`

```python
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Run, RunEvent
from orchestrator.state.machine import InvalidTransitionError, next_state


async def transition(
    session: AsyncSession, *, run_id: int, event: str,
    job_id: int | None = None, data: dict[str, Any] | None = None,
) -> None:
    run = await session.get(Run, run_id, with_for_update=True)
    if run is None:
        raise ValueError(f"run {run_id} not found")
    try:
        new = next_state(run.state, event)
    except InvalidTransitionError:
        await session.rollback()
        raise
    payload = {"from": run.state.value, "to": new.value, "event": event}
    if data:
        payload.update(data)
    run.state = new
    run.state_updated_at = datetime.now(timezone.utc)
    session.add(RunEvent(run_id=run_id, job_id=job_id, type="transition", data_jsonb=payload))
    await session.commit()
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/state/transitions.py orchestrator/tests/integration/test_transitions.py
git commit -m "feat(orchestrator): tx-wrapped state transition with audit row"
```

---

## Task 3: pg job queue with SKIP LOCKED (TDD)

**Files:** `orchestrator/src/orchestrator/db/queue.py`, `orchestrator/tests/integration/test_queue_skip_locked.py`

- [ ] **Step 1: Failing test**

```python
# orchestrator/tests/integration/test_queue_skip_locked.py
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from orchestrator.db.base import Base
from orchestrator.db.models import Job, JobKind, JobStatus, Repo, Run
from orchestrator.db.queue import claim_one, complete, enqueue


@pytest.mark.asyncio
async def test_two_workers_do_not_double_claim(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        s.add(repo := Repo(full_name="o/r3", installation_id=1))
        await s.flush()
        s.add(run := Run(repo_id=repo.id, issue_number=10))
        await s.commit()
        await enqueue(s, run_id=run.id, kind=JobKind.implement, payload={"x": 1})

    async with factory() as a, factory() as b:
        ja, jb = await asyncio.gather(
            claim_one(a, worker_id="A", lease=timedelta(minutes=1)),
            claim_one(b, worker_id="B", lease=timedelta(minutes=1)),
        )
    assert (ja is None) ^ (jb is None)


@pytest.mark.asyncio
async def test_claim_run_complete(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        s.add(repo := Repo(full_name="o/r4", installation_id=1))
        await s.flush()
        s.add(run := Run(repo_id=repo.id, issue_number=11))
        await s.commit()
        await enqueue(s, run_id=run.id, kind=JobKind.review)

    async with factory() as s:
        job = await claim_one(s, worker_id="w1", lease=timedelta(seconds=30))
        assert job is not None
        assert job.status == JobStatus.running
        assert job.locked_by == "w1"
        await complete(s, job_id=job.id, result={"ok": True})

    async with factory() as s:
        rows = (await s.execute(select(Job))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == JobStatus.done
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/db/queue.py`

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import bindparam, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Job, JobKind, JobStatus


async def enqueue(
    session: AsyncSession, *, run_id: int, kind: JobKind,
    profile: str | None = None, priority: int = 0,
    payload: dict[str, Any] | None = None,
) -> int:
    job = Job(
        run_id=run_id, kind=kind, profile=profile,
        status=JobStatus.ready, priority=priority, payload_jsonb=payload,
    )
    session.add(job)
    await session.commit()
    await session.execute(text("NOTIFY jobs_ready"))
    return job.id


async def claim_one(
    session: AsyncSession, *, worker_id: str, lease: timedelta,
) -> Job | None:
    now = datetime.now(timezone.utc)
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.ready)
        .where((Job.locked_until.is_(None)) | (Job.locked_until < now))
        .order_by(Job.priority.desc(), Job.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    res = await session.execute(stmt)
    job = res.scalar_one_or_none()
    if job is None:
        await session.commit()
        return None
    job.status = JobStatus.running
    job.locked_by = worker_id
    job.locked_until = now + lease
    job.attempts += 1
    await session.commit()
    return job


async def heartbeat(session: AsyncSession, *, job_id: int, lease: timedelta) -> None:
    until = datetime.now(timezone.utc) + lease
    await session.execute(update(Job).where(Job.id == job_id).values(locked_until=until))
    await session.commit()


async def complete(
    session: AsyncSession, *, job_id: int, result: dict[str, Any] | None = None,
) -> None:
    await session.execute(
        update(Job).where(Job.id == job_id).values(
            status=JobStatus.done, result_jsonb=result, locked_by=None, locked_until=None,
        )
    )
    await session.commit()


async def fail_job(session: AsyncSession, *, job_id: int, reason: str) -> None:
    await session.execute(
        update(Job).where(Job.id == job_id).values(
            status=JobStatus.failed, result_jsonb={"error": reason},
            locked_by=None, locked_until=None,
        )
    )
    await session.commit()


async def release_lease(session: AsyncSession, *, job_id: int) -> None:
    await session.execute(
        update(Job).where(Job.id == job_id).values(
            status=JobStatus.ready, locked_by=None, locked_until=None,
        )
    )
    await session.commit()


async def acquire_repo_lock(session: AsyncSession, full_name: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))").bindparams(
            bindparam("k", value=f"repo:{full_name}")
        )
    )


async def acquire_run_lock(session: AsyncSession, run_id: int) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))").bindparams(
            bindparam("k", value=f"run:{run_id}")
        )
    )
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/db/queue.py orchestrator/tests/integration/test_queue_skip_locked.py
git commit -m "feat(orchestrator): pg queue (SKIP LOCKED + lease + NOTIFY) + advisory locks"
```

---

## Task 4: Lease reaper (TDD)

**Files:** `orchestrator/src/orchestrator/worker/__init__.py`, `orchestrator/src/orchestrator/worker/leases.py`, `orchestrator/tests/integration/test_listen_notify.py`

- [ ] **Step 1: Failing test**

```python
# orchestrator/tests/integration/test_listen_notify.py
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from orchestrator.db.base import Base
from orchestrator.db.models import JobKind, JobStatus, Repo, Run
from orchestrator.db.queue import claim_one, enqueue
from orchestrator.worker.leases import reap_expired_leases


@pytest.mark.asyncio
async def test_reaper_recovers_expired_lease(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        s.add(repo := Repo(full_name="o/r5", installation_id=1))
        await s.flush()
        s.add(run := Run(repo_id=repo.id, issue_number=20))
        await s.commit()
        await enqueue(s, run_id=run.id, kind=JobKind.implement)

    async with factory() as s:
        job = await claim_one(s, worker_id="dead", lease=timedelta(milliseconds=10))
        assert job is not None

    await asyncio.sleep(0.05)
    async with factory() as s:
        n = await reap_expired_leases(s, grace=timedelta(milliseconds=0))
        assert n == 1

    async with factory() as s:
        job = await claim_one(s, worker_id="alive", lease=timedelta(seconds=30))
        assert job is not None
        assert job.status == JobStatus.running
        assert job.locked_by == "alive"
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/worker/leases.py`

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Job, JobStatus


async def reap_expired_leases(session: AsyncSession, *, grace: timedelta) -> int:
    cutoff = datetime.now(timezone.utc) - grace
    stmt = (
        update(Job)
        .where(Job.status == JobStatus.running, Job.locked_until < cutoff)
        .values(status=JobStatus.ready, locked_by=None, locked_until=None)
        .returning(Job.id)
    )
    res = await session.execute(stmt)
    rows = res.fetchall()
    await session.commit()
    return len(rows)
```

Add empty `orchestrator/src/orchestrator/worker/__init__.py`.

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/worker/ orchestrator/tests/integration/test_listen_notify.py
git commit -m "feat(orchestrator): lease reaper recovers crashed-worker jobs"
```

---

## Task 5: Worker loop

**Files:** `orchestrator/src/orchestrator/worker/loop.py`, `orchestrator/src/orchestrator/worker/handlers/__init__.py`, modify `orchestrator/src/orchestrator/__main__.py`.

- [ ] **Step 1: Implement loop** at `orchestrator/src/orchestrator/worker/loop.py`

```python
from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta

import asyncpg
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.models import Job
from orchestrator.db.queue import claim_one, complete, fail_job
from orchestrator.db.session import make_engine, make_session_factory
from orchestrator.worker.leases import reap_expired_leases

log = structlog.get_logger()
JobHandler = Callable[[AsyncSession, Job], Awaitable[dict]]


class WorkerLoop:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        handlers: dict[str, JobHandler],
        *,
        worker_id: str | None = None,
        lease: timedelta = timedelta(minutes=10),
        idle_poll: float = 5.0,
        reap_grace: timedelta = timedelta(seconds=30),
    ) -> None:
        self.factory = factory
        self.handlers = handlers
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.lease = lease
        self.idle_poll = idle_poll
        self.reap_grace = reap_grace
        self._stop = asyncio.Event()

    async def run_forever(self, *, dsn: str) -> None:
        log.info("worker_start", worker_id=self.worker_id)
        listener = asyncio.create_task(self._listen(dsn))
        reaper = asyncio.create_task(self._reaper_loop())
        try:
            while not self._stop.is_set():
                claimed = await self._claim_and_run_one()
                if not claimed:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=self.idle_poll)
                    except asyncio.TimeoutError:
                        pass
        finally:
            listener.cancel()
            reaper.cancel()

    async def stop(self) -> None:
        self._stop.set()

    async def _listen(self, dsn: str) -> None:
        raw_dsn = dsn.replace("+asyncpg", "")
        conn: asyncpg.Connection = await asyncpg.connect(raw_dsn)
        try:
            await conn.add_listener("jobs_ready", lambda *_: None)
            while not self._stop.is_set():
                await asyncio.sleep(self.idle_poll)
        finally:
            await conn.close()

    async def _reaper_loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with self.factory() as s:
                    n = await reap_expired_leases(s, grace=self.reap_grace)
                if n:
                    log.info("reaper_recovered", count=n)
            except Exception as e:
                log.warning("reaper_error", error=str(e))
            await asyncio.sleep(15)

    async def _claim_and_run_one(self) -> bool:
        async with self.factory() as s:
            job = await claim_one(s, worker_id=self.worker_id, lease=self.lease)
        if job is None:
            return False
        log.info("job_claimed", job_id=job.id, kind=job.kind.value)
        try:
            handler = self.handlers[job.kind.value]
            async with self.factory() as s:
                result = await handler(s, job)
            async with self.factory() as s:
                await complete(s, job_id=job.id, result=result)
        except Exception as e:
            log.exception("job_failed", job_id=job.id, error=str(e))
            async with self.factory() as s:
                await fail_job(s, job_id=job.id, reason=str(e))
        return True


def build_default_loop() -> WorkerLoop:
    from orchestrator.worker.handlers import HANDLERS
    factory = make_session_factory(make_engine())
    return WorkerLoop(factory, HANDLERS)
```

- [ ] **Step 2: Stub HANDLERS** at `orchestrator/src/orchestrator/worker/handlers/__init__.py` (filled in Tasks 14–18)

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Job

JobHandler = Callable[[AsyncSession, Job], Awaitable[dict]]
HANDLERS: dict[str, JobHandler] = {}
```

- [ ] **Step 3: Wire `cmd_worker`** in `orchestrator/src/orchestrator/__main__.py`

```python
def cmd_worker() -> int:
    import asyncio
    from orchestrator.worker.loop import build_default_loop
    s = get_settings()
    loop_obj = build_default_loop()
    asyncio.run(loop_obj.run_forever(dsn=s.database_url))
    return 0
```

- [ ] **Step 4: Smoke**

```bash
docker compose -f deploy/compose/docker-compose.yml up -d postgres
DATABASE_URL=postgresql+asyncpg://orch:orch@localhost:5432/orch \
GH_APP_ID=1 GH_APP_PRIVATE_KEY_PATH=deploy/compose/gh-app.pem GH_APP_WEBHOOK_SECRET=x \
ANTHROPIC_API_KEY=x ZAI_API_KEY=x \
timeout 5 python -m orchestrator worker || true
```

Expected: prints `worker_start` JSON, idles, exits via timeout.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/worker/ orchestrator/src/orchestrator/__main__.py
git commit -m "feat(orchestrator): worker loop with LISTEN + claim + reaper"
```

---

## Task 6: Sandbox workdir + git ops (TDD)

**Files:** `orchestrator/src/orchestrator/sandbox/{__init__,workdir,git}.py`, `orchestrator/tests/unit/test_sandbox_workdir.py`, `orchestrator/tests/integration/test_sandbox_git.py`

- [ ] **Step 1: workdir test** at `orchestrator/tests/unit/test_sandbox_workdir.py`

```python
from __future__ import annotations

from pathlib import Path

from orchestrator.sandbox.workdir import RunSandbox


def test_sandbox_paths(tmp_path: Path) -> None:
    sb = RunSandbox(root=tmp_path, run_id=42)
    sb.ensure()
    assert sb.repo_dir == tmp_path / "42" / "repo"
    assert sb.home_dir == tmp_path / "42" / "home"
    assert sb.logs_dir == tmp_path / "42" / "logs"
    assert sb.tmp_dir == tmp_path / "42" / "tmp"
    for p in [sb.repo_dir, sb.home_dir, sb.logs_dir, sb.tmp_dir]:
        assert p.is_dir()


def test_sandbox_destroy(tmp_path: Path) -> None:
    sb = RunSandbox(root=tmp_path, run_id=7)
    sb.ensure()
    (sb.repo_dir / "x.txt").write_text("hi")
    sb.destroy()
    assert not (tmp_path / "7").exists()
```

- [ ] **Step 2: Implement workdir**

```python
# orchestrator/src/orchestrator/sandbox/workdir.py
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunSandbox:
    root: Path
    run_id: int

    @property
    def base(self) -> Path:
        return self.root / str(self.run_id)

    @property
    def repo_dir(self) -> Path:
        return self.base / "repo"

    @property
    def home_dir(self) -> Path:
        return self.base / "home"

    @property
    def logs_dir(self) -> Path:
        return self.base / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.base / "tmp"

    def ensure(self) -> None:
        for p in [self.repo_dir, self.home_dir, self.logs_dir, self.tmp_dir]:
            p.mkdir(parents=True, exist_ok=True)

    def destroy(self) -> None:
        if self.base.exists():
            shutil.rmtree(self.base)
```

- [ ] **Step 3: git ops test** at `orchestrator/tests/integration/test_sandbox_git.py`

```python
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.sandbox.git import clone, create_branch, current_branch
from orchestrator.sandbox.workdir import RunSandbox


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    o = tmp_path / "origin"
    o.mkdir()
    subprocess.run(["git", "init", "-q", "--bare"], check=True, cwd=o)
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", "-q"], check=True, cwd=work)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-b", "main"], check=True)
    (work / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(o)], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "main"], check=True)
    return o


def test_clone_and_branch(tmp_path: Path, origin_repo: Path) -> None:
    sb = RunSandbox(root=tmp_path / "sb", run_id=1)
    sb.ensure()
    clone(sb, repo_url=str(origin_repo), token="ignored-for-local")
    assert (sb.repo_dir / "README.md").read_text() == "hi\n"
    create_branch(sb, "ai/1-feature")
    assert current_branch(sb) == "ai/1-feature"
```

- [ ] **Step 4: Implement git ops** at `orchestrator/src/orchestrator/sandbox/git.py`

```python
from __future__ import annotations

import subprocess
from urllib.parse import urlparse

import structlog

from orchestrator.sandbox.workdir import RunSandbox

log = structlog.get_logger()


def _run(cmd: list[str], cwd: str, env: dict[str, str] | None = None) -> str:
    out = subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True, env=env)
    return out.stdout.strip()


def _authed_url(repo_url: str, token: str) -> str:
    p = urlparse(repo_url)
    if p.scheme != "https":
        return repo_url
    return f"https://x-access-token:{token}@{p.netloc}{p.path}"


def clone(sandbox: RunSandbox, *, repo_url: str, token: str) -> None:
    url = _authed_url(repo_url, token)
    _run(["git", "clone", "--quiet", url, "."], cwd=str(sandbox.repo_dir))
    log.info("git_clone_done", repo_dir=str(sandbox.repo_dir))


def create_branch(sandbox: RunSandbox, branch: str) -> None:
    _run(["git", "checkout", "-q", "-b", branch], cwd=str(sandbox.repo_dir))


def current_branch(sandbox: RunSandbox) -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(sandbox.repo_dir))


def commit_all(sandbox: RunSandbox, *, message: str, author_email: str, author_name: str) -> str:
    _run(["git", "config", "user.email", author_email], cwd=str(sandbox.repo_dir))
    _run(["git", "config", "user.name", author_name], cwd=str(sandbox.repo_dir))
    _run(["git", "add", "-A"], cwd=str(sandbox.repo_dir))
    _run(["git", "commit", "--allow-empty", "-q", "-m", message], cwd=str(sandbox.repo_dir))
    return _run(["git", "rev-parse", "HEAD"], cwd=str(sandbox.repo_dir))


def push(sandbox: RunSandbox, *, branch: str, repo_url: str, token: str) -> None:
    url = _authed_url(repo_url, token)
    _run(["git", "remote", "set-url", "origin", url], cwd=str(sandbox.repo_dir))
    _run(["git", "push", "-q", "origin", f"HEAD:{branch}"], cwd=str(sandbox.repo_dir))
    log.info("git_push_done", branch=branch)
```

- [ ] **Step 5: Run, expect PASS**

- [ ] **Step 6: Commit**

```bash
git add orchestrator/src/orchestrator/sandbox/ orchestrator/tests/unit/test_sandbox_workdir.py orchestrator/tests/integration/test_sandbox_git.py
git commit -m "feat(orchestrator): sandbox workdir + git ops"
```

---

## Task 7: CLI profile registry (TDD)

**Files:** `orchestrator/src/orchestrator/cli/{__init__,profile}.py`, `orchestrator/tests/unit/test_cli_profile.py`

Add `pathspec>=0.12` to `orchestrator/pyproject.toml` `dependencies`.

- [ ] **Step 1: Failing test**

```python
# orchestrator/tests/unit/test_cli_profile.py
from __future__ import annotations

from orchestrator.cli.profile import PROFILES, build_env


def test_three_profiles_registered() -> None:
    assert set(PROFILES.keys()) == {"interviewer", "glm-implementer", "anthropic-reviewer"}


def test_glm_targets_zai() -> None:
    p = PROFILES["glm-implementer"]
    assert p.base_url == "https://api.z.ai/api/anthropic"
    assert p.auth_env_var == "ZAI_API_KEY"


def test_glm_env_uses_anthropic_auth_token() -> None:
    p = PROFILES["glm-implementer"]
    env = build_env(p, secret="zai-X", install_token="ghs-Y", home_dir="/sandbox/home")
    assert env["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "zai-X"
    assert env["ANTHROPIC_MODEL"] == "glm-5.1"
    assert env["GH_TOKEN"] == "ghs-Y"
    assert "ANTHROPIC_API_KEY" not in env


def test_real_anthropic_uses_api_key() -> None:
    p = PROFILES["anthropic-reviewer"]
    env = build_env(p, secret="sk-ant", install_token="ghs", home_dir="/sandbox/home")
    assert env["ANTHROPIC_API_KEY"] == "sk-ant"
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["ANTHROPIC_MODEL"] == "claude-opus-4-7"
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/cli/profile.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    base_url: str | None
    auth_env_var: str
    permission_mode: str
    extra_env: dict[str, str] = field(default_factory=dict)
    output_format: str = "json"
    max_turns: int | None = None
    timeout_s: int = 600


PROFILES: dict[str, Profile] = {
    "interviewer": Profile(
        name="interviewer", model="claude-sonnet-4-6",
        base_url=None, auth_env_var="ANTHROPIC_API_KEY",
        permission_mode="plan", output_format="json",
        max_turns=12, timeout_s=300,
    ),
    "glm-implementer": Profile(
        name="glm-implementer", model="glm-5.1",
        base_url="https://api.z.ai/api/anthropic",
        auth_env_var="ZAI_API_KEY",
        permission_mode="acceptEdits",
        extra_env={"DISABLE_TELEMETRY": "1"},
        output_format="stream-json",
        max_turns=None, timeout_s=1800,
    ),
    "anthropic-reviewer": Profile(
        name="anthropic-reviewer", model="claude-opus-4-7",
        base_url=None, auth_env_var="ANTHROPIC_API_KEY",
        permission_mode="plan", output_format="json",
        max_turns=4, timeout_s=600,
    ),
}


def build_env(profile: Profile, *, secret: str, install_token: str, home_dir: str) -> dict[str, str]:
    env: dict[str, str] = {
        "PATH": DEFAULT_PATH, "HOME": home_dir, "LANG": "C.UTF-8", "TZ": "UTC",
        "GH_TOKEN": install_token, "ANTHROPIC_MODEL": profile.model,
    }
    if profile.base_url:
        env["ANTHROPIC_BASE_URL"] = profile.base_url
        env["ANTHROPIC_AUTH_TOKEN"] = secret
    else:
        env["ANTHROPIC_API_KEY"] = secret
    env.update(profile.extra_env)
    return env
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/cli/ orchestrator/tests/unit/test_cli_profile.py orchestrator/pyproject.toml
git commit -m "feat(orchestrator): CLI profile registry (interviewer | glm-implementer | anthropic-reviewer)"
```

---

## Task 8: CLI runner with stream capture

**Files:** `orchestrator/src/orchestrator/cli/runner.py`, `orchestrator/tests/integration/test_cli_runner.py`

- [ ] **Step 1: Failing test**

```python
# orchestrator/tests/integration/test_cli_runner.py
from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.cli.profile import Profile
from orchestrator.cli.runner import CliResult, run_cli


@pytest.mark.asyncio
async def test_run_cli_captures_stdout(tmp_path: Path) -> None:
    fake = Profile(
        name="fake", model="x", base_url=None, auth_env_var="ANTHROPIC_API_KEY",
        permission_mode="plan", output_format="json", max_turns=1, timeout_s=10,
    )
    res: CliResult = await run_cli(
        argv=["python", "-c", "import sys; print('hello'); sys.exit(0)"],
        profile=fake, secret="x", install_token="y", home_dir=str(tmp_path / "home"),
        cwd=str(tmp_path), log_path=tmp_path / "log.txt",
    )
    assert res.exit_code == 0
    assert "hello" in res.stdout


@pytest.mark.asyncio
async def test_run_cli_timeout_kills(tmp_path: Path) -> None:
    fake = Profile(
        name="fake", model="x", base_url=None, auth_env_var="ANTHROPIC_API_KEY",
        permission_mode="plan", output_format="json", max_turns=1, timeout_s=1,
    )
    res = await run_cli(
        argv=["python", "-c", "import time; time.sleep(10)"],
        profile=fake, secret="x", install_token="y", home_dir=str(tmp_path / "home"),
        cwd=str(tmp_path), log_path=tmp_path / "log.txt",
    )
    assert res.exit_code != 0
    assert res.timed_out is True
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/cli/runner.py`

```python
from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

import structlog

from orchestrator.cli.profile import Profile, build_env

log = structlog.get_logger()


@dataclass(frozen=True)
class CliResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


async def run_cli(
    *,
    argv: list[str],
    profile: Profile,
    secret: str,
    install_token: str,
    home_dir: str,
    cwd: str,
    log_path: Path,
) -> CliResult:
    Path(home_dir).mkdir(parents=True, exist_ok=True)
    env = build_env(profile, secret=secret, install_token=install_token, home_dir=home_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log.info("cli_run_start", profile=profile.name, model=profile.model, argv=argv[0])

    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    out_chunks: list[str] = []
    err_chunks: list[str] = []

    async def pump(stream: asyncio.StreamReader, sink: list[str], label: str) -> None:
        async for line in stream:
            text = line.decode(errors="replace")
            sink.append(text)
            with log_path.open("a") as fh:
                fh.write(f"[{label}] {text}")

    pumps = asyncio.gather(
        pump(proc.stdout, out_chunks, "out"),
        pump(proc.stderr, err_chunks, "err"),
    )
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=profile.timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
    await pumps
    return CliResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout="".join(out_chunks),
        stderr="".join(err_chunks),
        timed_out=timed_out,
    )
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/cli/runner.py orchestrator/tests/integration/test_cli_runner.py
git commit -m "feat(orchestrator): CLI subprocess runner with stream tee + timeout"
```

---

## Task 9: CLI parsers (TDD)

**Files:** `orchestrator/src/orchestrator/cli/parsers.py`, `orchestrator/tests/unit/test_cli_parsers.py`

- [ ] **Step 1: Failing test**

```python
# orchestrator/tests/unit/test_cli_parsers.py
from __future__ import annotations

import pytest

from orchestrator.cli.parsers import (
    parse_interview_output, parse_review_output, parse_stream_json_token_usage,
)


def test_parse_interview_ask() -> None:
    out = parse_interview_output('{"kind":"ask","question":"What test framework?"}')
    assert out.kind == "ask"
    assert out.question == "What test framework?"


def test_parse_interview_complete() -> None:
    raw = ('{"kind":"complete","template":'
           '{"goal":"x","acceptance_criteria":["a"],"files":["f"],"test_command":"t","risk_level":"low"}}')
    out = parse_interview_output(raw)
    assert out.kind == "complete"
    assert out.template["goal"] == "x"


def test_parse_review_approve() -> None:
    out = parse_review_output('{"verdict":"approve","findings":[]}')
    assert out.verdict == "approve"
    assert out.findings == []


def test_parse_review_blocking_with_findings() -> None:
    raw = ('{"verdict":"blocking","findings":['
           '{"category":"security","severity":"blocking","path":"a.py","line":3,'
           '"message":"sql injection","suggested_fix":"use ?"}]}')
    out = parse_review_output(raw)
    assert out.verdict == "blocking"
    assert out.findings[0].category == "security"


def test_malformed_review_raises() -> None:
    with pytest.raises(ValueError):
        parse_review_output("not json")


def test_stream_json_token_usage() -> None:
    lines = [
        '{"type":"system"}',
        '{"type":"assistant","message":{"usage":{"input_tokens":100,"output_tokens":50}}}',
        '{"type":"assistant","message":{"usage":{"input_tokens":80,"output_tokens":40}}}',
    ]
    usage = parse_stream_json_token_usage("\n".join(lines))
    assert usage.input_tokens == 180
    assert usage.output_tokens == 90
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/cli/parsers.py`

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError


class InterviewTurnOutput(BaseModel):
    kind: Literal["ask", "complete"]
    question: str | None = None
    template: dict[str, Any] | None = None


class Finding(BaseModel):
    category: Literal["bug", "security", "perf", "tests_missing", "style"]
    severity: Literal["blocking", "non_blocking"]
    path: str
    line: int | None = None
    message: str
    suggested_fix: str | None = None


class ReviewOutput(BaseModel):
    verdict: Literal["approve", "request_changes", "blocking"]
    findings: list[Finding] = []


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int


def _last_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("empty CLI output")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON object found in CLI output")


def parse_interview_output(raw: str) -> InterviewTurnOutput:
    try:
        return InterviewTurnOutput.model_validate(_last_json_object(raw))
    except ValidationError as e:
        raise ValueError(f"interview output schema mismatch: {e}") from e


def parse_review_output(raw: str) -> ReviewOutput:
    try:
        return ReviewOutput.model_validate(_last_json_object(raw))
    except ValidationError as e:
        raise ValueError(f"review output schema mismatch: {e}") from e


def parse_stream_json_token_usage(raw: str) -> TokenUsage:
    inp = 0
    out = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        inp += int(usage.get("input_tokens") or 0)
        out += int(usage.get("output_tokens") or 0)
    return TokenUsage(input_tokens=inp, output_tokens=out)
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/cli/parsers.py orchestrator/tests/unit/test_cli_parsers.py
git commit -m "feat(orchestrator): CLI output parsers (interview, review, stream tokens)"
```

---

## Task 10: GitHub PR client (httpx wrapper)

**Files:** `orchestrator/src/orchestrator/github/{client,pr}.py`, `orchestrator/tests/integration/test_pr_ops.py`

- [ ] **Step 1: Failing test**

```python
# orchestrator/tests/integration/test_pr_ops.py
from __future__ import annotations

import pytest
import respx
from httpx import Response

from orchestrator.github.pr import PrApi


@pytest.mark.asyncio
async def test_create_pull_request() -> None:
    with respx.mock(base_url="https://api.github.com") as rx:
        rx.post("/repos/o/r/pulls").mock(
            return_value=Response(201, json={"number": 7, "html_url": "https://x"})
        )
        api = PrApi(token="ghs", owner="o", repo="r")
        n, url = await api.create_pull_request(
            title="t", body="b", head="ai/1-x", base="main",
        )
        assert n == 7
        assert url == "https://x"


@pytest.mark.asyncio
async def test_create_review_with_comments() -> None:
    with respx.mock(base_url="https://api.github.com") as rx:
        rx.post("/repos/o/r/pulls/7/reviews").mock(
            return_value=Response(200, json={"id": 999})
        )
        api = PrApi(token="ghs", owner="o", repo="r")
        rid = await api.create_review(
            pull_number=7, event="REQUEST_CHANGES",
            body="see comments",
            comments=[{"path": "a.py", "line": 3, "body": "fix this"}],
        )
        assert rid == 999


@pytest.mark.asyncio
async def test_merge_squash() -> None:
    with respx.mock(base_url="https://api.github.com") as rx:
        rx.put("/repos/o/r/pulls/7/merge").mock(
            return_value=Response(200, json={"merged": True, "sha": "abc"})
        )
        api = PrApi(token="ghs", owner="o", repo="r")
        sha = await api.merge(pull_number=7, method="squash")
        assert sha == "abc"
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/github/client.py`

```python
from __future__ import annotations

import httpx


def make_client(token: str, *, timeout: float = 15) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://api.github.com",
        timeout=timeout,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
```

`orchestrator/src/orchestrator/github/pr.py`:

```python
from __future__ import annotations

from typing import Any

from orchestrator.github.client import make_client


class PrApi:
    def __init__(self, *, token: str, owner: str, repo: str) -> None:
        self.token = token
        self.owner = owner
        self.repo = repo

    async def create_pull_request(
        self, *, title: str, body: str, head: str, base: str = "main", draft: bool = False
    ) -> tuple[int, str]:
        async with make_client(self.token) as cli:
            r = await cli.post(
                f"/repos/{self.owner}/{self.repo}/pulls",
                json={"title": title, "body": body, "head": head, "base": base, "draft": draft},
            )
            r.raise_for_status()
            data = r.json()
            return int(data["number"]), str(data["html_url"])

    async def comment(self, *, issue_number: int, body: str) -> int:
        async with make_client(self.token) as cli:
            r = await cli.post(
                f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
                json={"body": body},
            )
            r.raise_for_status()
            return int(r.json()["id"])

    async def add_label(self, *, issue_number: int, label: str) -> None:
        async with make_client(self.token) as cli:
            r = await cli.post(
                f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/labels",
                json={"labels": [label]},
            )
            r.raise_for_status()

    async def remove_label(self, *, issue_number: int, label: str) -> None:
        async with make_client(self.token) as cli:
            r = await cli.delete(
                f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/labels/{label}",
            )
            if r.status_code not in (200, 204, 404):
                r.raise_for_status()

    async def get_diff(self, *, pull_number: int) -> str:
        async with make_client(self.token) as cli:
            r = await cli.get(
                f"/repos/{self.owner}/{self.repo}/pulls/{pull_number}",
                headers={"Accept": "application/vnd.github.v3.diff"},
            )
            r.raise_for_status()
            return r.text

    async def create_review(
        self, *, pull_number: int, event: str, body: str,
        comments: list[dict[str, Any]] | None = None,
    ) -> int:
        async with make_client(self.token) as cli:
            r = await cli.post(
                f"/repos/{self.owner}/{self.repo}/pulls/{pull_number}/reviews",
                json={"event": event, "body": body, "comments": comments or []},
            )
            r.raise_for_status()
            return int(r.json()["id"])

    async def merge(self, *, pull_number: int, method: str = "squash") -> str:
        async with make_client(self.token) as cli:
            r = await cli.put(
                f"/repos/{self.owner}/{self.repo}/pulls/{pull_number}/merge",
                json={"merge_method": method},
            )
            r.raise_for_status()
            return str(r.json()["sha"])

    async def list_changed_paths(self, *, pull_number: int) -> list[str]:
        async with make_client(self.token) as cli:
            r = await cli.get(
                f"/repos/{self.owner}/{self.repo}/pulls/{pull_number}/files",
                params={"per_page": 100},
            )
            r.raise_for_status()
            return [f["filename"] for f in r.json()]
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/github/client.py orchestrator/src/orchestrator/github/pr.py orchestrator/tests/integration/test_pr_ops.py
git commit -m "feat(orchestrator): GitHub PR API wrapper"
```

---

## Task 11: CODEOWNERS parser (TDD)

**Files:** `orchestrator/src/orchestrator/github/codeowners.py`, `orchestrator/tests/unit/test_codeowners.py`

- [ ] **Step 1: Failing test**

```python
# orchestrator/tests/unit/test_codeowners.py
from __future__ import annotations

from orchestrator.github.codeowners import CodeOwners


def test_root_default_owner() -> None:
    co = CodeOwners.parse("* @alice\n")
    assert co.owners_for("README.md") == {"@alice"}
    assert co.owners_for("src/foo.py") == {"@alice"}


def test_path_overrides_root() -> None:
    co = CodeOwners.parse("* @alice\nsrc/auth/* @sec-team\n")
    assert co.owners_for("src/auth/login.py") == {"@sec-team"}
    assert co.owners_for("src/other.py") == {"@alice"}


def test_glob_doublestar() -> None:
    co = CodeOwners.parse("docs/** @docs-team\n")
    assert co.owners_for("docs/intro.md") == {"@docs-team"}
    assert co.owners_for("docs/sub/dir/x.md") == {"@docs-team"}
    assert co.owners_for("README.md") == set()


def test_multiple_owners_per_rule() -> None:
    co = CodeOwners.parse("payments/** @bob @sec-team\n")
    assert co.owners_for("payments/checkout.py") == {"@bob", "@sec-team"}


def test_uncovered_partial_coverage() -> None:
    co = CodeOwners.parse("auth/** @sec\npayments/** @bob\n")
    paths = ["auth/x.py", "payments/y.py"]
    assert co.uncovered(paths, approvers={"@sec"}) == ["payments/y.py"]


def test_uncovered_full_coverage() -> None:
    co = CodeOwners.parse("auth/** @sec\npayments/** @bob @sec\n")
    paths = ["auth/x.py", "payments/y.py"]
    assert co.uncovered(paths, approvers={"@sec", "@bob"}) == []
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/github/codeowners.py`

```python
from __future__ import annotations

from dataclasses import dataclass

from pathspec.patterns import GitWildMatchPattern


@dataclass(frozen=True)
class _Rule:
    pattern: str
    owners: tuple[str, ...]
    matcher: GitWildMatchPattern


class CodeOwners:
    def __init__(self, rules: list[_Rule]) -> None:
        self._rules = rules

    @classmethod
    def parse(cls, text: str) -> "CodeOwners":
        rules: list[_Rule] = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            pattern, owners = parts[0], tuple(parts[1:])
            rules.append(_Rule(pattern, owners, GitWildMatchPattern(pattern)))
        return cls(rules)

    def owners_for(self, path: str) -> set[str]:
        chosen: tuple[str, ...] = ()
        for rule in self._rules:
            if rule.matcher.match_file(path):
                chosen = rule.owners
        return set(chosen)

    def uncovered(self, paths: list[str], *, approvers: set[str]) -> list[str]:
        out: list[str] = []
        for p in paths:
            owners = self.owners_for(p)
            if owners and not (owners & approvers):
                out.append(p)
        return out
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/github/codeowners.py orchestrator/tests/unit/test_codeowners.py
git commit -m "feat(orchestrator): CODEOWNERS parser + ownership coverage"
```

---

## Task 12: Policy schema + gates + loader (TDD)

**Files:** `orchestrator/src/orchestrator/policy/{__init__,schema,loader,gates}.py`, `orchestrator/tests/unit/test_policy_schema.py`, `orchestrator/tests/unit/test_policy_gates.py`, `orchestrator/tests/integration/test_policy_loader.py`

- [ ] **Step 1: Schema + gates tests**

```python
# orchestrator/tests/unit/test_policy_schema.py
from __future__ import annotations

import pytest

from orchestrator.policy.schema import Policy, PolicyValidationError, parse_policy_yaml


YAML_OK = """
version: 1
intake:
  triage_label: ai-triage
  implement_label: ai-implement
  required_template_fields: [goal, acceptance_criteria, files, test_command, risk_level]
models: { interviewer: claude-sonnet-4-6, implementer: glm-5.1, reviewer: claude-opus-4-7 }
limits: { max_remediation_cycles: 3, diff_max_files: 40, diff_max_lines: 2000, per_run_timeout_h: 4 }
risk_globs: ["migrations/**"]
human_required_when:
  - any_path_matches: risk_globs
  - cycle_at_or_above: 3
merge: { strategy: squash, auto_merge: true, required_codeowner_approval: true, required_checks: [ci] }
review: { comment_style: inline, ignore_categories: [], approve_when_no_findings: true }
test_command: "make test"
"""


def test_minimal_policy() -> None:
    p: Policy = parse_policy_yaml(YAML_OK)
    assert p.limits.max_remediation_cycles == 3
    assert p.merge.auto_merge is True


def test_unknown_version_rejected() -> None:
    with pytest.raises(PolicyValidationError):
        parse_policy_yaml("version: 99\n")
```

```python
# orchestrator/tests/unit/test_policy_gates.py
from __future__ import annotations

from orchestrator.policy.gates import GateContext, decide_human_required


def _ctx(**kw):
    return GateContext(
        risk_globs=["migrations/**", "auth/**"],
        max_cycles=3,
        cycle_count=kw.get("cycle_count", 0),
        changed_paths=kw.get("changed_paths", []),
        review_findings=kw.get("review_findings", []),
    )


def test_risk_path_triggers_gate() -> None:
    d = decide_human_required(_ctx(changed_paths=["migrations/0042.sql"]))
    assert d.required is True
    assert d.reason.startswith("risk_path:")


def test_cycle_limit_triggers_gate() -> None:
    d = decide_human_required(_ctx(cycle_count=3))
    assert d.required is True
    assert "cycle" in d.reason


def test_security_blocking_triggers_gate() -> None:
    d = decide_human_required(_ctx(review_findings=[{"category":"security","severity":"blocking"}]))
    assert d.required is True
    assert "security" in d.reason


def test_no_gate_when_safe() -> None:
    assert decide_human_required(_ctx(changed_paths=["src/foo.py"])).required is False
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement schema** at `orchestrator/src/orchestrator/policy/schema.py`

```python
from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError


class PolicyValidationError(ValueError):
    pass


class Intake(BaseModel):
    triage_label: str = "ai-triage"
    implement_label: str = "ai-implement"
    required_template_fields: list[str] = []


class Models(BaseModel):
    interviewer: str
    implementer: str
    reviewer: str


class Limits(BaseModel):
    max_remediation_cycles: int = 3
    diff_max_files: int = 40
    diff_max_lines: int = 2000
    per_run_timeout_h: int = 4


class Merge(BaseModel):
    strategy: Literal["squash", "merge", "rebase"] = "squash"
    auto_merge: bool = True
    required_codeowner_approval: bool = True
    required_checks: list[str] = []


class Review(BaseModel):
    comment_style: Literal["inline", "summary"] = "inline"
    ignore_categories: list[str] = []
    approve_when_no_findings: bool = True


class HumanRule(BaseModel):
    any_path_matches: str | None = None
    cycle_at_or_above: int | None = None
    finding: dict[str, str] | None = None


class Policy(BaseModel):
    version: int = Field(...)
    intake: Intake
    models: Models
    limits: Limits
    risk_globs: list[str] = []
    human_required_when: list[HumanRule] = []
    merge: Merge
    review: Review
    test_command: str
    prompts: dict[str, str] | None = None
    ignore: dict[str, list[str]] | None = None


def parse_policy_yaml(text: str) -> Policy:
    data: Any = yaml.safe_load(text) or {}
    if data.get("version") != 1:
        raise PolicyValidationError(f"unsupported policy version: {data.get('version')!r}")
    try:
        return Policy.model_validate(data)
    except ValidationError as e:
        raise PolicyValidationError(str(e)) from e
```

- [ ] **Step 4: Implement gates** at `orchestrator/src/orchestrator/policy/gates.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pathspec.patterns import GitWildMatchPattern


@dataclass(frozen=True)
class GateContext:
    risk_globs: list[str]
    max_cycles: int
    cycle_count: int = 0
    changed_paths: list[str] = field(default_factory=list)
    review_findings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class GateDecision:
    required: bool
    reason: str


def decide_human_required(ctx: GateContext) -> GateDecision:
    matchers = [GitWildMatchPattern(g) for g in ctx.risk_globs]
    for path in ctx.changed_paths:
        for m, glob in zip(matchers, ctx.risk_globs, strict=True):
            if m.match_file(path):
                return GateDecision(True, f"risk_path: {path} matches {glob}")
    if ctx.cycle_count >= ctx.max_cycles:
        return GateDecision(True, f"cycle_at_or_above: {ctx.cycle_count}/{ctx.max_cycles}")
    for f in ctx.review_findings:
        if f.get("category") == "security" and f.get("severity") == "blocking":
            return GateDecision(True, "finding: security/blocking")
    return GateDecision(False, "")
```

- [ ] **Step 5: Implement loader** at `orchestrator/src/orchestrator/policy/loader.py`

```python
from __future__ import annotations

import hashlib

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Repo
from orchestrator.github.client import make_client
from orchestrator.policy.schema import Policy, parse_policy_yaml

POLICY_PATH = ".devops/orchestrator.yml"


async def fetch_policy_yaml(token: str, *, owner: str, repo: str, ref: str = "HEAD") -> str | None:
    async with make_client(token) as cli:
        r = await cli.get(
            f"/repos/{owner}/{repo}/contents/{POLICY_PATH}",
            headers={"Accept": "application/vnd.github.v3.raw"},
            params={"ref": ref} if ref != "HEAD" else None,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text


async def load_and_snapshot(
    session: AsyncSession, *, repo_row: Repo, token: str,
) -> Policy | None:
    owner, name = repo_row.full_name.split("/", 1)
    text = await fetch_policy_yaml(token, owner=owner, repo=name)
    if text is None:
        return None
    sha = hashlib.sha256(text.encode()).hexdigest()
    if repo_row.policy_yaml_sha == sha and repo_row.policy_snapshot_jsonb is not None:
        return parse_policy_yaml(text)
    policy = parse_policy_yaml(text)
    repo_row.policy_yaml_sha = sha
    repo_row.policy_snapshot_jsonb = policy.model_dump(mode="json")
    await session.commit()
    return policy
```

- [ ] **Step 6: Loader integration test**

```python
# orchestrator/tests/integration/test_policy_loader.py
from __future__ import annotations

import pytest
import respx
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.base import Base
from orchestrator.db.models import Repo
from orchestrator.policy.loader import load_and_snapshot


YAML = """
version: 1
intake:
  triage_label: ai-triage
  implement_label: ai-implement
  required_template_fields: [goal, acceptance_criteria, files, test_command, risk_level]
models: { interviewer: claude-sonnet-4-6, implementer: glm-5.1, reviewer: claude-opus-4-7 }
limits: { max_remediation_cycles: 3, diff_max_files: 40, diff_max_lines: 2000, per_run_timeout_h: 4 }
risk_globs: ["migrations/**"]
human_required_when: []
merge: { strategy: squash, auto_merge: true, required_codeowner_approval: true, required_checks: [ci] }
review: { comment_style: inline, ignore_categories: [], approve_when_no_findings: true }
test_command: "make test"
"""


@pytest.mark.asyncio
async def test_load_snapshot_roundtrip(session: AsyncSession) -> None:
    async with session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session.add(repo := Repo(full_name="o/r", installation_id=1))
    await session.commit()

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.get("/repos/o/r/contents/.devops/orchestrator.yml").mock(
            return_value=Response(200, content=YAML)
        )
        p = await load_and_snapshot(session, repo_row=repo, token="ghs")
        assert p is not None
        assert repo.policy_yaml_sha is not None
        assert repo.policy_snapshot_jsonb["limits"]["max_remediation_cycles"] == 3
```

- [ ] **Step 7: Run all three policy test files, expect PASS**

- [ ] **Step 8: Commit**

```bash
git add orchestrator/src/orchestrator/policy/ orchestrator/tests/unit/test_policy_*.py orchestrator/tests/integration/test_policy_loader.py
git commit -m "feat(orchestrator): policy schema + loader + gate evaluator"
```

---

## Task 13: Prompt files

**Files:** `orchestrator/src/orchestrator/prompts/{interview,implement,review,remediate}/system.md`, `orchestrator/src/orchestrator/prompts/implement/glm-fallback.md`

- [ ] **Step 1: `prompts/interview/system.md`**

```markdown
# Interview Agent — Issue Triage

You are an interviewer for a code-implementation pipeline. Your goal is to fill out a structured implementation template by asking the user clarifying questions one at a time, OR — if you already have enough information — return the completed template.

You have read access to the repository through your tools. Use them to make your questions concrete (e.g. "I see you have `pytest` configured; should the new feature live in `tests/foo/`?").

Output exactly one JSON object on the final line:

- To ask: `{"kind": "ask", "question": "<single clarifying question>"}`
- To complete: `{"kind": "complete", "template": {"goal": "...", "acceptance_criteria": ["..."], "files": ["..."], "test_command": "...", "risk_level": "low|medium|high"}}`

Rules:
- One question per turn.
- Stop after at most 12 turns. If still unclear, return `complete` with best-effort template and `risk_level: "high"`.
- Never invent acceptance criteria. If the user is vague, ask.
```

- [ ] **Step 2: `prompts/implement/system.md`**

```markdown
# Implementer Agent — Code Generation

You implement the template provided below in the cloned repository at `$PWD`. You have full edit and shell access via your tools.

Required behavior:
1. Read the template (`goal`, `acceptance_criteria`, `files`, `test_command`, `risk_level`).
2. Make the smallest set of changes that satisfies all acceptance criteria.
3. Run `${test_command}` and ensure it passes before finishing.
4. Do NOT touch `migrations/`, `auth/`, `payments/`, `infra/`, `deploy/`, or `.github/workflows/` unless explicitly listed in `files`.
5. Output a final JSON line summarising the change:

   `{"summary": "...", "files_changed": ["..."], "tests_run": "<command>", "tests_passed": true|false, "risk_notes": "..."}`

If you cannot complete the task, do not push partial garbage. Output `{"summary": "ABORTED: <reason>", "files_changed": [], "tests_run": null, "tests_passed": false}` and exit non-zero.
```

- [ ] **Step 3: `prompts/implement/glm-fallback.md`**

```markdown
# GLM-Implementer Fallback Mode

The previous attempt's tool-use output was malformed. Run in conservative single-turn mode:

- Make exactly one focused edit at a time.
- After each edit, output the file diff in a fenced ```diff block, then re-emit the final JSON line.
- Do not chain multi-step tool calls.

Same template + final JSON contract as the main implementer prompt.
```

- [ ] **Step 4: `prompts/review/system.md`**

````markdown
# Reviewer Agent — PR Review

You are reviewing a pull request. The diff is in `$PWD/.diff`, the full repo at HEAD is in `$PWD`. The original implementation template is provided.

Review for:
1. Acceptance criteria coverage.
2. Bugs (logic, off-by-one, error handling regressions).
3. Security issues (injection, auth bypass, secret leakage, unsafe deserialization).
4. Performance regressions (N+1 queries, blocking calls, inefficient algorithms).
5. Missing tests for new behavior.

Ignore stylistic preferences. Do not block on whitespace, naming, or comment phrasing.

Output a single final JSON line matching this schema:

```json
{
  "verdict": "approve" | "request_changes" | "blocking",
  "findings": [
    {
      "category": "bug" | "security" | "perf" | "tests_missing" | "style",
      "severity": "blocking" | "non_blocking",
      "path": "src/foo.py",
      "line": 42,
      "message": "...",
      "suggested_fix": "..."
    }
  ]
}
```

Rules:
- `verdict: "approve"` requires zero `severity: "blocking"` findings.
- A single security/blocking finding forces `verdict: "blocking"`.
- Skip `style` findings entirely if the policy ignores them.
````

- [ ] **Step 5: `prompts/remediate/system.md`**

```markdown
# Remediator Agent — Apply Review Findings

You are fixing the issues identified by the reviewer in the previous turn. The findings JSON is provided, plus the running diff so far and the original template.

Required behavior:
1. Address every `severity: "blocking"` finding.
2. For `non_blocking` findings, apply the suggested fix unless it conflicts with acceptance criteria.
3. Re-run `${test_command}`. If tests fail, fix the cause and re-run.
4. Output a final JSON line:

   `{"summary": "...", "addressed_findings": [<finding_ids>], "skipped_findings": [<id>: "<reason>"], "tests_passed": true|false}`

Do not introduce changes unrelated to the findings.
```

- [ ] **Step 6: Commit**

```bash
git add orchestrator/src/orchestrator/prompts/
git commit -m "feat(orchestrator): prompts (interview, implement, review, remediate)"
```

---

## Task 14: Handler — interview

**Files:** `orchestrator/src/orchestrator/worker/handlers/interview.py`, `orchestrator/tests/integration/test_handler_interview.py`

- [ ] **Step 1: Implement** at `orchestrator/src/orchestrator/worker/handlers/interview.py`

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib import resources
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.cli.parsers import parse_interview_output, parse_stream_json_token_usage
from orchestrator.cli.profile import PROFILES
from orchestrator.cli.runner import run_cli
from orchestrator.db.models import CliRun, Job, Repo, Run
from orchestrator.github.app import mint_app_jwt
from orchestrator.github.pr import PrApi
from orchestrator.github.tokens import get_installation_token
from orchestrator.sandbox.git import _run as gitrun, clone
from orchestrator.sandbox.workdir import RunSandbox
from orchestrator.settings import get_settings
from orchestrator.state.transitions import transition

log = structlog.get_logger()


def _load_prompt(name: str) -> str:
    return resources.files("orchestrator.prompts").joinpath(name).read_text()


async def handle_interview(session: AsyncSession, job: Job) -> dict[str, Any]:
    s = get_settings()
    run = await session.get(Run, job.run_id)
    repo_row = await session.get(Repo, run.repo_id)
    sb = RunSandbox(root=s.workdir_root, run_id=run.id)
    sb.ensure()

    token = await get_installation_token(
        session,
        installation_id=repo_row.installation_id,
        mint_app_jwt=lambda: mint_app_jwt(
            app_id=s.gh_app_id, private_key_path=s.gh_app_private_key_path,
        ),
    )

    if not (sb.repo_dir / ".git").exists():
        clone(sb, repo_url=f"https://github.com/{repo_row.full_name}", token=token)

    if run.codemap_md is None:
        files = gitrun(["git", "ls-files"], cwd=str(sb.repo_dir)).splitlines()[:200]
        readme = (sb.repo_dir / "README.md")
        readme_text = readme.read_text() if readme.exists() else ""
        run.codemap_md = "## Files\n" + "\n".join(files) + "\n\n## README\n" + readme_text
        await session.commit()

    prompt = sb.tmp_dir / "interview.md"
    issue = job.payload_jsonb or {}
    prompt.write_text(
        _load_prompt("interview/system.md")
        + f"\n\n## Repo Codemap\n{run.codemap_md}\n\n"
        + f"## Issue Body\n{issue.get('issue_body','')}\n\n"
        + f"## Prior Comments\n{issue.get('prior_comments','')}\n"
    )

    profile = PROFILES["interviewer"]
    log_path = sb.logs_dir / f"job-{job.id}-interview.log"
    res = await run_cli(
        argv=["claude", "-p", str(prompt), "--output-format", "json"],
        profile=profile, secret=s.anthropic_api_key, install_token=token,
        home_dir=str(sb.home_dir), cwd=str(sb.repo_dir), log_path=log_path,
    )
    usage = parse_stream_json_token_usage(res.stdout)
    session.add(CliRun(
        job_id=job.id, profile=profile.name, model=profile.model,
        ended_at=datetime.now(timezone.utc), exit_code=res.exit_code,
        token_in=usage.input_tokens, token_out=usage.output_tokens,
        log_path=str(log_path),
    ))
    await session.commit()
    if res.exit_code != 0:
        raise RuntimeError(f"interview CLI failed: {res.stderr[-500:]}")

    parsed = parse_interview_output(res.stdout)
    owner, repo = repo_row.full_name.split("/", 1)
    api = PrApi(token=token, owner=owner, repo=repo)

    if parsed.kind == "ask":
        await api.comment(issue_number=run.issue_number, body=parsed.question or "")
        return {"kind": "ask"}

    template_md = "## Implementation template\n```json\n" + json.dumps(
        parsed.template, indent=2,
    ) + "\n```"
    await api.comment(issue_number=run.issue_number, body=template_md)
    await api.remove_label(issue_number=run.issue_number, label="ai-triage")
    await api.add_label(issue_number=run.issue_number, label="ai-implement")
    await transition(session, run_id=run.id, event="interview.complete", job_id=job.id)
    return {"kind": "complete"}
```

- [ ] **Step 2: Register handler in `worker/handlers/__init__.py`** — full set imported lazily after Tasks 15–18:

```python
from __future__ import annotations

from orchestrator.worker.handlers.implement import handle_implement
from orchestrator.worker.handlers.interview import handle_interview
from orchestrator.worker.handlers.merge import handle_merge
from orchestrator.worker.handlers.remediate import handle_remediate
from orchestrator.worker.handlers.review import handle_review

HANDLERS = {
    "interview_turn": handle_interview,
    "implement": handle_implement,
    "review": handle_review,
    "remediate": handle_remediate,
    "merge": handle_merge,
}
```

(If running tests before Tasks 15–18 land, comment out missing imports and register only `interview_turn`.)

- [ ] **Step 3: Test** at `orchestrator/tests/integration/test_handler_interview.py`

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import respx
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.cli.runner import CliResult
from orchestrator.db.base import Base
from orchestrator.db.models import Job, JobKind, Repo, Run, RunState
from orchestrator.worker.handlers.interview import handle_interview


@pytest.mark.asyncio
async def test_interview_ask_posts_comment(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(
        "orchestrator.settings.Settings.workdir_root",
        property(lambda self: tmp_path / "work"), raising=False,
    )

    session.add(repo := Repo(full_name="o/r", installation_id=42))
    await session.flush()
    session.add(run := Run(repo_id=repo.id, issue_number=5, state=RunState.NEW))
    await session.flush()
    job = Job(run_id=run.id, kind=JobKind.interview_turn,
              payload_jsonb={"issue_body": "make foo do bar", "prior_comments": ""})
    session.add(job)
    await session.commit()

    fake = CliResult(
        exit_code=0,
        stdout='{"kind":"ask","question":"What test framework do you use?"}',
        stderr="", timed_out=False,
    )
    with respx.mock(base_url="https://api.github.com") as rx, \
         patch("orchestrator.worker.handlers.interview.run_cli", return_value=fake), \
         patch("orchestrator.worker.handlers.interview.clone", lambda *a, **kw: None), \
         patch("orchestrator.worker.handlers.interview.gitrun", return_value=""), \
         patch("orchestrator.worker.handlers.interview.get_installation_token",
               return_value="ghs-T"):
        rx.post("/repos/o/r/issues/5/comments").mock(return_value=Response(201, json={"id": 1}))
        out = await handle_interview(session, job)
        assert out == {"kind": "ask"}
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/worker/handlers/interview.py orchestrator/src/orchestrator/worker/handlers/__init__.py orchestrator/tests/integration/test_handler_interview.py
git commit -m "feat(orchestrator): interview handler"
```

---

## Task 15: Handler — implement

Same shape as interview. Use GLM profile. Branch from main, commit on feature branch, push, open PR. See spec §8.2 for flow.

- [ ] **Step 1: Implement** at `orchestrator/src/orchestrator/worker/handlers/implement.py`

```python
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from importlib import resources
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.cli.parsers import parse_stream_json_token_usage
from orchestrator.cli.profile import PROFILES
from orchestrator.cli.runner import run_cli
from orchestrator.db.models import CliRun, Job, Repo, Run
from orchestrator.github.app import mint_app_jwt
from orchestrator.github.pr import PrApi
from orchestrator.github.tokens import get_installation_token
from orchestrator.sandbox.git import clone, commit_all, create_branch, push
from orchestrator.sandbox.workdir import RunSandbox
from orchestrator.settings import get_settings
from orchestrator.state.transitions import transition

log = structlog.get_logger()


def _load_prompt(name: str) -> str:
    return resources.files("orchestrator.prompts").joinpath(name).read_text()


async def handle_implement(session: AsyncSession, job: Job) -> dict[str, Any]:
    s = get_settings()
    run = await session.get(Run, job.run_id)
    repo_row = await session.get(Repo, run.repo_id)
    sb = RunSandbox(root=s.workdir_root, run_id=run.id)
    sb.ensure()

    token = await get_installation_token(
        session,
        installation_id=repo_row.installation_id,
        mint_app_jwt=lambda: mint_app_jwt(
            app_id=s.gh_app_id, private_key_path=s.gh_app_private_key_path,
        ),
    )
    if not (sb.repo_dir / ".git").exists():
        clone(sb, repo_url=f"https://github.com/{repo_row.full_name}", token=token)
    branch = f"ai/{run.id}-{(job.payload_jsonb or {}).get('slug','impl')}"
    create_branch(sb, branch)

    template = (job.payload_jsonb or {}).get("template", {})
    prompt = sb.tmp_dir / "impl.md"
    prompt.write_text(
        _load_prompt("implement/system.md")
        + "\n\n## Template\n```json\n" + json.dumps(template, indent=2) + "\n```\n"
    )

    profile = PROFILES["glm-implementer"]
    log_path = sb.logs_dir / f"job-{job.id}-implement.log"
    res = await run_cli(
        argv=["claude", "-p", str(prompt), "--output-format", "stream-json",
              "--permission-mode", profile.permission_mode],
        profile=profile, secret=s.zai_api_key, install_token=token,
        home_dir=str(sb.home_dir), cwd=str(sb.repo_dir), log_path=log_path,
    )
    usage = parse_stream_json_token_usage(res.stdout)
    session.add(CliRun(
        job_id=job.id, profile=profile.name, model=profile.model,
        ended_at=datetime.now(timezone.utc), exit_code=res.exit_code,
        token_in=usage.input_tokens, token_out=usage.output_tokens,
        log_path=str(log_path),
    ))
    await session.commit()
    if res.exit_code != 0:
        raise RuntimeError(f"impl CLI failed: {res.stderr[-500:]}")

    diff = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(sb.repo_dir),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not diff:
        await transition(session, run_id=run.id, event="fail", job_id=job.id,
                         data={"reason": "no_changes"})
        return {"status": "failed", "reason": "no_changes"}

    commit_all(
        sb, message=f"feat: {template.get('goal', 'auto')}",
        author_email="orchestrator@noreply", author_name="orchestrator-bot",
    )
    push(sb, branch=branch, repo_url=f"https://github.com/{repo_row.full_name}", token=token)

    owner, repo = repo_row.full_name.split("/", 1)
    api = PrApi(token=token, owner=owner, repo=repo)
    pr_num, pr_url = await api.create_pull_request(
        title=f"feat: {template.get('goal', 'auto')}",
        body=f"Run {run.id}\n\n{template.get('goal','')}",
        head=branch, base="main",
    )
    run.pr_number = pr_num
    await session.commit()
    await transition(session, run_id=run.id, event="pr.opened", job_id=job.id,
                     data={"pr": pr_num, "url": pr_url})
    return {"pr_number": pr_num}
```

- [ ] **Step 2: Test** at `orchestrator/tests/integration/test_handler_implement.py`

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import respx
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.cli.runner import CliResult
from orchestrator.db.base import Base
from orchestrator.db.models import Job, JobKind, Repo, Run, RunState
from orchestrator.worker.handlers.implement import handle_implement


@pytest.mark.asyncio
async def test_implement_opens_pr(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session.add(repo := Repo(full_name="o/r", installation_id=42))
    await session.flush()
    session.add(run := Run(repo_id=repo.id, issue_number=9, state=RunState.READY))
    await session.flush()
    job = Job(run_id=run.id, kind=JobKind.implement,
              payload_jsonb={"slug": "feat", "template": {"goal": "x", "test_command": "true"}})
    session.add(job)
    await session.commit()

    monkeypatch.setattr(
        "orchestrator.settings.Settings.workdir_root",
        property(lambda self: tmp_path / "work"), raising=False,
    )

    fake = CliResult(exit_code=0, stdout='{"summary":"ok"}', stderr="", timed_out=False)

    with respx.mock(base_url="https://api.github.com") as rx, \
         patch("orchestrator.worker.handlers.implement.run_cli", return_value=fake), \
         patch("orchestrator.worker.handlers.implement.clone", lambda *a, **kw: None), \
         patch("orchestrator.worker.handlers.implement.create_branch", lambda *a, **kw: None), \
         patch("orchestrator.worker.handlers.implement.commit_all", lambda *a, **kw: "abc"), \
         patch("orchestrator.worker.handlers.implement.push", lambda *a, **kw: None), \
         patch("orchestrator.worker.handlers.implement.get_installation_token", return_value="ghs"), \
         patch("orchestrator.worker.handlers.implement.subprocess.run") as srun:
        srun.return_value.stdout = "M src/foo.py\n"
        rx.post("/repos/o/r/pulls").mock(
            return_value=Response(201, json={"number": 77, "html_url": "https://pr"})
        )
        out = await handle_implement(session, job)
        assert out == {"pr_number": 77}
```

- [ ] **Step 3: Run, expect PASS**

- [ ] **Step 4: Commit**

```bash
git add orchestrator/src/orchestrator/worker/handlers/implement.py orchestrator/tests/integration/test_handler_implement.py
git commit -m "feat(orchestrator): implement handler (GLM via claude CLI -> PR)"
```

---

## Task 16: Handler — review

**Files:** `orchestrator/src/orchestrator/worker/handlers/review.py`, `orchestrator/tests/integration/test_handler_review.py`

- [ ] **Step 1: Implement** at `orchestrator/src/orchestrator/worker/handlers/review.py`

```python
from __future__ import annotations

from datetime import datetime, timezone
from importlib import resources
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.cli.parsers import parse_review_output, parse_stream_json_token_usage
from orchestrator.cli.profile import PROFILES
from orchestrator.cli.runner import run_cli
from orchestrator.db.models import CliRun, Job, PolicyDecision, Repo, Run
from orchestrator.github.app import mint_app_jwt
from orchestrator.github.pr import PrApi
from orchestrator.github.tokens import get_installation_token
from orchestrator.policy.gates import GateContext, decide_human_required
from orchestrator.policy.schema import Policy
from orchestrator.sandbox.git import _authed_url, _run as gitrun
from orchestrator.sandbox.workdir import RunSandbox
from orchestrator.settings import get_settings
from orchestrator.state.transitions import transition

log = structlog.get_logger()


def _load_prompt(name: str) -> str:
    return resources.files("orchestrator.prompts").joinpath(name).read_text()


async def handle_review(session: AsyncSession, job: Job) -> dict[str, Any]:
    s = get_settings()
    run = await session.get(Run, job.run_id)
    repo_row = await session.get(Repo, run.repo_id)
    sb = RunSandbox(root=s.workdir_root, run_id=run.id)
    sb.ensure()

    token = await get_installation_token(
        session,
        installation_id=repo_row.installation_id,
        mint_app_jwt=lambda: mint_app_jwt(
            app_id=s.gh_app_id, private_key_path=s.gh_app_private_key_path,
        ),
    )
    owner, repo = repo_row.full_name.split("/", 1)
    api = PrApi(token=token, owner=owner, repo=repo)

    diff = await api.get_diff(pull_number=run.pr_number)
    review_dir = sb.tmp_dir / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    if not (review_dir / ".git").exists():
        gitrun(
            ["git", "clone", "--quiet",
             _authed_url(f"https://github.com/{repo_row.full_name}", token), "."],
            cwd=str(review_dir),
        )

    (review_dir / ".diff").write_text(diff)

    prompt = sb.tmp_dir / "review.md"
    prompt.write_text(
        _load_prompt("review/system.md") + "\n\n## Diff\n```\n" + diff + "\n```\n"
    )

    profile = PROFILES["anthropic-reviewer"]
    log_path = sb.logs_dir / f"job-{job.id}-review.log"
    res = await run_cli(
        argv=["claude", "-p", str(prompt), "--output-format", "json"],
        profile=profile, secret=s.anthropic_api_key, install_token=token,
        home_dir=str(sb.home_dir), cwd=str(review_dir), log_path=log_path,
    )
    usage = parse_stream_json_token_usage(res.stdout)
    session.add(CliRun(
        job_id=job.id, profile=profile.name, model=profile.model,
        ended_at=datetime.now(timezone.utc), exit_code=res.exit_code,
        token_in=usage.input_tokens, token_out=usage.output_tokens,
        log_path=str(log_path),
    ))
    await session.commit()
    if res.exit_code != 0:
        raise RuntimeError(f"review CLI failed: {res.stderr[-500:]}")

    parsed = parse_review_output(res.stdout)
    inline = [
        {"path": f.path, "line": f.line or 1, "body": f.message}
        for f in parsed.findings
    ]
    event_map = {"approve": "APPROVE", "request_changes": "REQUEST_CHANGES", "blocking": "REQUEST_CHANGES"}
    await api.create_review(
        pull_number=run.pr_number, event=event_map[parsed.verdict],
        body=f"Review verdict: **{parsed.verdict}**", comments=inline,
    )

    policy_dict = repo_row.policy_snapshot_jsonb or {}
    policy = Policy.model_validate(policy_dict) if policy_dict else None
    risk_globs = policy.risk_globs if policy else []
    max_cycles = policy.limits.max_remediation_cycles if policy else 3
    changed_paths = await api.list_changed_paths(pull_number=run.pr_number)

    decision = decide_human_required(GateContext(
        risk_globs=risk_globs, max_cycles=max_cycles,
        cycle_count=run.cycle_count, changed_paths=changed_paths,
        review_findings=[f.model_dump() for f in parsed.findings],
    ))
    if decision.required:
        session.add(PolicyDecision(
            run_id=run.id, gate="human_required",
            verdict="require", reason=decision.reason,
        ))
        await session.commit()
        await transition(session, run_id=run.id, event="gate.human_required", job_id=job.id,
                         data={"reason": decision.reason})
        await api.comment(issue_number=run.issue_number,
                          body=f"Human approval required: {decision.reason}")
        return {"verdict": parsed.verdict, "human_required": True, "reason": decision.reason}

    if parsed.verdict == "approve":
        await transition(session, run_id=run.id, event="review.approve", job_id=job.id)
    else:
        await transition(session, run_id=run.id, event="review.blocking", job_id=job.id)
    return {"verdict": parsed.verdict, "human_required": False}
```

- [ ] **Step 2: Test** at `orchestrator/tests/integration/test_handler_review.py`

```python
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import respx
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.cli.runner import CliResult
from orchestrator.db.base import Base
from orchestrator.db.models import Job, JobKind, Repo, Run, RunState
from orchestrator.worker.handlers.review import handle_review


POLICY = {
    "version": 1,
    "intake": {"triage_label": "ai-triage", "implement_label": "ai-implement",
               "required_template_fields": []},
    "models": {"interviewer":"x","implementer":"y","reviewer":"z"},
    "limits": {"max_remediation_cycles":3,"diff_max_files":40,"diff_max_lines":2000,"per_run_timeout_h":4},
    "risk_globs": [],
    "human_required_when": [],
    "merge": {"strategy":"squash","auto_merge":True,"required_codeowner_approval":True,"required_checks":["ci"]},
    "review": {"comment_style":"inline","ignore_categories":[],"approve_when_no_findings":True},
    "test_command": "make test",
}


@pytest.mark.asyncio
async def test_review_blocking_security_triggers_human(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(
        "orchestrator.settings.Settings.workdir_root",
        property(lambda self: tmp_path / "work"), raising=False,
    )
    session.add(repo := Repo(full_name="o/r", installation_id=42, policy_snapshot_jsonb=POLICY))
    await session.flush()
    session.add(run := Run(repo_id=repo.id, issue_number=33, state=RunState.UNDER_REVIEW, pr_number=55))
    await session.flush()
    job = Job(run_id=run.id, kind=JobKind.review)
    session.add(job)
    await session.commit()

    cli_out = json.dumps({
        "verdict": "blocking",
        "findings": [{"category":"security","severity":"blocking","path":"a.py","line":3,
                      "message":"sql injection"}],
    })
    fake = CliResult(exit_code=0, stdout=cli_out, stderr="", timed_out=False)

    with respx.mock(base_url="https://api.github.com") as rx, \
         patch("orchestrator.worker.handlers.review.run_cli", return_value=fake), \
         patch("orchestrator.worker.handlers.review.gitrun", return_value=""), \
         patch("orchestrator.worker.handlers.review.get_installation_token", return_value="ghs"):
        rx.get("/repos/o/r/pulls/55").mock(return_value=Response(200, content="diff --git a/a.py b/a.py\n"))
        rx.post("/repos/o/r/pulls/55/reviews").mock(return_value=Response(200, json={"id": 1}))
        rx.get("/repos/o/r/pulls/55/files").mock(return_value=Response(200, json=[{"filename":"a.py"}]))
        rx.post("/repos/o/r/issues/33/comments").mock(return_value=Response(201, json={"id": 1}))
        out = await handle_review(session, job)
        assert out["human_required"] is True
        assert "security" in out["reason"]
```

- [ ] **Step 3: Run, expect PASS**

- [ ] **Step 4: Commit**

```bash
git add orchestrator/src/orchestrator/worker/handlers/review.py orchestrator/tests/integration/test_handler_review.py
git commit -m "feat(orchestrator): review handler (Opus 4.7) + gate evaluation"
```

---

## Task 17: Handler — remediate

**Files:** `orchestrator/src/orchestrator/worker/handlers/remediate.py`, `orchestrator/tests/integration/test_handler_remediate.py`

- [ ] **Step 1: Implement**

```python
# orchestrator/src/orchestrator/worker/handlers/remediate.py
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from importlib import resources
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.cli.parsers import parse_stream_json_token_usage
from orchestrator.cli.profile import PROFILES
from orchestrator.cli.runner import run_cli
from orchestrator.db.models import CliRun, Job, Repo, Run
from orchestrator.github.app import mint_app_jwt
from orchestrator.github.tokens import get_installation_token
from orchestrator.sandbox.git import commit_all, push
from orchestrator.sandbox.workdir import RunSandbox
from orchestrator.settings import get_settings
from orchestrator.state.transitions import transition


def _load_prompt(name: str) -> str:
    return resources.files("orchestrator.prompts").joinpath(name).read_text()


async def handle_remediate(session: AsyncSession, job: Job) -> dict[str, Any]:
    s = get_settings()
    run = await session.get(Run, job.run_id)
    repo_row = await session.get(Repo, run.repo_id)
    sb = RunSandbox(root=s.workdir_root, run_id=run.id)
    sb.ensure()

    policy_max = (repo_row.policy_snapshot_jsonb or {}).get("limits", {}).get(
        "max_remediation_cycles", 3
    )
    if run.cycle_count >= policy_max:
        await transition(session, run_id=run.id, event="gate.human_required", job_id=job.id,
                         data={"reason": f"cycle_at_or_above:{run.cycle_count}"})
        return {"status": "human_required"}
    run.cycle_count += 1
    await session.commit()

    token = await get_installation_token(
        session,
        installation_id=repo_row.installation_id,
        mint_app_jwt=lambda: mint_app_jwt(
            app_id=s.gh_app_id, private_key_path=s.gh_app_private_key_path,
        ),
    )
    findings = (job.payload_jsonb or {}).get("findings", [])
    template = (job.payload_jsonb or {}).get("template", {})

    prompt = sb.tmp_dir / "remediate.md"
    prompt.write_text(
        _load_prompt("remediate/system.md")
        + "\n\n## Findings\n```json\n" + json.dumps(findings, indent=2) + "\n```\n"
        + "\n## Template\n```json\n" + json.dumps(template, indent=2) + "\n```\n"
    )

    profile = PROFILES["glm-implementer"]
    log_path = sb.logs_dir / f"job-{job.id}-remediate.log"
    res = await run_cli(
        argv=["claude", "-p", str(prompt), "--output-format", "stream-json",
              "--permission-mode", profile.permission_mode],
        profile=profile, secret=s.zai_api_key, install_token=token,
        home_dir=str(sb.home_dir), cwd=str(sb.repo_dir), log_path=log_path,
    )
    usage = parse_stream_json_token_usage(res.stdout)
    session.add(CliRun(
        job_id=job.id, profile=profile.name, model=profile.model,
        ended_at=datetime.now(timezone.utc), exit_code=res.exit_code,
        token_in=usage.input_tokens, token_out=usage.output_tokens,
        log_path=str(log_path),
    ))
    await session.commit()
    if res.exit_code != 0:
        raise RuntimeError(f"remediate CLI failed: {res.stderr[-500:]}")

    diff = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(sb.repo_dir),
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not diff:
        await transition(session, run_id=run.id, event="fail", job_id=job.id,
                         data={"reason": "remediate_no_changes"})
        return {"status": "failed"}

    branch = (job.payload_jsonb or {}).get("branch") or f"ai/{run.id}-impl"
    commit_all(
        sb, message=f"fix: remediate cycle {run.cycle_count}",
        author_email="orchestrator@noreply", author_name="orchestrator-bot",
    )
    push(sb, branch=branch, repo_url=f"https://github.com/{repo_row.full_name}", token=token)
    await transition(session, run_id=run.id, event="remediate.pushed", job_id=job.id,
                     data={"cycle": run.cycle_count})
    return {"cycle": run.cycle_count}
```

- [ ] **Step 2: Test** at `orchestrator/tests/integration/test_handler_remediate.py`

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.cli.runner import CliResult
from orchestrator.db.base import Base
from orchestrator.db.models import Job, JobKind, Repo, Run, RunState
from orchestrator.worker.handlers.remediate import handle_remediate


@pytest.mark.asyncio
async def test_remediate_cycle_limit_triggers_human(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(
        "orchestrator.settings.Settings.workdir_root",
        property(lambda self: tmp_path / "work"), raising=False,
    )
    policy = {"limits": {"max_remediation_cycles": 3}}
    session.add(repo := Repo(full_name="o/r", installation_id=42, policy_snapshot_jsonb=policy))
    await session.flush()
    session.add(run := Run(repo_id=repo.id, issue_number=11, state=RunState.REMEDIATING,
                           cycle_count=3, pr_number=88))
    await session.flush()
    job = Job(run_id=run.id, kind=JobKind.remediate, payload_jsonb={})
    session.add(job)
    await session.commit()

    out = await handle_remediate(session, job)
    assert out == {"status": "human_required"}
```

- [ ] **Step 3: Run, expect PASS**

- [ ] **Step 4: Commit**

```bash
git add orchestrator/src/orchestrator/worker/handlers/remediate.py orchestrator/tests/integration/test_handler_remediate.py
git commit -m "feat(orchestrator): remediate handler (cycle limit + push)"
```

---

## Task 18: Handler — merge

**Files:** `orchestrator/src/orchestrator/worker/handlers/merge.py`, `orchestrator/tests/integration/test_handler_merge.py`

- [ ] **Step 1: Implement**

```python
# orchestrator/src/orchestrator/worker/handlers/merge.py
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import Job, Repo, Run
from orchestrator.github.app import mint_app_jwt
from orchestrator.github.client import make_client
from orchestrator.github.codeowners import CodeOwners
from orchestrator.github.pr import PrApi
from orchestrator.github.tokens import get_installation_token
from orchestrator.settings import get_settings
from orchestrator.state.transitions import transition


async def _fetch_codeowners(token: str, owner: str, repo: str) -> str | None:
    async with make_client(token) as cli:
        for path in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
            r = await cli.get(
                f"/repos/{owner}/{repo}/contents/{path}",
                headers={"Accept": "application/vnd.github.v3.raw"},
            )
            if r.status_code == 200:
                return r.text
        return None


async def handle_merge(session: AsyncSession, job: Job) -> dict[str, Any]:
    s = get_settings()
    run = await session.get(Run, job.run_id)
    repo_row = await session.get(Repo, run.repo_id)
    token = await get_installation_token(
        session,
        installation_id=repo_row.installation_id,
        mint_app_jwt=lambda: mint_app_jwt(
            app_id=s.gh_app_id, private_key_path=s.gh_app_private_key_path,
        ),
    )
    owner, repo = repo_row.full_name.split("/", 1)
    api = PrApi(token=token, owner=owner, repo=repo)

    approver = (job.payload_jsonb or {}).get("approver_login")
    if not approver:
        return {"status": "noop"}

    co_text = await _fetch_codeowners(token, owner, repo)
    paths = await api.list_changed_paths(pull_number=run.pr_number)
    if co_text:
        co = CodeOwners.parse(co_text)
        uncovered = co.uncovered(paths, approvers={f"@{approver}"})
        if uncovered:
            await api.comment(
                issue_number=run.issue_number,
                body="Need owner approval for: " + ", ".join(uncovered),
            )
            return {"status": "uncovered", "paths": uncovered}

    await transition(session, run_id=run.id, event="codeowner.approved", job_id=job.id,
                     data={"approver": approver})
    method = (repo_row.policy_snapshot_jsonb or {}).get("merge", {}).get("strategy", "squash")
    sha = await api.merge(pull_number=run.pr_number, method=method)
    await transition(session, run_id=run.id, event="merge.success", job_id=job.id,
                     data={"sha": sha})
    return {"sha": sha}
```

- [ ] **Step 2: Test** at `orchestrator/tests/integration/test_handler_merge.py`

```python
from __future__ import annotations

from unittest.mock import patch

import pytest
import respx
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.base import Base
from orchestrator.db.models import Job, JobKind, Repo, Run, RunState
from orchestrator.worker.handlers.merge import handle_merge


@pytest.mark.asyncio
async def test_merge_happy_path(session: AsyncSession) -> None:
    async with session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session.add(repo := Repo(full_name="o/r", installation_id=42,
                             policy_snapshot_jsonb={"merge": {"strategy": "squash"}}))
    await session.flush()
    session.add(run := Run(repo_id=repo.id, issue_number=12, state=RunState.APPROVED, pr_number=21))
    await session.flush()
    job = Job(run_id=run.id, kind=JobKind.merge, payload_jsonb={"approver_login": "alice"})
    session.add(job)
    await session.commit()

    with respx.mock(base_url="https://api.github.com") as rx, \
         patch("orchestrator.worker.handlers.merge.get_installation_token", return_value="ghs"):
        rx.get("/repos/o/r/contents/.github/CODEOWNERS").mock(
            return_value=Response(200, content="* @alice\n")
        )
        rx.get("/repos/o/r/pulls/21/files").mock(
            return_value=Response(200, json=[{"filename": "src/foo.py"}])
        )
        rx.put("/repos/o/r/pulls/21/merge").mock(
            return_value=Response(200, json={"merged": True, "sha": "abc"})
        )
        out = await handle_merge(session, job)
        assert out == {"sha": "abc"}
```

- [ ] **Step 3: Run, expect PASS**

- [ ] **Step 4: Commit**

```bash
git add orchestrator/src/orchestrator/worker/handlers/merge.py orchestrator/tests/integration/test_handler_merge.py
git commit -m "feat(orchestrator): merge handler (codeowner check + squash merge)"
```

---

## Task 19: Webhook event router → enqueue jobs

**Files:** modify `orchestrator/src/orchestrator/api/webhooks.py`, create `orchestrator/src/orchestrator/api/webhook_router.py`, create `orchestrator/tests/integration/test_webhook_routing.py`

- [ ] **Step 1: Dispatcher**

```python
# orchestrator/src/orchestrator/api/webhook_router.py
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import JobKind, Repo, Run, RunState
from orchestrator.db.queue import enqueue


async def _ensure_repo(session: AsyncSession, full_name: str, installation_id: int) -> Repo:
    row = (await session.execute(select(Repo).where(Repo.full_name == full_name))).scalar_one_or_none()
    if row is None:
        row = Repo(full_name=full_name, installation_id=installation_id)
        session.add(row)
        await session.commit()
    return row


async def _ensure_run(session: AsyncSession, repo: Repo, issue_number: int) -> Run:
    row = (await session.execute(
        select(Run).where(Run.repo_id == repo.id, Run.issue_number == issue_number)
    )).scalar_one_or_none()
    if row is None:
        row = Run(repo_id=repo.id, issue_number=issue_number, state=RunState.NEW)
        session.add(row)
        await session.commit()
    return row


async def dispatch(session: AsyncSession, *, event: str, payload: dict[str, Any]) -> None:
    repo_payload = payload.get("repository") or {}
    inst = (payload.get("installation") or {}).get("id") or 0
    full = repo_payload.get("full_name")
    if not full:
        return
    repo = await _ensure_repo(session, full, int(inst))

    if event == "issues":
        action = payload.get("action")
        issue = payload.get("issue") or {}
        labels = {l.get("name") for l in (issue.get("labels") or [])}
        run = await _ensure_run(session, repo, int(issue.get("number")))
        if action in ("opened", "labeled") and "ai-triage" in labels and run.state == RunState.NEW:
            await enqueue(session, run_id=run.id, kind=JobKind.interview_turn,
                          payload={"issue_body": issue.get("body") or "", "prior_comments": ""})
        elif action == "labeled" and "ai-implement" in labels and run.state == RunState.READY:
            template = payload.get("issue", {}).get("body", "") or ""
            await enqueue(session, run_id=run.id, kind=JobKind.implement,
                          payload={"slug": "feat", "template": {"goal": template[:120]}})

    elif event == "issue_comment":
        action = payload.get("action")
        issue = payload.get("issue") or {}
        run = await _ensure_run(session, repo, int(issue.get("number")))
        if action == "created" and run.state == RunState.INTERVIEWING:
            await enqueue(session, run_id=run.id, kind=JobKind.interview_turn,
                          payload={"issue_body": issue.get("body") or "",
                                   "prior_comments": payload.get("comment", {}).get("body", "")})

    elif event == "pull_request":
        action = payload.get("action")
        pr = payload.get("pull_request") or {}
        run_q = (await session.execute(select(Run).where(
            Run.repo_id == repo.id, Run.pr_number == int(pr.get("number") or 0)
        ))).scalar_one_or_none()
        if run_q is None:
            return
        if action in ("opened", "synchronize") and run_q.state in (RunState.PR_OPEN, RunState.REMEDIATING):
            await enqueue(session, run_id=run_q.id, kind=JobKind.review)

    elif event == "pull_request_review":
        action = payload.get("action")
        review = payload.get("review") or {}
        if action == "submitted" and review.get("state") == "approved":
            pr = payload.get("pull_request") or {}
            run_q = (await session.execute(select(Run).where(
                Run.repo_id == repo.id, Run.pr_number == int(pr.get("number") or 0)
            ))).scalar_one_or_none()
            if run_q and run_q.state == RunState.APPROVED:
                approver = (review.get("user") or {}).get("login")
                await enqueue(session, run_id=run_q.id, kind=JobKind.merge,
                              payload={"approver_login": approver})
```

- [ ] **Step 2: Wire from webhook receiver** — append to `orchestrator/src/orchestrator/api/webhooks.py` `receive()` (after the `commit()` of the gh_webhooks insert):

```python
from orchestrator.api.webhook_router import dispatch
await dispatch(session, event=event, payload=payload)
```

- [ ] **Step 3: Test** at `orchestrator/tests/integration/test_webhook_routing.py`

```python
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from orchestrator.api.app import create_app
from orchestrator.db.base import Base
from orchestrator.db.models import Job, JobKind


@pytest_asyncio.fixture
async def app(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setenv("GH_APP_WEBHOOK_SECRET", "shh")
    return create_app()


@pytest.mark.asyncio
async def test_issues_opened_with_triage_enqueues_interview(app, session: AsyncSession) -> None:
    payload = {
        "action": "opened",
        "repository": {"full_name": "o/r"},
        "installation": {"id": 42},
        "issue": {"number": 5, "body": "do x", "labels": [{"name": "ai-triage"}]},
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cli:
        r = await cli.post("/gh/webhook", content=body,
                           headers={"X-Hub-Signature-256": sig,
                                    "X-GitHub-Event": "issues",
                                    "X-GitHub-Delivery": "d1"})
    assert r.status_code == 202
    rows = (await session.execute(select(Job))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == JobKind.interview_turn
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/api/webhooks.py orchestrator/src/orchestrator/api/webhook_router.py orchestrator/tests/integration/test_webhook_routing.py
git commit -m "feat(orchestrator): webhook -> run/job dispatcher"
```

---

## Task 20: Schema parity check

If Plan 1 already created every column needed (it did — `runs.codemap_md`, `cycle_count`, `pr_number`, `cli_runs.*`, `policy_decisions.*`, `installation_tokens.*` all in `0001_initial`), no migration needed. Verify by running `alembic upgrade head` against a fresh pg and inspecting `\d runs`. If anything is missing, add `0002_engine.py`.

- [ ] **Step 1: Verify**

```bash
docker compose -f deploy/compose/docker-compose.yml up -d postgres
DATABASE_URL=postgresql+asyncpg://orch:orch@localhost:5432/orch \
GH_APP_ID=1 GH_APP_PRIVATE_KEY_PATH=deploy/compose/gh-app.pem GH_APP_WEBHOOK_SECRET=x \
ANTHROPIC_API_KEY=x ZAI_API_KEY=x \
alembic upgrade head
psql postgres://orch:orch@localhost:5432/orch -c '\d runs'
psql postgres://orch:orch@localhost:5432/orch -c '\d jobs'
```

Confirm columns from Plan 1 spec §5 all present. If any missing, write `orchestrator/alembic/versions/0002_engine.py` adding them.

---

## Task 21: E2E fixture repo

The fixture repo `SevFle/orchestrator-e2e-fixture` is a separate GitHub repo, NOT in this codebase.

- [ ] **Step 1: Create**

```bash
gh repo create SevFle/orchestrator-e2e-fixture --public \
  --description "E2E fixture for orchestrator" --add-readme
gh repo clone SevFle/orchestrator-e2e-fixture /tmp/oef
cd /tmp/oef
```

- [ ] **Step 2: Add minimal lib + tests + CODEOWNERS + policy**

```bash
mkdir -p src tests migrations .devops .github
cat > pyproject.toml <<'EOF'
[project]
name = "fixture"
version = "0.1.0"
[project.optional-dependencies]
test = ["pytest"]
EOF
cat > src/__init__.py <<'EOF'
def add(a: int, b: int) -> int:
    return a + b
EOF
cat > tests/test_add.py <<'EOF'
from src import add

def test_add() -> None:
    assert add(2, 3) == 5
EOF
cat > Makefile <<'EOF'
.PHONY: test
test:
	pytest -q
EOF
cat > .github/CODEOWNERS <<'EOF'
* @SevFle
migrations/* @SevFle
EOF
cat > .devops/orchestrator.yml <<'EOF'
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
risk_globs: ["migrations/**"]
human_required_when:
  - any_path_matches: risk_globs
  - cycle_at_or_above: 3
  - finding: { category: security, severity: blocking }
merge:
  strategy: squash
  auto_merge: true
  required_codeowner_approval: true
  required_checks: [ci]
review:
  comment_style: inline
  ignore_categories: [style]
  approve_when_no_findings: true
test_command: "make test"
EOF

gh label create ai-triage --color "fbca04" -R SevFle/orchestrator-e2e-fixture
gh label create ai-implement --color "1d76db" -R SevFle/orchestrator-e2e-fixture
git add -A && git commit -m "init: orchestrator e2e fixture" && git push
```

- [ ] **Step 3: Install staging GH App on this repo (manual)**

In GitHub App settings, install the **staging** instance of the orchestrator app on `SevFle/orchestrator-e2e-fixture` only.

- [ ] **Step 4: Smoke E2E**

```bash
gh issue create -R SevFle/orchestrator-e2e-fixture \
  --title "Add subtract function" \
  --label ai-triage \
  --body "Goal: also export subtract(a,b)->int. Tests in tests/test_subtract.py."
```

Watch logs:

```bash
docker compose -f deploy/compose/docker-compose.yml logs -f api worker
```

Expect: webhook → interview job → comment or template completion → label flip → impl job → PR opened → review job → review comment → manual codeowner approve → merge job → squash-merged.

- [ ] **Step 5: Document in `orchestrator/README.md`** an "E2E smoke" section. Commit.

```bash
git -C /Users/sevfle/projects/sevfle/devops-toolkit add orchestrator/README.md
git -C /Users/sevfle/projects/sevfle/devops-toolkit commit -m "docs(orchestrator): E2E smoke procedure for fixture repo"
```

---

## Task 22: Open Plan 2 PR

- [ ] **Step 1: Push**

```bash
git push -u origin feat/orchestrator-engine
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "feat(orchestrator): Plan 2 — Engine" --body "$(cat <<'EOF'
Implements Plan 2 of the orchestrator rebuild.

Closes #34 once merged.

## Summary

- State machine + tx-wrapped transition + audit row
- pg-only queue (SKIP LOCKED + LISTEN/NOTIFY) + lease reaper + advisory locks
- Worker loop (claim/run/complete) with reaper and listener tasks
- Sandbox (workdir + git ops)
- CLI profile registry (interviewer / glm-implementer / anthropic-reviewer) + strict-env runner + JSON parsers
- 5 handlers (interview, implement, review, remediate, merge)
- Policy schema + loader + gate evaluator
- CODEOWNERS parser + ownership coverage
- Webhook → run/job dispatcher
- E2E smoke procedure against `SevFle/orchestrator-e2e-fixture`

## Test plan

- [ ] `make test` green (unit + integration)
- [ ] E2E loop: triage → interview → impl → review → remediate (1 cycle) → approve → merge in fixture repo
- [ ] Risk-glob (`migrations/**`) edit forces HUMAN_REQUIRED
- [ ] Cycle limit (cycle 3) forces HUMAN_REQUIRED with PolicyDecision row
- [ ] Codeowner partial coverage produces "Need owner approval for: ..." comment
- [ ] Reaper recovers leased job after worker kill

Refs: spec PR #32, Plan 1.
EOF
)"
```

- [ ] **Step 3: Verify CI** + iterate.

- [ ] **Step 4: Hand off** — Plan 3 (issue #35) starts on `feat/orchestrator-operate` once merged.

---

## Self-review

- **Spec coverage** — Plan 2 implements §4 state machine, §5 queue + advisory locks, §6 worker subset, §7 CLI profiles + sandbox + parsers, §8 all flows, §9 policy + codeowners + gates. Admin UI / OAuth / prom / Strato deploy / hardening polish are deferred to Plan 3.
- **Placeholder scan** — no TBD/TODO. Tests for handlers (review, remediate, merge) cover the most consequential branch (HUMAN_REQUIRED, cycle limit, happy merge); engineer should follow the same mock pattern when adding more cases.
- **Type consistency** — `JobKind` enum values match webhook dispatcher, handlers' registry, queue module. `Profile.permission_mode` values match `claude --permission-mode` accepted set. `RunState` events used by handlers all appear in the transitions table (`label.ai-triage`, `interview.complete`, `label.ai-implement`, `pr.opened`, `review.start`, `review.blocking`, `review.approve`, `remediate.pushed`, `codeowner.approved`, `merge.success`, `gate.human_required`, `fail`). `transition()` signature consistent across all handler call sites: `(session, *, run_id, event, job_id?, data?)`.
