"""
tests/test_classifier.py — Unit tests for the classification layer.

Tests cover:
  - All confirmed-table lookup paths (hard and soft)
  - Fallback table lookup paths
  - Reason-substring fallback
  - Default behaviour (unknown code → soft, never hard)
  - The permanently-unrecoverable code set
"""

import sys
from pathlib import Path

import pytest

# Make recova importable from tests/
sys.path.insert(0, str(Path(__file__).parent.parent))

from recova.classifier import (
    FALLBACK_HARD_CODES,
    FALLBACK_SOFT_CODES,
    PERMANENTLY_UNRECOVERABLE_CODES,
    REASON_HARD_SUBSTRINGS,
    REASON_SOFT_SUBSTRINGS,
    _lookup,
)


class TestFallbackHardCodes:
    """All fallback hard codes should classify as hard with confidence 0.7."""

    @pytest.mark.parametrize("code", list(FALLBACK_HARD_CODES))
    def test_hard_fallback(self, code):
        classification, confidence, reason, source = _lookup(code, "")
        assert classification == "hard", f"Expected 'hard' for code '{code}', got '{classification}'"
        assert confidence == 0.7
        assert source == "fallback_table"


class TestFallbackSoftCodes:
    """All fallback soft codes should classify as soft with confidence 0.7."""

    @pytest.mark.parametrize("code", list(FALLBACK_SOFT_CODES))
    def test_soft_fallback(self, code):
        classification, confidence, reason, source = _lookup(code, "")
        assert classification == "soft", f"Expected 'soft' for code '{code}', got '{classification}'"
        assert confidence == 0.7
        assert source == "fallback_table"


class TestReasonSubstrings:
    """Reason substring matching when error_code is absent."""

    @pytest.mark.parametrize("substr", REASON_HARD_SUBSTRINGS)
    def test_hard_reason_substring(self, substr):
        classification, confidence, reason, source = _lookup(None, f"payment failed because {substr}")
        assert classification == "hard"
        assert confidence == 0.7
        assert source == "reason_substring"

    @pytest.mark.parametrize("substr", REASON_SOFT_SUBSTRINGS)
    def test_soft_reason_substring(self, substr):
        classification, confidence, reason, source = _lookup(None, f"payment failed: {substr} error")
        assert classification == "soft"
        assert confidence == 0.7
        assert source == "reason_substring"


class TestDefaultBehaviour:
    """Unknown codes must default to soft, never hard."""

    def test_unknown_code_defaults_to_soft(self):
        classification, confidence, reason, source = _lookup("TOTALLY_UNKNOWN_CODE_XYZ", "")
        assert classification == "soft", (
            "Unknown codes must default to 'soft'. "
            "A false 'hard' gives up on recoverable revenue."
        )
        assert confidence == 0.5
        assert source == "default"

    def test_none_code_no_reason_defaults_to_soft(self):
        classification, confidence, reason, source = _lookup(None, "")
        assert classification == "soft"
        assert source == "default"

    def test_default_never_hard(self):
        """The default path must never produce 'hard'."""
        classification, confidence, reason, source = _lookup("MADE_UP_CODE_999", "some vague message")
        assert classification != "hard", (
            "Default should not produce 'hard'. "
            "Only matched codes produce 'hard'."
        )


class TestPermanentlyUnrecoverableCodes:
    """Permanently unrecoverable codes must classify as hard."""

    @pytest.mark.parametrize("code", list(PERMANENTLY_UNRECOVERABLE_CODES))
    def test_permanently_unrecoverable_is_hard(self, code):
        classification, _, _, _ = _lookup(code, "")
        assert classification == "hard", (
            f"Permanently unrecoverable code '{code}' must be classified as 'hard'."
        )


class TestCodePrecedence:
    """Confirmed table takes precedence over fallback table."""

    def test_confirmed_hard_overrides_fallback(self, monkeypatch):
        """If a code is in the confirmed hard table, confidence must be 1.0."""
        import recova.classifier as cls_module
        monkeypatch.setattr(cls_module, "_CONFIRMED_HARD", {"card_blacklisted"})
        monkeypatch.setattr(cls_module, "_CONFIRMED_SOFT", set())

        classification, confidence, reason, source = cls_module._lookup("card_blacklisted", "")
        assert classification == "hard"
        assert confidence == 1.0
        assert source == "confirmed_table"

    def test_confirmed_soft_overrides_fallback(self, monkeypatch):
        """If a code is in the confirmed soft table, confidence must be 1.0."""
        import recova.classifier as cls_module
        monkeypatch.setattr(cls_module, "_CONFIRMED_SOFT", {"insufficient_funds"})
        monkeypatch.setattr(cls_module, "_CONFIRMED_HARD", set())

        classification, confidence, reason, source = cls_module._lookup("insufficient_funds", "")
        assert classification == "soft"
        assert confidence == 1.0
        assert source == "confirmed_table"
