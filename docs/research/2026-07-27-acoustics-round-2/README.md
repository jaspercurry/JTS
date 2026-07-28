# Research — acoustics round 2 (2026-07-27)

Verbatim results of two owner-run deep-research sessions, preserved as
primary sources for the round-2 work orders. Prompts and laptop-side
evidence live in `captures/gate-research-20260727/` and
`captures/room-correction-research-20260727/` (gitignored).

- [`01-gating-v2.md`](01-gating-v2.md) — robust acoustic gating for
  the speaker-measurement instrument: prior-art survey (MLSSA / CLIO /
  ARTA / REW / Klippel; seismology arrival-picking), the
  artifact-vs-real-vs-source-fixed discriminator, group aggregation,
  anomaly policy, the 1/T validity model, frequency-dependent gating,
  session invariants. Adopted (with architect deltas) by
  [`docs/gating-v2-plan.md`](../../gating-v2-plan.md) (issue #1790).
- [`02-room-correction-competitive.md`](02-room-correction-competitive.md)
  — competitive room correction: the industry survey (Dirac /
  Audyssey / Trinnov / Lyngdorf / Anthem / Genelec / Neumann /
  Trueplay), bandwidth doctrine, the two-instrument attribution
  boundary, target ownership, LF boost policy, phase/FIR verdict,
  spatial protocol. Adopted (with architect deltas) by
  [`docs/room-correction-regime-plan.md`](../../room-correction-regime-plan.md)
  (issue #1791).

These are point-in-time research artifacts: cited product behavior and
forum evidence reflect early 2026 and are not maintained. The adopted
decisions, as amended by their reviews, live in the two plan docs —
read those for current doctrine.
