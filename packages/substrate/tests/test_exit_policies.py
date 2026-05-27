"""Unit tests for substrate.execution.exit_policies.

These tests pin the exact numerical behaviour the policies inherit from the
original `recon.py` if/elif chain. Any change here should be deliberate and
documented — the recon refactor depends on bit-for-bit equivalence.
"""

from __future__ import annotations

import pytest

from rift_substrate.execution import (
    BasicExit,
    ExitAction,
    ExitPolicy,
    FundingHoldExit,
    MeanReversionExit,
    PositionState,
    TrailingMomentumExit,
    resolve_policy,
)
from rift_substrate.execution.exit_policies import register_policy


# ─── PositionState properties ─────────────────────────────────────────


class TestPositionState:
    def test_in_profit_long(self):
        s = _state(side="long", entry_price=100.0, current_price=101.0)
        assert s.in_profit is True

        s = _state(side="long", entry_price=100.0, current_price=99.0)
        assert s.in_profit is False

    def test_in_profit_short(self):
        s = _state(side="short", entry_price=100.0, current_price=99.0)
        assert s.in_profit is True

        s = _state(side="short", entry_price=100.0, current_price=101.0)
        assert s.in_profit is False

    def test_profit_from_entry_long(self):
        s = _state(entry_price=100.0, current_price=102.0)
        assert s.profit_from_entry == pytest.approx(0.02)

    def test_profit_from_entry_handles_zero_entry(self):
        s = _state(entry_price=0.0, current_price=100.0)
        assert s.profit_from_entry == 0.0

    def test_profit_from_entry_is_unsigned(self):
        """Same magnitude whether price moved up or down."""
        s_up = _state(entry_price=100.0, current_price=103.0)
        s_dn = _state(entry_price=100.0, current_price=97.0)
        assert s_up.profit_from_entry == s_dn.profit_from_entry == pytest.approx(0.03)


# ─── BasicExit ─────────────────────────────────────────────────────────


class TestBasicExit:
    def test_no_op_always(self):
        policy = BasicExit()
        s = _state(elapsed_seconds=999_999, in_profit=True)
        action = policy.update(s)
        assert action.new_stop is None
        assert action.breakeven_reached is False
        assert action.close_position is False
        assert action.status_message is None

    def test_max_hold_default(self):
        assert BasicExit().max_hold_seconds == 4 * 3600


# ─── TrailingMomentumExit ─────────────────────────────────────────────


class TestTrailingMomentumExit:
    def test_no_action_when_not_in_profit(self):
        policy = TrailingMomentumExit()
        s = _state(side="long", entry_price=100.0, current_price=99.0,
                   atr_dist=2.0, current_stop=98.0)
        action = policy.update(s)
        assert action.new_stop is None
        assert action.breakeven_reached is False

    def test_no_breakeven_when_below_1x_atr_profit(self):
        """In profit but only by 0.5x ATR — should NOT trigger breakeven."""
        policy = TrailingMomentumExit()
        # entry=100, atr=2, threshold=2/100=0.02; price needs to be > 102 for trigger
        s = _state(side="long", entry_price=100.0, current_price=101.0,
                   atr_dist=2.0, current_stop=98.0)
        action = policy.update(s)
        assert action.new_stop is None
        assert action.breakeven_reached is False

    def test_breakeven_triggers_at_1x_atr_profit_long(self):
        policy = TrailingMomentumExit()
        # entry=100, atr=2, profit_from_entry needs >= 2/100 = 0.02 → price >= 102
        s = _state(side="long", entry_price=100.0, current_price=102.0,
                   peak_price=102.0, atr_dist=2.0, current_stop=98.0)
        action = policy.update(s)
        assert action.new_stop == 100.0  # breakeven = entry price
        assert action.breakeven_reached is True
        assert action.status_message is not None
        assert "breakeven" in action.status_message

    def test_breakeven_triggers_at_1x_atr_profit_short(self):
        policy = TrailingMomentumExit()
        # short entry=100, atr=2, current=98 → profit_from_entry = 0.02
        s = _state(side="short", entry_price=100.0, current_price=98.0,
                   peak_price=98.0, atr_dist=2.0, current_stop=102.0)
        action = policy.update(s)
        assert action.new_stop == 100.0
        assert action.breakeven_reached is True

    def test_breakeven_does_not_re_trigger(self):
        """Once breakeven_reached=True, the breakeven branch shouldn't fire again."""
        policy = TrailingMomentumExit()
        s = _state(side="long", entry_price=100.0, current_price=102.0,
                   peak_price=102.0, atr_dist=2.0, current_stop=100.0,
                   breakeven_reached=True)
        action = policy.update(s)
        # peak hasn't moved above breakeven+1.5*atr=103, so trail does nothing
        assert action.new_stop is None
        # breakeven flag NOT re-asserted (it was already True)
        assert action.breakeven_reached is False

    def test_trailing_after_breakeven_long(self):
        """After breakeven, stop trails at peak - 1.5x ATR."""
        policy = TrailingMomentumExit()
        # peak=110, atr=2, trail_stop=110-3=107; current_stop=100 → should update to 107
        s = _state(side="long", entry_price=100.0, current_price=110.0,
                   peak_price=110.0, atr_dist=2.0, current_stop=100.0,
                   breakeven_reached=True)
        action = policy.update(s)
        assert action.new_stop == 107.0

    def test_trailing_after_breakeven_short(self):
        """For shorts, trail at peak + 1.5x ATR (peak is lowest price)."""
        policy = TrailingMomentumExit()
        # short, peak (lowest)=90, atr=2, trail_stop=90+3=93; current=100 → update to 93
        s = _state(side="short", entry_price=100.0, current_price=90.0,
                   peak_price=90.0, atr_dist=2.0, current_stop=100.0,
                   breakeven_reached=True)
        action = policy.update(s)
        assert action.new_stop == 93.0

    def test_trailing_does_not_loosen_stop(self):
        """Trail stop only tightens (favorable direction), never loosens."""
        policy = TrailingMomentumExit()
        # long, peak=110, current_stop=108 (already tighter than 107)
        s = _state(side="long", entry_price=100.0, current_price=110.0,
                   peak_price=110.0, atr_dist=2.0, current_stop=108.0,
                   breakeven_reached=True)
        action = policy.update(s)
        assert action.new_stop is None


# ─── FundingHoldExit ──────────────────────────────────────────────────


class TestFundingHoldExit:
    def test_no_action_before_2h(self):
        policy = FundingHoldExit()
        s = _state(elapsed_seconds=3600, funding_collected=10.0,
                   side="long", entry_price=100.0, current_stop=98.0, atr_dist=2.0)
        assert policy.update(s).new_stop is None

    def test_no_action_after_2h_with_zero_funding(self):
        policy = FundingHoldExit()
        s = _state(elapsed_seconds=8000, funding_collected=0.0,
                   side="long", entry_price=100.0, current_stop=98.0, atr_dist=2.0)
        assert policy.update(s).new_stop is None

    def test_widen_after_2h_long(self):
        """After 2h + funding>0, widen to entry - 1.5x ATR (for longs)."""
        policy = FundingHoldExit()
        # entry=100, atr=2, widened=100-3=97; current_stop=98 → 97 is wider (lower for longs)
        s = _state(elapsed_seconds=8000, funding_collected=10.0,
                   side="long", entry_price=100.0, current_stop=98.0, atr_dist=2.0)
        action = policy.update(s)
        assert action.new_stop == 97.0

    def test_widen_after_2h_short(self):
        """After 2h + funding>0, widen to entry + 1.5x ATR (for shorts)."""
        policy = FundingHoldExit()
        # entry=100, atr=2, widened=100+3=103; current_stop=102 → 103 is wider (higher for shorts)
        s = _state(elapsed_seconds=8000, funding_collected=10.0,
                   side="short", entry_price=100.0, current_stop=102.0, atr_dist=2.0)
        action = policy.update(s)
        assert action.new_stop == 103.0

    def test_widen_skipped_when_current_stop_already_wider(self):
        policy = FundingHoldExit()
        # current_stop=96 is already wider than the proposed 97 → no change
        s = _state(elapsed_seconds=8000, funding_collected=10.0,
                   side="long", entry_price=100.0, current_stop=96.0, atr_dist=2.0)
        assert policy.update(s).new_stop is None

    def test_negative_funding_also_triggers_widen(self):
        """abs(funding) > 0 — works either way."""
        policy = FundingHoldExit()
        s = _state(elapsed_seconds=8000, funding_collected=-10.0,
                   side="long", entry_price=100.0, current_stop=98.0, atr_dist=2.0)
        action = policy.update(s)
        assert action.new_stop == 97.0

    def test_does_not_set_breakeven_flag(self):
        """Funding policy is independent of the breakeven mechanism."""
        policy = FundingHoldExit()
        s = _state(elapsed_seconds=8000, funding_collected=10.0,
                   side="long", entry_price=100.0, current_stop=98.0, atr_dist=2.0)
        assert policy.update(s).breakeven_reached is False


# ─── MeanReversionExit ────────────────────────────────────────────────


class TestMeanReversionExit:
    def test_no_action_before_half_max_hold(self):
        policy = MeanReversionExit()
        # max_hold=1800, half=900 → at 800s, no action
        s = _state(elapsed_seconds=800, side="long", entry_price=100.0,
                   current_stop=98.0, atr_dist=2.0)
        action = policy.update(s)
        assert action.new_stop is None
        assert action.breakeven_reached is False

    def test_no_action_when_breakeven_already_reached(self):
        policy = MeanReversionExit()
        s = _state(elapsed_seconds=1500, side="long", entry_price=100.0,
                   current_stop=98.0, atr_dist=2.0, breakeven_reached=True)
        action = policy.update(s)
        assert action.new_stop is None
        assert action.breakeven_reached is False  # not re-asserted

    def test_tightens_stop_long_after_half_max_hold(self):
        policy = MeanReversionExit()
        # entry=100, atr=2, tight=100-1=99; current_stop=98 → tighten to 99
        s = _state(elapsed_seconds=1500, side="long", entry_price=100.0,
                   current_stop=98.0, atr_dist=2.0)
        action = policy.update(s)
        assert action.new_stop == 99.0
        assert action.breakeven_reached is True
        assert action.status_message is not None
        assert "tightened" in action.status_message.lower()

    def test_tightens_stop_short_after_half_max_hold(self):
        policy = MeanReversionExit()
        # entry=100, atr=2, tight=100+1=101; current_stop=102 → tighten to 101
        s = _state(elapsed_seconds=1500, side="short", entry_price=100.0,
                   current_stop=102.0, atr_dist=2.0)
        action = policy.update(s)
        assert action.new_stop == 101.0
        assert action.breakeven_reached is True

    def test_flag_and_message_fire_even_when_stop_not_actually_moved(self):
        """Match original recon behaviour: even if proposed stop isn't more favorable,
        the breakeven flag is set + status emitted to prevent re-firing."""
        policy = MeanReversionExit()
        # current_stop=99.5 is already tighter than proposed 99 → no actual move
        s = _state(elapsed_seconds=1500, side="long", entry_price=100.0,
                   current_stop=99.5, atr_dist=2.0)
        action = policy.update(s)
        assert action.new_stop is None  # stop didn't actually move
        assert action.breakeven_reached is True  # but flag still set
        assert action.status_message is not None  # and message still emitted


# ─── resolve_policy ───────────────────────────────────────────────────


class TestResolvePolicy:
    def test_resolves_known_names(self):
        assert isinstance(resolve_policy("basic"), BasicExit)
        assert isinstance(resolve_policy("momentum"), TrailingMomentumExit)
        assert isinstance(resolve_policy("funding"), FundingHoldExit)
        assert isinstance(resolve_policy("mean_reversion"), MeanReversionExit)

    def test_unknown_falls_back_to_basic(self):
        for name in ["", "unknown", "hmm_trend", "carry", None]:
            try:
                p = resolve_policy(name)  # type: ignore[arg-type]
                assert isinstance(p, BasicExit), f"{name!r} should fall back to BasicExit"
            except TypeError:
                # None breaks dict.get(); acceptable
                pass

    def test_max_hold_seconds_match_original_recon_dict(self):
        """The original recon.py MAX_HOLD dict the policies replace."""
        assert resolve_policy("funding").max_hold_seconds == 8 * 3600
        assert resolve_policy("momentum").max_hold_seconds == 4 * 3600
        assert resolve_policy("mean_reversion").max_hold_seconds == 30 * 60
        # Default fallback matched original `MAX_HOLD.get(..., 4 * 3600)`
        assert resolve_policy("unknown").max_hold_seconds == 4 * 3600

    def test_register_custom_policy(self):
        class _Custom(BasicExit):
            name = "custom_test"
            max_hold_seconds = 99

        register_policy("custom_test", _Custom)
        assert isinstance(resolve_policy("custom_test"), _Custom)


# ─── helpers ──────────────────────────────────────────────────────────


def _state(**overrides) -> PositionState:
    """Build a PositionState with sensible defaults for testing.

    Defaults match a typical small long position at entry, so individual
    tests only need to override the fields they care about.
    """
    defaults = dict(
        side="long",
        entry_price=100.0,
        current_price=100.0,
        peak_price=100.0,
        elapsed_seconds=0.0,
        current_stop=98.0,
        original_stop=98.0,
        atr_dist=2.0,
        funding_collected=0.0,
        breakeven_reached=False,
    )
    # Allow `in_profit=True` as a convenience override that just bumps current_price
    in_profit = overrides.pop("in_profit", None)
    if in_profit is True:
        defaults["current_price"] = defaults["entry_price"] * 1.05  # 5% favorable
    defaults.update(overrides)
    return PositionState(**defaults)  # type: ignore[arg-type]
