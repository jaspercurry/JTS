# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Stage 5: the per-driver floor-unmute gain ramp (the first AUDIBLE step).

A commission load (``startup_load.load_driver_commissioning_config``) arms one
driver's physical output at the protected floor — ``{gain: -120 dB, mute: off}``
— which is silent. Stage 5 is what raises that per-output gain to an audible
level, one bounded step at a time, behind a gate that fails closed. The level
model and bounds are owned by :mod:`calibration_level`; the per-driver
operator-confirmation tri-state is owned by :mod:`safe_playback`; the actual
graph change rides the same guarded inline load as the arm. This module is the
orchestration + the Stage-5 gate that ties them together.

The model (the design-of-record, HANDOFF-active-speaker-dsp.md "Stage 5"):

  armed (-120 dB, silent)
    -> first audible step == the audible floor (MIN_TEST_LEVEL_DBFS, -80 dB)
       -> safe_playback ``floor_pending_operator`` (driver unmuted, awaiting ACK)
       -> operator ACK "heard_correct_driver" -> ``floor_confirmed``
    -> bounded ramp: +AUDIBLE_RAMP_STEP_DB per step toward
       COMMISSION_RAMP_MAX_LEVEL_DBFS (each louder step requires the driver to
       be floor-confirmed first)
  woofer before tweeter (a driver is ramped only after its lower-frequency
  siblings are floor-confirmed), and before ANY tweeter step the protective
  high-pass is re-asserted against the RUNNING graph, not just the file.

The gate's "subsonic/DC protection present" requirement is satisfied by the
protections that already exist in the active graph — the bounded commissioning
gain envelope, the 0 dB volume ceiling, and the per-driver limiter (AGENTS.md
"Assert existing protections only"; a dedicated woofer subsonic high-pass is a
deliberate deferral). The gate does NOT widen ``running_commission_evidence``
with gain bounds — that live gate checks ``mute: off`` at the floor; the gain
envelope and per-step limit live here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

import yaml

from jasper.log_event import log_event

from ._common import issue as _issue
from .calibration_level import (
    AUDIBLE_RAMP_STEP_DB,
    MIN_TEST_LEVEL_DBFS,
)
from .camilla_yaml import (
    COMMISSIONING_HEADROOM_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB,
    STARTUP_MUTE_GAIN_DB,
)
from .graph_evidence import driver_limiter_name
from .safe_playback import (
    arm_safe_playback_session,
    load_safe_playback_state,
    playback_target_signature,
    record_floor_audio_operator_result,
    record_safe_playback_result,
)
from .staging import (
    prepare_driver_commissioning_config,
    running_commission_evidence,
)
from .startup_load import (
    load_commission_load_state,
    load_driver_commissioning_config,
    rollback_driver_commissioning_config,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
RAMP_STATE_KIND = "jts_active_speaker_commission_ramp"
DEFAULT_RAMP_STATE_PATH = Path("/var/lib/jasper/active_speaker_commission_ramp.json")
RAMP_STATE_ENV = "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE"
RAMP_BACKEND = "commission_gain_ramp"
COMMISSION_RAMP_MAX_LEVEL_DBFS = 0.0

# Low-frequency first: a driver is ramped audible only after its lower siblings
# are floor-confirmed. The protective tweeter high-pass is re-asserted live
# before the tweeter is ever raised (running_commission_evidence below).
RAMP_ROLE_ORDER = ("woofer", "mid", "tweeter")

_EPS = 1e-6

PathLoader = Callable[[str], Awaitable[bool]]
RunningConfigReader = Callable[[], Awaitable[str | None]]
ConfigPathReader = Callable[[], Awaitable[str | None]]
ToneEmitter = Callable[..., Awaitable[dict[str, Any]]]


def next_ramp_gain_db(current_gain_db: float) -> float:
    """The next per-output audible gain, given the current one.

    From the silent/armed floor (anything below the audible floor), the first
    audible step is exactly the audible floor (``MIN_TEST_LEVEL_DBFS``). Once
    audible, each step rises by ``AUDIBLE_RAMP_STEP_DB`` and is clamped to the
    Stop-controlled commissioning ramp ceiling. The bound is enforced again by
    the gate; this is the proposer.
    """
    current = float(current_gain_db)
    if current < MIN_TEST_LEVEL_DBFS:
        return MIN_TEST_LEVEL_DBFS
    return min(current + AUDIBLE_RAMP_STEP_DB, COMMISSION_RAMP_MAX_LEVEL_DBFS)


# --- ramp progress state -----------------------------------------------------
#
# Small and group-scoped: which roles have been floor-confirmed (the
# woofer-before-tweeter memory the per-target safe_playback tri-state cannot
# carry across drivers), plus the one step currently awaiting an operator ACK
# (so a second step cannot be taken before the last one is acknowledged). The
# authoritative per-driver floor confirmation lives in safe_playback's
# tri-state; the loaded gain lives in the commission-load state. This file holds
# only what neither of those can.


def ramp_state_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get(RAMP_STATE_ENV) or DEFAULT_RAMP_STATE_PATH)


def _ramp_base_state(path: Path) -> dict[str, Any]:
    return {
        "artifact_schema_version": SCHEMA_VERSION,
        "kind": RAMP_STATE_KIND,
        "state_path": str(path),
        "speaker_group_id": None,
        "confirmed_roles": [],
        "pending": None,
        "last_action": "status",
        "issues": [],
    }


def load_ramp_state(*, state_path: str | Path | None = None) -> dict[str, Any]:
    path = ramp_state_path(state_path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return _ramp_base_state(path)
    if not isinstance(raw, dict):
        return _ramp_base_state(path)
    state = _ramp_base_state(path)
    state.update(raw)
    state["state_path"] = str(path)
    state["confirmed_roles"] = [
        str(r) for r in (state.get("confirmed_roles") or []) if isinstance(r, str)
    ]
    if not isinstance(state.get("pending"), dict):
        state["pending"] = None
    return state


def _normalised_roles(roles: Iterable[str] | None) -> set[str]:
    if roles is None or isinstance(roles, str):
        return set()
    return {
        str(role).strip().lower()
        for role in roles
        if isinstance(role, str) and str(role).strip()
    }


def _ordered_roles(roles: Iterable[str] | None) -> list[str]:
    order = {role: index for index, role in enumerate(RAMP_ROLE_ORDER)}
    return sorted(
        _normalised_roles(roles),
        key=lambda role: (order.get(role, len(order)), role),
    )


def effective_confirmed_roles(
    ramp_state: dict[str, Any],
    *,
    speaker_group_id: str,
    confirmed_roles: Iterable[str] | None = None,
) -> list[str]:
    """Merge transient ramp memory with durable current-topology evidence."""

    group = str(speaker_group_id or "").strip()
    ramp_group = str(ramp_state.get("speaker_group_id") or "").strip()
    same_group = not ramp_group or not group or ramp_group == group
    transient = (
        _normalised_roles(ramp_state.get("confirmed_roles") or [])
        if same_group
        else set()
    )
    return _ordered_roles(transient | _normalised_roles(confirmed_roles))


def _record_ramp_state(
    payload: dict[str, Any], *, state_path: str | Path | None = None
) -> dict[str, Any]:
    import json

    path = ramp_state_path(state_path)
    payload = dict(payload)
    payload["state_path"] = str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return payload


def reset_ramp_state(*, state_path: str | Path | None = None) -> dict[str, Any]:
    return _record_ramp_state(
        {**_ramp_base_state(ramp_state_path(state_path)), "last_action": "reset"},
        state_path=state_path,
    )


def clear_pending_ramp_step(
    *,
    speaker_group_id: str,
    confirmed_roles: Iterable[str] | None = None,
    state_path: str | Path | None = None,
) -> dict[str, Any]:
    """Start a fresh silent arm with no stale audible step awaiting ACK."""

    prior = load_ramp_state(state_path=state_path)
    group = str(speaker_group_id or "").strip()
    same_group = (
        not prior.get("speaker_group_id") or prior.get("speaker_group_id") == group
    )
    merged_confirmed_roles: list[str] = []
    if same_group or confirmed_roles:
        merged_confirmed_roles = effective_confirmed_roles(
            prior,
            speaker_group_id=group,
            confirmed_roles=confirmed_roles,
        )
    return _record_ramp_state(
        {
            **_ramp_base_state(ramp_state_path(state_path)),
            "speaker_group_id": group or prior.get("speaker_group_id"),
            "confirmed_roles": merged_confirmed_roles,
            "pending": None,
            "last_action": "clear_pending",
        },
        state_path=state_path,
    )


# --- live running-graph protection checks (the gate's extra assertions) ------
#
# running_commission_evidence covers the audible mask, the live tweeter
# high-pass, and the transient commissioning headroom. The Stage-5 gate adds the
# two remaining "existing protections" the AGENTS.md decision names: the 0 dB
# volume ceiling and the audible driver's per-driver limiter. Kept
# self-contained (a small YAML parse) rather than reaching into staging's private
# helpers.


def _safe_load_running(running_config_raw: str | None) -> dict[str, Any]:
    if not isinstance(running_config_raw, str) or not running_config_raw.strip():
        return {}
    try:
        config = yaml.safe_load(running_config_raw)
    except yaml.YAMLError:
        return {}
    return config if isinstance(config, dict) else {}


def _running_volume_ceiling_ok(config: dict[str, Any]) -> bool:
    """The CamillaDSP volume ceiling must be present and <= 0 dB in the RUNNING
    graph (an omitted limit defaults the main fader above 0 dB)."""
    devices = config.get("devices")
    if not isinstance(devices, dict):
        return False
    limit = devices.get("volume_limit")
    if not isinstance(limit, (int, float)) or isinstance(limit, bool):
        return False
    return float(limit) <= _EPS


def _running_role_limiter_ok(
    config: dict[str, Any], *, role: str, channels: set[int]
) -> bool:
    """The audible driver's startup limiter must be defined at the project clip
    limit AND wired onto its channels in the RUNNING pipeline."""
    if not channels:
        return False
    name = driver_limiter_name(role)
    filters = config.get("filters")
    entry = filters.get(name) if isinstance(filters, dict) else None
    if not isinstance(entry, dict) or entry.get("type") != "Limiter":
        return False
    params = entry.get("parameters")
    if not isinstance(params, dict):
        return False
    clip = params.get("clip_limit")
    if not isinstance(clip, (int, float)) or isinstance(clip, bool):
        return False
    if abs(float(clip) - STARTUP_LIMITER_CLIP_LIMIT_DB) > 1e-3:
        return False
    return _pipeline_wires(config, channels=channels, name=name)


def _pipeline_wires(config: dict[str, Any], *, channels: set[int], name: str) -> bool:
    pipeline = config.get("pipeline")
    if not isinstance(pipeline, list):
        return False
    for step in pipeline:
        if not isinstance(step, dict) or step.get("type") != "Filter":
            continue
        chans = step.get("channels")
        step_channels: set[int]
        if isinstance(chans, list):
            step_channels = {
                int(c) for c in chans if isinstance(c, int) and not isinstance(c, bool)
            }
        elif isinstance(step.get("channel"), int) and not isinstance(
            step.get("channel"), bool
        ):
            step_channels = {int(step["channel"])}
        else:
            continue
        if not channels <= step_channels:
            continue
        names = step.get("names")
        if isinstance(names, list) and name in [str(n) for n in names]:
            return True
    return False


# --- the Stage-5 gate --------------------------------------------------------


def build_stage5_ramp_gate(
    *,
    running_config_raw: str | None,
    role: str,
    present_roles: set[str] | frozenset[str],
    audible_outputs: list[int],
    muted_outputs: list[int],
    tweeter_outputs: list[int],
    protective_hp_hz: float | None,
    current_gain_db: float,
    next_gain_db: float,
    confirmed_roles: set[str] | frozenset[str],
    prior_step_cleared: bool,
) -> dict[str, Any]:
    """Decide whether one audible gain step is safe. Pure; fails closed.

    Owns level/gain bounds (the slice-2b-ii live gate deliberately does not):
    the gain envelope ``[MIN_TEST_LEVEL_DBFS, COMMISSION_RAMP_MAX_LEVEL_DBFS]``
    and the per-step limit. Defers the live mask + tweeter high-pass + transient
    commissioning headroom to :func:`running_commission_evidence`, and adds the
    0 dB ceiling + the audible driver's limiter (the "existing protections"
    reading of subsonic/DC). Also enforces woofer-before-tweeter ordering and
    that a louder step only follows an operator-handled prior step.

    ``prior_step_cleared`` means the previous audible step was acknowledged in a
    way that permits going louder — either the operator confirmed it
    (``floor_confirmed``) or judged it inaudible and asked to retry louder
    (``silent`` retry). The first step up from the silent floor needs neither
    (it IS the confirmation step), so it is vacuously satisfied there.
    """
    role = (role or "").strip().lower()
    present = {str(r).strip().lower() for r in present_roles}
    confirmed = {str(r).strip().lower() for r in confirmed_roles}
    audible = sorted({int(i) for i in audible_outputs})
    at_silent_floor = current_gain_db < MIN_TEST_LEVEL_DBFS

    # (1) gain envelope: the audible test range, never beyond the ceiling.
    gain_in_envelope = (
        MIN_TEST_LEVEL_DBFS - _EPS
        <= next_gain_db
        <= COMMISSION_RAMP_MAX_LEVEL_DBFS + _EPS
    )
    # (2) step bound: the first audible step is exactly the audible floor; a
    #     subsequent step rises by at most AUDIBLE_RAMP_STEP_DB (lowering is
    #     always allowed — it reduces risk).
    if at_silent_floor:
        step_bounded = abs(next_gain_db - MIN_TEST_LEVEL_DBFS) <= _EPS
    else:
        step_bounded = (next_gain_db - current_gain_db) <= AUDIBLE_RAMP_STEP_DB + _EPS

    # (3) live mask + tweeter high-pass (re-asserted on the RUNNING graph) +
    #     transient commissioning headroom. The protective-HP-present-before-
    #     tweeter rule is exactly the tweeter_protected_while_audible check here.
    live = running_commission_evidence(
        running_config_raw,
        audible_outputs=audible,
        muted_outputs=muted_outputs,
        tweeter_outputs=tweeter_outputs,
        protective_hp_hz=protective_hp_hz,
        expected_headroom_db=COMMISSIONING_HEADROOM_DB,
    )
    live_mask_and_highpass = bool(live.get("passed"))

    # (4)+(5) the existing protections the subsonic/DC gate stands on.
    config = _safe_load_running(running_config_raw)
    volume_ceiling = _running_volume_ceiling_ok(config)
    limiter_present = _running_role_limiter_ok(config, role=role, channels=set(audible))

    # (6) woofer before tweeter: every lower-frequency sibling present in this
    #     speaker must already be floor-confirmed.
    if role in RAMP_ROLE_ORDER:
        predecessors = [
            r
            for r in RAMP_ROLE_ORDER[: RAMP_ROLE_ORDER.index(role)]
            if r in present
        ]
    else:
        predecessors = []
    role_order_ok = all(r in confirmed for r in predecessors)

    # (7) ACK before each step: a louder step only after the prior audible step
    #     was operator-handled (confirmed, or judged silent -> retry louder). The
    #     first step up from the silent floor IS that handling, so it is vacuous.
    prior_step_acknowledged = at_silent_floor or bool(prior_step_cleared)

    checks = {
        "gain_within_envelope": gain_in_envelope,
        "gain_step_bounded": step_bounded,
        "live_mask_and_highpass": live_mask_and_highpass,
        "volume_ceiling_0db": volume_ceiling,
        "driver_limiter_present": limiter_present,
        "role_order_woofer_first": role_order_ok,
        "prior_step_acknowledged": prior_step_acknowledged,
    }
    passed = all(checks.values())
    return {
        "kind": "jts_active_speaker_stage5_ramp_gate",
        "passed": passed,
        "role": role,
        "audible_outputs": audible,
        "current_gain_db": current_gain_db,
        "next_gain_db": next_gain_db,
        "at_silent_floor": at_silent_floor,
        "is_tweeter_step": role == "tweeter",
        "predecessors_required": predecessors,
        "checks": checks,
        "live_evidence": live,
    }


# --- orchestration -----------------------------------------------------------


def _target(group_id: str, role: str, audible_outputs: list[int]) -> dict[str, Any]:
    return {
        "speaker_group_id": group_id,
        "driver_role": role,
        "output_index": audible_outputs[0] if audible_outputs else None,
    }


async def ramp_audible_step(
    topology: Any,
    *,
    speaker_group_id: str,
    role: str,
    load_config: PathLoader,
    read_running_config: RunningConfigReader,
    get_current_config_path: ConfigPathReader,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
    playback_device: str | None = None,
    path_safety_evidence_path: str | Path | None = None,
    staged_config: dict[str, Any] | None = None,
    statefile_path: str | Path | None = None,
    environment_report: dict[str, Any] | None = None,
    ramp_state_path_override: str | Path | None = None,
    safe_playback_state_path: str | Path | None = None,
    commission_load_state_path: str | Path | None = None,
    config_dir: str | Path | None = None,
    config_path: str | Path | None = None,
    validate: Callable[..., Any] | None = None,
    play_tone: ToneEmitter | None = None,
    auto_retry_pending: bool = False,
    confirmed_roles: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Raise one driver's per-output gain by one bounded, gated audible step.

    Reads the loaded gain from the commission-load state, proposes the next
    gain, runs :func:`build_stage5_ramp_gate` against the RUNNING graph, and —
    only if it passes — performs the SAME guarded inline load as the arm at the
    new ``audible_gain_db``. On success the driver is unmuted at the new level
    and the per-driver safe_playback tri-state moves to ``floor_pending_operator``
    (the operator must confirm the correct driver before any louder step or any
    sibling driver). Fails closed: a blocked gate or a failed load emits no new
    audible level.
    """
    role = (role or "").strip().lower()
    group_id = (speaker_group_id or "").strip()

    commission = load_commission_load_state(state_path=commission_load_state_path)
    loaded_target = commission.get("target") or {}
    if commission.get("status") != "loaded":
        return _blocked(
            "commission_not_loaded",
            "arm the driver with a commission load before ramping it audible",
            role=role,
            group_id=group_id,
        )
    if (loaded_target.get("speaker_group_id") or "") != group_id or (
        loaded_target.get("role") or ""
    ) != role:
        return _blocked(
            "commission_target_mismatch",
            "the loaded commissioning target is not the driver being ramped; "
            "roll back and arm the intended driver first",
            role=role,
            group_id=group_id,
        )

    ramp_state = load_ramp_state(state_path=ramp_state_path_override)
    ramp_confirmed_roles = effective_confirmed_roles(
        ramp_state,
        speaker_group_id=group_id,
        confirmed_roles=confirmed_roles,
    )
    pending = ramp_state.get("pending")
    replaced_pending: dict[str, Any] | None = None
    if isinstance(pending, dict):
        pending_role = str(pending.get("role") or "").strip().lower()
        pending_group = str(ramp_state.get("speaker_group_id") or "").strip()
        if auto_retry_pending and pending_role == role and pending_group == group_id:
            replaced_pending = dict(pending)
        else:
            return _blocked(
                "ramp_step_awaiting_ack",
                "acknowledge the last audible step (commission-ramp ack) before stepping again",
                role=role,
                group_id=group_id,
                extra={"pending": pending},
            )

    try:
        current_gain_db = float(
            loaded_target.get("audible_gain_db", STARTUP_MUTE_GAIN_DB)
        )
    except (TypeError, ValueError):
        current_gain_db = STARTUP_MUTE_GAIN_DB
    next_gain_db = next_ramp_gain_db(current_gain_db)
    if (
        current_gain_db >= COMMISSION_RAMP_MAX_LEVEL_DBFS - _EPS
        and next_gain_db <= current_gain_db + _EPS
    ):
        return _blocked(
            "commission_ramp_at_limit",
            "the driver test is already at the maximum bounded level",
            role=role,
            group_id=group_id,
            extra={
                "current_gain_db": current_gain_db,
                "next_gain_db": next_gain_db,
                "max_gain_db": COMMISSION_RAMP_MAX_LEVEL_DBFS,
            },
        )

    # Mask params for the gate: re-emit the per-driver config at the next gain
    # (stateless, no syntax check — the load re-validates) and read the off-device
    # evidence's mask. present_roles comes from the same preset binding. This is a
    # deliberately separate emit from the load's two (its preflight gate + its
    # TOCTOU-safe in-lock re-emit): the ramp gate must see the mask BEFORE the load
    # decides to apply. The emit is cheap and the operator steps are seconds apart,
    # so the redundancy is intentional — do not collapse it by trusting the file
    # the load will overwrite.
    prepare = prepare_driver_commissioning_config(
        topology,
        speaker_group_id=group_id,
        role=role,
        preset=preset,
        crossover_preview=crossover_preview,
        playback_device=playback_device,
        audible_gain_db=next_gain_db,
        config_dir=config_dir,
        config_path=config_path,
        run_config_check=False,
    )
    if prepare.get("status") != "prepared":
        return _blocked(
            "ramp_prepare_failed",
            "could not prepare the per-driver commissioning config for the next step",
            role=role,
            group_id=group_id,
            extra={"prepare_issues": prepare.get("issues")},
        )
    evidence = prepare.get("audible_evidence") or {}
    present_roles = _present_roles(prepare)

    safe_state = load_safe_playback_state(state_path=safe_playback_state_path)
    target = _target(group_id, role, evidence.get("audible_outputs") or [])
    # A louder step may proceed if the operator confirmed the prior step OR
    # judged it inaudible and asked to retry louder (both are "handled"). The web
    # commissioning surface also runs a same-driver automatic ramp; that may replace
    # the current pending step in place so a user click can still confirm the tone
    # at any moment instead of racing through a no-pending gap.
    prior_step_cleared = bool(replaced_pending) or _floor_confirmed(
        safe_state, target
    ) or _silent_retry(safe_state, target)

    running_raw = await read_running_config()
    gate = build_stage5_ramp_gate(
        running_config_raw=running_raw,
        role=role,
        present_roles=present_roles,
        audible_outputs=evidence.get("audible_outputs") or [],
        muted_outputs=evidence.get("muted_outputs") or [],
        tweeter_outputs=evidence.get("tweeter_outputs") or [],
        protective_hp_hz=evidence.get("protective_highpass_hz"),
        current_gain_db=current_gain_db,
        next_gain_db=next_gain_db,
        confirmed_roles=set(ramp_confirmed_roles),
        prior_step_cleared=prior_step_cleared,
    )
    if not gate["passed"]:
        failed = sorted(k for k, ok in gate["checks"].items() if not ok)
        log_event(
            logger,
            "active_speaker.stage5_ramp",
            level=logging.WARNING,
            result="gate_blocked",
            group=group_id,
            role=role,
            current_db=current_gain_db,
            next_db=next_gain_db,
            failed=",".join(failed),
        )
        return {
            "status": "gate_blocked",
            "role": role,
            "speaker_group_id": group_id,
            "current_gain_db": current_gain_db,
            "next_gain_db": next_gain_db,
            "gate": gate,
            "load": None,
            "issues": [_stage5_gate_issue(f) for f in failed],
        }

    # Precondition (fail-closed): the per-driver operator-confirmation session must
    # be armed BEFORE we make the driver audible, so a confirm can always land. If
    # it cannot arm, do NOT load — emit no new audible level.
    if not _ensure_safe_session_armed(
        environment_report=environment_report,
        safe_playback_state_path=safe_playback_state_path,
    ):
        log_event(
            logger,
            "active_speaker.stage5_ramp",
            level=logging.WARNING,
            result="arm_failed",
            group=group_id,
            role=role,
            next_db=next_gain_db,
        )
        return {
            "status": "blocked",
            "role": role,
            "speaker_group_id": group_id,
            "next_gain_db": next_gain_db,
            "gate": gate,
            "load": None,
            "issues": [
                _issue(
                    "blocker",
                    "stage5_safe_session_arm_failed",
                    "could not arm the operator-confirmation session; the driver "
                    "was NOT made audible",
                )
            ],
        }

    if replaced_pending is not None and not _pending_step_still_current(
        replaced_pending,
        group_id=group_id,
        role=role,
        ramp_state_path_override=ramp_state_path_override,
    ):
        log_event(
            logger,
            "active_speaker.stage5_ramp",
            level=logging.INFO,
            result="stale_auto_retry_pre_load",
            group=group_id,
            role=role,
            replaced_playback_id=replaced_pending.get("playback_id"),
        )
        return _stale_retry_payload(
            role=role,
            group_id=group_id,
            current_gain_db=current_gain_db,
            next_gain_db=next_gain_db,
            gate=gate,
            load_payload=None,
            rollback=None,
        )

    load_kwargs: dict[str, Any] = {}
    if validate is not None:
        load_kwargs["validate"] = validate
    load_payload = await load_driver_commissioning_config(
        topology,
        speaker_group_id=group_id,
        role=role,
        load_config=load_config,
        read_running_config=read_running_config,
        get_current_config_path=get_current_config_path,
        preset=preset,
        crossover_preview=crossover_preview,
        playback_device=playback_device,
        audible_gain_db=next_gain_db,
        path_safety_evidence_path=path_safety_evidence_path,
        staged_config=staged_config,
        config_dir=config_dir,
        config_path=config_path,
        statefile_path=statefile_path,
        state_path=commission_load_state_path,
        reconcile_output_hardware=False,
        **load_kwargs,
    )
    if (load_payload.get("load") or {}).get("status") != "loaded":
        log_event(
            logger,
            "active_speaker.stage5_ramp",
            level=logging.WARNING,
            result="load_failed",
            group=group_id,
            role=role,
            next_db=next_gain_db,
        )
        return {
            "status": "load_failed",
            "role": role,
            "speaker_group_id": group_id,
            "next_gain_db": next_gain_db,
            "gate": gate,
            "load": load_payload,
            "issues": [
                _issue(
                    "blocker",
                    "stage5_ramp_load_failed",
                    "the guarded commissioning load did not reach the new audible level",
                )
            ],
        }

    if replaced_pending is not None:
        if not _pending_step_still_current(
            replaced_pending,
            group_id=group_id,
            role=role,
            ramp_state_path_override=ramp_state_path_override,
        ):
            rollback = await rollback_driver_commissioning_config(
                load_config=load_config,
                state_path=commission_load_state_path,
                **({"validate": validate} if validate is not None else {}),
            )
            log_event(
                logger,
                "active_speaker.stage5_ramp",
                level=logging.INFO,
                result="stale_auto_retry",
                group=group_id,
                role=role,
                replaced_playback_id=replaced_pending.get("playback_id"),
            )
            return _stale_retry_payload(
                role=role,
                group_id=group_id,
                current_gain_db=current_gain_db,
                next_gain_db=next_gain_db,
                gate=gate,
                load_payload=load_payload,
                rollback=rollback.get("rollback"),
            )

    # The driver is now audible at next_gain_db. Record it into the (already-armed)
    # safe_playback tri-state -> floor_pending_operator, and mark the ramp step
    # pending so a second step cannot precede the ACK.
    playback_id = (
        (load_payload.get("load") or {}).get("dsp_apply") or {}
    ).get("op_id") or f"{group_id}:{role}:{next_gain_db:.1f}"
    tone_payload: dict[str, Any] | None = None
    if play_tone is not None:
        try:
            tone_payload = await play_tone(
                group_id=group_id,
                role=role,
                level_dbfs=next_gain_db,
                playback_id=playback_id,
                target=target,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed and re-mute.
            tone_payload = {
                "status": "failed",
                "audio_emitted": False,
                "issues": [
                    _issue(
                        "blocker",
                        "commission_tone_backend_failed",
                        f"commissioning tone backend raised {type(exc).__name__}: {exc}",
                    )
                ],
            }
        if not _tone_playback_confirmable(tone_payload):
            rollback = await rollback_driver_commissioning_config(
                load_config=load_config,
                state_path=commission_load_state_path,
                **({"validate": validate} if validate is not None else {}),
            )
            log_event(
                logger,
                "active_speaker.stage5_ramp",
                level=logging.WARNING,
                result="tone_failed",
                group=group_id,
                role=role,
                next_db=next_gain_db,
                tone_status=tone_payload.get("status"),
            )
            return {
                "status": "tone_failed",
                "role": role,
                "speaker_group_id": group_id,
                "current_gain_db": current_gain_db,
                "next_gain_db": next_gain_db,
                "gate": gate,
                "load": load_payload,
                "tone_playback": tone_payload,
                "rollback": rollback.get("rollback"),
                "issues": [
                    *_tone_playback_issues(tone_payload),
                    _issue(
                        "blocker",
                        "commission_tone_playback_failed",
                        "JTS loaded the protected driver graph but could not play "
                        "the test tone, so it re-muted the driver",
                    ),
                ],
            }
    safe = _record_floor_pending(
        target=target,
        level_dbfs=next_gain_db,
        playback_id=playback_id,
        safe_playback_state_path=safe_playback_state_path,
    )
    ramp_payload = _record_ramp_state(
        {
            **_ramp_base_state(ramp_state_path(ramp_state_path_override)),
            "speaker_group_id": group_id,
            "confirmed_roles": ramp_confirmed_roles,
            "pending": {
                "role": role,
                "gain_db": next_gain_db,
                "is_floor_step": gate["at_silent_floor"],
                "playback_id": playback_id,
                "frequency_hz": _tone_frequency_hz(tone_payload),
            },
            "last_action": "step",
        },
        state_path=ramp_state_path_override,
    )
    log_event(
        logger,
        "active_speaker.stage5_ramp",
        result="stepped",
        group=group_id,
        role=role,
        from_db=current_gain_db,
        to_db=next_gain_db,
        tri_state=(safe.get("quiet_start") or {}).get("status"),
    )
    return {
        "status": "stepped",
        "role": role,
        "speaker_group_id": group_id,
        "current_gain_db": current_gain_db,
        "next_gain_db": next_gain_db,
        "gate": gate,
        "load": load_payload,
        "tone_playback": tone_payload,
        "safe_playback": _safe_summary(safe),
        "ramp": ramp_payload,
        "issues": [],
    }


# The Stage-5 gate (`build_stage5_ramp_gate`) + the guarded load ARE the safety
# authority for the audible step. The safe_playback session is only the per-driver
# operator-confirmation tri-state holder, armed so the ACK can land. So it arms on
# a static ready report rather than re-running `probe_active_speaker_environment`,
# whose `ok_to_load_active_config` folds in `calibration_level_not_at_floor` — a
# gate orthogonal to the ramp's own gated gain that would otherwise block the
# confirm flow (observed live on jts3).
_RAMP_ARM_REPORT: dict[str, Any] = {
    "status": "ready",
    "load_gate": "ready",
    "ok_to_load_active_config": True,
    "camilla_config": {},
    "safe_playback": {},
    "issues": [],
}


def _ensure_safe_session_armed(
    *,
    environment_report: dict[str, Any] | None,
    safe_playback_state_path: str | Path | None,
) -> bool:
    """Ensure an armed safe_playback session exists to hold the per-driver floor
    tri-state. Returns whether it is armed. Arming an already-armed session is a
    no-op (so a mid-ramp ``floor_confirmed`` is preserved)."""
    state = load_safe_playback_state(state_path=safe_playback_state_path)
    if state.get("status") == "armed":
        return True
    report = environment_report if environment_report is not None else _RAMP_ARM_REPORT
    armed = arm_safe_playback_session(report, state_path=safe_playback_state_path)
    return armed.get("status") == "armed"


def _record_floor_pending(
    *,
    target: dict[str, Any],
    level_dbfs: float,
    playback_id: str,
    safe_playback_state_path: str | Path | None,
) -> dict[str, Any]:
    """Record the now-audible step into the (already-armed) safe_playback session,
    moving the per-driver tri-state to ``floor_pending_operator``."""
    return record_safe_playback_result(
        {
            "status": "completed",
            "backend": RAMP_BACKEND,
            "playback_id": playback_id,
            "audio_emitted": True,
            "target": target,
            "tone": {"level_dbfs": level_dbfs},
        },
        state_path=safe_playback_state_path,
    )


def _tone_frequency_hz(tone_payload: dict[str, Any] | None) -> float | None:
    if not isinstance(tone_payload, dict):
        return None
    tone = tone_payload.get("tone")
    if not isinstance(tone, dict):
        return None
    try:
        value = float(tone.get("frequency_hz"))
    except (TypeError, ValueError):
        return None
    return value


async def record_ramp_operator_ack(
    *,
    outcome: str,
    load_config: PathLoader | None = None,
    ramp_state_path_override: str | Path | None = None,
    safe_playback_state_path: str | Path | None = None,
    commission_load_state_path: str | Path | None = None,
    validate: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Record the operator's verdict for the pending audible step.

    ``heard_correct_driver`` confirms the floor (safe_playback ->
    ``floor_confirmed``), adds the role to the ramp's confirmed-roles ordering
    memory, and — when a loader seam is provided — re-mutes the transient graph
    so a returned browser session starts from a clean state. ``too_loud`` /
    ``heard_wrong_driver`` abort the ramp with the same rollback. ``silent``
    clears the step so it can be retried louder. Either way the pending step is
    cleared (ACK-before-each-step).
    """
    outcome = str(outcome or "").strip().lower()
    ramp_state = load_ramp_state(state_path=ramp_state_path_override)
    pending = ramp_state.get("pending")
    if not isinstance(pending, dict):
        return {
            "status": "no_pending_step",
            "ramp": ramp_state,
            "issues": [
                _issue(
                    "blocker",
                    "no_pending_ramp_step",
                    "there is no audible ramp step awaiting an operator acknowledgement",
                )
            ],
        }

    # Two distinct things are being acknowledged, and conflating them was a bug:
    #   * ramp.pending — the per-STEP gate (you can't take another step until you
    #     acknowledge this one). The authority for that is ``pending`` here.
    #   * the safe_playback floor tri-state — the per-DRIVER "heard correctly"
    #     confirmation. Its API only accepts a verdict while it is
    #     ``floor_pending_operator`` (the floor step, or a silent-retry step). A
    #     LOUDER step on an already-confirmed driver leaves it ``floor_confirmed``,
    #     so driving it there returns ``floor_confirmation_not_pending`` — which
    #     used to wedge the ramp. Drive the tri-state only while it is genuinely
    #     awaiting a confirm; the ramp.pending gate below stands on its own.
    safe = load_safe_playback_state(state_path=safe_playback_state_path)
    floor_pending = (safe.get("quiet_start") or {}).get("status") == (
        "floor_pending_operator"
    )
    if floor_pending:
        safe = record_floor_audio_operator_result(
            outcome=outcome,
            playback_id=pending.get("playback_id"),
            state_path=safe_playback_state_path,
        )
    safe_issues = list(safe.get("issues") or [])
    confirmed_roles = set(ramp_state.get("confirmed_roles") or [])
    aborted: dict[str, Any] | None = None

    # A "heard correct" ACK only genuinely confirms the floor when safe_playback
    # ACCEPTED it: no blocking issues AND the per-driver tri-state actually sits
    # at ``floor_confirmed`` (it advanced this call, or was already confirmed for
    # a louder step on the same driver). Without this gate a REJECTED confirm —
    # e.g. a stale ``playback_id`` from a racing auto-retry — still marked the
    # role confirmed, and ``confirmed_roles`` is the woofer-before-tweeter
    # ordering authority, so a tweeter step could proceed before the woofer was
    # truly heard. Fail closed: anything short of an accepted confirm does NOT
    # advance the ordering memory.
    floor_status = (safe.get("quiet_start") or {}).get("status")
    floor_confirmed_ok = not safe_issues and floor_status == "floor_confirmed"
    safe_issue_codes = {
        str(issue.get("code"))
        for issue in safe_issues
        if isinstance(issue, dict) and issue.get("code")
    }
    ack_reopen_required = (
        outcome == "heard_correct_driver"
        and (
            safe.get("status") in {"expired", "idle", "stopped"}
            or (
                bool(safe_issues)
                and bool(
                    safe_issue_codes
                    & {
                        "safe_session_not_armed",
                        "floor_playback_missing",
                        "floor_confirmation_not_pending",
                    }
                )
            )
        )
    )

    if outcome == "heard_correct_driver" and floor_confirmed_ok:
        # Heard correctly and the floor confirm was accepted — record it for the
        # woofer-before-tweeter ordering, release the per-step gate, and re-mute
        # the transient graph when the operator surface gave us a loader seam.
        confirmed_roles.add(str(pending.get("role")))
        new_pending = None
        status = "confirmed"
        if load_config is not None:
            aborted = await rollback_driver_commissioning_config(
                load_config=load_config,
                state_path=commission_load_state_path,
                **({"validate": validate} if validate is not None else {}),
            )
    elif outcome == "heard_correct_driver" and ack_reopen_required:
        # The UI can outlive the short continuous-tone lease. Once the safe
        # session is expired/stopped/not-pending, the pending ramp step is no
        # longer confirmable. Clear it and re-mute so Play can reopen the driver
        # quietly instead of leaving a confirm button that will never succeed.
        if "commission_ramp_ack_expired" not in safe_issue_codes:
            safe_issues.append(
                _issue(
                    "blocker",
                    "commission_ramp_ack_expired",
                    "the driver tone expired before it could be confirmed",
                )
            )
        new_pending = None
        status = "expired"
        if load_config is not None:
            aborted = await rollback_driver_commissioning_config(
                load_config=load_config,
                state_path=commission_load_state_path,
                **({"validate": validate} if validate is not None else {}),
            )
    elif outcome == "heard_correct_driver":
        # Operator said "heard correct" but safe_playback REJECTED the floor
        # confirm. Do NOT advance the ordering memory; leave the step pending so
        # the operator can re-confirm, and surface the safe issues to the caller.
        new_pending = pending
        status = "rejected"
    elif outcome in {"too_loud", "heard_wrong_driver"}:
        new_pending = None
        status = "aborted"
        if outcome == "heard_wrong_driver":
            # Hearing the WRONG driver at this step casts doubt on the role's
            # identity — drop it from the ordering memory so a sibling step can't
            # rely on a now-suspect earlier confirmation.
            confirmed_roles.discard(str(pending.get("role")))
        if load_config is not None:
            aborted = await rollback_driver_commissioning_config(
                load_config=load_config,
                state_path=commission_load_state_path,
                **({"validate": validate} if validate is not None else {}),
            )
    elif outcome == "silent":
        new_pending = None  # cleared so the operator can retry louder
        status = "retry"
    else:
        # An unsupported / rejected outcome leaves the step pending.
        new_pending = pending
        status = "rejected"

    ramp_payload = _record_ramp_state(
        {
            **_ramp_base_state(ramp_state_path(ramp_state_path_override)),
            "speaker_group_id": ramp_state.get("speaker_group_id"),
            "confirmed_roles": sorted(confirmed_roles),
            "pending": new_pending,
            "last_action": f"ack_{outcome}",
        },
        state_path=ramp_state_path_override,
    )
    log_event(
        logger,
        "active_speaker.stage5_ramp",
        result="ack",
        outcome=outcome,
        status=status,
        role=pending.get("role"),
        tri_state=(safe.get("quiet_start") or {}).get("status"),
    )
    return {
        "status": status,
        "outcome": outcome,
        "acknowledged_step": pending,
        "safe_playback": _safe_summary(safe),
        "ramp": ramp_payload,
        "rollback": (aborted or {}).get("rollback") if aborted else None,
        "issues": safe_issues,
    }


async def abort_ramp(
    *,
    load_config: PathLoader,
    ramp_state_path_override: str | Path | None = None,
    commission_load_state_path: str | Path | None = None,
    safe_playback_state_path: str | Path | None = None,
    validate: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Roll the running graph back to the all-muted staged config and reset the
    ramp. The operator's hard Stop — always available, always re-mutes."""
    from .safe_playback import stop_safe_playback_session

    rollback = await rollback_driver_commissioning_config(
        load_config=load_config,
        state_path=commission_load_state_path,
        **({"validate": validate} if validate is not None else {}),
    )
    ramp_payload = reset_ramp_state(state_path=ramp_state_path_override)
    safe = stop_safe_playback_session(
        reason="commission_abort",
        state_path=safe_playback_state_path,
    )
    log_event(
        logger,
        "active_speaker.stage5_ramp",
        result="aborted",
        rollback=(rollback.get("rollback") or {}).get("status"),
    )
    return {
        "status": "aborted",
        "rollback": rollback.get("rollback"),
        "ramp": ramp_payload,
        "safe_playback": _safe_summary(safe),
    }


# --- helpers -----------------------------------------------------------------


def _blocked(
    code: str,
    message: str,
    *,
    role: str,
    group_id: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "status": "blocked",
        "role": role,
        "speaker_group_id": group_id,
        "gate": None,
        "load": None,
        "issues": [_issue("blocker", code, message)],
    }
    if extra:
        payload.update(extra)
    return payload


def _stage5_gate_issue(check: str) -> dict[str, str]:
    if check == "role_order_woofer_first":
        return _issue(
            "blocker",
            "stage5_ramp_role_order_woofer_first",
            "confirm the lower-frequency driver before testing this driver",
        )
    return _issue("blocker", "stage5_ramp_gate_blocked", f"gate failed: {check}")


def _present_roles(prepare: dict[str, Any]) -> set[str]:
    """The driver roles that exist in this speaker.

    Derived from the speaker's way count so a tweeter step can require the WOOFER
    (not visible in a tweeter-step's own mask) and a 2-way is never asked to
    confirm a non-existent ``mid``. Falls back to the audible role if the way
    count is somehow absent (the ordering check then only blocks on confirmed
    siblings it can actually see).
    """
    from .profile import required_driver_roles

    way_count = prepare.get("way_count")
    if isinstance(way_count, int) and way_count > 0:
        return {r.strip().lower() for r in required_driver_roles(way_count)}
    role = (prepare.get("target") or {}).get("role")
    return {role.strip().lower()} if isinstance(role, str) and role else set()


def _floor_confirmed(safe_state: dict[str, Any], target: dict[str, Any]) -> bool:
    from .safe_playback import floor_audio_confirmed_for_target

    return floor_audio_confirmed_for_target(safe_state, target)


def _silent_retry(safe_state: dict[str, Any], target: dict[str, Any]) -> bool:
    from .safe_playback import floor_audio_retry_allowed_for_target

    return floor_audio_retry_allowed_for_target(safe_state, target)


def _pending_step_still_current(
    replaced_pending: dict[str, Any],
    *,
    group_id: str,
    role: str,
    ramp_state_path_override: str | Path | None,
) -> bool:
    """Return whether the pending step this retry is replacing still exists.

    The browser's automatic louder retry can race with an operator click. This
    check is intentionally run immediately before the hardware load so a
    superseded retry does not briefly reload an old driver after the operator
    has confirmed, stopped, or changed the test.
    """

    current_ramp = load_ramp_state(state_path=ramp_state_path_override)
    current_pending = current_ramp.get("pending")
    if not isinstance(current_pending, dict):
        return False
    pending_role = str(current_pending.get("role") or "").strip().lower()
    pending_group = str(current_ramp.get("speaker_group_id") or "").strip()
    return (
        pending_role == role
        and pending_group == group_id
        and current_pending.get("playback_id") == replaced_pending.get("playback_id")
    )


def _stale_retry_payload(
    *,
    role: str,
    group_id: str,
    current_gain_db: float,
    next_gain_db: float,
    gate: dict[str, Any],
    load_payload: dict[str, Any] | None,
    rollback: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "status": "stale_retry",
        "role": role,
        "speaker_group_id": group_id,
        "current_gain_db": current_gain_db,
        "next_gain_db": next_gain_db,
        "gate": gate,
        "load": load_payload,
        "rollback": rollback,
        "issues": [
            _issue(
                "info",
                "commission_ramp_retry_superseded",
                "the driver test changed while the next louder step was preparing",
            )
        ],
    }


def _safe_summary(safe: dict[str, Any]) -> dict[str, Any]:
    quiet = safe.get("quiet_start") or {}
    return {
        "status": safe.get("status"),
        "floor_status": quiet.get("status"),
        "floor_audio_confirmed": quiet.get("floor_audio_confirmed"),
        "last_level_dbfs": quiet.get("last_level_dbfs"),
        "current_target": playback_target_signature(quiet.get("current_target")),
    }


def _tone_playback_issues(payload: dict[str, Any] | None) -> list[dict[str, str]]:
    issues = payload.get("issues") if isinstance(payload, dict) else []
    return [issue for issue in issues if isinstance(issue, dict)]


def _tone_playback_confirmable(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("status") != "completed":
        return False
    if payload.get("audio_emitted") is not True:
        return False
    return not any(
        issue.get("severity") == "blocker"
        for issue in _tone_playback_issues(payload)
    )
