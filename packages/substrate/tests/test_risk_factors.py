"""Tests for substrate.risk.factors — MKT/SMB/UMD construction.

Each test pins one invariant:
  - Synthetic panel with KNOWN characteristic structure → factor sign matches.
  - Warmup correctness — NaN until enough history.
  - Point-in-time discipline — factor at t uses only data ≤ t.
  - Edge cases — NaN inputs, too few coins, missing volumes.
"""

from __future__ import annotations

import numpy as np
import pytest

from rift_substrate.risk.factors import (
    Factor,
    MarketFactor,
    MomentumFactor,
    ReturnsPanel,
    SizeFactor,
)


def _make_panel(
    T: int = 100,
    N: int = 20,
    seed: int = 42,
    drift: float = 0.001,
    vol: float = 0.02,
    with_volumes: bool = True,
) -> ReturnsPanel:
    rng = np.random.default_rng(seed)
    returns = rng.normal(drift, vol, (T, N))
    volumes = rng.lognormal(15, 1, (T, N)) if with_volumes else None
    timestamps = np.arange(T, dtype=np.int64) * 86_400_000
    coins = [f"COIN{i}" for i in range(N)]
    return ReturnsPanel(returns=returns, coins=coins, timestamps=timestamps, volumes=volumes)


# ─── ReturnsPanel basic ────────────────────────────────────────────────


class TestReturnsPanel:
    def test_basic_construction(self):
        p = _make_panel()
        assert p.n_periods == 100
        assert p.n_coins == 20
        assert p.returns.shape == (100, 20)

    def test_rejects_shape_mismatch(self):
        with pytest.raises(ValueError, match="columns"):
            ReturnsPanel(
                returns=np.zeros((10, 5)),
                coins=["A", "B", "C"],  # only 3, but returns has 5 cols
                timestamps=np.arange(10),
            )

    def test_rejects_row_mismatch(self):
        with pytest.raises(ValueError, match="rows"):
            ReturnsPanel(
                returns=np.zeros((10, 3)),
                coins=["A", "B", "C"],
                timestamps=np.arange(5),  # only 5 but returns has 10 rows
            )

    def test_volumes_shape_must_match(self):
        with pytest.raises(ValueError, match="volumes shape"):
            ReturnsPanel(
                returns=np.zeros((10, 3)),
                coins=["A", "B", "C"],
                timestamps=np.arange(10),
                volumes=np.zeros((10, 4)),  # wrong N
            )


# ─── MarketFactor ──────────────────────────────────────────────────────


class TestMarketFactor:
    def test_recovers_market_return(self):
        """If every coin returns +1%, MKT should be +1%."""
        T, N = 50, 10
        rets = np.full((T, N), 0.01)
        vols = np.full((T, N), 1e6)
        ts = np.arange(T) * 86_400_000
        panel = ReturnsPanel(
            returns=rets, coins=[f"C{i}" for i in range(N)],
            timestamps=ts, volumes=vols,
        )
        result = MarketFactor().build(panel)
        np.testing.assert_allclose(result, 0.01, atol=1e-10)

    def test_volume_weighted_dominates_high_vol(self):
        """Coins with bigger volumes get bigger weight in MKT."""
        T, N = 10, 6  # need >= MarketFactor.min_coins (5)
        rets = np.zeros((T, N))
        rets[:, 0] = 0.10   # +10% on coin 0 (the giant)
        rets[:, 1:] = -0.10  # everyone else loses
        # Coin 0 has 1000x the volume of others
        vols = np.ones((T, N)) * 1000.0
        vols[:, 0] = 1_000_000.0
        ts = np.arange(T) * 86_400_000
        panel = ReturnsPanel(
            returns=rets, coins=[f"C{i}" for i in range(N)],
            timestamps=ts, volumes=vols,
        )
        result = MarketFactor().build(panel)
        # Heavily weighted toward coin 0 → market return should be > +5%
        assert result[0] > 0.05

    def test_falls_back_to_equal_weight_without_volumes(self):
        T, N = 5, 6  # need >= MarketFactor.min_coins (5)
        rets = np.array([
            [0.01, 0.02, 0.03, 0.04, 0.05, 0.06],  # mean = 0.035
            [0.0] * 6,
            [-0.01, -0.02, -0.03, -0.04, -0.05, -0.06],  # mean = -0.035
            [0.10, -0.10, 0.10, -0.10, 0.10, -0.10],     # mean = 0
            [0.05] * 6,
        ])
        ts = np.arange(T) * 86_400_000
        panel = ReturnsPanel(
            returns=rets, coins=[f"C{i}" for i in range(N)], timestamps=ts, volumes=None,
        )
        result = MarketFactor().build(panel)
        np.testing.assert_allclose(result[0], 0.035, atol=1e-9)
        np.testing.assert_allclose(result[3], 0.0, atol=1e-9)

    def test_nan_handling_drops_invalid_rows(self):
        T, N = 5, 4
        rets = np.array([
            [0.01, np.nan, 0.03, 0.04],  # one NaN — should drop it
            [0.0, 0.0, np.nan, np.nan],   # two NaN — only 2 valid, < min_coins=5
            [0.02, 0.02, 0.02, 0.02],     # only 4 valid — < min_coins
            [0.01, 0.01, 0.01, 0.01],     # same
            [0.0, 0.0, 0.0, 0.0],
        ])
        ts = np.arange(T) * 86_400_000
        panel = ReturnsPanel(
            returns=rets, coins=["A", "B", "C", "D"], timestamps=ts, volumes=None,
        )
        result = MarketFactor().build(panel)
        # min_coins=5 but only 4 in universe → all NaN
        assert np.all(np.isnan(result))


# ─── SizeFactor ────────────────────────────────────────────────────────


class TestSizeFactor:
    def test_requires_volumes(self):
        panel = _make_panel(with_volumes=False)
        with pytest.raises(ValueError, match="volumes"):
            SizeFactor().build(panel)

    def test_warmup_period_is_nan(self):
        panel = _make_panel(T=50)
        result = SizeFactor(lookback_periods=30).build(panel)
        # First 30 periods should be NaN
        assert np.all(np.isnan(result[:30]))
        # Period 30 and onward — may or may not be valid (depends on data)
        # But at least some should be computed
        assert (~np.isnan(result[30:])).sum() > 0

    def test_small_coins_outperform_big_coins(self):
        """Construct a panel where small (low-vol) coins have higher returns —
        SMB should be POSITIVE on average."""
        T, N = 100, 20
        rng = np.random.default_rng(7)
        # Big coins (idx 0..9): low volume rank, but they get LOW returns
        # Small coins (idx 10..19): high volume rank ... wait this is backwards

        # Let me redo: arrange so coins 0..9 have BIG volumes, coins 10..19 SMALL
        # And give SMALL coins higher returns.
        volumes = np.zeros((T, N))
        volumes[:, :10] = 1e9   # big coins
        volumes[:, 10:] = 1e6   # small coins

        # Returns: small coins (10..19) get +0.005/period drift, big coins -0.005
        returns = rng.normal(0, 0.01, (T, N))
        returns[:, :10] -= 0.005
        returns[:, 10:] += 0.005

        ts = np.arange(T) * 86_400_000
        panel = ReturnsPanel(
            returns=returns, coins=[f"C{i}" for i in range(N)],
            timestamps=ts, volumes=volumes,
        )
        result = SizeFactor(lookback_periods=20, quantile=0.4).build(panel)
        valid = result[~np.isnan(result)]
        # Avg SMB should be POSITIVE (small minus big = positive)
        assert valid.mean() > 0
        # And meaningfully so — expected ~0.01 by construction
        assert valid.mean() > 0.005

    def test_big_coins_outperform_yields_negative_smb(self):
        """Inverse setup — big coins outperform → SMB should be NEGATIVE."""
        T, N = 100, 20
        rng = np.random.default_rng(8)
        volumes = np.zeros((T, N))
        volumes[:, :10] = 1e9
        volumes[:, 10:] = 1e6
        returns = rng.normal(0, 0.01, (T, N))
        returns[:, :10] += 0.005   # big coins win
        returns[:, 10:] -= 0.005
        ts = np.arange(T) * 86_400_000
        panel = ReturnsPanel(
            returns=returns, coins=[f"C{i}" for i in range(N)],
            timestamps=ts, volumes=volumes,
        )
        result = SizeFactor(lookback_periods=20, quantile=0.4).build(panel)
        valid = result[~np.isnan(result)]
        assert valid.mean() < 0
        assert valid.mean() < -0.005

    def test_rejects_invalid_quantile(self):
        with pytest.raises(ValueError, match="quantile"):
            SizeFactor(quantile=0.5)
        with pytest.raises(ValueError, match="quantile"):
            SizeFactor(quantile=0.0)


# ─── MomentumFactor ───────────────────────────────────────────────────


class TestMomentumFactor:
    def test_warmup_includes_skip(self):
        panel = _make_panel(T=60)
        result = MomentumFactor(lookback_periods=20, skip_periods=5).build(panel)
        # First 25 periods (lookback + skip) should be NaN
        assert np.all(np.isnan(result[:25]))

    def test_past_winners_continue_winning(self):
        """Coins that had high past returns get high current returns → UMD positive."""
        T, N = 80, 20
        rng = np.random.default_rng(9)
        returns = rng.normal(0, 0.01, (T, N))

        # Build a persistent-momentum panel:
        # Half the coins are "winners" with positive drift, half are "losers".
        returns[:, :10] += 0.003   # winners
        returns[:, 10:] -= 0.003   # losers

        ts = np.arange(T) * 86_400_000
        panel = ReturnsPanel(
            returns=returns, coins=[f"C{i}" for i in range(N)],
            timestamps=ts, volumes=None,
        )
        result = MomentumFactor(lookback_periods=20, skip_periods=3, quantile=0.3).build(panel)
        valid = result[~np.isnan(result)]
        # UMD should be positive on average — past winners continued to win
        assert valid.mean() > 0
        assert valid.mean() > 0.003  # expected difference ~2*drift

    def test_past_winners_now_losing_yields_negative_umd(self):
        """Right after a regime flip, past winners are now losing → UMD negative.

        Only check the early-second-half window where the trailing lookback
        is still dominated by the first-regime data. Later, the trailing
        window catches up to the new regime and UMD swings positive again.
        """
        T, N = 80, 20
        rng = np.random.default_rng(10)
        returns = rng.normal(0, 0.005, (T, N))  # quieter noise → cleaner signal
        # First 40 periods: coins 0-9 down, 10-19 up
        returns[:40, :10] -= 0.01
        returns[:40, 10:] += 0.01
        # Second 40 periods: invert — past winners (10-19) now losers
        returns[40:, :10] += 0.01
        returns[40:, 10:] -= 0.01
        ts = np.arange(T) * 86_400_000
        panel = ReturnsPanel(
            returns=returns, coins=[f"C{i}" for i in range(N)],
            timestamps=ts, volumes=None,
        )
        result = MomentumFactor(lookback_periods=20, skip_periods=3, quantile=0.3).build(panel)
        # Periods 40-50: the trailing window [17-47, 17-47] still mostly first regime
        # → ranks past winners (10-19), but they realize losses
        early_second_half = result[40:50]
        valid = early_second_half[~np.isnan(early_second_half)]
        assert valid.size > 0
        assert valid.mean() < 0

    def test_skip_period_is_respected(self):
        """Returns from the skip period should NOT influence ranking."""
        # Build a panel where the LAST `skip` periods strongly disagree
        # with the prior 30. Without skip: high-prior-coins might be misranked.
        T, N = 60, 10
        returns = np.zeros((T, N))
        # Periods 0-30: coins 0-4 (-0.01), coins 5-9 (+0.01)
        returns[:30, :5] = -0.01
        returns[:30, 5:] = 0.01
        # Periods 30-37 (the skip window): inverted
        returns[30:37, :5] = 0.05
        returns[30:37, 5:] = -0.05
        # Period 37 onward (target): coins 5-9 (past winners) keep winning
        returns[37:, :5] = -0.01
        returns[37:, 5:] = 0.01
        ts = np.arange(T) * 86_400_000
        panel = ReturnsPanel(
            returns=returns, coins=[f"C{i}" for i in range(N)],
            timestamps=ts, volumes=None,
        )
        # Ranking at t=37 should use periods [0, 30) (skip=7, lookback=30)
        # → coins 5-9 ranked highest, coins 0-4 lowest
        # Realize at t=37 → coins 5-9 returned +0.01 → UMD positive
        result = MomentumFactor(lookback_periods=30, skip_periods=7, quantile=0.4).build(panel)
        assert result[37] > 0  # positive — winners continued to win


# ─── Point-in-time discipline ─────────────────────────────────────────


class TestPointInTime:
    def test_factor_at_t_uses_only_data_at_or_before_t(self):
        """If we truncate the panel at period T_cut, the factor values at
        indices < T_cut must be unchanged."""
        full_panel = _make_panel(T=80, seed=11)
        full_result_smb = SizeFactor(lookback_periods=20).build(full_panel)
        full_result_umd = MomentumFactor(lookback_periods=15, skip_periods=3).build(full_panel)

        # Truncate to first 50 periods
        T_cut = 50
        truncated_panel = ReturnsPanel(
            returns=full_panel.returns[:T_cut].copy(),
            coins=full_panel.coins,
            timestamps=full_panel.timestamps[:T_cut].copy(),
            volumes=full_panel.volumes[:T_cut].copy() if full_panel.volumes is not None else None,
        )
        trunc_result_smb = SizeFactor(lookback_periods=20).build(truncated_panel)
        trunc_result_umd = MomentumFactor(lookback_periods=15, skip_periods=3).build(truncated_panel)

        # Up to T_cut, both should match (ignoring NaN comparisons)
        for full, trunc, name in [(full_result_smb, trunc_result_smb, "SMB"),
                                   (full_result_umd, trunc_result_umd, "UMD")]:
            for t in range(T_cut):
                if np.isnan(full[t]) and np.isnan(trunc[t]):
                    continue
                assert np.isclose(full[t], trunc[t], atol=1e-12), \
                    f"{name} at t={t} differs: full={full[t]}, trunc={trunc[t]}"
