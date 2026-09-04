# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered registry for jasper-doctor checks.

Contract:

- **Order is a sparse sort KEY, not a contiguous index.** Orders must be
  UNIQUE; gaps are intentional, so a check inserted between two others
  takes any value strictly between their orders and nothing renumbers.
  Sorting by ``order`` also makes the sequence independent of the order
  the per-domain modules were imported in.

- **One naming rule for every entry.** A check's displayed name and its
  crash-path label are both ``entry.label or _check_name(entry.func)``
  (``check_env_file`` → ``"env file"``), whatever its calling convention.

- **Async and hardware-sensitive checks carry explicit metadata.** Only
  one check in an ``exclusive_group=`` runs at a time, which keeps
  ALSA/proc evidence probes from observing one another's temporary opens
  while unrelated checks still run concurrently.

``group=`` is the per-domain dimension: it affects neither order nor
output, and only records which subsystem a check belongs to.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from ._shared import CheckResult


@dataclass(frozen=True)
class RegisteredCheck:
    """One registry entry. ``label`` left empty is derived from
    ``func.__name__``.
    """

    order: float
    group: str
    func: Callable[..., CheckResult] | Callable[..., Awaitable[CheckResult]]
    needs_cfg: bool = False
    is_async: bool = False
    label: str = ""
    exclusive_group: str = ""


_REGISTRY: list[RegisteredCheck] = []


def doctor_check(
    *,
    order: float,
    group: str,
    label: str = "",
    needs_cfg: bool = False,
    is_async: bool = False,
    exclusive_group: str = "",
) -> Callable[[Callable], Callable]:
    """Register a doctor check and return the function object unchanged, so
    it stays directly importable and unit-testable.

    Args:
        order: sparse sort key in the canonical run sequence; must be unique.
        group: subsystem/domain the check belongs to. Need not match the
            module name (``drift.py`` registers under ``install``).
            Organizational only, except that
            ``__init__._STREAMBOX_OMITTED_DOCTOR_GROUPS`` skips whole groups
            a streambox does not install.
        label: explicit display/crash label; empty derives it from
            ``__name__``.
        needs_cfg: True iff the check takes the ``Config`` argument.
        is_async: True for checks implemented as async callables.
        exclusive_group: serialization key for probes that are individually
            safe but perturb one another when run at the same instant (ALSA
            open probes, `/proc/asound` ownership reads). Empty = no lane.
    """

    def _register(fn: Callable) -> Callable:
        clash = next((c for c in _REGISTRY if c.order == order), None)
        if clash is not None:
            raise ValueError(
                f"doctor_check order={order} is already registered by "
                f"{clash.func.__module__}.{clash.func.__name__}; check orders "
                "must be unique — a duplicate would silently fall back to "
                "import-order tie-breaking. Conflicting check: "
                f"{fn.__module__}.{fn.__name__}."
            )
        _REGISTRY.append(
            RegisteredCheck(
                order=order,
                group=group,
                func=fn,
                needs_cfg=needs_cfg,
                is_async=is_async,
                label=label,
                exclusive_group=exclusive_group,
            )
        )
        return fn

    return _register


def registered_checks() -> list[RegisteredCheck]:
    """All registered checks sorted by ``order``."""
    return sorted(_REGISTRY, key=lambda c: c.order)
