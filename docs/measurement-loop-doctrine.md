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
- the output limiters — `STARTUP_LIMITER_CLIP_LIMIT_DB` /
  `BASELINE_LIMITER_CLIP_LIMIT_DB` in
  `jasper/active_speaker/camilla_yaml.py` — and the volume /
  `HARD_CEILING_DBFS` rail (`jasper/audio_measurement/ramp.py`)
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

### Known deviations at 2026-08-21, and where each stands

Five tested refusals sat outside the list above when this doc was ratified,
none naming a component-damage mechanism. **Four are now closed and one is
retained by ruling, so this table tracks nothing outstanding.** Rows are struck
as they close and a struck row stays, so a reader meeting one of these names in
an old round or an old commit can tell it was retired on purpose:

| # | refusal | file | status |
|---|---|---|---|
| ~~a~~ | ~~`BOOST_VERTICALLY_BLIND`~~ | `jasper/active_speaker/crossover_v2/driver_prescription.py` | **CLOSED** — removed in #2805; a boost admitted on a horizontal capture now owes a measurement, not a plane |
| ~~b~~ | ~~`FC_REJECT_BEAMING` clamps the Fc grid against a prior #1675 rules "guidance, never refuses"~~ | — | **CLOSED 2026-08-21.** The refusal only ever bound the corner hunt's proposal grid, and the hunt was deleted with `fc_sweep`'s sweep half (plan ticket 2.3). No admissibility bound reads the ka onset now; it rides the receipt as provenance, which is what section 3 asked for. |
| ~~c~~ | ~~`REASON_CORRECTION_NOT_AN_IMPROVEMENT`~~ — refused on predicted-vs-predicted, no measurement in the loop | `jasper/active_speaker/crossover_v2/accountability.py`, `jasper/active_speaker/crossover_v2_flow.py` | **CLOSED** — it vetoed jts3's first prescribed-boost round on 2026-08-22 (`improvement_db=-0.703`, one line after disclosing its own inputs 11.635 dB apart); the forecast now banks `LEDGER_NOT_AN_IMPROVEMENT` and the round proceeds |
| ~~d~~ | ~~`_strategy_gates` score floors~~; ~~`measurement_evidence_failure`'s fail-severity apply blocker~~ | `jasper/correction/confidence.py`, `jasper/correction/failures.py` | **CLOSED** — the score floors' only veto (`response._policy_allows`) went in #2808 and every remaining reader was already disclosure; the apply blocker is deleted, and a `fail`-severity finding now reaches the household as a `warn` nudge |
| e | `prescription_route` refuses the boost class outright | `jasper/active_speaker/crossover_v2/blend_prescription.py` | **RETAINED by ruling R8** (`docs/tuning-master-plan.md`): "Blend's `BOOST_ROUTE_UNAVAILABLE` stays for its two recorded reasons (blend is not a headroom term; a summed capture cannot attribute a deficit to a driver)" — a stated limit of the instrument, not a prior about the outcome |

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
  `jasper-crossover-prescriber` (`packet` / `propose` / `stage`; its fourth
  verb `status` orients rather than prescribes) plus the
  session-open request body; cataloged in
  [`testing-tooling.md`](testing-tooling.md#crossover-prescriber-harness).
- Evidence packet — one document per round a reader (human or LLM) can
  answer from: `jasper/active_speaker/crossover_v2/evidence_packet.py`.
- More than one capture per mic position, so one mic movement answers more
  questions (§1.4) — `--per-position N` on the round runner above, plus the
  derived `position_cycle.json` that says which pose each take was measured
  at. Multiple DSP *configs* per position has a door but no wiring:
  `POST /crossover/v2/republish` makes a banked candidate the live one by its
  own fingerprint, so republish-then-apply reaches a named prior config
  between takes. The open part is sequencing — holding a pose's next capture
  until the apply has landed — which is a design to write rather than a
  refusal to remove. The `awaiting_apply` hold is explicitly not the seam for
  it (its own vocabulary says "no new design may depend on it").

---

Scope of this verification: the deviation table above was re-derived against
the tree — every row's named symbol grepped for a live producer — and rows (a),
(c), and (d) closed; row (b) closed separately the same day (#2853) and its
account is that PR's. Sections 1-3, 4, and 5 were re-read and stand unchanged.

Last verified: 2026-08-22
