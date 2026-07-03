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
design-of-record (`HANDOFF-active-speaker-dsp.md`'s "Layer Boundary" section
documents Layer A/B/C, and the CamillaDSP graph already composes them in
order behind a `volume_limit: 0.0` ceiling), the shared measurement core
already exists (`active_speaker`
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
`AutolevelController` into a shared `RampController` in `jasper/audio_measurement/`.

**Transport — batched, not singular.** The relay `event` channel is a pure
last-write-wins single slot (`relay/src/worker.js::postEvent` overwrites
`meta.event` on every post) read by the Pi's ~0.75 s poll
(`capture_relay/session.py::DEFAULT_POLL_INTERVAL_S`). Singular per-sample
`{level:{...}}` events would be decimated to ~1 Hz with every intervening
phone post silently lost — not the dense series any transport-delay recovery
needs. So level events carry **batched, client-timestamped sample arrays**:
a rolling 2–4 s window of `{seq,t_client_ms,rms_dbfs,peak_dbfs,clip,
agc_frozen}` samples, posted ≤2 Hz. This is still zero relay *schema* change
(same `event` slot, richer payload) but the brief must budget the rate: level
posts plus phone-status polls must fit the relay's 80 req/10 s per-phone-route
cap. **Race note:** phone `event` posts and Pi `host_event` posts each read
the whole `meta/<id>` R2 object and write it back (`postEvent` /
`postHostEvent`, both `putMeta` after an independent read) — under continuous
level streaming a Pi ramp-control post and a phone level post routinely
interleave and the last writer silently reverts the other's field (a lost
`aborted`, a lost stop/hold host-event). All ramp control signals must
therefore be **latched and idempotent**, with the Pi re-posting until its
signal is observed back, and every phone level event must carry the phone's
current abort/armed state as a superset envelope rather than relying on a
one-shot host-event round trip.
- Play a **quiet-start staircase ramp** (band-limited noise, ~1 dB per ~0.4–0.6 s
  step) of `main_volume` (already 0 dB-clamped in `camilla.py::_coerce_main_volume_db`).
- The phone computes mic RMS→dBFS locally (freeze getUserMedia AGC/NS/EC; report
  `agc_frozen` per event) and streams the batched samples above over the
  existing relay `event` channel.
- **Settle-based two-point mapping, not cross-correlation.** An earlier draft
  of this plan prescribed reusing `capture_relay/alignment.py
  ::cross_correlation_alignment` to recover the transport delay τ between the
  known played envelope and the received mic envelope; that's replaced here
  because the estimator is waveform-domain (48 kHz, 5 ms main-lobe exclusion,
  peak-to-second-peak confidence) and on a ~1 Hz *monotonic*-staircase
  envelope it is structurally near-degenerate — a ramp correlated with a ramp
  yields a broad unimodal plateau where τ can't be separated from the unknown
  amp gain, and the exclusion radius collapses to one sample so confidence
  reads ≈0 even on perfect data. The replacement never estimates τ at all:
  ramp coarse from quiet → once the (delayed) reported level crosses a
  conservative pre-window, **hold ≥ the max loop latency** → read the settled
  level (the gain map is now exact at that held point, transport delay
  already elapsed) → step or jump the computed remainder → hold again →
  require **k ≥ 3 consecutive in-window samples** before treating the level
  as trustworthy → lock. If a correlation-based estimator is ever revisited,
  it needs non-monotonic probe markers (known level dips, not a monotonic
  ramp) and its own validation — `alignment.py` reuse is only honest for
  sweep waveforms, not envelope series.
- **Stop-ahead** the instant the settled mic level enters the **−20…−12 dBFS**
  window with clip margin — never blast up to find it. If the ramp maxes out
  without reaching the window (amp too quiet), stop and tell the user to raise the
  analog amp; never exceed the 0 dB ceiling to compensate.
- **Failure and margin rules** (the previous draft left these unspecified):
  (a) a reading is only trustable once it clears **noise_floor + ~10 dB**
  (reuse the phone's existing `noise_floor` event — below that the RMS is
  ambient-dominated and the early ramp shape is meaningless); (b)
  `clip=true` on any sample is an **immediate abort**, not a data point; (c)
  `agc_frozen=false` (iOS has historically ignored the constraint request)
  **degrades to the existing manual-lock UX** with a nudge and disables the
  drift rule below — never silently trust an AGC-compressed level as a
  reference map; (d) the quantitative overshoot guard is
  `ramp_rate × max_loop_latency < half the window width`, and the ramp aims
  at the **window bottom** (−20 dBFS), not the center — at 1 dB/0.5 s with
  ~2 s of loop latency the played level is already ~4 dB past the newest
  report, which eats half of an 8 dB window if you aim for the middle; (e)
  `RampController` preserves `AutolevelController`'s existing safety
  semantics rather than loosening them: the **dynamic cap**
  (`original + 6` clamped to **[−20, −6] dBFS** `main_volume`) is the
  operative bound — tighter than, and not to be confused with, the 0 dB
  hard ceiling above — plus the existing safety timeout and
  graceful-fade-before-tone-kill.
- **Lock**, scoped **per mic-geometry step, not blanket per-session.**
  Near-field (Layer A, phone at the baffle) and listening-position (Layer B)
  differ by roughly 15–25 dB at the mic for the same played level — a
  listening-position lock reused for a near-field capture blows past the
  window into clip, and the reverse starves listening-position SNR. The flow
  re-ramps on every geometry transition (cheap once `RampController` exists);
  `MeasurementLevelLock` is the lock for the *current* geometry step, not one
  value for the whole session.
- **Drift check**, split by cause and computed on the right signal: at the
  **same geometry**, a *uniform* per-band dB shift vs the lock (|mean Δ| >
  ~3 dB, all bands within ±2 dB of the mean) means the amp/volume moved —
  flag + offer re-level. A geometry *change* expects a level shift and must
  not trigger that message — the two must not be conflated in the UI. A
  *non-uniform* change at the same geometry is acoustic, not a level drift.
  Critically, the drift reference must be stored from the **raw
  (pre-`normalize_to_band`) magnitudes** — every capture is normalized so its
  200–1000 Hz band-mean reads 0 dB, which erases exactly the uniform shift
  this check exists to catch; compare sweep-to-sweep on raw band levels
  (`raw_magnitude_db`, already retained in replay artifacts), not ramp RMS
  against sweep levels.

Hardware-free now: the algorithm, the relay batched-event schema, the
settle-based two-point mapping, the SNR-window/stop/lock/drift logic, all
under synthetic/mocked tests. Parked: the on-device settle-cadence tuning and
the iPhone/Android AGC-freeze confirmation (H1).

### 3.2 Room correction — simple, honest, dumb-frontend

- **One JSON screen envelope** per step (`{screen, curves{measured,target,
  predicted,verify — server-smoothed}, fill_segments[], headline{before,after,
  delta}, verdict_text, nudges[], next_action, progress}`). Browser is a pure
  renderer; all smoothing/thresholds/verdicts on the Pi. **Not built yet** —
  see P3b in §4; today's page computes some of this client-side.
- **Stepped wizard:** entry → mic + calibration *nudge* → level-match (§3.1) →
  guided N-position sweep with "move the mic" prompts → review vs target → apply
  → verify → **before/after result** → save. Every gate is a sentence + a
  checkmark; nothing disabled. **Not built yet** — P3b.
- **Honest two-tone before/after fill** — a ~40-line extension of the existing
  canvas `drawSpread()`: green where |after−target| < |before−target| (helped),
  amber where a band regressed. Headline one number: "±6 dB → ±2 dB in the bass."
  Never show a raw jagged curve — server-smoothed (variable for design,
  psychoacoustic for the "what you hear" view). **Shipped in P3a (pending
  merge)** — rendered into the existing single-page UI, not yet the §3.2
  screen envelope; P3b relocates it into the envelope, it doesn't rebuild it.
- **Real measured before/after delta** in `verify_metrics` — recompute the
  pre-correction deviation over the *same* 50–350 Hz band from the stored measured
  curve (do not reuse `design.before`, which is over a different band), and stop
  calling the *predicted* number "improvement." **Shipped in P3a (pending
  merge)** — same caveat as above.

### 3.3 Bass management — corner/slope/level now, delay/polarity via the null-walk

- **Ownership:** the Speaker layer owns crossover corner/slope, sub level, and
  sub delay/polarity; the Room layer corrects the *already-bass-managed summed*
  response (it *reads* the corner, never re-picks it); Preference owns the
  sub-bass shelf. One bass target — never double-cut.
- **The LR4 emit primitive is already shared** — `camilla_emit.emit_linkwitz_riley`
  is used verbatim by both `channel_split` and `active_speaker`. P5's real
  unification work is narrower than it first looks: the **duplicated corner
  constant/bounds** (`channel_split.DEFAULT_CROSSOVER_HZ` vs
  `profile.DEFAULT_SUB_CROSSOVER_HZ`, both already 80 Hz / 40–200 bounds, but
  two independent numbers that can drift) plus the §6 corner-precedence
  default for the main+wireless-sub case. Fix the stale `channel_split.py`
  "mains HP is a V1 non-goal" docstring while touching this file — it
  contradicts the shipped `reconcile.py` mains-HP path.
- **"Reads the corner, never double-cuts" — operative definition.** The
  measured listening-position response already *is* the acoustic sum, so
  "the room designer corrects the summed response" is automatic once it
  measures at listening position; the load-bearing rule is what the PEQ
  designer is forbidden from doing *near* the corner: **no boosts within
  ±1/3 octave of Fc** (an LR4 sum is flat there by design — a measured dip at
  the corner is usually phase/placement, not a room mode, and boosting it
  fights the crossover rather than correcting the room). The room designer
  receives the active corner value, and the envelope's `verdict_text`
  distinguishes "that's your crossover, not a room mode" from a genuine
  room-mode call.
- **Timing is part of bass management.** Sub level rides the §3.1 ramp
  (band-limited to the overlap region). Sub↔mains **delay/polarity** rides the
  same timing-locked null-walk as driver time-align (parked, hardware) — with the
  extra wrinkle that a *wireless* sub also carries snapcast transport delay to
  account for.
- Hardware-free now: unifying the corner constant/bounds, the corner-precedence
  default, the near-Fc no-boost rule + verdict-text distinction, building out the
  bass wizard, and the stale docstring fix. Parked: sub-level ramp on-device and
  sub↔mains delay.

### 3.4 The tuning LLM — OpenAI-first, one agent spanning both jobs

Reuse the shipped `calibration_agent` propose→validate→execute→revert contract;
extend it, don't rebuild it.

- **Provider:** start on the existing OpenAI adapter — the seeded config
  default is a current GPT model (do not doc-pin a model name here; a model
  rename is a config-value change, not a plan-doc edit). The `provider` field
  on `AdvisorModelSettings` is the swap seam for better models later, though
  today `resolve_model_settings` hard-rejects any `provider != "openai"` —
  that's the intended current state, not a gap to close in P6. (Correct the
  design doc's stale "Anthropic-first" mandate to "OpenAI-shipped,
  provider-swappable.")
- **Key provisioning:** reuse `OPENAI_API_KEY` from the existing
  `/var/lib/jasper-secrets/voice_keys.env` — it's already there whenever the
  household's voice provider is OpenAI, and `jasper-web` already has group
  read on that file (WS1 Phase 4a). Don't provision a second copy. When the
  household is on Gemini/Grok voice and no OpenAI key exists, the tuning-LLM
  surface is **hidden with a nudge**, never a broken button. Confirm which
  process makes the paid call (correction runs under `jasper-web` today) and
  that it actually has compartment access before wiring the live surface.
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
- **P3a — Room correction: honest before/after (§3.2, shipped once merged).**
  The measured before/after delta and the Pi-computed `fill_segments`,
  rendered into the existing single-page UI; the predicted-vs-measured
  relabel. This is the piece already built on the `p3a` branch — landing it
  is a merge, not new design work.
- **P3b — Room correction: the screen envelope + stepped flow (§3.2, not yet
  built).** The `{screen, curves{measured,target,predicted,verify}
  ,fill_segments[],headline,verdict_text,nudges[],next_action,progress}` JSON
  envelope endpoint, the stepped dumb-frontend wizard, and the mic/calibration
  nudges — genuinely unbuilt (P3a shipped its results into the current
  single-page UI, not this envelope). Given the current file sizes
  (`correction_setup.py` ~3,000 lines, `main.js` ~2,500 lines), **decompose
  into at least two PRs**: (1) the envelope endpoint added *additively*
  alongside today's payloads, so nothing consuming the old shape breaks; (2)
  the page migrated to consume the envelope and the legacy client-side
  computation retired. A one-shot rewrite of both files is exactly the
  long-lived-branch staleness profile AGENTS.md warns about on this fast
  `main`.
- **P2 — Level-match ramp (§3.1)** logic: `RampController` + the relay's
  batched level-event schema + settle-based two-point mapping + SNR-window
  stop + per-geometry lock + drift (on raw magnitudes), all under synthetic
  tests.
- **P4 — Verify-acceptance loop.** `AcceptanceEvaluator` (store predicted curve
  at apply → after verify compute error-to-target reduction + a "did any band
  get worse" guard → accept / surface / auto-revert on clear regression),
  under synthetic before/after captures. **The acceptance rule, concretely**
  (a naive per-band comparison would revert good corrections on measurement
  noise — see §8): (1) aggregate to **≥1/3-octave smoothed bands** before any
  per-band verdict — never judge on raw per-bin noise; (2) "clear regression"
  = a band worsening **beyond the repeatability floor**, seeded from
  `spatial.py`'s existing 4–6 dB std constants and shipped as **env-tunable
  knobs** (mirroring the `JASPER_CAPTURE_ALIGNMENT_THRESHOLD` pattern in
  `alignment.py`), retuned once H1 supplies real on-device repeatability data,
  **and** an overall RMS delta that's negative beyond noise — not either alone;
  (3) **matched comparison basis** — verify is captured at position 1 (a flow
  instruction) and/or compared against the stored position-1 curve, never only
  against the multi-position average, so before and after are apples-to-apples;
  (4) **one confirmatory re-measure is required before auto-revert** — a
  second concordant verify is cheap, a false revert is trust-expensive; (5)
  every accept/surface/revert verdict emits an `event=` log, lands in the
  envelope's `verdict_text`, and is recorded in the evidence bundle.
- **P5 — Bass management unification (§3.3, non-timing):** unify the
  duplicated crossover-corner constant/bounds and apply the §6 corner-
  precedence default; room correction reads the corner and enforces the
  ±1/3-octave no-boost rule near Fc with the crossover-vs-room-mode verdict
  distinction; build out the bass wizard; fix the stale docstring.
- **P6 — Tuning LLM (§3.4):** extend the advisor vocabulary to target + correction
  moves; reuse the existing OpenAI key from `jasper-secrets` (hide the surface
  with a nudge when no OpenAI key is configured); surface the interpreter in
  the flow; the confirm-gated proposer with simulate-before-apply. (Paid-call
  cost discipline per AGENTS.md — never in CI.)
- **P7 — Active-crossover measurement flow (hardware-free shaping).** The Layer-A
  commissioning *flow* is fair game now — only its acoustic proof is parked
  (H2). Wire the relay `crossover_sweep` spec into `correction_crossover_flow` so
  all layers ride one transport + one upload seam; align the commissioning
  sequence with **P3b's** screen envelope (P3b defines it, P7 consumes it —
  see the ordering note below for why P7 sits after P3b and P2); ride the
  shared kernel, **P2's `RampController`**, and the L0 gate; tidy
  `commissioning_coordinator`. Every acoustic assumption gets an on-device
  sanity-check line for H2, not a claim.

**Cross-cutting, every phase:** ships `event=` structured logs for its new
state transitions, a schema-version bump plus a pinning test for any
bundle/`result.json`/envelope field addition, and env-knob defaults (not
hardcoded constants) for any threshold whose true value is hardware-gated.

**Ordering: P3a → P3b → P2 → P7 → P4 → P5 → P6** (P3b and P2 are
parallelizable — disjoint files: the room-flow envelope vs. the ramp
controller — but coordinate on the session/status touchpoints they both
write; P4 may move ahead of P7 if P7 stalls, since verify curves are already
band-normalized and measure/verify already share one locked volume per
session). **P7 explicitly consumes P3b's envelope and P2's `RampController`**
— this resolves, in one direction, the plan's prior internal contradiction
where P7's own brief said it "rides the level-ramp" while the ordering
placed P7 before P2 (built first, P7 could only wire the existing
browser-locked `AutolevelController` and would need rework once
`RampController` landed). If P7 must start earlier than this order allows,
its brief scopes it explicitly to the existing `AutolevelController` behind
a named `RampController` seam, rather than silently building on the old ramp.
P3a is a merge, not a phase — it lands first regardless. P5 and P6 stay
last. Foundation (P1) first in all cases.

## 5. Roadmap — hardware-gated (parked until hardware in hand)

Each carries an on-device validation checklist attached to its PR; **none merges
as "validated" — it merges as "hardware-free complete, on-device pending."**

- **H0 — Prove the loop on JTS5.** A throwaway CLI spike routing a sweep through
  the production active graph to one driver, printing a real level-match number —
  AND confirming the relay + measurement capture actually work on JTS5 (no
  passwordless sudo there; XVF mic absent → phone/relay or USB calibrated mic).
- **H1 — Level-ramp on-device tuning:** settle-cadence tuning, AGC-freeze
  confirmation on iOS/Android, **and** derivation of the P4 acceptance
  thresholds and the P2 window/drift constants from measured on-device
  repeatability — the env-knob defaults seeded in P2/P4 are placeholders
  until H1 supplies real numbers.
- **H2 — Active-crossover on-device *sanity-check*** of the P7 flow (guided L1
  level-match woofer→tweeter; L2 calibrated null-margin polarity). The flow is
  built hardware-free in P7; H2 is the acoustic proof only.
- **H3 — Bass timing:** sub-level ramp + sub↔mains delay/polarity.
- **H4 — Future delay/phase:** the timing-locked reverse-polarity null-walk
  (driver time-align + sub delay), calibrated-mic-gated; the delay filter slot
  already exists.

## 6. Decisions (locked)

- Kernel module: `jasper/audio_measurement/`.
- LLM: OpenAI-first (the seeded config default, not a doc-pinned model name —
  see §3.4), swappable via the `provider` field.
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
- **Reviewers = separate Fable (max effort) subagents for ALL adversarial
  reviews** (always isolated from the implementer), running **Jasper's
  canonical staff-maintainer adversarial review prompt** (memory:
  `reference_adversarial_review_prompt`), verbatim, scope-adapted per PR, with
  structured output `{blockers, should_fix, nits, findings[], report_md}`.
  Safety-critical PRs — anything touching audio/hearing safety, the
  CamillaDSP graph, DSP math, or secrets — get a **perspective-diverse panel
  of Fable-max reviewers** (correctness + hearing-safety + resilience
  lenses), not one reviewer. **Opus/Sonnet are implementation-only and never
  review.**
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
- **§3.1 transport delay was mis-specified in an earlier draft** (singular
  events over a last-write-wins relay slot, cross-correlated with a
  waveform-domain estimator on monotonic envelopes) — would have made the
  ramp fail-loud essentially always, or spuriously pass on a loudness-deciding
  path. Resolved by the batched-transport + settle-based-mapping rewrite in
  §3.1; P2's brief should not need to rediscover this.
- **§4/P4's naive acceptance rule would revert good corrections on measurement
  noise** — comparing a single verify position against the multi-position
  average, unqualified, sits inside the repo's own 4–6 dB seat-to-seat
  repeatability floor (`spatial.py`). Resolved by the concrete acceptance
  rule (1/3-octave aggregation, env-tunable repeatability threshold, matched
  comparison basis, confirmatory re-measure before revert) now in P4's bullet
  in §4.

Last verified: 2026-07-03
