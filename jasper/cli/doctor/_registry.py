# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ordered registry for jasper-doctor checks.

A check is registered by the module of the subsystem it observes, and
``MODULE_ROSTER`` below is the single place display order and section
label are decided (ADR-0233 rule 4). Checks appear module by module in
roster order, and within a module in source order; registering from a
module the roster does not name is an import-time error, so a new
module has to be given a position and a label before it can run.

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

The group label is the per-domain dimension. It does not affect order;
it names the section a module's checks belong to, and
``__init__._STREAMBOX_OMITTED_DOCTOR_GROUPS`` skips whole groups a
streambox does not install. Several modules share one label on purpose
(the ``audio_runtime_*`` modules and ``boot_config`` are all ``audio``;
``drift`` is ``install``; ``secret_compartments`` is ``privsep``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from ._shared import CheckResult

MODULE_ROSTER: tuple[tuple[str, str], ...] = (
    ("env", "env"),
    ("voice", "voice"),
    ("audio", "audio"),
    ("wake", "wake"),
    ("renderers", "renderers"),
    ("integrations", "integrations"),
    ("boot_config", "audio"),
    ("privsep", "privsep"),
    ("secret_compartments", "privsep"),
    ("web", "web"),
    ("research", "research"),
    ("correction", "correction"),
    ("memory", "memory"),
    ("drift", "install"),
    ("resilience", "resilience"),
    ("aec", "aec"),
    ("audio_runtime_fanin", "audio"),
    ("audio_runtime_camilla", "audio"),
    ("audio_runtime_ring", "audio"),
    ("audio_runtime_outputd", "audio"),
    ("usbsink", "usbsink"),
    ("network", "network"),
    ("peering", "peering"),
    ("grouping", "grouping"),
)

_GROUP_BY_MODULE: dict[str, str] = dict(MODULE_ROSTER)
_ROSTER_POSITION: dict[str, int] = {
    module: position for position, (module, _) in enumerate(MODULE_ROSTER)
}


@dataclass(frozen=True)
class RegisteredCheck:
    """One registry entry.

    ``module`` is the doctor module basename the check registered from;
    it decides both display position and ``group``. ``func`` is the raw
    check function. ``needs_cfg`` marks the checks the harness calls
    with the ``Config`` argument. ``label`` is required for those; for
    bare checks it is left empty and the harness derives the
    displayed/crash label from ``func.__name__``.
    """

    module: str
    func: Callable[..., CheckResult] | Callable[..., Awaitable[CheckResult]]
    needs_cfg: bool = False
    is_async: bool = False
    label: str = ""
    exclusive_group: str = ""

    @property
    def group(self) -> str:
        return _GROUP_BY_MODULE[self.module]


_REGISTRY: list[RegisteredCheck] = []


def doctor_check(
    *,
    label: str = "",
    needs_cfg: bool = False,
    is_async: bool = False,
    exclusive_group: str = "",
) -> Callable[[Callable], Callable]:
    """Register a doctor check and return it unchanged.

    The decorator is *additive* — it records metadata in the registry and
    returns the original function object untouched, so the function's
    identity, signature, and body are preserved (it stays directly
    importable and unit-testable).

    Display position and group come from the defining module's entry in
    ``MODULE_ROSTER``; neither is a per-check argument.

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
                "roster at the position its checks should display, with the "
                "group label they belong under."
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

    return _register


def registered_checks() -> list[RegisteredCheck]:
    """All registered checks in canonical order.

    Modules follow ``MODULE_ROSTER``; within a module, source order. The
    sort is stable over the append-ordered registry, so the sequence is
    independent of the order in which the per-domain modules happened to
    be imported.
    """
    return sorted(_REGISTRY, key=lambda c: _ROSTER_POSITION[c.module])
