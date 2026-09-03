# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP routes for the voice control-plane concern."""

from __future__ import annotations

from .. import server as _server
from ._base import ControlHandlerMixin


class VoiceRoutes(ControlHandlerMixin):
    def _get_mic(self) -> None:
        # Read mic mute state from the voice daemon's STATUS
        # response. A bonded follower intentionally parks local
        # voice, and a daemon restart temporarily lacks its UDS socket;
        # report both as first-class states instead of making every
        # client reinterpret a missing UDS as failure.
        leader = _server._pair_follower_leader_addr()
        if leader:
            self._send_json(_server._bonded_follower_mic_payload(leader))
            return
        try:
            st = _server.asyncio.run(
                _server._voice_socket_command(
                    self._voice_socket_path,
                    "STATUS",
                    timeout=2.0,
                ),
            )
        except (FileNotFoundError, OSError, _server.asyncio.TimeoutError) as e:
            starting = _server._voice_starting_mic_payload()
            if starting is not None:
                self._send_json(starting)
                return
            self._send_json(
                _server._voice_offline_mic_payload(f"voice_daemon unreachable: {e}"),
                status=503,
            )
            return
        except (RuntimeError, _server.json.JSONDecodeError, UnicodeDecodeError) as e:
            _server.logger.exception("mic STATUS failed")
            self._send_json({"error": str(e)}, status=502)
            return
        muted = bool(st.get("mic_muted", False))
        self._send_json(
            {
                "status": "muted" if muted else "listening",
                "available": True,
                "muted": muted,
            }
        )

    def _post_session(self) -> None:
        cmd = "START" if self.path.endswith("start") else "END"
        if cmd == "START":
            payload = self._read_json()
            source = payload.get("source")
            if source is not None:
                if (
                    not isinstance(source, str)
                    or not source.strip()
                    or any(ch.isspace() for ch in source)
                ):
                    self._send_json(
                        {"error": "source must be a non-empty token"},
                        status=400,
                    )
                    return
                try:
                    source.encode("ascii")
                except UnicodeEncodeError:
                    self._send_json(
                        {"error": "source must be ASCII"},
                        status=400,
                    )
                    return
                cmd = f"START {source.strip()}"
        result = self._voice_cmd_or_error(
            cmd,
            missing_error="voice_daemon not running (socket not found)",
            log_label=f"session {cmd}",
            refusal_event="session.manual_refused" if cmd.startswith("START") else None,
        )
        if result is None:
            return
        # Result codes from voice_daemon's manual_session_*:
        #   OK / BUSY / CAP / PAUSED / MUTED / MEASURING /
        #   NO_SESSION / ALREADY_ENDED / UNKNOWN_SOURCE / NO_ROOM_MIC / ERROR
        # Map non-OK outcomes to non-2xx so remote and automation
        # callers receive an actionable status.
        http_status = 200
        if result.get("result") not in ("OK", "ALREADY_ENDED", None):
            if result.get("result") in ("CAP", "PAUSED", "MUTED", "MEASURING"):
                http_status = 503
            elif result.get("result") in ("BUSY", "NO_SESSION"):
                http_status = 409
            # Both are bad REQUESTS, not transient states: the caller named a
            # source this speaker does not have, or named none on a speaker
            # whose only input is a push-to-talk source. Retrying either
            # unchanged can never succeed, so 400 rather than 503.
            elif result.get("result") in ("UNKNOWN_SOURCE", "NO_ROOM_MIC"):
                http_status = 400
            else:
                http_status = 502
        self._send_json(result, status=http_status)
        return

    def _post_cue_play(self) -> None:
        # POST /cue/play  body: {"slug": "<cue_slug>"}
        # Routes the request through voice_daemon's control
        # socket so the cue plays through the daemon's
        # already-correctly-gained TtsPlayout. A separate
        # standalone client (e.g., `jasper-cues play <slug>`)
        # would have to recreate the daemon's volume math
        # to match levels, and got it wrong (~20 dB too
        # loud). Centralising here keeps levels consistent.
        body = self._read_json()
        slug = (body.get("slug") or "").strip()
        if not slug:
            self._send_json(
                {"error": "missing 'slug' in body"},
                status=400,
            )
            return
        # Cues run ~5-6s of audio plus duck/restore plus drain.
        # 30s gives generous headroom even for the longest reasonable cue.
        result = self._voice_cmd_or_error(
            f"CUE_PLAY {slug}",
            timeout=30.0,
            missing_error="voice_daemon not running",
            log_label="cue play",
        )
        if result is None:
            return
        http_status = 200
        if result.get("result") == "missing_slug":
            http_status = 400
        elif result.get("result") == "unknown_slug":
            http_status = 404
        elif result.get("result") == "cues_not_configured":
            http_status = 503
        elif result.get("result") == "busy":
            http_status = 409
        elif result.get("result") != "ok":
            http_status = 502
        self._send_json(result, status=http_status)
        return

    def _post_mic_mute(self) -> None:
        # POST /mic/mute  body: {"muted": bool}
        # Idempotent set. Forwards MUTE or UNMUTE to the voice
        # daemon's control socket, which drops mic frames at
        # the wake-loop gate (mute) or resumes (unmute) and
        # plays a short click on either edge for feedback.
        leader = _server._pair_follower_leader_addr()
        if leader:
            payload = _server._bonded_follower_mic_payload(leader)
            self._send_json({**payload, "error": payload["message"]}, status=409)
            return
        body = self._read_json()
        if "muted" not in body:
            self._send_json(
                {"error": "missing 'muted' in body"},
                status=400,
            )
            return
        cmd = "MUTE" if bool(body["muted"]) else "UNMUTE"
        result = self._voice_cmd_or_error(
            cmd,
            timeout=3.0,
            missing_error="voice_daemon not running",
            log_label=f"mic {cmd}",
        )
        if result is None:
            return
        _server.log_event(
            _server.logger,
            "mic.set",
            muted=bool(body["muted"]),
            client=self.address_string(),
        )
        # Read back the truth from the daemon. STATUS is cheap
        # and the daemon's flag is authoritative.
        try:
            st = _server.asyncio.run(
                _server._voice_socket_command(
                    self._voice_socket_path,
                    "STATUS",
                    timeout=2.0,
                )
            )
            muted_now = bool(st.get("mic_muted", False))
        except Exception:  # noqa: BLE001
            # If readback fails, trust the set and move on.
            muted_now = bool(body["muted"])
        self._send_json({"muted": muted_now, "result": result.get("result")})
        return
