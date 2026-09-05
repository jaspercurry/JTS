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

from jasper.dsp_apply import CANONICAL_CAMILLA_CONFIG_DIR, same_config_file
from jasper.fanin_coupling import coupling_capture_kwargs_from_env
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

DEFAULT_CONFIG_DIR = CANONICAL_CAMILLA_CONFIG_DIR
RECONCILE_PROFILE_ID = "reconcile-current-dsp"


@dataclass(frozen=True)
class _SavedDspRender:
    """One carrier render of the persisted preference/settings intent."""

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
        ("transport", "transport"),
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


class StatefileCamillaController:
    """Disk-backed stand-in for the live CamillaDSP controller.

    :func:`load_profile_config` asks the daemon exactly two things — "which
    config is loaded?" and "load this one" — and both have an honest on-disk
    answer while the daemon is down: CamillaDSP's statefile names the config it
    will open on its next start. Answering from there is what lets a reconcile
    CONVERGE a box whose CamillaDSP is stopped instead of aborting on a refused
    websocket (#2664).

    Why that matters, from the jts4 incident: install could stop CamillaDSP
    before the reconcile, and the width flip is EXACTLY when the graph must be
    re-emitted — so the one deploy that needed the reconcile most was the one
    that could not reach the daemon. It aborted, and install then started
    CamillaDSP against a statefile still naming the pre-flip graph:
    ``set_format`` EINVAL, five restarts, ``start-limit-hit``.

    This is a TRANSPORT, not a graph choice. The carrier is still resolved from
    the config the statefile already names, so a roleful box re-emits its own
    roleful graph and a flat box its flat one — no topology decision is taken
    or restated here. Choosing a graph when the statefile names none stays
    :mod:`jasper.active_speaker.runtime_contract`'s job (install runs it as
    ``jasper-active-speaker runtime-safe-graph`` immediately before this).

    WHY THE SEEDING CONTRACT IS NOT REUSED HERE. Asking it for a SAFE graph is
    right for a recovery that is deliberately de-arming a box; a deploy is the
    opposite job — it must keep the speaker on its own graph and merely refresh
    it. It also could not have healed jts4 —
    ``classify_camilla_config_text`` reads ``playback_device``,
    ``playback_channels`` and ``volume_limit_db``, never the sample format, so
    the seeding contract re-proves a stale-width graph LEGAL and preserves it.
    Only a re-emit moves the width, which is why this converges through the
    carrier rather than through a second call to the seeder.
    """

    def __init__(self, statefile_path: str | Path | None = None) -> None:
        from jasper.active_speaker.environment import camilla_statefile_path

        self.statefile_path = camilla_statefile_path(statefile_path)

    async def get_config_file_path(
        self, *, best_effort: bool = False
    ) -> str | None:
        from jasper.active_speaker.environment import (
            read_camilla_statefile_config_path,
        )

        return read_camilla_statefile_config_path(self.statefile_path)

    async def set_config_file_path(
        self, path: str, *, best_effort: bool = False
    ) -> bool:
        from jasper.active_speaker.runtime_contract import write_camilla_statefile

        write_camilla_statefile(self.statefile_path, path)
        return True


def _render_saved_dsp_on_carrier(
    base_config_path: str | Path,
    *,
    profile_path: str | Path,
    config_dir: str | Path,
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
            fanin_coupling_capture_kwargs=coupling_capture_kwargs_from_env(),
        )
    except CarrierCannotHostEq as exc:
        raise CarrierCannotHostEq(
            exc.reason_code,
            exc.message,
            carrier_kind=carrier.kind,
        ) from exc
    return _SavedDspRender(
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

    ``coupling`` selects nothing — one transport (ADR-0100), so the capture
    kwargs are the ring whatever any token says. It is accepted only because
    :func:`jasper.active_speaker.runtime_convergence.compose_selected_flat_graph`
    still passes one; remove it with that caller's own coupling thread.
    """

    del coupling
    return _render_saved_dsp_on_carrier(
        base_config_path,
        profile_path=profile_path,
        config_dir=config_dir,
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
    out_path: str | Path | None = None,
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
    from jasper.sound.live_edit import does_live_edits, plan_live_edit_for

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
    # ``out_path`` names the file this render is written to, and WINS over
    # ``audition`` when both are given. The default is the
    # household's own ``sound_current.yml`` (or the audition preview). The
    # reconcile overrides it to RE-ANCHOR: a speaker running a kept
    # active-crossover candidate must keep running THAT file, so a refreshed
    # graph is written back over it rather than appearing under a second name
    # and displacing the applied-profile record from the statefile (#2572). Safe
    # to rewrite in place because the candidate's filename is a fingerprint of
    # its SOURCE inputs, not a hash of its emitted bytes
    # (:func:`jasper.active_speaker.baseline_profile._source_payload`), and the
    # content is recomposed from the same immutable applied-profile snapshot —
    # so the name still describes what the file was built from.
    out_path = (
        Path(out_path)
        if out_path is not None
        else (
            sound_audition_config_path(config_path)
            if audition
            else sound_config_path(config_path)
        )
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
    # ONCE. ONE transport (ADR-0100) — unconditionally the ring, never {}.
    # Stereo carriers apply the shm-ring devices; active baselines keep their
    # own topology-specific paths; grouped pipe sinks keep their own PLAYBACK
    # (capture still follows).
    coupling_capture_kwargs = coupling_capture_kwargs_from_env()

    # One shot: apply_dsp_config reuses load_config to ROLL BACK, and an
    # in-place rollback has already put the pre-prepare bytes back on disk, so
    # re-sending the candidate held here would undo exactly that.
    quiet_load: dict[str, str] = {}

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
        # Rewriting the file CamillaDSP already runs, with a graph it will
        # update in place, is as silent as a live edit, so an A/B the listener
        # is making on purpose does not fade. Loading a DIFFERENT file is a
        # real swap and keeps its bracket; the statefile transport, standing in
        # with CamillaDSP down, cannot be asked at all.
        if (
            same_config_file(str(current_path), out_path)
            and does_live_edits(cam)
            and not (await plan_live_edit_for(cam, result.yaml)).duck
        ):
            quiet_load["yaml"] = result.yaml
        return {
            "prior_config_path": current_path,
            "room_peq_count": result.room_peq_count,
            "sound_filter_count": len(build_sound_filters(profile)),
        }

    async def _load_config(path: str) -> bool:
        raw = quiet_load.pop("yaml", None)
        if raw is not None and same_config_file(path, out_path):
            # The bytes are already at out_path and the loaded path does not
            # move, so this leaves the end state a file reload would have.
            return bool(
                await cam.set_active_config_raw(raw, best_effort=False, duck=False)
            )
        return bool(await cam.set_config_file_path(path, best_effort=False))

    apply_state = await apply_dsp_config(
        source=source,
        candidate_path=out_path,
        prepare=_prepare_config,
        load_config=_load_config,
        get_current_config_path=lambda: cam.get_config_file_path(
            best_effort=True,
        ),
        persist=(lambda: save_profile(profile, profile_path))
        if persist_profile
        else None,
        sound_filter_count=len(build_sound_filters(profile)),
    )
    return apply_state, out_path, profile


async def reconcile_current_dsp(
    *,
    profile_path: str | Path = PROFILE_PATH,
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
    camilla_factory: Callable[[], Any] = default_camilla_factory,
    force: bool = False,
    statefile_path: str | Path | None = None,
) -> dict[str, Any]:
    """Refresh the current JTS-owned generated DSP graph from saved intent.

    ``sound_profile.json`` and ``sound_settings.json`` are source of truth. The
    CamillaDSP YAML is a derived artifact. This function deliberately skips
    unknown or non-hostable graphs instead of trying to patch arbitrary YAML.

    A CamillaDSP that is DOWN does not abort the pass: the reconcile falls back
    to :class:`StatefileCamillaController` and converges the graph the box will
    boot instead (``transport=statefile`` on the result line). One bounded
    fallback, no retry ladder — either the disk answers on the first read or the
    ordinary skip result names why it could not.
    """

    from jasper.camilla import CamillaConfigRejected, CamillaUnavailable
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
        transport = "websocket"
        try:
            current_path = await cam.get_config_file_path(best_effort=False)
        except CamillaConfigRejected:
            # A LIVE daemon that rejected something is not an absent daemon.
            # CamillaConfigRejected subclasses CamillaUnavailable, so catching
            # the parent alone would divert a real config refusal down the
            # disk path and answer it with a statefile write.
            raise
        except CamillaUnavailable:
            # The daemon is down, so there is no running graph to read — but
            # there IS a next one, and the statefile names it. Converging that
            # is the same job over a different transport; see
            # StatefileCamillaController. Reassigning ``cam`` moves the whole
            # remaining pass (dry run, apply, rollback, confirm) onto it, so no
            # apply logic is duplicated for this branch.
            cam = StatefileCamillaController(statefile_path)
            transport = "statefile"
            current_path = await cam.get_config_file_path(best_effort=False)
        if not current_path:
            return _log_reconcile_result(
                {
                    "status": "skipped",
                    "reason": "camilla_config_path_missing",
                    "transport": transport,
                    "current_config_path": None,
                    "candidate_config_path": str(default_out_path),
                    "output_trim_db": trim_db,
                    "sound_filter_count": sound_filter_count,
                }
            )

        if same_config_file(current_path, audition_path):
            return _log_reconcile_result(
                {
                    "status": "skipped",
                    "reason": "active_audition",
                    "transport": transport,
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
                write=False,
                profile=profile,
                settings=settings,
            )
        except CarrierCannotHostEq as exc:
            return _log_reconcile_result(
                {
                    "status": "skipped",
                    "reason": exc.reason_code,
                    "transport": transport,
                    "message": exc.message,
                    "carrier_kind": exc.carrier_kind,
                    "current_config_path": str(current_path),
                    "output_trim_db": trim_db,
                    "sound_filter_count": sound_filter_count,
                }
            )

        out_path = dry.output_path

        # THE FLAT-PROFILE NOOP IS GONE, and its own guard is why. It skipped the
        # apply on a flat box — nothing to EQ, so nothing to write — but it was
        # already conditioned on the coupling kwargs being empty, because a graph
        # that has to name the ring's capture must be written even when the
        # profile is flat. ADR-0100 made those kwargs unconditional, so the skip
        # became unreachable; restoring it would strand a flat box's graph on a
        # lane fan-in does not write. The equality check below is what now
        # short-circuits a flat box, and it compares the actual bytes.
        #
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
                    "transport": transport,
                    "carrier_kind": dry.carrier_kind,
                    "current_config_path": str(current_path),
                    "candidate_config_path": str(out_path),
                    "output_trim_db": trim_db,
                    "sound_filter_count": sound_filter_count,
                    "room_peq_count": dry.room_peq_count,
                }
            )

        # ONE DERIVER, TWO TRIGGERS — not a second writer. The candidate is a
        # DERIVED artifact, and both the commissioning path and this one produce
        # it through the SAME carrier recompose of that candidate's own
        # immutable applied-profile record (`load_profile_config` ->
        # `carrier.reemit`). This branch may never write candidate bytes that
        # differ from that shared recompose; it chooses the destination, never
        # the content. Written that way deliberately so AGENTS.md's
        # single-writer rule survives intact rather than acquiring an exception.
        #
        # RE-ANCHOR, don't displace. The bytes differ, so this box does need a
        # refreshed graph — but a speaker running a kept active-crossover
        # candidate must keep running THAT file. Writing the refresh under
        # ``sound_current.yml`` instead is what moves the statefile off the
        # candidate, leaves the applied record and the statefile disagreeing on
        # a pure path compare, and costs a crossover-v2 round its entry graph
        # (#2572). The equality short-circuit above used to be the whole defence
        # and only held while the emitted content never changed; this holds when
        # it does.
        reanchor_path = (
            current_path
            if dry.carrier_kind == "active"
            and not same_config_file(current_path, out_path)
            else None
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
            out_path=reanchor_path,
        )
    return _log_reconcile_result(
        {
            "status": "reconciled",
            "transport": transport,
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
