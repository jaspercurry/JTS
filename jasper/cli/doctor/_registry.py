# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered registry for jasper-doctor checks.

A check is registered by the module of the subsystem it observes, and
``MODULE_ROSTER`` below is the display order (ADR-0233 rule 4): checks
appear module by module in roster order, and within a module in source
order. Registering from a module the roster does not name is an
import-time error, so a new module has to be given a position before it
can run.

- **The bare-vs-tuple distinction.** A check with ``needs_cfg=False`` is
  emitted as a bare function, so the harness derives its displayed/crash
  label from ``fn.__name__`` (``check_env_file`` → ``"env file"``). A
  check with ``needs_cfg=True`` is emitted as ``(label, fn bound to cfg)``
  — the explicit label plus the ``cfg`` binding.

- **Async and hardware-sensitive checks carry explicit metadata.** A
  check flagged ``is_async=True`` is awaited directly by the harness.
  A check with ``exclusive_group=`` may still run while unrelated checks
  are in flight, but only one check in that group runs at a time. This
  keeps ALSA/proc evidence probes from observing one another's temporary
  opens while still allowing the rest of the subprocess-heavy doctor to
  run concurrently.

- **``core=True`` puts a check in ``--core``** (ADR-0233 rule 5); a
  ``--core`` run imports only ``CORE_MODULES``.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from ._shared import CheckResult

F = TypeVar(
    "F", bound=Callable[..., CheckResult] | Callable[..., Awaitable[CheckResult]]
)

MODULE_ROSTER: tuple[str, ...] = (
    "env",
    "voice",
    "audio",
    "active_speaker",
    "boot_config",
    "wake",
    "renderers",
    "integrations",
    "privsep",
    "secret_compartments",
    "web",
    "research",
    "correction",
    "memory",
    "drift",
    "resilience",
    "aec",
    "audio_runtime_fanin",
    "audio_runtime_camilla",
    "audio_runtime_ring",
    "audio_runtime_outputd",
    "usbsink",
    "network",
    "peering",
    "grouping",
)

_ROSTER_POSITION: dict[str, int] = {
    module: position for position, module in enumerate(MODULE_ROSTER)
}

# The only modules a ``--core`` run imports: those declaring a ``core=True``
# check.
CORE_MODULES: frozenset[str] = frozenset({
    "renderers",
    "web",
    "resilience",
    "audio_runtime_fanin",
    "audio_runtime_camilla",
    "audio_runtime_outputd",
})

STREAMBOX_OMITTED_DOCTOR_MODULES = frozenset({
    "voice",
    "wake",
    "integrations",
    "aec",
})

STREAMBOX_OMITTED_DOCTOR_CHECKS = frozenset({
    "check_mic_card_matches_config",
    "check_mic_capture",
    "check_tts_open",
    # A streambox has no crossover at all; the ``correction`` module still
    # owns a cert-SAN check that does apply, so only these two are omitted
    # rather than the whole module.
    "check_crossover_v2_cloud_pipeline",
    "check_crossover_v2_applied_is_graded",
})


@dataclass(frozen=True)
class RegisteredCheck:
    """One registry entry; ``module`` and ``label`` follow the roster and
    needs_cfg rules from the module docstring."""

    module: str
    func: Callable[..., CheckResult] | Callable[..., Awaitable[CheckResult]]
    needs_cfg: bool = False
    is_async: bool = False
    label: str = ""
    exclusive_group: str = ""
    core: bool = False


_REGISTRY: list[RegisteredCheck] = []


def doctor_check(
    *,
    label: str = "",
    needs_cfg: bool = False,
    is_async: bool = False,
    exclusive_group: str = "",
    core: bool = False,
) -> Callable[[F], F]:
    """Register a doctor check and return it unchanged.

    The decorator is additive — it registers metadata and returns ``fn``
    unchanged, so it stays directly importable and unit-testable.

    Display position comes from the defining module's entry in
    ``MODULE_ROSTER``; it is not a per-check argument.

    Args:
        label: explicit display/crash label. Required for ``needs_cfg``
            checks. Leave empty for bare checks so the label is derived
            from ``__name__``.
        needs_cfg: True iff the check takes the ``Config`` argument.
        is_async: True for checks implemented as async callables.
        exclusive_group: Optional serialization key for probes that are
            individually safe but can perturb one another when run at the
            same instant (for example, ALSA open probes and `/proc/asound`
            ownership reads). Empty string means no exclusive lane.
        core: True iff ``--core`` runs this check. The defining module
            must be in ``CORE_MODULES``, or ``--core`` would never import
            it, and the check may not take ``needs_cfg``: a ``--core`` run
            builds no ``Config``.
    """

    def _register(fn: F) -> F:
        if core and needs_cfg:
            raise ValueError(
                f"{fn.__name__} is core=True and needs_cfg=True; --core runs "
                "with no Config built (jasper/cli/doctor/_cli.py)."
            )
        module = fn.__module__.rsplit(".", 1)[-1]
        if module not in _ROSTER_POSITION:
            raise ValueError(
                f"{module}.{fn.__name__} registers a doctor check from a "
                "module MODULE_ROSTER does not name; add the module to the "
                "roster at the position its checks should display."
            )
        _REGISTRY.append(
            RegisteredCheck(
                module=module,
                func=fn,
                needs_cfg=needs_cfg,
                is_async=is_async,
                label=label,
                exclusive_group=exclusive_group,
                core=core,
            )
        )
        return fn

    return _register


def registered_checks(
    *,
    core_only: bool = False,
    only: str | None = None,
) -> list[RegisteredCheck]:
    """All registered checks in canonical order, importing what it needs.

    Modules follow ``MODULE_ROSTER``; within a module, source order. The
    sort is stable over the append-ordered registry, so the sequence is
    independent of the order in which the per-domain modules happened to
    be imported. ``only``, when given, restricts both the import and the
    result to that one module (composes with ``core_only``: both
    conditions apply) — the caller's ``--only`` skips the work, not just
    the display.
    """
    wanted = CORE_MODULES if core_only else frozenset(MODULE_ROSTER)
    if only is not None:
        wanted = wanted & {only}
    for name in wanted:
        importlib.import_module(f".{name}", __package__)
    entries = [
        c for c in _REGISTRY
        if c.module in wanted and (not core_only or c.core)
    ]
    return sorted(entries, key=lambda c: _ROSTER_POSITION[c.module])
