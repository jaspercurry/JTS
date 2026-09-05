# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered registry for jasper-doctor checks.

A check is registered by the module of the subsystem it observes, and
``MODULE_ROSTER`` below is the display order (ADR-0233 rule 4): checks
appear module by module in roster order, and within a module in source
order. The module name is also the key
``__init__._STREAMBOX_OMITTED_DOCTOR_MODULES`` filters on. Registering
from a module the roster does not name is an import-time error, so a new
module has to be given a position before it can run.

- **The bare-vs-tuple distinction is preserved.** A check with
  ``needs_cfg=False`` is emitted as a bare function, so the harness
  derives its displayed/crash label from ``fn.__name__``
  (``check_env_file`` → ``"env file"``). A check with
  ``needs_cfg=True`` is emitted as ``(label, lambda: fn(cfg))`` — the
  explicit label plus the ``cfg`` closure.

- **Async and hardware-sensitive checks carry explicit metadata.** A
  check flagged ``is_async=True`` is awaited directly by the harness.
  A check with ``exclusive_group=`` may still run while unrelated checks
  are in flight, but only one check in that group runs at a time. This
  keeps ALSA/proc evidence probes from observing one another's temporary
  opens while still allowing the rest of the subprocess-heavy doctor to
  run concurrently.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, overload

from ._shared import CheckResult

MODULE_ROSTER: tuple[str, ...] = (
    "env",
    "voice",
    "audio",
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


@dataclass(frozen=True)
class RegisteredCheck:
    """One registry entry.

    ``module`` is the doctor module basename the check registered from;
    it decides display position. ``func`` is the raw check function.
    ``needs_cfg`` marks the checks the harness calls with the ``Config``
    argument. ``label`` is required for those; for bare checks it is
    left empty and the harness derives the displayed/crash label from
    ``func.__name__``.
    """

    module: str
    func: Callable[..., CheckResult] | Callable[..., Awaitable[CheckResult]]
    needs_cfg: bool = False
    is_async: bool = False
    label: str = ""
    exclusive_group: str = ""


_REGISTRY: list[RegisteredCheck] = []


@overload
def doctor_check(func: Callable, /) -> Callable: ...


@overload
def doctor_check(
    *,
    label: str = ...,
    needs_cfg: bool = ...,
    is_async: bool = ...,
    exclusive_group: str = ...,
) -> Callable[[Callable], Callable]: ...


def doctor_check(
    func: Callable | None = None,
    /,
    *,
    label: str = "",
    needs_cfg: bool = False,
    is_async: bool = False,
    exclusive_group: str = "",
) -> Callable:
    """Register a doctor check and return it unchanged.

    Usable bare (``@doctor_check``) or called (``@doctor_check(...)``).

    The decorator is *additive* — it records metadata in the registry and
    returns the original function object untouched, so the function's
    identity, signature, and body are preserved (it stays directly
    importable and unit-testable).

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
    """

    def _register(fn: Callable) -> Callable:
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
            )
        )
        return fn

    return _register if func is None else _register(func)


def registered_checks() -> list[RegisteredCheck]:
    """All registered checks in canonical order.

    Modules follow ``MODULE_ROSTER``; within a module, source order. The
    sort is stable over the append-ordered registry, so the sequence is
    independent of the order in which the per-domain modules happened to
    be imported.
    """
    return sorted(_REGISTRY, key=lambda c: _ROSTER_POSITION[c.module])
