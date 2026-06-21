"""Tests for the voice provider catalog's barge-in capability declaration.

Each ``ProviderCatalogEntry`` declares an ``interrupt_reconcile`` kind — the
"pack" metadata the robust-barge-in packs branch on instead of testing
provider name. These tests pin that every registry entry carries a valid,
resolvable declaration and that the three known providers map to the kinds
documented in the "Provider Interruption Contract"
(docs/HANDOFF-voice-providers.md).
"""
from __future__ import annotations

import dataclasses

import pytest

from jasper.voice import catalog
from jasper.voice.catalog import (
    PROVIDERS,
    InterruptReconcile,
    ProviderCatalogEntry,
    resolve_interrupt_reconcile,
)


def test_every_provider_declares_a_valid_kind():
    for entry in PROVIDERS:
        assert isinstance(entry.interrupt_reconcile, InterruptReconcile), (
            f"{entry.id} interrupt_reconcile is not an InterruptReconcile"
        )


def test_every_provider_resolves_to_a_concrete_kind():
    """resolve_interrupt_reconcile() must always return a concrete kind —
    never INHERITS — so packs get an actionable value without following the
    inheritance edge themselves."""
    concrete = {
        InterruptReconcile.NEEDS_CLIENT_TRUNCATE,
        InterruptReconcile.SERVER_SELF_TRUNCATES,
    }
    for entry in PROVIDERS:
        resolved = resolve_interrupt_reconcile(entry.id)
        assert resolved in concrete, f"{entry.id} resolved to {resolved}"


def test_inherits_entries_have_a_resolvable_base():
    for entry in PROVIDERS:
        if entry.interrupt_reconcile is InterruptReconcile.INHERITS:
            assert entry.interrupt_reconcile_base, (
                f"{entry.id} declares INHERITS but sets no base"
            )
            base = catalog.provider_by_id(entry.interrupt_reconcile_base)
            assert base is not None, (
                f"{entry.id} inherits from unknown provider "
                f"{entry.interrupt_reconcile_base!r}"
            )


def test_base_only_set_when_inheriting():
    """A non-empty base on a non-INHERITS entry is a declaration smell —
    it would silently never be consulted."""
    for entry in PROVIDERS:
        if entry.interrupt_reconcile is not InterruptReconcile.INHERITS:
            assert entry.interrupt_reconcile_base == "", (
                f"{entry.id} sets interrupt_reconcile_base without INHERITS"
            )


def test_known_provider_kinds():
    """Pin the documented contract: OpenAI needs a client truncate, Gemini
    self-truncates server-side, Grok inherits OpenAI's shape."""
    by_id = {entry.id: entry for entry in PROVIDERS}

    assert (
        by_id["openai"].interrupt_reconcile
        is InterruptReconcile.NEEDS_CLIENT_TRUNCATE
    )
    assert (
        by_id["gemini"].interrupt_reconcile
        is InterruptReconcile.SERVER_SELF_TRUNCATES
    )
    assert by_id["grok"].interrupt_reconcile is InterruptReconcile.INHERITS
    assert by_id["grok"].interrupt_reconcile_base == "openai"

    # Grok resolves to OpenAI's concrete kind.
    assert (
        resolve_interrupt_reconcile("grok")
        is InterruptReconcile.NEEDS_CLIENT_TRUNCATE
    )


def test_resolve_unknown_provider_raises():
    with pytest.raises(KeyError):
        resolve_interrupt_reconcile("nonexistent-provider")


# ---------------------------------------------------------------------------
# Resolver safety guards (exercised against crafted registries — the real
# registry can never reach these states, but a future provider edit could).
# ---------------------------------------------------------------------------


def _entry(provider_id, kind, base=""):
    return ProviderCatalogEntry(
        id=provider_id,
        label=provider_id,
        vendor="test",
        key_env="X",
        key_prefix_hint="",
        key_url="",
        model_env="M",
        voice_env="V",
        cost_hint="",
        models=(),
        voices=(),
        interrupt_reconcile=kind,
        interrupt_reconcile_base=base,
    )


def test_resolver_rejects_inherits_without_base(monkeypatch):
    crafted = (_entry("solo", InterruptReconcile.INHERITS, base=""),)
    monkeypatch.setattr(catalog, "PROVIDERS", crafted)
    with pytest.raises(RuntimeError, match="no interrupt_reconcile_base"):
        resolve_interrupt_reconcile("solo")


def test_resolver_rejects_cyclic_inheritance(monkeypatch):
    crafted = (
        _entry("a", InterruptReconcile.INHERITS, base="b"),
        _entry("b", InterruptReconcile.INHERITS, base="a"),
    )
    monkeypatch.setattr(catalog, "PROVIDERS", crafted)
    with pytest.raises(RuntimeError, match="cyclic"):
        resolve_interrupt_reconcile("a")


def test_interrupt_reconcile_is_a_required_field():
    """A correctness-bearing capability is declared per provider, never
    silently defaulted — omitting it must fail loudly at construction rather
    than inherit a wrong barge-in behaviour."""
    fields = {f.name: f for f in dataclasses.fields(ProviderCatalogEntry)}
    assert fields["interrupt_reconcile"].default is dataclasses.MISSING
    assert fields["interrupt_reconcile"].default_factory is dataclasses.MISSING
    # Constructing an entry without it is a TypeError.
    with pytest.raises(TypeError):
        ProviderCatalogEntry(
            id="x",
            label="x",
            vendor="x",
            key_env="X",
            key_prefix_hint="",
            key_url="",
            model_env="M",
            voice_env="V",
            cost_hint="",
            models=(),
            voices=(),
        )
