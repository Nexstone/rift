"""Unit tests for rift_core.output — NaN/Inf sanitization + NDJSON emit."""

from __future__ import annotations

import io
import json
import math
import sys

import pytest

from rift_core.output import emit, sanitize_for_json


class TestSanitizeForJSON:
    def test_normal_float_unchanged(self):
        assert sanitize_for_json(1.5) == 1.5
        assert sanitize_for_json(-3.14) == -3.14

    def test_nan_becomes_none(self):
        assert sanitize_for_json(float("nan")) is None

    def test_positive_inf_becomes_none(self):
        assert sanitize_for_json(float("inf")) is None

    def test_negative_inf_becomes_none(self):
        assert sanitize_for_json(float("-inf")) is None

    def test_int_unchanged(self):
        assert sanitize_for_json(42) == 42

    def test_str_unchanged(self):
        assert sanitize_for_json("hello") == "hello"

    def test_none_unchanged(self):
        assert sanitize_for_json(None) is None

    def test_bool_unchanged(self):
        assert sanitize_for_json(True) is True
        assert sanitize_for_json(False) is False

    def test_dict_recurses(self):
        out = sanitize_for_json({"a": 1.0, "b": float("nan"), "c": "x"})
        assert out == {"a": 1.0, "b": None, "c": "x"}

    def test_list_recurses(self):
        out = sanitize_for_json([1.0, float("inf"), 2.0])
        assert out == [1.0, None, 2.0]

    def test_tuple_becomes_list(self):
        out = sanitize_for_json((1.0, float("nan")))
        assert out == [1.0, None]

    def test_deeply_nested(self):
        data = {
            "outer": {
                "list": [1.0, {"inner": float("nan")}, [float("inf"), 2.0]],
                "scalar": float("-inf"),
            }
        }
        out = sanitize_for_json(data)
        assert out == {
            "outer": {
                "list": [1.0, {"inner": None}, [None, 2.0]],
                "scalar": None,
            }
        }

    def test_output_is_json_serializable(self):
        """The whole point — sanitized output must round-trip through json.dumps."""
        bad = {"a": float("nan"), "b": [float("inf"), 1.0]}
        safe = sanitize_for_json(bad)
        # Should not raise
        json.dumps(safe)


class TestEmit:
    def test_writes_one_ndjson_line_to_stdout(self, capsys):
        emit({"type": "result", "value": 42})
        cap = capsys.readouterr()
        line = cap.out.strip()
        assert json.loads(line) == {"type": "result", "value": 42}

    def test_sanitizes_nan(self, capsys):
        emit({"type": "result", "score": float("nan")})
        cap = capsys.readouterr()
        data = json.loads(cap.out.strip())
        assert data["score"] is None

    def test_error_type_also_writes_to_stderr(self, capsys):
        emit({"type": "error", "msg": "boom"})
        cap = capsys.readouterr()
        # stdout has the JSON
        assert json.loads(cap.out.strip())["type"] == "error"
        # stderr has the "Error: boom" line
        assert "boom" in cap.err

    def test_non_error_does_not_write_stderr(self, capsys):
        emit({"type": "progress", "pct": 50})
        cap = capsys.readouterr()
        assert cap.err == ""
