# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Mux activity truth, the live signal-path classifier, and its cause-naming
overrides.

A leaf the audio-health composer reads every fast tick: whether mux's
canonical per-source ``playing`` truth can even be trusted right now
(:func:`resolve_active_source`, :func:`activity_truth_unknown`), and —
given that verdict — the ordered precedence ladder that turns
fan-in/outputd/ring observations into one ``signal_path`` shape
(:func:`classify_signal_path`). The TTS-backlog derivation lives here too:
it feeds only this classifier.

The cause-naming detectors ``compose_audio_health`` layers onto that verdict
also live here: a parked transport (:func:`parked_signal`,
:func:`_transport_park_signal`), a stopped CamillaDSP
(:func:`stopped_dsp_signal`, :func:`camilla_stopped_verdict`), and hardware the
reconciler has found but the household never declared
(:func:`undeclared_hardware_signal`). The override SEQUENCE and its guard
(``_yields_to_a_named_cause``) are the composer's own ladder logic and stay
there — only the detectors moved.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..output_hardware import detected_hardware_adoption_precondition
from ..output_topology_observation import declared_hardware_mismatch
from ..fanin.status import DIRECT_HEALTH_BROKEN
from ..music_sources import MUSIC_SOURCE_SPECS
from ..platform.status_socket import FANIN_STALE_MS, OUTPUTD_STALE_MS
from ..service_units import CAMILLA_SERVICE, unit_not_running
from ._health_fields import (
    DIAGNOSTICS_REMEDY,
    RESTART_REMEDY,
    as_int,
    finite_number,
    mapping,
)
from ._health_sources import SOURCE_LABELS
from .transport_eligibility import (
    PARK_DAC_CONTENT_MARKER_BESIDE_BRIDGE,
    PARK_MONO_FULL_RANGE,
    PARK_PASSIVE_STEREO_COMPOSITE,
    PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED,
)

# `classify_signal_path`'s generic "outputd never started" and "fan-in is not
# reporting" sentences. Written once because
# `jasper.control.audio_state_issues._state_issues` raises the matching
# `path.outputd_unavailable` / `path.fanin_unavailable` incidents from the
# same two facts and neither pair may drift.
OUTPUT_ABSENT_TITLE = "The speaker's sound output is not running"
OUTPUT_ABSENT_DETAIL = (
    f"Nothing will play until it comes back. {RESTART_REMEDY} "
    f"{DIAGNOSTICS_REMEDY}"
)
PATH_UNREPORTED_TITLE = "Sound status unavailable"
PATH_UNREPORTED_DETAIL = (
    "JTS cannot tell whether sound is reaching the speaker right now, so "
    f"music may be missing. {RESTART_REMEDY}"
)

# The closed vocabulary of signal-path shape codes — every `code` any
# signal-path producer emits (`classify_signal_path` and the overrides
# `compose_audio_health` layers on it). A new shape registers itself HERE, which
# is what makes `test_the_household_shapes_cover_every_signal_path_code` fail
# until it is added to the household-register sweep as well.
SIGNAL_PATH_CODES = frozenset({
    "activity_unknown",
    "camilla_not_installed",
    "camilla_stopped",
    "clean",
    "input_absent",
    "input_broken",
    "input_stalled",
    "output_absent",
    "output_backend_inactive",
    "output_deaf",
    "output_ring_stalled",
    "output_stalled",
    "path_pressured",
    "path_stalled",
    "path_unreported",
    "starting",
    "transport_parked",
    "transport_unservable",
    "tts_queue_full",
    "undeclared_hardware",
})

# The one household-facing sentence for a box whose post-DSP transport is
# broken: CamillaDSP and outputd are on different loopback lanes, so nothing
# reaches the drivers however healthy each daemon looks. Sole writer of the
# /state wording; doctor phrases its own operator remedy.
#
# TWO detectors carry it, for the same household fact through different
# evidence: :func:`parked_signal` (a live transport contradiction) and
# :func:`_transport_park_signal` (one of ADR-0178's shapes the ring cannot
# serve). One sentence, so a household cannot be told two things about a
# speaker that is silent either way.
PARKED_HEADLINE = "Sound cannot come out of the speaker"

# ...and the sentence under it, for a park whose cause the household cannot be
# told anything more useful about: `parked_signal`'s live transport
# contradiction, and any park class with no row in the table below.
PARKED_DETAIL = (
    "The speaker's audio setup does not fit together, so nothing can play. "
    f"Check the speaker layout at /sound/speaker/. {DIAGNOSTICS_REMEDY}"
)

# One household sentence per ADR-0178 park class, in the register this card
# owns (#2472); the classifier's own `detail` is operator copy and is never
# spliced in here. The class token is imported rather than retyped so a rename
# cannot silently orphan a row. A class with no row here contributes no
# sentence; a park set with no rows at all degrades whole to PARKED_DETAIL.
_PARK_MESSAGES: dict[str, str] = {
    PARK_PASSIVE_STEREO_COMPOSITE: (
        "This speaker sends sound to two sound cards at once, and JTS can no "
        "longer drive that pair together. The speaker layout is at /sound/speaker/."
    ),
    PARK_MONO_FULL_RANGE: (
        "This speaker is set up as a single mono output, and JTS now needs at "
        "least two channels. The speaker layout is at /sound/speaker/."
    ),
    PARK_ROLEFUL_ACTIVE_ENDPOINT_UNCONVERGED: (
        "This speaker's per-driver outputs are ready, but sound is not pointed "
        "at them yet."
    ),
    PARK_DAC_CONTENT_MARKER_BESIDE_BRIDGE: (
        "This speaker is grouped, but its audio settings disagree with each "
        "other, so it cannot start playing. Run diagnostics for the one step "
        "that repairs it."
    ),
}

# What a park's household sentence adds when the class carries a recorded
# command rather than a tracked issue: where the household finds it. The
# command itself stays in doctor and `/system/snapshot`'s `transport_park`
# (#2472).
_PARK_REPAIRABLE = "Run diagnostics for the one step that repairs it."

# The one household-facing sentence for a stopped CamillaDSP (#2163), read by
# both surfaces it has to agree on: the `path.camilla_stopped` incident title
# and the signal-path headline that carries it into `overall`.
STOPPED_DSP_HEADLINE = "Sound processing has stopped"

# The one household-facing sentence for output hardware the reconciler has
# positively identified and is ready to use, when the DECLARED topology does
# not already claim it too (or nothing has ever been declared). Being ready
# alone is NOT enough: an already-declared, already-armed box hitting an
# ordinary outputd hiccup is also "positively identified and ready" and must
# not see this sentence (#2812 B1/B2). Sole writer of this wording.
UNDECLARED_HARDWARE_HEADLINE = "Detected hardware is ready — finish setup"

_LABEL_TO_SOURCE = {
    spec.fanin_label: spec.id.value for spec in MUSIC_SOURCE_SPECS
}


def fanin_selected_source(airplay: Mapping[str, Any]) -> str | None:
    current = mapping(airplay.get("current"))
    fanin = mapping(current.get("fanin"))
    selected = fanin.get("selected_input")
    if not isinstance(selected, str):
        return None
    normalized = selected.strip().lower()
    if normalized in _LABEL_TO_SOURCE:
        normalized = _LABEL_TO_SOURCE[normalized]
    return normalized if normalized in SOURCE_LABELS else None


def _source_playing(
    mux_status: Mapping[str, Any] | None,
    source_id: str | None,
) -> bool | None:
    """Project mux's canonical per-source activity without inventing fallback."""
    if source_id is None or not isinstance(mux_status, Mapping):
        return None
    source = mapping(mapping(mux_status.get("sources")).get(source_id))
    playing = source.get("playing")
    return playing if isinstance(playing, bool) else None


def resolve_active_source(
    airplay: Mapping[str, Any],
    mux_status: Mapping[str, Any] | None,
) -> str | None:
    selected = fanin_selected_source(airplay)
    return selected if _source_playing(mux_status, selected) is True else None


def activity_truth_unknown(
    airplay: Mapping[str, Any],
    mux_status: Mapping[str, Any] | None,
) -> bool:
    """Whether mux cannot authoritatively classify the selected lane."""
    if not isinstance(mux_status, Mapping) or not isinstance(
        mux_status.get("sources"),
        Mapping,
    ):
        return True
    selected = fanin_selected_source(airplay)
    return selected is not None and _source_playing(mux_status, selected) is None


ACTIVITY_UNKNOWN_DETAIL = "JTS cannot tell which source is playing right now."


def activity_unavailable_signal() -> dict[str, str]:
    return {
        "code": "activity_unknown",
        "status": "unknown",
        "headline": "Playback activity unavailable",
        "detail": ACTIVITY_UNKNOWN_DETAIL,
    }


def _tts_backlog_ratio(*lanes: Any) -> float:
    """Deepest ``pending/budget`` across every armed TTS lane; 0.0 if none is."""
    deepest = 0.0
    for lane_raw in lanes:
        lane = mapping(lane_raw)
        if lane.get("enabled") is not True:
            continue
        budget_frames = as_int(lane.get("budget_frames"))
        if budget_frames <= 0:
            continue
        deepest = max(deepest, as_int(lane.get("pending_frames")) / budget_frames)
    return deepest


def classify_signal_path(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    active_source: str | None,
) -> dict[str, Any]:
    current = mapping(airplay.get("current"))
    fanin_raw = current.get("fanin")
    warmup = bool(airplay.get("warmup_active"))
    if not isinstance(fanin_raw, Mapping):
        if warmup:
            return {
                "code": "starting",
                "status": "idle",
                "headline": "Audio is starting",
                "detail": "Sound will be ready in a moment.",
            }
        return {
            "code": "path_unreported",
            "status": "unknown",
            "headline": PATH_UNREPORTED_TITLE,
            "detail": PATH_UNREPORTED_DETAIL,
        }
    if outputd is None:
        if warmup:
            return {
                "code": "starting",
                "status": "idle",
                "headline": "Audio is starting",
                "detail": "Sound will be ready in a moment.",
            }
        return {
            "code": "output_absent",
            "status": "issue",
            "headline": OUTPUT_ABSENT_TITLE,
            "detail": OUTPUT_ABSENT_DETAIL,
        }

    outputd_map = mapping(outputd)
    backend = outputd_map.get("backend")
    if backend is not None and backend != "alsa":
        return {
            "code": "output_backend_inactive",
            "status": "issue",
            "headline": "The speaker is not connected to its sound hardware",
            "detail": (
                "Sound is being processed but has nowhere to go, so nothing "
                f"will play. {RESTART_REMEDY} {DIAGNOSTICS_REMEDY}"
            ),
        }
    outputd_watchdog = mapping(outputd_map.get("watchdog"))
    outputd_progress_age = as_int(
        outputd_watchdog.get("last_progress_age_ms"),
    )
    if outputd_watchdog and outputd_progress_age > OUTPUTD_STALE_MS:
        return {
            "code": "output_stalled",
            "status": "issue",
            "headline": "Sound has stopped reaching the speaker",
            "detail": (
                "Sound stopped moving out to the speaker a few seconds ago. "
                f"{RESTART_REMEDY}"
            ),
        }

    fanin = mapping(fanin_raw)
    watchdog = mapping(fanin.get("watchdog"))
    if as_int(watchdog.get("last_progress_age_ms")) > FANIN_STALE_MS:
        return {
            "code": "path_stalled",
            "status": "issue",
            "headline": "Sound has stopped moving through the speaker",
            "detail": (
                "Sound from your sources stopped moving through the speaker a "
                f"few seconds ago. {RESTART_REMEDY}"
            ),
        }

    output = mapping(fanin.get("output"))
    ring = mapping(output.get("ring"))
    if ring.get("stall_active") is True:
        # ABOVE `output_deaf` for the same reason the fan-in watchdog is: a
        # ring the reader has stopped draining is what leaves outputd with
        # nothing to play, and the cause outranks its own symptom.
        return {
            "code": "output_ring_stalled",
            "status": "issue",
            "headline": "Sound is stuck inside the speaker",
            "detail": (
                "Sound from your sources is arriving but cannot move on to "
                f"the speaker's output. {RESTART_REMEDY}"
            ),
        }

    # outputd is writing periods, but what it writes is silence it did not
    # intend: a deaf chain leaves both watchdogs progressing and every xrun
    # count flat (#3458). The verdict is outputd's own — it owns the DAC
    # geometry its threshold is derived from.
    #
    # BELOW the fan-in watchdog deliberately: a stalled fan-in starves
    # CamillaDSP, which empties the ring, so outputd latches deaf at 2 s while
    # FANIN_STALE_MS only trips at 5 — the cause outranks its own symptom, the
    # same way `camilla_stopped` outranks this in `compose_audio_health`.
    #
    # Not during warmup: outputd primes and starts reading an empty ring before
    # CamillaDSP is producing (the gate `stopped_dsp_signal` carries for the
    # same reason).
    if not warmup and mapping(outputd_map.get("content")).get("deaf") is True:
        return {
            "code": "output_deaf",
            "status": "issue",
            "headline": "The speaker is playing silence",
            "detail": (
                "Sound is reaching the speaker's last step, but nothing is "
                f"arriving for it to play. {RESTART_REMEDY} "
                f"{DIAGNOSTICS_REMEDY}"
            ),
        }

    active = active_source
    inputs = mapping(fanin.get("inputs"))
    active_input = mapping(inputs.get(active)) if active else {}
    if active and active_input.get("present") is False:
        return {
            "code": "input_absent",
            "status": "issue",
            "headline": "This source is not reaching the speaker",
            "detail": (
                f"{SOURCE_LABELS.get(active, 'The source')} is playing, but "
                "the speaker has no open connection for it. Play it again, or "
                "try another source."
            ),
        }
    if active_input.get("health") == DIRECT_HEALTH_BROKEN:
        return {
            "code": "input_broken",
            "status": "issue",
            "headline": "This source is not reaching the speaker",
            "detail": (
                f"{SOURCE_LABELS.get(active or '', 'The source')} stopped "
                "sending sound to the speaker. Play it again, or try another "
                "source."
            ),
        }
    frames_per_sec = active_input.get("frames_per_sec")
    if (
        active
        and isinstance(frames_per_sec, (int, float))
        and not isinstance(frames_per_sec, bool)
        and frames_per_sec < 1000.0
    ):
        return {
            "code": "input_stalled",
            "status": "issue",
            "headline": "No sound is arriving from this source",
            "detail": (
                f"{SOURCE_LABELS.get(active, active)} is selected, but no "
                "sound is coming from it. Play it again, or try another source."
            ),
        }
    # Losing periods, from either end: the ring dropped a period the reader
    # never took, or the active lane is xrunning. Both are rates, so neither
    # latches once the box recovers.
    ring_drops = finite_number(ring.get("drops_per_sec"))
    input_xrun_rate = finite_number(active_input.get("xruns_per_sec"))
    if (
        (ring_drops is not None and ring_drops > 0.0)
        or (input_xrun_rate is not None and input_xrun_rate > 0.0)
    ):
        return {
            "code": "path_pressured",
            "status": "warn",
            "headline": "Sound is only just keeping up",
            "detail": (
                "Music is playing, but the speaker is right at the edge of "
                f"keeping up with it, so it may skip. {RESTART_REMEDY}"
            ),
        }

    # Both TTS lanes can be armed at once — fan-in's socket has a non-optional
    # default, and outputd's arms on a passive bonded member — so the enabled
    # flag cannot pick between them. Report on whichever is deepest against its
    # own budget; an idle lane can never mask a backed-up one.
    if _tts_backlog_ratio(fanin.get("tts"), outputd_map.get("tts")) >= 1.0:
        return {
            "code": "tts_queue_full",
            "status": "warn",
            "headline": "Voice replies are delayed",
            "detail": (
                "JTS has more spoken replies waiting than it can play right "
                "now, so answers may arrive late. Music is unaffected."
            ),
        }
    return {
        "code": "clean",
        "status": "ok",
        "headline": "Sound path is healthy",
        "detail": "Everything between your sources and the speaker is responding.",
    }


def parked_signal(route: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the parked signal-path state, or None when the transport is sane.

    When the cause is a DAC that cannot host the saved layout at all, the
    detail names the DAC and where the household fixes it — no reconcile or
    restart clears that one. Otherwise it says only what the household can act
    on: the contradiction itself is operator evidence and stays in doctor's
    transport-coherence check, which fails on the same fact.
    """
    transport = mapping(route.get("transport"))
    errors = [
        error
        for error in transport.get("coherence_errors") or []
        if isinstance(error, str) and error
    ]
    if not errors:
        return None
    label = str(mapping(transport.get("capability_gap")).get("device_label") or "")
    if label.strip():
        detail = (
            f"{label.strip()} cannot drive an active speaker layout, so nothing "
            "can play. Choose a passive speaker layout at /sound/speaker/ (passive "
            "sends full-range to every output; requires a built-in passive "
            "crossover) or attach an active-capable DAC."
        )
    else:
        detail = PARKED_DETAIL
    return {
        "code": "transport_parked",
        "status": "issue",
        "headline": PARKED_HEADLINE,
        "detail": detail,
    }


def _park_detail(parks: Any) -> str:
    """The household sentence for one or more live transport parks.

    Composed from :data:`_PARK_MESSAGES` plus the park record's OWN ``issue``
    and ``remedy``. Falls back whole to :data:`PARKED_DETAIL` when no park in
    ``parks`` has a message, so an unknown class still says something.

    Joins every park's sentence rather than picking one: a box can be in two
    classes at once (a bonded mono speaker waits on both), and ADR-0178 keeps
    them all for the same reason.
    """
    messages: list[str] = []
    for park in parks if isinstance(parks, (list, tuple)) else []:
        park = mapping(park)
        message = _PARK_MESSAGES.get(str(park.get("park_class") or ""))
        if message is None:
            continue
        issue = str(park.get("issue") or "").strip()
        if issue:
            message = f"{message} Tracked as {issue}."
        elif str(park.get("remedy") or "").strip():
            message = f"{message} {_PARK_REPAIRABLE}"
        messages.append(message)
    return " ".join(messages) if messages else PARKED_DETAIL


def _transport_park_signal(
    transport_park: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the signal path for a LIVE transport park, or ``None``.

    Only ``status="parked"`` reaches the household: that is the state where no
    transport serves this box and it emits nothing.

    Presentation only, like :func:`parked_signal`: the incident rows
    :func:`~jasper.control.audio_state_issues._state_issues` writes from the
    same snapshot keep one row per park class, named by its key.
    """
    state = mapping(transport_park)
    if state.get("status") != "parked":
        return None
    return {
        "code": "transport_unservable",
        "status": "issue",
        "headline": PARKED_HEADLINE,
        "detail": _park_detail(state.get("parks")),
    }


def stopped_dsp_signal(
    airplay: Mapping[str, Any],
    service_states: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the stopped-CamillaDSP signal path, or None when it is running.

    :func:`classify_signal_path` structurally CANNOT see this.  It reads only
    fan-in and outputd, and both keep looping when the stage between them
    disappears: fan-in's `shm_ring` coupling free-run-drops on an absent reader
    rather than blocking, outputd reads its content lane nonblocking and
    zero-fills, and BOTH `last_progress_age_ms` counters time the work loop's
    iteration, not audio actually moving.  A dead CamillaDSP therefore leaves
    every input to `classify_signal_path` healthy while the speaker emits
    nothing.

    Presentation only, like :func:`parked_signal`:
    :class:`~jasper.control.audio_health_sampler.AudioHealthSampler` feeds
    :func:`~jasper.control.audio_state_issues._state_issues` the raw signal
    path, so `path.camilla_stopped` keeps its own incident row.

    Shares the boot-warmup gate with that issue, so a deploy's coordinated
    restart does not flicker the card.
    """
    if bool(airplay.get("warmup_active")):
        return None
    stopped = camilla_stopped_verdict(
        mapping(service_states).get(CAMILLA_SERVICE)
    )
    if stopped is None:
        return None
    code, detail = stopped
    return {
        "code": code,
        "status": "issue",
        "headline": STOPPED_DSP_HEADLINE,
        "detail": detail,
    }


def undeclared_hardware_signal(
    output_hardware: Any,
    output_topology_snapshot: Any,
) -> dict[str, Any] | None:
    """Return the "ready hardware is waiting to be declared" signal, or None.

    ``output_hardware`` is the reconciler-published
    :class:`~jasper.output_hardware.OutputHardwareState` (or ``None`` when
    unreadable); ``output_topology_snapshot`` is a
    :class:`~jasper.output_topology_store.OutputTopologySnapshot` (or ``None``
    before the sampler's first read) — the bare topology is not enough, see
    below.

    Two conjuncts, mirroring the wizard's own "Use detected hardware"
    affordance (#2812 B1); neither is re-derived here, both call the owners
    the browser's mismatch card and adoption button read.
    :func:`~jasper.output_hardware.detected_hardware_adoption_precondition`
    (INNER) says the detected hardware is usable at all — known profile, no
    blocking issue, at least one output — and says nothing about whether the
    household already declared it.
    :func:`~jasper.output_topology_observation.declared_hardware_mismatch` (OUTER) says
    the DECLARED topology does not already match what is attached; skipping it
    told an already-armed box hitting an ordinary outputd hiccup to "finish
    setup" for a setup that already happened.

    A never-declared box does NOT reach that second conjunct (#2812 B2).
    ``load_output_topology``'s missing-file fallback (``new_topology_draft``)
    auto-seeds ``hardware`` FROM the observed record whenever it has outputs —
    which the inner conjunct just proved. ``declared_hardware_mismatch`` on
    that ephemeral draft would always find a match, making the two conjuncts
    mutually exclusive on a fresh box. ``snapshot.revision == "missing"`` is
    read directly instead and satisfies the outer conjunct on its own.
    """
    if output_hardware is None or output_topology_snapshot is None:
        return None

    if not detected_hardware_adoption_precondition(output_hardware)["allowed"]:
        return None
    if output_topology_snapshot.revision != "missing":
        if declared_hardware_mismatch(
            output_topology_snapshot.topology, output_hardware
        ) is None:
            return None
    detail = (
        f"{output_hardware.profile_label} is connected and detected, but "
        "hasn't been set as the speaker's active output yet. Finish setup "
        "at /sound/speaker/."
    )
    return {
        "code": "undeclared_hardware",
        "status": "issue",
        "headline": UNDECLARED_HARDWARE_HEADLINE,
        "detail": detail,
    }


def camilla_stopped_verdict(raw_state: Any) -> tuple[str, str] | None:
    """``(code, household detail)`` for a CamillaDSP unit that is not running.

    ``None`` when it is running or on the way up. The code is what surfaces
    and tests discriminate on; the detail is household copy, so the unit
    name, its systemd state and the `journalctl` line stay in doctor's
    `check_camilla_service`, which fails on the same fact.

    Reads :func:`jasper.service_units.unit_not_running`, wider than a bare
    `failed` check on purpose: a clean stop and a jasper-camilla-recover park
    (#2163, ADR-0175) both count, because CamillaDSP — unlike jasper-outputd's
    missing-DAC `ExecCondition` or jasper-voice's `voice-input-absent` marker —
    has no `Condition*`/`ExecCondition` of its own and runs `Restart=always`.

    A NEVER-INSTALLED unit keeps its own code and its own remedy: reinstalling
    is the fix, and no restart can clear it.
    """
    code = unit_not_running(mapping(raw_state))
    if code == "missing":
        return (
            "camilla_not_installed",
            "This speaker's sound processing is not installed, and all sound "
            "runs through it, so nothing can play. Re-run the installer.",
        )
    if code is None or code == "starting":
        return None
    return (
        "camilla_stopped",
        "All sound runs through this speaker's processing, and it is not "
        f"running, so nothing will play until it starts. {RESTART_REMEDY}",
    )
