# Handoff: shared audio measurement & calibration core

> **Status: living architecture & product plan, created 2026-06-19.**
> This doc owns the cross-cutting plan for turning JTS's audio
> measurement/DSP/calibration work into ONE shared core that three
> consumers build on — **room correction**, **active-crossover
> calibration**, and **pair/leader-follower balance & sync** — plus the
> layered calibration *product* (L0/L1/L2) and a regression-safe refactor
> roadmap. It is the **output/measurement-side sibling** of
> [HANDOFF-audio-capability-platform.md](HANDOFF-audio-capability-platform.md)
> (which owns the *input* side: mic/AEC/DAC hardware capability). Backing
> safety/DSP contracts stay canonical in
> [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md),
> [HANDOFF-correction.md](HANDOFF-correction.md), and
> [HANDOFF-volume.md](HANDOFF-volume.md). Research backing:
> [docs/research/2026-06-19-active-crossover-calibration/](research/2026-06-19-active-crossover-calibration/README.md).
> The manual-or-measured crossover-builder product contract is canonical in
> [active-crossover-information-design.md](active-crossover-information-design.md);
> this document owns only the shared measurement architecture and policies.

---

## TL;DR

The audio subsystem is the heart of JTS and should become **one clean,
resilient measurement/calibration core** that distinct consumers ride as
thin adapters — not three parallel stacks. The good news from the
2026-06-19 audit: **this is mostly consolidation + wiring, not a
ground-up build.** The room-correction measurement pipeline is
production-grade and *already* reused by the others (active-speaker's
`driver_acoustics.py` imports `jasper.audio_measurement.{sweep,deconv,analysis,quality}`;
`balance_flow.py`/`sync_flow.py` import `correction/coordinator.py`'s
`measurement_window`). The work is to (1) formalize that shared core,
(2) kill the duplicated graph-safety parsing, (3) close the
already-built-but-unwired active-speaker measurement loop, and (4) ship a
layered calibration product anyone can use.

The product is three tiers:

- **L0 — the crossover is actually applied, fail-closed.** Foundational.
  On the JTS3 lab Pi today it is **not** (the live CamillaDSP graph is a
  flat passthrough — see "Current state"). This is the real cause of the
  "shrill / horn far too powerful" symptom, and the first thing to fix.
- **L1 — phone-mic woofer↔tweeter level matching.** No special hardware.
  Relative level is a ratio measurement; an uncalibrated phone mic is good
  enough (±3–6 dB) with guardrails. One fixed trim, measured once.
- **L2 — calibrated-mic FR / phase / null-depth.** Optional, for users
  with a measurement mic (the maintainer's Dayton USB-C). Reuses the
  existing `correction/calibration.py` upload path. This is active-speaker
  commissioning **Stage 6 (sweep+measure)** and **Stage 7 (freeze)**.

---

## Current state (verified against the Wave 1 worktree and its `origin/main` base, 2026-07-13)

### Wave 1 contract-only foundation (2026-07-13)

Wave 1 adds strict, pure boundary values; it does **not** change the current
measurement, playback, bundle, DSP, or Room-correction flows:

- `excitation_admission.py` owns one deterministic allow/refuse calculation over
  an exact request, caller-composed limits, and fresh-protection claim. The
  request and authority bind target, safety profile, excitation plan, closed
  band, effective peak, duration, and repeat count. The owning adapter must
  intersect code-owned, confirmed-profile, and plan limits; bind the plan to
  normalized stimulus/generator/effective-peak inputs; issue protection evidence
  from a fresh graph readback; and rerun admission immediately before playback.
  The strict SHA-256 values are content identities, not signatures, trusted
  issuers, or transferable playback capabilities. This slice has no producer or
  live consumer and performs no signal generation, graph I/O, audio, or
  persistence.
- `evidence_identity.py` adds neutral `ArtifactIdentity`, `CaptureIdentity`, and
  `ReplayIdentity` values. They bind exact feature-owned files, raw captures,
  replay inputs, admission artifacts, algorithm id/version, geometry, placement,
  and context. They do not read files, migrate Room or Active bundle formats,
  decide quality, own a verdict, or promote a forensic bundle into authority.
  Existing `jasper.correction.bundles` and `jasper.active_speaker.bundles`
  retain their current ownership and behavior.
- The same module distinguishes a normalized CamillaDSP `active_raw` content
  identity from the exact host transaction/rollback state. The latter reuses
  the existing `null_walk.DspPredecessor` canonical JSON and fingerprint rules.
  No generic graph-transaction abstraction landed. The feature host must still
  hold the real writer lock across apply, fresh readback, and exact restoration.
- Active owns the nine-state lifecycle and positive eligibility receipt built on
  these shared identities. The lifecycle's
  `blocked_live_state_unknown` state prevents an attempted/unknown mutation from
  returning to an ordinary pre-mutation block without exact restore evidence.
  A positive receipt requires an evaluated-`verified` topology and every
  topology-derived combined active-speaker
  target to pass exactly three distinct admitted fixed-axis post-apply captures
  from one session/threshold profile, and binds the retained applied candidate,
  fresh graph proof, predecessor, and rollback result bound to the same
  operation, mutation, and observed applied graph. Those types are
  inert until the Active integration lane issues and persists them.

Contract status is not current `/state`. Existing Active bundles remain
forensic/fail-soft, and Room still gates from the applied recomposition snapshot;
neither `active_speaker.setup_status` nor `/correction/start` parses the new
receipt. No hardware behavior was changed or revalidated by Wave 1.

### What exists and is production-grade
- **Measurement kernel** (the pure primitives now in `jasper/audio_measurement/`
  since P1b; the correction-specific rest stays in `jasper/correction/`):
  `sweep.py` (Novak ESS), `deconv.py` (FFT/Tikhonov IR), `analysis.py` (octave
  smoothing, log resample, band normalize), `quality.py` (+ correction's
  `acoustic_quality.py`) (SNR/clipping gates), `calibration.py` (Dayton/miniDSP/
  UMIK lookup + upload), `snr_policy.py` (the crossover-builder Slice 0
  band-specific, decision-class-split SNR gate — `band_levels_dbfs` moved
  verbatim from `correction/session.py._band_levels_dbfs`, which now
  delegates to it; `band_snr_verdicts` / `cap_null_depth_db` are new,
  consumed by `active_speaker/driver_acoustics.py` and
  `crossover_alignment.py`) — all under `jasper/audio_measurement/`; plus,
  staying in `jasper/correction/`: `confidence.py`, `coordinator.py`
  (`measurement_window`: pauses renderers + voice, serializes), `session.py`
  (`MeasurementSession` state machine), `bundles.py` (schema-versioned
  durable evidence). Shipped, tested.
- **Shared browser-mic capture**: `deploy/assets/shared/js/measurement-audio.js`
  (mono 48 kHz, AGC/EC/NS hard-coded off) + `correction/browser_audio.py`.
- **Active-speaker subsystem** (`jasper/active_speaker/`): commissioning
  stages 1–5 (muted load → per-driver unmute
  at a floor → audible gain ramp → audible-evidence confirmation), driver
  research/preset, `camilla_yaml.py` per-driver Gain/Crossover emit,
  `driver_protection.py`, `safe_playback.py`, runtime contract & staging.

### What is already shared (the core is now an explicit package)
- The pure primitives live in `jasper/audio_measurement/` (P1b extraction):
  `sweep`, `deconv`, `analysis`, `calibration`, `quality`, plus a parameterized
  `quality_model.QualityModel` (`ROOM` / `DRIVER` / `RAMP` profiles) that
  replaced the previously-forked capture-quality constants. Moved verbatim from
  `jasper/correction/`, which now *consumes* the kernel. P2 added `ramp` —
  the settle-based level-match `RampController` / `MeasurementRamp` (the
  generalization of `correction/autolevel.py`; that browser-locked controller
  remains the no-relay local fallback). The ramp's control-loop tuning lives on
  `MeasurementRamp` (validated, env-overridable), not on the `RAMP` quality
  profile. Crossover-builder Slice 0 (see
  [active-crossover-information-design.md](active-crossover-information-design.md)
  "Level control and SNR") added `snr_policy.py` — `band_snr_verdicts` splits
  SNR trust by decision class (magnitude/trim reuses `snr_ok_db`/`snr_warn_db`;
  null/alignment reads the new `QualityModel.alignment_snr_ok_db` (35 dB) and
  rejects scalar-only evidence) and `cap_null_depth_db` caps a measured
  reverse-polarity null to what the overlap-band SNR can prove
  (`QualityModel.null_cap_margin_db`, 10 dB). Both new fields default
  identically across `ROOM`/`DRIVER`/`RAMP`, so room correction (which does
  not call `snr_policy` yet) is unaffected.
- `null_walk.py` is the shared decision foundation for active-driver and
  sub-to-mains timing. Its signed coordinate names both possible delay targets
  and maps either sign to a non-negative target-specific DSP operation. It
  bounds an exhaustive search to ± half one crossover period, accepts only
  50–100 µs grids, and selects only after *every* candidate has at least five
  calibrated reverse-null captures from that exact crossover region, each
  gated, above-floor, alignment-SNR-qualified, and with <2 dB null spread. Its
  public capture input has no impulse-arrival field. The injected runner rejects
  explicit apply/restore failure and reports a walk failure together with a
  restore failure. Before its first candidate mutation, the runner requires a
  host-owned `DspPredecessor` carrying the exact entry-state payload; it freezes
  the unambiguous JSON data model at the transaction boundary, derives its
  canonical SHA-256 fingerprint, and passes a fresh copy of that snapshot to the
  subsystem restore adapter. Restore must read back the active DSP state and
  build `DspRestoreConfirmation` from that observation; the runner compares its
  fingerprint with the predecessor. Restore runs in a dedicated task shielded
  from repeated caller cancellation with a 15-second cancellation deadline by
  default (30-second configured maximum); wall completion also includes the
  host adapter's own bounded cancellation drain. Candidate DSP mutation is
  likewise shielded and settled before restoration starts, so a cancelled
  offloaded worker cannot finish after rollback and put the candidate back
  live. Host adapters must
  bound and cancellation-drain their mutation I/O (the shared Camilla controller
  does), and orchestration must exclude concurrent DSP writers for the whole
  walk. Cancellation is propagated only after restoration terminates; if
  restore also fails, the runner preserves the entry failure, a cancellation
  observed during cleanup, and the restore failure in causal order in a
  `BaseExceptionGroup`. Timeout, refusal, or a mismatched read-back fails loudly.
  Lifecycle evidence uses the generic `correction.delay_walk_*` event family
  with one required closed scope declared by each adapter:
  `active_crossover` or `bass_management`. Failure events expose only the closed
  `failure_code` vocabulary (`timeout`, `readback_mismatch`,
  `invalid_confirmation`, `self_cancelled`, or `other`); arbitrary exception
  text and the snapshot payload never enter the journal. Subsystem adapters
  still own the actual DSP mutation, exact restore, read-back, writer exclusion,
  and capture transport. The exhaustive runner preflights and refuses above 25
  candidates or beyond CamillaDSP's 20 ms delay ceiling before touching DSP.
  `delay_graph.py` is the inert candidate graph-*content* seam beside that
  runner. Inside an outer exact-restore transaction, a host stages both delay
  lanes to numeric zero and supplies the same `DspPredecessor` the F1 runner
  will restore, with parsed CamillaDSP `active_raw` in its frozen state. Typed
  bindings carry the owning host's exact non-empty topology channel set plus one
  non-Delay identity filter from that target's emitter-owned chain. Mono roles
  use a one-channel tuple; stereo role chains can use sets such as `[0, 2]`.
  Bindings are admitted only when the identity and Delay filters each occur in
  exactly one shared pipeline step over that exact channel set; unused, extra,
  missing, overlapping, duplicated, swapped, malformed, or unknown-target
  bindings refuse. The shared core does not parse scope-specific filter names.
  `DelayGraphSnapshot` fingerprints those graph-derived lane proofs with the
  scope, topology id, and complete walk spec. `confirm_delay_candidate` proves
  only that supplied graph content is the zero-relative predecessor with the
  requested lane's four-decimal-quantized millisecond delay as the sole changed
  field. It derives the signed relative delay from both bound slots and requires
  a real numeric non-positive `devices.volume_limit`; every other graph value,
  including any pre-existing compensated positive PEQ, must remain byte-model
  equivalent in canonical JSON. This helper does **not** establish that a
  read-back is live, fresh, or from the current writer transaction. F2b must
  own writer-locked candidate apply → fresh `active_raw` → typed confirmation,
  bind that confirmation to the current run/evidence, and feed it into the F1
  runner. Until that host contract lands, stale/replayed but content-identical
  graphs remain an explicit integration gap, not admitted measurement
  authority. Emitted-file hashes are never compared with CamillaDSP's
  normalized/default-expanded graph. Production CamillaDSP/web/persistence
  wiring is not shipped yet, and low-frequency bass walks require a separately
  reviewed adaptive scheduler before they are executable.
- The relay level target is reusable state, not a long-lived live gain. A
  successful ramp restores the original listening volume immediately. Room,
  verification, and active-crossover adapters reassert the target only inside
  the serialized `measurement_window()` that owns playback, then restore it in
  that window's `finally` before renderers resume. The shared ensure/restore
  transition lock makes concurrent cleanup idempotent and retryable. Room and
  active-crossover adapters may accept the kernel's explicitly degraded
  `bounded_low_level` result only after its unchanged AGC, clip, liveness, SNR,
  spread, and shortfall gates pass; the relay establishes the SNR floor from a
  short rolling ambient median rather than one microphone-startup block.
  Room alone allows the listening-position ramp +15 dB of travel up to the
  unchanged 0 dB hard ceiling because its stimulus is already −12 dBFS;
  crossover/near-field keeps the shared +12/−3 cap. Ramp snapshots retain
  compact admission counts plus maximum observed RMS, peak, trust threshold,
  and trust deficit for an exact zero-trusted-sample diagnosis.
- `active_speaker/driver_acoustics.py` **imports**
  `jasper.audio_measurement.{sweep, deconv, analysis, quality}` and the `DRIVER`
  quality profile — it reuses the shared DSP verbatim.
- `web/balance_flow.py` + `web/sync_flow.py` **import** `measurement_window`.
  Their `/start` dispatches first consult correction's read-only
  `_correction_start_blocker`; the correction `/start` path alone uses
  `_reserve_start_slot`. The coordinator's atomic `measurement_window` mutex
  is the final race-free exclusion once any of those flows begins opening a
  window.
- `jasper/measurement/` now holds the first small shared primitives outside
  correction: `level.py` retains browser-mic dBFS frames and derives backend
  floor/target/liveness, while `volume_guard.py` snapshots, normalizes, and
  restores owned output-volume controls for guarded calibration sessions
  (first consumer: pair balance, including Snapcast client volume/mute). The
  flow owner, not the browser, decides how long missing/stale mic evidence may
  block a measurement before failing visibly.
- `commissioning_capture.py` accepts a calibration flag and routes to the
  same analysis. Formalizing a "core" mostly *names* a dependency that's
  already there — that's why the refactor is low-risk.
- **Active-crossover repeat + SNR controller (2026-07-12).** The protected
  level probe now owns only a safe, non-clipping playback level. Each driver
  measurement holds a 14-second controlled quiet interval followed by a
  role-sized ESS (woofer/subwoofer 12 s, midrange 8 s, tweeter 4 s), and
  compares deconvolved sweep-band magnitude against ambient passed through
  the same regularized inverse, signal-owned arrival window/reflection gate,
  and calibration domain (ambient noise never selects its own IR argmax).
  Because the phone records before posting `armed`, a bounded 16 kHz locator
  finds the sweep after relay latency. Separate, real, equal-length full-rate
  signal and quiet crops traverse the same inverse and signal-owned gate; there
  is no guessed prefix, tiling, or zero padding.
  The normal path collects three exact-position repeats,
  keeps their WAVs and acceptance evidence, and writes one durable driver
  measurement only after the shared repeat aggregator accepts at least two;
  one bounded fourth attempt is allowed. Repeat state is scoped by comparison
  set plus immutable target fingerprint and atomically persisted before audio
  by `active_speaker.repeat_admission`; bundles remain optional forensics. A
  final measurement stores only the compact repeat projection; the full
  process-local winning attempt remains bundle evidence. A failed measurement
  write moves `ready` to `aborted`; a failed admission-completion write does
  the same with a distinct reason. If either follow-up abort write succeeds,
  the envelope can immediately require a new level check. If that abort write
  also fails, same-process `ready` remains fail-closed and blocks replay and
  automatic apply; it becomes actionable when the next service-start ownership
  claim retires the old owner. A service restart preserves the bounded attempts: the
  single-process startup claim marks an old `active` or `ready` set aborted
  rather than silently restarting at one. In every interrupted case the
  envelope requires a new level check. A new setup cannot inherit it. If
  attempt four fails in transport but
  two prior deconvolved captures were accepted, the same shared finalizer keeps
  them at reduced confidence; fewer than two refuses the set.
  This closes the prior live-hardware `acoustic.snr: null` path without making
  the probe's raw RMS an acoustic SNR verdict.
- **Lane B fixed-axis admission contract (2026-07-12).** Driver analysis no
  longer accepts `capture_geometry` from the browser. It derives near-field vs
  reference-axis from a complete relay proof revalidated against the active
  comparison set, physical target fingerprint, group, role, capture build, and
  acknowledgement/session binding. Summed analysis uses the same proof seam;
  browser geometry is never authoritative. The future LF far-field capture
  enters the existing repeat admission, controlled
  ambient, excitation ledger, placement proof, bundle, and measurement-state
  path. Reference-axis IR gating is tri-state: a finite measured/search-bound
  floor is known, a crossover below it is invalid, and an ungateable IR is
  unknown. Unknown refuses the repeat and persists as JSON `null`; it is never
  treated as implicitly above the floor. A fixed-axis repeat also requires one
  complete immutable placement/comparison binding, and near-field/fixed-axis or
  cross-binding repeats cannot share an aggregate. Automatic summed alignment
  independently requires fixed-axis geometry, an applied finite validity floor,
  and `above_validity_floor is True`. Measurement state keeps near-field and
  fixed-axis latest-record indexes separate, so a later far-field capture cannot
  replace the near-field evidence used for level trims.
- **Lane B fixed-axis capture flow (2026-07-12).** After every driver's
  near-field repeat set completes, the server-authored crossover envelope keeps
  the microphone stationary on the tweeter reference axis, acquires a separate
  safe level for each isolated driver, and targets three gated repeats through
  the same relay/ambient/excitation/persistence kernel. A bounded fourth attempt
  may replace a rejection; automatic apply requires three accepted repeats in
  both geometries. The kernel may retain a two-accepted reduced-confidence
  aggregate for diagnosis, but the shared apply gate refuses it. Near-field and
  reference-axis attempts have distinct durable controller identities and
  geometry-scoped level locks; neither can continue or complete the other.
  The fixed-axis level uses the listening-position `+15 dB` / hard `0 dB` cap,
  and its exact reasserted lock is part of the played excitation ledger.
  Request geometry is only a hint until the route proves it equals the
  envelope's next action; the relay acknowledgement policy is the authority at
  playback and analysis. Both geometry stages participate in the correction
  adapter's durable crossover-volume lease; the detailed persistence, readback,
  restart, and recovery contract is owned by
  [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md) under the
  consumer measurement protocol. Partial process identity is always discarded;
  service restarts preserve completed durable near-field work. The envelope and
  direct automatic apply share one pure gate requiring the current comparison,
  topology, protected profile, usable unclipped/gated acoustic evidence, and
  exact completed near-field/reference-axis controller fingerprints for every
  active driver. This slice still
  does not perform baffle-step correction or splice the raw responses; that
  consumer is Lane B's next slice.

  `acoustic.fr_curve` remains a peak-normalized display surface and must never
  be treated as physical splice evidence. Existing driver repeat artifacts now
  store a versioned `analysis_input` beside each immutable raw WAV: exact
  generated-sweep metadata, played excitation/level ledger, capture geometry,
  ambient duration, and a serial-free snapshot of the applied calibration
  curve. The splice lane must replay those inputs with
  `magnitude_response(..., normalize=False)` so calibrated amplitude is not
  lost by subtracting two normalized display plots.

### The gaps (worktree-confirmed)
- ~~**Active-speaker measurement loop is built but UNWIRED.**~~ **CLOSED.**
  The measurement loop *is* wired. The live browser mic-capture surface is the
  HTTPS `/correction/crossover/` page through the Jasper relay:
  `/crossover/relay-capture` → `correction_crossover_backend` →
  `web_measurement.record_driver_capture` /
  `record_summed_capture`, which run `driver_acoustics`
  (`record_driver_acoustic_capture` / `record_summed_acoustic_capture`) and
  persist the real acoustic verdict block into measurement state (the 2026-06-19
  audit inspected a pre-wiring snapshot — the wiring landed 2026-06-18). **L1
  then closed the level-match loop (2026-06-20):** each per-driver capture also
  records an **overlap-band level** at the crossover Fc, and
  `baseline_profile._measured_level_trims` chains the driver-to-driver overlap
  deltas into a per-driver attenuation candidate. A safe applied manual
  crossover remains valid for room correction; operator-pinned values keep
  ownership until the user explicitly applies the automatic candidate, at which
  point its complete measured data replaces those pins. Automatic analysis never
  silently overwrites manual settings. Missing/silent/clipped/low-SNR evidence
  blocks that automatic replacement. **Product routing changed
  2026-06-23:** the core `/sound/` active-crossover walkthrough does not expose
  browser mic capture (it is plain HTTP and cannot `getUserMedia`); it uses
  by-ear driver and combined confirmations, then hands users to the HTTPS
  `/correction/crossover/` measurement experience for acoustic proof. The old
  `/sound/active-speaker/driver-capture` + `/summed-capture` routes — a verbatim
  duplicate of the `web_measurement` capture path that nothing reached after the
  move — were deleted (Codex-week review C4a-1). See "L1 measured level match"
  below.
- ~~**`DriverSpec.sensitivity_db` is stored but never read to set gain.**~~
  **CLOSED.** `baseline_profile._derive_corrections` derives an interim per-driver
  trim from the declared sensitivities (the ~25 dB woofer/horn gap is
  attenuated), and L1 measurements can replace it only through the explicit
  automatic-apply path. Manual operator pins otherwise remain authoritative. (The
  schema field carrying the datasheet sensitivity is `sensitivity_db_2v83_1m` on
  the crossover-preview drivers, not `DriverSpec.sensitivity_db`.)
- **Duplicated graph-safety parsing.** The same CamillaDSP-graph
  invariants (per-output commission mute at −120 dB + wired; tweeter
  outputs wrapped by protective HP + limiter; fail-closed on parse error)
  are re-implemented across `runtime_contract.py` (`_commission_mutes`,
  `_pipeline_contains`, `_filter_params`, …) and `staging.py`
  (`_parse_generated_filters`, `_pipeline_contains_chain`,
  `_running_filter_matches`, plus three functions —
  `_all_commission_mutes_engaged`, `_software_guard_evidence`,
  `driver_commission_audible_evidence` — that each re-parse), with a live
  read-back variant too. ≈4 parallel paths. (Matches the prior staff
  review's P1.)
- ~~**Active-speaker commissioning does not use `measurement_window`**~~
  **CLOSED (cooperatively, 2026-06-20).** Commissioning can't *hold* a
  `measurement_window` the way correction/balance/sync do — it spans many
  `/active-speaker/*` requests (each on its own per-request `asyncio.run` loop)
  with the ramp tone continuous across them, so there is no persistent loop to
  own the context manager. Instead [`jasper/web/active_speaker_flow.py`](../jasper/web/active_speaker_flow.py)
  derives a self-expiring commission `active_phase()` from the safe-playback
  session; `correction._reserve_start_slot` + `balance_flow`/`sync_flow`
  `handle_start` consult it (refuse while commissioning), and `commission-load`
  refuses while any of the three is active. Same guarantee (never two
  measurement flows at once), self-healing via the safe-playback TTL.
- ~~**Confidence/quality thresholds are hard-coded per domain with no shared,
  parameterized model.**~~ **CLOSED (2026-07-12).**
  `jasper.audio_measurement.quality_model.QualityModel` now owns the shared
  capture-quality vocabulary, with `ROOM`, `DRIVER`, and `RAMP` profiles.
  Room SNR and driver-acoustics thresholds remain domain-specific fields on
  those profiles; callers no longer depend on compatibility aliases in
  `quality.py`.
- ~~**Evidence durability is inconsistent**: correction has schema-versioned
  per-session bundles; active-speaker uses one global JSON state file~~
  **PARTIALLY CLOSED (2026-07-11, active-crossover Slice 0).**
  Active-speaker now also has a schema-versioned, append-only commissioning
  bundle (`jasper/active_speaker/bundles.py`, ported directly from
  correction's `bundles.py` pattern — same manifest/hashing primitives,
  reused not forked). The global JSON state file
  (`active_speaker_measurements.json`) stays exactly what it was: the
  deliberate "latest-wins current pointer" the baseline compiler reads. The
  bundle is separate — durable, retention-bounded, forensic-only evidence
  keyed by `session_id` — and is never read back as an input to any
  decision. See
  [active-crossover-information-design.md](active-crossover-information-design.md)
  "Durable evidence and observability". balance/sync still don't persist
  bundles (lost on restart).
- **JTS3 lab Pi (2026-06-19): the crossover is not live.** Output HW is a
  HiFiBerry DAC8x (8 outputs); the live CamillaDSP graph (`v1.yml` and the
  outputd `outputd-cutover.yml`) is a **flat identity passthrough** — no
  crossover, no per-driver trim. With a B&C DE250-8 compression driver
  (~108.5 dB) ~**25 dB hotter** than the Epique E150HE-44 woofer
  (~83.3 dB), full-range equal-level audio = shrill/horn-dominant, and a
  tweeter-safety risk. This is the L0 failure made concrete.

> **Provenance note.** A design workflow's adversarial verifier inspected
> the *main checkout* (`/Users/jaspercurry/Code/JTS`), which was parked on
> a sibling session's branch lacking `jasper/active_speaker/`, and wrongly
> concluded the subsystem was "unbuilt." All "what exists" claims here
> were re-verified against this worktree and `origin/main` (32 files).
> Future automated audits: pin paths to the working tree / `origin/main`,
> not whatever branch the shared main checkout happens to be on.

---

## Two settled questions (full reasoning in the research snapshot)

1. **"Is it just level matching?" — Half.** Broadband level fixes
   "shouty/shrill" (tweeter too hot). "Nasal/honky" (~300 Hz–2 kHz) is a
   midrange/baffle-step or crossover-region problem that a trim won't fix.
   An LLM-designed crossover from datasheets won't have modeled the baffle.
2. **"Calibrated mic vs iPhone?" — Uncalibrated phone is fine for level
   matching, not for phase/FR.** Relative level is a ratio at one mic
   position; mic + room cancel in the crossover overlap band (±3–6 dB).
   Guardrails: AGC/EC/NS off (already enforced), fixed position, compare in
   the overlap band, average several captures. Calibrated mic required for
   FR/phase/null-depth (uncalibrated phase error ±20–40° at Fc).

## Multi-volume verdict (settled)

Woofer↔tweeter level matching is **ONE fixed trim, level-INDEPENDENT** in
the drivers' linear region — measured once at a 75–85 dB reference. **Do
not build per-volume level curves.** Perceived tonal change with volume is
**loudness compensation** (ISO 226 / Fletcher-Munson) — a *separate,
optional* feature (the Audyssey MultEQ-vs-Dynamic-EQ split), absent today
and out of scope for commissioning. Keep them orthogonal.

---

## Target architecture

**Pattern: functional core + imperative shell + adapter ports, reached via
strangler-fig extraction.** (Deliberately *not* a grand `MeasurementCore`
Protocol — the consumers differ enough — room: modal 20–350 Hz; crossover:
full-range per-driver; balance: level+time — that premature abstraction
would overfit. Honor "don't abstract before the second real instance"; we
have the second instance, so a thin shared kernel is justified.)

```
        ┌──────────────── Measurement Orchestrator (shell) ───────────────┐
        │  MeasurementSession lifecycle + measurement_window() + slot lock │
        │  pluggable MeasurementReporter callback per consumer             │
        └───────┬───────────────────┬──────────────────────┬──────────────┘
   CorrectionAdapter        CrossoverAdapter           BalanceAdapter
     (shipped)              (wire Stage 6/7)           (level + sync)
        └───────────────────────┼──────────────────────────┘
              ┌─────────────── Measurement Kernel (pure) ───────────────┐
              │ sweep · deconv · analysis · quality · calibration ·      │
              │ evidence(bundles) · QualityModel(params per consumer)    │
              └───────────────────────────┬──────────────────────────────┘
                            GraphValidator (single, fail-closed)
                                          │
                                  camilla_yaml emit  →  CamillaDSP / outputd
```

**Core OWNS** (move/extract, mostly from `jasper/correction/`):
- Signal gen (`sweep`), deconvolution (`deconv`), FR analysis (`analysis`).
- A **parameterized `QualityModel`** (room_response vs driver_presence vs
  level_ramp thresholds) instead of hard-coded per-module constants.
- Mic calibration lookup/upload (`calibration.py`) as a `CalibratedMicProvider`.
- Durable, schema-versioned **evidence bundles** (extend to tag
  `consumer_id` / `measurement_type` / `kernel_version`).
- **`measurement_window` + a single mutual-exclusion slot registry** that
  *all* consumers (including active-speaker commissioning) register with.
- **One graph-safety module** (`jasper/active_speaker/graph_safety.py` —
  kept in `active_speaker` for now since it's the only consumer; promote to
  a top-level shared module when balance/sync need it. NB `jasper/camilla.py`
  already exists, so `jasper/camilla/` as a package would collide).
  **Design: normalize-then-predicate, NOT "one parser."** A 2026-06-19 read
  of the code found the ≈4 paths parse YAML *three legitimately different
  ways*, by design: (a) `staging.py` hand-rolls a **line/text parser** over
  the JTS-emitted config — this doubles as an *emitter-format-drift guard*;
  (b) `staging.py`'s live check uses `yaml.safe_load` because CamillaDSP
  re-serializes the running graph in its own dialect (block lists, `channel:`
  scalar sugar, reordered keys) the text parser can't read (see
  staging.py:780–788); (c) `runtime_contract.py` uses `yaml.safe_load` for
  candidate-graph classification. Forcing one parser would change what's
  accepted/rejected and weaken the drift guard. So the module owns: one
  normalized `GraphView` (`filters: {name→{type,parameters}}`,
  `pipeline_steps: [{channels:set, names:[]}]`); three thin **adapters**
  (`view_from_emitted_text`, `view_from_camilla_dict`, `view_from_yaml_dict` —
  the last two dict-taking, the caller owning the `yaml.safe_load`)
  that preserve each source's parsing semantics; and the shared **predicates**
  (`output_hard_muted_and_wired`, `output_unmuted_and_wired`,
  `tweeter_guard_present`, `startup_headroom_ok`, …), fail-closed. The ≈4
  callers keep their parser choice but call the shared predicates — killing
  the duplicated *logic* without changing behavior. **DELIVERED (2026-07-02):**
  the shared predicates are also wired at the `camilla_yaml` active-speaker emit
  gate — see "Active-emitter L0 gate landed" below — so an unsafe graph can't
  reach disk.

**Consumer-specific (stays in adapters):** room target curves +
multi-position averaging + PEQ design; active-speaker role assignment,
per-driver sweep routing, crossover/trim, stage-gate ladder; balance/sync
leader ownership, per-speaker trim / Delay + Snapcast latency.

**Naming:** core module `jasper/audio_measurement/` (or
`jasper/audio_core/`); safety `jasper/camilla/graph_safety.py`. Decide in
the decision points below.

---

## Layered product spec

| Tier | Audience | What it does | Reuses | New |
|---|---|---|---|---|
| **L0** | everyone (implicit) | Designed crossover + protective HP **applied, fail-closed**; flat-graph-with-tweeter-role is illegal | `GraphValidator`, outputd graph | ~~wire the validator at the emit gate~~ **DONE (2026-07-02)** — flat-program lane + active-emitter gate both landed; make commission cut-over actually apply |
| **L1** | anyone, phone only | Per-driver level match: play band-limited tone/sweep per driver through the production graph, capture phone mic, compute overlap-band dB delta → fixed trim, propose + confirm + apply; `measurement_mode=magnitude_only` so it can never authorize a phase/delay decision | sweep/deconv/analysis/quality, `measurement_window`, browser-mic | trim algorithm; Stage-6 endpoint+UI; sensitivity-fallback when skipped |
| **L1.5** | optional | Loudness compensation (ISO 226) as a *separate* volume-dependent EQ layer | — | separate feature, default off; **not** part of commissioning |
| **L2** | enthusiasts w/ calibrated mic | calibrated FR + null-depth; measured **polarity** proposal + delay *status* (the delay value + per-driver EQ stay OUT) — **landed 2026-06-21, corrected 2026-06-21, see below** | `calibration.py` upload, full deconv pipeline, `phase_aware` mode | reverse-vs-in-phase null margin; polarity proposal gated on `phase_aware` |

**Fail-closed default:** if L1 capture is low-SNR or aborts, fall back to
datasheet sensitivity (or a conservative tweeter trim) and mark the config
**provisional** in `/state` + UI; never emit a graph that sends full-level
signal to a compression driver.

### L1 measured level match (landed 2026-06-20)

The phone level match refines the datasheet sensitivity trim with a measured
one. End-to-end, magnitude-only (it can never authorize a phase/delay change):

1. **Capture (near-field, per driver).** The Confirm outputs card's per-driver
   Play control ramps one driver audible through the production crossover
   (`commission_ramp.build_stage5_ramp_gate`), the household holds the phone
   ~2–5 cm from that driver, and the browser records the sweep with
   [`measurement-audio.js`](../deploy/assets/shared/js/measurement-audio.js).
   Placement copy lives on the page (`active-speaker-ui.js`
   `NEARFIELD_LEVEL_MATCH_GUIDANCE`).
   The correction-native relay flow strengthens that advice into a comparable
   measurement contract: 3 cm from the microphone capsule to the named driver's
   radiating-surface center (horn mouth for a compression driver), on-axis, with
   the same distance for every driver. Capture protocol v2 renders an explicit
   acknowledgement and the Pi verifies its per-link binding before playback.
   The resulting server-owned placement proof is tied to one durable comparison
   set created by the near-field level check (profile + mic/setup + calibration +
   locked common volume). Legacy, mixed-set, or geometry-less records cannot
   refine or automatically replace a crossover; they remain available as
   historical/by-ear routing evidence.
2. **Overlap-band level.** `driver_acoustics.analyze_driver_capture(overlap_fcs=…)`
   records, per crossover Fc the driver touches, the deconvolved magnitude **at
   Fc** (the 1/24-octave-smoothed point, not a linear-bin band mean which would
   skew a sloped response). Both adjacent drivers sit at their matched −6 dB
   Linkwitz-Riley shoulder there, so the driver-to-driver delta is their relative
   sensitivity. Each entry carries a `usable` flag (capture not
   silent/clipped/unusable, ≥ `OVERLAP_MIN_BINS` bins) so the trim fails closed.
3. **Trim chain → override.** `baseline_profile._measured_level_trims` reads those
   overlap levels from measurement state, requires BOTH drivers of EVERY
   crossover in a group to be `present` + `usable`, and requires the capture
   ledger (generated sweep peak + applied role gain + that driver's locked main
   volume) to normalize both captures to one effective excitation. The
   automatic level tone and ESS share the
   `AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS` −12 dBFS source peak; each isolated
   driver gets a preset-derived passband-safe tone, its own gradual level ramp,
   and its gain from the current immutable applied Layer-A snapshot. Playback
   and overlap analysis use the same frozen preset; neither resolves the mutable
   design draft after the relay link has been created.
   The quiet −20/−60 dB by-ear record proves driver identity only and is never
   reused as an acoustic capture level. A missing, stale, or mismatched applied
   snapshot/ledger blocks before playback or recording. The first product slice
   stops after per-driver level matching; summed response remains an optional
   diagnostic until every crossover region has its own validation contract. It
   then chains the deltas into a
   per-driver attenuation (quietest driver = 0 dB reference), averages usable
   groups, and clamps to the −60 dB floor. `_derive_corrections` then applies it
   **over** research/UI/datasheet estimates. A manual apply preserves the
   operator's ownership (`operator_pinned` > measured > estimate > datasheet).
   An explicit automatic replacement deliberately reverses the first two
   (`measured` > prior operator pin > estimate > datasheet). Legacy manual gains
   without provenance are treated as pinned; UI sensitivity proposals declare
   `sensitivity_estimate`.
4. **Fail-closed + provisional.** No usable measurement ⇒ keep the datasheet
   trim, set `provisional=True` + `corrections_source[role]="sensitivity"` and the
   `baseline_level_match_provisional` issue. Surfaced in the baseline payload, the
   `/sound/` card ("Driver levels"), and jasper-control `/state`
   (`active_speaker_output_safety.level_match_provisional`, read off the applied
   baseline). Attenuation-only + the 0 dB ceiling hold either way; the emitted
   baseline still re-proves the runtime_contract tweeter guard.
5. **Serialization.** Commissioning excludes room correction / balance / sync
   cooperatively — see the closed measurement-window gap above.
6. **Repeat/SNR admission (2026-07-12).** Relay driver capture is a server-owned
   three-repeat sequence. Interim accepted repeats remain bundle evidence but
   do not create a measurement record, so the envelope stays on the same driver
   and advances its repeat count. The final record uses the repeat kernel's
   median representative and aggregate spread. Its band SNR compares the
   deconvolved sweep against the signal-bounded controlled quiet crop after the same
   signal-owned direct-arrival alignment, linear windows, and calibration
   domain; 25/20 dB magnitude
   pass/warn policy remains authoritative. Fewer
   than two accepted captures after the bounded fourth attempt refuses the
   driver and asks for a quieter room or an external-amplifier adjustment.

Tests: `tests/test_active_speaker_level_match.py` (trim math + fail-closed),
overlap-band cases in `tests/test_active_speaker_driver_acoustics.py`, and
end-to-end override/provisional in `tests/test_active_speaker_baseline_profile.py`.
**Owed: on-Pi (jts3) audible pass** — run the guided flow with a phone near each
driver and confirm the measured trim lands near the datasheet ~25 dB delta and
the speaker is audibly level-matched.

### L2 calibrated crossover alignment (landed 2026-06-21, corrected 2026-06-21)

The calibrated-mic tier proposes crossover **polarity** (plus a delay *status* and
calibrated FR curves) on top of L1's level match. Gated so an uncalibrated phone
can never authorize a phase decision:

1. **Calibrated capture.** The driver / summed capture endpoints accept a
   `calibration_id` — the SAME `jasper.audio_measurement.calibration` store the `/correction/`
   wizard fills (Dayton iMM-6/UMM-6, miniDSP UMIK, uploaded REW curve). The handler
   loads the record and threads `record.curve` into `driver_acoustics`;
   `_capture_to_magnitude` applies it via the shared
   `jasper.audio_measurement.calibration.apply_calibration_curve`, so the surfaced FR is
   calibrated and the null-depth shoulders (different frequencies) are corrected
   rather than relying on an additive cal cancelling.
2. **The phase_aware gate.** `crossover_alignment.resolve_measurement_mode` is
   downgrade-only: `phase_aware` is granted ONLY with a calibrated mic, re-enforced
   at the data layer in `build_crossover_alignment_proposal` (every contributing
   capture must report `acoustic.calibrated`). A magnitude-only (phone) proposal is
   explicitly *unauthorized* — no polarity decision. Uncalibrated phase error is
   ±20–40° at Fc, so this is a correctness gate, not a preference.
3. **Polarity from the reverse-vs-in-phase null MARGIN.** `propose_crossover_alignment`
   is deterministic (no LLM). The robust, capture-model-correct signal is the
   *summed* response (a magnitude ratio within ONE capture, immune to capture-start
   jitter): the reverse-polarity null being clearly DEEPER than the in-phase null
   means the branches are in phase → keep; clearly SHALLOWER → out of phase →
   invert; similar → review. Judging the **margin** (both measured identically) is
   cap-independent — unlike an absolute "reverse null ≥ 25 dB" gate, which JTS's
   1/24-octave smoothed-shoulder measurement may never reach. Single-capture
   fallbacks: in-phase-only deep null → invert *candidate* (capture reverse to
   confirm); in-phase-only flat → keep *tentative*.
   `analyze_summed_crossover(expect_null=…)` flips the per-capture verdict for a
   reverse-polarity capture (a present null is the *pass*).
   The pair is now admitted through one fail-closed decision contract before
   those depths are read: each contributing record must prove its audible
   playback, a normalized ESS excitation ledger exactly matching the immutable
   applied topology/baseline/per-role corrections, exact current region/Fc in
   both record and analyzer output, the
   expected normal/reverse polarity slot, full active comparison/profile
   fingerprint, and the `summed_reference_axis_v1` fixed-axis acknowledgement.
   Automatic apply also requires the current preset and pre-alignment
   corrections to equal the protected profile's immutable recomposition
   snapshot; a same-Fc family/order/trim/polarity/delay edit therefore
   invalidates old evidence. Preview may still surface an unknown-SNR proposal,
   but apply requires affirmative per-band SNR and an uncapped null.
   Old listening-position policy, legacy, stale, malformed, or blocker-bearing
   records remain in the evidence history but cannot authorize an automatic
   polarity decision.
4. **No delay VALUE here — only a status.** JTS's near-field captures are
   browser-recorded with **no sample-sync to the Pi's playback** (`recordDriverCapture`
   / `captureMicWavBase64` just record a window while the tone plays), so a
   per-driver IR arrival delta is capture jitter, not acoustic time-of-flight — and
   the canonical method agrees IR "[is] not [a] substitute for phase-aware
   summation". The delay *value* therefore comes from the timing-locked
   reverse-polarity null **walk** (the deferred follow-up); the proposal surfaces a
   delay *status* (`aligned` when the in-phase sum is flat, `needs_alignment` when a
   deep null remains) so the maintainer knows whether to run it.
5. **Preview, then apply through the existing measured path.**
   `GET /active-speaker/crossover-alignment` previews the proposal + the surfaced
   per-driver/summed FR curves (the maintainer tweaks Fc/slope by hand — this
   feature NEVER auto-rewrites Fc/slope). To **apply** a polarity decision, the
   automatic baseline composition may fold an admitted, complete normal/reverse
   pair's polarity decision into per-driver `corrections` (`inverted`) exactly
   like L1's measured level trim. It never consumes `delay_ms` from a capture;
   the bounded Lane-F walk exclusively owns measured delay. The relay transport
   preserves candidate polarity/Fc/delay metadata, but the current wizard
   envelope does not yet expose the two per-region actions or load a transient
   reverse-polarity graph. The playback boundary refuses reverse/delay
   candidates before audio rather than persist unchanged playback under a false
   label, so this is not yet a live end-to-end pair-capture UI.
   The recompiled baseline re-proves the
   runtime_contract tweeter guard; level stays L1's attenuation-only job and the 0 dB
   ceiling holds.

Scope held: NO per-driver post-split EQ, NO listening-position room correction —
driver level/LF work is near-field, while summed alignment uses the fixed
tweeter-axis reference placement. Multi-group (stereo-pair) measured polarity
*emission* is also deferred (`group_specific_alignment_not_applied`); the proposal
computes for one group, so a mono/single-group speaker (jts3's
`active_mono_2way`) gets the full refinement.

**Update, 2026-07-12 (Slice 2 — every crossover region, not only the
lowest).** `build_crossover_alignment_proposal` used to cover ONE crossover
(the primary / lowest); a 3-way's upper crossover needed its own summed-null
capture and was explicitly out of scope. `measurement.py` now retains BOTH
in-phase and reverse-polarity summed evidence per crossover region (region
identity is stamped at record time — a fix in its own right, since a single
latest-record-per-group slot let a reverse capture silently overwrite the
in-phase evidence used by the room-correction blend gate and the automatic
delay/polarity tier). `build_crossover_alignment_proposal` iterates every
region sorted by fc and returns a `proposals` list (one `{region, proposal}`
entry each, independently phase_aware-gated on its own contributing
captures' calibration); the top-level `mode`/`proposal` keys stay the lowest
region's, for callers that only know about a single crossover. The proposer
itself (`propose_crossover_alignment`) is unchanged — this is wiring
persisted paired evidence around it. See
[active-crossover-information-design.md](active-crossover-information-design.md)
"Slice 2: automatic alignment".

Tests: `tests/test_active_speaker_crossover_alignment.py` (cal-curve application via
the null-depth shift, the phase_aware gate at both layers, the relative-margin
polarity table + delay status, reverse-polarity `expect_null`) and the pure UI
summary in `tests/js/active_speaker_ui_test.mjs`.
**Owed: on-Pi (jts3) calibrated pass** — with the Dayton USB-C near-field on each
driver, confirm the captured FR is sane and the reverse-polarity null margin reads
the right polarity; nothing exceeds the 0 dB ceiling. The interactive `main.js`
render of the proposal card + FR-curve plot, and the timing-locked **delay walk**,
are the deferred follow-ups (the pure summary helper `crossoverAlignmentSummary` +
the JSON contract ship here).

> **Correction (2026-06-21).** The initial cut (#918) proposed a *delay value* from
> per-driver IR arrival deltas and a one-click confirm POST. A staff review found
> the arrival delta is capture jitter (the captures aren't timing-locked) — a
> plausible-looking but meaningless number — and the confirm duplicated the
> summed-capture fold while falsely asserting a measured `blend_ok`. Both were
> removed; polarity moved to the cap-independent relative-margin signal.

---

## Refactor roadmap (strangler-fig, regression-safe)

Each phase keeps the **room-correction test suite green as the regression
gate**; no big-bang. "Extract/move" ≠ "net-new".

| Phase | Scope | Size | Net-new? | Done when |
|---|---|---|---|---|
| **0. Spike** | ~150-line CLI: route a band-limited sweep to one driver through the production graph → capture via existing pipeline → print proposed trim | ~1 day | net-new (throwaway) | a real "tweeter +25 dB" number from JTS3 hardware |
| **1. GraphValidator** | Extract one `graph_safety.GraphValidator`; call it at the `camilla_yaml` emit gate; replace the ≈4 parsers; add `test_graph_validator_rejects_flat_with_tweeter_role` | M | extract + 1 net-new gate | parsers deduped, all old safety tests pass, flat-with-tweeter is rejected (fixes JTS3 L0) |
| **2. Kernel extraction** | Move pure `sweep/deconv/analysis/quality` into `jasper/audio_measurement/`; wrap with characterization tests (pass unchanged); add parameterized `QualityModel` | M | extract | correction + active-speaker import the kernel; behavior identical |
| **3. Close Stage 6** | Keep `commissioning_capture` as the production measurement core; move the browser-mic active-crossover experience out of the HTTP `/sound/` walkthrough and into the HTTPS measurement/correction framework; read `DriverSpec.sensitivity_db` → propose per-driver trim; register commissioning into `measurement_window`; `measurement_mode` enum | L | net-new UI/routing | L0+L1 core ship: a user can level-match a 2-way and hear it; trim persists + re-freezes — **mostly landed (2026-06-20), see "L1 measured level match"; HTTPS UI integration and on-Pi (jts3) audible pass owed** |
| **4. Balance/sync as 3rd consumer** | Reuse the kernel + bundles for pair level-match (and Delay/Snapcast for sync); persist durable bundles | M | net-new adapter | leader-measured pair balance rides the core with no forked DSP |

**Progress (2026-06-19):** Phase 1 slice 1 landed (additive, no caller
changes): `jasper/active_speaker/graph_safety.py` — the leaf module
(normalized `GraphView` + two adapters `view_from_emitted_text` /
`view_from_camilla_dict` + shared fail-closed predicates `filter_param_matches`
/ `pipeline_contains_chain` / `output_hard_muted_and_wired` /
`output_unmuted_and_wired`) with `tests/test_active_speaker_graph_safety.py`.
The candidate/unknown-graph adapter (`view_from_yaml_text`) and a tweeter-guard
predicate are intentionally NOT pre-built — they land in slice 2b, driven by
`runtime_contract`'s real needs (its `<=`-clip / order≥2 / soft_clip policy and
its two parse-error issue codes), per "don't abstract before the second real
instance."

Phase 1 slice 2a landed: `staging.py`'s `_all_commission_mutes_engaged`,
`_software_guard_evidence`, `driver_commission_audible_evidence`, and
`running_commission_evidence` now call the shared predicates; the duplicated
emitted-text + running-graph parser/predicate cluster (~150 lines:
`_parse_generated_filters`/`_parse_generated_pipeline_filters`/
`_filter_param_matches`/`_pipeline_contains_chain`/`_float_matches`/the
`_parse_scalar`/`_parse_inline_*`/`_top_level_sections` text helpers/the
`_running_*` helpers) is deleted. Behavior-preserving.

An adversarial staff review (2026-06-19) then tightened slice 1: removed the
speculative `tweeter_guard_present` / `view_from_yaml_text` / orphaned helpers
(deferred to 2b), wired the staging mask loops to `output_hard_muted_and_wired`
/ `output_unmuted_and_wired` so every predicate has a real caller, and
documented + tested the intentional bool-channel / None-name parse hardening
(uniform across both adapters; the protective direction). Ruff clean;
active-speaker suite green (390 passed — the −3 vs 393 is the retired
speculative-predicate unit tests). **Owed before PR:** land on a branch cut
fresh from `origin/main` — this worktree branch carries unrelated prior commits
(`staging.py` +197 vs `origin/main`), so the slice must be recreated there to
PR cleanly.

Phase 1 slice 2b landed: `runtime_contract.py`'s `_active_graph_evidence` now
builds the shared `GraphView` via a new shared list-only adapter (no scalar
`channel: N` sugar, mirroring the deleted `_pipeline_contains`; see the
follow-up below for its current dict-taking shape) and proves its invariants
through the shared
predicates (`pipeline_contains_chain`, `filter_param_matches`, and a new
`tweeter_guard_present` carrying runtime_contract's LOOSE policy: any positive
Fc, order ≥ 2, soft_clip, clip ≤ ceiling — separate from staging's exact-match
guard, which is untouched). The duplicated local cluster
(`_safe_load_yaml`/`_pipeline_contains`/`_commission_mutes`/
`_commission_mute_gain_ok`) is deleted; the commission-mute scan keeps its
runtime_contract-specific `as_out{N}_commission_mute` name pattern but reads
`GraphView.filters`. Behavior-preserving: the granular issue codes and the two
distinct parse-error codes (`camilla_yaml_unparseable` vs
`camilla_yaml_not_object`) are preserved — the latter via a local parse, since
the shared view collapses both to `parsed_ok=False`. Ruff clean; full
suite green (6539 passed).

Phase 1 slice 2b-follow-up landed (`graph_evidence`/`graph_safety` reconcile +
the `runtime_contract` double-parse). The two modules now have one crisp,
independent ownership split. `graph_safety` (the leaf — **stdlib only**; callers
own the `yaml.safe_load`) owns the normalized `GraphView`, the parse adapters,
the fail-closed wiring predicates, AND the shared scalar matchers
(`float_matches`/`float_value`/`truthy_bool`) those predicates run on — the
single home, with the byte-identical copies removed. `graph_evidence` owns the
complementary, emitter-coupled half: the canonical filter NAMES (re-exported from
`camilla_yaml`, which is why it is *not* a leaf) plus the raw-dict accessors
(`filter_spec`/`filter_params`/`filter_type`) for `runtime_contract`'s baseline
path. There is **no re-export** between them — consumers import names+accessors
from `graph_evidence` and the GraphView/predicates/scalars from their owner
`graph_safety`, so every symbol has exactly one home and one import path, and the
leaf stays promotable to a top-level shared module.

The yaml-dialect adapter is `view_from_yaml_dict(config)` — dict-taking like
`view_from_camilla_dict`, so the caller owns the parse.
`runtime_contract._active_graph_evidence` already `yaml.safe_load`s the candidate
text once (for its two distinct parse-error codes + the baseline raw-dict
accessors) and builds the shared view from that same `payload`, so the text is
parsed once. The `view_from_camilla_dict` swap was **rejected** (it honors the
scalar `channel: N` sugar; `runtime_contract` deliberately stays list-only),
pinned by `test_view_from_yaml_dict_is_list_only_unlike_camilla_dict`. Other new
`view_from_yaml_dict` cases pin the emitted-graph invariants, fail-closed on
non-dict, and bool-channel exclusion. `classify_camilla_graph`'s two distinct
candidate parse-error codes (`camilla_yaml_unparseable` vs
`camilla_yaml_not_object`) are now pinned too (`test_active_speaker_runtime_contract.py`)
— reachable through the public API because `classify_camilla_config_text` routes
on a substring marker, not a full parse, so a malformed/non-mapping body still
reaches the runtime contract's own parse. Behavior-preserving; full
active-speaker suite green.

Phase 1 slice 3 landed (the L0 program-graph gate): a flat full-range program
graph can no longer go live (emitted *or* loaded) to the DAC while the saved
topology assigns a protected tweeter role. The shared judgement is the topology
predicate `runtime_contract.flat_program_graph_blocked_reason()` — the program
lane is structurally a 2-channel passthrough, so the only question is whether the
topology has a tweeter to protect; fail-closed on a corrupt/unreadable topology.
The refuse POLICY lives at each caller's boundary, **never** on the shared
`emit_sound_config` leaf: the `/sound` graph-carrier (`_StereoHostCarrier`) reads
it at construction so `can_host_eq` is `False` (the durable pre-check refuses
early, no spurious `prepare_failed`) and re-asserts in `reemit`, so BOTH the
live-draft SetConfig path and the durable write refuse with the existing typed
`CarrierCannotHostEq("flat_graph_protected_tweeter", …)` → honest blocked-200;
room correction's direct emit gates via
`correction.runtime_safety.assert_flat_apply_safe` (the sweep entry already
blocks measuring on a roleful topology — this is the measure-then-reassign
backstop); the multiroom solo-restore emit stays deliberately lenient
(un-bonding must always succeed). No-op for full-range / mono / subwoofer /
unconfigured topologies. (An earlier cut wired the gate inside
`emit_sound_config` itself with an inline `graph_safety`-predicate check —
[#871](https://github.com/jaspercurry/JTS/pull/871); a staff review found the
leaf placement missed the live-draft SetConfig path, raised a
non-`CarrierCannotHostEq` type the `/sound` route couldn't map to an honest
blocked-200, and broke the multiroom never-refuse invariant — so the gate moved
to the caller boundaries, reusing `CarrierCannotHostEq`.) Contract doc updated:
[HANDOFF-dsp-graph-carrier.md](HANDOFF-dsp-graph-carrier.md). On-Pi (jts3) status
(2026-06-20): the refusal LOGIC is validated on jts3's real topology
(`active_mono_2way`, tweeter @ DAC output 2), running the merged code on-device
(non-destructively, via a temp tree — not deployed): the verdict blocks a flat
program graph, the stereo-host carrier refuses the live-draft path
(`can_host_eq=False` + `CarrierCannotHostEq("flat_graph_protected_tweeter")`),
correction apply refuses, multiroom solo-restore stays lenient, and the live
active baseline still resolves to the active carrier (unaffected). STILL OWED:
the full DEPLOYED HTTP end-to-end (a real `/sound` request returning
blocked-200), which requires jts3 to actually be in the flat-graph state — not
induced on a wired compression tweeter, since that is the hazard the gate
prevents; confirm opportunistically when jts3 is transiently flat under the
tweeter topology (e.g. right after a fresh topology assignment, before the
active graph is staged), and that un-bonding still succeeds.

**Active-emitter L0 gate landed (2026-07-03):** the complement to the flat-
program gate above. That gate stops a *flat* (`emit_sound_config`) program graph
reaching a tweeter output; this one makes the four active-speaker emitters
(`emit_active_speaker_{startup,commissioning,baseline,driver_domain}_config`)
enforce their own tweeter-protection invariant at the emit boundary, rather than
relying only on the downstream `classify_camilla_graph` re-prove. Each emitter
now runs a fail-closed gate (`camilla_yaml._assert_tweeter_outputs_protected`)
just before the YAML is returned or written: it re-parses the emitted text
(`graph_safety.view_from_emitted_text`) and, for every physical output the preset
assigns a `tweeter` role, proves a `LinkwitzRileyHighpass` `BiquadCombo` is wired
**within the tweeter-role output channel set** (a subset check that rejects a
pre-split program-bus HP the Mixer-less `GraphView` would otherwise let "cover"
the output) with a corner **at or above a 400 Hz absolute floor**
(`graph_safety.TWEETER_PROTECTIVE_HP_MIN_CORNER_HZ` — well below the shipped
1600 Hz crossover, so it never over-blocks a real preset, but it catches a
tweeter HP left at 30/80/100 Hz). The predicates are the shared
`graph_safety.output_highpass_protected` / `unprotected_tweeter_outputs`
(normalize-then-predicate, the same GraphView the ≈4 verifiers use). The
File-sink program bake is *not* gated (no DAC, no driver to over-drive). Scope:
L0 proves HP-presence + a safe corner FLOOR only — validating that a preset's
*designed* Fc suits its specific driver is preset-validation's job (follow-up).
**New failure mode callers handle:** an emit now raises
`ActiveSpeakerConfigError` (a `ValueError`) with `event=active_speaker.emit_gate`
logged first (never silent) if a graph would ship an unprotected tweeter — the
active emitters wire that protection by construction, so this only fires on a
regression, but it is now ENFORCED rather than assumed. The bond prechecks
(`precheck_active_{leader,follower}`) convert that refusal to
`ActiveLeaderError` / `ActiveFollowerError` (reason `driver_domain_emit_refused`)
so the grouping reconciler's `except RuntimeError` still fail-safes to solo
instead of crashing the oneshot. Hardware-free (code + tests); on-device H2
acoustic sanity on jts5 still owed (confirm a real DE250 2-way commissions
through the gate and is audibly band-limited).

**Next slice (Phase 2 — kernel extraction):** move pure `sweep`/`deconv`/
`analysis`/`quality` into `jasper/audio_measurement/` behind characterization
tests; add the parameterized `QualityModel`. `runtime_contract` remains the
proven graph-safety re-use pattern.
NB: a worktree may have no `.venv`; run tests as
`PYTHONPATH=$PWD /Users/jaspercurry/Code/JTS/.venv/bin/python -m pytest …`
so `import jasper` resolves to the worktree, not the main checkout.

**Smallest valuable first step:** Phase 1 (GraphValidator) — it both kills
the P1 duplication *and* fixes the JTS3 L0 hole (a flat graph can no longer
go live when a tweeter role is assigned). Phase 0 spike can run in parallel
to de-risk Phase 3.

---

## Decision points (need maintainer input)

1. **Sequence: foundation-first vs feature-first.** Recommend
   **foundation-first** — Phase 1 (GraphValidator/L0) then Phase 3 (L1),
   because L0 is a live safety/correctness hole on JTS3. (Alternative: ship
   L1 first for momentum; riskier given the flat-graph state.)
2. **Refactor aggressiveness.** Recommend the **incremental strangler-fig**
   (extract kernel, leave adapters in place) over a sweeping reorg —
   matches "don't over-abstract," keeps the regression suite meaningful.
3. **Module placement/naming.** `jasper/audio_measurement/` (core) +
   `jasper/camilla/graph_safety.py` (validator). Confirm or adjust.
4. **L1 launch scope.** Recommend **uncalibrated-only** at L1 launch with an
   honest "±3–6 dB, gross balance" disclaimer; L2 calibrated path follows.

---

## Risks & what to verify on hardware

- **Kernel extraction must preserve load-bearing contracts** (deconv
  regularization constant + peak window; `analysis` return dtypes;
  `measurement_window` pause/restore protocol; `camilla_yaml` emit shape;
  `percent_to_db` mapping; the 0 dB `volume_limit` ceiling). Pin with
  characterization tests *before* moving code.
- **iPhone/Android AGC** actually honoring `autoGainControl:false` — capture
  a constant tone, confirm RMS flat ±2 dB on ≥2 iOS + 2 Android devices.
- **Protective HP** not skewing the tweeter passband vs the deployed config.
- **Null-depth repeatability** on JTS3's DAC8x (≥5 captures, variance <2 dB)
  before trusting any `phase_aware` delay step.
- **DAC8x clock coherence** for the chip-AEC reference path (separate, but
  shares the hardware).

---

Last verified: 2026-07-13 (Wave 1 excitation/evidence identities, reuse of
`null_walk.DspPredecessor`, and the deliberate no-consumer/no-bundle-migration
boundary checked contract-only; no hardware behavior revalidated. Crossover
adapter volume-lease participation and
measurement-flow admission ownership rechecked
against correction, balance, sync, and the coordinator mutex)
