# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The copyable driver-research prompt, built from one bound research request."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .driver_protection import (
    driver_low_limit_plausibility_band_hz,
    driver_protection_profile,
)
from .driver_safety import DRIVER_RESEARCH_KIND


_PROMPT_TARGET_KEYS = (
    "target_id",
    "target_fingerprint",
    "role",
    "manufacturer_and_model",
    "driver_style",
    "speaker_group_id",
    "speaker_group_mode",
    "operator_declared_context",
)

# Keys whose value directly bounds what the speaker is allowed to excite. Only
# these carry per-field provenance in the ask; the rest are advisory prefill an
# operator reviews anyway.
_PROMPT_PROVENANCE_KEYS = (
    "hard_excitation_band_hz",
    "recommended_highpass_hz",
    "required_protection_filters",
    "level_duration_limits",
    "sensitivity_db_2v83_1m",
)


def _driver_research_prompt_targets(request: Mapping[str, Any]) -> str:
    """Return the compact target projection the assistant actually needs."""

    targets = [
        {
            key: target[key]
            for key in _PROMPT_TARGET_KEYS
            if target.get(key) is not None
        }
        for target in request.get("targets", [])
        if isinstance(target, Mapping)
    ]
    projection: dict[str, Any] = {
        "request_fingerprint": request.get("request_fingerprint"),
        "targets": targets,
    }
    if request.get("build_notes"):
        projection["build_notes"] = request["build_notes"]
    return json.dumps(projection, indent=1, sort_keys=True)


def _prompt_example_highpass_hz(request: Mapping[str, Any]) -> float:
    """The worked RESULT SHAPE example's tweeter cutoff, in Hz.

    Read from policy rather than fixed, so the example cannot argue with the
    plausibility band the LIMITS section prints beside it. The strictest
    high-frequency floor among this request's targets is used, so a mixed
    request cannot teach a cutoff illegal for one of its own drivers; a request
    with no high-frequency target falls back to the undeclared-tweeter default.
    """

    floors = [
        policy.min_highpass_hz
        for target in request.get("targets", [])
        if isinstance(target, Mapping)
        for policy in (
            driver_protection_profile(
                str(target.get("role") or ""),
                driver_style=target.get("driver_style"),
            ),
        )
        if policy.min_highpass_hz is not None
    ]
    if floors:
        return max(floors)
    return driver_protection_profile("tweeter").min_highpass_hz or 5000.0


def _driver_research_prompt_limits(request: Mapping[str, Any]) -> list[str]:
    """Return the LIMITS section: its heading, its preamble, and its bounds.

    One gate enforces them — the low-limit band, at intake, by
    :func:`validate_research_low_limit_plausibility` — and they are read from
    that band's owner rather than restated as prose, so the ask cannot drift
    from what the gate refuses. There is deliberately NO level bound: asking for
    a class figure and then reading the reply's echo as a declaration is what
    made a code default the operative ceiling (ADR-0227 §1).

    Every bound is per-target and optional, so the heading is emitted only when
    one exists. Prompt text only: ``request`` and its fingerprint are untouched.
    """

    lines: list[str] = []
    for target in request.get("targets", []):
        if not isinstance(target, Mapping):
            continue
        band = driver_low_limit_plausibility_band_hz(
            str(target.get("role") or ""),
            driver_style=target.get("driver_style"),
        )
        if band is None:
            continue
        lines.append(
            f"- {target.get('target_id')}: recommended_highpass_hz between "
            f"{band[0]:g} and {band[1]:g} if published, else null."
        )
    if not lines:
        return []
    return [
        "LIMITS",
        "This build refuses a reply outside these bounds. They are outer bounds, not recommended values: when a published requirement is stricter, the published one wins.",
        "The minimum-crossover bound is a PLAUSIBILITY range, not a target. A published figure inside it is believed even when it sits below what is typical for the driver type; a figure outside it is refused as a mis-read rather than believed.",
        *lines,
        "",
    ]


def build_driver_research_prompt(request: Mapping[str, Any]) -> str:
    """Return the copyable v2 research prompt for one exact request.

    The contract with the assistant is exactly one fenced ``json`` block back,
    so the browser's paste box can recover the object from an ordinary chat
    reply. The prompt embeds a *projection* of ``request`` — target identities,
    models, operator-declared context — never the whole request; ``request``
    stays the single source of truth the server binds against. The ask is a
    strict SUBSET of what the parser accepts, so asking for less cannot
    invalidate a previously-saved result.

    **The estimate contract.** The ask orders the answer: published value first,
    then the researcher's best reality-grounded engineering estimate tagged
    ``confidence: "low"`` with its derivation in ``basis``, and null only for
    the genuinely unknowable. Fields like ``hard_excitation_band_hz`` appear in
    essentially no consumer datasheet while ``_target_issues`` requires them, so
    forbidding estimates deadlocked most real drivers. Safety never lived in a
    number's timidity: it lives in ``_target_issues``, the per-style
    plausibility screen on the reply, and the quiet-start ramp, and /sound/
    echoes every consumed value back with its badge and source before a save.

    ``max_effective_peak_dbfs`` is asked for as a published fact or not at all:
    naming a class-default ceiling and reading the echo back as a declaration
    pinned a 75 dB SPL seat target at 68.3 dB with ~30 dB of headroom unused.
    The level a measurement runs at comes from the sensitivity derivation
    (:func:`jasper.active_speaker.driver_protection.derive_hf_measurement_ceiling_dbfs`).

    Protection itself is unmoved: a reply outside code policy is refused by name
    rather than silently clamped, by ``_target_issues`` and by
    :func:`validate_research_low_limit_plausibility`.
    """

    # Dropped from the ASK (still accepted, normalised and prefilled when a
    # reply includes them): ``manufacturer`` and ``recommended_lowpass_hz`` have
    # no computational consumer, and ``gain_offset_db`` is a guessed level that
    # would outrank the derived trim in baseline_profile's ladder (measured >
    # pinned > estimate > sensitivity).
    #
    # The crossover vocabulary the KEY GUIDE states is READ from the compiler,
    # never spelled here: the reply is refused against exactly these sets when
    # it is saved, so asking for a vocabulary the saver rejects is an invisible
    # deadlock. Imported inside the call because this module is the research
    # surface, not an audio-graph consumer.
    from .declaration_vocabulary import (
        supported_declaration_filter_types,
        supported_declaration_slopes_db_per_octave,
    )

    # The result shape is fenced because a chat UI's copy button copies the
    # code block's contents, not the prose around it.
    target_count = len(request.get("targets", []))
    entries = "entry" if target_count == 1 else "entries"
    # The worked example's own numbers, derived so they are legal for THIS
    # request's drivers and mutually nested. Round by construction (every policy
    # floor is round and every offset here is a multiple of 500).
    hp = int(_prompt_example_highpass_hz(request))
    hard_low = hp - 500
    return "\n".join(
        (
            "You are a loudspeaker-driver datasheet researcher. Your entire reply is data for a machine to parse, not prose for a human.",
            "",
            "OUTPUT RULE",
            "Reply with exactly one ```json fenced code block containing exactly one JSON object. No text before the fence, no text after it. Do not ask clarifying questions; record any ambiguity in unknowns instead.",
            "",
            "TASK",
            "Research the real published specifications of each target below. Use the manufacturer datasheet first and reputable independent measurements second. When sources conflict, prefer the datasheet and record the conflict in unknowns.",
            "If you can browse, browse silently: no search narration, no source summaries outside the JSON.",
            "",
            "TARGETS",
            _driver_research_prompt_targets(request),
            "",
            "ACCURACY",
            'When a datasheet or a reputable independent measurement gives the number, report that number, with provenance confidence "high" for a datasheet and "medium" for a measurement.',
            'When the number is not published, give your best reality-grounded engineering estimate from the driver\'s published facts and physics. Tag it confidence "low", say in basis how you derived it (for example "estimated: 25 mm soft dome, Fs unpublished"), and round it — an estimate should look like one.',
            "Declare every estimate as an estimate and name one source in that field's provenance either way: the datasheet or measurement for a published number, and for an estimate the one fact or document it leaned on.",
            "Use null only for a field with no engineering basis at all, and add one entry to that driver's unknowns saying which fact is missing.",
            "Never infer physical installation choices such as enclosure kind or horn or waveguide use. Treat operator_declared_context as authoritative; if an installation choice is undeclared, leave it unknown.",
            "For cabinet geometry, research radiator count, effective radiating diameter, and baffle width only when supported by evidence, while preserving any operator-declared enclosure choice.",
            "For a tweeter or compression driver, recommended_highpass_hz is the priority lookup.",
            "",
            "THE MINIMUM CROSSOVER FREQUENCY, AND HOW IT IS PUBLISHED",
            "recommended_highpass_hz is the manufacturer's minimum recommended crossover frequency for this driver. It is the single most important number in this reply: this build derives the driver's protective high-pass, the bottom of its allowed excitation band, and the bottom of its analysis window from it.",
            "Horn and compression-driver makers print it on a dedicated spec line. The exact wording varies — \"Recommended Crossover\" (B&C, BMS, 18 Sound), \"Minimum Crossover Frequency\" or \"Recommended min. crossover\" (FaitalPro, Celestion) — so match the meaning, not the phrase.",
            "Dome tweeters usually have no such line at all. Look instead at the test condition footnoted to the POWER HANDLING rating, which states the filter used: \"IEC 268-5, high-pass Butterworth, 2600 Hz, 12 dB/oct\" or \"X-over: 2. order HP Butterworth, 2.5 kHz\". That frequency is the answer.",
            "recommended_highpass_slope_db_per_octave is the slope CONDITION the manufacturer attaches to that frequency, reported separately. Convert a filter order to dB/octave: 2nd order is 12, 3rd is 18, 4th is 24. Send it only when the manufacturer states one — it is not universal, and some datasheets give the frequency with no slope at all.",
            "If the manufacturer publishes no minimum crossover frequency, send null for both and add an entry to unknowns. Absent is a correct answer here. Do NOT estimate this one: a safety margin is computed downstream by this build, and an invented number would be indistinguishable from a datasheet figure.",
            "The numbers in RESULT SHAPE below are format placeholders, not answers — recommended_highpass_hz especially. Send this driver's own published figure, or null. Never copy the example's number, and never badge a number you did not read on a datasheet as confidence \"high\".",
            "",
            "ESTIMATING THE REMAINING PROTECTION FIELDS",
            "required_protection_filters: send this ONLY for a mid, which needs a low-pass. A high-pass requirement is DERIVED from recommended_highpass_hz and must not be sent. cutoff_hz and minimum_slope_db_per_octave are both numbers, never null.",
            'A mid therefore adds exactly one entry, in this shape: "required_protection_filters": [{"kind":"lowpass","cutoff_hz":3000,"minimum_slope_db_per_octave":24,"family_or_equivalent":"equivalent_or_steeper"}]. No other role sends this key.',
            "hard_excitation_band_hz: the published usable range when there is one, otherwise the range typical for that type, tightened at both ends. Its LOWER edge is derived from recommended_highpass_hz, so what matters here is the upper edge.",
            "measurement_band_hz is the driver's published frequency-response range — for example a compression driver rated 1.0-18.0 kHz sends [1000, 18000]. Send the published range even when it extends below the minimum crossover; this build clamps the analysis window up into the allowed band itself.",
            "Nest the bands: the measurement band sits inside the hard excitation band. A reply that does not nest is refused.",
            "level_duration_limits: measurement-protocol discipline, not datasheet facts. Send max_sweep_duration_s 4, max_repeat_count 3, minimum_cooldown_s 2 unless a datasheet says stricter.",
            "max_effective_peak_dbfs is the one key in that object that IS a datasheet fact, so send it ONLY when the manufacturer publishes a level limit for this driver — a maximum input level, or a power rating stated as a limit you can convert. Omit the key entirely when they publish none; that is the ordinary answer and it is not a gap to record in unknowns. Never estimate it, and never send a protocol default in its place: this build chooses the measurement level from the driver's declared sensitivity against its low-frequency sibling's own limit, and a made-up number here would override that with a guess.",
            "",
            *_driver_research_prompt_limits(request),
            "RESULT SHAPE",
            "```json",
            "{",
            '  "artifact_schema_version": 2,',
            f'  "kind": "{DRIVER_RESEARCH_KIND}",',
            '  "request_fingerprint": "echo from TARGETS",',
            '  "drivers": [{',
            '    "target_id": "echo from TARGETS",',
            '    "target_fingerprint": "echo from TARGETS",',
            '    "role": "full_range|woofer|mid|tweeter",',
            '    "model": "echo manufacturer_and_model from TARGETS",',
            '    "nominal_impedance_ohm": 8,',
            '    "sensitivity_db_2v83_1m": 90,',
            f'    "usable_frequency_range_hz": [{hp - 1000}, 20000],',
            f'    "recommended_highpass_hz": {hp},',
            '    "recommended_highpass_slope_db_per_octave": 12,',
            f'    "hard_excitation_band_hz": [{hard_low}, 20000],',
            f'    "measurement_band_hz": [{hp - 1000}, 18000],',
            '    "level_duration_limits": {"max_sweep_duration_s":4,"max_repeat_count":3,"minimum_cooldown_s":2},',
            '    "cabinet": {"enclosure_kind":"sealed|vented|passive_radiator|open_baffle|transmission_line|unknown","radiator_count":1,"effective_radiating_diameter_mm":null,"baffle_width_mm":null},',
            '    "driver_class": "compression_horn|soft_dome|metal_dome|beryllium_diamond_dome|ribbon_amt|unknown",',
            '    "radiating_diameter_mm": 25,',
            '    "unknowns": ["facts that could not be established"],',
            # The high-confidence exemplar is deliberately NOT
            # recommended_highpass_hz: that field's example is a number this
            # build COMPUTED from its style table, so badging it
            # high/datasheet would teach the very mistake the prompt forbids.
            # sensitivity_db_2v83_1m is genuinely a datasheet line.
            '    "field_provenance": {"sensitivity_db_2v83_1m":{"confidence":"high","basis":"datasheet sensitivity line","source":"manufacturer datasheet","sources":["https://..."]},"level_duration_limits":{"confidence":"low","basis":"estimated: protocol default, no published limit","source":"measurement protocol, no published limit","sources":[]}},',
            '    "notes": "one short sentence",',
            '    "sources": ["https://..."]',
            "  }],",
            '  "crossover_candidates": [{"between_roles":["woofer","tweeter"],"frequency_hz":2500,"filter_type":"Linkwitz-Riley","slope_db_per_octave":24,"confidence":"low|medium|high","rationale":"why this point","warnings":[]}]',
            "}",
            "```",
            "",
            f"Return exactly {target_count} {entries} in drivers[] — one per target above, in the same order. Copy target_id, target_fingerprint, and model verbatim.",
            "",
            "KEY GUIDE",
            "- Every numeric key names its own unit (_hz, _ohm, _db, _mm, _s, _dbfs).",
            "- All numbers are bare JSON numbers. No units, no comments, no text after a value.",
            '- Confidence vocabulary is "low", "medium", "high", or "unknown".',
            "- field_provenance covers only these five keys: "
            + ", ".join(_PROMPT_PROVENANCE_KEYS)
            + ". Each entry is confidence + basis (12 words or fewer) + source (one citation, 12 words or fewer) + at most 2 source URLs.",
            "- sources: at most 3 URLs you actually consulted for that driver.",
            "- notes: one sentence, 15 words or fewer.",
            "- crossover_candidates[].filter_type is one of: "
            + ", ".join(supported_declaration_filter_types())
            + ". crossover_candidates[].slope_db_per_octave is one of: "
            + ", ".join(
                f"{slope:g}" for slope in supported_declaration_slopes_db_per_octave()
            )
            + ". Anything else is refused when the reply is saved. This is the "
            "crossover this build compiles, and is a different question from "
            "the slope condition a datasheet states for "
            "recommended_highpass_slope_db_per_octave above.",
            "- crossover_candidates[].rationale: 15 words or fewer.",
            "",
            "STOP",
            'No introduction ("Here is the JSON:"), no summary, no caveats outside the object, nothing after the closing fence.',
            "Begin the ```json block now.",
        )
    )
