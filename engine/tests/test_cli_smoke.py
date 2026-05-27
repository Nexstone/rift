"""CLI smoke tests — every command parses and key read-only commands run.

These tests guard against the kind of regressions that broke supervisor.py
(invisible IndentationError because nobody imported it) — by actually
invoking each command, we catch import-time errors that simple unit
tests miss.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


RIFT = str(Path.home() / "rift" / "engine" / ".venv" / "bin" / "rift-engine")


def _run(*args, timeout=15) -> subprocess.CompletedProcess:
    return subprocess.run([RIFT, *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope="module")
def all_command_names() -> list[str]:
    """Introspect the Typer app to get the list of registered commands.

    Importing rift.cli triggers the command-module imports that register
    every @app.command on the shared Typer instance.
    """
    proc = subprocess.run(
        [
            str(Path.home() / "rift" / "engine" / ".venv" / "bin" / "python"),
            "-c",
            "import rift.cli; from rift.commands._shared import app; "
            "[print(c.name) for c in app.registered_commands]",
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    return [c for c in proc.stdout.strip().split("\n") if c]


def test_command_registry_is_populated(all_command_names):
    """The flat CLI surface should have all 91 commands registered (Phase 6 split)."""
    assert len(all_command_names) >= 80, f"only {len(all_command_names)} commands registered"


def test_no_duplicate_command_names(all_command_names):
    """Typer would silently let later @app.command() with the same name
    overwrite earlier ones — guard against accidental dupes."""
    assert len(all_command_names) == len(set(all_command_names))


def test_every_command_help_runs(all_command_names):
    """Every registered command's --help must render without error.

    This is the cheapest way to catch broken signatures (typer option
    type errors, broken default factories, etc.) for all 91 commands."""
    failed = []
    for cmd in all_command_names:
        r = _run(cmd, "--help", timeout=15)
        if r.returncode != 0 or "Usage" not in r.stdout:
            failed.append((cmd, r.returncode, (r.stderr or r.stdout)[:200]))
    assert not failed, "commands with broken --help:\n" + "\n".join(
        f"  {c} (rc={rc}): {err}" for c, rc, err in failed
    )


def test_root_help_works():
    r = _run("--help")
    assert r.returncode == 0
    assert "rift-engine" in r.stdout
    # A few commands should appear
    assert "sync" in r.stdout
    assert "backtest" in r.stdout
    assert "algo" in r.stdout


def test_version_command_emits_valid_ndjson():
    r = _run("version")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["type"] == "result"
    assert data["command"] == "version"
    assert "version" in data


def test_doctor_emits_valid_ndjson_with_checks():
    r = _run("doctor", timeout=20)
    assert r.returncode == 0, r.stderr
    data = json.loads(r.stdout.strip())
    assert data["type"] == "result"
    assert "checks" in data
    # At least Python, Engine, Polars should be checked
    names = {c["name"] for c in data["checks"]}
    assert {"Python", "Engine", "Polars"}.issubset(names)


def test_strategies_command_lists_oss_demo():
    r = _run("strategies")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    names = {s["name"] for s in data["strategies"]}
    assert "trend_follow" in names, "OSS demo strategy missing from registry"


def test_list_data_emits_valid_ndjson():
    r = _run("list-data")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["type"] == "result"
    assert "data" in data


def test_guide_command_works():
    """Guide is purely text — must succeed without any external state."""
    r = _run("guide")
    assert r.returncode == 0
    data = json.loads(r.stdout.strip())
    assert data["type"] == "result"
    assert "steps" in data
    assert len(data["steps"]) > 0
