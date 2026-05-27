"""Unit tests for rift_core.config — env file management + parse_duration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rift_core.config import parse_duration


class TestParseDuration:
    @pytest.mark.parametrize("s,expected", [
        ("60", 60),
        ("60s", 60),
        ("30m", 30 * 60),
        ("4h", 4 * 3600),
        ("1d", 86400),
        ("7d", 7 * 86400),
        ("0", 0),
        ("", 0),
    ])
    def test_parses(self, s, expected):
        assert parse_duration(s) == expected

    def test_handles_uppercase_units(self):
        # parse_duration lowercases internally
        assert parse_duration("4H") == 4 * 3600
        assert parse_duration("30M") == 30 * 60

    def test_handles_whitespace(self):
        assert parse_duration("  4h  ") == 4 * 3600


class TestEnvFile:
    """Tests that mutate ~/.rift/.env need isolation. We point CONFIG_DIR/ENV_PATH
    to a tmp path via monkey-patching the module constants."""

    def test_set_and_get_round_trip(self, tmp_path, monkeypatch):
        from rift_core import config

        env_file = tmp_path / ".env"
        monkeypatch.setattr(config, "ENV_PATH", env_file)
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
        # Also clear from env so get_env_var has to read the file
        monkeypatch.delenv("RIFT_TEST_KEY", raising=False)

        config.set_env_var("RIFT_TEST_KEY", "hello-world")
        # set_env_var also writes to os.environ; remove to force file read
        monkeypatch.delenv("RIFT_TEST_KEY", raising=False)

        assert config.get_env_var("RIFT_TEST_KEY") == "hello-world"

    def test_get_returns_default_when_missing(self, tmp_path, monkeypatch):
        from rift_core import config

        env_file = tmp_path / ".env"
        monkeypatch.setattr(config, "ENV_PATH", env_file)
        monkeypatch.delenv("RIFT_DOES_NOT_EXIST", raising=False)

        assert config.get_env_var("RIFT_DOES_NOT_EXIST") == ""
        assert config.get_env_var("RIFT_DOES_NOT_EXIST", default="fallback") == "fallback"

    def test_env_overrides_file(self, tmp_path, monkeypatch):
        from rift_core import config

        env_file = tmp_path / ".env"
        env_file.write_text("RIFT_X=from-file\n")
        monkeypatch.setattr(config, "ENV_PATH", env_file)
        monkeypatch.setenv("RIFT_X", "from-environ")

        # os.environ wins
        assert config.get_env_var("RIFT_X") == "from-environ"

    def test_sensitive_keys_not_loaded_into_environ(self, tmp_path, monkeypatch):
        from rift_core import config

        env_file = tmp_path / ".env"
        env_file.write_text("HYPERLIQUID_PRIVATE_KEY=secret123\n")
        monkeypatch.setattr(config, "ENV_PATH", env_file)
        monkeypatch.delenv("HYPERLIQUID_PRIVATE_KEY", raising=False)

        config.load_env()
        # Must NOT be in environ
        assert os.environ.get("HYPERLIQUID_PRIVATE_KEY") is None
        # But IS readable just-in-time
        assert config.get_env_var("HYPERLIQUID_PRIVATE_KEY") == "secret123"

    def test_strips_quotes_from_values(self, tmp_path, monkeypatch):
        from rift_core import config

        env_file = tmp_path / ".env"
        env_file.write_text('RIFT_QUOTED="value with spaces"\n')
        monkeypatch.setattr(config, "ENV_PATH", env_file)
        monkeypatch.delenv("RIFT_QUOTED", raising=False)

        config.load_env()
        assert os.environ.get("RIFT_QUOTED") == "value with spaces"

    def test_ignores_comments_and_blank_lines(self, tmp_path, monkeypatch):
        from rift_core import config

        env_file = tmp_path / ".env"
        env_file.write_text(
            "# this is a comment\n"
            "\n"
            "RIFT_REAL=actual\n"
            "  # leading-space comment\n"
        )
        monkeypatch.setattr(config, "ENV_PATH", env_file)
        monkeypatch.delenv("RIFT_REAL", raising=False)

        config.load_env()
        assert os.environ.get("RIFT_REAL") == "actual"
