#!/usr/bin/env python3
"""TUI monitor for OpenSpec orchestrator workflow runs.

Usage:
    python3 -m orchestrator.monitor [repo] [workflow] [interval_seconds]

Defaults:
    repo:     SevFle/WedPilot
    workflow: orchestrate.yml
    interval: 15
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Cache resolved change names across refreshes
_name_cache: dict[int, str] = {}


@dataclass(frozen=True)
class Run:
    id: int
    status: str
    conclusion: str
    created: str
    updated: str
    change_name: str


STATUS_STYLE = {
    "success": ("green", "✓"),
    "failure": ("red", "✗"),
    "cancelled": ("yellow", "⊘"),
    "in_progress": ("blue", "⟳"),
    "queued": ("dim", "…"),
    "waiting": ("dim", "…"),
}

# Matches "  change_name: some-name" from workflow input echo
_INPUT_RE = re.compile(r"change_name:\s+(.+)")
# Matches JSON log with change_name field
_JSON_RE = re.compile(r'"change_name"\s*:\s*"([^"]+)"')


def _style_for(run: Run) -> tuple[str, str]:
    key = run.conclusion if run.status == "completed" else run.status
    return STATUS_STYLE.get(key, ("dim", "?"))


def _elapsed(created: str, updated: str, status: str) -> str:
    try:
        c = datetime.fromisoformat(created.replace("Z", "+00:00"))
        u = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        end = now if status in ("in_progress", "queued", "waiting") else u
        secs = max(0, int((end - c).total_seconds()))
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except Exception:
        return "—"


def _fetch_change_name(repo: str, run_id: int) -> tuple[int, str]:
    """Fetch change_name from run logs."""
    if run_id in _name_cache:
        return run_id, _name_cache[run_id]

    try:
        result = subprocess.run(
            ["gh", "run", "view", str(run_id), "--repo", repo, "--log"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return run_id, ""

        for line in result.stdout.split("\n")[:200]:  # Check first 200 lines
            # Try workflow input format: "  change_name: add-empty-state-component"
            m = _INPUT_RE.search(line)
            if m:
                name = m.group(1).strip()
                _name_cache[run_id] = name
                return run_id, name
            # Try JSON log format
            m = _JSON_RE.search(line)
            if m:
                name = m.group(1)
                _name_cache[run_id] = name
                return run_id, name
    except Exception:
        pass
    # Cache empty so we don't retry endlessly for runs with no logs
    _name_cache[run_id] = ""
    return run_id, ""


def fetch_runs(repo: str, workflow: str, limit: int = 40) -> list[Run]:
    """Fetch runs and resolve change names for non-queued runs."""
    result = subprocess.run(
        [
            "gh", "run", "list",
            "--repo", repo,
            "--workflow", workflow,
            "--limit", str(limit),
            "--json", "databaseId,status,conclusion,createdAt,updatedAt",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return []

    data = json.loads(result.stdout)

    # Resolve change names in parallel for non-queued runs not already cached
    ids_to_resolve = [
        r["databaseId"] for r in data
        if r["status"] in ("completed", "in_progress")
        and r["databaseId"] not in _name_cache
    ]

    if ids_to_resolve:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [
                pool.submit(_fetch_change_name, repo, rid)
                for rid in ids_to_resolve
            ]
            for fut in as_completed(futures):
                rid, name = fut.result()
                if name:
                    _name_cache[rid] = name

    return [
        Run(
            id=r["databaseId"],
            status=r["status"],
            conclusion=r.get("conclusion", ""),
            created=r["createdAt"],
            updated=r["updatedAt"],
            change_name=_name_cache.get(r["databaseId"], ""),
        )
        for r in data
    ]


def build_display(runs: list[Run]) -> Panel:
    counts: dict[str, int] = {}
    for r in runs:
        key = r.conclusion if r.status == "completed" else r.status
        counts[key] = counts.get(key, 0) + 1

    summary_parts = []
    for key, label in [("success", "pass"), ("failure", "fail"),
                       ("in_progress", "run"), ("queued", "queue"),
                       ("cancelled", "skip")]:
        n = counts.get(key, 0)
        if n > 0:
            color, icon = STATUS_STYLE.get(key, ("dim", "?"))
            summary_parts.append(f"[{color}]{icon} {n} {label}[/]")

    summary = "  ".join(summary_parts)

    table = Table(show_header=True, show_lines=False, expand=True, padding=(0, 1))
    table.add_column("", width=2, justify="center")
    table.add_column("Change", ratio=3)
    table.add_column("Status", width=10)
    table.add_column("Time", width=9, justify="right")
    table.add_column("ID", width=12, justify="right", style="dim")

    queued_count = 0
    for r in runs:
        if r.status in ("queued", "waiting"):
            queued_count += 1
            continue

        color, icon = _style_for(r)
        change = r.change_name or "[dim]resolving…[/]"
        status_label = r.conclusion if r.status == "completed" else "running"
        dur = _elapsed(r.created, r.updated, r.status)
        table.add_row(
            f"[{color}]{icon}[/]",
            change,
            Text(status_label, style=color),
            dur,
            str(r.id),
        )

    if queued_count > 0:
        table.add_row(
            "[dim]…[/]",
            f"[dim]{queued_count} runs queued, waiting for runners[/]",
            Text("queued", style="dim"),
            "",
            "",
        )

    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    title = f"[bold]OpenSpec Orchestrator[/]  {summary}  [dim]({now})[/]"
    return Panel(table, title=title, border_style="blue")


def main() -> int:
    repo = sys.argv[1] if len(sys.argv) > 1 else "SevFle/WedPilot"
    workflow = sys.argv[2] if len(sys.argv) > 2 else "orchestrate.yml"
    interval = int(sys.argv[3]) if len(sys.argv) > 3 else 15

    console = Console()
    console.print(
        f"[dim]Monitoring {repo} / {workflow}  "
        f"(refresh every {interval}s, Ctrl+C to quit)[/]\n"
    )

    try:
        with Live(console=console, refresh_per_second=1) as live:
            while True:
                runs = fetch_runs(repo, workflow)
                live.update(build_display(runs))
                time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/]")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
