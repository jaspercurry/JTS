# Measurement-loop doctrine

> **Status: canonical doctrine, ratified by the owner 2026-08-21.** Governs
> how an LLM drives the crossover-v2 / correction measurement loop — what it
> may try, what stops it, and who decides. States the ruling once; other
> docs point here rather than restating it.

The owner's goal: an LLM that can propose, run, and grade its own
experiments against real measurements — the way a scientist works, not a
call-center script reading rules off a card. Rules exist only where a
mistake would damage hardware. Everything else is the LLM's and the
household's judgment call, made on data.

## 1. The loop

1. **Measure** — a round runs and banks its evidence.
2. **Propose** — the LLM reads the evidence, states pre-registered
   expectations, and proposes candidates to test next.
3. **Run** — a candidate that is cheap, safe, and reversible runs without
   ceremony.
4. **Collect** — every mic movement gathers the maximum information it can
   support, not the minimum that answers one question.
5. **Recommend** — the LLM makes a final call from the gathered evidence.
6. **Confirm** — one more measurement round checks the recommendation held.

## 2. The authority model

- **Predictions and heuristics PROPOSE.** Priors, confidence scores, and
  rankings are advisory. They never veto an in-band experiment.
- **Measurements DISPOSE.** Keep/rollback is decided by citing a measured
  delta, not a forecast.
- **The owner rules** on taste and on which risk to accept.
- **Hard stops exist only for a known component-damage mechanism** — never
  for "this probably won't work" or "we haven't tried this before."

## 3. The hard-stop enumeration (closed list)

The only refusals a scientist accepts from the bench, because each guards a
real component against damage:

- the excitation ledger and the excitation safety plan
- the output limiters and the volume / `HARD_CEILING_DBFS` rail
- declared per-driver excitation bands and their level-duration limits
- the commissioning level stop
- firmware brick hazards (e.g. the XVF `SAVE_CONFIGURATION` ban)

Everything else — geometry blindness, beaming priors, confidence
heuristics, prediction-engine rankings — is **provenance**, not a gate: it
rides with the data into the evidence packet for the prescriber (human or
LLM) to weigh. It must never refuse an experiment on its own.

## 4. The nanny test

Before a review adds a new refusal, ask: **does this block a reversible
experiment a scientist would run, on the theory it might not work?** If
yes, it ships as an informational flag, never a gate. A refusal earns its
place only by naming the component-damage mechanism it guards against —
"seems risky" is not a mechanism.

## 5. Pointers

- Round runner (the loop's home; measure and apply are two separate,
  fingerprint-named steps): `scripts/run-crossover-round.py`.
- Prescription doors (alignment, topology, blend, driver) —
  `jasper-crossover-prescriber` (`packet` / `propose` / `stage`) plus the
  session-open request body; cataloged in
  [`testing-tooling.md`](testing-tooling.md#crossover-prescriber-harness).
- Evidence packet — one document per round a reader (human or LLM) can
  answer from: `jasper/active_speaker/crossover_v2/evidence_packet.py`.
- Multiple DSP configs measured per mic position, so one mic movement
  answers more questions — lands on the round runner above
  (`scripts/run-crossover-round.py`); in flight as of this doc's date.

---

Last verified: 2026-08-21
