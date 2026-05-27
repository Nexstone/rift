"""Execution primitives — composable building blocks for trade lifecycle.

Today: exit policies (when/how to move stops, when to close).
Future: queue position models, latency models, smart order routing.

Strategy-agnostic. The engine doesn't pick which policy to use — the
user (or workbench config) does. Power users implement their own
`ExitPolicy` subclass and plug it in.
"""

from rift_substrate.execution.exit_policies import (
    BasicExit,
    ExitAction,
    ExitPolicy,
    FundingHoldExit,
    MeanReversionExit,
    PositionState,
    TrailingMomentumExit,
    resolve_policy,
)

__all__ = [
    "BasicExit",
    "ExitAction",
    "ExitPolicy",
    "FundingHoldExit",
    "MeanReversionExit",
    "PositionState",
    "TrailingMomentumExit",
    "resolve_policy",
]
