# Active speaker tuning — the layer model (design)

> **Status: adopted direction.** Owner-approved 2026-07-23 after the
> "no sparkle" investigation (issues #1666–#1668; forensic evidence in
> `captures/xover-e0-2026-07-21/OVERNIGHT-REPORT.md`, session-artifact).
> This doc is the execution handoff for the implementing session: the
> architecture is decided; per-phase design details are decided during
> implementation within these boundaries. Companion operational truth for
> today's shipped flow stays in
> [HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md).

## Why this exists (one paragraph of history)

The v2 crossover flow tunes trims/delay/polarity and verifies crossover
integration — and nothing else. On a compression-driver horn that left the
top octaves uncompensated (measured raw, analysis-bypassed, 58 dB SNR:
≈−4.6 dB in the 8–16 kHz octave re 2–4 kHz, −8.8 dB @12 k on JTS3), which
the owner heard immediately as "missing sparkle" while every VERIFY passed —
because VERIFY's band caps at 2·Fc AND it is a tracking metric against a
prediction that shares the rolloff. The gap is a missing, nameable layer:
**driver linearization**. Two secondary findings ride along: the trim solve
band-averages inside the woofer's rolloff skirt (#1667, ≈3.4 dB horn
over-attenuation), and the apply transaction can activate without durably
promoting (#1666).

## The five layers

One DSP graph, composed in fixed order. Each layer is its own artifact with
its own owner, measurement instrument, and re-run cadence. One fact, one
owner — shape never hides inside a level knob, level never hides inside a
shape.

| # | Layer | Job | Instrument | Re-runs when |
|---|---|---|---|---|
| 1a | **Driver linearization** | each driver flat *within its own band* on the design axis (CD-horn compensation, baffle step, breakup) | gated/quasi-anechoic sweep at the listening axis (already captured to 18 kHz every MEASURE); optional near-field supplement for the woofer below the gate validity floor | hardware changes (driver, horn, pad) |
| 1b | **Crossover integration** | drivers sum correctly: crossover filters, **scalar** trim per driver, relative delay, polarity | same gated session as 1a | hardware/geometry changes |
| 2 | **Bass** | extension/sub integration below the gated validity floor | in-room, ungated (the bass-ext program) | hardware/placement |
| 3 | **Room correction** | what the room does: modal peaks below the transition (~300–500 Hz here), at most a gentle broadband tilt above | in-room at the listening position | placement/room changes |
| 4 | **Preference** | declared taste on top of honest-flat | the household's ears | whenever |

Layers 1a+1b together are **the speaker layer**: they make the *device*
measure flat in direct sound, like a factory-tuned active monitor, and they
travel with the speaker. Layer 3 belongs to a room+position. Keeping that
boundary is load-bearing: 1a/1b are measured gated (reflections excluded);
3 is measured in-room; conflating them EQs directivity artifacts and ruins
off-axis sound. Room correction may *lightly* touch speaker-response
residuals only for speaker classes that have no Layer 1 (passive — #1671),
inside its conservative-above-transition philosophy.

**The "top of the table" contract (the owner's flatness vision), stated
precisely:** after Layers 1a+1b, the gated direct-sound magnitude on the
design axis is flat within a declared tolerance from the measurement
validity floor (≈143–200 Hz in the JTS3 room; set by the reflection-gate
window) up to ≈16 kHz. Below the floor, flatness is Layers 2–3's contract
with in-room instruments. Preference (4) then deviates deliberately and
visibly.

## Decisions already made (do not re-litigate)

1. **Linearization lives in the crossover program** (same wizard surface,
   same gated instrument, one commissioning session) — the surface gets a
   more honest name, "Active speaker" tuning (#1670). It produces a
   *separate artifact* from the trim: per-driver EQ curves.
2. **The trim stays a scalar** level anchor. Frequency-dependent balance is
   linearization's job. Corollary: implement 1a first — flattened branches
   structurally defuse most of #1667's band-average bias; the
   ripple-optimal trim fix lands after, as robustness for un-linearized
   tiers.
3. **Verification splits into two named claims.** Integration-verify (the
   existing 1–4 kHz tracking gate: "the correction realized the predicted
   summation") and a NEW **flatness-verify** ("gated response within
   tolerance from validity floor to 16 kHz"). Envelope/report copy must
   never let one imply the other — that conflation is how this gap stayed
   invisible.
4. **Safety posture unchanged:** per-driver correction gains stay
   non-positive in the emitted graph; an HF shelf is emitted as attenuation
   elsewhere + headroom accounting, never a positive ceiling raise; the
   two-invariant protection model and declared-sensitivity ceilings stand
   (#1665 adds pad/component declarations so effective sensitivities track
   reality — the L-pad lesson).
5. **Simple-first execution:** everything proves out on JTS3 with the
   UMIK-2 over the headless direct-Pi drive path (no capture relay). The
   relay/phone/product UX hardening comes after the acoustics are right —
   same pattern that worked for the measurement campaign.

## Layer 1a concretely — UX and data flow

**The household UX does not change.** One commissioning flow, the same
phone-tap ethos (set the mic, press Go, ~3 captures, fast honest verdicts):
CHECK → MEASURE → auto-apply → VERIFY, exactly as today. Linearization is
not a separate flow, a second wizard, or an extra sweep — it is a new
consumer of data every session already records:

1. **MEASURE (unchanged capture, richer analysis).** The existing per-driver
   gated sweeps already span each driver's declared band to 18 kHz with
   per-serial mic cal applied (`DriverResponse`, ~58 dB SNR measured on
   JTS3). The analysis first fits a per-driver linearization curve within
   each driver's band (bounded biquad set / smoothed inverse; conservative
   octave-fraction smoothing; capped correction depth so single-capture
   noise cannot imprint — exact fit form is a Phase-2 implementation
   decision against the real JTS3 curves), **then** computes integration
   (trim/delay/polarity) against the LINEARIZED branch responses. That
   internal ordering is what structurally defuses #1667. The candidate
   artifact grows a `linearization` member beside trims/delay/polarity; a
   re-run refits everything atomically; profiles without the artifact stay
   valid (absent = no stage emitted).
2. **APPLY (one more emitted stage).** The baseline emission gains one
   per-role linearization filter stage, same transaction, same safety
   posture (non-positive gains + headroom accounting).
3. **VERIFY (same capture, second claim).** The verify sweep is already a
   full-band summed ESS — the flatness-verify evaluates the SAME capture
   against the flat target from the validity floor to ~16 kHz, alongside
   the existing integration tracking gate. Two named verdicts from one
   capture; neither implies the other.

**Consistency without extra user steps:** robustness comes from the
capped/smoothed fit plus the flatness-verify gate, not from asking the
household for more taps. Before choosing the fit form, Phase 2 quantifies
fit-to-fit variance OFFLINE against the existing 2026-07-22/23 corpus
(15+ archived measure captures → fit each → curve spread); if field data
ever shows single-capture fits are too noisy, N-run averaging becomes an
operator-tier option — a data-driven decision, not a default cost. The
woofer's low edge honestly stops at the gate validity floor (~150–200 Hz
here); below that is Layers 2–3 by contract, not a linearization gap.
Fix-4 (#1654, wider tweeter sweep) composes naturally but is not a
prerequisite.

## Session operating model (how the implementing session runs)

Fable is the brains, not the hands: architect, coordinator, debugger, and
the owner's collaborator. Fable designs, decomposes, dispatches, interprets
evidence, holds the review gate, and talks to the owner — and **delegates
the doing**: implementation, replay/evidence-gathering, and drive-tooling
to **Sonnet-5 (high)** subagents; adversarial reviews (always) and any
unusually subtle DSP-adjacent core work to **Opus 4.8 (high)**. Token
discipline is a design constraint: Fable context is spent only where
Fable-level judgment is worth it — subagents carry the file-level work, and
their reports (not raw transcripts) come back to the coordinator. Every
merge passes the canonical adversarial review gate at 0 blockers / 0
should-fixes, rerun until clean; hardware claims are validated on JTS3
before merge when the change touches the acoustic path. The owner decides
taste, thresholds-by-ear, and anything physical at the rig.

## Composition & code seams (verified present)

The config emitter already composes in the right order and most seams
exist empty: `emit_active_speaker_baseline_config`
([jasper/active_speaker/camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py))
emits per-role `[crossover, delay, baseline_gain, limiter]`; the
`/sound/` recomposition (`_recompose_active_baseline_with_eq` in
[jasper/sound/graph_carrier.py](../jasper/sound/graph_carrier.py)) already
threads `preference_filters` + `room_peqs` slots (audited live: currently
empty). Layer 1a adds a per-role linearization stage to the *baseline*
emission (owned by the speaker layer, NOT injected through the sound-profile
seam — different owner, different cadence). The measured tweeter/woofer TFs
that the fit consumes are already produced by every MEASURE
(`analyze_program_capture` → `DriverResponse` to 18 kHz with per-serial cal
applied) — the data pipeline needs zero new capture work for 2-way.

## Speaker-class applicability (#1671)

Component entry (#1665) declares the class; the class drives which layers'
wizard steps exist:

| Class | 1a | 1b | 2 | 3 | 4 |
|---|---|---|---|---|---|
| Active 2-way (today) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Active 3-way (#1669) | ✓ ×3 | ✓ ×2 regions | ✓ | ✓ | ✓ |
| Passive | — | — | ✓ | ✓ (may absorb gentle speaker residuals) | ✓ |

## Microphone doctrine (with one open arbitration — #1672)

Both household mics carry **per-serial** calibrations (miniDSP by serial
…8494; Dayton resolves iMM-6C serials per-unit — CMM31555 verified live).
The distinction that matters is NOT per-serial-vs-generic; it is:

- **Pedigree/uncertainty above ~8 kHz** — the two calibrated readings of
  the same horn disagree by ~4.7 dB up top; at most one is right.
- **Incidence-angle sensitivity** — the iMM-6C's readings (delay family,
  HF tilt) shifted when it was physically handled; the deliberate re-aim
  test (owner's hands, 2 minutes) is the pending discriminator.
- **Empirical scatter** — quantified 2026-07-22/23: UMIK ≈0.3 µs
  repeatability vs iMM ≈8–12 µs; 25% honest-refusal rate on the iMM tier.

Rules until #1672 resolves: Layer-1a HF fitting requires the
reference-tier mic; consumer-tier mics remain integration-tier (1b) under
the shipped honesty gates. Future option with real leverage: the 17-run
same-sweep witness corpus supports deriving a unit-specific **transfer
calibration** for a consumer mic against the reference — a
"calibrate-your-cheap-mic-once" product story.

## Execution plan for the implementing session

Phase 0 — read this doc, the HANDOFF, and issues #1666–#1672; JTS3 +
UMIK-2 is the rig; headless drive tooling per
`captures/xover-e0-2026-07-21/drive-tooling/` (no relay).
Phase 1 — **#1666** (apply promotion durability + doctor divergence check):
small, isolated, protects everything after it.
Phase 2 — **#1668 Layer 1a for 2-way**: fit per-driver linearization from
the existing measured TFs (fit shape/order per-driver within its band;
tolerance + smoothing decided against the JTS3 curves), emit into the
baseline, add flatness-verify, wizard copy per #1670. Owner listening
session validates (ladder protocol from the overnight report), with the
ripple-optimal trim stop (−23.0) as the first rung.
Phase 3 — **#1667** trim solve fix (now mostly defused; keep for
robustness) + re-verify.
Phase 4+ — #1669 (3-way), #1671 (passive UX), #1665 (component entry),
#1672 (mic arbitration/transfer-cal), relay-tier productization.
Every phase: PR flow, adversarial review to 0/0, hardware validation on
JTS3, issues for anything parked.

## Issue ledger (all open threads, one place)

#1650 relay voids (two located causes) · #1652 anomaly/quality program ·
#1654 Fix-4 tweeter-sweep energy (revival trigger fired ×3) · #1656
crossover-v2 wrong-cal primary scope · #1658 capture-page on-device pass +
optional nits · #1660 room-relay device threading · #1664 worktree hygiene ·
#1665 component entry + pad declarations · #1666 apply promotion · #1667
trim-band bias · #1668 driver linearization (this doc's Phase 2) · #1669
3-way · #1670 rename · #1671 passive-class UX · #1672 mic HF arbitration.
