# LLM walkthrough of methodology + doctrine + runbook (read-only Opus recon, 2026-09-04, main b963dad84)

## The walk (closes? = does the next step read a named artifact the previous step wrote)
- §0 DECLARE → jasper-declare-geometry → measurement_geometry.json → frozen into round as declared-geometry.json → Y
- §1a LEVEL → jasper-seat-level → active_speaker_seat_level_reference.json (path named NOWHERE in docs) → session_volume_plan at round open → Y in code, prose in doc
- §1b POLARITY → doc names NO tool (names modules); shipped instrument is `jasper-null --polarity both` → null_runs/ rows nothing reads → N
- §1c REPEAT FLOOR → round-views repeat-floor --out <you choose> → packet reads it ONLY after a manual copy to /var/lib/jasper/active_speaker_repeat_floor.json → manual step
- §2 RAW DRIVERS → arm-walk, angle-capture stage, prescriber status; the verb that MEASURES is unnamed (run-crossover-round.py / jasper-round / jasper-measure) → banked round → partial (bank step + <session-dir> runbook-only)
- §3 CORNER → names none for the act (names door refusals); the door is session-open key topology_prescription; jasper-round open cannot carry it (only run-crossover-round.py / raw POST) → only in LLM prose
- §4 TIME ALIGN → names three (delay-sweep propose: stdout only; jasper-null: null_runs/; alignment door reads the TYPED delay_us) → N. meth:424 admits: "Grading the confirmation is not wired: no banked take records the delay coordinate it was played at."
- §5 LEVEL MATCH → "no separate verb to run"; apply seam banks base trim → Y in code, invisible
- §6 LINEARIZE → project-ring, classify-features --dumps, gate-sweep, round-views spec-sweep → feature_classification.json / gate_sweep.json / spec_gate_sensitivity.json → packet → propose/stage → Y (the one fully closed loop)
- §6a ROOM-OR-SPEAKER → gate-sweep, close-reference → attribution only, prescribes nothing → N by design
- §7 SUMMED VERIFY → names none ("read that table in code") → verification_result in round receipt → only in prose
- §8 VOICING → jasper-audition → runtime only; "if you apply a tilt, declare it" — no field exists → N
- §10 ITERATE → names none (doctrine §1 names round-views frozen) → only in prose

## Gaps, ranked
1. §4 confirm produces evidence nothing grades (null_runs/ has no reader). Fix: bank the played delay coordinate on the take + a `round-views null` view.
2. §1b names no tool; its tool has no reader. Fix: name `jasper-null --polarity both`, say where depth lands.
3. §7 and §10 (keep/iterate decisions) name no tool. Fix: name the verify verb (`jasper-round open --stage post_apply`) and `round-views entry|frozen`.
4. Live <session-dir> never given a path; `jasper-round wait` returns a session_id. Fix: wait prints the bundle path.
5. Session-crossing memory the LLM must carry in its own context: pre-registered expected delta (doctrine:35; only home would be the driver document's undocumented `rationale`); declared voicing tilt (meth:776); which protection-phase composition the propose curve got (meth:336 "recorded nowhere"); which σ kind quoted (meth:172); §6a rung verdict; the --state/bundle pairing for read-distortion (runbook:890 "the mis-scope trap, which nothing can refuse").
6. §1c artifact needs a manual file move. Fix: default --out to the on-box path.
7. §1a artifact unnamed, no read-back verb. Fix: name it; add `show`.
8. CONTRADICTION: runbook:615 says a refusal naming no damage/hearing mechanism is a deviation → open an issue; doctrine §4a documents ~100 INTEGRITY refusals that legitimately refuse a claim, and meth:445 blesses PRESCRIPTION_OUT_OF_LOBE. Fix: runbook's hard-stops paragraph excepts the integrity class, pointing at §4a.
9. CONTRADICTION: σ count — meth:171 "two spreads", runbook:1210 "three", runbook:1103 "a fourth". Fix: methodology says four; runbook owns them.
10. Load-bearing constants (stopping rule, drive level) live in tuning-master-plan.md which meth:19 says is "for the program's developers". Fix: move "Measurement program constants" into the methodology.
11. Menu tools with no step: jasper-measure (its help says "raw-driver plants or ad-hoc work" = §2 exactly), jasper-forward-model, jasper-read-distortion. Fix: one methodology sentence each or mark off-walk.
12. §3 names the topology door only by refusals; the key is runbook-only; jasper-round open cannot carry it. Fix: say so in §3.

## Three docs or one
Mostly one story with clean layers (doctrine = rules/authority; runbook = verbs/fields/exit codes; methodology = order + decision rule). Full second copies: LLM-recommends-measurement-decides ×3; corner declared not searched ×2; BOOST_ROUTE_UNAVAILABLE ×3; series cap (doctrine says "three… may extend to four", code says 3); four verdicts ×2; operator prose ×2. Two contradictions: σ count; what a non-clamp refusal means.

## Room correction
Separate product by the docs' own framing (layer 3 vs speaker layers 1a/1b; doctrine:51 rules it out of the tuning graph). Methodology never sends the LLM there and should not; §6a is attribution only. One nominal confusion: /correction/ is the crossover wizard's URL while jasper/correction/ is the Room package — one disambiguating sentence. Methodology should say once (§8/§9) that below-floor and room-modal work is layer 3's.

## Verdict
The linearization spine §0 → §1a/§1c → §2 → §6 → §7 → §10 is closed: every step names a tool, every tool writes a named artifact into the round dir, the packet is the join. The DIAGNOSTIC half (a speaker that sounds off because it is mis-timed) is not: §1b names no verb; §4 names three of which one writes rows nothing grades, so the LLM eyeballs JSON and re-types a number into a door — the first invented step. Second stall is clerical: after `jasper-round wait` the LLM holds a session_id and the runbook wants a <session-dir>. Across sessions the docs assume a memory the artifacts do not provide. Fix the delay-lane grading, name §1b's and §7's verbs, print the session path, bank the expectation, and the walk closes end to end.
