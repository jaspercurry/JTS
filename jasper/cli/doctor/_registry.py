"""Ordered registry for jasper-doctor checks.

This is the *re-homing* layer for ``jasper-doctor``'s decomposition: the
checks themselves moved into per-domain modules
(``jasper/cli/doctor/audio.py``, ``network.py``, …) but their
membership, display order, and per-check calling convention used to be
a single hand-ordered ``sync_checks`` literal inside ``run_async``.
This module replaces that literal with an explicit registry so the
order survives the split *byte-for-byte*.

Behaviour contract preserved from the old literal list:

- **Order is an explicit integer.** ``order=`` on each
  :func:`doctor_check` is the index the entry had in the original
  ``sync_checks`` list (the async CamillaDSP websocket check is the
  single tail entry). Registration happens as each per-domain module is
  imported by the package ``__init__``; sorting by ``order`` makes the
  final sequence independent of import order, so it equals the manifest
  order regardless of how the package wires its imports.

- **The bare-vs-tuple distinction is preserved.** In the old list a
  *bare* function reference got its crash-path label derived from
  ``fn.__name__`` (``check_env_file`` → ``"env file"``) by
  ``_normalize_doctor_check``; a ``(label, lambda: fn(cfg))`` *tuple*
  pinned the label explicitly and bound ``cfg``. ``run_async`` rebuilds
  exactly that ``DoctorCheck`` shape from the registry: a check with
  ``needs_cfg=False`` is emitted as the bare function (so the harness
  derives the same ``__name__`` crash label); a check with
  ``needs_cfg=True`` is emitted as ``(label, lambda: fn(cfg))`` (same
  explicit crash label, same ``cfg`` closure). So the displayed name on
  the success path (``CheckResult.name``) and the crash path
  (``_crashed_check_result``) are identical to before.

- **The single async check is modelled as a tail entry.** The
  CamillaDSP websocket check is the only async check; it is flagged
  ``is_async=True`` and ``run_async`` invokes it via
  ``_run_async_doctor_check`` after the synchronous list, so it always
  lands last — exactly as the old code appended it.

``group=`` is the per-domain dimension. It does not affect order or
output; it records which subsystem a check belongs to (the same domain
the check's module name reflects), so the registry can be filtered or
introspected by domain without re-deriving it from import paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from ._shared import CheckResult


@dataclass(frozen=True)
class RegisteredCheck:
    """One registry entry.

    ``func`` is the raw check function. ``needs_cfg`` mirrors whether the
    original list entry was a ``(label, lambda: fn(cfg))`` tuple (True)
    or a bare callable (False). ``label`` is the explicit tuple label
    when ``needs_cfg`` is True; for bare checks it is left empty and the
    harness derives the displayed/crash label from ``func.__name__`` —
    preserving the original behaviour exactly.
    """

    order: int
    group: str
    func: Callable[..., CheckResult] | Callable[..., Awaitable[CheckResult]]
    needs_cfg: bool = False
    is_async: bool = False
    label: str = ""


_REGISTRY: list[RegisteredCheck] = []


def doctor_check(
    *,
    order: int,
    group: str,
    label: str = "",
    needs_cfg: bool = False,
    is_async: bool = False,
) -> Callable[[Callable], Callable]:
    """Register a doctor check and return it unchanged.

    The decorator is *additive* — it records metadata in the ordered
    registry and returns the original function object untouched, so the
    function's identity, signature, and body are preserved (it stays
    directly importable and unit-testable, exactly as before).

    Args:
        order: integer position in the canonical run sequence. Must be
            unique across all registered checks.
        group: subsystem/domain the check belongs to (env, voice, audio,
            wake, renderers, integrations, web, correction, memory,
            resilience, aec, usbsink, network, satellites, peering,
            grouping). Organizational metadata only.
        label: explicit display/crash label. Required for ``needs_cfg``
            checks (the original ``(label, lambda)`` tuples). Leave empty
            for bare checks so the label is derived from ``__name__``.
        needs_cfg: True iff the check takes the ``Config`` argument (the
            original tuple-with-cfg-lambda entries).
        is_async: True for the single async CamillaDSP websocket check.
    """

    def _register(fn: Callable) -> Callable:
        clash = next((c for c in _REGISTRY if c.order == order), None)
        if clash is not None:
            raise ValueError(
                f"doctor_check order={order} is already registered by "
                f"{clash.func.__module__}.{clash.func.__name__}; check orders "
                "must be unique — they pin the canonical run sequence that the "
                "decomposition preserves, and a duplicate would silently fall "
                "back to import-order tie-breaking. Conflicting check: "
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
            )
        )
        return fn

    return _register


def registered_checks() -> list[RegisteredCheck]:
    """All registered checks in canonical order (sorted by ``order``).

    Sorting by the explicit ``order`` key makes the sequence independent
    of the order in which the per-domain modules happened to be
    imported, so it always equals the manifest order.
    """
    return sorted(_REGISTRY, key=lambda c: c.order)
