# ADR-0192: The campaign is the validation

- **Date:** 2026-08-29
- **Status:** Accepted

## Context

Acceptance row 9 (`REFACTOR-TUNING-2026-08.md:1359`) gated row 10 on
reproducing the old `postfix-baseline-2026-08` campaign on the new engine,
inside that campaign's own measured noise floor — worst round-to-round change
≤ 0.37 dB, 16/16 captures with the fader held, 0 glitched captures. Row 10
(`:1360`) is the owner's real acceptance bar — *"the full candidate campaign
— many candidates, each measured, one winner, and the winner re-measured"* —
scoped to open only after row 9 closed, beginning with the instrument roster
(R-1…R-6, `:1405-1413`) in rank order. Chunk 3's own audit
(`cutover-briefs-acceptance.md` §1) found row 9's pass/fail instrument is not
a shippable tool and flagged three gaps in running it at all; the roster
still carries two unbuilt mic-only rows (R-1, R-3, R-4); and the "tuning
constants (PROVISIONAL pending W6 bench validation)" banner
(`REFACTOR-CUTOVER-2026-08.md:632,1073,1211`) still hangs on the constants
row 9/10 measure against, quoted as still-standing by ADR-0181 and ADR-0182.
The owner reviewed the endgame shape in chat on 2026-08-29 rather than let
the campaign wait behind a rehearsal of an instrument it no longer trusts.

## Decision

Owner ruling, 2026-08-29, in chat, five parts.

**1. Rows 9 and 10 merge — the campaign IS the validation.** The owner's
reasoning, in spirit: the old `postfix-baseline-2026-08` measurements are not
a reference — *"the old ones were shite"* — and the new system replaced the
measurement instrument itself; the program is never going back to the old
one. Reproducing that baseline inside its 0.37 dB floor therefore proves the
wrong thing: that the new engine can numerically match a discarded yardstick,
not that it tunes the speaker correctly. **Row 9's 0.37 dB reproduction bar
is retired as an acceptance gate.** The hours go instead into flushing bugs
out of the new system by running the real campaign on real hardware — issues
found along the way get fixed along the way, rather than gated behind a
rehearsal of the old measurement first.

What survives from old row 9 is its **mechanics**, not its purpose,
re-scoped as the campaign's **day-1 preflight**: deploy, with 7j
(`b56ea4257`) as an ancestor of the running build; `jasper-doctor`; the
five-minute gate probe — one doctor run plus one attempted session open
(`cutover-briefs-acceptance.md:447-450`) answers which of the two
`driver_style` gates is actually blocking the box, rather than guessing from
paper; S11 act 5, the 7j demotion verification; and the re-mint. This is the
same dependency chain `cutover-briefs-acceptance.md` §4 already worked out
for old row 9's bench evening (deploy → act 5 → re-mint) — the order does not
change, only what runs after it does.

**Then the campaign opens by measuring the NEW baseline twice, back to
back** — same box, same held fader, no re-level between rounds, mechanically
the r1/r2 pair old row 9 ran. What it produces is different: not a
comparison against the retired campaign, but the new system's **own**
round-to-round noise floor, measured fresh on its own instrument. That floor
is the yardstick every later candidate is judged against, replacing 0.37 dB
with a number the new system earned itself.

**Then the campaign proper:** analyze → three recommendations → each
measurement angle × each candidate, measured → apply the best. Crossover
(~2.5 kHz, jts3's Fc) first; then driver linearization over the trusted
range. Owner-present, daytime, on jts3.

**2. R-1 reverse polarity: BUILD.** In flight as its own PR. Both ends, per
the roster's own accounting: make `polarity=inverted` play and bank
(`run_null_walk`'s executor was deleted for want of callers), then write the
`analyze(null_depth)` consumer the decision half already assumes
(`crossover_alignment.py`'s `POLARITY_KEEP` / `POLARITY_INVERT` already
decide on measured null depth; nothing has fed them one).

**3. R-3/R-4 near-field: PARKED.** Both stay stubbed
(`measure_spec.py`'s `NEAR_FIELD_SPLICE_NOT_IMPLEMENTED` /
`DISTORTION_VS_LEVEL_NOT_IMPLEMENTED`); R-3's capture keeps banking against a
splice that has not landed, R-4's ladder keeps banking against a consumer
that has not landed. Stated consequence, recorded because the campaign
depends on it: the standing ~1 kHz room-comb ruling — EQ in that region is
forbidden until near-field measurement can separate driver from room —
**REMAINS IN FORCE**. Campaign candidates avoid that region until near-field
revives. The ~2.5 kHz crossover work in part 1 is unaffected — it sits over
an octave away from the parked region.

**4. The "PROVISIONAL pending W6 bench validation" banner: DELETE.** The
banner is quoted as still-standing by ADR-0181 and ADR-0182
(`REFACTOR-CUTOVER-2026-08.md:632,1073,1211`). The owner ruled it goes. A
separate, tiny PR removes it from the tuning constants; this ADR is the
record of the decision to make that PR, so ADR-0181 and ADR-0182 are not
stale when the banner disappears — they were accurate as of their own date,
and this ADR is what changed after it.

**5. Unchanged.** S11 acts 4 and 6 remain deferred with the relay park
(ADR-0188) — nothing here reopens or resolves that. Quiet-hours and
owner-present discipline for hardware work are unchanged: campaign work runs
owner-present, daytime, per the household's standing rule.

## Consequences

- Acceptance row 9 no longer gates row 10 on a numeric reproduction of a
  retired campaign; the two rows merge into one, whose opening act —
  measuring the new baseline twice — both fills old row 9's mechanical slot
  and produces the noise floor the rest of the campaign measures against.
- The day-1 preflight (deploy → doctor → gate probe → act 5 → re-mint) is now
  load-bearing on every campaign day, not a one-time acceptance gate — it
  runs each time the box is stood up for a session, not just once.
- R-1 is unblocked to build ahead of the campaign, since the reverse-null
  diagnostic is direct evidence for the crossover work the campaign opens
  with.
- R-3 and R-4 stay parked; the campaign's trusted range excludes the
  near-field-dependent floor extension (the 357 Hz trusted floor, per the
  roster) until they revive. The ~1 kHz room-comb finding stays exactly as
  forbidding as it was — nothing here relaxes it, and no candidate in this
  campaign may touch that region.
- The PROVISIONAL banner disappears from the tuning constants once its
  follow-up PR lands. ADR-0181 and ADR-0182 are not edited to match — ADRs
  are immutable — so a reader who reaches either after the banner PR merges
  should follow this ADR forward rather than trust their banner quote as
  current.
- `REFACTOR-TUNING-2026-08.md` rows 9 and 10 and roster rows R-1, R-3, R-4
  keep their existing text, each with a dated pointer appended to this ADR —
  the mechanics and the reasoning live here, not restated there.
- Rejected: keeping row 9 as a literal reproduction gate and treating the
  candidate campaign as a separate, later phase. That spends campaign-grade
  hardware time proving the new instrument agrees with a discarded one,
  instead of spending it finding and fixing the new system's actual bugs —
  the higher-value use of the hours, by the owner's own reasoning.
