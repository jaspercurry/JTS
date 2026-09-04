# The tuning toolbox — target shape (2026-09-04)

One page a new LLM reads to know the whole toolbox. Everything below is
either already true at HEAD or is a row in `PLAN-FRESH-EYES.md`.

## The tools (ten binaries, one verb family each)

| Binary | Question it answers | Inputs | Artifact it writes | Authority | Step |
|---|---|---|---|---|---|
| `jasper-declare-geometry set\|show` | What rig am I measuring on? | heights, distance, ceiling | `/var/lib/jasper/measurement_geometry.json`, frozen per round as `declared-geometry.json` | advisory | §0 |
| `jasper-seat-level` | What volume is the measurement reference? | target dB SPL, tolerance | `/var/lib/jasper/active_speaker_seat_level_reference.json` | measured | §1a |
| `jasper-angle-capture plan\|stage\|withdraw\|serve` | Which poses will the next round walk, and who moves the mic? | program, size, `--mover human\|turntable` | staged walk; `position_cycle.json` on bank | mutating (`stage`/`serve`) | §2 |
| `jasper-measure` | Measure this speaker once at one placement | `MeasureSpec` flags or file | banked takes with ids | measured | §2 |
| `jasper-null` | What cancels at each delay coordinate, played for real? | fc, delays, polarity | `<round>/null_runs/*.json` | measured | §4 |
| `jasper-round open\|wait\|bank\|apply` | Run, wait on, bank and apply one round from the box | tier, stage, door documents, fingerprint | the round directory; `apply.json` | mutating-with-gates | §2, §3, §4, §7, §10 |
| `jasper-crossover-prescriber status\|packet\|propose\|stage` | Where does this speaker stand, what did the round say, is my prescription admissible? | round dir, prescription JSON | `packet.json` beside the round; the staged slot | advisory (`stage` mutates) | every step |
| `jasper-round-views <view>` | One question about a banked round, answered small | round dir, per-view flags | one named JSON per view (`ARTIFACT_BY_VIEW`), incl. `delay_landscape`, `delay_confirmation`, `gate_sweep`, `feature_classification`, `harmonic_distortion`, `close_reference`, `forward_model` | advisory | §1c, §4, §6, §6a, §7, §10 |
| `jasper-basic-profile review\|apply` | Put the declared crossover on, nothing else | — | live tune | mutating-with-gates | §3 |
| `jasper-audition start\|stop\|status` | What does the correction layer sound like, by ear? | layer | runtime only | mutating (runtime) | §8 |

Retired into those ten: `jasper-arm-walk` → `angle-capture serve`;
`jasper-round-bank` → `round bank`; `jasper-delay-sweep`, `jasper-gate-sweep`,
`jasper-classify-features`, `jasper-read-distortion`, `jasper-close-reference`,
`jasper-forward-model`, `jasper-project-ring` → `round-views` views (the ring
projection becomes the default `--dumps`). `jasper-active-speaker` stays a
commissioning and deploy binary, off the tuning menu by design.
`scripts/run-crossover-round.py` becomes a laptop transport for `jasper-round`,
not a second implementation.

## Conventions every tool obeys

- **One exit vocabulary**, owned by `jasper/cli/_refusal.py`: 0 ok, 1 refused,
  2 unreadable, 3 unwritable. A transport-shaped verb (`round`) adds nothing
  above 3; it reports transport loss as `unreadable` with `reason=transport`.
- **One refusal shape**: stdout `{"status", "reason", "detail"}`, one sentence
  on stderr, status and code always agree. The `--json`-gated `{"ok": false}`
  shape is retired.
- **One artifact rule**: a tool that computes writes one named JSON beside the
  round (`ARTIFACT_BY_VIEW` names it), defaulting there; `--out` overrides.
  Nothing that computes is stdout-only.
- **Answer small, pull depth on demand**: a tool's stdout is a summary under
  ~120 lines of JSON with no curve arrays; curves live in the artifact, and the
  next verb to run is printed as a ready command line (the `delay-sweep
  propose` pattern). `inventory` names what a round is missing.
- **One `--help` style**: `prog`, one-sentence description, `AUTHORITY_TIER`,
  subcommands with the same flag names for the same inputs (`<round-dir>`,
  `--out`, `--dumps`, `--state`). The runbook menu is generated, never edited.

## The round directory as memory

After bank: `info.json`, `declared-geometry.json`, `round_receipt.json`
(four verdicts and the adoption row), `position_cycle.json`, the take records
(each carrying `phase` and `phase_composition`), `repeat-floor.json` when
banked. After analysis: the per-view artifacts above. After prescribe:
`packet.json`, `proposal.json` (with `expected_delta` and `declared_tilt_db`
when the LLM states them), `delay_confirmation.json` after §4. After apply:
`apply.json`, `applied-profile.json` with `LinearizationState` carrying
`committed_side` and `anchor_drift_db`. Session-crossing facts live in those
fields, never in the LLM's context: expected delta, declared tilt, phase
composition, σ kind (each σ names `kind`), the §6a rung reached
(`close_reference.json`'s verdict).

## Boundaries, as an import rule

`audio_measurement` imports no `active_speaker`, `correction`, `web`, `cli`
(pinned: `tests/test_runtime_import_closure.py`). `active_speaker` imports no
`web`, `cli`, `correction` (pinned for `correction` and for `crossover_v2`→`web`
in `tests/test_correction_boundary_ssot.py`; extend to all of `active_speaker`).
`cli` imports no `web` (new row). `correction` imports `active_speaker` only
from `runtime_safety.py` (new allowlist row). `web` and `cli` import the engine;
the engine never imports a surface.

## Out of scope by design

Room correction (`jasper/correction/`, the PEQ wizard, `jasper-correction-bundle`,
`calibration_agent`), the benches (`emit-bench`, `bass-extension-bench`,
`bench/`), the voice stack, and the commissioning binary
`jasper-active-speaker`. The methodology attributes room effects and prescribes
nothing there.
