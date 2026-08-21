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

This is the ruling the tree converges to, not a snapshot of its current
refusal surface — known deviations at this doc's date are tracked below,
never silently treated as more hard stops. The only refusals a scientist
accepts from the bench, because each guards a real component against
damage:

- the excitation ledger and the excitation safety plan
  (`jasper/active_speaker/excitation_safety_plan.py`)
- the output limiters and the volume / `HARD_CEILING_DBFS` rail
  (`jasper/audio_measurement/ramp.py`)
- declared per-driver excitation bands and level-duration limits —
  `permitted_band` / `level_duration_limits` in `excitation_safety_plan.py`
- the commissioning level stop — `max_commissioning_level_db_spl`
  (`jasper/active_speaker/profile.py`)
- firmware brick hazards, e.g. the XVF `SAVE_CONFIGURATION` ban
  (`jasper/cli/aec_tune.py`)

Everything else — geometry blindness, beaming priors, confidence
heuristics, prediction-engine rankings — is **provenance**, not a gate: it
rides with the data into the evidence packet for the prescriber (human or
LLM) to weigh. It must never refuse an experiment on its own.

### Known deviations at 2026-08-21

Five live, tested refusals sit outside the list above, none naming a
component-damage mechanism:

| # | refusal | file | status |
|---|---|---|---|
| a | `BOOST_VERTICALLY_BLIND` | `jasper/active_speaker/crossover_v2/driver_prescription.py` | removal in flight |
| b | `FC_REJECT_BEAMING` clamps the Fc grid; `candidate_space.py` calls the same prior "guidance, never refuses" (#1675) — self-contradictory | `jasper/active_speaker/crossover_v2/fc_sweep.py` | tracked |
| c | `REASON_CORRECTION_NOT_AN_IMPROVEMENT` — refuses on predicted-vs-predicted, no measurement in the loop | `jasper/active_speaker/crossover_v2/accountability.py`, `jasper/active_speaker/crossover_v2_flow.py` | tracked |
| d | `_strategy_gates` score floors; `measurement_evidence_failure`'s fail-severity apply blocker | `jasper/correction/confidence.py`, `jasper/correction/failures.py` | tracked |
| e | `prescription_route` refuses the boost class outright | `jasper/active_speaker/crossover_v2/blend_prescription.py` | tracked, most defensible |

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
