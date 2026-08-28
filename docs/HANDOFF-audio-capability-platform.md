# Handoff: audio hardware capability platform

**Part of the JTS extensibility model** — this doc owns the *Hardware
profiles* contract: how JTS decides what audio hardware is present, what it
may be used for, and how that answer reaches every status surface. The
cross-cutting lens (the host-mediated-indirection invariant, the five
extension contracts, the decision tree) lives in
[extensibility.md](extensibility.md).

It sits above the empirical workstreams: [HANDOFF-aec.md](HANDOFF-aec.md) owns
AEC engine/topology, [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md) owns
chip-AEC lab findings and DAC viability methodology,
[HANDOFF-mic-fusion-architecture.md](HANDOFF-mic-fusion-architecture.md) owns
wake-leg/fusion architecture,
[HANDOFF-wake-training-experiment.md](HANDOFF-wake-training-experiment.md) owns
corpus/model training, and
[HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md) owns the wake-event
schema. The output/measurement-side sibling is
[HANDOFF-audio-measurement-core.md](HANDOFF-audio-measurement-core.md).

The 2026-06 roadmap that produced this contract — the conceptual
`MicCapability`/`DacCapability` vocabulary, the Phase 0 duplication audit, the
phased plan, and the UI direction — is archived in
[audio-capability-platform-plan-2026-06.md](historical/audio-capability-platform-plan-2026-06.md).

## Who owns which fact

Add capability fields to the owner below; do not build a parallel truth.

| Owner | Owns |
|---|---|
| `jasper/wake_legs.py` | Stable wake/corpus leg identity: tokens, UDP ports, kinds, whether a leg is a production wake input. |
| `jasper/mics/xvf3800.py` | The XVF3800 mic-family profile: USB identity, ALSA card name, firmware variants, mixer invariants, channel indices. |
| `jasper/audio_hardware/dac.py` | The static, pure-data DAC profile registry, including `supports_active_crossover_commissioning` (base DAC8x only — the owning Active service also requires a two-way preset). Product-scope authority, not a substitute for live evidence. |
| `jasper/output_hardware.py` + `deploy/bin/jasper-audio-hardware-reconcile` | Output-side runtime classification. The reconciler writes `/run/jasper-output-hardware/output_hardware.json` (observed output profile/card facts plus resolved `usb_data_role`: board topology, registered I²S overlays, desired/configured/active role, strict gadget and active management-transport availability, reason, reboot requirement). `/state`, `/sound/output-topology`, and doctor consume that artifact instead of re-deriving DAC semantics. |
| `jasper/chip_aec_policy.py` | The DAC side of the chip-AEC gate: registry qualification (`approved_chip_aec_dac_ids()` derives `APPROVED_DAC_IDS` from every profile whose `chip_aec_qualification` is `approved` — no hard-coded pair list) plus optional live outputd `aec_clock` evidence. `auto_allowed` is the only gated fact (`approved` ⇒ true; `testing` and `needs_calibration` ⇒ false), and `permits_selection(auto_allowed=, testing_requested=)` is the one seam that answers whether a given selection may arm (`ChipAecGate.permits` delegates to it; a caller holding only a serialized gate calls it directly). The AEC reconciler writes the answer into `/etc/jasper/jasper.env`; `/aec`, `/state`, doctor, and audio-validation consume it. |
| `jasper/audio_profile_state.py` | The shared read-only classifier for intent vs observed runtime truth. Feeds `/aec`, `/state.aec`, `/wake/` (via the `/aec` proxy), and doctor's "Audio profile" check, so every surface reports the same requested/active profile, session source, legs, and warnings. |
| `deploy/bin/jasper-aec-reconcile` | Intent (`/var/lib/jasper/aec_mode.env`) → runtime env for voice, bridge, init, and outputd. Sole writer of concrete `JASPER_MIC_DEVICE*`. |
| `jasper/cli/aec_init.py` | Volatile XVF3800 fixed-profile writes and read-back verification. Never resets the chip, never persists. |
| `jasper/cli/aec_commission.py` | The explicit commissioner (CLI, or the /wake button via `jasper-aec-commission.service` — never automatic) — the only thing that performs the single volatile reset, and the only producer of the banked alignment K that arms the chip. Its absence discloses and falls back; it does not park. |
| `jasper/cli/aec_bridge.py` | Mic capture, WebRTC AEC3, optional DTLN, chip-AEC beam forwarding, UDP leg emission, corpus-only streams. |
| `rust/jasper-outputd` | Final DAC playback and, in chip-AEC mode, fanning the final speaker buffer to the XVF3800 USB-IN reference path; owns reference health counters. |
| `jasper/voice/input_policy.py` | The provider-facing boundary. Converts applied mic/AEC runtime config into an input contract (`xvf_chip_aec`, `xvf_software_aec3`, `custom_udp`, `direct_mic`) and resolves provider preprocessing (e.g. OpenAI `noise_reduction=auto`) from it, so provider adapters receive a resolved policy, not raw hardware guesses. |
| `jasper/audio_validation.py` (+ `audio_validation_route.py`) | Schema-v1 validation artifacts under `/var/lib/jasper/audio-validation/`. `audio_validation_route.py` is separate: live fan-in identity assessment, no artifact of its own. |

## The rules that hold

1. **Capability beats special case.** Prefer "this mic exposes a hardware-AEC
   beam and a raw channel" over "if XVF, do X". Chip-specific commands stay
   inside the chip profile/init layer.
2. **Observed truth beats intent.** `/aec.raw_intent` is the saved request;
   `/aec.mode`, `.bridge_role`, `.software_aec3`, `.legs`,
   `.audio_profile.active`, and `.mic_settings` are reconciler-applied runtime
   truth. With no concrete profile active, `bridge_role=pending` is the honest
   answer — a live bridge process alone is not proof that AEC3 is running on
   the detected mic.
3. **Chip-AEC-or-disclose, not chip-AEC-or-park** —
   [ADR-0101](adr/0101-proven-once-disclose-on-change.md). A managed XVF whose
   chip path cannot be armed keeps hearing: `disclose_stale_chip_aec` writes
   `alignment=disclosed_stale` with a reason and an action and falls back to
   the software AEC3 leg, or to the chip's plain capture when no bridge can
   run. `disclosed_stale` is a **running** state, so `/aec` names the engine
   the wake path actually has. Parking is reserved for a box that cannot hear
   at all: an absent/unsupported mic (`unavailable`) and the `fault` arms —
   a chip-reference producer, bridge, or alignment reapply that failed.
   `audio_profile_state` encodes exactly that: park when the alignment status
   is set and is **not** in `{ready, disclosed_stale}`.
4. **A profile applies its whole leg set** —
   [ADR-0170](adr/0170-a-selectable-audio-input-profile-owns-its-whole-wake-leg-set.md).
   `custom` remains the sole explicit lab escape and owns its legs directly.
5. **Persist only what reproduces a product decision.** Managed-XVF activation
   uses one compact identity + timing artifact. Broader timestamped validation
   reports are diagnostic evidence, not a second activation authority.
6. **Profiles are declarations, not scripts.** A profile says "chip-AEC
   150/210, session carrier on :9876, no raw or DTLN"; the reconciler and
   producers make it true.
7. **Cost is part of capability.** A 1 GB Pi cannot treat DTLN, AEC3 sweeps,
   chip-AEC, raw capture, and corpus comparison as equivalent.
8. **Do not over-abstract before the second real mic.** `jasper/mics/README.md`
   still stands: no broad `MicProfile` Protocol from one mic. Add concrete
   fields to the current profile; extract interfaces when the second production
   mic lands.

## The profiles

`jasper/audio_profile_state.py` is the vocabulary. Selecting any of the first
five writes the full leg set; `custom` alone preserves the low-level booleans.

| Profile | Meaning |
|---|---|
| `auto` | Fresh-install default. A managed XVF resolves to the commissioned fixed `xvf_chip_aec` path, or discloses and falls back (rule 3); non-XVF mics may resolve to software AEC3 or direct capture. |
| `xvf_chip_aec` | The managed-XVF product path: fixed chip ASR beams, native outputd USB-IN reference, no double-AEC/raw/DTLN stacking. Requires supported hardware and commissioned alignment; without them it discloses rather than parks. |
| `xvf_chip_aec_testing` | Operator testing label. It sets the gate's `testing` arm: `auto_allowed=False`, so it never wins automatic selection, while `permits(testing_requested=True)` still arms an uncalibrated DAC and carries the detail as its disclosure. |
| `xvf_software_aec3` | The software profile. Selectable for non-XVF mics — and the leg set the reconciler's own disclosure fallback applies on a managed XVF whose chip path cannot arm. |
| `direct_mic` | Basic path with the AEC bridge disabled. Selectable for non-XVF mics, and the handover the disclosure fallback takes when no bridge can run (e.g. the 6-channel capture endpoint is not ready). |
| `custom` | The sole expert/lab/corpus escape; `JASPER_WAKE_LEG_*` booleans own the leg set directly. |

## Validation artifacts

Immutable timestamped JSON under `/var/lib/jasper/audio-validation/`, keyed by
mic/DAC/profile/status. Status surfaces load the newest matching schema-v1
artifact through `latest.json` only when that pointer is valid, fresh, and
matches the requested filters; otherwise they fall back to the timestamped
history. **The pointer is a convenience; the durable record is the timestamped
artifact.**

```json
{
  "schema_version": 1,
  "validated_at": "2026-06-01T16:00:00Z",
  "hardware": {"mic_id": "xvf3800", "dac_id": "apple_usb_c_dongle"},
  "profile": "xvf_chip_aec",
  "status": "warn",
  "checks": {
    "runtime_identity": {"status": "pass", "required": false},
    "runtime_profile": {"status": "pass"},
    "mic_detected": {"status": "pass"},
    "runtime_env": {"status": "pass"},
    "dac_reference": {"status": "pass"},
    "wake_legs": {"status": "pass"},
    "outputd_reference_health": {"status": "pass"},
    "bridge_counter_window": {"status": "pass"},
    "chip_profile_readback": {"status": "pass"},
    "chip_convergence": {"status": "not_observed"},
    "measured_drift_delay": {"status": "not_run"}
  },
  "recommendation": "run_drift_delay_validation"
}
```

`jasper-audio-validate` is the cheap producer: an on-demand `xvf_chip_aec`
readiness snapshot from safe runtime facts only (env/profile truth, service
state, outputd reference outputs, bridge counters, wake-leg state, journaled
drift-warning evidence, Pi/build identity). It plays no audio, opens no capture
loop, and writes no XVF settings.

```sh
jasper-audio-validate --stdout
jasper-audio-hw-validate --dry-run
sudo jasper-audio-hw-validate --duration-seconds 10 --stdout
sudo jasper-audio-hw-validate --long-window --stdout
sudo jasper-audio-hw-validate --profile hifiberry_dac8x_outputd_stability --long-window --stdout
```

`jasper-audio-hw-validate` is the explicit operator step. It never runs from
doctor, `/aec`, deploy, service startup, or the reconciler, and it refuses when
chip-AEC is not requested and active unless `--force`. It passively observes
outputd reference health and bridge counters across a bounded window, then
polls read-only XVF profile/convergence state **only after** runtime/reference
health passes. Bounds: `--duration-seconds` (default 10) is the observation
window, not a wall-clock cap — bounded read-only XVF subprocesses may add time;
windows above 120 s require `--allow-long` or `--long-window`; the long-window
preset is 30 minutes; `--dry-run`/`--report-only` writes nothing and skips the
sleep. It generates no audio, opens no capture loop, and calls no XVF write
path — never `SAVE_CONFIGURATION` or `REBOOT`.

`--profile hifiberry_dac8x_outputd_stability` is the narrower DAC8x
content-pipeline soak: fan-in/Camilla/outputd service state, outputd DAC
STATUS, and outputd xrun/clipping/progress counters across the window. It needs
no chip-AEC, bridge stats, XVF readback, wake legs, or active voice provider,
so a parked `jasper-voice` cannot turn an outputd stability result into a
chip-AEC failure.

**Reading the status honestly.** `measured_drift_delay` stays `not_run` until
an explicit operator-confirmed playback/capture probe exists, so a raw artifact
can read `status=warn` with `recommendation=run_drift_delay_validation` even
when runtime, outputd, bridge, and chip readback are all clean. Doctor resolves
that partial artifact rather than warning on it: `_chip_aec_passive_evidence_pair`
clears the warn when the artifact is `current`, its recommendation is
`run_drift_delay_validation`, every required passive check passed, and the
artifact's `dac_id` resolves to gate status `approved`. The qualification is a
**registry field**, not a hard-coded pair — doctor keeps no second pair table,
and any DAC the registry approves gets the same treatment. An uncodified DAC
stays a warning until drift/delay evidence exists. Passive
`AEC_AECCONVERGED=0` is `not_observed`, not failure — no far-end stimulus may
have been present; a flag that reaches `1` and returns to `0` in the same
window is `warn`.

## Validation gates

### Chip-AEC DAC gate

What qualifying a new DAC for **automatic** chip-AEC selection takes. A DAC
label alone never authorizes the chip path: registry approval, backed by
measured evidence, is what sets `auto_allowed`. Arming on an explicit request
is a separate, always-permitted thing that discloses.

Pass: the outputd chip-reference writer shows no sustained queue-full, xrun, or
write-failed events; ref→air→mic drift stays bounded over at least 30 minutes;
fixed delay is measurable and stable enough for the chip AEC tail; wake tests
show chip beams add recall without unacceptable false accepts.

Fail (managed XVF): the box keeps hearing. Per rule 3 it discloses
(`disclosed_stale` + reason + action) and runs the software AEC3 leg, or the
direct-mic handover where no bridge can run — the evidence trail lives in the
disclosure, which is what `/aec`, `/state`, and doctor read. Voice parks only
if the mic is absent/unsupported or an arm actually faults.

### Generic mic software-AEC gate

Before recommending a non-XVF mic. Pass: capture is stable at a supported
rate/channel shape; levels are usable without clipping or severe hardware AGC
pumping; AEC3 receives a valid reference; wake scores separate real wakes from
hard negatives for the chosen model/threshold; CPU/RAM fits the target Pi.
Fail: offer direct-mic/no-AEC only if the user accepts the limitation,
otherwise mark unsupported with clear remediation.

## Observability

`event=voice.input_policy` and `event=voice.input_policy.warning`
(`jasper/voice/daemon_main.py`) are what the platform emits today. Wake-corpus
sessions and clips carry an `audio_context` snapshot: production profile
classification, mic firmware/channel identity, selected leg details from
`jasper/wake_legs.py`, DAC/reference env, optional validation-artifact status,
and per-clip capture health. Wake-event parity with that shape is not built.

Doctor distinguishes: not configured · configured and healthy · configured but
unvalidated · configured but failed validation · configured but hardware absent
· **disclosed-stale** — a managed XVF running its fallback engine with the
reason and re-commission action attached (a running state, not a park) ·
parked, which now means only absent/unsupported hardware or a `fault` arm.

## Known gaps

- Mic capability facts are not rich enough to describe generic USB mics,
  hardware-AEC mics, and raw-only mics in one place.
- The decisive fixed-delay and long-window drift gate for a **new** DAC still
  needs an explicit playback/capture validation mode; today's hardware runner
  is passive evidence, not acoustic proof.
- Intent and observed runtime truth still span env files, systemd state, chip
  read-backs, outputd health, bridge logs, wake legs, and dashboard cards —
  `audio_profile_state.py` unifies the reporting, not the storage.
- Corpus/test modes can enable richer comparison profiles than production, and
  the vocabulary is not yet centralized enough to guarantee comparability.

## Non-goals

- No PipeWire dependency.
- No broad `MicProfile` Protocol before a second real mic forces one.
- No *automatic* chip-AEC selection without a validated XVF chip beam plan
  (`chip_available`) and a registry-approved DAC (`auto_allowed`) — an explicit
  request may still arm and disclose. No stacked software AEC/raw/DTLN under
  the chip-AEC profile.
- No DTLN-by-default on small Pis without measured value and resource budget.
- No production mode that depends on corpus-only legs.
- No persistent XVF chip writes during routine tuning or validation, and no
  volatile reset outside the explicit foreground commissioner.

---

Last verified: 2026-08-26 (triage pass — every owner row rechecked against the
named module or script; the six-profile vocabulary and `custom`-only escape
against `jasper/audio_profile_state.py`; the `/aec` applied-runtime keys
against `jasper/control/aec_endpoints.py`; the input contracts against
`jasper/voice/input_policy.py`; the hw-validate bounds (120 s cap, 1800 s
long-window preset, `hifiberry_dac8x_outputd_stability`) and the
`measured_drift_delay` / `AEC_AECCONVERGED` status semantics against
`jasper/audio_validation.py`; `supports_active_crossover_commissioning`
against `jasper/audio_hardware/dac.py`; the output-hardware artifact path
against `deploy/bin/jasper-audio-hardware-reconcile` and
`jasper/output_hardware.py`; `audio_context` against
`jasper/web/wake_corpus_setup.py`. Plan-stage vocabulary, the phased roadmap,
and six never-emitted `audio_profile.*`/`audio_validation.*` event names were
archived rather than carried as if current.

Corrections made this pass against the ADR-0101 disclose sweep, which the doc
still predated: chip-AEC-or-park became chip-AEC-or-disclose
(`disclose_stale_chip_aec` in `deploy/bin/jasper-aec-reconcile`, and
`audio_profile_state`'s park arm firing only when the alignment status is
outside `{ready, disclosed_stale}`); `xvf_software_aec3` and `direct_mic` are
no longer described as unselectable on a managed XVF — they are the leg sets
the disclosure fallback applies; `xvf_chip_aec_testing` is `auto_allowed=False`
yet still explicitly armable, not a blanket non-authority; and the doctor
passive-evidence clearance is keyed to registry gate status `approved`
(`_chip_aec_passive_evidence_pair`), not to a hard-coded XVF3800 + DAC8x pair.)
