# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""DEFAULT-RESOLUTION for the fan-in coupling + USB combo (campaign P3/P4).

WHY THIS EXISTS — the audio-graph-consolidation campaign flipped the shipped
DEFAULT for two independent feature sets from "off, opt-in" to "on where the box
is eligible":

- **P4 (ring coupling):** on a solo, stereo-eligible box the default coupling
  becomes ``shm_ring`` (the end-to-end SHM-ring path) instead of loopback —
  BUT only when every #1169 arm preflight would pass (ring platform assets
  present, topology ring-eligible, and BOTH geometry axes coherent). On an
  ineligible box (roleful/composite/mono topology, or a box with no ring
  platform) the default stays loopback, byte-for-byte as before.

- **P3 (USB combo):** on a box whose USB gadget stack is enabled (the
  ``dtoverlay=dwc2,dr_mode=peripheral`` line is present in
  ``/boot/firmware/config.txt`` — the same signal the ``/sources/`` wizard uses
  for the USB-in toggle), the default arms the certified USB-in low-latency combo
  (``JASPER_FANIN_USB_DIRECT`` + ``JASPER_FANIN_HOST_CLOCK`` +
  ``JASPER_FANIN_RESAMPLER_CUSHION_DECAY``, all ``enabled``). On a box without the
  gadget, the keys stay absent and the Rust defaults (all off) rule.

This module owns the pure DECISION only. The reconciler
(:mod:`jasper.fanin.coupling_reconcile`) owns the env I/O and the daemon
transitions — the single-writer discipline (pattern 3: reconciler is the single
env writer; daemons read the resolved env). It is import-cheap (stdlib only) so
the reconciler CLI and any tests can resolve the decision without pulling in the
heavy topology/ring readers unless a real box asks.

OPERATOR-CHOICE MARKER (the revert lever). Absence-vs-present, mirroring
``JASPER_TRANSIT_CITIES``:

- ``JASPER_FANIN_COUPLING_CHOICE`` **absent** → the household made no explicit
  choice; the auto pass OWNS the coupling + USB combo and resolves them by
  eligibility.
- ``JASPER_FANIN_COUPLING_CHOICE=operator`` → the operator made an explicit
  choice (via the reconciler CLI's positional-coupling path). The auto pass is a
  COMPLETE NO-OP — it touches neither the coupling nor the USB combo keys. This is
  what makes a deliberate revert STICK across deploys: set the marker +
  ``JASPER_FANIN_CAMILLA_COUPLING=loopback`` +
  ``JASPER_OUTPUTD_CONTENT_BRIDGE=direct`` (+ unset the USB combo flags), and the
  auto pass will never override them.

FAIL-SAFE DIRECTION = loopback + combo-off. Any gate that cannot prove
eligibility resolves to the byte-identical-to-today path. An unreadable topology
or config file is NOT treated as eligible.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from jasper.audio_runtime_plan import RuntimeEnvAction
from jasper.fanin_coupling import (
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
)

logger = logging.getLogger(__name__)

# The operator-choice marker. Written into fanin.env by the reconciler's explicit
# coupling paths; absent means the auto pass owns the resolution. Single accepted
# value keeps the semantics binary (present-and-operator vs absent) — any other
# value is treated as NOT an operator choice (fail toward auto-ownership so a typo
# never silently freezes a box off the default). See module docstring.
COUPLING_CHOICE_ENV_VAR = "JASPER_FANIN_COUPLING_CHOICE"
COUPLING_CHOICE_OPERATOR = "operator"

# The USB low-latency combo the P3 default arms on a gadget box. Each is a fan-in
# Rust flag whose fail-safe is off (only the literal ``enabled`` arms it — see
# rust/jasper-fanin/src/config.rs). The reconciler is the SINGLE writer of this
# set (mirrors jasper-aec-reconcile owning the mic-device vars): present+enabled
# on a gadget box, absent otherwise so the Rust off-default rules.
USB_DIRECT_ENV_VAR = "JASPER_FANIN_USB_DIRECT"
HOST_CLOCK_ENV_VAR = "JASPER_FANIN_HOST_CLOCK"
CUSHION_DECAY_ENV_VAR = "JASPER_FANIN_RESAMPLER_CUSHION_DECAY"
USB_COMBO_ENABLED_VALUE = "enabled"
# The ordered combo keys (deterministic write order for idempotence + readable
# logs). Order is not load-bearing to the Rust reader; it is fixed only so the
# emitted actions are stable across runs.
USB_COMBO_ENV_VARS = (USB_DIRECT_ENV_VAR, HOST_CLOCK_ENV_VAR, CUSHION_DECAY_ENV_VAR)

# The USB gadget-stack presence signal: the dtoverlay that puts the Pi's USB-C
# port in peripheral (device) mode. SAME needle the /sources/ wizard
# (``jasper.web.sources_setup``) and the doctor (``jasper.cli.doctor.usbsink``)
# match — the single "this box can be a USB audio gadget" fact. Reused here rather
# than re-derived so the three surfaces never drift on what "gadget enabled" means.
BOOT_CONFIG_PATH = "/boot/firmware/config.txt"
USB_GADGET_DTOVERLAY_LINE = "dtoverlay=dwc2,dr_mode=peripheral"


def is_operator_choice(marker_raw: str | None) -> bool:
    """True iff the coupling-choice marker names an explicit operator choice.

    Present-and-``operator`` (case-insensitive, whitespace-trimmed) → the operator
    owns the coupling; the auto pass must not touch it. Absent / empty / anything
    else → NOT an operator choice (auto owns it). Fail toward auto-ownership on a
    typo so a bad marker never silently freezes a box off the default.
    """
    if marker_raw is None:
        return False
    return marker_raw.strip().lower() == COUPLING_CHOICE_OPERATOR


def usb_gadget_stack_present(boot_config_path: str = BOOT_CONFIG_PATH) -> bool:
    """True iff the USB gadget dtoverlay is present in the boot config.

    The SAME detection ``jasper.web.sources_setup._usbsink_available`` uses: the
    ``dtoverlay=dwc2,dr_mode=peripheral`` line (tolerating leading whitespace and a
    trailing comment) in ``/boot/firmware/config.txt``. Fail-soft to False on any
    read error (a box we can't prove is gadget-capable is treated as not-a-gadget,
    so the combo stays off — the fail-safe direction).
    """
    try:
        with open(boot_config_path) as f:
            content = f.read()
    except OSError as e:
        logger.debug("usb gadget dtoverlay probe failed: %s", e)
        return False
    for line in content.splitlines():
        if line.strip().startswith(USB_GADGET_DTOVERLAY_LINE):
            return True
    return False


@dataclass(frozen=True)
class AutoCouplingDecision:
    """The pure default-resolution outcome for one box.

    ``owned`` is False when an operator choice is in force — the reconciler then
    makes NO env change (a complete no-op). When ``owned`` is True, ``coupling`` is
    the resolved default (``shm_ring`` when every ring gate passed, else
    ``loopback``) and ``usb_combo_actions`` is the reconciler-owned set of
    fanin.env actions for the USB combo (set-enabled on a gadget box, unset
    otherwise). ``reason`` is a stable, log-friendly explanation of the coupling
    decision; ``gate_details`` carries the per-gate detail for the journal.
    """

    owned: bool
    coupling: str
    usb_combo_actions: tuple[RuntimeEnvAction, ...] = ()
    gadget_present: bool = False
    reason: str = ""
    gate_details: tuple[str, ...] = field(default_factory=tuple)


def usb_combo_actions(gadget_present: bool) -> tuple[RuntimeEnvAction, ...]:
    """The reconciler-owned fanin.env actions for the USB combo on this box.

    Gadget present → set all three combo keys to ``enabled``. Gadget absent →
    UNSET all three (the reconciler is the single writer; on a box that lost the
    gadget the keys must be cleared so the Rust off-default rules, mirroring
    jasper-aec-reconcile clearing stale mic-device vars). Deterministic order for
    idempotence.
    """
    if gadget_present:
        return tuple(
            RuntimeEnvAction("set", key, USB_COMBO_ENABLED_VALUE)
            for key in USB_COMBO_ENV_VARS
        )
    return tuple(RuntimeEnvAction("unset", key) for key in USB_COMBO_ENV_VARS)


# A ring gate is a zero-arg callable returning (ok, detail) — the same shape the
# reconciler's ``ring_assets_ready`` / ``ring_topology_ready`` /
# ``ring_geometry_ready`` / ``ring_slot_geometry_ready`` preflights already return.
RingGate = Callable[[], "tuple[bool, str]"]


def resolve_auto_decision(
    *,
    marker_raw: str | None,
    gadget_present: bool,
    ring_gates: "tuple[tuple[str, RingGate], ...]",
) -> AutoCouplingDecision:
    """Resolve the default coupling + USB combo for one box (pure).

    - If the marker names an operator choice → ``owned=False`` and NO actions (the
      reconciler makes zero env changes; the operator's revert sticks).
    - Else the auto pass owns the box:
        * ``coupling`` = ``shm_ring`` iff EVERY ring gate returns ``ok`` (assets
          present, topology ring-eligible, geometry coherent on both axes); the
          first failing gate short-circuits to ``loopback`` with its detail as the
          reason (so an ineligible box — jts3 roleful, jts5 composite — resolves
          loopback with a crisp explanation).
        * ``usb_combo_actions`` = set-enabled on a gadget box, unset otherwise.

    ``ring_gates`` is an ordered ``(name, gate)`` tuple; each gate is the same
    ``() -> (ok, detail)`` callable the reconciler's arm preflights use. Injected
    (not imported) so this stays pure/testable and the caller controls which real
    gates run.
    """
    if is_operator_choice(marker_raw):
        return AutoCouplingDecision(
            owned=False,
            coupling=COUPLING_LOOPBACK,
            usb_combo_actions=(),
            gadget_present=gadget_present,
            reason="operator choice in force — auto pass is a no-op",
        )

    details: list[str] = []
    coupling = COUPLING_SHM_RING
    reason = "all ring gates passed — default resolves shm_ring"
    for name, gate in ring_gates:
        try:
            ok, detail = gate()
        except (OSError, ValueError, RuntimeError, ImportError) as e:
            # A gate that cannot even evaluate is NOT proven eligible — fail safe to
            # loopback (never arm a ring on an indeterminate gate).
            ok, detail = False, f"{name} gate raised: {e}"
        details.append(f"{name}: {detail}")
        if not ok:
            coupling = COUPLING_LOOPBACK
            reason = f"not ring-eligible ({name}): {detail}"
            break

    return AutoCouplingDecision(
        owned=True,
        coupling=coupling,
        usb_combo_actions=usb_combo_actions(gadget_present),
        gadget_present=gadget_present,
        reason=reason,
        gate_details=tuple(details),
    )


def read_marker(fanin_text: str) -> str | None:
    """Read the operator-choice marker from fanin.env text (or None if absent)."""
    from jasper.env_file import read_value

    return read_value(fanin_text, COUPLING_CHOICE_ENV_VAR)


def resolved_choice_label(marker_raw: str | None) -> str:
    """``"operator"`` when the marker is an explicit operator choice, else
    ``"auto"``. Used by ``/state.audio_graph.coupling`` and the doctor to show
    WHOSE choice the current coupling is."""
    return COUPLING_CHOICE_OPERATOR if is_operator_choice(marker_raw) else "auto"


def default_ring_gates() -> "tuple[tuple[str, RingGate], ...]":
    """The real #1169 ring arm preflights, in the order ``_arm_ring`` runs them.

    Lazily binds the reconciler's own gate helpers so the auto pass gates on
    EXACTLY the same predicates a manual ``shm_ring`` arm would — no second,
    drift-prone eligibility copy. ``ring_geometry_ready`` /
    ``ring_slot_geometry_ready`` need the outputd/fanin env text; the reconciler
    passes bound closures. This factory is only the ASSET + TOPOLOGY pair that need
    no env text; the reconciler appends the geometry gates with the env text bound.
    """
    from jasper.fanin.coupling_reconcile import (
        ring_assets_ready,
        ring_topology_ready,
    )

    return (
        ("ring_assets", ring_assets_ready),
        ("ring_topology", ring_topology_ready),
    )


def read_boot_config_gadget_present() -> bool:
    """Live gadget-presence read (thin wrapper for the reconciler + tests)."""
    return usb_gadget_stack_present(os.environ.get("JASPER_BOOT_CONFIG_PATH", BOOT_CONFIG_PATH))
