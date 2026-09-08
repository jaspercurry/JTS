# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared web/operator orchestration for active-speaker measurement tests.

The active-speaker domain already owns the safety state machines:
``commission_load`` loads guarded graphs, ``commission_ramp`` gates audible
driver steps, and ``safe_playback`` records floor confirmation. This module wires those
pieces into one reusable operator service so HTTPS correction can run the same
measurement prerequisites without importing the `/sound/` page module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Awaitable, Callable

from jasper.active_speaker import capture_entry_anchor
from jasper.active_speaker.calibration_level import (
    AUDIBLE_RAMP_STEP_DB,
    calibration_level_payload,
    clamp_test_level_dbfs,
    load_calibration_level_state,
)
from jasper.active_speaker.commission_ramp import (
    load_ramp_state,
)
from jasper.active_speaker.commission_wiring import (
    CommissionPresetResolutionError,
    commission_load_config,
    commission_seams,
    read_current_config_path,
    resolve_commission_preset,
    resolve_commission_inputs,
    write_commission_path_safety,
)
from jasper.active_speaker.driver_protection import driver_excitation_floor_hz
from jasper.active_speaker.measurement import (
    load_measurement_state,
    record_summed_test_artifact,
)
from jasper.active_speaker.restore_wait import resilient_restore
from jasper.active_speaker.safe_playback import (
    arm_safe_playback_session,
    load_safe_playback_state,
    record_safe_playback_result,
)
from jasper.active_speaker.staging import (
    DEFAULT_CAMILLA_CONFIG_DIR as DEFAULT_CAMILLA_CONFIG_DIR,
    load_staged_startup_config,
    stage_protected_startup_config,
)
from jasper.active_speaker.commission_load import (
    load_commission_load_state,
    load_summed_commissioning_config,
    rollback_driver_commissioning_config,
)
from jasper.active_speaker.startup_load import (
    load_protected_startup_config,
    load_startup_load_state,
    staged_topology_match_status,
)
from jasper.active_speaker.topology_tone import build_summed_topology_tone_plan
# P6c-ii dissolved the static COMMISSION_TONE_ALSA_DEVICE alias: the lane's
# device is no longer one import-time constant but this box's armed-vs-unarmed
# transport, resolved fresh per use by correction_play_device() (the lane's
# one reader) so a spawn and the payload reporting it can never disagree in
# steady state about which transport the box is on.
from jasper.audio_measurement.correction_lane import (
    CORRECTION_TONE_DIR,
    correction_play_device,
)
from jasper.camilla import CamillaUnavailable
from jasper.platform.status_socket import MUX_CONTROL_SOCKET_PATH
from jasper.dsp_apply import same_config_file
from jasper.json_fields import finite_float as _finite
from jasper.log_event import log_event
from jasper.output_topology import (
    OutputTopology,
    load_output_topology,
    output_topology_mutation,
    set_channel_protection_status,
)

from ._common import blocker_issue as _issue

logger = logging.getLogger(__name__)

CamillaFactory = Callable[[], Any]

COMMISSION_TONE_DURATION_S = 35.0
COMMISSION_TONE_RESTART_MARGIN_S = 3.0
COMMISSION_TONE_STARTUP_CHECK_S = 0.08
COMMISSION_TONE_SAMPLE_RATE = 48000
COMMISSION_TONE_SOURCE_DBFS = 0.0
COMMISSION_TONE_BACKEND = "correction_substream_continuous_tone"
SUMMED_COMMISSION_SPEECH_BACKEND = "correction_substream_summed_speech"
COMMISSION_TONE_FANIN_LABEL = "correction"


_EVIDENCE_READ_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    json.JSONDecodeError,
)
_MUX_COMMAND_ERRORS = (
    OSError,
    RuntimeError,
    json.JSONDecodeError,
    UnicodeError,
)
_COMMISSION_OPERATION_ERRORS = (
    CamillaUnavailable,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
)
_TASK_SETTLE_ERRORS = (
    Exception,
    asyncio.CancelledError,
    KeyboardInterrupt,
    SystemExit,
    GeneratorExit,
)
_PLAYBACK_OPERATION_ERRORS = (
    OSError,
    RuntimeError,
    subprocess.TimeoutExpired,
)


async def attempt_graph_restore(
    restore: Callable[[], Awaitable[Any]],
) -> tuple[bool, str | None]:
    """Run one graph restore and never raise: ``(took_effect, raise_message)``.

    The one verdict the swap transaction reaches, here and on
    ``program_playback``'s measurement path: it TOOK, it RAISED (message
    present), or CamillaDSP REJECTED it (``False``, no message). Both failures
    are returned rather than collapsed to a bool because they are different
    failures at the same call site — #2198 is what an absent distinction costs.
    Callers own the consequence, which is the half that legitimately differs:
    a restore inside a ``finally`` reports, one inside an ``except`` raises.
    """
    try:
        restored = await restore()
    except _COMMISSION_OPERATION_ERRORS as exc:
        return False, str(exc)
    return restored is True, None


# Both live in ``restore_wait`` so a caller that only needs to put a graph back
# does not import this module's commissioning stack to get there.
_resilient = resilient_restore


_SUMMED_TEST_ARM_REPORT: dict[str, Any] = {
    "status": "ready",
    "load_gate": "ready",
    "ok_to_load_active_config": True,
    "camilla_config": {},
    "safe_playback": {},
    "issues": [],
}


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def request_missing_software_guards(
    topology: OutputTopology,
) -> tuple[OutputTopology, bool]:
    """Return intent with commissioning's required guard requests applied."""

    updated = topology
    changed = False
    for group in topology.speaker_groups:
        if not str(group.mode or "").startswith("active_"):
            continue
        for channel in group.channels:
            if not channel.protection_required:
                continue
            if channel.protection_status in {"present", "software_guard_requested"}:
                continue
            updated = set_channel_protection_status(
                updated,
                speaker_group_id=group.id,
                role=channel.role,
                protection_status="software_guard_requested",
            )
            changed = True
    return updated, changed


def ensure_missing_software_guards() -> tuple[OutputTopology, bool]:
    """Fresh-read and persist missing protection requests transactionally."""

    with output_topology_mutation() as mutation:
        topology = mutation.snapshot().topology
        updated, changed = request_missing_software_guards(topology)
        if changed:
            mutation.save(updated)
        return updated, changed


def regenerate_crossover_preview_from_current_draft(
    *, durable: bool = False
) -> dict[str, Any]:
    """Rebuild and persist a fresh crossover preview from the saved design draft.

    This is the exact machinery ``/sound/``'s Preview button drives
    (``jasper.web.sound_active_speaker._active_speaker_crossover_preview_save_payload``):
    request any missing software guards on the current topology, rebuild the
    design draft against it (preserving the saved draft's own inputs and
    revision), then persist through :func:`~jasper.active_speaker.crossover_preview.save_crossover_preview`
    — the one real generator, never reimplemented. Exposed here rather than
    imported from the `/sound/` page module so a second caller (the v2 flow's
    session-start preview ensure) can reuse it without importing a wizard page
    — the same reason this module already re-exposes
    :func:`request_missing_software_guards` for its own startup-anchor use.

    ``durable`` passes straight through to :func:`~jasper.active_speaker.crossover_preview.save_crossover_preview`;
    the default keeps routine Preview regenerations cheap, and the v2 flow's
    crossover-accept caller opts in.

    Returns whatever :func:`~jasper.active_speaker.crossover_preview.save_crossover_preview`
    produces, ready or not — callers decide what a non-ready result means.
    """
    from jasper.active_speaker.crossover_preview import save_crossover_preview
    from jasper.active_speaker.design_draft import build_design_draft, load_design_draft

    draft = load_design_draft()
    if draft.get("status") not in {"not_saved", "unreadable"}:
        saved_revision = draft.get("revision", 0)
        topology, _guards_changed = ensure_missing_software_guards()
        draft = build_design_draft(
            topology,
            driver_research_request=draft.get("driver_research_request"),
            driver_research=draft.get("driver_research"),
            manual_settings=draft.get("manual_settings"),
            operator_inputs=draft.get("operator_inputs"),
            created_at=draft.get("created_at"),
            updated_at=draft.get("updated_at"),
        )
        draft["revision"] = saved_revision
    return save_crossover_preview(draft, durable=durable)


def _stage_startup_config(
    topology: OutputTopology,
    *,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if preset is None and crossover_preview is None:
        from jasper.active_speaker.crossover_preview import load_crossover_preview
        from jasper.active_speaker.design_draft import load_design_draft

        design_draft = load_design_draft()
        crossover_preview = load_crossover_preview(
            current_design_draft=design_draft
        )
    return stage_protected_startup_config(
        topology,
        preset=preset,
        crossover_preview=crossover_preview,
    )


async def _load_startup_config(
    camilla_factory: CamillaFactory,
    *,
    path_safety_evidence_path: str | Path | None = None,
) -> dict[str, Any]:
    topology = load_output_topology()
    cam = camilla_factory()
    return await load_protected_startup_config(
        topology,
        load_config=lambda path: cam.set_config_file_path(path, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(best_effort=False),
        path_safety_evidence_path=path_safety_evidence_path
        or _path_safety_evidence_path(),
    )


def _path_safety_evidence_path() -> str | None:
    from jasper.active_speaker.path_safety import path_safety_evidence_path

    evidence_path = os.environ.get("JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE")
    if evidence_path and evidence_path.strip():
        return evidence_path.strip()
    default_path = path_safety_evidence_path()
    return str(default_path) if default_path.exists() else None


# The five commissioning blockers BOTH operator surfaces mint —
# /sound/speaker/crossover/
# here and /sound/ through `jasper.web.sound_setup`. This module is their ONE
# owner and /sound/ imports these factories, the same way it already imports the
# commission-tone helpers rather than keeping a hand-copied fork (see the import
# block's comment in sound_active_speaker.py). The two surfaces' surrounding
# orchestrations genuinely differ — /sound/ re-saves a crossover preview and
# runs a stoppable playback loop, the crossover page takes resolved inputs
# and plays
# once — so what is shared is the vocabulary, not the flow. Hand-copying the
# sentence is what let the two drift while reading identical.
#
# One factory per code rather than a code->message table, so the literal pair
# stays visible to the AST copy guard in tests/test_sound_setup.py, which reads
# the message an author wrote next to the code.


def commission_startup_anchor_not_staged_issue() -> dict[str, str]:
    return _issue(
        "commission_startup_anchor_not_staged",
        "could not stage the silent active-speaker setup before driver testing",
    )


def commission_startup_anchor_path_safety_blocked_issue() -> dict[str, str]:
    return _issue(
        "commission_startup_anchor_path_safety_blocked",
        "could not verify the silent active-speaker setup path before driver testing",
    )


def commission_startup_anchor_load_failed_issue() -> dict[str, str]:
    return _issue(
        "commission_startup_anchor_load_failed",
        "could not load the silent active-speaker setup before driver testing",
    )


def summed_commission_load_failed_issue() -> dict[str, str]:
    return _issue(
        "summed_commission_load_failed",
        "could not open the combined active-speaker test path",
    )


def summed_commission_rollback_failed_issue() -> dict[str, str]:
    return _issue(
        "summed_commission_rollback_failed",
        "combined test played, but JTS could not re-mute the active-speaker test path",
    )


async def rollback_summed_commission_teardown(
    rollback: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
    *,
    log_event_name: str,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Re-mute the combined-test path. Return ``(rollback, blocker)``, never raise.

    THE ONE owner of the re-mute FAILURE CONTRACT for both operator surfaces,
    because a teardown that raises is a teardown that leaves a household audible
    with no copy telling them so. Both callers run this from a ``finally``, and
    the blocker it returns is the highest-stakes sentence in the whole map
    ("could not restore the quiet setup … before playing anything else"). Which
    rollback to run stays the caller's — the two surfaces reach the same
    ``rollback_driver_commissioning_config`` through their own seam, and each
    keeps the name its own tests substitute.

    The catch is broad ON PURPOSE. This surface used to catch a five-entry
    tuple, so a rollback failure raising anything outside it — a ``KeyError``
    out of a payload, an ``AttributeError`` off a stubbed camilla — escaped the
    ``finally`` unconverted: no issue, no copy, an unhandled exception exactly
    where the household needed a warning. /sound/ already caught broadly; that
    is the behaviour that survives. Consolidating here keeps the repo's
    broad-catch count flat rather than adding a second handler.
    """

    try:
        return await rollback(), None
    except Exception as exc:  # noqa: BLE001 - a silent teardown failure is the bug.
        log_event(
            logger,
            log_event_name,
            level=logging.WARNING,
            action="rollback",
            status="failed",
            error=str(exc),
        )
        return None, summed_commission_rollback_failed_issue()


def _blocked_startup_anchor(
    *,
    group: str,
    role: str,
    issue: dict[str, str],
    startup_setup: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "startup_setup": startup_setup,
        "preflight": None,
        "load": {
            "status": "blocked",
            "last_action": "startup_anchor_blocked",
            "target": {"speaker_group_id": group, "role": role},
            "issues": [issue],
        },
    }


async def _ensure_commission_startup_anchor(
    *,
    group: str,
    role: str,
    staged_config: dict[str, Any],
    current_config_path: str | None,
    camilla_factory: CamillaFactory,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ensure commissioning has the silent startup graph as rollback anchor."""

    if preset is not None and crossover_preview is not None:
        raise ValueError(
            "commissioning startup anchor requires one resolved graph source"
        )
    staged_path = (staged_config.get("config") or {}).get("path")

    # A PATH MATCH IS NOT AN ANCHOR MATCH, and this second term is why. The
    # staged pair's metadata records the topology it was built for, so a box
    # whose saved topology has moved since — a DAC swap, a role edit — can hold
    # a staged graph at the very path this check is about to accept, describing
    # hardware the box no longer has. Reusing it would anchor a commissioning
    # rollback to a graph for the wrong speaker.
    #
    # SHARED WITH /sound/, deliberately: this is the same two-term gate
    # `sound_setup._active_speaker_ensure_commission_startup_anchor` has run
    # since the jts5 2026-08-06 regression, and `staged_topology_mismatch` is a
    # mapped household code. This surface short-circuited on the path term
    # alone, so the two surfaces disagreed about what "already loaded" meant and
    # only one of them could ever emit that code (#2285).
    #
    # A mismatch is NOT a refusal — it falls through to the re-stage below,
    # which rebuilds the pair against the topology the box actually has. The log
    # line is what makes the re-stage attributable rather than silent.
    topology = load_output_topology()
    staged_topology = staged_topology_match_status(topology, staged_config)
    paths_match = same_config_file(current_config_path, staged_path)
    if paths_match and bool(staged_topology.get("matched")):
        return {"status": "already_loaded", "staged_config_path": staged_path}
    if paths_match:
        log_event(
            logger,
            "active_speaker.web_commission_startup_anchor",
            action="startup_anchor",
            group=group,
            role=role,
            status="refresh_required",
            reason="staged_topology_mismatch",
        )

    topology, _guards_changed = ensure_missing_software_guards()
    stage = _stage_startup_config(
        topology,
        preset=preset,
        crossover_preview=crossover_preview,
    )
    if stage.get("status") != "staged":
        return _blocked_startup_anchor(
            group=group,
            role=role,
            issue=commission_startup_anchor_not_staged_issue(),
            startup_setup={"status": "blocked", "stage": stage},
        )

    staged = load_staged_startup_config()
    cam = camilla_factory()
    path, error = await read_current_config_path(cam)
    evidence_path = write_commission_path_safety(topology, staged, path, error)

    from jasper.active_speaker.path_safety import evaluate_path_safety_evidence

    try:
        report = evaluate_path_safety_evidence(
            json.loads(Path(evidence_path).read_text(encoding="utf-8"))
        )
    except _EVIDENCE_READ_ERRORS as exc:
        report = {"status": "blocked", "load_gate": "blocked", "error": str(exc)}
    if report.get("load_gate") != "ready":
        return _blocked_startup_anchor(
            group=group,
            role=role,
            issue=commission_startup_anchor_path_safety_blocked_issue(),
            startup_setup={"status": "blocked", "stage": stage, "path_safety": report},
        )

    startup_load = await _load_startup_config(
        camilla_factory,
        path_safety_evidence_path=evidence_path,
    )
    load_state = _dict_value(startup_load.get("load"))
    if load_state.get("status") != "loaded" or not load_state.get(
        "rollback_available"
    ):
        return _blocked_startup_anchor(
            group=group,
            role=role,
            issue=commission_startup_anchor_load_failed_issue(),
            startup_setup={
                "status": "blocked",
                "stage": stage,
                "path_safety": report,
                "startup_load": startup_load,
            },
        )

    return {
        "status": "loaded",
        "staged_config_path": _dict_value(stage.get("config")).get("path"),
        "path_safety_load_gate": report.get("load_gate"),
        "startup_load_status": load_state.get("status"),
        "rollback_available": bool(load_state.get("rollback_available")),
    }


def _commission_tone_target_key(
    *,
    role: str,
    group_id: str | None,
    target: dict[str, Any] | None,
) -> str:
    target = target or {}
    output_index = target.get("output_index")
    if output_index is None:
        output_index = target.get("physical_output_index")
    return ":".join(
        [
            str(target.get("speaker_group_id") or group_id or ""),
            str(target.get("role") or target.get("driver_role") or role or ""),
            "" if output_index is None else str(output_index),
        ]
    )


def _commission_tone_wav_path(
    *,
    frequency_hz: float,
    duration_s: float = COMMISSION_TONE_DURATION_S,
) -> Path:
    from jasper.audio_measurement.playback import ensure_sine_wav

    return ensure_sine_wav(
        freq_hz=frequency_hz,
        duration_s=duration_s,
        dbfs=COMMISSION_TONE_SOURCE_DBFS,
        sample_rate=COMMISSION_TONE_SAMPLE_RATE,
        cache_dir=CORRECTION_TONE_DIR,
    )


def _combined_speech_stimulus_wav_path() -> tuple[Path, dict[str, Any]]:
    from jasper.active_speaker.speech_stimulus import ensure_combined_speech_stimulus

    return ensure_combined_speech_stimulus()


def _commission_tone_mux_command(cmd: str) -> dict[str, Any]:
    data = b""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2.0)
        sock.connect(MUX_CONTROL_SOCKET_PATH)
        sock.sendall((cmd + "\n").encode("ascii"))
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    if not data:
        raise RuntimeError("jasper-mux returned no response")
    payload = json.loads(data.decode("utf-8", "replace"))
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError(str(payload["error"]))
    if not isinstance(payload, dict):
        raise RuntimeError("jasper-mux returned a non-object response")
    return payload


def _commission_tone_select_fanin_lane() -> dict[str, Any]:
    try:
        return _commission_tone_mux_command(
            f"TEST_SELECT {COMMISSION_TONE_FANIN_LABEL} active-speaker-commissioning",
        )
    except _MUX_COMMAND_ERRORS:
        # SELECT may have landed even when its response was lost, and the
        # owner-scoped release cannot disturb another feature's gate.
        _commission_tone_release_fanin_lane(reason="select_indeterminate")
        raise


def _commission_tone_release_fanin_lane(*, reason: str) -> dict[str, Any]:
    try:
        payload = _commission_tone_mux_command(
            "TEST_RELEASE active-speaker-commissioning"
        )
    except _MUX_COMMAND_ERRORS as exc:
        log_event(
            logger,
            "active_speaker.web_commission_tone",
            level=logging.WARNING,
            action="fanin_release",
            reason=reason,
            status="failed",
            error=str(exc),
        )
        return {"status": "failed", "reason": reason, "error": str(exc)}
    log_event(
        logger,
        "active_speaker.web_commission_tone",
        action="fanin_release",
        reason=reason,
        status="ok",
        active_source=payload.get("active_source"),
    )
    return payload


async def _commission_tone_select_fanin_lane_async() -> dict[str, Any]:
    """Acquire the test lane without orphaning a late thread-side success."""

    select_task = asyncio.create_task(
        asyncio.to_thread(_commission_tone_select_fanin_lane)
    )
    try:
        # ``to_thread`` workers keep running after their awaiting task is
        # cancelled. Shield this worker so cancellation cannot detach us from
        # a TEST_SELECT that may still land and start a mux lease.
        return await asyncio.shield(select_task)
    except asyncio.CancelledError:

        async def _settle_select_then_release() -> dict[str, Any]:
            try:
                await select_task
            except _TASK_SETTLE_ERRORS:
                # The synchronous selector owns indeterminate-response
                # recovery. If it did not return successfully, there is no
                # successful acquisition for this async boundary to release.
                return {"status": "not_acquired"}
            return await _commission_tone_release_fanin_lane_async(
                reason="select_cancelled"
            )

        await _resilient(_settle_select_then_release())
        raise


async def _commission_tone_release_fanin_lane_async(*, reason: str) -> dict[str, Any]:
    return await asyncio.to_thread(_commission_tone_release_fanin_lane, reason=reason)


def _commission_tone_issue(exc: BaseException) -> dict[str, str]:
    return {
        "severity": "blocker",
        "code": "commission_tone_backend_failed",
        "message": f"could not play commissioning tone: {exc}",
    }


def _commission_tone_driver_style(
    *,
    topology: Any,
    group_id: str | None,
    role: str,
) -> str | None:
    for group in getattr(topology, "speaker_groups", ()):
        if group_id and getattr(group, "id", None) != group_id:
            continue
        for channel in getattr(group, "channels", ()):
            if getattr(channel, "role", None) == role:
                return getattr(channel, "driver_style", None)
    return None


def _commission_tone_signal_plan(
    *,
    role: str,
    group_id: str | None,
    topology: Any = None,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from jasper.active_speaker.test_signal_plan import (
        DRIVER_TEST_SIGNAL_PLAN_KIND,
        driver_test_signal_plan,
    )

    role_id = str(role or "").strip().lower()
    source = "explicit_preset" if preset is not None else "preset_fallback"
    if preset is None and crossover_preview is not None:
        source = "crossover_preview"
    preset_topology = topology
    if preset is None and crossover_preview is not None and preset_topology is None:
        preset_topology = load_output_topology()
    try:
        bound_preset = resolve_commission_preset(
            preset_topology,
            preset=preset,
            crossover_preview=crossover_preview,
        )
    except CommissionPresetResolutionError as exc:
        issues = exc.issues or [
            _issue(
                "commission_tone_preset_unresolved",
                "could not compile the saved crossover preview into a driver test preset",
            )
        ]
        return {
            "artifact_schema_version": 1,
            "kind": DRIVER_TEST_SIGNAL_PLAN_KIND,
            "status": "blocked",
            "role": role_id,
            "frequency_hz": None,
            "preset_source": source,
            "issues": issues,
        }
    except (OSError, ValueError, TypeError) as exc:
        return {
            "artifact_schema_version": 1,
            "kind": DRIVER_TEST_SIGNAL_PLAN_KIND,
            "status": "blocked",
            "role": role_id,
            "frequency_hz": None,
            "preset_source": source,
            "issues": [_issue(
                "commission_tone_preset_unreadable",
                f"could not load active-speaker preset: {exc}",
            )],
        }

    driver_style = (
        _commission_tone_driver_style(
            topology=topology,
            group_id=group_id,
            role=role_id,
        )
        if topology is not None
        else None
    )
    # The preset carries only the stored protective high-pass; a ``full_range``
    # driver's floor lives in the declaration the preview was compiled from.
    preview_drivers = (
        crossover_preview.get("drivers") if isinstance(crossover_preview, dict) else None
    )
    declared_driver = (
        preview_drivers.get(role_id) if isinstance(preview_drivers, dict) else None
    )
    plan = driver_test_signal_plan(
        bound_preset,
        role_id,
        driver_style=driver_style,
        declared_floor_hz=driver_excitation_floor_hz(declared_driver),
    )
    plan["preset_source"] = source
    plan["preset_id"] = getattr(bound_preset, "preset_id", None)
    plan["preset_name"] = getattr(bound_preset, "name", None)
    return plan


def _commission_tone_payload(
    *,
    status: str,
    playback_id: str,
    role: str,
    level_dbfs: float,
    frequency_hz: float | None,
    target: dict[str, Any] | None,
    group_id: str | None,
    audio_emitted: bool,
    issues: list[dict[str, str]],
    session_reused: bool = False,
    fanin_gate: dict[str, Any] | None = None,
    signal_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "backend": COMMISSION_TONE_BACKEND,
        "playback_id": playback_id,
        "audio_emitted": audio_emitted,
        "confirmable": audio_emitted and not issues,
        "continuous": True,
        "session_reused": session_reused,
        "target": target or {"speaker_group_id": group_id, "driver_role": role},
        "tone": {
            "frequency_hz": frequency_hz,
            "source_level_dbfs": COMMISSION_TONE_SOURCE_DBFS,
            "commission_gain_db": level_dbfs,
            "duration_ms": int(round(COMMISSION_TONE_DURATION_S * 1000)),
        },
        "audio_device": {"pcm": correction_play_device()},
        "issues": issues,
    }
    if fanin_gate is not None:
        payload["fanin_gate"] = fanin_gate
    if signal_plan is not None:
        payload["signal_plan"] = signal_plan
    return payload


def _plan_with_issues(
    plan: dict[str, Any],
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    if not issues:
        return plan
    return {
        **plan,
        "status": "blocked",
        "playback_allowed": False,
        "would_play": False,
        "issues": [*_dict_items(plan.get("issues")), *issues],
    }


def commission_status_payload() -> dict[str, Any]:
    """Return the active-speaker operator measurement state."""

    return {
        "commission_load": load_commission_load_state(),
        "ramp": load_ramp_state(),
        "safe_playback": load_safe_playback_state(),
    }


# THE PER-DRIVER TEST TRIO LIVED HERE and is deleted (#2285 / #2628). It was a
# SECOND, unrouted implementation of a shipped feature: nothing reached
# ``start_driver_test`` but an uncalled wrapper in
# ``jasper.web.correction_crossover_backend`` and one test, and its two siblings
# had no caller at all. The live per-driver test is /sound/'s commission-ramp
# trio -- ``/active-speaker/commission-ramp-step``/``-ack``/``-abort`` ->
# ``_active_speaker_commission_ramp_*_payload`` -> ``jasper.active_speaker
# .commission_ramp`` -- which superseded this layer and kept its own routes.
# Wiring these up instead would have given one household feature two
# orchestrations, which is the drift this campaign exists to remove.
#
# WHAT SURVIVED AND WHAT WENT WITH THEM, re-derived rather than asserted after
# the first version of this comment named a live caller that did not exist.
# ``abort_ramp``, ``load_ramp_state`` and ``commission_load_config`` are
# untouched -- /sound/'s ramp routes are their live callers. ``stop_commission_tone``
# and ``play_commission_tone`` were NOT: their only callers were the wrappers
# above, so the same commit that removed those orphaned these, and they are
# deleted here with the module-local session state they owned
# (``_stop_commission_tone_locked`` and its ``_COMMISSION_TONE_SESSION`` /
# ``_COMMISSION_TONE_LOCK`` pair). /sound/ keeps its own same-named locals --
# see the note at ``jasper/web/sound_active_speaker.py``'s commission-tone import
# block, which is why the shared-owner contract in
# tests/test_commission_tone_single_owner.py deliberately excludes them.


def _crossover_frequency_for_group(
    preview: dict[str, Any],
    speaker_group_id: str,
) -> float | None:
    groups = _dict_items(preview.get("groups"))
    for group in groups:
        if group.get("group_id") != speaker_group_id:
            continue
        crossovers = _dict_items(group.get("crossovers"))
        for crossover in crossovers:
            frequency = _finite(crossover.get("proposed_frequency_hz"))
            if frequency is None:
                continue
            if frequency > 0:
                return frequency
    return None


def _transient_summed_level(
    *,
    calibration_level: dict[str, Any],
    measurements: dict[str, Any],
    speaker_group_id: str,
    requested_level: Any,
) -> dict[str, Any]:
    current = _finite(
        _dict_value(calibration_level.get("test_signal")).get("requested_level_dbfs")
    )
    summary = _dict_value(measurements.get("summary"))
    latest_tests = _dict_value(summary.get("latest_summed_tests"))
    latest = latest_tests.get(speaker_group_id)
    latest_record = _dict_value(latest)
    latest_issues = _dict_items(latest_record.get("issues"))
    latest_ok = (
        bool(latest_record)
        and latest_record.get("captured") is True
        and latest_record.get("audio_emitted") is True
        and not any(
            issue.get("severity") == "blocker"
            for issue in latest_issues
        )
    )
    if latest_ok:
        latest_tone = _dict_value(latest_record.get("tone"))
        current = _finite(latest_tone.get("level_dbfs")) or current
    if current is None:
        current = clamp_test_level_dbfs(None)
    requested = _finite(requested_level)
    issues: list[dict[str, str]] = []
    if requested is None:
        level = clamp_test_level_dbfs(current)
    elif requested > current + AUDIBLE_RAMP_STEP_DB:
        level = clamp_test_level_dbfs(current + AUDIBLE_RAMP_STEP_DB)
        issues.append({
            "severity": "warning",
            "code": "audible_ramp_step_limited",
            "message": "requested combined-test level exceeded the bounded step",
        })
    else:
        level = clamp_test_level_dbfs(requested)
    payload = calibration_level_payload(requested_level_dbfs=level)
    payload["last_action"] = "summed_transient_level"
    payload["prior_level_dbfs"] = current
    payload["requested_level_dbfs"] = requested
    payload["applied_delta_db"] = round(level - current, 3)
    payload["issues"] = issues
    return payload


async def _load_summed_commissioning_config(
    *,
    topology: OutputTopology,
    speaker_group_id: str,
    level_dbfs: float,
    startup_gate_calibration_level: dict[str, Any] | None,
    preset: Any,
    crossover_preview: dict[str, Any] | None,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    cam = camilla_factory()
    staged = load_staged_startup_config()
    current_config_path, _ = await read_current_config_path(cam)
    startup_setup = await _ensure_commission_startup_anchor(
        group=speaker_group_id,
        role="summed",
        staged_config=staged,
        current_config_path=current_config_path,
        camilla_factory=camilla_factory,
        preset=preset,
        crossover_preview=crossover_preview,
    )
    if startup_setup.get("status") == "blocked":
        return startup_setup

    staged = load_staged_startup_config()
    current_config_path, current_config_error = await read_current_config_path(cam)
    evidence_path = write_commission_path_safety(
        topology,
        staged,
        current_config_path,
        current_config_error,
    )
    load_config, read_running_config, get_current_config_path = commission_seams(cam)
    payload = await load_summed_commissioning_config(
        topology,
        speaker_group_id=speaker_group_id,
        calibration_level=startup_gate_calibration_level,
        load_config=load_config,
        read_running_config=read_running_config,
        get_current_config_path=get_current_config_path,
        preset=preset,
        crossover_preview=crossover_preview,
        staged_config=staged,
        audible_gain_db=level_dbfs,
        path_safety_evidence_path=evidence_path,
    )
    payload["startup_setup"] = startup_setup
    return payload


async def _rollback_summed_commissioning_config(
    *,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    cam = camilla_factory()
    return await rollback_driver_commissioning_config(
        load_config=commission_load_config(cam),
    )


def _summed_playback_with_issue(
    playback: dict[str, Any],
    *,
    issue: dict[str, str],
    status: str = "failed",
    commissioning_load: dict[str, Any] | None = None,
    rollback: dict[str, Any] | None = None,
    fanin_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = dict(playback)
    out.update({
        "status": status,
        "backend": SUMMED_COMMISSION_SPEECH_BACKEND,
        "audio_emitted": False,
        "confirmable": False,
        "issues": [*_dict_items(playback.get("issues")), issue],
    })
    if commissioning_load is not None:
        out["commissioning_load"] = commissioning_load
    if rollback is not None:
        out["rollback"] = rollback
    if fanin_gate is not None:
        out["fanin_gate"] = fanin_gate
    return out


def _commission_summed_stimulus_issue(exc: BaseException) -> dict[str, str]:
    return _issue(
        "tone_backend_failed",
        f"could not prepare the combined test speech: {exc}",
    )


async def restore_pending_capture_entry_config(
    *,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    """Restore the stashed production entry config once, at sequence exit.

    The counterpart of ``capture_entry_anchor.record_entry``: automatic
    capture attempts leave the persisted CamillaDSP path on the all-muted
    staged anchor between attempts, and this converges it back to the
    production config from sequence entry. Called from recovery surfaces
    (jasper-correction-web's service-start claim boundary). Outcomes:

    - ``idle``: no stash — nothing pending.
    - ``deferred``: CamillaDSP unreachable; stash retained (muted-safe) so a
      later surface can converge.
    - ``superseded``: the persisted path is no longer the staged anchor —
      another owner (a crossover apply, an operator) repointed production;
      the stash is obsolete and cleared without touching CamillaDSP.
    - ``entry_missing``: the stashed config file no longer exists; stash
      cleared, speaker stays on the anchor (muted, never loud).
    - ``restored``: production reloaded, stash cleared.
    """

    entry = capture_entry_anchor.pending_entry()
    if not entry:
        return {"status": "idle"}
    cam = camilla_factory()
    current, current_error = await read_current_config_path(cam)
    if current_error is not None or not current:
        log_event(
            logger,
            "active_speaker.capture_entry_restore",
            level=logging.WARNING,
            status="deferred",
            reason=current_error or "current_config_unknown",
        )
        return {
            "status": "deferred",
            "reason": current_error or "current_config_unknown",
        }
    staged = load_staged_startup_config()
    staged_anchor_path = (staged.get("config") or {}).get("path")
    if not staged_anchor_path or not same_config_file(current, staged_anchor_path):
        capture_entry_anchor.clear()
        log_event(
            logger,
            "active_speaker.capture_entry_restore",
            status="superseded",
            current_config_path=current,
        )
        return {"status": "superseded", "current_config_path": current}
    if not Path(entry).exists():
        capture_entry_anchor.clear()
        log_event(
            logger,
            "active_speaker.capture_entry_restore",
            level=logging.WARNING,
            status="entry_missing",
            entry_config_path=entry,
        )
        return {"status": "entry_missing", "entry_config_path": entry}
    restored = await cam.set_config_file_path(entry, best_effort=False)
    if restored is not True:
        log_event(
            logger,
            "active_speaker.capture_entry_restore",
            level=logging.WARNING,
            status="failed",
            entry_config_path=entry,
        )
        return {"status": "failed", "entry_config_path": entry}
    capture_entry_anchor.clear()
    log_event(
        logger,
        "active_speaker.capture_entry_restore",
        status="restored",
        entry_config_path=entry,
    )
    return {"status": "restored", "config_path": entry}


async def play_summed_capture_sweep(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Play the analyzer sweep through one already-tested summed path."""

    if not isinstance(raw, dict):
        raise ValueError("summed capture sweep request must be an object")
    if blocking_phase is not None:
        return {
            "status": "refused",
            "reason": "measurement_in_progress",
            "blocking_phase": blocking_phase,
            "next_step": "Finish the other measurement before capturing the crossover.",
        }
    speaker_group_id = str(raw.get("speaker_group_id") or "").strip()
    if not speaker_group_id:
        raise ValueError("speaker_group_id is required")
    log_event(
        logger,
        "active_speaker.web_summed_capture_sweep",
        status="refused",
        reason="active_summed_persisted_admission_unavailable",
        group_id=speaker_group_id,
        audio_emitted=False,
    )
    return {
        "status": "refused",
        "reason": "active_summed_persisted_admission_unavailable",
        "audio_emitted": False,
        "issues": [_issue(
            "active_summed_persisted_admission_unavailable",
            (
                "combined crossover capture is paused until its multi-driver "
                "protection authority is available"
            ),
        )],
        "commission": commission_status_payload(),
    }


async def _play_summed_commission_tone(
    plan: dict[str, Any],
    *,
    safe_session: dict[str, Any],
    topology: OutputTopology,
    speaker_group_id: str,
    startup_gate_calibration_level: dict[str, Any] | None,
    preset: Any,
    crossover_preview: dict[str, Any] | None,
    camilla_factory: CamillaFactory,
) -> dict[str, Any]:
    from jasper.active_speaker.playback import start_tone_playback

    artifact_playback = start_tone_playback(
        plan,
        safe_session=safe_session,
        backend=None,
        allow_audio=True,
    )
    if artifact_playback.get("status") != "completed":
        return artifact_playback

    tone = _dict_value(artifact_playback.get("tone"))
    level_dbfs = _finite(tone.get("level_dbfs"))
    if level_dbfs is None:
        level_dbfs = -80.0
    try:
        wav_path, stimulus = _combined_speech_stimulus_wav_path()
        duration_s = max(0.05, float(stimulus.get("duration_s") or 0.0))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _summed_playback_with_issue(
            artifact_playback,
            issue=_commission_summed_stimulus_issue(exc),
        )

    load_payload = await _load_summed_commissioning_config(
        topology=topology,
        speaker_group_id=speaker_group_id,
        level_dbfs=level_dbfs,
        startup_gate_calibration_level=startup_gate_calibration_level,
        preset=preset,
        crossover_preview=crossover_preview,
        camilla_factory=camilla_factory,
    )
    load_state = _dict_value(load_payload.get("load"))
    if load_state.get("status") != "loaded":
        load_issues = _dict_items(load_state.get("issues"))
        issue = (
            load_issues[0] if load_issues else summed_commission_load_failed_issue()
        )
        return _summed_playback_with_issue(
            artifact_playback,
            issue=issue,
            commissioning_load=load_payload,
        )

    from jasper.audio_measurement.playback import play_wav

    fanin_gate: dict[str, Any] | None = None
    rollback: dict[str, Any] | None = None
    rollback_issue: dict[str, str] | None = None
    try:
        fanin_gate = await _commission_tone_select_fanin_lane_async()
        # Off-loop playback: ``play_wav`` runs ``aplay`` via
        # ``asyncio.create_subprocess_exec`` and awaits it, so the shared
        # correction loop stays responsive (status polls, SSE progress, the
        # safe-playback TTL deadman) for the whole stimulus instead of being
        # blocked by a synchronous ``subprocess.run``. Playback has a
        # ``duration_s + 1.0`` bound; ``play_wav`` raises ``SweepPlaybackError``
        # (a ``RuntimeError``) on non-zero exit or timeout, caught below.
        # Resolved ONCE for this operation: the spawn and the payload
        # reporting it use the same transport answer.
        alsa_device = correction_play_device()
        await play_wav(
            wav_path,
            alsa_device=alsa_device,
            timeout_s=duration_s + 1.0,
        )
        playback_result = dict(artifact_playback)
        playback_result.update({
            "status": "completed",
            "backend": SUMMED_COMMISSION_SPEECH_BACKEND,
            "audio_emitted": True,
            "confirmable": True,
            "audio_device": {"pcm": alsa_device},
            "stimulus": stimulus,
            "commissioning_load": load_payload,
            "fanin_gate": fanin_gate,
            "issues": [],
        })
    except _PLAYBACK_OPERATION_ERRORS as exc:
        playback_result = _summed_playback_with_issue(
            artifact_playback,
            issue=_commission_summed_stimulus_issue(exc),
            commissioning_load=load_payload,
            rollback=rollback,
            fanin_gate=fanin_gate,
        )
    finally:
        if fanin_gate is not None:
            await _commission_tone_release_fanin_lane_async(reason="summed_test")
        rollback, rollback_issue = await rollback_summed_commission_teardown(
            lambda: _rollback_summed_commissioning_config(
                camilla_factory=camilla_factory,
            ),
            log_event_name="active_speaker.web_summed_test",
        )
    if rollback is not None:
        playback_result["rollback"] = rollback
    if rollback_issue is not None:
        playback_result["status"] = "failed"
        playback_result["confirmable"] = False
        playback_result["issues"] = [
            *_dict_items(playback_result.get("issues")),
            rollback_issue,
        ]
    return playback_result


async def start_summed_test(
    raw: dict[str, Any],
    *,
    camilla_factory: CamillaFactory,
    blocking_phase: str | None = None,
) -> dict[str, Any]:
    """Run and record one bounded combined-driver test."""

    if not isinstance(raw, dict):
        raise ValueError("summed test request must be an object")
    if blocking_phase is not None:
        return {
            "status": "refused",
            "reason": "measurement_in_progress",
            "blocking_phase": blocking_phase,
            "next_step": "Finish the other measurement before testing the crossover.",
        }
    topology = load_output_topology()
    speaker_group_id = str(raw.get("speaker_group_id") or "").strip()
    if not speaker_group_id:
        raise ValueError("speaker_group_id is required")

    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.playback import start_tone_playback

    design_draft = load_design_draft()
    preview = load_crossover_preview(current_design_draft=design_draft)
    requested_level = raw.get("level_dbfs", raw.get("requested_level_dbfs"))
    measurements = load_measurement_state(topology)
    persisted_calibration_level = load_calibration_level_state()
    calibration_level = (
        _transient_summed_level(
            calibration_level=persisted_calibration_level,
            measurements=measurements,
            speaker_group_id=speaker_group_id,
            requested_level=requested_level,
        )
        if requested_level is not None
        else persisted_calibration_level
    )
    startup_gate_level = calibration_level_payload()
    safe_session = load_safe_playback_state()
    wants_audio = bool(raw.get("audio", True))
    if wants_audio and safe_session.get("status") != "armed":
        safe_session = arm_safe_playback_session(_SUMMED_TEST_ARM_REPORT)
    startup_load = load_startup_load_state()
    protected_loaded = bool(
        startup_load.get("loaded")
        and startup_load.get("rollback_available")
        and startup_load.get("current_config_matches_loaded") is not False
    )
    plan = build_summed_topology_tone_plan(
        topology,
        speaker_group_id=speaker_group_id,
        requested_frequency_hz=(
            raw.get("frequency_hz")
            or _crossover_frequency_for_group(preview, speaker_group_id)
        ),
        requested_level_dbfs=calibration_level.get("test_signal", {}).get(
            "requested_level_dbfs"
        ),
        requested_duration_ms=raw.get("duration_ms", 500),
        playback_allowed=(
            wants_audio
            and safe_session.get("status") == "armed"
            and protected_loaded
        ),
        safe_session_id=safe_session.get("session_id"),
        protected_startup_loaded=protected_loaded,
    )
    summary = _dict_value(measurements.get("summary"))
    if not summary.get("driver_measurements_complete"):
        plan = _plan_with_issues(
            plan,
            [
                {
                    "severity": "blocker",
                    "code": "summed_test_driver_measurements_missing",
                    "message": "test each driver before running the combined test",
                },
            ],
        )
    preset, resolved_preview = resolve_commission_inputs()
    if wants_audio:
        playback = await _play_summed_commission_tone(
            plan,
            safe_session=safe_session,
            topology=topology,
            speaker_group_id=speaker_group_id,
            startup_gate_calibration_level=startup_gate_level,
            preset=preset,
            crossover_preview=resolved_preview,
            camilla_factory=camilla_factory,
        )
    else:
        playback = start_tone_playback(
            plan,
            safe_session=safe_session,
            backend=None,
            allow_audio=False,
        )
    session = record_safe_playback_result(playback)
    measurement_payload = record_summed_test_artifact(
        topology,
        {
            "speaker_group_id": speaker_group_id,
            "playback": playback,
            "plan": plan,
        },
    )
    log_event(
        logger,
        "active_speaker.web_summed_test",
        status=playback.get("status"),
        group_id=speaker_group_id,
        audio_requested=wants_audio,
        audio_emitted=bool(playback.get("audio_emitted")),
        blockers=len(playback.get("issues") or []),
    )
    return {
        "status": playback.get("status"),
        "plan": plan,
        "playback": playback,
        "session": session,
        "calibration_level": calibration_level,
        "measurements": measurement_payload,
        "commission": commission_status_payload(),
    }
