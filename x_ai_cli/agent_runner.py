"""Agent subprocess launcher for x-ai CLI orchestrator.

Launches Claude CLI instances in interactive mode inside tmux sessions.
Controls them natively via tmux send-keys.

Communication is FILE-BASED:
- Orchestrator writes {id}.task.md
- Claude reads the task file and writes {id}.result.md
- Orchestrator polls the filesystem for the result file

Tmux is ONLY used for:
- Creating/managing Claude sessions
- Injecting prompts (send-keys)
- Handling startup prompts (bypass permissions, trust folder)
- Detecting errors and confirmations (capture-pane)
"""

from __future__ import annotations

import asyncio
import shlex
import shutil
import time
from pathlib import Path

from x_ai_cli.config import Config
from x_ai_cli.logger import logger, log_agent_start, log_agent_done
from x_ai_cli.models import AgentResult, parse_frontmatter, write_frontmatter


class TmuxSession:
    """Manages tmux sessions — lifecycle and key injection only.

    Uses shell execution (create_subprocess_shell) for send-keys commands
    so that single-quote escaping works correctly.
    """

    def __init__(self, tmux_bin: str = "tmux") -> None:
        self.tmux_bin = tmux_bin

    async def _shell(self, cmd: str) -> tuple[int, str, str]:
        """Run a shell command and return (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_shell(
            cmd,
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
        rc, _, _ = await self._shell(
            f"{self.tmux_bin} has-session -t {shlex.quote(session_name)} 2>/dev/null"
        )
        return rc == 0

    async def create(self, session_name: str) -> bool:
        """Create a new detached tmux session (no command)."""
        rc, _, stderr = await self._shell(
            f"{self.tmux_bin} new-session -d"
            f" -s {shlex.quote(session_name)}"
            f" -x 250 -y 50"
        )
        if rc != 0:
            logger.error("Failed to create tmux session %s: %s", session_name, stderr)
            return False
        logger.debug("Created tmux session: %s", session_name)
        return True

    async def send_keys_raw(self, session_name: str, keys: str) -> None:
        """Send raw keys (control sequences like C-u, C-m, Down, Enter).

        These are NOT quoted — tmux interprets them as key names.
        """
        cmd = f"{self.tmux_bin} send-keys -t {shlex.quote(session_name)} {keys}"
        await self._shell(cmd)

    async def send_text(self, session_name: str, text: str) -> None:
        """Send literal text to tmux session.

        Uses single-quote shell escaping (replace ' with '"'"')
        so tmux receives the text literally, not as key names.
        """
        escaped = text.replace("'", "'\"'\"'")
        cmd = f"{self.tmux_bin} send-keys -t {shlex.quote(session_name)} '{escaped}'"
        await self._shell(cmd)

    async def capture_pane(self, session_name: str) -> str:
        """Capture visible pane content.

        Used ONLY for detecting prompts/confirmations/errors,
        NOT for extracting Claude's response.
        """
        _, stdout, _ = await self._shell(
            f"{self.tmux_bin} capture-pane -t {shlex.quote(session_name)} -p"
        )
        return stdout

    async def kill(self, session_name: str) -> None:
        """Kill a tmux session."""
        await self._shell(
            f"{self.tmux_bin} kill-session -t {shlex.quote(session_name)} 2>/dev/null"
        )
        logger.debug("Killed tmux session: %s", session_name)


class AgentRunner:
    """Launches and manages Claude CLI agents via native tmux control.

    File-based communication flow:
    1. Write task to {id}.task.md
    2. Create tmux session with Claude
    3. Handle startup prompts (bypass permissions, trust folder)
    4. Inject prompt: tell Claude to read task file and write result file
    5. Poll filesystem for {id}.result.md
    6. Parse result file with AgentResult.from_file()
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.tmux = TmuxSession(config.tmux_bin)
        self._active_sessions: set[str] = set()

    async def cleanup_all(self) -> None:
        """Kill all active tmux sessions. Called on Ctrl+C / shutdown."""
        if not self._active_sessions:
            return
        logger.info(
            "Cleaning up %d active tmux session(s)...", len(self._active_sessions)
        )
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

    def _build_prompt(
        self,
        task_file: Path,
        result_file: Path,
        role: str,
    ) -> str:
        """Build the full prompt for Claude.

        Tells Claude exactly which file to read (task) and
        which file to write (result). All communication is file-based.
        """
        skill_content = self._get_skill_content(role)

        return (
            f"{skill_content}\n\n"
            f"---\n\n"
            f"# YOUR TASK\n\n"
            f"1. Read the task file at: `{task_file}`\n"
            f"2. Follow the instructions in the task file\n"
            f"3. Write your result (Markdown with YAML frontmatter) to: `{result_file}`\n\n"
            f"IMPORTANT:\n"
            f"- The result file MUST contain YAML frontmatter (---\\nkey: value\\n---) "
            f"followed by your response body.\n"
            f"- Include `id`, `status`, and other required fields in the frontmatter.\n"
            f"- Use the exact same `id` from the task file.\n"
            f"- Do NOT print the result to stdout — WRITE it to the file path above."
        )

    def _get_timeout(self, role: str) -> int:
        """Get timeout seconds based on agent role."""
        if role == "leader":
            return self.config.leader_timeout_sec
        return self.config.worker_timeout_sec

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def _create_claude_session(
        self,
        session_name: str,
        worktree_name: str | None = None,
    ) -> bool:
        """Create a tmux session and launch Claude interactive mode inside it.

        2-step approach:
        1. Create empty tmux session
        2. send-keys to launch Claude inside the shell
        """
        created = await self.tmux.create(session_name)
        if not created:
            return False

        self._active_sessions.add(session_name)

        cmd_parts = [self.config.claude_bin, "--dangerously-skip-permissions"]
        if worktree_name and self.config.use_worktree:
            cmd_parts.extend(["--worktree", worktree_name])

        launch_cmd = (
            f"cd {shlex.quote(str(self.config.work_path))} && {' '.join(cmd_parts)}"
        )

        await self.tmux.send_text(session_name, launch_cmd)
        await asyncio.sleep(0.2)
        await self.tmux.send_keys_raw(session_name, "C-m")

        # Wait for Claude to initialize
        await asyncio.sleep(5)

        return True

    async def _handle_startup_prompts(
        self,
        session_name: str,
        timeout: int = 60,
    ) -> bool:
        """Handle Claude startup prompts, wait for ready.

        Polls capture-pane and auto-answers prompts:
        1. Bypass Permissions warning → send Down + Enter
        2. Trust folder prompt → send Enter
        3. Other Y/N → send 'y' + Enter
        4. "Enter to confirm" → send Enter

        Returns True when prompt marker (❯) appears.
        """
        marker = self.config.prompt_marker
        start = time.monotonic()
        max_attempts = 30

        for attempt in range(1, max_attempts + 1):
            if time.monotonic() - start > timeout:
                break

            output = await self.tmux.capture_pane(session_name)
            if not output.strip():
                await asyncio.sleep(self.config.poll_interval_sec)
                continue

            last_lines = output.strip().splitlines()[-10:]
            tail = "\n".join(last_lines)
            tail_lower = tail.lower()

            # Check: Bypass Permissions PROMPT
            if "no, exit" in tail_lower and "yes, i accept" in tail_lower:
                logger.info("Bypass Permissions prompt detected (attempt %d)", attempt)
                await self.tmux.send_keys_raw(session_name, "Down")
                await asyncio.sleep(0.3)
                await self.tmux.send_keys_raw(session_name, "Enter")
                await asyncio.sleep(2)
                continue

            # Check: Trust folder prompt
            if "trust this folder" in tail_lower or "safety check" in tail_lower:
                logger.info("Trust folder prompt detected (attempt %d)", attempt)
                await self.tmux.send_keys_raw(session_name, "Enter")
                await asyncio.sleep(2)
                continue

            # Check: Multi-option confirmation
            if "do you want to proceed?" in tail_lower and "1. yes" in tail_lower:
                logger.info("Multi-option confirmation detected (attempt %d)", attempt)
                await self.tmux.send_text(session_name, "2")
                await asyncio.sleep(0.3)
                await self.tmux.send_keys_raw(session_name, "Enter")
                await asyncio.sleep(2)
                continue

            # Check: Single Y/N prompt
            if "(y/n)" in tail_lower or "[y/n]" in tail_lower:
                logger.info("Y/N prompt detected (attempt %d)", attempt)
                await self.tmux.send_text(session_name, "y")
                await asyncio.sleep(0.3)
                await self.tmux.send_keys_raw(session_name, "Enter")
                await asyncio.sleep(1)
                continue

            # Check: "Enter to confirm" / "Press Enter"
            if "enter to confirm" in tail_lower or "press enter" in tail_lower:
                logger.info("Enter prompt detected (attempt %d)", attempt)
                await self.tmux.send_keys_raw(session_name, "Enter")
                await asyncio.sleep(1)
                continue

            # Check: Claude is ready (prompt marker ❯)
            for line in last_lines:
                if marker in line and "bypass" not in line.lower():
                    elapsed = time.monotonic() - start
                    logger.info(
                        "Claude ready in session %s (%.1fs, %d attempts)",
                        session_name,
                        elapsed,
                        attempt,
                    )
                    return True

            # Check: Still loading
            if any(w in tail_lower for w in ["loading", "connecting", "starting"]):
                logger.debug("Claude still loading (attempt %d)", attempt)

            await asyncio.sleep(self.config.poll_interval_sec)

        logger.error(
            "Timed out waiting for Claude in %s after %ds", session_name, timeout
        )
        return False

    # ------------------------------------------------------------------
    # Command injection
    # ------------------------------------------------------------------

    async def _inject_prompt(
        self,
        session_name: str,
        prompt: str,
    ) -> bool:
        """Inject a prompt into Claude's tmux session.

        Always writes prompt to a temp file and tells Claude to read it,
        avoiding shell escaping issues with send-keys for long text.

        Steps:
        1. Write prompt to /tmp file
        2. C-u (clear input)
        3. send-keys instruction to read the file
        4. C-m (Enter)
        """
        # Always use file-based prompt injection to avoid escaping issues
        prompt_file = Path(f"/tmp/xai_prompt_{session_name}.txt")
        prompt_file.write_text(prompt, encoding="utf-8")

        instruction = (
            f"Read and follow ALL instructions in the file {prompt_file}. "
            f"Execute the task as described. Do NOT ask for clarification."
        )

        # Step 1: Clear input field
        await self.tmux.send_keys_raw(session_name, "C-u")
        await asyncio.sleep(0.2)

        # Step 2: Send instruction
        await self.tmux.send_text(session_name, instruction)
        logger.debug(
            "Sent file-based prompt (%d chars → %s) to %s",
            len(prompt),
            prompt_file.name,
            session_name,
        )

        await asyncio.sleep(0.2)

        # Step 3: Press Enter
        await self.tmux.send_keys_raw(session_name, "C-m")

        # Brief wait
        await asyncio.sleep(1)
        return True

    # ------------------------------------------------------------------
    # Wait for result file + handle runtime confirmations
    # ------------------------------------------------------------------

    async def _wait_for_result_file(
        self,
        session_name: str,
        result_file: Path,
        timeout: int,
    ) -> bool:
        """Wait for Claude to write the result file on disk.

        Polls the filesystem for result_file to appear.
        Also monitors tmux capture-pane for confirmations that need
        auto-answering, but does NOT use capture-pane for output.

        Returns True if result file appeared, False on timeout.
        """
        processing_indicators = [
            "thinking",
            "sprouting",
            "clauding",
            "working",
            "reading",
            "writing",
            "processing",
            "waiting",
            "searching",
            "analyzing",
            "running",
            "building",
            "crafting",
            "composing",
            "generating",
            "editing",
            "envisioning",
            "creating",
        ]

        start = time.monotonic()
        logger.info(
            "Waiting for result file %s (timeout: %ds)", result_file.name, timeout
        )

        # Minimum wait — Claude needs time to read prompt and start
        min_wait = 10
        logger.debug("Phase 1: minimum wait %ds for processing to start", min_wait)
        await asyncio.sleep(min_wait)

        # Phase 2: Poll for result file + handle confirmations
        poll_interval = 2.0

        while time.monotonic() - start < timeout:
            # Check if result file exists and has content
            if result_file.exists():
                try:
                    content = result_file.read_text(encoding="utf-8").strip()
                    if content and "---" in content:
                        # Result file has frontmatter — verify it's complete
                        # Wait a moment to ensure file write is finished
                        await asyncio.sleep(1)
                        content2 = result_file.read_text(encoding="utf-8").strip()
                        if content2 == content:
                            elapsed = time.monotonic() - start
                            logger.info(
                                "Result file appeared: %s (%.1fs)",
                                result_file.name,
                                elapsed,
                            )
                            return True
                        # File still being written, continue polling
                except (OSError, UnicodeDecodeError):
                    pass  # File may be partially written

            # Monitor tmux for confirmations
            output = await self.tmux.capture_pane(session_name)
            if output.strip():
                lines = output.strip().splitlines()
                tail = "\n".join(lines[-10:]).lower() if lines else ""

                # Handle runtime confirmations
                if "do you want to proceed?" in tail:
                    logger.info("Runtime confirmation detected, auto-accepting")
                    await self.tmux.send_text(session_name, "2")
                    await asyncio.sleep(0.3)
                    await self.tmux.send_keys_raw(session_name, "Enter")
                    await asyncio.sleep(2)
                    continue

                if "(y/n)" in tail or "[y/n]" in tail:
                    logger.info("Runtime Y/N prompt, sending 'y'")
                    await self.tmux.send_text(session_name, "y")
                    await asyncio.sleep(0.3)
                    await self.tmux.send_keys_raw(session_name, "Enter")
                    await asyncio.sleep(1)
                    continue

                # Log processing status
                is_processing = any(ind in tail for ind in processing_indicators)
                if is_processing:
                    logger.debug(
                        "Still processing (%ds elapsed)",
                        int(time.monotonic() - start),
                    )

            await asyncio.sleep(poll_interval)

        logger.warning(
            "Timed out waiting for result file %s after %ds",
            result_file.name,
            timeout,
        )
        return False

    # ------------------------------------------------------------------
    # Leader
    # ------------------------------------------------------------------

    async def run_leader(self, task_file: Path) -> AgentResult:
        """Run Claude as Leader in native interactive tmux session.

        File-based flow:
        1. Create tmux session → handle startup prompts
        2. Inject prompt (read task file + write result file)
        3. Poll filesystem for result file
        4. Parse result file → AgentResult
        """
        for binary, label in [
            (self.config.claude_bin, "claude"),
            (self.config.tmux_bin, "tmux"),
        ]:
            if not shutil.which(binary):
                return AgentResult.error(f"{label} binary not found: {binary}")

        task_id = task_file.stem.replace(".task", "")
        timeout = self._get_timeout("leader")
        result_file = task_file.parent / f"{task_id}.result.md"
        session_name = f"xai_leader_{task_id[:8]}"

        log_agent_start("claude:leader", task_id)

        try:
            # 1. Create session with Claude
            created = await self._create_claude_session(session_name)
            if not created:
                return AgentResult.error(f"Failed to create session: {session_name}")

            # 2. Handle startup prompts
            ready = await self._handle_startup_prompts(session_name)
            if not ready:
                log_agent_done("claude:leader", task_id, "error")
                return AgentResult.error(f"Claude failed to start in {session_name}")

            # 3. Inject prompt (file-based)
            prompt = self._build_prompt(task_file, result_file, "leader")
            await self._inject_prompt(session_name, prompt)

            # 4. Wait for result file on disk
            found = await self._wait_for_result_file(session_name, result_file, timeout)

            if not found:
                log_agent_done("claude:leader", task_id, "timeout")
                return AgentResult.error(
                    f"claude:leader timed out after {timeout}s — "
                    f"result file not written: {result_file}"
                )

        except Exception as e:
            logger.error("claude:leader error: %s", e, exc_info=True)
            log_agent_done("claude:leader", task_id, "error")
            return AgentResult.error(f"claude:leader error: {e}")

        finally:
            await self.tmux.kill(session_name)
            self._active_sessions.discard(session_name)

        # 5. Parse result file
        result = AgentResult.from_file(result_file)
        log_agent_done("claude:leader", task_id, result.status)
        return result

    # ------------------------------------------------------------------
    # Workers
    # ------------------------------------------------------------------

    async def run_worker(
        self,
        worker_name: str,
        task_file: Path,
        run_id: str,
    ) -> AgentResult:
        """Run a Claude worker in native interactive tmux session.

        File-based flow — same as run_leader.
        """
        for binary, label in [
            (self.config.claude_bin, "claude"),
            (self.config.tmux_bin, "tmux"),
        ]:
            if not shutil.which(binary):
                return AgentResult.error(f"{label} binary not found: {binary}")

        task_id = task_file.stem.replace(".task", "")
        timeout = self._get_timeout("worker")
        result_file = task_file.parent / f"{task_id}.result.md"
        session_name = f"xai_{run_id}_{worker_name}"
        worktree_name = f"xai_{worker_name}" if self.config.use_worktree else None

        log_agent_start(worker_name, task_id)

        try:
            created = await self._create_claude_session(session_name, worktree_name)
            if not created:
                return AgentResult.error(f"Failed to create session: {session_name}")

            ready = await self._handle_startup_prompts(session_name)
            if not ready:
                log_agent_done(worker_name, task_id, "error")
                return AgentResult.error(f"Claude failed to start in {session_name}")

            prompt = self._build_prompt(task_file, result_file, "worker")
            await self._inject_prompt(session_name, prompt)

            found = await self._wait_for_result_file(session_name, result_file, timeout)

            if not found:
                log_agent_done(worker_name, task_id, "timeout")
                return AgentResult.error(
                    f"{worker_name} timed out after {timeout}s — "
                    f"result file not written: {result_file}"
                )

        except Exception as e:
            logger.error("%s error: %s", worker_name, e, exc_info=True)
            log_agent_done(worker_name, task_id, "error")
            return AgentResult.error(f"{worker_name} error: {e}")

        finally:
            await self.tmux.kill(session_name)
            self._active_sessions.discard(session_name)

        result = AgentResult.from_file(result_file)
        log_agent_done(worker_name, task_id, result.status)
        return result

    async def run_workers_parallel(
        self,
        task_files: dict[str, Path],
        run_id: str,
    ) -> dict[str, AgentResult]:
        """Run multiple Claude workers in parallel via tmux sessions."""
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
                logger.error("%s failed: %s", worker_name, result)
                results[worker_name] = AgentResult.error(str(result))
            else:
                results[worker_name] = result

        return results
