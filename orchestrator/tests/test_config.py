"""Tests for configuration module."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from lib.config import Config


class TestConfigDefaults:
    def test_default_values(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_env()

        assert config.max_implementation_attempts == 5
        assert config.max_review_cycles == 3
        assert config.claude_timeout == 1800
        assert config.review_timeout == 600
        assert config.github_token == ""
        assert config.max_consecutive_no_progress == 2

    def test_default_db_path(self):
        with patch.dict(os.environ, {}, clear=True):
            config = Config.from_env()

        assert config.db_path == Path.home() / ".orchestrator" / "history.db"


class TestConfigFromEnv:
    def test_reads_env_vars(self):
        env = {
            "ORCHESTRATOR_MAX_ATTEMPTS": "10",
            "ORCHESTRATOR_MAX_REVIEW_CYCLES": "5",
            "ORCHESTRATOR_CLAUDE_TIMEOUT": "3600",
            "ORCHESTRATOR_REVIEW_TIMEOUT": "900",
            "ORCHESTRATOR_DB_PATH": "/tmp/test.db",
            "GITHUB_TOKEN": "ghp_test123",
            "ORCHESTRATOR_MAX_NO_PROGRESS": "3",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()

        assert config.max_implementation_attempts == 10
        assert config.max_review_cycles == 5
        assert config.claude_timeout == 3600
        assert config.review_timeout == 900
        assert config.db_path == Path("/tmp/test.db")
        assert config.github_token == "ghp_test123"
        assert config.max_consecutive_no_progress == 3

    def test_partial_env_uses_defaults(self):
        env = {"ORCHESTRATOR_MAX_ATTEMPTS": "7"}

        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()

        assert config.max_implementation_attempts == 7
        assert config.max_review_cycles == 3  # default


class TestConfigValidation:
    def test_valid_config(self):
        config = Config()
        assert config.validate() == []

    def test_invalid_attempts(self):
        config = Config(max_implementation_attempts=0)
        errors = config.validate()
        assert any("max_implementation_attempts" in e for e in errors)

    def test_invalid_review_cycles(self):
        config = Config(max_review_cycles=0)
        errors = config.validate()
        assert any("max_review_cycles" in e for e in errors)

    def test_invalid_timeout(self):
        config = Config(claude_timeout=10)
        errors = config.validate()
        assert any("claude_timeout" in e for e in errors)

    def test_invalid_review_timeout(self):
        config = Config(review_timeout=5)
        errors = config.validate()
        assert any("review_timeout" in e for e in errors)

    def test_invalid_time_budget_seconds(self):
        config = Config(time_budget_seconds=0)
        errors = config.validate()
        assert any("time_budget_seconds" in e for e in errors)

    def test_invalid_max_consecutive_no_progress(self):
        config = Config(max_consecutive_no_progress=0)
        errors = config.validate()
        assert any("max_consecutive_no_progress" in e for e in errors)


class TestConfigImmutability:
    def test_frozen(self):
        config = Config()
        try:
            config.max_implementation_attempts = 99  # type: ignore[misc]
            assert False, "Should have raised FrozenInstanceError"
        except AttributeError:
            pass  # Expected — frozen dataclass
