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

from jasper.atomic_io import CONFIG_FILE_MODE, atomic_write_text
from jasper.dsp_apply import CANONICAL_CAMILLA_CONFIG_DIR, same_config_file, dsp_writer_lock
from jasper.fanin_coupling import capture_kwargs_for_coupling
from jasper.log_event import log_event
from jasper.sound.profile import (
    PROFILE_PATH,
    SoundProfile,
    build_sound_filters,
    build_sound_filter_slots,
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
    """Compare the loaded bytes with the saved graph, ignoring the stereo render ID."""

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
    """Use CamillaDSP's persisted config path while the daemon is down.

    The saved path still selects the carrier, including its driver protection.
    Reconcile must re-emit that carrier to refresh transport format and geometry;
    the recovery seeder proves graph safety but cannot perform that refresh.
    If the statefile names no graph, selection belongs to
    :mod:`jasper.active_speaker.runtime_contract`.
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
    trim. ``write`` controls the DSP config file.
    """

    from jasper.sound.graph_carrier import CarrierCannotHostEq, carrier_for_loaded_config

    config_path = Path(config_dir)
    selected_profile = profile if profile is not None else load_profile(profile_path)
    selected_settings = settings if settings is not None else load_sound_settings()
    trim_db = output_trim_db(selected_profile, selected_settings)
    carrier = carrier_for_loaded_config(base_config_path, config_dir=config_path)
    if write:
        config_path.mkdir(parents=True, exist_ok=True)
    try:
        result = carrier.reemit(
            selected_profile,
            profile_id=RECONCILE_PROFILE_ID,
            output_trim_db=trim_db,
            fanin_coupling_capture_kwargs=capture_kwargs_for_coupling(),
        )
    except CarrierCannotHostEq as exc:
        raise CarrierCannotHostEq(
            exc.reason_code,
            exc.message,
            carrier_kind=carrier.kind,
        ) from exc
    out_path = carrier.destination(result, config_path)
    if write:
        atomic_write_text(out_path, result.yaml, mode=CONFIG_FILE_MODE)
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

    The carrier selects the destination for the atomic write. This
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
    from jasper.sound.graph_carrier import (
        ReemitResult,
        carrier_for_loaded_config,
        eq_block_for_loaded_config,
    )
    from jasper.sound.live_edit import does_live_edits, plan_live_edit_for

    config_path = Path(config_dir)
    config_path.mkdir(parents=True, exist_ok=True)
    render_id = profile_id if profile_id is not None else str(time.time_ns())
    cam = camilla_factory()

    pre_path = await cam.get_config_file_path(best_effort=False)
    if not pre_path:
        raise RuntimeError("CamillaDSP did not report a loaded config path")
    carrier = carrier_for_loaded_config(pre_path, config_dir=config_path)
    if carrier.kind != "active" or not carrier.can_host_eq:
        pre_block = eq_block_for_loaded_config(
            profile, current_path=pre_path, config_dir=config_path, output_trim_db=output_trim_db,
        )
        if pre_block is not None:
            raise pre_block

    from jasper.active_speaker.baseline_profile import load_composed_graph  # lazy: active graph owner

    async with dsp_writer_lock(config_path, source=source):
        active = carrier.kind == "active"
        tune = carrier.prepare_eq() if active else None
        prepared: dict[str, Any] = {}
        out_path = sound_audition_config_path(config_path) if audition else sound_config_path(config_path)
        coupling_capture_kwargs = capture_kwargs_for_coupling()

        # One shot: apply_dsp_config reuses load_config to ROLL BACK, and an
        # in-place rollback has already put the pre-prepare bytes back on disk, so
        # re-sending the candidate held here would undo exactly that.
        quiet_load: dict[str, str] = {}

        async def _render_config() -> tuple[str, ReemitResult]:
            current_path = await cam.get_config_file_path(best_effort=False)
            if not current_path:
                raise RuntimeError("CamillaDSP did not report a loaded config path")
            current_carrier = carrier_for_loaded_config(current_path, config_dir=config_path)
            rendered = current_carrier.reemit(
                profile, profile_id=render_id, output_trim_db=output_trim_db,
                fanin_coupling_capture_kwargs=coupling_capture_kwargs,
                **({"tune": tune} if current_carrier.kind == "active" else {}),
            )
            if current_carrier.kind != carrier.kind:
                raise RuntimeError("Loaded graph carrier changed during sound preparation")
            if (
                (active or same_config_file(current_path, out_path))
                and does_live_edits(cam)
                and not (await plan_live_edit_for(cam, rendered.yaml)).duck
            ):
                quiet_load["yaml"] = rendered.yaml
                quiet_load["current_path"] = current_path
            return current_path, rendered

        async def _load_config(path: str) -> bool:
            raw = quiet_load.pop("yaml", None)
            if raw is not None:
                if same_config_file(path, quiet_load.pop("current_path", None)):
                    return bool(await cam.set_active_config_raw(raw, best_effort=False, duck=False))
                return bool(await cam.set_config_file_path(path, best_effort=False, duck=False))
            return bool(await cam.set_config_file_path(path, best_effort=False))

        if active:
            async def _prepare_active() -> str:
                _, rendered = await _render_config()
                prepared.update(rendered.applied_profile)
                return rendered.yaml

            async with load_composed_graph(
                _prepare_active, source=source, profile=prepared, config_dir=config_path,
                audition=audition, load_config=_load_config,
                get_current_config_path=lambda: cam.get_config_file_path(best_effort=True),
                persist=(lambda: save_profile(profile, profile_path)) if persist_profile else None,
                record=None if audition else "sound",
                sound_filter_count=len(build_sound_filter_slots(profile)),
            ) as (state, _applied):
                return state, Path(state.candidate_config_path), profile

        async def _prepare_config() -> dict[str, Any]:
            current_path, rendered = await _render_config()
            atomic_write_text(out_path, rendered.yaml, mode=CONFIG_FILE_MODE)
            return {"prior_config_path": current_path, "room_peq_count": rendered.room_peq_count,
                    "sound_filter_count": len(build_sound_filters(profile))}

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

        apply_state, applied_path, _ = await load_profile_config(
            profile,
            profile_path=profile_path,
            config_dir=config_path,
            camilla_factory=lambda: cam,
            source="sound_reconcile",
            persist_profile=False,
            output_trim_db=trim_db,
            profile_id=RECONCILE_PROFILE_ID,
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
