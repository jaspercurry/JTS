# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Sound state and the profile/settings apply engine behind the /sound/ pages.

:mod:`jasper.web.sound_setup` owns the HTTP surface; this module owns the
``/state`` payload and the apply, settings, audition and live-draft paths that
serialize behind one write lock.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from jasper.log_event import log_event
from jasper.sound.profile import (
    ADVANCED_GAIN_LIMIT_DB,
    CUT_MAX_Q,
    MAX_FREQ_HZ,
    MAX_PARAMETRIC_BANDS,
    MAX_Q,
    MIN_FREQ_HZ,
    MIN_Q,
    SIMPLE_EQ_LIMIT_DB,
    SoundProfile,
    build_sound_filters,
    curve_payload,
    estimate_headroom_db,
    load_profile_library,
    load_profile,
    profile_library_payload,
    response_preview,
    simple_bands_payload,
)
from jasper.sound.settings import (
    DEFAULT_VOLUME_FLOOR_DB,
    HEADROOM_TRIM_MAX_DB,
    SoundSettings,
    VOLUME_FLOOR_MAX_DB,
    VOLUME_FLOOR_MIN_DB,
    load_sound_settings,
    output_trim_db as _output_trim,  # aliased so local `output_trim_db` vars don't shadow it
    save_sound_settings,
)

logger = logging.getLogger(__name__)


LIVE_DRAFT_UNAVAILABLE_LOG_INTERVAL_SEC = 30.0

_live_draft_unavailable_log_at: dict[str, float] = {}


# Profile Apply and the split EQ/setup settings pages both replace the live DSP
# graph from persisted Sound state. Serialize their fresh reads, durable writes,
# and live side effects so the final profile, settings, and graph describe one
# ordered result instead of interleaving browser saves.
_sound_state_write_lock = threading.Lock()
_LAST_DSP_APPLY_SNAPSHOT_UNSET = object()
# "Nobody probed the loaded graph on this response" — distinct from "probed,
# and it can host EQ", so a payload built without a live CamillaDSP read never
# claims the /sound/eq/ editor is usable.
_EQ_CARRIER_NOT_PROBED = object()
_SOUND_SETTINGS_FIELDS = frozenset({
    "headroom_trim_db",
    "match_loudness",
    "volume_floor_db",
})


def _camilla():
    from jasper.camilla import primary_controller

    return primary_controller()


def _state_payload(
    profile: SoundProfile,
    *,
    library_path: str | Path | None = None,
    include_library: bool = False,
    settings_snapshot: SoundSettings | None = None,
    last_dsp_apply_snapshot: Mapping[str, Any] | None | object = (
        _LAST_DSP_APPLY_SNAPSHOT_UNSET
    ),
    eq_block: Any = _EQ_CARRIER_NOT_PROBED,
) -> dict[str, Any]:
    from jasper.dsp_apply import dsp_write_epoch_from_state, last_dsp_apply_state

    if last_dsp_apply_snapshot is _LAST_DSP_APPLY_SNAPSHOT_UNSET:
        last_dsp_apply = last_dsp_apply_state()
    elif last_dsp_apply_snapshot is None:
        last_dsp_apply = None
    else:
        last_dsp_apply = dict(last_dsp_apply_snapshot)
    settings = (
        settings_snapshot
        if settings_snapshot is not None
        else load_sound_settings()
    )

    payload = {
        "profile": profile.to_dict(),
        "curves": curve_payload(),
        "preview": response_preview(profile),
        "headroom_db": estimate_headroom_db(profile),
        # 0 when the profile is disabled (bypass) OR flat (no active filters).
        # The page opens on Off vs Saved based on this.
        "filter_count": len(build_sound_filters(profile)),
        "sound_settings": settings.to_dict(),
        "output_trim_db": _output_trim(profile, settings),
        "limits": {
            "simple_gain_db": SIMPLE_EQ_LIMIT_DB,
            "advanced_gain_db": ADVANCED_GAIN_LIMIT_DB,
            "max_parametric_bands": MAX_PARAMETRIC_BANDS,
            "min_freq_hz": MIN_FREQ_HZ,
            "max_freq_hz": MAX_FREQ_HZ,
            "min_q": MIN_Q,
            "max_q": MAX_Q,
            "cut_max_q": CUT_MAX_Q,
            "simple_bands": simple_bands_payload(),
            "headroom_trim_max_db": HEADROOM_TRIM_MAX_DB,
            "volume_floor_min_db": VOLUME_FLOOR_MIN_DB,
            "volume_floor_max_db": VOLUME_FLOOR_MAX_DB,
            # One owner (volume_curve.DEFAULT_VOLUME_FLOOR_DB, re-exported via
            # sound.settings) → this payload → the page's reset control.
            "volume_floor_default_db": DEFAULT_VOLUME_FLOOR_DB,
        },
        "last_dsp_apply": last_dsp_apply,
        "dsp_write_epoch": dsp_write_epoch_from_state(last_dsp_apply),
        # Whether the LOADED graph can host preference EQ, so /sound/eq/ can be
        # a page state rather than a status line after a save is refused.
        "eq_carrier": (
            {"status": "unknown"}
            if eq_block is _EQ_CARRIER_NOT_PROBED
            else {"status": "ok"} if eq_block is None
            else eq_block.to_payload()
        ),
    }
    if include_library:
        payload["profile_library"] = profile_library_payload(
            load_profile_library(library_path)
        )
    return payload


def _log_live_draft_unavailable(
    *,
    reason: str,
    output_trim_db: float,
    room_peq_count: int,
    sound_filter_count: int,
    error: Exception | None = None,
) -> None:
    now = time.monotonic()
    last = _live_draft_unavailable_log_at.get(reason, 0.0)
    if now - last < LIVE_DRAFT_UNAVAILABLE_LOG_INTERVAL_SEC:
        return
    _live_draft_unavailable_log_at[reason] = now
    log_event(
        logger,
        "sound.live_draft",
        level=logging.WARNING,
        result="unavailable",
        reason=reason,
        output_trim=f"{output_trim_db:.1f}",
        room_peqs=room_peq_count,
        sound_filters=sound_filter_count,
        err=repr(error),
    )


async def _apply_profile(
    profile: SoundProfile,
    *,
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    # Settings Apply uses this same ordering boundary. Read settings only after
    # entering it, then keep the durable profile write and live graph/trim emit
    # together so neither operation can finish with the other's stale half.
    with _sound_state_write_lock:
        settings = load_sound_settings()
        apply_state, out_path, stamped = await _load_profile_config(
            profile.with_timestamp(),
            profile_path=profile_path,
            config_dir=config_dir,
            camilla_factory=camilla_factory,
            source="sound",
            persist_profile=True,
            output_trim_db=_output_trim(profile, settings),
        )
    log_event(
        logger,
        "sound.apply",
        enabled=str(stamped.enabled),
        curve=stamped.curve_id,
        simple=(
            f"{stamped.simple_eq.sub_bass_db:.1f}/"
            f"{stamped.simple_eq.bass_db:.1f}/"
            f"{stamped.simple_eq.mid_db:.1f}/"
            f"{stamped.simple_eq.presence_db:.1f}/"
            f"{stamped.simple_eq.treble_db:.1f}"
        ),
        bands=len(stamped.parametric_bands),
        room_peqs=apply_state.room_peq_count or 0,
        config=out_path,
        op_id=apply_state.op_id,
    )
    # Previews and the optional library are assembled outside the ordering
    # boundary; the captured settings keep the response coherent with the DSP
    # transaction without holding the lock across unrelated file I/O.
    payload = _state_payload(
        stamped,
        library_path=library_path,
        include_library=library_path is not None,
        settings_snapshot=settings,
        last_dsp_apply_snapshot=apply_state.to_dict(),
    )
    payload["active_config_path"] = str(out_path)
    payload["preserved_room_peqs"] = apply_state.room_peq_count or 0
    payload["last_dsp_apply"] = apply_state.to_dict()
    payload["dsp_write_epoch"] = apply_state.op_id
    return payload


def _carrier_refusal(exc: BaseException):
    """Return the ``CarrierCannotHostEq`` behind ``exc`` if the loaded
    CamillaDSP graph refused to host preference EQ, else ``None``.

    A refusal arrives RAW from the live-draft path and the durable path's
    pre-lock fast-check, and wrapped as ``DspApplyError`` (its ``__cause__``)
    from the durable path's in-lock re-check in a concurrent-swap race. Both
    map to a typed 200 body instead of a 502.
    """
    from jasper.sound.graph_carrier import CarrierCannotHostEq

    if isinstance(exc, CarrierCannotHostEq):
        return exc
    cause = exc.__cause__
    if isinstance(cause, CarrierCannotHostEq):
        return cause
    return None


async def _apply_settings(
    changes: Mapping[str, Any] | SoundSettings,
    *,
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    """Merge global sound settings, then make the merged state live.

    EQ and Setup share the one aggregate ``SoundSettings`` file and each page
    posts only the fields it owns, so this fresh-reads and merges recognized
    fields while holding one process-local lock through the durable save, DSP
    re-emit, volume reconciliation, and response snapshots. The profile content
    is not re-stamped or re-persisted. A full ``SoundSettings`` is also accepted
    for internal callers and tests. A failed live re-apply returns the saved
    state with a ``warning`` rather than reverting a setting already kept.
    """
    raw_changes = changes.to_dict() if isinstance(changes, SoundSettings) else changes
    recognized = {
        key: raw_changes[key]
        for key in _SOUND_SETTINGS_FIELDS
        if key in raw_changes
    }
    with _sound_state_write_lock:
        merged_raw = load_sound_settings().to_dict()
        merged_raw.update(recognized)
        settings = SoundSettings.from_mapping(merged_raw)
        save_sound_settings(settings)
        log_event(
            logger,
            "sound.settings",
            headroom_trim=f"{settings.headroom_trim_db:.1f}",
            match_loudness=str(settings.match_loudness),
            volume_floor_db=f"{settings.volume_floor_db:.1f}",
        )
        profile = load_profile(profile_path)
        warning: str | None = None
        blocked: dict[str, str] | None = None
        volume_warning: str | None = None
        apply_result: tuple[Any, Path, SoundProfile] | None = None
        reconciled = False
        try:
            apply_result = await _load_profile_config(
                profile,
                profile_path=profile_path,
                config_dir=config_dir,
                camilla_factory=camilla_factory,
                source="sound_settings",
                persist_profile=False,
                output_trim_db=_output_trim(profile, settings),
            )
        except (OSError, RuntimeError, ValueError, TypeError) as e:
            refusal = _carrier_refusal(e)
            if refusal is None:
                logger.exception("sound settings re-apply failed")
                warning = f"Saved, but applying to the speaker failed: {e}"
            else:
                # The same typed body /apply and /live-draft return, so the
                # settings card branches on one shape instead of parsing prose.
                log_event(
                    logger,
                    "sound.eq_blocked",
                    path="/settings",
                    reason=refusal.reason_code,
                )
                blocked = refusal.to_payload()

        try:
            reconciled = await _reconcile_volume_curve_after_settings(
                camilla_factory=camilla_factory,
            )
        except (AttributeError, OSError, RuntimeError) as e:
            logger.warning("volume floor saved but volume reconcile failed: %s", e)
            volume_warning = (
                "Saved, but the current volume will use the new floor on the next "
                f"volume change: {e}"
            )

        # Capture the response's live-state anchor after every side effect,
        # still serialized; rendering happens outside the boundary.
        if apply_result is not None:
            apply_state, out_path, _ = apply_result
            last_dsp_apply_snapshot = apply_state.to_dict()
        else:
            from jasper.dsp_apply import last_dsp_apply_state

            last_dsp_apply_snapshot = last_dsp_apply_state()

    payload = _state_payload(
        profile,
        library_path=library_path,
        include_library=library_path is not None,
        settings_snapshot=settings,
        last_dsp_apply_snapshot=last_dsp_apply_snapshot,
    )
    if warning is not None:
        payload["warning"] = warning
    if blocked is not None:
        payload.update(blocked)
    if volume_warning is not None:
        payload["volume_warning"] = volume_warning
    if apply_result is not None:
        payload["active_config_path"] = str(out_path)
        payload["preserved_room_peqs"] = apply_state.room_peq_count or 0
        payload["last_dsp_apply"] = apply_state.to_dict()
        payload["dsp_write_epoch"] = apply_state.op_id
    if reconciled:
        payload["volume_reconciled"] = True
    return payload


async def _reconcile_volume_curve_after_settings(
    *,
    camilla_factory: Callable[[], Any] = _camilla,
) -> bool:
    """Apply the newly saved floor to the current listening level when safe.

    ``maybe_reconcile_camilla`` only writes for camilla-master sources
    (idle/AirPlay/USB), so changing the floor cannot unguard a
    Spotify/Bluetooth push-mode handoff.
    """
    from jasper import librespot_state
    from jasper.renderer import RendererClient
    from jasper.volume_coordinator import VolumeCoordinator
    from jasper.volume_persistence import VolumePersistence
    from jasper.volume_persistence import configured_path as volume_state_path

    coord = VolumeCoordinator(
        camilla=camilla_factory(),
        persistence=VolumePersistence(volume_state_path()),
        backend=RendererClient(
            librespot_state_path=librespot_state.configured_path(),
        ),
    )
    try:
        coord.load_persisted_level()
        await coord.maybe_reconcile_camilla()
        return True
    finally:
        await coord.aclose()


async def _audition_profile(
    profile: SoundProfile,
    *,
    audition_mode: str = "draft",
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    settings = load_sound_settings()
    output_trim_db = _output_trim(profile, settings)
    apply_state, out_path, loaded = await _load_profile_config(
        profile,
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=camilla_factory,
        source="sound_audition",
        persist_profile=False,
        audition=True,
        output_trim_db=output_trim_db,
    )
    log_event(
        logger,
        "sound.audition",
        mode=audition_mode,
        enabled=str(loaded.enabled),
        curve=loaded.curve_id,
        bands=len(loaded.parametric_bands),
        output_trim=f"{output_trim_db:.1f}",
        room_peqs=apply_state.room_peq_count or 0,
        config=out_path,
        op_id=apply_state.op_id,
    )
    saved = load_profile(profile_path)
    payload = _state_payload(
        saved,
        library_path=library_path,
        include_library=library_path is not None,
    )
    payload.update(
        {
            "audition_profile": loaded.to_dict(),
            "audition_mode": audition_mode,
            "output_trim_db": output_trim_db,
            "active_config_path": str(out_path),
            "preserved_room_peqs": apply_state.room_peq_count or 0,
            "last_dsp_apply": apply_state.to_dict(),
            "dsp_write_epoch": apply_state.op_id,
        }
    )
    return payload


async def audition_profile(
    profile: SoundProfile,
    *,
    audition_mode: str = "draft",
    profile_path: str | Path,
    library_path: str | Path | None = None,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    """Public backend seam for reversible preference-EQ auditions.

    The web route and the calibration-advisor action runner share this
    implementation, so model-suggested auditions inherit ``/sound/audition``'s
    config validation, room-PEQ preservation and no-persist semantics.
    """

    return await _audition_profile(
        profile,
        audition_mode=audition_mode,
        profile_path=profile_path,
        library_path=library_path,
        config_dir=config_dir,
        camilla_factory=camilla_factory,
    )


async def _live_draft_profile(
    profile: SoundProfile,
    *,
    expected_dsp_write_epoch: str,
    config_dir: str | Path,
    profile_path: str | Path | None = None,
    camilla_factory: Callable[[], Any] = _camilla,
) -> dict[str, Any]:
    """Load a bounded preference-EQ draft into the active Camilla config.

    The low-latency editing path: no profile persistence, no config-file
    pointer change, no shared apply-state mutation. The durable Save/Apply path
    is `_apply_profile`, which writes a validated YAML file and records
    rollback state.

    Returns only `live_status` and `dsp_write_epoch` — the browser reads
    nothing else from this response (`runLiveDraft` in
    `deploy/assets/sound-profile/js/main.js`).
    """
    from jasper.dsp_apply import dsp_write_epoch, dsp_writer_lock
    from jasper.fanin_coupling import coupling_capture_kwargs_from_env
    from jasper.sound.graph_carrier import carrier_for_loaded_config
    from jasper.sound.live_edit import does_live_edits, plan_live_edit_for

    cam = camilla_factory()
    config_path = Path(config_dir)
    settings = load_sound_settings()
    # The trim is derived from the SAVED profile, never from the draft, so an
    # edit cannot move it. Match-loudness makes the trim a function of the
    # profile's own EQ, so a draft-derived trim would fold into
    # `active_baseline_headroom`'s VALUE and be written in place — an instant,
    # un-ducked, full-spectrum level step mid-drag. The durable save realises
    # the change instead, once and on the same rule (ADR-0219). `settings` is
    # still re-read above, so a settings-page change to headroom_trim_db or
    # match_loudness does move the trim, at one swap; the property this buys
    # is only "not draft-derived". Safe because the trim is comfort accounting,
    # not a clip guard — `devices.volume_limit` stays the hard ceiling regardless
    # (`jasper.camilla_stereo_prefix`). The cost is that match-loudness stops
    # tracking the draft until save.
    output_trim_db = _output_trim(load_profile(profile_path), settings)
    sound_filter_count = len(build_sound_filters(profile))

    def _live_payload(*, status: str, current_epoch: str) -> dict[str, Any]:
        return {"live_status": status, "dsp_write_epoch": current_epoch}

    if not does_live_edits(cam):
        current_epoch = dsp_write_epoch()
        _log_live_draft_unavailable(
            reason="active_config_raw_unavailable",
            output_trim_db=output_trim_db,
            room_peq_count=0,
            sound_filter_count=sound_filter_count,
            error=None,
        )
        return _live_payload(status="unavailable", current_epoch=current_epoch)

    async with dsp_writer_lock(config_path, source="sound_live_draft"):
        current_epoch = dsp_write_epoch()
        if expected_dsp_write_epoch != current_epoch:
            log_event(
                logger,
                "sound.live_draft",
                result="stale",
                expected_epoch=str(expected_dsp_write_epoch),
                current_epoch=str(current_epoch),
            )
            return _live_payload(status="stale", current_epoch=current_epoch)

        current_path = await cam.get_config_file_path(best_effort=False)
        if not current_path:
            raise RuntimeError("CamillaDSP did not report a loaded config path")

        carrier = carrier_for_loaded_config(current_path, config_dir=config_path)
        result = carrier.reemit(
            profile,
            profile_id=f"live-{time.time_ns()}",
            output_trim_db=output_trim_db,
            fanin_coupling_capture_kwargs=coupling_capture_kwargs_from_env(),
        )
        yaml = result.yaml
        plan = await plan_live_edit_for(cam, yaml)
        method = plan.method

        try:
            # Duck-or-not is decided in jasper.sound.live_edit; an unchanged
            # graph is not written at all.
            if method != "unchanged":
                await cam.set_active_config_raw(
                    yaml, best_effort=False, duck=plan.duck,
                )
        except Exception as e:  # noqa: BLE001
            _log_live_draft_unavailable(
                reason=f"{method}_failed",
                output_trim_db=output_trim_db,
                room_peq_count=result.room_peq_count,
                sound_filter_count=sound_filter_count,
                error=e,
            )
            return _live_payload(status="unavailable", current_epoch=current_epoch)

        log_event(
            logger,
            "sound.live_draft",
            result="live",
            method=method,
            # Empty unless the edit fell back to a ducked pipeline replace, and
            # then it names the section that moved — the field that explains an
            # audible fade to whoever reads this line.
            swap_reason=plan.reason,
            output_trim=f"{output_trim_db:.1f}",
            room_peqs=result.room_peq_count,
            sound_filters=sound_filter_count,
            active_anchor=str(current_path),
            epoch=str(current_epoch),
        )
        return _live_payload(status="live", current_epoch=current_epoch)


async def _load_profile_config(
    profile: SoundProfile,
    *,
    profile_path: str | Path,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any],
    source: str,
    persist_profile: bool,
    audition: bool = False,
    output_trim_db: float = 0.0,
) -> tuple[Any, Path, SoundProfile]:
    from jasper.sound.runtime import load_profile_config

    return await load_profile_config(
        profile,
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=camilla_factory,
        source=source,
        persist_profile=persist_profile,
        audition=audition,
        output_trim_db=output_trim_db,
    )
