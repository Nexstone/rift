"""Unit tests for the field catalog."""

from __future__ import annotations

import pytest

from rift_substrate.data.fields import (
    FIELDS,
    INGESTION_HINTS,
    field_requires,
    fields_by_ingestion,
    fields_by_source,
    hint_for_field,
)


class TestFieldCatalog:
    def test_catalog_nonempty(self):
        assert len(FIELDS) > 0

    def test_every_field_has_required_metadata(self):
        for name, spec in FIELDS.items():
            assert spec.name == name
            assert spec.source in ("candles", "funding", "fills", "l2", "ctx")
            assert spec.ingestion in ("fetch", "sync", "subscribe")
            assert spec.description

    def test_close_is_candle_field(self):
        spec = FIELDS["close"]
        assert spec.source == "candles"
        assert spec.ingestion == "fetch"

    def test_taker_ratio_requires_sync(self):
        spec = FIELDS["taker_ratio"]
        assert spec.source == "fills"
        assert spec.ingestion == "sync"

    def test_l2_fields_require_sync(self):
        for f in fields_by_source("l2"):
            assert f.ingestion == "sync"


class TestFieldRequires:
    def test_known_field(self):
        source, ingestion = field_requires("close")
        assert source == "candles"
        assert ingestion == "fetch"

    def test_unknown_field_raises(self):
        with pytest.raises(KeyError, match="Unknown field"):
            field_requires("nonexistent")


class TestFieldsBySource:
    def test_candles_includes_close(self):
        names = [f.name for f in fields_by_source("candles")]
        assert "close" in names
        assert "open" in names

    def test_l2_includes_spread(self):
        names = [f.name for f in fields_by_source("l2")]
        assert "spread_bps" in names


class TestHintForField:
    def test_fetch_field_returns_fetch_hint(self):
        h = hint_for_field("close")
        assert "rift fetch" in h

    def test_sync_field_returns_sync_hint(self):
        h = hint_for_field("taker_ratio")
        assert "rift sync" in h

    def test_unknown_field(self):
        h = hint_for_field("xyz")
        assert "Unknown" in h
