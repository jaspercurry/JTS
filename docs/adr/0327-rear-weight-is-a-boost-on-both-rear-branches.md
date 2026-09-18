# ADR-0327: The rear weight is a boost on both rear branches, never front attenuation

- **Date:** 2026-09-18
- **Status:** Accepted
- **Supersedes:** the last sentence of ADR-0326 decision 1 ("A rear weight
  above 1 is written as front attenuation plus the band boost")
- **Extends:** ADR-0325, ADR-0326

## Context

A cardioid document's *rear weight* is the ratio |H_rear / H_front| over the
cancellation band (100–300 Hz on this cabinet). ADR-0326 allowed a bounded
filter boost and, because a chain's flat `gain_db` stays an attenuation, told
the author to realise a weight above 1 as front attenuation plus a band boost.

Rounds 8–9 on jts3 (2026-09-18, #5330) played that recipe: `front.gain_db` at
−2 and −4 dB. The rear stage's front chain is not a rear-band lever — it is the
front woofer's whole path up to the crossover, while both rear branches are
low-passed. Attenuating it cut the front woofer by the same amount from 350 Hz
to the tweeter hand-over (measured −2.0 dB at 350–1500 Hz for −2 dB; −5.3 to
−5.8 dB for the −5 dB relative document), and the level had to be bought back
with program headroom. Rounds 10–11 realised the same weights as one identical
Peaking boost on both rear branches with the front chain untouched: the
350 Hz–5 kHz band stayed within 0.4 dB of rear-muted, the bass lift was kept,
and the hole at the wall bounce closed by 4–8 dB.

## Decision

1. A rear weight above 1 is written as the same filter boost on **both** rear
   branches (`rear.bass` and `rear.cancellation`), within
   `rear_calibration.MAX_CHAIN_BOOST_DB`, so the fitted bass/cancellation
   ratio holds. The front chain carries only what belongs to the front
   woofer's own path (shared cuts such as the 190 Hz notch); it is never a
   level lever.
2. The published `rear` contract's `chain_gain_rule` says so in place of the
   superseded sentence, from the same constants. No new bound, no new guard:
   a document that attenuates the front is still valid — the preview
   (ADR-0325) discloses the 350 Hz–5 kHz cost, and the LLM reads it.
3. The headroom charge (ADR-0324) still prices the boosted stage's realised
   peak, and it lands as broadband attenuation pre-split: every level figure
   the preview reports subtracts it.

## Consequences

- `docs/rear-calibration-tuning-fields.md` and the playbook's rear recipe name
  the boost-on-both-branches rule; the "front attenuation plus a band boost"
  wording is retired everywhere it appears.
- Rejected: forbidding front attenuation in the validator (a nanny — the
  front chain legitimately carries shared filters, and the cost is disclosed,
  not hidden); a separate "rear weight" field (a second source of truth beside
  the two branch chains).
