# Toolbox inventory and sizing (read-only Opus recon, 2026-09-04, main b963dad84)

## Headline
- 24 tuning console scripts + 12 round-views subcommands + 9 laptop scripts + 1 experiment script. Retired plan's target: ~8 binaries. Reachable mostly by RELOCATION; only 3 moves are adversarial tier.
- FOUR exit vocabularies (shared _refusal.py 0/1/2/3 used by 8 tools; prescriber has 1/2 inverted (runbook:631 admits it); angle-capture/round-bank 0/2/3; measure/null 0/1/2 with 2=bad flags; round/basic-profile 0/1/2/3 different meanings; arm-walk 0/3–15+128+signal; run-crossover-round.py 0/3–12+78; bank-crossover-round.sh 0/3/4; declare-geometry 2=NOT_FOUND).
- THREE artifact conventions (+1): (a) beside-the-round default with --out override: 12 views, gate-sweep, classify-features, read-distortion; (b) --out optional, no default: close-reference compare, prescriber packet/propose; (c) stdout only: delay-sweep, forward-model, angle-capture plan, prescriber status; (d) repeat-floor: --out REQUIRED; jasper-null writes <bundle>/null_runs/ (unique).

## Size verdicts (non-RIGHT only)
- jasper-angle-capture plan → TOO SMALL (stage --dry-run); withdraw → flag on stage.
- jasper-measure → orphan-ish: general door nothing in methodology points at (menu only); 1188 CLI lines, builds specs inline (no engine wrapper).
- prescriber propose → TOO SMALL (stage --dry-run, same gate verbatim).
- jasper-delay-sweep propose → TOO SMALL (one verb + subparser; stdout only).
- jasper-null → TOO BIG: 919 CLI lines of program synthesis + play + capture + bank, no engine module.
- jasper-active-speaker → TOO BIG: 1800 lines, 10 verbs (startup-template, path-audit, path-probe, environment-probe, runtime-safe-graph, baseline-reemit, commission-load, commission-rollback, commission-ramp step|ack|status|abort); called by deploy/install.sh + 2 systemd units; it IS §1 "prove the plumbing" but no doc points at it; no AUTHORITY_TIER so the generator cannot list it.
- scripts/run-crossover-round.py → TOO BIG: 1451 lines composing 5 tools + own 12-code vocabulary.
- scripts/bank-crossover-round.sh → duplicate of jasper-round-bank.
- jasper-correction-web → not a tool; the engine host (web/correction_setup.py 5203 lines).
- Not tuning tools: emit-bench (verification bench; the only thing answering "did my EQ land" — §6 needs it), bass-extension-bench (loudest thing in the tree, mutates live graph, unlisted), correction-bundle (room-correction v2), tuning-llm-live-check (paid harness), derive-crossover-incident-fixture (test infra), compare-readings, capture-reference-condition (AEC/wake).

## Not-a-tool capabilities an LLM would reach for
- Move the mic without a session: experiments/usb-turntable/jts_turntable.py (9 verbs; production per AGENTS.md; reachable only by path) → measure's mover backend or at least a console script + menu row.
- Open/apply a round from a laptop: scripts/run-crossover-round.py → jasper-round with --hostname transport, not a parallel implementation.
- Bank from a laptop: scripts/bank-crossover-round.sh → jasper-round-bank --from-host.
- Alignment and topology doors: request-body keys on POST /crossover/v2/session — NO CLI → prescriber stage --alignment/--topology.
- Republish / decline / reset: web-only → jasper-round republish|decline|reset.
- Room-correction diagnostics: scripts/capture-correction-diagnostic.py + analyze-… → jasper-correction-bundle diagnose (or scope out explicitly).
- jasper-correction-bundle absent from menu → row or explicit "room correction, not speaker tuning".

## Overlap clusters
A. Three ways to run a round: jasper-measure (one placement, one MeasureSpec, banks takes) · jasper-round (3 wizard verbs over HTTP) · run-crossover-round.py (stage·walk·open·await·bank + --apply). Arg names rhyme without matching. LLM must know: on-box or laptop, arm or not, graded round or one capture. Merge = relocation of composition into `jasper-measure round --mover turntable|human --hostname H`, keep jasper-round's verbs as transport; --apply half is a rewrite (one apply gate).
B. Movers: angle-capture (declares walk → spool), arm-walk (serves position gate, 16 exit codes), null. ADR-0228 + test_measurement_mover_agnostic.py already pin mover-agnosticism → --mover human|turntable is RELOCATION. jasper-null is NOT a mover: a second measurement front-end → `jasper-measure --kind null`, REWRITE.
C. Advisory views split two ways for no stated reason: 12 subcommands (one ARTIFACT_BY_VIEW table, shared _refusal) vs 6 binaries (3 output conventions). Real discriminator is what they read (round dir vs bundle+--dumps ring vs two rounds) — a flag, not a binary. Relocation for gate-sweep, classify-features, read-distortion, spec-sweep; light rewrite for close-reference distance (pure calculator) and forward-model (needs candidates).
D. jasper-project-ring: hardlinks a bundle's captures into sidecar/+wav/ that only classify-features --dumps and read-distortion --dumps consume → a --project-ring flag (or implicit step), not a third binary an operator must remember to run first.

## Recommended moves, ordered (R = relocation, W = rewrite, NN = adversarial tier)
1. R  AUTHORITY_TIER + menu row (or explicit "not tuning" line) for jasper-active-speaker and the two benches.
2. R  bank-crossover-round.sh → jasper-round-bank --from-host.
3. R  Collapse the one-off exit vocabularies onto _refusal.py; keep arm-walk's (real hardware states); fix prescriber's inverted 1/2.
4. R  jasper-project-ring → --project-ring flag on classify-features / read-distortion.
5. R  gate-sweep, classify-features, read-distortion, close-reference, delay-sweep, forward-model → one `jasper-read <view>` with the shared beside-the-round --out default.
6. R+W NN  run-crossover-round.py → `jasper-measure --mover human|turntable --hostname H`; promote jts_turntable.py to a console script. Apply path reaches POST /crossover/v2/apply and the session-volume plan (set_volume_db, session ceiling).
7. W NN  jasper-null → `jasper-measure --kind null` (919 CLI lines of program synthesis/capture move to the engine first; excitation path: excitation_safety_plan, program_admission, back-off gain).
8. W NN  CLI for alignment/topology doors (prescriber stage --alignment/--topology) + jasper-round republish|decline (mutating-with-gates on the DSP output path).
9. —  Leave jasper-seat-level alone (touches the commissioning SPL stop; merging buys nothing).
10. R  capture-/analyze-correction-diagnostic.py → jasper-correction-bundle; delete or quarantine THROWAWAY benches (multiroom-spike-measure.py, s0-sync-measure.py, _sync_measure_audio.py, _make_click_track.py).
Net: ~8 binaries — measure, read, round, round-bank, prescriber, seat-level, basic-profile, audition, declare-geometry.
