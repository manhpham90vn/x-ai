"""Configuration for x-ai CLI orchestrator."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Central configuration for the orchestrator pipeline."""

    # Agent binary paths
    claude_bin: str = "claude"
    tmux_bin: str = "tmux"

    # Pipeline settings
    work_dir: str = "."
    tasks_dir: str = "tasks"
    logs_dir: str = "logs"
    max_rounds: int = 3
    quality_threshold: float = 70.0
    worker_timeout_sec: int = 300  # 5 min per worker agent
    leader_timeout_sec: int = 600  # 10 min per leader agent

    # Interactive mode settings
    prompt_marker: str = "❯"  # Marker to detect Claude is ready for input
    poll_interval_sec: float = 1.0  # Interval for polling tmux output

    # Worker settings
    num_workers: int = 2  # Number of parallel Claude workers
    use_worktree: bool = True  # Use git worktree isolation per worker

    # Logging
    verbose: bool = False
    log_to_file: bool = True

    # Resolved paths (set in __post_init__)
    _work_path: Path = field(init=False, repr=False)
    _tasks_path: Path = field(init=False, repr=False)
    _logs_path: Path = field(init=False, repr=False)
    _skills_path: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._work_path = Path(self.work_dir).resolve()
        self._tasks_path = self._work_path / self.tasks_dir
        self._logs_path = self._work_path / self.logs_dir
        self._skills_path = Path(__file__).parent.parent / "skills"

    @property
    def work_path(self) -> Path:
        return self._work_path

    @property
    def tasks_path(self) -> Path:
        return self._tasks_path

    @property
    def logs_path(self) -> Path:
        return self._logs_path

    @property
    def skills_path(self) -> Path:
        return self._skills_path

    def ensure_dirs(self) -> None:
        """Create required directories if they don't exist."""
        self._tasks_path.mkdir(parents=True, exist_ok=True)
        if self.log_to_file:
            self._logs_path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> Config:
        """Create config with overrides from environment variables."""
        return cls(
            claude_bin=os.getenv("XAI_CLAUDE_BIN", "claude"),
            tmux_bin=os.getenv("XAI_TMUX_BIN", "tmux"),
            work_dir=os.getenv("XAI_WORK_DIR", "."),
            max_rounds=int(os.getenv("XAI_MAX_ROUNDS", "3")),
            quality_threshold=float(os.getenv("XAI_QUALITY_THRESHOLD", "70.0")),
            worker_timeout_sec=int(os.getenv("XAI_WORKER_TIMEOUT", "300")),
            leader_timeout_sec=int(os.getenv("XAI_LEADER_TIMEOUT", "600")),
            num_workers=int(os.getenv("XAI_NUM_WORKERS", "2")),
            use_worktree=os.getenv("XAI_USE_WORKTREE", "true").lower()
            in ("1", "true", "yes"),
            verbose=os.getenv("XAI_VERBOSE", "").lower() in ("1", "true", "yes"),
            prompt_marker=os.getenv("XAI_PROMPT_MARKER", "❯"),
            poll_interval_sec=float(os.getenv("XAI_POLL_INTERVAL", "1.0")),
        )
