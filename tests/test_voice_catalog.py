"""Tests for the voice provider catalog's barge-in capability declaration.

Each ``ProviderCatalogEntry`` declares an ``interrupt_reconcile`` kind — the
"pack" metadata the robust-barge-in packs branch on instead of testing
provider name. These tests pin that every registry entry carries a valid,
resolvable declaration and that the four known providers map to the kinds
documented in the "Provider Interruption Contract".
"""
from __future__ import annotations

import pytest

from jasper.voice.catalog import (
    PROVIDERS,
    InterruptReconcile,
    resolve_interrupt_reconcile,
)


def test_every_provider_declares_a_valid_kind():
    """Every registry entry carries a resolvable ``InterruptReconcile`` kind,
    and ``resolve_interrupt_reconcile`` returns exactly what it declares."""
    for entry in PROVIDERS:
        assert isinstance(entry.interrupt_reconcile, InterruptReconcile), (
            f"{entry.id} interrupt_reconcile is not an InterruptReconcile"
        )
        assert resolve_interrupt_reconcile(entry.id) is entry.interrupt_reconcile


def test_known_provider_kinds():
    """Pin the documented contract: OpenAI needs a client truncate, Gemini
    self-truncates server-side, OpenAI Live owns its own interruption, and
    Grok declares OpenAI's shape directly (its adapter subclasses OpenAI's,
    tests/test_voice_barge_in_contract.py::test_grok_inherits_openai_seam)."""
    by_id = {entry.id: entry for entry in PROVIDERS}

    assert (
        by_id["openai"].interrupt_reconcile
        is InterruptReconcile.NEEDS_CLIENT_TRUNCATE
    )
    assert (
        by_id["gemini"].interrupt_reconcile
        is InterruptReconcile.SERVER_SELF_TRUNCATES
    )
    assert (
        by_id["openai_live"].interrupt_reconcile
        is InterruptReconcile.NATIVE_CONTINUOUS
    )
    assert (
        by_id["grok"].interrupt_reconcile
        is InterruptReconcile.NEEDS_CLIENT_TRUNCATE
    )

    # Grok's adapter subclasses OpenAI's, so the two resolve identically.
    assert resolve_interrupt_reconcile("grok") is resolve_interrupt_reconcile("openai")


def test_resolve_unknown_provider_raises():
    with pytest.raises(KeyError):
        resolve_interrupt_reconcile("nonexistent-provider")
