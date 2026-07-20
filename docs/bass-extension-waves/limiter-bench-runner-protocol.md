# Bass Extension limiter bench-runner — reviewed amendment

> **Status (2026-07-20): accepted amendment** (reviewed; gate 1 complete). This
> is the *reviewed amendment* that
> [`limiter-evidence-protocol.md`](limiter-evidence-protocol.md) "Required bench
> owner — no hidden authority" and
> [`wave-4-commissioning-backend.md`](wave-4-commissioning-backend.md) Revision 9
> both defer to. It authorizes **building the bench runner** that executes the
> already-frozen limiter-evidence campaign and emits the replayable bundle the
> frozen `produce_limiter_thresholds` consumes. It authorizes **no production
> wiring, no profile persistence, and no hardware playback** until the runner
> implementation passes its own independent safety review and Jasper runs the
> supervised bench session. This document is a contract only — it changed no code
> and no hardware behavior.

## Relationship to the frozen protocol

`limiter-evidence-protocol.md` is **frozen and is not reopened here**. It already
fixes everything about the *evidence*: the detector sample point and units, the
three stimulus roles (`digital_transfer_probe`, `sweep_transparency`,
`sustain_stress`), the discovery and candidate passes, the abort/refusal rules,
the replayable bundle schema, and the pure producer contract. This amendment
adds only the half that protocol explicitly deferred — the **runner** that
authors the campaign, performs the temporary graph activation, drives
playback/capture, and writes the bundle.

Where this amendment and the frozen protocol could disagree, the frozen protocol
wins. A need discovered mid-implementation that the protocol does not cover is a
**stop-and-report**, not an improvisation — the same rule the wave charter
applies everywhere.

## What is missing today

The protocol's "Required bench owner" section states the tree has no owner that
chooses a detector probe, authors a bass sustain request, or activates a proposed
target/candidate pair, and that "the runner and its temporary graph activation
must receive their own independent safety review. The pure producer skeleton does
not implement either one." A repo scan confirms this: the pure producer
(`jasper/bass_extension/limiter_evidence.py`) is complete and waiting, and there
is no runner. The gap is entirely on the evidence-producing side.

## The runner's responsibilities

Build **one** bench runner that, driven by Jasper at the bench:

1. **Authors the `campaign_manifest`** from operator-supplied, operator-authorized
   inputs (the requested stimulus band, effective peak, commanded volume, hold,
   cooldown, repeats, and generator identity — exactly the manifest fields the
   protocol names), citing the current confirmed driver-safety profile and the
   selected `MarginPolicy`. It **never invents a manifest value from a default**
   — the protocol forbids it, and a missing value is a refusal.
2. **Executes the discovery and candidate passes exactly as the frozen protocol
   specifies** — per target, per candidate, in the frozen order, including the
   isolated `digital_transfer_probe` render, the admitted `sweep_transparency`,
   and the admitted `sustain_stress`. It does not search for a maximally
   permissive setting; it follows the protocol's smallest campaign.
3. **Owns the temporary graph activation** (safety contract below).
4. **Records the receipts and PCM/capture artifacts** and **emits the replayable
   bundle** via the existing `jasper/audio_measurement/bundles.py` +
   `evidence_identity.py`, in the exact shape the frozen protocol pins.

The runner produces an on-disk bundle and nothing else. It does **not** call
`produce_limiter_thresholds`, `apply_bass_extension`, `bypass_bass_extension`,
`recover_pending_bass_extension_apply`, or any profile writer.

## Temporary graph activation — the safety contract (highest risk)

This is the only part of the whole program that mutates a **live CamillaDSP
graph** to play test tones through a proposed alignment. It is bench-only,
operator-supervised, and fail-closed. For every discovery/candidate activation
the runner:

- enters the existing `measurement_window()`
  (`jasper/correction/coordinator.py`);
- snapshots the **exact** predecessor graph + profile;
- fades to the safe floor via the existing ramp / `safe_playback` path;
- applies the proposed sealed natural graph and the target's LT/subsonic values
  with the **Wave 0 micro-stepped `PatchConfig` mechanism**, and — in the
  candidate pass only — the candidate value to the named limiter's `clip_limit`.
  This is an operator bench mutation; it is **explicitly not
  `apply_bass_extension` and persists no profile**;
- reads the complete active graph back and **proves**, before unmuting, target
  identity, the ordered owner chain (`bass_ext_lt` → `bass_ext_subsonic` → the
  named limiter on exactly the owner channels, via
  `_assert_bass_extension_safe`), the configured `clip_limit`, and the owner
  channels; any mismatch refuses **before** any tone plays;
- after the pass, fades to the floor, restores the **exact** predecessor,
  re-proves it, and records a restoration receipt;
- **aborts immediately** on operator Stop or any of the protocol's abort
  conditions, preserving partial artifacts with the `refused`/`aborted` arm.

The runner uses the existing per-driver limiter as-is. It never adds a
compressor, a second limiter, a soft-knee, or a new threshold knob.

## Playback and capture — reuse, do not rebuild

Sweep/sustain playback rides the **existing** two-boundary excitation-admission
chain (`admit_excitation` / `ExcitationRequest` / `ExcitationLimits` /
`ProtectionEvidence` in `jasper/audio_measurement/excitation_admission.py`) with
a bass-owner limits derivation, the existing `MeasurementRamp`, the located WAV/
tone playback module, and the existing capture relay. Nearfield capture uses one
new builder `build_bass_nearfield_spec(...)` mirroring `build_crossover_sweep_spec`
(server-derived geometry, never operator-supplied); if the commissioning ladder
lands that builder first, the runner reuses it rather than duplicating it. If a
`sustain_stress` hold exceeds `level_duration_limits.max_sweep_duration_s`, that
admission refusal is **correct** — surface it; never split the hold to sneak past
the limit.

The pre/post-limiter taps are non-mutating, content-addressed reads of the exact
sample stream at the limiter input and immediately after it, per the frozen
protocol's sample point. The `digital_transfer_probe` renders through an isolated
CamillaDSP file sink and never reaches hardware.

## Two review gates (both mandatory before any tone plays)

1. **This amendment** is reviewed and merged — the contract is accepted.
2. **The runner implementation** passes an *independent adversarial safety review*
   (the JTS staff-maintainer gate in [`adversarial-review.md`](adversarial-review.md),
   zero Blockers / zero Should-fixes) **before any hardware playback runs**, and
   Jasper runs the actual bench session under supervision. Merging the runner
   code does not authorize an unattended campaign.

## What stays blocked

- **Production wiring of the producer.** `produce_limiter_thresholds` stays
  unimported and uncalled by every production path. It is wired only after
  Jasper's bench pass yields an **accepted** bundle, that bundle survives
  independent review at zero Blockers / zero Should-fixes, and a **later Wave 4
  revision names its exact `evidence_fingerprint`** and authorizes a trusted
  caller. This amendment names no bundle and no fingerprint.
- **No profile publication, persistence, runtime eligibility, or scheduler.** The
  runner writes a bundle; that is all. `apply_bass_extension`,
  `bypass_bass_extension`, and `recover_pending_bass_extension_apply` gain no
  caller.
- **The commissioning ladder's `review → accepted` path** (explicitly *excluded*
  from the Wave 4 Revision 9 hardware-free slice — it enters Wave 3) stays
  blocked on the same accepted bundle.
- **No mux / fan-in / voice / sound-wizard / volume / systemd / installer / cue /
  boot-recovery changes.** The runner rides the existing correction/measurement
  infrastructure; it adds no daemon, socket, HTTP route, timer, or unit.

## File allowlist (for the later runner-implementation PR, not this amendment)

**This amendment adds only this document.** The runner-implementation PR that
follows — after both gates above — may **Create** the runner module plus its
manifest/tap helpers, the `build_bass_nearfield_spec` builder (if not already
landed), and hardware-free tests; and may **reuse without modifying** the
admission chain, the ramp, `measurement_window()`, the capture relay,
`bundles.py`, `evidence_identity.py`, the `camilla_yaml` limiter-name/proof
helpers (`driver_baseline_limiter_name`, `sub_baseline_limiter_name`,
`_assert_bass_extension_safe`), and the Wave 0 `PatchConfig` mechanism. Changing
the behavior of any reused module, or adding a daemon/route/unit/env-knob, is a
**stop-and-report**. The exact module list is fixed by that PR's prompt, the same
way `limiter-evidence-protocol.md` fixed the producer's two-file allowlist.

## Tests

Hardware-free scaffolding tests only, in the runner-implementation PR: manifest
authoring from operator inputs (including refusal on a missing input); receipt
and bundle **shape** matches the frozen protocol (schema-checked keys); abort and
predecessor-restore logic with a mocked measurement window, admission chain,
playback, and camilla graph; and a **round-trip** where a synthetic
runner-emitted bundle is accepted by `produce_limiter_thresholds` (proving the
runner writes what the producer reads). The real evidence is Jasper's supervised
on-device bench session — that is not, and must not be, an automated test.

## Fences

Do **not**: re-architect the audio topology (the reference → engine → UDP → voice
/ outputd graph is fixed — swap engines, not topology); add a compressor, second
limiter, or a new threshold / crest-factor knob; invent any hardware-safety
number (all of them live in the manifest, the admission receipts, or the
measurement outputs); build a generic "measurement orchestration framework" (this
is one runner executing one frozen campaign); parallelize passes; add
SSE/websockets/queues; persist a profile; wire the producer into production; or
edit `limiter-evidence-protocol.md`.

## Status

**Accepted contract — gate 1 (this amendment reviewed and merged) is complete.**
No code, no hardware behavior, and no production wiring changed. The runner
implementation is the next, separately-gated step: gate 2 is its own independent
adversarial safety review before any hardware playback, with Jasper running the
supervised bench session. This doc is reachable from the waves README; when the
runner-implementation prompt is opened, give it a plan wave-table row and the
exact module allowlist.
