# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Runtime apply/reconcile helpers for saved sound preference DSP graphs."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from jasper.audio_runtime_plan import fanin_coupling_capture_kwargs
from jasper.log_event import log_event
from jasper.sound.profile import (
    PROFILE_PATH,
    SoundProfile,
    build_sound_filters,
    load_profile,
    save_profile,
)
from jasper.sound.settings import SoundSettings, load_sound_settings, output_trim_db

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path("/var/lib/camilladsp/configs")
RECONCILE_PROFILE_ID = "reconcile-current-dsp"


@dataclass(frozen=True)
class _SavedDspRender:
    """One carrier render of the persisted preference/settings intent."""

    profile: SoundProfile
    output_path: Path
    yaml: str
    carrier_kind: str
    output_trim_db: float
    sound_filter_count: int
    room_peq_count: int

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


def _running_config_is_intent(current_path: str | Path, dry_yaml: str) -> bool:
    """Does the config CamillaDSP is RUNNING already carry ``dry_yaml``'s DSP?

    The question this asks is deliberately about the running config and not
    about ``sound_current.yml``, the file the reconcile would write. Those are
    the same file on an ordinary stereo box and are NOT the same file on a
    speaker running a kept active-crossover candidate
    (``active_speaker_baseline_candidate_<hash>.yml``) — and asking the
    narrower question there is the #2572 defect:

    The active carrier recomposes from the immutable applied-profile record, so
    the CONTENT survives a reconcile; only the NAME moves. The old check was
    gated on the running config being the write target, which a candidate never
    is, so identical bytes were written under a second filename and CamillaDSP's
    statefile stopped naming the candidate. From there the applied record and the
    statefile disagree on a pure path compare
    (:func:`~jasper.active_speaker.baseline_profile.applied_profile_displacement`),
    the record reads as DISPLACED, and the crossover-v2 round loses its entry
    graph identity — a kept correction stops chaining into the next round even
    though the speaker never stopped playing it. Observed on jts3 2026-08-15:
    post-deploy ``sound_current.yml`` and the kept candidate had the same
    sha256. An identity move, not a content move.

    Compared modulo the cosmetic ``(id=...)`` header (see
    :data:`_CONFIG_ID_HEADER_RE`) exactly as the same-path comparison always
    was; this is that comparison with its path precondition dropped, so a box
    where the two paths DO match answers identically to before.

    FAIL-SAFE: an unreadable or undecodable running config answers ``False`` and
    falls through to the ordinary write-and-apply path. A comparison that cannot
    be made is never read as "nothing to do".
    """

    try:
        running = Path(current_path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    return _config_without_id_header(running) == _config_without_id_header(dry_yaml)


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
    from jasper.camilla import primary_controller

    return primary_controller()


def _paths_match(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return Path(left) == Path(right)


def _render_saved_dsp_on_carrier(
    base_config_path: str | Path,
    *,
    profile_path: str | Path,
    config_dir: str | Path,
    coupling: str | None,
    write: bool,
    profile: SoundProfile | None = None,
    settings: SoundSettings | None = None,
) -> _SavedDspRender:
    """Compose persisted program DSP onto ``base_config_path``.

    This is the one render boundary shared by the reset-safe materializer and
    reconcile's dry run. Carrier dispatch owns graph compatibility and room-PEQ
    preservation; the sound profile/settings files own preference EQ and output
    trim. ``write=False`` has no filesystem mutation.
    """

    from jasper.sound.camilla_yaml import sound_config_path
    from jasper.sound.graph_carrier import CarrierCannotHostEq, carrier_for_loaded_config

    config_path = Path(config_dir)
    selected_profile = profile if profile is not None else load_profile(profile_path)
    selected_settings = settings if settings is not None else load_sound_settings()
    trim_db = output_trim_db(selected_profile, selected_settings)
    out_path = sound_config_path(config_path)
    carrier = carrier_for_loaded_config(base_config_path, config_dir=config_path)
    if write:
        config_path.mkdir(parents=True, exist_ok=True)
    try:
        result = carrier.reemit(
            selected_profile,
            out_path=out_path if write else None,
            profile_id=RECONCILE_PROFILE_ID,
            output_trim_db=trim_db,
            fanin_coupling_capture_kwargs=fanin_coupling_capture_kwargs(coupling),
        )
    except CarrierCannotHostEq as exc:
        raise CarrierCannotHostEq(
            exc.reason_code,
            exc.message,
            carrier_kind=carrier.kind,
        ) from exc
    return _SavedDspRender(
        profile=selected_profile,
        output_path=out_path,
        yaml=result.yaml,
        carrier_kind=carrier.kind,
        output_trim_db=trim_db,
        sound_filter_count=len(build_sound_filters(selected_profile)),
        room_peq_count=result.room_peq_count,
    )


def materialise_saved_dsp_on_carrier(
    base_config_path: str | Path,
    *,
    profile_path: str | Path = PROFILE_PATH,
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
    coupling: str | None = None,
) -> Path:
    """Write saved program DSP onto a proved carrier and return its path.

    The write is atomic and targets canonical ``sound_current.yml``. This
    function deliberately does not acquire the DSP writer lock, ask CamillaDSP
    to load the graph, or mutate the saved profile/settings. Its caller owns the
    surrounding transaction and must re-prove the returned graph before load.

    Carrier incompatibility raises
    :class:`jasper.sound.graph_carrier.CarrierCannotHostEq`; I/O failures are
    allowed to propagate. There is no flat-graph fallback.
    """

    return _render_saved_dsp_on_carrier(
        base_config_path,
        profile_path=profile_path,
        config_dir=config_dir,
        coupling=coupling,
        write=True,
    ).output_path


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
    coupling: str | None = None,
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
    cam = camilla_factory()

    # Fast pre-check: refuse non-hostable graphs before recording an apply failure
    # for handled active/custom/dynamic-pipe graph refusals. The authoritative
    # check repeats inside the writer lock below.
    pre_path = await cam.get_config_file_path(best_effort=False)
    if not pre_path:
        raise RuntimeError("CamillaDSP did not report a loaded config path")
    out_path = (
        sound_audition_config_path(config_path)
        if audition
        else sound_config_path(config_path)
    )
    pre_carrier = carrier_for_loaded_config(pre_path, config_dir=config_path)
    if (
        not pre_carrier.can_host_eq
        or pre_carrier.kind in {"active", "active_leader_program_bake"}
    ):
        pre_carrier.reemit(
            profile,
            output_trim_db=output_trim_db,
        )

    # SHARED fan-in→Camilla coupling: resolve the capture/playback-device kwargs
    # ONCE (explicit override from the coupling reconciler, else the live env).
    # Default loopback -> {} -> byte-identical emit. Stereo carriers apply the
    # shm-ring devices; active baselines and grouped pipe sinks keep their own
    # topology-specific paths.
    coupling_capture_kwargs = fanin_coupling_capture_kwargs(coupling)

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
            fanin_coupling_capture_kwargs=coupling_capture_kwargs,
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
    coupling: str | None = None,
) -> dict[str, Any]:
    """Refresh the current JTS-owned generated DSP graph from saved intent.

    ``sound_profile.json`` and ``sound_settings.json`` are source of truth. The
    CamillaDSP YAML is a derived artifact. This function deliberately skips
    unknown or non-hostable graphs instead of trying to patch arbitrary YAML.
    """

    from jasper.dsp_apply import dsp_writer_lock
    from jasper.sound.camilla_yaml import sound_audition_config_path, sound_config_path
    from jasper.sound.graph_carrier import CarrierCannotHostEq

    config_path = Path(config_dir)
    profile = load_profile(profile_path)
    settings = load_sound_settings()
    trim_db = output_trim_db(profile, settings)
    sound_filter_count = len(build_sound_filters(profile))
    cam = camilla_factory()
    default_out_path = sound_config_path(config_path)
    audition_path = sound_audition_config_path(config_path)

    async with dsp_writer_lock(
        config_path,
        source="sound_reconcile_current_dsp",
    ):
        current_path = await cam.get_config_file_path(best_effort=False)
        if not current_path:
            return _log_reconcile_result(
                {
                    "status": "skipped",
                    "reason": "camilla_config_path_missing",
                    "current_config_path": None,
                    "candidate_config_path": str(default_out_path),
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

        try:
            dry = _render_saved_dsp_on_carrier(
                current_path,
                profile_path=profile_path,
                config_dir=config_path,
                coupling=coupling,
                write=False,
                profile=profile,
                settings=settings,
            )
        except CarrierCannotHostEq as exc:
            return _log_reconcile_result(
                {
                    "status": "skipped",
                    "reason": exc.reason_code,
                    "message": exc.message,
                    "carrier_kind": exc.carrier_kind,
                    "current_config_path": str(current_path),
                    "output_trim_db": trim_db,
                    "sound_filter_count": sound_filter_count,
                }
            )

        out_path = dry.output_path
        coupling_capture_kwargs = fanin_coupling_capture_kwargs(coupling)

        if (
            not force
            # A non-loopback coupling (shm_ring) must flip the shared graph even on
            # a flat profile. This noop used to fire before topology differences
            # were considered, which can strand endpoints on different transports.
            # When coupling kwargs are set, fall through to the YAML diff below so
            # the arm actually applies.
            and not coupling_capture_kwargs
            and dry.carrier_kind == "base_flat"
            and sound_filter_count == 0
            and trim_db == 0.0
            and dry.room_peq_count == 0
        ):
            return _log_reconcile_result(
                {
                    "status": "skipped",
                    "reason": "flat_profile_noop",
                    "carrier_kind": dry.carrier_kind,
                    "current_config_path": str(current_path),
                    "candidate_config_path": str(out_path),
                    "output_trim_db": trim_db,
                    "sound_filter_count": sound_filter_count,
                    "room_peq_count": dry.room_peq_count,
                }
            )
        # The saved intent is ALREADY what the speaker is playing, whatever that
        # config is named — so there is nothing to refresh. Returning here is
        # what keeps a kept active-crossover candidate the running config
        # instead of re-writing its own bytes under ``sound_current.yml`` and
        # displacing the applied-profile record from the statefile (#2572; see
        # ``_running_config_is_intent``). ``current=`` and ``candidate=`` on the
        # journal line below name both paths, so an operator can see when this
        # left a NON-``sound_current.yml`` graph in place.
        if not force and _running_config_is_intent(current_path, dry.yaml):
            return _log_reconcile_result(
                {
                    "status": "unchanged",
                    "reason": "running_config_matches_intent",
                    "carrier_kind": dry.carrier_kind,
                    "current_config_path": str(current_path),
                    "candidate_config_path": str(out_path),
                    "output_trim_db": trim_db,
                    "sound_filter_count": sound_filter_count,
                    "room_peq_count": dry.room_peq_count,
                }
            )

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
            coupling=coupling,
        )
    return _log_reconcile_result(
        {
            "status": "reconciled",
            "carrier_kind": dry.carrier_kind,
            "current_config_path": str(current_path),
            "candidate_config_path": str(applied_path),
            "active_config_path": apply_state.active_config_path,
            "output_trim_db": trim_db,
            "sound_filter_count": sound_filter_count,
            "room_peq_count": apply_state.room_peq_count or 0,
            "apply": apply_state.to_dict(),
        }
    )
