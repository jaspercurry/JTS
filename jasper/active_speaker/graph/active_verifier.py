# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Evidence for roleful active-speaker CamillaDSP graphs."""

from __future__ import annotations

import logging
import math
from typing import Any, Collection, Mapping, Sequence

import yaml

from jasper.camilla_config_contract import DRIVER_DOMAIN_PAIR_TRIM_FILTER as _DRIVER_DOMAIN_PAIR_TRIM
from jasper.camilla_emit import mono_sum_sources
from jasper.json_fields import as_float
from jasper.log_event import log_event
from jasper.audio_measurement.null_walk import MAX_DSP_DELAY_US
from jasper.speaker_layout import (
    LOWEST_DRIVER_ROLE_BY_MAIN_MODE,
    SUB_CROSSOVER_HZ_HI,
    SUB_CROSSOVER_HZ_LO,
    SUB_CROSSOVER_ORDER,
    WAY_COUNT_BY_MAIN_MODE,
    cardioid_cabinet_channels,
    measurement_target_id,
)
from jasper.camilla_emit import CHANNEL_SELECT_MIXER as _channel_select_mixer_name
from .._common import issue as _issue
from ..camilla_yaml import BASELINE_HEADROOM_DB, BASELINE_LIMITER_CLIP_LIMIT_DB, STARTUP_LIMITER_CLIP_LIMIT_DB
from ..camilla_names import (
    STARTUP_MUTE_GAIN_DB,
    baseline_protection_name,
    bass_management_hp_name as _bass_management_hp_name,
    driver_baseline_gain_name as _baseline_gain_name,
    driver_baseline_limiter_name as _baseline_limiter_name,
    driver_delay_name as _driver_delay_name,
    driver_limiter_name,
    driver_linearization_peak_name as _linearization_peak_name,
    driver_linearization_shelf_name as _linearization_shelf_name,
    driver_linearization_taper_name as _linearization_taper_name,
    output_commission_mute_name as _commission_mute_name,
    protective_tweeter_hp_name,
    sub_baseline_gain_name as _sub_baseline_gain_name,
    sub_baseline_limiter_name as _sub_baseline_limiter_name,
    sub_lowpass_name as _sub_lowpass_name,
    sub_startup_limiter_name as _sub_startup_limiter_name,
)
from ..graph_evidence import filter_params as _filter_params, filter_type as _filter_type
from ..graph_safety import (
    TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ,
    GraphView,
    bass_extension_block_valid,
    bass_management_corner_matched,
    filter_param_matches,
    mains_highpass_present,
    mixer_output_proved as _mixer_output_proved,
    output_terminally_muted,
    pipeline_contains_chain,
    sub_audible_guard_present,
    sub_guard_present,
    truthy_bool as _truthy_bool,
    tweeter_guard_present,
    view_from_yaml_dict,
)
from ..output_contract import (
    ACTIVE_BASELINE_SOURCE,
    ACTIVE_DRIVER_DOMAIN_SOURCE,
    ACTIVE_PROGRAM_SOURCE,
    OutputAssignment,
    OutputContract,
    mains_lowest_driver_indexes as _mains_lowest_driver_indexes,
    subwoofer_output_indexes as _subwoofer_output_indexes,
)
from ..profile import ADJACENT_PAIRS_BY_WAY, SUPPORTED_LR_ORDERS
from ..rear_calibration import RearCalibrationError, compile_rear_stage, read_rear_calibration

logger = logging.getLogger(__name__)

# Both emitted baseline-shaped sources run every output live through a
# protective per-driver chain; they differ only in the pre-split prefix
# (program-domain headroom + preference EQ vs inter-speaker channel-select).
# Summed commissioning may derive a narrowly verified final mute tail from the
# primary baseline source; the driver-domain source never may.
_BASELINE_LIKE_SOURCES = (ACTIVE_BASELINE_SOURCE, ACTIVE_DRIVER_DOMAIN_SOURCE)

ACTIVE_SPLIT_MIXER_PREFIX = "split_active_"


# Float slack (dB) on the boost-vs-headroom proof. The emitter writes both
# numbers with 3-decimal formatting, so an exactly-absorbed boost can read a
# hair over its allowance after the YAML round-trip; this keeps a graph that
# is correct by construction from failing its own proof on the last digit.
_LINEARIZATION_BOOST_EPS_DB: float = 1e-3

#: The one NUMERIC refusal in this walk, named apart from the shape refusals
#: because two other seams key on it rather than re-deriving the condition. A
#: shape refusal says the graph is not the emitter's; this one says the graph IS
#: the emitter's and its arithmetic no longer holds — a different remedy
#: (re-emit, not re-commission).
LINEARIZATION_HEADROOM_UNPROVEN_CODE = "active_linearization_headroom_unproven"

#: Journal name for the same event — a grep contract, so a rename is visible as
#: one.
EVENT_LINEARIZATION_HEADROOM_UNPROVEN = (
    "active_speaker.linearization_headroom_unproven"
)


def _pipeline_mixer_names(payload: dict[str, Any]) -> list[str]:
    """The names of the ``Mixer`` pipeline steps in order (``Filter`` steps
    excluded).

    ``GraphView.pipeline_steps`` captures only ``Filter`` steps, so the
    channel-select / split mixer ORDER — which the driver-domain arm must prove —
    is read from the parsed payload here rather than from the shared view.
    """
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return []
    names: list[str] = []
    for step in pipeline:
        if not isinstance(step, dict) or step.get("type") != "Mixer":
            continue
        name = step.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def _channel_select_precedes_split(mixer_names: list[str]) -> bool:
    """True iff a ``channel_select`` Mixer step runs strictly before the
    ``split_active_*`` Mixer step — the inter-speaker pick before the
    intra-speaker driver split. Fails closed (missing either -> ``False``)."""
    if _channel_select_mixer_name not in mixer_names:
        return False
    select_idx = mixer_names.index(_channel_select_mixer_name)
    split_idxs = [
        i for i, name in enumerate(mixer_names)
        if name.startswith(ACTIVE_SPLIT_MIXER_PREFIX)
    ]
    return bool(split_idxs) and select_idx < min(split_idxs)


def _program_domain_filter_step_names(view: GraphView) -> tuple[str, ...]:
    """Filter names wired to the stereo program bus ``[0, 1]``.

    A driver-domain follower has no program-domain Filter step at all: it mixes
    channel_select -> optional pair trim -> split_active, then filters physical
    driver outputs. So a Filter step on exactly channels [0, 1] is Layer B/C
    leaking onto the follower, except for the dedicated pair-balance trim.
    """
    names: list[str] = []
    for step in view.pipeline_steps:
        if step.channels == frozenset({0, 1}):
            names.extend(
                name for name in step.names if name != _DRIVER_DOMAIN_PAIR_TRIM
            )
    return tuple(names)


def _room_peq_filter_names(view: GraphView) -> tuple[str, ...]:
    return tuple(sorted(name for name in view.filters if name.startswith("room_peq")))


def _driver_domain_pair_trim_between_select_and_split(
    payload: dict[str, Any],
) -> bool:
    """Prove ``channel_select -> pair_balance_trim -> split_active_*`` order.

    ``GraphView`` intentionally stores only Filter steps, so this raw-pipeline
    check owns the mixed Mixer/Filter ordering proof for the optional pair trim.
    """
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return False
    select_idx: int | None = None
    trim_idx: int | None = None
    split_idxs: list[int] = []
    for idx, raw_step in enumerate(pipeline):
        step = raw_step if isinstance(raw_step, dict) else {}
        step_type = step.get("type")
        if step_type == "Mixer":
            name = step.get("name")
            if name == _channel_select_mixer_name and select_idx is None:
                select_idx = idx
            if isinstance(name, str) and name.startswith(ACTIVE_SPLIT_MIXER_PREFIX):
                split_idxs.append(idx)
            continue
        if step_type != "Filter":
            continue
        names = step.get("names")
        if not isinstance(names, list) or _DRIVER_DOMAIN_PAIR_TRIM not in names:
            continue
        if trim_idx is not None:
            return False
        trim_idx = idx
    if select_idx is None or trim_idx is None or not split_idxs:
        return False
    return select_idx < trim_idx < min(split_idxs)


def _filter_step_channels(step: dict[str, Any]) -> set[int] | None:
    raw_channels = step.get("channels")
    if not isinstance(raw_channels, list) or any(
        isinstance(value, bool) for value in raw_channels
    ):
        return None
    try:
        return {int(value) for value in raw_channels}
    except (TypeError, ValueError):
        return None


def _exact_filter_step_channels(
    step: dict[str, Any], expected: set[int]
) -> bool:
    raw_channels = step.get("channels")
    return (
        isinstance(raw_channels, list)
        and len(raw_channels) == len(expected)
        and all(type(value) is int for value in raw_channels)
        and set(raw_channels) == expected
    )


def _strict_finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _post_split_filter_names(
    payload: dict[str, Any],
    *,
    channel: int,
) -> tuple[str, ...]:
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return ()
    split_seen = False
    out: list[str] = []
    for raw_step in pipeline:
        step = raw_step if isinstance(raw_step, dict) else {}
        if step.get("type") == "Mixer":
            name = step.get("name")
            if isinstance(name, str) and name.startswith(ACTIVE_SPLIT_MIXER_PREFIX):
                split_seen = True
            continue
        if not split_seen or step.get("type") != "Filter":
            continue
        step_channels = _filter_step_channels(step)
        if step_channels is None or channel not in step_channels:
            continue
        raw_names = step.get("names")
        if not isinstance(raw_names, list):
            continue
        out.extend(
            name if isinstance(name, str) else "<invalid-filter-name>"
            for name in raw_names
        )
    return tuple(out)


def _rear_cabinet_channels(contract: OutputContract) -> tuple[int, int, int] | None:
    """``(front woofer, rear woofer, tweeter)`` of the one mono cabinet a rear
    calibration document describes, re-derived from the SAVED topology rather
    than from the emitter's preset."""
    roleful = [
        item for item in contract.roleful_assignments
        if item.physical_output_index is not None
    ]
    if len(roleful) != 3:
        return None
    return cardioid_cabinet_channels(
        (item.role, item.output_variant, int(item.physical_output_index))
        for item in roleful
    )


def _rear_stage_evidence(
    payload: dict[str, Any],
    *,
    contract: OutputContract,
    document: Mapping[str, Any] | None,
) -> tuple[dict[int, int], tuple[str, ...], str | None]:
    """``(stage names to skip per channel, the stage's mixer names, unproven code)``.

    The stage is proved by RECOMPILING it from the saved document and requiring
    the graph's leading post-split fragment to EQUAL the result: filters by name
    AND value, both branch mixers, the step order, and each step's channels. A
    name pattern proves nothing — the channel a step is pointed at, a filter's
    parameters and the branch routing are all outside it.

    No document means no fragment is tolerated: ``rear_out*`` names are then as
    unrecognised as any other, exactly as before the stage existed.
    """
    if not document:
        return {}, (), None
    channels = _rear_cabinet_channels(contract)
    if channels is None:
        return {}, (), "saved_topology_not_cardioid"
    required = _required_roleful_indexes(contract)
    devices = payload.get("devices")
    samplerate = devices.get("samplerate") if isinstance(devices, Mapping) else None
    if isinstance(samplerate, bool) or not isinstance(samplerate, int):
        return {}, (), "graph_sample_rate_unreadable"
    front_channel, rear_channel, tweeter_channel = channels
    try:
        stage = compile_rear_stage(
            read_rear_calibration(document, sample_rate=samplerate),
            front_channel=front_channel,
            rear_channel=rear_channel,
            tweeter_channel=tweeter_channel,
            channel_count=max(required) + 1,
        )
    except (RearCalibrationError, TypeError, ValueError):
        return {}, (), "saved_calibration_does_not_compile"
    for section in ("filters", "mixers"):
        defined = payload.get(section)
        if not isinstance(defined, Mapping) or any(
            defined.get(name) != definition for name, definition in stage[section].items()
        ):
            return {}, (), f"graph_{section}_are_not_the_stage"
    pipeline = payload.get("pipeline")
    fragment = stage["pipeline"]
    start = next(
        (
            index + 1
            for index, step in enumerate(pipeline)
            if isinstance(step, Mapping) and step.get("type") == "Mixer"
            and str(step.get("name") or "").startswith(ACTIVE_SPLIT_MIXER_PREFIX)
        ),
        None,
    ) if isinstance(pipeline, list) else None
    if start is None or pipeline[start : start + len(fragment)] != fragment:
        return {}, (), "leading_fragment_is_not_the_stage"
    lead: dict[int, int] = {}
    for step in fragment:
        if step["type"] != "Filter":
            continue
        for channel in step["channels"]:
            lead[channel] = lead.get(channel, 0) + len(step["names"])
    return (
        lead,
        tuple(step["name"] for step in fragment if step["type"] == "Mixer"),
        None,
    )


def _pipeline_names_for_channels(
    payload: dict[str, Any],
    *,
    channels: set[int],
) -> tuple[str, ...]:
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return ()
    out: list[str] = []
    for raw_step in pipeline:
        step = raw_step if isinstance(raw_step, dict) else {}
        if step.get("type") != "Filter":
            continue
        step_channels = _filter_step_channels(step)
        if step_channels is None:
            continue
        # A Camilla filter step may intentionally apply one role's baseline
        # chain to multiple outputs at once, for example both stereo woofers.
        # For per-output evidence we only need to prove that the requested
        # output is covered by the chain.
        if not channels.issubset(step_channels):
            continue
        out.extend(str(name) for name in step.get("names", []) if name is not None)
    return tuple(out)


def _unsafe_post_split_gains(payload: dict[str, Any]) -> tuple[str, ...]:
    """Gain filters after the active split must remain non-positive.

    Program-domain preference EQ can legitimately boost before the split because
    every driver limiter remains downstream. After the split, an added positive
    Gain could sit behind that limiter and defeat the active-output ceiling.
    """

    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return ()
    split_seen = False
    unsafe: set[str] = set()
    for raw_step in pipeline:
        step = raw_step if isinstance(raw_step, dict) else {}
        if step.get("type") == "Mixer":
            name = step.get("name")
            if isinstance(name, str) and name.startswith(ACTIVE_SPLIT_MIXER_PREFIX):
                split_seen = True
            continue
        if not split_seen or step.get("type") != "Filter":
            continue
        names = step.get("names")
        if not isinstance(names, list):
            continue
        for name in names:
            if not isinstance(name, str) or _filter_type(payload, name) != "Gain":
                continue
            gain = _strict_finite_number(_filter_params(payload, name).get("gain"))
            if gain is None or gain > 0.0:
                unsafe.add(name)
    return tuple(sorted(unsafe))


def _safe_commissioning_tail_filter(payload: dict[str, Any], name: str) -> bool:
    runtime_lane = name.startswith("as_commission_")
    output_mute = False
    suffix = "_rear_pending_mute" if name.endswith("_rear_pending_mute") else "_commission_mute"
    if name.startswith("as_out") and name.endswith(suffix):
        index_s = name.removeprefix("as_out").removesuffix(suffix)
        try:
            index = int(index_s)
        except ValueError:
            pass
        else:
            output_mute = name == f"as_out{index}{suffix}"
    if not runtime_lane and not output_mute:
        return False
    filter_type = _filter_type(payload, name)
    params = _filter_params(payload, name)
    if filter_type == "Delay":
        delay_ms = _strict_finite_number(params.get("delay"))
        return (
            params.get("unit") == "ms"
            and delay_ms is not None
            and 0.0 <= delay_ms <= MAX_DSP_DELAY_US / 1000.0
        )
    if filter_type == "Gain":
        gain = _strict_finite_number(params.get("gain"))
        return (
            gain is not None
            and gain <= 0.0
            and type(params.get("inverted")) is bool
            and type(params.get("mute")) is bool
        )
    return False


def _post_limiter_tail_evidence(
    payload: dict[str, Any],
    *,
    channel: int,
    limiter_name: str,
) -> tuple[int, tuple[str, ...]]:
    """Count the post-split limiter and reject transforms placed behind it."""

    names = _post_split_filter_names(payload, channel=channel)
    limiter_count = names.count(limiter_name)
    unsafe: set[str] = set()
    if limiter_count:
        start = names.index(limiter_name) + 1
        for name in names[start:]:
            if name != limiter_name and not _safe_commissioning_tail_filter(
                payload, name
            ):
                unsafe.add(name)
    return limiter_count, tuple(sorted(unsafe))


def _post_split_delay_evidence(
    payload: dict[str, Any],
    *,
    channel: int,
) -> tuple[float, tuple[str, ...]]:
    """Return cumulative physical delay and malformed lanes for one output."""

    total_ms = 0.0
    invalid: set[str] = set()
    for name in _post_split_filter_names(payload, channel=channel):
        if _filter_type(payload, name) != "Delay":
            continue
        params = _filter_params(payload, name)
        delay_ms = _strict_finite_number(params.get("delay"))
        if (
            params.get("unit") != "ms"
            or delay_ms is None
            or delay_ms < 0.0
        ):
            invalid.add(name)
            continue
        total_ms += delay_ms
    return total_ms, tuple(sorted(invalid))


def _crossover_directions(assignment: OutputAssignment) -> tuple[str, ...] | None:
    way_count = WAY_COUNT_BY_MAIN_MODE.get(assignment.speaker_mode)
    if way_count is None:
        return None
    if way_count == 1 and assignment.role == "full_range":
        return ()
    directions: list[str] = []
    for lower_role, upper_role in ADJACENT_PAIRS_BY_WAY[way_count]:
        if assignment.role == lower_role:
            directions.append("lowpass")
        if assignment.role == upper_role:
            directions.append("highpass")
    return tuple(directions) or None


def _crossover_filter_safe(
    payload: dict[str, Any],
    *,
    name: str,
    role: str,
    direction: str,
) -> bool:
    suffix = "lp" if direction == "lowpass" else "hp"
    params = _filter_params(payload, name)
    order = params.get("order")
    frequency = _strict_finite_number(params.get("freq"))
    minimum_frequency = (
        TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ
        if role == "tweeter" and direction == "highpass"
        else 0.0
    )
    return (
        name.startswith(f"as_{role}_")
        and name.endswith(f"_{suffix}")
        and _filter_type(payload, name) == "BiquadCombo"
        and params.get("type") == f"LinkwitzRiley{direction.title()}"
        and frequency is not None
        and frequency > 0.0
        and frequency >= minimum_frequency
        and not isinstance(order, bool)
        and isinstance(order, int)
        and order in SUPPORTED_LR_ORDERS
    )


def _bass_management_filter_safe(
    payload: dict[str, Any],
    *,
    name: str,
    direction: str,
) -> bool:
    params = _filter_params(payload, name)
    return (
        _filter_type(payload, name) == "BiquadCombo"
        and params.get("type") == f"LinkwitzRiley{direction.title()}"
        and SUB_CROSSOVER_HZ_LO
        <= (_strict_finite_number(params.get("freq")) or 0.0)
        <= SUB_CROSSOVER_HZ_HI
        and params.get("order") == SUB_CROSSOVER_ORDER
    )


def _baseline_gain_limiter_safe(
    payload: dict[str, Any],
    *,
    gain_name: str,
    limiter_name: str,
) -> bool:
    gain_params = _filter_params(payload, gain_name)
    gain = _strict_finite_number(gain_params.get("gain"))
    limiter_params = _filter_params(payload, limiter_name)
    clip_limit = _strict_finite_number(limiter_params.get("clip_limit"))
    return (
        _filter_type(payload, gain_name) == "Gain"
        and gain is not None
        and gain <= 0.0
        and type(gain_params.get("inverted")) is bool
        and gain_params.get("mute") is False
        and _filter_type(payload, limiter_name) == "Limiter"
        and clip_limit is not None
        and clip_limit <= 0.0
        and limiter_params.get("soft_clip") is True
    )


def _linearization_boost_allowance_db(payload: dict[str, Any]) -> float:
    """How much branch-chain peak THIS graph has already paid for.

    The magnitude of the program-domain ``active_baseline_headroom`` gain —
    the pre-split common attenuation the emitter folds baseline headroom,
    room-correction boost and linearization boost into — MINUS the contributors
    that are not linearization's. A branch whose peak is no more than what is
    left cannot drive the chain past unity, so the CamillaDSP 0 dB ceiling holds
    by arithmetic rather than by a policy number written down twice.

    Attributing the share matters: reading the whole magnitude let a tampered
    +5 dB linearization filter "spend" headroom already committed to the room
    PEQs, and prove safe while the two together could clip. Room-PEQ boost is
    recoverable from the graph, so it is subtracted exactly as the emitter added
    it.

    **Residual slack, stated rather than hidden**: ``output_trim_db`` and the
    cardioid stage's evaluated peak
    (``branch_chain.rear_branch_sum_headroom_db``) are folded into the same gain
    and are NOT recoverable, so with preference EQ or a cardioid stage present
    this allowance is generous by at most those terms — never tight. The emitter
    also adds a caller-supplied ``baseline_headroom_db`` while this subtracts
    the module default; they agree only because every production path takes the
    default 0.0, which a test pins.

    Returns 0.0 when the filter is absent or non-negative — the driver-domain
    (follower) graph, which has no program-domain headroom and therefore proves
    the original cut-only invariant: a follower has nothing to absorb a boost.
    """
    if _filter_type(payload, "active_baseline_headroom") != "Gain":
        return 0.0
    gain = _strict_finite_number(
        _filter_params(payload, "active_baseline_headroom").get("gain")
    )
    if gain is None or gain >= 0.0:
        return 0.0
    absorbed_db = -float(gain)
    filters = payload.get("filters")
    room_boost_db = 0.0
    if isinstance(filters, Mapping):
        for name in filters:
            if not isinstance(name, str) or not name.startswith("room_peq"):
                continue
            room_gain = _strict_finite_number(
                _filter_params(payload, name).get("gain")
            )
            if room_gain is not None and room_gain > 0.0:
                room_boost_db += float(room_gain)
    return max(0.0, absorbed_db - BASELINE_HEADROOM_DB - room_boost_db)


def _linearization_biquad(payload: dict[str, Any], name: str) -> dict[str, Any]:
    """One emitted linearization Biquad reduced to the plain
    ``{biquad_type, freq, q, gain}`` record
    :func:`jasper.active_speaker.branch_chain.chain_response` evaluates.

    Read straight off the graph text — this module never trusts a candidate's
    claim about what it emitted.
    """
    params = _filter_params(payload, name)
    return {
        "biquad_type": str(params.get("type") or ""),
        "freq": _strict_finite_number(params.get("freq")) or 0.0,
        "q": _strict_finite_number(params.get("q")) or 0.0,
        "gain": _strict_finite_number(params.get("gain")) or 0.0,
    }


def _linearization_chain_peak_db(
    payload: dict[str, Any],
    *,
    filters: Sequence[Mapping[str, Any]],
    crossovers: Sequence[tuple[str, str]],
    gain_name: str,
) -> tuple[float, float]:
    """The realized peak of this branch's emitted chain — ``(dB, Hz)``,
    re-derived from the graph, never from the candidate that produced it.

    ``crossover ⊗ linearization ⊗ trim``, through the same
    :func:`jasper.active_speaker.branch_chain.branch_chain_peak` the emitter
    charges ``active_baseline_headroom`` with, so a graph correct by
    construction cannot fail its own proof on a modelling difference. The
    frequency rides along so a refusal can NAME where the chain peaks.

    A trim that is absent or unreadable is treated as 0 dB, which over-states
    the peak — the safe direction for a proof.
    """
    from ..branch_chain import CrossoverSection, branch_chain_peak  # lazy: import cost (numpy)

    sections: list[CrossoverSection] = []
    for direction, name in crossovers:
        params = _filter_params(payload, name)
        freq = _strict_finite_number(params.get("freq"))
        order = params.get("order")
        if freq is None or isinstance(order, bool) or not isinstance(order, int):
            continue
        sections.append(
            CrossoverSection(
                fc_hz=float(freq), order=int(order), highpass=direction == "highpass",
            )
        )
    trim_db = _strict_finite_number(_filter_params(payload, gain_name).get("gain"))
    return branch_chain_peak(
        filters,
        sections=tuple(sections),
        trim_db=min(0.0, float(trim_db)) if trim_db is not None else 0.0,
    )


def _linearization_filter_safe(
    payload: dict[str, Any],
    *,
    name: str,
    biquad_types: tuple[str, ...],
) -> bool:
    """Check slot shape and finite gain; the composed branch owns headroom."""

    if _filter_type(payload, name) != "Biquad":
        return False
    params = _filter_params(payload, name)
    if str(params.get("type") or "") not in biquad_types:
        return False
    gain = _strict_finite_number(params.get("gain"))
    return gain is not None


def _consume_linearization_chain(
    chain: tuple[str, ...],
    cursor: int,
    payload: dict[str, Any],
    role: str,
    *,
    crossovers: Sequence[tuple[str, str]] = (),
    notes: list[dict[str, str]] | None = None,
) -> tuple[int, bool]:
    """Advance ``cursor`` past a well-formed, provably-safe Layer-1a
    linearization run for ``role``: an optional named leading shelf, then 0..N
    named peaking filters, then an optional trailing Highshelf taper, in the
    emitter's own naming convention.

    SELF-PROVING from the graph text alone — a linearization filter's full shape
    is recoverable from its own name and params, so unlike bass-extension no
    external evidence parameter needs threading through this module's public
    entry points or their callers.

    ``notes`` is an optional sink for the ONE refusal a caller cannot
    reconstruct from a bare ``False``: the headroom proof is a NUMERIC
    comparison, and its failure otherwise surfaced as the caller's shape
    refusal. An issue appended here carries the peak, the allowance and the
    FREQUENCY.

    Returns ``(new_cursor, ok)``; ``ok`` is False iff a recognized
    linearization-named filter proves UNSAFE (wrong Biquad subtype for its slot,
    or gain outside what the graph can carry). A name at ``cursor`` outside the
    convention is not an error: zero filters are consumed and the caller's tail
    check decides whether what remains is a legal chain.

    **Boost accounting.** Cuts are unconditionally safe. A boost is safe only if
    the graph attenuates the program by at least as much ahead of the split, so
    this walk EVALUATES the branch chain whose shape it just proved — the
    crossover BiquadCombos, the linearization biquads and the branch's own
    baseline Gain — against :func:`_linearization_boost_allowance_db`. The peak,
    not the sum of positive gains: emitter and prover must agree about one
    number, and charging the loose sum once cost a real profile 22.458 dB of
    program attenuation for a branch peaking at +4.00 dB. It newly permits a
    boost its own crossover fully removes, which is physically correct and is
    separately prevented from being GENERATED by the fit-band bound.
    """

    index = cursor
    allowance_db = _linearization_boost_allowance_db(payload)
    emitted: list[dict[str, Any]] = []

    shelf_name = _linearization_shelf_name(role)
    if index < len(chain) and chain[index] == shelf_name:
        if not _linearization_filter_safe(
            payload, name=shelf_name, biquad_types=("Highshelf", "Lowshelf"),
        ):
            return index, False
        emitted.append(_linearization_biquad(payload, shelf_name))
        index += 1
    peak_number = 1
    while index < len(chain):
        peak_name = _linearization_peak_name(role, peak_number)
        if chain[index] != peak_name:
            break
        if not _linearization_filter_safe(
            payload, name=peak_name, biquad_types=("Peaking",),
        ):
            return index, False
        emitted.append(_linearization_biquad(payload, peak_name))
        index += 1
        peak_number += 1
    taper_name = _linearization_taper_name(role)
    if index < len(chain) and chain[index] == taper_name:
        if not _linearization_filter_safe(
            payload, name=taper_name, biquad_types=("Highshelf",),
        ):
            return index, False
        emitted.append(_linearization_biquad(payload, taper_name))
        index += 1
    # A chain with no positive gain cannot exceed unity through a
    # Linkwitz-Riley section and a non-positive trim, so the ordinary cut-only
    # graph is proved without evaluating anything — and without this module
    # importing numpy, which it otherwise does not (see branch_chain).
    if not any(float(entry["gain"]) > 0.0 for entry in emitted):
        return index, True
    peak_db, peak_hz = _linearization_chain_peak_db(
        payload,
        filters=emitted,
        crossovers=crossovers,
        gain_name=_baseline_gain_name(role),
    )
    if peak_db > allowance_db + _LINEARIZATION_BOOST_EPS_DB:
        detail = (
            f"{role} linearization chain peaks {peak_db:.4f} dB at "
            f"{peak_hz:.1f} Hz, past the {allowance_db:.4f} dB this graph set "
            "aside for it ahead of the split; the chain's ORDER is correct and "
            "the headroom arithmetic is what failed"
        )
        log_event(
            logger,
            EVENT_LINEARIZATION_HEADROOM_UNPROVEN,
            level=logging.WARNING,
            role=role,
            peak_db=round(peak_db, 4),
            peak_hz=round(peak_hz, 1),
            allowance_db=round(allowance_db, 4),
        )
        if notes is not None:
            notes.append(_issue(
                "blocker", LINEARIZATION_HEADROOM_UNPROVEN_CODE, detail,
            ))
        return index, False
    return index, True


def _baseline_output_chain(
    payload: dict[str, Any],
    *,
    assignment: OutputAssignment,
    channel: int,
    bass_management_highpass: bool,
    rear_stage_lead: int = 0,
    notes: list[dict[str, str]] | None = None,
) -> tuple[tuple[str, str], ...] | None:
    """Prove the exact emitter-owned chain before the canonical limiter.

    ``notes`` is handed straight to :func:`_consume_linearization_chain`, the
    one refusal here that is arithmetic rather than shape — see its docstring.
    Every other ``None`` this returns genuinely means "not the emitter's
    chain", which the caller's own issue already says."""

    names = _post_split_filter_names(payload, channel=channel)[rear_stage_lead:]
    if assignment.role == "subwoofer":
        expected = (
            _sub_lowpass_name(),
            _sub_baseline_gain_name(),
            _sub_baseline_limiter_name(),
        )
        return (
            ()
            if (
                names[: len(expected)] == expected
                and _bass_management_filter_safe(
                    payload,
                    name=_sub_lowpass_name(),
                    direction="lowpass",
                )
                and _baseline_gain_limiter_safe(
                    payload,
                    gain_name=_sub_baseline_gain_name(),
                    limiter_name=_sub_baseline_limiter_name(),
                )
            )
            else None
        )

    limiter_name = _baseline_limiter_name(assignment.role)
    if names.count(limiter_name) != 1:
        return None
    limiter_index = names.index(limiter_name)
    chain = names[: limiter_index + 1]
    cursor = 0
    if bass_management_highpass:
        bass_name = _bass_management_hp_name(assignment.role)
        if (
            not chain
            or chain[0] != bass_name
            or not _bass_management_filter_safe(
                payload,
                name=bass_name,
                direction="highpass",
            )
        ):
            return None
        cursor += 1
    directions = _crossover_directions(assignment)
    if directions is None:
        return None
    crossovers: list[tuple[str, str]] = []
    for direction in directions:
        if cursor >= len(chain):
            return None
        name = chain[cursor]
        if not _crossover_filter_safe(
            payload,
            name=name,
            role=assignment.role,
            direction=direction,
        ):
            return None
        crossovers.append((direction, name))
        cursor += 1
    # Layer-1a driver linearization: immediately after the crossover HP/LP,
    # before bass-extension, and SELF-PROVING from the graph text alone (see
    # _consume_linearization_chain).
    cursor, linearization_ok = _consume_linearization_chain(
        chain, cursor, payload, assignment.role,
        crossovers=tuple(crossovers), notes=notes,
    )
    if not linearization_ok:
        return None
    protection_index = 0
    while cursor < len(chain):
        direction = next((
            direction for direction in ("highpass", "lowpass")
            if chain[cursor] == baseline_protection_name(
                assignment.role, protection_index, direction == "highpass",
            )
        ), None)
        if direction is None:
            break
        if not _crossover_filter_safe(
            payload, name=chain[cursor], role=assignment.role, direction=direction,
        ):
            return None
        cursor += 1
        protection_index += 1
    expected_tail = (
        _driver_delay_name(assignment.role),
        _baseline_gain_name(assignment.role),
        limiter_name,
    )
    delay_params = _filter_params(payload, expected_tail[0])
    delay_ms = _strict_finite_number(delay_params.get("delay"))
    if (
        chain[cursor:] != expected_tail
        or _filter_type(payload, expected_tail[0]) != "Delay"
        or delay_params.get("unit") != "ms"
        or delay_ms is None
        or not 0.0 <= delay_ms <= MAX_DSP_DELAY_US / 1000.0
        or not _baseline_gain_limiter_safe(
            payload,
            gain_name=expected_tail[1],
            limiter_name=limiter_name,
        )
    ):
        return None
    return tuple(crossovers)


def _commissioning_output_chain(
    payload: dict[str, Any],
    *,
    assignment: OutputAssignment,
    channel: int,
    bass_management_highpass: bool,
) -> tuple[tuple[str, str], ...] | None:
    """Prove one exact commissioning chain through its per-output mute."""

    names = _post_split_filter_names(payload, channel=channel)
    mute_name = _commission_mute_name(channel)
    mute_params = _filter_params(payload, mute_name)
    mute_gain = _strict_finite_number(mute_params.get("gain"))
    mute_safe = (
        _filter_type(payload, mute_name) == "Gain"
        and mute_gain is not None
        and mute_gain <= 0.0
        and type(mute_params.get("inverted")) is bool
        and type(mute_params.get("mute")) is bool
    )
    if assignment.role == "subwoofer":
        limiter_name = _sub_startup_limiter_name()
        expected = (_sub_lowpass_name(), limiter_name, mute_name)
        limiter = _filter_params(payload, limiter_name)
        clip_limit = _strict_finite_number(limiter.get("clip_limit"))
        return (
            ()
            if (
                names == expected
                and mute_safe
                and _bass_management_filter_safe(
                    payload,
                    name=_sub_lowpass_name(),
                    direction="lowpass",
                )
                and _filter_type(payload, limiter_name) == "Limiter"
                and clip_limit is not None
                and clip_limit <= 0.0
                and limiter.get("soft_clip") is True
            )
            else None
        )

    cursor = 0
    if bass_management_highpass:
        bass_name = _bass_management_hp_name(assignment.role)
        if (
            not names
            or names[0] != bass_name
            or not _bass_management_filter_safe(
                payload,
                name=bass_name,
                direction="highpass",
            )
        ):
            return None
        cursor += 1
    protective_name = protective_tweeter_hp_name(assignment.role)
    if cursor < len(names) and names[cursor] == protective_name:
        protective = _filter_params(payload, protective_name)
        protective_order = protective.get("order")
        if not (
            _filter_type(payload, protective_name) == "BiquadCombo"
            and protective.get("type") == "LinkwitzRileyHighpass"
            and (
                _strict_finite_number(protective.get("freq")) or 0.0
            ) >= TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ
            and not isinstance(protective_order, bool)
            and isinstance(protective_order, int)
            and protective_order in SUPPORTED_LR_ORDERS
        ):
            return None
        cursor += 1
    directions = _crossover_directions(assignment)
    if directions is None:
        return None
    crossovers: list[tuple[str, str]] = []
    for direction in directions:
        if cursor >= len(names):
            return None
        name = names[cursor]
        if not _crossover_filter_safe(
            payload,
            name=name,
            role=assignment.role,
            direction=direction,
        ):
            return None
        crossovers.append((direction, name))
        cursor += 1
    delay_name = _driver_delay_name(assignment.role)
    limiter_name = driver_limiter_name(assignment.role)
    expected_tail = (delay_name, limiter_name, mute_name) + (
        (f"as_out{channel}_rear_pending_mute",) if assignment.output_variant == "rear" else ()
    )
    delay = _filter_params(payload, delay_name)
    delay_ms = _strict_finite_number(delay.get("delay"))
    limiter = _filter_params(payload, limiter_name)
    clip_limit = _strict_finite_number(limiter.get("clip_limit"))
    if (
        names[cursor:] != expected_tail
        or not mute_safe
        or _filter_type(payload, delay_name) != "Delay"
        or delay.get("unit") != "ms"
        or delay_ms is None
        or not 0.0 <= delay_ms <= MAX_DSP_DELAY_US / 1000.0
        or _filter_type(payload, limiter_name) != "Limiter"
        or clip_limit is None
        or clip_limit > 0.0
        or limiter.get("soft_clip") is not True
    ):
        return None
    return tuple(crossovers)


def _excited_rear_protected(
    payload: dict[str, Any],
    contract: OutputContract,
    *,
    assignment: Any,
    index: int,
) -> bool:
    """Whether an EXCITED rear output is protected without its pending mute.

    A take that measures the rear drives it on its own program channel, so the
    terminal mute ADR-0316 otherwise requires would record silence instead of a
    sweep. What replaces the mute is proof that the signal reaching the rear
    passes exactly the chain its role's primary output passes — the same
    filters, in ONE grouped step over both outputs, carrying that role's
    limiter (either emitter's spelling). Never an authority for a household
    graph: the caller hands in the take's own excited targets, which no saved
    file can claim.
    """
    role_outputs = {
        item.physical_output_index for item in contract.assignments
        if item.role == assignment.role and item.physical_output_index is not None
    }
    chain = _post_split_filter_names(payload, channel=index)
    if chain and chain[-1] == _commission_mute_name(index):
        chain = chain[:-1]
    return (
        len(role_outputs) > 1
        and bool({driver_limiter_name(assignment.role),
                  _baseline_limiter_name(assignment.role)} & set(chain))
        and _canonical_chain_grouped(
            payload, expected_channels=role_outputs, expected_names=chain,
        )
    )


def _canonical_chain_grouped(
    payload: dict[str, Any],
    *,
    expected_channels: set[int],
    expected_names: tuple[str, ...],
) -> bool:
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return False
    split_seen = False
    matches = 0
    for raw_step in pipeline:
        step = raw_step if isinstance(raw_step, dict) else {}
        if step.get("type") == "Mixer":
            name = step.get("name")
            if isinstance(name, str) and name.startswith(ACTIVE_SPLIT_MIXER_PREFIX):
                split_seen = True
            continue
        if not split_seen or step.get("type") != "Filter":
            continue
        raw_names = step.get("names")
        if not isinstance(raw_names, list):
            continue
        names = tuple(name for name in raw_names if isinstance(name, str))
        if (
            _exact_filter_step_channels(step, expected_channels)
            and names == expected_names
        ):
            matches += 1
    return matches == 1


def _crossover_pair_matches(
    payload: dict[str, Any], lower_name: str, upper_name: str
) -> bool:
    lower = _filter_params(payload, lower_name)
    upper = _filter_params(payload, upper_name)
    return (
        _strict_finite_number(lower.get("freq"))
        == _strict_finite_number(upper.get("freq"))
        and lower.get("order") == upper.get("order")
    )


def _mismatched_crossover_pairs(
    payload: dict[str, Any],
    crossovers_by_role: dict[str, tuple[tuple[str, str], ...]],
    way_counts: set[int],
) -> tuple[tuple[str, str], ...]:
    mismatched: list[tuple[str, str]] = []
    for way_count in sorted(way_counts):
        for lower_role, upper_role in ADJACENT_PAIRS_BY_WAY.get(way_count, ()):
            lower_name = next(
                (
                    name
                    for direction, name in crossovers_by_role.get(lower_role, ())
                    if direction == "lowpass"
                ),
                None,
            )
            upper_name = next(
                (
                    name
                    for direction, name in crossovers_by_role.get(upper_role, ())
                    if direction == "highpass"
                ),
                None,
            )
            if (
                lower_name is None
                or upper_name is None
                or not _crossover_pair_matches(payload, lower_name, upper_name)
            ):
                mismatched.append((lower_role, upper_role))
    return tuple(mismatched)


def _driver_domain_pair_trim_safe(
    payload: dict[str, Any],
    view: GraphView,
) -> bool:
    """Optional pair-balance trim must be a non-positive Gain on the stereo bus."""
    present = (
        _DRIVER_DOMAIN_PAIR_TRIM in view.filters
        or any(_DRIVER_DOMAIN_PAIR_TRIM in step.names for step in view.pipeline_steps)
    )
    if not present:
        return True
    gain = as_float(_filter_params(payload, _DRIVER_DOMAIN_PAIR_TRIM).get("gain"))
    return (
        _filter_type(payload, _DRIVER_DOMAIN_PAIR_TRIM) == "Gain"
        and gain is not None
        and gain <= 0.0
        and pipeline_contains_chain(
            view,
            channels={0, 1},
            required_names=(_DRIVER_DOMAIN_PAIR_TRIM,),
        )
        and _driver_domain_pair_trim_between_select_and_split(payload)
    )


def _commission_mute_states(view: GraphView) -> dict[int, bool]:
    """Map each ``as_out{N}_commission_mute`` filter's output index to its
    ``mute`` boolean, read from the shared view's parsed filters.

    The ``as_out{N}_commission_mute`` name pattern is runtime_contract-specific
    (``graph_safety``'s predicates take a single ``mute_name``, never a pattern),
    so the scan stays here — but it now reads the already-parsed
    ``GraphView.filters`` instead of re-walking the raw config dict.
    """
    out: dict[int, bool] = {}
    for name, fdef in view.filters.items():
        if not name.startswith("as_out") or not name.endswith("_commission_mute"):
            continue
        index_s = name.removeprefix("as_out").removesuffix("_commission_mute")
        try:
            index = int(index_s)
        except ValueError:
            continue
        out[index] = bool(fdef.params.get("mute"))
    return out


def _baseline_commissioning_pair(
    contract: OutputContract,
    unmuted_outputs: set[int],
) -> tuple[str, tuple[str, str]] | None:
    """Infer one exact adjacent pair in one active speaker group."""

    if len(unmuted_outputs) != 2:
        return None
    by_output = _assignment_by_output(contract)
    assignments = [by_output.get(index) for index in sorted(unmuted_outputs)]
    if any(item is None for item in assignments):
        return None
    exact = [item for item in assignments if item is not None]
    group_ids = {item.speaker_group_id for item in exact}
    modes = {item.speaker_mode for item in exact}
    if len(group_ids) != 1 or len(modes) != 1:
        return None
    mode = next(iter(modes))
    way_count = WAY_COUNT_BY_MAIN_MODE.get(mode)
    if way_count not in {2, 3}:
        return None
    roles = {item.role for item in exact}
    pair = next(
        (
            candidate
            for candidate in ADJACENT_PAIRS_BY_WAY[way_count]
            if set(candidate) == roles
        ),
        None,
    )
    if pair is None:
        return None
    return next(iter(group_ids)), pair


def _baseline_commissioning_isolation_issues(
    payload: dict[str, Any],
    contract: OutputContract,
    *,
    graph_indexes: set[int],
    mutes: dict[int, bool],
    unmuted_outputs: set[int],
) -> tuple[list[dict[str, str]], tuple[str, tuple[str, str]] | None]:
    """Independently prove the runtime-owned final per-output mute tail."""

    issues: list[dict[str, str]] = []
    if set(mutes) != graph_indexes:
        issues.append(_issue(
            "blocker",
            "active_baseline_commissioning_mute_set_invalid",
            (
                "summed commissioning baseline must define exactly one mute "
                "filter for every graph output"
            ),
        ))
    filters = payload.get("filters")
    pipeline = payload.get("pipeline")
    expected_steps: list[dict[str, Any]] = []
    for index in sorted(graph_indexes):
        name = _commission_mute_name(index)
        is_audible = index in unmuted_outputs
        expected_filter = {
            "type": "Gain",
            "parameters": {
                "gain": 0.0 if is_audible else STARTUP_MUTE_GAIN_DB,
                "inverted": False,
                "mute": not is_audible,
            },
        }
        definition = filters.get(name) if isinstance(filters, dict) else None
        if definition != expected_filter:
            issues.append(_issue(
                "blocker",
                "active_baseline_commissioning_mute_invalid",
                (
                    "summed commissioning output mute is not the exact canonical "
                    f"state for DAC output {index + 1}"
                ),
            ))
        expected_steps.append(
            {"type": "Filter", "channels": [index], "names": [name]}
        )
        if not _canonical_chain_grouped(
            payload,
            expected_channels={index},
            expected_names=(name,),
        ):
            issues.append(_issue(
                "blocker",
                "active_baseline_commissioning_mute_step_invalid",
                (
                    "summed commissioning must wire one exact output mute step "
                    f"for DAC output {index + 1}"
                ),
            ))
    tail = (
        pipeline[-len(expected_steps):]
        if isinstance(pipeline, list) and expected_steps
        else []
    )
    if tail != expected_steps:
        issues.append(_issue(
            "blocker",
            "active_baseline_commissioning_mute_tail_invalid",
            (
                "summed commissioning output mutes must be the final ordered "
                "pipeline tail"
            ),
        ))
    pair = _baseline_commissioning_pair(contract, unmuted_outputs)
    if pair is None:
        issues.append(_issue(
            "blocker",
            "active_baseline_commissioning_target_invalid",
            (
                "summed commissioning may unmute exactly two adjacent roles "
                "within one active speaker group"
            ),
        ))
    return issues, pair


def _assignment_by_output(contract: OutputContract) -> dict[int, OutputAssignment]:
    out: dict[int, OutputAssignment] = {}
    for item in contract.assignments:
        if item.physical_output_index is not None and (
            item.roleful or LOWEST_DRIVER_ROLE_BY_MAIN_MODE.get(item.speaker_mode) == "full_range"
        ):
            out[item.physical_output_index] = item
    return out


def _required_roleful_indexes(contract: OutputContract) -> set[int]:
    return {
        int(item.physical_output_index)
        for item in contract.roleful_assignments
        if item.physical_output_index is not None
    }


def _active_graph_evidence(
    text: str,
    contract: OutputContract,
    summary: dict[str, Any],
    bass_profile_summary: Mapping[str, Any] | None,
    rear_calibration: Mapping[str, Any] | None = None,
    excited_target_ids: Collection[str] = (),
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    # Parse the text ONCE. `payload` gives the two distinct parse-error codes
    # callers branch on (which the shared view collapses to parsed_ok=False) and
    # backs the baseline path's raw-dict accessors; the normalised view is built
    # from that SAME dict via view_from_yaml_dict (list-only, like the candidate
    # dialect), so the text is never yaml.safe_load-ed twice.
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        issues.append(_issue(
            "blocker",
            "camilla_yaml_unparseable",
            f"could not parse CamillaDSP YAML: {type(exc).__name__}",
        ))
        return {"issues": issues, "safe": False}
    if not isinstance(payload, dict):
        issues.append(_issue(
            "blocker",
            "camilla_yaml_not_object",
            "CamillaDSP YAML did not parse to an object",
        ))
        return {"issues": issues, "safe": False}
    view = view_from_yaml_dict(payload)
    # A rear physical output plays only behind the calibration stage the SAVED
    # document compiles to (ADR-0318). Anything else — no document, a stage that
    # is not that one, every emitter but the baseline's — must stay silent.
    rear_lead, rear_stage_mixers, rear_unproven = _rear_stage_evidence(
        payload, contract=contract, document=rear_calibration,
    )
    for assignment in contract.assignments:
        if assignment.output_variant == "rear" and assignment.physical_output_index is not None:
            index = assignment.physical_output_index
            if index in rear_lead or output_terminally_muted(
                payload, view, index, mute_name=f"as_out{index}_rear_pending_mute",
                mute_gain_db=STARTUP_MUTE_GAIN_DB,
            ):
                continue
            # The third arm: a measurement take that names this rear as one of
            # its excited targets. It cannot be muted and has no document yet —
            # it is being measured to author one — so the role chain proves it.
            if measurement_target_id(assignment.role, assignment.output_variant) in excited_target_ids:
                if _excited_rear_protected(
                    payload, contract, assignment=assignment, index=index,
                ):
                    continue
                issues.append(_issue(
                    "blocker", "excited_rear_unprotected",
                    f"Rear output {index + 1} is excited without its role's "
                    "grouped protection chain and limiter",
                ))
                continue
            if rear_calibration:
                issues.append({**_issue(
                    "blocker", "rear_stage_unproven",
                    f"Rear output {index + 1} does not carry the saved rear "
                    "calibration stage",
                ), "detail": rear_unproven or ""})
            else:
                issues.append(_issue("blocker", "rear_output_not_muted", f"Rear output {index + 1} requires a fitted transfer and protection"))
    if payload.get("processors") or any(
        not isinstance(step, Mapping) or step.get("type") not in {"Filter", "Mixer"}
        for step in payload.get("pipeline", [])
    ):
        return {"issues": [_issue(
            "blocker", "active_graph_processor_unrecognized",
            "active graph contains an unproved processing step",
        )], "safe": False}

    by_output = _assignment_by_output(contract)
    required_indexes = set(by_output)
    required_count = max(required_indexes) + 1 if required_indexes else 0
    split = summary.get("active_split") if isinstance(summary.get("active_split"), dict) else {}
    split_channels = split.get("mixer_output_channels")
    if not required_indexes:
        issues.append(_issue(
            "blocker",
            "active_graph_without_roleful_topology",
            "active-speaker graph is loaded but saved topology has no roleful outputs",
        ))
    if split_channels != required_count:
        issues.append(_issue(
            "blocker",
            "active_graph_output_count_mismatch",
            (
                f"active graph exposes {split_channels or 'unknown'} output channels; "
                f"saved roleful topology requires {required_count}"
            ),
        ))
    unsafe_output_gains = _unsafe_post_split_gains(payload)
    if unsafe_output_gains:
        issues.append(_issue(
            "blocker",
            "active_output_gain_positive",
            (
                "active graph has a positive or malformed Gain after the driver "
                "split: " + ", ".join(unsafe_output_gains)
            ),
        ))

    mutes = _commission_mute_states(view)
    graph_indexes = (
        set(range(split_channels))
        if isinstance(split_channels, int) and split_channels >= 0
        else set(required_indexes)
    )
    missing_mutes = sorted(index for index in required_indexes if index not in mutes)
    if missing_mutes:
        source = str(summary.get("source") or "")
        if source not in _BASELINE_LIKE_SOURCES:
            issues.append(_issue(
                "blocker",
                "active_graph_missing_commission_mutes",
                "active graph is missing per-output mute filters for DAC outputs "
                + ", ".join(str(index + 1) for index in missing_mutes),
            ))
    weak_mutes = sorted(
        index for index in required_indexes
        if mutes.get(index) is True
        and not filter_param_matches(
            view,
            _commission_mute_name(index),
            filter_type="Gain",
            params={"gain": STARTUP_MUTE_GAIN_DB},
        )
    )
    if weak_mutes:
        issues.append(_issue(
            "blocker",
            "active_graph_commission_mute_not_hard_mute",
            "active graph mute filters are not at the expected hard-mute floor for DAC outputs "
            + ", ".join(str(index + 1) for index in weak_mutes),
        ))

    unwired_mutes = sorted(
        index for index in required_indexes
        if not pipeline_contains_chain(
            view,
            channels={index},
            required_names=(_commission_mute_name(index),),
        )
    )
    if unwired_mutes:
        source = str(summary.get("source") or "")
        if source not in _BASELINE_LIKE_SOURCES:
            issues.append(_issue(
                "blocker",
                "active_graph_unwired_commission_mutes",
                "active graph does not wire per-output mutes for DAC outputs "
                + ", ".join(str(index + 1) for index in unwired_mutes),
            ))

    source = str(summary.get("source") or "")
    if source == ACTIVE_PROGRAM_SOURCE:
        return {"issues": [_issue(
            "blocker", "active_graph_program_shape_unproven",
            "a measurement program graph has no proof arm in this door",
        )], "safe": False}
    is_baseline = source == ACTIVE_BASELINE_SOURCE
    is_driver_domain = source == ACTIVE_DRIVER_DOMAIN_SOURCE
    is_baseline_commissioning = is_baseline and bool(mutes)
    if is_driver_domain and mutes:
        issues.append(_issue(
            "blocker",
            "active_driver_domain_commission_mutes_present",
            "driver-domain baseline must not carry runtime commissioning mutes",
        ))
    # Both baseline-shaped graphs retain the same protective per-driver chain;
    # the primary baseline may additionally carry the exact runtime-owned
    # summed-isolation tail proved below. They otherwise differ only in the
    # pre-split prefix, branched inside the `is_baseline_like` block.
    is_baseline_like = is_baseline or is_driver_domain
    if is_baseline_like:
        if bass_profile_summary is None:
            issues.append(_issue(
                "blocker",
                "bass_extension_evidence_missing",
                "baseline-shaped graph requires explicit bass-extension profile evidence",
            ))
        else:
            bass_evidence = bass_extension_block_valid(view, bass_profile_summary)
            if not bass_evidence.valid:
                issues.append(_issue(
                    "blocker",
                    bass_evidence.reason or "bass_extension_block_invalid",
                    "baseline-shaped graph does not match its evaluated bass-extension profile",
                ))
    mixer_names = _pipeline_mixer_names(payload)
    active_way_counts = {
        way_count
        for item in contract.assignments
        if (way_count := WAY_COUNT_BY_MAIN_MODE.get(item.speaker_mode)) is not None
    }
    expected_split = (
        f"split_active_{next(iter(active_way_counts))}way"
        if len(active_way_counts) == 1
        else None
    )
    # A proven rear calibration stage adds exactly the branch mixers the
    # recompiled fragment wires, in its order; nothing else is post-split.
    expected_mixers = (
        (_channel_select_mixer_name, expected_split)
        if is_driver_domain and expected_split is not None
        else ((expected_split,) if expected_split is not None else ())
    ) + (rear_stage_mixers if expected_split is not None else ())
    if tuple(mixer_names) != expected_mixers:
        issues.append(_issue(
            "blocker",
            "active_graph_mixer_sequence_invalid",
            (
                "active graph must retain the exact emitter mixer sequence with "
                "one active split and no post-split mixer"
            ),
        ))
    for index, assignment in by_output.items():
        if assignment.roleful:
            continue
        sources = (
            mono_sum_sources() if contract.main_layout == "mono"
            else [(0 if assignment.speaker_kind == "left" else 1, 0.0, False)]
        )
        if (any(_truthy_bool(step.get("bypassed"))
                for step in payload.get("pipeline") or [] if isinstance(step, dict))
                or not _mixer_output_proved(payload, expected_split, index, sources)):
            issues.append(_issue(
                "blocker", "active_graph_output_routing_unproven",
                f"Passive main output {index + 1} does not preserve its program feed",
            ))
    unmuted_outputs = (
        set(graph_indexes)
        if is_baseline_like and not is_baseline_commissioning
        else {
            index for index in graph_indexes
            if index in mutes and mutes[index] is False
        }
    )
    muted_outputs = {
        index for index in required_indexes
        if index in mutes and mutes[index] is True
    }
    all_muted = bool(required_indexes) and muted_outputs == required_indexes
    baseline_commissioning_pair: tuple[str, tuple[str, str]] | None = None
    if is_baseline_commissioning:
        isolation_issues, baseline_commissioning_pair = (
            _baseline_commissioning_isolation_issues(
                payload,
                contract,
                graph_indexes=graph_indexes,
                mutes=mutes,
                unmuted_outputs=unmuted_outputs,
            )
        )
        issues.extend(isolation_issues)

    tweeter_outputs = {
        int(item.physical_output_index)
        for item in contract.protected_assignments
        if item.physical_output_index is not None and item.role == "tweeter"
    }
    if tweeter_outputs and not is_baseline_like:
        if not tweeter_guard_present(
            view,
            channels=tweeter_outputs,
            hp_name=protective_tweeter_hp_name("tweeter"),
            limiter_name=driver_limiter_name("tweeter"),
            limiter_clip_ceiling_db=STARTUP_LIMITER_CLIP_LIMIT_DB,
        ):
            issues.append(_issue(
                "blocker",
                "active_graph_tweeter_guard_missing",
                (
                    "active graph does not prove tweeter outputs are wrapped by "
                    "the protective high-pass and limiter"
                ),
            ))

    # All physical outputs the saved topology assigns (roleful drivers + sub +
    # full-range passive mains). A bass-managed passive main is a full_range
    # output — legitimately unmuted/routed but NOT roleful — so the unknown-output
    # guards below must treat it as known, not as an unexpected leak.
    known_indexes = {
        int(item.physical_output_index)
        for item in contract.assignments
        if item.physical_output_index is not None
    }
    sub_outputs = _subwoofer_output_indexes(contract)
    mains_low_outputs = _mains_lowest_driver_indexes(contract)
    unmuted_roles = {
        by_output[index].role
        for index in unmuted_outputs
        if index in by_output
    }
    unknown_unmuted = sorted(index for index in unmuted_outputs if index not in known_indexes)
    if unknown_unmuted:
        issues.append(_issue(
            "blocker",
            "active_graph_unmutes_unknown_outputs",
            "active graph unmutes outputs not assigned by the saved topology: "
            + ", ".join(str(index + 1) for index in unknown_unmuted),
        ))
    if len(unmuted_roles) > 1 and not is_baseline_like:
        issues.append(_issue(
            "blocker",
            "active_graph_unmutes_multiple_roles",
            "guarded commissioning may unmute only one driver role at a time",
        ))
    if unmuted_outputs & tweeter_outputs and any(
        issue["code"] == "active_graph_tweeter_guard_missing" for issue in issues
    ):
        issues.append(_issue(
            "blocker",
            "active_graph_unprotected_tweeter_audible",
            "active graph unmutes a tweeter output without proving software protection",
        ))

    # Local-subwoofer audible-protection guard (commissioning/startup): an
    # UNMUTED sub output MUST be band-limited (LR4 low-pass) and
    # excursion-limited, because a full-range feed to a powered sub is the
    # tampered-statefile hazard this re-proof exists to catch. The commissioning
    # sub lane has no gain filter, so only LP + limiter are provable here; the
    # baseline path proves the sub with its non-positive gain inside
    # is_baseline_like, and this is gated not-baseline-like so it never trips on
    # one.
    if not is_baseline_like:
        for index in sorted(unmuted_outputs & sub_outputs):
            if not sub_audible_guard_present(
                view,
                channels={index},
                lowpass_name=_sub_lowpass_name(),
                # The corner ceiling is load-bearing: a sub LOW-pass at a high
                # corner (e.g. 20 kHz) is full-range to a bass driver, so cap it
                # at the legal sub-crossover ceiling. The baseline class bounds
                # the corner via bass_management_corner_matched instead.
                lowpass_freq_ceiling_hz=SUB_CROSSOVER_HZ_HI,
                limiter_name=_sub_startup_limiter_name(),
                limiter_clip_ceiling_db=STARTUP_LIMITER_CLIP_LIMIT_DB,
            ):
                issues.append(_issue(
                    "blocker",
                    "active_graph_unprotected_sub_audible",
                    (
                        "active graph unmutes a subwoofer output without proving "
                        "the band-limit + excursion limiter on DAC output "
                        f"{index + 1}"
                    ),
                ))

    if not is_baseline_like:
        commissioning_crossovers: dict[str, tuple[tuple[str, str], ...]] = {}
        for index in sorted(required_indexes):
            assignment = by_output.get(index)
            if assignment is None:
                continue
            role = assignment.role
            crossovers = _commissioning_output_chain(
                payload,
                assignment=assignment,
                channel=index,
                bass_management_highpass=(
                    contract.subwoofer_present and index in mains_low_outputs
                ),
            )
            if crossovers is None:
                issues.append(_issue(
                    "blocker",
                    "active_commissioning_chain_unrecognized",
                    (
                        "active graph does not use the exact ordered commissioning "
                        f"chain through its mute on DAC output {index + 1} ({role})"
                    ),
                ))
                continue
            prior = commissioning_crossovers.setdefault(role, crossovers)
            if prior != crossovers:
                issues.append(_issue(
                    "blocker",
                    "active_commissioning_chain_unrecognized",
                    f"active graph uses inconsistent {role} commissioning chains",
                ))
            role_channels = {
                output for output, item in by_output.items() if item.role == role
            }
            post_split_names = _post_split_filter_names(payload, channel=index)
            role_chain_names = post_split_names[:-(2 if assignment.output_variant == "rear" else 1)]
            if index == min(role_channels) and not _canonical_chain_grouped(
                payload,
                expected_channels=role_channels,
                expected_names=role_chain_names,
            ):
                issues.append(_issue(
                    "blocker",
                    "active_commissioning_chain_not_grouped",
                    (
                        f"active graph must wire one exact grouped {role} "
                        "commissioning chain across its current outputs"
                    ),
                ))
            if not _canonical_chain_grouped(
                payload,
                expected_channels={index},
                expected_names=(_commission_mute_name(index),),
            ):
                issues.append(_issue(
                    "blocker",
                    "active_commissioning_mute_step_invalid",
                    (
                        "active graph must end each physical output with one exact "
                        f"commission mute step on DAC output {index + 1}"
                    ),
                ))
        for lower_role, upper_role in _mismatched_crossover_pairs(
            payload,
            commissioning_crossovers,
            active_way_counts,
        ):
            issues.append(_issue(
                "blocker",
                "active_commissioning_crossover_pair_mismatch",
                (
                    f"active graph {lower_role}/{upper_role} commissioning "
                    "crossovers must share one finite corner and LR order"
                ),
            ))

    if is_baseline_like:
        if is_baseline:
            # Program-domain prefix: the shared headroom gain rides channels
            # [0, 1] before the split and must be non-positive.
            if not pipeline_contains_chain(
                view,
                channels={0, 1},
                required_names=("active_baseline_headroom",),
            ):
                issues.append(_issue(
                    "blocker",
                    "active_baseline_headroom_unwired",
                    "active baseline graph does not wire the shared headroom filter",
                ))
            headroom = as_float(
                _filter_params(payload, "active_baseline_headroom").get("gain")
            )
            if headroom is None or headroom > 0.0:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_headroom_invalid",
                    "active baseline headroom gain is missing or positive",
                ))
        else:
            # Driver-domain (follower) prefix: the leader baked Layer B/C, so
            # this graph carries NO program-domain prefix. Prove the
            # inter-speaker channel-select runs strictly before the intra-speaker
            # split, and that no program-domain headroom gain leaked in (its
            # presence would mean an un-relocated Layer B/C). channel-select is a
            # Mixer step, read from the parsed pipeline order.
            if _channel_select_mixer_name not in mixer_names:
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_channel_select_missing",
                    "driver-domain graph does not wire the channel-select mixer",
                ))
            elif not _channel_select_precedes_split(mixer_names):
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_channel_select_after_split",
                    "driver-domain channel-select must run before the driver split",
                ))
            if "active_baseline_headroom" in view.filters:
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_program_prefix_present",
                    (
                        "driver-domain graph carries a program-domain headroom "
                        "filter (the leader owns Layer B/C, not the follower)"
                    ),
                ))
            room_peqs = _room_peq_filter_names(view)
            if room_peqs:
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_room_peq_present",
                    (
                        "driver-domain graph carries room-correction PEQ filters "
                        "(the leader owns Layer B, not the follower): "
                        + ", ".join(room_peqs)
                    ),
                ))
            program_step_names = _program_domain_filter_step_names(view)
            if program_step_names:
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_program_filter_step_present",
                    (
                        "driver-domain graph wires program-domain filters on "
                        "channels [0, 1] (the leader owns Layer B/C, not the "
                        "follower): "
                        + ", ".join(program_step_names)
                    ),
                ))
            if not _driver_domain_pair_trim_safe(payload, view):
                issues.append(_issue(
                    "blocker",
                    "active_driver_domain_pair_trim_invalid",
                    (
                        "driver-domain pair-balance trim must be a non-positive "
                        "Gain wired to the selected stereo bus before the driver split"
                    ),
                ))
        unknown_baseline_outputs = sorted(graph_indexes - known_indexes)
        if unknown_baseline_outputs:
            issues.append(_issue(
                "blocker",
                "active_baseline_routes_unknown_outputs",
                "active baseline routes outputs not assigned by the saved topology: "
                + ", ".join(str(index + 1) for index in unknown_baseline_outputs),
            ))
        # Local-subwoofer bass-management re-proof. A sub topology DEMANDS the sub
        # guard (the sub output is band-limited + excursion-limited + gain<=0) AND
        # the complementary mains high-pass on every main's lowest driver — the two
        # halves of one crossover. A half-present crossover (sub LP without the
        # mains HP, or a sub output missing its low-pass) is fail-closed UNSAFE.
        if contract.subwoofer_present:
            for index in sorted(sub_outputs):
                if not sub_guard_present(
                    view,
                    channels={index},
                    lowpass_name=_sub_lowpass_name(),
                    gain_name=_sub_baseline_gain_name(),
                    limiter_name=_sub_baseline_limiter_name(),
                    limiter_clip_ceiling_db=BASELINE_LIMITER_CLIP_LIMIT_DB,
                ):
                    issues.append(_issue(
                        "blocker",
                        "active_baseline_sub_guard_missing",
                        (
                            "active baseline subwoofer output is not band-limited, "
                            "excursion-limited, and non-positive-gain on DAC output "
                            f"{index + 1}"
                        ),
                    ))
            if not mains_low_outputs:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_bass_mgmt_mains_missing",
                    (
                        "saved topology has a subwoofer but no main lowest-driver "
                        "output to carry the complementary bass-management high-pass"
                    ),
                ))
            else:
                # The emitter folds the bass-management HP into the lowest
                # driver's role-grouped Filter step (one step targets all of that
                # role's outputs), so the HP is proven once against the whole
                # lowest-driver output set — woofer for active mains, full_range
                # for passive.
                low_role = next(
                    (
                        by_output[index].role
                        for index in sorted(mains_low_outputs)
                        if index in by_output
                    ),
                    "full_range",
                )
                bass_highpass_name = _bass_management_hp_name(low_role)
                if (
                    not mains_highpass_present(
                        view,
                        channels=mains_low_outputs,
                        highpass_name=bass_highpass_name,
                    )
                    or not _bass_management_filter_safe(
                        payload,
                        name=bass_highpass_name,
                        direction="highpass",
                    )
                ):
                    issues.append(_issue(
                        "blocker",
                        "active_baseline_bass_mgmt_highpass_missing",
                        (
                            "active baseline main lowest-driver outputs are missing "
                            "the complementary bass-management high-pass on DAC "
                            "outputs "
                            + ", ".join(
                                str(index + 1) for index in sorted(mains_low_outputs)
                            )
                            + f" ({low_role})"
                        ),
                    ))
                elif not bass_management_corner_matched(
                    view,
                    lowpass_name=_sub_lowpass_name(),
                    highpass_name=_bass_management_hp_name(low_role),
                ):
                    # Both halves exist, but at DIFFERENT corners — not two halves
                    # of one crossover. A split crossover (e.g. an 80 Hz mains HP
                    # under a 1000 Hz sub LP) leaves the sub reproducing midrange or
                    # a mid-band hole. The emitter drives both from one Fc, so this
                    # only fires on a corrupted/tampered statefile — fail closed.
                    issues.append(_issue(
                        "blocker",
                        "active_baseline_bass_mgmt_corner_split",
                        (
                            "active baseline sub low-pass and mains bass-management "
                            "high-pass are at different corners — not two halves of "
                            "one crossover (the crossover Fc has been split)"
                        ),
                    ))
        crossovers_by_role: dict[str, tuple[tuple[str, str], ...]] = {}
        for index in sorted(required_indexes):
            assignment = by_output.get(index)
            if assignment is None:
                continue
            role = assignment.role
            # The sub output's protection is proven by sub_guard_present above
            # (its gain/limiter names are sub-specific, not role-derived). Its
            # post-limiter tail still needs the same fail-closed check as a main.
            limiter_name = (
                _sub_baseline_limiter_name()
                if role == "subwoofer"
                else _baseline_limiter_name(role)
            )
            chain_notes: list[dict[str, str]] = []
            crossovers = _baseline_output_chain(
                payload,
                assignment=assignment,
                channel=index,
                bass_management_highpass=(
                    contract.subwoofer_present and index in mains_low_outputs
                ),
                rear_stage_lead=rear_lead.get(index, 0),
                notes=chain_notes,
            )
            if crossovers is None:
                # The NUMERIC refusal reports itself, with the peak, the
                # allowance and the frequency; fall back to the shape sentence
                # only when the shape is genuinely what failed, or a reader is
                # sent after the wrong defect.
                if chain_notes:
                    issues.extend(chain_notes)
                else:
                    issues.append(_issue(
                        "blocker",
                        "active_output_driver_chain_unrecognized",
                        (
                            "active graph does not use the exact ordered emitter "
                            f"chain on DAC output {index + 1} ({role})"
                        ),
                    ))
            else:
                prior = crossovers_by_role.setdefault(role, crossovers)
                if prior != crossovers:
                    issues.append(_issue(
                        "blocker",
                        "active_output_driver_chain_unrecognized",
                        f"active graph uses inconsistent {role} crossover chains",
                    ))
                role_channels = {
                    output
                    for output, item in by_output.items()
                    if item.role == role
                }
                post_split_names = _post_split_filter_names(
                    payload, channel=index,
                )[rear_lead.get(index, 0):]
                limiter_index = post_split_names.index(limiter_name)
                expected_names = post_split_names[: limiter_index + 1]
                if index == min(role_channels) and not _canonical_chain_grouped(
                    payload,
                    expected_channels=role_channels,
                    expected_names=expected_names,
                ):
                    issues.append(_issue(
                        "blocker",
                        "active_output_driver_chain_not_grouped",
                        (
                            f"active graph must wire one exact grouped {role} "
                            "driver chain across its current outputs"
                        ),
                    ))
            limiter_count, unsafe_tail = _post_limiter_tail_evidence(
                payload,
                channel=index,
                limiter_name=limiter_name,
            )
            if limiter_count != 1:
                issues.append(_issue(
                    "blocker",
                    "active_output_limiter_order_invalid",
                    (
                        "active graph must wire exactly one canonical limiter "
                        f"after the active split on DAC output {index + 1}; "
                        f"found {limiter_count}"
                    ),
                ))
            if unsafe_tail:
                issues.append(_issue(
                    "blocker",
                    "active_output_post_limiter_filter_unsafe",
                    (
                        "active graph has an unapproved filter after the canonical "
                        f"limiter on DAC output {index + 1}: "
                        + ", ".join(unsafe_tail)
                    ),
                ))
            total_delay_ms, invalid_delays = _post_split_delay_evidence(
                payload,
                channel=index,
            )
            if invalid_delays:
                issues.append(_issue(
                    "blocker",
                    "active_output_delay_invalid",
                    (
                        "active graph has a malformed post-split delay on DAC "
                        f"output {index + 1}: " + ", ".join(invalid_delays)
                    ),
                ))
            maximum_delay_ms = MAX_DSP_DELAY_US / 1000.0
            if total_delay_ms > maximum_delay_ms:
                issues.append(_issue(
                    "blocker",
                    "active_output_delay_ceiling_exceeded",
                    (
                        "active graph cumulative post-split delay exceeds the "
                        f"{maximum_delay_ms:g} ms ceiling on DAC output "
                        f"{index + 1}: {total_delay_ms:g} ms"
                    ),
                ))
            if role == "subwoofer":
                continue
            gain_name = _baseline_gain_name(role)
            names = _pipeline_names_for_channels(payload, channels={index})
            if limiter_name not in names or gain_name not in names:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_driver_chain_missing",
                    (
                        "active baseline graph does not wire gain and limiter "
                        f"filters for DAC output {index + 1} ({role})"
                    ),
                ))
            limiter_params = _filter_params(payload, limiter_name)
            limiter_clip = as_float(limiter_params.get("clip_limit"))
            if (
                _filter_type(payload, limiter_name) != "Limiter"
                or limiter_clip is None
                or not math.isfinite(limiter_clip)
                or limiter_clip > 0.0
                or not _truthy_bool(limiter_params.get("soft_clip"))
            ):
                issues.append(_issue(
                    "blocker",
                    "active_baseline_limiter_invalid",
                    (
                        "active baseline limiter is missing or unsafe for "
                        f"DAC output {index + 1} ({role})"
                    ),
                ))
            gain = as_float(_filter_params(payload, gain_name).get("gain"))
            if gain is None or not math.isfinite(gain) or gain > 0.0:
                issues.append(_issue(
                    "blocker",
                    "active_baseline_gain_positive",
                    (
                        "active baseline driver gain is missing or positive for "
                        f"DAC output {index + 1} ({role})"
                    ),
                ))
            if index in tweeter_outputs:
                highpass_names = [
                    name for name in names
                    if _filter_type(payload, name) == "BiquadCombo"
                    and str(_filter_params(payload, name).get("type") or "")
                    == "LinkwitzRileyHighpass"
                    and (as_float(_filter_params(payload, name).get("freq")) or 0.0)
                    > 0.0
                ]
                if not highpass_names:
                    issues.append(_issue(
                        "blocker",
                        "active_baseline_tweeter_highpass_missing",
                        (
                            "active baseline tweeter output is missing a "
                            f"wired high-pass filter on DAC output {index + 1}"
                        ),
                    ))
        for lower_role, upper_role in _mismatched_crossover_pairs(
            payload,
            crossovers_by_role,
            active_way_counts,
        ):
            issues.append(_issue(
                "blocker",
                "active_output_crossover_pair_mismatch",
                (
                    f"active graph {lower_role}/{upper_role} low-pass and "
                    "high-pass must share one finite corner and LR order"
                ),
            ))

    return {
        "safe": not issues,
        "issues": issues,
        "required_outputs": sorted(required_indexes),
        "unmuted_outputs": sorted(unmuted_outputs),
        "muted_outputs": sorted(muted_outputs),
        "all_muted": all_muted,
        "baseline_candidate": is_baseline and not is_baseline_commissioning,
        "baseline_commissioning_candidate": is_baseline_commissioning,
        "baseline_commissioning_group": (
            baseline_commissioning_pair[0]
            if baseline_commissioning_pair is not None
            else None
        ),
        "baseline_commissioning_roles": (
            list(baseline_commissioning_pair[1])
            if baseline_commissioning_pair is not None
            else []
        ),
        "driver_domain_candidate": is_driver_domain,
        "unmuted_roles": sorted(unmuted_roles),
        "tweeter_outputs": sorted(tweeter_outputs),
        "subwoofer_present": contract.subwoofer_present,
        "subwoofer_outputs": sorted(sub_outputs),
        "mains_bass_mgmt_outputs": sorted(mains_low_outputs),
        "split_channels": split_channels,
    }
