# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""DEFAULT-RESOLUTION for the USB low-latency combo.

WHY THIS EXISTS — the campaign flipped this feature's shipped default from
"off, opt-in" to "on where the box is eligible". The combo arms ONLY on a box
that BOTH (a) has the resolved USB gadget capability available and (b) has USB
Audio Input turned ON by the household (canonical source intent enabled), local
sources allowed for this speaker's current role, AND the coordinator-derived
``jasper-usbsink.service`` enablement confirming lifecycle readiness. The boot
overlay alone is NOT enough: the same data port may belong to a USB output DAC
on a Zero-class board. All signals present → arm the fan-in half
(``JASPER_FANIN_USB_DIRECT`` + ``JASPER_FANIN_HOST_CLOCK`` and the household's
fixed cushion-decay preset in fanin.env; fan-in owns the gadget capture). Off a
combo box the feature keys are written to their EXPLICIT off value
(``disabled``), NOT unset — an unset key lets a stale ``enabled`` in
``/etc/jasper/jasper.env`` (loaded before the reconciler-owned files) win. There
is no separate USB bridge process: armed means USB flows through fan-in's DIRECT
lane, while disarmed means USB audio is unavailable.

This module owns the pure DECISION only. The reconciler
(:mod:`jasper.fanin.coupling_reconcile`) owns the env I/O and the daemon
transitions — the single-writer discipline (pattern 3: reconciler is the single
env writer; daemons read the resolved env). It is import-cheap (stdlib only) so
the reconciler CLI and any tests can resolve the decision without pulling in the
heavy topology/ring readers unless a real box asks.

THE COUPLING IS NOT DECIDED HERE: ADR-0100 left one central transport, so there
is no route to choose between.

FAIL-SAFE DIRECTION = combo-off. A signal that cannot be proved is never read as
permission: an unreadable config file does not arm a capture lane.
"""

from __future__ import annotations

import logging
import subprocess

from jasper.audio_runtime_plan import RuntimeEnvAction
from jasper.fanin.latency_mode import DEFAULT_MODE, preset_for
from jasper.output_hardware import current_usb_data_role

logger = logging.getLogger(__name__)

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


def combo_armed_from_env(text: str) -> bool:
    """The OBSERVED counterpart to ``combo_is_armed``'s INTENT.

    Reads whether ``fanin.env`` content shows the combo already armed, rather
    than recomputing the decision — the doctor and ``/state`` both need this
    same read of the reconciler's own output.
    """
    from jasper.env_file import read_value

    return read_value(text, USB_DIRECT_ENV_VAR) == USB_COMBO_ENABLED_VALUE


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
    from jasper.local_sources.markers import local_sources_allowed
    from jasper.music_sources import Source
    from jasper.source_intent import source_intent_enabled

    if not source_intent_enabled(Source.USBSINK):
        return False
    if not local_sources_allowed()[0]:
        return False
    return _usbsink_lifecycle_ready()
