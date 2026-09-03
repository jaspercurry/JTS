# Recon brief — right-sizing the JTS tuning codebase

You are one of several parallel reconnaissance agents. Repo: /home/user/JTS (Python
smart-speaker project; read AGENTS.md first — it is short and binding). Branch is
`claude/busy-goodall-mz0gvv`, rebased on origin/main today (2026-09-02). Do NOT
edit or commit anything. Read-only recon. Write your report to the file named
in your task prompt and return a <=300-word summary.

## The owner's goal (verbatim spirit)

"Sift through everything in the tuning side of the house as it stands today and
help me clean it up. Ideally at the end it is smaller, tighter, has clear
boundaries, and anyone would understand it from the folder structure and files.
Code clean and elegant, easy to work in or extend manually." Values: separation
of concerns, single source of truth, clear contracts and boundaries, 80/20
solutions that are not over-engineered. Most of this code is meant to be driven
by an LLM that runs measurements, analyzes, tests candidates, and tunes the
speaker (docs/tuning-methodology.md is the LLM-facing methodology;
docs/measurement-loop-doctrine.md is the binding doctrine;
docs/tuning-operator-runbook.md is the tool manual). The owner suspects lots of
random stuff that adds no value: stale prose, old docs, abstractions that don't
make sense, duplicates. Breaking open PRs is acceptable if needed.

## What "tuning scope" means

jasper/active_speaker/ (incl. crossover_v2/, bench/, presets/),
jasper/audio_measurement/, jasper/correction/, jasper/attribution/,
jasper/calibration_agent/, jasper/web/correction_*.py (+ active_speaker_flow,
balance_*), the tuning CLIs in jasper/cli/ (active_speaker, audition,
active_speaker_attempts_replay, crossover_prescriber, project_ring,
classify_features, read_distortion, round_views, round_bank, round,
angle_capture, arm_walk, active_speaker_emit_bench, basic_profile, seat_level,
delay_sweep, forward_model, gate_sweep, close_reference, measure, null_door,
bass_extension_bench, declare_geometry, correction_bundle, measurement_mic),
experiments/usb-turntable/, the tuning docs under docs/, and the tests for all
of the above. Sizes at HEAD: active_speaker 172 files/168k lines (crossover_v2
subpackage 54k; crossover_v2_flow.py 7.8k), audio_measurement 32k, correction
17k, attribution 2.7k, web/correction_crossover_v2.py 7.8k,
web/correction_setup.py 7.5k. Tests for the scope ~280k lines.

## Prior analysis (a few days old; still broadly right) — key claims

- Only ~25% of the scope is measurement science. ~35% is prose (docstrings +
  comments), much of it ruling history, "superseded" paragraphs, issue narration
  (1,763 `#NNNN` citations, 132 ADR citations). AGENTS.md's bar: a comment is a
  non-derivable constraint or a one-line pointer.
- Refusal/gate plumbing re-rolled per module: `_refuse` defined in 22 files,
  `_refused` in 10, `_gate` in 7, `_issue` in 6, `_blocked` in 5; ~110
  error/refusal classes; 131 `REFUSE_*`/`VERDICT_*`/`PHASE_*` constants across
  32 files. A registry exists (crossover_v2/refusal_copy.py, 43 codes) but is
  imported by 5 files.
- Hand-written serializers: ~297 to_dict/from_dict-ish methods, 230 on
  @dataclass; 190 to_dict vs 16 from_dict. 15 sha256 helpers with 6
  signatures; 11 `_text`, 8 `_mapping` re-rolls; identical 6-line exception
  __init__ in 7 files.
- The session has a twin: crossover_v2_flow.py (one ~7k-line class, 160+
  methods) and web/correction_crossover_v2.py (100+ top-level defs, a 900-line
  prepare_v2_session). Two objects for one session.
- Tests: 10,770 test functions, 9.5% parametrized; many pin one finding each.
- Growth ratio +3.6 lines added per line deleted over 8 weeks.
- Docs restate the same fact 4x (doctrine, call-site comment, ADR,
  REFACTOR-TUNING-2026-08.md 1.9k lines).
- Recommended order: prose bar → one refusal primitive → parametrize tests →
  generated serialization → finish the strangler on the two session objects.
  Do NOT big-bang rewrite; do NOT bulk-delete prose by script; do NOT add
  line-count CI gates.

## Doctrine facts you must respect (from measurement-loop-doctrine.md)

- Non-negotiables (AGENTS.md): hearing clamp (volume_limit 0.0, set_volume_db
  clamp, commissioning SPL stop), XVF SAVE_CONFIGURATION ban, secrets, deploy
  path, renderer ALSA check, no silent deafness, paid tests, protected main.
- Five hard-stop CLAMP mechanisms (excitation ledger/safety plan; output
  limiters + volume rail; declared per-driver bands; commissioning level stop;
  firmware brick). These stay production-grade. Everything else is INTEGRITY
  (refuse a claim, still bank) or DISCLOSURE (never blocks).
- Target architecture (REFACTOR-TUNING §1): ONE engine with four verbs
  measure/analyze/recommend/save; two THIN front ends (web wizard, LLM+arm);
  truth layer (audio_measurement + crossover_v2 analysis) with no upward import;
  presets are data; analyze defaults to everything the bank supports.
- Layering rule: a measurement plays through layer N and below, never
  preference EQ above it.

## In-flight PRs that touch this scope (do not duplicate; note overlap)

- #3724 "Collapse the crossover-v2 capture source and the room capture
  transport to their one answer" — seam 3 of the phone-relay deletion
  (tracking issue #3661). Deletes JASPER_CAPTURE_SOURCE, per-source forks in
  web/correction_crossover_v2.py, correction_crossover_v2_wired.py,
  crossover_v2/capture_source.py, room flow capture_transport. Stacked
  follow-ups (routes PR, deploy PR) will remove correction_crossover_v2_relay.py
  and jasper/capture_relay/.
- #3719 docs: relay leaves documentation, ADR-0220.
- #3748 retires jasper-aec-tune (moves fader helpers into camilla.py) — AEC,
  not tuning, but it touches the XVF SAVE_CONFIGURATION ban location.
- #3705 (draft) wake page alignment button — not tuning.

## What a good report looks like

Concrete, evidence-first, no fluff. For each finding: file:line (or file), what
it is, why it is a smell against the owner's values, the proposed move (delete /
merge into X / extract to Y / rewrite as Z), estimated lines removed, risk
(low/med/high) and what test or check proves it safe. Prefer a table per
category. Distinguish: (1) dead code (no caller — verify with grep incl.
registries, pyproject entry points, systemd units in deploy/, deploy/bin,
importlib/getattr strings, tests-only callers), (2) duplication (name both
copies), (3) prose over the AGENTS.md bar (quantify per file; sample and quote
2-3 examples, do not list every block), (4) boundary violations / wrong-altitude
code (e.g. web module doing engine work, CLI re-implementing analysis, truth
layer importing upward), (5) abstractions that don't earn their keep (a class
with one caller, a registry with one entry, a module of 40 lines that is really
one function, three names for one concept), (6) stale docs (say what at HEAD
contradicts them), (7) anything the LLM driver actually cannot use or that is
never reached from the runbook's tool menu.

End with a ranked "top moves" list for your area: each with a one-line
description, line delta estimate, risk, and a rough order. Be honest about
uncertainty. Total report <= ~400 lines. Use `wc`, `grep -c`, `python -c`
scripts freely for counts; cite the command so numbers are reproducible.
