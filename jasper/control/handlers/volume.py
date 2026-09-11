# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP routes for the volume control-plane concern."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from ...active_speaker.setup_status import read_active_speaker_setup_status
from ...local_sources import status as source_status
from ...log_event import log_event
from ...music_sources import MUSIC_SOURCE_SPECS
from ...platform import wire
from ...platform.uds import mux_socket_command
from ...volume_curve import db_to_percent
from .. import measurement_hold
from .. import volume_ops
from ._base import ControlHandlerMixin, logger

SOURCE_AVAILABILITY_TTL_SEC = 10.0
SOURCE_SELECT_IDS = {spec.id.value for spec in MUSIC_SOURCE_SPECS}
_source_availability_cache: tuple[float, dict[str, Any]] | None = None
_source_availability_lock = threading.Lock()


def _augment_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Add on/off wizard availability to mux source status.

    Mux knows audio policy; `/sources/` knows whether each renderer is
    enabled/available. The landing selector needs both, but keeping the
    merge here avoids teaching mux about systemd/DBus source toggles.
    """
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        return payload
    global _source_availability_cache
    now = time.monotonic()
    with _source_availability_lock:
        cached = _source_availability_cache
        if cached is not None and now - cached[0] < SOURCE_AVAILABILITY_TTL_SEC:
            wizard_state = cached[1]
        else:
            wizard_state = None
    if wizard_state is None:
        try:
            fresh_state = source_status.read_source_status()
        except Exception as e:  # noqa: BLE001
            logger.debug("source availability read failed: %s", e)
            return payload
        with _source_availability_lock:
            _source_availability_cache = (now, fresh_state)
        wizard_state = fresh_state
    for spec in MUSIC_SOURCE_SPECS:
        wizard_key = spec.wizard_key
        mux_key = spec.id.value
        state = wizard_state.get(wizard_key)
        if not isinstance(state, dict):
            continue
        slot = sources.setdefault(mux_key, {})
        if isinstance(slot, dict):
            slot["available"] = bool(state.get("available", True))
            slot["enabled"] = bool(state.get("enabled", False))
    return payload


def _active_speaker_volume_block() -> dict[str, Any] | None:
    setup = read_active_speaker_setup_status()
    if setup.get("volume_allowed") is not True:
        return setup
    return None


async def _dispatch_transport(action: str) -> dict:
    return await volume_ops._dispatch_transport(
        action,
        spotify_router_factory=volume_ops._build_spotify_router_or_none,
    )


class VolumeRoutes(ControlHandlerMixin):
    def _get_volume(self) -> None:
        if self._maybe_forward_pair_action_to_leader():
            return
        try:
            state = self._get_op()
        except Exception as e:  # noqa: BLE001
            logger.exception("get volume failed")
            self._send_json({"error": str(e)}, status=502)
            return
        self._send_json(self._volume_payload(state))

    def _mux_cmd_or_error(
        self,
        cmd: str,
        *,
        log_label: str,
        timeout: float | None = None,
    ) -> dict[str, Any] | None:
        try:
            command = (
                mux_socket_command(cmd) if timeout is None
                else mux_socket_command(cmd, timeout=timeout)
            )
            return asyncio.run(command)
        except (OSError, asyncio.TimeoutError) as e:
            self._send_json(
                {"error": f"jasper-mux unreachable: {e}"},
                status=503,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("%s failed", log_label)
            self._send_json({"error": str(e)}, status=502)
        return None

    def _get_source_state(self) -> None:
        result = self._mux_cmd_or_error(wire.STATUS, log_label="source STATUS")
        if result is not None:
            self._send_json(_augment_source_payload(result))

    def _measurement_hold_decline(
        self, *, source: str, **fields: Any
    ) -> str | None:
        """Count one declined source-observed write, and log it. Owner or None."""
        declined = measurement_hold.record_declined_observation()
        if declined is None:
            return None
        hold_owner, first_decline = declined
        log_event(
            logger,
            "volume.observation_declined",
            source=source,
            owner=hold_owner,
            client=self.address_string(),
            level=logging.INFO if first_decline else logging.DEBUG,
            **fields,
        )
        return hold_owner

    def _refuse_authoritative_write(self, *, kind: str, **fields: Any) -> bool:
        """Refuse a fader write the measurement owns; True once the 409 is sent.

        Its own event and its own per-hold counter: `volume.observation_declined`
        and `declined_observations` are the source-observed vocabulary. The
        first line per hold is INFO and the rest DEBUG for the same reason that
        one is — a slider drag and a spun HID knob repeat.
        """
        refused = measurement_hold.record_refused_write()
        if refused is None:
            return False
        hold_owner, first_refusal, snapshot = refused
        log_event(
            logger,
            "volume.write_refused_measurement_hold",
            kind=kind,
            owner=hold_owner,
            client=self.address_string(),
            level=logging.INFO if first_refusal else logging.DEBUG,
            **fields,
        )
        self._send_json(
            {
                "error": f"a measurement is in progress (owner={hold_owner})",
                "owner": hold_owner,
                "measurement": snapshot,
            },
            status=409,
        )
        return True

    def _post_volume_adjust(self) -> None:
        if self._maybe_forward_pair_action_to_leader():
            return
        blocked = _active_speaker_volume_block()
        if blocked is not None:
            self._send_json(
                {
                    "error": blocked.get("detail") or "speaker output is not ready",
                    "active_speaker_setup": blocked,
                },
                status=409,
            )
            return
        body = self._read_json()
        if "delta_percent" not in body:
            self._send_json(
                {"error": "missing delta_percent"},
                status=400,
            )
            return
        try:
            delta_pct = int(body["delta_percent"])
        except (TypeError, ValueError):
            self._send_json(
                {"error": "delta_percent must be an integer"},
                status=400,
            )
            return
        # Either direction is refused while a measurement holds the fader; see
        # _post_volume_set.
        if measurement_hold.held() and self._refuse_authoritative_write(
            kind="adjust", delta_pct=delta_pct,
        ):
            return
        try:
            state = asyncio.run(self._adjust_op(delta_pct))
        except Exception as e:  # noqa: BLE001
            logger.exception("adjust volume failed")
            self._send_json({"error": str(e)}, status=502)
            return
        log_event(
            logger,
            "volume.adjust",
            delta_pct=delta_pct,
            new_pct=state.effective_percent,
            client=self.address_string(),
        )
        self._send_json(self._volume_payload(state))

    def _post_volume_set(self) -> None:
        if self._maybe_forward_pair_action_to_leader():
            return
        blocked = _active_speaker_volume_block()
        if blocked is not None:
            self._send_json(
                {
                    "error": blocked.get("detail") or "speaker output is not ready",
                    "active_speaker_setup": blocked,
                },
                status=409,
            )
            return
        body = self._read_json()
        # Percent is the canonical listening-level unit. Keep absolute
        # dB as a compatibility input for existing automation clients.
        if "percent" in body:
            try:
                target_pct = int(body["percent"])
            except (TypeError, ValueError):
                self._send_json(
                    {"error": "percent must be an integer"},
                    status=400,
                )
                return
        elif "db" in body:
            try:
                target_pct = db_to_percent(float(body["db"]))
            except (TypeError, ValueError):
                self._send_json(
                    {"error": "db must be a number"},
                    status=400,
                )
                return
        else:
            self._send_json(
                {"error": "missing db or percent"},
                status=400,
            )
            return
        # Optional `source` field marks the caller as an
        # observed source-side change (e.g. host moved its
        # volume slider on the USB gadget). Route through
        # observe_source_volume so the coordinator's echo
        # window and source-active gate apply. Without
        # `source`, the caller is treated as authoritative
        # (management UI, HID accessory, voice "louder", etc.).
        source_name = body.get("source")
        observation_initial = body.get("observation_initial", False)
        if not isinstance(observation_initial, bool):
            self._send_json(
                {"error": "observation_initial must be a boolean"},
                status=400,
            )
            return
        # A live measurement OWNS the fader: it drives camilla's main_volume
        # directly (audio_measurement.ramp) and never writes the persistence
        # file, so the persisted household level says nothing about where the
        # fader actually sits. A write of ANY size can therefore land a level
        # far above the ramp's — the writer war seat-level hit on jts3
        # (journal: `event=volume.reconciled source=idle drift_db=+9.35`, once
        # a second) and a driver taken above its declared cap for the playing
        # stimulus. So every level write is refused for the life of the hold,
        # and MUTE stays open as the emergency door.
        # The two answers differ by contract, not by policy: a SOURCE-OBSERVED
        # write gets the ESTABLISHED `observation_applied: false` 200 that the
        # USB bridge already understands and retries against, while an
        # AUTHORITATIVE one gets /measurement/hold's own 409 envelope.
        if measurement_hold.held():
            if not source_name:
                if self._refuse_authoritative_write(
                    kind="set", requested_pct=target_pct,
                ):
                    return
            else:
                try:
                    state = self._get_op()
                except Exception as e:  # noqa: BLE001
                    # Same shape as _get_volume's guard: this reads the
                    # persisted projection, and a read failure is a 502, not a
                    # silent 200 carrying a half-built payload.
                    logger.exception("declined observation state read failed")
                    self._send_json({"error": str(e)}, status=502)
                    return
                if self._measurement_hold_decline(
                    source=str(source_name), requested_pct=target_pct,
                ) is not None:
                    payload = self._volume_payload(state)
                    payload["observation_applied"] = False
                    self._send_json(payload)
                    return
        observation_applied: bool | None = None
        try:
            if source_name:
                state, observation_applied = asyncio.run(
                    self._observe_op(
                        str(source_name),
                        target_pct,
                        initial=observation_initial,
                    ),
                )
            else:
                state = asyncio.run(self._set_op(target_pct))
        except Exception as e:  # noqa: BLE001
            logger.exception("set volume failed")
            self._send_json({"error": str(e)}, status=502)
            return
        log_event(
            logger,
            "volume.set",
            new_pct=state.effective_percent,
            source=source_name or "authoritative",
            observation_applied=observation_applied,
            client=self.address_string(),
            # A declined observation is a no-op (state unchanged) — an
            # inactive-source host slider can retry for hours, and INFO
            # would spam the journal for something that changed nothing.
            # Every other outcome (authoritative set, applied observation)
            # is a real state change and stays at the default INFO level.
            level=(
                logging.DEBUG if observation_applied is False
                else logging.INFO
            ),
        )
        payload = self._volume_payload(state)
        if observation_applied is not None:
            payload["observation_applied"] = observation_applied
        self._send_json(payload)

    def _post_volume_mute(self) -> None:
        if self._maybe_forward_pair_action_to_leader():
            return
        blocked = _active_speaker_volume_block()
        if blocked is not None:
            self._send_json(
                {
                    "error": blocked.get("detail") or "speaker output is not ready",
                    "active_speaker_setup": blocked,
                },
                status=409,
            )
            return
        # Default is TOGGLE: muted → unmute (restore pre-mute
        # level), unmuted → mute. Used by HID accessory clicks
        # (jasper-input) and other one-shot toggle callers. An
        # optional explicit {"muted": true|false} body sets the
        # state idempotently — the shape voice's distinct
        # mute/unmute intents need (additive; absent = toggle).
        body = self._read_json()
        explicit = body.get("muted")
        if explicit is not None and not isinstance(explicit, bool):
            self._send_json(
                {"error": "muted must be a boolean"},
                status=400,
            )
            return
        # Unmuting restores the household listening level onto the fader the
        # measurement is holding, so it is a level write like any other and is
        # refused (see _post_volume_set). Muting is the emergency door and
        # stays open in both its shapes — explicit and toggle-to-muted.
        if measurement_hold.held():
            resolves_unmuted = explicit is False
            if explicit is None:
                try:
                    state = self._get_op()
                except Exception:  # noqa: BLE001
                    # An unreadable latch must not close the emergency door:
                    # the toggle proceeds as a MUTE, and toggle_mute's own
                    # read under the coordinator's lock decides the direction.
                    logger.exception("mute toggle state read failed")
                    resolves_unmuted = False
                else:
                    # The same latch toggle_mute itself branches on, so the
                    # refusal cannot disagree with what the toggle would do.
                    resolves_unmuted = state.restore_percent is not None
            if resolves_unmuted and self._refuse_authoritative_write(
                kind="unmute", explicit=str(explicit),
            ):
                return
        try:
            if explicit is None:
                state = asyncio.run(self._mute_toggle_op())
            else:
                state = asyncio.run(self._mute_set_op(explicit))
        except Exception as e:  # noqa: BLE001
            logger.exception("mute failed")
            self._send_json({"error": str(e)}, status=502)
            return
        log_event(
            logger,
            "volume.mute",
            new_pct=state.effective_percent,
            explicit=str(explicit),
            client=self.address_string(),
        )
        self._send_json(self._volume_payload(state))
        return

    def _post_transport(self) -> None:
        # Bonded-follower: transport targets the PAIR. A remote paired
        # to the follower sends play/pause here; with the local
        # renderer stack parked (dumb-follower profile) the local
        # mux has nothing to toggle — the leader owns playback, so
        # the request forwards exactly like /volume*.
        if self._maybe_forward_pair_action_to_leader():
            return
        action = self.path.rsplit("/", 1)[1]  # toggle | next | previous
        try:
            result = asyncio.run(_dispatch_transport(action))
        except Exception as e:  # noqa: BLE001
            logger.exception("transport %s failed", action)
            self._send_json({"error": str(e)}, status=502)
            return
        log_event(
            logger,
            "transport.dispatch",
            action=action,
            client=self.address_string(),
        )
        if "error" in result:
            self._send_json(result, status=502)
            return
        self._send_json(result)
        return

    def _post_source_select(self) -> None:
        # POST /source/select body: {"source": "airplay"} or
        # {"source": "auto"}. The mux validates policy and
        # forwards the low-level lane choice to fan-in.
        if self._maybe_forward_pair_action_to_leader():
            return
        body = self._read_json()
        source = str(body.get("source") or "").strip().lower()
        if source == "auto":
            cmd = wire.MUX_AUTO
        elif source in SOURCE_SELECT_IDS:
            cmd = wire.mux_select(source)
        else:
            choices = ", ".join(sorted(SOURCE_SELECT_IDS))
            self._send_json(
                {
                    "error": (f"source must be {choices}, or auto"),
                },
                status=400,
            )
            return
        result = self._mux_cmd_or_error(cmd, timeout=6.0, log_label="source select")
        if result is None:
            return
        log_event(
            logger,
            "source.select",
            source=source,
            client=self.address_string(),
        )
        self._send_json(_augment_source_payload(result))
        return
