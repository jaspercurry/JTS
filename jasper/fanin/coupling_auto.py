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
  **No box class is refused for being its class.** #2285 deleted
  ``ring_install_profile_ready``, which held streamboxes on loopback because
  the ring had not been VALIDATED on Zero-class hardware — a class-shaped
  refusal standing in for the per-box proof the gates below already make. A
  streambox is now judged on its own ring evidence like any other box, and a
  refusal names the gate rather than the profile. The USB DIRECT decision
  remains independent of the ring one, which is what that gate's separation
  was also for.

- **P3 (USB combo):** the default arms the certified USB-in low-latency path
  ONLY on a box that BOTH (a) has the resolved USB gadget capability available
  and (b) has USB Audio Input turned ON by the
  household (canonical source intent is enabled), local sources are allowed
  for this speaker's current role, AND the coordinator-derived
  ``jasper-usbsink.service`` enablement confirms lifecycle readiness. The
  boot overlay alone is NOT enough: the same data port may belong to a USB
  output DAC on a Zero-class board. All signals present
  → arm the fan-in half: ``JASPER_FANIN_USB_DIRECT`` + ``JASPER_FANIN_HOST_CLOCK``
  and the household's fixed cushion-decay preset in fanin.env (fan-in owns the
  gadget capture). Off a combo box the feature keys are written to their
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
  and current hardware eligibility. This makes a deliberate transport revert STICK across
  deploys without allowing the marker to override household USB Off: set it with
  ``JASPER_FANIN_CAMILLA_COUPLING=loopback`` +
  ``JASPER_OUTPUTD_CONTENT_BRIDGE=direct``; the auto pass will not override that
  coupling.

FAIL-SAFE DIRECTION = loopback + combo-off. Any gate that cannot prove
eligibility resolves to the byte-identical-to-today path. An unreadable topology
or config file is NOT treated as eligible: a boot/deploy pass must not arm a ring
on a box it cannot prove is eligible, or it would arm→rollback churn every boot.

The human-initiated arm (:mod:`jasper.fanin.coupling_reconcile`'s
``ring_topology_ready``) used to fail OPEN there on the reasoning that a human
accepts the risk of an indeterminate read. **It no longer does** — its stated
backstop (outputd's own guard) was shown to fail open on the same error, so both
paths are now fail-CLOSED and this module's direction is simply the shared one.

The unattended pass additionally holds a ROLEFUL box to its own gate
(``ring_roleful_unattended_ready``), ahead of every eligibility predicate. It
refuses by DEFAULT and admits only two proven graph shapes — a
hardware-fingerprint-matched applied baseline, or the all-muted staged anchor,
the same two legal roleful boot graphs the operator ladder's step 1 accepts.
The narrowing and its safety argument live in that gate's docstring and in §4.7
of the convergence design (owner ruling, §12 decision 1); they are not restated
here.
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
from jasper.fanin.latency_mode import DEFAULT_MODE, preset_for
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
# audio turned on. Its feature flags fail safe off (only the literal ``enabled``
# arms them — see rust/jasper-fanin/src/config.rs); the preset also owns the
# explicit decay floor. The
# reconciler is the SINGLE writer of this set (mirrors jasper-aec-reconcile owning
# the mic-device vars). Off a combo box each feature is written the EXPLICIT off
# literal ``disabled`` (NOT unset — an unset key
# lets a stale ``enabled`` in /etc/jasper/jasper.env, loaded BEFORE fanin.env, win;
# ``disabled`` in the later-loaded fanin.env overrides it and the Rust reader treats
# any non-``enabled`` value as off).
USB_DIRECT_ENV_VAR = "JASPER_FANIN_USB_DIRECT"
HOST_CLOCK_ENV_VAR = "JASPER_FANIN_HOST_CLOCK"
CUSHION_DECAY_ENV_VAR = "JASPER_FANIN_RESAMPLER_CUSHION_DECAY"
CUSHION_DECAY_FLOOR_ENV_VAR = "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES"
USB_COMBO_ENABLED_VALUE = "enabled"
USB_COMBO_DISABLED_VALUE = "disabled"
# The ordered combo keys (deterministic write order for idempotence + readable
# logs). Order is not load-bearing to the Rust reader; it is fixed only so the
# emitted actions are stable across runs.
USB_COMBO_ENV_VARS = (
    USB_DIRECT_ENV_VAR,
    HOST_CLOCK_ENV_VAR,
    CUSHION_DECAY_ENV_VAR,
    CUSHION_DECAY_FLOOR_ENV_VAR,
)


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
    the fan-in feature keys and selected latency floor. ``reason`` is a stable,
    log-friendly explanation of the coupling
    decision; ``gate_details`` carries the per-gate detail for the journal.
    """

    owned: bool
    coupling: str
    usb_combo_actions: tuple[RuntimeEnvAction, ...] = ()
    combo_armed: bool = False
    gadget_present: bool = False
    usb_intent_enabled: bool = False
    usb_latency_mode: str = DEFAULT_MODE
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


def usb_combo_actions(
    *, armed: bool, latency_mode: str = DEFAULT_MODE,
) -> tuple[RuntimeEnvAction, ...]:
    """The reconciler-owned ``fanin.env`` actions for the USB fan-in keys.

    Direct capture and host clock follow ``armed``. Cushion decay additionally
    follows the preset; High keeps direct capture but pins the stable ceiling.
    Feature keys use explicit ``disabled`` rather than unset, so a stale earlier
    environment value cannot win. Deterministic order keeps writes idempotent.
    """
    preset = preset_for(latency_mode)
    feature_value = USB_COMBO_ENABLED_VALUE if armed else USB_COMBO_DISABLED_VALUE
    decay_value = (
        USB_COMBO_ENABLED_VALUE
        if armed and preset.decay_enabled
        else USB_COMBO_DISABLED_VALUE
    )
    return (
        RuntimeEnvAction("set", USB_DIRECT_ENV_VAR, feature_value),
        RuntimeEnvAction("set", HOST_CLOCK_ENV_VAR, feature_value),
        RuntimeEnvAction("set", CUSHION_DECAY_ENV_VAR, decay_value),
        RuntimeEnvAction(
            "set", CUSHION_DECAY_FLOOR_ENV_VAR, str(preset.floor_frames)
        ),
    )


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
    current_coupling: str = COUPLING_LOOPBACK,
    usb_latency_mode: str = DEFAULT_MODE,
) -> AutoCouplingDecision:
    """Resolve the default coupling + USB combo for one box (pure).

    - If the marker names an operator choice → ``owned=False`` and the coupling
      remains ``current_coupling``. USB combo actions still converge from
      canonical source intent because the operator marker freezes transport
      topology, not permission to capture a household-Off source.
    - Else the auto pass owns the box:
        * ``coupling`` = ``shm_ring`` iff EVERY ring gate returns ``ok``
          (roleful boxes only via ``ring_roleful_unattended_ready``'s two proven
          graph shapes, assets present, topology ring-eligible, geometry
          coherent on both axes, route supports the ring); the first failing
          gate short-circuits to ``loopback`` with its detail as the reason, so
          a box this pass must not arm — a roleful box carrying NEITHER a
          hardware-matched applied baseline nor an all-muted anchor, a grouped
          box, a box whose ring assets or geometry do not check out — resolves
          loopback with a crisp explanation. Note what is NOT in that list
          since #2285: an install profile. No gate here refuses a box for its
          class.
        * combo = ARMED iff ``gadget_present AND usb_intent_enabled`` (see
          :func:`combo_is_armed`);
          ``usb_combo_actions`` carries explicit on/off writes either way (the
          single-writer discipline writes an explicit off, never an unset).

    ``ring_gates`` is an ordered ``(name, gate)`` tuple; each gate is the same
    ``() -> (ok, detail)`` callable the reconciler's arm preflights use. Injected
    (not imported) so this stays pure/testable and the caller controls which real
    gates run.
    """
    armed = combo_is_armed(
        gadget_present=gadget_present, usb_intent_enabled=usb_intent_enabled
    )
    if is_operator_choice(marker_raw):
        return AutoCouplingDecision(
            owned=False,
            coupling=current_coupling,
            usb_combo_actions=usb_combo_actions(
                armed=armed, latency_mode=usb_latency_mode
            ),
            combo_armed=armed,
            gadget_present=gadget_present,
            usb_intent_enabled=usb_intent_enabled,
            usb_latency_mode=usb_latency_mode,
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
        usb_combo_actions=usb_combo_actions(
            armed=armed, latency_mode=usb_latency_mode
        ),
        combo_armed=armed,
        gadget_present=gadget_present,
        usb_intent_enabled=usb_intent_enabled,
        usb_latency_mode=usb_latency_mode,
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


# NO INSTALL-PROFILE GATE. A streambox arms the ring on exactly the same
# evidence every other box does — #2285 deleted ``ring_install_profile_ready``,
# which refused automatic shm_ring on Zero-class hardware purely because the
# ring had not been VALIDATED there. That is a class-shaped refusal standing in
# for a per-box proof the remaining gates already make: topology eligibility,
# asset presence, ioplug capability, wire agreement and both geometry gates all
# run per box, and ``_arm_ring`` rolls the WHOLE box back to loopback + direct
# on any failure, so a streambox the ring genuinely does not suit lands exactly
# where the gate used to hold it — with a named reason instead of a silent
# class exclusion.


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
