# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The env files the reconcile pass publishes: the outputd.env candidate it
stages, validates and commits; fan-in's route keys and outputd's latency
floor, both planned by :mod:`jasper.audio_control.audio_runtime_plan`; and the published
files' group and mode.

Each step takes the pass, whose state says where the stage is and what the
steps before it wrote; ``Pass.execute`` decides the order.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from jasper.atomic_io import (
    ENV_FILE_LOCK_TIMEOUT_SECONDS,
    advisory_file_lock,
    env_key_action,
    env_lock_path,
)
from jasper.audio_hardware.reconcile_common import (
    ENV_DIR_MODE,
    ENV_FILE_MODE,
    _ensure_dir,
    _log_token,
)

if TYPE_CHECKING:
    from jasper.audio_hardware.reconcile import Pass


def repair_generated_env_permissions(run: Pass) -> None:
    for path in (run.outputd_env_file, run.fanin_env_file):
        target = Path(path)
        if not target.is_file():
            continue
        # A content-current but root:root file is unreadable to the
        # non-root status daemons, and /state then drifts from root doctor.
        try:
            os.chown(path, -1, target.parent.stat().st_gid)
        except OSError:
            pass
        os.chmod(path, ENV_FILE_MODE)


# -- the outputd.env stage ---------------------------------------------------


def stage_outputd_env(run: Pass) -> None:
    directory = Path(run.outputd_env_file).parent
    _ensure_dir(directory, ENV_DIR_MODE)
    # See ADR-0235 G8. The hold spans snapshot -> rename, so a second
    # whole-file publisher cannot discard this candidate's base.
    held = True
    try:
        run.outputd_env_stage_hold.enter_context(
            advisory_file_lock(
                env_lock_path(run.outputd_env_file),
                timeout_sec=ENV_FILE_LOCK_TIMEOUT_SECONDS,
            )
        )
    except (OSError, TimeoutError):
        held = False
        run.log(
            "outputd_env_stage_unlocked",
            outputd_env=run.outputd_env_file,
            reason="stage_lock_unheld",
        )
    if held:
        # Debris an earlier pass could not clean (a SIGKILL mid-stage).
        # Gated on the hold: a refused hold means a live holder mid-stage,
        # whose in-flight candidate this must not delete.
        for stale in directory.glob(".outputd.env.candidate.*"):
            stale.unlink(missing_ok=True)
            Path(env_lock_path(str(stale))).unlink(missing_ok=True)
    handle, stage = tempfile.mkstemp(
        prefix=".outputd.env.candidate.", dir=directory
    )
    os.close(handle)
    run.outputd_env_stage = stage
    if Path(run.outputd_env_file).is_file():
        shutil.copy2(run.outputd_env_file, stage)
    else:
        try:
            os.chown(stage, -1, directory.stat().st_gid)
        except OSError:
            pass
        os.chmod(stage, ENV_FILE_MODE)


def cleanup_outputd_env_stage(run: Pass) -> None:
    """End the stage: drop the candidate, its own lock, and the live hold."""
    stage = run.outputd_env_stage
    if stage:
        Path(stage).unlink(missing_ok=True)
        Path(env_lock_path(stage)).unlink(missing_ok=True)
    run.outputd_env_stage_hold.close()


def finish_outputd_env_stage(run: Pass) -> None:
    cleanup_outputd_env_stage(run)
    run.outputd_env_stage = None


def validate_outputd_env_stage(run: Pass) -> bool:
    # lazy: patch target — the tests replace it on the source module,
    # which only a per-call import sees.
    from jasper.audio_control.audio_runtime_plan import validate_outputd_env

    stage = run.outputd_env_stage
    if stage is None:
        return True
    try:
        ok, lines = validate_outputd_env(
            base_env=run.env_file,
            outputd_env=stage,
            outputd_label=run.outputd_env_file,
            camilla_statefile=run.camilla_statefile,
            camilla2_statefile=run.camilla2_statefile,
            output_topology=run.output_topology_path,
            topology=run.saved_topology(),
        )
    # noqa reason: a validator that cannot answer must REJECT the candidate,
    # never abort the pass — the refusal is what preserves the running env.
    except Exception as exc:  # noqa: BLE001
        run.mark_degraded()
        ok, lines = False, (f"{type(exc).__name__}: {exc}",)
    detail = "; ".join(lines)
    if ok:
        # The validator accepted the candidate, and MAY have reported a
        # coherent-but-transient state (`ok note=...` — today the
        # ACTIVE-ring arm waypoint). Log it so the journal carries the
        # whole reason a mid-ladder box goes silent at its next Camilla
        # load; the reconcile proceeds either way.
        note = next((line for line in lines if line.startswith("ok note=")), "")
        if note:
            run.log(
                "outputd_env_note",
                outputd_env=run.outputd_env_file,
                detail=_log_token(note[len("ok note=") :]),
            )
        return True
    run.log(
        "outputd_env_invalid",
        outputd_env=run.outputd_env_file,
        preserved=1,
        detail=_log_token(detail),
    )
    return False


def commit_outputd_env_stage(run: Pass) -> bool:
    stage = run.outputd_env_stage
    if stage is None:
        return False
    if not validate_outputd_env_stage(run):
        run.outputd_env_stage_rejected = True
        finish_outputd_env_stage(run)
        return False
    live = Path(run.outputd_env_file)
    if live.is_file() and live.read_bytes() == Path(stage).read_bytes():
        finish_outputd_env_stage(run)
        return False
    try:
        if live.exists():
            info = live.stat()
            os.chown(stage, info.st_uid, info.st_gid)
        else:
            os.chown(stage, -1, live.parent.stat().st_gid)
    except OSError:
        pass
    os.chmod(stage, ENV_FILE_MODE)
    os.replace(stage, live)
    finish_outputd_env_stage(run)
    return True


# -- route and latency-floor env ---------------------------------------------


def apply_route_env(run: Pass) -> bool:
    """Apply the route-owned fan-in env actions. Returns whether it moved."""
    # lazy: import cost — the route plan is a policy layer the --print-env
    # path never reaches (ADR-0226).
    from jasper.audio_control.audio_runtime_plan import (
        resolve_audio_route_profile,
        route_owned_env_actions,
    )
    from jasper.env_load import read_env_file_state  # lazy: with the plan

    run.route_fanin_changed = False
    try:
        base = read_env_file_state(run.env_file)
        actions = route_owned_env_actions(
            resolve_audio_route_profile(base.values)
        )
    # noqa reason: a route plan that cannot be built leaves fanin.env alone;
    # the pass still reconciles the DAC.
    except Exception:  # noqa: BLE001
        run.mark_degraded()
        run.log("route_env_skip", reason="audio_config_unavailable")
        return False
    changed = run.set_env_file_var(
        run.fanin_env_file, [env_key_action(action) for action in actions]
    )
    run.route_fanin_changed = changed
    run.log(
        "route_env",
        fanin_env=run.fanin_env_file,
        changed=int(changed),
        fanin_changed=int(run.route_fanin_changed),
    )
    return changed


def apply_latency_floor_env(run: Pass, dac_id: str) -> None:
    """Apply the active DAC's codified latency floor into outputd.env.

    The decisions come from jasper.audio_control.audio_runtime_plan (operator env >
    profile floor > packaged default, in one policy layer); this only
    performs the requested mutations and reports whether the file moved.

    A probe that cannot answer leaves the four keys ALONE, the same way
    the DAC-format and content-format probes do: clearing them would
    silently drop a tuned box to packaged defaults with no error anywhere,
    while a stale floor is the loud option.
    """
    # lazy: import cost — --print-env never reaches the floor policy (ADR-0226).
    from jasper.audio_control.audio_runtime_plan import outputd_floor_plan

    try:
        summary, actions = outputd_floor_plan(
            profile_id=dac_id,
            base_env=run.env_file,
            outputd_env=run.outputd_env_target,
        )
    # noqa reason: any failure preserves the previous floor keys; the pass
    # is marked degraded so the shim leaves no stamp to skip against.
    except Exception:  # noqa: BLE001
        run.mark_degraded()
        run.latency_floor_changed = False
        run.log(
            "latency_floor_skip",
            reason="probe_unavailable",
            output_dac_id=dac_id,
            outputd_env=run.outputd_env_file,
        )
        return
    run.latency_floor_changed = run.set_env_file_var(
        run.outputd_env_target, [env_key_action(action) for action in actions]
    )
    run.log(
        "latency_floor",
        output_dac_id=dac_id,
        camilla_chunksize=summary.get("JASPER_CAMILLA_CHUNKSIZE") or "default",
        camilla_target_level=(
            summary.get("JASPER_CAMILLA_TARGET_LEVEL") or "default"
        ),
        outputd_period_frames=(
            summary.get("JASPER_OUTPUTD_PERIOD_FRAMES") or "default"
        ),
        outputd_dac_buffer_frames=(
            summary.get("JASPER_OUTPUTD_DAC_BUFFER_FRAMES") or "default"
        ),
        changed=int(run.latency_floor_changed),
    )
