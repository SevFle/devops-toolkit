"""Complexity scoring for adaptive time budgets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Keywords and their complexity weights
COMPLEXITY_KEYWORDS: dict[str, float] = {
    "refactor": 2.0,
    "rewrite": 2.5,
    "migrate": 2.0,
    "redesign": 2.5,
    "overhaul": 2.5,
    "add": 1.0,
    "create": 1.0,
    "implement": 1.5,
    "fix": 0.8,
    "rename": 0.5,
    "update": 0.8,
    "remove": 0.7,
    "delete": 0.7,
    "move": 0.6,
    "typo": 0.3,
    "docs": 0.4,
    "documentation": 0.4,
    "test": 1.0,
    "tests": 1.0,
}


@dataclass(frozen=True)
class ComplexityScore:
    """Immutable complexity assessment."""
    task_count: int
    keyword_score: float
    raw_score: float
    level: str  # "low", "medium", "high"

    @property
    def recommended_attempts(self) -> int:
        if self.level == "low":
            return 2
        if self.level == "medium":
            return 4
        return 6

    @property
    def recommended_timeout(self) -> int:
        """Recommended per-attempt timeout in seconds."""
        if self.level == "low":
            return 900  # 15 min
        if self.level == "medium":
            return 1800  # 30 min
        return 2400  # 40 min

    @property
    def recommended_budget(self) -> int:
        """Recommended total time budget in seconds."""
        if self.level == "low":
            return 3000  # 50 min
        if self.level == "medium":
            return 6600  # 110 min (current default)
        return 9000  # 150 min


def score_complexity(change_name: str, work_dir: Path) -> ComplexityScore:
    """Score the complexity of an OpenSpec change."""
    change_dir = work_dir / "openspec" / "changes" / change_name
    tasks_file = change_dir / "tasks.md"

    # Count tasks
    task_count = 0
    task_texts: list[str] = []
    if tasks_file.exists():
        for line in tasks_file.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("- [ ]") or stripped.startswith("- [x]"):
                task_count += 1
                task_texts.append(stripped.lower())

    # Keyword analysis
    all_text = " ".join(task_texts + [change_name.replace("-", " ").lower()])
    keyword_score = 0.0
    for keyword, weight in COMPLEXITY_KEYWORDS.items():
        if re.search(r'\b' + re.escape(keyword) + r'\b', all_text):
            keyword_score += weight

    # Read proposal for additional context
    proposal = change_dir / "proposal.md"
    if proposal.exists():
        proposal_text = proposal.read_text().lower()
        for keyword, weight in COMPLEXITY_KEYWORDS.items():
            if re.search(r'\b' + re.escape(keyword) + r'\b', proposal_text):
                keyword_score += weight * 0.5  # half weight for proposal mentions

    # Compute raw score: task_count * task_weight + keyword_score
    task_weight = 1.0 if task_count <= 5 else (1.5 if task_count <= 10 else 2.0)
    raw_score = (task_count * task_weight) + keyword_score

    # Classify
    if raw_score < 5:
        level = "low"
    elif raw_score < 15:
        level = "medium"
    else:
        level = "high"

    return ComplexityScore(
        task_count=task_count,
        keyword_score=round(keyword_score, 2),
        raw_score=round(raw_score, 2),
        level=level,
    )


def timeout_for_attempt(base_timeout: int, attempt: int, max_attempts: int) -> int:
    """Calculate per-attempt timeout with progressive scaling.

    First attempt gets full timeout, subsequent attempts get progressively less.
    """
    if attempt <= 1:
        return base_timeout
    # Each subsequent attempt gets 15% less time (minimum 50% of base)
    factor = max(0.5, 1.0 - (attempt - 1) * 0.15)
    return int(base_timeout * factor)
