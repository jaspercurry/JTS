# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""DEFAULT-RESOLUTION for the fan-in coupling + USB combo (campaign P3/P4).

WHY THIS EXISTS — the audio-graph-consolidation campaign flipped the shipped
DEFAULT for two independent feature sets from "off, opt-in" to "on where the box
is eligible":

- **P4 (ring coupling):** on a validated full-profile, solo, stereo-eligible box the default coupling
  becomes ``shm_ring`` (the end-to-end SHM-ring path) instead of loopback —
  BUT only when every #1169 arm preflight would pass (ring platform assets
  present, topology ring-eligible, and BOTH geometry axes coherent). On an
  ineligible box (streambox profile, roleful/composite/mono topology, or a box
  with no ring platform) the default stays loopback, byte-for-byte as before.
  Streamboxes still run this owner for the independent USB DIRECT decision;
  installed ring assets alone are not hardware validation.

- **P3 (USB combo):** the default arms the certified USB-in low-latency path
  ONLY on a box that BOTH (a) has the resolved USB gadget capability available
  and (b) has USB Audio Input turned ON by the
  household (canonical source intent is enabled), local sources are allowed
  for this speaker's current role, AND the coordinator-derived
  ``jasper-usbsink.service`` enablement confirms lifecycle readiness. The
  boot overlay alone is NOT enough: the same data port may belong to a USB
  output DAC on a Zero-class board. All signals present
  → arm the fan-in half: ``JASPER_FANIN_USB_DIRECT`` + ``JASPER_FANIN_HOST_CLOCK``
  + ``JASPER_FANIN_RESAMPLER_CUSHION_DECAY`` = ``enabled`` in fanin.env (fan-in owns
  the gadget capture). Off a combo box the three fan-in keys are written to their
  EXPLICIT off value (``disabled``), NOT unset — an unset key lets a stale
  ``enabled`` in ``/etc/jasper/jasper.env`` (loaded before the reconciler-owned
  files) win. There is no separate USB bridge process: armed means USB flows
  through fan-in's DIRECT lane, while disarmed means USB audio is unavailable.

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
  coupling no-op but still converges USB combo keys from canonical source intent
  and runtime fallback. This makes a deliberate transport revert STICK across
  deploys without allowing the marker to override household USB Off: set it with
  ``JASPER_FANIN_CAMILLA_COUPLING=loopback`` +
  ``JASPER_OUTPUTD_CONTENT_BRIDGE=direct``; the auto pass will not override that
  coupling.

FAIL-SAFE DIRECTION = loopback + combo-off. Any gate that cannot prove
eligibility resolves to the byte-identical-to-today path. An unreadable topology
or config file is NOT treated as eligible — the unattended default fails CLOSED
where a human-initiated arm (:mod:`jasper.fanin.coupling_reconcile`'s
``ring_topology_ready``) deliberately fails open (a human accepts the risk of an
indeterminate read; a boot/deploy pass must not arm a ring on a box it cannot
prove is eligible, or it would arm→rollback churn every boot).
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field

from jasper.audio_runtime_plan import RuntimeEnvAction
from jasper.fanin_coupling import (
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
)
from jasper.output_hardware import current_usb_data_role

logger = logging.getLogger(__name__)

# The operator-choice marker. Written into fanin.env by the reconciler's explicit
# coupling paths; absent means the auto pass owns the resolution. Single accepted
# value keeps the semantics binary (present-and-operator vs absent) — any other
# value is treated as NOT an operator choice (fail toward auto-ownership so a typo
# never silently freezes a box off the default). See module docstring.
COUPLING_CHOICE_ENV_VAR = "JASPER_FANIN_COUPLING_CHOICE"
COUPLING_CHOICE_OPERATOR = "operator"

# The USB low-latency combo the P3 default arms on a gadget box that ALSO has USB
# audio turned on. Each is a fan-in Rust flag whose fail-safe is off (only the
# literal ``enabled`` arms it — see rust/jasper-fanin/src/config.rs). The
# reconciler is the SINGLE writer of this set (mirrors jasper-aec-reconcile owning
# the mic-device vars). On a combo box each is written ``enabled``; off a combo box
# each is written the EXPLICIT off literal ``disabled`` (NOT unset — an unset key
# lets a stale ``enabled`` in /etc/jasper/jasper.env, loaded BEFORE fanin.env, win;
# ``disabled`` in the later-loaded fanin.env overrides it and the Rust reader treats
# any non-``enabled`` value as off).
USB_DIRECT_ENV_VAR = "JASPER_FANIN_USB_DIRECT"
HOST_CLOCK_ENV_VAR = "JASPER_FANIN_HOST_CLOCK"
CUSHION_DECAY_ENV_VAR = "JASPER_FANIN_RESAMPLER_CUSHION_DECAY"
USB_COMBO_ENABLED_VALUE = "enabled"
USB_COMBO_DISABLED_VALUE = "disabled"
# The ordered combo keys (deterministic write order for idempotence + readable
# logs). Order is not load-bearing to the Rust reader; it is fixed only so the
# emitted actions are stable across runs.
USB_COMBO_ENV_VARS = (USB_DIRECT_ENV_VAR, HOST_CLOCK_ENV_VAR, CUSHION_DECAY_ENV_VAR)

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


@dataclass(frozen=True)
class AutoCouplingDecision:
    """The pure default-resolution outcome for one box.

    ``owned`` describes the coupling only. False means an operator choice is in
    force and the exact current coupling is preserved; USB combo actions still
    converge from canonical authorization. When ``owned`` is True, ``coupling``
    is the resolved default (``shm_ring`` when every ring gate passed, else
    ``loopback``). ``combo_armed`` records whether the USB combo is on;
    ``usb_combo_actions`` is the reconciler-owned set of ``fanin.env`` actions for
    the three fan-in combo keys (``enabled`` when armed, explicit ``disabled``
    otherwise). ``reason`` is a stable, log-friendly explanation of the coupling
    decision; ``gate_details`` carries the per-gate detail for the journal.
    """

    owned: bool
    coupling: str
    usb_combo_actions: tuple[RuntimeEnvAction, ...] = ()
    combo_armed: bool = False
    gadget_present: bool = False
    usb_intent_enabled: bool = False
    # True when a runtime-fallback marker forced the combo OFF despite the box
    # being combo-eligible (gadget present + USB audio on). See ``fallback_active``
    # in :func:`resolve_auto_decision`.
    fallback_active: bool = False
    reason: str = ""
    gate_details: tuple[str, ...] = field(default_factory=tuple)


def combo_is_armed(*, gadget_present: bool, usb_intent_enabled: bool) -> bool:
    """The P3 combo arms iff BOTH the gadget stack is available AND USB audio is
    turned on by the household.

    The shared resolver's strict gadget availability is a NECESSARY but not
    SUFFICIENT signal — a peripheral overlay or currently active management
    transport alone does not authorize audio on a shared-port Zero. The combo
    also needs the household's persistent USB-audio intent from
    ``/var/lib/jasper/source_intent.env``. The source coordinator resolves that
    preference before this function is called; ``jasper-usbsink.service``
    enablement is only the derived gadget-composition mirror. Gating on the
    controller state alone would arm a split-brain combo.
    """
    return gadget_present and usb_intent_enabled


def usb_combo_actions(*, armed: bool) -> tuple[RuntimeEnvAction, ...]:
    """The reconciler-owned ``fanin.env`` actions for the three fan-in combo keys.

    Armed → set all three to ``enabled``. NOT armed → set all three to the EXPLICIT
    ``disabled`` literal (NOT unset — the reconciler is the single writer, and an
    unset key would let a stale ``enabled`` in the earlier-loaded jasper.env win;
    ``disabled`` in the later-loaded fanin.env overrides it and the Rust reader
    treats any non-``enabled`` value as off). Deterministic order for idempotence.
    """
    value = USB_COMBO_ENABLED_VALUE if armed else USB_COMBO_DISABLED_VALUE
    return tuple(RuntimeEnvAction("set", key, value) for key in USB_COMBO_ENV_VARS)


# A ring gate is a zero-arg callable returning (ok, detail) — the same shape the
# reconciler's ``ring_assets_ready`` / ``ring_topology_ready`` /
# ``ring_geometry_ready`` / ``ring_slot_geometry_ready`` preflights already return.
RingGate = Callable[[], "tuple[bool, str]"]


def resolve_auto_decision(
    *,
    marker_raw: str | None,
    gadget_present: bool,
    usb_intent_enabled: bool,
    ring_gates: "tuple[tuple[str, RingGate], ...]",
    fallback_active: bool = False,
    current_coupling: str = COUPLING_LOOPBACK,
) -> AutoCouplingDecision:
    """Resolve the default coupling + USB combo for one box (pure).

    - If the marker names an operator choice → ``owned=False`` and the coupling
      remains ``current_coupling``. USB combo actions still converge from
      canonical source intent because the operator marker freezes transport
      topology, not permission to capture a household-Off source.
    - Else the auto pass owns the box:
        * ``coupling`` = ``shm_ring`` iff EVERY ring gate returns ``ok`` (assets
          present, topology ring-eligible, geometry coherent on both axes, route
          supports the ring); the first failing gate short-circuits to ``loopback``
          with its detail as the reason (so an ineligible box — jts3 roleful, jts5
          composite, a grouped box — resolves loopback with a crisp explanation).
        * combo = ARMED iff ``gadget_present AND usb_intent_enabled`` (see
          :func:`combo_is_armed`) AND NOT ``fallback_active``;
          ``usb_combo_actions`` carries explicit on/off writes either way (the
          single-writer discipline writes an explicit off, never an unset).

    ``fallback_active`` is the runtime-fallback flap guard (defect 2026-07-10): a
    combo-eligible box whose live capture broke at runtime carries the fallback
    marker (:mod:`jasper.fanin.combo_health`), which forces the combo OFF here even
    though ``gadget_present AND usb_intent_enabled``. Since the aloop solo path was
    deleted there is NO fallback capture to promote — the box is simply left with
    USB audio UNAVAILABLE (the direct lane disarmed and UAC2 withdrawn), which the
    doctor + ``/state`` surface LOUDLY, and it re-attempts on the next
    ``--auto`` clear-event (boot/deploy/toggle) that drops the marker. It does NOT
    touch the ring coupling decision (a broken USB capture is not a reason to
    disarm the ring).

    ``ring_gates`` is an ordered ``(name, gate)`` tuple; each gate is the same
    ``() -> (ok, detail)`` callable the reconciler's arm preflights use. Injected
    (not imported) so this stays pure/testable and the caller controls which real
    gates run.
    """
    eligible = combo_is_armed(
        gadget_present=gadget_present, usb_intent_enabled=usb_intent_enabled
    )
    armed = eligible and not fallback_active
    # A fallback is only meaningful when the box WOULD otherwise arm — reporting it
    # on an ineligible box would be noise.
    fallback = fallback_active and eligible
    if is_operator_choice(marker_raw):
        return AutoCouplingDecision(
            owned=False,
            coupling=current_coupling,
            usb_combo_actions=usb_combo_actions(armed=armed),
            combo_armed=armed,
            gadget_present=gadget_present,
            usb_intent_enabled=usb_intent_enabled,
            fallback_active=fallback,
            reason=(
                "operator coupling choice preserved; USB combo resolved from "
                "canonical source intent"
            ),
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
        usb_combo_actions=usb_combo_actions(armed=armed),
        combo_armed=armed,
        gadget_present=gadget_present,
        usb_intent_enabled=usb_intent_enabled,
        fallback_active=fallback,
        reason=reason,
        gate_details=tuple(details),
    )


def read_marker(fanin_text: str) -> str | None:
    """Read the operator-choice marker from fanin.env text (or None if absent)."""
    from jasper.env_file import read_value

    return read_value(fanin_text, COUPLING_CHOICE_ENV_VAR)


def resolved_choice_label(marker_raw: str | None) -> str:
    """``"operator"`` when the marker is an explicit operator choice, else
    ``"auto"``. Used by ``/state.audio_graph.coupling.choice`` to show WHOSE choice
    the current coupling is (an operator revert vs the auto-resolved default)."""
    return COUPLING_CHOICE_OPERATOR if is_operator_choice(marker_raw) else "auto"


def ring_install_profile_ready() -> tuple[bool, str]:
    """Allow automatic SHM-ring coupling only on validated full profiles.

    Streamboxes share the fan-in graph and therefore install the ring assets,
    but the P4 ring path has not been validated on Zero-class hardware. This
    gate is deliberately independent from USB combo eligibility so a
    streambox can arm USB DIRECT while retaining loopback coupling.
    """

    from jasper.install_profile import (
        is_streambox_install_profile,
        read_install_profile,
    )

    profile = read_install_profile()
    if is_streambox_install_profile(profile):
        return False, "streambox profile is not validated for automatic shm_ring"
    return True, f"install profile {profile} permits automatic shm_ring"


def default_ring_gates() -> "tuple[tuple[str, RingGate], ...]":
    """The #1169 ring arm preflights the auto pass reuses, in ``_arm_ring`` order.

    Lazily binds the reconciler's own gate helpers so the auto pass gates on the
    SAME predicates a manual ``shm_ring`` arm would — no second, drift-prone
    eligibility copy — with ONE deliberate difference: the topology gate is the
    STRICT (fail-closed-on-unreadable) variant ``ring_topology_ready_strict`` rather
    than the human-arm ``ring_topology_ready`` (an unattended default must not
    arm→rollback-churn on a transiently unreadable topology — defect-F4). This
    factory owns the PROFILE + ASSET + TOPOLOGY gates that need no env text; the
    reconciler appends the ROUTE-support gate and the two geometry gates (which need the
    outputd/fanin env text) as bound closures.
    """
    from jasper.fanin.coupling_reconcile import (
        ring_assets_ready,
        ring_topology_ready_strict,
    )

    return (
        ("install_profile", ring_install_profile_ready),
        ("ring_assets", ring_assets_ready),
        ("ring_topology", ring_topology_ready_strict),
    )


def read_usb_gadget_available() -> bool:
    """Read the reconciler-owned capability used by every USB consumer."""

    try:
        return current_usb_data_role().gadget_available
    except (OSError, RuntimeError, ValueError) as exc:
        logger.debug("USB data-role read failed: %s", exc)
        return False


def _usbsink_lifecycle_ready() -> bool:
    """Return the coordinator-derived USB lifecycle readiness mirror."""

    try:
        process = subprocess.run(
            ["systemctl", "is-enabled", "--quiet", "jasper-usbsink.service"],
            check=False,
            timeout=5.0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return process.returncode == 0


def usbsink_effectively_enabled() -> bool:
    """True iff USB Audio is authorized and its lifecycle mirror is ready.

    Canonical source intent remains the preference SSOT and is checked first,
    followed by the same local-source role gate used by the source units.
    Finally, the derived ``jasper-usbsink.service`` enablement must confirm the
    coordinator completed the lifecycle transition. A desired-on USB source on
    a bonded follower remains persisted On but its direct fan-in lane stays
    disarmed until unparked. Desired-On with stale/failed derived enablement
    also stays disarmed rather than opening capture for an unadvertised UAC2
    function. A malformed or unreadable intent raises visibly.
    """
    from jasper.local_sources.guard import local_sources_allowed
    from jasper.music_sources import Source
    from jasper.source_intent import source_intent_enabled

    if not source_intent_enabled(Source.USBSINK):
        return False
    if not local_sources_allowed()[0]:
        return False
    return _usbsink_lifecycle_ready()
