# Handoff: Bass Extension — volume-scheduled low-frequency alignment (plan of record)

> **Status: planning brief / execution plan of record (2026-07-17).**
> Waves 1–2 (numerics, profile, and observability skeleton) are merged;
> runtime/audio emission has not shipped. This document is the architecture
> and phased implementation plan for the feature; it was produced by a
> six-agent survey of the codebase at `origin/main` `7dd803c8d`
> (2026-07-16) plus external loudspeaker-science research. Every code
> claim below was verified against that commit. When implementation
> begins, each wave's PR should update the **Wave status** table and,
> once the feature ships, this plan gets the historical tag and a
> living `HANDOFF-bass-extension.md` takes over as operational truth.
>
> **Revision 2026-07-16b:** integrated an external deep-research
> prior-art/validation report (patents, T/S-drift data, port-compression
> data, perceptual JNDs). Deltas listed in §2.5 and folded into
> §§5–8, 12–15 in place.
>
> **Revision 2026-07-17:** resolves the Wave 1/Wave 3 frozen-contract
> contradiction found by draft PR #1558. The first runtime slice is
> sealed-only; ported/PR profiles remain retained and observable but
> are not armed until a fixed graph for their changing filter shapes
> is separately designed and proved.

## 0. One-paragraph summary

JTS will let a technically-inclined user commission a **measured,
volume-scheduled low-frequency alignment** for an active speaker: at
low listening levels the speaker plays a deeper-than-natural neutral
bass alignment (Linkwitz-Transform-style for sealed boxes; bounded
EQ-plus-subsonic-protection families for ported and passive-radiator
boxes). The first runtime slice arms only sealed profiles: as the
canonical `listening_level` rises it smoothly retreats through a
small commissioned family of progressively shallower targets until,
near maximum volume, the speaker plays its natural protected
response. Ported/PR commissioning profiles are retained for the later
runtime carrier but do not alter the live graph in this slice.
Commissioning reuses the calibrated
phone-mic relay, the synchronized-ESS measurement kernel, the
excitation-admission chain, and the commissioning apply/restore
transaction that already exist; the runtime is a handful of extra IIR
biquads inside the existing CamillaDSP graph plus a small
volume-watching scheduler — no new daemon, no added audio latency, no
runtime microphone.

---

## 1. Product behavior (restated, with the physics that shapes it)

### 1.1 Behavior

- **Low volume** — deepest commissioned neutral extension
  (e.g. flat to 31 Hz on a speaker whose natural corner is 61 Hz).
- **Medium volume** — reduced but still extended targets
  (35 → 41 → 50 Hz as level rises).
- **High volume** — the natural safe alignment.
- Transitions are inaudible: no clicks, no pumping, no obvious tonal
  steps. Left/right stay linked (one shared filter definition drives
  both bass channels).
- This is a **measured loudspeaker alignment whose corner moves with
  available headroom** — not loudness compensation (a perceptual
  equal-loudness shelf), not a preference bass shelf, not
  psychoacoustic bass synthesis. Loudness compensation may coexist
  later as a separate, composable Layer-C stage; conflating the two
  risks double-boosting into the excursion wall.

### 1.2 The physics that makes the design almost inevitable

Three facts (all verified against primary sources, §14 references)
pin the architecture:

1. **The Linkwitz Transform is exact pole/zero replacement.** A sealed
   box is a 2nd-order high-pass `H(s) = s²/(s² + (ω0/Q0)s + ω0²)`.
   The LT places a zero pair on the measured `(f0, Q0)` poles and
   installs a new pole pair at `(fp, Qp)` — the cascade *is* a sealed
   box with the new alignment. CamillaDSP ships it natively as
   `Biquad → type: LinkwitzTransform {freq_act, q_act, freq_target,
   q_target}` (present since 0.x; our pinned v4.1.3 has it).
2. **The boost cost is analytic:** peak boost ≈ `40·log10(f0/fp)` dB
   (exact at DC for `Qp ≤ ~0.71`). One octave of extension = ~12 dB
   of boost = 4× cone excursion = 16× amplifier power at the new
   corner. This is *why* a static LT is either shallow or wasteful —
   and why the alignment must retreat with level.
3. **Each 3 dB of level increase can be paid for by ~3 dB less
   boost**, i.e. raising `fp` by a factor of `10^(3/40) ≈ 1.19` (about
   a quarter octave). This "equal-excursion" mapping is independently
   validated by prior art — Google's US10,200,003 discloses the
   identical rule ("for each decibel of volume attenuation, the
   frequency can be reduced by one-twelfth of an octave"). **Scope
   caveat (deep-research finding):** the mapping holds for the cold
   voice-coil, single-tone, mechanically-limited sealed case and
   degrades under thermal compression, port compression, and program
   crest factor. It is therefore an **interpolation prior between
   measured anchors**, not a load-bearing derivation (§7.4), and the
   sustain stress test (§7.6) probes the regimes it misses.

Prior-art positioning (per the 2026-07-16 deep-research report):
"bass EQ scheduled by the volume control" is *not* novel — it dates
to Bose's 1980s dynamic-EQ patents, and two modern patents claim the
DSP alignment-changing version directly: **Google US10,200,003 /
US10,666,217** (headroom/volume-driven Linkwitz-Transform
modification) and **US12,342,139** (volume-indexed lookup table of
Linkwitz Transforms + multiband compressor, microspeakers). The
signal-driven camp (B&O ABL, Devialet SAM, TI/NXP/Cirrus smart amps,
Sonos/Samsung limiters) runs live excursion/thermal models, lab-baked
per model. **What has no located precedent — commercial, patent, or
DIY — is the commissioning workflow**: a per-unit, installed-system
measurement that fits the box, builds a discrete family of measured
targets, verifies the deepest with a stepped-level
distortion/compression ladder plus a sustained-program stress test,
and derives a hysteretic volume schedule from that evidence. That
workflow is this feature's actual contribution; all product copy and
docs should frame it that way (never as "bass that changes with
volume"). Patent implications: §13.

---

## 2. Assumption audit — what the feature prompt got right and wrong

Verified against `origin/main` (2026-07-16):

| Prompt claim | Verdict |
|---|---|
| JTS emits LR crossovers, no Linkwitz Transform | **TRUE** — zero `LinkwitzTransform` hits; all "Linkwitz" is LR crossover |
| CamillaDSP 4.1.3 supports LinkwitzTransform | **TRUE** (upstream) — pinned v4.1.3 in `deploy/provenance.toml`; LT is a `Biquad` subtype, **not** a `BiquadCombo` |
| Nearfield ≠ gated reference-axis handling | **TRUE** — `capture_geometry` is first-class; nearfield exempt from gating, reference-axis carries `f_valid_floor_hz` |
| Relay requests clean mono, EC/NS/AGC off | **TRUE** — plus new empirical AGC-slope verification for browsers that won't attest |
| Sync ESS places harmonics at separable offsets | **TRUE — and they are discarded today.** `deconv.direct_arrival_window`'s 5 ms pre-arrival window throws the harmonic IRs away; nothing anywhere computes THD |
| Strict LF reconstruction authorizes nothing | **TRUE, strongly** — `reconstruction_capability.py` hardcodes every `authorizes_*` False, sealed-single-radiator only, always refuses (geometry artifact unshipped), and is **not wired into production** (test-only imports) |
| Bass Management is a display/read seam | **TRUE** — `resolve_bass_management()` is a pure read resolver; the page is deliberately control-free |
| Low-latency path is near budget | **TRUE** — p95 cert budget 40 ms, measured p95 37.93 ms (~2 ms headroom). But minimum-phase IIR biquads add **zero buffer latency**, so this constrains *mechanism choice*, not feasibility |

Corrections that changed the plan:

- **Two prompt file references were stale-checkout artifacts** at
  first read but exist on current main: `driver_safety.py` (landed
  ~2026-07-14, 2 kloc) and `reconstruction_capability.py` (#1452).
  ~35 kloc of commissioning machinery landed in the four days before
  this plan was written — the reuse map (§3) is built on that new
  code, and it is *far* more favorable than the prompt assumed.
- **Enclosure vocabulary already exists.** `driver_safety.py`'s
  `cabinet` block models `enclosure_kind ∈ {sealed, vented,
  passive_radiator, open_baffle, transmission_line, unknown}`,
  `radiator_count`, `effective_radiating_diameter_mm`,
  `baffle_width_mm`. The adapters key off this; we do not invent a
  parallel enclosure taxonomy.
- **The strict reconstruction gate does not need to be relaxed —
  it needs to be *routed around*, legitimately.**
  `reconstruction_capability.py` guards **absolute far-field truth
  claims** (nearfield→far-field splice with baffle-step correction).
  Bass extension only needs **relative, fixed-position claims**: the
  plant *shape* fitted from a nearfield capture, and level-to-level
  *deltas* at the same unmoved mic. Neither claim requires the
  unshipped geometry artifact. The profile therefore carries its own
  narrower admission (§5.4) using the same typed-refusal pattern, and
  explicitly does **not** claim far-field reconstruction. When the
  splice lane ships later, it upgrades the optional "relate extension
  to overall neutral response" step — it is not a v1 dependency.
- **CamillaDSP does not document click-free coefficient reloads.**
  Only ramped *fader gain* (`Main`, `Aux1..4`, `SetFaderVolume`,
  ramp_time) is documented smooth. The runtime transition mechanism
  is therefore a Wave-0 hardware prototype with a documented-safe
  fallback (§8.3), not an assumption.
- **The stepped-level verification ladder can be mostly collapsed.**
  The prompt's flow (measure every target at every level) is the
  deep/optional mode. The default commissioning path is: fit plant →
  generate family analytically → ladder **only the deepest target**
  to find the clean-operating ceiling → sustain stress test at that
  ceiling (§7.6) → measure two more (target, level) anchor points →
  interpolate the rest via the equal-excursion mapping (§7.4). Same
  evidence quality where it matters, roughly a third of the sweeps.

### 2.5 Deep-research deltas (2026-07-16b)

An external deep-research pass (patents, standards, quantitative
driver-physics data, perceptual literature) produced six design
changes, folded into the sections noted:

1. **Anchors: ≥3 measured points; equal-excursion is an
   interpolation prior only** (§7.4). Klippel AN42 drift budgets:
   fs varies ~±3 % small-signal cold, ~10 % at high excursion, far
   more when hot (Re +~0.39 %/°C; ~4.5 dB sensitivity loss at
   200 °C coil temp). The cold single-tone mapping cannot carry the
   schedule alone.
2. **New sustain stress test stage** (§7.6): sweeps structurally
   understate thermal and port compression (both are
   sustained-power phenomena; Salvatti/Devantier/Button JAES 2002
   measured port compression in 6 dB steps with turbulence onset
   ~10 m/s and a sharp ~1 dB-to-audible-chuffing knee). A 60 s
   band-limited noise hold at the commissioned ceiling + re-sweep
   is now part of the default ladder.
3. **Target-coupled limiter threshold** (§8.4): the existing
   per-driver limiter's threshold is repointed per family member to
   that member's *verified* maximum digital drive + margin, patched
   together with the alignment. Last-resort backstop for
   mis-commissioned/thermally-drifted units on sustained bass;
   failure-coupled in the safe direction (stuck-deep ⇒ stuck-
   conservative).
4. **Subsonic HP default-on for sealed too** (§6.2; was optional).
5. **Optional impedance cross-check via file import, not hardware**
   (§6.2, §14): user-supplied DATS/REW `.zma` cross-checks
   fc/Qtc/fb with a >10 % disagreement refusal; plus a fit-order
   sanity check for leakage/stuffing. (The report suggested adding
   V/I sensing hardware; rejected — outside the JTS hardware
   envelope. Verify-deepest tracking remains the functional
   backstop.)
6. **Perceptual transition constraints** (§8.2–8.3): ≤1 dB
   effective bass-level change per micro-step (below the
   ~1.25 dB program-material JND, Toole & Olive 1988), transitions
   rate-limited and triggered only on sustained level changes; LF Q
   changes below Q≈16 are undetectable (Avis et al. 2007), so the
   corner motion itself is safe — the level *trajectory* is what
   must stay smooth. No published JND exists for a moving LF corner
   (open literature gap; Wave 7 listening notes are new data).

---

## 3. Existing-code reuse map

**Verdict up front: roughly 80 % of the commissioning substrate
already exists.** The genuinely new work is (a) the alignment/adapter
numerics, (b) harmonic/compression extraction, (c) the level-ladder
orchestration + per-rung retention, (d) LT/subsonic graph emission,
(e) the runtime scheduler. Everything else is reuse.

### Reused as-is (no changes)

| Piece | Where | Role here |
|---|---|---|
| Synchronized ESS generator | `jasper/audio_measurement/sweep.py` (`synchronized_swept_sine`, `synchronized_sweep_metadata`) | Stimulus; its Novak-sync property finally gets used for harmonics |
| Regularized deconvolution (full IR) | `jasper/audio_measurement/deconv.py` (`regularized_deconvolution_full`) | Already returns the unwindowed IR containing harmonic images |
| Capture quality gate | `jasper/audio_measurement/quality.py` (`assess_capture`) + `quality_model.py` | Clip/SNR/AGC refusals per rung |
| SNR policy | `jasper/audio_measurement/snr_policy.py` | Band-limited trust for LF decisions |
| Mic calibration identity | `jasper/audio_measurement/calibration.py` (`CalibrationRecord.calibration_id`) | Profile binding |
| Phone relay | `jasper/capture_relay/` — one new `CaptureSpec` builder, zero relay/page changes | Nearfield capture at each rung |
| Level settle ramp | `jasper/audio_measurement/ramp.py` (`MeasurementRamp`, incl. AGC-slope verification) | Reaching each ladder rung reliably |
| Excitation admission (two-boundary) | `jasper/audio_measurement/excitation_admission.py` + `excitation_artifacts.py` + `admitted_playback.py` | **Reuse the existing admission kind** — it is explicitly domain-agnostic; we only supply our own limits derivation |
| Measurement window / renderer pause | `jasper/correction/coordinator.py` (`measurement_window`) | Exclusive playback window per session |
| Quiet-start / floor confirmation | `jasper/active_speaker/safe_playback.py` | Operator confirms the right driver at floor level |
| Evidence identity + bundles | `jasper/audio_measurement/evidence_identity.py` (`json_fingerprint`, `ArtifactIdentity`, `ExactDspStateIdentity`), `bundles.py` | All profile fingerprints and forensic storage |
| Volume model | `jasper/volume_coordinator.py`, `volume_curve.py` (`percent_to_db`), `volume_persistence.py` (`speaker_volume.json`) | Scheduler input; canonical `listening_level` |
| Live single-filter patch | `jasper/camilla.py` (`patch_config` → CamillaDSP 4.1 `PatchConfig`); precedent `jasper/multiroom/runtime_balance.py` | Candidate runtime mechanism (Wave 0 decides) |
| Full-graph swap transaction | `jasper/dsp_apply.py` (`apply_dsp_config`, `dsp_writer_lock`) | Profile apply/bypass |

### Mirrored patterns (copy the shape, new instance)

| Pattern | Source | New instance |
|---|---|---|
| Apply/restore with CAS + fresh-readback proof | `jasper/active_speaker/commissioning_apply.py` (predecessor snapshot → mutation intent → apply → readback verify graph+volume+protection → retained proof; any failure unwinds via proven restore) | Bass-extension profile apply/bypass |
| Excitation limits derivation | `jasper/active_speaker/excitation_safety_plan.py` | `bass_extension` limits derivation from the driver-safety profile's bass-owner target |
| Typed fail-closed refusal vocabulary | `jasper/active_speaker/reconstruction_capability.py` | `BassExtensionRefusal` (§5.4) |
| Bounded candidate search + repeat admission + fail-closed restore | `jasper/audio_measurement/null_walk.py`, `jasper/active_speaker/repeat_admission.py` | Ladder rung admission (3 repeats at anchor rungs, spread bound) |
| Emit-gate re-proof of emitted YAML | `camilla_yaml.py` `_assert_tweeter_outputs_protected` + `graph_safety.py` | Prove subsonic HP present, boost bounded, limiter intact (§8.5) |
| Single current-run slot store | `jasper/active_speaker/crossover_level_run.py` (`CrossoverLevelRunStore`) | Ladder session store (but multi-rung retention added) |

### Extended (small, surgical diffs in high-traffic files)

| File | Change |
|---|---|
| `jasper/camilla_emit.py` | `emit_linkwitz_transform_biquad()`, `emit_butterworth_highpass()` (subsonic), shared bounds constants |
| `jasper/active_speaker/camilla_yaml.py` | Optional bass-extension block on the bass-owner chain in `_emit_baseline_pipeline`; emit-gate extension |
| `jasper/active_speaker/runtime_contract.py` | Teach `classify_camilla_graph` that a baseline graph carrying the bass-extension filter set is still `approved_active_runtime` (⚠ without this, every re-proof fails closed — see §12 risks) |
| `jasper/bass_management.py` | Extend the read resolver to also report bass-extension status (still read-only) |
| `jasper/audio_measurement/deconv.py` | `harmonic_impulse_offsets(meta, orders)` + `extract_harmonic_windows(full_ir, meta, orders)` — new consumers of the *existing* unwindowed IR |
| `jasper/audio_measurement/analysis.py` | `compression_curve()`, `thd_curve()`, `tracking_error()` (§7.5) |
| `jasper/control/state_aggregate.py` | `/state.bass_extension` section |
| `jasper/cli/doctor/audio.py` | `check_bass_extension_profile` (flat, one `CheckResult`) |

### Genuinely new (the build)

New package `jasper/bass_extension/` (pure numerics + profile +
scheduler; no HTTP, no subprocess):

```
jasper/bass_extension/
  __init__.py          # public seam: load_profile(), scheduler entry
  alignment.py         # LT math, biquad response eval, boost calc — pure
  adapters/
    base.py            # EnclosureAdapter Protocol + ADAPTERS registry
    sealed.py          # 2nd-order fit → LT family
    ported.py          # fb locate + knee fit → HP+shelf family
    passive_radiator.py# ported + PR-notch guard
  targets.py           # family generation, anchors, margins — pure
  profile.py           # BassExtensionProfile schema v1, refusals, staleness
  ladder.py            # commissioning ladder state machine (no I/O)
  scheduler.py         # listening_level → target index, hysteresis — pure
  runtime.py           # transition executor (patch/crossfade per Wave 0)
```

Plus: `jasper/web/` wizard flow (Wave 6), one relay spec builder, the
ladder HTTP backend, and the hardware-validation scripts.

---

## 4. Ownership model — where this sits in the system

- **Layer:** the profile is **Layer A** (hardware-bound speaker
  baseline) — it describes the physical bass system, is commissioned
  once per hardware build, and is bound to the applied baseline. It
  is *not* room correction (Layer B) or preference EQ (Layer C).
  Volume-dependence is architecturally novel for Layer A. In the
  sealed first runtime slice, the graph stays a static compiled
  artifact and only the **parameters of one named filter pair** move
  at runtime, inside bounds frozen into the profile. Ported/PR
  profiles remain Layer-A commissioning artifacts, but have no live
  carrier in this slice.
- **Extensibility contract:** per `docs/extensibility.md` this is
  **not** a new contract and not a Pattern-3 hardware-presence
  profile — it is a sub-artifact of the existing active-speaker
  profile family (the doctrine's "second instance of an existing
  shape" default). The three enclosure adapters get a minimal
  Protocol + dict registry (three real implementations justify it;
  no generic plugin framework).
- **Config ownership:** the profile is wizard/flow-owned state in
  `/var/lib/jasper/bass_extension_profile.json` (mode 0640, atomic
  write), same pattern as `active_speaker_baseline_profile.json`. No
  new `jasper/config.py` fields except (possibly) one scheduler
  enable/override env with a documented `.env.example` comment.
- **Bass-owner identity:** `resolve_bass_management()` precedence is
  reused. Exactly one physical bass system receives extension:
  - Local-DAC subwoofer present → the **sub chain** owns extension;
    mains keep their bass-management HP untouched.
  - No sub → the **woofer way** owns it (both sides, linked).
  - Wireless-sub bonds → **out of scope v1** (§13); the profile
    refuses to arm when the box is in a bonded topology whose bass
    owner is remote.

---

## 5. The Bass Extension Profile (single source of truth)

### 5.1 Schema (v1)

`kind = "jts_bass_extension_profile"`, `schema_version = 1`,
persisted at `/var/lib/jasper/bass_extension_profile.json`; all
fingerprints via `evidence_identity.json_fingerprint`; raw evidence
in an append-only bundle (`audio_measurement/bundles.py`), never read
back as a decision input.

```jsonc
{
  "kind": "jts_bass_extension_profile",
  "schema_version": 1,
  "profile_id": "bex-<sha12>",
  "created_at": "...",
  "algorithm_version": "bass_extension_v1",     // thresholds ride this
  // ---- bindings (all must match at load time or the profile is stale)
  "baseline_fingerprint": "...",   // baseline_candidate_fingerprint of the
                                   // applied recomposition_snapshot
  "topology_id": "...", "topology_fingerprint": "...",
  "bass_owner": {"kind": "woofer_way" | "local_sub",
                 "roles": ["woofer"], "channels": [2, 3]},
  "enclosure": {"adapter_id": "sealed_v1" | "ported_v1" | "passive_radiator_v1",
                "adapter_version": 1,
                "cabinet_fingerprint": "..."},   // driver_safety cabinet block
  "mic_calibration_id": "minidsp-umik1-abc123def456" | null,
  "measurement_ids": ["<ArtifactIdentity>", ...],
  // ---- the fitted plant (adapter-specific payload, versioned)
  "natural": {
     // sealed:            {"f0_hz": 61.2, "q0": 0.72, "fit_rms_db": 0.4}
     // ported:            {"fb_hz": 43.1, "knee_hz": 55.0, "knee_slope_db_oct": 21.0,
     //                     "natural_curve": { /* 96-pt log-grid resample — the
     //                        empirical model predicted_response builds on */ }}
     // passive_radiator:  {"fb_hz": 41.0, "notch_hz": 24.5, natural_curve, ...}
  },
  // ---- the commissioned family, deepest first; the final target is ALWAYS
  //      the natural/safest member (the fallback identity target)
  "targets": [
    {"target_id": "t31", "fp_hz": 31.0, "qp": 0.65,
     "filters": [ /* exact CamillaDSP filter param dicts */ ],
     "boost_headroom_db": 11.8,          // max over freq grid vs natural
     "limiter_threshold_dbfs": -5.2,     // verified max digital drive + margin (§8.4)
     "subsonic": {"type": "ButterworthHighpass", "freq": 22.0, "order": 4}},
    ...
    {"target_id": "natural", "fp_hz": 61.2, "qp": 0.72,
     "filters": [], "boost_headroom_db": 0.0,
     "limiter_threshold_dbfs": -1.0,     // baseline limiter default
     "subsonic": {...}}                  // always present; expert-removable
  ],
  // ---- level anchors: listening_level (0-100) ceilings per target
  "anchors": [
    {"target_id": "t31", "max_listening_level": 50,
     "evidence": "measured" | "derived" | "spot_verified"},
    ...
  ],
  "margin": "conservative" | "normal" | "aggressive",
  "digital_margin_db": 3.0,
  // ---- ladder evidence summary (full data in the bundle)
  "clean_ceiling": {"listening_level": 62, "limited_by":
        "compression" | "thd" | "mic_clip" | "digital" | "operator_stop"
        | "sustain_sag" | "sustain_fc_shift"},
  "sustain_test": {"duration_s": 60, "fundamental_sag_db": 0.7,
                   "fc_shift_pct": 2.1, "verdict": "passed"} | null,
  "impedance_import": {"source": "rew_zma", "fc_hz": 60.4, "qtc": 0.74,
                       "agreement_pct": 1.3} | null,   // expert-optional
  "status": "accepted" | "bypassed"
}
```

Design points:

- **Immutable once accepted.** A margin change re-derives anchors
  from the *retained ladder evidence* without re-measuring → new
  `profile_id`, same `measurement_ids`. Recommissioning replaces the
  file (predecessor archived in the bundle).
- **`targets[last]` is always the natural alignment** with empty
  filter list — so "retreat fully" and "profile bypassed" converge on
  the same graph shape, and the fallback story stays one sentence.
- **Filters are stored as exact CamillaDSP parameter dicts**, frozen
  at accept time. The runtime never re-derives filter math; it
  selects among frozen members. In the first runtime slice that
  selection is sealed-only; accepted ported/PR profiles retain these
  dicts as commissioned evidence but are not armed. This is the "no
  free-form optimizer" guarantee made structural.

### 5.2 Staleness and authorization

`evaluate_bass_extension_profile(profile, topology, applied_baseline)`
mirrors `evaluate_driver_safety_profile`'s ladder:
`missing → malformed → stale → accepted`. Stale iff any binding
fingerprint mismatches (baseline re-applied, topology changed,
adapter version bumped, algorithm version bumped). **Stale or missing
never blocks music** — the runtime simply holds the natural target
(§10.2). Only an `accepted` + current **sealed_v1** profile authorizes
the first-slice scheduler to leave the natural target. Accepted
ported/PR profiles remain valid and visible but report runtime
ineligible; acceptance is not permission to synthesize a graph shape
their adapter did not define.

### 5.3 What it deliberately does not claim

The profile records a **measured clean operating envelope** — "at
these levels, these targets tracked linearly within X dB with THD
below Y at the mic position used" — not a guarantee of driver
survival. The wizard copy says exactly this. No far-field/absolute
SPL claims (no splice dependency); the fit and the ladder are both
relative to the fixed nearfield position.

### 5.4 Refusal vocabulary

`BassExtensionRefusal(StrEnum)`, same style as
`ReconstructionRefusal`: `BASELINE_NOT_APPLIED`,
`TOPOLOGY_MISMATCH`, `BASS_OWNER_AMBIGUOUS`, `BONDED_BASS_OWNER_REMOTE`,
`ENCLOSURE_UNKNOWN`, `ENCLOSURE_UNSUPPORTED` (open-baffle, TL,
multi-radiator in v1), `TUNING_NOT_LOCATED` (ported/PR: no fb),
`PR_NOTCH_NOT_LOCATED`, `FIT_QUALITY_INSUFFICIENT`,
`CAPTURE_QUALITY_REFUSED`, `CAPTURE_SNR_INSUFFICIENT`,
`MIC_MOVED_BETWEEN_RUNGS` (level-to-level coherence check failed),
`LADDER_INCOMPLETE`, `BOOST_LIMIT_EXCEEDED`, `PROFILE_STALE`.

---

## 6. Enclosure adapters

### 6.1 The contract (`jasper/bass_extension/adapters/base.py`)

Small Protocol, three implementations, dict registry keyed by
`adapter_id`. Pure functions of numpy arrays + dataclasses; no I/O,
no CamillaDSP knowledge beyond emitting filter *parameter dicts*.

```python
class EnclosureAdapter(Protocol):
    adapter_id: str
    adapter_version: int
    required_captures: tuple[CaptureRole, ...]
    #   sealed: (WOOFER_NEARFIELD,)
    #   ported: (WOOFER_NEARFIELD, PORT_NEARFIELD)      # port optional, see 6.3
    #   pr:     (WOOFER_NEARFIELD, PR_NEARFIELD)

    def fit_plant(self, captures: Mapping[CaptureRole, MagnitudeCurve],
                  cabinet: CabinetInfo) -> PlantFit | FitRefusal: ...

    def generate_family(self, plant: PlantFit, *, deepest_fp_hz: float,
                        boost_cap_db: float, n_targets: int,
                        ) -> tuple[TargetSpec, ...]: ...
        # returns deepest-first, ALWAYS ends with the natural target;
        # every member carries boost_headroom_db + subsonic spec

    def predicted_response(self, plant: PlantFit, target: TargetSpec,
                           freqs: np.ndarray) -> np.ndarray: ...
        # used for tracking-error verification at each rung
```

### 6.2 Sealed (`sealed_v1`)

- **Fit:** nonlinear least squares of the 2nd-order HP magnitude over
  `[0.3·f0 … 3·f0]` (start below 25 Hz, iterate the window once the
  first f0 estimate lands). Two free parameters (`f0`, `Qtc`) makes
  this robust; fit is on curve *shape* so absolute-level calibration
  error cancels. Refuse when fit RMS > 1.5 dB in-window
  (`FIT_QUALITY_INSUFFICIENT`) — leaky/aperiodic boxes bias the order
  and show up here.
- **Family:** `LinkwitzTransform` targets. Deepest
  `fp = max(commission floor, f0 / 10^(boost_cap/40))`; boost caps by
  margin: conservative 6 dB, normal 9 dB, aggressive 12 dB
  (expert-overridable to the CamillaDSP-valid limit with a stark
  warning). `Qp = 0.65` default (between Bessel transient behavior
  and Butterworth flatness; expert-overridable 0.5–0.71).
  Intermediate targets spaced ~3 dB of boost apart (fp ratio ≈ 1.19).
- **Subsonic:** 2nd-order HP at ~0.5·fp_deepest, **default-on**
  (expert-removable). Sealed boxes unload gracefully, so this is
  mostly rumble hygiene — but it also bounds the LT's sub-band boost
  against pathological program material, which matters more now that
  the schedule is open-loop by design.
- **Fit-error tolerance:** ±10 % f0 or ±0.1 Qtc error yields ~±1–1.5
  dB local ripple at low Q — acceptable; the verification rung
  catches worse. Known physics (Linkwitz's caveat, quantified by
  Klippel AN42): `fs` moves ~±3 % with small-signal drive alone,
  ~10 % at high excursion, and substantially more with a hot coil —
  so **fit at a low-moderate level and verify the deepest target at
  low level**; residual error at higher levels shows up as ladder
  tracking error and sustain-test drift (§7.6), which is exactly what
  those stages are for. The margin tiers encode these budgets
  (conservative sizes for the ~10 % hot case).
- **Fit-order sanity check:** if a 3rd-order rolloff model improves
  the fit RMS by more than a threshold over the 2nd-order model, the
  box is leaky/stuffed enough that the sealed model is suspect →
  `FIT_QUALITY_INSUFFICIENT` with a leakage hint in the detail.
- **Optional impedance cross-check (expert):** accept a DATS/REW
  `.zma` import; when present, compare impedance-derived `fc/Qtc`
  (ported/PR: `fb` from the saddle) against the acoustic fit and
  refuse on >10 % disagreement. File import only — no V/I sensing
  hardware is in scope for JTS.

### 6.3 Ported (`ported_v1`)

- **What "extension" honestly means here:** a healthy reflex box is
  already near-maximally extended; below `fb` the cone unloads and
  excursion rises catastrophically. The family is therefore **not**
  an LT. It is: (a) an always-present subsonic high-pass, (b) bounded
  passband EQ that flattens the region between the natural knee and
  just above `fb` (many small boxes are tuned with an early sagging
  knee that EQ genuinely improves), and (c) retreat = progressively
  *raising the effective high-pass corner* (the pro-audio
  sliding-HPF family), ending at the natural protected response.
- **Fit:** `fb` = the sharp minimum of the **woofer** nearfield
  magnitude (Keele/D'Appolito; more robust than impedance and needs
  **no port-area math**). Knee shape fitted empirically (frequency +
  local slope of the summed or woofer-dominant curve above fb).
  Port nearfield capture is **optional evidence, not a fit input**,
  in v1: it confirms fb (port output max ≈ woofer minimum) and gives
  the ladder a port-compression witness band. When supplied with a
  port diameter, the Keele diameter-ratio sum
  (`p = p_w + (D_p/D_w)·p_port`) renders a nicer combined curve for
  the UI — display, not decision.
- **Family:** deepest target = flat to ~1.15–1.25·fb with subsonic
  `ButterworthHighpass` order 4 at `0.65·fb` (conservative:
  `0.75·fb`, order 4; aggressive: `0.65·fb`, order 2 + more shelf).
  EQ realized as ≤ 2 biquads (one low-shelf + one PEQ) — a
  known-stable constrained family, never a free EQ solve. Retreat
  members raise the HP corner toward the knee and shed the shelf.
- **Ladder bonus:** port compression appears distinctly (level-linked
  sag + distortion rise concentrated near fb) — reported as evidence,
  not simulated.

### 6.4 Passive radiator (`passive_radiator_v1`)

Ported adapter with three deltas:

- **Extra landmark:** the PR free-resonance **notch** below `fb`
  (locate from the PR-nearfield/woofer-nearfield pair — the system
  cancellation point). Refuse if not locatable
  (`PR_NOTCH_NOT_LOCATED`).
- **Hard rule:** no target may boost at or below the notch — it is an
  acoustic zero and the PR's max-travel region. Subsonic HP corner
  must sit **above the notch** (≥ 1.1·notch), which usually lands it
  nearer `0.75·fb` than the ported default.
- Steeper natural ultimate rolloff (~30 dB/oct) means lower-order
  subsonic filters suffice; the family generator accounts for the
  summed slope when computing `boost_headroom_db`.

**Runtime boundary for ported/PR (2026-07-17 revision).** Wave 1's
adapter truth is retained unchanged: every ported/PR target has
`qp=None` and no LT, while its `filters` tuple may shed a shelf or
peak and may add a separate retreat high-pass; the natural member has
an empty tuple. These members therefore cannot transition through the
sealed `bass_ext_lt` identity slot. A zero-gain shelf/peak could be an
identity in isolation, but the retreat high-pass has no exact valid
pass-through parameterization, and Wave 0 measured `PatchConfig`
coefficient changes on an already-present named LT — not filter
add/remove, type changes, or bypass machinery. A fixed ported/PR slot
graph would consequently require new carrier/bypass structure and a
fresh audio-safety/re-proof program, materially expanding Waves 3/5.
The 80/20 decision is explicit: ported/PR profiles remain
commissionable, persisted, and observable, but the first runtime
slice does not arm them or emit any `bass_ext_*` block.

Multi-driver / multi-port / TL / open-baffle / horn:
`ENCLOSURE_UNSUPPORTED` in v1, listed as future adapters. The
contract already carries `radiator_count` for that day.

---

## 7. Commissioning: state machine, measurement, analysis

### 7.1 Preconditions (all existing machinery)

Applied active baseline (`status=applied`, current fingerprint);
confirmed-and-current driver-safety profile (source of the cabinet
block and per-driver level ceilings); measurement window available
(`measurement_window()`); relay or local capture reachable; mic
calibration selected (recommended, not mandatory — an uncalibrated
mic gets `mic_uncalibrated` WARN and blocks nothing, since the fit is
shape-relative; calibration mostly improves the 20–40 Hz tail).

### 7.2 State machine (`jasper/bass_extension/ladder.py` — pure; the
web backend owns I/O, mirroring `commissioning_service.py`)

```
idle
 → characterize        # nearfield captures of bass owner (+port/PR),
                       #  3 repeats (repeat_admission pattern), at a
                       #  ramp-settled low level; room correction and any
                       #  prior bass extension DISABLED via the
                       #  commissioning-runtime bounded transaction
                       #  (crossover + protection filters STAY active)
 → fit                 # adapter.fit_plant → PlantFit or refusal
 → propose             # adapter.generate_family + analytic anchors
                       #  (§7.4); operator reviews family + margin
 → verify_deepest      # apply deepest target (bounded live transaction,
                       #  commissioning_runtime pattern), measure at the
                       #  lowest anchor level; tracking error vs
                       #  predicted_response must pass
 → ladder              # step main_volume up (default +3 dB/rung,
                       #  margin-selectable ±); one sweep per rung,
                       #  mic UNMOVED; evaluate per §7.5; stop on any
                       #  stop-condition
 → sustain_test        # 60 s band-limited noise hold at the sweep-clean
                       #  ceiling, then one re-sweep (§7.6); probes the
                       #  thermal/port compression sweeps understate;
                       #  failure lowers the ceiling one rung
 → derive_anchors      # ≥3 MEASURED anchor points (deepest ceiling +
                       #  two more (target, level) verifications);
                       #  equal-excursion interpolates BETWEEN measured
                       #  points (§7.4); digital clamp throughout
 → review              # operator sees family, anchors, evidence,
                       #  margin selector; explicit accept
 → accepted            # profile written, apply transaction (§8.6)
 (any state) → aborted # graceful_stop fade-down + exact restore
```

Durable single-current-session slot (crossover_level_run pattern) at
`/var/lib/jasper/bass_extension_session.json`, plus a **multi-rung
retention store** inside the session bundle — the one storage shape
the codebase lacks today (everything existing is
latest-wins-single-slot). Rung records:
`{rung_ordinal, commanded_level_db, listening_level, capture
ArtifactIdentity, fundamental curve, h2/h3 curves, thd_curve,
compression_db_by_band, tracking_error_db, quality verdicts}`.

### 7.3 Playback + capture path per rung

Every rung is: `excitation_safety_plan`-style limits derivation for
the bass owner (band = `hard_excitation_band_hz` ∩ [f_lo … 250 Hz],
peak/duration/repeat/cooldown from `level_duration_limits`) →
generation admission → relay capture session
(`build_bass_nearfield_spec`, new builder, mono 48 k, EC/NS/AGC off,
server-derived `capture_geometry=near_field`) → playback re-admission
under the live lock → `aplay` through the **full production graph**
(this is deliberate: the ladder verifies the *actual* commissioned
chain, protections included) → capture pull → quality gate → analysis.
Stop paths: phone clip sample → immediate abort (existing ramp
behavior); operator Stop button → `_graceful_stop` fade-down;
session timeout → coordinator lease expiry auto-restores.

Sweep parameters per rung: `f1 = max(10, 0.4·expected_corner)` Hz,
`f2 = 500` Hz, `duration ≈ 8 s` (long-L sync sweep: LF energy density
and a `L·ln2 ≈ 1.1 s` fundamental↔H2 gap at these settings — clean
harmonic separation down to 20 Hz), amplitude −12 dBFS (the shared
`AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS`). Total ladder wall time
at 6 rungs ≈ 6 × (8 s sweep + settle + cooldown) ≈ 3–4 min.

**Mic fixity check:** consecutive-rung fundamental curves, gain-
normalized by the commanded delta, must correlate > threshold in the
150–400 Hz band (a band the bass filters barely touch). A mic bump
mid-ladder shows up as a broadband shape change there →
`MIC_MOVED_BETWEEN_RUNGS`, ladder pauses and offers re-run from the
last good rung. Simple, no jig required (a jig/spacer is a docs
recommendation, not a product dependency).

### 7.4 The anchor 80/20 (default mode) vs the full ladder (deep mode)

Default mode produces **at least three measured anchor points**, with
equal-excursion interpolation *between* them (deep-research delta #1
— the mapping is a prior, not a derivation; it holds cold/single-tone
/sealed and degrades under thermal load, port compression, and
program crest factor):

1. Ladder **the deepest target**. The rung where it stops tracking
   cleanly (per §7.5) minus one rung, then confirmed by the sustain
   test (§7.6), = its measured `max_listening_level`.
2. Measure the **middle target at its interpolated anchor level**
   (one sweep + short sustain hold). Pass → `spot_verified`; fail →
   ladder that target properly and re-interpolate.
3. Measure the **shallowest non-natural target at one loud rung**.

Interpolation between measured points: raising the corner by ratio
`r` frees `40·log10(r)` dB of boost, buying the same dB of level at
equal excursion — so intermediate anchors sit on the line between
their measured neighbors, clamped by the digital constraint (§8.4).
If any measured point falls > 1 rung off the interpolation drawn
from its neighbors, the interpolation is distrusted for that span
and the affected targets get laddered (`evidence: "measured"`).

Deep mode (operator opt-in): full per-target ladders; same code
path, more rungs, every anchor `"measured"`. The first hardware
commission of each enclosure family should run deep mode once to
calibrate how much the interpolation can be trusted (§14.4).

### 7.5 Analysis additions (all pure numpy; Wave 1)

- **Harmonic extraction** (`deconv.py`): the Novak-sync property
  gives harmonic-image offsets `Δt_n = L·ln(n)` *ahead of* the linear
  IR. `extract_harmonic_windows(full_ir, meta, orders=(2,3))` windows
  each image (Hann, half-width bounded by the gap to the neighboring
  order), FFTs to `H_n(f)`, and maps output frequency `n·f` back to
  excitation `f`. `thd_curve(f) = sqrt(Σ|H_n|²)/|H_1|`. This consumes
  the **existing** unwindowed output of
  `regularized_deconvolution_full` — no change to the linear path.
- **Compression** (`analysis.py`): per-rung fundamental band levels
  (1/3-oct bands 20–200 Hz) minus (previous rung + commanded delta) →
  `compression_db_by_band`, cumulative vs the linear-extrapolated
  baseline rung (Klippel's compression-of-fundamental shape, 3 dB
  steps).
- **Tracking error:** measured minus `adapter.predicted_response`
  over the target's decision band, RMS + max.
- **Thresholds (v1, ride `algorithm_version`):** rung fails when
  compression > 2.0 dB in any 1/3-oct band within the decision band
  (normal margin; 1.5 conservative / 3.0 aggressive), or THD > 10 %
  (−20 dB) in the extension band below 80 Hz (3 % conservative /
  operator-set aggressive), or capture clip, or repeat spread > 2 dB,
  or SNR verdict `insufficient` (existing `snr_policy` bands), or the
  digital ceiling (§8.4) is reached. First failing rung stops the
  ladder; "stops tracking cleanly → try a shallower target" is
  realized by the anchor derivation, not by adding corrective EQ —
  the family is fixed at `propose` time.

### 7.6 Sustain stress test (new stage, deep-research delta #2)

Swept sines structurally understate the two phenomena that defeat an
open-loop schedule: **thermal compression** (voice-coil Re rises with
sustained power; Qes/Qts rise, sensitivity falls — up to ~4.5 dB at
coil-temperature extremes) and **port compression** (turbulence onset
~10 m/s air velocity with a sharp ~1 dB-to-audible knee, per
Salvatti/Devantier/Button JAES 2002). Both are sustained-power
effects a 8 s sweep barely tickles.

The stage: with the deepest target applied at the sweep-clean
ceiling level, play **60 s of band-limited pink noise** (default
band ~0.8·fp_deepest to 120 Hz; conservative margin 90 s, aggressive
30 s), honoring the driver-safety `minimum_cooldown_s` before and
after, with the same clip-abort and Stop paths as every stimulus.
Immediately re-run one short sweep and compare against the same
rung's pre-hold sweep:

- **Fundamental sag** > 1.5 dB in any decision band → thermal/port
  compression at this level; lower the ceiling one rung, allow one
  retry after cooldown (`limited_by: "sustain_sag"`).
- **Refit fc shift** > 5 % → suspension/thermal drift beyond the
  fit's trust region at this level → same action
  (`limited_by: "sustain_fc_shift"`).
- Pass → the ceiling stands and the profile records the sustain
  evidence (§5.1 `sustain_test`).

Admission note: the sustained stimulus goes through the same
two-boundary excitation admission as sweeps but needs its own
stimulus kind — the driver-safety `max_sweep_duration_s` ceiling is
sized for sweeps, so the bass-extension limits derivation must
declare the hold explicitly (duration, band, level) and the
per-target mandatory cooldown, rather than sneaking a 60 s tone
through a sweep-shaped request.

---

## 8. Runtime design

### 8.1 Graph placement

Bass extension's first runtime slice is **sealed-only** and lives
post-split on the bass-owner chain, i.e. as Layer-A per-driver filters
exactly where the layer model puts driver linearization — after the
split mixer, before the existing
`driver_delay → driver_baseline_gain → driver_baseline_limiter`
suffix ordering (concretely: `[bass_management_hp?] → crossover
biquads → bass_ext_lt → bass_ext_subsonic → delay → gain → limiter`).
Rationale:

- Only the bass owner is touched; tweeter/mid ways never see the
  filters (composition with the tweeter-protection emit gate is
  trivial).
- Both stereo woofer channels reference the **same named filter
  definitions** (`bass_ext_lt`, `bass_ext_subsonic`) — left/right
  linkage is by construction, and one `PatchConfig` touches both.
- The per-driver limiter (`clip_limit −1 dB`) downstream is the
  digital backstop for any headroom-math error — worst case is
  graceful limiting, never wrap/clip.
- Local-sub topologies with a sealed bass owner emit the same pair on
  the sub chain instead.

An accepted, current sealed profile's emitted **static** graph always
carries the pair at the **natural target's parameters** (LT with
`freq_act == freq_target`, `q_act == q_target` is an exact
pass-through). Extension is an *actively maintained enhancement*: the
scheduler patches deeper only while it is alive and confirmed; every
reset/reload/boot lands on natural. Fail-safe by construction, and it
means `classify_camilla_graph` re-proof and the statefile never see a
"deep" graph at rest.

Accepted ported/PR profiles emit **no `bass_ext_*` block** and must be
byte-identical to the ordinary baseline. They stay persisted with
`status="accepted"`, while `/state` reports their `adapter_id`,
`runtime_eligible=false`, and
`runtime_deferred_reason="fixed_graph_not_defined"`. This separates
"commissioned profile retained" from "runtime armed" without
changing Wave 1 or inventing an audio parameter. `runtime_eligible`
is adapter-level graph support, not live state; Wave 5's
`runtime_armed` is true only when an accepted, current eligible
profile has a live, confirmed scheduler.

### 8.2 The controller (scheduler)

Pure function first: `select_target(profile, listening_level,
current_target) -> TargetDecision`, with **safety-asymmetric
hysteresis** — retreat (shallower) triggers immediately when
`listening_level > anchor`, re-extend (deeper) only after the level
has been ≥ 4 points *below* the anchor for ≥ 2 s (sustained changes
only — a transient dip never triggers a re-extend). Anchors quantize
transitions to at most ~5 per full volume swing, and re-extends are
additionally rate-limited (≥ 5 s between deepening steps) so a volume
wiggle can never pump the alignment. Perceptual budget (deep-research
delta #6): each transition's effective bass-level trajectory moves in
≤ 1 dB micro-steps (below the ~1.25 dB program-material JND); the
corner *frequency* motion itself is not the audible quantity — the
level trajectory is — so smoothness of level beats exactness of
corner path.

This selection logic arms only when the profile is accepted, current,
and its adapter is in
`BASS_EXTENSION_RUNTIME_ADAPTER_IDS = frozenset({"sealed_v1"})`.
For accepted ported/PR profiles it deterministically returns
`natural` with reason `adapter_deferred`, does not start dwell/rate
timers, and never reads or patches CamillaDSP.

Placement — two hooks calling one idempotent
`ensure_bass_target(level)`:

1. **Synchronous, retreat-first:** inside
   `VolumeCoordinator._dispatch`, immediately **before** the
   `main_volume` write when the level is rising across an anchor, and
   after it when falling. Both host processes (`jasper-voice`
   long-lived coordinator, `jasper-control` per-request coordinators)
   run the same convergent logic; concurrent writes patch identical
   values, so the race is benign. CamillaDSP's own 400 ms
   `main_volume` ramp perceptually masks the concurrent target step.
2. **1 Hz reconciler backstop** in `jasper-voice`'s existing
   `VolumeObserver._tick` (the established polling pattern): re-read
   `speaker_volume.json` + the live filter params (`get_config`
   best-effort), converge drift, and — on any doubt (camilla
   unreachable, profile stale, params unreadable) — **converge toward
   natural**, never toward deep.

PUSH-source subtlety (Spotify/BT pin camilla at 0 dB and carry level
source-side): the scheduler keys off canonical `listening_level`,
which `VolumeCoordinator` maintains across both carrier modes, so the
mapping holds; source-side curve mismatch is absorbed by the digital
margin (§8.4) and the limiter backstop.

### 8.3 The transition mechanism — Wave 0 chose sealed R1

CamillaDSP documents smooth ramping **only** for fader gains;
coefficient updates are undocumented (assume hard swap). Wave 0
measured the two candidates and chose R1 for the fixed sealed graph;
R2 remains the rejected fallback record:

- **R1 — stepped `PatchConfig` on the live sealed filter pair
  (confirmed).** Interpolate `(fp, Qp)` — the *design parameters*, which
  is the stability-safe interpolation domain — in enough steps that
  no step changes the response by more than ~1 dB anywhere
  (typically 4–8 steps for adjacent members), spread over
  ~0.5–1 s, one `PatchConfig` per step. Adjacent family members are
  low-Q and ≤ ~3 dB apart, literature expectation is transients
  < −60 dBFS, and the concurrent volume ramp masks further. Zero
  graph-structure cost. Precedent: `runtime_balance.py` patches a
  named Gain live today. Wave 0 measured ≥15 dB burst margin below
  the bench threshold for the micro-stepped LT transition.
- **R2 — parallel A/B branches crossfaded by Aux faders (documented-
  safe fallback).** Split the bass-owner channels into an A and B
  lane (one extra mixer pair), each lane carrying its own LT+HP set
  and a `Volume` filter on `Aux1`/`Aux2`; patch the **silent** lane's
  coefficients (inaudible by definition), then equal-power
  `SetFaderVolume` ramp (~300 ms). Costs two mixers + duplicate
  filters + two faders in the emitted graph, all static structure.
- Rejected: full `set_config_file_path` reload per transition — the
  right tool for profile apply/bypass, too heavy and unproven-
  continuous for a knob-coupled runtime path.

R1 changes only parameters of definitions already present in the
emitted graph; it never changes pipeline or filter-map structure.
The Wave 0 evidence does not authorize ported/PR member transitions:
those would add/remove filters or change slot types unless a new fixed
carrier/bypass design were introduced and independently measured.

**Both mechanisms add zero buffer latency** (minimum-phase IIR; no
chunk/queue/rate changes), so the 40 ms USB cert is untouched — the
cert measures broadband arrival, not LF group delay (which any
alignment, physical or DSP, inherently shapes). CPU: ≤ 4 extra
biquads (R1) or ≤ 10 (R2) — far below 1 % of a Pi 5 core against
CamillaDSP's published FIR benchmarks.

⚠ One discovered gotcha: **`PatchConfig` state does not
survive a config reload from file.** Any `apply_dsp_config` (room
correction save, preference EQ change, deploy bounce) resets the
filters to the emitted natural params. This is *fail-safe by
direction* (resets go shallow, never deep) and the 1 Hz reconciler
re-converges within a second; the doctor check (§10.3) watches for a
scheduler that stopped re-converging.

### 8.4 Digital headroom — calculation and representation

- Per-target `boost_headroom_db = max_f(|H_target(f)| − |H_natural(f)|)`
  over a 10–500 Hz grid, computed at family generation and **frozen
  into the profile** (§5.1).
- Digital anchor constraint:
  `percent_to_db(listening_level) + boost_headroom_db ≤ −digital_margin_db`
  (default margin 3 dB; conservative 4, aggressive 2). With the 100 %
  → 0 dB ceiling this means an 11.8 dB target is digitally ineligible
  above the level whose curve value is −14.8 dB — the effective
  anchor is `min(digital, acoustic)` and the profile records which
  bound.
- Interactions already accounted: the split mixer's mono-sum is
  clip-safe (−6.02 dB); `active_baseline_headroom` (Layer-B boost
  budget) is upstream and separate — but Layer-B room boosts in the
  same band *do* stack acoustically, which is one reason the margin
  exists and the per-driver limiter is the enforced backstop. The
  wizard surfaces `max boost + current room-EQ low-band boost` when
  it exceeds budget as a WARN, not a block (tinkerer philosophy).
- **Target-coupled limiter threshold (deep-research delta #3).** The
  bass-owner chain's existing `driver_baseline_limiter` slot stops
  being a fixed −1 dB digital clip guard when a sealed profile is
  accepted and runtime-eligible:
  each family member carries `limiter_threshold_dbfs` = the maximum
  digital drive *verified clean for that member* (ladder + sustain
  evidence) + margin, and the runtime patches the limiter threshold
  **in the same transition as the alignment**. This is the cheap
  answer to "an open-loop schedule has no runtime protection": the
  fault coupling is in the safe direction — a scheduler wedged on a
  deep target is also wedged on that target's *conservative*
  threshold, so sustained bass beyond the verified envelope clips
  gracefully at the verified ceiling instead of over-driving the
  woofer. It is a last resort, not the control mechanism; if
  fault-mode clipping proves audible in practice, the upgrade path is
  a CamillaDSP `Compressor` processor in the same slot (no lookahead
  → no latency). The natural member's threshold is the baseline
  −1 dB. Ported/PR members retain their commissioned threshold facts,
  but the first runtime slice does not patch them.
- UI representation: each target row shows "needs X dB headroom →
  usable below level N (digital) / level M (measured)".

### 8.5 Emit gate (mirrors the L0 tweeter gate)

`_assert_bass_extension_safe(emitted_text, profile)` re-parses the
just-emitted YAML. For an accepted, current sealed profile it raises
unless: (a) the subsonic HP is on every bass-owner channel with a
legal corner (unless expert removal is explicitly recorded); (b) the
LT params exactly match the frozen natural member; (c)
`boost_headroom_db` of the emitted member is 0.0; and (d) the
downstream limiter is present with the natural member's threshold and
`volume_limit ≤ 0` survives. For ported/PR, bypassed, stale, or
missing profiles it requires **no `bass_ext_*` definitions or chain
references**; any injected block fails closed. Independent re-proof,
not emitter trust — same philosophy, new predicate in
`graph_safety.py`.

### 8.6 Apply / bypass / fallback

Sealed profile accept and bypass are full-graph transactions mirroring
`commissioning_apply.apply_measured_candidate`: compile via
`build_baseline_profile_candidate` (now bass-extension-aware) →
predecessor `snapshot_exact_dsp_state` as rollback artifact →
`apply_dsp_config` → fresh readback must match expected graph
fingerprint + unchanged volume + protection classification → retained
proof; any failure restores the exact predecessor with readback proof.
Bypass pins the scheduler to natural and marks the profile `bypassed`;
the graph already carries the natural identity pair at rest.

Ported/PR acceptance retains `status="accepted"` and its evidence but
does not run a graph transaction or alter the baseline candidate/YAML;
bypass is likewise profile-state-only. Missing/stale/invalid/deferred
profiles never arm the scheduler and keep ordinary applied-baseline
behavior. **There is no failure mode that requires the graph to change
to become safe**: sealed reloads land on identity, while deferred
adapters never add the block.

---

## 9. UI placement

- **Commissioning** attaches to the active-speaker setup flow (the
  profile is hardware-bound), as a post-baseline step: "Bass
  extension (optional)". Served through the `/correction/` HTTPS
  framework (phone mic requires it), new page module +
  `deploy/assets/correction/js/bassext/` ES modules per the canonical
  wizard conventions (`canonical_page()`, `json_island`, shared
  dialog/escape/dom modules, CSRF helpers). ⚠ Do **not** bolt onto
  `correction_setup.py`/`session.py`/`main.js` internals beyond the
  minimal routing seam — those god-files have a pending planned split
  (room-correction modernization R1–R5); keep every new surface in
  new modules so the split doesn't have to move us.
- **The existing Bass Management page** (`/correction/bass/`,
  `correction_bass_flow.py`) becomes the **low-frequency overview**,
  still read-only: static crossover corner + owner (existing), plus
  Bass Extension status — commissioned range ("31 Hz to level 50 →
  natural 61 Hz"), current active target, enabled/bypassed/stale, and
  a link *to* the commissioning flow. Control stays in the flow; the
  page stays a seam.
- Wizard UX beats: margin selector with plain-language consequences;
  the family/anchor review as a simple table + predicted-curves plot;
  a persistent red Stop during any playback (wired to
  `_graceful_stop`, honoring the `/sound/` combined-test wedge
  lesson: Stop must not depend on page reload); explicit "measured
  clean operating envelope, not a driver warranty" copy; expert
  drawer for Qp, boost cap, rung size, subsonic overrides.

---

## 10. Observability, doctor, failure behavior

### 10.1 Logs (all via `jasper.log_event`)

`event=bass_ext.commission_*` (state transitions, rung results with
compression/THD numbers), `event=bass_ext.target_change`
(from/to/level/mechanism/duration), `event=bass_ext.reconcile_drift`,
`event=bass_ext.fallback_natural reason=…`. Target changes are ≤ a
few per volume swing — no journal spam.

### 10.2 `/state.bass_extension`

`{commissioned, profile_id, status: accepted|bypassed|stale|absent,
adapter_id, runtime_eligible, runtime_deferred_reason,
runtime_armed, current_target, current_extension_hz, deepest_hz,
natural_hz, anchors: [...], scheduler_alive, last_transition_at}` —
read fresh from disk + best-effort camilla, fail-soft null like every
other section. Accepted ported/PR is an ordinary healthy state:
`runtime_eligible=false`,
`runtime_deferred_reason="fixed_graph_not_defined"`,
`runtime_armed=false`, and `current_target="natural"`.

### 10.3 Doctor (flat, one `CheckResult` each)

`check_bass_extension_profile`: absent → OK ("not commissioned");
stale bindings → WARN with the mismatched fingerprint named;
accepted sealed + emitted graph missing the frozen filter names →
FAIL; sealed scheduler heartbeat stale → WARN; sealed live filter
params ∉ frozen family (best-effort read) → WARN ("drifted,
reconciler should converge; investigate if persistent"). Accepted
ported/PR → OK with explicit "runtime deferred" detail; absence of
`bass_ext_*` filters and a runtime heartbeat is correct, not drift.

### 10.4 Failure ladder

| Failure | Behavior |
|---|---|
| Profile missing/stale/bypassed | Sealed graph is already natural at rest (or block absent); scheduler never arms. Silent-by-design (missing/bypassed are OK; stale WARNs; status on overview page) |
| Accepted ported/PR profile | Profile/evidence retained; ordinary baseline graph stays active; scheduler reports `adapter_deferred` and never patches. `/state` + doctor make the deferral explicit. |
| Scheduler process dies | Filters stay at last patched target; **worst case is bounded**: deepest target at rising volume is still inside the limiter backstop and the digital margin; partner-process hook or 1 Hz reconciler (whichever survives) converges; doctor WARNs on heartbeat |
| CamillaDSP restart / config reload | Filters reset to natural (emitted params); reconciler re-extends within ~1 s if level permits — fail-safe direction |
| camilla unreachable during transition | `patch_config(best_effort=True)` no-ops; reconciler retries; scheduler holds *shallower* of (current, desired) |
| Mid-ladder crash during commissioning | Coordinator lease expiry restores renderers; commissioning-runtime bounded transaction restores the exact entry graph with readback proof; session store marks `interrupted` (crossover_level_run pattern) |

No audible-cue additions needed: nothing here can block wake/voice,
and music degradation is "less deep bass," which the overview page
and doctor surface.

---

## 11. Test plan

- **Numerics (deterministic, hardware-free — the bulk):** synthetic
  plants (known f0/Q0; 4th-order vented with known fb; PR with known
  notch) → fit round-trips within tolerance; fit-error injection
  (±10 % f0, ±0.15 Q, ±1 dB noise) → bounded family error; boost
  formula pinned (`40·log10(f0/fp)` worked examples); harmonic
  offsets pinned against `Δt_n = L·ln(n)` on synthetically distorted
  sweeps (inject known H2/H3, recover THD within 1 dB down to 20 Hz);
  compression curves on synthetic soft-clipped ladders; anchor
  derivation (equal-excursion + digital clamp) pinned; scheduler
  hysteresis property tests for sealed profiles (never extends above
  anchor, retreat monotone in level, natural at boot), plus ported/PR
  always-natural `adapter_deferred` tests.
- **Contract tests:** profile schema round-trip + staleness matrix
  (every binding mismatch → stale); refusal vocabulary completeness;
  sealed emit-gate red-team (strip subsonic HP → raises; off-family
  params → raises; non-natural at-rest params → raises);
  `classify_camilla_graph` accepts sealed extension-bearing baselines
  and still rejects tampered ones; accepted ported/PR emits the
  byte-identical ordinary baseline, reports runtime-ineligible, and
  rejects any injected `bass_ext_*` block; targets[last]-is-natural
  invariant; ladder state machine transition table (mirror
  `test_commissioning_lifecycle` style).
- **Integration (mocked camilla/relay):** ladder end-to-end with
  scripted rung results including mid-ladder abort/restore; apply
  transaction rollback on injected readback mismatch; scheduler +
  fake VolumeCoordinator dispatch ordering (retreat-before-louder);
  accepted ported/PR never calls graph apply or `PatchConfig`.
- **Browser (harness per existing JS conventions):** wizard module
  conventions test coverage (auto via
  `test_web_wizard_conventions.py`), Stop-button wiring test.
- **Pi performance:** camilla CPU% with the sealed extension block
  (R1) vs baseline; `PatchConfig` round-trip latency under load;
  scheduler wall-time per tick.
- **Latency:** re-run the existing route-latency cert after Wave 3 on
  the lab box — expected delta zero; the cert gate is the proof.
- **Hardware validation (Wave 7, the only paid-in-time wave):**
  transition-audibility bench (Wave 0 rig, re-run on final code);
  full sealed commission on a lab box incl. deliberately-wrong cases
  (mic bumped mid-ladder → refusal fires; AGC phone → ramp refuses);
  ported + PR **commissioning/profile-retention** on whatever boxes
  exist (borrow/build — a cheap PR bookshelf is worth buying for
  this), confirming the UI/state says runtime deferred; one week of
  daily listening with a sealed family armed, checking
  `event=bass_ext.*` cadence and the overview page.

---

## 12. Phased implementation waves (Codex delegation plan)

Sequencing constraints honored: Waves 1–2 are pure new files (zero
collision with the crossover program's active churn); Wave 3 touches
the hot files (`camilla_yaml.py`, `runtime_contract.py`,
`camilla_emit.py`) and must be small-PR + rebase-often; the wizard
wave keeps out of the god-files. Every implementation PR runs
`scripts/test-fast` locally and lands via the normal PR flow.

> **Execution prompts live in
> [`docs/bass-extension-waves/`](bass-extension-waves/README.md)** —
> one self-contained Codex prompt per wave plus a shared engineering
> charter (file allowlists, frozen interfaces, anti-overengineering
> fences, preflight drift checks). The operator hands Codex exactly
> one wave file per session. The wave summaries below remain the
> architectural intent; the prompt files are the binding execution
> contracts and are revised (not improvised around) when main drifts.

**Wave status:** (update as waves land)

| Wave | Prompt | Status |
|---|---|---|
| 0 | [wave-0](bass-extension-waves/wave-0-hardware-spikes.md) | spikes 1–3 done 2026-07-16 — **R1 confirmed** ([memo](research/2026-07-16-bass-extension-spikes/README.md)); spike 4 + ears-on listen with operator |
| 1 | [wave-1](bass-extension-waves/wave-1-numerics.md) | **merged 2026-07-16** (#1549, contract rev 3; review-gate loop caught 6 rev-1 spec contradictions → rev 2) |
| 2 | [wave-2](bass-extension-waves/wave-2-profile-observability.md) | **merged 2026-07-16** (#1553; clean gate after 3 review findings fixed in-session) |
| 3 | [wave-3](bass-extension-waves/wave-3-graph-emission.md) | implementation parked after draft #1558 stop report; contract rev 2 narrows runtime to sealed-only |
| 4 | [wave-4](bass-extension-waves/wave-4-commissioning-backend.md) | not started |
| 5 | [wave-5](bass-extension-waves/wave-5-runtime-scheduler.md) | not started |
| 6 | [wave-6](bass-extension-waves/wave-6-ui.md) | not started |
| 7 | [wave-7](bass-extension-waves/wave-7-hardware-validation.md) | not started |

### Wave 0 — Hardware prototypes (decision spikes, lab box; ~2 days; NOT Codex — needs hardware)

Deliverable: a short decision memo committed under
`docs/research/`, answering:

1. **Transition mechanism:** with a 45 Hz sine + pink noise playing,
   measure the electrical output (existing `:9891` reference tap)
   across (a) single hard `PatchConfig` LT swap between adjacent
   family members, (b) 6-step parameter interpolation, (c) A/B
   Aux-fader crossfade, (d) `set_config_file_path` reload. Metric:
   max sample-to-sample discontinuity + audibility. Pick R1 or R2.
2. **Harmonic extraction sanity:** run the Wave-1 extraction math
   (prototype notebook is fine) on one real nearfield sweep;
   cross-check THD shape against REW on the same capture.
3. **`PatchConfig` semantics:** confirm patched params survive/reset
   across volume writes, `Reload`, and `apply_dsp_config` (expected:
   reset on file reload — encode the answer in the reconciler test).
4. **Nearfield mic ceiling:** at the loudest planned rung, does a
   phone mic at 1–2 cm clip? Establish the recommended mic distance
   and whether the ladder needs a distance-vs-level note in the UI.

### Wave 1 — Numerics core (pure Python, hardware-free; ideal Codex wave)

`jasper/bass_extension/{alignment,targets}.py`,
`adapters/{base,sealed,ported,passive_radiator}.py`,
`deconv.py` harmonic extraction, `analysis.py`
compression/THD/tracking + sustain-comparison metrics (§7.6),
the fit-order sanity check, and the optional impedance `.zma`
parser/cross-check (§6.2). Full deterministic test suite (§11 bullet
1). **Interfaces frozen by this plan**: §6.1 Protocol, §7.5 function
names, §5.1 target/anchor dict shapes (including
`limiter_threshold_dbfs`). No I/O, no camilla, no HTTP.
Acceptance: tests green + a demo script that takes a synthetic plant
and prints the family/anchor table from §1.1.

### Wave 2 — Profile, admission, observability skeleton

`jasper/bass_extension/profile.py` (schema, refusals, staleness
evaluation), persistence + bundle integration,
`/state.bass_extension` (reporting absent/stale only, no runtime
yet), `check_bass_extension_profile` doctor check,
`bass_management.py` read-resolver extension. Contract tests per §11.
Depends: Wave 1 dataclasses.

### Wave 3 — Graph emission + apply transaction (⚠ hot-file wave; small PRs)

`camilla_emit.py` LT/Butterworth-HP emitters;
`camilla_yaml.py` sealed-only bass-extension block (natural-at-rest
invariant); ported/PR profiles remain accepted/observable but emit the
byte-identical ordinary baseline;
`graph_safety.py` predicate + emit gate;
`runtime_contract.py` classification extension (**land this in the
same PR as the emitter change** — a split ships a graph the contract
re-proof rejects); apply/bypass transaction. Red-team contract tests.
Depends: Wave 2. Coordinate with any in-flight crossover-program PRs
touching the same files; rebase before push per AGENTS.md.

### Wave 4 — Commissioning backend

`ladder.py` state machine (pure) + web backend module (new file, thin
routing seam into `/correction/`), relay `build_bass_nearfield_spec`,
per-rung retention store, integration of ramp/admission/
measurement_window/repeat-admission, the sustain stress-test stage
(§7.6) including a **sustained-stimulus admission kind** (explicit
duration/band/level + mandatory cooldown — do not push a 60 s hold
through a sweep-shaped `ExcitationRequest`), and the
characterize→fit→propose→verify→ladder→sustain→anchors→review HTTP
endpoints returning JSON the Wave-6 UI consumes. Integration tests
with mocked camilla/relay per §11. Depends: Waves 1–3.

### Wave 5 — Runtime scheduler

`scheduler.py` (pure) + `runtime.py` sealed-only transition executor
(R1 per Wave 0; ≤1 dB micro-steps over 0.5–1 s; limiter threshold
patched in the same transition as the alignment per §8.4),
`VolumeCoordinator` dispatch hook (retreat-first ordering), re-extend
rate limiting per §8.2, 1 Hz reconciler in the existing
`VolumeObserver`, `/state` live/deferred fields, doctor
heartbeat/drift/deferred checks, failure-ladder tests. Ported/PR is a
tested no-patch `adapter_deferred` state. Depends: Waves 2–3;
mechanism decision from Wave 0.

### Wave 6 — UI

Commissioning wizard pages + ES modules (canonical conventions),
Bass Management → low-frequency overview upgrade, Stop-button
robustness, expert drawer. Depends: Wave 4 endpoints; Wave 5 for live
status display.

### Wave 7 — Hardware validation + docs (human-driven, Codex assists)

The §11 hardware program on the lab boxes; threshold tuning from real
ladders (expect the §7.5 numbers to move — they ride
`algorithm_version`); `docs/HANDOFF-bass-extension.md` operational
doc (current-state-first, this plan gets the historical tag); README
atlas + `docs/doc-map.toml` entries; `.env.example` comments for any
env knobs added.

---

## 13. Risks and explicit non-goals

### Risks

1. **Transition clicks (R1 bench-proven; ears-on final listen open).**
   Wave 0 measured ≥15 dB burst margin for sealed micro-steps; retain
   the operator's final ears-on check. R2 remains the documented
   fallback; transitions are rare and volume-ramp-masked.
2. **Hot-file collision with the crossover program** (main is
   churning in `camilla_yaml`/`runtime_contract`/correction backend
   *today*). Mitigation: wave ordering puts pure-new-file work first;
   Wave 3 is small-PR + same-PR contract updates; rebase discipline.
3. **The whole correction/crossover substrate is code-complete but
   hardware-unvalidated** (2026-07-15 assessment). Bass extension
   inherits that risk transitively — its Wave 7 cannot start until
   the crossover program's own on-device validation has burned in the
   shared machinery (ramp, admission, relay). Sequence accordingly.
4. **Plant drift with level** (f0/Q0 rise with drive; fb shifts) —
   the deepest target's fit degrades at the top of its range.
   Mitigation: verify-deepest-at-low-level + ladder tracking-error
   thresholds catch it; margins encode the residual.
5. **Nearfield mic clipping at loud rungs** (port exits exceed
   120 dB SPL). Mitigation: existing clip-abort; Wave 0 establishes
   distance guidance; the ladder stops at `mic_clip` and records the
   bound honestly.
6. **Room-EQ boost stacking** in the extension band erodes digital
   margin. Mitigation: WARN surface + limiter backstop + margin;
   full budget unification with `active_baseline_headroom` is
   deliberately deferred (a later `algorithm_version`).
7. **Two-process scheduler writes** (voice + control) race in theory.
   Mitigation: idempotent convergent target selection; worst case is
   a duplicate patch of identical values; reconciler converges.
8. **Patents (deep-research finding; technical characterization, not
   legal advice).** The runtime behavior likely reads on the broad
   independent claims of Google **US10,200,003 / US10,666,217**
   (headroom/volume-driven Linkwitz-Transform modification — the spec
   even contains our 1 dB ↔ 1/12-octave mapping) and is close to
   **US12,342,139** (volume-indexed LT lookup table + multiband
   compressor, microspeakers; we differ by having no compressor-as-
   protection, per-unit measurement, and retained ported/PR
   commissioning without runtime arming). The
   commissioning workflow appears free of located prior art. JTS is
   open-source and non-commercial, but before this feature is
   promoted as a product capability — and certainly before any
   filing — have counsel review those families. Product copy should
   describe the *commissioning workflow*, never "bass that changes
   with volume," both for accuracy and positioning.
9. **Thermal/sustained-content drift is the physics we cannot
   measure at runtime** (no V/I sensing on this platform). The
   sustain test (§7.6), the margin tiers (conservative sized for
   ~10 % fs drift + hot-coil sensitivity loss), and the
   target-coupled limiter (§8.4) are the three compensations; a
   household that plays sustained organ music at the ceiling for ten
   minutes is still relying on the limiter, and the docs say so
   plainly.

### Non-goals (v1)

- No driver database, no excursion/thermal simulation, no
  voltage/current sensing, no runtime microphone loop, no ML, no new
  daemon, no lookahead/FIR in the bass path. **V/I (back-EMF)
  sensing hardware is a permanent platform non-goal, not a deferral**
  (maintainer decision 2026-07-16): the deep-research report's
  "switch to a live excursion model if you add sensing" branch is
  closed — the impedance cross-check stays file-import only, and the
  open-loop schedule + sustain test + target-coupled limiter is the
  design, not a stopgap.
- No signal-aware (program-dependent) controller — designed-for but
  explicitly after the volume-linked product works; the scheduler
  seam (`select_target`) is where it would slot in.
- No ported/PR runtime emission or scheduling in the first shipped
  slice. Their Wave 1 adapters, commissioning evidence, accepted
  profiles, and UI/state visibility remain in scope; only live graph
  arming is deferred behind the fixed-graph proof in §14.
- No far-field reconstruction / splice / baffle-step claims — the
  profile's claims are fixed-position-relative by design; the splice
  lane upgrades the *display* later, not the decision.
- No wireless-sub or bonded-pair emission (refusal
  `BONDED_BASS_OWNER_REMOTE`); no passive full-range (non-active)
  speakers — the math generalizes but the graph carrier work is its
  own slice; no multi-driver/multi-port/TL/OB/horn adapters
  (`ENCLOSURE_UNSUPPORTED`, future adapters).
- No loudness compensation (separate feature; explicitly kept
  composable and distinct).
- No psychoacoustic/virtual bass (missing-fundamental synthesis à la
  MaxxBass/UltraBass). Noted as a *possible future* high-volume
  fallback — smart amps fall back to it when real bass runs out of
  excursion — but it is a different product with different artifacts,
  and v1's retreat-to-natural is the honest version.

---

## 14. Decisions requiring a focused prototype before implementation

1. **R1 vs R2 transition mechanism — decided for sealed:** Wave 0.1
   confirmed micro-stepped R1. This does not authorize changing graph
   structure for another adapter.
2. **`PatchConfig` reset semantics — decided:** Wave 0.3 confirmed
   patches survive volume writes and reset on file reload; the sealed
   reconciler encodes that result.
3. **Ported family shape** (pure sliding-HPF vs HPF+shelf composite)
   — decide after the first real ported fit; both are inside the
   `ported_v1` adapter's parameter space, so this is a tuning
   decision, not an interface one.
4. **Anchor-derivation fidelity** (equal-excursion mapping vs full
   ladder) — validate once on the first hardware commission by
   running deep mode and comparing; if derived anchors are off by
   > 1 rung, default mode ladders two targets instead of one.
5. **Threshold defaults** (§7.5, §7.6) — expected to move after the
   first three real commissions; they ride `algorithm_version` so
   profiles self-describe which rules produced them.
6. **Impedance-import necessity** — if the first several hardware
   commissions show nearfield-fit and `.zma` cross-checks agreeing
   within 10 %, the import stays a rarely-used expert feature; if
   they disagree on ordinary boxes, promote it in the wizard copy for
   ported/PR (where fb matters most). Decided by Wave-7 data.
7. **Ported/PR fixed runtime graph** — deferred beyond the sealed
   first slice. Before either adapter can arm, a focused contract and
   hardware prototype must prove one static, re-proofable graph across
   every frozen family member (including members whose current
   `filters` tuples add/remove the retreat high-pass), click-safe
   transitions, natural-at-rest/reset behavior, limiter coupling, and
   no invented Q/filter parameters. Until then those profiles remain
   retained and observable with `fixed_graph_not_defined`.

## 15. External references

- Linkwitz Transform / pole-zero math: linkwitzlab.com/filters.htm
- Keele nearfield method: AES e-lib #2774 (1974); D'Appolito,
  audioXpress "Measuring Loudspeaker Low-Frequency Response"
  (fb from woofer-nearfield minimum; port diameter-ratio summation;
  `f_max ≈ 4311/D_inches` validity; mic ≤ 0.11·a)
- Synchronized ESS + harmonic separation: Novak et al., JAES 61(7),
  `Δt_n = L·ln(n)`; REW distortion docs (LF sweep-length practice)
- Level-stepped compression: Klippel "Compression of the fundamental"
  / Max-SPL application notes (3 dB steps; 1/3 dB convention)
- Volume-adaptive prior art: B&O ABL (tonmeister.ca), Devialet SAM,
  Powersoft limiter app note (sliding HPF)
- CamillaDSP: github.com/HEnquist/camilladsp — `LinkwitzTransform`
  Biquad subtype; fader `SetFaderVolume` ramp; `PatchConfig`
  (v4.1); reload semantics ("apply without interrupting processing";
  no click-free coefficient claim)

From the 2026-07-16 deep-research report:

- Patents: Google US10,200,003 / US10,666,217 (dynamically extending
  loudspeaker capabilities — volume/headroom-driven LT);
  US12,342,139 (volume-dependent LT lookup + MBDRC, microspeakers);
  Bose US4,490,843 / US4,739,514 and Short US5,361,381 (analog
  dynamic bass EQ); US5,481,617 (signal-driven sliding HPF); Samsung
  US10,797,666 (port-velocity limiter; ~35 m/s ceiling); TI
  US10,674,263 (model-based excursion protection incl. ported/PR)
- Driver physics: Klippel AN42 (fs drift ≈ ½ relative peak-
  displacement change; ±3 % small-signal, compliance +200 % at
  temperature extremes); Button JAES 1992 (thermal compression,
  Re ≈ +0.39 %/°C, ~4.5 dB loss at 200 °C);
  Salvatti/Devantier/Button JAES 2002 (port compression, ~10 m/s
  turbulence onset); Struck & Temme JAES 1994 (nearfield validity
  < 1 dB where λ > ~3× source dimension)
- Perception: Toole & Olive JAES 1988 (~0.25–1.25 dB resonance JND by
  program material); Olive et al. JAES 1997 (LF detection thresholds
  unpredictable); Avis/Fazenda et al. AES 2007 (LF Q changes below
  Q≈16 undetectable with music). No published JND for a moving LF
  corner — open gap.
- Standards: IEC 60268-21:2018 (acoustic, DSP-system, small+large
  signal — matches our mic-only method), IEC 60268-22:2020
  (electrical/impedance — the optional cross-check), CTA-2010/2034
  (reporting formats)

---

Last verified: 2026-07-17
