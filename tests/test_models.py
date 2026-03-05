"""Tests for models.py - data models and frontmatter parsing."""

from pathlib import Path

from x_ai_cli.models import (
    AgentResult,
    AgentStatus,
    AgentTask,
    Feedback,
    Task,
    TaskStatus,
    TaskType,
    new_id,
    now_iso,
    parse_frontmatter,
    write_frontmatter,
)


class TestFrontmatterParsing:
    """Tests for YAML frontmatter parsing functions."""

    def test_parse_frontmatter_with_valid_frontmatter(self) -> None:
        """Test parsing markdown with valid YAML frontmatter."""
        text = """---
id: test-123
status: pending
score: 85.5
---

## Body Content

This is the body.
"""
        meta, body = parse_frontmatter(text)
        assert meta["id"] == "test-123"
        assert meta["status"] == "pending"
        assert meta["score"] == 85.5
        assert "## Body Content" in body
        assert "This is the body." in body

    def test_parse_frontmatter_without_frontmatter(self) -> None:
        """Test parsing markdown without frontmatter."""
        text = "## Just a body\n\nNo frontmatter here."
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert body == text

    def test_parse_frontmatter_empty_frontmatter(self) -> None:
        """Test parsing markdown with empty frontmatter."""
        text = """---
---

Body content.
"""
        meta, body = parse_frontmatter(text)
        assert meta == {}
        assert "Body content." in body

    def test_write_frontmatter(self) -> None:
        """Test writing frontmatter and body to markdown string."""
        meta = {"id": "test-456", "status": "success", "score": 90.0}
        body = "## Result\n\nAll tests passed."
        result = write_frontmatter(meta, body)
        assert result.startswith("---\n")
        assert "id: test-456" in result
        assert "status: success" in result
        assert "score: 90.0" in result
        assert "---\n\n## Result" in result
        assert "All tests passed." in result

    def test_roundtrip_frontmatter(self) -> None:
        """Test that parse and write are inverse operations."""
        original_meta = {
            "id": "abc",
            "list": ["a", "b", "c"],
            "nested": {"key": "value"},
        }
        original_body = "## Test Body\n\nContent here."
        written = write_frontmatter(original_meta, original_body)
        parsed_meta, parsed_body = parse_frontmatter(written)
        assert parsed_meta["id"] == original_meta["id"]
        assert parsed_body.strip() == original_body.strip()


class TestEnums:
    """Tests for enum classes."""

    def test_task_status_values(self) -> None:
        """Test TaskStatus enum values."""
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.IN_PROGRESS.value == "in_progress"
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.FAILED.value == "failed"

    def test_agent_status_values(self) -> None:
        """Test AgentStatus enum values."""
        assert AgentStatus.SUCCESS.value == "success"
        assert AgentStatus.ERROR.value == "error"
        assert AgentStatus.PARTIAL.value == "partial"

    def test_task_type_values(self) -> None:
        """Test TaskType enum values."""
        assert TaskType.PLAN.value == "plan"
        assert TaskType.EXECUTE.value == "execute"
        assert TaskType.REVIEW.value == "review"


class TestTask:
    """Tests for Task dataclass."""

    def test_task_defaults(self) -> None:
        """Test Task with default values."""
        task = Task()
        assert task.id is not None
        assert len(task.id) > 0  # UUID string
        assert task.original_request == ""
        assert task.status == TaskStatus.PENDING
        assert task.current_round == 0
        assert task.created_at is not None

    def test_task_custom_values(self) -> None:
        """Test Task with custom values."""
        task = Task(
            id="custom-id",
            original_request="Test request",
            status=TaskStatus.COMPLETED,
            current_round=2,
            created_at="2024-01-01T00:00:00",
        )
        assert task.id == "custom-id"
        assert task.original_request == "Test request"
        assert task.status == TaskStatus.COMPLETED
        assert task.current_round == 2
        assert task.created_at == "2024-01-01T00:00:00"


class TestAgentTask:
    """Tests for AgentTask dataclass."""

    def test_agent_task_defaults(self) -> None:
        """Test AgentTask with default values."""
        task = AgentTask()
        assert task.id is not None
        assert task.task_type == TaskType.EXECUTE
        assert task.work_dir == "."
        assert task.round == 1
        assert task.parent_task_id is None
        assert task.instructions == ""
        assert task.context == ""
        assert task.plan_content == ""
        assert task.feedback is None

    def test_agent_task_to_markdown_no_feedback(self) -> None:
        """Test AgentTask.to_markdown() without feedback."""
        task = AgentTask(
            id="task-123",
            task_type=TaskType.PLAN,
            work_dir="/work",
            round=1,
            instructions="Do something",
        )
        markdown = task.to_markdown()
        assert "id: task-123" in markdown
        assert "type: plan" in markdown
        assert "work_dir: /work" in markdown
        assert "round: 1" in markdown
        assert "feedback:" in markdown
        assert "## Instructions" in markdown
        assert "Do something" in markdown

    def test_agent_task_to_markdown_with_feedback(self) -> None:
        """Test AgentTask.to_markdown() with feedback."""
        feedback = Feedback(
            mandatory_fixes=["Fix bug A"],
            score_gaps={"quality": "needs work"},
            execution_errors=["Timeout"],
            previous_score=50.0,
        )
        task = AgentTask(
            id="task-456",
            task_type=TaskType.EXECUTE,
            feedback=feedback,
        )
        markdown = task.to_markdown()
        assert "mandatory_fixes:" in markdown
        assert "- Fix bug A" in markdown
        assert "score_gaps:" in markdown
        assert "previous_score: 50.0" in markdown

    def test_agent_task_write(self, tmp_path: Path) -> None:
        """Test AgentTask.write() creates file correctly."""
        task = AgentTask(
            id="write-test",
            instructions="Test writing",
        )
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        path = task.write(tasks_dir)
        assert path.exists()
        assert path.name == "write-test.task.md"
        content = path.read_text()
        assert "Test writing" in content


class TestAgentResult:
    """Tests for AgentResult dataclass."""

    def test_agent_result_defaults(self) -> None:
        """Test AgentResult with default values."""
        result = AgentResult()
        assert result.id == ""
        assert result.status == AgentStatus.ERROR
        assert result.score is None
        assert result.round == 1
        assert result.files_changed == []
        assert result.error_message is None
        assert result.body == ""

    def test_agent_result_from_file_not_found(self) -> None:
        """Test AgentResult.from_file() with missing file."""
        path = Path("/nonexistent/result.md")
        result = AgentResult.from_file(path)
        assert result.status == AgentStatus.ERROR
        assert "not found" in (result.error_message or "").lower()

    def test_agent_result_from_file_valid(self, tmp_path: Path) -> None:
        """Test AgentResult.from_file() with valid file."""
        result_file = tmp_path / "test.result.md"
        content = """---
id: result-123
status: success
score: 85.5
round: 2
files_changed:
  - file1.py
  - file2.py
---

## Summary

All done!
"""
        result_file.write_text(content)
        result = AgentResult.from_file(result_file)
        assert result.id == "result-123"
        assert result.status == AgentStatus.SUCCESS
        assert result.score == 85.5
        assert result.round == 2
        assert result.files_changed == ["file1.py", "file2.py"]
        assert "All done!" in result.body

    def test_agent_result_from_file_invalid_status(self, tmp_path: Path) -> None:
        """Test AgentResult.from_file() with invalid status falls back to ERROR."""
        result_file = tmp_path / "test.result.md"
        content = """---
status: invalid_status
---

Body
"""
        result_file.write_text(content)
        result = AgentResult.from_file(result_file)
        assert result.status == AgentStatus.ERROR

    def test_agent_result_error_factory(self) -> None:
        """Test AgentResult.error() factory method."""
        result = AgentResult.error("Something went wrong")
        assert result.status == AgentStatus.ERROR
        assert result.error_message == "Something went wrong"


class TestFeedback:
    """Tests for Feedback dataclass."""

    def test_feedback_defaults(self) -> None:
        """Test Feedback with default values."""
        feedback = Feedback()
        assert feedback.mandatory_fixes == []
        assert feedback.score_gaps == {}
        assert feedback.execution_errors == []
        assert feedback.previous_score == 0.0

    def test_feedback_with_values(self) -> None:
        """Test Feedback with custom values."""
        feedback = Feedback(
            mandatory_fixes=["Fix A", "Fix B"],
            score_gaps={"quality": "low", "security": "medium"},
            execution_errors=["Error 1"],
            previous_score=65.5,
        )
        assert feedback.mandatory_fixes == ["Fix A", "Fix B"]
        assert feedback.score_gaps == {"quality": "low", "security": "medium"}
        assert feedback.execution_errors == ["Error 1"]
        assert feedback.previous_score == 65.5


class TestUtilityFunctions:
    """Tests for utility functions."""

    def test_new_id_format(self) -> None:
        """Test new_id() returns valid UUID string."""
        id1 = new_id()
        id2 = new_id()
        assert isinstance(id1, str)
        assert len(id1) == 36  # UUID4 string length
        assert id1 != id2  # Should be unique

    def test_now_iso_format(self) -> None:
        """Test now_iso() returns valid ISO timestamp."""
        timestamp = now_iso()
        assert isinstance(timestamp, str)
        assert "T" in timestamp  # ISO format separator
        assert timestamp.endswith("+00:00") or "Z" in timestamp  # UTC indicator
