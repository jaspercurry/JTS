# Correction & tuning — revision plan (the layered pipeline)

> **Status: planning brief / execution plan of record.** Written 2026-07-03
> from an orchestrated audit (3 research rounds, 21 subagents) + maintainer
> alignment. This governs the reshaping of room correction into a layered,
> foolproof, self-verifying tuning feature. Current *operational* truth for the
> already-shipped subsystems still lives in
> [docs/HANDOFF-correction.md](HANDOFF-correction.md),
> [docs/HANDOFF-audio-measurement-core.md](HANDOFF-audio-measurement-core.md),
> and [docs/HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md). This
> doc is the *plan*; those stay the source of truth for what ships.

## TL;DR

This is **not a rebuild.** The layered model is already the repo's
design-of-record (`HANDOFF-active-speaker-dsp.md:958` documents Layer A/B/C, and
the CamillaDSP graph already composes them in order behind a `volume_limit: 0.0`
ceiling), the shared measurement core already exists (`active_speaker`
imports the `jasper.correction` sweep/deconv kernel verbatim), the level ramp is
half-built (`correction/autolevel.py`), and bass management is shipped (wireless
2.1 + local-DAC sub, LR4 @ 80 Hz, with the "sub-LP upper ceiling" already the
`graph_safety` invariant). The work is **consolidate → close the loops → prove
on hardware.** The maintainer is away, so all consolidation/loop-closing happens
now (hardware-free); on-device proof is parked with per-PR checklists.

## 0. Governing principles

1. **Conditional layered pipeline.** Active speakers do Layer A first; passive
   speakers (single full-range DAC — the majority) skip it. Detected today via
   `output_topology` (`full_range_passive` vs `composite`).
2. **Three-tier control (the governing rule):**
   - **Safety = hard-enforced. The one thing we block.** No genuinely
     unsafe/conflicting config (full-range to a compression driver, an
     unprotected tweeter, a sub low-pass above 200 Hz). Already fail-closed in
     `active_speaker/graph_safety.py`; every candidate graph re-proves it.
   - **Measurement quality = nudge, never block.** Mic didn't move / uncalibrated
     → a sentence + a checkmark, Continue always live. "That's on them."
   - **Preference / taste = allow, never block.** Wacky tilt is fine — subjective.
3. **One shared measurement core** — `jasper/audio_measurement/` (sweep, deconv,
   analysis, calibration, a parameterized `QualityModel`, a shared
   `RampController`). Layers differ by *method* (near-field-gated vs
   listening-position; per-driver vs summed), not by primitive.
4. **One shared target** across Layers B+C: correction removes the room's
   deviation *from* a target; preference *chooses* that target. Physically the
   two DSP stages stay separate (modal cuts below transition; broadband tilt
   above), but the target, the agent, and the vocabulary are unified.
5. **Verify-by-re-measure with a *deterministic* acceptance verdict** is JTS's
   genuine differentiator — no shipping product (Dirac/Audyssey/Trinnov/
   Sonarworks) and no surveyed paper closes this loop. The LLM proposes;
   deterministic code decides; the room's re-measurement is the judge.
6. **Dumb frontend / smart backend.** The browser captures audio and renders a
   server-computed JSON screen envelope; all smoothing, analysis, verdicts, and
   filter design live on the Pi.

## 1. The layered architecture

For an **active** speaker the ordered pipeline is:

**Layer A — Speaker** (near-field, gated, room *removed*; PRIMARY/foundational,
replaced from presets by commissioning, not skippable): per-driver level-match,
crossover corner/slope, delay/polarity, bass-management high-pass, protection.
Per speaker build. → **Layer B — Room** (listening position, room *included*,
spatial average): modal-region correction to the shared target. Per room. →
**Layer C — Preference** (broadband tilt/shelf, to taste).

For a **passive** speaker the pipeline is **B → C only** (Layer A hidden).

The graph already stages this safely (verified in `active_speaker/camilla_yaml.py
::_emit_baseline_pipeline`): on the stereo bus pre-split → Layer B `room_peq_*` →
`active_baseline_headroom` gain → Layer C `preference_filters` → split mixer →
per-output Layer A driver chain `[bass-mgmt HP, crossover, delay, polarity/gain,
limiter]`, tweeters additionally protective-HP'd, `volume_limit: 0.0` ceiling,
re-proven by `runtime_contract.py` before every load. Preference is strictly
upstream of every driver limiter, so a preference boost can never bypass driver
protection.

The UI becomes a pipeline that walks the applicable layers and lets the user
**re-enter at the right layer** (moved the couch → just redo B; new taste → just
C).

## 2. What exists today (verified against code)

- **Shared core is real:** `active_speaker/driver_acoustics.py` imports
  `jasper.correction.{sweep,deconv,analysis,quality,calibration}` verbatim;
  `camilla_emit.py` is the shared emission leaf; room PEQs compose *into* the
  active-speaker graph as `room_peq_N` slots.
- **Level ramp half-built:** `correction/autolevel.py::AutolevelController` ramps
  `main_volume` from quiet and locks — but ramps *blind* (lock decision in the
  browser), does not recover relay latency, does not pick an SNR window Pi-side.
- **Bass management shipped:** wireless 2.1 (`multiroom/channel_split.py` +
  `reconcile.py` mains-HP) and local-DAC sub (`active_speaker/profile.py
  LocalSubwoofer`), both LR4 @ 80 Hz (40–200 bounds). The "sub-LP upper ceiling"
  is already `graph_safety.py::sub_audible_guard_present` (200 Hz cap + mandatory
  limiter). Gaps: two layers emit the crossover independently; room correction
  (20–500 Hz) overlaps the crossover with no awareness (double-correction risk);
  the bass wizard `correction_bass_flow.py` is a stub.
- **Crossover commissioning** is the most-built layer (~35 modules, safe-by-
  construction, evidence-complete) but **acoustically unvalidated on real
  drivers.** JTS5 runs the crossover live; JTS3 has been flat passthrough (the
  live shrill/hot-tweeter L0 hole). Time/phase: polarity done; per-driver
  **delay deliberately deferred** (browser captures aren't sample-synced).
- **LLM:** `calibration_agent/` is a live OpenAI advisor
  (`model_client.py::AdvisorModelSettings{provider,model,base_url}`) but
  CLI-only (zero non-test importers), scoped to *preference* auditions, with an
  excellent safety substrate (redaction, strict-schema validation, ±12 dB
  re-clip, reversible audition, prohibited-key blocklist).
- **Room-correction trust gaps (round 1):** confidence gates are computed but
  never consulted at apply (per the maintainer, that's fine — we *nudge*, never
  block); the shown "improvement" is *predicted*, not a real measured
  before/after delta; no honest before/after visualization.

## 3. Subsystem designs

### 3.1 Level-match ramp (relay-closed) — the maintainer's priority

The analog amp gain is unknown; JTS controls only digital level. Upgrade
`AutolevelController` into a shared `RampController` in `jasper/audio_measurement/`:

- Play a **quiet-start staircase ramp** (band-limited noise, ~1 dB per ~0.4–0.6 s
  step) of `main_volume` (already 0 dB-clamped in `camilla.py::_coerce_main_volume_db`).
- The phone computes mic RMS→dBFS locally (freeze getUserMedia AGC/NS/EC; report
  `agc_frozen` per event) and streams rolling `{level:{seq,t_client_ms,rms_dbfs,
  peak_dbfs,clip,agc_frozen}}` events over the **existing relay `event` channel**
  (zero relay change — the relay is opaque by contract).
- The Pi holds the known played envelope (output-dB vs Pi-time) and the received
  mic envelope, and **cross-correlates them** (reuse `capture_relay/alignment.py
  ::cross_correlation_alignment`, which scores confidence) to recover the transport
  delay τ. Weak correlation → fail loud and re-cue, never lock a wrong map.
- **Stop-ahead** the instant the τ-corrected mic level enters the **−20…−12 dBFS**
  window with clip margin — never blast up to find it. If the ramp maxes out
  without reaching the window (amp too quiet), stop and tell the user to raise the
  analog amp; never exceed the 0 dB ceiling to compensate.
- **Lock** a session-scoped `MeasurementLevelLock` every layer's captures reuse
  (one measurement volume for the whole session).
- **Drift check:** on later captures, a *uniform* per-band dB shift vs the lock
  (|mean Δ| > ~3 dB, all bands within ±2 dB of the mean) = the amp/volume moved →
  flag + offer re-level. A *non-uniform* change is acoustic — do not confuse them.

Hardware-free now: the algorithm, the relay schema, the τ-recovery, the
SNR-window/stop/lock/drift logic, all under synthetic/mocked tests. Parked: the
on-device τ-cadence tuning and the iPhone/Android AGC-freeze confirmation.

### 3.2 Room correction — simple, honest, dumb-frontend

- **One JSON screen envelope** per step (`{screen, curves{measured,target,
  predicted,verify — server-smoothed}, fill_segments[], headline{before,after,
  delta}, verdict_text, nudges[], next_action, progress}`). Browser is a pure
  renderer; all smoothing/thresholds/verdicts on the Pi.
- **Stepped wizard:** entry → mic + calibration *nudge* → level-match (§3.1) →
  guided N-position sweep with "move the mic" prompts → review vs target → apply
  → verify → **before/after result** → save. Every gate is a sentence + a
  checkmark; nothing disabled.
- **Honest two-tone before/after fill** — a ~40-line extension of the existing
  canvas `drawSpread()`: green where |after−target| < |before−target| (helped),
  amber where a band regressed. Headline one number: "±6 dB → ±2 dB in the bass."
  Never show a raw jagged curve — server-smoothed (variable for design,
  psychoacoustic for the "what you hear" view).
- **Real measured before/after delta** in `verify_metrics` — recompute the
  pre-correction deviation over the *same* 50–350 Hz band from the stored measured
  curve (do not reuse `design.before`, which is over a different band), and stop
  calling the *predicted* number "improvement."

### 3.3 Bass management — corner/slope/level now, delay/polarity via the null-walk

- **Ownership:** the Speaker layer owns crossover corner/slope, sub level, and
  sub delay/polarity; the Room layer corrects the *already-bass-managed summed*
  response (it *reads* the corner, never re-picks it); Preference owns the
  sub-bass shelf. One bass target — never double-cut.
- **Unify the corner:** one shared crossover-corner constant + LR4 emit primitive
  so `multiroom` and `active_speaker` cannot drift (both already LR4 @ 80 Hz,
  40–200). Fix the stale `channel_split.py` "mains HP is a V1 non-goal" docstring
  (it contradicts the shipped `reconcile.py` mains-HP path).
- **Timing is part of bass management.** Sub level rides the §3.1 ramp
  (band-limited to the overlap region). Sub↔mains **delay/polarity** rides the
  same timing-locked null-walk as driver time-align (parked, hardware) — with the
  extra wrinkle that a *wireless* sub also carries snapcast transport delay to
  account for.
- Hardware-free now: the corner-primitive unification, room-correction reading the
  corner + correcting the summed response, and building out the bass wizard.
  Parked: sub-level ramp on-device and sub↔mains delay.

### 3.4 The tuning LLM — OpenAI-first, one agent spanning both jobs

Reuse the shipped `calibration_agent` propose→validate→execute→revert contract;
extend it, don't rebuild it.

- **Provider:** start on the existing OpenAI adapter (GPT-5.4) — the `provider`
  field on `AdvisorModelSettings` is the swap seam for better models later. Move
  the OpenAI key into the `jasper-secrets` compartment before any live-surface
  wiring. (Correct the design doc's stale "Anthropic-first" mandate to
  "OpenAI-shipped, provider-swappable.")
- **Two jobs, one agent, one target:** *interpret the measurement* (correction —
  "you've a 60 Hz room mode; here's a tighter filter") and *shape the target to
  taste* (preference — "warmer" → a bounded low-shelf on the shared target), with
  a fixed voicing lexicon and an optional short A/B audition loop that learns a
  small 2–3-D preference vector (tilt + bass level). The LLM proposes bounded
  JSON; deterministic code validates/clamps and is the only writer of CamillaDSP.
- **Two loops, different closure:** correction claims are *verified* by re-measure
  ("55 Hz mode now within 2 dB of target"); preference claims are subjective and
  phrased as questions ("this should sound warmer — better?"). Privacy holds — the
  LLM sees only the redacted curve summary `advisor_context.py` already produces,
  never raw audio.
- Surface it first as an *interpreter/narrator* in the flow (plain-language "here's
  what your room is doing," explains the verdict), then as a confirm-gated proposer
  whose every proposal is simulated and rejected-if-it-would-ring before Apply.

## 4. Roadmap — hardware-free (do now, while Jasper is away)

Each item is one or more small PRs to `main`, each with hardware-free tests.

- **P1 — Foundation.** (a) Close the **L0 safety hole**: one consolidated
  `GraphValidator` wired at the `camilla_yaml` emit gate so a flat graph with a
  tweeter role can never go live, pinned by a test. (b) **Extract the kernel** to
  `jasper/audio_measurement/` (sweep/deconv/analysis/calibration move unchanged;
  add a parameterized `QualityModel(room|driver|ramp)` so forked thresholds
  become profiles) behind characterization tests.
- **P2 — Level-match ramp (§3.1)** logic: `RampController` + relay level-event
  schema + τ-recovery + SNR-window stop + lock + drift, all under synthetic tests.
- **P3 — Room correction simple & honest (§3.2):** the JSON screen envelope, the
  stepped dumb-frontend flow, the honest two-tone fill, the real measured
  before/after delta, nudges (never blocks).
- **P4 — Verify-acceptance loop:** `AcceptanceEvaluator` (store predicted curve at
  apply → after verify compute error-to-target reduction + per-band "did any band
  get worse" guard → accept / surface / auto-revert on clear regression), under
  synthetic before/after captures.
- **P5 — Bass management unification (§3.3, non-timing):** one crossover-corner
  primitive; room correction reads the corner and corrects the summed response;
  build out the bass wizard; fix the stale docstring.
- **P6 — Tuning LLM (§3.4):** extend the advisor vocabulary to target + correction
  moves; move the OpenAI key to `jasper-secrets`; surface the interpreter in the
  flow; the confirm-gated proposer with simulate-before-apply. (Paid-call cost
  discipline per AGENTS.md — never in CI.)
- **P7 — Active-crossover measurement flow (hardware-free shaping).** The Layer-A
  commissioning *flow* is fair game now — only its acoustic proof is parked
  (H2). Wire the relay `crossover_sweep` spec into `correction_crossover_flow` so
  all layers ride one transport + one upload seam; align the commissioning
  sequence with the room flow's screen envelope (§3.2); ride the shared kernel,
  the level-ramp, and the L0 gate; tidy `commissioning_coordinator`. Every
  acoustic assumption gets an on-device sanity-check line for H2, not a claim.

Ordering: **P1 → P3 → P2 → P4 → P5 → P6**, with **P7 shaped alongside P3** (they
share the screen envelope and the shared core). Foundation first; the flow
simplifications (room P3 + crossover P7) are the biggest hardware-free user win;
the ramp logic and verify-loop follow; bass + LLM last. P1 and P3 can start in
parallel.

## 5. Roadmap — hardware-gated (parked until hardware in hand)

Each carries an on-device validation checklist attached to its PR; **none merges
as "validated" — it merges as "hardware-free complete, on-device pending."**

- **H0 — Prove the loop on JTS5.** A throwaway CLI spike routing a sweep through
  the production active graph to one driver, printing a real level-match number —
  AND confirming the relay + measurement capture actually work on JTS5 (no
  passwordless sudo there; XVF mic absent → phone/relay or USB calibrated mic).
- **H1 — Level-ramp on-device tuning** (τ cadence, AGC-freeze on iOS/Android).
- **H2 — Active-crossover on-device *sanity-check*** of the P7 flow (guided L1
  level-match woofer→tweeter; L2 calibrated null-margin polarity). The flow is
  built hardware-free in P7; H2 is the acoustic proof only.
- **H3 — Bass timing:** sub-level ramp + sub↔mains delay/polarity.
- **H4 — Future delay/phase:** the timing-locked reverse-polarity null-walk
  (driver time-align + sub delay), calibrated-mic-gated; the delay filter slot
  already exists.

## 6. Decisions (locked)

- Kernel module: `jasper/audio_measurement/`.
- LLM: OpenAI-first (GPT-5.4), swappable via the `provider` field.
- Shared target across Room + Preference: yes. Auto-revert on clear regression: yes.
- Pipeline branches on active/passive; active is primary/foundational.
- Safety hard-blocks; measurement-quality + preference never block.
- Bass management includes delay/polarity (rides the null-walk; wireless sub adds
  transport delay).
- Hardware validation on JTS5 (H0 confirms relay/measurement there).
- **Corner precedence (default):** when a speaker is both an active main and
  bonded to a wireless sub, the active-speaker local config owns the crossover
  corner; the wireless-sub path defers to it (one writer); mains-HP applied once.

## 7. Execution model (orchestration)

Follows the JTS orchestrator pattern (memory: `orchestrator-pattern-default`).

- **Orchestrator = Fable (Mythos-class), max effort.** Decomposes each phase into
  small PRs, directs implementer subagents, runs review gates, verifies
  load-bearing claims personally, decides merge. Stays in the loop between PRs.
- **Implementers = subagents.** Opus (effort xhigh) for reasoning-heavy or
  safety-critical work (L0 gate, kernel extraction, ramp math, verify-loop, graph
  safety); Sonnet-5 (effort max) for well-specified mechanical work (JSON
  envelope wiring, docstring/doc fixes, the canvas fill, test scaffolding).
- **Reviewers = separate subagents** (always isolated from the implementer),
  running **Jasper's canonical staff-maintainer adversarial review prompt**
  (memory: `reference_adversarial_review_prompt`), verbatim, scope-adapted per PR,
  with structured output `{blockers, should_fix, nits, findings[], report_md}`.
  **Model is tiered by criticality, both at max effort:** **critical** reviews —
  anything touching audio/hearing safety, the CamillaDSP graph, DSP math, or
  secrets — run on **Fable (max)**; **all other** reviews (web/UX, the level-ramp,
  mechanical, docs) run on **Opus (max)**. For the safety-critical PRs (L0 gate,
  ramp ceiling, bass safety) run a **perspective-diverse panel of Fable-max
  reviewers** (correctness + hearing-safety + resilience lenses), not one reviewer.
- **Per-PR loop:** plan → implement (Opus/Sonnet) → self-verify (`scripts/test-fast`
  then `scripts/test-merge`; ruff; mypy; shell/rust lanes as relevant) →
  adversarial review → fix to **zero Blocker + zero Should-fix** → Fable verifies
  the load-bearing claims against the code → docs-impact (`scripts/docs-impact.py`,
  `docs-linkcheck.py`) → PR to `main`.
- **Merge gate (hardware-away adaptation):** CI green + adversarial-clean
  (0 Blocker/Should-fix) + Fable's independent verification + docs scanned. Because
  Jasper is away, the usual "hardware-validate before PR" becomes **"attach the
  on-device validation checklist and mark it pending"** — no PR claims on-device
  behavior it hasn't proven; anything that changes live audio on a box ships behind
  its existing default-off/gated posture until §5 validation runs.
- **Coordination:** `main` moves fast (Claude + Codex). `git fetch` + rebase before
  each push; short-lived branches; one concern per PR.

## 8. Risks & open items

- **Acoustically unproven Layer A.** The whole crossover-measurement edifice is
  untested on real drivers; H0 is the gate before trusting L1/L2 numbers. The
  hardware-free work (L0 gate, kernel, safety) is valid regardless.
- **JTS5 relay/measurement unknown.** Whether the relay is ported/working on JTS5
  is unconfirmed — H0's first job.
- **Paid LLM cost** (P6) — reuse the shipped cost discipline; never CI on every commit.
- **Corner precedence** for main+wireless-sub uses the §6 default unless revised.

Last verified: 2026-07-03
