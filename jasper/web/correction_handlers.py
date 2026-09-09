# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Room-correction wizard: the route bodies.

One ``_handle_*`` function per route the wizard serves, plus the helpers only
they use. Each takes the live ``BaseHTTPRequestHandler`` and returns the JSON
payload (or ``(payload, status)``); the routes table and the handler class
that dispatch them live in :mod:`jasper.web.correction_setup`, and the
session/capture state they act on lives in
:mod:`jasper.web.correction_capture`.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import re
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from jasper.audio_measurement.calibration import configured_calibration_root
from jasper.camilla import CamillaUnavailable
from jasper.active_speaker.crossover_v2.composition import confirm_graph_is_live
from jasper.active_speaker.crossover_v2.volume_claim import OwnerVolumeDoor
from jasper.active_speaker.commissioning_admission import running_graph_fingerprint
from jasper.correction.status import _MEASUREMENT_FILENAME_RE, _SOUND_FILENAME_RE
from jasper.dsp_apply import config_file_sha256, last_dsp_apply_state, same_config_file
from jasper.active_speaker.restore_wait import resilient_restore
from jasper.active_speaker.session_volume_plan import RestoreOutcome
from jasper.volume_coordinator import env_canonical_target_db
from jasper.volume_owner import volume_owner

from ..log_event import log_event
from ..platform.systemd import no_hold

from . import correction_capture
from .correction_capture import (
    BadRequest,
    CaptureKind,
    MAX_CALIBRATION_UPLOAD_JSON_BYTES,
    MAX_WAV_BODY_BYTES,
    REQUIRED_SAMPLE_RATE,
    RequestConflict,
    _BUNDLE_DELETE_BLOCKED_STATES,
    _session_lock,
    logger,
)


def _handle_start(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /start: snapshot the current DSP graph, load a measurement
    baseline with room/preference layers stripped, replace the session, and
    ask the browser for pre-sweep room-noise capture. The sweep starts only
    after `POST /upload-noise` lands.

    Body fields:
      - total_positions: supported household count; defaults to the
        session-owned six-position policy.
      - target_choice:   one registered Room target; defaults to flat.
      - strategy_choice: 'safe' | 'balanced' on the household surface.
      - noise_floor_db:  float | None — optional, client autolevel
        preflight measurement; only saved into the debug bundle.
      - repeat_main_position: when present, must agree with the session-owned
        automatic same-seat trust repeat.

    Why strip layers before sweeping: if a correction or preference EQ is
    loaded, the sweep traverses that layer and the resulting curve reflects
    the user's taste or the old correction, not the raw room. The carrier
    keeps the topology-owned speaker graph (crossovers, driver EQ, delays,
    gains, limiters) and strips only Layer B/C.
    """
    from jasper.correction.session import (
        DEFAULT_REPEAT_MAIN_POSITION,
        DEFAULT_ROOM_POSITION_COUNT,
        ROOM_POSITION_COUNT_CHOICES,
        SessionState,
    )
    from jasper.correction.strategy import (
        DEFAULT_CORRECTION_STRATEGY_ID,
        DEFAULT_TARGET_PROFILE_ID,
        HOUSEHOLD_CORRECTION_STRATEGY_IDS,
        TARGET_PROFILES,
    )
    readiness = correction_capture._room_readiness()
    if not readiness.allowed:
        # Ruling S10: an unproven or stale speaker decision is a loud
        # disclosure, never a stop. This used to raise 409/503 here, so a
        # metadata edit or an unminted receipt could keep the household from
        # measuring a speaker that was playing fine. What still holds is the
        # other half — the run may not CLAIM the authority it could not read:
        # `readiness.authority_binding` carries the un-vouched answer forward,
        # and `_room_readiness().blocker` keeps surfacing on the idle screen
        # and in `/envelope` for as long as it is true.
        log_event(
            logger,
            "correction.start_unproven_speaker_readiness",
            reason=readiness.reason,
            code=str((readiness.blocker or {}).get("code") or ""),
            level=logging.WARNING,
        )
    authority_binding = readiness.authority_binding

    body = correction_capture._read_json_body(handler)
    blocking_state = correction_capture._reserve_start_slot()
    if blocking_state is not None:
        log_event(
            logger,
            "correction.start_rejected",
            reason="active_session",
            state=blocking_state,
            level=logging.WARNING,
        )
        raise RequestConflict(
            "measurement already in progress; wait for the current sweep "
            "or reset before starting again"
        )

    try:
        total_raw = body.get("total_positions", DEFAULT_ROOM_POSITION_COUNT)
        if not isinstance(total_raw, int) or isinstance(total_raw, bool):
            raise ValueError("total_positions must be a supported count")
        total_positions = total_raw
        if total_positions not in ROOM_POSITION_COUNT_CHOICES:
            raise ValueError("total_positions must be a supported count")
        target_choice = str(
            body.get("target_choice", DEFAULT_TARGET_PROFILE_ID)
        )
        if target_choice not in TARGET_PROFILES:
            raise ValueError("target_choice must be a registered Room target")
        strategy_choice = str(
            body.get("strategy_choice", DEFAULT_CORRECTION_STRATEGY_ID)
        )
        if strategy_choice not in HOUSEHOLD_CORRECTION_STRATEGY_IDS:
            raise ValueError(
                "strategy_choice must be an authorized household strategy"
            )
        noise_floor_db_raw = body.get("noise_floor_db")
        calibration_id = str(body.get("calibration_id") or "").strip()
        input_device = correction_capture._sanitize_input_device(body.get("input_device"))
        repeat_raw = body.get(
            "repeat_main_position",
            DEFAULT_REPEAT_MAIN_POSITION,
        )
        if repeat_raw is not DEFAULT_REPEAT_MAIN_POSITION:
            raise ValueError(
                "repeat_main_position must use the automatic trust check"
            )
        repeat_main_position = DEFAULT_REPEAT_MAIN_POSITION
        noise_floor_db: float | None
        try:
            noise_floor_db = (
                float(noise_floor_db_raw)
                if noise_floor_db_raw is not None
                else None
            )
        except (TypeError, ValueError):
            noise_floor_db = None

        mic_calibration = None
        if calibration_id:
            from jasper.audio_measurement.calibration import load_calibration_record
            mic_calibration = load_calibration_record(
                calibration_id,
                root=configured_calibration_root(),
            )

        mismatch = correction_capture._calibration_device_mismatch(mic_calibration, input_device)
        if mismatch is not None:
            log_event(
                logger,
                "correction.start_rejected",
                reason="calibration_device_mismatch",
                provider=getattr(mic_calibration, "provider", ""),
                level=logging.WARNING,
            )
            raise ValueError(mismatch)

        from jasper.correction import browser_audio

        browser_report = browser_audio.assess_browser_audio_path(
            input_device=input_device,
            expected_sample_rate=REQUIRED_SAMPLE_RATE,
            has_mic_calibration=mic_calibration is not None,
        ).to_dict()
        if browser_report.get("failed") is True:
            issue_codes = [
                issue.get("code")
                for issue in browser_report.get("issues", [])
                if isinstance(issue, dict) and issue.get("severity") == "fail"
            ]
            log_event(
                logger,
                "correction.start_rejected",
                reason="browser_audio_path_failed",
                issue_codes=",".join(
                    str(code) for code in issue_codes if code
                ),
                level=logging.WARNING,
            )
            raise ValueError(
                browser_report.get("summary")
                or "browser audio path is not safe for measurement"
            )

        cam = correction_capture._camilla()
        prior_session = correction_capture._get_or_create_session()
        if (getattr(prior_session, "startup_recovery", None) or {}).get("required"):
            correction_capture._run_graph_mutation(
                recover_room_startup_state(prior_session, cam),
            )
            if prior_session.startup_recovery["required"]:
                raise RequestConflict("Room recovery is incomplete; retry Reset")
        if not correction_capture._run_async(
            prior_session._restore_listening_volume_if_ramped(), timeout=5.0,
        ):
            raise RequestConflict("speaker volume could not be restored; retry Reset")
        correction_capture._run_async(
            prior_session.restore_level_match_volume(correction_capture._household_level_door()),
            timeout=5.0,
        )
        sess = correction_capture._replace_session(
            total_positions=total_positions,
            target_choice=target_choice,
            strategy_choice=strategy_choice,
            mic_calibration=mic_calibration,
            input_device=input_device,
            repeat_main_position=repeat_main_position,
        )
        sess.noise_floor_db = noise_floor_db
        sess.room_authority_binding = authority_binding

        # A second copy of the browser-audio refusal used to sit here, re-reading
        # ``sess.browser_audio_report``. It could not fire: MeasurementSession
        # builds that report by calling the same pure
        # ``browser_audio.assess_browser_audio_path`` with the same three inputs
        # -- this ``input_device``, ``mic_calibration is not None``, and
        # ``SessionConfig.sample_rate`` (48000), which equals
        # ``REQUIRED_SAMPLE_RATE`` -- and neither input is reassigned between
        # the two points. So it was always the verdict the block above had
        # already raised on.

        from jasper.correction.runtime_safety import CorrectionRuntimeSafetyError
        from jasper.sound.graph_carrier import CarrierCannotHostEq

        try:
            baseline_payload = correction_capture._run_graph_mutation(
                _load_measurement_baseline(
                    sess,
                    cam,
                    expected_authority_binding=authority_binding,
                ),
            )
        except CarrierCannotHostEq:
            logger.warning("/start: measurement baseline rejected by graph carrier")
            raise
        except CorrectionRuntimeSafetyError:
            # It subclasses RuntimeError, so the arm below would re-raise it as
            # a BARE RuntimeError and the dispatcher's typed arm would never
            # see it — an unsafe graph would reach the household as an untyped
            # 500. Matters more now that `/start` no longer refuses ahead of
            # this point: this is the surface that answers for an unready
            # speaker, so it has to keep its type.
            logger.warning("/start: measurement baseline refused as unsafe")
            raise
        except RuntimeError as exc:
            logger.exception("/start: measurement baseline load rejected")
            raise RuntimeError(str(exc)) from None
        except Exception:  # noqa: BLE001
            logger.exception("/start: measurement baseline load failed")
            raise RuntimeError(
                "could not load speaker measurement baseline before measuring"
            ) from None
        sess.current_correction_at_start = baseline_payload.get(
            "current_correction_at_start"
        )

        try:
            correction_capture._run_async(sess.begin_noise_capture(), timeout=3.0)
            state_started = sess.state == SessionState.NEEDS_NOISE_CAPTURE
        except concurrent.futures.TimeoutError:
            state_started = False

        if state_started:
            # Browser permission + device selection are human-paced. The
            # ordinary upload watchdog resumes when the first noise upload
            # actually begins, after setup and level matching are done.
            sess.suspend_capture_timeout()
            correction_capture._clear_start_slot()
        else:
            correction_capture._clear_start_slot()
            log_event(
                logger,
                "correction.start_state_wait_timeout",
                session=sess.session_id,
                level=logging.WARNING,
            )

        snapshot = sess.snapshot()
        return {
            "session_id": sess.session_id,
            "state": sess.state.value,
            "total_positions": sess.total_positions,
            "target_choice": sess.target_choice,
            "strategy_choice": sess.strategy_choice,
            "target_profile": snapshot.get("target_profile"),
            "correction_strategy": snapshot.get("correction_strategy"),
            "input_device": sess.input_device,
            "browser_audio_report": sess.browser_audio_report,
            "mic_calibration": (
                sess.mic_calibration.public_metadata()
                if sess.mic_calibration
                else None
            ),
            "current_correction_at_start": sess.current_correction_at_start,
            "measurement_config_path": baseline_payload.get(
                "measurement_config_path"
            ),
        }
    except Exception:  # noqa: BLE001
        correction_capture._clear_start_slot()
        raise


def _room_graph_artifact_path(sess: Any, label: str) -> Path:
    """Return a collision-free managed config path for one Room transaction."""

    cfg = getattr(sess, "cfg", None)
    config_dir = Path(
        getattr(cfg, "config_dir", None)
        or "/var/lib/camilladsp/configs"
    )
    token = re.sub(
        r"[^A-Za-z0-9]",
        "",
        str(getattr(sess, "session_id", "session")),
    ) or "session"
    return config_dir / f"sound_{label}_{token}_{time.time_ns()}.yml"


def _running_graph_snapshot_text(
    raw: str,
    current_path: str | Path,
    *,
    carrier: Any | None = None,
) -> str:
    """Make Camilla's comment-free active_raw reloadable with provenance.

    CamillaDSP's active_raw is the graph-content authority but drops YAML
    comments. Preserve only the bounded JTS ``# Source:`` marker from the
    durable path so the graph carrier can distinguish a safe Active baseline
    from transient commissioning graphs. All executable graph content remains
    the fresh Camilla readback.
    """

    source_line = None
    try:
        for line in Path(current_path).read_text(encoding="utf-8").splitlines():
            if line.startswith("# Source: ") and len(line) <= 256:
                source_line = line
                break
    except OSError:
        pass
    # PR #1009's one-time recovery shape is a protected active-leader pipe
    # graph stamped with the generic sound marker. Resolve it while the
    # original durable name is still available; the collision-free snapshot
    # name intentionally cannot trigger that filename-scoped compatibility
    # rule later.
    if carrier is None:
        from jasper.sound.graph_carrier import carrier_for_loaded_config

        carrier = carrier_for_loaded_config(
            current_path,
            config_dir=Path(current_path).parent,
        )
    if carrier.kind == "active_leader_program_bake":
        from jasper.active_speaker.camilla_yaml import ACTIVE_PROGRAM_BAKE_SOURCE

        source_line = f"# Source: {ACTIVE_PROGRAM_BAKE_SOURCE}"
    text = raw.rstrip() + "\n"
    if source_line:
        body = "\n".join(
            line for line in text.splitlines()
            if not line.startswith("# Source: ")
        )
        return f"{source_line}\n{body.rstrip()}\n"
    return text


def _running_graph_body(text: str) -> str:
    """Executable snapshot body, excluding the one JTS provenance comment."""

    return "\n".join(
        line for line in text.splitlines()
        if not line.startswith("# Source: ")
    ).strip()


async def _snapshot_running_room_graph(
    sess: Any,
    cam: Any,
    *,
    current_path: str | Path | None = None,
    bass_profile_summary: Mapping[str, Any] | None = None,
    snapshot_path: Path | None = None,
) -> tuple[Path, Path, Mapping[str, Any]]:
    """Persist one validated, content-stable copy of Camilla's running graph."""

    from jasper.atomic_io import atomic_write_text
    from jasper.correction.runtime_safety import assert_correction_graph_safe
    from jasper.dsp_apply import validate_camilla_config
    from jasper.sound.graph_carrier import (
        CarrierCannotHostEq,
        carrier_for_loaded_config,
    )

    current = current_path or await cam.get_config_file_path(best_effort=False)
    if not current:
        raise RuntimeError("CamillaDSP did not report a loaded config path")
    carrier = carrier_for_loaded_config(
        current,
        config_dir=Path(current).parent,
    )
    if carrier.kind == "unknown":
        raise CarrierCannotHostEq(
            "unknown_config",
            "CamillaDSP is running a configuration JTS didn't generate, so "
            "Room cannot preserve it for exact restoration.",
        )
    if bass_profile_summary is None:
        live_authority = await correction_capture._classify_live_bass_extension_graph(cam)
        bass_profile_summary = live_authority.details[
            "bass_extension_profile_summary"
        ]
    raw = await cam.get_active_config_raw(best_effort=False)
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError("CamillaDSP did not report a running graph")
    text = _running_graph_snapshot_text(raw, current, carrier=carrier)
    assert_correction_graph_safe(
        text,
        bass_profile_summary=bass_profile_summary,
    )
    snapshot = snapshot_path or _room_graph_artifact_path(sess, "snapshot")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        snapshot,
        text,
        mode=0o640,
        durable=True,
    )
    validation = validate_camilla_config(snapshot)
    if not validation.ok_to_apply:
        snapshot.unlink(missing_ok=True)
        raise RuntimeError(
            "CamillaDSP's running graph could not be validated for exact "
            f"restoration: {validation.error or validation.status.value}"
        )
    return Path(current), snapshot, bass_profile_summary


async def _load_measurement_baseline(
    sess: Any,
    cam: Any,
    *,
    expected_authority_binding: tuple[bool | None, str | None, str | None],
) -> dict[str, Any]:
    """Load a topology-preserving measurement graph for this correction run.

    The graph carrier is the single bridge between "whatever CamillaDSP is
    running" and "emit the same speaker topology with different program-domain
    layers." Passing ``room_peqs=[]`` and ``SoundProfile(enabled=False)`` strips
    old room correction and preference EQ while keeping crossovers/protection.
    """

    from jasper.correction.runtime_safety import (
        CorrectionRuntimeSafetyError,
        assert_correction_graph_safe,
    )
    from jasper.dsp_apply import DspApplyError, apply_dsp_config
    from jasper.correction.status import describe_current_config
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env
    from jasper.sound.graph_carrier import (
        CarrierCannotHostEq,
        carrier_for_loaded_config,
    )
    from jasper.sound.profile import SoundProfile

    sess.cfg.config_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = _room_graph_artifact_path(sess, "snapshot")
    out_path = snapshot_path.with_name(
        snapshot_path.name.replace("sound_snapshot_", "correction_measurement_", 1)
    )
    previous_apply = last_dsp_apply_state()
    # The measurement graph must capture the SAME program tap fan-in is feeding,
    # else under shm_ring it would measure a dead loopback. Thread the coupling.
    coupling_capture_kwargs = coupling_capture_kwargs_from_env()

    async def _prepare_config() -> dict[str, Any]:
        # apply_dsp_config invokes prepare while /start owns the shared
        # DSP-writer lock. Re-read Active's decision here so the graph being
        # re-emitted cannot rely on a Layer-A sample taken before reservation.
        bass_profile_summary = await correction_capture._assert_room_authority_current(
            cam,
            expected_authority_binding,
        )
        anchor = await cam.get_config_file_path(best_effort=False)
        if not anchor:
            raise RuntimeError("CamillaDSP did not report a loaded config path")
        predecessor = await _pre_measurement_restore_target(
            sess, cam, current_path=anchor, previous_apply=previous_apply,
        )
        if predecessor is not None:
            if not await cam.set_config_file_path(str(predecessor), best_effort=False):
                raise RuntimeError("Room predecessor could not be restored")
            await confirm_graph_is_live(cam, predecessor.read_text(encoding="utf-8"))
            anchor = str(predecessor)
        _, restore_path, _ = await _snapshot_running_room_graph(
            sess,
            cam,
            current_path=anchor,
            bass_profile_summary=bass_profile_summary,
            snapshot_path=snapshot_path,
        )
        carrier = carrier_for_loaded_config(
            restore_path,
            config_dir=sess.cfg.config_dir,
        )
        result = carrier.reemit(
            SoundProfile(enabled=False),
            room_peqs=[],
            out_path=out_path,
            profile_id=f"measurement-{sess.session_id}",
            fanin_coupling_capture_kwargs=coupling_capture_kwargs,
        )
        assert_correction_graph_safe(
            result.yaml,
            bass_profile_summary=bass_profile_summary,
        )
        sess.pre_measurement_config_path = Path(anchor)
        sess.pre_measurement_restore_path = restore_path
        return {
            # apply_dsp_config must roll back to immutable graph content, not
            # the mutable durable filename Camilla happened to report.
            "prior_config_path": str(restore_path),
            "room_peq_count": result.room_peq_count,
            "sound_filter_count": 0,
        }

    try:
        state = await apply_dsp_config(
            source="correction_measurement",
            candidate_path=out_path,
            load_config=lambda path: cam.set_config_file_path(
                path,
                best_effort=False,
            ),
            get_current_config_path=lambda: cam.get_config_file_path(
                best_effort=True,
            ),
            prepare=_prepare_config,
            room_peq_count=0,
            sound_filter_count=0,
        )
    except DspApplyError as exc:
        if isinstance(
            exc.__cause__,
            (CarrierCannotHostEq, CorrectionRuntimeSafetyError),
        ):
            raise exc.__cause__ from exc
        raise
    sess.measurement_config_path = out_path
    descriptor = describe_current_config(
        sess.pre_measurement_restore_path,
        config_dir=sess.cfg.config_dir,
        base_config_path=sess.cfg.base_config_path,
    )
    log_event(
        logger,
        "correction.measurement_baseline_loaded",
        session=sess.session_id,
        prior=str(sess.pre_measurement_config_path),
        restore=str(sess.pre_measurement_restore_path),
        candidate=str(out_path),
        op_id=state.op_id,
    )
    return {
        "current_correction_at_start": descriptor,
        "measurement_config_path": str(out_path),
        "prior_config_path": str(sess.pre_measurement_config_path),
        "restore_config_path": str(sess.pre_measurement_restore_path),
        "last_dsp_apply": state.to_dict(),
    }


def _handle_next_position(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /next-position: request pre-sweep noise for the next
    multi-position measurement. Only valid in NEEDS_NEXT_POSITION
    state.

    The sweep itself starts after the browser uploads
    `noise/p<N>_pre.wav` to `/upload-noise`.
    """
    from jasper.correction.session import SessionState

    sess = correction_capture._get_or_create_session()
    if sess.state != SessionState.NEEDS_NEXT_POSITION:
        raise RuntimeError(
            f"cannot advance to next position from state {sess.state.value}"
        )

    correction_capture._run_async(sess.begin_noise_capture(), timeout=3.0)

    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
    }


def _handle_verify(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /verify: re-measure after Apply to see the actual effect
    of the correction. One-position only; result lands in
    verify_curve / verify_metrics. Same stale-state-avoidance wait
    as /next-position."""
    from jasper.correction import playback
    from jasper.measurement_window import measurement_window
    from jasper.correction.session import SessionState

    sess = correction_capture._get_or_create_session()
    cam = correction_capture._camilla()

    async def _run_verify_sweep() -> None:
        async def _runtime_probe() -> dict[str, Any] | None:
            return await cam.get_runtime_status(best_effort=True)

        try:
            async with measurement_window():
                await sess.start_verify_sweep(
                    playback.play_sweep,
                    runtime_probe_async=_runtime_probe,
                )
        except Exception as e:  # noqa: BLE001
            logger.exception("verify sweep failed: %s", e)

    asyncio.run_coroutine_threadsafe(
        correction_capture._run_session_background_audio(sess, _run_verify_sweep),
        correction_capture._ensure_loop(),
    )

    correction_capture._run_async(
        sess.state_changed_from(
            {SessionState.APPLIED, SessionState.VERIFIED},
        ),
        timeout=6.0,
    )

    return {"session_id": sess.session_id, "state": sess.state.value}


def _wait_for_new_autolevel_run(
    sess: Any,
    previous_data: Any,
    future: Any,
    *,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Wait for ``run()`` to replace terminal/idle autolevel data."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        current = sess.autolevel
        if current is not previous_data:
            return current.snapshot()
        if future.done():
            break
        time.sleep(0.05)
    try:
        correction_capture._run_async(sess.cancel_autolevel(), timeout=1.0)
    except Exception:  # noqa: BLE001
        logger.warning("could not cancel a stalled autolevel start", exc_info=True)
    raise RequestConflict("the measurement level check could not start")


def _handle_autolevel_start(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /autolevel/start: ramp CamillaDSP main_volume upward
    while a continuous 1 kHz tone plays, until the iPhone client
    POSTs to /autolevel/lock (or the ramp tops out and we report
    `maxed_out`).

    Client behavior:
      1. POST /autolevel/start (kicks off the background task).
      2. Watch the live mic-level meter via AudioWorklet.
      3. When the captured mic RMS lands in the target range
         (computed by the browser from the pre-sweep noise floor),
         POST /autolevel/lock.
      4. Poll GET /status; `autolevel.status` becomes `locked`,
         `maxed_out`, `cancelled`, or `error`.
    """
    from jasper.correction import playback
    from jasper.measurement_window import measurement_window
    from jasper.correction.session import AutolevelStatus, SessionState

    sess = correction_capture._get_or_create_session()
    if (
        sess.state != SessionState.NEEDS_NOISE_CAPTURE
        or not bool(getattr(sess, "local_capture_setup_bound", False))
    ):
        raise RequestConflict(
            "microphone setup must be complete before level matching"
        )
    retryable_statuses = {
        AutolevelStatus.IDLE,
        AutolevelStatus.CANCELLED,
        AutolevelStatus.ERROR,
        AutolevelStatus.MAXED_OUT,
    }
    if sess.autolevel.status not in retryable_statuses:
        raise RequestConflict(
            "the measurement level is already locked or still running"
        )
    previous_data = sess.autolevel

    cam = correction_capture._camilla()
    from jasper.volume_owner import ClaimKind, volume_owner

    owner = volume_owner()
    if owner is None:
        log_event(
            logger,
            "correction.autolevel_owner_absent",
            level=logging.CRITICAL,
        )
        raise RequestConflict("the speaker volume owner is not available")

    async def _run_autolevel() -> None:
        claim = None

        async def _get_vol() -> float:
            value = await cam.get_volume_db(best_effort=False)
            if value is None:
                raise RuntimeError("speaker volume is unavailable")
            return float(value)

        async def _set_vol(db: float) -> None:
            async def _move_claim() -> None:
                nonlocal claim
                if claim is None:
                    claim = await owner.acquire_level(ClaimKind.SESSION_MEASUREMENT, db)
                else:
                    claim = await owner.relevel(claim, db)

            # Cancellation must not lose the replacement handle after the owner
            # moves its ledger; cleanup needs that exact handle to release it.
            await resilient_restore(_move_claim())

        async def _restore_vol(db: float) -> bool:
            async def _release_and_restore() -> bool:
                nonlocal claim
                if claim is not None:
                    await owner.release(claim, household_level_db=db)
                    claim = None
                door = OwnerVolumeDoor(owner, read_fader=_get_vol)
                return await door.restore_household_level_db(db) is RestoreOutcome.LANDED

            return await resilient_restore(_release_and_restore())

        try:
            async with measurement_window():
                tone_wav = playback._ensure_tone_wav(
                    freq_hz=1000.0,
                    duration_s=15.0,
                    dbfs=-12.0,
                    sample_rate=48000,
                )
                player = playback.TonePlayer(tone_wav)
                await sess.run_autolevel(
                    reservation_token=reserved,
                    get_main_volume_db=_get_vol,
                    set_main_volume_db=_set_vol,
                    restore_main_volume_db=_restore_vol,
                    play_continuous_tone=player.play,
                    cancel_tone=player.cancel,
                )
        except asyncio.CancelledError:
            sess.autolevel.status = AutolevelStatus.CANCELLED
            raise
        except Exception as exc:  # noqa: BLE001
            sess.autolevel.status = AutolevelStatus.ERROR
            sess.autolevel.error = type(exc).__name__
            logger.exception("autolevel run failed")
        finally:
            try:
                if sess.autolevel.status is not AutolevelStatus.LOCKED:
                    await resilient_restore(sess._restore_listening_volume_if_ramped())
            finally:
                await sess.release_autolevel_run_reservation(reserved)

    reserved = correction_capture._run_async(sess.reserve_autolevel_run(), timeout=2.0)
    if not reserved:
        raise RequestConflict("the measurement level check is already running")
    try:
        future = asyncio.run_coroutine_threadsafe(
            _run_autolevel(), correction_capture._ensure_loop()
        )
    except RuntimeError:
        correction_capture._run_async(
            sess.release_autolevel_run_reservation(reserved),
            timeout=2.0,
        )
        raise
    started = _wait_for_new_autolevel_run(sess, previous_data, future)

    return {"started": True, "autolevel": started}


def _handle_autolevel_lock(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /autolevel/lock: signal the autolevel task to stop
    ramping and freeze main_volume at its current value. The
    locked level is what subsequent sweeps will play through."""
    sess = correction_capture._get_or_create_session()
    fired = correction_capture._run_async(sess.lock_autolevel(), timeout=2.0)
    return {"locked": bool(fired), "autolevel": sess.autolevel.snapshot()}


def _handle_autolevel_cancel(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /autolevel/cancel: abort the autolevel run and restore
    main_volume to whatever it was before the ramp started."""
    sess = correction_capture._get_or_create_session()
    fired = correction_capture._run_async(sess.cancel_autolevel(), timeout=2.0)
    snapshot = sess.autolevel.snapshot()
    return {
        "cancel_requested": bool(fired),
        "cancelled": snapshot["status"] == "cancelled",
        "autolevel": snapshot,
    }


def _handle_test_tone(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /test-tone: play a 5-second 1 kHz sine through the music
    chain so the user can adjust their amp's volume by watching the
    live mic level meter. Pauses renderers + voice loop for the tone
    duration via the same measurement_window the sweep uses.

    Synchronous-feeling from the browser's POV (it returns once the
    tone has finished playing) so the polling state machine doesn't
    have to track a "test tone in progress" sub-state.
    """
    from jasper.correction import playback
    from jasper.measurement_window import measurement_window

    body = correction_capture._read_json_body(handler)
    duration_s = max(1.0, min(15.0, float(body.get("duration_s", 5.0))))

    async def _run_test_tone() -> None:
        async with measurement_window():
            await playback.play_test_tone(duration_s=duration_s)

    correction_capture._run_async(_run_test_tone(), timeout=duration_s + 30.0)
    return {"played": True, "duration_s": duration_s}


def _handle_calibration_models(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import SUPPORTED_MODELS
    return {
        "models": [
            {"key": key, **value}
            for key, value in SUPPORTED_MODELS.items()
        ]
    }


def _handle_calibration_fetch(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import fetch_vendor_calibration

    body = correction_capture._read_json_body(handler)
    model = str(body.get("model") or "").strip()
    serial = str(body.get("serial") or "").strip()
    orientation = str(body.get("orientation") or "unknown").strip() or "unknown"
    record = fetch_vendor_calibration(
        model_key=model,
        serial=serial,
        orientation=orientation,
        root=configured_calibration_root(),
    )
    correction_capture._save_household_mic(record, serial=serial)
    return correction_capture._calibration_payload(record)


def _handle_calibration_upload(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    from jasper.audio_measurement.calibration import (
        DEFAULT_SIGN_CONVENTION,
        store_calibration,
    )

    body = correction_capture._read_json_body(
        handler,
        max_bytes=MAX_CALIBRATION_UPLOAD_JSON_BYTES,
    )
    text = str(body.get("content") or "")
    filename = str(body.get("filename") or "uploaded-calibration.txt")
    model = str(body.get("model") or "other").strip() or "other"
    label = str(body.get("label") or "Other calibrated mic").strip()
    orientation = str(body.get("orientation") or "unknown").strip() or "unknown"
    # The page's own control defaults to "response" because that is what a
    # measurement-mic calibration file states (see the upload card's help
    # copy and jasper.audio_measurement.calibration.SUPPORTED_MODELS); a
    # caller that omits the field gets the same answer, not the opposite one.
    sign_convention = (
        str(body.get("sign_convention") or DEFAULT_SIGN_CONVENTION).strip()
        or DEFAULT_SIGN_CONVENTION
    )
    record = store_calibration(
        text=text,
        provider="manual_upload",
        model=model,
        label=label,
        source=f"uploaded:{filename}",
        orientation=orientation,
        sign_convention=sign_convention,
        root=configured_calibration_root(),
    )
    correction_capture._save_household_mic(record)
    return correction_capture._calibration_payload(record)






def _handle_status(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /status: snapshot the current session + currently-loaded
    CamillaDSP config descriptor. `current_correction` is best-effort
    (returns None if CamillaDSP is unreachable) so the page still
    renders something useful when the daemon is restarting."""
    from jasper.dsp_apply import last_dsp_apply_state

    sess = correction_capture._get_or_create_session()
    snap = sess.snapshot()
    current_config, presentation = _current_config_presentation(sess)
    snap["current_config"] = current_config
    snap["current_correction"] = current_config.get("current_correction")
    snap["current_correction_presentation"] = presentation
    snap["last_dsp_apply"] = last_dsp_apply_state()
    return snap


def _current_config_presentation(sess: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the current Camilla descriptor and its homeowner presentation."""

    from jasper.correction.status import (
        current_correction_presentation,
        describe_current_config,
    )

    cam = correction_capture._camilla()
    try:
        path = correction_capture._run_async(
            cam.get_config_file_path(best_effort=True), timeout=2.0,
        )
    except Exception:  # noqa: BLE001
        logger.exception("status: get_config_file_path failed")
        path = None
    current_config = describe_current_config(
        path,
        config_dir=sess.cfg.config_dir,
        base_config_path=sess.cfg.base_config_path,
    )
    return current_config, current_correction_presentation(current_config)


def _handle_entry_status(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """Lightweight idle refresh: screen/state, readiness, and config banner."""

    from jasper.correction import envelope

    sess = correction_capture._get_or_create_session()
    _current_config, presentation = _current_config_presentation(sess)
    return {
        "screen": envelope.screen_for_session(sess),
        "state": sess.state.value,
        "readiness_blocker": correction_capture._room_readiness().blocker,
        "current_correction_presentation": presentation,
    }


def _handle_envelope(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /envelope: the server-computed screen envelope for the current
    session. It is a pure presentation read alongside the unchanged
    mechanism snapshot at /status. The browser renders the envelope's exact
    ordered section list and closed action vocabulary without owning a
    second screen policy."""
    from jasper.correction import envelope

    sess = correction_capture._get_or_create_session()
    screen = envelope.screen_for_session(sess)
    readiness_blocker = None
    if screen == envelope.SCREEN_IDLE:
        readiness_blocker = correction_capture._room_readiness().blocker

    # Session discovery reads every bundle today, so it is intentionally
    # confined to idle/result static edges. Active screens are fetched every
    # 900 ms and must never inherit this directory scan.
    reports_available = False
    if screen in envelope.REPORT_SECTION_SCREENS:
        from jasper.correction.bundles import list_bundles

        try:
            reports_available = bool(
                list_bundles(sess.cfg.sessions_dir, limit=1)
            )
        except OSError as exc:
            # Reports are optional evidence, never a reason to strand the
            # measurement entry/result screen when storage is unavailable.
            log_event(
                logger,
                "correction.report_discovery_failed",
                session=getattr(sess, "session_id", ""),
                error_type=type(exc).__name__,
                level=logging.WARNING,
            )

    envelope_kwargs: dict[str, Any] = {}
    if screen == envelope.SCREEN_IDLE:
        # Pass an explicit decision only when this read observed idle. If the
        # session races from active back to idle before the pure builder reads
        # it, the omitted argument takes the builder's fail-closed path rather
        # than accidentally treating `None` as a positive readiness decision.
        envelope_kwargs["readiness_blocker"] = readiness_blocker
    return envelope.build_envelope_logged(
        sess,
        reports_available=reports_available,
        **envelope_kwargs,
    )


def _handle_sessions(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /sessions: list recent session bundles for debugging /
    future UI history. Returns the parsed info.json for each entry,
    sorted by started_at desc; capped at 20. Bundles without a
    parseable info.json (in-progress writes, crashed mid-state) are
    skipped silently."""
    from jasper.correction.bundles import list_bundles

    sess = correction_capture._get_or_create_session()
    return {"sessions": list_bundles(sess.cfg.sessions_dir, limit=20)}


def _handle_session_report(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """GET /session-report?id=<session_id>: return a read-only,
    browser-safe measurement report built from one session bundle.

    This intentionally returns metadata and derived evidence only. Raw
    recordings stay in the private bundle for operator/CLI workflows.
    """
    from . import correction_report

    sess = correction_capture._get_or_create_session()
    query = parse_qs(urlparse(handler.path).query)
    session_id = (query.get("id") or [""])[0]
    try:
        payload = correction_report.build_session_report_payload(
            sessions_dir=sess.cfg.sessions_dir,
            session_id=session_id,
        )
    except correction_report.InvalidSessionId as e:
        raise BadRequest(str(e)) from e
    log_event(
        logger,
        "correction.session_report",
        session=payload.get("session_id") or session_id,
    )
    return payload


def _handle_session_delete(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /session/delete: delete one historical measurement bundle."""
    import shutil

    from . import correction_report

    sess = correction_capture._get_or_create_session()
    body = correction_capture._read_json_body(handler)
    session_id = str(body.get("id") or "")
    try:
        bundle_dir = correction_report.resolve_session_bundle_dir(
            sess.cfg.sessions_dir,
            session_id,
        )
    except correction_report.InvalidSessionId as e:
        raise BadRequest(str(e)) from e
    current_state = getattr(getattr(sess, "state", None), "value", None)
    if (
        session_id == getattr(sess, "session_id", None)
        and current_state in _BUNDLE_DELETE_BLOCKED_STATES
    ):
        raise RequestConflict(
            "cannot delete the measurement bundle for an active session"
        )
    shutil.rmtree(bundle_dir)
    log_event(
        logger,
        "correction.session_bundle_deleted",
        session=session_id,
        bundle=bundle_dir,
    )
    return {"deleted": True, "session_id": session_id}


def _read_wav_body(
    handler: BaseHTTPRequestHandler,
    *,
    max_bytes: int = MAX_WAV_BODY_BYTES,
) -> bytes:
    try:
        length = int(handler.headers.get("Content-Length") or "0")
    except ValueError as e:
        raise BadRequest("invalid Content-Length") from e
    if length <= 0:
        raise BadRequest("empty body")
    if length > max_bytes:
        raise BadRequest(f"WAV body too large ({length} bytes)")
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise BadRequest("incomplete WAV body")
    return raw


def _handle_local_capture_setup(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /local-capture/setup: bind the realized browser input.

    The browser asks for microphone permission after the run is reserved.
    This narrow setup write makes the selected device/calibration the live
    session authority before any audio upload.
    """
    from jasper.audio_measurement.calibration import load_calibration_record
    from jasper.correction.session import SessionState

    sess = correction_capture._get_or_create_session()
    if sess.state != SessionState.NEEDS_NOISE_CAPTURE:
        raise RequestConflict("microphone setup is not available now")

    body = correction_capture._read_json_body(handler)
    requested_session_id = str(body.get("session_id") or "")
    if requested_session_id != sess.session_id:
        raise RequestConflict("this room-correction run is no longer current")
    input_device = correction_capture._sanitize_input_device(body.get("input_device"))
    if input_device is None:
        raise ValueError("select a microphone before continuing")

    calibration_id = str(body.get("calibration_id") or "").strip()
    mic_calibration = (
        load_calibration_record(calibration_id, root=configured_calibration_root())
        if calibration_id
        else None
    )
    mismatch = correction_capture._calibration_device_mismatch(mic_calibration, input_device)
    if mismatch is not None:
        raise ValueError(mismatch)

    try:
        browser_report = correction_capture._run_async(
            sess.bind_local_capture_setup(
                mic_calibration=mic_calibration,
                input_device=input_device,
            ),
            timeout=3.0,
        )
    except RuntimeError as exc:
        raise RequestConflict("microphone setup is not available now") from exc

    log_event(
        logger,
        "correction.local_capture_setup_bound",
        session=sess.session_id,
        calibrated=mic_calibration is not None,
        browser_audio_level=str(browser_report.get("level") or ""),
    )
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "input_device": sess.input_device,
        "browser_audio_report": browser_report,
        "mic_calibration": (
            mic_calibration.public_metadata() if mic_calibration else None
        ),
    }


def _handle_upload_noise(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /upload-noise: persist pre-sweep silence, then play sweep."""
    from jasper.correction.session import AutolevelStatus, SessionState

    sess = correction_capture._get_or_create_session()
    if sess is None:
        raise RuntimeError("no session — POST /start first")
    if sess.state != SessionState.NEEDS_NOISE_CAPTURE:
        raise RuntimeError(
            f"cannot accept noise capture from state {sess.state.value}"
        )
    if not bool(getattr(sess, "local_capture_setup_bound", False)):
        raise RequestConflict(
            "bind the local microphone setup before uploading room noise"
        )
    if (
        sess.autolevel.status != AutolevelStatus.LOCKED
        or bool(getattr(sess, "autolevel_run_in_progress", False))
    ):
        raise RequestConflict(
            "complete and lock the measurement level check before measuring"
        )

    correction_capture._run_async(sess.resume_capture_timeout_on_loop(), timeout=2.0)
    body = _read_wav_body(handler)
    captured_path = sess.noise_capture_path_for_position(sess.current_position)
    captured_path.parent.mkdir(parents=True, exist_ok=True)
    captured_path.write_bytes(body)
    correction_capture._run_async(sess.on_noise_capture_uploaded(captured_path), timeout=10.0)
    correction_capture._schedule_measurement_sweep(
        sess,
        correction_capture._camilla(),
        from_state=SessionState.NEEDS_NOISE_CAPTURE,
    )
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
        "noise_reports": sess.noise_reports,
        "acoustic_quality": (
            (sess.acoustic_quality or {}).get("summary")
            if sess.acoustic_quality
            else None
        ),
    }


def _handle_repeat_position(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /repeat-position: play the optional same-seat repeat."""
    from jasper.correction.session import SessionState

    sess = correction_capture._get_or_create_session()
    if sess.state != SessionState.NEEDS_REPEAT_CAPTURE:
        raise RuntimeError(
            f"cannot repeat main seat from state {sess.state.value}"
        )
    correction_capture._schedule_repeat_sweep(
        sess,
        correction_capture._camilla(),
        from_state=SessionState.NEEDS_REPEAT_CAPTURE,
    )
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
    }


def _handle_upload_capture(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /upload-capture: read the WAV body, write to disk, run
    the analysis pipeline. Routes to either the multi-position
    capture path (if state == AWAITING_CAPTURE) or the verify path
    (if state == AWAITING_VERIFY_CAPTURE)."""
    from jasper.correction.session import SessionState

    sess = correction_capture._get_or_create_session()
    if sess is None:
        raise RuntimeError("no session — POST /start first")

    body = _read_wav_body(handler)

    if sess.state == SessionState.AWAITING_VERIFY_CAPTURE:
        captured_path = sess.verify_capture_path()
    elif sess.state == SessionState.AWAITING_REPEAT_CAPTURE:
        captured_path = sess.repeat_capture_path_for_position(0)
    else:
        captured_path = sess.capture_path_for_position(sess.current_position)
    captured_path.parent.mkdir(parents=True, exist_ok=True)
    captured_path.write_bytes(body)

    auto_reverted = False
    if sess.state == SessionState.AWAITING_VERIFY_CAPTURE:
        correction_capture._run_async(
            sess.on_verify_capture_uploaded(captured_path), timeout=30.0,
        )
        # P4: a CONFIRMED-regression verdict auto-reverts. The verdict was
        # computed inside on_verify_capture_uploaded (pure, no CamillaDSP); the
        # rollback happens here where the CamillaDSP callbacks live, riding the
        # SAME reset target the /reset button uses (Layer B removed, speaker
        # DSP + preference preserved). Every other verdict is a no-op.
        auto_reverted = _maybe_auto_revert(sess)
    elif sess.state == SessionState.AWAITING_REPEAT_CAPTURE:
        correction_capture._run_async(
            sess.on_repeat_capture_uploaded(captured_path), timeout=30.0,
        )
    else:
        correction_capture._run_async(sess.on_capture_uploaded(captured_path), timeout=30.0)

    # The upload response is a mechanism acknowledgement, not a second
    # presentation contract. The browser refreshes the server envelope for
    # curves, verdict, nudges, sections, and actions.
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "current_position": sess.current_position,
        "total_positions": sess.total_positions,
        "auto_reverted": auto_reverted,
    }










def _handle_crossover_capture_cancel() -> dict[str, Any]:
    """Stop Crossover capture work and keep its slot until cleanup completes.

    The Stop button is already hidden once the rendered status turns terminal
    (crossover/main.js's ``CAPTURE_STOPPABLE`` gate), but a poll-cycle race can
    still let a click reach the server after the capture finished on its own
    (it completed, or another tab already stopped it). ``_request_capture_stop``
    raises a diagnostic message for that case; map it to a plain-language
    sentence here rather than leaking it to the page.
    """

    try:
        capture = correction_capture._request_capture_stop("crossover_v2:")
    except ValueError:
        raise ValueError(
            "This measurement already stopped — nothing more to do here."
        ) from None
    return {"capture": capture}


def _handle_crossover_v2_position_ready(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """Release only the capture attempt whose pose the mover confirmed."""
    raw = correction_capture._read_json_body(handler)
    for key in ("index", "attempt"):
        if key not in raw:
            raise BadRequest(f"{key} is required")
        if isinstance(raw[key], bool) or not isinstance(raw[key], int):
            raise BadRequest(f"{key} must be an integer")
    with _session_lock:
        gate = correction_capture._capture_position_gate
    if gate is None:
        raise ValueError(
            "no remote measurement is waiting for the microphone right now"
        )
    released = gate.release(raw["index"], raw["attempt"])
    return {"ok": True, "released": released}


def _handle_crossover_v2_complete(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/v2/complete — the wired all-spots-measured signal (D1).

    The wired session's stand-in for the phone's authenticated
    complete-capture-set event (#2662 W2b): the driver (or the W3 wizard
    surface) says the household is done measuring, the held pre-apply group
    closes, and the fit runs. Only a live WIRED session holds the signal — a
    a finished session drops it with the slot — so "nothing waiting" is a conflict
    (stale caller), the position-ready shape.
    """
    correction_capture._read_json_body(handler)  # no fields consumed; drains the request body
    with _session_lock:
        request_complete = correction_capture._capture_complete_request
    if request_complete is None:
        raise ValueError(
            "no wired measurement is waiting for an all-spots-measured "
            "confirmation right now"
        )
    request_complete()
    return {"ok": True}


def _handle_crossover_v2_retake(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/v2/retake — the wired session's per-take retake.

    The local stand-in for the phone's ``begin_capture {retake: true}``: the
    household (or the W3 wizard surface) says the take that just completed
    should be measured again. The walk re-opens THAT slot the next time it is
    waiting on a person — a held begin, or the held-set window — on the
    same terms.

    **No ``index``, and that is the contract rather than a shortcut.** The
    rule is that a retake names the slot which JUST COMPLETED
    (``retakes_the_just_accepted_slot``: ``index == accepted_count``), and the
    walk is the only thing that knows that number — it is a worker-thread
    local, not a published one. Accepting an index here would mint a second
    answer to "which slot", and the only thing a caller could do with it is
    disagree. The signal says WHAT the household wants; WHICH slot stays the
    walk's own fact.

    Only a live session holds the signal, and a finished session drops it
    with the slot, so "nothing waiting" is a conflict (stale caller), the
    position-ready shape. Whether the retake is then ADMISSIBLE (a take exists
    to replace, the plan's attempts are not spent, the slot's extras ledger
    still has room) is the walk's decision, journalled as
    ``event=correction.crossover_v2_wired_retake_refused``: a refused retake
    leaves the household with the take they already had, which is why it is
    never a session death.
    """
    correction_capture._read_json_body(handler)  # no fields consumed; drains the request body
    with _session_lock:
        request_retake = correction_capture._capture_retake_request
    if request_retake is None:
        raise ValueError(
            "no wired measurement is waiting to re-take a spot right now"
        )
    request_retake()
    return {"ok": True}


def _handle_crossover_v2_capture(
    handler: BaseHTTPRequestHandler,
    *,
    verify_only: bool,
    idle_hold: Callable[[str], AbstractContextManager[Any]] = no_hold,
) -> dict[str, Any]:
    """POST /crossover/v2/session | /crossover/v2/verify (Wave 5a).

    Thin dispatch over :mod:`jasper.web.correction_crossover_v2` — the v2 host
    module owns gating, conductor construction, seam bindings, and the plan
    runner; this bridges it into the shared capture slot/lifecycle machinery
    (``_run_capture``) exactly as the other hosted crossover
    captures do.

    ``idle_hold`` covers the one background lifetime a v2 session still owns:
    the capture runner (through ``_run_capture``). It serves no HTTP
    request, and it is the flow the 600 s idle exit actually killed (issue
    #1854). It used to reach a SECOND lifetime — the auto-apply worker thread
    the runner spawned — which the two-stage split removed: the apply is now a
    household POST served in-request, so the tracker's ordinary
    in-flight-request accounting holds the process for it.
    """
    raw = correction_capture._read_json_body(handler)

    from . import correction_crossover_backend, correction_crossover_v2 as v2host

    blocking = correction_capture._crossover_blocking_phase()
    if blocking is not None:
        raise ValueError(
            f"another measurement is in progress ({blocking}) — finish it "
            "before starting a crossover measurement session"
        )
    status = correction_crossover_backend.status_payload()
    prepared = v2host.prepare_v2_session(
        raw,
        status=status,
        run_async=correction_capture._run_async,
        camilla_factory=correction_capture._camilla,
        verify_only=verify_only,
    )
    kind = CaptureKind(
        label=prepared.label,
        open=prepared.open,
        run_and_consume=prepared.run_and_consume,
        request_stop=prepared.request_stop,
        position_gate=prepared.position_gate,
        request_complete=prepared.request_complete,
        request_retake=prepared.request_retake,
    )
    return {"capture": correction_capture._run_capture(kind, idle_hold=idle_hold)}


def _handle_crossover_v2_apply(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /crossover/v2/apply: apply the reviewed v2 measured candidate.

    Reads the same ``status_payload()`` the session preparers do, because the
    apply now runs the stage-2 openability preflight server-side (two-stage
    commission work order D3): a speaker that cannot open its post-apply check
    must not be corrected and left ungraded.
    """
    raw = correction_capture._read_json_body(handler)

    from . import correction_crossover_backend, correction_crossover_v2 as v2host

    return v2host.handle_v2_apply(
        raw,
        correction_capture._run_async,
        correction_capture._camilla,
        status=correction_crossover_backend.status_payload(),
    )


def _handle_crossover_v2_republish(
    handler: BaseHTTPRequestHandler,
) -> dict[str, Any]:
    """POST /crossover/v2/republish: re-publish a banked candidate by fingerprint.

    Touches no DSP and holds no capture — it replaces the durable session
    document around the published-candidate slot (host-owned apply keys
    carried forward) and moves no graph — so unlike its apply sibling it
    needs neither ``_run_async`` nor ``_camilla`` nor the stage-2
    ``status_payload()``. The apply door still runs every gate it always did,
    on the next request.
    """
    raw = correction_capture._read_json_body(handler)

    from . import correction_crossover_v2_republish as republish

    return republish.handle_v2_republish(raw)


def _handle_crossover_v2_decline(
    handler: BaseHTTPRequestHandler,
) -> tuple[dict[str, Any], HTTPStatus]:
    """POST /crossover/v2/decline: the review screen's "Keep current sound".

    Touches no DSP and holds no capture, so unlike its apply/restore siblings it
    needs neither ``_run_async`` nor ``_camilla`` — it records a decision and
    re-renders. The capture snapshot rides the response for the same reason
    ``/crossover/reset``'s does: the page renders one envelope per round trip.
    """
    raw = correction_capture._read_json_body(handler)

    from . import correction_crossover_flow

    return correction_crossover_flow.handle_v2_decline(
        raw,
        capture=correction_capture._get_capture_slot_for("crossover_v2:"),
    )


def _handle_crossover_reset() -> tuple[dict[str, Any], HTTPStatus]:
    """POST /crossover/reset: in-flow "start over" for the crossover flow.

    Unlike ``_handle_crossover_capture_cancel``, an unstarted capture is the
    COMMON case here (most Start-over clicks happen between measurements,
    not mid-capture), so a "nothing to stop" ``ValueError`` is swallowed
    rather than surfaced. Any crossover-owned capture or level-match ramp is
    requested to stop first; the actual state clear
    (``correction_crossover_flow.handle_reset``) fails closed if that stop
    has not finished draining yet, rather than racing it.
    """

    try:
        correction_capture._request_capture_stop("crossover_v2:")
    except ValueError:
        pass

    from . import correction_crossover_flow

    return correction_crossover_flow.handle_reset(
        capture=correction_capture._get_capture_slot_for("crossover_v2:"),
    )


def _maybe_restore_main_volume(sess, cam) -> None:
    """Use the session's terminal cleanup, including retries after failed writes."""
    from jasper.correction.session import SessionState

    if sess.state in {
        SessionState.PREPARING, SessionState.SWEEPING,
        SessionState.ANALYZING, SessionState.VERIFYING,
    }:
        return
    try:
        async def _restore() -> None:
            await sess._restore_listening_volume_if_ramped()
            restore_level_match = getattr(sess, "restore_level_match_volume", None)
            if callable(restore_level_match):
                await restore_level_match(correction_capture._household_level_door())

        correction_capture._run_async(resilient_restore(_restore()), timeout=5.0)
    except Exception:  # noqa: BLE001
        logger.exception("main_volume restore after autolevel workflow failed")


def _handle_apply(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /apply: write YAML + reload CamillaDSP. Restores
    pre-autolevel main_volume if autolevel was used."""
    sess = correction_capture._get_or_create_session()
    # No confidence pre-check here. Until the nanny burn-down
    # (docs/measurement-loop-doctrine.md deviation (d)) this raised a 422
    # before ``_camilla()`` whenever the confidence report held a
    # ``fail``-severity finding — a prediction about how good the evidence was
    # refusing a reversible, measurable experiment, which is not on the
    # doctrine's closed hard-stop list. The doubt now rides to the household as
    # a ``warn`` nudge on the envelope (``jasper.correction.envelope._nudges``)
    # and the apply proceeds. What still bounds this path is structural and
    # unchanged: the session state machine, the room-authority binding checked
    # in ``prepare_guard``, and the volume restore below.
    cam = correction_capture._camilla()

    async def _set(path: str) -> bool:
        return await cam.set_config_file_path(path, best_effort=False)

    async def _get() -> str | None:
        return await cam.get_config_file_path(best_effort=True)

    try:
        correction_capture._run_graph_mutation(
            sess.apply(
                _set,
                camilla_get_config=_get,
                prepare_guard=lambda: correction_capture._assert_room_authority_current(
                    cam,
                    sess.room_authority_binding,
                ),
            )
        )
    finally:
        # Audio-safety: autolevel may have ramped main_volume well above the
        # listening level for measurement SNR. Restore it even if apply()
        # raised, so a failed apply never strands the speaker loud.
        _maybe_restore_main_volume(sess, cam)
    return {
        "session_id": sess.session_id,
        "state": sess.state.value,
        "config_path": (
            str(sess.config_path) if sess.config_path else None
        ),
    }


def _accepts_target_config_path(fn: Any) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    if "target_config_path" in params:
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in params.values()
    )


def _handle_reset(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    """POST /reset: cancel a measurement or strip active room correction.

    If a measurement is in progress (or failed before apply), restore the graph
    that was active before `/start`. Once a correction is applied, reset means
    "remove Layer B" — re-emit the current graph with room PEQs cleared while
    preserving topology-owned speaker DSP and current preference EQ.
    """
    sess = correction_capture._get_or_create_session()
    cam = correction_capture._camilla()

    reset_intent = None
    if hasattr(sess, "begin_autolevel_reset"):
        reset_intent = correction_capture._run_async(sess.begin_autolevel_reset(), timeout=45.0)
    else:
        # Duck-typed test/legacy sessions retain the old seam. Production
        # MeasurementSession uses the atomic reset intent above.
        autolevel_status = getattr(
            getattr(sess, "autolevel", None), "status", None
        )
        autolevel_active = bool(
            getattr(
                sess,
                "autolevel_run_in_progress",
                getattr(autolevel_status, "value", None) == "ramping",
            )
        )
        if autolevel_active:
            correction_capture._run_async(sess.cancel_autolevel_and_wait(), timeout=7.0)

    try:
        if hasattr(sess, "stop_background_audio_for_reset"):
            correction_capture._run_async(sess.stop_background_audio_for_reset(), timeout=45.0)
        correction_capture._run_graph_mutation(_run_locked_room_reset(sess, cam))
    finally:
        # Audio-safety: restore the pre-autolevel listening level even if
        # reset() raised (see _handle_apply).
        try:
            _maybe_restore_main_volume(sess, cam)
        finally:
            if reset_intent is not None:
                correction_capture._run_async(sess.end_autolevel_reset(reset_intent), timeout=2.0)
    return {"session_id": sess.session_id, "state": sess.state.value}


async def _pre_measurement_restore_target(
    sess: Any,
    cam: Any,
    *,
    current_path: str | Path | None = None,
    previous_apply: Mapping[str, Any] | None = None,
) -> Path | None:
    """Prior graph to restore only while this measurement still owns Camilla."""
    state_value = getattr(getattr(sess, "state", None), "value", None)
    if state_value in {"applied", "verified"}:
        return None
    prior = getattr(sess, "pre_measurement_config_path", None)
    restore = getattr(sess, "pre_measurement_restore_path", None)
    if state_value == "idle" and (prior or restore):
        return None
    current = current_path or await cam.get_config_file_path(best_effort=False)
    if not current:
        raise RuntimeError("CamillaDSP did not report a loaded config path")
    if not prior or not restore:
        candidate = Path(current)
        if candidate.name.startswith("sound_snapshot_"):
            candidate = candidate.with_name(
                candidate.name.replace("sound_snapshot_", "correction_measurement_", 1)
            )
            if not candidate.is_file():
                return None
        config_dir = getattr(getattr(sess, "cfg", None), "config_dir", None)
        match = _MEASUREMENT_FILENAME_RE.fullmatch(candidate.name)
        if not match or not config_dir or not same_config_file(candidate.parent, config_dir):
            return None
        normalized = await cam.normalize_config_raw(
            candidate.read_text(encoding="utf-8"), best_effort=False,
        )
        live = await cam.get_active_config_raw(best_effort=False)
        if running_graph_fingerprint(normalized) != running_graph_fingerprint(live):
            log_event(logger, "correction.measurement_graph_superseded", current=str(current))
            return None
        snapshot = candidate.with_name(
            candidate.name.replace("correction_measurement_", "sound_snapshot_", 1)
        )
        if not snapshot.is_file():
            # Older runs used unrelated snapshot names. The last operation can
            # recover those only when its candidate still matches live audio.
            operation = previous_apply or last_dsp_apply_state() or {}
            saved = operation.get("prior_config_path")
            if (
                operation.get("source") == "correction_measurement"
                and operation.get("phase") in {"load", "confirm", "persist", "done"}
                and same_config_file(operation.get("candidate_config_path"), candidate)
                and operation.get("config_sha256") == config_file_sha256(candidate)
                and isinstance(saved, str)
                and same_config_file(Path(saved).parent, config_dir)
                and Path(saved).name.startswith("sound_snapshot_")
                and _SOUND_FILENAME_RE.fullmatch(Path(saved).name)
            ):
                snapshot = Path(saved)
            if not snapshot.is_file():
                raise RequestConflict("Room predecessor snapshot is unavailable")
        log_event(
            logger, "correction.measurement_predecessor_recovered",
            current=str(candidate), restore=str(snapshot),
        )
        return snapshot

    measurement = getattr(sess, "measurement_config_path", None)
    owned_path = Path(measurement) if measurement else Path(prior)
    prior_path = Path(prior)
    restore_path = Path(restore)
    if Path(current) in {owned_path, restore_path}:
        return restore_path
    if Path(current) == prior_path:
        # A durable Active filename can be overwritten by a blocked candidate
        # without CamillaDSP loading those new bytes. Compare the daemon's
        # running graph with Start's immutable snapshot; filename equality by
        # itself is not evidence that either the old or new content is active.
        raw = await cam.get_active_config_raw(best_effort=False)
        try:
            saved = restore_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(
                "Room's immutable predecessor snapshot is unavailable"
            ) from exc
        if _running_graph_body(raw) == _running_graph_body(saved):
            return restore_path

    # A legal DSP writer may publish a newer Active graph after Room Start.
    # The shared lock makes this read stable; never use Room's saved predecessor
    # once Camilla has moved away from Room's own measurement graph. The caller
    # will instead strip Room from the fresh current graph, preserving new A.
    log_event(
        logger,
        "correction.pre_measurement_predecessor_superseded",
        session=getattr(sess, "session_id", None),
        current=str(current),
        room_owned=str(owned_path),
        saved_predecessor=str(prior),
        immutable_restore=str(restore_path),
        level=logging.WARNING,
    )
    return None


async def _resolve_reset_target_async(sess: Any, cam: Any) -> Path:
    """Resolve the graph to restore for a reset / auto-revert.

    The single source of truth for "what should the speaker load when we undo
    room correction," shared by ``POST /reset`` (user-driven) and the P4
    confirmed-regression auto-revert (deterministic). If a measurement is
    mid-flight and Camilla still runs Room's measurement graph, restore the
    pre-``/start`` graph. If another legal writer has since published a graph,
    or once a correction is applied/verified, re-emit that current topology
    with room PEQs cleared (Layer B removed, speaker DSP + preference EQ
    preserved). A re-emit failure may retain only the observably managed,
    no-Room graph captured from Camilla's active_raw before re-emit; otherwise
    reversal fails loudly without claiming that Layer B was removed.
    """
    cfg = getattr(sess, "cfg", None)
    base_config_path = getattr(
        cfg,
        "base_config_path",
        Path("/etc/camilladsp/outputd-cutover.yml"),
    )
    target = await _pre_measurement_restore_target(sess, cam)
    if target is None:
        (
            _current,
            current_snapshot,
            bass_profile_summary,
        ) = await _snapshot_running_room_graph(sess, cam)
        try:
            target = await _write_no_room_correction_config(
                sess,
                cam,
                current_snapshot_path=current_snapshot,
                bass_profile_summary=bass_profile_summary,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "reset/auto-revert: no-room re-emit failed; checking the "
                "fresh current graph",
            )
            from jasper.correction.status import describe_current_config

            config_dir = Path(
                getattr(cfg, "config_dir", None)
                or "/var/lib/camilladsp/configs"
            )
            # This immutable snapshot was captured and safety-validated before
            # the failed re-emit wrote its separate candidate. Never re-read a
            # mutable current filename here: it may be the rejected output.
            target = current_snapshot
            descriptor = describe_current_config(
                str(target),
                config_dir=config_dir,
                base_config_path=Path(base_config_path),
            )
            fallback_kind = descriptor.get("kind")
            fallback_is_no_room = (
                descriptor.get("managed") is True
                and descriptor.get("current_correction") is None
                and fallback_kind
                in {"base", "active_speaker", "sound_preference"}
            )
            if not fallback_is_no_room:
                log_event(
                    logger,
                    "correction.reset_fallback_rejected",
                    session=getattr(sess, "session_id", None),
                    target=str(target),
                    kind=fallback_kind,
                    managed=descriptor.get("managed"),
                    room_correction_present=isinstance(
                        descriptor.get("current_correction"),
                        dict,
                    ),
                    level=logging.ERROR,
                )
                raise RuntimeError(
                    "Room correction could not be removed because no verified "
                    "no-Room graph is available; the current graph remains "
                    "loaded"
                ) from exc
            log_event(
                logger,
                "correction.reset_fallback_selected",
                session=getattr(sess, "session_id", None),
                target=str(target),
                kind=fallback_kind,
                level=logging.WARNING,
            )
    return target


async def _run_locked_room_reset(
    sess: Any,
    cam: Any,
    *,
    automatic: bool = False,
) -> Any:
    """Resolve and load one Room reversal under the shared DSP-writer lock."""

    from jasper.dsp_apply import dsp_writer_lock

    cfg = getattr(sess, "cfg", None)
    config_dir = getattr(cfg, "config_dir", None)
    if config_dir is None:
        raise RuntimeError("Room session has no CamillaDSP config directory")

    async def _set(path: str) -> bool:
        return await cam.set_config_file_path(path, best_effort=False)

    operation = sess.auto_revert if automatic else sess.reset
    source = "correction_auto_revert" if automatic else "correction_reset"
    async with dsp_writer_lock(config_dir, source=source):
        if (getattr(sess, "startup_recovery", None) or {}).get("required"):
            await _recover_room_startup_state_locked(sess, cam)
            return
        # Restoration must not depend on fresh Room authority: its purpose is
        # to recover from a stale/failed Room session.  It does need to resolve
        # the no-Room carrier after admission so a legal Active writer cannot
        # swap Layer A between target construction and load.
        target = await _resolve_reset_target_async(sess, cam)
        kwargs = (
            {"target_config_path": target}
            if _accepts_target_config_path(operation)
            else {}
        )
        return await operation(_set, **kwargs)


async def recover_room_startup_state(sess: Any, cam: Any) -> None:
    """Recover only the paired graph still owned by an abandoned Room run."""
    from jasper.dsp_apply import dsp_writer_lock

    async with dsp_writer_lock(sess.cfg.config_dir, source="correction_startup_recovery"):
        await _recover_room_startup_state_locked(sess, cam)


async def _recover_room_startup_state_locked(sess: Any, cam: Any) -> None:
    pending = getattr(sess, "startup_recovery", None) or {}
    recovery = {"required": pending.get("required", False), "graph": "unknown", "volume": "unknown"}
    try:
        current = await cam.get_config_file_path(best_effort=False)
        if not current or current == "None":
            return
        recovery["required"] = True
        target = await _pre_measurement_restore_target(sess, cam, current_path=current)
        if target is None:
            if getattr(sess, "startup_recovery", None):
                sess.startup_recovery = {"required": False, "graph": "superseded", "volume": "unchanged"}
            return
        sess.startup_recovery = recovery
        recovery["graph"] = "pending"
        owner = volume_owner()
        if owner is None:
            raise RuntimeError("the volume owner is unavailable")
        door = OwnerVolumeDoor(owner, read_fader=lambda: cam.get_volume_db(best_effort=False))
        # Keep the paired measurement graph until volume confirms, so process
        # loss cannot consume the only durable recovery association first.
        result = await door.restore_household_level_db(await env_canonical_target_db())
        recovery["volume"] = result.value
        if result is not RestoreOutcome.LANDED:
            return
        if not await cam.set_config_file_path(str(target), best_effort=False):
            raise RuntimeError("Room predecessor could not be restored")
        await confirm_graph_is_live(cam, target.read_text(encoding="utf-8"))
        recovery.update(required=False, graph="restored")
    except (OSError, RuntimeError, ValueError, CamillaUnavailable) as exc:
        recovery["error"] = type(exc).__name__
        sess.startup_recovery = recovery
    finally:
        if getattr(sess, "startup_recovery", None):
            await sess.note_startup_recovery(sess.startup_recovery)
            log_event(
                logger, "correction.startup_recovery",
                level=logging.WARNING if sess.startup_recovery["required"] else logging.INFO,
                **sess.startup_recovery,
            )


def _maybe_auto_revert(sess: Any) -> bool:
    """Perform the P4 auto-revert when the verdict is a confirmed regression.

    Reads ``sess.acceptance_verdict``; only ``revert`` acts. Resolves the same
    reset target ``/reset`` uses and drives the session's ``auto_revert`` (which
    rides the existing ``reset()`` reversal). Returns True when a rollback ran.
    Best-effort: an auto-revert failure is logged and leaves the correction
    applied with the ``revert`` verdict still visible — the household can undo
    manually — rather than 500-ing the verify upload response. reset() itself
    fails the session loudly on a CamillaDSP rejection, so a failed revert is
    never silent.

    Failure honesty: when the attempt dies BEFORE the session could record an
    outcome (for example, target-resolution failure), a "failed" outcome is
    stamped here so the result screen says the correction is STILL APPLIED.
    The stamp never overwrites a recorded outcome; after any shared writer
    admission, graph mutation runs to a terminal result, so success is never
    reported as a timeout-driven cancellation.
    """
    if getattr(sess, "acceptance_verdict", None) != "revert":
        return False
    cam = correction_capture._camilla()

    try:
        return bool(
            correction_capture._run_graph_mutation(
                _run_locked_room_reset(sess, cam, automatic=True)
            )
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "P4 auto-revert failed; correction left applied for manual undo",
        )
        if getattr(sess, "auto_revert_outcome", None) is None:
            sess.auto_revert_outcome = {"result": "failed", "at": time.time()}
        return False


async def _write_no_room_correction_config(
    sess: Any,
    cam: Any,
    *,
    current_snapshot_path: str | Path | None = None,
    bass_profile_summary: Mapping[str, Any] | None = None,
) -> Path:
    """Emit the current graph with room correction cleared.

    For passive/full-range graphs this is an ordinary sound config. For active
    baselines it remains an active graph. The candidate is session-unique so a
    validation failure cannot alter the durable filename Camilla is running.
    """

    from jasper.correction.runtime_safety import assert_correction_graph_safe
    from jasper.dsp_apply import validate_camilla_config
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env
    from jasper.sound.graph_carrier import carrier_for_loaded_config
    from jasper.sound.profile import load_profile

    cfg = getattr(sess, "cfg", None)
    config_dir = Path(
        getattr(cfg, "config_dir", Path("/var/lib/camilladsp/configs"))
    )
    config_dir.mkdir(parents=True, exist_ok=True)
    if current_snapshot_path is None:
        (
            _current,
            snapshot_path,
            bass_profile_summary,
        ) = await _snapshot_running_room_graph(sess, cam)
    else:
        snapshot_path = Path(current_snapshot_path)
    # Never emit over Camilla's reported current filename. Some JTS writers use
    # durable names such as sound_current.yml; post-write validation failure
    # must leave that live predecessor's bytes untouched.
    out_path = _room_graph_artifact_path(sess, "reset")
    carrier = carrier_for_loaded_config(snapshot_path, config_dir=config_dir)
    profile = load_profile()
    result = carrier.reemit(
        profile,
        room_peqs=[],
        out_path=out_path,
        profile_id=f"correction-reset-{time.time_ns()}",
        fanin_coupling_capture_kwargs=coupling_capture_kwargs_from_env(),
    )
    assert_correction_graph_safe(
        result.yaml,
        bass_profile_summary=bass_profile_summary,
    )
    validation = validate_camilla_config(out_path)
    if not validation.ok_to_apply:
        raise RuntimeError(
            "the generated no-Room graph failed CamillaDSP validation: "
            f"{validation.error or validation.status.value}"
        )
    log_event(
        logger,
        "correction.reset_no_room_config",
        current_snapshot=str(snapshot_path),
        candidate=str(out_path),
        room_peqs=result.room_peq_count,
    )
    return out_path
