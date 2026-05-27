"""Unit tests for translate_builder_fee_error.

Mid-session a user can revoke builder-fee approval via the HL UI. The
next order then comes back rejected and the raw HL message is opaque.
translate_builder_fee_error() detects those rejections and returns an
actionable message; non-builder errors pass through unchanged (caller
emits the raw error).
"""

from __future__ import annotations

import pytest

from rift_trade.builder_fee import translate_builder_fee_error


class TestRecognizesBuilderFeeErrors:
    @pytest.mark.parametrize("msg", [
        "Order rejected: builder fee exceeds approved max",
        "Builder fee not approved",
        "builder fee approval insufficient",
        "Insufficient builder fee rate",
        "maxBuilderFee check failed for builder 0x09...",
        "Builder fee approval has been revoked",
    ])
    def test_returns_guidance_for_builder_fee_errors(self, msg):
        out = translate_builder_fee_error(msg)
        assert out is not None
        assert "approve-builder-fee" in out

    def test_accepts_dict_response(self):
        resp = {"status": "err", "response": "Builder fee approval revoked"}
        out = translate_builder_fee_error(resp)
        assert out is not None
        assert "approve-builder-fee" in out


class TestPassesThroughNonBuilderErrors:
    @pytest.mark.parametrize("msg", [
        "Insufficient margin",
        "Order size below minimum",
        "Reduce-only order would not reduce position",
        "Tick size violation",
        "",
    ])
    def test_returns_none_for_unrelated_errors(self, msg):
        assert translate_builder_fee_error(msg) is None

    def test_returns_none_for_none(self):
        assert translate_builder_fee_error(None) is None

    def test_word_builder_alone_is_not_enough(self):
        # "builder" without fee/approv/rate context — not a fee error
        assert translate_builder_fee_error("builder address malformed") is None
