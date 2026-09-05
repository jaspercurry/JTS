# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Listen to the applied speaker at a reduced DSP layer, and always get it back.

Layer semantics, the crash-safety argument and the rejected alternatives are
ADR-0193's; this module is its implementation. The one fact worth repeating at
the call sites below, because every function here depends on it: the swap is
``set_active_config_raw``, which leaves CamillaDSP's persisted
``config_file_path`` alone, so a restart, a reboot or a ``kill -9`` of the owner
puts the applied graph back by doing nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from jasper.active_speaker.restore_wait import resilient_restore
from jasper.atomic_io import atomic_write_json
from jasper.log_event import log_event

logger = logging.getLogger(__name__)

AUDITION_LAYER_BASELINE = "baseline"
AUDITION_LAYER_FULL = "full"
AUDITION_LAYERS = (AUDITION_LAYER_BASELINE, AUDITION_LAYER_FULL)

# The walked-away bound, matching session_volume_plan's own wall-clock ceiling.
# Removal condition: it exists only while the audition is a foreground-owned
# swap (see ADR-0193).
AUDITION_DEADLINE_S = 1800.0
# How often the owner re-reads the state file to notice a stop or a takeover.
AUDITION_TICK_S = 5.0

AUDITION_STATE_KIND = "jts_active_speaker_audition"
AUDITION_SCHEMA_VERSION = 1
DEFAULT_AUDITION_STATE_PATH = Path("/run/jasper-active-speaker/audition.json")
AUDITION_STATE_ENV = "JASPER_ACTIVE_SPEAKER_AUDITION_STATE"

REFUSE_NO_APPLIED_PROFILE = "audition_no_applied_profile"
REFUSE_PROFILE_DISPLACED = "audition_applied_profile_displaced"
REFUSE_MEASUREMENT_ACTIVE = "audition_measurement_session_active"
REFUSE_COMMISSION_LOAD_ACTIVE = "audition_commission_load_active"
REFUSE_NO_DURABLE_ANCHOR = "audition_no_durable_anchor"
REFUSE_EMIT = "audition_emit_refused"
REFUSE_LOAD = "audition_load_refused"
REFUSE_RESTORE = "audition_restore_failed"

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
    "audition_state_path",
    "build_reduced_yaml",
    "hold_audition",
    "level_give_back_db",
    "read_audition_state",
    "start_audition",
    "stop_audition",
]


class AuditionRefused(RuntimeError):
    """The audition did not start or could not be put back. Carries a code."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}")



def audition_state_path(path: str | Path | None = None) -> Path:
    """Resolve the audition state path (explicit arg > env override > default)."""

    return Path(
        path or os.environ.get(AUDITION_STATE_ENV) or DEFAULT_AUDITION_STATE_PATH
    )


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
        or payload.get("layer") != AUDITION_LAYER_BASELINE
    ):
        return None
    return payload


def _clear_audition_state(path: str | Path | None = None) -> None:
    try:
        audition_state_path(path).unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        # The graph is already back; a stranded record only mis-reports. Loud
        # rather than silent, because /state would keep claiming an audition.
        log_event(
            logger,
            "active_speaker.audition",
            level=logging.WARNING,
            action="clear_state",
            result="failed",
            error=type(exc).__name__,
        )


def _refuse_if_graph_is_claimed() -> None:
    """Refuse while somebody else owns the running graph.

    ``live_measurement_session`` is the repo's ONE answer to "may an operator
    door act right now", and this consumes it rather than reading the volume
    statefile itself. That matters concretely, not just tidily: a
    ``/sound/room/`` room sweep takes jasper-control's measurement hold and
    never constructs a ``SessionVolumePlan``, so a statefile-only reading finds
    nothing and swaps the graph out from under a running capture.
    """

    from jasper.active_speaker.session_volume_plan import live_measurement_session
    from jasper.active_speaker.commission_load import load_commission_load_state

    refusal = live_measurement_session(action="auditioning")
    if refusal is not None:
        raise AuditionRefused(REFUSE_MEASUREMENT_ACTIVE, refusal)
    if load_commission_load_state().get("status") == "loaded":
        raise AuditionRefused(
            REFUSE_COMMISSION_LOAD_ACTIVE,
            "a per-driver commissioning config is armed, so the applied baseline "
            "is not what is playing; run `jasper-active-speaker commission-rollback` first",
        )


def _household_layers(
    anchor_path: str,
) -> tuple[list[Any], list[Any], float]:
    """The three program-domain inputs the live graph carries: (room, EQ, trim).

    Read exactly where ``/sound``'s own re-emit reads them, so an audition that
    drops the measured correction does not silently drop the household's EQ with
    it and confound the comparison. Room PEQs come back out of the durable anchor,
    which the displacement refusal has already proven is what is playing.
    """

    from jasper.sound.camilla_yaml import extract_room_peqs_from_config
    from jasper.sound.profile import build_sound_filter_slots, load_profile
    from jasper.sound.settings import load_sound_settings, output_trim_db

    profile = load_profile()
    settings = load_sound_settings()
    return (
        extract_room_peqs_from_config(anchor_path),
        list(build_sound_filter_slots(profile)),
        output_trim_db(profile, settings),
    )


def build_reduced_yaml(
    topology: Any,
    *,
    applied_profile: dict[str, Any],
    anchor_path: str,
) -> tuple[str | None, list[dict[str, str]]]:
    """Re-emit the applied graph without its two measured-correction stages.

    The bass profile is evaluated against the UNMUTATED record and passed
    explicitly: its acceptance is fingerprinted against the profile, so asking
    after the reduction would drop the stage and add a second difference to
    an A/B whose value is that there is only one.
    """

    from jasper.active_speaker.baseline_profile import recompose_applied_baseline_yaml
    from jasper.active_speaker.playback_route import resolve_live_active_endpoint
    from jasper.bass_extension.profile import evaluate_bass_extension_profile

    bass = evaluate_bass_extension_profile(
        topology=topology, applied_baseline_state=applied_profile
    )
    room_peqs, preference_filters, trim_db = _household_layers(anchor_path)
    device, _source = resolve_live_active_endpoint(topology)
    return recompose_applied_baseline_yaml(
        topology,
        applied_profile=applied_profile,
        room_peqs=room_peqs,
        preference_filters=preference_filters,
        output_trim_db=trim_db,
        out_path=None,
        playback_device=device,
        bass_extension_profile=(bass.profile if bass.status == "accepted" else None),
        drop_measured_correction=True,
    )


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
    """Load ``yaml_text`` as the running graph and prove it took.

    ``set_active_config_raw`` is what makes every swap here runtime-only. Its
    fader duck is kept on: unlike the measurement graph, an audition replaces
    the pipeline under live household audio, which is exactly the step the duck
    exists for.
    """

    from jasper.active_speaker.crossover_v2.composition import confirm_graph_is_live

    if not await cam.set_active_config_raw(yaml_text, best_effort=False):
        raise AuditionRefused(REFUSE_LOAD, refusal)
    await confirm_graph_is_live(cam, yaml_text)


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

    :func:`~jasper.active_speaker.web_commissioning.attempt_graph_restore` is
    the repo's one verdict a swap transaction reaches, and it decides success
    on ``is True`` — so the bridge from :func:`_put_back`, which returns
    ``None`` and raises instead, lives HERE rather than at each site. Two sites
    writing their own bridge is how one of them comes to read a restore that
    worked as one that failed.

    The put-back keeps its own duck (``set_active_config_raw``'s default) and
    its own ``confirm_graph_is_live`` read-back; this wraps the verdict, not
    the swap.
    """

    from jasper.active_speaker.web_commissioning import attempt_graph_restore

    async def _restore() -> bool:
        await _put_back(cam, anchor)
        return True

    return await attempt_graph_restore(_restore)


async def _undo_failed_arm(cam: Any, anchor: str) -> None:
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
    if not took_effect:
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
    state_path: str | Path | None = None,
    play_cue: CueSender | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Put ``layer`` on the speaker and record who owns the swap.

    ``full`` is the durable graph, so asking for it is the restore — one code
    path, not a second one that could drift from :func:`stop_audition`.
    """

    if layer not in AUDITION_LAYERS:
        raise ValueError(f"unknown audition layer: {layer!r}")
    if layer == AUDITION_LAYER_FULL:
        return await stop_audition(
            cam=cam, state_path=state_path, play_cue=play_cue
        )

    from jasper.active_speaker.baseline_profile import (
        applied_profile_displacement,
        baseline_config_path,
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.runtime_contract import (
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        classify_bass_extension_graph,
    )
    from jasper.dsp_apply import dsp_writer_lock
    from jasper.output_topology import load_output_topology

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
        anchor = await _durable_anchor(cam)
        yaml_text, issues = build_reduced_yaml(
            topology, applied_profile=applied, anchor_path=anchor
        )
        if yaml_text is None or issues:
            detail = "; ".join(
                str(issue.get("message") or issue.get("code")) for issue in issues
            ) or "the applied profile could not be re-emitted"
            raise AuditionRefused(REFUSE_EMIT, detail)
        # Re-proved here rather than trusted from the emitter, exactly as
        # `jasper-active-speaker baseline-reemit` does before it writes a byte.
        graph = classify_bass_extension_graph(
            topology,
            evidence_source="desired",
            graph_text=yaml_text,
            applied_baseline_state=applied,
        )
        if not graph.allowed or graph.classification != GRAPH_APPROVED_ACTIVE_RUNTIME:
            raise AuditionRefused(
                REFUSE_EMIT,
                f"the reduced graph did not re-prove as "
                f"{GRAPH_APPROVED_ACTIVE_RUNTIME} (got {graph.classification})",
            )
        # A swap that TOOK but was never recorded is the one state nothing
        # would put back: no record means no owner, no deadline and no /state
        # disclosure. Undoing it here costs a redundant reload on the paths
        # where nothing was loaded at all, which is the cheaper mistake.
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
                "layer": AUDITION_LAYER_BASELINE,
                "started_at": started_at,
                "deadline_at": started_at + AUDITION_DEADLINE_S,
                "entry_config_path": anchor,
                # Disclosed, never compensated: compensating would move a trim,
                # and identical trims are what makes the A/B mean anything.
                "louder_than_full_db": level_give_back_db(applied),
            }
            atomic_write_json(audition_state_path(state_path), state)
            armed = True
        finally:
            if not armed:
                await _undo_failed_arm(cam, anchor)

    log_event(
        logger,
        "active_speaker.audition",
        action="start",
        result="swapped",
        layer=AUDITION_LAYER_BASELINE,
        deadline_at=f"{state['deadline_at']:.0f}",
        louder_than_full_db=f"{state['louder_than_full_db']:.2f}",
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

    from jasper.active_speaker.baseline_profile import baseline_config_path
    from jasper.dsp_apply import dsp_writer_lock

    if not audition_state_path(state_path).exists():
        return {"status": "not_auditioning", "layer": AUDITION_LAYER_FULL}
    if expect_token is not None:
        live = read_audition_state(state_path)
        if live is None or live.get("token") != expect_token:
            return {"status": "superseded", "layer": AUDITION_LAYER_BASELINE}

    async with dsp_writer_lock(
        baseline_config_path().parent, source="active_speaker_audition_stop"
    ):
        anchor = await _durable_anchor(cam)
        took_effect, message = await _restore_verdict(cam, anchor)
        if not took_effect:
            # The record stays on disk on purpose: /state keeps disclosing that
            # the speaker is not on its applied graph, and the next `stop` has
            # something to retry against.
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
        _clear_audition_state(state_path)

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
