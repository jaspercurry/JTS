# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks for the loaded CamillaDSP graph and the runtime plan.

Import direction across the audio-runtime check modules runs one way —
``audio_runtime_camilla`` -> ``_fanin`` -> ``_outputd`` -> ``_ring``, so this
module may not import from any of the three.

Closed vocabulary for this module's `CheckResult.reason`: one snake_case
constant per distinct decision branch of the checks below, its value unique
across the doctor and prefixed by the check that emits it. `detail` stays the
human sentence (free to reword); `reason` is what tests and self-healing
consumers pin instead (ADR-0233 rule 3).

A branch that formed NO verdict — subsystem not installed, not applicable to
this box, or the evidence source unreachable so nothing was observed — is
`skipped` with a reason, never `ok`. An `ok` reason means an actual verdict a
consumer would branch on (a feature the box turned off, a floor that is
deliberately not renderable).
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
from .correction import (
    REASON_CAMILLA_CONFIG_MISSING,
    REASON_CAMILLA_CONFIG_UNREADABLE,
    REASON_CAMILLA_STATEFILE_UNREADABLE,
)
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import CheckResult, _group_writable_dir, _service_state_failure

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
REASON_RING_CHUNK_CLAMPED = "ring_chunk_clamped"
REASON_RING_TARGET_LEVEL_ABOVE_CEILING = "ring_target_level_above_ceiling"
REASON_RING_CHUNK_ABOVE_CAPACITY = "ring_chunk_above_capacity"

REASON_CAMILLA_PARK_RECORD_UNREADABLE = "camilla_park_record_unreadable"
REASON_CAMILLA_PARK_RECORD_UNINTELLIGIBLE = "camilla_park_record_unintelligible"
REASON_CAMILLA_GRAPH_PARKED = "camilla_graph_parked"


@doctor_check(core=True)
def check_camilla_service() -> CheckResult:
    """The jasper-camilla systemd unit must never stay stopped.

    Owns the CLEAN-stop state its peers miss (#2163): `check_service_runtime_state`
    flags only `failed`, and `check_camilla_websocket` reports it as an
    unreachable 127.0.0.1:1234. "Enabled but not active" is unambiguous here
    because CamillaDSP has no gate that makes `inactive` legitimate, unlike
    jasper-outputd (missing-DAC `ExecCondition`) or jasper-voice
    (`voice-input-absent` marker).

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
    restart-loops it, and the speaker emits nothing. The emitters clamp the
    resolved chunk (``resolve_camilla_latency_for_devices``), so this covers the
    one case the clamp cannot reach — a config written by an OLDER build and
    still on disk.

    Removal condition: delete this check once no supported upgrade path can
    still carry a pre-clamp config onto a box.
    """
    # lazy: import cost, check is not core
    from ...camilla_latency import resolve_camilla_chunksize
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
                speaker_silent=True,
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
            speaker_silent=True,
            reason=REASON_RING_CHUNK_ABOVE_CAPACITY,
        )
    # Say so when the clamp is what put this number here; otherwise the box runs
    # a chunk its own DacProfile does not declare with no on-box explanation.
    # Asked of the SAME resolver the emitters fall back to, never of a second
    # derivation of "which DAC is active".
    fits = (
        f"chunksize={chunksize} fits the ring's {capacity}-frame capacity "
        f"({'/'.join(ring_ends)})"
    )
    unclamped = resolve_camilla_chunksize()
    if unclamped > capacity:
        return CheckResult(
            label, "ok",
            f"{fits}, clamped from the {unclamped} this box resolves to",
            reason=REASON_RING_CHUNK_CLAMPED,
        )
    return CheckResult(label, "ok", fits)

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
        f"coupling={plan.setting('JASPER_FANIN_CAMILLA_COUPLING').value or '(unset)'}, "
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
    surfaced verbatim rather than restated here.
    """
    label = "camilla recovery park"

    from ...control import camilla_recover_state

    state = camilla_recover_state.snapshot()
    status = state.get("status")

    if status == "absent":
        return CheckResult(
            label, "ok", "no core-graph recovery park this boot"
        )

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
        speaker_silent=True,
        reason=REASON_CAMILLA_GRAPH_PARKED,
    )
