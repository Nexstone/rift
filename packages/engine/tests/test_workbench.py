"""Tests for the rebuilt workbench — Phase 8.

Pins three invariants:
  1. TEMPLATES contains only generic scaffolds (no style-named templates)
  2. SizingConfig serializes round-trip; legacy configs (no sizing) stay backward-compat
  3. Generated code: legacy path has no substrate.risk imports; substrate-sizing path
     has the imports + position_size() override + sizing params on the config dataclass
"""

from __future__ import annotations

import ast

import pytest

from rift_engine.workbench import (
    Condition,
    SizingConfig,
    StrategyConfig,
    TEMPLATES,
    generate_strategy_code,
    create_from_template,
)


# ─── TEMPLATES restructure ────────────────────────────────────────────


class TestTemplates:
    def test_only_generic_templates_ship(self):
        """Engine ships no strategy-style opinions — only blank + generic example."""
        assert set(TEMPLATES.keys()) == {"blank", "single_signal_example"}

    def test_no_style_named_templates(self):
        """Specifically: funding / vwap_reversion / trend_follow are gone."""
        for forbidden in ("funding", "vwap_reversion", "trend_follow"):
            assert forbidden not in TEMPLATES

    def test_blank_template_is_empty_skeleton(self):
        blank = TEMPLATES["blank"]
        assert blank.entry_conditions == []
        assert blank.exit_conditions == []

    def test_single_signal_example_has_minimal_conditions(self):
        example = TEMPLATES["single_signal_example"]
        assert len(example.entry_conditions) > 0
        assert len(example.exit_conditions) > 0

    def test_create_from_template_works_for_blank(self):
        cfg = create_from_template("blank", "my_new_strategy")
        assert cfg.name == "my_new_strategy"
        assert cfg.entry_conditions == []

    def test_create_from_template_rejects_removed_template_name(self):
        with pytest.raises(ValueError, match="Unknown template"):
            create_from_template("funding", "test")


# ─── SizingConfig dataclass ───────────────────────────────────────────


class TestSizingConfig:
    def test_round_trip_serialization(self):
        s = SizingConfig(
            method="vol_target",
            target_vol_annualized=0.20,
            kelly_fraction=0.25,
            max_single_position_pct=0.15,
        )
        d = s.to_dict()
        s2 = SizingConfig.from_dict(d)
        assert s2.method == "vol_target"
        assert s2.target_vol_annualized == 0.20
        assert s2.kelly_fraction == 0.25
        assert s2.max_single_position_pct == 0.15

    def test_defaults(self):
        s = SizingConfig()
        assert s.method == "vol_target"
        assert s.kelly_fraction == 0.5  # half-Kelly default

    def test_from_dict_handles_missing_keys(self):
        s = SizingConfig.from_dict({})
        # Should populate with defaults
        assert s.method == "vol_target"
        assert s.target_vol_annualized == 0.15


# ─── StrategyConfig integration ───────────────────────────────────────


class TestStrategyConfigSizing:
    def test_legacy_config_has_no_sizing(self):
        cfg = StrategyConfig(name="legacy", timeframe="1h", leverage=2.0)
        assert cfg.sizing is None
        d = cfg.to_dict()
        assert "sizing" not in d["risk"]

    def test_legacy_serialization_round_trip(self):
        """Old configs (no sizing field) load cleanly with sizing=None."""
        cfg = StrategyConfig(name="legacy", timeframe="1h", leverage=2.0)
        d = cfg.to_dict()
        cfg2 = StrategyConfig.from_dict(d)
        assert cfg2.sizing is None
        assert cfg2.leverage == 2.0

    def test_sizing_round_trip(self):
        cfg = StrategyConfig(
            name="vt", timeframe="1h",
            sizing=SizingConfig(method="vol_target", target_vol_annualized=0.20),
        )
        d = cfg.to_dict()
        assert "sizing" in d["risk"]
        cfg2 = StrategyConfig.from_dict(d)
        assert cfg2.sizing is not None
        assert cfg2.sizing.method == "vol_target"
        assert cfg2.sizing.target_vol_annualized == 0.20

    def test_load_legacy_json_config_without_sizing(self):
        """Configs serialized before Phase 8 must still load."""
        legacy_json = {
            "name": "old",
            "timeframe": "1h",
            "entry": {"conditions": [], "direction": "both"},
            "exit": {"conditions": [], "max_hold": 48},
            "risk": {"stop_loss": 0.02, "risk_per_trade": 0.02, "leverage": 2.0},
            # NO 'sizing' field — this is the test
            "filters": {},
            "version": 1,
        }
        cfg = StrategyConfig.from_dict(legacy_json)
        assert cfg.sizing is None
        assert cfg.leverage == 2.0


# ─── Generated code ───────────────────────────────────────────────────


class TestGeneratedCode:
    def _make_config(self, sizing: SizingConfig | None = None) -> StrategyConfig:
        return StrategyConfig(
            name="test_strat", description="",
            timeframe="1h",
            entry_conditions=[Condition(indicator="rsi", op="<", value=30, side="long")],
            exit_conditions=[Condition(indicator="rsi", op=">", value=70, side="long")],
            sizing=sizing,
        )

    def test_legacy_generated_code_parses(self):
        code = generate_strategy_code(self._make_config(sizing=None))
        ast.parse(code)

    def test_legacy_has_no_substrate_risk_imports(self):
        code = generate_strategy_code(self._make_config(sizing=None))
        assert "rift_substrate.risk" not in code
        assert "size_position" not in code

    def test_vol_target_generated_code_parses(self):
        code = generate_strategy_code(
            self._make_config(sizing=SizingConfig(method="vol_target"))
        )
        ast.parse(code)

    def test_vol_target_has_substrate_imports(self):
        code = generate_strategy_code(
            self._make_config(sizing=SizingConfig(method="vol_target"))
        )
        assert "from rift_substrate.risk import PositionLimits, size_position" in code
        assert "from rift_substrate import periods_per_year_for_interval" in code

    def test_vol_target_overrides_position_size(self):
        code = generate_strategy_code(
            self._make_config(sizing=SizingConfig(method="vol_target"))
        )
        # Generated class should define position_size method
        assert "def position_size(self) -> float:" in code
        # Should track recent returns
        assert "self._sizing_returns" in code

    def test_kelly_path_emits_kelly_params(self):
        code = generate_strategy_code(
            self._make_config(sizing=SizingConfig(method="kelly", kelly_fraction=0.25))
        )
        ast.parse(code)
        assert "sizing_method: str = 'kelly'" in code
        assert "kelly_fraction: float = 0.25" in code

    def test_fixed_fraction_path(self):
        code = generate_strategy_code(
            self._make_config(sizing=SizingConfig(method="fixed_fraction", fixed_fraction=0.03))
        )
        ast.parse(code)
        assert "sizing_method: str = 'fixed_fraction'" in code
        assert "fixed_fraction: float = 0.03" in code

    def test_periods_per_year_baked_in_correctly(self):
        """Generated code should use periods_per_year_for_interval(<timeframe>)
        not a hardcoded annualization."""
        code = generate_strategy_code(
            self._make_config(sizing=SizingConfig(method="vol_target"))
        )
        assert "periods_per_year_for_interval('1h')" in code
