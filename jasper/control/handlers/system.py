# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP routes for the system control-plane concern."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any, Callable

from ...audio_quality import (
    DEFAULT_CONVERTER as _default_audio_converter,
    apply_requested_converter,
    converter_options as _audio_converter_options,
    normalize_converter,
    read_active_converter as _read_active_audio_converter,
    read_state as _read_audio_quality_state,
)
from ...doctor_contract import (
    REASON_SNAPSHOT_PENDING,
    REASON_SNAPSHOT_UNAVAILABLE,
)
from ...fanin.latency_mode import (
    LatencyApplyError,
    apply_requested_mode,
    normalize_mode,
)
from ...install_profile import system_capabilities_for_profile
from ...local_sources import local_source_park_units
from ...log_event import log_event
from .. import debug_control
from .. import server as _server
from .. import state_aggregate
from .. import usb_gadget_forensics
from ._base import ControlHandlerMixin, logger


def _safe_audio_quality_state() -> dict[str, Any]:
    try:
        return _read_audio_quality_state()
    except Exception as e:  # noqa: BLE001
        logger.exception("audio quality state read failed")
        converter = _default_audio_converter
        options = _audio_converter_options()
        meta = next(
            option for option in options if option["converter"] == converter
        )
        try:
            active = _read_active_audio_converter()
        except Exception:  # noqa: BLE001
            active = None
        return {
            "converter": converter,
            "active_converter": active,
            "label": meta["label"],
            "summary": meta["summary"],
            "options": options,
            "error": str(e),
        }


class SystemRoutes(ControlHandlerMixin):
    def _transport_park_reader(self) -> Callable[[], dict[str, Any]]:
        """The park-verdict reader both operator surfaces share.

        The health sampler's CACHED verdict when it has one, so the park rows
        and the health rows built from it in one payload are the same
        observation rather than two reads minutes apart; the module's own
        fresh read otherwise, because ``/state`` and ``/system/snapshot`` must
        keep answering when the sampler does not — a contract older than this
        field. ``transport_park.snapshot()`` is fail-soft and never raises, so
        a sampler-less handler still answers.

        One resolver for both routes: the fallback rule is a single fact, and
        a change to it that reached only one surface is exactly the drift the
        park classifier exists to prevent.
        """
        from ..transport_park import snapshot

        return getattr(
            self._audio_health_sampler, "transport_park_snapshot", None,
        ) or snapshot
    def _get_healthz(self) -> None:
        self._send_json({"ok": True})

    def _get_debug(self) -> None:
        # Runtime debug-logging state for the /system Debug card:
        # per-subsystem on/off + the shared auto-expiry countdown.
        self._send_json(debug_control.snapshot())

    def _get_state(self) -> None:
        # Cross-daemon snapshot — voice / audio / renderers.
        # Polled by the management UI for live
        # status, used by jasper-doctor for one-shot health,
        # and consumable from `curl jts.local:8780/state | jq`
        # for ad-hoc debugging. ~200 ms typical (mostly the
        # parallel busctl + camilla WS probes).
        try:
            state = self._state_response_cache.get_or_compute(
                lambda: asyncio.run(
                    _server._get_state(
                        camilla_host=self._camilla_host,
                        camilla_port=self._camilla_port,
                        voice_socket_path=self._voice_socket_path,
                        ha_status_snapshot=self._ha_status_cache.snapshot,
                        # shairport's MPRIS PlaybackStatus from the health
                        # sampler that already holds it, so `/state` runs no
                        # `busctl` of its own (ADR-0233 rules 1 and 2).
                        airplay_playing_snapshot=(
                            None if self._audio_health_sampler is None
                            else self._audio_health_sampler.airplay_playing
                        ),
                        # One tick's transport-park verdict for the whole
                        # payload: `resilience.transport_park` and the
                        # `audio_health` rows attached below are then the same
                        # observation, not two reads minutes apart. Resolved by
                        # the shared reader, so /system/snapshot cannot end up
                        # on a different fallback rule.
                        transport_park_snapshot=self._transport_park_reader(),
                        # The 30 s systemd snapshot this daemon already
                        # samples for /system — reused so audio_graph can
                        # report a stopped CamillaDSP without a second probe.
                        service_states_snapshot=(
                            None if self._sampler is None
                            else self._sampler.service_states_snapshot
                        ),
                    )
                ),
            )
            # The audio-health sampler is the single normalized health
            # contract shared by /state and /system/snapshot. Copy the
            # cached aggregate before attaching it so the cross-request
            # state cache never retains a stale health object.
            if self._audio_health_sampler is not None:
                state = dict(state)
                try:
                    state["audio_health"] = self._audio_health_sampler.snapshot()
                except (
                    AttributeError,
                    KeyError,
                    OSError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ):
                    logger.exception("/state audio health snapshot failed")
                    state["audio_health"] = None
            state = dict(state)
            state["usb_gadget_forensics"] = usb_gadget_forensics.snapshot()
        except Exception as e:  # noqa: BLE001
            logger.exception("/state aggregation failed")
            self._send_json({"error": str(e)}, status=502)
            return
        self._send_json(state)

    def _get_system_snapshot(self) -> None:
        # Snapshot for the /system dashboard. Current values +
        # 60-min ring buffers for the sparklines + build info +
        # home_assistant connection status.
        # Sampler may be None in tests / direct CLI invocation;
        # surface an empty history rather than 500.
        from ..system_metrics import read_build_info
        from ...speaker_name import read_state as _read_speaker_name_state
        from ...voice.provider_state import read_active_provider

        try:
            ha_status = self._ha_status_cache.snapshot()
        except Exception:  # noqa: BLE001
            # Fail-soft per the existing aggregator convention —
            # never break /system/snapshot because HA is wedged.
            logger.exception("home assistant status snapshot failed")
            ha_status = {
                "configured": False,
                "connected": False,
                "url": "",
                "instance_name": None,
                "version": None,
                "error": "probe failed",
            }

        try:
            if self._audio_health_sampler is not None:
                airplay_health = self._audio_health_sampler.airplay_snapshot()
            else:
                airplay_health = (
                    self._airplay_health_sampler.snapshot()
                    if self._airplay_health_sampler is not None
                    else None
                )
        except Exception:  # noqa: BLE001
            logger.exception("airplay health snapshot failed")
            airplay_health = {
                "status": "unknown",
                "reason": "AirPlay health sampler failed",
            }

        try:
            if self._audio_health_sampler is not None:
                outputd_status = self._audio_health_sampler.outputd_snapshot()
            else:
                outputd_status = asyncio.run(state_aggregate._outputd_status())
        except Exception:  # noqa: BLE001
            logger.exception("outputd status snapshot failed")
            outputd_status = None

        try:
            audio_health = (
                self._audio_health_sampler.snapshot()
                if self._audio_health_sampler is not None
                else None
            )
        except Exception:  # noqa: BLE001
            logger.exception("audio health snapshot failed")
            audio_health = None

        # The park verdict the /system page's park card renders — the same
        # cached verdict `/state.resilience.transport_park` reads, through the
        # same resolver.
        #
        # NOT identical at every instant: `/state`'s whole response sits behind
        # `STATE_RESPONSE_CACHE_TTL_SEC` (server.py) while this route composes
        # fresh, so a sampler tick between the two leaves a skew window bounded
        # by that TTL. Bounded and self-clearing, and both surfaces name the
        # same park either way — what one writer for the verdict buys is that
        # they cannot name DIFFERENT parks, not that they update in lockstep.
        park_reader = self._transport_park_reader()

        install_profile = _server._control_install_profile()
        payload: dict[str, Any] = {
            "build": read_build_info(),
            "transport_park": park_reader(),
            "metrics": (
                self._sampler.snapshot() if self._sampler is not None else None
            ),
            "airplay_health": airplay_health,
            "audio_health": audio_health,
            "active_speaker_output_safety": (
                _server._active_speaker_output_safety_snapshot(airplay_health)
            ),
            "outputd": outputd_status,
            "audio_quality": _safe_audio_quality_state(),
            "usb_latency": _server._safe_usb_latency_state(airplay_health),
            "voice_provider": read_active_provider(),
            "speaker_name": _read_speaker_name_state().__dict__,
            "home_assistant": ha_status,
            "system_capabilities": system_capabilities_for_profile(
                install_profile,
            ),
            "usb_gadget_forensics": usb_gadget_forensics.snapshot(),
        }
        self._send_json(payload)

    # jasper-doctor is a ROOT tool (audio/mixer/journal/renderer probes + `sudo -u
    # <renderer> aplay`), and jasper-control is now non-root — running the doctor
    # in-process here would make ~7 hardware checks fail on permissions (false red on
    # the dashboard). So the report is produced by the root jasper-doctor-json.service
    # oneshot, which jasper-control starts via its polkit manage-units grant (the unit
    # is in MANAGED_UNITS). The HTTP path serves the latest cached report immediately
    # and schedules stale/missing refreshes with `systemctl --no-block`, so the
    # dashboard never waits on a live run.
    def _get_system_diagnostics(self) -> None:
        body, read_error = _server._read_diagnostics_snapshot(
            _server._DIAGNOSTICS_RESULT_PATH,
            ttl_seconds=_server._DIAGNOSTICS_CACHE_TTL_SECONDS,
        )
        if body is None:
            refreshing, refresh_error = _server._start_diagnostics_refresh(
                snapshot_age_seconds=None,
            )
            if refreshing:
                self._send_json(
                    _server._diagnostics_placeholder_result(
                        detail=(
                            "diagnostics snapshot not ready yet; "
                            "background refresh started"
                        ),
                        status="warn",
                        reason=REASON_SNAPSHOT_PENDING,
                        refreshing=True,
                    ),
                )
                return
            self._send_json(
                _server._diagnostics_placeholder_result(
                    detail=(
                        f"diagnostics snapshot unavailable ({read_error}); "
                        f"{refresh_error}"
                    ),
                    status="fail",
                    reason=REASON_SNAPSHOT_UNAVAILABLE,
                    refreshing=False,
                ),
            )
            return

        if body.get("stale"):
            refreshing, refresh_error = _server._start_diagnostics_refresh(
                snapshot_age_seconds=body.get("cache_age_seconds"),
            )
            body["refreshing"] = refreshing
            if refresh_error:
                _server._append_diagnostics_refresh_failure(body, refresh_error)
        else:
            body["refreshing"] = False
        self._send_json(body)

    def _post_debug(self) -> None:
        # /system Debug card: raise one subsystem to DEBUG
        # logging. Additive-only + auto-expiring (jasper/
        # debug_mode.py). Daemon subsystems restart to apply;
        # control applies in-process. Non-blocking — the card's
        # "Applying…" state is just UX.
        body = self._read_json()
        subsystem = str(body.get("subsystem") or "")
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            self._send_json(
                {"error": "enabled must be a boolean"},
                status=400,
            )
            return
        try:
            debug_control.set_debug(subsystem, enabled)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
            return
        except (OSError, subprocess.SubprocessError) as e:
            self._send_json(
                {"error": f"debug toggle failed: {e}"},
                status=502,
            )
            return
        log_event(
            logger,
            "debug.toggle",
            subsystem=subsystem,
            enabled=enabled,
            client=self.address_string(),
        )
        self._send_json(debug_control.snapshot())
        return

    def _post_usb_forensics(self) -> None:
        body = self._read_json()
        action = str(body.get("action") or "")
        try:
            if action == "set_enabled":
                if not isinstance(body.get("enabled"), bool):
                    self._send_json(
                        {"error": "enabled must be a boolean"},
                        status=400,
                    )
                    return
                result = usb_gadget_forensics.set_enabled(body["enabled"])
            else:
                result = usb_gadget_forensics.request(action)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=409)
            return
        except OSError as e:
            self._send_json(
                {"error": f"USB forensics request failed: {e}"},
                status=502,
            )
            return
        log_event(
            logger,
            "usb_gadget.forensics_request",
            action=action,
            client=self.address_string(),
        )
        self._send_json(result, status=202 if action != "set_enabled" else 200)
        return

    def _post_system_audio_quality(self) -> None:
        body = self._read_json()
        if not isinstance(body, dict):
            self._send_json(
                {"error": "invalid request body: expected JSON object"},
                status=400,
            )
            return
        raw_converter = body.get("converter")
        if not isinstance(raw_converter, str) or not raw_converter.strip():
            self._send_json(
                {"error": "converter is required"},
                status=400,
            )
            return
        try:
            converter = normalize_converter(raw_converter)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
            return
        try:
            state = apply_requested_converter(converter)
        except (OSError, subprocess.SubprocessError) as e:
            logger.exception("audio quality apply failed")
            self._send_json(
                {"error": f"audio quality apply failed: {e}"},
                status=502,
            )
            return
        try:
            # Refresh active renderers without resurrecting sources the
            # household explicitly disabled in /sources/.
            subprocess.Popen(
                [
                    "systemctl",
                    "try-restart",
                    *_server.LOCAL_SOURCE_AUDIO_REFRESH_UNITS,
                ],
            )
        except (OSError, subprocess.SubprocessError) as e:
            self._send_json(
                {"error": f"renderer restart failed: {e}"},
                status=502,
            )
            return
        log_event(
            logger,
            "audio_quality.set",
            converter=converter,
            client=self.address_string(),
        )
        self._send_json(
            {
                "ok": True,
                "action": "audio-quality",
                "try_restart_units": _server.LOCAL_SOURCE_AUDIO_REFRESH_UNITS,
                "audio_quality": state,
            }
        )
        return

    def _post_system_usb_latency(self) -> None:
        body = self._read_json()
        if not isinstance(body, dict):
            self._send_json(
                {"error": "invalid request body: expected JSON object"},
                status=400,
            )
            return
        raw_mode = body.get("mode")
        if not isinstance(raw_mode, str) or not raw_mode.strip():
            self._send_json({"error": "mode is required"}, status=400)
            return
        try:
            mode = normalize_mode(raw_mode)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
            return
        try:
            apply_requested_mode(mode)
        except (OSError, LatencyApplyError) as e:
            logger.exception("USB latency apply failed")
            self._send_json(
                {
                    "error": f"USB latency apply failed: {e}",
                    "selected_mode": mode,
                },
                status=502,
            )
            return
        _server._mark_usb_latency_applying(mode)
        log_event(
            logger,
            "usb_latency.set",
            mode=mode,
            client=self.address_string(),
        )
        self._send_json({"ok": True, "action": "usb-latency", "mode": mode})
        return

    def _post_system_action(self) -> None:
        # Action endpoints for the /system dashboard. All
        # shell out to systemctl; jasper-control already runs
        # as root so no sudo needed. Returns immediately —
        # the restart is async on systemd's side and the
        # dashboard polls /system/snapshot to know when
        # things are back up.
        #
        # Risk model: LAN-local + browser-origin guard
        # (consistent with the wizards). Anyone already on the
        # trusted WiFi can trigger these; the dashboard's
        # confirm dialogs are UX, not security.
        parked = _server._pair_follower_leader_addr() is not None
        restart_units: list[str] = []
        try_restart_units: list[str] = []
        if self.path == "/system/restart/voice":
            if parked:
                # The dumb-follower profile keeps voice disabled
                # while paired — a dashboard restart would boot
                # 240 MB of models that jasper-aec-reconcile
                # re-parks. Refuse with the story, never silently.
                self._send_json(
                    {
                        "error": "voice is parked while this speaker "
                        "is in a stereo pair — the assistant "
                        "runs on the pair leader"
                    },
                    status=409,
                )
                return
            units = ["jasper-voice.service"]
            restart_units = units
            action = "restart-voice"
        elif self.path == "/system/restart/audio":
            restart_units = list(_server.CORE_AUDIO_RESTART_UNITS)
            try_restart_units = list(_server.LOCAL_SOURCE_AUDIO_REFRESH_UNITS)
            if parked:
                # Restart only the units the follower profile keeps
                # alive — derived from the local-source lifecycle
                # registry so it cannot drift from follower parking.
                parked_units = set(local_source_park_units())
                try_restart_units = [
                    u for u in try_restart_units if u not in parked_units
                ]
            units = restart_units + try_restart_units
            action = "restart-audio"
        elif self.path == "/system/reboot":
            units = []  # systemctl reboot — no units
            action = "reboot"
        else:
            # poweroff is reboot's terminal sibling: the speaker
            # stays off until someone physically re-plugs power.
            # The "graceful" part matters more than usual here —
            # this endpoint exists *specifically* to give the
            # household a non-power-yank way to shut down before
            # hardware changes, after 2026-05-23's dirty-shutdown
            # incident wiped the NetworkManager keyfile.
            units = []  # systemctl poweroff — no units
            action = "poweroff"
        # Audit BEFORE the action: reboot/poweroff take the system down, so
        # a log-after might never flush. This is the line that
        # distinguishes a dashboard-triggered restart/reboot/poweroff from a
        # watchdog or crash reset when debugging "the speaker restarted on
        # its own" (see AGENTS.md). No secrets — action + units + requester.
        log_event(
            logger,
            "system.action",
            action=action,
            units=",".join(units) or "-",
            client=self.address_string(),
        )
        try:
            if action == "reboot":
                subprocess.Popen(["systemctl", "reboot"])
            elif action == "poweroff":
                subprocess.Popen(["systemctl", "poweroff"])
            else:
                # Use start-after-stop semantics for core services. Local
                # source daemons use try-restart so dashboard audio restart
                # never turns on a source the household disabled in
                # /sources/ (USB would otherwise re-advertise its gadget).
                if restart_units:
                    subprocess.Popen(["systemctl", "restart", *restart_units])
                if try_restart_units:
                    subprocess.Popen(
                        [
                            "systemctl",
                            "try-restart",
                            *try_restart_units,
                        ]
                    )
        except (OSError, subprocess.SubprocessError) as e:
            self._send_json(
                {"error": f"systemctl invocation failed: {e}"},
                status=502,
            )
            return
        self._send_json(
            {
                "ok": True,
                "action": action,
                "units": units,
                "restart_units": restart_units,
                "try_restart_units": try_restart_units,
            }
        )
        return
