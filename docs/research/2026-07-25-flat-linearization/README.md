# Research: robust measurement under early boundary reflections + the flat spec (2026-07-25)

One owner-commissioned deep-research artifact behind the flat-linearization
program plan in
[`flat-linearization-plan.md`](../../flat-linearization-plan.md).
Preserved verbatim as a primary source; the plan carries the adopted
synthesis and wins where they disagree. Commissioned after offline comb
forensics on the 2026-07-24/25 JTS3 session found a ~0.31 ms boundary-bounce
echo contaminating every existing capture (evidence corpus:
`captures/flat-linearization-20260725/`, laptop-durable, gitignored).

1. [`01-robust-measurement-and-flat-spec.md`](01-robust-measurement-and-flat-spec.md)
   — five questions: how shipped auto-cal products neutralize early boundary
   reflections (spatial power averaging everywhere; nobody removes the
   bounce from one capture); the combining estimator (power mean primary,
   median cross-check, max-hold rejected; sinc(kr) decorrelation, ~8–12
   captures for ±1 dB); cepstral/parametric echo removal (academic-only —
   detection, never removal); what "flat" should mean (CTA-2034 Listening
   Window, ±1.5–3 dB staged tolerances, non-minimum-phase dips excluded
   from correction and metric); and closed-loop measure–correct–remeasure
   practice (re-measure at target SPL; thermal power compression as a
   candidate mechanism for realized-lift shortfall).

Known errata (caught in the plan's adversarial review, corrected in the
plan, left verbatim here): US 8,130,966 is assigned to Performance Media
Industries, Ltd., not Harman.

Last verified: 2026-07-25
