"""Data models for x-ai CLI orchestrator.

Defines all dataclasses for tasks, results, feedback, and
provides YAML frontmatter parsing/writing for Markdown files.

Planner-Executor model:
- Planner creates plans and reviews code
- Executor implements code following the plan
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# YAML Frontmatter helpers
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)",
    re.DOTALL,
)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter and body from a Markdown string.

    Returns:
        (metadata_dict, body_string)
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw_yaml, body = match.group(1), match.group(2)
    meta = yaml.safe_load(raw_yaml) or {}
    return meta, body.strip()


def write_frontmatter(meta: dict[str, Any], body: str) -> str:
    """Serialize metadata + body into a Markdown string with YAML frontmatter."""
    yaml_str = yaml.dump(meta, default_flow_style=False, allow_unicode=True).strip()
    return f"---\n{yaml_str}\n---\n\n{body}\n"


def new_id() -> str:
    """Generate a new UUID string."""
    return str(uuid.uuid4())


def now_iso() -> str:
    """Current UTC timestamp in ISO format."""
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """Top-level task representing the user's original request."""

    id: str = field(default_factory=new_id)
    original_request: str = ""
    status: str = "pending"  # pending | in_progress | completed | failed
    current_round: int = 0
    created_at: str = field(default_factory=now_iso)


@dataclass
class AgentTask:
    """A task file to be written for an agent to consume.

    Serialised to `{task_id}.task.md`.

    task_type:
        - plan: Planner reads repo and creates implementation plan
        - execute: Executor follows plan and implements code
        - review: Planner reviews Executor's code changes
    """

    id: str = field(default_factory=new_id)
    task_type: str = "execute"  # plan | execute | review
    work_dir: str = "."
    round: int = 1
    parent_task_id: str | None = None
    instructions: str = ""
    context: str = ""
    plan_content: str = ""  # Plan from Planner, sent to Executor
    feedback: Feedback | None = None

    def to_markdown(self) -> str:
        meta: dict[str, Any] = {
            "id": self.id,
            "type": self.task_type,
            "work_dir": self.work_dir,
            "round": self.round,
            "parent_task_id": self.parent_task_id,
        }
        if self.feedback:
            meta["feedback"] = {
                "mandatory_fixes": self.feedback.mandatory_fixes,
                "score_gaps": self.feedback.score_gaps,
                "execution_errors": self.feedback.execution_errors,
                "previous_score": self.feedback.previous_score,
            }
        else:
            meta["feedback"] = None

        body_parts = [f"## Instructions\n\n{self.instructions}"]
        if self.plan_content:
            body_parts.append(f"## Implementation Plan\n\n{self.plan_content}")
        if self.context:
            body_parts.append(f"## Context\n\n{self.context}")

        return write_frontmatter(meta, "\n\n".join(body_parts))

    def write(self, tasks_dir: Path) -> Path:
        """Write task file to disk."""
        path = tasks_dir / f"{self.id}.task.md"
        path.write_text(self.to_markdown(), encoding="utf-8")
        return path


@dataclass
class AgentResult:
    """Parsed result from an agent's `{task_id}.result.md`."""

    id: str = ""
    status: str = "error"  # success | error | partial
    score: float | None = None
    round: int = 1
    files_changed: list[str] = field(default_factory=list)
    error_message: str | None = None
    body: str = ""

    @classmethod
    def from_file(cls, path: Path) -> AgentResult:
        """Parse a result.md file into an AgentResult."""
        if not path.exists():
            return cls(status="error", error_message=f"Result file not found: {path}")

        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)

        return cls(
            id=meta.get("id", ""),
            status=meta.get("status", "error"),
            score=meta.get("score"),
            round=meta.get("round", 1),
            files_changed=meta.get("files_changed", []) or [],
            error_message=meta.get("error_message"),
            body=body,
        )

    @classmethod
    def error(cls, message: str) -> AgentResult:
        """Create an error result."""
        return cls(status="error", error_message=message)


@dataclass
class ReviewResult:
    """Parsed review from the Planner agent."""

    score: float = 0.0
    review_notes: str = ""
    solution: AgentResult | None = None


@dataclass
class Feedback:
    """Structured feedback for the next round."""

    mandatory_fixes: list[str] = field(default_factory=list)
    score_gaps: dict[str, str] = field(default_factory=dict)
    execution_errors: list[str] = field(default_factory=list)
    previous_score: float = 0.0


@dataclass
class ExecutionResult:
    """Result of running tests in work_dir."""

    exit_code: int = 1
    stdout: str = ""
    stderr: str = ""
    tests_passed: bool = False
