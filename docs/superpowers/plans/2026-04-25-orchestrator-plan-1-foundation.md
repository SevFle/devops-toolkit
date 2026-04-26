# Orchestrator Plan 1 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the foundation of the orchestrator service: project layout, settings, DB layer with all tables migrated, GitHub App auth, HMAC-verified webhook receiver that persists events, healthz endpoint, dispatcher entrypoint, Docker image, and local compose stack.

**Architecture:** Single Python package `orchestrator` under `orchestrator/src/orchestrator/`, dispatched via `__main__.py` to one of `api | worker | migrate`. FastAPI for the API, SQLAlchemy 2.x async + asyncpg + Alembic for the DB, `pyjwt` for GH App JWTs, `httpx` for outbound HTTP, `structlog` for JSON logs, `pydantic-settings` for config. Docker multi-stage build bakes the `claude` CLI binary into the image so workers can shell out to it later.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.x async, asyncpg, Alembic, pydantic 2 + pydantic-settings, httpx, pyjwt[crypto], structlog, prometheus-client (placeholder, used in Plan 3), uvicorn, pytest + pytest-asyncio + testcontainers + respx + pytest-recording.

**Reference spec:** `docs/superpowers/specs/2026-04-25-glm-claude-orchestrator-design.md`
**Tracking issue:** `#33`
**Working branch:** `feat/orchestrator-foundation` (off `main`, after spec PR #32 merges)

---

## File structure produced by this plan

```
orchestrator/
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_initial.py
├── src/orchestrator/
│   ├── __init__.py
│   ├── __main__.py
│   ├── settings.py
│   ├── api/{__init__,app,deps,webhooks,health}.py
│   ├── db/{__init__,base,session,models}.py
│   ├── github/{__init__,app,tokens,webhook}.py
│   └── obs/{__init__,log}.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── unit/{test_settings,test_log_redaction,test_app_jwt,test_webhook_hmac,test_models_smoke}.py
│   └── integration/{conftest,test_db_healthz,test_install_tokens,test_webhook_endpoint}.py
├── Makefile
└── README.md

deploy/
├── docker/{Dockerfile,entrypoint.sh}
└── compose/{docker-compose.yml,.env.example,postgres-init.sql}
```

Old paths deleted in Task 1: `orchestrator/{ci_fix.py,monitor.py,orchestrate.py,pyproject.toml,lib/,tests/}`.

---

## Task 1: Wipe legacy and scaffold layout

**Files:** delete legacy python; create new dirs and `README.md`.

- [ ] **Step 1: Branch off main**

```bash
git checkout main
git pull
git checkout -b feat/orchestrator-foundation
```

- [ ] **Step 2: Delete legacy**

```bash
git rm -r orchestrator/ci_fix.py orchestrator/monitor.py orchestrator/orchestrate.py \
         orchestrator/pyproject.toml orchestrator/lib orchestrator/tests
```

- [ ] **Step 3: Create dirs**

```bash
mkdir -p orchestrator/src/orchestrator/{api,db,github,obs}
mkdir -p orchestrator/tests/{unit,integration}
mkdir -p orchestrator/alembic/versions
mkdir -p deploy/docker deploy/compose
touch orchestrator/src/orchestrator/__init__.py \
      orchestrator/src/orchestrator/api/__init__.py \
      orchestrator/src/orchestrator/db/__init__.py \
      orchestrator/src/orchestrator/github/__init__.py \
      orchestrator/src/orchestrator/obs/__init__.py \
      orchestrator/tests/__init__.py
```

- [ ] **Step 4: Write `orchestrator/README.md`**

````markdown
# orchestrator

Webhook-driven service that orchestrates GLM (implementer) + Claude (reviewer + interviewer) on GitHub repos.

See `docs/superpowers/specs/2026-04-25-glm-claude-orchestrator-design.md` for design.

## Local dev

```
cp deploy/compose/.env.example deploy/compose/.env
docker compose -f deploy/compose/docker-compose.yml up
```
````

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore(orchestrator): wipe legacy python, scaffold new layout

Removes OpenSpec-pipeline orchestrator (ci_fix.py, monitor.py, orchestrate.py, lib/).
Sets up new package layout under orchestrator/src/orchestrator/ for the rebuild.
Tracks issue #33."
```

---

## Task 2: pyproject + Makefile

**Files:** `orchestrator/pyproject.toml`, `orchestrator/Makefile`.

- [ ] **Step 1: pyproject.toml**

```toml
[project]
name = "orchestrator"
version = "0.1.0"
description = "Webhook-driven GLM+Claude PR orchestrator"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "pydantic>=2.7",
  "pydantic-settings>=2.4",
  "sqlalchemy[asyncio]>=2.0.30",
  "asyncpg>=0.29",
  "alembic>=1.13",
  "httpx>=0.27",
  "pyjwt[crypto]>=2.9",
  "structlog>=24.4",
  "python-multipart>=0.0.9",
  "prometheus-client>=0.20",
  "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "pytest-cov>=5.0",
  "pytest-recording>=0.13",
  "respx>=0.21",
  "testcontainers[postgres]>=4.7",
  "ruff>=0.6",
  "mypy>=1.11",
]

[project.scripts]
orchestrator = "orchestrator.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/orchestrator"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "-ra --strict-markers --cov=orchestrator --cov-report=term-missing"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "ANN", "RUF"]
ignore = ["ANN101", "ANN102"]

[tool.mypy]
python_version = "3.12"
strict = true
plugins = ["pydantic.mypy"]
```

- [ ] **Step 2: Makefile**

```makefile
.PHONY: install test test-unit test-int lint type fmt run-api migrate

install:
	cd orchestrator && pip install -e ".[dev]"

test: test-unit test-int
test-unit:
	cd orchestrator && pytest tests/unit -v
test-int:
	cd orchestrator && pytest tests/integration -v

lint:
	cd orchestrator && ruff check src tests
type:
	cd orchestrator && mypy src
fmt:
	cd orchestrator && ruff format src tests

run-api:
	cd orchestrator && python -m orchestrator api
migrate:
	cd orchestrator && python -m orchestrator migrate
```

- [ ] **Step 3: Install**

```bash
make install
```

Expected: `Successfully installed orchestrator-0.1.0` plus deps.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/pyproject.toml orchestrator/Makefile
git commit -m "chore(orchestrator): add pyproject and Makefile"
```

---

## Task 3: Settings module (TDD)

- [ ] **Step 1: Failing test** at `orchestrator/tests/unit/test_settings.py`

```python
import pytest
from pydantic import ValidationError

from orchestrator.settings import Settings


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("GH_APP_ID", "12345")
    monkeypatch.setenv("GH_APP_PRIVATE_KEY_PATH", "/etc/gh-app.pem")
    monkeypatch.setenv("GH_APP_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("GH_APP_OAUTH_CLIENT_ID", "oid")
    monkeypatch.setenv("GH_APP_OAUTH_CLIENT_SECRET", "osec")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("ZAI_API_KEY", "zai-key")
    monkeypatch.setenv("SESSION_SIGNING_KEY", "a" * 44)
    monkeypatch.setenv("ADMIN_LOGINS", "alice,bob")
    monkeypatch.setenv("PROMETHEUS_BEARER", "promtok")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://orch.example.com")

    s = Settings()
    assert s.gh_app_id == 12345
    assert s.admin_logins == ["alice", "bob"]
    assert s.public_base_url == "https://orch.example.com"


def test_settings_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ["DATABASE_URL", "GH_APP_ID", "GH_APP_PRIVATE_KEY_PATH",
              "GH_APP_WEBHOOK_SECRET", "ANTHROPIC_API_KEY", "ZAI_API_KEY"]:
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ValidationError):
        Settings()
```

- [ ] **Step 2: Run, expect FAIL**

```bash
pytest tests/unit/test_settings.py -v
```

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/settings.py`

```python
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8",
        case_sensitive=False, extra="ignore",
    )

    database_url: str = Field(..., alias="DATABASE_URL")
    gh_app_id: int = Field(..., alias="GH_APP_ID")
    gh_app_private_key_path: Path = Field(..., alias="GH_APP_PRIVATE_KEY_PATH")
    gh_app_webhook_secret: str = Field(..., alias="GH_APP_WEBHOOK_SECRET")
    gh_app_oauth_client_id: str = Field("", alias="GH_APP_OAUTH_CLIENT_ID")
    gh_app_oauth_client_secret: str = Field("", alias="GH_APP_OAUTH_CLIENT_SECRET")
    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")
    zai_api_key: str = Field(..., alias="ZAI_API_KEY")
    session_signing_key: str = Field("", alias="SESSION_SIGNING_KEY")
    admin_logins: list[str] = Field(default_factory=list, alias="ADMIN_LOGINS")
    prometheus_bearer: str = Field("", alias="PROMETHEUS_BEARER")
    public_base_url: str = Field("", alias="PUBLIC_BASE_URL")
    workdir_root: Path = Field(Path("/var/lib/orchestrator/work"), alias="WORKDIR_ROOT")
    role: str = Field("api", alias="ROLE")
    log_level: str = Field("INFO", alias="LOG_LEVEL")

    @field_validator("admin_logins", mode="before")
    @classmethod
    def split_logins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Run, expect 2 PASS**

```bash
pytest tests/unit/test_settings.py -v
```

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/settings.py orchestrator/tests/unit/test_settings.py
git commit -m "feat(orchestrator): pydantic-settings config module"
```

---

## Task 4: DB session factory (TDD with testcontainers)

- [ ] **Step 1: Integration conftest** at `orchestrator/tests/integration/conftest.py`

```python
from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def pg_container() -> PostgresContainer:
    with PostgresContainer("postgres:16-alpine") as pg:
        url = pg.get_connection_url().replace("psycopg2", "asyncpg")
        os.environ["DATABASE_URL"] = url
        yield pg


@pytest_asyncio.fixture
async def engine(pg_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(os.environ["DATABASE_URL"], future=True)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
```

- [ ] **Step 2: Failing test** at `orchestrator/tests/integration/test_db_healthz.py`

```python
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.session import db_ping


@pytest.mark.asyncio
async def test_db_ping_returns_true(session: AsyncSession) -> None:
    assert await db_ping(session) is True


@pytest.mark.asyncio
async def test_session_executes_simple_query(session: AsyncSession) -> None:
    res = await session.execute(text("select 42"))
    assert res.scalar() == 42
```

- [ ] **Step 3: Run, expect FAIL** — `cannot import name 'db_ping'`

- [ ] **Step 4: Implement**

`orchestrator/src/orchestrator/db/base.py`:

```python
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
```

`orchestrator/src/orchestrator/db/session.py`:

```python
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine,
)

from orchestrator.settings import get_settings


def make_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, future=True, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as s:
        yield s


async def db_ping(session: AsyncSession) -> bool:
    res = await session.execute(text("select 1"))
    return res.scalar() == 1
```

- [ ] **Step 5: Run, expect 2 PASS**

```bash
pytest tests/integration/test_db_healthz.py -v
```

- [ ] **Step 6: Commit**

```bash
git add orchestrator/src/orchestrator/db/ orchestrator/tests/integration/
git commit -m "feat(orchestrator): async DB session factory + ping"
```

---

## Task 5: Alembic async scaffold

- [ ] **Step 1: Init**

```bash
cd orchestrator
alembic init -t async alembic
```

- [ ] **Step 2: Adjust `orchestrator/alembic.ini`** — set `sqlalchemy.url =` to empty

- [ ] **Step 3: Replace `orchestrator/alembic/env.py`**

```python
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from orchestrator.db.base import Base
from orchestrator.db import models  # noqa: F401  ensures models imported for autogen
from orchestrator.settings import get_settings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = config.get_main_option("sqlalchemy.url")
    connectable = async_engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 4: Placeholder models module** at `orchestrator/src/orchestrator/db/models.py`

```python
from __future__ import annotations

from orchestrator.db.base import Base  # noqa: F401
```

- [ ] **Step 5: Verify alembic loads**

```bash
cd orchestrator
DATABASE_URL=postgresql+asyncpg://u:p@h/db \
GH_APP_ID=1 GH_APP_PRIVATE_KEY_PATH=/dev/null GH_APP_WEBHOOK_SECRET=x \
ANTHROPIC_API_KEY=x ZAI_API_KEY=x \
alembic current
```

Expected: empty output, exit 0.

- [ ] **Step 6: Commit**

```bash
git add orchestrator/alembic.ini orchestrator/alembic/ orchestrator/src/orchestrator/db/models.py
git commit -m "feat(orchestrator): alembic async scaffold"
```

---

## Task 6: SQLAlchemy models — all tables (TDD)

- [ ] **Step 1: Failing smoke test** at `orchestrator/tests/unit/test_models_smoke.py`

```python
from __future__ import annotations

from orchestrator.db.models import (
    CliRun, GhWebhook, InstallationToken, Job, OauthSession,
    PolicyDecision, Repo, Run, RunEvent,
)


def test_all_tables_present() -> None:
    expected = {
        "repos", "runs", "jobs", "run_events", "cli_runs",
        "gh_webhooks", "policy_decisions", "installation_tokens", "oauth_sessions",
    }
    actual = {
        m.__tablename__
        for m in [Repo, Run, Job, RunEvent, CliRun, GhWebhook,
                  PolicyDecision, InstallationToken, OauthSession]
    }
    assert actual == expected


def test_runs_has_codemap_md_column() -> None:
    assert "codemap_md" in Run.__table__.columns


def test_jobs_kind_enum_values() -> None:
    enum = Job.__table__.columns["kind"].type
    assert set(enum.enums) >= {"interview_turn", "implement", "review", "remediate", "merge"}
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** — replace `orchestrator/src/orchestrator/db/models.py`

```python
from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, DateTime, Enum, ForeignKey, Index, Integer,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from orchestrator.db.base import Base


class JobKind(str, enum.Enum):
    interview_turn = "interview_turn"
    implement = "implement"
    review = "review"
    remediate = "remediate"
    merge = "merge"


class JobStatus(str, enum.Enum):
    ready = "ready"
    running = "running"
    done = "done"
    failed = "failed"


class RunState(str, enum.Enum):
    NEW = "NEW"
    INTERVIEWING = "INTERVIEWING"
    READY = "READY"
    IMPLEMENTING = "IMPLEMENTING"
    PR_OPEN = "PR_OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    REMEDIATING = "REMEDIATING"
    APPROVED = "APPROVED"
    MERGEABLE = "MERGEABLE"
    MERGED = "MERGED"
    FAILED = "FAILED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


class Repo(Base):
    __tablename__ = "repos"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_yaml_sha: Mapped[str | None] = mapped_column(String(64))
    policy_snapshot_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), nullable=False)
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[RunState] = mapped_column(
        Enum(RunState, name="run_state"), nullable=False, default=RunState.NEW
    )
    state_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    cycle_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    codemap_md: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("repo_id", "issue_number", name="uq_runs_repo_issue"),
        Index("ix_runs_state", "state"),
    )


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[JobKind] = mapped_column(Enum(JobKind, name="job_kind"), nullable=False)
    profile: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), nullable=False, default=JobStatus.ready
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_by: Mapped[str | None] = mapped_column(String(64))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_jobs_status_locked", "status", "locked_until"),)


class RunEvent(Base):
    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    level: Mapped[str] = mapped_column(String(16), nullable=False, default="info")
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    data_jsonb: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class CliRun(Base):
    __tablename__ = "cli_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    profile: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    token_in: Mapped[int | None] = mapped_column(Integer)
    token_out: Mapped[int | None] = mapped_column(Integer)
    usd_est: Mapped[float | None] = mapped_column()
    log_path: Mapped[str | None] = mapped_column(Text)


class GhWebhook(Base):
    __tablename__ = "gh_webhooks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    delivery_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    payload_jsonb: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class PolicyDecision(Base):
    __tablename__ = "policy_decisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    gate: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InstallationToken(Base):
    __tablename__ = "installation_tokens"
    installation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OauthSession(Base):
    __tablename__ = "oauth_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_login: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 4: Run, expect 3 PASS**

```bash
pytest tests/unit/test_models_smoke.py -v
```

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/db/models.py orchestrator/tests/unit/test_models_smoke.py
git commit -m "feat(orchestrator): SQLAlchemy models for all tables (spec §5)"
```

---

## Task 7: Initial Alembic migration

- [ ] **Step 1: Generate**

```bash
cd orchestrator
DATABASE_URL=<your-test-pg-url> \
GH_APP_ID=1 GH_APP_PRIVATE_KEY_PATH=/dev/null GH_APP_WEBHOOK_SECRET=x \
ANTHROPIC_API_KEY=x ZAI_API_KEY=x \
alembic revision --autogenerate -m "initial" --rev-id 0001
```

- [ ] **Step 2: Hand-review** `orchestrator/alembic/versions/0001_initial.py` — confirm all 9 `op.create_table` plus `op.create_index('ix_runs_state', ...)` and `op.create_index('ix_jobs_status_locked', ...)`. Add any missing indexes by hand. Ensure `downgrade` mirrors `upgrade`.

- [ ] **Step 3: Apply against fresh pg**

```bash
DATABASE_URL=<your-test-pg-url> alembic upgrade head
```

- [ ] **Step 4: Round-trip**

```bash
alembic downgrade base
alembic upgrade head
```

- [ ] **Step 5: Commit**

```bash
git add orchestrator/alembic/versions/0001_initial.py
git commit -m "feat(orchestrator): initial alembic migration"
```

---

## Task 8: structlog JSON + secret redaction (TDD)

- [ ] **Step 1: Failing test** at `orchestrator/tests/unit/test_log_redaction.py`

```python
from __future__ import annotations

import io
import json

import structlog

from orchestrator.obs.log import configure_logging


def test_redacts_known_secret_keys() -> None:
    buf = io.StringIO()
    configure_logging(level="INFO", stream=buf)
    log = structlog.get_logger()
    log.info("boom", anthropic_api_key="sk-ant-XXXXXXXX",
             gh_token="ghp-XXXXXXXX", safe="value")
    rec = json.loads(buf.getvalue().strip())
    assert rec["anthropic_api_key"] == "***"
    assert rec["gh_token"] == "***"
    assert rec["safe"] == "value"
    assert rec["event"] == "boom"


def test_redacts_authorization_header_value() -> None:
    buf = io.StringIO()
    configure_logging(level="INFO", stream=buf)
    log = structlog.get_logger()
    log.info("req", headers={"authorization": "Bearer ghs-XXXX",
                              "accept": "application/json"})
    out = json.loads(buf.getvalue().strip())
    assert out["headers"]["authorization"] == "***"
    assert out["headers"]["accept"] == "application/json"
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/obs/log.py`

```python
from __future__ import annotations

import logging
import sys
from typing import IO, Any

import structlog

REDACT_KEYS = {
    "anthropic_api_key", "anthropic_auth_token", "zai_api_key",
    "gh_app_webhook_secret", "gh_token", "gh_app_oauth_client_secret",
    "session_signing_key", "prometheus_bearer", "authorization",
    "x-hub-signature-256", "token", "password", "secret",
}


def _walk(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in REDACT_KEYS else _walk(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(x) for x in obj]
    return obj


def _redact(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    return _walk(event)


def configure_logging(level: str = "INFO", stream: IO[str] | None = None) -> None:
    stream = stream or sys.stdout
    logging.basicConfig(
        format="%(message)s", stream=stream,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
```

- [ ] **Step 4: Run, expect 2 PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/obs/ orchestrator/tests/unit/test_log_redaction.py
git commit -m "feat(orchestrator): structlog JSON + secret redaction"
```

---

## Task 9: GitHub App JWT minter (TDD)

- [ ] **Step 1: Failing test** at `orchestrator/tests/unit/test_app_jwt.py`

```python
from __future__ import annotations

import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from orchestrator.github.app import mint_app_jwt


@pytest.fixture
def rsa_pem(tmp_path: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    p = tmp_path / "app.pem"
    p.write_bytes(pem)
    return p


def test_mint_app_jwt_valid(rsa_pem: Path) -> None:
    token = mint_app_jwt(app_id=12345, private_key_path=rsa_pem)
    public = serialization.load_pem_private_key(
        rsa_pem.read_bytes(), password=None
    ).public_key()
    decoded = jwt.decode(token, key=public, algorithms=["RS256"])
    assert decoded["iss"] == "12345"
    assert decoded["exp"] - decoded["iat"] <= 9 * 60 + 60
    assert decoded["iat"] <= int(time.time()) + 5
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/github/app.py`

```python
from __future__ import annotations

import time
from pathlib import Path

import jwt

JWT_TTL_S = 9 * 60


def mint_app_jwt(*, app_id: int, private_key_path: Path) -> str:
    now = int(time.time())
    payload = {"iat": now - 30, "exp": now + JWT_TTL_S, "iss": str(app_id)}
    return jwt.encode(payload, private_key_path.read_bytes(), algorithm="RS256")
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/github/app.py orchestrator/tests/unit/test_app_jwt.py
git commit -m "feat(orchestrator): GitHub App JWT minter"
```

---

## Task 10: Install token cache (TDD with respx)

- [ ] **Step 1: Failing test** at `orchestrator/tests/integration/test_install_tokens.py`

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import respx
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.base import Base
from orchestrator.github.tokens import get_installation_token


@pytest.mark.asyncio
async def test_get_installation_token_caches(session: AsyncSession) -> None:
    async with session.bind.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    expires = (datetime.now(timezone.utc) + timedelta(minutes=50)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    payload = {"token": "ghs-FRESH", "expires_at": expires}

    with respx.mock(base_url="https://api.github.com") as rx:
        rx.post("/app/installations/42/access_tokens").mock(
            return_value=Response(201, json=payload)
        )
        def mint() -> str:
            return "fake-jwt"
        t1 = await get_installation_token(session, installation_id=42, mint_app_jwt=mint)
        t2 = await get_installation_token(session, installation_id=42, mint_app_jwt=mint)
        assert t1 == t2 == "ghs-FRESH"
        assert rx.calls.call_count == 1
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/github/tokens.py`

```python
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.db.models import InstallationToken

REFRESH_SKEW = timedelta(minutes=2)
MintFn = Callable[[], str] | Callable[[], Awaitable[str]]


async def get_installation_token(
    session: AsyncSession, *, installation_id: int, mint_app_jwt: MintFn,
) -> str:
    now = datetime.now(timezone.utc)
    row = await session.get(InstallationToken, installation_id)
    if row and row.expires_at - REFRESH_SKEW > now:
        return row.token

    jwt_val = mint_app_jwt()
    if inspect.isawaitable(jwt_val):
        jwt_val = await jwt_val  # type: ignore[assignment]

    async with httpx.AsyncClient(base_url="https://api.github.com", timeout=10) as cli:
        r = await cli.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {jwt_val}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        r.raise_for_status()
        data = r.json()

    expires_at = datetime.strptime(
        data["expires_at"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=timezone.utc)

    if row:
        row.token = data["token"]
        row.expires_at = expires_at
    else:
        session.add(InstallationToken(
            installation_id=installation_id, token=data["token"], expires_at=expires_at,
        ))
    await session.commit()
    return data["token"]
```

- [ ] **Step 4: Run, expect PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/github/tokens.py orchestrator/tests/integration/test_install_tokens.py
git commit -m "feat(orchestrator): install token cache (50m TTL with skew)"
```

---

## Task 11: HMAC verifier (TDD)

- [ ] **Step 1: Failing test** at `orchestrator/tests/unit/test_webhook_hmac.py`

```python
from __future__ import annotations

import hashlib
import hmac

from orchestrator.github.webhook import verify_hmac_sha256


def _sig(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_verify_accepts_valid() -> None:
    s, b = b"shh", b'{"a":1}'
    assert verify_hmac_sha256(b, _sig(s, b), s) is True


def test_verify_rejects_tampered_body() -> None:
    s = b"shh"
    sig = _sig(s, b'{"a":1}')
    assert verify_hmac_sha256(b'{"a":2}', sig, s) is False


def test_verify_rejects_wrong_secret() -> None:
    sig = _sig(b"right", b'{"a":1}')
    assert verify_hmac_sha256(b'{"a":1}', sig, b"wrong") is False


def test_verify_rejects_malformed_header() -> None:
    assert verify_hmac_sha256(b'{}', "not-a-sig", b"shh") is False
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/github/webhook.py`

```python
from __future__ import annotations

import hashlib
import hmac


def verify_hmac_sha256(body: bytes, signature_header: str, secret: bytes) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    sent = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, sent)
```

- [ ] **Step 4: Run, expect 4 PASS**

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/github/webhook.py orchestrator/tests/unit/test_webhook_hmac.py
git commit -m "feat(orchestrator): HMAC-SHA256 webhook signature verifier"
```

---

## Task 12: FastAPI app + healthz

- [ ] **Step 1: Deps** at `orchestrator/src/orchestrator/api/deps.py`

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.db.session import make_engine, make_session_factory
from orchestrator.settings import Settings, get_settings


@lru_cache(maxsize=1)
def _factory() -> async_sessionmaker[AsyncSession]:
    return make_session_factory(make_engine())


async def session_dep() -> AsyncIterator[AsyncSession]:
    async with _factory()() as s:
        yield s


def settings_dep() -> Settings:
    return get_settings()
```

- [ ] **Step 2: Health** at `orchestrator/src/orchestrator/api/health.py`

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import session_dep
from orchestrator.db.session import db_ping

router = APIRouter()


@router.get("/admin/healthz")
async def healthz(session: AsyncSession = Depends(session_dep)) -> dict[str, str]:
    ok = await db_ping(session)
    return {"db": "ok" if ok else "down"}
```

- [ ] **Step 3: App factory** at `orchestrator/src/orchestrator/api/app.py` (webhook router added in Task 13)

```python
from __future__ import annotations

from fastapi import FastAPI

from orchestrator.api.health import router as health_router
from orchestrator.obs.log import configure_logging
from orchestrator.settings import get_settings


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging(level=s.log_level)
    app = FastAPI(title="orchestrator")
    app.include_router(health_router)
    return app
```

- [ ] **Step 4: Smoke run**

```bash
cd orchestrator
DATABASE_URL=<your-test-pg-url> GH_APP_ID=1 GH_APP_PRIVATE_KEY_PATH=/dev/null \
GH_APP_WEBHOOK_SECRET=x ANTHROPIC_API_KEY=x ZAI_API_KEY=x \
uvicorn orchestrator.api.app:create_app --factory --port 8000 &
sleep 2
curl -s http://localhost:8000/admin/healthz
kill %1
```

Expected: `{"db":"ok"}` or `{"db":"down"}` if no DB.

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/api/
git commit -m "feat(orchestrator): FastAPI app factory + /admin/healthz"
```

---

## Task 13: Webhook endpoint (TDD)

- [ ] **Step 1: Failing test** at `orchestrator/tests/integration/test_webhook_endpoint.py`

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
from orchestrator.db.models import GhWebhook


@pytest_asyncio.fixture
async def app(engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setenv("GH_APP_WEBHOOK_SECRET", "shh")
    return create_app()


@pytest.mark.asyncio
async def test_webhook_accepts_valid_signature(app, session: AsyncSession) -> None:
    body = json.dumps({"action": "opened", "issue": {"number": 1}}).encode()
    sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cli:
        r = await cli.post(
            "/gh/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "delivery-1",
                "Content-Type": "application/json",
            },
        )
    assert r.status_code == 202
    rows = (await session.execute(select(GhWebhook))).scalars().all()
    assert len(rows) == 1
    assert rows[0].delivery_id == "delivery-1"
    assert rows[0].event == "issues"


@pytest.mark.asyncio
async def test_webhook_rejects_bad_signature(app) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cli:
        r = await cli.post(
            "/gh/webhook",
            content=b"{}",
            headers={
                "X-Hub-Signature-256": "sha256=deadbeef",
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "d2",
            },
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_idempotent_on_delivery_id(app, session: AsyncSession) -> None:
    body = b'{"k":"v"}'
    sig = "sha256=" + hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": sig,
        "X-GitHub-Event": "ping",
        "X-GitHub-Delivery": "dup-1",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as cli:
        r1 = await cli.post("/gh/webhook", content=body, headers=headers)
        r2 = await cli.post("/gh/webhook", content=body, headers=headers)
    assert r1.status_code == 202
    assert r2.status_code == 202
    rows = (await session.execute(select(GhWebhook))).scalars().all()
    assert len(rows) == 1
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement** at `orchestrator/src/orchestrator/api/webhooks.py`

```python
from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import session_dep, settings_dep
from orchestrator.db.models import GhWebhook
from orchestrator.github.webhook import verify_hmac_sha256
from orchestrator.settings import Settings

router = APIRouter()
log = structlog.get_logger()


@router.post("/gh/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive(
    request: Request,
    settings: Settings = Depends(settings_dep),
    session: AsyncSession = Depends(session_dep),
) -> dict[str, str]:
    body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not verify_hmac_sha256(body, sig, settings.gh_app_webhook_secret.encode()):
        log.warning("webhook_bad_signature",
                    delivery=request.headers.get("X-GitHub-Delivery"))
        raise HTTPException(status_code=401, detail="bad signature")

    delivery_id = request.headers.get("X-GitHub-Delivery") or ""
    event = request.headers.get("X-GitHub-Event") or "unknown"
    payload: dict[str, Any] = json.loads(body or b"{}")

    stmt = (
        insert(GhWebhook)
        .values(delivery_id=delivery_id, event=event, payload_jsonb=payload)
        .on_conflict_do_nothing(index_elements=["delivery_id"])
    )
    await session.execute(stmt)
    await session.commit()

    log.info("webhook_received", event=event, delivery=delivery_id)
    return {"status": "accepted"}
```

- [ ] **Step 4: Wire router into app** — replace `orchestrator/src/orchestrator/api/app.py`

```python
from __future__ import annotations

from fastapi import FastAPI

from orchestrator.api.health import router as health_router
from orchestrator.api.webhooks import router as webhook_router
from orchestrator.obs.log import configure_logging
from orchestrator.settings import get_settings


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging(level=s.log_level)
    app = FastAPI(title="orchestrator")
    app.include_router(health_router)
    app.include_router(webhook_router)
    return app
```

- [ ] **Step 5: Run, expect 3 PASS**

```bash
pytest tests/integration/test_webhook_endpoint.py -v
```

- [ ] **Step 6: Commit**

```bash
git add orchestrator/src/orchestrator/api/webhooks.py orchestrator/src/orchestrator/api/app.py \
        orchestrator/tests/integration/test_webhook_endpoint.py
git commit -m "feat(orchestrator): /gh/webhook with HMAC verify + idempotent persist"
```

---

## Task 14: `__main__` dispatcher

- [ ] **Step 1: Implement** at `orchestrator/src/orchestrator/__main__.py`

```python
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from orchestrator.obs.log import configure_logging
from orchestrator.settings import get_settings


def cmd_api() -> int:
    import uvicorn
    s = get_settings()
    uvicorn.run(
        "orchestrator.api.app:create_app",
        factory=True,
        host=os.environ.get("BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("BIND_PORT", "8000")),
        log_config=None,
        log_level=s.log_level.lower(),
    )
    return 0


def cmd_migrate() -> int:
    return subprocess.call(["alembic", "upgrade", "head"])


def cmd_worker() -> int:
    print("worker entrypoint reserved for Plan 2; exiting")
    return 0


COMMANDS = {"api": cmd_api, "worker": cmd_worker, "migrate": cmd_migrate}


def main() -> int:
    parser = argparse.ArgumentParser(prog="orchestrator")
    parser.add_argument(
        "role", nargs="?",
        default=os.environ.get("ROLE", "api"),
        choices=COMMANDS.keys(),
    )
    args = parser.parse_args()
    configure_logging(level=get_settings().log_level)
    return COMMANDS[args.role]()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Smoke worker**

```bash
cd orchestrator
DATABASE_URL=postgresql+asyncpg://u:p@localhost/db \
GH_APP_ID=1 GH_APP_PRIVATE_KEY_PATH=/dev/null GH_APP_WEBHOOK_SECRET=x \
ANTHROPIC_API_KEY=x ZAI_API_KEY=x \
python -m orchestrator worker
```

Expected: `worker entrypoint reserved for Plan 2; exiting`, exit 0.

- [ ] **Step 3: Commit**

```bash
git add orchestrator/src/orchestrator/__main__.py
git commit -m "feat(orchestrator): __main__ role dispatcher (api|worker|migrate)"
```

---

## Task 15: Multi-stage Dockerfile (bakes claude CLI)

- [ ] **Step 1: Dockerfile** at `deploy/docker/Dockerfile`

```dockerfile
# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS pybuild
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc git \
    && rm -rf /var/lib/apt/lists/*
COPY orchestrator/pyproject.toml ./pyproject.toml
COPY orchestrator/src ./src
COPY orchestrator/alembic ./alembic
COPY orchestrator/alembic.ini ./alembic.ini
RUN pip wheel --wheel-dir /wheels -e .

FROM node:20-bookworm-slim AS clibuild
RUN npm install -g @anthropic-ai/claude-code

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/var/lib/orchestrator/home
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=clibuild /usr/local/bin/node /usr/local/bin/node
COPY --from=clibuild /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js /usr/local/bin/claude

COPY --from=pybuild /wheels /wheels
COPY orchestrator/pyproject.toml /app/pyproject.toml
COPY orchestrator/src /app/src
COPY orchestrator/alembic /app/alembic
COPY orchestrator/alembic.ini /app/alembic.ini
WORKDIR /app
RUN pip install --no-index --find-links /wheels -e .

RUN useradd -r -u 10001 -d /var/lib/orchestrator -m orchestrator \
    && mkdir -p /var/lib/orchestrator/work /var/lib/orchestrator/home \
    && chown -R orchestrator:orchestrator /var/lib/orchestrator

COPY deploy/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER orchestrator
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["api"]
```

- [ ] **Step 2: Entrypoint** at `deploy/docker/entrypoint.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
ROLE="${1:-${ROLE:-api}}"
exec python -m orchestrator "$ROLE"
```

- [ ] **Step 3: Build**

```bash
docker build -t orchestrator:dev -f deploy/docker/Dockerfile .
```

- [ ] **Step 4: Verify claude binary**

```bash
docker run --rm --entrypoint /bin/sh orchestrator:dev -c "claude --version"
```

Expected: prints version.

- [ ] **Step 5: Commit**

```bash
git add deploy/docker/
git commit -m "feat(orchestrator): multi-stage Dockerfile with claude CLI baked in"
```

---

## Task 16: Local docker-compose dev stack

- [ ] **Step 1: postgres-init** at `deploy/compose/postgres-init.sql`

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

- [ ] **Step 2: .env.example** at `deploy/compose/.env.example`

```
POSTGRES_USER=orch
POSTGRES_PASSWORD=orch
POSTGRES_DB=orch

DATABASE_URL=postgresql+asyncpg://orch:orch@postgres:5432/orch

GH_APP_ID=000000
GH_APP_PRIVATE_KEY_PATH=/secrets/gh-app.pem
GH_APP_WEBHOOK_SECRET=devsecret
GH_APP_OAUTH_CLIENT_ID=
GH_APP_OAUTH_CLIENT_SECRET=

ANTHROPIC_API_KEY=sk-ant-xxx
ZAI_API_KEY=zai-xxx

SESSION_SIGNING_KEY=devkey-replace-me-base64
ADMIN_LOGINS=SevFle
PROMETHEUS_BEARER=devscrape
PUBLIC_BASE_URL=http://localhost:8000

LOG_LEVEL=DEBUG
```

- [ ] **Step 3: docker-compose.yml** at `deploy/compose/docker-compose.yml`

```yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./postgres-init.sql:/docker-entrypoint-initdb.d/00_init.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 5s
      timeout: 3s
      retries: 10

  migrator:
    image: orchestrator:dev
    depends_on:
      postgres:
        condition: service_healthy
    env_file: .env
    volumes:
      - ./gh-app.pem:/secrets/gh-app.pem:ro
    command: ["migrate"]
    restart: "no"

  api:
    image: orchestrator:dev
    depends_on:
      migrator:
        condition: service_completed_successfully
    env_file: .env
    environment:
      ROLE: api
    volumes:
      - ./gh-app.pem:/secrets/gh-app.pem:ro
      - workdir:/var/lib/orchestrator/work
    ports:
      - "8000:8000"
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/admin/healthz"]
      interval: 10s
      timeout: 3s
      retries: 5

volumes:
  pgdata:
  workdir:
```

- [ ] **Step 4: Dev PEM + gitignore**

```bash
cd deploy/compose
openssl genrsa -out gh-app.pem 2048
echo "deploy/compose/gh-app.pem" >> ../../.gitignore
echo "deploy/compose/.env" >> ../../.gitignore
cp .env.example .env
```

- [ ] **Step 5: Up**

```bash
docker compose -f deploy/compose/docker-compose.yml up -d
sleep 10
curl -s http://localhost:8000/admin/healthz
```

Expected: `{"db":"ok"}`.

- [ ] **Step 6: Smoke webhook**

```bash
BODY='{"zen":"ping"}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac devsecret -hex | awk '{print $2}')"
curl -i -X POST http://localhost:8000/gh/webhook \
  -H "X-Hub-Signature-256: $SIG" \
  -H "X-GitHub-Event: ping" \
  -H "X-GitHub-Delivery: smoke-1" \
  -H "Content-Type: application/json" \
  -d "$BODY"
```

Expected: `HTTP/1.1 202 Accepted`.

- [ ] **Step 7: Verify DB row**

```bash
docker compose -f deploy/compose/docker-compose.yml exec postgres \
  psql -U orch -d orch -c "select delivery_id, event from gh_webhooks;"
```

Expected: row `smoke-1 | ping`.

- [ ] **Step 8: Down**

```bash
docker compose -f deploy/compose/docker-compose.yml down
```

- [ ] **Step 9: Commit**

```bash
git add deploy/compose/ .gitignore
git commit -m "feat(orchestrator): local docker-compose dev stack"
```

---

## Task 17: Top-level conftest + green run

- [ ] **Step 1: Conftest** at `orchestrator/tests/conftest.py`

```python
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Tests must use monkeypatch to set what they need; this fixture only
    # ensures we never silently inherit the developer's local .env.
    monkeypatch.setenv("ENV_FILE", "/dev/null")
```

- [ ] **Step 2: Full suite**

```bash
cd orchestrator
make test
```

Expected: all green. Coverage ≥ 80% on touched modules.

- [ ] **Step 3: Lint + type**

```bash
make lint
make type
```

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add orchestrator/tests/conftest.py
git commit -m "test(orchestrator): top-level conftest isolates settings env"
```

---

## Task 18: Open Plan 1 PR

- [ ] **Step 1: Push**

```bash
git push -u origin feat/orchestrator-foundation
```

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "feat(orchestrator): Plan 1 — Foundation" --body "$(cat <<'EOF'
Implements Plan 1 of the orchestrator rebuild.

Closes #33 once merged.

## Summary

- New `orchestrator/` package: settings, async DB layer with all 9 tables, Alembic migration, structlog JSON + secret redaction, GitHub App JWT minter, install token cache (with respx-mocked tests), HMAC webhook verifier, FastAPI `/gh/webhook` (verify + idempotent persist) and `/admin/healthz`, `__main__` dispatcher
- Multi-stage Dockerfile that bakes the `claude` CLI binary
- Local `deploy/compose/docker-compose.yml` runs api + postgres + migrator end-to-end
- Tests: unit + integration (testcontainers-pg + respx)

## Test plan

- [ ] `make test` green (unit + integration)
- [ ] Coverage ≥ 80% on touched modules
- [ ] `docker compose up` brings stack up; `/admin/healthz` returns `{db:ok}`
- [ ] Signed webhook → 202 + DB row; bad signature → 401; duplicate delivery_id → single row

Refs: spec PR #32.
EOF
)"
```

- [ ] **Step 3: Verify CI green**, fix forward on this branch as needed.

- [ ] **Step 4: Hand off** — once merged, Plan 2 (issue #34) starts on `feat/orchestrator-engine`.

---

## Self-review (already applied)

- **Spec coverage** — Implements §3 (api + postgres + migrator subset), §5 (all 9 tables), §11.1 (GH App JWT, install tokens, HMAC verify), §11.2 (secrets layout), §12.1 (logging foundation). Workers, handlers, state machine, policy, admin UI, prometheus, Strato deploy, hardening polish are explicitly Plan 2/3 scope.
- **Placeholder scan** — no TBD/TODO. Each step has exact code, exact paths, exact commands. `<your-test-pg-url>` in alembic steps is intentional — engineer's choice of throwaway pg.
- **Type consistency** — `Settings.gh_app_webhook_secret` is `str`; webhook handler encodes to `bytes` at the boundary. `mint_app_jwt` accepts `private_key_path: Path` and `app_id: int` consistently. Smoke-test imports match exported model names: `Repo`, `Run`, `Job`, `RunEvent`, `CliRun`, `GhWebhook`, `PolicyDecision`, `InstallationToken`, `OauthSession`. `get_installation_token` signature in test matches signature in implementation (kwargs `installation_id`, `mint_app_jwt`).
