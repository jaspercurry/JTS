# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Listen to the applied speaker at a reduced DSP layer, and always get it back.

Layer semantics, the crash-safety argument and the rejected alternatives are
ADR-0193's; see ADR-0329 for rear comparison. The one fact worth repeating at
the call sites below, because every function here depends on it: the swap is
``set_active_config_raw``, which leaves CamillaDSP's persisted
``config_file_path`` alone, so a restart, a reboot or a ``kill -9`` of the owner
puts the applied graph back by doing nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
import uuid
from pathlib import Path
from dataclasses import replace
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from jasper.active_speaker.restore_wait import attempt_graph_restore, resilient_restore
from jasper.atomic_io import atomic_write_json
from jasper.camilla import CamillaUnavailable
from jasper.log_event import log_event
from jasper.sound.settings import saved_sound_layers
from jasper.sound.live_edit import dump_graph_yaml, load_graph_yaml, plan_live_edit_for
from jasper.active_speaker.rear_calibration import rear_stage_gain_name
from jasper.active_speaker.audition_claim import AUDITION_WRITE, clear_audition_state
from jasper.active_speaker.state_paths import audition_state_path, baseline_config_path

logger = logging.getLogger(__name__)

AUDITION_LAYER_BASELINE = "baseline"
AUDITION_LAYER_FULL = "full"
AUDITION_LAYER_REAR_COMPARE = "rear_compare"
# Bound the runtime-only loudness-match attenuation; see ADR-0329.
MAX_COMPARE_TRIM_DB = 6.0
AUDITION_LAYERS = (AUDITION_LAYER_BASELINE, AUDITION_LAYER_FULL)

# The walked-away bound, matching session_volume_plan's own wall-clock ceiling.
# Applies to both foreground and web owners (ADR-0329).
AUDITION_DEADLINE_S = 1800.0
# How often the owner re-reads the state file to notice a stop or a takeover.
AUDITION_TICK_S = 5.0

AUDITION_STATE_KIND = "jts_active_speaker_audition"
AUDITION_SCHEMA_VERSION = 1

REFUSE_NO_APPLIED_PROFILE = "audition_no_applied_profile"
REFUSE_PROFILE_DISPLACED = "audition_applied_profile_displaced"
REFUSE_MEASUREMENT_ACTIVE = "audition_measurement_session_active"
REFUSE_COMMISSION_LOAD_ACTIVE = "audition_commission_load_active"
REFUSE_NO_DURABLE_ANCHOR = "audition_no_durable_anchor"
REFUSE_EMIT = "audition_emit_refused"
REFUSE_LOAD = "audition_load_refused"
REFUSE_RESTORE = "audition_restore_failed"
REFUSE_NO_REAR_STAGE = "audition_no_rear_stage"
REFUSE_REAR_MUTED = "audition_rear_muted_in_tune"
REFUSE_MALFORMED_GRAPH = "audition_malformed_graph"
REFUSE_RUNNING_GRAPH_DIFFERS = "audition_running_graph_differs"

# Played THROUGH the graph that was just swapped in, so the announcement is also
# a liveness proof. A silent wrong-graph state is the failure mode this door has.
AUDITION_REDUCED_CUE_SLUG = "audition_reduced_graph"
AUDITION_RESTORED_CUE_SLUG = "audition_full_graph"

END_DEADLINE = "deadline"
END_SUPERSEDED = "superseded"
END_INTERRUPTED = "interrupted"


CueSender = Callable[[str], None]

__all__ = [
    "AUDITION_DEADLINE_S",
    "AUDITION_LAYERS",
    "AUDITION_LAYER_BASELINE",
    "AUDITION_LAYER_FULL",
    "AuditionRefused",
    "build_reduced_yaml",
    "hold_audition",
    "level_give_back_db",
    "read_audition_state",
    "start_audition",
    "stop_audition",
    "rear_compare_yaml",
    "set_compare_state",
]


class AuditionRefused(RuntimeError):
    """The audition did not start or could not be put back. Carries a code."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


def read_audition_state(path: str | Path | None = None) -> dict[str, Any] | None:
    """The live audition, or ``None`` when the speaker is on its full graph.

    Fail-soft: an unreadable or malformed record answers ``None``. The graph is
    whatever CamillaDSP is running either way, and reporting "no audition" is
    the answer that sends a reader to ``stop`` rather than to a parser.
    """

    try:
        payload = json.loads(audition_state_path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != AUDITION_STATE_KIND
        or payload.get("schema_version") != AUDITION_SCHEMA_VERSION
        or payload.get("layer") not in (AUDITION_LAYER_BASELINE, AUDITION_LAYER_REAR_COMPARE)
        or not isinstance(payload.get("token"), str)
        or type(payload.get("owner_pid")) is not int
        or payload.get("state") not in (None, "on", "off")
        or type(payload.get("deadline_at")) not in (int, float)
        or not math.isfinite(payload["deadline_at"])
    ):
        return None
    return payload


def audition_summary() -> dict[str, Any] | None:
    state = read_audition_state()
    if state is None:
        return None
    return {"layer": state["layer"], "state": state.get("state"),
            "expires_in_s": max(0, int(state["deadline_at"] - time.time()))}


def rear_compare_yaml(applied_yaml: str, *, rear_muted: bool, trim_db: float) -> str:
    # float(), not just a bound check: a numpy scalar passes every comparison and
    # then the YAML dumper cannot represent it (met on jts3, 2026-09-21).
    trim_db = float(trim_db)
    if not 0.0 <= trim_db <= MAX_COMPARE_TRIM_DB:
        raise ValueError("compare trim is outside its attenuation range")
    graph = load_graph_yaml(applied_yaml)
    try:
        filters = graph["filters"]
        names = [rear_stage_gain_name(i, "output")
                 for i in range(graph["devices"]["playback"]["channels"])
                 if rear_stage_gain_name(i, "output") in filters]
        if len(names) != 1 or filters[names[0]]["type"] != "Gain":
            raise AuditionRefused(REFUSE_NO_REAR_STAGE, "The applied graph needs one fitted rear stage.")
        rear = filters[names[0]]["parameters"]
        if rear["mute"]:
            raise AuditionRefused(REFUSE_REAR_MUTED, "The applied tune already mutes the rear output.")
        rear["mute"] = rear_muted
        filters["active_baseline_headroom"]["parameters"]["gain"] -= trim_db
    except (KeyError, TypeError) as exc:
        raise AuditionRefused(REFUSE_MALFORMED_GRAPH, "The applied graph is malformed.") from exc
    return dump_graph_yaml(graph)


def _refuse_if_graph_is_claimed() -> None:
    """Refuse while somebody else owns the running graph.

    ``live_measurement_session`` is the repo's ONE answer to "may an operator
    door act right now", and this consumes it rather than reading the volume
    statefile itself. That matters concretely, not just tidily: a room sweep
    takes jasper-control's measurement hold and
    never constructs a ``SessionVolumePlan``, so a statefile-only reading finds
    nothing and swaps the graph out from under a running capture.
    """

    from jasper.active_speaker.session_volume_plan import live_measurement_session
    from jasper.active_speaker.startup_load import load_commission_load_state  # lazy: the load transaction imports the graph proof

    refusal = live_measurement_session(action="auditioning")
    if refusal is not None:
        raise AuditionRefused(REFUSE_MEASUREMENT_ACTIVE, refusal)
    if load_commission_load_state().get("status") == "loaded":
        raise AuditionRefused(
            REFUSE_COMMISSION_LOAD_ACTIVE,
            "a per-driver commissioning config is armed, so the applied baseline "
            "is not what is playing; run `jasper-active-speaker commission-rollback` first",
        )


def build_reduced_yaml(
    topology: Any,
    *,
    applied_profile: dict[str, Any],
) -> tuple[str | None, list[dict[str, str]]]:
    """Compile the applied candidate without driver linearization or blend EQ."""
    from jasper.active_speaker.candidate_bank import CandidateBankRefusal  # lazy: candidate lookup boundary
    from .candidate_parts import candidate_from_applied_profile  # lazy: audition-only graph compilation
    from .measurement_emit import compile_tuning_graph, load_tuning_declaration  # lazy: audition-only graph compilation

    try:
        declaration = load_tuning_declaration(topology)
        candidate = replace(candidate_from_applied_profile(topology, applied_profile), linearization={}, blend_correction=())
        preference_filters, trim_db = saved_sound_layers()
        return compile_tuning_graph(declaration, candidate=candidate,
            preference_filters=preference_filters, output_trim_db=trim_db), []
    except (CandidateBankRefusal, OSError, ValueError) as exc:
        return None, [{"severity": "blocker", "code": getattr(exc, "code", "audition_compile_failed"), "message": str(exc)}]


def level_give_back_db(applied_profile: Mapping[str, Any]) -> float:
    """How much LOUDER the baseline layer can play than the full graph, dB.

    Two terms, and both are read off the profile's own emitter inputs rather
    than modelled:

    * the pre-split attenuation the linearization stage charged
      (:func:`~.baseline_profile.profile_program_headroom_db`), which comes
      back broadband when the stage goes — often ``0.0``, because a boost the
      branch's own crossover and trim already swallow is charged nothing; and
    * the DEEPEST single cut the two dropped stages carry, which comes back in
      that filter's own band. This is the larger term in practice, and the one
      a "give back the headroom" reading of this reduction would miss.

    Not a bound on the sum: two cuts overlapping in one band give back more
    than the deeper of them. It does not need to be a bound — the ceiling is
    structural rather than arithmetic. The reduced graph IS the speaker's
    pre-linearization baseline, the graph it was commissioned and measured
    through, and this reduction changes no trim, no crossover, no protection
    filter, no limiter and not the 0 dB ``volume_limit``. This number is a
    disclosure so the owner knows the A/B is not level-matched.
    """

    from jasper.active_speaker.baseline_profile import (
        profile_blend_correction,
        profile_linearization,
        profile_program_headroom_db,
    )

    dropped: list[Any] = []
    for filters in profile_linearization(applied_profile).values():
        if isinstance(filters, Sequence) and not isinstance(filters, (str, bytes)):
            dropped.extend(filters)
    dropped.extend(profile_blend_correction(applied_profile) or ())
    cuts = [
        -float(entry["gain"])
        for entry in dropped
        if isinstance(entry, Mapping)
        and isinstance(entry.get("gain"), (int, float))
        and float(entry["gain"]) < 0.0
    ]
    return profile_program_headroom_db(applied_profile) + max(cuts, default=0.0)


async def _swap_running_graph(cam: Any, yaml_text: str, *, refusal: str) -> None:
    """Write through the controller's admission door, with ADR-0211 routing."""
    from jasper.active_speaker.crossover_v2.composition import confirm_graph_is_live

    plan = await plan_live_edit_for(cam, yaml_text)
    token = AUDITION_WRITE.set(True)
    try:
        if plan.method != "unchanged" and not await cam.set_active_config_raw(
            yaml_text, best_effort=False, duck=plan.duck,
        ):
            raise AuditionRefused(REFUSE_LOAD, refusal)
        await confirm_graph_is_live(cam, yaml_text)
    finally:
        AUDITION_WRITE.reset(token)


async def _put_back(cam: Any, anchor: str) -> None:
    """Make the durable graph the running one again, from its own bytes."""

    await _swap_running_graph(
        cam,
        Path(anchor).read_text(encoding="utf-8"),
        refusal=(
            "the reduced graph was played but the applied graph could not be "
            f"reloaded from {anchor}; reapply the speaker profile before "
            "playing audio"
        ),
    )


async def _restore_verdict(cam: Any, anchor: str) -> tuple[bool, str | None]:
    """``(took_effect, message)`` for one put-back, through the shared verdict.

    :func:`~jasper.active_speaker.restore_wait.attempt_graph_restore` is
    the repo's one verdict a swap transaction reaches, and it decides success
    on ``is True`` — so the bridge from :func:`_put_back`, which returns
    ``None`` and raises instead, lives HERE rather than at each site. Two sites
    writing their own bridge is how one of them comes to read a restore that
    worked as one that failed.

    The put-back uses the same structural routing and live read-back.
    """

    async def _restore() -> bool:
        await _put_back(cam, anchor)
        return True

    return await attempt_graph_restore(_restore)


async def _undo_failed_arm(cam: Any, anchor: str, state_path: str | Path | None = None) -> None:
    """Put the durable graph back after an arm that could not be completed.

    Swallows its own failure so the caller's original exception is the one that
    escapes — but never SILENTLY: a swap that took, was never recorded, and
    could not be undone is the single state nothing else repairs, so it leaves
    a CRITICAL line naming the anchor an operator has to reload by hand. The
    anchor read and the load are both inside the verdict, because either
    raising here would replace the error the caller needs and abandon the rest
    of the undo.
    """

    took_effect, message = await _restore_verdict(cam, anchor)
    if took_effect:
        clear_audition_state(state_path)
    else:
        log_event(
            logger,
            "active_speaker.audition",
            level=logging.CRITICAL,
            action="undo_failed_arm",
            result="failed",
            entry_config_path=anchor,
            error=message or "",
        )


async def _durable_anchor(cam: Any) -> str:
    """The config path CamillaDSP boots from — read, never written."""

    anchor = await cam.get_config_file_path(best_effort=False)
    if not anchor:
        raise AuditionRefused(
            REFUSE_NO_DURABLE_ANCHOR,
            "CamillaDSP reports no persisted config path, so there is no graph "
            "to put back; refusing to swap",
        )
    return str(anchor)


async def start_audition(
    *,
    cam: Any,
    layer: str = AUDITION_LAYER_BASELINE,
    compare_state: str = "on",
    trim_db: float = 0.0,
    state_path: str | Path | None = None,
    play_cue: CueSender | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Put ``layer`` on the speaker and record who owns the swap.

    ``full`` is the durable graph, so asking for it is the restore — one code
    path, not a second one that could drift from :func:`stop_audition`.
    """

    if layer not in (*AUDITION_LAYERS, AUDITION_LAYER_REAR_COMPARE):
        raise ValueError(f"unknown audition layer: {layer!r}")
    compare = layer == AUDITION_LAYER_REAR_COMPARE
    if compare and compare_state not in {"on", "off"}:
        raise ValueError("invalid compare state")
    if layer == AUDITION_LAYER_FULL:
        return await stop_audition(
            cam=cam, state_path=state_path, play_cue=play_cue
        )

    from jasper.active_speaker.baseline_profile import (
        applied_profile_displacement,
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.runtime_contract import (
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        classify_bass_extension_graph,
    )
    from jasper.dsp_apply import dsp_writer_lock
    from jasper.output_topology_store import load_output_topology  # lazy: test_active_speaker_audition pins the store lookup

    _refuse_if_graph_is_claimed()
    applied = load_applied_baseline_profile_state()
    if not applied:
        raise AuditionRefused(
            REFUSE_NO_APPLIED_PROFILE,
            "no applied active-speaker baseline is saved, so there is no live "
            "graph to reduce",
        )
    displaced = applied_profile_displacement(applied)
    if displaced:
        raise AuditionRefused(
            REFUSE_PROFILE_DISPLACED,
            f"the saved applied profile is not what the speaker is playing "
            f"({displaced}); reapply the speaker profile before auditioning",
        )

    topology = load_output_topology()
    # The same writer boundary and the same way of naming it as the ordinary
    # apply, so an audition and a /sound save serialize against each other.
    async with dsp_writer_lock(
        baseline_config_path().parent, source="active_speaker_audition_start"
    ):
        _refuse_if_graph_is_claimed()
        anchor = await _durable_anchor(cam)
        anchor_text = Path(anchor).read_text(encoding="utf-8") if compare else ""
        live = read_audition_state(state_path)
        if compare and (not live or live["layer"] != AUDITION_LAYER_REAR_COMPARE):
            if (await plan_live_edit_for(cam, anchor_text)).method != "unchanged":
                raise AuditionRefused(REFUSE_RUNNING_GRAPH_DIFFERS,
                    "An unsaved EQ draft or another live edit is playing. Save or leave it, then compare.")
        yaml_text, issues = (rear_compare_yaml(
            anchor_text,
            rear_muted=compare_state == "off", trim_db=trim_db,
        ), []) if compare else build_reduced_yaml(
            topology, applied_profile=applied
        )
        if yaml_text is None or issues:
            detail = "; ".join(
                str(issue.get("message") or issue.get("code")) for issue in issues
            ) or "the applied profile could not be re-emitted"
            raise AuditionRefused(REFUSE_EMIT, detail)
        # Re-proved here rather than trusted from the emitter, exactly as
        # `jasper-active-speaker baseline-reemit` does before it writes a byte.
        graph = None if compare else classify_bass_extension_graph(
            topology,
            evidence_source="desired",
            graph_text=yaml_text,
            applied_baseline_state=applied,
        )
        if graph is not None and (not graph.allowed or graph.classification != GRAPH_APPROVED_ACTIVE_RUNTIME):
            raise AuditionRefused(
                REFUSE_EMIT,
                f"the reduced graph did not re-prove as "
                f"{GRAPH_APPROVED_ACTIVE_RUNTIME} (got {graph.classification})",
            )
        # A swap that TOOK but was never recorded is the one state nothing
        # would put back: no record means no owner, no deadline, and
        # `jasper-audition status` has nothing to disclose. Undoing it here
        # costs a redundant reload on the paths where nothing was loaded at
        # all, which is the cheaper mistake.
        armed = False
        try:
            await _swap_running_graph(
                cam,
                yaml_text,
                refusal="CamillaDSP rejected the reduced graph",
            )
            started_at = float(clock())
            state = {
                "kind": AUDITION_STATE_KIND,
                "schema_version": AUDITION_SCHEMA_VERSION,
                "token": uuid.uuid4().hex,
                "layer": layer,
                "state": compare_state if compare else None,
                "owner_pid": os.getpid(),
                "expires_at": started_at + AUDITION_DEADLINE_S,
                "started_at": started_at,
                "deadline_at": started_at + AUDITION_DEADLINE_S,
                "entry_config_path": anchor,
                # Disclosed, never compensated: compensating would move a trim,
                # and identical trims are what makes the A/B mean anything.
                "louder_than_full_db": None if compare else level_give_back_db(applied),
            }
            atomic_write_json(audition_state_path(state_path), state)
            armed = True
        finally:
            if not armed:
                await _undo_failed_arm(cam, anchor, state_path)

    log_event(
        logger,
        "active_speaker.audition",
        action="start",
        result="swapped",
        layer=layer,
        deadline_at=f"{state['deadline_at']:.0f}",
        louder_than_full_db=state["louder_than_full_db"],
        entry_config_path=anchor,
    )
    _send_cue(play_cue, AUDITION_REDUCED_CUE_SLUG)
    return {"status": "auditioning", **state}


async def stop_audition(
    *,
    cam: Any,
    state_path: str | Path | None = None,
    play_cue: CueSender | None = None,
    expect_token: str | None = None,
) -> dict[str, Any]:
    """Put the durable graph back now. Idempotent.

    The anchor is re-read from CamillaDSP rather than taken from the audition
    record: the record's copy would be stale if the durable graph legitimately
    moved under us (a ``/sound`` save re-emits and re-applies), and the thing
    the owner is owed back is the CURRENT durable graph, not the one that was
    durable when the audition started.

    ``expect_token`` closes the check-then-act window a departing owner opens:
    it verified the record was its own, and a replacement ``start`` can land
    before this call re-reads. Passing the token makes the two reads one
    decision, so the leaving owner cannot un-swap and un-record an audition
    that began a millisecond ago. ``None`` means "stop whatever is running" —
    the operator's own ``stop``, which is entitled to end any of them.

    The presence of the FILE decides whether there is anything to stop, not
    whether it parses: a record this build cannot read still means the speaker
    is on somebody's reduced graph, and refusing to restore it would be the
    worst possible reading of a corrupt byte.
    """

    from jasper.dsp_apply import dsp_writer_lock

    if not audition_state_path(state_path).exists():
        return {"status": "not_auditioning", "layer": AUDITION_LAYER_FULL}
    async with dsp_writer_lock(
        baseline_config_path().parent, source="active_speaker_audition_stop"
    ):
        if expect_token is not None:
            live = read_audition_state(state_path)
            if live is None or live.get("token") != expect_token:
                return {"status": "superseded", "layer": AUDITION_LAYER_BASELINE}
        anchor = await _durable_anchor(cam)
        took_effect, message = await _restore_verdict(cam, anchor)
        if not took_effect:
            # The record stays on disk on purpose: `jasper-audition status`
            # keeps disclosing that the speaker is not on its applied graph,
            # and the next `stop` has something to retry against.
            log_event(
                logger,
                "active_speaker.audition",
                level=logging.CRITICAL,
                action="stop",
                result="failed",
                entry_config_path=anchor,
                error=message or "",
            )
            # Composed, never chosen: the operator needs the anchor to reload
            # by hand, and the upstream sentence names what went wrong. Picking
            # one drops whichever half the reader was missing.
            detail = (
                "the reduced graph is still playing and the applied graph "
                f"could not be reloaded from {anchor}"
            )
            raise AuditionRefused(
                REFUSE_RESTORE, f"{detail} ({message})" if message else detail,
            )
        clear_audition_state(state_path)

    log_event(
        logger,
        "active_speaker.audition",
        action="stop",
        result="restored",
        layer=AUDITION_LAYER_FULL,
        entry_config_path=anchor,
    )
    _send_cue(play_cue, AUDITION_RESTORED_CUE_SLUG)
    return {
        "status": "restored",
        "layer": AUDITION_LAYER_FULL,
        "entry_config_path": anchor,
    }


async def hold_audition(
    state: dict[str, Any],
    *,
    cam: Any,
    state_path: str | Path | None = None,
    play_cue: CueSender | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> str:
    """Own the swap until the deadline, and restore on every way out.

    Returns why the hold ended. The one exit that does NOT restore is
    :data:`END_SUPERSEDED` — the state file stopped naming this owner's token,
    so either an explicit stop already put the graph back or a newer audition
    owns it now, and restoring would un-swap somebody else's work.
    """

    token = state.get("token")
    deadline = float(state.get("deadline_at", 0.0))
    # Replaced on every ORDINARY exit, so an interrupt, a cancellation or a
    # raising tick all land on the honest reason without a broad except.
    reason = END_INTERRUPTED
    try:
        while True:
            live = read_audition_state(state_path)
            if live is None or live.get("token") != token:
                reason = END_SUPERSEDED
                return reason
            if float(clock()) >= deadline:
                reason = END_DEADLINE
                return reason
            await sleep(AUDITION_TICK_S)
    finally:
        live = read_audition_state(state_path)
        if live is not None and live.get("token") == token:
            log_event(
                logger,
                "active_speaker.audition",
                action="hold_ended",
                reason=reason,
                layer=state.get("layer"),
            )
            # Finishes BEFORE a caller's cancellation propagates: a Ctrl-C
            # landing between the two strands the speaker on a graph nobody
            # chose. See jasper.active_speaker.restore_wait.
            await resilient_restore(
                stop_audition(
                    cam=cam,
                    state_path=state_path,
                    play_cue=play_cue,
                    expect_token=token,
                )
            )


def _send_cue(play_cue: CueSender | None, slug: str) -> None:
    """Announce the layer change, best-effort. A missing cue never fails a swap.

    The catch set is the daemon-reachability family the control client raises —
    ``ControlError`` is a ``RuntimeError``, an unreachable socket is an
    ``OSError``, a non-JSON body is a ``ValueError``. A cue that cannot be
    spoken is worth a warning; it is never worth failing a graph that already
    swapped.
    """

    if play_cue is None:
        return
    try:
        play_cue(slug)
    except (OSError, RuntimeError, ValueError) as exc:
        log_event(
            logger,
            "active_speaker.audition",
            level=logging.WARNING,
            action="cue",
            result="failed",
            slug=slug,
            error=type(exc).__name__,
        )


async def set_compare_state(state: str, *, cam: Any, trim_db: float) -> dict[str, Any]:
    if state not in {"on", "off", "normal"}:
        raise ValueError("invalid compare state")
    try:
        if state == "normal":
            return await stop_audition(cam=cam)
        return await start_audition(cam=cam, layer=AUDITION_LAYER_REAR_COMPARE,
                                    compare_state=state, trim_db=trim_db)
    except CamillaUnavailable as exc:
        raise AuditionRefused(REFUSE_LOAD, "CamillaDSP could not load the comparison.") from exc


async def recover_web_audition(cam: Any) -> None:
    state = read_audition_state()
    if (state and state["layer"] == AUDITION_LAYER_REAR_COMPARE
            and state.get("owner_pid") != os.getpid()):
        await stop_audition(cam=cam, expect_token=state["token"])


def start_web_audition_holder(state: dict[str, Any], camilla_factory: Callable[[], Any],
                              idle_hold: Callable[[], Any]) -> threading.Thread:
    entered = False

    async def run() -> None:
        cam = camilla_factory()
        try:
            await hold_audition(state, cam=cam)
        finally:
            await cam.close()

    def worker() -> None:
        try:
            asyncio.run(run())
        except (OSError, RuntimeError, ValueError):
            log_event(logger, "active_speaker.audition", action="web_restore",
                      result="failed", level=logging.ERROR, exc_info=True)
        finally:
            hold.__exit__(None, None, None)

    try:
        hold = idle_hold()
        hold.__enter__()  # Take the idle hold before the request can finish.
        entered = True
        thread = threading.Thread(target=worker, daemon=True, name="speaker-audition")
        thread.start()
    except Exception:  # noqa: BLE001 - restore every holder setup failure (ADR-0329)
        try:
            if entered:
                hold.__exit__(None, None, None)
        finally:
            asyncio.run(stop_audition(cam=camilla_factory(), expect_token=state["token"]))
        raise
    return thread
