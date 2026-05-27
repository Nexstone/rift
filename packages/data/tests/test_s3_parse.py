"""Unit tests for rift_data.s3.parse — JSONL fill extraction."""

from __future__ import annotations

import json
import gzip

import pytest

from rift_data.s3.parse import _extract_coin_fills, _fills_list_to_df


def _make_jsonl_bytes(records: list[dict]) -> bytes:
    """Serialize a list of records as NDJSON bytes (the format S3 hourly files use)."""
    return b"\n".join(json.dumps(r).encode("utf-8") for r in records)


def _evt(coin: str, side: str = "B", dir_str: str = "Open Long", px: float = 50000,
         sz: float = 1.0, crossed: bool = True, time_ms: int = 1700000000000) -> list:
    """Build one HL event in the format S3 stores: [block_data, fill_dict]."""
    return [
        {"block": 1, "tx": "0xabc"},
        {
            "coin": coin, "side": side, "dir": dir_str,
            "px": str(px), "sz": str(sz), "crossed": crossed,
            "time": time_ms, "closedPnl": "0", "fee": "1.0", "startPosition": "0",
        },
    ]


class TestExtractCoinFills:
    def test_filters_to_target_coins(self):
        raw = _make_jsonl_bytes([
            {"events": [_evt("BTC"), _evt("ETH"), _evt("SOL")]}
        ])
        out = _extract_coin_fills(raw, {"BTC", "ETH"})
        assert len(out["BTC"]) == 1
        assert len(out["ETH"]) == 1
        assert "SOL" not in out  # not requested

    def test_empty_when_no_matches(self):
        raw = _make_jsonl_bytes([{"events": [_evt("SOL")]}])
        out = _extract_coin_fills(raw, {"BTC"})
        assert out["BTC"] == []

    def test_handles_multiple_lines(self):
        raw = _make_jsonl_bytes([
            {"events": [_evt("BTC", time_ms=1700000000000)]},
            {"events": [_evt("BTC", time_ms=1700000001000)]},
            {"events": [_evt("BTC", time_ms=1700000002000)]},
        ])
        out = _extract_coin_fills(raw, {"BTC"})
        assert len(out["BTC"]) == 3

    def test_handles_dict_event_shape(self):
        """Some events are dicts directly (not list-of-[block, fill])."""
        raw = _make_jsonl_bytes([
            {"events": [{"coin": "BTC", "side": "B", "dir": "Open Long",
                        "px": "50000", "sz": "1", "crossed": True,
                        "time": 1700000000000, "closedPnl": "0", "fee": "1", "startPosition": "0"}]}
        ])
        out = _extract_coin_fills(raw, {"BTC"})
        assert len(out["BTC"]) == 1

    def test_skips_malformed_json_lines(self):
        # One good line, one garbage, one good
        good = json.dumps({"events": [_evt("BTC")]}).encode("utf-8")
        raw = good + b"\nthis is not json\n" + good
        out = _extract_coin_fills(raw, {"BTC"})
        assert len(out["BTC"]) == 2

    def test_skips_events_with_invalid_timestamp(self):
        raw = _make_jsonl_bytes([
            {"events": [
                {"coin": "BTC", "side": "B", "dir": "Open Long",
                 "px": "50000", "sz": "1", "crossed": True,
                 "time": "not-a-number", "closedPnl": "0", "fee": "1", "startPosition": "0"},
            ]}
        ])
        out = _extract_coin_fills(raw, {"BTC"})
        assert out["BTC"] == []

    def test_is_open_set_from_dir_string(self):
        raw = _make_jsonl_bytes([
            {"events": [
                _evt("BTC", dir_str="Open Long"),
                _evt("BTC", dir_str="Close Long"),
            ]}
        ])
        out = _extract_coin_fills(raw, {"BTC"})
        # Tuple shape: (ts, px, sz, side, dir, is_open, is_long, ...)
        assert out["BTC"][0][5] is True   # Open Long → is_open=True
        assert out["BTC"][1][5] is False  # Close Long → is_open=False

    def test_is_long_set_from_dir_string(self):
        raw = _make_jsonl_bytes([
            {"events": [
                _evt("BTC", dir_str="Open Long"),
                _evt("BTC", dir_str="Open Short"),
            ]}
        ])
        out = _extract_coin_fills(raw, {"BTC"})
        assert out["BTC"][0][6] is True   # Open Long → is_long=True
        assert out["BTC"][1][6] is False  # Open Short → is_long=False

    def test_blank_lines_ignored(self):
        raw = b"\n\n" + json.dumps({"events": [_evt("BTC")]}).encode("utf-8") + b"\n\n\n"
        out = _extract_coin_fills(raw, {"BTC"})
        assert len(out["BTC"]) == 1


class TestFillsListToDf:
    def test_empty_returns_empty_df_with_full_schema(self):
        from rift_core.schema import FILL_SCHEMA
        df = _fills_list_to_df([])
        assert len(df) == 0
        assert set(df.columns) == set(FILL_SCHEMA.keys())

    def test_tuple_to_dataframe_shape(self, sample_fill_tuples):
        df = _fills_list_to_df(sample_fill_tuples)
        assert df.shape == (len(sample_fill_tuples), 11)

    def test_columns_in_correct_order(self, sample_fill_tuples):
        df = _fills_list_to_df(sample_fill_tuples)
        expected = ["timestamp", "price", "size", "side", "dir", "is_open",
                    "is_long", "crossed", "closed_pnl", "fee", "start_position"]
        assert df.columns == expected
