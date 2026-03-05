"""Logging setup for x-ai CLI orchestrator.

Provides rich console output and optional file logging.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.theme import Theme

# Custom theme for x-ai CLI
_THEME = Theme(
    {
        "info": "cyan",
        "warning": "yellow",
        "error": "bold red",
        "success": "bold green",
        "phase": "bold magenta",
        "round": "bold blue",
        "score": "bold yellow",
        "agent": "bold cyan",
    }
)

console = Console(theme=_THEME, stderr=True)

# Module-level logger
logger = logging.getLogger("x_ai_cli")


def setup_logging(
    verbose: bool = False,
    log_to_file: bool = True,
    logs_dir: Path | None = None,
    run_id: str | None = None,
) -> None:
    """Configure logging with rich console and optional file output.

    Args:
        verbose: Enable DEBUG level on console.
        log_to_file: Write logs to file in logs_dir.
        logs_dir: Directory for log files.
        run_id: Unique run identifier for log subdirectory.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logger.setLevel(level)
    logger.handlers.clear()

    # Rich console handler
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=verbose,
        rich_tracebacks=True,
        markup=True,
        log_time_format="[%H:%M:%S]",
    )
    rich_handler.setLevel(level)
    logger.addHandler(rich_handler)

    # File handler
    if log_to_file and logs_dir:
        run_id = run_id or datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        run_log_dir = logs_dir / run_id
        run_log_dir.mkdir(parents=True, exist_ok=True)

        file_handler = logging.FileHandler(
            run_log_dir / "orchestrator.log",
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        logger.debug("Log file: %s", run_log_dir / "orchestrator.log")


def log_phase(phase: str, detail: str = "") -> None:
    """Log a pipeline phase transition."""
    msg = f"[phase]▶ {phase}[/phase]"
    if detail:
        msg += f" — {detail}"
    logger.info(msg)


def log_round(round_num: int, max_rounds: int) -> None:
    """Log the start of a round."""
    logger.info("[round]◆ Round %d/%d[/round]", round_num, max_rounds)


def log_score(worker: str, score: float, threshold: float) -> None:
    """Log a worker's score against threshold."""
    status = "✓ PASS" if score >= threshold else "✗ FAIL"
    style = "success" if score >= threshold else "warning"
    logger.info(
        "[agent]%s[/agent] score: [score]%.1f[/score] (threshold: %.1f) [%s]%s[/%s]",
        worker,
        score,
        threshold,
        style,
        status,
        style,
    )


def log_agent_start(agent_type: str, task_id: str) -> None:
    """Log when an agent starts processing."""
    logger.info("[agent]⚙ %s[/agent] started task %s", agent_type, task_id[:8])


def log_agent_done(agent_type: str, task_id: str, status: str) -> None:
    """Log when an agent finishes."""
    style = "success" if status == "success" else "error"
    logger.info(
        "[agent]⚙ %s[/agent] finished %s → [%s]%s[/%s]",
        agent_type,
        task_id[:8],
        style,
        status,
        style,
    )
