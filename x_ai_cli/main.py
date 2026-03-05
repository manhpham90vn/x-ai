"""CLI entrypoint for x-ai multi-agent orchestrator."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time
from typing import Any

from rich.panel import Panel
from rich.prompt import FloatPrompt, IntPrompt, Prompt
from rich.table import Table

from x_ai_cli import __version__
from x_ai_cli.config import Config
from x_ai_cli.logger import console, logger, setup_logging
from x_ai_cli.orchestrator import Orchestrator, PipelineResult


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x-ai",
        description="Multi-Agent AI Coding System — Planner-Executor CLI",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="The coding task to execute",
    )
    parser.add_argument(
        "--work-dir",
        "-d",
        default=".",
        help="Working directory for the project (default: current dir)",
    )
    parser.add_argument(
        "--max-rounds",
        "-r",
        type=int,
        default=3,
        help="Maximum retry rounds (default: 3)",
    )
    parser.add_argument(
        "--threshold",
        "-t",
        type=float,
        default=70.0,
        help="Quality threshold score 0-100 (default: 70.0)",
    )
    parser.add_argument(
        "--planner-timeout",
        type=int,
        default=600,
        help="Planner agent timeout in seconds (default: 600)",
    )
    parser.add_argument(
        "--executor-timeout",
        type=int,
        default=600,
        help="Executor agent timeout in seconds (default: 600)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose/debug output",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"x-ai {__version__}",
    )
    return parser


def print_banner() -> None:
    console.print(
        Panel(
            "[bold cyan]x-ai[/bold cyan] — Planner-Executor AI Coding System\n"
            f"[dim]v{__version__}[/dim]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


def print_result(result: PipelineResult, elapsed: float) -> None:
    """Print the final result summary."""
    table = Table(title="Pipeline Result", border_style="cyan")
    table.add_column("Field", style="bold")
    table.add_column("Value")

    status_style = {
        "success": "[bold green]✓ SUCCESS[/bold green]",
        "best_effort": "[bold yellow]⚠ BEST EFFORT[/bold yellow]",
        "failed": "[bold red]✗ FAILED[/bold red]",
    }

    table.add_row("Status", status_style.get(result.status, result.status))
    table.add_row("Rounds Used", str(result.rounds_used))
    table.add_row("Time", f"{elapsed:.1f}s")

    if result.solution and result.solution.files_changed:
        table.add_row("Files Changed", "\n".join(result.solution.files_changed))

    if result.warnings:
        table.add_row("Warnings", "\n".join(result.warnings))

    console.print()
    console.print(table)

    if result.solution and result.solution.body:
        console.print()
        console.print(
            Panel(
                result.solution.body[:2000],
                title="Solution Summary",
                border_style="green" if result.status == "success" else "yellow",
            )
        )


async def async_main(args: argparse.Namespace) -> int:
    print_banner()

    is_interactive = not args.prompt

    if is_interactive:
        console.print(
            "[dim]Welcome to x-ai interactive mode. Press Ctrl+C to exit.[/dim]\n"
        )

        args.work_dir = Prompt.ask(
            "[bold cyan]? Work directory[/bold cyan]", default=args.work_dir
        )
        args.max_rounds = IntPrompt.ask(
            "[bold cyan]? Max retry rounds[/bold cyan]", default=args.max_rounds
        )
        args.threshold = FloatPrompt.ask(
            "[bold cyan]? Quality threshold (0-100)[/bold cyan]", default=args.threshold
        )
        console.print()

    config = Config(
        work_dir=args.work_dir,
        max_rounds=args.max_rounds,
        quality_threshold=args.threshold,
        planner_timeout_sec=args.planner_timeout,
        executor_timeout_sec=args.executor_timeout,
        verbose=args.verbose,
    )

    setup_logging(
        verbose=config.verbose,
        log_to_file=config.log_to_file,
        logs_dir=config.logs_path,
    )
    # Setup robust signal handling for graceful shutdown
    loop = asyncio.get_running_loop()
    main_task: asyncio.Task[Any] | None = None
    _shutting_down = False
    _state = "prompting"

    def _signal_handler() -> None:
        nonlocal _shutting_down
        if _shutting_down or _state == "prompting":
            # Second Ctrl+C or at prompt — force exit immediately
            console.print("\n[dim]Exiting...[/dim]")
            import os

            os._exit(130)
        _shutting_down = True
        console.print(
            "\n[warning]Shutting down gracefully (Ctrl+C again to force)...[/warning]"
        )
        # Cancel the main task so the await below raises CancelledError
        if main_task and not main_task.done():
            main_task.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, _signal_handler)
        loop.add_signal_handler(signal.SIGTERM, _signal_handler)
    except NotImplementedError:
        # Signal handlers not supported on this platform
        signal.signal(signal.SIGINT, lambda s, f: _signal_handler())
        signal.signal(signal.SIGTERM, lambda s, f: _signal_handler())

    # Single-shot mode
    prompt = str(args.prompt)
    console.print(
        f"[dim]Prompt:[/dim] {prompt[:200]}{'...' if len(prompt) > 200 else ''}"
    )
    console.print(f"[dim]Work dir:[/dim] {config.work_path}")
    console.print(f"[dim]Max rounds:[/dim] {config.max_rounds}")
    console.print("[dim]Mode:[/dim] Planner-Executor")
    console.print(f"[dim]Threshold:[/dim] {config.quality_threshold}")
    console.print(
        f"[dim]Timeouts:[/dim] Planner {config.planner_timeout_sec}s, "
        f"Executor {config.executor_timeout_sec}s"
    )
    console.print()

    orchestrator = Orchestrator(config)
    start = time.monotonic()
    try:
        main_task = asyncio.current_task()
        result = await orchestrator.run_pipeline(prompt)
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[warning]Interrupted — cleaning up...[/warning]")
        try:
            if hasattr(orchestrator, "runner") and hasattr(
                orchestrator.runner, "cleanup_all"
            ):
                cleanup_task = asyncio.create_task(orchestrator.runner.cleanup_all())
                await asyncio.wait([cleanup_task], timeout=10)
                console.print("[success]All tmux sessions cleaned up ✓[/success]")
        except Exception as e:
            console.print(f"[error]Cleanup timed out or failed: {e}[/error]")
        return 130
    except BaseException as e:
        logger.error("Pipeline failed: %s", e, exc_info=True)
        console.print(f"\n[error]Pipeline failed: {e}[/error]")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(orchestrator.runner.cleanup_all(), timeout=10)
        return 1

    elapsed = time.monotonic() - start
    print_result(result, elapsed)
    return 0 if result.status == "success" else 1


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    is_interactive = not args.prompt

    if is_interactive:
        from x_ai_cli.tui.app import XAITui

        app = XAITui(args)
        # run() will block until the TUI exits
        app.run()
        exit_code = 0
    else:
        exit_code = asyncio.run(async_main(args))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
