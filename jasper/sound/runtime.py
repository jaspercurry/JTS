# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime apply/reconcile helpers for saved sound preference DSP graphs."""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from jasper.camilla_config_contract import (
    DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
    DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE,
    DEFAULT_LEAN_CAPTURE_FIFO,
)
from jasper.log_event import log_event
from jasper.sound.profile import (
    PROFILE_PATH,
    SoundProfile,
    build_sound_filters,
    load_profile,
    save_profile,
)
from jasper.sound.settings import load_sound_settings, output_trim_db

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path("/var/lib/camilladsp/configs")
RECONCILE_PROFILE_ID = "reconcile-current-dsp"

# Stage-4b lean-lane staging artifact. stage_lean_capture_config emits +
# validates + classifies a lean File-capture config WITHOUT live-loading — the
# smallest safe step that proves a lean config is statically valid and
# L0-graph-safe with zero audio risk. The live-load + lane-arming is the mux
# wiring's job (gated on JASPER_LEAN_LANE). See
# docs/HANDOFF-audio-latency-foundation.md.
LEAN_STAGED_CONFIG_NAME = "sound_lean_staged.yml"

# The generated YAML header carries a cosmetic ``(id=<profile_id>)`` marker
# (see ``jasper.sound.camilla_yaml.emit_sound_config`` — it is the ONLY place
# ``profile_id`` reaches the emitted YAML). A wizard save stamps a wall-clock
# ``time.time_ns()`` id; reconcile's dry-run stamps ``RECONCILE_PROFILE_ID``. So
# the on-disk file and a freshly re-emitted candidate differ in this header
# even when the DSP is byte-identical otherwise. Strip the marker on both sides
# before the "is the config unchanged?" comparison so the no-op path can fire on
# a redeploy.
#
# Anchored to the exact ``# Auto-generated JTS DSP config (id=...).`` header line
# (group 1 is that line minus the marker) so a stray ``(id=...)`` substring
# elsewhere in the YAML — e.g. inside a device name like
# ``hw:CARD=x (id=realA)`` — is NEVER stripped. A genuine change to such a value
# must still register as different, so no real change can be masked.
_CONFIG_ID_HEADER_RE = re.compile(
    r"^(# Auto-generated JTS DSP config) \(id=[^)]*\)\.$",
    re.MULTILINE,
)


def _config_without_id_header(text: str) -> str:
    """Return ``text`` with the cosmetic ``(id=...)`` header marker removed."""

    return _CONFIG_ID_HEADER_RE.sub(r"\1.", text)


def _log_reconcile_result(payload: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "result": payload.get("status"),
    }
    for field, key in (
        ("reason", "reason"),
        ("carrier", "carrier_kind"),
        ("current", "current_config_path"),
        ("candidate", "candidate_config_path"),
        ("active", "active_config_path"),
        ("output_trim", "output_trim_db"),
        ("sound_filters", "sound_filter_count"),
        ("room_peqs", "room_peq_count"),
    ):
        value = payload.get(key)
        if value is not None:
            fields[field] = value
    apply = payload.get("apply")
    if isinstance(apply, dict) and apply.get("op_id"):
        fields["op_id"] = apply["op_id"]
    log_event(logger, "sound.reconcile_current_dsp", fields=fields)
    return payload


def default_camilla_factory():
    from jasper.camilla import CamillaController

    host = os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1")
    port = int(os.environ.get("JASPER_CAMILLA_PORT", "1234"))
    return CamillaController(host, port)


def _paths_match(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return Path(left) == Path(right)


async def load_profile_config(
    profile: SoundProfile,
    *,
    profile_path: str | Path,
    config_dir: str | Path,
    camilla_factory: Callable[[], Any] = default_camilla_factory,
    source: str,
    persist_profile: bool,
    audition: bool = False,
    output_trim_db: float = 0.0,
    profile_id: str | None = None,
    writer_lock_held: bool = False,
) -> tuple[Any, Path, SoundProfile]:
    """Render and load ``profile`` on top of the currently loaded DSP graph.

    This is the durable sibling of the browser's live-draft path: resolve the
    current graph to a carrier, re-emit under the shared DSP writer lock, validate,
    load, confirm, and optionally persist the saved profile.
    """

    from jasper.dsp_apply import apply_dsp_config
    from jasper.sound.camilla_yaml import (
        sound_audition_config_path,
        sound_config_path,
    )
    from jasper.sound.graph_carrier import carrier_for_loaded_config

    config_path = Path(config_dir)
    config_path.mkdir(parents=True, exist_ok=True)
    render_id = profile_id if profile_id is not None else str(time.time_ns())
    out_path = (
        sound_audition_config_path(config_path)
        if audition
        else sound_config_path(config_path)
    )
    cam = camilla_factory()

    # Fast pre-check: refuse non-hostable graphs before recording an apply failure
    # for handled active/custom/dynamic-pipe graph refusals. The authoritative
    # check repeats inside the writer lock below.
    pre_path = await cam.get_config_file_path(best_effort=False)
    if not pre_path:
        raise RuntimeError("CamillaDSP did not report a loaded config path")
    pre_carrier = carrier_for_loaded_config(pre_path, config_dir=config_path)
    if (
        not pre_carrier.can_host_eq
        or pre_carrier.kind in {"active", "active_leader_program_bake"}
    ):
        pre_carrier.reemit(profile, output_trim_db=output_trim_db)

    async def _prepare_config() -> dict[str, Any]:
        current_path = await cam.get_config_file_path(best_effort=False)
        if not current_path:
            raise RuntimeError("CamillaDSP did not report a loaded config path")
        carrier = carrier_for_loaded_config(current_path, config_dir=config_path)
        result = carrier.reemit(
            profile,
            out_path=out_path,
            profile_id=render_id,
            output_trim_db=output_trim_db,
        )
        return {
            "prior_config_path": current_path,
            "room_peq_count": result.room_peq_count,
            "sound_filter_count": len(build_sound_filters(profile)),
        }

    apply_state = await apply_dsp_config(
        source=source,
        candidate_path=out_path,
        prepare=_prepare_config,
        load_config=lambda path: cam.set_config_file_path(
            path,
            best_effort=False,
        ),
        get_current_config_path=lambda: cam.get_config_file_path(
            best_effort=True,
        ),
        persist=(lambda: save_profile(profile, profile_path))
        if persist_profile
        else None,
        sound_filter_count=len(build_sound_filters(profile)),
        acquire_lock=not writer_lock_held,
    )
    return apply_state, out_path, profile


async def reconcile_current_dsp(
    *,
    profile_path: str | Path = PROFILE_PATH,
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
    camilla_factory: Callable[[], Any] = default_camilla_factory,
    force: bool = False,
) -> dict[str, Any]:
    """Refresh the current JTS-owned generated DSP graph from saved intent.

    ``sound_profile.json`` and ``sound_settings.json`` are source of truth. The
    CamillaDSP YAML is a derived artifact. This function deliberately skips
    unknown or non-hostable graphs instead of trying to patch arbitrary YAML.
    """

    from jasper.dsp_apply import dsp_writer_lock
    from jasper.sound.camilla_yaml import sound_audition_config_path, sound_config_path
    from jasper.sound.graph_carrier import (
        CarrierCannotHostEq,
        carrier_for_loaded_config,
    )

    config_path = Path(config_dir)
    profile = load_profile(profile_path)
    settings = load_sound_settings()
    trim_db = output_trim_db(profile, settings)
    sound_filter_count = len(build_sound_filters(profile))
    cam = camilla_factory()
    out_path = sound_config_path(config_path)
    audition_path = sound_audition_config_path(config_path)

    async with dsp_writer_lock(config_path):
        current_path = await cam.get_config_file_path(best_effort=False)
        if not current_path:
            return _log_reconcile_result(
                {
                    "status": "skipped",
                    "reason": "camilla_config_path_missing",
                    "current_config_path": None,
                    "candidate_config_path": str(out_path),
                    "output_trim_db": trim_db,
                    "sound_filter_count": sound_filter_count,
                }
            )

        if _paths_match(current_path, audition_path):
            return _log_reconcile_result(
                {
                    "status": "skipped",
                    "reason": "active_audition",
                    "message": "sound_audition.yml is an unsaved preview",
                    "current_config_path": str(current_path),
                    "output_trim_db": trim_db,
                    "sound_filter_count": sound_filter_count,
                }
            )

        carrier = carrier_for_loaded_config(current_path, config_dir=config_path)
        try:
            dry = carrier.reemit(
                profile,
                profile_id=RECONCILE_PROFILE_ID,
                output_trim_db=trim_db,
            )
        except CarrierCannotHostEq as exc:
            return _log_reconcile_result(
                {
                    "status": "skipped",
                    "reason": exc.reason_code,
                    "message": exc.message,
                    "carrier_kind": carrier.kind,
                    "current_config_path": str(current_path),
                    "output_trim_db": trim_db,
                    "sound_filter_count": sound_filter_count,
                }
            )

        if (
            not force
            and carrier.kind == "base_flat"
            and sound_filter_count == 0
            and trim_db == 0.0
            and dry.room_peq_count == 0
        ):
            return _log_reconcile_result(
                {
                    "status": "skipped",
                    "reason": "flat_profile_noop",
                    "carrier_kind": carrier.kind,
                    "current_config_path": str(current_path),
                    "candidate_config_path": str(out_path),
                    "output_trim_db": trim_db,
                    "sound_filter_count": sound_filter_count,
                    "room_peq_count": dry.room_peq_count,
                }
            )
        if not force and _paths_match(current_path, out_path):
            try:
                on_disk = _config_without_id_header(
                    out_path.read_text(encoding="utf-8")
                )
                if on_disk == _config_without_id_header(dry.yaml):
                    return _log_reconcile_result(
                        {
                            "status": "unchanged",
                            "carrier_kind": carrier.kind,
                            "current_config_path": str(current_path),
                            "candidate_config_path": str(out_path),
                            "output_trim_db": trim_db,
                            "sound_filter_count": sound_filter_count,
                            "room_peq_count": dry.room_peq_count,
                        }
                    )
            except OSError:
                pass

        apply_state, applied_path, _ = await load_profile_config(
            profile,
            profile_path=profile_path,
            config_dir=config_path,
            camilla_factory=lambda: cam,
            source="sound_reconcile",
            persist_profile=False,
            output_trim_db=trim_db,
            profile_id=RECONCILE_PROFILE_ID,
            writer_lock_held=True,
        )
    return _log_reconcile_result(
        {
            "status": "reconciled",
            "carrier_kind": carrier.kind,
            "current_config_path": str(current_path),
            "candidate_config_path": str(applied_path),
            "active_config_path": apply_state.active_config_path,
            "output_trim_db": trim_db,
            "sound_filter_count": sound_filter_count,
            "room_peq_count": apply_state.room_peq_count or 0,
            "apply": apply_state.to_dict(),
        }
    )


def stage_lean_capture_config(
    *,
    profile_path: str | Path = PROFILE_PATH,
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
    capture_pipe_path: str | None = None,
    topology: Any = None,
) -> dict[str, Any]:
    """Emit + validate + classify a Stage-4b lean File-capture config WITHOUT
    live-loading it.

    The smallest safe step toward the lean lane: it proves the lean *mechanics*
    are sound — a File-capture source + the v4 async resampler + the unchanged
    ``outputd_content_playback`` target are statically valid
    (``camilladsp --check``) and L0-graph-safe (``classify_camilla_graph``) —
    with zero audio risk. It never touches the production ``/sound`` carrier
    path, never live-loads, and leaves ``emit_sound_config``'s solo byte
    contract intact (the lean kwargs default to ``None`` for every existing
    caller). It writes a dedicated staging file (``sound_lean_staged.yml``).

    **Not playback-faithful (deliberately).** The staged config carries only the
    saved *preference* profile filters — it does NOT preserve the household's
    room-correction PEQs or output/headroom trim, which the live
    :func:`load_profile_config` path reads from the currently-loaded config and
    the sound settings. That's fine here because this artifact is only
    ``--check``'d, never loaded. The mux wiring (gated on ``JASPER_LEAN_LANE``,
    task #8) MUST re-emit the lean lane through the carrier — preserving room
    PEQs + trim — before it live-loads anything, or a calibrated household
    would lose its correction the instant the lane goes live.

    The lean config changes only CamillaDSP's CAPTURE (an ALSA fan-in lane → a
    ``File`` pipe source); the PLAYBACK stays ``outputd_content_playback``, so
    jasper-outputd needs zero change and the classifier (which keys on the
    source marker + playback role, never ``capture.type``) classifies it like a
    normal stereo sound config. ``topology=None`` lets the classifier load the
    box's *real* saved topology — so on an active/roleful speaker this stereo
    config is correctly REFUSED (``graph_unsafe``); lean staging for an active
    box needs the Layer-A active emitter and is a follow-up.

    Returns a status dict, ``status`` one of:
      - ``"staged"``       — emitted, ``--check`` ok-to-apply, classify allowed.
      - ``"invalid"``      — ``camilladsp --check`` rejected the config.
      - ``"graph_unsafe"`` — ``classify_camilla_graph`` refused (NOT allowed,
        e.g. a stereo config on an active/roleful topology).
    Never live-loads, never raises on a validation/classification miss — the
    miss is reported (via the status + a structured WARN), not swallowed. Only
    an ``emit_sound_config`` contract violation (a caller bug) raises.
    """

    from jasper.active_speaker.runtime_contract import classify_camilla_graph
    from jasper.dsp_apply import validate_camilla_config
    from jasper.sound.camilla_yaml import emit_sound_config

    config_path = Path(config_dir)
    config_path.mkdir(parents=True, exist_ok=True)
    staged_path = config_path / LEAN_STAGED_CONFIG_NAME
    fifo = capture_pipe_path or DEFAULT_LEAN_CAPTURE_FIFO
    profile = load_profile(profile_path)
    render_id = str(time.time_ns())

    # emit_sound_config's own guards (File capture requires enable_rate_adjust
    # + an async resampler, and rejects a File-in/File-out config) make this
    # fail-loud-correct: a guard violation here is a caller bug and SHOULD
    # raise — it is never a runtime-degrade path. The resampler is the v4
    # object form (AsyncSinc/Balanced); the deployed CamillaDSP rejects the
    # pre-v2 scalar. Playback is UNCHANGED (outputd_content_playback default).
    yaml = emit_sound_config(
        profile,
        out_path=staged_path,
        profile_id=render_id,
        capture_pipe_path=fifo,
        resampler_type=DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE,
        resampler_profile=DEFAULT_FILE_CAPTURE_RESAMPLER_PROFILE,
        enable_rate_adjust=True,
    )

    # Static validity: camilladsp --check. No FIFO needed (a static YAML check
    # opens no devices). MISSING (no binary on a dev host) is ok_to_apply, so
    # the emitter stays exercisable hardware-free.
    validation = validate_camilla_config(staged_path)
    if not validation.ok_to_apply:
        log_event(
            logger,
            "sound.lean_stage",
            result="invalid",
            candidate=str(staged_path),
            capture_pipe=fifo,
            detail=validation.error or validation.stderr_tail,
            level=logging.WARNING,
        )
        return {
            "status": "invalid",
            "candidate_config_path": str(staged_path),
            "capture_pipe_path": fifo,
            "validator": validation.to_dict(),
        }

    # L0 re-prove: the lean config must classify as allowed (a normal stereo
    # sound config) for the saved topology. Never stage an unproven graph.
    graph = classify_camilla_graph(topology=topology, text=yaml)
    if not graph.allowed:
        log_event(
            logger,
            "sound.lean_stage",
            result="graph_unsafe",
            candidate=str(staged_path),
            capture_pipe=fifo,
            classification=graph.classification,
            level=logging.WARNING,
        )
        return {
            "status": "graph_unsafe",
            "candidate_config_path": str(staged_path),
            "capture_pipe_path": fifo,
            "classification": graph.classification,
            "issues": [dict(i) for i in graph.issues],
        }

    log_event(
        logger,
        "sound.lean_stage",
        result="staged",
        candidate=str(staged_path),
        capture_pipe=fifo,
        classification=graph.classification,
        camilla_classification=graph.camilla_classification,
    )
    return {
        "status": "staged",
        "candidate_config_path": str(staged_path),
        "capture_pipe_path": fifo,
        "classification": graph.classification,
        "camilla_classification": graph.camilla_classification,
    }
