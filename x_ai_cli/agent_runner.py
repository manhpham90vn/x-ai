"""Agent subprocess launcher for x-ai CLI orchestrator.

Launches Claude CLI instances in interactive mode inside tmux sessions.
Controls them natively via tmux send-keys — following the approach from
Claude-Code-Remote's tmux-injector.js.

Key patterns (from tmux-injector.js):
- Create session WITH claude command: tmux new-session -d -s name claude ...
- Inject: C-u → send-keys 'escaped_text' → C-m  (via shell exec)
- Confirmations: poll capture-pane, detect prompts, auto-answer
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
    """Manages tmux sessions — mirrors tmux-injector.js approach.

    Uses shell execution (create_subprocess_shell) for send-keys commands
    so that single-quote escaping works correctly, exactly like the JS
    version's exec() calls.
    """

    def __init__(self, tmux_bin: str = "tmux") -> None:
        self.tmux_bin = tmux_bin

    async def _shell(self, cmd: str) -> tuple[int, str, str]:
        """Run a shell command and return (returncode, stdout, stderr).

        Uses shell execution — same as Node.js exec() in tmux-injector.js.
        """
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

    async def _exec(self, *args: str) -> tuple[int, str, str]:
        """Run a command directly (no shell) and return (returncode, stdout, stderr)."""
        proc = await asyncio.create_subprocess_exec(
            *args,
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

    async def create_with_command(
        self,
        session_name: str,
        command: str,
        work_dir: str = ".",
    ) -> bool:
        """Create a tmux session that immediately runs a command.

        Mirrors tmux-injector.js createClaudeSession():
            tmux new-session -d -s name -c cwd command
        """
        cmd = (
            f"{self.tmux_bin} new-session -d"
            f" -s {shlex.quote(session_name)}"
            f" -x 250 -y 50"
            f" -c {shlex.quote(work_dir)}"
            f" {command}"
        )
        rc, _, stderr = await self._shell(cmd)
        if rc != 0:
            logger.error("Failed to create tmux session %s: %s", session_name, stderr)
            return False
        logger.debug("Created tmux session: %s (cmd: %s)", session_name, command)
        return True

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

        Mirrors tmux-injector.js injectCommand():
            tmux send-keys -t session 'escaped_text'

        Uses single-quote shell escaping (replace ' with '"'"')
        so tmux receives the text literally, not as key names.
        """
        # Shell single-quote escape: replace ' with '"'"'
        escaped = text.replace("'", "'\"'\"'")
        cmd = f"{self.tmux_bin} send-keys -t {shlex.quote(session_name)} '{escaped}'"
        await self._shell(cmd)

    async def capture_pane(self, session_name: str) -> str:
        """Capture visible pane content.

        Mirrors tmux-injector.js getCaptureOutput():
            tmux capture-pane -t session -p
        """
        _, stdout, _ = await self._shell(
            f"{self.tmux_bin} capture-pane -t {shlex.quote(session_name)} -p"
        )
        return stdout

    async def capture_full(self, session_name: str) -> str:
        """Capture entire scrollback (up to 50000 lines)."""
        _, stdout, _ = await self._shell(
            f"{self.tmux_bin} capture-pane -t {shlex.quote(session_name)}"
            f" -p -S -50000 -J"
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

    Follows the tmux-injector.js approach from Claude-Code-Remote:
    1. Create session with claude command
    2. Handle startup prompts (bypass permissions, trust folder)
    3. Inject prompts: C-u → send-keys 'text' → C-m
    4. Poll capture-pane for completion
    5. Handle confirmations automatically
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

    def _get_timeout(self, role: str) -> int:
        """Get timeout seconds based on agent role."""
        if role == "leader":
            return self.config.leader_timeout_sec
        return self.config.worker_timeout_sec

    # ------------------------------------------------------------------
    # Session lifecycle (mirrors tmux-injector.js)
    # ------------------------------------------------------------------

    async def _create_claude_session(
        self,
        session_name: str,
        worktree_name: str | None = None,
    ) -> bool:
        """Create a tmux session and launch Claude interactive mode inside it.

        2-step approach (because `tmux new-session -d -s name command` runs
        the command WITHOUT a shell, so Claude exits immediately):
        1. Create empty tmux session
        2. send-keys to launch Claude inside the shell
        """
        # Step 1: Create empty session
        created = await self.tmux.create(session_name)
        if not created:
            return False

        self._active_sessions.add(session_name)

        # Step 2: cd to work_dir and launch Claude
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

        Mirrors tmux-injector.js handleConfirmations() — polls capture-pane
        and auto-answers prompts:

        1. Bypass Permissions warning → send Down + Enter (select "Yes, I accept")
        2. Trust folder prompt → send Enter (default is "Yes")
        3. Other Y/N → send 'y' + Enter
        4. "Enter to confirm" → send Enter

        Returns True when prompt marker (❯) appears.
        """
        marker = self.config.prompt_marker
        start = time.monotonic()
        max_attempts = 30  # like tmux-injector.js maxAttempts

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

            # Check: Bypass Permissions warning PROMPT (the actual dialog)
            # Must distinguish from status bar "⏵⏵ bypass permissions on"
            # The PROMPT has "No, exit" and "Yes, I accept" options
            if "no, exit" in tail_lower and "yes, i accept" in tail_lower:
                logger.info("Bypass Permissions prompt detected (attempt %d)", attempt)
                await self.tmux.send_keys_raw(session_name, "Down")
                await asyncio.sleep(0.3)
                await self.tmux.send_keys_raw(session_name, "Enter")
                await asyncio.sleep(2)
                continue

            # Check: Trust folder prompt (default = "Yes")
            # → just Enter
            if "trust this folder" in tail_lower or "safety check" in tail_lower:
                logger.info("Trust folder prompt detected (attempt %d)", attempt)
                await self.tmux.send_keys_raw(session_name, "Enter")
                await asyncio.sleep(2)
                continue

            # Check: Multi-option confirmation (like tmux-injector.js)
            # → select option 2 "Yes, and don't ask again"
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

            # Check: Claude is ready (prompt marker ❯ anywhere in last lines)
            # Note: ❯ is NOT on the very last line — it's above the
            # status bar "⏵⏵ bypass permissions on (shift+tab to cycle)"
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
    # Command injection (mirrors tmux-injector.js injectCommand)
    # ------------------------------------------------------------------

    async def _inject_command(
        self,
        session_name: str,
        command: str,
    ) -> bool:
        """Inject a command into Claude's tmux session.

        Exact replica of tmux-injector.js injectCommand():
        1. tmux send-keys -t session C-u          (clear input)
        2. tmux send-keys -t session 'escaped'    (send text)
        3. tmux send-keys -t session C-m          (press Enter)

        For long commands (>4000 chars), writes to file and sends
        a reference instruction instead.
        """
        # Step 1: Clear input field
        await self.tmux.send_keys_raw(session_name, "C-u")
        await asyncio.sleep(0.2)

        # Step 2: Send command text
        if len(command) > 4000:
            # Long command → write to file, send read instruction
            prompt_file = Path(f"/tmp/xai_prompt_{session_name}.txt")
            prompt_file.write_text(command, encoding="utf-8")
            instruction = (
                f"Read and follow ALL instructions in the file {prompt_file}. "
                f"Output your result exactly as described in the instructions."
            )
            await self.tmux.send_text(session_name, instruction)
            logger.debug(
                "Sent file-based prompt (%d chars → %s) to %s",
                len(command),
                prompt_file.name,
                session_name,
            )
        else:
            await self.tmux.send_text(session_name, command)
            logger.debug("Sent prompt (%d chars) to %s", len(command), session_name)

        await asyncio.sleep(0.2)

        # Step 3: Press Enter (C-m)
        await self.tmux.send_keys_raw(session_name, "C-m")

        # Brief wait then verify (like tmux-injector.js)
        await asyncio.sleep(1)
        output = await self.tmux.capture_pane(session_name)
        logger.debug("State after inject: %s", output.strip()[-200:].replace("\n", " "))

        return True

    # ------------------------------------------------------------------
    # Wait for response + handle runtime confirmations
    # ------------------------------------------------------------------

    async def _wait_for_response(
        self,
        session_name: str,
        timeout: int,
    ) -> str | None:
        """Wait for Claude to finish responding.

        Robust approach:
        1. Wait minimum 10s for Claude to start processing
        2. Then poll every 2s for completion:
           - Claude is DONE when capture-pane has NO processing indicators
             AND an empty ❯ line (no text after it)
           - Must see this state in TWO consecutive polls to avoid
             false positives from TUI transitions

        Returns the full captured scrollback, or None on timeout.
        """
        marker = self.config.prompt_marker
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
        logger.info("Waiting for response in %s (timeout: %ds)", session_name, timeout)

        # Minimum wait before checking completion — Claude needs time to
        # read the prompt file, think, and start generating output
        min_wait = 10
        logger.debug("Phase 1: minimum wait %ds for processing to start", min_wait)
        await asyncio.sleep(min_wait)

        # Phase 2: Poll for completion with double-check
        consecutive_done = 0
        poll_interval = 2.0

        while time.monotonic() - start < timeout:
            output = await self.tmux.capture_pane(session_name)
            lines = output.strip().splitlines() if output.strip() else []
            tail = "\n".join(lines[-10:]).lower() if lines else ""

            # Handle runtime confirmations
            if "do you want to proceed?" in tail:
                logger.info("Runtime confirmation detected, auto-accepting")
                await self.tmux.send_text(session_name, "2")
                await asyncio.sleep(0.3)
                await self.tmux.send_keys_raw(session_name, "Enter")
                await asyncio.sleep(2)
                consecutive_done = 0
                continue

            if "(y/n)" in tail or "[y/n]" in tail:
                logger.info("Runtime Y/N prompt, sending 'y'")
                await self.tmux.send_text(session_name, "y")
                await asyncio.sleep(0.3)
                await self.tmux.send_keys_raw(session_name, "Enter")
                await asyncio.sleep(1)
                consecutive_done = 0
                continue

            # Check if still processing
            is_processing = any(ind in tail for ind in processing_indicators)

            if is_processing:
                consecutive_done = 0
                logger.debug(
                    "Still processing (%ds elapsed)",
                    int(time.monotonic() - start),
                )
                await asyncio.sleep(poll_interval)
                continue

            # Not processing — check for empty ❯ marker
            has_empty_marker = False
            for line in lines[-8:] if lines else []:
                if marker in line and "bypass" not in line.lower():
                    after_marker = line.split(marker, 1)[-1].strip()
                    if not after_marker:
                        has_empty_marker = True
                        break

            if has_empty_marker:
                consecutive_done += 1
                if consecutive_done >= 2:
                    elapsed = time.monotonic() - start
                    logger.info(
                        "Response completed in %s (%.1fs, confirmed)",
                        session_name,
                        elapsed,
                    )
                    return await self.tmux.capture_full(session_name)
                else:
                    logger.debug(
                        "Possible completion (check %d/2, %ds elapsed)",
                        consecutive_done,
                        int(time.monotonic() - start),
                    )
            else:
                consecutive_done = 0

            await asyncio.sleep(poll_interval)

        logger.warning("Timed out waiting for response in %s", session_name)
        return await self.tmux.capture_full(session_name)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _extract_response(self, full_output: str) -> str:
        """Extract Claude's response from tmux scrollback.

        Finds text between the last two prompt markers (❯).
        """
        marker = self.config.prompt_marker
        lines = full_output.strip().splitlines()

        # Find last marker
        last_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            if marker in lines[i]:
                last_idx = i
                break

        if last_idx <= 0:
            return full_output.strip()

        # Find second-to-last marker
        prev_idx = -1
        for i in range(last_idx - 1, -1, -1):
            if marker in lines[i]:
                prev_idx = i
                break

        if prev_idx >= 0:
            response_lines = lines[prev_idx + 1 : last_idx]
        else:
            response_lines = lines[:last_idx]

        return "\n".join(response_lines).strip()

    def _parse_response_to_result(self, task_id: str, response: str) -> AgentResult:
        """Parse Claude's text response into an AgentResult."""
        response = response.strip()
        if not response:
            return AgentResult.error("Agent produced no output")

        meta, body = parse_frontmatter(response)

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
                body=response,
            )

    def _save_result_file(self, result: AgentResult, result_file: Path) -> None:
        """Save an AgentResult to .result.md for audit."""
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
    # Leader (mirrors tmux-injector.js injectCommandFull)
    # ------------------------------------------------------------------

    async def run_leader(self, task_file: Path) -> AgentResult:
        """Run Claude as Leader in native interactive tmux session."""
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
            # 1. Create session with Claude (like createClaudeSession)
            created = await self._create_claude_session(session_name)
            if not created:
                return AgentResult.error(f"Failed to create session: {session_name}")

            # 2. Handle startup prompts (like handleConfirmations)
            ready = await self._handle_startup_prompts(session_name)
            if not ready:
                log_agent_done("claude:leader", task_id, "error")
                return AgentResult.error(f"Claude failed to start in {session_name}")

            # 3. Inject prompt (like injectCommand)
            prompt = self._build_prompt(task_file, "leader")
            await self._inject_command(session_name, prompt)

            # 4. Wait for response (+ handle runtime confirmations)
            raw_output = await self._wait_for_response(session_name, timeout)
            if raw_output is None:
                log_agent_done("claude:leader", task_id, "timeout")
                return AgentResult.error(f"claude:leader timed out after {timeout}s")

            # 5. Extract response
            response = self._extract_response(raw_output)

        except Exception as e:
            logger.error("claude:leader error: %s", e, exc_info=True)
            log_agent_done("claude:leader", task_id, "error")
            return AgentResult.error(f"claude:leader error: {e}")

        finally:
            await self.tmux.kill(session_name)
            self._active_sessions.discard(session_name)

        # Check if agent wrote a result file
        if result_file.exists():
            result = AgentResult.from_file(result_file)
            log_agent_done("claude:leader", task_id, result.status)
            return result

        result = self._parse_response_to_result(task_id, response)
        self._save_result_file(result, result_file)
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
        """Run a Claude worker in native interactive tmux session."""
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

            prompt = self._build_prompt(task_file, "worker")
            await self._inject_command(session_name, prompt)

            raw_output = await self._wait_for_response(session_name, timeout)
            if raw_output is None:
                log_agent_done(worker_name, task_id, "timeout")
                return AgentResult.error(f"{worker_name} timed out after {timeout}s")

            response = self._extract_response(raw_output)

        except Exception as e:
            logger.error("%s error: %s", worker_name, e, exc_info=True)
            log_agent_done(worker_name, task_id, "error")
            return AgentResult.error(f"{worker_name} error: {e}")

        finally:
            await self.tmux.kill(session_name)
            self._active_sessions.discard(session_name)

        if result_file.exists():
            result = AgentResult.from_file(result_file)
            log_agent_done(worker_name, task_id, result.status)
            return result

        result = self._parse_response_to_result(task_id, response)
        self._save_result_file(result, result_file)
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
