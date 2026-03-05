"""Tests for orchestrator.py - pipeline controller and feedback logic."""

from pathlib import Path

from x_ai_cli.config import Config
from x_ai_cli.models import AgentResult, AgentStatus, Feedback, ReviewResult
from x_ai_cli.orchestrator import Orchestrator, PipelineResult


class TestPipelineResult:
    """Tests for PipelineResult dataclass."""

    def test_pipeline_result_defaults(self) -> None:
        """Test PipelineResult with default values."""
        result = PipelineResult()
        assert result.status == "success"
        assert result.solution is None
        assert result.rounds_used == 0
        assert result.warnings == []

    def test_pipeline_result_with_values(self) -> None:
        """Test PipelineResult with custom values."""
        solution = AgentResult(status=AgentStatus.SUCCESS)
        result = PipelineResult(
            status="best_effort",
            solution=solution,
            rounds_used=3,
            warnings=["Warning 1", "Warning 2"],
        )
        assert result.status == "best_effort"
        assert result.solution == solution
        assert result.rounds_used == 3
        assert result.warnings == ["Warning 1", "Warning 2"]


class TestOrchestratorInit:
    """Tests for Orchestrator initialization."""

    def test_orchestrator_init(self, tmp_path: Path) -> None:
        """Test Orchestrator initializes with config."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        assert orchestrator.config == config
        assert orchestrator.runner is not None
        assert orchestrator.run_id is not None
        assert len(orchestrator.run_id) == 15  # YYYYMMDD_HHMMSS format

    def test_task_dir_creation(self, tmp_path: Path) -> None:
        """Test _task_dir creates directory structure."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        task_dir = orchestrator._task_dir(1, "planner")
        assert task_dir.exists()
        assert task_dir.name == "planner"
        assert task_dir.parent.name == "round_1"


class TestBuildFeedback:
    """Tests for _build_feedback method."""

    def test_build_feedback_empty_review(self, tmp_path: Path) -> None:
        """Test _build_feedback with empty review."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        review = ReviewResult(score=80.0, review_notes="")  # Above threshold
        feedback = orchestrator._build_feedback(review)
        assert isinstance(feedback, Feedback)
        assert feedback.previous_score == 80.0
        assert feedback.mandatory_fixes == []
        assert feedback.score_gaps == {}

    def test_build_feedback_extracts_issues(self, tmp_path: Path) -> None:
        """Test _build_feedback extracts issues from review notes."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        review = ReviewResult(
            score=60.0,
            review_notes="""
- Fix the bug in line 10
- Missing documentation
- Add more tests
- This is an error in the logic
""",
        )
        feedback = orchestrator._build_feedback(review)
        assert len(feedback.mandatory_fixes) > 0
        assert any("bug" in fix.lower() for fix in feedback.mandatory_fixes)
        assert any("documentation" in fix.lower() for fix in feedback.mandatory_fixes)
        assert any("tests" in fix.lower() for fix in feedback.mandatory_fixes)

    def test_build_feedback_deduplication(self, tmp_path: Path) -> None:
        """Test _build_feedback deduplicates similar issues."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        review = ReviewResult(
            score=60.0,
            review_notes="""
- Fix the bug in authentication
- fix the bug in authentication
- FIX THE BUG IN AUTHENTICATION
""",
        )
        feedback = orchestrator._build_feedback(review)
        # Should deduplicate similar issues
        assert len(feedback.mandatory_fixes) == 1

    def test_build_feedback_skips_short_lines(self, tmp_path: Path) -> None:
        """Test _build_feedback skips lines that are too short."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        review = ReviewResult(
            score=60.0,
            review_notes="""
- Fix this
- bug
- Fix the actual problem here
""",
        )
        feedback = orchestrator._build_feedback(review)
        # Should skip short lines
        assert all(len(fix) >= 10 for fix in feedback.mandatory_fixes)

    def test_build_feedback_score_gap(self, tmp_path: Path) -> None:
        """Test _build_feedback adds score gap when below threshold."""
        config = Config(work_dir=str(tmp_path), quality_threshold=80.0)
        orchestrator = Orchestrator(config)
        review = ReviewResult(score=60.0, review_notes="")
        feedback = orchestrator._build_feedback(review)
        assert "overall" in feedback.score_gaps
        assert "60" in feedback.score_gaps["overall"]
        assert "80" in feedback.score_gaps["overall"]
        assert "gap: 20" in feedback.score_gaps["overall"]

    def test_build_feedback_no_score_gap_when_above_threshold(
        self, tmp_path: Path
    ) -> None:
        """Test _build_feedback doesn't add score gap when above threshold."""
        config = Config(work_dir=str(tmp_path), quality_threshold=70.0)
        orchestrator = Orchestrator(config)
        review = ReviewResult(score=85.0, review_notes="")
        feedback = orchestrator._build_feedback(review)
        assert feedback.score_gaps == {}


class TestFeedbackKeywordMatching:
    """Tests for feedback keyword matching."""

    def test_detects_fix_keyword(self, tmp_path: Path) -> None:
        """Test detection of 'fix' keyword."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        review = ReviewResult(score=60.0, review_notes="- Fix the memory leak issue")
        feedback = orchestrator._build_feedback(review)
        assert any("memory leak" in fix for fix in feedback.mandatory_fixes)

    def test_detects_issue_keyword(self, tmp_path: Path) -> None:
        """Test detection of 'issue' keyword."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        review = ReviewResult(
            score=60.0, review_notes="- There is an issue with the loop"
        )
        feedback = orchestrator._build_feedback(review)
        assert any("issue" in fix.lower() for fix in feedback.mandatory_fixes)

    def test_detects_bug_keyword(self, tmp_path: Path) -> None:
        """Test detection of 'bug' keyword."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        review = ReviewResult(score=60.0, review_notes="- Bug found in the parser")
        feedback = orchestrator._build_feedback(review)
        assert any("bug" in fix.lower() for fix in feedback.mandatory_fixes)

    def test_detects_missing_keyword(self, tmp_path: Path) -> None:
        """Test detection of 'missing' keyword."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        review = ReviewResult(score=60.0, review_notes="- Missing error handling")
        feedback = orchestrator._build_feedback(review)
        assert any("missing" in fix.lower() for fix in feedback.mandatory_fixes)

    def test_detects_add_keyword(self, tmp_path: Path) -> None:
        """Test detection of 'add' keyword."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        review = ReviewResult(score=60.0, review_notes="- Add input validation")
        feedback = orchestrator._build_feedback(review)
        assert any("add" in fix.lower() for fix in feedback.mandatory_fixes)

    def test_detects_error_keyword(self, tmp_path: Path) -> None:
        """Test detection of 'error' keyword."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        review = ReviewResult(score=60.0, review_notes="- Error in calculation logic")
        feedback = orchestrator._build_feedback(review)
        assert any("error" in fix.lower() for fix in feedback.mandatory_fixes)

    def test_case_insensitive_matching(self, tmp_path: Path) -> None:
        """Test case insensitive keyword matching."""
        config = Config(work_dir=str(tmp_path))
        orchestrator = Orchestrator(config)
        review = ReviewResult(score=60.0, review_notes="- FIX this ASAP")
        feedback = orchestrator._build_feedback(review)
        assert len(feedback.mandatory_fixes) == 1
