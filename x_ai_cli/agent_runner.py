"""Agent subprocess launcher for x-ai CLI orchestrator.

Launches Claude CLI instances as workers using tmux sessions for process
management and git worktrees for filesystem isolation.

All agents are Claude CLI:
- Leader: `claude -p` (headless, captures stdout directly)
- Workers: `claude -p --worktree` inside tmux sessions (parallel, isolated)
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from x_ai_cli.config import Config
from x_ai_cli.logger import logger, log_agent_start, log_agent_done
from x_ai_cli.models import AgentResult, parse_frontmatter, write_frontmatter

# Poll interval when waiting for tmux command to finish
_POLL_INTERVAL_SEC = 2


class TmuxSession:
    """Manages a single tmux session for running commands."""

    def __init__(self, tmux_bin: str = "tmux") -> None:
        self.tmux_bin = tmux_bin

    async def _run(self, *args: str) -> tuple[int, str, str]:
        """Run a tmux subcommand and return (returncode, stdout, stderr)."""
        cmd = [self.tmux_bin, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        return (
            proc.returncode or 0,
            stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
            stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
        )

    async def exists(self, session_name: str) -> bool:
        """Check if a tmux session exists."""
        rc, _, _ = await self._run("has-session", "-t", session_name)
        return rc == 0

    async def create(self, session_name: str) -> bool:
        """Create a new detached tmux session.

        Returns True if created successfully.
        """
        rc, _, stderr = await self._run(
            "new-session", "-d", "-s", session_name,
            "-x", "250", "-y", "50",  # wide pane for full output
        )
        if rc != 0:
            logger.error("Failed to create tmux session %s: %s", session_name, stderr)
            return False
        logger.debug("Created tmux session: %s", session_name)
        return True

    async def send_command(self, session_name: str, command: str) -> None:
        """Send a command string to the tmux session."""
        # Use send-keys to type the command and press Enter
        await self._run("send-keys", "-t", session_name, command, "Enter")

    async def capture_output(self, session_name: str) -> str:
        """Capture the full scrollback buffer from the tmux pane."""
        _, stdout, _ = await self._run(
            "capture-pane", "-t", session_name,
            "-p",       # print to stdout
            "-S", "-",  # start from beginning of scrollback
        )
        return stdout

    async def capture_full_output(self, session_name: str) -> str:
        """Capture entire scrollback (up to 50000 lines) from tmux pane."""
        _, stdout, _ = await self._run(
            "capture-pane", "-t", session_name,
            "-p",            # print to stdout
            "-S", "-50000",  # large scrollback
            "-J",            # join wrapped lines
        )
        return stdout

    async def kill(self, session_name: str) -> None:
        """Kill a tmux session."""
        await self._run("kill-session", "-t", session_name)
        logger.debug("Killed tmux session: %s", session_name)


class AgentRunner:
    """Launches and manages Claude CLI agent instances."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.tmux = TmuxSession(config.tmux_bin)
        self._active_sessions: set[str] = set()

    @staticmethod
    def _parse_stream_json_output(raw: str) -> str:
        """Extract final text from Claude stream-json output in tmux scrollback.

        Parses line-delimited JSON events captured from tmux.
        Returns the `result` field from the final 'result' event,
        or concatenated text blocks from 'assistant' events as fallback.
        """
        result_text = ""
        text_parts: list[str] = []

        for line in raw.strip().splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            evt_type = event.get("type")
            if evt_type == "result":
                result_text = event.get("result", "")
            elif evt_type == "assistant":
                msg = event.get("message", {})
                for block in msg.get("content", []):
                    if block.get("type") == "text":
                        text_parts.append(block["text"])

        return result_text or "\n".join(text_parts)



    async def cleanup_all(self) -> None:
        """Kill all active tmux sessions. Called on Ctrl+C / shutdown."""
        if not self._active_sessions:
            return
        logger.info("Cleaning up %d active tmux session(s)...", len(self._active_sessions))
        for session_name in list(self._active_sessions):
            try:
                await self.tmux.kill(session_name)
            except Exception:
                pass
        self._active_sessions.clear()
        logger.info("All tmux sessions cleaned up")

    def _get_skill_content(self, role: str) -> str:
        """Read the skill file content for a given role."""
        skill_path = self.config.skills_path / f"{role}.md"
        if not skill_path.exists():
            logger.warning("Skill file not found: %s", skill_path)
            return ""
        return skill_path.read_text(encoding="utf-8")

    def _build_prompt(self, task_file: Path, role: str) -> str:
        """Build the full prompt: skill instructions + task content."""
        task_content = task_file.read_text(encoding="utf-8")
        skill_content = self._get_skill_content(role)

        return (
            f"{skill_content}\n\n"
            f"---\n\n"
            f"# TASK FILE\n\n"
            f"{task_content}\n\n"
            f"---\n\n"
            f"IMPORTANT: Output your result as Markdown with YAML frontmatter "
            f"(---\\nkey: value\\n---\\n) as described in the skill instructions above. "
            f"Include id, status, and other required fields in the frontmatter."
        )

    def _build_claude_args(
        self,
        prompt: str,
        worktree_name: str | None = None,
    ) -> list[str]:
        """Build claude CLI arguments."""
        args = [
            self.config.claude_bin,
            "-p", prompt,
            "--dangerously-skip-permissions",
        ]
        if worktree_name and self.config.use_worktree:
            args.extend(["--worktree", worktree_name])
        return args

    def _get_timeout(self, role: str) -> int:
        """Get timeout seconds based on agent role."""
        if role == "leader":
            return self.config.leader_timeout_sec
        return self.config.worker_timeout_sec

    def _parse_stdout_to_result(self, task_id: str, stdout: str) -> AgentResult:
        """Parse agent stdout into an AgentResult.

        Tries to extract YAML frontmatter from stdout.
        Falls back to wrapping raw stdout as result body.
        """
        stdout = stdout.strip()
        if not stdout:
            return AgentResult.error("Agent produced no output")

        # Try parsing frontmatter from stdout
        meta, body = parse_frontmatter(stdout)

        if meta:
            return AgentResult(
                id=meta.get("id", task_id),
                status=meta.get("status", "success"),
                score=meta.get("score"),
                round=meta.get("round", 1),
                files_changed=meta.get("files_changed", []) or [],
                error_message=meta.get("error_message"),
                body=body,
            )
        else:
            return AgentResult(
                id=task_id,
                status="success",
                body=stdout,
            )

    def _save_result_file(
        self, result: AgentResult, result_file: Path,
    ) -> None:
        """Save an AgentResult to a .result.md file for audit."""
        result_md = write_frontmatter(
            {
                "id": result.id,
                "status": result.status,
                "score": result.score,
                "round": result.round,
                "files_changed": result.files_changed,
            },
            result.body,
        )
        result_file.write_text(result_md, encoding="utf-8")
        logger.debug("Saved result to %s", result_file.name)

    # ------------------------------------------------------------------
    # Leader: tmux session (observable via tmux attach)
    # ------------------------------------------------------------------

    async def run_leader(self, task_file: Path) -> AgentResult:
        """Run Claude Code as Leader agent in a tmux session.

        Uses the same tmux + runner.sh pattern as workers so the user
        can attach and observe the leader's output in real-time.
        """
        for binary, label in [
            (self.config.claude_bin, "claude"),
            (self.config.tmux_bin, "tmux"),
        ]:
            if not shutil.which(binary):
                msg = f"{label} binary not found: {binary}"
                logger.error(msg)
                return AgentResult.error(msg)

        task_id = task_file.stem.replace(".task", "")
        timeout = self._get_timeout("leader")
        result_file = task_file.parent / f"{task_id}.result.md"
        session_name = f"xai_leader_{task_id[:8]}"

        log_agent_start("claude:leader", task_id)

        try:
            # 1. Create tmux session
            created = await self.tmux.create(session_name)
            if not created:
                msg = f"Failed to create tmux session: {session_name}"
                return AgentResult.error(msg)
            self._active_sessions.add(session_name)

            # 2. Build runner script — run Claude directly (no pipes)
            prompt = self._build_prompt(task_file, "leader")
            prompt_file = task_file.parent / f"{task_id}.prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")

            done_flag = task_file.parent / f"{task_id}.done"
            done_flag.unlink(missing_ok=True)

            runner_script = task_file.parent / f"{task_id}.runner.sh"
            runner_script.write_text(
                f'#!/bin/bash\n'
                f'cd "{self.config.work_path}"\n'
                f'cat \'{prompt_file}\' | {self.config.claude_bin} -p -'
                f' --dangerously-skip-permissions'
                f' --output-format stream-json --verbose\n'
                f'touch "{done_flag}"\n',
                encoding="utf-8",
            )
            runner_script.chmod(0o755)

            # 3. Run in tmux — user can `tmux attach` to watch live
            await self.tmux.send_command(session_name, f"bash '{runner_script}'")

            # 4. Poll for completion
            completed = await self._wait_for_done_flag(done_flag, timeout)

            # 5. Capture output from tmux scrollback and parse JSON
            raw = await self.tmux.capture_full_output(session_name)
            stdout = self._parse_stream_json_output(raw)
            if not completed:
                logger.warning("claude:leader timed out but partial output captured (%d chars)", len(stdout))

            # 6. Clean up temp files
            prompt_file.unlink(missing_ok=True)
            done_flag.unlink(missing_ok=True)
            runner_script.unlink(missing_ok=True)

        except Exception as e:
            msg = f"claude:leader unexpected error: {e}"
            logger.error(msg, exc_info=True)
            log_agent_done("claude:leader", task_id, "error")
            return AgentResult.error(msg)

        finally:
            await self.tmux.kill(session_name)
            self._active_sessions.discard(session_name)

        if not completed:
            msg = f"claude:leader timed out after {timeout}s"
            logger.error(msg)
            log_agent_done("claude:leader", task_id, "timeout")
            return AgentResult.error(msg)

        # Check if agent wrote a result file directly
        if result_file.exists():
            result = AgentResult.from_file(result_file)
            log_agent_done("claude:leader", task_id, result.status)
            return result

        # Parse stdout as the result
        result = self._parse_stdout_to_result(task_id, stdout)
        self._save_result_file(result, result_file)
        log_agent_done("claude:leader", task_id, result.status)
        return result

    # ------------------------------------------------------------------
    # Workers: tmux sessions + optional git worktrees
    # ------------------------------------------------------------------

    async def run_worker(
        self,
        worker_name: str,
        task_file: Path,
        run_id: str,
    ) -> AgentResult:
        """Run a Claude worker in a tmux session with optional worktree isolation.

        Args:
            worker_name: e.g. "claude_worker_0"
            task_file: Path to the .task.md file
            run_id: Unique run identifier for session naming
        """
        # Preflight checks
        for binary, label in [
            (self.config.claude_bin, "claude"),
            (self.config.tmux_bin, "tmux"),
        ]:
            if not shutil.which(binary):
                msg = f"{label} binary not found: {binary}"
                logger.error(msg)
                return AgentResult.error(msg)

        task_id = task_file.stem.replace(".task", "")
        timeout = self._get_timeout("worker")
        result_file = task_file.parent / f"{task_id}.result.md"
        session_name = f"xai_{run_id}_{worker_name}"
        worktree_name = f"xai_{worker_name}" if self.config.use_worktree else None

        log_agent_start(worker_name, task_id)

        try:
            # 1. Create tmux session
            created = await self.tmux.create(session_name)
            if not created:
                msg = f"Failed to create tmux session: {session_name}"
                return AgentResult.error(msg)
            self._active_sessions.add(session_name)

            # 2. Build runner script — run Claude directly (no pipes)
            prompt = self._build_prompt(task_file, "worker")
            prompt_file = task_file.parent / f"{task_id}.prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")

            done_flag = task_file.parent / f"{task_id}.done"
            done_flag.unlink(missing_ok=True)

            worktree_arg = f' --worktree {worktree_name}' if worktree_name and self.config.use_worktree else ''
            runner_script = task_file.parent / f"{task_id}.runner.sh"
            runner_script.write_text(
                f'#!/bin/bash\n'
                f'cd "{self.config.work_path}"\n'
                f'cat \'{prompt_file}\' | {self.config.claude_bin} -p -'
                f' --dangerously-skip-permissions'
                f'{worktree_arg}'
                f' --output-format stream-json --verbose\n'
                f'touch "{done_flag}"\n',
                encoding="utf-8",
            )
            runner_script.chmod(0o755)

            # Send command to tmux — user can `tmux attach` to watch
            await self.tmux.send_command(session_name, f"bash '{runner_script}'")

            # 3. Poll for completion (wait for done flag)
            completed = await self._wait_for_done_flag(done_flag, timeout)

            # 4. Capture output from tmux scrollback and parse JSON
            raw = await self.tmux.capture_full_output(session_name)
            stdout = self._parse_stream_json_output(raw)
            if not completed and stdout:
                logger.warning("%s timed out but partial output captured (%d chars)",
                               worker_name, len(stdout))

            # 5. Clean up temp files
            prompt_file.unlink(missing_ok=True)
            done_flag.unlink(missing_ok=True)
            runner_script.unlink(missing_ok=True)

        except Exception as e:
            msg = f"{worker_name} unexpected error: {e}"
            logger.error(msg, exc_info=True)
            log_agent_done(worker_name, task_id, "error")
            return AgentResult.error(msg)

        finally:
            # 6. Always clean up the tmux session
            await self.tmux.kill(session_name)
            self._active_sessions.discard(session_name)

        if not completed:
            msg = f"{worker_name} timed out after {timeout}s"
            logger.error(msg)
            log_agent_done(worker_name, task_id, "timeout")
            return AgentResult.error(msg)

        # Check if agent wrote a result file directly
        if result_file.exists():
            result = AgentResult.from_file(result_file)
            log_agent_done(worker_name, task_id, result.status)
            return result

        # Parse captured output from file
        result = self._parse_stdout_to_result(task_id, stdout)
        self._save_result_file(result, result_file)
        log_agent_done(worker_name, task_id, result.status)
        return result

    async def _wait_for_done_flag(
        self,
        done_flag: Path,
        timeout: int,
    ) -> bool:
        """Poll for the done flag file to appear.

        Returns True if completed, False on timeout.
        """
        start = time.monotonic()
        logger.info(
            "Waiting for done flag %s (timeout: %ds)",
            done_flag.name,
            timeout,
        )

        while time.monotonic() - start < timeout:
            if done_flag.exists():
                elapsed = time.monotonic() - start
                logger.info("Done flag detected after %.1fs", elapsed)
                return True

            await asyncio.sleep(_POLL_INTERVAL_SEC)

        return False  # timeout

    async def run_workers_parallel(
        self,
        task_files: dict[str, Path],
        run_id: str,
    ) -> dict[str, AgentResult]:
        """Run multiple Claude workers in parallel via tmux sessions.

        Args:
            task_files: Dict of {worker_name: task_file_path}
            run_id: Unique run identifier

        Returns:
            Dict of {worker_name: AgentResult}
        """
        tasks = {
            worker_name: self.run_worker(worker_name, task_file, run_id)
            for worker_name, task_file in task_files.items()
        }

        results: dict[str, AgentResult] = {}
        gathered = await asyncio.gather(
            *tasks.values(),
            return_exceptions=True,
        )

        for worker_name, result in zip(tasks.keys(), gathered):
            if isinstance(result, Exception):
                logger.error("%s failed with exception: %s", worker_name, result)
                results[worker_name] = AgentResult.error(str(result))
            else:
                results[worker_name] = result

        return results
