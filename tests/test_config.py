"""Tests for config.py - configuration loading and management."""

import os
from pathlib import Path

import pytest

from x_ai_cli.config import Config


class TestConfigDefaults:
    """Tests for Config default values."""

    def test_config_default_values(self) -> None:
        """Test Config with default values."""
        config = Config()
        assert config.claude_bin == "claude"
        assert config.tmux_bin == "tmux"
        assert config.work_dir == "."
        assert config.tasks_dir == "tasks"
        assert config.logs_dir == "logs"
        assert config.max_rounds == 3
        assert config.quality_threshold == 70.0
        assert config.planner_timeout_sec == 600
        assert config.executor_timeout_sec == 600
        assert config.prompt_marker == "❯"
        assert config.poll_interval_sec == 1.0
        assert config.verbose is False
        assert config.log_to_file is True


class TestConfigPaths:
    """Tests for Config path resolution."""

    def test_work_path_resolution(self, tmp_path: Path) -> None:
        """Test work_path is resolved correctly."""
        config = Config(work_dir=str(tmp_path))
        assert config.work_path == tmp_path.resolve()

    def test_tasks_path(self, tmp_path: Path) -> None:
        """Test tasks_path is derived from work_path."""
        config = Config(work_dir=str(tmp_path), tasks_dir="custom_tasks")
        assert config.tasks_path == tmp_path.resolve() / "custom_tasks"

    def test_logs_path(self, tmp_path: Path) -> None:
        """Test logs_path is derived from work_path."""
        config = Config(work_dir=str(tmp_path), logs_dir="custom_logs")
        assert config.logs_path == tmp_path.resolve() / "custom_logs"

    def test_skills_path_exists(self) -> None:
        """Test skills_path points to existing skills directory."""
        config = Config()
        assert config.skills_path.exists()
        assert config.skills_path.name == "skills"
        assert (config.skills_path / "planner.md").exists()
        assert (config.skills_path / "executor.md").exists()


class TestConfigEnsureDirs:
    """Tests for Config.ensure_dirs() method."""

    def test_ensure_dirs_creates_tasks_dir(self, tmp_path: Path) -> None:
        """Test ensure_dirs creates tasks directory."""
        config = Config(work_dir=str(tmp_path))
        config.ensure_dirs()
        assert config.tasks_path.exists()

    def test_ensure_dirs_creates_logs_dir(self, tmp_path: Path) -> None:
        """Test ensure_dirs creates logs directory."""
        config = Config(work_dir=str(tmp_path))
        config.ensure_dirs()
        assert config.logs_path.exists()

    def test_ensure_dirs_skips_logs_when_disabled(self, tmp_path: Path) -> None:
        """Test ensure_dirs skips logs when log_to_file is False."""
        config = Config(work_dir=str(tmp_path), log_to_file=False)
        config.ensure_dirs()
        assert not config.logs_path.exists()

    def test_ensure_dirs_idempotent(self, tmp_path: Path) -> None:
        """Test ensure_dirs can be called multiple times safely."""
        config = Config(work_dir=str(tmp_path))
        config.ensure_dirs()
        config.ensure_dirs()  # Should not raise
        assert config.tasks_path.exists()


class TestConfigFromEnv:
    """Tests for Config.from_env() factory method."""

    def test_from_env_uses_defaults_when_no_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test from_env uses defaults when environment is not set."""
        # Clear all XAI_ env vars
        for key in list(os.environ.keys()):
            if key.startswith("XAI_"):
                monkeypatch.delenv(key, raising=False)

        config = Config.from_env()
        assert config.claude_bin == "claude"
        assert config.tmux_bin == "tmux"
        assert config.max_rounds == 3

    def test_from_env_reads_claude_bin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test from_env reads XAI_CLAUDE_BIN."""
        monkeypatch.setenv("XAI_CLAUDE_BIN", "/custom/claude")
        config = Config.from_env()
        assert config.claude_bin == "/custom/claude"

    def test_from_env_reads_tmux_bin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test from_env reads XAI_TMUX_BIN."""
        monkeypatch.setenv("XAI_TMUX_BIN", "/custom/tmux")
        config = Config.from_env()
        assert config.tmux_bin == "/custom/tmux"

    def test_from_env_reads_work_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test from_env reads XAI_WORK_DIR."""
        monkeypatch.setenv("XAI_WORK_DIR", str(tmp_path))
        config = Config.from_env()
        assert config.work_dir == str(tmp_path)

    def test_from_env_reads_max_rounds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test from_env reads XAI_MAX_ROUNDS."""
        monkeypatch.setenv("XAI_MAX_ROUNDS", "5")
        config = Config.from_env()
        assert config.max_rounds == 5

    def test_from_env_reads_quality_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test from_env reads XAI_QUALITY_THRESHOLD."""
        monkeypatch.setenv("XAI_QUALITY_THRESHOLD", "85.5")
        config = Config.from_env()
        assert config.quality_threshold == 85.5

    def test_from_env_reads_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test from_env reads timeout environment variables."""
        monkeypatch.setenv("XAI_PLANNER_TIMEOUT", "1200")
        monkeypatch.setenv("XAI_EXECUTOR_TIMEOUT", "900")
        config = Config.from_env()
        assert config.planner_timeout_sec == 1200
        assert config.executor_timeout_sec == 900

    def test_from_env_reads_verbose_true_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test from_env reads XAI_VERBOSE with true values."""
        for value in ["1", "true", "True", "TRUE", "yes", "Yes", "YES"]:
            monkeypatch.setenv("XAI_VERBOSE", value)
            config = Config.from_env()
            assert config.verbose is True, f"Expected True for value: {value}"

    def test_from_env_reads_verbose_false_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test from_env reads XAI_VERBOSE with false values."""
        for value in ["0", "false", "False", "FALSE", "no", "", "random"]:
            monkeypatch.setenv("XAI_VERBOSE", value)
            config = Config.from_env()
            assert config.verbose is False, f"Expected False for value: {value}"

    def test_from_env_reads_prompt_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test from_env reads XAI_PROMPT_MARKER."""
        monkeypatch.setenv("XAI_PROMPT_MARKER", ">>>")
        config = Config.from_env()
        assert config.prompt_marker == ">>>"

    def test_from_env_reads_poll_interval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test from_env reads XAI_POLL_INTERVAL."""
        monkeypatch.setenv("XAI_POLL_INTERVAL", "2.5")
        config = Config.from_env()
        assert config.poll_interval_sec == 2.5
