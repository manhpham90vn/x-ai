"""Orchestrator pipeline for x-ai CLI.

Implements the full pipeline: DECOMPOSE → WORKERS → REVIEW → EXECUTE → MERGE
with round loop, quality gate, and feedback mechanism.

All workers are Claude CLI instances running in parallel via tmux sessions
with optional git worktree isolation.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from x_ai_cli.agent_runner import AgentRunner
from x_ai_cli.config import Config
from x_ai_cli.logger import (
    logger,
    log_phase,
    log_round,
    log_score,
)
from x_ai_cli.models import (
    AgentResult,
    AgentTask,
    ExecutionResult,
    Feedback,
    ReviewResult,
    SubTask,
    new_id,
)


@dataclass
class PipelineResult:
    """Final result of the orchestrator pipeline."""

    status: str = "success"  # success | best_effort | failed
    solution: AgentResult | None = None
    rounds_used: int = 0
    warnings: list[str] = field(default_factory=list)


class Orchestrator:
    """Main orchestrator that runs the multi-agent pipeline."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.runner = AgentRunner(config)
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    def _worker_names(self) -> list[str]:
        """Generate worker names based on config.num_workers."""
        return [f"claude_worker_{i}" for i in range(self.config.num_workers)]

    def _task_dir(self, round_num: int | str, agent_name: str) -> Path:
        """Get task directory: tasks/{run_id}/round_{n}/{agent_name}/

        Structure:
            tasks/{run_id}/
            ├── round_0/claude/             # DECOMPOSE phase
            ├── round_1/
            │   ├── claude_worker_0/        # Worker 0
            │   ├── claude_worker_1/        # Worker 1
            │   └── claude/                 # REVIEW phase
            ├── round_2/...
            └── merge/claude/              # MERGE phase
        """
        if isinstance(round_num, int):
            round_dir = f"round_{round_num}"
        else:
            round_dir = str(round_num)  # e.g. "merge"
        d = self.config.tasks_path / self.run_id / round_dir / agent_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def run_pipeline(self, user_request: str) -> PipelineResult:
        """Run the full orchestration pipeline.

        Flow: DECOMPOSE → (WORKERS → REVIEW → EXECUTE)×rounds → MERGE
        """
        self.config.ensure_dirs()

        # Phase 1: Decompose
        log_phase("DECOMPOSE", "Leader splitting task into sub-tasks")
        sub_tasks = await self.decompose_task(user_request)

        if not sub_tasks:
            logger.error("Leader failed to decompose task")
            return PipelineResult(status="failed", warnings=["Decomposition failed"])

        logger.info("Decomposed into %d sub-task(s)", len(sub_tasks))

        # Process each sub-task through the round loop
        all_results: list[PipelineResult] = []
        for i, sub_task in enumerate(sub_tasks):
            logger.info(
                "Processing sub-task %d/%d: %s",
                i + 1,
                len(sub_tasks),
                sub_task.description[:80],
            )
            result = await self._process_sub_task(sub_task, user_request)
            all_results.append(result)

        # Phase 5: Merge (if multiple sub-tasks)
        if len(all_results) == 1:
            return all_results[0]

        log_phase("MERGE", "Leader merging all sub-task results")
        return await self._merge_all(user_request, sub_tasks, all_results)

    async def decompose_task(self, user_request: str) -> list[SubTask]:
        """Ask the Leader to decompose the user request into sub-tasks."""
        task = AgentTask(
            task_type="decompose",
            work_dir=str(self.config.work_path),
            instructions=user_request,
        )
        task_dir = self._task_dir(0, "claude")
        task_file = task.write(task_dir)
        result = await self.runner.run_leader(task_file)

        if result.status == "error":
            logger.error("Decompose failed: %s", result.error_message)
            return []

        return self._parse_sub_tasks(result.body)

    async def _process_sub_task(
        self,
        sub_task: SubTask,
        original_request: str,
    ) -> PipelineResult:
        """Process a single sub-task through the round loop."""
        feedback: Feedback | None = None
        best_solution: AgentResult | None = None
        best_score: float = 0.0

        for round_num in range(1, self.config.max_rounds + 1):
            log_round(round_num, self.config.max_rounds)

            # Phase 2: Workers generate code (parallel via tmux)
            log_phase(
                "WORKERS",
                f"Generating code (round {round_num}, "
                f"{self.config.num_workers} workers)",
            )
            worker_results = await self.run_workers(
                sub_task, round_num, feedback, original_request,
            )

            successful = {k: v for k, v in worker_results.items() if v.status != "error"}
            if not successful:
                logger.error("No workers produced a result in round %d", round_num)
                continue

            # Phase 3: Leader reviews and scores
            log_phase("REVIEW", f"Leader scoring {len(successful)} solution(s)")
            review = await self.review_solutions(sub_task, successful, round_num)

            if review.best_score > best_score:
                best_score = review.best_score
                best_solution = review.best_solution

            log_score("Best", review.best_score, self.config.quality_threshold)

            # Quality gate
            if review.best_score >= self.config.quality_threshold:
                # Phase 4: Leader runs tests (format, lint, unit tests)
                log_phase("EXECUTE", "Leader running tests")
                exec_result = await self.execute_tests(sub_task, round_num)

                if exec_result.tests_passed:
                    logger.info("Tests passed ✓")
                    return PipelineResult(
                        status="success",
                        solution=best_solution,
                        rounds_used=round_num,
                    )
                else:
                    logger.warning("Tests failed, building feedback for retry")
                    feedback = self._build_feedback(review, exec_result)
                    continue
            else:
                # Score too low — retry with feedback
                logger.warning(
                    "Score %.1f below threshold %.1f, retrying",
                    review.best_score,
                    self.config.quality_threshold,
                )
                feedback = self._build_feedback(review, exec_result=None)

        # Exhausted all rounds
        logger.warning("Max rounds (%d) exhausted — returning best-effort", self.config.max_rounds)
        return PipelineResult(
            status="best_effort",
            solution=best_solution,
            rounds_used=self.config.max_rounds,
            warnings=["Max rounds exhausted, returning best available solution"],
        )

    async def run_workers(
        self,
        sub_task: SubTask,
        round_num: int,
        feedback: Feedback | None,
        original_request: str,
    ) -> dict[str, AgentResult]:
        """Dispatch sub-task to N Claude workers in parallel via tmux."""
        worker_names = self._worker_names()
        task_files: dict[str, Path] = {}

        instructions = (
            f"Original request: {original_request}\n\n"
            f"Sub-task: {sub_task.description}"
        )
        if sub_task.constraints:
            instructions += f"\n\nConstraints:\n" + "\n".join(
                f"- {c}" for c in sub_task.constraints
            )

        for worker_name in worker_names:
            task = AgentTask(
                task_type="generate",
                work_dir=str(self.config.work_path),
                round=round_num,
                instructions=instructions,
                feedback=feedback,
            )
            task_dir = self._task_dir(round_num, worker_name)
            task_file = task.write(task_dir)
            task_files[worker_name] = task_file

        return await self.runner.run_workers_parallel(task_files, self.run_id)

    async def review_solutions(
        self,
        sub_task: SubTask,
        worker_results: dict[str, AgentResult],
        round_num: int,
    ) -> ReviewResult:
        """Ask the Leader to review and score all worker solutions."""
        # Build review prompt with all solutions
        solutions_text = ""
        for worker_name, result in worker_results.items():
            solutions_text += (
                f"### {worker_name} Solution\n\n"
                f"**Status**: {result.status}\n"
                f"**Files changed**: {', '.join(result.files_changed) if result.files_changed else 'N/A'}\n\n"
                f"{result.body}\n\n---\n\n"
            )

        instructions = (
            f"Review and score the following worker solutions for sub-task: "
            f"{sub_task.description}\n\n"
            f"{solutions_text}\n"
            f"Score each solution (0-100) on: correctness, code_quality, "
            f"security, performance, maintainability.\n"
            f"Provide weighted average and recommendation."
        )

        task = AgentTask(
            task_type="review",
            work_dir=str(self.config.work_path),
            round=round_num,
            instructions=instructions,
        )
        task_dir = self._task_dir(round_num, "claude")
        task_file = task.write(task_dir)
        result = await self.runner.run_leader(task_file)

        review = ReviewResult()

        if result.status != "error" and result.score is not None:
            review.best_score = result.score
            review.merge_plan = result.body

            # Try to identify best worker from the body
            best_worker = self._extract_best_worker(result.body, worker_results)
            if best_worker:
                review.best_worker = best_worker
                review.best_solution = worker_results.get(best_worker)
        else:
            # Fallback: pick first successful worker
            for worker_name, wr in worker_results.items():
                if wr.status != "error":
                    review.best_worker = worker_name
                    review.best_solution = wr
                    review.best_score = 50.0  # default low score
                    break

        return review

    async def execute_tests(
        self,
        sub_task: SubTask,
        round_num: int,
    ) -> ExecutionResult:
        """Ask the Leader to run formatter, linter, and unit tests."""
        instructions = (
            f"Run quality checks on the code in `{self.config.work_path}` "
            f"for sub-task: {sub_task.description}\n\n"
            f"You MUST execute the following steps in order:\n\n"
            f"1. **Detect project type** — look at the files in work_dir to determine "
            f"the language/framework (Python, Node.js, Go, Rust, etc.)\n\n"
            f"2. **Format** — run the appropriate formatter:\n"
            f"   - Python: `ruff format .` or `black .`\n"
            f"   - Node.js: `npx prettier --write .`\n"
            f"   - Go: `gofmt -w .`\n"
            f"   - Rust: `cargo fmt`\n\n"
            f"3. **Lint** — run the appropriate linter:\n"
            f"   - Python: `ruff check . --fix` or `flake8`\n"
            f"   - Node.js: `npx eslint . --fix`\n"
            f"   - Go: `golangci-lint run`\n"
            f"   - Rust: `cargo clippy`\n\n"
            f"4. **Unit tests** — run the test suite if one exists:\n"
            f"   - Python: `pytest` or `python -m unittest`\n"
            f"   - Node.js: `npm test`\n"
            f"   - Go: `go test ./...`\n"
            f"   - Rust: `cargo test`\n\n"
            f"In your result frontmatter:\n"
            f"- Set `status: success` if ALL checks pass\n"
            f"- Set `status: error` if ANY check fails\n"
            f"- Include the full output of each step in the body\n"
            f"- List any errors clearly so workers can fix them"
        )

        task = AgentTask(
            task_type="execute",
            work_dir=str(self.config.work_path),
            round=round_num,
            instructions=instructions,
        )
        task_dir = self._task_dir(round_num, "claude")
        task_file = task.write(task_dir)
        result = await self.runner.run_leader(task_file)

        # Map AgentResult → ExecutionResult
        tests_passed = result.status == "success"
        return ExecutionResult(
            exit_code=0 if tests_passed else 1,
            stdout=result.body,
            stderr=result.error_message or "",
            tests_passed=tests_passed,
        )

    async def _merge_all(
        self,
        original_request: str,
        sub_tasks: list[SubTask],
        results: list[PipelineResult],
    ) -> PipelineResult:
        """Ask Leader to merge all sub-task results into final solution."""
        merge_text = ""
        for sub_task, result in zip(sub_tasks, results):
            merge_text += (
                f"### Sub-task: {sub_task.description}\n"
                f"**Status**: {result.status}\n\n"
            )
            if result.solution:
                merge_text += f"{result.solution.body}\n\n---\n\n"

        task = AgentTask(
            task_type="merge",
            work_dir=str(self.config.work_path),
            instructions=(
                f"Original request: {original_request}\n\n"
                f"Merge the following sub-task solutions into a cohesive final solution:\n\n"
                f"{merge_text}"
            ),
        )
        task_dir = self._task_dir("merge", "claude")
        task_file = task.write(task_dir)
        result = await self.runner.run_leader(task_file)

        total_rounds = max(r.rounds_used for r in results) if results else 0
        all_warnings = [w for r in results for w in r.warnings]

        return PipelineResult(
            status="success" if result.status != "error" else "failed",
            solution=result,
            rounds_used=total_rounds,
            warnings=all_warnings,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _parse_sub_tasks(self, body: str) -> list[SubTask]:
        """Parse sub-tasks from Leader's decompose result body.

        Captures the full text block per numbered item, including all
        indented lines (constraints, language, files, etc.).

        Expects format like:
        1. **Task description**
           - Language: python
           - Constraints:
             - Must validate input
        """
        sub_tasks: list[SubTask] = []
        lines = body.split("\n")

        current_block: list[str] = []
        in_task = False

        for line in lines:
            # Detect a new numbered item (e.g. '1. ', '2. ', '10. ')
            if re.match(r"^\s*\d+\.\s+", line):
                # Save previous block
                if current_block:
                    full_text = "\n".join(current_block).strip()
                    if full_text:
                        sub_tasks.append(SubTask(description=full_text))
                current_block = [line]
                in_task = True
            elif in_task:
                # Continuation lines (indented or empty lines within the block)
                if line.strip() == "":
                    # Empty line might end the block or be spacing within it
                    current_block.append(line)
                elif line.startswith(" ") or line.startswith("\t") or line.startswith("-"):
                    current_block.append(line)
                else:
                    # Non-indented, non-numbered line → end of block
                    current_block.append(line)

        # Don't forget the last block
        if current_block:
            full_text = "\n".join(current_block).strip()
            if full_text:
                sub_tasks.append(SubTask(description=full_text))

        # Fallback: if no numbered list found, treat entire body as one task
        if not sub_tasks and body.strip():
            sub_tasks.append(SubTask(description=body.strip()[:2000]))

        return sub_tasks

    def _extract_best_worker(
        self,
        review_body: str,
        worker_results: dict[str, AgentResult],
    ) -> str | None:
        """Try to identify which worker was picked as best from review text."""
        review_lower = review_body.lower()
        for worker_name in worker_results:
            if worker_name.lower() in review_lower:
                # Simple heuristic: check if this worker is mentioned near "best" or "recommend"
                idx = review_lower.find(worker_name.lower())
                context = review_lower[max(0, idx - 50) : idx + 50]
                if any(word in context for word in ["best", "recommend", "base", "winner", "pick"]):
                    return worker_name
        # Fallback: first worker
        return next(iter(worker_results), None)

    def _build_feedback(
        self,
        review: ReviewResult,
        exec_result: ExecutionResult | None,
    ) -> Feedback:
        """Build structured feedback for the next round."""
        feedback = Feedback(
            previous_best_approach=f"{review.best_worker} (score: {review.best_score})",
        )

        # Extract issues from review
        if review.merge_plan:
            # Look for "Issues" or "Fix" sections in the review body
            lines = review.merge_plan.split("\n")
            for line in lines:
                line_stripped = line.strip().lstrip("- ").lstrip("* ")
                if any(kw in line.lower() for kw in ["fix", "issue", "bug", "missing", "add"]):
                    if line_stripped and len(line_stripped) > 10:
                        feedback.mandatory_fixes.append(line_stripped)

        # Add execution errors
        if exec_result and not exec_result.tests_passed:
            if exec_result.stderr:
                feedback.execution_errors.append(exec_result.stderr[:500])
            if exec_result.stdout:
                feedback.execution_errors.append(f"stdout: {exec_result.stdout[:300]}")

        # Score gaps
        if review.best_score < self.config.quality_threshold:
            gap = self.config.quality_threshold - review.best_score
            feedback.score_gaps["overall"] = (
                f"{review.best_score:.0f} → target {self.config.quality_threshold:.0f} "
                f"(gap: {gap:.0f})"
            )

        return feedback
