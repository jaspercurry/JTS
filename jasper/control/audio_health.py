# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One bounded, normalized audio-health snapshot for management surfaces.

The existing AirPlay collector already owns the expensive monitoring cadence
(fan-in STATUS, shairport/Camilla journals, MPRIS, and Camilla status).  This
module composes it with cheap local outputd and mux STATUS reads plus a slow
route-claim read.  Mux owns the canonical per-source ``playing`` predicates;
the dashboard does not duplicate them.  Production starts only
:class:`AudioHealthSampler`'s thread; the AirPlay collector is sampled inline,
so the broader dashboard adds no daemon or second resident loop.

The contract deliberately separates continuity from timing.  A USB host-clock
``l2_fallback`` keeps audio playing safely, so it degrades the latency axis but
does not claim the signal path failed.  Likewise ``l0_locked`` is live clocking
state, not proof of end-to-end latency; only the route artifact can verify that
claim.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

from ..audio_runtime_plan import (
    USB_LOW_LATENCY_P95_BUDGET_MS,
    USB_LOW_LATENCY_P99_BUDGET_MS,
)
from ..local_sources.registry import local_source_lifecycles
from ..music_sources import MUSIC_SOURCE_SPECS, Source
from ..fanin.latency_mode import PRESETS, classify_runtime
from ..source_intent import read_source_intents
from .airplay_health import (
    CAMILLA_UNIT_FULL,
    AirPlayHealthSampler,
    SAMPLE_INTERVAL_SEC,
)
from .audio_incidents import IncidentStore, IssueTracker, SessionRollup
from .uds import MAX_STATUS_BYTES, MUX_CONTROL_SOCKET_PATH, _mux_socket_command

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ROUTE_INTERVAL_SEC = 60.0
OUTPUTD_SOCKET = "/run/jasper-outputd/control.sock"
LOCAL_STATUS_TIMEOUT_SEC = 1.0
FANIN_STALE_MS = 5000
OUTPUTD_STALE_MS = 3000

# Every household-facing sentence this module writes is household register:
# what is wrong with the household's sound and what they can do about it, never
# a daemon name, a unit, a systemd state, or a command (#2472). The operator
# half of each state already has a home — `jasper-doctor` carries the unit
# names and `journalctl` lines, and `/state.audio_health.technical` carries the
# raw counters — so nothing is lost by keeping them off the front page. The two
# remedies named below are the buttons that sit on the same /system/ page as
# this card.
RESTART_REMEDY = "Try Restart audio."
DIAGNOSTICS_REMEDY = "Run diagnostics if sound doesn't come back."

# The one household-facing sentence for a box whose post-DSP transport is
# broken: CamillaDSP and outputd are on different loopback lanes, so nothing
# reaches the drivers however healthy each daemon looks. Doctor phrases its own
# operator remedy; this is the only writer of the /state wording.
#
# TWO detectors carry it, for the same household fact through different
# evidence: :func:`_parked_signal` (a live transport contradiction) and
# :func:`_transport_park_signal` (one of ADR-0178's four shapes the ring
# cannot serve). One sentence, so a household cannot be told two things about
# a speaker that is silent either way.
PARKED_HEADLINE = "Sound cannot come out of the speaker"

# ...and the sentence under it, for every park whose cause the household
# cannot be told anything more useful about. Both detectors and the
# `path.transport_park.*` incident rows share it: which class parked the box,
# which endpoint disagreed, and the operator's one-command remedy are carried
# by `/state.resilience.transport_park` and doctor's `check_ring_transport_park`,
# which reads the same verdict.
PARKED_DETAIL = (
    "The speaker's audio setup does not fit together, so nothing can play. "
    f"Check the speaker layout at /sound/setup/. {DIAGNOSTICS_REMEDY}"
)

# The one household-facing sentence for a stopped CamillaDSP (#2163). Written
# once and read by both surfaces it has to agree on: the `path.camilla_stopped`
# incident title and the signal-path headline that carries it into `overall`.
STOPPED_DSP_HEADLINE = "Sound processing has stopped"

# `_signal_path`'s generic "outputd never started" and "fan-in is not
# reporting" sentences. Written once because `_state_issues` raises the
# matching `path.outputd_unavailable` / `path.fanin_unavailable` incidents from
# the same two facts and neither pair may drift.
_OUTPUT_ABSENT_TITLE = "The speaker's sound output is not running"
_OUTPUT_ABSENT_DETAIL = (
    f"Nothing will play until it comes back. {RESTART_REMEDY} "
    f"{DIAGNOSTICS_REMEDY}"
)
PATH_UNREPORTED_TITLE = "Sound status unavailable"
PATH_UNREPORTED_DETAIL = (
    "JTS cannot tell whether sound is reaching the speaker right now, so "
    f"music may be missing. {RESTART_REMEDY}"
)

# The closed vocabulary of signal-path shape codes — every `code` any
# signal-path producer emits (`_signal_path` and the three overrides
# `compose_audio_health` layers on it). A new shape registers itself HERE,
# beside the branch that emits it, and that is what makes
# `test_the_household_shapes_cover_every_signal_path_code` fail until the new
# shape is added to the household-register sweep as well.
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
    "output_stalled",
    "path_stalled",
    "path_unreported",
    "starting",
    "transport_parked",
    "transport_unservable",
    "tts_queue_full",
    "undeclared_hardware",
})

# The two `_signal_path` codes that mean "outputd is not delivering audio, for
# a reason `_signal_path` cannot see": outputd never started at all (its
# missing-declaration `ExecCondition` kept the unit down, so its control socket
# never answers) or it is up but self-reports a non-ALSA backend (the
# dual-Apple `action=park_until_active_graph` path keeps sockets alive on a
# `fake` backend without opening ALSA). `_undeclared_hardware_signal` refines
# only these two; every other concrete `_signal_path` issue — fan-in down, a
# stale watchdog, a broken active input — is left untouched.
_UNDECLARED_OUTPUT_CODES = frozenset({"output_absent", "output_backend_inactive"})

# Expected failures at optional/cached observability boundaries. Programming
# errors outside this set should not be hidden; a dead sampler is surfaced as
# stale by snapshot() instead of silently retrying a broken implementation.
_MONITOR_ERRORS = (
    AttributeError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)

_LABEL_TO_SOURCE = {
    spec.fanin_label: spec.id.value for spec in MUSIC_SOURCE_SPECS
}
_SOURCE_LABELS = {
    spec.id.value: spec.display_name for spec in MUSIC_SOURCE_SPECS
}
_SOURCE_HEALTH_UNITS = {
    lifecycle.source.value: lifecycle.health_units
    for lifecycle in local_source_lifecycles()
}
_SOURCE_OFF_DRIFT_UNITS = {
    lifecycle.source.value: lifecycle.park_units
    for lifecycle in local_source_lifecycles()
}
_SOURCE_PRIMARY_UNITS = {
    lifecycle.source.value: (
        lifecycle.intent_unit
        or (lifecycle.runtime_units[0] if lifecycle.runtime_units else None)
    )
    for lifecycle in local_source_lifecycles()
}


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _nonnegative_counter(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _read_local_status(
    socket_path: str = OUTPUTD_SOCKET,
    timeout_sec: float = LOCAL_STATUS_TIMEOUT_SEC,
    max_bytes: int = MAX_STATUS_BYTES,
) -> dict[str, Any] | None:
    """Read one local daemon STATUS response, byte/time bounded and fail-soft."""
    try:
        deadline = time.monotonic() + timeout_sec
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_sec)
            sock.connect(socket_path)
            sock.sendall(b"STATUS\n")
            chunks: list[bytes] = []
            total = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                chunk = sock.recv(min(8192, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, OSError):
        return None
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _read_mux_status(
    socket_path: str = MUX_CONTROL_SOCKET_PATH,
    timeout_sec: float = LOCAL_STATUS_TIMEOUT_SEC,
) -> dict[str, Any] | None:
    """Read mux's already-normalized source activity over its local UDS."""
    try:
        return asyncio.run(
            _mux_socket_command(
                "STATUS",
                socket_path=socket_path,
                timeout=timeout_sec,
            )
        )
    except _MONITOR_ERRORS:
        logger.debug("audio health mux STATUS probe failed", exc_info=True)
        return None


def _read_output_hardware() -> Any:
    """Read the reconciler-published output-hardware record, fail-soft.

    Same reader ``/state.audio.output_hardware``
    (:mod:`jasper.control.state_aggregate`) and the ``/sound/setup/``
    hardware-adoption precondition use — a small ``/run`` JSON read that
    ``jasper.output_hardware.load_state`` already returns ``None`` for on any
    missing/corrupt file. The extra guard here matches every other probe in
    this module: an unexpected attribute, type, or OS-level surprise
    (``_MONITOR_ERRORS``) degrades to "no record" rather than taking a health
    tick down. A broken import is not one of those — it would fail identically
    on every call from process start, so it is a startup-time bug to fix, not
    a per-tick condition to swallow.
    """
    try:
        from ..output_hardware import load_state

        return load_state()
    except _MONITOR_ERRORS:
        logger.debug("audio health output-hardware probe failed", exc_info=True)
        return None


def _read_output_topology() -> Any:
    """Read the DECLARED output topology's SNAPSHOT (topology + revision),
    fail-soft.

    Reads ``load_output_topology_snapshot`` -- the same reader
    ``/sound/setup/`` uses (``jasper.web.sound_setup._output_topology_payload``)
    -- rather than the bare ``load_output_topology``. This matters (#2812
    B2): on a missing file, both readers fall back to ``new_topology_draft``,
    which auto-seeds ``hardware`` FROM the observed record whenever it has
    outputs. A caller that only sees the resulting ``OutputTopology`` cannot
    tell "genuinely never declared" from "declared and already matches" --
    the auto-seed makes them look identical whenever the observed hardware is
    ready, which is exactly the state this module's setup hint needs to
    recognize as UNdeclared. ``snapshot.revision == "missing"`` is the fact
    that survives the auto-seed: it says a topology was never actually
    persisted, independent of what the ephemeral draft's ``hardware`` field
    happens to contain.
    """
    try:
        from ..output_topology import load_output_topology_snapshot

        return load_output_topology_snapshot()
    except _MONITOR_ERRORS:
        logger.debug("audio health output-topology probe failed", exc_info=True)
        return None


def _empty_transport() -> dict[str, Any]:
    """Return a fresh "no contradictions" transport state.

    Built per call, never copied from a module constant: ``dict(constant)`` is
    shallow, so every caller would share one ``coherence_errors`` list and a
    single append anywhere would report the box as parked on every later
    degraded read for the lifetime of the process.
    """
    return {"coherence_errors": [], "coherence_notes": [], "capability_gap": None}


def _transport_state(
    *,
    coupling: str | None,
    outputd_env: Mapping[str, str],
    camilla_devices: Mapping[str, Any] | None,
    topology: Any,
) -> dict[str, Any]:
    """Pair the post-DSP transport contradictions with their actionable cause.

    ``transport_coherence_report`` is the single detector — doctor reads the
    same function — so this offers no second opinion about what "disconnected"
    means.  The capability gap says *why* it cannot self-heal when the saved
    layout needs hardware the DAC does not have.

    ``coherence_notes`` carries the report's non-error half verbatim: coherent
    but not steady, so it is published for whoever curls ``/state`` and is
    deliberately NOT fed to :func:`_parked_signal` — the household card would
    say "parked" about a rung of an operator-only ladder the household cannot
    act on.  ``jasper-doctor`` is the loud surface for that state.

    ``topology`` is an :class:`~jasper.output_topology.OutputTopology`, typed
    loosely because this module imports the topology layer lazily.
    """
    from ..active_speaker.playback_route import active_lane_capability_gap
    from ..audio_runtime_plan import transport_coherence_report

    report = transport_coherence_report(
        coupling=coupling,
        outputd_env=dict(outputd_env),
        camilla_devices=camilla_devices,
    )
    gap = active_lane_capability_gap(topology)
    return {
        "coherence_errors": list(report.errors),
        "coherence_notes": list(report.notes),
        "capability_gap": gap.to_dict() if gap is not None else None,
    }


def _parked_graph_transport() -> dict[str, Any] | None:
    """Transport state for the intentional PARKED graph, or None when absent.

    Feeds :func:`_parked_signal` through the same ``coherence_errors`` channel
    the transport detector uses, so the parked wording keeps exactly one writer.
    The capability gap is resolved the same way :func:`_transport_state` does, so
    a no-active-lane DAC still gets its "and it can never work here" clause after
    this reason rather than instead of it.
    """
    from ..active_speaker.environment import read_camilla_statefile_config_path
    from ..active_speaker.playback_route import active_lane_capability_gap
    from ..active_speaker.runtime_contract import (
        active_graph_is_parked,
        parked_muted_exits,
    )
    from ..audio_runtime_plan import DEFAULT_CAMILLA_STATEFILE_PATH
    from ..output_topology import OutputTopologyError, load_output_topology_strict

    config_path = read_camilla_statefile_config_path(DEFAULT_CAMILLA_STATEFILE_PATH)
    if not active_graph_is_parked(config_path):
        return None
    try:
        topology = load_output_topology_strict()
    except OutputTopologyError:
        # A malformed saved layout must not be reclassified as an empty draft:
        # that would tell a household to choose a new layout while hiding a
        # fault the doctor correctly fails. The parked graph remains safe, but
        # this is not intentional setup silence.
        return {
            "coherence_errors": [
                "Saved speaker layout is unavailable or invalid; run jasper-doctor"
            ],
            "coherence_notes": [],
            "capability_gap": None,
        }
    gap = active_lane_capability_gap(topology)
    return {
        "coherence_errors": [
            "CamillaDSP is holding the parked graph, so every output is muted "
            f"({parked_muted_exits(topology)})"
        ],
        "coherence_notes": [],
        "capability_gap": gap.to_dict() if gap is not None else None,
    }


def _read_transport_state(plan: Any) -> dict[str, Any]:
    """Read the transport evidence the coherence detector needs, for ``plan``.

    Reads the evidence doctor reads, so the dashboard and ``jasper-doctor``
    cannot disagree about whether the post-DSP route is connected: both halves
    of the loopback pair (the loaded CamillaDSP graph, and outputd's LIVE
    capture PCM with its env as the fallback), and both statefiles.

    Deliberately silent when the loaded graph does not target a registered
    output endpoint: that is "coherence unknown", and doctor skips the same
    detector on the same evidence rather than reporting a contradiction it
    cannot see both halves of.

    ``plan.route_policy_errors`` is not read instead: that tuple deliberately
    mixes these contradictions with USB low-latency route-policy errors, and a
    policy error is not a reason to tell a household its speaker is parked.
    """
    from ..audio_runtime_plan import (
        DEFAULT_CAMILLA2_STATEFILE_PATH,
        DEFAULT_CAMILLA_STATEFILE_PATH,
        DEFAULT_OUTPUTD_ENV_PATH,
        output_endpoint_evidence_from_statefiles,
    )
    from ..env_load import read_env_file_state
    from ..fanin_coupling import COUPLING_ENV_VAR
    from ..output_topology import load_output_topology

    evidence = output_endpoint_evidence_from_statefiles(
        DEFAULT_CAMILLA_STATEFILE_PATH,
        DEFAULT_CAMILLA2_STATEFILE_PATH,
    )
    if evidence.devices is None or not evidence.endpoint_recognized:
        # One unrecognized endpoint is NOT "coherence unknown": the PARKED graph
        # (#2135) writes to a File sink on purpose, because the saved roleful
        # layout has no staged startup graph yet. That is precisely the parked
        # state, so it must not read as ready just because the graph declines
        # to name an outputd lane.
        return _parked_graph_transport() or _empty_transport()
    # outputd.env is read here because the plan carries decisions, not the
    # generated env it was built from.
    outputd_env = dict(read_env_file_state(DEFAULT_OUTPUTD_ENV_PATH).values)
    outputd_status = _mapping(_read_local_status())
    live_pcm = str(_mapping(outputd_status.get("content")).get("pcm") or "")
    if live_pcm:
        # Prefer what outputd actually opened over what it was told to open, so
        # a reconcile window (env already rewritten, daemon not yet restarted)
        # cannot read as a disconnect.
        outputd_env["JASPER_OUTPUTD_CONTENT_PCM"] = live_pcm
    return _transport_state(
        # The plan's own resolved COUPLING, never its transport topology NAME.
        # `transport_coherence_report` takes a coupling TOKEN and re-derives the
        # shape itself (from that token plus outputd's endpoint marker), so a
        # shape name handed in here goes through `resolve_coupling`, whose
        # deliberate fail-SAFE maps everything outside {loopback, shm_ring} to
        # loopback. Two of the three shape names alias their coupling token
        # (TRANSPORT_LOOPBACK, TRANSPORT_SHM_RING), so the substitution looked
        # right until the third arrived: on an armed roleful box the shape is
        # `shm_ring_active`, which silently resolved to loopback and told a
        # demonstrably-playing speaker it was parked (#2376) while `/state`'s own
        # coupling surface reported the ring armed and live. This reads the fact
        # doctor reads — the persisted coupling — resolved once, by the same plan
        # that produced `plan.transport_topology`.
        coupling=str(plan.setting(COUPLING_ENV_VAR).value),
        outputd_env=outputd_env,
        camilla_devices=evidence.devices,
        topology=load_output_topology(),
    )


def read_route_claim() -> dict[str, Any]:
    """Read the declared route plus its measured latency artifact.

    This is config/statefile work plus one bounded outputd STATUS read rather
    than a live audio probe, and therefore runs on the slow cadence.  The
    artifact assessment is shared with ``/state`` via
    :func:`jasper.control.state_aggregate.route_latency_artifact_state`.
    """
    try:
        from ..audio_runtime_plan import build_audio_runtime_plan_from_system
        from .state_aggregate import route_latency_artifact_state

        plan = build_audio_runtime_plan_from_system()
        profile = plan.route_profile
        # Own try: a transport read that fails must not downgrade the whole
        # route claim to "unavailable" and take the latency card with it.
        try:
            transport = _read_transport_state(plan)
        except _MONITOR_ERRORS:
            logger.debug("audio transport coherence read failed", exc_info=True)
            transport = _empty_transport()
        return {
            "status": "available",
            "route_id": profile.route_id,
            "source_id": profile.source_id,
            "fixed_sample_rate": profile.fixed_sample_rate,
            "low_latency_claim": profile.low_latency_claim,
            "route_config_hash": plan.route_config_hash,
            "p95_budget_ms": profile.p95_budget_ms,
            "p99_budget_ms": profile.p99_budget_ms,
            "artifact": route_latency_artifact_state(plan),
            "transport": transport,
        }
    except _MONITOR_ERRORS as exc:
        logger.debug("audio route claim read failed", exc_info=True)
        return {
            "status": "unavailable",
            "route_id": None,
            "source_id": None,
            "fixed_sample_rate": None,
            "low_latency_claim": False,
            "route_config_hash": None,
            "p95_budget_ms": None,
            "p99_budget_ms": None,
            "artifact": {"status": "fail", "reason": str(exc)},
            "transport": _empty_transport(),
        }


def _issue(
    key: str,
    *,
    scope: str,
    impact: str,
    severity: str,
    title: str,
    detail: str,
    source_id: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "scope": scope,
        "source_id": source_id,
        "impact": impact,
        "severity": severity,
        "title": title,
        "detail": detail,
    }


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return value

def _selected_source(airplay: Mapping[str, Any]) -> str | None:
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    selected = fanin.get("selected_input")
    if not isinstance(selected, str):
        return None
    normalized = selected.strip().lower()
    if normalized in _LABEL_TO_SOURCE:
        normalized = _LABEL_TO_SOURCE[normalized]
    return normalized if normalized in _SOURCE_LABELS else None


def _source_playing(
    mux_status: Mapping[str, Any] | None,
    source_id: str | None,
) -> bool | None:
    """Project mux's canonical per-source activity without inventing fallback."""
    if source_id is None or not isinstance(mux_status, Mapping):
        return None
    source = _mapping(_mapping(mux_status.get("sources")).get(source_id))
    playing = source.get("playing")
    return playing if isinstance(playing, bool) else None


def _active_source(
    airplay: Mapping[str, Any],
    mux_status: Mapping[str, Any] | None,
) -> str | None:
    selected = _selected_source(airplay)
    return selected if _source_playing(mux_status, selected) is True else None


def _activity_truth_unknown(
    airplay: Mapping[str, Any],
    mux_status: Mapping[str, Any] | None,
) -> bool:
    """Whether mux cannot authoritatively classify the selected lane."""
    if not isinstance(mux_status, Mapping) or not isinstance(
        mux_status.get("sources"),
        Mapping,
    ):
        return True
    selected = _selected_source(airplay)
    return selected is not None and _source_playing(mux_status, selected) is None


ACTIVITY_UNKNOWN_DETAIL = "JTS cannot tell which source is playing right now."


def _activity_unavailable_signal() -> dict[str, str]:
    return {
        "code": "activity_unknown",
        "status": "unknown",
        "headline": "Playback activity unavailable",
        "detail": ACTIVITY_UNKNOWN_DETAIL,
    }


def _parked_signal(route: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the parked signal-path state, or None when the transport is sane.

    The sole writer of the parked wording.  When the cause is a DAC that cannot
    host the saved layout at all, the detail names the DAC and where the
    household fixes it — because no reconcile or restart can clear that one.
    Otherwise it says only what the household can act on: the contradiction
    itself is operator evidence and stays where operators read it — doctor's
    transport-coherence check, which fails on the same fact and prints every
    error with its remedy.

    Presentation only: :class:`AudioHealthSampler` deliberately feeds
    :func:`_state_issues` the raw signal path, so a warn-level issue keeps its
    own incident row instead of being swallowed by the standing reason.
    """
    transport = _mapping(route.get("transport"))
    errors = [
        error
        for error in transport.get("coherence_errors") or []
        if isinstance(error, str) and error
    ]
    if not errors:
        return None
    label = str(_mapping(transport.get("capability_gap")).get("device_label") or "")
    if label.strip():
        detail = (
            f"{label.strip()} cannot drive an active speaker layout, so nothing "
            "can play. Choose a passive speaker layout at /sound/setup/ (passive "
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


def _transport_park_signal(
    transport_park: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the signal path for a LIVE transport park, or ``None``.

    Only ``status="parked"`` reaches the household: that is the ring-only
    state where no transport serves this box and it emits nothing. A
    ``"pending"`` verdict — the box is in one of ADR-0178's four classes but
    the loopback route still carries it — is an OPERATOR fact and stops at
    jasper-doctor and ``/state``. Telling a household its playing speaker is
    parked would be the confusion ADR-0100 exists to prevent, pointed the
    wrong way.

    Presentation only, exactly like :func:`_parked_signal`: the incident rows
    :func:`_state_issues` writes from the same snapshot keep one row per park
    class, named by its key.

    The classifier's own ``detail`` is deliberately NOT spliced in here. It is
    written for doctor and ``/state.resilience.transport_park`` and reads like
    it ("Ring B", the endpoint marker, the dac_content lane) — operator
    evidence, which is the register #2472 took off this card.
    """
    state = _mapping(transport_park)
    if state.get("status") != "parked":
        return None
    return {
        "code": "transport_unservable",
        "status": "issue",
        "headline": PARKED_HEADLINE,
        "detail": PARKED_DETAIL,
    }


def _stopped_dsp_signal(
    airplay: Mapping[str, Any],
    service_states: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the stopped-CamillaDSP signal path, or None when it is running.

    :func:`_signal_path` structurally CANNOT see this.  It reads only fan-in
    and outputd, and both are built to keep looping when the stage between
    them disappears: fan-in's default `loopback` coupling is timer-paced
    (`docs/HANDOFF-fan-in-daemon.md` — "structurally immune"), `shm_ring`
    free-run-drops on an absent reader rather than blocking, outputd reads its
    content lane nonblocking and zero-fills ("absent content becomes silence.
    This keeps the final output loop alive"), and BOTH `last_progress_age_ms`
    counters time the work loop's iteration, not audio actually moving.  So a
    dead CamillaDSP leaves every input to `_signal_path` healthy while the
    speaker emits nothing — `overall` would otherwise report a clean path and
    a playing source next to its own :data:`STOPPED_DSP_HEADLINE` incident.

    Presentation only, exactly like :func:`_parked_signal`:
    :class:`AudioHealthSampler` feeds :func:`_state_issues` the raw signal
    path, so `path.camilla_stopped` keeps its own incident row rather than
    being swallowed by the headline it produces here.

    Shares the boot-warmup gate with that issue, so a deploy's coordinated
    restart does not flicker the card.
    """
    if bool(airplay.get("warmup_active")):
        return None
    stopped = _camilla_stopped(_mapping(service_states).get(CAMILLA_UNIT_FULL))
    if stopped is None:
        return None
    code, detail = stopped
    return {
        "code": code,
        "status": "issue",
        "headline": STOPPED_DSP_HEADLINE,
        "detail": detail,
    }


# The one household-facing sentence for output hardware the reconciler has
# positively identified and is ready to use, when the DECLARED topology does
# not already claim it too (or nothing has ever been declared) -- being ready
# alone is not enough to show this: an already-declared, already-armed box
# hitting an ordinary outputd hiccup is also "positively identified and
# ready" and must not see this sentence (#2812 B1/B2). A composite DAC can
# sit in the genuine gap for minutes after a hotplug -- admitted, but held
# back pending the active-graph handshake -- and until this detector existed
# the household saw only the generic "outputd is not reporting" line, which
# reads as a broken speaker rather than as a two-minute setup step. Written
# once; the only writer of this wording.
UNDECLARED_HARDWARE_HEADLINE = "Detected hardware is ready — finish setup"


def _undeclared_hardware_signal(
    output_hardware: Any,
    output_topology_snapshot: Any,
) -> dict[str, Any] | None:
    """Return the "ready hardware is waiting to be declared" signal, or None.

    ``output_hardware`` is the reconciler-published
    :class:`~jasper.output_hardware.OutputHardwareState` (or ``None`` when
    unreadable) — the same record ``/state.audio.output_hardware`` publishes
    and ``/sound/setup/``'s "Use detected hardware" button already gates on.
    ``output_topology_snapshot`` is a
    :class:`~jasper.output_topology.OutputTopologySnapshot` (or ``None``
    before the sampler's first read) — the topology alone is not enough; see
    below.

    Two conjuncts, mirroring the wizard's own "Use detected hardware"
    affordance exactly (#2812 B1):

    * :func:`jasper.output_hardware.detected_hardware_adoption_precondition`
      — the INNER conjunct — says the detected hardware is usable at all
      (a known profile, no blocking issue, at least one output). This alone
      is NOT enough: it says nothing about whether the household already
      declared and armed this exact hardware.
    * :func:`jasper.output_topology.declared_hardware_mismatch` — the OUTER
      conjunct — says the DECLARED topology does not already match what's
      attached. Skipping this let an already-armed box hitting an ordinary
      outputd hiccup (a deploy's audio-graph bounce, a crash) be told to
      "finish setup" for a setup that already happened — proven live on a
      declared, serial-bound speaker with an unrelated `outputd` fault
      (#2812 B1).

    A never-declared box does NOT reach that second conjunct at all (#2812
    B2). ``load_output_topology``'s (and thus a bare
    ``OutputTopologySnapshot.topology``'s) missing-file fallback
    (``new_topology_draft``) auto-seeds ``hardware`` FROM the observed
    record whenever it has outputs — which the inner conjunct just proved is
    true here. Calling ``declared_hardware_mismatch`` on that ephemeral,
    never-persisted draft would always find a match, making the two
    conjuncts mutually exclusive on a fresh box and hiding the exact speaker
    #2812 exists for. ``snapshot.revision == "missing"`` is read directly
    instead: it says nothing was ever persisted, independent of what the
    auto-seeded draft's ``hardware`` field contains, and satisfies the outer
    conjunct on its own without ever calling ``declared_hardware_mismatch``.

    Neither conjunct is re-derived here: the gate mandate is
    single-source-of-truth, so this function calls the same two owners the
    browser's own mismatch card and adoption button read, rather than
    forking either rule into a third Python-only copy.
    """
    if output_hardware is None or output_topology_snapshot is None:
        return None
    from ..output_hardware import detected_hardware_adoption_precondition

    if not detected_hardware_adoption_precondition(output_hardware)["allowed"]:
        return None
    if output_topology_snapshot.revision != "missing":
        from ..output_topology import declared_hardware_mismatch

        if declared_hardware_mismatch(
            output_topology_snapshot.topology, output_hardware
        ) is None:
            return None
    detail = (
        f"{output_hardware.profile_label} is connected and detected, but "
        "hasn't been set as the speaker's active output yet. Finish setup "
        "at /sound/setup/."
    )
    return {
        "code": "undeclared_hardware",
        "status": "issue",
        "headline": UNDECLARED_HARDWARE_HEADLINE,
        "detail": detail,
    }


def _signal_path(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    active_source: str | None,
) -> dict[str, Any]:
    current = _mapping(airplay.get("current"))
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
            "headline": _OUTPUT_ABSENT_TITLE,
            "detail": _OUTPUT_ABSENT_DETAIL,
        }

    outputd_map = _mapping(outputd)
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
    outputd_watchdog = _mapping(outputd_map.get("watchdog"))
    outputd_progress_age = _as_int(
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

    fanin = _mapping(fanin_raw)
    watchdog = _mapping(fanin.get("watchdog"))
    if _as_int(watchdog.get("last_progress_age_ms")) > FANIN_STALE_MS:
        return {
            "code": "path_stalled",
            "status": "issue",
            "headline": "Sound has stopped moving through the speaker",
            "detail": (
                "Sound from your sources stopped moving through the speaker a "
                f"few seconds ago. {RESTART_REMEDY}"
            ),
        }
    active = active_source
    inputs = _mapping(fanin.get("inputs"))
    active_input = _mapping(inputs.get(active)) if active else {}
    if active and active_input.get("present") is False:
        return {
            "code": "input_absent",
            "status": "issue",
            "headline": "This source is not reaching the speaker",
            "detail": (
                f"{_SOURCE_LABELS.get(active, 'The source')} is playing, but "
                "the speaker has no open connection for it. Play it again, or "
                "try another source."
            ),
        }
    if active_input.get("health") == "broken":
        return {
            "code": "input_broken",
            "status": "issue",
            "headline": "This source is not reaching the speaker",
            "detail": (
                f"{_SOURCE_LABELS.get(active or '', 'The source')} stopped "
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
                f"{_SOURCE_LABELS.get(active, active)} is selected, but no "
                "sound is coming from it. Play it again, or try another source."
            ),
        }
    tts = _mapping(outputd_map.get("tts"))
    pending_frames = _as_int(tts.get("pending_frames"))
    budget_frames = _as_int(tts.get("budget_frames"))
    if (
        tts.get("enabled") is True
        and budget_frames > 0
        and pending_frames >= budget_frames
    ):
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


def _verification(route: Mapping[str, Any]) -> dict[str, Any]:
    if not bool(route.get("low_latency_claim")):
        return {
            "status": "not_applicable",
            "validated_at": None,
            "p95_ms": None,
            "p99_ms": None,
            "p95_budget_ms": None,
            "p99_budget_ms": None,
            "issues": [],
        }
    artifact = _mapping(route.get("artifact"))
    artifact_status = artifact.get("status")
    issues = [
        str(issue)
        for issue in artifact.get("issues") or []
        if isinstance(issue, str)
    ]
    budget_issue_codes = {
        f"p95_exceeds_{USB_LOW_LATENCY_P95_BUDGET_MS:g}ms",
        f"p99_exceeds_{USB_LOW_LATENCY_P99_BUDGET_MS:g}ms",
    }
    target_missed = any(issue in budget_issue_codes for issue in issues)
    if target_missed:
        status = "target_missed"
    else:
        status = {
            "pass": "verified",
            "warn": "partial",
        }.get(str(artifact_status), "unverified")
    return {
        "status": status,
        "validated_at": artifact.get("validated_at"),
        "p95_ms": artifact.get("p95_ms"),
        "p99_ms": artifact.get("p99_ms"),
        "p95_budget_ms": route.get("p95_budget_ms"),
        "p99_budget_ms": route.get("p99_budget_ms"),
        "issues": issues,
    }


def _usb_timing(
    route: Mapping[str, Any],
    host_clock: Mapping[str, Any] | None,
    usb_input: Mapping[str, Any] | None = None,
    *,
    active: bool,
) -> dict[str, Any]:
    claimed = bool(route.get("low_latency_claim"))
    verification = _verification(route)
    resampler = _mapping(_mapping(usb_input).get("resampler"))
    latency_runtime = classify_runtime(resampler, host_clock)
    raw_mode = latency_runtime.ladder
    preset_mode = latency_runtime.applied_mode
    mode = {
        "l0_locked": "lowest_latency",
        "l1_warn": "tracking_warn",
        "l2_fallback": "fallback",
        "probing": "checking",
        "disabled": "standard",
    }.get(str(raw_mode), "unknown")
    runtime: dict[str, Any] = {
        "mode": mode,
        "raw_mode": raw_mode,
        "phase": latency_runtime.phase,
    }
    if preset_mode is not None:
        runtime.update({
            "preset": preset_mode,
            "effective_preset": latency_runtime.effective_mode,
            "held_target_frames": latency_runtime.held_frames,
            "floor_frames": latency_runtime.floor_frames,
        })

    if preset_mode is not None:
        preset = PRESETS[preset_mode]
        current_frames = latency_runtime.held_frames or preset.floor_frames
        current_ms = current_frames * 1000 / 48_000
        if active and latency_runtime.phase == "fallback":
            status = "warn"
            headline = f"Stable fallback · {current_ms:.1f} ms input buffer"
            detail = (
                "Playback is protected by more buffering while JTS retries "
                "USB timing."
                if latency_runtime.fallback_reason == "actuator_unavailable"
                else "Playback is protected by more buffering for this USB session."
            )
        elif active and latency_runtime.phase == "clock_adjusting":
            status = "warn"
            headline = f"{preset.label} latency · clock tracking under strain"
            detail = "Playback remains locked while USB host timing stabilizes."
        elif active and latency_runtime.phase == "buffer_adjusting":
            status = "warn"
            headline = f"Recovery buffer active · {current_ms:.1f} ms input buffer"
            detail = "Latency will fall after USB host timing stabilizes."
        elif active and latency_runtime.phase == "checking":
            status = "idle"
            headline = "Checking USB host timing"
            detail = "Playback is safe while JTS checks USB timing."
        else:
            status = "ok"
            headline = f"{preset.label} latency · {current_ms:.1f} ms input buffer"
            detail = (
                "The larger stable USB buffer is active."
                if preset_mode == "high"
                else "The selected USB input buffer is active."
            )
        return {
            "applicable": active,
            "source_id": Source.USBSINK.value,
            "kind": "route_latency",
            "status": status,
            "headline": headline,
            "detail": detail,
            "route_id": route.get("route_id"),
            "verification": verification,
            "runtime": runtime,
        }

    if route.get("status") != "available":
        return {
            "applicable": active,
            "source_id": Source.USBSINK.value,
            "kind": "route_latency",
            "status": "unknown",
            "headline": "USB latency state unavailable",
            "detail": (
                "JTS cannot check this computer's USB audio delay; playback "
                "health is checked separately."
            ),
            "route_id": route.get("route_id"),
            "verification": verification,
            "runtime": runtime,
        }
    if not claimed:
        return {
            "applicable": active,
            "source_id": Source.USBSINK.value,
            "kind": "route_latency",
            "status": "idle",
            "headline": "Standard buffered route",
            "detail": "This route does not make a measured low-latency claim.",
            "route_id": route.get("route_id"),
            "verification": verification,
            "runtime": runtime,
        }
    if active and raw_mode == "l2_fallback":
        status = "warn"
        headline = "Stable fallback · latency increased"
        detail = "Playback is protected by resampling while host timing recovers."
    elif active and raw_mode == "l1_warn":
        status = "warn"
        headline = "Low latency active · clock tracking under strain"
        detail = "The host is following the speaker clock with unusually high demand."
    elif active and raw_mode == "probing":
        status = "idle"
        headline = "Checking USB host timing"
        detail = "Playback is safe while JTS checks USB timing."
    elif active and raw_mode not in {"l0_locked", "l1_warn", "l2_fallback"}:
        status = "warn"
        headline = "USB low-latency clock mode unavailable"
        detail = (
            "Playback continues with standard buffering; JTS is not "
            "fine-tuning USB timing right now."
        )
    else:
        status = "ok"
        headline = "Low latency · stable"
        if active and raw_mode == "l0_locked":
            detail = "USB is running with the smallest safe delay."
        else:
            detail = "The low-latency route is active."
    return {
        "applicable": active or claimed,
        "source_id": Source.USBSINK.value,
        "kind": "route_latency",
        "status": status,
        "headline": headline,
        "detail": detail,
        "route_id": route.get("route_id"),
        "verification": verification,
        "runtime": runtime,
    }


def _airplay_timing(airplay: Mapping[str, Any], *, active: bool) -> dict[str, Any]:
    if not active:
        status = "idle"
        headline = "AirPlay idle"
        detail = "Sync timing is checked while AirPlay is playing."
    else:
        recent = _mapping(airplay.get("summary_5m"))
        sync_events = (
            _as_int(recent.get("shairport_packet_drops"))
            + _as_int(recent.get("shairport_sync_errors"))
            + _as_int(recent.get("shairport_underruns"))
        )
        if sync_events:
            status = "warn"
            headline = "AirPlay sync recently recovered"
            detail = "Wireless timing had a recent correction; playback is still monitored."
        else:
            status = "ok"
            headline = "AirPlay sync timing clean"
            detail = "No recent sender or synchronization corrections."
    return {
        "applicable": active,
        "source_id": Source.AIRPLAY.value,
        "kind": "sync",
        "status": status,
        "headline": headline,
        "detail": detail,
        "route_id": None,
        "verification": {
            "status": "not_applicable",
            "validated_at": None,
            "p95_ms": None,
            "p99_ms": None,
            "p95_budget_ms": None,
            "p99_budget_ms": None,
            "issues": [],
        },
        "runtime": {"mode": "standard", "raw_mode": None},
    }


def _not_applicable_timing() -> dict[str, Any]:
    return {
        "applicable": False,
        "source_id": None,
        "kind": "none",
        "status": "idle",
        "headline": "No timing contract for this source",
        "detail": "Timing is shown only where JTS has an honest runtime signal.",
        "route_id": None,
        "verification": {
            "status": "not_applicable",
            "validated_at": None,
            "p95_ms": None,
            "p99_ms": None,
            "p95_budget_ms": None,
            "p99_budget_ms": None,
            "issues": [],
        },
        "runtime": {"mode": "standard", "raw_mode": None},
    }


def _state_issues(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    signal_path: Mapping[str, Any],
    latency: Mapping[str, Any],
    active_source: str | None,
    service_states: Mapping[str, Any] | None = None,
    source_intents: Mapping[str, bool] | None = None,
    *,
    activity_unknown: bool = False,
    undeclared_hardware: Mapping[str, Any] | None = None,
    transport_park: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    # ADR-0178's named transport parks, one row per class so the household
    # card and the operator both see EVERY tracked issue this box waits on
    # rather than a first-match verdict. Ahead of the live-daemon rows and
    # outside the warmup gate: a park is structural, it is true at boot, and
    # a restart never clears it. Only a LIVE park (ring-only) is a household
    # incident — see :func:`_transport_park_signal`.
    park_state = _mapping(transport_park)
    if park_state.get("status") == "parked":
        for park in park_state.get("parks") or []:
            if not isinstance(park, Mapping):
                continue
            # The park CLASS rides the key, where an identifier belongs; the
            # classifier's operator detail, its tracked issue and the
            # one-command remedy stay in doctor and
            # `/state.resilience.transport_park`, which read the same verdict.
            park_class = str(park.get("park_class"))
            issues.append(_issue(
                f"path.transport_park.{park_class}",
                scope="path",
                impact="continuity",
                severity="issue",
                title=PARKED_HEADLINE,
                detail=PARKED_DETAIL,
            ))
    warmup = bool(airplay.get("warmup_active"))
    current = _mapping(airplay.get("current"))
    fanin = current.get("fanin")
    if activity_unknown:
        issues.append(_issue(
            "monitor.mux_status_unavailable",
            scope="monitor",
            impact="observability",
            severity="warn",
            title="Playback activity unavailable",
            detail=ACTIVITY_UNKNOWN_DETAIL,
        ))
    if not warmup and not isinstance(fanin, Mapping):
        issues.append(_issue(
            "path.fanin_unavailable",
            scope="path",
            impact="continuity",
            severity="issue",
            title=PATH_UNREPORTED_TITLE,
            detail=PATH_UNREPORTED_DETAIL,
        ))
    if not warmup:
        camilla_stopped = _camilla_stopped(
            _mapping(service_states).get(CAMILLA_UNIT_FULL)
        )
        if camilla_stopped is not None:
            issues.append(_issue(
                "path.camilla_stopped",
                scope="path",
                impact="continuity",
                severity="issue",
                title=STOPPED_DSP_HEADLINE,
                detail=camilla_stopped[1],
            ))
    if not warmup and outputd is None:
        # S3 (#2812 gate round 1): when the setup hint fires for this exact
        # condition, the incident row must say the same thing the headline
        # does — otherwise the household sees a friendly "finish setup" card
        # right next to a danger badge for the identical fact. Both are
        # computed from the same `undeclared_hardware` value the sampler
        # passes in, so they cannot drift out of alignment with each other,
        # only together.
        if undeclared_hardware is not None:
            title = str(undeclared_hardware.get("headline"))
            detail = str(undeclared_hardware.get("detail"))
        else:
            title = _OUTPUT_ABSENT_TITLE
            detail = _OUTPUT_ABSENT_DETAIL
        issues.append(_issue(
            "path.outputd_unavailable",
            scope="path",
            impact="continuity",
            severity="issue",
            title=title,
            detail=detail,
        ))
    # The rows below ARE their signal-path shape, so they carry its sentence
    # rather than a second copy of it: one writer per household sentence.
    path_code = signal_path.get("code")
    if path_code == "path_stalled":
        issues.append(_issue(
            "path.fanin_watchdog_stale",
            scope="path",
            impact="continuity",
            severity="issue",
            title=str(signal_path.get("headline")),
            detail=str(signal_path.get("detail")),
        ))
    if path_code == "output_stalled":
        issues.append(_issue(
            "path.outputd_watchdog_stale",
            scope="path",
            impact="continuity",
            severity="issue",
            title=str(signal_path.get("headline")),
            detail=str(signal_path.get("detail")),
        ))
    if path_code == "output_backend_inactive":
        # Same alignment as path.outputd_unavailable above.
        if undeclared_hardware is not None:
            title = str(undeclared_hardware.get("headline"))
            detail = str(undeclared_hardware.get("detail"))
        else:
            title = str(signal_path.get("headline"))
            detail = str(signal_path.get("detail"))
        issues.append(_issue(
            "path.outputd_backend_inactive",
            scope="path",
            impact="continuity",
            severity="issue",
            title=title,
            detail=detail,
        ))
    if path_code == "tts_queue_full":
        issues.append(_issue(
            "path.tts_queue_full",
            scope="path",
            impact="continuity",
            severity="warn",
            title=str(signal_path.get("headline")),
            detail=str(signal_path.get("detail")),
        ))
    if path_code in {"input_absent", "input_broken", "input_stalled"}:
        source_id = active_source
        issues.append(_issue(
            f"{source_id or 'source'}.input_unavailable",
            scope="source",
            source_id=source_id,
            impact="continuity",
            severity="issue",
            title=str(signal_path.get("headline")),
            detail=str(signal_path.get("detail")),
        ))
    if active_source == Source.USBSINK.value:
        latency_runtime = _mapping(latency.get("runtime"))
        raw_mode = latency_runtime.get("raw_mode")
        if raw_mode == "l2_fallback" and latency_runtime.get("preset") != "high":
            issues.append(_issue(
                "usbsink.latency_fallback",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB switched to stable latency fallback",
                detail="Playback continues safely with more buffering.",
            ))
        elif raw_mode == "l1_warn":
            issues.append(_issue(
                "usbsink.clock_tracking_warn",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB clock tracking is under strain",
                detail="Playback is still in its low-delay mode.",
            ))
        if latency.get("status") == "unknown":
            issues.append(_issue(
                "usbsink.latency_state_unavailable",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB latency state unavailable",
                detail="JTS cannot check this computer's USB audio delay.",
            ))
        elif (
            _mapping(latency.get("runtime")).get("raw_mode")
            not in {"l0_locked", "l1_warn", "l2_fallback", "probing"}
        ):
            issues.append(_issue(
                "usbsink.host_clock_unavailable",
                scope="latency",
                source_id=Source.USBSINK.value,
                impact="latency",
                severity="warn",
                title="USB low-latency clock mode unavailable",
                detail="Playback continues with standard buffering.",
            ))
    for source_id, health_units in _SOURCE_HEALTH_UNITS.items():
        desired = _mapping(source_intents).get(source_id)
        units = (
            _SOURCE_OFF_DRIFT_UNITS.get(source_id, ())
            if desired is False
            else health_units
        )
        for unit in units:
            unit_state = _mapping(service_states).get(unit)
            if desired is False:
                if _mapping(unit_state).get("active_state") == "active":
                    issues.append(_issue(
                        f"{source_id}.service.{unit}.off_drift",
                        scope="source",
                        source_id=source_id,
                        impact="availability",
                        severity="issue",
                        title=(
                            f"{_SOURCE_LABELS.get(source_id, source_id)} "
                            "is running while Off"
                        ),
                        detail=SOURCE_OFF_DRIFT_DETAIL,
                    ))
                continue
            if not _service_failed(unit_state):
                continue
            issues.append(_issue(
                f"{source_id}.service.{unit}",
                scope="source",
                source_id=source_id,
                impact="availability",
                severity="issue",
                title=f"{_SOURCE_LABELS.get(source_id, source_id)} is unavailable",
                detail=SOURCE_UNAVAILABLE_DETAIL,
            ))
    return issues


# systemd ActiveState values that mean the unit is up or on its way up.
# Everything else — `inactive`, `deactivating`, `failed`, `maintenance` — means
# no process is doing the unit's job right now.
_UNIT_RUNNING_ACTIVE_STATES = frozenset({"active", "activating", "reloading"})


def _camilla_stopped(raw_state: Any) -> tuple[str, str] | None:
    """``(code, household detail)`` for a CamillaDSP unit that is not running.

    ``None`` when it is running.  The code is what surfaces and tests
    discriminate on; the detail is household copy, so the unit name, its
    systemd state and the `journalctl` line stay in doctor's
    `check_camilla_service`, which fails on the same fact.

    Deliberately wider than :func:`_service_failed`, which only fires on
    `failed`/`error`/`not-found`. A CLEANLY stopped CamillaDSP — `inactive`
    with `result=success` — was the state no surface could see (#2163), and it
    is reachable: `jasper-camilla-recover` parks the unit stopped after an
    exhausted start-limit burst, and a kill between the coupling reconciler's
    camilla-stop and camilla-start leaves it stopped without ever going
    `failed` (which is why `OnFailure=jasper-camilla-recover` does not catch
    it).

    Nor does a stopped unit necessarily carry a restart count that some OTHER
    surface could have caught: the recover script's `camilla_start_failed`
    exit runs `systemctl reset-failed jasper-camilla.service` immediately
    before the start it then fails, so it parks the unit with the counter
    already cleared. This detector reads neither `result` nor `n_restarts`
    for exactly that reason — not running is the fact.

    Scoped to CamillaDSP rather than generalised over the core units, because
    the two neighbours have a legitimate parked state and it would be a false
    alarm to treat them the same way: `jasper-outputd` parks itself `inactive`
    through a missing-DAC `ExecCondition`, and `jasper-voice` through the
    `voice-input-absent` marker. CamillaDSP has no such gate — no `Condition*`,
    no `ExecCondition` — and its unit file says it must never stay stopped.

    Silent when systemd truth is unavailable (no `systemctl`, or before the
    first service-state probe): unknown is not stopped.

    A NEVER-INSTALLED unit keeps its own code and its own remedy: reinstalling
    is the fix, and no restart can clear it. Whole point of #2163 is one fact
    reading the same way on every surface, so the two must not disagree about
    the same box.
    """
    state = _mapping(raw_state)
    active_state = str(state.get("active_state") or "")
    if str(state.get("load_state") or "") in {"error", "not-found"}:
        return (
            "camilla_not_installed",
            "This speaker's sound processing is not installed, and all sound "
            "runs through it, so nothing can play. Re-run the installer.",
        )
    if not active_state or active_state in _UNIT_RUNNING_ACTIVE_STATES:
        return None
    return (
        "camilla_stopped",
        "All sound runs through this speaker's processing, and it is not "
        f"running, so nothing will play until it starts. {RESTART_REMEDY}",
    )


# The one household-facing sentence for a source whose renderer has failed.
# Which unit failed and how is doctor's per-renderer checks; the household is
# told what it can do instead.
SOURCE_UNAVAILABLE_DETAIL = (
    f"JTS could not start this source. {RESTART_REMEDY} {DIAGNOSTICS_REMEDY}"
)

# ...and for a source still running after the household turned it Off. Saving
# the choice again is what re-runs the reconciler that stops it.
SOURCE_OFF_DRIFT_DETAIL = (
    "It is still running even though Music sources has it turned off. Set it "
    "to Off again in Music sources to clear this."
)


def _service_failed(raw_state: Any) -> bool:
    """Whether cached systemd truth says this unit is not doing its job."""
    state = _mapping(raw_state)
    active_state = str(state.get("active_state") or "")
    result = str(state.get("result") or "")
    load_state = str(state.get("load_state") or "")
    return (
        active_state == "failed"
        or load_state in {"error", "not-found"}
        or (result not in {"", "success"} and active_state != "active")
    )


def _source_service_summary(
    source_id: str,
    service_states: Mapping[str, Any] | None,
    source_intents: Mapping[str, bool] | None = None,
) -> tuple[str, str, str] | None:
    """Return ``(state, headline, detail)`` from cached systemd truth."""
    states = _mapping(service_states)
    desired = _mapping(source_intents).get(source_id)
    if desired is False:
        if any(
            _mapping(states.get(unit)).get("active_state") == "active"
            for unit in _SOURCE_OFF_DRIFT_UNITS.get(source_id, ())
        ):
            return (
                "unavailable",
                f"{_SOURCE_LABELS.get(source_id, source_id)} is running while Off",
                SOURCE_OFF_DRIFT_DETAIL,
            )
        return "off", "Off", "Turned off in Music sources."
    if not states:
        return None
    for unit in _SOURCE_HEALTH_UNITS.get(source_id, ()):
        if _service_failed(states.get(unit)):
            return (
                "unavailable",
                f"{_SOURCE_LABELS.get(source_id, source_id)} unavailable",
                SOURCE_UNAVAILABLE_DETAIL,
            )
    primary = _SOURCE_PRIMARY_UNITS.get(source_id)
    primary_state = _mapping(states.get(primary)) if primary else {}
    if primary_state.get("active_state") == "active":
        return "ready", "Ready", "Waiting for a stream."
    if primary_state.get("active_state") == "inactive":
        return "not_running", "Not running", "Nothing is running for this source."
    return None


def _source_cards(
    airplay: Mapping[str, Any],
    signal_path: Mapping[str, Any],
    route: Mapping[str, Any],
    active_source: str | None,
    service_states: Mapping[str, Any] | None = None,
    source_intents: Mapping[str, bool] | None = None,
) -> list[dict[str, Any]]:
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    inputs = _mapping(fanin.get("inputs"))
    host_clock = _mapping(fanin.get("host_clock")) or None
    cards: list[dict[str, Any]] = []
    for spec in MUSIC_SOURCE_SPECS:
        source_id = spec.id.value
        active = active_source == source_id
        status = "ok" if active else "idle"
        headline = "Playing" if active else "Idle"
        detail = (
            "Playing through the speaker."
            if active else "No active stream."
        )
        state = "active" if active else "idle"
        service_summary = _source_service_summary(
            source_id,
            service_states,
            source_intents,
        )
        if service_summary is not None and (
            not active or service_summary[0] == "unavailable"
        ):
            state, headline, detail = service_summary
            if state == "ready":
                status = "ok"
            elif state == "unavailable":
                status = "issue"
        timing: dict[str, Any] | None = None
        if spec.id == Source.AIRPLAY:
            timing = _airplay_timing(airplay, active=active)
            if active and timing["status"] in {"warn", "unknown"}:
                status = "warn"
        elif spec.id == Source.USBSINK:
            timing = _usb_timing(
                route, host_clock, _mapping(inputs.get(source_id)), active=active
            )
            if active and timing["status"] in {"warn", "unknown"}:
                status = "warn"
        if active and signal_path.get("status") in {"issue", "unknown"}:
            status = str(signal_path.get("status"))
            headline = str(signal_path.get("headline"))
            detail = str(signal_path.get("detail"))
        cards.append({
            "id": source_id,
            "label": spec.display_name,
            "state": state,
            "status": status,
            "headline": headline,
            "detail": detail,
            "timing": timing,
        })
    return cards


def _detail(label: str, value: Any) -> dict[str, str]:
    return {"label": label, "value": str(value)}


def _duration_label(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 1.0:
        return f"{round(seconds * 1000):d} ms"
    if seconds < 60.0:
        return f"{round(seconds):d} sec"
    minutes = int(seconds // 60)
    remainder = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {remainder}s" if remainder else f"{minutes} min"
    hours = int(minutes // 60)
    return f"{hours}h {minutes % 60}m"


def _fresh_dac_delay_ms(dac: Mapping[str, Any]) -> float | None:
    delay = _finite_number(dac.get("snd_pcm_delay_ms"))
    age = _finite_number(dac.get("snd_pcm_delay_sample_age_ms"))
    if (
        delay is None
        or age is None
        or float(delay) < 0.0
        or float(age) < 0.0
        or float(age) > OUTPUTD_STALE_MS
    ):
        return None
    return float(delay)


def _incident_context(
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    active_source: str | None,
) -> dict[str, Any]:
    """Capture only the evidence rendered on a persisted incident."""
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    source_input = (
        _mapping(_mapping(fanin.get("inputs")).get(active_source))
        if active_source is not None else {}
    )
    output = _mapping(_mapping(outputd).get("dac"))
    return {
        "clock_mode": _mapping(fanin.get("host_clock")).get("ladder"),
        "input": {"rms_dbfs": source_input.get("rms_dbfs")},
        "output": {"snd_pcm_delay_ms": _fresh_dac_delay_ms(output)},
    }


def _receiver_latency(
    active_source: str,
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    route: Mapping[str, Any],
    timing: Mapping[str, Any],
) -> dict[str, Any]:
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    output = _mapping(fanin.get("output"))
    source_input = _mapping(_mapping(fanin.get("inputs")).get(active_source))
    resampler = _mapping(source_input.get("resampler"))
    camilla = _mapping(current.get("camilla"))
    dac = _mapping(_mapping(outputd).get("dac"))
    rate = (
        _as_int(output.get("sample_rate"))
        or _as_int(route.get("fixed_sample_rate"))
        or _as_int(dac.get("sample_rate"))
    )
    components: list[tuple[str, float]] = []
    if rate > 0 and active_source == Source.USBSINK.value:
        fill = _finite_number(resampler.get("fill_frames"))
        if fill is not None and float(fill) >= 0.0:
            components.append(("USB input queue", float(fill) * 1000.0 / rate))
    fanin_delay = _finite_number(output.get("snd_pcm_delay_ms"))
    if fanin_delay is not None and float(fanin_delay) >= 0.0:
        components.append(("Mixing queue", float(fanin_delay)))
    capture_rate = _as_int(camilla.get("capture_rate")) or rate
    camilla_frames = _finite_number(camilla.get("buffer_level"))
    if (
        capture_rate > 0
        and camilla_frames is not None
        and float(camilla_frames) >= 0.0
    ):
        components.append((
            "DSP queue",
            float(camilla_frames) * 1000.0 / capture_rate,
        ))
    dac_delay = _fresh_dac_delay_ms(dac)
    if dac_delay is not None:
        components.append(("DAC presentation queue", float(dac_delay)))

    runtime = _mapping(timing.get("runtime"))
    phase = str(runtime.get("phase") or "")
    raw_mode = str(runtime.get("raw_mode") or "")
    preset = str(runtime.get("preset") or "")
    if phase == "fallback":
        mode_label = "stable fallback"
    elif phase == "checking":
        mode_label = "timing check in progress"
    elif phase == "clock_adjusting":
        mode_label = "clock adjusting"
    elif phase == "buffer_adjusting":
        mode_label = "latency adjusting"
    elif phase == "stable":
        label = PRESETS[preset].label.lower() if preset in PRESETS else "low"
        mode_label = f"{label} latency stable"
    else:
        mode_label = None
    details = [
        _detail(label, f"{value:.1f} ms")
        for label, value in components
    ]
    estimate: dict[str, float] | None = None
    if components:
        total = sum(value for _label, value in components)
        lower = int(max(0.0, total) * 10.0) / 10.0
        estimate = {"lower_ms": lower}
        summary = f"{lower:g} ms"
    else:
        summary = "Live queue timing unavailable"
    if active_source == Source.USBSINK.value and mode_label:
        summary = f"{summary} · {mode_label}"
    return {
        "summary": summary,
        "detail": "",
        "details": details,
        "estimate": estimate,
        "mode": raw_mode or None,
    }


def _current_stream(
    *,
    active_source: str | None,
    airplay: Mapping[str, Any],
    outputd: Mapping[str, Any] | None,
    route: Mapping[str, Any],
    timing: Mapping[str, Any],
    sampled_at: float,
    session: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if active_source is None:
        return None
    current = _mapping(airplay.get("current"))
    fanin = _mapping(current.get("fanin"))
    source_input = _mapping(_mapping(fanin.get("inputs")).get(active_source))
    resampler = _mapping(source_input.get("resampler"))
    camilla = _mapping(current.get("camilla"))
    dac = _mapping(_mapping(outputd).get("dac"))
    session_state = _mapping(session)
    session_start = session_state.get("started_at") or sampled_at
    stream: dict[str, Any] = {
        "source_id": active_source,
        "label": _SOURCE_LABELS.get(active_source, active_source),
        "started_at": session_start,
    }
    if resampler or camilla:
        stream["processing"] = {
            "summary": (
                "Adaptive resampling · shared DSP"
                if resampler else "Shared DSP path"
            ),
            "detail": "Configured processing route for this stream.",
            "details": [
                _detail("DSP rate", f"{_as_int(camilla.get('capture_rate')):,} Hz")
            ] if _as_int(camilla.get("capture_rate")) else [],
        }
    if session_state:
        stream["session"] = dict(session_state)
    if active_source == Source.USBSINK.value:
        stream["latency"] = _receiver_latency(
            active_source,
            airplay,
            outputd,
            route,
            timing,
        )
    elif active_source == Source.AIRPLAY.value:
        airplay_timing = _airplay_timing(airplay, active=True)
        stream["latency"] = {
            "summary": airplay_timing["headline"],
            "detail": airplay_timing["detail"],
            "details": [],
        }
    if active_source == Source.USBSINK.value:
        rate = _as_int(route.get("fixed_sample_rate"))
        if rate:
            stream["media"] = {
                "summary": f"{rate / 1000:g} kHz · Stereo PCM",
                "detail": "The format advertised by JTS to the connected USB host.",
                "details": [],
            }
    output_rate = _as_int(dac.get("sample_rate"))
    output_details: list[dict[str, str]] = []
    dac_delay = _fresh_dac_delay_ms(dac)
    if dac_delay is not None:
        output_details.append(_detail(
            "DAC queue",
            f"{dac_delay:.1f} ms",
        ))
    if outputd is not None and _mapping(outputd).get("backend") == "alsa" and dac:
        stream["output"] = {
            "summary": (
                f"{output_rate / 1000:g} kHz final output"
                if output_rate else "Final output reporting"
            ),
            "detail": "Post-DSP audio at the physical output stage.",
            "details": output_details,
        }
    rms = _finite_number(source_input.get("rms_dbfs"))
    if rms is not None:
        stream["signal"] = {
            "summary": f"{float(rms):.1f} dBFS recent signal level",
            "detail": "The most recent level measured on the source that is playing.",
            "details": [],
        }
    return stream


def _incident_impact(issue: Mapping[str, Any]) -> str:
    return {
        "continuity": "Audio may have briefly interrupted.",
        "latency": "Audio continued with higher latency.",
        "sync": "Playback may have briefly lost synchronization.",
        "quality": "Audio may have briefly distorted.",
        "availability": "This source may not be available.",
        "observability": "JTS could not confirm current audio health.",
    }.get(str(issue.get("impact")), "Audio quality may have been affected.")


def _likely_area(issue: Mapping[str, Any]) -> str:
    key = str(issue.get("key") or "")
    if key.startswith("path.outputd"):
        return "Final output stage"
    if key.startswith("path.fanin") or key.startswith("path.camilla"):
        return "Shared processing path"
    if key.startswith("airplay"):
        return "AirPlay transport and synchronization"
    if key.startswith("usbsink.latency") or key.startswith("usbsink.clock"):
        return "USB host timing"
    source_id = issue.get("source_id")
    if isinstance(source_id, str):
        return f"{_SOURCE_LABELS.get(source_id, source_id)} source"
    return "Audio monitoring"


def _incident_evidence(issue: Mapping[str, Any]) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    context = _mapping(_mapping(issue.get("context")).get("started"))
    if context.get("clock_mode"):
        evidence.append(_detail("Clock mode", context["clock_mode"]))
    input_context = _mapping(context.get("input"))
    if _finite_number(input_context.get("rms_dbfs")) is not None:
        evidence.append(_detail(
            "Input level",
            f"{float(input_context['rms_dbfs']):.1f} dBFS",
        ))
    output_context = _mapping(context.get("output"))
    if _finite_number(output_context.get("snd_pcm_delay_ms")) is not None:
        evidence.append(_detail(
            "DAC queue",
            f"{float(output_context['snd_pcm_delay_ms']):.1f} ms",
        ))
    return evidence


def _timestamp(value: Any, default: float) -> float:
    number = _finite_number(value)
    return float(number) if number is not None else default


def _incident_duration(issue: Mapping[str, Any], now: float) -> float:
    observed = _finite_number(issue.get("observed_seconds"))
    if observed is not None:
        return max(0.0, float(observed))
    started = _timestamp(issue.get("started_at"), now)
    end = _timestamp(issue.get("recovered_at"), now)
    return max(0.0, end - started)


def _present_incident(
    issue: Mapping[str, Any],
    now: float,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    started = _timestamp(issue.get("started_at"), now)
    duration = _incident_duration(issue, now)
    cutoff = now - 1800.0
    matching = [
        item for item in history
        if item.get("key") == issue.get("key")
        and _timestamp(
            item.get("last_occurrence_at")
            or item.get("last_seen_at")
            or item.get("started_at"),
            0.0,
        ) >= cutoff
    ]
    recurrence: dict[str, Any] | None = None
    if matching:
        # A coalesced record retains first/last occurrence plus total count,
        # not every timestamp. If it straddles the window boundary, only its
        # last occurrence is provably inside, so expose a lower bound.
        count = sum(
            max(1, _as_int(item.get("count"), 1))
            if _timestamp(
                item.get("first_occurrence_at") or item.get("started_at"),
                0.0,
            ) >= cutoff
            else 1
            for item in matching
        )
        known_firsts = []
        for item in matching:
            item_first = _timestamp(
                item.get("first_occurrence_at") or item.get("started_at"),
                now,
            )
            item_last = _timestamp(
                item.get("last_occurrence_at")
                or item.get("last_seen_at")
                or item.get("started_at"),
                now,
            )
            known_firsts.append(item_first if item_first >= cutoff else item_last)
        first_at = min(known_firsts)
        last_at = max(
            _timestamp(
                item.get("last_occurrence_at")
                or item.get("last_seen_at")
                or item.get("started_at"),
                now,
            )
            for item in matching
        )
        recurrence = {
            "count": count,
            "first_at": first_at,
            "last_at": last_at,
            "window_seconds": 1800.0,
            "count_is_lower_bound": True,
            "summary": (
                f"At least {count} occurrence"
                f"{'s' if count != 1 else ''} observed in 30 min"
            ),
        }
    key = str(issue.get("key") or "audio.issue")
    presented = {
        "id": f"{key}:{started:.3f}",
        "key": key,
        "status": issue.get("status"),
        "severity": issue.get("severity"),
        "title": issue.get("title"),
        "detail": issue.get("detail"),
        "source_id": issue.get("source_id"),
        "started_at": started,
        "last_seen_at": issue.get("last_seen_at"),
        "recovered_at": issue.get("recovered_at"),
        "count": max(1, _as_int(issue.get("count"), 1)),
        "impact": _incident_impact(issue),
        "observed": str(issue.get("detail") or "JTS observed an audio-path change."),
        "likely_area": _likely_area(issue),
        "evidence": _incident_evidence(issue),
    }
    if recurrence is not None and recurrence["count"] > 1:
        presented["recurrence"] = recurrence
    if issue.get("status") == "recovered" and duration > 0.0:
        presented["duration_seconds"] = round(duration, 1)
        presented["duration_label"] = _duration_label(duration)
    return presented


def _incident_priority(
    issue: Mapping[str, Any],
    active_source: str | None,
) -> tuple[int, int, float]:
    relevant = _incident_is_relevant(issue, active_source)
    return (
        1 if relevant else 0,
        1 if issue.get("severity") == "issue" else 0,
        _timestamp(issue.get("last_seen_at"), 0.0),
    )


def _incident_is_relevant(
    issue: Mapping[str, Any],
    active_source: str | None,
) -> bool:
    source_id = issue.get("source_id")
    return (
        issue.get("scope") in {"path", "monitor"}
        or source_id is None
        or (active_source is not None and source_id == active_source)
    )


def compose_audio_health(
    *,
    airplay: Mapping[str, Any] | None,
    outputd: Mapping[str, Any] | None,
    route: Mapping[str, Any] | None,
    issues: list[dict[str, Any]],
    sampled_at: float,
    previous_overall: Mapping[str, Any] | None = None,
    service_states: Mapping[str, Any] | None = None,
    source_intents: Mapping[str, bool] | None = None,
    session: Mapping[str, Any] | None = None,
    mux_status: Mapping[str, Any] | None = None,
    output_hardware: Any = None,
    output_topology_snapshot: Any = None,
    transport_park: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the public, presentation-ready audio-health contract.

    ``output_hardware`` is an
    :class:`~jasper.output_hardware.OutputHardwareState` or ``None``, and
    ``output_topology_snapshot`` is a
    :class:`~jasper.output_topology.OutputTopologySnapshot` or ``None``
    (before the sampler's first slow-cadence read) — deliberately the
    snapshot, not the bare topology; see :func:`_undeclared_hardware_signal`
    for why. Both typed loosely because this module imports those layers
    lazily (same convention as ``topology`` in :func:`_transport_state`).

    ``transport_park`` is ``jasper.control.transport_park.snapshot()`` (or
    ``None`` before the sampler's first slow-cadence read), passed in rather
    than read here for the same reason ``undeclared_hardware`` is computed by
    the caller: the incident rows and this headline must be the same tick's
    verdict, and the read is a file read that belongs on the slow cadence.
    """
    ap = _mapping(airplay)
    route_state = _mapping(route)
    mux = mux_status if mux_status is not None else _mapping(ap.get("mux_status"))
    active_source = _active_source(ap, mux)
    activity_unknown = _activity_truth_unknown(ap, mux)
    signal_path = _signal_path(ap, outputd, active_source)
    if activity_unknown and signal_path.get("status") not in {"issue", "unknown"}:
        signal_path = _activity_unavailable_signal()
    stopped_dsp = _stopped_dsp_signal(ap, service_states)
    if stopped_dsp is not None and signal_path.get("status") != "issue":
        # Ahead of `parked` on the same principle parked states below: a
        # daemon that is not running is happening NOW and is fixed by starting
        # it, while parked is persistent and fixed by changing the layout.
        # Same `!= "issue"` guard, so a concrete live fan-in / outputd failure
        # still wins — this only claims the ground where the path looks clean.
        signal_path = stopped_dsp
    parked = _parked_signal(route_state)
    if parked is not None and signal_path.get("status") != "issue":
        # A verified structural fault outranks ok / warn / idle / unknown: the
        # box cannot emit audio at all, and absence of evidence should not hide
        # that.  It does NOT outrank a concrete live failure — parked is
        # persistent and fixed by changing the layout, while a stalled daemon
        # is happening now and has its own remedy.
        signal_path = parked
    transport_parked = _transport_park_signal(transport_park)
    if transport_parked is not None and signal_path.get("status") != "issue":
        # Last of the three "the box cannot emit at all" detectors, and the
        # most structural: a live coherence contradiction or a stopped daemon
        # names something an operator can act on THIS boot, while a transport
        # park is cleared only by rebuilding the topology on the ring. Same
        # `!= "issue"` guard, so neither of those is displaced.
        signal_path = transport_parked
    undeclared_hardware = _undeclared_hardware_signal(
        output_hardware, output_topology_snapshot
    )
    if (
        undeclared_hardware is not None
        and signal_path.get("code") in _UNDECLARED_OUTPUT_CODES
    ):
        # Checked by code, not the `!= "issue"` guard `stopped_dsp`/
        # `parked` use above: `_signal_path`'s own outputd-absent/non-ALSA
        # branch is already "issue" status, so this refines its generic
        # wording rather than outranking a different concrete issue. Because
        # it runs last, it only claims the ground when neither `stopped_dsp`
        # nor `parked` already replaced signal_path with a more relevant,
        # differently-worded diagnosis (camilla down, transport
        # disconnected) — both of those keep priority.
        signal_path = undeclared_hardware
    current = _mapping(ap.get("current"))
    fanin = _mapping(current.get("fanin"))
    inputs = _mapping(fanin.get("inputs"))
    host_clock = _mapping(fanin.get("host_clock")) or None
    if active_source == Source.USBSINK.value:
        latency = _usb_timing(
            route_state,
            host_clock,
            _mapping(inputs.get(Source.USBSINK.value)),
            active=True,
        )
    else:
        latency = _not_applicable_timing()
    source_cards = _source_cards(
        ap,
        signal_path,
        route_state,
        active_source,
        service_states,
        source_intents,
    )
    unavailable_sources = [
        str(source.get("label") or source.get("id"))
        for source in source_cards
        if source.get("status") == "issue"
        and source.get("id") == active_source
    ]

    path_status = str(signal_path.get("status") or "unknown")
    if path_status in {"issue", "unknown"}:
        overall_status = path_status
        headline = str(signal_path.get("headline"))
        detail = str(signal_path.get("detail"))
    elif unavailable_sources:
        overall_status = "warn"
        headline = "A playback source needs attention"
        detail = f"Unavailable: {', '.join(unavailable_sources)}."
    elif path_status == "warn":
        overall_status = "warn"
        headline = "Audio is playing" if active_source else str(signal_path.get("headline"))
        detail = str(signal_path.get("headline") if active_source else signal_path.get("detail"))
    elif path_status == "idle":
        overall_status = "idle"
        headline = str(signal_path.get("headline"))
        detail = str(signal_path.get("detail"))
    elif active_source is None:
        overall_status = "idle"
        headline = "Audio is ready"
        detail = "No source is playing."
    elif latency.get("status") in {"warn", "unknown"}:
        overall_status = "warn"
        headline = "Audio is playing"
        detail = str(latency.get("headline"))
    else:
        overall_status = "ok"
        headline = "Audio is playing"
        detail = (
            f"{_SOURCE_LABELS.get(active_source, active_source)} · sound path healthy."
        )

    previous = _mapping(previous_overall)
    same_overall = (
        previous.get("status") == overall_status
        and previous.get("headline") == headline
        and previous.get("active_source") == active_source
    )
    since = previous.get("since") if same_overall else sampled_at
    overall = {
        "status": overall_status,
        "headline": headline,
        "detail": detail,
        "active_source": active_source,
        "since": since,
    }
    ongoing_issues = [
        issue for issue in issues
        if issue.get("status") == "ongoing"
        and _incident_is_relevant(issue, active_source)
    ]
    ongoing = max(
        ongoing_issues,
        key=lambda issue: _incident_priority(issue, active_source),
        default=None,
    )
    current_incident = (
        _present_incident(ongoing, sampled_at, issues)
        if ongoing is not None else None
    )
    secondary_ongoing = sorted(
        (issue for issue in ongoing_issues if issue is not ongoing),
        key=lambda issue: _incident_priority(issue, active_source),
        reverse=True,
    )
    recovered = [
        issue for issue in issues if issue.get("status") == "recovered"
    ]
    recent_incidents = [
        _present_incident(issue, sampled_at, issues)
        for issue in (*secondary_ongoing, *recovered)
    ][:5]
    current_stream = _current_stream(
        active_source=active_source,
        airplay=ap,
        outputd=outputd,
        route=route_state,
        timing=latency,
        sampled_at=sampled_at,
        session=session,
    )
    if activity_unknown:
        selected = _selected_source(ap)
        current_stream = {
            "source_id": selected,
            "label": _SOURCE_LABELS.get(selected or "", "Audio activity"),
            "signal": {
                "summary": "Playback state unavailable",
                "detail": "Waiting for a fresh reading of what is playing.",
                "details": [],
            },
        }
        if session is not None:
            current_stream["session"] = dict(session)
    return {
        "schema_version": SCHEMA_VERSION,
        "sampled_at": sampled_at,
        "overall": overall,
        "signal_path": signal_path,
        "latency": latency,
        "sources": source_cards,
        "issues": copy.deepcopy(issues),
        "current_stream": current_stream,
        "current_incident": current_incident,
        "recent_incidents": recent_incidents,
        "technical": {
            "sampler": {
                "last_sample_at": ap.get("last_sample_at"),
                "warmup_active": bool(ap.get("warmup_active")),
                "suppressed_reason": ap.get("suppressed_reason"),
            },
            "fanin": {
                "available": bool(fanin.get("available")),
                "input_buffer_frames": fanin.get("input_buffer_frames"),
                "output_buffer_frames": fanin.get("output_buffer_frames"),
                "inputs": copy.deepcopy(fanin.get("inputs")),
                "host_clock": copy.deepcopy(fanin.get("host_clock")),
                "watchdog": copy.deepcopy(fanin.get("watchdog")),
            },
            "outputd": {
                "available": outputd is not None,
                "mix": copy.deepcopy(_mapping(outputd).get("mix")),
                "content": copy.deepcopy(_mapping(outputd).get("content")),
                "dac": copy.deepcopy(_mapping(outputd).get("dac")),
                "tts": copy.deepcopy(_mapping(outputd).get("tts")),
            },
            "route_verification": {
                "route_id": route_state.get("route_id"),
                "route_config_hash": route_state.get("route_config_hash"),
                **_verification(route_state),
            },
            "airplay": {
                "status": ap.get("status"),
                "reason": ap.get("reason"),
                "mpris": copy.deepcopy(current.get("mpris")),
                "camilla": copy.deepcopy(current.get("camilla")),
                "summary_5m": copy.deepcopy(ap.get("summary_5m")),
                "summary_30m": copy.deepcopy(ap.get("summary_30m")),
                "storm": copy.deepcopy(ap.get("storm")),
            },
        },
    }


class AudioHealthSampler:
    """The one production audio-health loop, with bounded in-memory history."""

    def __init__(
        self,
        *,
        sample_interval_sec: float = SAMPLE_INTERVAL_SEC,
        route_interval_sec: float = ROUTE_INTERVAL_SEC,
        airplay_sampler: AirPlayHealthSampler | Any | None = None,
        outputd_probe: Callable[[], dict[str, Any] | None] | None = None,
        mux_probe: Callable[[], dict[str, Any] | None] | None = None,
        route_probe: Callable[[], dict[str, Any]] | None = None,
        service_probe: Callable[[], dict[str, dict[str, Any]]] | None = None,
        output_hardware_probe: Callable[[], Any] | None = None,
        output_topology_probe: Callable[[], Any] | None = None,
        incident_store: IncidentStore | None = None,
        time_fn: Callable[[], float] = time.time,
        camilla_host: str = "127.0.0.1",
        camilla_port: int = 1234,
    ) -> None:
        self._sample_interval = sample_interval_sec
        self._route_interval = route_interval_sec
        self._time = time_fn
        self._airplay = airplay_sampler or AirPlayHealthSampler(
            sample_interval_sec=sample_interval_sec,
            camilla_host=camilla_host,
            camilla_port=camilla_port,
            time_fn=time_fn,
        )
        self._outputd_probe = outputd_probe or _read_local_status
        self._mux_probe = mux_probe or _read_mux_status
        self._route_probe = route_probe or read_route_claim
        self._service_probe = service_probe
        self._output_hardware_probe = output_hardware_probe or _read_output_hardware
        self._output_topology_probe = output_topology_probe or _read_output_topology
        observation_gap = max(15.0, sample_interval_sec * 3.0)
        self._issues = IssueTracker(
            store=incident_store,
            max_observation_gap_sec=observation_gap,
        )
        self._session = SessionRollup(
            max_observation_gap_sec=observation_gap,
        )
        self._outputd: dict[str, Any] | None = None
        self._route: dict[str, Any] | None = None
        # Declared topology changes only when a household explicitly saves a
        # new layout -- a rare event -- so it is refreshed on the same slow
        # `_route_interval` cadence as the route/transport check below, not
        # every fast tick, matching this module's "slow evidence stays on the
        # slow cadence" discipline (see the module docstring). A SNAPSHOT
        # (topology + revision), not a bare topology -- see
        # `_undeclared_hardware_signal`'s docstring for why revision matters.
        self._output_topology_snapshot: Any = None
        self._transport_park: dict[str, Any] | None = None
        self._service_states: dict[str, dict[str, Any]] = {}
        self._snapshot: dict[str, Any] | None = None
        self._last_route_sample_at = 0.0
        self._previous_input_xruns: dict[str, int] | None = None
        self._previous_usb_buffer_counts: tuple[int, int] | None = None
        self._previous_fanin_pings_skipped: int | None = None
        self._previous_outputd_xruns: dict[str, int] | None = None
        self._previous_outputd_clipped: int | None = None
        self._seen_raw_events: deque[tuple[Any, ...]] = deque(maxlen=40)
        self._seen_raw_event_set: set[tuple[Any, ...]] = set()
        self._lock = threading.Lock()
        self._stopped = False
        self._thread = threading.Thread(
            target=self._run,
            name="jasper-audio-health-sampler",
            daemon=True,
        )

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def stop(self) -> None:
        self._stopped = True

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            snapshot = copy.deepcopy(self._snapshot)
        if snapshot is None:
            return None
        sampled_at = snapshot.get("sampled_at")
        stale_after = max(15.0, self._sample_interval * 3.0)
        if (
            isinstance(sampled_at, (int, float))
            and self._time() - float(sampled_at) > stale_after
        ):
            stale_since = float(sampled_at) + stale_after
            snapshot["overall"] = {
                "status": "unknown",
                "headline": "Audio monitor is stale",
                "detail": "The last health sample is no longer current.",
                "active_source": _mapping(snapshot.get("overall")).get(
                    "active_source",
                ),
                "since": stale_since,
            }
            snapshot["signal_path"] = {
                "status": "unknown",
                "headline": "Audio health unavailable",
                "detail": "The monitor has not completed a fresh sample.",
            }
            stale_issue = {
                "key": "monitor.sample_stale",
                "scope": "monitor",
                "source_id": None,
                "impact": "observability",
                "severity": "issue",
                "title": "Audio monitor is stale",
                "detail": "Current audio health cannot be confirmed.",
                "status": "ongoing",
                "started_at": stale_since,
                "last_seen_at": self._time(),
                "recovered_at": None,
                "count": 1,
                "first_occurrence_at": stale_since,
                "last_occurrence_at": stale_since,
            }
            issues = list(snapshot.get("issues") or [])
            issues.insert(0, stale_issue)
            snapshot["issues"] = issues
            previous_stream = _mapping(snapshot.get("current_stream"))
            source_id = previous_stream.get("source_id") or _mapping(
                snapshot.get("overall")
            ).get("active_source")
            snapshot["current_stream"] = {
                "source_id": source_id,
                "label": previous_stream.get("label") or "Audio",
                "started_at": stale_since,
                "signal": {
                    "summary": "Current stream details unavailable",
                    "detail": "The audio monitor has not completed a fresh sample.",
                    "details": [],
                },
            }
            snapshot["current_incident"] = _present_incident(
                stale_issue,
                self._time(),
                issues,
            )
        return snapshot

    def airplay_snapshot(self) -> dict[str, Any]:
        """Compatibility surface for the existing ``airplay_health`` payload."""
        return self._airplay.snapshot()

    def outputd_snapshot(self) -> dict[str, Any] | None:
        """Reuse the cached outputd observation in ``/system/snapshot``."""
        with self._lock:
            return copy.deepcopy(self._outputd)

    def _run(self) -> None:
        while not self._stopped:
            started = time.monotonic()
            try:
                self._tick()
            except _MONITOR_ERRORS:
                logger.exception("audio health sampler tick failed")
            elapsed = time.monotonic() - started
            time.sleep(max(0.1, self._sample_interval - elapsed))

    def _tick(self) -> None:
        now = self._time()
        self._airplay.sample_once()
        airplay = self._airplay.snapshot()
        try:
            outputd = self._outputd_probe()
        except _MONITOR_ERRORS:
            logger.debug("audio health outputd probe failed", exc_info=True)
            outputd = None
        try:
            mux_status = self._mux_probe()
        except _MONITOR_ERRORS:
            logger.debug("audio health mux STATUS probe failed", exc_info=True)
            mux_status = None
        try:
            output_hardware = self._output_hardware_probe()
        except _MONITOR_ERRORS:
            logger.debug("audio health output-hardware probe failed", exc_info=True)
            output_hardware = None
        if mux_status is None and isinstance(airplay.get("mux_status"), Mapping):
            # Explicit fixture/injected observation seam; production AirPlay
            # snapshots do not carry mux state and therefore still fail closed.
            mux_status = dict(airplay["mux_status"])
        if self._service_probe is not None:
            try:
                service_states = self._service_probe()
            except _MONITOR_ERRORS:
                logger.debug("audio health service-state probe failed", exc_info=True)
            else:
                if isinstance(service_states, dict):
                    self._service_states = service_states
        if (
            self._route is None
            or now - self._last_route_sample_at >= self._route_interval
        ):
            try:
                route = self._route_probe()
            except _MONITOR_ERRORS:
                logger.debug("audio health route probe failed", exc_info=True)
                route = {"status": "unavailable", "low_latency_claim": False}
            self._route = route if isinstance(route, dict) else None
            try:
                self._output_topology_snapshot = self._output_topology_probe()
            except _MONITOR_ERRORS:
                logger.debug("audio health output-topology probe failed", exc_info=True)
                # Keep the previously cached snapshot: a transient read
                # failure on a box that already had a good read a moment ago
                # should not blank the declared side of the B1/B2 comparison.
            # ADR-0178's transport parks ride the SLOW cadence with the
            # topology read they classify: both change only when a reconciler
            # or a bond rewrites a file, and its own snapshot() is fail-soft,
            # so a bad read lands as status="unavailable" rather than raising.
            # Imported here, not at module scope, so the name cannot shadow
            # the `transport_park` PARAMETER the composers below take.
            from . import transport_park as transport_park_reader

            self._transport_park = transport_park_reader.snapshot()
            self._last_route_sample_at = now

        active_source = _active_source(airplay, mux_status)
        activity_unknown = _activity_truth_unknown(airplay, mux_status)
        selected_source = _selected_source(airplay)
        if activity_unknown:
            if (
                self._session.source_id is not None
                and selected_source != self._session.source_id
            ):
                self._session.reset(None, now)
        elif active_source != self._session.source_id:
            self._session.reset(active_source, now)
        context = _incident_context(airplay, outputd, active_source)
        try:
            intents = {
                source.value: enabled
                for source, enabled in read_source_intents().items()
            }
        except RuntimeError:
            logger.debug("audio health source-intent probe failed", exc_info=True)
            intents = None
        signal_path = _signal_path(airplay, outputd, active_source)
        if activity_unknown and signal_path.get("status") not in {"issue", "unknown"}:
            signal_path = _activity_unavailable_signal()
        current = _mapping(airplay.get("current"))
        fanin = _mapping(current.get("fanin"))
        inputs = _mapping(fanin.get("inputs"))
        host_clock = _mapping(fanin.get("host_clock")) or None
        if active_source == Source.USBSINK.value:
            latency = _usb_timing(
                _mapping(self._route),
                host_clock,
                _mapping(inputs.get(Source.USBSINK.value)),
                active=True,
            )
        else:
            latency = _not_applicable_timing()
        # Computed once here and passed to both _state_issues (below) and
        # compose_audio_health (which recomputes it from the same
        # output_hardware/output_topology_snapshot inputs, mirroring how
        # signal_path itself is deliberately computed raw here and
        # independently inside compose_audio_health): the two surfaces read
        # the same value, so they cannot present a different verdict for the
        # same tick (#2812 S3 — the raw path.outputd_unavailable incident
        # must not contradict the overall headline when the setup hint wins).
        undeclared_hardware = _undeclared_hardware_signal(
            output_hardware, self._output_topology_snapshot
        )
        state_issues = _state_issues(
            airplay,
            outputd,
            signal_path,
            latency,
            active_source,
            self._service_states,
            intents,
            activity_unknown=activity_unknown,
            undeclared_hardware=undeclared_hardware,
            transport_park=self._transport_park,
        )
        tracked_state_issues = [
            issue for issue in state_issues
            if not (
                issue.get("impact") == "availability"
                and issue.get("source_id") != active_source
            )
        ]
        with self._issues.batch(now):
            self._record_raw_events(
                airplay,
                active_source=active_source,
                now=now,
            )
            clipping_issue, preserve_clipping = self._record_counter_events(
                airplay,
                outputd,
                now,
                context=context,
            )
            if clipping_issue is not None:
                tracked_state_issues.append(clipping_issue)
            preserve_unseen_keys: set[str] = set()
            if preserve_clipping:
                preserve_unseen_keys.add("path.outputd_clipping")
            if (
                activity_unknown
                and self._session.source_id is not None
                and selected_source == self._session.source_id
            ):
                preserve_unseen_keys.update(
                    str(issue["key"])
                    for issue in self._issues.snapshot()
                    if issue.get("status") == "ongoing"
                    and issue.get("source_id") == self._session.source_id
                )
            self._issues.update(
                tracked_state_issues,
                now,
                context=context,
                preserve_unseen_keys=preserve_unseen_keys,
            )
        self._session.observe_state(
            tracked_state_issues,
            now,
            preserve_unseen_keys=preserve_unseen_keys,
        )
        with self._lock:
            previous_overall = (
                self._snapshot.get("overall")
                if isinstance(self._snapshot, dict)
                else None
            )
            self._outputd = copy.deepcopy(outputd)
            self._snapshot = compose_audio_health(
                airplay=airplay,
                outputd=outputd,
                route=self._route,
                issues=self._issues.snapshot(),
                sampled_at=now,
                previous_overall=previous_overall,
                service_states=self._service_states,
                source_intents=intents,
                session=self._session.snapshot(now),
                mux_status=mux_status,
                output_hardware=output_hardware,
                output_topology_snapshot=self._output_topology_snapshot,
                transport_park=self._transport_park,
            )

    def transport_park_snapshot(self) -> dict[str, Any]:
        """The transport-park verdict THIS sampler last computed.

        ``/state`` reads it from here rather than calling
        ``transport_park.snapshot()`` again: the household incident rows and
        the signal-path headline in the same payload were built from this
        cached value on the sampler's slow cadence, and a second, fresher read
        would let one response disagree with itself in time — the box parked
        in ``resilience`` and playing in ``audio_health``.

        Falls back to a fresh read only before the first slow tick, when there
        is no cached verdict to be consistent with yet.
        """
        from . import transport_park as transport_park_reader

        cached = self._transport_park
        if cached is not None:
            return cached
        return transport_park_reader.snapshot()

    def _record_point(
        self,
        candidate: dict[str, Any],
        when: float,
        *,
        count: int,
        context: Mapping[str, Any] | None,
        observed_at: float | None = None,
    ) -> None:
        self._issues.record_point(
            candidate,
            when,
            count=count,
            context=context,
            observed_at=observed_at,
        )
        self._session.record_point(candidate, when, count=count)

    def _record_raw_events(
        self,
        airplay: Mapping[str, Any],
        *,
        active_source: str | None,
        now: float,
    ) -> None:
        for raw in airplay.get("events") or []:
            if not isinstance(raw, Mapping):
                continue
            fingerprint = (
                raw.get("ts"), raw.get("type"), raw.get("count"), raw.get("detail")
            )
            if fingerprint in self._seen_raw_event_set:
                continue
            if len(self._seen_raw_events) == self._seen_raw_events.maxlen:
                oldest = self._seen_raw_events.popleft()
                self._seen_raw_event_set.discard(oldest)
            self._seen_raw_events.append(fingerprint)
            self._seen_raw_event_set.add(fingerprint)
            event_type = str(raw.get("type") or "")
            if event_type == "camilla_short_read":
                # Documented inaudible recovered partials are technical evidence,
                # not a household issue. A playback underrun is surfaced below.
                continue
            if (
                event_type == "fanin_airplay_xrun"
                and active_source != Source.AIRPLAY.value
            ):
                continue
            if event_type in {"fanin_output_xrun", "camilla_playback_underrun"}:
                candidate = _issue(
                    f"path.{event_type}",
                    scope="path",
                    impact="continuity",
                    severity="issue",
                    title=str(raw.get("title") or "Audio path recovered"),
                    detail=str(raw.get("detail") or "The shared path recovered."),
                )
            elif event_type.startswith("shairport_") or event_type == "fanin_airplay_xrun":
                impact = "sync" if event_type in {
                    "shairport_packet_drop",
                    "shairport_oos",
                    "shairport_sync_positive",
                    "shairport_sync_negative",
                    "shairport_offset_too_short",
                } else "continuity"
                candidate = _issue(
                    f"airplay.{event_type}",
                    scope="source",
                    source_id=Source.AIRPLAY.value,
                    impact=impact,
                    severity=(
                        "issue" if raw.get("severity") == "issue" else "warn"
                    ),
                    title=str(raw.get("title") or "AirPlay recovered"),
                    detail=str(raw.get("detail") or "AirPlay recovered."),
                )
            else:
                continue
            event_time = _finite_number(raw.get("ts"))
            when = float(event_time) if event_time is not None else now
            self._record_point(
                candidate,
                when,
                count=_as_int(raw.get("count"), 1),
                context=None,
                observed_at=now,
            )

    def _record_counter_events(
        self,
        airplay: Mapping[str, Any],
        outputd: Mapping[str, Any] | None,
        now: float,
        *,
        context: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, bool]:
        current = _mapping(airplay.get("current"))
        fanin = _mapping(current.get("fanin"))
        watchdog = _mapping(fanin.get("watchdog"))
        pings_skipped = _as_int(watchdog.get("pings_skipped"))
        if self._previous_fanin_pings_skipped is not None:
            skipped_delta = pings_skipped - self._previous_fanin_pings_skipped
            if skipped_delta > 0:
                self._record_point(
                    _issue(
                        "path.fanin_watchdog_recovered",
                        scope="path",
                        impact="continuity",
                        severity="issue",
                        title="Sound recovered after a brief pause",
                        # No number in the sentence: `skipped_delta` counts
                        # watchdog ticks missed, i.e. how LONG one stall
                        # lasted, not how many stalls there were. It rides the
                        # structured `count` field below, where it is read as
                        # what it is.
                        detail=(
                            "Sound stopped moving through the speaker briefly "
                            "and resumed."
                        ),
                    ),
                    now,
                    count=skipped_delta,
                    context=context,
                )
        self._previous_fanin_pings_skipped = pings_skipped
        inputs = _mapping(fanin.get("inputs"))
        input_counts = {
            source_id: _as_int(_mapping(observation).get("xrun_count"))
            for source_id, observation in inputs.items()
            if isinstance(source_id, str)
            and bool(_mapping(observation).get("present"))
        }
        if self._previous_input_xruns is not None:
            for source_id, count in input_counts.items():
                if (
                    source_id == Source.AIRPLAY.value
                    or source_id != self._session.source_id
                ):
                    continue  # AirPlay has its own events; idle lanes are noise.
                previous = self._previous_input_xruns.get(source_id, count)
                delta = count - previous
                if delta > 0:
                    self._record_point(
                        _issue(
                            f"{source_id}.input_xrun",
                            scope="source",
                            source_id=source_id,
                            impact="continuity",
                            severity="issue",
                            title=f"{_SOURCE_LABELS.get(source_id, source_id)} input recovered",
                            detail=f"The input recovered {delta} interruption(s).",
                        ),
                        now,
                        count=delta,
                        context=context,
                    )
        self._previous_input_xruns = input_counts

        usb_input = _mapping(inputs.get(Source.USBSINK.value))
        unlocks = _nonnegative_counter(
            _mapping(usb_input.get("resampler")).get("unlock_count")
        )
        stream_stops = _nonnegative_counter(
            _mapping(usb_input.get("direct")).get("stream_stops")
        )
        if unlocks is None or stream_stops is None:
            self._previous_usb_buffer_counts = None
        else:
            previous_usb = self._previous_usb_buffer_counts
            if previous_usb is not None:
                unlock_delta = unlocks - previous_usb[0]
                stop_delta = stream_stops - previous_usb[1]
                if (
                    unlock_delta > 0
                    and stop_delta >= 0
                    and self._session.source_id == Source.USBSINK.value
                ):
                    unexpected = max(0, unlock_delta - stop_delta)
                    if unexpected:
                        self._record_point(
                            _issue(
                                "usbsink.latency_buffer_underfill",
                                scope="source",
                                source_id=Source.USBSINK.value,
                                impact="continuity",
                                severity="issue",
                                title="USB input buffer ran dry",
                                detail=(
                                    "USB audio arrived too late for the selected "
                                    "buffer. JTS refilled it and resumed playback."
                                ),
                            ),
                            now,
                            count=unexpected,
                            context=context,
                        )
            self._previous_usb_buffer_counts = (unlocks, stream_stops)

        if outputd is None:
            self._previous_outputd_xruns = None
            self._previous_outputd_clipped = None
            return None, True
        outputd_map = _mapping(outputd)
        clipping_issue: dict[str, Any] | None = None
        clipped_samples = _nonnegative_counter(
            _mapping(outputd_map.get("mix")).get("clipped_samples"),
        )
        preserve_clipping = False
        if clipped_samples is None:
            self._previous_outputd_clipped = None
            preserve_clipping = True
        elif self._previous_outputd_clipped is None:
            self._previous_outputd_clipped = clipped_samples
            preserve_clipping = True
        else:
            clipped_delta = clipped_samples - self._previous_outputd_clipped
            if clipped_delta > 0:
                clipping_issue = _issue(
                    "path.outputd_clipping",
                    scope="path",
                    impact="quality",
                    severity="issue",
                    title="Audio clipping detected",
                    detail=(
                        f"JTS observed {clipped_delta} clipped sample(s) "
                        "in the latest output interval."
                    ),
                )
            elif clipped_delta < 0:
                # A daemon restart/reset establishes a new baseline; it does
                # not prove that an already-observed episode recovered.
                preserve_clipping = True
            self._previous_outputd_clipped = clipped_samples
        outputd_counts = {
            "content": _as_int(_mapping(outputd_map.get("content")).get("xrun_count")),
            "dac": _as_int(_mapping(outputd_map.get("dac")).get("xrun_count")),
        }
        if self._previous_outputd_xruns is not None:
            for stage, count in outputd_counts.items():
                previous = self._previous_outputd_xruns.get(stage, count)
                delta = count - previous
                if delta > 0:
                    title = (
                        "Sound to the speaker recovered"
                        if stage == "dac" else "Music path recovered"
                    )
                    self._record_point(
                        _issue(
                            f"path.outputd_{stage}_xrun",
                            scope="path",
                            impact="continuity",
                            severity="issue",
                            title=title,
                            detail=f"Sound was interrupted {delta} time(s) and resumed.",
                        ),
                        now,
                        count=delta,
                        context=context,
                    )
        self._previous_outputd_xruns = outputd_counts
        return clipping_issue, preserve_clipping
