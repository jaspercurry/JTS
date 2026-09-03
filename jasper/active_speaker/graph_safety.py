# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared CamillaDSP graph-safety primitives for active-speaker commissioning.

Three adapters normalise a graph into one :class:`GraphView` — the emitted text
(a strict shape check that doubles as the emitter-drift guard), CamillaDSP's
own read-back dialect, and a ``yaml.safe_load``ed candidate — and the
fail-closed predicates then run on the view, so the logic is shared while each
source keeps its parsing semantics. Everything is pure: an unparseable graph, a
missing filter or a mismatched wiring yields ``parsed_ok=False``/``False``, so a
caller can never read "safe" out of a graph it could not prove safe. A leaf
(stdlib only — callers own the ``yaml.safe_load`` and pass in filter names), and
the single home of the shared scalar matchers no verifier may re-implement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

# --------------------------------------------------------------------------- #
# Scalar / inline-collection text parsing (the emitted-config dialect).
# --------------------------------------------------------------------------- #


def _parse_scalar(value: str) -> Any:
    cleaned = value.split("#", 1)[0].strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        return cleaned[1:-1]
    if cleaned in {"true", "false"}:
        return cleaned == "true"
    try:
        if "." in cleaned:
            return float(cleaned)
        return int(cleaned)
    except ValueError:
        return cleaned


def _parse_inline_mapping(value: str) -> dict[str, Any]:
    value = value.strip()
    if not (value.startswith("{") and value.endswith("}")):
        return {}
    out: dict[str, Any] = {}
    for item in value[1:-1].split(","):
        if ":" not in item:
            continue
        key, raw_value = item.split(":", 1)
        out[key.strip()] = _parse_scalar(raw_value)
    return out


def _parse_inline_list(value: str) -> list[Any]:
    value = value.strip()
    if not (value.startswith("[") and value.endswith("]")):
        return []
    return [_parse_scalar(item) for item in value[1:-1].split(",") if item.strip()]


def _top_level_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not line.startswith(" ") and stripped.endswith(":"):
            current = stripped[:-1]
            sections[current] = []
            continue
        if current:
            sections[current].append(line)
    return sections


# --------------------------------------------------------------------------- #
# Scalar matchers — the shared scalar vocabulary, owned HERE (the leaf).
# The raw-dict verifiers import them from here too; never re-implement them.
# --------------------------------------------------------------------------- #


def float_matches(value: Any, expected: float) -> bool:
    """True iff ``value`` parses to within 1e-4 of ``expected`` (fail-closed)."""
    try:
        return abs(float(value) - expected) < 0.0001
    except (OverflowError, TypeError, ValueError):
        return False


def float_value(value: Any) -> float | None:
    """``value`` as a float, or ``None`` if it does not parse.

    For threshold predicates (``freq > 0``, ``clip <= ceiling``) where a missing
    or unparseable value must fail the check rather than raise."""
    try:
        return float(value)
    except (OverflowError, TypeError, ValueError):
        return None


def truthy_bool(value: Any) -> bool:
    """A CamillaDSP YAML boolean: ``True`` or the string ``"true"``."""
    return value is True or (isinstance(value, str) and value.lower() == "true")


# --------------------------------------------------------------------------- #
# Normalised graph view + source-specific adapters.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GraphFilter:
    """A CamillaDSP filter definition reduced to ``type`` + ``parameters``."""

    type: str | None
    params: dict[str, Any]


@dataclass(frozen=True)
class GraphPipelineStep:
    """A pipeline ``Filter`` step reduced to its target channels + filter names."""

    channels: frozenset[int]
    names: tuple[str, ...]


@dataclass(frozen=True)
class GraphView:
    """A CamillaDSP graph normalised for invariant checks.

    ``parsed_ok`` is ``False`` when the source could not be parsed into a graph
    object; predicates then fail closed against the empty view.
    """

    parsed_ok: bool
    filters: dict[str, GraphFilter] = field(default_factory=dict)
    pipeline_steps: tuple[GraphPipelineStep, ...] = ()


@dataclass(frozen=True)
class BassExtensionBlockEvidence:
    """Independent proof of the optional natural-at-rest bass filter block."""

    valid: bool
    expected: bool
    definitions_present: tuple[str, ...]
    reference_channels: tuple[int, ...]
    reason: str | None = None


def _filters_from_dict(payload: dict[str, Any]) -> dict[str, GraphFilter]:
    raw = payload.get("filters")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, GraphFilter] = {}
    for name, spec in raw.items():
        if not isinstance(name, str):
            continue
        spec = spec if isinstance(spec, dict) else {}
        ftype = spec.get("type")
        params = spec.get("parameters")
        out[name] = GraphFilter(
            type=str(ftype) if ftype is not None else None,
            params=params if isinstance(params, dict) else {},
        )
    return out


def _names_tuple(raw: Any) -> tuple[str, ...]:
    # A non-list `names`, and any `None` entry, are dropped (not stringified to
    # "None") — uniform across adapters, and harmless for the `all(required in
    # names)` membership checks the predicates run.
    if not isinstance(raw, list):
        return ()
    return tuple(str(name) for name in raw if name is not None)


def view_from_camilla_dict(config: Any) -> GraphView:
    """Adapter for CamillaDSP's read-back of the *running* graph (its dialect).

    A pipeline ``Filter`` step's channels may be a ``channels: [..]`` list OR the
    scalar ``channel: N`` sugar; bools are not channels. Fails closed if
    ``config`` is not a dict.
    """
    if not isinstance(config, dict):
        return GraphView(parsed_ok=False)
    steps: list[GraphPipelineStep] = []
    pipeline = config.get("pipeline")
    if isinstance(pipeline, list):
        for step in pipeline:
            if not isinstance(step, dict) or step.get("type") != "Filter":
                continue
            steps.append(
                GraphPipelineStep(
                    _running_step_channels(step), _names_tuple(step.get("names"))
                )
            )
    return GraphView(True, _filters_from_dict(config), tuple(steps))


def _running_step_channels(step: dict[str, Any]) -> frozenset[int]:
    chans = step.get("channels")
    if isinstance(chans, list):
        return frozenset(
            int(c) for c in chans if isinstance(c, int) and not isinstance(c, bool)
        )
    ch = step.get("channel")
    if isinstance(ch, int) and not isinstance(ch, bool):
        return frozenset({int(ch)})
    return frozenset()


def view_from_yaml_dict(config: Any) -> GraphView:
    """Adapter for ``runtime_contract``'s candidate/unknown graph (already parsed).

    Accepts ONLY the ``channels: [..]`` list form — never CamillaDSP's scalar
    ``channel: N`` sugar, which is a read-back artifact no candidate graph
    carries, so a list-only reader keeps candidate verification from accepting
    one. Dict-taking: the caller owns the ``yaml.safe_load``, so the candidate
    text is parsed once. Fails closed on a non-mapping; ``bool`` channels and
    ``None`` names are dropped, which only makes a wiring check stricter.
    """
    if not isinstance(config, dict):
        return GraphView(parsed_ok=False)
    steps: list[GraphPipelineStep] = []
    pipeline = config.get("pipeline")
    if isinstance(pipeline, list):
        for step in pipeline:
            if not isinstance(step, dict) or step.get("type") != "Filter":
                continue
            chans = step.get("channels")
            if not isinstance(chans, list):
                continue  # list form only — scalar `channel: N` sugar is ignored
            channels = frozenset(
                int(c) for c in chans if isinstance(c, int) and not isinstance(c, bool)
            )
            steps.append(GraphPipelineStep(channels, _names_tuple(step.get("names"))))
    return GraphView(True, _filters_from_dict(config), tuple(steps))


def view_from_emitted_text(text: str) -> GraphView:
    """Adapter for the *JTS-emitted* config text (the emitter-drift guard).

    A line/indent parser that reads the inline shapes the JTS emitter writes and
    intentionally does NOT accept CamillaDSP's re-serialised dialect — that is
    what catches emitter drift. Always ``parsed_ok=True``: an empty or garbled
    graph yields no filters/steps, so predicates still fail closed.
    """
    sections = _top_level_sections(text)

    filters: dict[str, GraphFilter] = {}
    current_name: str | None = None
    in_parameters = False
    pending: dict[str, dict[str, Any]] = {}
    for line in sections.get("filters", []):
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 2 and stripped.endswith(":"):
            current_name = stripped[:-1]
            pending[current_name] = {"type": None, "parameters": {}}
            in_parameters = False
            continue
        if not current_name or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        if indent == 4 and key == "type":
            pending[current_name]["type"] = str(_parse_scalar(raw_value))
            in_parameters = False
            continue
        if indent == 4 and key == "parameters":
            pending[current_name]["parameters"].update(_parse_inline_mapping(raw_value))
            in_parameters = True
            continue
        if indent > 4 and in_parameters:
            pending[current_name]["parameters"][key] = _parse_scalar(raw_value)
    for name, spec in pending.items():
        filters[name] = GraphFilter(type=spec["type"], params=spec["parameters"])

    # _emitted_step returns None for non-Filter steps; filtering at the append
    # sites rather than at the end keeps mypy's narrowing version-independent.
    steps: list[GraphPipelineStep] = []
    current: dict[str, Any] | None = None
    for line in sections.get("pipeline", []):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            if current is not None:
                step = _emitted_step(current)
                if step is not None:
                    steps.append(step)
            current = {}
            stripped = stripped[2:]
        if current is None or ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value.startswith("["):
            current[key] = _parse_inline_list(raw_value)
        else:
            current[key] = _parse_scalar(raw_value)
    if current is not None:
        step = _emitted_step(current)
        if step is not None:
            steps.append(step)

    return GraphView(True, filters, tuple(steps))


def _emitted_step(item: dict[str, Any]) -> GraphPipelineStep | None:
    if item.get("type") != "Filter":
        return None
    # `bool` is a subclass of `int`; a `true`/`false` in a channel list is
    # malformed, so exclude it. Excluding only makes a wiring check stricter.
    channels = frozenset(
        int(channel)
        for channel in item.get("channels", [])
        if isinstance(channel, int) and not isinstance(channel, bool)
    )
    return GraphPipelineStep(channels, _names_tuple(item.get("names")))


# --------------------------------------------------------------------------- #
# Invariant predicates (run on a normalised GraphView).
# --------------------------------------------------------------------------- #


def filter_param_matches(
    view: GraphView,
    name: str,
    *,
    filter_type: str,
    params: dict[str, Any],
) -> bool:
    """True iff filter ``name`` is of ``filter_type`` and every param matches.

    Float params compare with :func:`float_matches`; others compare ``==``.
    """
    fdef = view.filters.get(name)
    if fdef is None or fdef.type != filter_type:
        return False
    for key, expected in params.items():
        value = fdef.params.get(key)
        if isinstance(expected, float):
            if not float_matches(value, expected):
                return False
        elif value != expected:
            return False
    return True


def pipeline_contains_chain(
    view: GraphView,
    *,
    channels: set[int] | frozenset[int],
    required_names: tuple[str, ...],
) -> bool:
    """True iff some pipeline Filter step targets exactly ``channels`` and lists
    every name in ``required_names``."""
    target = frozenset(channels)
    for step in view.pipeline_steps:
        if step.channels == target and all(n in step.names for n in required_names):
            return True
    return False


def protection_requirement_present(
    view: GraphView,
    *,
    output_index: int,
    allowed_channels: set[int] | frozenset[int],
    requirement: Any,
) -> bool:
    """Whether one output proves a confirmed driver band-limit requirement.

    A high-pass must be at or above its confirmed corner, a low-pass at or below
    it, and either must meet the confirmed minimum slope. A covering pipeline
    step may group outputs only inside the caller-supplied same-role channel set.
    """

    if not isinstance(requirement, dict):
        return False
    kind = str(requirement.get("kind") or "")
    expected_type = {
        "highpass": "LinkwitzRileyHighpass",
        "lowpass": "LinkwitzRileyLowpass",
    }.get(kind)
    cutoff = requirement.get("cutoff_hz")
    slope = requirement.get("minimum_slope_db_per_octave")
    if (
        expected_type is None
        or isinstance(cutoff, bool)
        or not isinstance(cutoff, (int, float))
        or isinstance(slope, bool)
        or not isinstance(slope, (int, float))
        or requirement.get("family_or_equivalent") != "equivalent_or_steeper"
    ):
        return False
    allowed = frozenset(int(channel) for channel in allowed_channels)
    if output_index not in allowed:
        return False
    for step in view.pipeline_steps:
        if output_index not in step.channels or not step.channels <= allowed:
            continue
        for name in step.names:
            definition = view.filters.get(name)
            if definition is None or definition.type != "BiquadCombo":
                continue
            if definition.params.get("type") != expected_type:
                continue
            actual_cutoff = float_value(definition.params.get("freq"))
            actual_order = float_value(definition.params.get("order"))
            if actual_cutoff is None or actual_order is None:
                continue
            cutoff_ok = (
                actual_cutoff >= float(cutoff)
                if kind == "highpass"
                else actual_cutoff <= float(cutoff)
            )
            if cutoff_ok and actual_order * 6.0 >= float(slope):
                return True
    return False


# Absolute lower bound (Hz) on a tweeter-role protective high-pass corner: the
# corner must keep the low-frequency excursion hazard band (roughly 100 Hz–1 kHz)
# off a driver ~25 dB more sensitive than the woofer. 400 Hz is deliberately
# conservative — far below any realistic tweeter crossover (the shipped presets
# cross at 1600 Hz), so it can never over-block a genuine preset while still
# catching a "high-pass" left at 30 / 80 / 100 Hz.
#
# SCOPE: presence + this corner FLOOR only. Whether a preset's *designed* Fc
# suits its specific driver is ``path_safety``'s
# ``tweeter_protection_floor_honoured``, against the driver's own declared
# floor; this absolute corner is the backstop for a driver that declared none.
TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ = 400.0


def output_highpass_protected(
    view: GraphView,
    *,
    channel: int,
    allowed_channels: set[int] | frozenset[int],
    min_corner_hz: float = TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ,
) -> bool:
    """True iff ``channel`` is high-pass protected in the pipeline (fail-closed).

    The L0 emit-gate primitive: a compression-driver / tweeter output MUST carry
    a protective high-pass so full-range program can never reach a ~25 dB-hotter
    driver. Two guards, both load-bearing:

    * **Channel-set boundary (subset-of-role).** ``GraphView`` drops ``Mixer``
      steps, so a pre-split high-pass on the stereo program bus ``[0, 1]`` could
      numerically "cover" a post-split tweeter output and false-PASS it. The
      covering step's channels must be a subset of the tweeter-role output set.
    * **Corner floor** — see :data:`TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ`.

    Any qualifying LR high-pass counts, including the crossover's own; this is
    deliberately looser than :func:`tweeter_guard_present`, which also pins a
    named protective HP + limiter for the commissioning re-prove.
    """
    allowed = frozenset(int(c) for c in allowed_channels)
    for step in view.pipeline_steps:
        if channel not in step.channels or not step.channels <= allowed:
            continue
        for name in step.names:
            fdef = view.filters.get(name)
            if fdef is None or fdef.type != "BiquadCombo":
                continue
            if str(fdef.params.get("type") or "") != "LinkwitzRileyHighpass":
                continue
            freq = float_value(fdef.params.get("freq"))
            if freq is not None and freq >= min_corner_hz:
                return True
    return False


def unprotected_tweeter_outputs(
    view: GraphView,
    *,
    tweeter_channels: set[int] | frozenset[int],
    min_corner_hz: float = TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ,
) -> tuple[int, ...]:
    """The tweeter output channels that are NOT high-pass protected (fail-closed).

    A non-empty result is the block signal: such a graph would send full-range
    program to a compression driver. ``tweeter_channels`` doubles as the
    ``allowed_channels`` boundary, so a high-pass counts only when wired within
    the tweeter-role set. An empty ``tweeter_channels`` returns ``()`` — no
    tweeter role means nothing to protect, so passive full-range graphs pass.
    """
    channels = {int(c) for c in tweeter_channels}
    return tuple(
        sorted(
            channel
            for channel in channels
            if not output_highpass_protected(
                view,
                channel=channel,
                allowed_channels=channels,
                min_corner_hz=min_corner_hz,
            )
        )
    )


def output_hard_muted_and_wired(
    view: GraphView,
    index: int,
    *,
    mute_name: str,
    mute_gain_db: float,
) -> bool:
    """True iff output ``index`` is a hard mute (Gain, ``mute_gain_db``,
    ``mute: True``) **and** that mute filter is wired to channel ``index``.

    The crash-recovery / "all others muted" invariant. Fails closed.
    """
    muted = filter_param_matches(
        view,
        mute_name,
        filter_type="Gain",
        params={"gain": mute_gain_db, "mute": True},
    )
    wired = pipeline_contains_chain(view, channels={index}, required_names=(mute_name,))
    return muted and wired


def output_terminally_muted(
    payload: Mapping[str, Any],
    view: GraphView,
    index: int,
    *,
    mute_name: str,
    mute_gain_db: float,
) -> bool:
    """True iff channel ``index`` ends the pipeline in a hard mute nothing undoes.

    Three facts, all read off the parsed graph — never off a filename or a
    source marker:

    1. The channel carries the repo's one mute idiom
       (:func:`output_hard_muted_and_wired`).
    2. That mute is TERMINAL: last name in its own ``Filter`` step, no later
       ``Filter`` step touches the channel, no step of any other type follows.
       CamillaDSP applies a step's filters in order, so a ``Gain`` after the mute
       re-amplifies and a later ``Mixer`` can re-inject another channel's signal;
       a mute that merely appears somewhere is not a mute. A ``Filter`` step with
       no ``channels`` key applies to every channel, so it counts as touching it.
    3. **No pipeline step is ``bypassed``** — CamillaDSP skips such a step
       entirely, leaving the channel live while fact 1 still reads as satisfied,
       and :class:`GraphView` does not model the flag. Checked here on the raw
       pipeline, and deliberately WHOLESALE: no JTS emitter writes ``bypassed``,
       so its presence means the graph was hand-edited.

    Fails closed on any shape it cannot read. ``mute_name``/``mute_gain_db`` are
    parameters, keeping this module free of the emitter's naming module.
    """
    if not output_hard_muted_and_wired(
        view, index, mute_name=mute_name, mute_gain_db=mute_gain_db
    ):
        return False
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return False
    muted = False
    for raw_step in pipeline:
        step = raw_step if isinstance(raw_step, dict) else {}
        if truthy_bool(step.get("bypassed")):
            return False
        if step.get("type") != "Filter":
            # Mixer / Processor / Dither after the mute can re-inject or
            # generate signal on the channel.
            if muted:
                return False
            continue
        channels = step.get("channels")
        if isinstance(channels, list) and index not in channels:
            continue
        names = step.get("names")
        if not isinstance(names, list):
            return False
        if muted:
            return False
        if mute_name in names:
            if names[-1] != mute_name:
                return False
            muted = True
    return muted


def output_unmuted_and_wired(view: GraphView, index: int, *, mute_name: str) -> bool:
    """True iff output ``index``'s commission-mute is ``mute: False`` (a Gain)
    **and** wired to channel ``index`` — the per-driver audible-target half."""
    unmuted = filter_param_matches(
        view, mute_name, filter_type="Gain", params={"mute": False}
    )
    wired = pipeline_contains_chain(view, channels={index}, required_names=(mute_name,))
    return unmuted and wired


def tweeter_guard_present(
    view: GraphView,
    *,
    channels: set[int] | frozenset[int],
    hp_name: str,
    limiter_name: str,
    limiter_clip_ceiling_db: float,
) -> bool:
    """True iff a protective high-pass + soft-clip limiter wrap ``channels`` (LOOSE).

    The loose policy ``runtime_contract`` uses when re-proving a candidate
    commissioning graph: it only needs the tweeter *protected enough to be
    audible*, not bit-identical to the emitter, so the bounds are tolerances —
    any positive LR high-pass ``freq`` with ``order`` absent or ``>= 2``, and a
    ``clip_limit <= limiter_clip_ceiling_db`` (a CEILING, not equality) with a
    truthy ``soft_clip``, both wired to exactly ``channels`` in one step.

    Fails closed. A separate predicate, NOT a relaxation of the strict mute/HP
    primitives above.
    """
    hp = view.filters.get(hp_name)
    limiter = view.filters.get(limiter_name)
    hp_params = hp.params if hp else {}
    limiter_params = limiter.params if limiter else {}
    hp_freq = float_value(hp_params.get("freq"))
    hp_order = float_value(hp_params.get("order"))
    limiter_clip = float_value(limiter_params.get("clip_limit"))
    hp_ok = (
        (hp.type if hp else None) == "BiquadCombo"
        and str(hp_params.get("type") or "") == "LinkwitzRileyHighpass"
        and hp_freq is not None
        and hp_freq > 0.0
        and (hp_order is None or hp_order >= 2.0)
    )
    limiter_ok = (
        (limiter.type if limiter else None) == "Limiter"
        and limiter_clip is not None
        and limiter_clip <= limiter_clip_ceiling_db
        and truthy_bool(limiter_params.get("soft_clip"))
    )
    wired = pipeline_contains_chain(
        view, channels=channels, required_names=(hp_name, limiter_name)
    )
    return hp_ok and limiter_ok and wired


def sub_guard_present(
    view: GraphView,
    *,
    channels: set[int] | frozenset[int],
    lowpass_name: str,
    gain_name: str,
    limiter_name: str,
    limiter_clip_ceiling_db: float,
) -> bool:
    """True iff the local-subwoofer output is band-limited AND excursion-limited
    AND non-positive gain — all wired to ``channels`` (LOOSE, fail-closed).

    The sub-lane mirror of :func:`tweeter_guard_present`. A sub output must NEVER
    carry a full-range / low-pass-absent feed, so all three are required: a
    positive-``freq`` LR low-pass with ``order`` absent or ``>= 2``, a ``Gain``
    that is present and ``<= 0`` (never a boost), and a soft-clip ``Limiter`` at
    or under ``limiter_clip_ceiling_db``, wired together in one step."""
    lowpass = view.filters.get(lowpass_name)
    gain = view.filters.get(gain_name)
    limiter = view.filters.get(limiter_name)
    lp_params = lowpass.params if lowpass else {}
    gain_params = gain.params if gain else {}
    limiter_params = limiter.params if limiter else {}
    lp_freq = float_value(lp_params.get("freq"))
    lp_order = float_value(lp_params.get("order"))
    gain_db = float_value(gain_params.get("gain"))
    limiter_clip = float_value(limiter_params.get("clip_limit"))
    lp_ok = (
        (lowpass.type if lowpass else None) == "BiquadCombo"
        and str(lp_params.get("type") or "") == "LinkwitzRileyLowpass"
        and lp_freq is not None
        and lp_freq > 0.0
        and (lp_order is None or lp_order >= 2.0)
    )
    gain_ok = (
        (gain.type if gain else None) == "Gain"
        and gain_db is not None
        and gain_db <= 0.0
    )
    limiter_ok = (
        (limiter.type if limiter else None) == "Limiter"
        and limiter_clip is not None
        and limiter_clip <= limiter_clip_ceiling_db
        and truthy_bool(limiter_params.get("soft_clip"))
    )
    wired = pipeline_contains_chain(
        view,
        channels=channels,
        required_names=(lowpass_name, gain_name, limiter_name),
    )
    return lp_ok and gain_ok and limiter_ok and wired


def sub_audible_guard_present(
    view: GraphView,
    *,
    channels: set[int] | frozenset[int],
    lowpass_name: str,
    lowpass_freq_ceiling_hz: float,
    limiter_name: str,
    limiter_clip_ceiling_db: float,
) -> bool:
    """True iff an AUDIBLE subwoofer output is band-limited AND excursion-limited
    (LOOSE, fail-closed) — the commissioning/startup analogue of
    :func:`sub_guard_present`.

    The commissioning/startup sub lane carries no ``Gain`` filter (the hard mute
    and startup limiter own the level), so only the band-limit and the excursion
    limiter are proved here.

    The low-pass corner CEILING is load-bearing, not cosmetic: for a tweeter
    high-pass a higher corner is MORE protective, but for a sub low-pass it is
    LESS — a 20 kHz "low-pass" passes full-range energy to a bass driver — so an
    upper bound on the corner is required."""
    lowpass = view.filters.get(lowpass_name)
    limiter = view.filters.get(limiter_name)
    lp_params = lowpass.params if lowpass else {}
    limiter_params = limiter.params if limiter else {}
    lp_freq = float_value(lp_params.get("freq"))
    lp_order = float_value(lp_params.get("order"))
    limiter_clip = float_value(limiter_params.get("clip_limit"))
    lp_ok = (
        (lowpass.type if lowpass else None) == "BiquadCombo"
        and str(lp_params.get("type") or "") == "LinkwitzRileyLowpass"
        and lp_freq is not None
        and lp_freq > 0.0
        and lp_freq <= lowpass_freq_ceiling_hz
        and (lp_order is None or lp_order >= 2.0)
    )
    limiter_ok = (
        (limiter.type if limiter else None) == "Limiter"
        and limiter_clip is not None
        and limiter_clip <= limiter_clip_ceiling_db
        and truthy_bool(limiter_params.get("soft_clip"))
    )
    wired = pipeline_contains_chain(
        view, channels=channels, required_names=(lowpass_name, limiter_name)
    )
    return lp_ok and limiter_ok and wired


def mains_highpass_present(
    view: GraphView,
    *,
    channels: set[int] | frozenset[int],
    highpass_name: str,
) -> bool:
    """True iff the bass-management high-pass is the complementary upper half of
    the sub crossover — an LR4 high-pass with any positive ``freq`` wired to the
    mains' lowest-driver ``channels`` (fail-closed).

    Without it the mains still carry full bass, defeating bass management and
    over-driving a woofer below the sub corner. That the two halves share ONE
    corner Fc is the separate :func:`bass_management_corner_matched` proof."""
    hp = view.filters.get(highpass_name)
    hp_params = hp.params if hp else {}
    hp_freq = float_value(hp_params.get("freq"))
    hp_order = float_value(hp_params.get("order"))
    hp_ok = (
        (hp.type if hp else None) == "BiquadCombo"
        and str(hp_params.get("type") or "") == "LinkwitzRileyHighpass"
        and hp_freq is not None
        and hp_freq > 0.0
        and (hp_order is None or hp_order >= 2.0)
    )
    wired = pipeline_contains_chain(
        view, channels=channels, required_names=(highpass_name,)
    )
    return hp_ok and wired


def bass_management_corner_matched(
    view: GraphView,
    *,
    lowpass_name: str,
    highpass_name: str,
) -> bool:
    """True iff the sub low-pass and the mains high-pass share ONE corner Fc —
    the "two halves of one crossover" invariant (fail-closed).

    The emitter drives both halves from one ``sub.crossover_fc_hz``, so this
    re-proof exists to catch a graph the emitter did NOT write — a tampered
    statefile splitting the crossover into an 80 Hz HP under a 1000 Hz LP leaves
    the sub reproducing midrange. Both freqs must be present, positive, and equal
    within the shared float tolerance."""
    lp = view.filters.get(lowpass_name)
    hp = view.filters.get(highpass_name)
    lp_freq = float_value(lp.params.get("freq")) if lp else None
    hp_freq = float_value(hp.params.get("freq")) if hp else None
    if lp_freq is None or hp_freq is None or lp_freq <= 0.0 or hp_freq <= 0.0:
        return False
    return float_matches(lp_freq, hp_freq)


def bass_extension_block_valid(
    view: GraphView,
    profile_summary: Mapping[str, Any],
) -> BassExtensionBlockEvidence:
    """Prove the complete optional sealed natural-at-rest filter pair.

    Permission comes only from separately evaluated profile evidence. A missing,
    deferred, bypassed, or stale profile requires the complete absence of both
    definitions and references. An eligible sealed profile requires the exact
    named pair, exact natural parameters, and one reference on exactly the
    recorded bass-owner channels.
    """

    from jasper.camilla_emit import (
        BASS_EXTENSION_FREQ_HZ_HI,
        BASS_EXTENSION_FREQ_HZ_LO,
        BASS_EXTENSION_Q_HI,
        BASS_EXTENSION_Q_LO,
        BASS_EXTENSION_SUBSONIC_ORDERS,
    )

    names = ("bass_ext_lt", "bass_ext_subsonic")
    definitions = tuple(sorted(name for name in view.filters if name.startswith("bass_ext")))
    references = tuple(
        step
        for step in view.pipeline_steps
        if any(name.startswith("bass_ext") for name in step.names)
    )
    if profile_summary.get("authority_valid") is False:
        return BassExtensionBlockEvidence(
            False,
            bool(profile_summary.get("runtime_block_required")),
            definitions,
            tuple(sorted({c for step in references for c in step.channels})),
            "bass_extension_authority_invalid",
        )
    expected = bool(profile_summary.get("runtime_block_required"))
    if not expected:
        valid = not definitions and not references
        return BassExtensionBlockEvidence(
            valid,
            False,
            definitions,
            tuple(sorted({c for step in references for c in step.channels})),
            None if valid else "bass_extension_block_forbidden",
        )

    if not view.parsed_ok or definitions != names:
        return BassExtensionBlockEvidence(
            False, True, definitions, (), "bass_extension_definitions_invalid"
        )
    natural = profile_summary.get("natural")
    owner_channels = profile_summary.get("bass_owner_channels")
    if not isinstance(natural, Mapping) or not isinstance(owner_channels, (list, tuple)):
        return BassExtensionBlockEvidence(
            False, True, definitions, (), "bass_extension_profile_evidence_invalid"
        )
    if (
        not owner_channels
        or any(type(channel) is not int or channel < 0 for channel in owner_channels)
        or len(set(owner_channels)) != len(owner_channels)
        or any(
            isinstance(natural.get(key), bool)
            or not isinstance(natural.get(key), (int, float))
            for key in ("fp_hz", "qp", "boost_headroom_db")
        )
    ):
        return BassExtensionBlockEvidence(
            False, True, definitions, (), "bass_extension_profile_evidence_invalid"
        )
    try:
        fp_hz = float(natural["fp_hz"])
        qp = float(natural["qp"])
        boost = float(natural["boost_headroom_db"])
        subsonic = natural["subsonic"]
        channels = frozenset(owner_channels)
    except (KeyError, TypeError, ValueError, OverflowError):
        return BassExtensionBlockEvidence(
            False, True, definitions, (), "bass_extension_profile_evidence_invalid"
        )
    if (
        not math.isfinite(fp_hz)
        or not BASS_EXTENSION_FREQ_HZ_LO <= fp_hz <= BASS_EXTENSION_FREQ_HZ_HI
        or not math.isfinite(qp)
        or not BASS_EXTENSION_Q_LO <= qp <= BASS_EXTENSION_Q_HI
        or boost != 0.0
        or not isinstance(subsonic, Mapping)
    ):
        return BassExtensionBlockEvidence(
            False, True, definitions, tuple(sorted(channels)),
            "bass_extension_natural_target_invalid",
        )
    if (
        type(subsonic.get("order")) is not int
        or isinstance(subsonic.get("freq"), bool)
        or not isinstance(subsonic.get("freq"), (int, float))
    ):
        return BassExtensionBlockEvidence(
            False, True, definitions, tuple(sorted(channels)),
            "bass_extension_subsonic_invalid",
        )
    try:
        sub_freq = float(subsonic["freq"])
        sub_order = subsonic["order"]
    except (KeyError, TypeError, ValueError, OverflowError):
        return BassExtensionBlockEvidence(
            False, True, definitions, tuple(sorted(channels)),
            "bass_extension_subsonic_invalid",
        )
    lt = view.filters.get(names[0])
    hp = view.filters.get(names[1])
    params = lt.params if lt else {}
    hp_params = hp.params if hp else {}
    definitions_ok = (
        lt is not None
        and lt.type == "Biquad"
        and params.get("type") == "LinkwitzTransform"
        and all(
            float_matches(params.get(key), expected_value)
            for key, expected_value in (
                ("freq_act", fp_hz),
                ("q_act", qp),
                ("freq_target", fp_hz),
                ("q_target", qp),
            )
        )
        and hp is not None
        and hp.type == "BiquadCombo"
        and subsonic.get("type") == "ButterworthHighpass"
        and hp_params.get("type") == "ButterworthHighpass"
        and math.isfinite(sub_freq)
        and BASS_EXTENSION_FREQ_HZ_LO <= sub_freq <= BASS_EXTENSION_FREQ_HZ_HI
        and sub_order in BASS_EXTENSION_SUBSONIC_ORDERS
        and float_matches(hp_params.get("freq"), sub_freq)
        and hp_params.get("order") == sub_order
    )
    reference_ok = (
        len(references) == 1
        and references[0].channels == channels
        and tuple(name for name in references[0].names if name.startswith("bass_ext"))
        == names
    )
    valid = definitions_ok and reference_ok
    return BassExtensionBlockEvidence(
        valid,
        True,
        definitions,
        tuple(sorted(references[0].channels)) if len(references) == 1 else (),
        None if valid else "bass_extension_block_invalid",
    )


# --------------------------------------------------------------------------- #
# Pipeline reference closure — a structural check, independent of GraphView.
#
# ``GraphView`` DROPS ``Mixer`` steps and never tracks the ``mixers:`` section,
# so no predicate above can catch a pipeline naming a mixer or filter the config
# does not define (CamillaDSP rejects such a graph at LOAD time). This is a
# deliberately narrower primitive: it does not reason about channels, filter
# parameters, or protection — only whether every name the pipeline points at
# exists. Dict-taking, so the caller keeps owning ``yaml.safe_load``.
# --------------------------------------------------------------------------- #


def pipeline_reference_closure_errors(payload: Any) -> tuple[str, ...]:
    """Every pipeline ``Mixer``/``Filter`` reference that resolves to nothing.

    A CamillaDSP pipeline step's ``Mixer.name`` must be a key under the
    top-level ``mixers:`` map, and every entry of a ``Filter.names`` list must
    be a key under ``filters:``. Returns a tuple of human-readable error
    strings (empty when the graph is reference-closed). Fails closed: a
    non-mapping payload or a missing/non-list ``pipeline`` is reported as an
    error rather than silently passing.
    """
    if not isinstance(payload, dict):
        return ("config is not a YAML mapping",)
    mixers = payload.get("mixers")
    mixer_names = set(mixers) if isinstance(mixers, dict) else set()
    filters = payload.get("filters")
    filter_names = set(filters) if isinstance(filters, dict) else set()
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return ("config has no pipeline list",)

    errors: list[str] = []
    for index, step in enumerate(pipeline):
        if not isinstance(step, dict):
            continue
        step_type = step.get("type")
        if step_type == "Mixer":
            name = step.get("name")
            if name not in mixer_names:
                errors.append(
                    f"pipeline step {index} (Mixer) references undefined "
                    f"mixer {name!r}"
                )
        elif step_type == "Filter":
            names = step.get("names")
            if not isinstance(names, list):
                continue
            for filter_name in names:
                if filter_name not in filter_names:
                    errors.append(
                        f"pipeline step {index} (Filter) references undefined "
                        f"filter {filter_name!r}"
                    )
    return tuple(errors)
