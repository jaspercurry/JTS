# Preference EQ

> **Status: distilled from 2026-05-25 deep-research intake.**
> Preference EQ is taste shaping, not room correction. It can share
> the DSP backend, but the UI, bundle metadata, and agent language
> must keep the distinction clear.

## Operational Summary

Room correction asks, "What repeatable speaker/room behavior should
we compensate?" Preference EQ asks, "What does this listener want to
hear?" A future JTS tuning flow should let the user play familiar
music, describe what they hear, A/B changes, and keep every preference
move reversible.

## Subjective Language Map

| User says | Likely area | Safe first action | Ask before changing |
|---|---|---|---|
| "More bass" | 20-120 Hz shelf, or 80-150 Hz punch | low-shelf +1 to +3 dB in preference layer | More rumble or more drum/bass-guitar punch? |
| "Less boomy" | 60-200 Hz modal excess or decay | inspect room-correction peaks/decay; broad or modal cut only if measured | Lingering bass, or just too much bass overall? |
| "Brighter" | 4-16 kHz shelf or gentler downward tilt | high-shelf +1 to +2 dB, or reduce target tilt | More sparkle/air or more vocal clarity? |
| "Warmer" | More 100-300 Hz, less 4-8 kHz, or steeper tilt | warmer target preview; small broad shelf/cut | Fuller body or less edge? |
| "Vocals recessed" | 1-4 kHz presence or masking below | gentle broad presence preview, or cleanup masking LF/low-mid excess | Male vocals, female vocals, or all dialog? |
| "Harsh" | 2-6 kHz excess, reflections, distortion, or recording | small broad cut only after context; avoid narrow blind cuts | Cymbals, sibilance, guitars, or fatigue at loud levels? |
| "Thin" | 80-250 Hz deficiency or too much presence | small low-mid/bass shelf preview | Lacking weight at all volumes or only quiet listening? |
| "Muddy" | 150-500 Hz excess or bass decay | inspect low-mid and decay; broad cut preview | Bass/drums muddy, or voices/instruments blending? |
| "Too much treble" | 6-16 kHz shelf or too-flat target | high-shelf -1 to -3 dB or steeper target tilt | Piercing "S" sounds or overall thinness? |

These mappings are heuristics. JTS should treat them as reversible
preference moves unless measurements independently support a physical
room-correction cause.

## Agent Rules

- Say whether a proposed move is correction or preference.
- Prefer small, reversible adjustments.
- Offer A/B listening.
- Do not call a preference curve "more accurate."
- Do not stack preference EQ into the room-correction layer without
  recording the distinction in the bundle/profile metadata.
- Emit high-level bounded intent, not filter coefficients. Example:
  `{"action":"preference_low_shelf","corner_hz":100,"gain_db":1.5}`.
- Let deterministic code clamp gains, Q, headroom, and ordering.
- Ask a clarifying question when the phrase could mean two different
  frequency regions, such as "more bass" vs "more punch."

## JTS Design Implication

Use one DSP/profile backend with separate layers:

1. base passthrough
2. measurement-derived room correction
3. target / house curve
4. user preference EQ

That stack lets the user reset taste without deleting room correction,
or compare two target curves against the same measurement.

Preference EQ should be chainable after room correction and before
the always-on limiter/headroom guard. It should have its own profile
ID, history, and bypass switch so "I liked it better before" is
always recoverable.

## Sources

- 2026-05-25 deep-research reports.
- B&K listening-curve descriptors and room-curve history.
- Toole / Olive preference and timbre literature.
- Audio engineering frequency-zone taxonomies, used only as
  practical vocabulary, not as physics proof.

Last verified: 2026-05-25
