"""Guard the jasper-doctor registry's ordering invariant.

The doctor decomposition (the jasper/cli/doctor/ package) preserves the exact
run sequence of the former monolith by giving every check an explicit `order=`
key and sorting the registry by it. This test pins the invariants that keep
that deterministic: orders are unique and contiguous (0..N-1), there is exactly
one async check and it sorts last, and the decorator rejects a duplicate order
at registration. A future check added with a duplicate or gappy order — which
would silently fall back to import-order tie-breaking, the exact fragility the
registry exists to remove — fails here.
"""
from __future__ import annotations

import pytest

from jasper.cli.doctor import _registry
from jasper.cli.doctor._registry import doctor_check, registered_checks


def test_registered_check_orders_are_unique_and_contiguous():
    checks = registered_checks()
    assert checks, "registry is empty — the per-domain modules did not register"
    orders = [c.order for c in checks]
    assert len(orders) == len(set(orders)), f"duplicate order keys: {orders}"
    assert orders == list(range(len(orders))), (
        "orders must be contiguous 0..N-1 so the run sequence is fully "
        f"determined by `order`, never by import order (got {orders})"
    )


def test_exactly_one_async_check_and_it_sorts_last():
    checks = registered_checks()
    async_positions = [i for i, c in enumerate(checks) if c.is_async]
    assert len(async_positions) == 1, (
        f"expected exactly one async check, got {len(async_positions)}"
    )
    assert async_positions[0] == len(checks) - 1, (
        "the single async check must sort last (it is appended after the "
        "synchronous checks in run_async)"
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
