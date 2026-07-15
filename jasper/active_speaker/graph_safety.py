# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared CamillaDSP graph-safety primitives for active-speaker commissioning.

This module is the single home for the shared parse adapters and fail-closed
predicates the active-speaker commissioning paths use to assert invariants
against a CamillaDSP graph:

- every per-output commission mute is a hard −120 dB mute **and** wired to its
  own channel (`output_hard_muted_and_wired`) — the crash-recovery boot state
  and the "all others muted" half of per-driver isolation;
- a per-driver unmute leaves exactly the target output un-muted and wired
  (`output_unmuted_and_wired`);
- richer, caller-specific invariants — e.g. an audible tweeter wrapped by the
  protective Linkwitz-Riley high-pass + startup limiter — are composed in the
  callers from `filter_param_matches` + `pipeline_contains_chain`, so each
  caller keeps the per-check evidence its result dict reports.

Before this module these checks were re-implemented across ``staging.py``
(the staged-text and the live read-back paths). The duplication was in the
*predicate logic*, not the parsing: the paths parse a CamillaDSP graph in
legitimately different ways and that difference must be preserved —

1. ``view_from_emitted_text`` — a line/indent text parser over the *JTS-emitted*
   config. It doubles as an emitter-format-drift guard: it asserts the exact
   emitted shape (inline ``channels: [..]`` / ``parameters: {..}``), not merely
   a semantically-equivalent graph.
2. ``view_from_camilla_dict`` — for CamillaDSP's *read-back* of the running
   graph, which it re-serializes in its own dialect (block-style lists, the
   scalar ``channel: N`` single-channel sugar, reordered keys, filled defaults)
   that the text parser cannot read. The caller ``yaml.safe_load``s the
   read-back and hands the dict here.

3. ``view_from_yaml_dict`` — for ``runtime_contract``'s candidate/unknown graph:
   a ``yaml.safe_load``ed emitted active config, accepting only the
   ``channels: [..]`` list form (no scalar ``channel: N`` sugar). Dict-taking
   like ``view_from_camilla_dict`` — ``runtime_contract`` ``yaml.safe_load``s the
   text itself (it needs the raw dict for its two distinct parse-error codes,
   ``camilla_yaml_unparseable`` vs ``camilla_yaml_not_object``, which this view
   collapses to ``parsed_ok=False``) and hands the dict here, so the text is
   parsed once.

All normalise to one :class:`GraphView`; the predicates run on the view, so the
logic is shared while each source keeps its own parsing semantics.

Everything here is pure and **fail-closed**: an unparseable graph, a missing
filter, or a mismatched wiring yields ``parsed_ok=False`` / ``False`` so a
caller can never read "safe" out of a graph it could not prove safe. The
module is a leaf (stdlib only — callers own the ``yaml.safe_load``);
active-speaker constants and filter names (``STARTUP_MUTE_GAIN_DB``,
``output_commission_mute_name``, …) are passed in by callers so the primitives
stay reusable.

As the leaf, this module also OWNS the shared scalar matchers
(``float_matches`` / ``float_value`` / ``truthy_bool``) the predicates here run
on. They are the single home: this module's predicates and the raw-dict
verifiers (``runtime_contract``'s baseline path) both import them from here, so
no verifier re-implements them. The sibling ``graph_evidence`` owns the
complementary, emitter-coupled half (filter names + raw-dict accessors); the two
modules are independent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Scalar / inline-collection text parsing (the emitted-config dialect).
# Ported verbatim from staging.py so the emitted-text adapter is an exact
# behavioural match; staging.py imports these once it migrates onto the view.
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
#
# The single home for the active-speaker scalar matchers: the predicates below
# need them, and the raw-dict verifiers (``runtime_contract``'s baseline path)
# import them from here too. Do not re-implement them in a verifier.
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

    Mirrors ``staging._running_*``: a pipeline ``Filter`` step's channels may be
    a ``channels: [..]`` list OR the scalar ``channel: N`` single-channel sugar;
    bools are not channels. Fails closed if ``config`` is not a dict.
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

    The dialect ``runtime_contract`` verifies: a JTS-emitted active-speaker
    candidate config, ``yaml.safe_load``ed (so inline ``parameters: {..}`` /
    ``channels: [..]`` arrive as real typed values, unlike the line/indent
    ``view_from_emitted_text`` parser). It accepts ONLY the ``channels: [..]``
    list form — NOT CamillaDSP's scalar ``channel: N`` single-channel sugar that
    ``view_from_camilla_dict`` reads. The sugar is a read-back artifact never
    present in a candidate graph, so a list-only reader keeps candidate
    verification from silently accepting it (and matches the deleted
    ``runtime_contract._pipeline_contains``, which was list-only too).

    Dict-taking like ``view_from_camilla_dict`` — the caller owns the
    ``yaml.safe_load``. ``runtime_contract`` already parses the candidate text
    once (it needs the raw dict for its two distinct parse-error codes,
    ``camilla_yaml_unparseable`` vs ``camilla_yaml_not_object``, and for the
    baseline path's raw-dict filter accessors) and hands that dict here rather
    than re-parsing.

    Fails closed: a non-mapping object yields ``parsed_ok=False``. ``bool``
    channels and ``None`` names are dropped, uniform with the other adapters (the
    protective direction — a wiring check only gets stricter).
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

    Mirrors ``staging._parse_generated_filters`` /
    ``_parse_generated_pipeline_filters``: a line/indent parser that reads inline
    ``type:`` / ``parameters: {..}`` filter defs and inline ``channels: [..]`` /
    ``names: [..]`` pipeline steps exactly as the JTS emitter writes them. It
    intentionally does NOT accept CamillaDSP's own re-serialised dialect — that
    is what catches emitter drift. Always ``parsed_ok=True`` (an empty/garbled
    graph yields no filters/steps, so predicates still fail closed).
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

    # _emitted_step returns None for non-Filter pipeline steps (e.g. the
    # master_gain Mixer); skip those at the append sites so `steps` only ever
    # holds real Filter steps. Filtering at append is narrowing-independent —
    # a trailing `tuple(s for s in steps if s is not None)` over an Optional
    # list is narrowed inconsistently by mypy across Python versions.
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
    # malformed, so exclude it (matching `view_from_camilla_dict`). Excluding is
    # the protective choice — it can only make a wiring check stricter, never
    # let an unintended channel satisfy one.
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
    Identical to the prior ``_filter_param_matches`` / ``_running_filter_matches``.
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
    requirement: Any,
) -> bool:
    """Whether one output proves a confirmed driver band-limit requirement.

    Driver and summed excitation share this exact fail-closed interpretation of
    the safety profile: a high-pass must be at or above its confirmed corner, a
    low-pass at or below it, and either must meet the confirmed minimum slope.
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
    for step in view.pipeline_steps:
        if step.channels != frozenset({output_index}):
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


# Absolute lower bound (Hz) on a tweeter-role protective high-pass corner. A
# compression driver is ~25 dB more sensitive than the woofer, so the L0 gate
# proves not just that a high-pass EXISTS on the tweeter output but that its
# corner is high enough to keep the low-frequency excursion hazard band (roughly
# 100 Hz–1 kHz) off the driver. 400 Hz is deliberately conservative: it sits far
# BELOW any realistic tweeter/compression crossover (the shipped presets cross at
# 1600 Hz; real compression crossovers are typically >=~800 Hz), so it can never
# over-block a genuine preset — while still catching the egregious cases (a
# tweeter "high-pass" left at 30 / 80 / 100 Hz that leaves full-range energy on a
# hot driver). Precedent: :func:`sub_audible_guard_present` grew a corner CEILING
# for the mirror-image "loose corner in the unprotective direction" reason.
#
# SCOPE: L0 proves high-pass PRESENCE + a safe corner FLOOR only. It does NOT
# validate that a preset's *designed* crossover Fc is appropriate for its
# specific driver (a DE250 vs a soft-dome tweeter want different corners) — that
# is preset-validation's job and a deliberate follow-up, not built here.
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
    a protective high-pass (its crossover high-pass and/or a dedicated protective
    high-pass) so full-range program can never reach a ~25 dB-hotter driver. True
    iff SOME pipeline ``Filter`` step whose channel set is a **subset of
    ``allowed_channels`` and contains ``channel``** lists a ``BiquadCombo`` filter
    of ``type: LinkwitzRileyHighpass`` with a ``freq`` at or above
    ``min_corner_hz``.

    Two guards, both load-bearing and both fail-closed:

    * **Channel-set boundary (subset-of-role).** ``GraphView`` drops ``Mixer``
      steps, so a pre-split high-pass on the stereo *program* bus ``[0, 1]`` could
      numerically "cover" a post-split tweeter output and false-PASS it. Requiring
      the covering step's channels to be a **subset of the tweeter-role output
      set** accepts the emitter's folded per-role step (e.g. both stereo tweeters
      ``[1, 3]``) and per-output splits (``[1]``), but rejects the ``[0, 1]``
      program bus and any cross-role fold. This is not reachable with today's
      emitter (preference EQ emits a plain ``Biquad``, not ``BiquadCombo``), but
      it is exactly the drift class this gate exists to catch.
    * **Corner floor.** A tweeter high-pass at a too-low corner (30 / 80 / 100 Hz)
      leaves the excursion hazard band on the driver, so the corner must be
      ``>= min_corner_hz`` (see :data:`TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ`).

    Any qualifying LR high-pass counts — the 2-way woofer/tweeter crossover
    high-pass at 1600 Hz is exactly the band-limit that protects the driver; this
    is deliberately looser than :func:`tweeter_guard_present` (which additionally
    pins a named protective HP + limiter for the commissioning re-prove). Fails
    closed: a missing filter, a non-``BiquadCombo`` type, the wrong LR variant
    (a low-pass), a below-floor / non-positive ``freq``, or a covering step that
    reaches outside the tweeter-role channel set all yield ``False``.
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

    The reusable L0 gate over a normalised view: given the set of physical output
    channels the topology/preset assigns a tweeter (compression-driver) role,
    return the sorted subset lacking a protective high-pass per
    :func:`output_highpass_protected`. An empty result means every tweeter output
    is protected. A non-empty result is the fail-closed block signal: such a graph
    would send full-range program to a compression driver.

    ``tweeter_channels`` doubles as the ``allowed_channels`` boundary passed to
    :func:`output_highpass_protected`, so a high-pass only counts when it is wired
    within the tweeter-role output set (rejecting a pre-split program-bus HP that
    would otherwise false-PASS a tweeter output — see that function's channel-set
    guard).

    Fail-closed by construction: an empty ``tweeter_channels`` returns ``()`` (no
    tweeter role → nothing to protect → not over-blocked, so passive full-range
    graphs pass), while every listed channel must PROVE its in-band, above-floor
    high-pass or it lands in the returned tuple.
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
    commissioning graph — deliberately wider than ``staging``'s exact-match guard,
    which pins the emitter's exact Fc/order/clip via ``filter_param_matches`` and
    composes inline. Here ``runtime_contract`` only needs to prove the tweeter is
    *protected enough to be audible*, not that the graph is bit-identical to the
    emitter, so the bounds are tolerances rather than equalities:

    - high-pass: a ``BiquadCombo`` of ``type: LinkwitzRileyHighpass`` with **any
      positive** ``freq`` and ``order`` absent or ``>= 2``;
    - limiter: a ``Limiter`` with ``clip_limit <= limiter_clip_ceiling_db`` (a
      *ceiling*, not equality) and a truthy ``soft_clip``;
    - both filters wired to exactly ``channels`` in one pipeline step.

    Fails closed (missing filter / wrong type / unwired -> ``False``). This is a
    separate predicate, NOT a relaxation of the strict mute/HP primitives above,
    so it cannot change ``staging``'s behaviour.
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

    Mirrors :func:`tweeter_guard_present` for the sub lane. A sub output must
    NEVER carry a full-range / low-pass-absent feed, so all three are required:

    - low-pass: a ``BiquadCombo`` of ``type: LinkwitzRileyLowpass`` with **any
      positive** ``freq`` and ``order`` absent or ``>= 2`` (the band-limit);
    - gain: a ``Gain`` whose ``gain`` is present and ``<= 0`` (never a boost);
    - limiter: a ``Limiter`` with ``clip_limit <= limiter_clip_ceiling_db`` (a
      *ceiling*, not equality) and a truthy ``soft_clip`` (excursion);
    - all three wired to exactly ``channels`` in one pipeline step.

    The loose tolerances match ``tweeter_guard_present`` — ``runtime_contract``
    only needs to prove the sub is protected enough, not bit-identical to the
    emitter. Fails closed (missing filter / wrong type / unwired -> ``False``)."""
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

    The durable-baseline sub lane carries a non-positive ``Gain`` filter, so
    :func:`sub_guard_present` also proves ``gain <= 0``. The commissioning/startup
    sub lane has NO gain filter (the hard mute / startup limiter own the level),
    so this predicate proves only the two that MUST hold for an *unmuted* sub:

    - low-pass: a ``BiquadCombo`` of ``type: LinkwitzRileyLowpass`` with a
      positive ``freq`` **at or below** ``lowpass_freq_ceiling_hz`` and ``order``
      absent or ``>= 2`` (the band-limit);
    - limiter: a ``Limiter`` with ``clip_limit <= limiter_clip_ceiling_db`` (a
      *ceiling*, not equality) and a truthy ``soft_clip`` (excursion);
    - both wired to exactly ``channels`` in one pipeline step.

    NOTE — the low-pass corner ceiling is load-bearing, NOT cosmetic, and is why
    this is **not** a verbatim mirror of :func:`tweeter_guard_present`. For a
    tweeter HIGH-pass, a looser (higher) corner is MORE protective; for a sub
    LOW-pass it is LESS protective — a 20 kHz "low-pass" passes full-range energy
    to a bass driver. So an *upper* bound on the corner (the legal sub-crossover
    ceiling, e.g. 200 Hz) is required; without it a degenerate high-corner LP
    would slip past while the baseline class catches the same shape via
    :func:`bass_management_corner_matched`.

    A sub output must NEVER carry a full-range / low-pass-absent / corner-too-high
    feed while audible; fails closed (missing filter / wrong type / over-ceiling /
    unwired -> ``False``)."""
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

    The sub low-pass without the mains high-pass is half a crossover: the mains
    would still carry full bass, defeating bass management and over-driving a
    woofer below the sub corner. This predicate proves the upper half EXISTS and
    is wired to the mains; that the two halves share ONE corner Fc is the separate
    :func:`bass_management_corner_matched` proof."""
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

    :func:`sub_guard_present` and :func:`mains_highpass_present` prove each half
    EXISTS and is wired; this proves they are complementary at the SAME corner.
    The emitter drives both halves from one ``sub.crossover_fc_hz`` so a freshly
    emitted graph always matches — but the re-proof exists to catch a graph the
    emitter did NOT write (a corrupted/tampered statefile that splits the
    crossover into, e.g., an 80 Hz HP under a 1000 Hz LP, leaving the sub
    reproducing midrange or a mid-band hole). Both freqs must be present, positive,
    and equal within the shared float tolerance; anything else fails closed."""
    lp = view.filters.get(lowpass_name)
    hp = view.filters.get(highpass_name)
    lp_freq = float_value(lp.params.get("freq")) if lp else None
    hp_freq = float_value(hp.params.get("freq")) if hp else None
    if lp_freq is None or hp_freq is None or lp_freq <= 0.0 or hp_freq <= 0.0:
        return False
    return float_matches(lp_freq, hp_freq)
