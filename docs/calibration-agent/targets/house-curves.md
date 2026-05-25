# Target And House Curves

> **Status: distilled from 2026-05-25 deep-research intake.**

## Operational Summary

A measured-flat in-room response is not necessarily perceived as
neutral. Many preferred in-room targets slope downward from bass to
treble, with the exact slope depending on speaker directivity, room
absorption, listening distance, and taste.

The target curve is not the same thing as physical room correction.
Room correction asks how to move a measured response toward a target
without violating acoustic limits. The target itself is a design and
preference choice.

## What JTS Does Today

`jasper.correction.target` exposes `flat`, `warm`, and `bright`
targets. The current PEQ designer only acts in the modal/bass region,
so target differences above that range are mostly visual guidance
until future FIR/preference layers exist.

## Research Consensus

- A flat anechoic/direct response in a typical room usually produces
  a downward-sloping steady-state in-room response.
- Forcing the measured in-room response to be flat can make direct
  sound too bright.
- B&K-style and Harman/Toole-style targets converge on some bass
  support plus a gentle downward slope, but no single slope is
  universal.
- Listener preference varies most in bass quantity and overall tilt;
  that makes bass shelf and tilt controls good first-class UI
  concepts.
- Directivity and room absorption matter. A dead room or high-directivity
  speaker may need a flatter in-room target than a lively room or wide
  radiator.

## Candidate Target Families

| Target | Intent | Notes |
|---|---|---|
| Flat reference | Measurement/debug baseline | Not necessarily preferred in-room. |
| B&K-like | Gentle traditional listening-room curve | Slight low-frequency support and modest treble roll-off. |
| Harman/Toole-like | Modern preferred in-room family | Downward slope with listener-adjustable bass. |
| Warm | More bass / lower-mid weight or steeper tilt | Preference layer, not "more correct." |
| Bright | Gentler tilt or modest high shelf | Preference layer; avoid narrow HF fixes. |
| Custom | User/expert curve | Must be stored as data with provenance. |

## JTS Design Rules

- Store target curves as data, not scattered code constants.
- Record the active target in every correction bundle.
- Keep target changes reversible and comparable against the same
  measurement.
- Surface target choice separately from "room problems fixed."
- Do not let an LLM describe one house curve as objectively correct
  for all listeners.

## Open Questions

- What slope ranges are well-supported by Harman / Olive / Welti and
  B&K-style research?
- When should JTS recommend bass-only correction with a separate
  house-curve preference layer?
- How should the UI explain that "warmer" and "brighter" are taste
  choices, not universal accuracy?
- How should speaker directivity affect target recommendations?
- What target family is most appropriate for a single-box smart
  speaker versus two-channel hi-fi?

## Sources

- Toole, *Sound Reproduction*, 3rd ed.
- Olive 2013, AES 8994.
- B&K Application Note 17-197.
- [HouseCurve target curves](https://housecurve.github.io/docs/tuning/target_curve.html)
- [REW target settings](https://www.roomeqwizard.com/help/help_en-GB/html/eq.html)
- 2026-05-25 deep-research reports.

Last verified: 2026-05-25
