# Prior Art And Tooling

> **Status: distilled from 2026-05-25 raw research archive.** This is
> a map of relevant public/open/commercial workflows, not an
> endorsement or dependency list.

## Operational Summary

JTS should learn from existing audio tools without turning into a
wrapper around any one of them. Prior art is most useful when it
teaches workflow boundaries: what deterministic DSP owns, what the UI
must show, what artifacts must be stored, and where automated
correction becomes unsafe.

## Room Correction / FIR

| Tool / family | What to learn | JTS posture |
|---|---|---|
| REW | Swept-sine capture, impulse inspection, EQ limits, alignment tooling, file formats, possible local API. | Interop first; do not clone everything. |
| rePhase | Manual linear-phase / phase-correction FIR workflow, REW-to-FIR handoff. | Expert reference for future FIR export/import. |
| DRC-FIR / DRCDesigner | Frequency-dependent windowing, target shaping, ringing control. | Strong algorithmic reference; dependency decision later. |
| Acourate / Audiolense | Mature FDW/FIR workflows, measurement discipline, verification UX. | Commercial reference, not dependency. |
| Dirac / Trinnov / Anthem ARC / Audyssey | Commercial examples of mixed-phase, spatial averaging, clustering, and bounded automation. | Study guardrails and UX; do not copy black-box claims. |
| Anti-Mode | Conservative low-frequency correction precedent. | Reinforces bass-first restraint. |
| Sonarworks SoundID | Target/profile management and pro-user workflows. | Usage claims need source caveats. |
| Genelec GLM / GRADE, Neumann MA 1 | Model-aware correction and reporting workflows. | Relevant to future JTS-known hardware profiles. |
| HouseCurve | Phone-first measurement UX and target curve editing. | Useful mobile UX reference; not enough for crossover timing. |

## Active Speaker / Crossover Commissioning

| Tool / source | What to learn | JTS posture |
|---|---|---|
| VituixCAD | Near/far merge, directivity, listening window, sound power, DI, active crossover simulation. | Canonical external design tool for early active-speaker workflow. |
| Linkwitz / Riley / Vanderkooy / Lipshitz literature | Non-coincident driver crossover behavior, lobing, polarity, delay, power response. | Preserve as design theory for active baseline work. |
| Charlie Hughes / Voice Coil articles | Measurement geometry, rotation center, off-axis sampling density, directivity-first crossover optimization. | Important for acceptance gates. |
| Rod Elliott / Purifi acoustic-center cautions | Acoustic center is not just a static voice-coil coordinate. | Use measured delay/null validation, not geometry-only delay. |
| miniDSP active-speaker notes | Tweeter protection capacitor and active-routing safety practice. | Hardware-safety reference. |
| Klippel methodology | Protection logic, stimulus discipline, driver measurement rigor. | Methodology reference even without Klippel hardware. |
| Hypex Filter Design | Active-speaker tuning UI patterns, per-channel biquad/FIR/limiter workflow. | UI reference for a future expert surface. |
| `pyCamillaDSP`, `camillagui`, `pyCamillaDSP-plot` | CamillaDSP control/visualization ecosystem. | Prefer interop before custom tooling. |
| `wirrunna/CamillaDSP-Building-a-Config` | Practical active multi-way CamillaDSP config workflow. | Good source for template and validation ideas. |
| `mdsimon2/RPi-CamillaDSP` | Raspberry Pi ALSA/CamillaDSP deployment practice. | Operational reference for active hardware experiments. |

## Gotchas Preserved From Reports

- REW impulse exports may include pre-peak padding; bundle importers
  must preserve timing metadata rather than trimming blindly.
- A tool can optimize on-axis magnitude while making vertical
  directivity worse.
- Commercial tools often hide their spatial averaging and phase
  constraints; JTS should expose its assumptions.
- Phone-first UX is valuable for room correction but not sufficient
  for active crossover phase alignment.
- Model-specific speaker profiles are a long-term advantage for JTS
  hardware; generic room-correction tools cannot know the driver
  baseline.

Last verified: 2026-05-25
