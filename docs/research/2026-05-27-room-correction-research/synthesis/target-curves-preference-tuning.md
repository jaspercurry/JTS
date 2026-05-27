# Target Curves And Preference Tuning - Synthesis

> **Status: research synthesis.** Distilled from
> [`../raw/target-curves-preference-chatgpt.md`](../raw/target-curves-preference-chatgpt.md)
> and
> [`../raw/target-curves-preference-claude.md`](../raw/target-curves-preference-claude.md)
> on 2026-05-27. This is not current operational truth; use it to
> guide design and documentation.

## Bottom Line

The reports converge on a strong product architecture:

1. Measurement-derived room correction fixes repeatable physical
   behavior, mostly bass/modal behavior.
2. Target or house curve is an editable preset voicing, not measured
   truth.
3. Preference EQ is a reversible user delta applied after correction
   and target.
4. Headroom management and level-matched A/B are mandatory.
5. The AI assistant may express and explain intent, but deterministic
   code owns filters, bounds, and deployment.

This matches the `/sound/` direction already taken in JTS. The main
change to carry forward is conceptual clarity: Harman-style, B&K-style,
and similar curves are best represented to users as sound-curve
presets that can be applied, compared, and edited, not as a hidden
technical subparameter inside room correction.

## What The Reports Agree On

- There is no single universal in-room target curve.
- Listeners generally prefer smooth in-room balance with modest bass
  rise and a gentle downward tilt toward treble.
- Listener variation is real, especially in bass preference.
- A flat anechoic/direct speaker target and a downward-sloping in-room
  preference target are different things.
- Toole-style caution matters: a measured room curve is a result of
  loudspeaker, room, and measurement method. It is not automatically a
  target.
- Room correction should not chase loudspeaker directivity problems,
  reflections, or high-frequency comb filtering.
- Preference controls should be bounded, reversible, and safe for
  headroom and driver excursion.

## Important Numeric And Literature Details

These values should be treated as starting-policy proposals, not
universal standards:

- Default target: modest bass lift plus gentle tilt. Reports mention
  roughly +3 dB low shelf around 105-120 Hz and about -0.7 to -1.0
  dB/octave tilt as defensible defaults.
- Bass preference clusters from Olive headphone work are repeatedly
  cited: about 64% near Harman, about 15% preferring +4 to +6 dB more
  bass, and about 21% preferring roughly 2 dB less bass. This is strong
  evidence that bass preference segmentation matters, but it is not
  direct speaker-room proof.
- Correction range proposals differ slightly. One report suggests
  default room correction mostly 20-300 Hz, optional extension to 500 Hz
  with broad stable features. The other suggests 20-500 Hz with soft
  taper to 1 kHz. JTS should continue to let strategy/confidence decide
  this rather than hard-coding one global number.
- Preference EQ novice bounds cluster around:
  - shelves up to +/-6 dB
  - peak boosts no more than +3 dB
  - cuts often allowed deeper than boosts
  - Q roughly 0.3-4.0 for user preference edits
  - automatic preamp/headroom reserve equal to max positive gain plus
    margin

## UX Interpretation

For normal users:

- Present sound curves as presets: Flat, Harman-style, B&K-style, and
  future named profiles.
- Let users apply a preset, then tweak Bass/Mid/Treble.
- Keep the language simple: "sound curve" or "profile" is fine.
- Do not teach users that Harman or B&K are objective room-correction
  answers.
- Always provide EQ on/off and compare/proposed-state toggles that are
  level matched.

For power users:

- Show the layer graph: room correction, target curve, preference EQ,
  headroom, and optional loudness.
- Expose target parameters: bass shelf, tilt, treble shelf, correction
  range, boost ceilings.
- Expose advanced parametric preference bands separately from measured
  room filters.
- Preserve import/export and provenance tags.

For the AI helper:

- The assistant should emit intent objects or bounded profile edits,
  not raw CamillaDSP biquad coefficients.
- The assistant should be able to explain "this is preference voicing"
  versus "this is a measured room correction."
- User phrases such as "more bass," "less boomy," "brighter,"
  "vocals forward," "harsh," "thin," and "muddy" can map to safe
  broad EQ moves, but the mapping is product policy and should be
  logged/reviewed over time.

## Layer Model Recommendation

The best JTS signal model is:

```text
source
  -> headroom reserve
  -> room correction
  -> target / house curve
  -> preference EQ
  -> optional loudness compensation
  -> protection / limiter
```

JTS currently composes room PEQs before `/sound/` preference shaping.
That is good. The next design task is making the target/preset layer
visually and semantically clear without collapsing it into room
correction.

## Sound Curve Versus Room Correction

The reports support the user's intuition that a sound curve behaves
like a stock EQ profile in the product experience. The technical nuance
is that a target curve also influences how the room-correction solver
judges "error" against the measured response.

Recommended JTS behavior:

- A user can select a sound curve without running room correction.
- If the user later runs room correction, the selected target/curve is
  pre-populated as the target profile.
- The room solver still stores its filters as measurement-derived room
  filters.
- The selected curve remains editable after correction.
- The graph should show separate traces where possible:
  - measured response
  - room correction effect
  - selected target curve
  - preference delta
  - combined predicted response

This keeps the mental model simple for users and the DSP contract clean
for code.

## A/B And Rollback Requirements

The reports are emphatic that A/B must be level matched. Otherwise the
louder option tends to win and the comparison becomes misleading.

Recommended compare states:

- EQ off: bypass preference shaping, preserve room correction if
  enabled.
- Current: saved room + target + preference chain.
- Proposed: current chain plus pending AI/user edit.

Future power-user states can add:

- no DSP
- room correction only
- room correction plus target
- full chain

Every preference profile should be versioned with parent revision,
author (`user`, `assistant`, `wizard`), target/correction IDs, headroom
reserve, and a short rationale.

## Safety Bounds

The reports support these conservative policies:

- No narrow positive boosts in preference mode.
- No boosting spatially inconsistent nulls.
- Total positive gain must trigger automatic negative preamp/headroom.
- Bass boosts need device-specific protection because small speakers can
  run out of excursion quickly.
- Preference filters should be few and broad in novice mode.
- Loudness compensation, if built, is a separate level-dependent layer
  and should default off.

## Prior-Art Lessons

The reports cite a mix of research, vendor docs, and community tooling.
Treat the exact vendor details as re-verification targets, but the
product pattern is consistent:

- Dirac exposes target editing.
- Sonarworks separates flat/reference from custom/translation targets.
- Genelec GLM separates AutoCal from Sound Character Profiler.
- Roon and similar DSP stacks expose headroom management when users add
  positive gain.
- WiiM/HouseCurve-style consumer flows frame target curves as selectable
  presets with user editability.

The lesson for JTS is not to copy any one UI. It is to preserve layer
separation while giving normal users a preset-and-tweak experience.

## Implementation Implications For JTS

- Keep `/sound/` import-cheap and usable without room correction.
- Keep the `SoundProfile` contract semantic and bounded.
- Add richer profile history before making the AI helper powerful.
- Store target curve selection and preference delta separately even if
  they compile into one generated CamillaDSP YAML.
- Add a level-matched proposed-profile compare path before letting the
  assistant apply iterative edits.
- When room correction uses a selected target, persist both the target
  identity and the exact compiled target curve in the correction bundle.

## Open Questions To Verify

- Exact current behavior and file formats for consumer preset EQs in
  Nothing, WiiM, Sonarworks, Dirac, Roon, and HouseCurve if we borrow
  UX language.
- Whether JTS should ship "Harman-style" and "B&K-style" labels or use
  more descriptive names with citations in an advanced/details panel.
- Device-specific bass safety model for the actual JTS speaker builds.
- Whether telemetry/ratings should be opt-in only and how much of the
  preference-learning loop belongs in open-source defaults.
- Best graph design for showing target curve plus user EQ without
  intimidating non-audiophiles.

Last synthesized: 2026-05-27
