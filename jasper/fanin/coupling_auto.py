# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve and persist the USB combo; the reconciler owns daemon ordering."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from jasper.audio_runtime_settings import RuntimeEnvAction
from jasper.env_file import env_value
from jasper.env_load import FANIN_ENV_PATH
from jasper.fanin.env_actions import _apply_actions, _write_env_actions
from jasper.fanin.latency_mode import (
    DEFAULT_MODE, STATE_ENV_KEY, normalize_mode,
    read_requested_mode as read_usb_latency_mode,
)
from jasper.log_event import log_event
from jasper.playback_state.music_sources import Source
from jasper.output_hardware import current_usb_data_role
from jasper.systemd_probe import unit_state

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
USB_COMBO_ENABLED_VALUE = "enabled"
USB_COMBO_DISABLED_VALUE = "disabled"


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


def combo_armed_from_env(env: str | Mapping[str, str]) -> bool:
    """The OBSERVED counterpart to ``combo_is_armed``'s INTENT.

    Reads whether ``fanin.env`` content shows the combo already armed, rather
    than recomputing the decision — the doctor needs this same read of the
    reconciler's own output. Takes either raw env-file text or an
    already-parsed mapping (e.g. a caller's cached read of the file).
    """
    return env_value(env, USB_DIRECT_ENV_VAR) == USB_COMBO_ENABLED_VALUE


def usb_combo_actions(
    *, armed: bool, latency_mode: str = DEFAULT_MODE,
) -> tuple[RuntimeEnvAction, ...]:
    # Explicit off values override stale enabled keys in the earlier jasper.env.
    mode = normalize_mode(latency_mode)
    feature_value = USB_COMBO_ENABLED_VALUE if armed else USB_COMBO_DISABLED_VALUE
    return (
        RuntimeEnvAction("set", USB_DIRECT_ENV_VAR, feature_value),
        RuntimeEnvAction("set", HOST_CLOCK_ENV_VAR, feature_value),
        RuntimeEnvAction("set", STATE_ENV_KEY, mode),
        RuntimeEnvAction("unset", "JASPER_FANIN_RESAMPLER_CUSHION_DECAY"),
        RuntimeEnvAction("unset", "JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES"),
    )


def read_usb_gadget_available() -> bool:
    """Read the reconciler-owned capability used by every USB consumer."""

    try:
        return current_usb_data_role().gadget_available
    except (OSError, RuntimeError, ValueError) as exc:
        logger.debug("USB data-role read failed: %s", exc)
        return False


def _usbsink_lifecycle_ready() -> bool:
    """Return the coordinator-derived USB lifecycle readiness mirror.

    Reads systemd's own ``is-enabled`` exit verdict, so a failed probe reads
    as not ready."""

    return unit_state("is-enabled", "jasper-usbsink.service", timeout=5.0).rc == 0


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
    from jasper.local_sources.markers import local_sources_allowed  # lazy: test patch boundary (tests/test_fanin_coupling_auto.py)
    from jasper.source_intent import source_intent_enabled  # lazy: test patch boundary (tests/test_fanin_coupling_auto.py)

    if not source_intent_enabled(Source.USBSINK):
        return False
    if not local_sources_allowed()[0]:
        return False
    return _usbsink_lifecycle_ready()


@dataclass(frozen=True)
class UsbComboResult:
    gadget_present: bool
    usb_intent_enabled: bool
    combo_armed: bool
    usb_latency_mode: str
    changed: bool
    intent_failure: str
    latency_failure: str
    write_error: str | None = None


def converge_usb_combo(
    *,
    reason: str,
    logger: logging.Logger,
    env_path: str | Path = FANIN_ENV_PATH,
    gadget_present: bool | None = None,
    usb_intent_enabled: bool | None = None,
) -> UsbComboResult:
    from jasper.fanin.ring_readiness import read_snapshot  # lazy: import cost; doctor uses the decision helpers without ring readers

    fanin_snapshot = read_snapshot(env_path)
    gadget = (
        read_usb_gadget_available() if gadget_present is None else gadget_present
    )
    usb_intent_failure = ""
    if usb_intent_enabled is None:
        try:
            usb_intent = usbsink_effectively_enabled()
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            # A previously armed fan-in process retains its DIRECT lane until
            # this owner writes + applies the explicit-off combo plan. Treat the
            # unreadable preference as effective False, complete the ordinary
            # ordered write below, and only then return failure.
            usb_intent = False
            usb_intent_failure = f"USB source intent invalid or unreadable: {exc}"[:500]
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result="auto_usb_intent_invalid",
                reason=reason,
                usb_intent_enabled=False,
                detail=usb_intent_failure,
                level=logging.ERROR,
            )
    else:
        usb_intent = usb_intent_enabled
    usb_latency_failure = ""
    try:
        latency_mode = read_usb_latency_mode()
    except (OSError, UnicodeError, ValueError) as exc:
        latency_mode = "high"
        usb_latency_failure = (
            f"USB latency preference invalid or unreadable: {exc}"[:500]
        )
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="auto_usb_latency_invalid",
            reason=reason,
            usb_latency_mode=latency_mode,
            detail=usb_latency_failure,
            level=logging.ERROR,
        )

    combo_armed = not usb_intent_failure and combo_is_armed(
        gadget_present=gadget, usb_intent_enabled=usb_intent
    )
    combo_actions = usb_combo_actions(armed=combo_armed, latency_mode=latency_mode)

    _, combo_changed = _apply_actions(fanin_snapshot.text, combo_actions)
    if combo_changed:
        try:
            _write_env_actions(fanin_snapshot.path, lambda _text: combo_actions)
        except OSError as e:
            log_event(
                logger,
                "fanin.coupling_reconcile",
                result="auto_usb_combo_write_failed",
                reason=reason,
                gadget_present=gadget,
                error=e,
                level=logging.ERROR,
            )
            return UsbComboResult(
                gadget_present=gadget,
                usb_intent_enabled=usb_intent,
                combo_armed=combo_armed,
                usb_latency_mode=latency_mode,
                changed=False,
                intent_failure=usb_intent_failure,
                latency_failure=usb_latency_failure,
                write_error=str(e),
            )
        # Keep the live env coherent for the ring convergence's own re-read.
        for a in combo_actions:
            if a.action == "set":
                os.environ[a.key] = a.value
            else:
                os.environ.pop(a.key, None)
        log_event(
            logger,
            "fanin.coupling_reconcile",
            result="auto_usb_combo_written",
            reason=reason,
            gadget_present=gadget,
            usb_intent_enabled=usb_intent,
            combo_armed=combo_armed,
            usb_latency_mode=latency_mode,
            keys=",".join(a.key for a in combo_actions),
        )

    return UsbComboResult(
        gadget_present=gadget,
        usb_intent_enabled=usb_intent,
        combo_armed=combo_armed,
        usb_latency_mode=latency_mode,
        changed=combo_changed,
        intent_failure=usb_intent_failure,
        latency_failure=usb_latency_failure,
    )
