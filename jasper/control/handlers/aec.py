# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP routes for the aec control-plane concern."""

from __future__ import annotations

import subprocess
import time
from typing import Any, cast

from ...log_event import log_event
from .. import aec_endpoints
from .. import server as _server
from ._base import ControlHandlerMixin, logger


def _commission_start_body(*, running: bool) -> dict[str, Any]:
    """The `commission` object on a start refusal: no run's verdict to report.

    Same keys as GET /aec's, so one client-side reader handles both.
    """
    return {"running": running, "state": "", "detail": ""}


class AecRoutes(ControlHandlerMixin):
    def _get_aec(self) -> None:
        # AEC bridge state + per-leg config + wake
        # threshold. Mode and leg booleans are the persisted
        # request (what the operator asked for, via the /wake/
        # page or aec_mode.env directly); bridge_active is the
        # observed truth from systemd. They diverge briefly
        # during a reconciler-driven transition (~10-15 s).
        # Threshold is read from wake_model.env — the same
        # file the /wake/ form save writes the model into, so
        # both controls stay in sync without sharing code.
        #
        # DTLN load failures don't surface in this payload —
        # /system's Diagnostics disclosure runs jasper-doctor
        # which has check_aec_bridge_dtln_engine for the
        # silent-failure case.
        self._send_json(aec_endpoints._aec_full_status())

    def _get_enhanced_aec(self) -> None:
        self._send_json(aec_endpoints._enhanced_aec_status())

    def _post_aec_leg(self) -> None:
        # Toggle one of the additive wake-detection legs
        # (raw chip-direct, DTLN neural, or chip-AEC beams). The
        # reconciler maps the boolean back to the underlying env vars
        # the bridge + voice each read at startup, then
        # restarts whichever daemons need to pick up the
        # change. Per-leg sub-toggles are only meaningful
        # when JASPER_AEC_MODE=auto and the bridge is up;
        # the reconciler clears the underlying vars when
        # AEC is disabled so a stale leg config doesn't
        # leave voice listening on a port nobody talks to.
        #
        # Risk model: LAN-local + browser-origin guard, same
        # as /system/restart/*.
        body = self._read_json()
        leg = body.get("leg")
        enabled_val = body.get("enabled")
        if leg not in aec_endpoints._TOGGLE_TO_TOKEN:
            self._send_json(
                {
                    "error": "leg must be one of: "
                    + ", ".join(sorted(aec_endpoints._TOGGLE_TO_TOKEN))
                },
                status=400,
            )
            return
        if not isinstance(enabled_val, bool):
            self._send_json(
                {"error": "enabled must be a boolean"},
                status=400,
            )
            return
        try:
            aec_endpoints._write_aec_leg(leg, enabled_val)
        except (OSError, ValueError) as e:
            self._send_json(
                {"error": f"write aec_mode.env failed: {e}"},
                status=502,
            )
            return
        try:
            aec_endpoints._kick_aec_reconciler()
        except (OSError, subprocess.SubprocessError) as e:
            self._send_json(
                {"error": f"reconciler restart failed: {e}"},
                status=502,
            )
            return
        log_event(
            logger,
            "aec.leg",
            leg=leg,
            enabled=enabled_val,
            client=self.address_string(),
        )
        self._send_json(aec_endpoints._aec_full_status())
        return

    def _post_aec_profile(self) -> None:
        # Set the canonical mic/AEC input profile. This is the
        # preferred surface for households and onboarding: it writes
        # one high-level choice plus rollback-safe legacy leg keys.
        # The older /aec/leg route remains as a custom expert control
        # and stamps JASPER_AUDIO_INPUT_PROFILE=custom.
        body = self._read_json()
        profile = body.get("profile")
        if not isinstance(profile, str):
            self._send_json(
                {"error": "profile must be a string"},
                status=400,
            )
            return
        try:
            aec_endpoints._write_audio_input_profile(profile)
        except (OSError, ValueError) as e:
            self._send_json(
                {"error": f"write aec_mode.env failed: {e}"},
                status=400 if isinstance(e, ValueError) else 502,
            )
            return
        try:
            aec_endpoints._kick_aec_reconciler()
        except (OSError, subprocess.SubprocessError) as e:
            self._send_json(
                {"error": f"reconciler restart failed: {e}"},
                status=502,
            )
            return
        log_event(
            logger,
            "aec.profile",
            profile=_server.normalize_audio_input_profile(profile, default=""),
            client=self.address_string(),
        )
        self._send_json(aec_endpoints._aec_full_status())
        return

    def _post_aec_usb_mic(self) -> None:
        # Persist intent before descriptor mutation. The gadget restart is
        # delayed very slightly so a request arriving over its own USB-NCM
        # link can receive this response before re-enumeration drops that
        # link for a few seconds.
        body = self._read_json()
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            self._send_json(
                {"error": "enabled must be a boolean"},
                status=400,
            )
            return
        current = aec_endpoints._aec_full_status()
        usb_mic = current.get("usb_mic") or {}
        if enabled and not bool(usb_mic.get("toggle_enabled")):
            self._send_json(
                {
                    "error": usb_mic.get("detail")
                    or "USB microphone is not available in the current audio setup.",
                    "usb_mic": usb_mic,
                },
                status=409,
            )
            return
        try:
            _server.write_usb_mic_enabled(enabled)
        except OSError as exc:
            self._send_json(
                {"error": f"write usb_mic.env failed: {exc}"},
                status=502,
            )
            return
        log_event(
            logger,
            "usb_mic.set",
            enabled=enabled,
            client=self.address_string(),
        )
        if not _server._schedule_usb_gadget_recompose():
            failed_status = aec_endpoints._aec_full_status()
            self._send_json(
                {
                    "error": (
                        "USB microphone preference was saved, but its "
                        "hardware update could not be scheduled."
                    ),
                    "code": "usb_mic_recompose_schedule_failed",
                    "intent_saved": True,
                    "requested_enabled": enabled,
                    "usb_mic": failed_status.get("usb_mic") or {},
                },
                status=502,
            )
            return
        self._send_json(aec_endpoints._aec_full_status())
        return

    def _post_aec_usb_mic_leg(self) -> None:
        """Persist the computer-mic source, then restart its producer only."""

        body = self._read_json()
        leg = body.get("leg")
        if not isinstance(leg, str) or not leg.strip():
            self._send_json(
                {"error": "leg must be a non-empty string"},
                status=400,
            )
            return
        leg = leg.strip()
        # Match GET /aec's fresh reconciler-owned view. jasper-control is
        # long-lived, so its process environment can lag a mic hotplug.
        choices = _server.usb_mic_leg_choices(aec_endpoints._fresh_jasper_env())
        allowed = {
            str(choice.get("value") or "")
            for choice in choices
            if isinstance(choice, dict)
        }
        if leg not in allowed:
            self._send_json(
                {
                    "error": (
                        "leg is not available for the current microphone profile"
                    ),
                    "requested_leg": leg,
                    "choices": choices,
                },
                status=409,
            )
            return
        # Serialize the read/write/apply decision across ThreadingHTTPServer
        # workers. Same-value saves are true no-ops. A deliberate changed
        # value clears systemd's crash-loop counter before restart so rapid
        # authenticated changes cannot spend StartLimitAction=reboot's
        # recovery budget (the reconciler uses the same safety contract).
        with _server._usb_mic_leg_apply_lock:
            persisted_matches = _server.read_usb_mic_leg() == leg
            if persisted_matches:
                current_status = aec_endpoints._aec_full_status()
                selection = (current_status.get("usb_mic") or {}).get(
                    "source_selection"
                ) or {}
                applied = selection.get("applied") or {}
                if applied.get("value") == leg:
                    _server._usb_mic_leg_apply_pending = None
                    log_event(
                        logger,
                        "usb_mic.leg_unchanged",
                        leg=leg,
                        client=self.address_string(),
                    )
                    self._send_json(current_status)
                    return
                pending = _server._usb_mic_leg_apply_pending
                if (
                    pending is not None
                    and pending[0] == leg
                    and time.monotonic() - pending[1]
                    < _server._USB_MIC_LEG_APPLY_COALESCE_SECONDS
                ):
                    log_event(
                        logger,
                        "usb_mic.leg_apply_pending",
                        leg=leg,
                        client=self.address_string(),
                    )
                    self._send_json(current_status)
                    return
            else:
                try:
                    _server.write_usb_mic_leg(leg)
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=400)
                    return
                except OSError as exc:
                    self._send_json(
                        {"error": f"write usb_mic.env failed: {exc}"},
                        status=502,
                    )
                    return
            log_event(
                logger,
                ("usb_mic.leg_reapply" if persisted_matches else "usb_mic.leg_set"),
                leg=leg,
                client=self.address_string(),
            )
            restart = _server.restart_broker.reset_then_manage(
                _server._AEC_BRIDGE_UNIT,
                verb="restart",
                reason="usb_mic_leg",
                no_block=True,
                timeout=5.0,
            )
            if not restart.get("ok"):
                failed_status = aec_endpoints._aec_full_status()
                self._send_json(
                    {
                        "error": (
                            "Computer microphone source was saved, but the "
                            "microphone bridge restart could not be scheduled."
                        ),
                        "code": "usb_mic_leg_restart_failed",
                        "intent_saved": True,
                        "requested_leg": leg,
                        "usb_mic": failed_status.get("usb_mic") or {},
                    },
                    status=502,
                )
                return
            _server._usb_mic_leg_apply_pending = (leg, time.monotonic())
        self._send_json(aec_endpoints._aec_full_status())
        return

    def _post_aec_threshold(self) -> None:
        # Sensitivity slider on the /wake/ page. Writes
        # JASPER_WAKE_THRESHOLD into wake_model.env (same
        # file the /wake/ form save writes the model into)
        # and restarts jasper-voice — the openWakeWord
        # detector reads the threshold at startup, so a hot
        # config change without a restart wouldn't take
        # effect on the next wake.
        #
        # AEC-mode and leg toggles share the reconciler
        # which already restarts voice; threshold-only
        # changes bypass the reconciler since they don't
        # need the bridge to restart. Non-blocking — the
        # slider's "Applying…" state is just UX.
        body = self._read_json()
        try:
            threshold = float(cast(Any, body.get("threshold")))
        except (TypeError, ValueError):
            self._send_json(
                {"error": "threshold must be a number"},
                status=400,
            )
            return
        if not 0.0 <= threshold <= 1.0:
            self._send_json(
                {"error": "threshold must be between 0 and 1"},
                status=400,
            )
            return
        try:
            aec_endpoints._write_wake_threshold(threshold)
        except (OSError, ValueError) as e:
            self._send_json(
                {"error": f"write wake_model.env failed: {e}"},
                status=502,
            )
            return
        try:
            subprocess.Popen(
                ["systemctl", "restart", "--no-block", "jasper-voice.service"],
            )
        except (OSError, subprocess.SubprocessError) as e:
            self._send_json(
                {"error": f"voice restart failed: {e}"},
                status=502,
            )
            return
        log_event(
            logger,
            "wake.threshold",
            value=f"{threshold:.2f}",
            client=self.address_string(),
        )
        self._send_json({"threshold": threshold})
        return

    def _post_aec_commission(self) -> None:
        # One-tap chip-AEC re-commissioning: start the root oneshot that runs
        # the audible measurement (minutes; stops voice/AEC while it runs).
        # Button-initiated only — nothing else starts this unit. Token-gated
        # like the other high-impact /aec mutations. The lock makes
        # check-then-start atomic across worker threads.
        with _server._aec_commission_start_lock:
            if _server._aec_commission_running():
                self._send_json(
                    {
                        "error": "chip-AEC re-commissioning is already running",
                        "commission": _commission_start_body(running=True),
                    },
                    status=409,
                )
                return
            started = _server._start_aec_commission()
        if not started:
            self._send_json(
                {
                    "error": "the re-commissioning run could not be started",
                    "code": "aec_commission_start_failed",
                    "commission": _commission_start_body(running=False),
                },
                status=502,
            )
            return
        log_event(
            logger,
            "aec.commission.start",
            client=self.address_string(),
        )
        self._send_json(aec_endpoints._aec_full_status())
        return

    def _post_aec_firmware_update(self) -> None:
        status = aec_endpoints._aec_full_status()
        firmware = status.get("firmware_update")
        action = firmware.get("action") if isinstance(firmware, dict) else {}
        if not isinstance(action, dict) or not action.get("enabled"):
            detail = (
                firmware.get("detail")
                if isinstance(firmware, dict)
                else "microphone firmware update is not available"
            )
            self._send_json({"error": detail}, status=409)
            return
        try:
            aec_endpoints._start_xvf_firmware_update()
        except (OSError, subprocess.SubprocessError) as e:
            self._send_json(
                {"error": f"firmware update start failed: {e}"},
                status=502,
            )
            return
        log_event(
            logger,
            "aec.firmware_update.start",
            client=self.address_string(),
            target=(firmware.get("target") or {}).get("id")
            if isinstance(firmware, dict)
            else "",
        )
        self._send_json(aec_endpoints._aec_full_status())
        return

    def _post_enhanced_aec_install(self) -> None:
        current = aec_endpoints._enhanced_aec_status()
        if current.get("state") in {"installed", "installing"}:
            self._send_json(current)
            return
        action = current.get("action")
        if not isinstance(action, dict) or not action.get("enabled"):
            self._send_json(
                {
                    "error": (
                        action.get("reason")
                        if isinstance(action, dict)
                        else "enhanced echo cancellation is unavailable"
                    ),
                    "enhanced_aec": current,
                },
                status=409,
            )
            return
        try:
            from ... import enhanced_aec

            enhanced_aec.request_install()
        except (OSError, TimeoutError, ValueError) as exc:
            self._send_json(
                {"error": f"enhanced-AEC request could not be saved: {exc}"},
                status=502,
            )
            return
        started = _server.restart_broker.manage_units(
            "jasper-enhanced-aec-install.service",
            verb="start",
            reason="enhanced_aec_install",
            no_block=True,
            timeout=5.0,
        )
        if not started.get("ok"):
            self._send_json(
                {
                    "error": (
                        "The preference was saved, but the background "
                        "installer could not be started."
                    ),
                    "code": "enhanced_aec_start_failed",
                    "intent_saved": True,
                    "enhanced_aec": aec_endpoints._enhanced_aec_status(),
                },
                status=502,
            )
            return
        log_event(
            logger,
            "aec.enhanced.install_start",
            client=self.address_string(),
        )
        self._send_json(aec_endpoints._enhanced_aec_status())
        return
