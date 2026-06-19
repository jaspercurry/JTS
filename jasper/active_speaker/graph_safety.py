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

Both normalise to one :class:`GraphView`; the predicates run on the view, so
the logic is shared while each source keeps its own parsing semantics. A third
adapter for ``runtime_contract``'s candidate/unknown-graph ``yaml.safe_load``
dialect lands with that module's migration — it carries its own
``camilla_yaml_unparseable`` vs ``camilla_yaml_not_object`` issue codes to
preserve, so it is added there, driven by a real caller, rather than
speculatively here.

Everything here is pure and **fail-closed**: an unparseable graph, a missing
filter, or a mismatched wiring yields ``parsed_ok=False`` / ``False`` so a
caller can never read "safe" out of a graph it could not prove safe. The
module is a leaf (stdlib + ``yaml`` only); active-speaker constants and filter
names (``STARTUP_MUTE_GAIN_DB``, ``output_commission_mute_name``, …) are passed
in by callers so the primitives stay reusable.
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
# Scalar matchers (identical across all prior paths).
# --------------------------------------------------------------------------- #


def float_matches(value: Any, expected: float) -> bool:
    """True iff ``value`` parses to within 1e-4 of ``expected`` (fail-closed)."""
    try:
        return abs(float(value) - expected) < 0.0001
    except (TypeError, ValueError):
        return False


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
