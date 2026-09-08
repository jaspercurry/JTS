# Tuning master plan — reusable speaker toolbox

This plan records the program's scope and stable ruling identifiers. Start a
measurement from the [runbook entry contract](tuning-operator-runbook.md#entry-contract).
The [doctrine](measurement-loop-doctrine.md) owns authority and layer boundaries;
[methodology](tuning-methodology.md) is optional science guidance. Tool schemas,
help, and code own current implementation details. Historical plans do not
establish what is implemented at HEAD.

## Product scope

- Commissioning produces a usable base tune: declared routing/topology,
  crossover, required protection, trims, and applicable delay/polarity.
  Linearization and room correction are optional enhancements.
- The LLM chooses experiments from evidence. Code owns calculation, validation,
  graph composition, capture, and cleanup. The human places the mic, starts
  pose batches, reports physical changes, and judges listening.
- A linearization baseline contains the base alone. Candidate measurements add
  only deliberate changes. Preference EQ and room correction are excluded by
  graph composition from every linearization capture.
- Each round can be the last or lead to another use of the same tools. There is
  no campaign round limit, forced final round, plateau stop, or compulsory score.
  Physical protection and per-operation resource limits remain.
- Same-design candidate batches compare filters, candidate-specific trims,
  delay, and polarity at one held pose before asking for the next placement.
  Summed captures answer combined-speaker questions.
- Preserve useful parts of losing candidates with source references. A child
  candidate has its own identity and is unmeasured until the combination runs.
- Adoption is explicit by candidate identity. Temporary measurement playback
  restores normal state without adopting a saved tune.
- Keep room correction working above the accepted speaker tune. Its expansion,
  an embedded adviser, and unrelated bass work are outside this implementation.

## A reusable flow, not a workflow engine

One possible method is base tune → baseline measurements → analysis → candidate
measurements → analysis/recombination → another useful measurement or explicit
adoption. It can end at any sufficient round. A losing/restored trial contributes
evidence for another candidate; it does not exhaust permission to continue.

For a comparison, hold the declared corner, filter family, slope, and topology
fixed. Use supported design paths for different base designs with correctly
derived driver protection. Offline complex-solo predictions can guide an
experiment; they neither replace summed measurements nor veto safe tests.

Inspect available artifacts and run useful enrichment before freezing the packet
used for a prescription. `propose` and `stage` reuse that same snapshot. New
evidence requires a new snapshot and matching prescription. Optional notes hold
questions and decisions with evidence references, not a second set of numbers.

## Substrate ownership

| Concern | Owner |
|---|---|
| Program and pose definitions; capture cost | `measurement_programs.py`, `angle_capture` request contract |
| Candidate contents and identity | Existing candidate bank and prescription contracts |
| Per-take execution | Shared protected execution path; web and CLI are adapters |
| Numerical analysis | `jasper/audio_measurement/` and existing round views |
| Valid captures and failure records | Session bundles and round bank |
| Frozen evidence view | `evidence_packet.build_crossover_evidence_packet` |
| Durable measured apply | `handle_v2_apply` |
| Temporary graph activation and restore | `crossover_v2/session_graph.py` |
| Tool discovery and help | CLI definitions; generated runbook menu |
| Reasoning and next human action | Optional round note with artifact references |

Keep one writer per fact. Retain valid takes and make interruptions visible.
Banked history must remain discoverable after live-session retention. Resolve
large artifacts on demand; do not add a memory service, intervention database,
automatic summarizer, or permanent handoff-document tier.

## Decision register

R-numbers are stable because code and records cite them. These concise rulings
replace obsolete implementation inventories; inspect current code for status.

| # | Ruling |
|---|---|
| R1 | Crossover design is declared, not selected by a measured search engine. Keep offline `forward_model` calculations. |
| R2 | Validate the supported filter vocabulary at entry. Butterworth expansion is separate work; do not infer support from this plan. |
| R3 | Use one operator-notes field. Quantitative declarations belong in typed fields only when they feed calculation or protection. |
| R4 | The operator is the external LLM. Do not build a new embedded-adviser UX or provider platform. |
| R5 | Code computes; the LLM interprets. Simulation and quality forecasts inform experiments, not authorization. |
| R6 | Select named measurement programs with bounded parameters. Capture provider and mic-position provider are independent. |
| R7 | Compile full candidates without adoption; run complete consecutive trials per pose through protected playback, readback, and restoration. The initial contract holds corner/family/slope/topology fixed. |
| R8 | Driver boost uses declared bands, per-filter/composed limits, headroom, excitation protection, and existing adoption rules. Blend's `BOOST_ROUTE_UNAVAILABLE` stays: that stage has no boost headroom term and a summed deficit does not identify a driver to boost. |
| R9 | Mic WAV and reproducible stimulus are evidence. Derived IR/complex-response caches may be pruned. Active rounds are protected from ring eviction; the campaign bank is operator-pruned. |
| R10 | Join relevant existing feature/evidence identities without a new mechanism platform. Keep observation, expected effect, and interpretation distinct. |
| R11 | Dependencies are expressed by artifact contracts, not a compulsory workflow. Status uses the same readers as the tools. |
| R12 | Remove dead routes, duplicate schemas, stale prose, and source-text tests where behavior tests cover the contract. |
| R13 | Preserve room correction and its layer boundary; finish speaker linearization before expanding room work. |
| R14 | Manual placement and the attached turntable use one capture engine and record model. Analysis remains separate on-demand verbs. |

## Measurement program constants

Code is the single source for pose counts, repeats, capture ceilings, excitation
caps, and timeouts. `jasper-angle-capture plan` reports actual cost before staging.
`measurement_programs.py` owns program geometry; the current registry and tool
help determine which programs are supported. A name in a plan is not a feature.

Drive level is relative to the calibrated measurement anchor. The preset's
commissioning SPL ceiling remains binding. `seat_level.py`, the calibration
record, and the excitation plan own the numeric limits. An additional level can
help diagnose level dependence only within those limits; there is no compulsory
level escalation.

The methodology gives optional distance, gate, and alignment guidance. Choose
poses that answer the goal and disclose absent axes. Batch size and repeat count
price a particular experiment; neither sets a campaign's number of rounds.

`repeat-floor` can measure random variation from existing repeats. Report a
banked floor versus an assumed threshold honestly. Plateau and uncertainty
numbers help the LLM assess value; they do not terminate iteration automatically.

## Deferred work

Alternative excitations, nearfield splicing, electrical impedance instruments,
a generalized crossover optimizer, new causal inference, sustained thermal
compression tests, and new room/adviser platforms are outside this toolbox.
A gate or harmonic analysis cannot claim information its capture lacks.

Optional calibration studies can establish repeatability, useful vertical angle
spacing, or splice uncertainty when those questions matter. Each is a bounded
experiment with its own evidence and human action, not a campaign prerequisite.

The prior productization, attribution, linearization, and crossover campaign
records remain in `docs/historical/`. Their dates and code references matter;
re-derive a subsystem fact at HEAD before relying on an old status claim.
