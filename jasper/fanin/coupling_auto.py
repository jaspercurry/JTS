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

- **P3 (USB combo):** the default arms the certified USB-in low-latency combo
  ONLY on a box that BOTH (a) has the USB gadget stack available
  (``dtoverlay=dwc2,dr_mode=peripheral`` in ``/boot/firmware/config.txt`` — the
  precondition for the gadget) AND (b) has USB Audio Input turned ON by the
  household (``jasper-usbsink.service`` is enabled — the SAME persistent-intent
  signal the ``/sources/`` wizard toggles). The dtoverlay alone is NOT enough: it
  is added on every install to carry the always-on USB management network, so it
  is present fleet-wide (jts3/jts5 included) even where USB audio is never used —
  gating on it alone would arm the combo on the whole fleet. Both signals present
  → arm the fan-in half: ``JASPER_FANIN_USB_DIRECT`` + ``JASPER_FANIN_HOST_CLOCK``
  + ``JASPER_FANIN_RESAMPLER_CUSHION_DECAY`` = ``enabled`` in fanin.env (fan-in owns
  the gadget capture). Off a combo box the three fan-in keys are written to their
  EXPLICIT off value (``disabled``), NOT unset — an unset key lets a stale
  ``enabled`` in ``/etc/jasper/jasper.env`` (loaded before the reconciler-owned
  files) win. The usbsink-bridge half (``JASPER_USBSINK_AUDIO_STANDBY``) is
  written unconditionally to ``1``: the jasper-usbsink daemon is standby-only now
  (the aloop capture path was deleted), so it never holds ``hw:UAC2Gadget``
  regardless — armed means USB flows through fan-in's DIRECT lane, disarmed means
  USB audio is simply UNAVAILABLE (there is no solo fallback to promote).

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
or config file is NOT treated as eligible — the unattended default fails CLOSED
where a human-initiated arm (:mod:`jasper.fanin.coupling_reconcile`'s
``ring_topology_ready``) deliberately fails open (a human accepts the risk of an
indeterminate read; a boot/deploy pass must not arm a ring on a box it cannot
prove is eligible, or it would arm→rollback churn every boot).
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

# The usbsink-bridge STANDBY half of the combo. Since the single-USB-pipeline
# convergence (2026-07-10) the jasper-usbsink daemon is standby-ONLY — its aloop
# capture/delivery path was deleted, so it opens no PCM and never holds
# hw:UAC2Gadget regardless of this key's value. The key is now written
# unconditionally to ``1`` (single-writer discipline + migration + downstream
# narration): whether or not the combo is armed, the bridge is always in standby
# and USB audio flows ONLY through fan-in's DIRECT capture. It lives in
# usbsink.env (loaded by jasper-usbsink.service AFTER jasper.env), a DIFFERENT
# file from the three fan-in keys, so the reconciler owns writes to BOTH
# fanin.env and usbsink.env. (The daemon no longer reads this key to gate
# behavior; it is retained for one release for a clean migration and for the
# state/doctor surfaces that narrate "standby".)
USBSINK_STANDBY_ENV_VAR = "JASPER_USBSINK_AUDIO_STANDBY"
USBSINK_STANDBY_ON_VALUE = "1"
# usbsink.env — where the standby key is written (jasper-usbsink.service's own
# EnvironmentFile). Independently declared here and in
# jasper.fanin.coupling_reconcile; kept from drifting by
# test_usbsink_env_path_agrees_between_coupling_writers, not by import.
USBSINK_ENV_PATH = "/var/lib/jasper/usbsink.env"

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
    ``loopback``); ``combo_armed`` records whether the USB combo is on;
    ``usb_combo_actions`` is the reconciler-owned set of ``fanin.env`` actions for
    the three fan-in combo keys (``enabled`` when armed, explicit ``disabled``
    otherwise); and ``usbsink_standby_actions`` is the reconciler-owned
    ``usbsink.env`` action for the bridge-standby half (always ``1`` — the daemon is
    standby-only; a DIFFERENT file, hence a separate action list). ``reason`` is
    a stable, log-friendly explanation of the coupling decision; ``gate_details``
    carries the per-gate detail for the journal.
    """

    owned: bool
    coupling: str
    usb_combo_actions: tuple[RuntimeEnvAction, ...] = ()
    usbsink_standby_actions: tuple[RuntimeEnvAction, ...] = ()
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

    Gadget presence (the ``dtoverlay``) is fleet-wide (it also carries the always-on
    USB management network), so it is a NECESSARY but not SUFFICIENT signal — the
    combo also needs the household's persistent USB-audio intent
    (``jasper-usbsink.service`` enabled), the same signal the ``/sources/`` wizard
    owns. Gating on the dtoverlay alone would arm the split-brain combo on every box.
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


def usbsink_standby_actions() -> tuple[RuntimeEnvAction, ...]:
    """The reconciler-owned ``usbsink.env`` action for the bridge-standby half.

    ALWAYS ``JASPER_USBSINK_AUDIO_STANDBY=1`` — armed or not. The jasper-usbsink
    daemon is standby-only now (the aloop capture/delivery path was deleted), so it
    never holds ``hw:UAC2Gadget`` and disarming the combo must NOT try to promote a
    bridge capture that no longer exists: disarm simply leaves USB audio
    UNAVAILABLE until the direct lane recovers. Written explicitly (not unset) to
    hold the single-writer line and keep the state/doctor "standby" narration
    coherent. Returned as its own list because this key lands in usbsink.env, a
    different file from the three fan-in keys.
    """
    return (RuntimeEnvAction("set", USBSINK_STANDBY_ENV_VAR, USBSINK_STANDBY_ON_VALUE),)


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
) -> AutoCouplingDecision:
    """Resolve the default coupling + USB combo for one box (pure).

    - If the marker names an operator choice → ``owned=False`` and NO actions (the
      reconciler makes zero env changes; the operator's revert sticks).
    - Else the auto pass owns the box:
        * ``coupling`` = ``shm_ring`` iff EVERY ring gate returns ``ok`` (assets
          present, topology ring-eligible, geometry coherent on both axes, route
          supports the ring); the first failing gate short-circuits to ``loopback``
          with its detail as the reason (so an ineligible box — jts3 roleful, jts5
          composite, a grouped box — resolves loopback with a crisp explanation).
        * combo = ARMED iff ``gadget_present AND usb_intent_enabled`` (see
          :func:`combo_is_armed`) AND NOT ``fallback_active``;
          ``usb_combo_actions`` and ``usbsink_standby_actions`` carry the explicit
          on/off writes for both halves either way (the single-writer discipline
          writes an explicit off, never an unset).

    ``fallback_active`` is the runtime-fallback flap guard (defect 2026-07-10): a
    combo-eligible box whose live capture broke at runtime carries the fallback
    marker (:mod:`jasper.fanin.combo_health`), which forces the combo OFF here even
    though ``gadget_present AND usb_intent_enabled``. Since the aloop solo path was
    deleted there is NO fallback capture to promote — the box is simply left with
    USB audio UNAVAILABLE (the direct lane disarmed, the bridge in standby), which
    the doctor + ``/state`` surface LOUDLY, and it re-attempts on the next
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
            coupling=COUPLING_LOOPBACK,
            usb_combo_actions=(),
            usbsink_standby_actions=(),
            combo_armed=armed,
            gadget_present=gadget_present,
            usb_intent_enabled=usb_intent_enabled,
            fallback_active=fallback,
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
        usb_combo_actions=usb_combo_actions(armed=armed),
        usbsink_standby_actions=usbsink_standby_actions(),
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


def default_ring_gates() -> "tuple[tuple[str, RingGate], ...]":
    """The #1169 ring arm preflights the auto pass reuses, in ``_arm_ring`` order.

    Lazily binds the reconciler's own gate helpers so the auto pass gates on the
    SAME predicates a manual ``shm_ring`` arm would — no second, drift-prone
    eligibility copy — with ONE deliberate difference: the topology gate is the
    STRICT (fail-closed-on-unreadable) variant ``ring_topology_ready_strict`` rather
    than the human-arm ``ring_topology_ready`` (an unattended default must not
    arm→rollback-churn on a transiently unreadable topology — defect-F4). This
    factory is only the ASSET + TOPOLOGY pair that need no env text; the reconciler
    appends the ROUTE-support gate and the two geometry gates (which need the
    outputd/fanin env text) as bound closures.
    """
    from jasper.fanin.coupling_reconcile import (
        ring_assets_ready,
        ring_topology_ready_strict,
    )

    return (
        ("ring_assets", ring_assets_ready),
        ("ring_topology", ring_topology_ready_strict),
    )


def read_boot_config_gadget_present() -> bool:
    """Live gadget-presence read (thin wrapper for the reconciler + tests).

    Honors both env override names: ``JTS_BOOT_CONFIG_FILE`` (install.sh's name,
    used by ``set_usb_gadget_mode``) and ``JASPER_BOOT_CONFIG_PATH`` — so a test
    harness or an operator that set either sees a consistent gadget read across the
    installer and this probe (defect-Nit10). ``JTS_BOOT_CONFIG_FILE`` wins when both
    are set (it is the installer-facing name); falls back to the default path.
    """
    override = os.environ.get("JTS_BOOT_CONFIG_FILE") or os.environ.get(
        "JASPER_BOOT_CONFIG_PATH"
    )
    return usb_gadget_stack_present(override or BOOT_CONFIG_PATH)


# The USB-audio persistent-intent unit — the SAME signal the /sources/ wizard
# toggles (jasper.web.sources_setup) and the doctor reads (jasper.cli.doctor.usbsink
# `_audio_wanted`) for "the household turned USB Audio Input on". Its enabled state,
# not the dtoverlay, is the household-intent half of the combo gate.
USBSINK_INTENT_UNIT = "jasper-usbsink.service"


def usbsink_intent_enabled(unit: str = USBSINK_INTENT_UNIT) -> bool:
    """True iff USB Audio Input is turned ON by the household.

    Reads ``systemctl is-enabled --quiet <unit>`` (returncode 0 == enabled) — the
    same persistent-intent probe the ``/sources/`` wizard and the doctor use. This is
    the household-intent half of the P3 combo gate (the gadget dtoverlay is the
    availability half). Fail-soft: any error (systemctl missing, timeout) reads as
    NOT enabled, so the combo stays off — the fail-safe direction. A read-only probe
    (systemd lets any user query unit state), so no restart-broker hop is needed.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["systemctl", "is-enabled", "--quiet", unit],
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("usbsink intent probe failed: %s", e)
        return False
    return proc.returncode == 0
