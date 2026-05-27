"""Exit policies — composable rules for managing an open position's stop and lifetime.

Each `ExitPolicy` is a self-contained primitive that recon (or any executor) can
call periodically to decide whether to move the stop, emit a status update, or
close the position. The policy holds no per-position state itself; everything
position-specific lives in `PositionState`, which the executor constructs each tick.

Available built-in policies (named to match user-facing `hold_type` config values):

- `BasicExit`         — fixed stop + max hold, no dynamic adjustment. Default fallback.
- `TrailingMomentumExit` — move to breakeven at 1x ATR profit; trail at peak ± 1.5x ATR.
- `FundingHoldExit`   — widen stop after 2h if funding has been accruing.
- `MeanReversionExit` — tighten stop aggressively at half max-hold elapsed.

User picks a profile in the workbench config (`hold_type: "funding"` etc.) and
`resolve_policy(name)` returns the matching `ExitPolicy` instance. Power users
subclass `ExitPolicy` and pass their own instance directly.

The behaviour here is a verbatim port of the if/elif/MAX_HOLD logic that used
to live in `recon.py` — same numerics, same edge cases.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PositionState:
    """Snapshot of an open position at one moment in time.

    Recon constructs this each loop iteration and passes it to the active
    `ExitPolicy`. Policies are stateless across calls; any "I already moved
    the stop to breakeven" memory lives in `breakeven_reached`, which the
    executor tracks and toggles based on `ExitAction.breakeven_reached`.
    """

    side: str                       # "long" or "short"
    entry_price: float              # actual fill price
    current_price: float            # live mid price
    peak_price: float               # best favorable price since entry
    elapsed_seconds: float          # seconds since entry
    current_stop: float             # current stop price (may have been moved already)
    original_stop: float            # stop price at entry
    atr_dist: float                 # ATR distance (price units) for sizing stop moves
    funding_collected: float = 0.0  # cumulative funding accrued
    breakeven_reached: bool = False # whether a policy has already armed the trailing mode

    @property
    def in_profit(self) -> bool:
        if self.side == "long":
            return self.current_price > self.entry_price
        return self.current_price < self.entry_price

    @property
    def profit_from_entry(self) -> float:
        """Unsigned fractional move from entry (e.g., 0.02 = 2%)."""
        if self.entry_price <= 0:
            return 0.0
        return abs(self.current_price - self.entry_price) / self.entry_price


@dataclass
class ExitAction:
    """What a policy thinks the executor should do this tick.

    All fields are optional / "no change". Executor reads each:
      - new_stop:         if non-None, move the stop to this price
      - breakeven_reached: if True, executor toggles its breakeven flag
      - close_position:   if True, exit the position now
      - status_message:   if non-None, executor emits a status update
    """

    new_stop: float | None = None
    breakeven_reached: bool = False
    close_position: bool = False
    status_message: str | None = None


class ExitPolicy(ABC):
    """Base class for exit-management policies.

    Subclasses declare:
      - `name`              — display label, also the value users pick in workbench config
      - `max_hold_seconds`  — when the executor should force-close regardless of price

    And implement:
      - `update(state)` returning an `ExitAction`
    """

    name: str = "basic"
    max_hold_seconds: int = 4 * 3600

    @abstractmethod
    def update(self, state: PositionState) -> ExitAction:
        """Called each tick while position is open. Return what to change."""
        raise NotImplementedError


class BasicExit(ExitPolicy):
    """Fixed stop + max hold. No dynamic adjustment.

    The safest fallback when no specific exit profile applies. Recon's
    standard stop-loss + max-hold checks still apply on top of this.
    """

    name = "basic"
    max_hold_seconds = 4 * 3600

    def update(self, state: PositionState) -> ExitAction:
        return ExitAction()


class TrailingMomentumExit(ExitPolicy):
    """Momentum-style trailing stop.

    Behavior:
      1. Once position is in profit by at least 1x ATR, move the stop to breakeven.
      2. After breakeven, trail the stop at peak ± 1.5x ATR (in the favorable direction).

    Replicates `recon.py` momentum branch verbatim.
    """

    name = "momentum"
    max_hold_seconds = 4 * 3600
    breakeven_trigger_atr_mult = 1.0
    trail_distance_atr_mult = 1.5

    def update(self, state: PositionState) -> ExitAction:
        action = ExitAction()

        # Breakeven trigger: in profit + crossed 1x ATR + haven't moved yet
        if (
            not state.breakeven_reached
            and state.in_profit
            and state.entry_price > 0
            and state.profit_from_entry >= (state.atr_dist * self.breakeven_trigger_atr_mult) / state.entry_price
        ):
            action.new_stop = state.entry_price
            action.breakeven_reached = True
            action.status_message = f"Stop moved to breakeven ${state.entry_price:,.6g}"
            return action

        # Trailing once breakeven is armed
        if state.breakeven_reached:
            if state.side == "long":
                trail = state.peak_price - state.atr_dist * self.trail_distance_atr_mult
                if trail > state.current_stop:
                    action.new_stop = trail
            else:
                trail = state.peak_price + state.atr_dist * self.trail_distance_atr_mult
                if trail < state.current_stop:
                    action.new_stop = trail

        return action


class FundingHoldExit(ExitPolicy):
    """Funding-capture exit profile.

    Behavior:
      - For the first 2 hours: no stop changes (let the position breathe; the edge
        is from funding accrual, not price movement).
      - After 2h, if funding has been accruing: widen the stop to entry ± 1.5x ATR
        (only in the direction that actually widens it). This buys more time for
        funding to keep paying.

    Replicates `recon.py` funding branch verbatim. Note: this policy does NOT
    set `breakeven_reached` — it's a separate concern from the breakeven flag.
    """

    name = "funding"
    max_hold_seconds = 8 * 3600
    widen_after_seconds = 2 * 3600
    widen_distance_atr_mult = 1.5

    def update(self, state: PositionState) -> ExitAction:
        action = ExitAction()

        if state.elapsed_seconds > self.widen_after_seconds and abs(state.funding_collected) > 0:
            if state.side == "long":
                widened = state.entry_price - state.atr_dist * self.widen_distance_atr_mult
                if widened < state.current_stop:
                    action.new_stop = widened
            else:
                widened = state.entry_price + state.atr_dist * self.widen_distance_atr_mult
                if widened > state.current_stop:
                    action.new_stop = widened

        return action


class MeanReversionExit(ExitPolicy):
    """Mean-reversion exit profile.

    Behavior:
      - At half of `max_hold_seconds` elapsed, tighten the stop to entry ± 0.5x ATR.
      - Sets `breakeven_reached` to suppress further mean-reversion tightening on
        subsequent ticks (same trick the original recon code used to fire once).

    Replicates `recon.py` mean_reversion branch verbatim. Like the original,
    the status message and `breakeven_reached` flag fire whenever the elapsed
    condition trips, even if the proposed tight stop wasn't more favorable than
    the current stop (which would leave `new_stop=None`).
    """

    name = "mean_reversion"
    max_hold_seconds = 30 * 60
    tighten_after_fraction = 0.5
    tighten_distance_atr_mult = 0.5

    def update(self, state: PositionState) -> ExitAction:
        action = ExitAction()

        if (
            state.elapsed_seconds > self.max_hold_seconds * self.tighten_after_fraction
            and not state.breakeven_reached
        ):
            proposed_stop = state.current_stop
            if state.side == "long":
                tight = state.entry_price - state.atr_dist * self.tighten_distance_atr_mult
                if tight > proposed_stop:
                    proposed_stop = tight
            else:
                tight = state.entry_price + state.atr_dist * self.tighten_distance_atr_mult
                if tight < proposed_stop:
                    proposed_stop = tight

            if proposed_stop != state.current_stop:
                action.new_stop = proposed_stop
            action.breakeven_reached = True
            action.status_message = f"Mean reversion: stop tightened to ${proposed_stop:,.6g}"

        return action


# ── Registry ──────────────────────────────────────────────────────────


_POLICY_REGISTRY: dict[str, type[ExitPolicy]] = {
    "basic": BasicExit,
    "momentum": TrailingMomentumExit,
    "funding": FundingHoldExit,
    "mean_reversion": MeanReversionExit,
}


def resolve_policy(name: str) -> ExitPolicy:
    """Resolve a user-facing label to an `ExitPolicy` instance.

    Unknown names fall back to `BasicExit` (safe default — fixed stop, no
    dynamic adjustment). Power users can pass their own `ExitPolicy` subclass
    directly to recon and skip this resolver.
    """
    cls = _POLICY_REGISTRY.get(name, BasicExit)
    return cls()


def register_policy(name: str, policy_cls: type[ExitPolicy]) -> None:
    """Register a custom policy under a name. For testing and extension."""
    _POLICY_REGISTRY[name] = policy_cls
