"""Guard the jasper-doctor registry's ordering invariant.

The doctor decomposition (the jasper/cli/doctor/ package) preserves the exact
run sequence of the former monolith by giving every check an explicit `order=`
key and sorting the registry by it. This test pins the invariants that keep
that deterministic: orders are unique sparse sort keys (gaps allowed so a
mid-list insert never renumbers — only strictly-increasing + unique matters),
async / exclusive-lane metadata is explicit, and the decorator rejects a
duplicate order at registration. A future check added with a DUPLICATE order —
which would silently fall back to import-order tie-breaking, the exact fragility
the registry exists to remove — fails here.
"""
from __future__ import annotations

import pytest

from jasper.cli.doctor import _registry
from jasper.cli.doctor._registry import doctor_check, registered_checks


def test_registered_check_orders_are_unique_and_strictly_increasing():
    checks = registered_checks()
    assert checks, "registry is empty — the per-domain modules did not register"
    orders = [c.order for c in checks]
    assert len(orders) == len(set(orders)), f"duplicate order keys: {orders}"
    # Sparse sort keys: gaps are intentional (a mid-list insert picks a value
    # between its neighbours, e.g. 20.5, renumbering nothing). registered_checks()
    # returns sorted, so the only remaining invariant is a tie-free sequence.
    assert all(a < b for a, b in zip(orders, orders[1:])), (
        f"orders must be strictly increasing (unique, no ties), got {orders}"
    )


def test_async_checks_keep_explicit_registry_metadata():
    checks = registered_checks()
    async_checks = [c for c in checks if c.is_async]
    assert async_checks, "expected at least one async check"
    assert all(c.label for c in async_checks), (
        "async checks need explicit labels for timeout/crash rows"
    )


def test_hardware_sensitive_checks_are_marked_exclusive():
    by_name = {c.func.__name__: c for c in registered_checks()}

    assert by_name["check_mic_capture"].exclusive_group == "audio-probe"
    assert (
        by_name["check_aec_bridge_output_health"].exclusive_group
        == "audio-probe"
    )
    assert (
        by_name["check_renderer_device_resolvable"].exclusive_group
        == "audio-probe"
    )


def test_duplicate_order_is_rejected_at_registration():
    """The decorator must enforce the documented uniqueness invariant — a
    silent duplicate would reintroduce import-order tie-breaking."""
    saved = list(_registry._REGISTRY)
    try:
        taken = next(iter(c.order for c in registered_checks()))
        with pytest.raises(ValueError, match="already registered"):
            doctor_check(order=taken, group="test")(lambda: None)
    finally:
        # Restore the registry even if the guard regressed and appended.
        _registry._REGISTRY[:] = saved
