# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Single-source verification vocabulary for active-speaker CamillaDSP graphs.

The active-speaker safety verifiers re-parse a CamillaDSP config and re-prove
the protective invariants *independently of the emitter*. That independence is
deliberate and stays. This module owns both the shared verification vocabulary
and the commissioning graph proofs used by staging, startup load, and the
Stage-5 live gate, keeping that analysis separate from config construction.

Before this module those were copied: three verifiers hardcoded
``"as_tweeter_protective_hp"`` / ``"as_tweeter_startup_limiter"`` and
``runtime_contract`` re-derived the commission-mute and baseline names, while
``_float_matches`` and the filter accessors were re-implemented verbatim. A
single name change in the emitter could then silently desync a verifier — it
would look for a filter that no longer exists and fail closed, spuriously
blocking commissioning.

Ownership boundary (this module vs the sibling ``graph_safety`` leaf)
--------------------------------------------------------------------
This module owns the verifier's emitter-coupled vocabulary and commissioning
proofs:

* **Canonical filter names** — re-exposed from the emitter (``camilla_yaml``
  owns the spellings; see the public aliases there). Importing ``camilla_yaml``
  is exactly why this module is NOT a leaf.
* **Raw-dict accessors** (``filter_spec`` / ``filter_params`` / ``filter_type``)
  that pull one field straight out of an already-parsed CamillaDSP config
  mapping — for ``runtime_contract``'s baseline path, which works on the raw
  ``payload`` rather than a normalized view.
* **Commissioning evidence** for emitted candidates and running read-back
  graphs, including the crash-recovery all-muted anchor predicate.

The complementary half — the normalized ``GraphView``, the parse adapters, the
fail-closed wiring predicates (``output_hard_muted_and_wired``,
``tweeter_guard_present``, …), and the shared scalar matchers (``float_matches``
/ ``float_value`` / ``truthy_bool``) — lives in the sibling ``graph_safety``
leaf; import those from there. The two modules are independent:
``graph_safety`` has no emitter dependency and stays promotable to a top-level
shared module.

The raw config mapping these accessors read::

    {"filters": {name: {"type": ..., "parameters": {...}}},
     "pipeline": [{"type": "Filter", "channels": [...]|"channel": N,
                   "names": [...]}]}
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import yaml

from . import graph_safety as gs

from .camilla_yaml import (
    APPLIED_RESPONSE_FILTER_MODE,
    COMMISSIONING_FILTER_MODE,
    STARTUP_HEADROOM_DB,
    STARTUP_LIMITER_CLIP_LIMIT_DB,
    STARTUP_MUTE_GAIN_DB,
    audible_outputs_for_role,
    bass_management_hp_name,
    channel_select_mixer_name,
    crossover_highpass_for_role,
    driver_baseline_gain_name,
    driver_baseline_limiter_name,
    driver_delay_name,
    driver_limiter_name,
    driver_linearization_peak_name,
    driver_linearization_shelf_name,
    driver_linearization_taper_name,
    output_commission_mute_name,
    protective_tweeter_hp_name,
    sub_baseline_gain_name,
    sub_baseline_limiter_name,
    sub_lowpass_name,
    sub_startup_limiter_name,
)
from .profile import ActiveSpeakerPreset
from .test_signal_plan import protective_tweeter_highpass_frequency_hz

__all__ = [
    # Canonical filter names (re-exported from the emitter, the single owner).
    "bass_management_hp_name",
    "channel_select_mixer_name",
    "driver_baseline_gain_name",
    "driver_baseline_limiter_name",
    "driver_delay_name",
    "driver_limiter_name",
    "driver_linearization_peak_name",
    "driver_linearization_shelf_name",
    "driver_linearization_taper_name",
    "output_commission_mute_name",
    "protective_tweeter_hp_name",
    "sub_baseline_gain_name",
    "sub_baseline_limiter_name",
    "sub_lowpass_name",
    "sub_startup_limiter_name",
    # Raw-dict accessors (owned here).
    "filter_spec",
    "filter_params",
    "filter_type",
    "driver_commission_audible_evidence",
    "all_commission_mutes_engaged",
    "protective_highpass_hz",
    "running_commission_evidence",
    "running_graph_matches_staged_anchor",
    "software_guard_evidence",
]


def filter_spec(payload: dict[str, Any], name: str) -> dict[str, Any]:
    """The ``filters[name]`` mapping, or ``{}`` if absent/malformed."""
    filters = payload.get("filters")
    raw = filters.get(name) if isinstance(filters, dict) else None
    return raw if isinstance(raw, dict) else {}


def filter_params(payload: dict[str, Any], name: str) -> dict[str, Any]:
    """The ``filters[name].parameters`` mapping, or ``{}``."""
    params = filter_spec(payload, name).get("parameters")
    return params if isinstance(params, dict) else {}


def filter_type(payload: dict[str, Any], name: str) -> str | None:
    """The ``filters[name].type`` as a string, or ``None``."""
    raw = filter_spec(payload, name).get("type")
    return str(raw) if raw is not None else None


def _tweeter_protected_while_audible(
    view: gs.GraphView,
    audible_tweeter: set[int],
    *,
    highpass_name: str,
    protective_hp_hz: float | None,
    highpass_order: int | None,
) -> bool:
    """Prove the audible tweeter chain retains its high-pass and limiter."""

    if not audible_tweeter:
        return True
    hp_defined = protective_hp_hz is not None and gs.filter_param_matches(
        view,
        highpass_name,
        filter_type="BiquadCombo",
        params={
            "type": "LinkwitzRileyHighpass",
            "freq": protective_hp_hz,
            "order": highpass_order,
        },
    )
    limiter_defined = gs.filter_param_matches(
        view,
        driver_limiter_name("tweeter"),
        filter_type="Limiter",
        params={"clip_limit": STARTUP_LIMITER_CLIP_LIMIT_DB},
    )
    chain_wired = gs.pipeline_contains_chain(
        view,
        channels=audible_tweeter,
        required_names=(
            highpass_name,
            driver_limiter_name("tweeter"),
        ),
    )
    return bool(hp_defined and limiter_defined and chain_wired)


def _startup_headroom_present(
    view: gs.GraphView,
    expected_headroom_db: float,
) -> bool:
    """Prove the shared startup headroom filter has the expected gain."""

    return gs.filter_param_matches(
        view,
        "active_startup_headroom",
        filter_type="Gain",
        params={"gain": -expected_headroom_db},
    )


def protective_highpass_hz(preset: ActiveSpeakerPreset) -> float | None:
    return protective_tweeter_highpass_frequency_hz(preset, "tweeter")


def all_commission_mutes_engaged(
    yaml: str,
    *,
    preset: ActiveSpeakerPreset,
) -> bool:
    """Every per-output commission mute is muted AND wired — the crash-recovery boot state.

    The single-audio-path commissioning config isolates drivers with a
    per-physical-output mute mask. The *staged* candidate is the muted boot
    config (``audible_outputs=frozenset()``): a crash or power loss partway
    through commissioning must reboot into everything-muted, never a driver left
    unmuted at level with no protection. Per-driver unmute is a transient runtime
    load, never the frozen boot config.

    This is the *only* mute assertion that runs on every staged config (the
    software guard runs solely in software-protection mode), so it verifies each
    physical output from the preset rather than trusting the emitter to keep its
    filter-definition and pipeline loops in lockstep: for every output index the
    ``as_out{idx}_commission_mute`` filter must be a -120 dB hard mute **and** be
    applied to channel ``{idx}`` in the pipeline. A muted-but-unwired (or
    wired-but-unmuted) output fails closed. Mirrors the per-index rigor of
    :func:`software_guard_evidence`.
    """
    view = gs.view_from_emitted_text(yaml)
    output_count = max((o.index for o in preset.channel_map.outputs), default=-1) + 1
    if output_count <= 0:
        return False
    return all(
        gs.output_hard_muted_and_wired(
            view,
            index,
            mute_name=output_commission_mute_name(index),
            mute_gain_db=STARTUP_MUTE_GAIN_DB,
        )
        for index in range(output_count)
    )


def software_guard_evidence(
    yaml: str,
    *,
    preset: ActiveSpeakerPreset,
) -> dict[str, Any]:
    protective_hp_hz = protective_highpass_hz(preset)
    view = gs.view_from_emitted_text(yaml)
    tweeter_channels = audible_outputs_for_role(preset, "tweeter")
    # Commissioning isolates per *physical output*, so the tweeter is muted iff
    # every physical output carrying it has its as_out{idx}_commission_mute layer
    # engaged. There is no per-role startup mute to check anymore.
    tweeter_outputs_muted = bool(tweeter_channels) and all(
        gs.filter_param_matches(
            view,
            output_commission_mute_name(index),
            filter_type="Gain",
            params={"gain": STARTUP_MUTE_GAIN_DB, "mute": True},
        )
        for index in tweeter_channels
    )
    # The protective high-pass + startup limiter still wrap the tweeter channel
    # in the running pipeline, and every tweeter output keeps its commission-mute
    # layer — so an unmuted tweeter cannot reach the amp without its protection.
    tweeter_pipeline_guarded = (
        bool(tweeter_channels)
        and gs.pipeline_contains_chain(
            view,
            channels=set(tweeter_channels),
            required_names=(
                protective_tweeter_hp_name("tweeter"),
                driver_limiter_name("tweeter"),
            ),
        )
        and all(
            gs.pipeline_contains_chain(
                view,
                channels={index},
                required_names=(output_commission_mute_name(index),),
            )
            for index in tweeter_channels
        )
    )
    checks = {
        "startup_muted": tweeter_outputs_muted,
        "protective_highpass": (
            protective_hp_hz is not None
            and gs.filter_param_matches(
                view,
                protective_tweeter_hp_name("tweeter"),
                filter_type="BiquadCombo",
                params={
                    "type": "LinkwitzRileyHighpass",
                    "freq": protective_hp_hz,
                    "order": 4,
                },
            )
        ),
        "startup_headroom": gs.filter_param_matches(
            view,
            "active_startup_headroom",
            filter_type="Gain",
            params={"gain": -STARTUP_HEADROOM_DB},
        ),
        "startup_limiter": gs.filter_param_matches(
            view,
            driver_limiter_name("tweeter"),
            filter_type="Limiter",
            params={"clip_limit": STARTUP_LIMITER_CLIP_LIMIT_DB},
        ),
        "tweeter_pipeline_guarded": tweeter_pipeline_guarded,
    }
    return {
        "mode": "software_guard_requested",
        "no_load": True,
        "no_playback": True,
        "protective_highpass_hz": protective_hp_hz,
        "startup_headroom_db": STARTUP_HEADROOM_DB,
        "limiter_clip_limit_db": STARTUP_LIMITER_CLIP_LIMIT_DB,
        "tweeter_channels": sorted(tweeter_channels),
        "checks": checks,
        "passed": all(checks.values()),
    }


def driver_commission_audible_evidence(
    yaml: str,
    *,
    preset: ActiveSpeakerPreset,
    audible_outputs: frozenset[int] | set[int],
    expected_headroom_db: float = STARTUP_HEADROOM_DB,
    filter_mode: str = COMMISSIONING_FILTER_MODE,
) -> dict[str, Any]:
    """Per-driver commissioning safety: ONLY the target is audible, and an
    audible tweeter still carries its protective high-pass + limiter.

    The single-audio-path commissioning loads, one driver at a time, a config
    where exactly the target driver's physical outputs are unmuted and every
    other output is hard-muted. This is the *config-level* form of the Stage-5
    safety rule "assert the high-pass is present before the tweeter is unmuted".
    It verifies, against the emitted YAML:

    1. **Audible mask is exactly ``audible_outputs``** — each listed output's
       ``as_out{idx}_commission_mute`` is un-muted AND wired to its channel;
       every OTHER output is a -120 dB hard mute wired to its channel. A muted
       output that is silently un-wired (or vice versa) fails closed. Mirrors
       :func:`all_commission_mutes_engaged`'s per-index rigor.
    2. **Protection-while-audible** — every AUDIBLE tweeter output keeps the
       ``as_tweeter_protective_hp`` Linkwitz-Riley high-pass (at the correct
       ``protective_hp_hz``) and the ``as_tweeter_startup_limiter`` wrapping its
       channel in the running pipeline. A woofer-only target has no audible
       tweeter, so this check is vacuously satisfied while the tweeter stays
       muted.

    Pure analysis of the emitted YAML — opens nothing, loads nothing. The
    assertion against the LIVE CamillaDSP graph (not just the file) before any
    tweeter is unmuted on hardware remains the on-device Stage-5 gate; this is
    the off-device half that gates whether the config is even allowed to load.
    """
    audible = {int(i) for i in audible_outputs}
    view = gs.view_from_emitted_text(yaml)
    output_count = max((o.index for o in preset.channel_map.outputs), default=-1) + 1

    # (1) Audible mask: listed outputs un-muted, all others -120 dB hard-muted,
    # every commission-mute filter wired to its own channel. Fail closed.
    mask_correct = output_count > 0 and bool(audible) and audible <= set(
        range(output_count)
    )
    muted_outputs: list[int] = []
    for index in range(output_count):
        name = output_commission_mute_name(index)
        if index in audible:
            ok = gs.output_unmuted_and_wired(view, index, mute_name=name)
        else:
            muted_outputs.append(index)
            ok = gs.output_hard_muted_and_wired(
                view, index, mute_name=name, mute_gain_db=STARTUP_MUTE_GAIN_DB
            )
        if not ok:
            mask_correct = False

    # (2) Protection-while-audible for an audible tweeter.
    tweeter_outputs = audible_outputs_for_role(preset, "tweeter")
    audible_tweeter = audible & set(tweeter_outputs)
    if filter_mode == APPLIED_RESPONSE_FILTER_MODE:
        highpass = crossover_highpass_for_role(preset, "tweeter")
    else:
        protective_hz = protective_highpass_hz(preset)
        highpass = (
            (
                protective_tweeter_hp_name("tweeter"),
                protective_hz,
                4,
            )
            if protective_hz is not None
            else None
        )
    highpass_name = highpass[0] if highpass is not None else ""
    protective_hp_hz = highpass[1] if highpass is not None else None
    highpass_order = highpass[2] if highpass is not None else None
    tweeter_protected = _tweeter_protected_while_audible(
        view,
        audible_tweeter,
        highpass_name=highpass_name,
        protective_hp_hz=protective_hp_hz,
        highpass_order=highpass_order,
    )
    headroom = _startup_headroom_present(view, expected_headroom_db)
    checks = {
        "audible_mask_correct": mask_correct,
        "tweeter_protected_while_audible": tweeter_protected,
        "startup_headroom": headroom,
    }
    return {
        "audible_outputs": sorted(audible),
        "muted_outputs": muted_outputs,
        "tweeter_outputs": sorted(int(i) for i in tweeter_outputs),
        "audible_tweeter_outputs": sorted(audible_tweeter),
        "protective_highpass_hz": protective_hp_hz,
        "tweeter_highpass_name": highpass_name,
        "tweeter_highpass_order": highpass_order,
        "startup_headroom_db": expected_headroom_db,
        "limiter_clip_limit_db": STARTUP_LIMITER_CLIP_LIMIT_DB,
        "checks": checks,
        "passed": all(checks.values()),
    }


# --- live (running-graph) read-back evidence ---------------------------------
#
# `driver_commission_audible_evidence` (above) proves a config is safe BEFORE it
# loads, parsing the emitted YAML *text*. The guarded commissioning load
# (`commission_load.load_driver_commissioning_config`) needs the same proof AFTER
# the load, against the config CamillaDSP is ACTUALLY running — read back over
# the websocket (`CamillaController.get_active_config_raw`), not the file on
# disk. CamillaDSP re-serializes the running graph in its own YAML dialect
# (block-style lists, defaults filled, keys reordered, scalar `channel: N`
# sugar) that the emitted-text parser does not handle, so
# `running_commission_evidence` parses the read-back with a real YAML loader
# and runs the SAME shared invariant predicates on it via
# `graph_safety.view_from_camilla_dict` (see graph_safety.py for why the three
# parse dialects are kept separate while the predicates are shared).


def _no_bypassed_pipeline_step(payload: Any) -> bool:
    """True iff the graph has a readable pipeline with no ``bypassed`` step.

    The raw-pipeline half of :func:`running_commission_evidence`'s proof, split
    out only so the check has a name. Fails CLOSED in both unreadable
    directions — a non-dict payload or a pipeline that is not a list returns
    False — matching :func:`graph_safety.output_terminally_muted`, which
    refuses its proof on exactly the same shapes.
    """
    if not isinstance(payload, dict):
        return False
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return False
    return not any(
        gs.truthy_bool(step.get("bypassed"))
        for step in pipeline
        if isinstance(step, dict)
    )


def running_commission_evidence(
    running_config_raw: str | None,
    *,
    audible_outputs: Iterable[int],
    muted_outputs: Iterable[int],
    tweeter_outputs: Iterable[int],
    protective_hp_hz: float | None,
    tweeter_highpass_name: str = "",
    tweeter_highpass_order: int = 4,
    expected_headroom_db: float = STARTUP_HEADROOM_DB,
) -> dict[str, Any]:
    """Per-driver commissioning safety, asserted on the RUNNING CamillaDSP graph.

    The live counterpart of :func:`driver_commission_audible_evidence`: given the
    config CamillaDSP is actually running (``CamillaController.get_active_config_raw``)
    and the INTENDED mask the off-device gate already validated, prove the live
    graph still matches — exactly ``audible_outputs`` un-muted (every other
    output a -120 dB hard mute, all wired) and every audible tweeter still wrapped
    by its protective high-pass + startup limiter. This is the "assert the
    high-pass is present in the RUNNING pipeline, not just the config file" gate
    that guards the per-driver tweeter unmute. Fails closed: an unparseable
    read-back, a missing filter, a mask
    that drifted from intent, or any ``bypassed`` pipeline step all return
    ``passed=False``.
    """
    audible = {int(i) for i in audible_outputs}
    muted = {int(i) for i in muted_outputs}
    tweeters = {int(i) for i in tweeter_outputs}
    declared = audible | muted
    audible_tweeter = audible & tweeters

    config: Any = None
    if isinstance(running_config_raw, str) and running_config_raw.strip():
        try:
            config = yaml.safe_load(running_config_raw)
        except yaml.YAMLError:
            config = None
    parse_ok = isinstance(config, dict)
    view = gs.view_from_camilla_dict(config if parse_ok else None)

    # (0) No pipeline step is `bypassed` (#2625). Every predicate below reads
    # :class:`GraphView`, which models filters and channels but NOT the per-step
    # bypass flag — so a graph that carries JTS's own mute filter names and a
    # `bypassed: true` step reads as fully masked while CamillaDSP skips the
    # step entirely and the channel runs live. Checked here, on the raw
    # pipeline, where the flag lives. Same WHOLESALE rule and same reasoning as
    # `graph_safety.output_terminally_muted` fact 3 and the two bench
    # derivation checkers: any bypassed step anywhere refuses the whole proof,
    # because no JTS emitter ever writes `bypassed`, so its presence means the
    # graph was hand-edited and picking which bypassed step is harmless is the
    # generous shape this evidence exists to reject. Not folded into (1) —
    # a named check tells an operator WHICH invariant failed.
    no_bypassed_step = _no_bypassed_pipeline_step(config if parse_ok else None)

    # (1) Audible mask: each declared output un-muted iff in `audible`, every
    # other declared output -120 dB hard-muted, and each mute wired to its own
    # channel. Fail closed on an empty declared set or any drift.
    mask_correct = parse_ok and bool(declared) and audible <= declared
    for index in sorted(declared):
        name = output_commission_mute_name(index)
        if index in audible:
            ok = gs.output_unmuted_and_wired(view, index, mute_name=name)
        else:
            ok = gs.output_hard_muted_and_wired(
                view, index, mute_name=name, mute_gain_db=STARTUP_MUTE_GAIN_DB
            )
        if not ok:
            mask_correct = False

    # (2) Protection-while-audible: an audible tweeter keeps its protective HP +
    # limiter, wired. A muted tweeter is vacuously safe — independent of parse
    # health, which the dedicated ``running_config_parsed`` check already gates.
    highpass_name = tweeter_highpass_name or protective_tweeter_hp_name("tweeter")
    tweeter_protected = _tweeter_protected_while_audible(
        view,
        audible_tweeter,
        highpass_name=highpass_name,
        protective_hp_hz=protective_hp_hz,
        highpass_order=tweeter_highpass_order,
    )
    headroom = _startup_headroom_present(view, expected_headroom_db)
    checks = {
        "running_config_parsed": parse_ok,
        "no_bypassed_pipeline_step": no_bypassed_step,
        "audible_mask_correct": mask_correct,
        "tweeter_protected_while_audible": tweeter_protected,
        "startup_headroom": headroom,
    }
    return {
        "audible_outputs": sorted(audible),
        "muted_outputs": sorted(muted),
        "audible_tweeter_outputs": sorted(audible_tweeter),
        "protective_highpass_hz": protective_hp_hz,
        "startup_headroom_db": expected_headroom_db,
        "checks": checks,
        "passed": all(checks.values()),
    }


def running_graph_matches_staged_anchor(
    running_config_raw: str | None,
    *,
    audible_outputs: Iterable[int],
) -> bool:
    """True when the RUNNING readback still shows the all-muted staged anchor.

    The convergence discriminator for the transient commissioning load
    (``commission_load.load_driver_commissioning_config``): CamillaDSP acks the
    inline ``SetConfig`` before its readback side reflects the new graph, so a
    read taken immediately after the load can still return the staged all-muted
    anchor (hardware-reproduced 2026-07-15, ~22 ms after the apply). The
    intended commission graph un-mutes exactly ``audible_outputs``; the staged
    anchor hard-mutes every output. So "every intended-audible output is still
    hard-muted AND wired" is the cheapest reliable "the switch has not landed
    yet" signal — robust to CamillaDSP's own YAML dialect, unlike raw-text
    comparison against the staged file.

    Fail direction: an unparseable/empty readback or an empty
    ``audible_outputs`` returns False — the caller cannot positively prove the
    graph is still the anchor, so the (failing) live evidence decides
    immediately rather than waiting out the convergence budget on a readback
    that will never discriminate.
    """
    audible = {int(i) for i in audible_outputs}
    if not audible:
        return False
    config: Any = None
    if isinstance(running_config_raw, str) and running_config_raw.strip():
        try:
            config = yaml.safe_load(running_config_raw)
        except yaml.YAMLError:
            config = None
    if not isinstance(config, dict):
        return False
    view = gs.view_from_camilla_dict(config)
    return all(
        gs.output_hard_muted_and_wired(
            view,
            index,
            mute_name=output_commission_mute_name(index),
            mute_gain_db=STARTUP_MUTE_GAIN_DB,
        )
        for index in audible
    )
