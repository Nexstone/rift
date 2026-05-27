"""Tests for rift_strategies_sdk.validator.validate_strategy().

Covers:
  - the shipped reference strategy (trend_follow) passes cleanly
  - mangled strategies (each common breakage) fail with the specific error
  - the report shape (ok, errors, warnings, summary) behaves
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
for sub in ("packages/engine/src", "packages/strategies-sdk/src", "engine/src"):
    p = str(_REPO_ROOT / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

from rift_strategies_sdk.validator import ValidationReport, validate_strategy


REFERENCE_STRATEGY = (
    _REPO_ROOT / "packages/strategies-sdk/src/rift_strategies_sdk/examples/trend_follow.py"
)


# ─── Reference strategy passes ──────────────────────────────────────


def test_trend_follow_reference_validates_clean():
    """The shipped OSS reference strategy must validate cleanly."""
    report = validate_strategy(REFERENCE_STRATEGY)
    assert isinstance(report, ValidationReport)
    assert report.ok is True, f"trend_follow failed validation: {report.errors}"
    assert report.strategy_name == "trend_follow"
    assert report.errors == []


# ─── File-level errors ──────────────────────────────────────────────


def test_missing_file_fails(tmp_path):
    report = validate_strategy(tmp_path / "nonexistent.py")
    assert not report.ok
    assert any("not found" in e for e in report.errors)


def test_non_py_file_fails(tmp_path):
    bad = tmp_path / "not_python.txt"
    bad.write_text("not python")
    report = validate_strategy(bad)
    assert not report.ok
    assert any("not a .py file" in e for e in report.errors)


def test_syntax_error_fails(tmp_path):
    bad = tmp_path / "bad_syntax.py"
    bad.write_text("def broken(:\n    pass\n")
    report = validate_strategy(bad)
    assert not report.ok
    assert any("syntax error" in e for e in report.errors)


# ─── Structural errors ──────────────────────────────────────────────


def _write_strategy(tmp_path: Path, body: str) -> Path:
    """Write a strategy file with shared boilerplate header."""
    full = textwrap.dedent("""
        from __future__ import annotations
        from dataclasses import dataclass
        from typing import Annotated
        from rift_engine.strategy import (
            Candle, Indicator, Param, Signal, Strategy, StrategyState, register, EMA,
        )
    """).lstrip() + textwrap.dedent(body)
    f = tmp_path / "strat.py"
    f.write_text(full)
    return f


def test_no_register_decorator_fails(tmp_path):
    f = _write_strategy(tmp_path, """
        @dataclass(frozen=True)
        class C: x: int = 1

        class MyStrat(Strategy):
            config_class = C
            def indicators(self): return {}
            def on_candle(self, candle, state): return None
    """)
    report = validate_strategy(f)
    assert not report.ok
    assert any("@register" in e for e in report.errors)


def test_doesnt_inherit_from_strategy_fails(tmp_path):
    f = _write_strategy(tmp_path, """
        @dataclass(frozen=True)
        class C: x: int = 1

        @register("bad_strat")
        class BadStrat:  # does NOT inherit from Strategy
            config_class = C
            def indicators(self): return {}
            def on_candle(self, candle, state): return None
    """)
    report = validate_strategy(f)
    assert not report.ok
    assert any("Strategy" in e for e in report.errors)


def test_missing_on_candle_fails(tmp_path):
    f = _write_strategy(tmp_path, """
        @dataclass(frozen=True)
        class C: x: int = 1

        @register("missing_on_candle")
        class S(Strategy):
            config_class = C
            def indicators(self): return {}
    """)
    report = validate_strategy(f)
    assert not report.ok
    assert any("on_candle" in e for e in report.errors)


def test_wrong_on_candle_signature_fails(tmp_path):
    f = _write_strategy(tmp_path, """
        @dataclass(frozen=True)
        class C: x: int = 1

        @register("wrong_sig")
        class S(Strategy):
            config_class = C
            def indicators(self): return {}
            def on_candle(self, foo, bar): return None
    """)
    report = validate_strategy(f)
    assert not report.ok
    assert any("on_candle signature" in e for e in report.errors)


def test_config_class_none_fails(tmp_path):
    f = _write_strategy(tmp_path, """
        @register("no_config")
        class S(Strategy):
            config_class = None
            def indicators(self): return {}
            def on_candle(self, candle, state): return None
    """)
    report = validate_strategy(f)
    assert not report.ok
    assert any("config_class" in e for e in report.errors)


def test_config_not_dataclass_fails(tmp_path):
    f = _write_strategy(tmp_path, """
        class C:  # not a dataclass
            x: int = 1

        @register("not_dc")
        class S(Strategy):
            config_class = C
            def indicators(self): return {}
            def on_candle(self, candle, state): return None
    """)
    report = validate_strategy(f)
    assert not report.ok
    assert any("not a dataclass" in e for e in report.errors)


# ─── Warnings (non-blocking) ────────────────────────────────────────


def test_config_not_frozen_warns(tmp_path):
    f = _write_strategy(tmp_path, """
        @dataclass  # NOT frozen
        class C: x: Annotated[int, Param("x", min=0, max=10, step=1)] = 1

        @register("not_frozen")
        class S(Strategy):
            config_class = C
            def indicators(self): return {}
            def on_candle(self, candle, state): return None
    """)
    report = validate_strategy(f)
    assert report.ok  # warnings only, no errors
    assert any("not frozen" in w for w in report.warnings)


def test_field_without_param_warns(tmp_path):
    f = _write_strategy(tmp_path, """
        @dataclass(frozen=True)
        class C:
            x: int = 1  # no Param() annotation

        @register("no_param")
        class S(Strategy):
            config_class = C
            def indicators(self): return {}
            def on_candle(self, candle, state): return None
    """)
    report = validate_strategy(f)
    assert report.ok
    assert any("Param" in w for w in report.warnings)


def test_unknown_promotion_gate_key_warns(tmp_path):
    f = _write_strategy(tmp_path, """
        @dataclass(frozen=True)
        class C:
            x: Annotated[int, Param("x", min=0, max=10, step=1)] = 1

        @register("weird_gate")
        class S(Strategy):
            config_class = C
            promotion_gates = {"min_dsr": 0.85, "made_up_gate": 1.0}
            def indicators(self): return {}
            def on_candle(self, candle, state): return None
    """)
    report = validate_strategy(f)
    assert report.ok
    assert any("made_up_gate" in w or "unknown" in w for w in report.warnings)


# ─── Multiple registered classes ────────────────────────────────────


def test_two_register_classes_fails(tmp_path):
    f = _write_strategy(tmp_path, """
        @dataclass(frozen=True)
        class C:
            x: Annotated[int, Param("x", min=0, max=10, step=1)] = 1

        @register("first")
        class A(Strategy):
            config_class = C
            def indicators(self): return {}
            def on_candle(self, candle, state): return None

        @register("second")
        class B(Strategy):
            config_class = C
            def indicators(self): return {}
            def on_candle(self, candle, state): return None
    """)
    report = validate_strategy(f)
    assert not report.ok
    assert any("multiple" in e.lower() for e in report.errors)


# ─── Report shape ───────────────────────────────────────────────────


def test_report_summary_renders():
    report = ValidationReport(
        ok=False,
        strategy_name="bad",
        errors=["thing missing"],
        warnings=["thing weird"],
    )
    s = report.summary()
    assert "FAIL" in s
    assert "thing missing" in s
    assert "thing weird" in s


def test_clean_report_summary():
    report = ValidationReport(ok=True, strategy_name="good")
    s = report.summary()
    assert "PASS" in s
    assert "No issues" in s
