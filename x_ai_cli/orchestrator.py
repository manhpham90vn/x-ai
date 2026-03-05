"""Orchestrator pipeline for x-ai CLI.

Implements the Planner-Executor pipeline:
    PLAN → EXECUTE → REVIEW → (loop if needed)

Flow:
1. Planner reads user request + repo → creates implementation plan
2. Executor follows the plan → implements code + runs verification
3. Planner reviews the changes → scores + provides feedback
4. If score >= threshold → success; else loop back to step 1 with feedback
5. If max_rounds exhausted → return best-effort result

All agents are Claude CLI instances running via tmux sessions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from x_ai_cli.agent_runner import AgentRunner
from x_ai_cli.config import Config
from x_ai_cli.logger import (
    log_phase,
    log_round,
    log_score,
    logger,
)
from x_ai_cli.models import (
    AgentResult,
    AgentTask,
    Feedback,
    ReviewResult,
    TaskType,
)


@dataclass
class PipelineResult:
    """Final result of the orchestrator pipeline."""

    status: str = "success"  # success | best_effort | failed
    solution: AgentResult | None = None
    rounds_used: int = 0
    warnings: list[str] = field(default_factory=list)


class Orchestrator:
    """Main orchestrator that runs the Planner-Executor pipeline."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.runner = AgentRunner(config)
        self.run_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    def _task_dir(self, round_num: int, agent_role: str) -> Path:
        """Get task directory: tasks/{run_id}/round_{n}/{agent_role}/

        Structure:
            tasks/{run_id}/
            ├── round_1/
            │   ├── planner/     # PLAN phase
            │   ├── executor/    # EXECUTE phase
            │   └── reviewer/    # REVIEW phase (planner reviewing)
            ├── round_2/...
        """
        d = self.config.tasks_path / self.run_id / f"round_{round_num}" / agent_role
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def run_pipeline(self, user_request: str) -> PipelineResult:
        """Run the Planner-Executor pipeline.

        Flow: (PLAN → EXECUTE → REVIEW) × rounds
        Stops early when score >= threshold.
        """
        self.config.ensure_dirs()

        feedback: Feedback | None = None
        best_solution: AgentResult | None = None
        best_score: float = 0.0

        for round_num in range(1, self.config.max_rounds + 1):
            log_round(round_num, self.config.max_rounds)

            # Phase 1: PLAN — Planner reads repo and creates plan
            log_phase(
                "PLAN",
                f"Planner analyzing codebase and creating plan (round {round_num})",
            )
            plan_result = await self.run_planner(user_request, round_num, feedback)

            if plan_result.status == "error":
                logger.error(
                    "Planner failed in round %d: %s",
                    round_num,
                    plan_result.error_message,
                )
                continue

            logger.info("Planner created implementation plan")

            # Phase 2: EXECUTE — Executor implements code following the plan
            log_phase(
                "EXECUTE",
                f"Executor implementing code (round {round_num})",
            )
            exec_result = await self.run_executor(plan_result, round_num)

            if exec_result.status == "error":
                logger.error(
                    "Executor failed in round %d: %s",
                    round_num,
                    exec_result.error_message,
                )
                # Build feedback from execution failure
                feedback = Feedback(
                    execution_errors=[exec_result.error_message or "Executor failed"],
                    previous_score=best_score,
                )
                continue

            logger.info(
                "Executor completed — %d file(s) changed",
                len(exec_result.files_changed),
            )

            # Phase 3: REVIEW — Planner reviews code changes
            log_phase(
                "REVIEW",
                f"Planner reviewing code changes (round {round_num})",
            )
            review = await self.run_planner_review(
                user_request, plan_result, exec_result, round_num
            )

            log_score("Planner", review.score, self.config.quality_threshold)

            # Track best solution
            if review.score > best_score:
                best_score = review.score
                best_solution = exec_result

            # Quality gate — stop if score meets threshold
            if review.score >= self.config.quality_threshold:
                logger.info(
                    "Score %.1f >= threshold %.1f — pipeline complete ✓",
                    review.score,
                    self.config.quality_threshold,
                )
                return PipelineResult(
                    status="success",
                    solution=exec_result,
                    rounds_used=round_num,
                )

            # Score too low — build feedback for next round
            logger.warning(
                "Score %.1f below threshold %.1f, retrying",
                review.score,
                self.config.quality_threshold,
            )
            feedback = self._build_feedback(review)

        # Exhausted all rounds
        logger.warning(
            "Max rounds (%d) exhausted — returning best-effort",
            self.config.max_rounds,
        )
        return PipelineResult(
            status="best_effort",
            solution=best_solution,
            rounds_used=self.config.max_rounds,
            warnings=["Max rounds exhausted, returning best available solution"],
        )

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    async def run_planner(
        self,
        user_request: str,
        round_num: int,
        feedback: Feedback | None,
    ) -> AgentResult:
        """Phase 1: Ask the Planner to analyze repo and create a plan."""
        task = AgentTask(
            task_type=TaskType.PLAN,
            work_dir=str(self.config.work_path),
            round=round_num,
            instructions=user_request,
            feedback=feedback,
        )
        task_dir = self._task_dir(round_num, "planner")
        task_file = task.write(task_dir)
        return await self.runner.run_agent(task_file, role="planner")

    async def run_executor(
        self,
        plan_result: AgentResult,
        round_num: int,
    ) -> AgentResult:
        """Phase 2: Ask the Executor to implement code following the plan."""
        task = AgentTask(
            task_type=TaskType.EXECUTE,
            work_dir=str(self.config.work_path),
            round=round_num,
            instructions="Follow the implementation plan below.",
            plan_content=plan_result.body,
        )
        task_dir = self._task_dir(round_num, "executor")
        task_file = task.write(task_dir)
        return await self.runner.run_agent(task_file, role="executor")

    async def run_planner_review(
        self,
        user_request: str,
        plan_result: AgentResult,
        exec_result: AgentResult,
        round_num: int,
    ) -> ReviewResult:
        """Phase 3: Ask the Planner to review the Executor's changes."""
        # Build review instructions with all context
        instructions = (
            f"## Original Request\n\n{user_request}\n\n"
            f"## Implementation Plan (what was expected)\n\n{plan_result.body}\n\n"
            f"## Executor Report\n\n"
            f"**Status**: {exec_result.status}\n"
            f"**Files changed**: "
            f"{', '.join(exec_result.files_changed)}"
            f"{'' if exec_result.files_changed else 'N/A'}"
            f"\n\n"
            f"{exec_result.body}\n\n"
            f"---\n\n"
            f"Review the Executor's implementation against "
            f"the plan and the original request.\n"
            f"Read each changed file in "
            f"`{self.config.work_path}` and score the solution.\n"
            f"Score on: correctness (0.25), code_quality (0.20), security (0.20), "
            f"performance (0.15), maintainability (0.20).\n"
            f"Provide the weighted average as `score` in frontmatter."
        )

        task = AgentTask(
            task_type=TaskType.REVIEW,
            work_dir=str(self.config.work_path),
            round=round_num,
            instructions=instructions,
        )
        task_dir = self._task_dir(round_num, "reviewer")
        task_file = task.write(task_dir)
        result = await self.runner.run_agent(task_file, role="planner")

        review = ReviewResult(solution=exec_result)

        if result.status != "error" and result.score is not None:
            review.score = result.score
            review.review_notes = result.body
        else:
            # Fallback: assign a low score if review failed
            review.score = 30.0
            review.review_notes = result.body or "Review failed"
            logger.warning("Review parsing failed, assigned fallback score 30.0")

        return review

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_feedback(self, review: ReviewResult) -> Feedback:
        """Build structured feedback for the next round with deduplication."""
        feedback = Feedback(previous_score=review.score)

        # Extract issues from review notes
        if review.review_notes:
            lines = review.review_notes.split("\n")
            seen_issues: set[str] = set()

            # Keywords + minimum line length to reduce false
            # positives
            issue_keywords = ["fix", "issue", "bug", "missing", "add", "error"]

            for line in lines:
                line_stripped = line.strip().lstrip("- ").lstrip("* ")

                # Skip empty or very short lines
                if len(line_stripped) < 10:
                    continue

                # Check for issue indicators with case-insensitive matching
                line_lower = line.lower()
                has_issue_keyword = any(kw in line_lower for kw in issue_keywords)

                if has_issue_keyword:
                    # Normalize for deduplication
                    normalized = " ".join(line_stripped.lower().split())

                    # Skip if we've already seen this issue (or a very similar one)
                    is_duplicate = any(
                        normalized in seen or seen in normalized for seen in seen_issues
                    )

                    if not is_duplicate:
                        feedback.mandatory_fixes.append(line_stripped)
                        seen_issues.add(normalized)

        # Score gaps
        if review.score < self.config.quality_threshold:
            gap = self.config.quality_threshold - review.score
            feedback.score_gaps["overall"] = (
                f"{review.score:.0f} → target {self.config.quality_threshold:.0f} "
                f"(gap: {gap:.0f})"
            )

        return feedback
