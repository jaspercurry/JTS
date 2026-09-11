# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks for the loaded CamillaDSP graph and the runtime plan.

One-way audio-runtime import chain ``audio_runtime_camilla`` -> ``_fanin`` ->
``_outputd`` -> ``_ring``: this module may not import from any of the three.
`CheckResult.reason` vocabulary and the skipped-vs-ok rule: ADR-0233 rule 3.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...camilla import CamillaController, CamillaUnavailable, primary_controller
from ...camilla_config_contract import (
    DEFAULT_PIPE_SINK_FORMAT,
    DEFAULT_VOLUME_LIMIT_DB,
    parse_camilla_devices_config,
    read_camilla_devices_config,
)
from ...config import Config
from ...fanin_coupling import RING_PCM_DEVICES, ring_capacity_frames
from ...service_units import unit_active
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import (
    CheckResult,
    REASON_CAMILLA_CONFIG_MISSING,
    REASON_CAMILLA_CONFIG_UNREADABLE,
    REASON_CAMILLA_STATEFILE_UNREADABLE,
    _group_writable_dir,
    _parked_ago,
    _service_state_failure,
)

REASON_CAMILLA_UNIT_MISSING = "camilla_unit_missing"
REASON_CAMILLA_UNIT_NOT_ENABLED = "camilla_unit_not_enabled"
REASON_CAMILLA_INACTIVE = "camilla_inactive"

REASON_CAMILLA_UNREACHABLE = "camilla_unreachable"
REASON_CAMILLA_VOLUME_ABOVE_CEILING = "camilla_volume_above_ceiling"

REASON_CAMILLA_CONFIG_DIR_MISSING = "camilla_config_dir_missing"
REASON_CAMILLA_CONFIG_DIR_UNREADABLE = "camilla_config_dir_unreadable"
REASON_CAMILLA_CONFIG_DIR_NOT_WRITABLE = "camilla_config_dir_not_writable"

REASON_PLAYBACK_FORMAT_NO_CONFIG = "playback_format_no_config"
REASON_PLAYBACK_FORMAT_FIELD_ABSENT = "playback_format_field_absent"
REASON_PLAYBACK_FORMAT_MISMATCH = "playback_format_mismatch"

REASON_VOLUME_LIMIT_INVALID = "volume_limit_invalid"
REASON_VOLUME_LIMIT_ABSENT = "volume_limit_absent"
REASON_VOLUME_LIMIT_ABOVE_CEILING = "volume_limit_above_ceiling"

REASON_AUDIO_PLAN_ERRORS = "audio_plan_errors"
REASON_AUDIO_PLAN_WARNINGS = "audio_plan_warnings"

REASON_LIVE_VOLUME_LIMIT_UNAVAILABLE = "live_volume_limit_unavailable"
REASON_LIVE_VOLUME_LIMIT_NO_ACTIVE_GRAPH = "live_volume_limit_no_active_graph"
REASON_LIVE_VOLUME_LIMIT_ABSENT = "live_volume_limit_absent"
REASON_LIVE_VOLUME_LIMIT_ABOVE_CEILING = "live_volume_limit_above_ceiling"

REASON_RING_CHUNK_NOT_APPLICABLE = "ring_chunk_not_applicable"
REASON_RING_TARGET_LEVEL_ABOVE_CAPACITY = "ring_target_level_above_capacity"
REASON_RING_TARGET_LEVEL_ABOVE_CEILING = "ring_target_level_above_ceiling"
REASON_RING_CHUNK_ABOVE_CAPACITY = "ring_chunk_above_capacity"

REASON_CAMILLA_PARK_RECORD_UNREADABLE = "camilla_park_record_unreadable"
REASON_CAMILLA_PARK_RECORD_UNINTELLIGIBLE = "camilla_park_record_unintelligible"
REASON_CAMILLA_PARK_RECORD_STALE = "camilla_park_record_stale"
REASON_CAMILLA_GRAPH_PARKED = "camilla_graph_parked"

REASON_CAMILLA_STATEFILE_TOPOLOGY_MISMATCH = "camilla_statefile_topology_mismatch"
REASON_CAMILLA_TOPOLOGY_GATE_UNREADABLE = "camilla_topology_gate_unreadable"
REASON_CAMILLA_TOPOLOGY_GATE_UNINTELLIGIBLE = "camilla_topology_gate_unintelligible"
REASON_CAMILLA_TOPOLOGY_STAMPS_MISSING = "camilla_topology_stamps_missing"


@doctor_check(core=True)
def check_camilla_service() -> CheckResult:
    """The jasper-camilla systemd unit must never stay stopped.

    Owns the CLEAN-stop state its peers miss (#2163): `check_service_runtime_state`
    flags only `failed`, and `check_camilla_websocket` reports it as an
    unreachable 127.0.0.1:1234. "Enabled but not active" is unambiguous here:
    unlike jasper-outputd (missing-DAC `ExecCondition`) or jasper-voice
    (`voice-input-absent` marker), CamillaDSP's own `ExecCondition` gate skips
    the start only on a topology mismatch, which is a silent speaker and not a
    legitimate rest state — `check_camilla_topology_gate` names that case.

    Returns:
      - ok when enabled and active.
      - fail when the unit is missing, disabled, or enabled and not active.
    """
    label = "jasper-camilla service"
    service_failure = _service_state_failure(
        label,
        "jasper-camilla.service",
        missing=REASON_CAMILLA_UNIT_MISSING,
        not_enabled=REASON_CAMILLA_UNIT_NOT_ENABLED,
        inactive=REASON_CAMILLA_INACTIVE,
    )
    if service_failure is not None:
        return service_failure
    return CheckResult(label, "ok", "enabled and active")


@doctor_check(label="CamillaDSP websocket", needs_cfg=True, is_async=True)
async def check_camilla_websocket(cfg: Config) -> CheckResult:
    controller: CamillaController | None = None
    try:
        controller = CamillaController(cfg.camilla_host, cfg.camilla_port)
        vol = await controller.get_volume_db()
        if vol is None:
            raise CamillaUnavailable("main volume unavailable")
        try:
            clipped = await controller.get_clipped_samples()
            clipped_msg = f" clipped_samples={clipped}"
        except (
            CamillaUnavailable, OSError, RuntimeError, TimeoutError, ValueError,
        ):
            clipped_msg = " clipped_samples=?"
        if float(vol) > DEFAULT_VOLUME_LIMIT_DB + 0.1:
            return CheckResult(
                "CamillaDSP websocket", "fail",
                f"{cfg.camilla_host}:{cfg.camilla_port} volume={vol:.1f} dB "
                f"above {DEFAULT_VOLUME_LIMIT_DB:.1f} dB safety ceiling."
                f"{clipped_msg}",
                reason=REASON_CAMILLA_VOLUME_ABOVE_CEILING,
            )
        return CheckResult(
            "CamillaDSP websocket", "ok",
            f"{cfg.camilla_host}:{cfg.camilla_port} volume={vol:.1f} dB"
            f"{clipped_msg}",
        )
    except (
        CamillaUnavailable, ImportError, OSError, RuntimeError,
        TimeoutError, ValueError,
    ) as e:
        return CheckResult(
            "CamillaDSP websocket", "fail",
            f"can't reach {cfg.camilla_host}:{cfg.camilla_port}: {e}. "
            f"Check `systemctl status jasper-camilla`.",
            reason=REASON_CAMILLA_UNREACHABLE,
        )
    finally:
        if controller is not None:
            await controller.close()


CAMILLA_CONFIGS_DIR = Path("/var/lib/camilladsp/configs")


def _camilla_configs_writable_result(
    path: Path, *, expected_group: str = "jasper"
) -> CheckResult:
    """CheckResult for the CamillaDSP config dir's group-write posture.

    ``jasper-web`` runs non-root and writes staged/commissioning and
    room-correction configs into this dir atomically (temp file in-dir +
    rename), which needs directory group-write. install.sh's intended posture
    is ``root:jasper 2775``; anything narrower fails staging with
    ``PermissionError`` at the wizard instead of here."""

    label = "CamillaDSP config dir writable"
    try:
        st = path.stat()
    except FileNotFoundError:
        return CheckResult(
            label, "warn", f"{path} missing — re-run install.sh",
            reason=REASON_CAMILLA_CONFIG_DIR_MISSING,
        )
    except OSError as exc:
        return CheckResult(
            label, "warn", f"{path}: {exc}",
            reason=REASON_CAMILLA_CONFIG_DIR_UNREADABLE,
        )

    writable, group_name = _group_writable_dir(st, expected_group=expected_group)
    mode = st.st_mode & 0o7777
    detail = f"{path} mode={mode:04o} group={group_name}"
    if not writable:
        return CheckResult(
            label,
            "fail",
            f"{detail} — non-root jasper-web cannot write staged/correction "
            f"configs; fix with `sudo install -d -m 2775 -g {expected_group} "
            f"{path}` and redeploy (active-speaker staging fails with "
            "PermissionError otherwise)",
            reason=REASON_CAMILLA_CONFIG_DIR_NOT_WRITABLE,
        )
    return CheckResult(label, "ok", detail)


@doctor_check()
def check_camilla_configs_writable() -> CheckResult:
    """Guard the CamillaDSP config dir's group-write posture for jasper-web."""

    return _camilla_configs_writable_result(CAMILLA_CONFIGS_DIR)


def _camilla_statefile() -> Path:
    """The statefile behind :meth:`Evidence.camilla_config_path`, from the same
    single read (same memo key)."""
    # Lazy: at module scope this drags `correction` into every `--core` run.
    from .correction import _active_camilla_config_path

    statefile, _config_path = evidence.get(
        "camilla_config", _active_camilla_config_path
    )
    return statefile


def _loaded_device_fields(config_path: Path | str | None) -> dict[str, Any]:
    """Every ``devices.*`` field the audio-runtime checks compare, keyed
    ``<block>_<field>``, from ONE read of ``config_path`` per doctor run.

    The loaded graph's fields must all come from the SAME revision of the file:
    reading them one at a time re-opened it per field and could answer from two
    revisions. ``config_path`` is a parameter rather than always
    :meth:`Evidence.camilla_config_path` because ``check_fanin_coupling`` falls
    back to the shipped config path when the statefile names nothing.
    """
    if not config_path:
        return {}
    # Normalized, so the two callers' spellings of one path share one memo
    # entry and one read.
    path = str(Path(config_path))

    def read() -> dict[str, Any]:
        active = evidence.camilla_config_path()
        if active and path == str(Path(active)):
            text = evidence.camilla_config_text()
            return dict(parse_camilla_devices_config(text)) if text else {}
        return dict(read_camilla_devices_config(path) or {})

    return evidence.get(f"camilla_devices:{path}", read)


def _expected_playback_format(
    playback_type: str | None, playback_device: str | None
) -> tuple[str, str]:
    """``(expected_format, constant_name)`` for a loaded config's playback lane.

    Three lanes, three owners of the width — see
    :func:`check_camilla_playback_format`. The first two predicates are DISJOINT
    in every reachable config (a ``File`` sink carries no ``device`` key), so
    their order is not load-bearing.
    """
    from jasper.fanin_coupling import (
        DEFAULT_PLAYBACK_FORMAT,
        RING_ACTIVE_PLAYBACK_DEVICE,
        RING_PLAYBACK_DEVICE,
        resolve_ring_wire,
    )

    if playback_type == "File":
        return DEFAULT_PIPE_SINK_FORMAT, "DEFAULT_PIPE_SINK_FORMAT"
    # MEMBERSHIP over every ring device, not one `==`: the resolved ring WIRE
    # format is one axis per box, shared by all three ring ends.
    if playback_device in (RING_PLAYBACK_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE):
        return resolve_ring_wire().sample_format, "resolve_ring_wire"
    return DEFAULT_PLAYBACK_FORMAT, "DEFAULT_PLAYBACK_FORMAT"


@doctor_check()
def check_camilla_playback_format() -> CheckResult:
    """The loaded CamillaDSP config's declared playback format must match its
    LANE's expected format.

    LANE-AWARE, and there are THREE lanes (:func:`_expected_playback_format`):

    - a ``File`` sink (the bonded-leader pipe, or the active-speaker parked
      graph's ``/dev/null``) expects ``DEFAULT_PIPE_SINK_FORMAT``, pinned narrow
      by the snapserver wire contract;
    - a ring playback device expects the wire ``resolve_ring_wire`` resolves —
      an armed ring is ``type: Alsa``, so the File split alone does not cover
      it, and the ring LAYOUT accepts both S16LE and S32LE, so nothing but this
      catches a ring config that drifted to the other one;
    - every other sink expects ``DEFAULT_PLAYBACK_FORMAT``.

    Keyed on the LOADED CONFIG's own ``device``/``type``, never on the persisted
    coupling, so a box mid-arm reads as whatever the config in front of it says.

    THIS CHECK FORMS NO VERDICT ON A CONFIG IT CANNOT READ: an
    unreadable/absent statefile, an unresolvable ``config_path``, or a missing
    ``devices.playback.format`` all return ``skipped``. The unreadable half is owned
    by ``check_correction_current_config`` (``jasper/cli/doctor/correction.py``).
    """
    label = "camilla playback format"
    config_path = evidence.camilla_config_path()
    if config_path is None:
        return CheckResult(
            label,
            "skipped",
            "no loaded config to compare",
            reason=REASON_PLAYBACK_FORMAT_NO_CONFIG,
        )
    devices = _loaded_device_fields(config_path)
    loaded_format = devices.get("playback_format")
    if loaded_format is None:
        return CheckResult(
            label, "skipped",
            f"{config_path} has no devices.playback.format field",
            reason=REASON_PLAYBACK_FORMAT_FIELD_ABSENT,
        )
    playback_type = devices.get("playback_type")
    playback_device = devices.get("playback_device")
    expected_format, expected_name = _expected_playback_format(
        playback_type, playback_device
    )
    if loaded_format == expected_format:
        return CheckResult(
            label,
            "ok",
            f"playback format={loaded_format} "
            f"(type={playback_type}, device={playback_device}, "
            f"expected {expected_name})",
        )
    return CheckResult(
        label,
        "fail",
        f"loaded CamillaDSP playback format={loaded_format!r} for playback "
        f"type={playback_type!r} device={playback_device!r}, expected "
        f"{expected_format!r} "
        f"({expected_name}) — a half-flipped box: {config_path} was generated "
        f"against a different {expected_name} than the one currently in "
        "force. Regenerate the config (sudo /opt/jasper/.venv/bin/jasper-sound "
        f"reconcile-current-dsp) or investigate why {expected_name} and the "
        "loaded config disagree.",
        reason=REASON_PLAYBACK_FORMAT_MISMATCH,
    )


@doctor_check(label="CamillaDSP live volume_limit", is_async=True, core=True)
async def check_camilla_live_volume_limit() -> CheckResult:
    """The graph CamillaDSP is RUNNING carries the non-positive fader cap.

    Non-negotiable #1's live half. ``check_camilla_volume_limit`` reads the
    persisted file; this one reads ``GetConfig`` back, which is the only
    surface that sees a graph installed by ``set_active_config_raw`` (no parse
    on upload) or by CamillaGUI (a second writer on the same websocket).
    """
    label = "CamillaDSP live volume_limit"
    controller: CamillaController | None = None
    try:
        controller = primary_controller()
        raw = await controller.get_active_config_raw()
    except (CamillaUnavailable, ImportError, OSError, RuntimeError, TimeoutError, ValueError) as e:
        return CheckResult(
            label, "skipped", f"live config unavailable: {e}",
            reason=REASON_LIVE_VOLUME_LIMIT_UNAVAILABLE,
        )
    finally:
        if controller is not None:
            await controller.close()
    if raw is None or not raw.strip():
        return CheckResult(
            label, "skipped", "CamillaDSP is running no graph",
            reason=REASON_LIVE_VOLUME_LIMIT_NO_ACTIVE_GRAPH,
        )
    limit = parse_camilla_devices_config(raw).get("volume_limit")
    if limit is None:
        return CheckResult(
            label, "fail",
            "the running graph omits devices.volume_limit; CamillaDSP defaults to +50 dB",
            reason=REASON_LIVE_VOLUME_LIMIT_ABSENT,
        )
    if limit > DEFAULT_VOLUME_LIMIT_DB:
        return CheckResult(
            label, "fail",
            f"the running graph sets devices.volume_limit={limit:.1f} dB "
            f"(expected <= {DEFAULT_VOLUME_LIMIT_DB:.1f} dB)",
            reason=REASON_LIVE_VOLUME_LIMIT_ABOVE_CEILING,
        )
    return CheckResult(label, "ok", f"running graph devices.volume_limit={limit:.1f} dB")


def _devices_volume_limit_from_text(text: str) -> float | None:
    """``devices.volume_limit`` from a CamillaDSP config, or None if absent /
    null. Uses the depth-aware shared devices parser so a nested capture or
    playback field cannot masquerade as the global fader ceiling."""
    value = parse_camilla_devices_config(text).get("volume_limit")
    if value is None:
        return None
    return float(value)

@doctor_check(core=True)
def check_camilla_volume_limit() -> CheckResult:
    """Verify the active Camilla config has JTS's non-positive fader cap."""
    config_path = evidence.camilla_config_path()
    if config_path is None:
        return CheckResult(
            "CamillaDSP volume_limit", "warn",
            f"could not read config_path from {_camilla_statefile()}",
            reason=REASON_CAMILLA_STATEFILE_UNREADABLE,
        )
    path = Path(config_path)
    if not path.exists():
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"statefile points at missing config {config_path}",
            reason=REASON_CAMILLA_CONFIG_MISSING,
        )
    text = evidence.camilla_config_text()
    if text is None:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"could not read {config_path}",
            reason=REASON_CAMILLA_CONFIG_UNREADABLE,
        )
    try:
        limit = _devices_volume_limit_from_text(text)
    except ValueError as e:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"invalid devices.volume_limit in {config_path}: {e}",
            reason=REASON_VOLUME_LIMIT_INVALID,
        )
    if limit is None:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"{config_path} omits devices.volume_limit; CamillaDSP "
            "defaults to +50 dB",
            reason=REASON_VOLUME_LIMIT_ABSENT,
        )
    if limit > DEFAULT_VOLUME_LIMIT_DB:
        return CheckResult(
            "CamillaDSP volume_limit", "fail",
            f"{config_path} sets devices.volume_limit={limit:.1f} dB "
            f"(expected <= {DEFAULT_VOLUME_LIMIT_DB:.1f} dB)",
            reason=REASON_VOLUME_LIMIT_ABOVE_CEILING,
        )
    return CheckResult(
        "CamillaDSP volume_limit", "ok",
        f"{config_path} devices.volume_limit={limit:.1f} dB",
    )

@doctor_check()
def check_camilla_ring_chunk_fits() -> CheckResult:
    """Verify a ring-crossing Camilla config asks for a chunk the ring can hold.

    CamillaDSP sets ``avail_min`` to its chunksize and ALSA refuses an
    ``avail_min`` above the device's buffer, so a ring config with a chunk over
    the ring's capacity does not degrade: CamillaDSP exits at open, systemd
    restart-loops it, and the speaker emits nothing. A ring-ended graph takes
    the whole certified ring geometry now
    (``resolve_camilla_latency_for_devices``), so this covers the one case that
    owner cannot reach — a config written by an OLDER build and still on disk.

    Removal condition: delete this check once no supported upgrade path can
    still carry a pre-ring-geometry config onto a box.
    """
    label = "camilla ring chunk"
    config_path = evidence.camilla_config_path()
    if config_path is None:
        return CheckResult(
            label, "warn",
            f"could not read config_path from {_camilla_statefile()}",
            reason=REASON_CAMILLA_STATEFILE_UNREADABLE,
        )
    path = Path(config_path)
    if not path.exists():
        return CheckResult(
            label, "fail", f"statefile points at missing config {config_path}",
            reason=REASON_CAMILLA_CONFIG_MISSING,
        )
    if evidence.camilla_config_text() is None:
        return CheckResult(
            label, "fail", f"could not read {config_path}",
            reason=REASON_CAMILLA_CONFIG_UNREADABLE,
        )
    devices = _loaded_device_fields(config_path)

    ring_ends = [
        name
        for name in (devices.get("capture_device"), devices.get("playback_device"))
        if name in RING_PCM_DEVICES
    ]
    chunksize = devices.get("chunksize")
    if not ring_ends or chunksize is None:
        return CheckResult(
            label, "skipped",
            f"{config_path} names no ring end (chunksize={chunksize})",
            reason=REASON_RING_CHUNK_NOT_APPLICABLE,
        )
    # CamillaDSP's own ceiling on the pair: target_level <= chunksize *
    # (queuelimit + 4), measured against CamillaDSP 4.1.3 and exact across
    # chunk 128/256/512 and queuelimit 1/2/4. Checked separately because a
    # config can carry a chunk that fits the ring and STILL be refused here.
    queuelimit = devices.get("queuelimit")
    target_level = devices.get("target_level")
    if queuelimit is not None and target_level is not None:
        ceiling = int(chunksize) * (int(queuelimit) + 4)
        if int(target_level) > ceiling:
            return CheckResult(
                label, "fail",
                f"{config_path} sets devices.target_level={target_level} with "
                f"chunksize={chunksize} and queuelimit={queuelimit}; CamillaDSP "
                f"refuses a target above {ceiling} and will restart-loop. "
                "Regenerate the config: `sudo jasper-sound reconcile-current-dsp`.",
                reason=REASON_RING_TARGET_LEVEL_ABOVE_CEILING,
            )

    capacity = ring_capacity_frames()
    if int(chunksize) > capacity:
        return CheckResult(
            label, "fail",
            f"{config_path} sets devices.chunksize={chunksize} on "
            f"{'/'.join(ring_ends)}, above the ring's {capacity}-frame capacity. "
            "CamillaDSP cannot open the ring with it and will restart-loop. "
            "Regenerate the config: `sudo jasper-sound reconcile-current-dsp`.",
            reason=REASON_RING_CHUNK_ABOVE_CAPACITY,
        )
    # target_level is the playback-buffer fill CamillaDSP steers towards, so
    # it is judged against the ring only when the ring IS the playback end. A
    # target the ring cannot hold is a graph emitted before the ring geometry
    # owned it (a DAC floor's 1536 against a 256-frame ring): it plays, with
    # rate_adjust off, but is stale — regenerate it.
    if (
        devices.get("playback_device") in RING_PCM_DEVICES
        and target_level is not None
        and int(target_level) > capacity
    ):
        return CheckResult(
            label, "warn",
            f"{config_path} sets devices.target_level={target_level} on "
            f"{devices.get('playback_device')}, above the ring's {capacity}-frame "
            "capacity: a graph emitted before the ring owned its geometry. "
            "Regenerate the config: `sudo jasper-sound reconcile-current-dsp`.",
            reason=REASON_RING_TARGET_LEVEL_ABOVE_CAPACITY,
        )
    return CheckResult(
        label, "ok",
        f"chunksize={chunksize} fits the ring's {capacity}-frame capacity "
        f"({'/'.join(ring_ends)})",
    )

@doctor_check()
def check_audio_runtime_plan() -> CheckResult:
    """Explainable SSOT check for audio latency/coupling knobs."""

    from jasper.audio_runtime_plan import build_audio_runtime_plan_from_system

    plan = build_audio_runtime_plan_from_system()
    # Policy vs observation: see AudioRuntimePlan.camilla_emitted. Reported, not
    # judged — the `camilla ring chunk` check owns the over-capacity failure.
    emitted = plan.camilla_emitted
    summary = (
        f"profile={plan.profile_id}, route={plan.route_mode}, "
        f"route_profile={plan.route_profile.route_id}, "
        f"route_hash={plan.route_config_hash}, "
        f"camilla_policy={plan.setting('JASPER_CAMILLA_CHUNKSIZE').value}/"
        f"{plan.setting('JASPER_CAMILLA_TARGET_LEVEL').value}, "
        + (
            f"camilla_emitted={emitted.chunksize}/{emitted.target_level}, "
            if emitted is not None
            else "camilla_emitted=unread, "
        )
        + f"outputd={plan.setting('JASPER_OUTPUTD_PERIOD_FRAMES').value}/"
        f"{plan.setting('JASPER_OUTPUTD_DAC_BUFFER_FRAMES').value}, "
        f"fanin={plan.setting('JASPER_FANIN_INPUT_BUFFER_FRAMES').value}"
    )
    if plan.errors:
        return CheckResult(
            "audio runtime plan",
            "fail",
            summary + "; " + "; ".join(plan.errors),
            reason=REASON_AUDIO_PLAN_ERRORS,
        )
    if plan.warnings:
        return CheckResult(
            "audio runtime plan",
            "warn",
            summary + "; " + "; ".join(plan.warnings[:3]),
            reason=REASON_AUDIO_PLAN_WARNINGS,
        )
    return CheckResult("audio runtime plan", "ok", summary)


@doctor_check(core=True)
def check_camilla_recover_park() -> CheckResult:
    """The core DSP graph is not parked by jasper-camilla-recover.

    ``deploy/bin/jasper-camilla-recover`` parks the graph when its one bounded
    recovery pass cannot bring it back (ADR-0175), and that park is TERMINAL for
    the boot by design. Severity is ``fail``: the speaker emits NOTHING and no
    automatic path recovers it. The record's own ``action=``/``re_arm=`` text is
    surfaced verbatim rather than restated here. A park the unit's
    ``ExecStartPost=`` already retired is named on the healthy row: the record
    lives in ``/run``, so a graph that parked and came back would otherwise
    leave this boot looking untroubled (R15, #4416).

    A record that survives while jasper-camilla is ACTIVE reads as ``warn``
    (stale), never ``fail``: the removal hook did not fire, but the graph
    itself is producing sound (mirrors outputd's
    ``check_outputd_failure_reconcile_park``).
    """
    label = "camilla recovery park"

    from ...control import camilla_recover_state

    state = camilla_recover_state.snapshot()
    status = state.get("status")
    last_park = state.get("last_park")

    if status == "absent":
        detail = "no core-graph recovery park this boot"
        if isinstance(last_park, dict):
            detail += (
                f" (parked {_parked_ago(last_park.get('parked_at'))}, since "
                f"retired: {last_park.get('reason') or '?'})"
            )
        return CheckResult(label, "ok", detail)

    if status == "unreadable":
        return CheckResult(
            label,
            "warn",
            f"recovery park record at {state.get('path')} exists but could "
            f"not be read ({state.get('error')}) — a park cannot be ruled "
            "out. Check journalctl -u jasper-camilla-recover.",
            reason=REASON_CAMILLA_PARK_RECORD_UNREADABLE,
        )

    if status == "unintelligible":
        return CheckResult(
            label,
            "warn",
            f"recovery park record at {state.get('path')} is present but "
            "carries no reason (a truncated write) — a park cannot be ruled "
            "out from it. Check journalctl -u jasper-camilla-recover.",
            reason=REASON_CAMILLA_PARK_RECORD_UNINTELLIGIBLE,
        )

    if unit_active(evidence.unit_state("jasper-camilla.service")):
        return CheckResult(
            label,
            "warn",
            f"recovery park record at {state.get('path')} is stale — "
            "jasper-camilla is running, so the unit's ExecStartPost removal "
            "did not fire. Delete it; a later unrelated failure would "
            "otherwise read as this park.",
            reason=REASON_CAMILLA_PARK_RECORD_STALE,
        )

    parts = [
        f"PARKED — the core DSP graph was stopped after a failed recovery "
        f"({state.get('reason')})",
    ]
    parked_utc = state.get("parked_utc")
    if parked_utc:
        parts.append(f"at {parked_utc}")
    for field, prefix in (
        ("detail", ""),
        ("action", "ACTION: "),
        ("re_arm", "RE-ARM: "),
    ):
        value = state.get(field)
        if value:
            parts.append(f"{prefix}{value}")
    return CheckResult(
        label,
        "fail",
        ". ".join(parts),
        reason=REASON_CAMILLA_GRAPH_PARKED,
    )


@doctor_check(core=True)
def check_camilla_topology_gate() -> CheckResult:
    """CamillaDSP is not held down by a statefile/topology mismatch.

    ``deploy/bin/jasper-camilla-topology-gate`` is jasper-camilla's
    ``ExecCondition=``: it skips the start when the graph the statefile names
    was proved against a different speaker topology than the one the last
    convergence was working on, so the previous speakers' crossover and
    protection cannot reach these drivers (#4416 R8, ADR-0283). Severity is
    ``fail``: the speaker emits NOTHING and only a convergence that succeeds
    clears it. The record's own ``action=``/``re_arm=`` text is surfaced
    verbatim rather than restated here.

    No refusal is not automatically ``ok``, because the gate ALLOWS on unknown:
    see :func:`_topology_gate_allowed_result`.
    """
    label = "camilla statefile topology"

    from ...control import camilla_topology_gate_state

    state = camilla_topology_gate_state.snapshot()
    status = state.get("status")

    if status == "absent":
        return _topology_gate_allowed_result(label)

    if status == "unreadable":
        return CheckResult(
            label,
            "warn",
            f"topology-gate record at {state.get('path')} exists but could "
            f"not be read ({state.get('error')}) — a refusal cannot be ruled "
            "out. Check journalctl -u jasper-camilla.",
            reason=REASON_CAMILLA_TOPOLOGY_GATE_UNREADABLE,
        )

    if status == "unintelligible":
        return CheckResult(
            label,
            "warn",
            f"topology-gate record at {state.get('path')} is present but "
            "carries no reason (a truncated write) — a refusal cannot be "
            "ruled out from it. Check journalctl -u jasper-camilla.",
            reason=REASON_CAMILLA_TOPOLOGY_GATE_UNINTELLIGIBLE,
        )

    parts = [
        "REFUSED — CamillaDSP was not started because the saved graph belongs "
        f"to a different speaker topology (proved {state.get('proved')}, "
        f"unproved {state.get('unproved')})",
    ]
    refused_utc = state.get("refused_utc")
    if refused_utc:
        parts.append(f"at {refused_utc}")
    for field, prefix in (
        ("detail", ""),
        ("action", "ACTION: "),
        ("re_arm", "RE-ARM: "),
    ):
        value = state.get(field)
        if value:
            parts.append(f"{prefix}{value}")
    return CheckResult(
        label,
        "fail",
        ". ".join(parts),
        reason=REASON_CAMILLA_STATEFILE_TOPOLOGY_MISMATCH,
    )


def _topology_gate_allowed_result(label: str) -> CheckResult:
    """No refusal this boot — but say whether the gate could have refused.

    The gate allows on UNKNOWN, so "no refusal" alone cannot tell a working
    speaker from a blind gate. The proof stamp beside the statefile is what
    makes the comparison possible at all, so its absence on a box running this
    build is the warning: either no convergence has written a statefile since
    the deploy, or the stamp writes are failing (they log
    `event=camilla_topology_stamp.write_failed`).
    """
    from ...active_speaker.environment import camilla_statefile_path
    from ...output_topology import (
        read_topology_fingerprint_stamp,
        statefile_topology_stamp_path,
    )

    statefile = camilla_statefile_path()
    if read_topology_fingerprint_stamp(statefile_topology_stamp_path(statefile)):
        return CheckResult(label, "ok", "no topology-gate refusal this boot")
    return CheckResult(
        label,
        "warn",
        "no topology-gate refusal this boot, but no proof stamp beside "
        f"{statefile} either, so the gate cannot tell this graph's topology "
        "from any other. Run the hardware reconciler "
        "(systemctl start jasper-audio-hardware-reconcile.service) and check "
        "the journal for event=camilla_topology_stamp.write_failed.",
        reason=REASON_CAMILLA_TOPOLOGY_STAMPS_MISSING,
    )
