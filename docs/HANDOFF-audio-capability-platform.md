# Handoff: audio hardware capability platform

**Part of the JTS extensibility model** — this doc owns the *Hardware
profiles* contract. The cross-cutting lens (the host-mediated-indirection
invariant, the five extension contracts, the decision tree) lives in
[extensibility.md](extensibility.md).

> **Status: living architecture plan, created 2026-06-01.** This doc
> owns the cross-cutting plan for turning JTS's current AEC/mic/DAC
> work from hand-tuned lab state into a hardware-capability platform:
> detect what audio hardware is present, measure what it can actually
> do, choose a safe profile, expose the truth in the UI/doctor/logs,
> collect usable telemetry, and degrade gracefully. It is deliberately
> above the individual empirical workstreams:
> [HANDOFF-aec.md](HANDOFF-aec.md) owns AEC engine/topology details,
> [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md) owns chip-AEC lab
> findings and DAC viability methodology,
> [HANDOFF-mic-fusion-architecture.md](HANDOFF-mic-fusion-architecture.md)
> owns the wake-leg/fusion architecture,
> [HANDOFF-wake-training-experiment.md](HANDOFF-wake-training-experiment.md)
> owns corpus/model-training methodology, and
> [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md) owns the
> wake-event schema.

---

## TL;DR

JTS should become an **audio hardware capability system**, not a set of
mic/DAC special cases. The system should be able to answer, from one
small set of canonical facts:

- What mic family is present?
- What streams can that mic produce?
- Which AEC modes are viable: hardware chip-AEC, WebRTC AEC3, DTLN,
  raw fallback, corpus-only comparison?
- What DAC/output path is active?
- Is that DAC compatible with chip-AEC's clock/reference requirements?
- Which wake legs should be armed, scored, or captured?
- What resource cost and reliability risk does each profile carry?
- What has been validated on this particular device, and when?

The near-term goal is not a giant abstraction. The near-term goal is a
small **capability contract** and **validation artifact** that the
existing surfaces can share: `jasper-audio-hardware-reconcile`,
`jasper-aec-reconcile`, `jasper-aec-init`, `jasper-aec-bridge`,
`jasper-outputd`, `/aec`, `/wake/`, `jasper-doctor`, wake telemetry,
and the wake-corpus recorder.

The product direction:

1. **Different DACs should be usable.** JTS should detect or validate
   whether a DAC can support chip-AEC. If yes, use chip-AEC where it
   wins. If no, fall back cleanly to software AEC3 without leaving the
   user in a half-enabled state. DAC role detection is event-driven:
   install and boot run one reconcile pass, and udev `controlC*`
   add/remove/change events trigger the same output-hardware reconciler
   for USB DAC changes. Apple USB-C DAC removal also has a USB-device
   helper because the disappearing ALSA control node may not always wake
   systemd on remove. I2S HATs generally appear through boot-time
   device-tree/ALSA enumeration, so boot/install reconciliation is the
   primary path for them.
2. **Different mics should be usable.** A mic with no hardware AEC
   should still get a principled software-AEC path. A future onboarding
   flow can ask the user to say the wake word under controlled
   conditions while JTS plays noise/music, then tune/choose profiles
   from measured evidence.
3. **Production, corpus, and onboarding must share the same profile
   vocabulary.** Lab-only toggles are fine, but they should compose
   through the same profile/capability registry as production so
   evidence collected in corpus mode applies to the real path.

---

## Current State

The foundation is partly built:

- `jasper/wake_legs.py` is the single source of truth for stable
  wake/corpus leg identity: tokens, UDP ports, kinds, and whether a
  leg is a production wake input.
- `jasper/mics/xvf3800.py` is the canonical mic-family profile for the
  Seeed ReSpeaker XVF3800: USB identity, ALSA card name, firmware
  variants, mixer invariants, channel indices, and helpers.
- `jasper-aec-reconcile` is the hardware/state policy layer. It maps
  user intent from `/var/lib/jasper/aec_mode.env` into runtime env
  for voice, bridge, init, and outputd.
- `jasper-aec-init` owns volatile XVF3800 profile writes and read-back
  verification. It must never persist chip writes with
  `SAVE_CONFIGURATION` / `REBOOT` during normal tuning.
- `jasper-aec-bridge` owns mic capture, WebRTC AEC3, optional DTLN,
  chip-AEC beam forwarding, UDP leg emission, and corpus-only streams.
- `jasper-outputd` owns final DAC playback and, in chip-AEC mode,
  fans the final speaker buffer to the XVF3800 USB-IN reference path.
- `/wake/`, `/aec`, `jasper-doctor`, wake telemetry, and the corpus UI
  expose pieces of this state.
- `jasper/audio_profile_state.py` is the first shared read-only
  classifier for intent vs observed runtime truth. It now feeds `/aec`,
  `/state.aec`, `/wake/` via the existing `/aec` proxy, and the
  `jasper-doctor` "Audio profile" check so those status surfaces report
  the same requested/active profile, session source, wake legs, and
  warnings.
- `jasper/audio_hardware/dac.py` is the static DAC profile registry.
  `jasper/output_hardware.py` is the output-side runtime classifier:
  `jasper-audio-hardware-reconcile` writes
  `/run/jasper-output-hardware/output_hardware.json` with the observed output
  profile/card facts plus the resolved `usb_data_role` (board topology,
  configured registered I²S overlays, desired/configured/active role,
  strict gadget and active management-transport availability, reason, and
  reboot requirement). `/state`,
  `/sound/output-topology`, and `jasper-doctor` consume that artifact
  instead of each reconstructing DAC semantics from raw env/card names.
- `jasper/chip_aec_policy.py` is the shared chip-AEC gate: static DAC
  qualification from the DAC registry, optional live outputd
  `aec_clock` evidence, and explicit testing-profile intent collapse into
  one `approved` / `testing` / `needs_calibration` answer. The AEC
  reconciler writes that answer into `/etc/jasper/jasper.env`; `/aec`,
  `/state`, `jasper-doctor`, and audio-validation checks consume it.
- `jasper/voice/input_policy.py` is the first provider-facing consumer
  of the audio-profile boundary. It converts the applied mic/AEC runtime
  config into an input-audio contract (`xvf_chip_aec`,
  `xvf_software_aec3`, `custom_udp`, `direct_mic`) and resolves provider
  preprocessing such as OpenAI `noise_reduction=auto` from that contract.
  This keeps provider adapters from hard-coding mic/DAC special cases:
  they receive a resolved provider policy, not raw hardware guesses.
- `jasper/audio_validation.py` owns schema-v1 audio-validation
  artifacts at `/var/lib/jasper/audio-validation/`. Artifacts are
  immutable timestamped JSON files keyed by mic/DAC/profile/status;
  `latest.json` is only the cheap status-surface pointer.
  `jasper-audio-validate` writes the first bounded producer artifact:
  an on-demand `xvf_chip_aec` readiness snapshot built only from safe
  runtime facts (env/profile truth, service state, outputd reference
  outputs, bridge counters, wake-leg state, recent drift-warning
  evidence if journaled, plus Pi/build identity for attribution). It
  writes both the timestamped durable record and the latest pointer. It
  does not play audio, open capture loops, or write XVF chip settings,
  so clean runtime readiness is still
  `status=warn` with `recommendation=run_hardware_validation` until
  measured drift/delay evidence exists.
  `jasper-audio-hw-validate` is the explicit operator-controlled next
  step. For the default `xvf_chip_aec` profile, it passively observes
  outputd reference health and bridge counters across a bounded window,
  then polls read-only XVF chip profile/convergence state only after
  runtime/reference health passes. For the DAC8x/outputd stability
  profile (`--profile hifiberry_dac8x_outputd_stability`), it validates
  the outputd/content pipeline independently of chip-AEC and
  `jasper-voice`: required checks are fan-in/Camilla/outputd service
  state, outputd DAC STATUS, and outputd xrun/clipping/progress counters
  across the bounded window. It writes through the same schema-v1 helper
  and still does not generate playback, open capture loops, or persist
  chip settings. On the known-good XVF3800 + HiFiBerry DAC8x
  `xvf_chip_aec` path, `jasper-doctor` treats a current clean passive
  hardware artifact as operator-OK even though the raw artifact remains
  partial. Unknown or new DAC paths still need the explicit acoustic
  drift/delay gate before chip-AEC should be recommended.
- The pure-data DAC registry now distinguishes broad active-output transport
  support from automatic crossover-commissioning launch support. Only the base
  DAC8x declares `supports_active_crossover_commissioning`; the owning Active
  service also requires a two-way preset. This is product-scope authority, not
  a substitute for live hardware-validation evidence.
- `/wake-corpus/` has the first additive reuse hook: new session and
  clip metadata write an `audio_context` snapshot with production
  profile classification, mic firmware/channel identity, selected leg
  details from `jasper/wake_legs.py`, DAC/reference env, optional
  validation-artifact status, and existing per-clip capture health.

The gaps are exactly where future hardware support would hurt:

- Mic capability facts are not yet rich enough to describe generic
  USB mics, hardware-AEC mics, or "raw only" mics in one place.
- DAC capability/validation facts have only the first durable home: the
  validation artifact records the configured DAC/outputd identity,
  chip-reference runtime state, passive outputd/bridge health windows,
  and read-only chip convergence/readback where available. The decisive
  fixed-delay and long-window drift gate for a new DAC still needs an
  explicit playback/capture validation mode.
- "Intent" and "observed runtime truth" are still spread across env
  files, systemd state, chip read-backs, outputd health, bridge logs,
  wake legs, and dashboard cards.
- Corpus/test modes can enable richer comparison profiles than
  production, but the profile vocabulary is not yet centralized enough
  to guarantee they remain comparable.
- There is now a single advisory readiness report for "this Pi is in
  production chip-AEC runtime state" vs "chip-AEC requested but runtime
  evidence is incomplete." It is not a full acoustic validation gate
  because long-window drift and fixed-delay stability are still
  operator-controlled measurements.

---

## Design Principles

1. **Capability beats special case.** Prefer "this mic exposes a
   hardware-AEC ASR beam and a raw channel" over "if XVF, do X" in new
   code. Keep chip-specific commands inside the chip profile/init
   layer.
2. **Observed truth beats intent.** A toggle says what the user wants;
   the runtime state says what actually happened. UI and doctor should
   show both when they differ. `/aec.raw_intent` is the saved request;
   `/aec.mode`, `/aec.bridge_role`, `/aec.software_aec3`, `/aec.legs`,
   `/aec.audio_profile.active`, and `/aec.mic_settings` are the
   reconciler-applied runtime truth. When no concrete profile is active
   yet, `bridge_role=pending` is the honest answer; a live bridge
   process alone is not proof that WebRTC AEC3 is running on the
   detected mic.
3. **Validation artifacts are product state.** DAC drift checks,
   chip-profile read-backs, outputd reference health, and mic level
   sanity should persist as small timestamped JSON artifacts, not just
   scroll by in journald.
4. **Fallback is a first-class path.** Unsupported or unvalidated
   chip-AEC should deliberately land on WebRTC AEC3 or direct mic
   capture, not a silent half-mode.
5. **Profiles are declarations, not imperative scripts.** A profile
   should say "use chip-AEC 150/210, session carrier on :9876, no raw
   or DTLN" and the reconciler/producers make it true.
6. **Cost is part of capability.** A 1 GB Pi cannot treat DTLN, AEC3
   sweeps, chip-AEC, raw capture, and corpus comparison as equivalent.
   Profiles need resource labels and test-only flags.
7. **Do not over-abstract before the second real mic.** The existing
   `jasper/mics/README.md` decision still stands: do not invent a broad
   `MicProfile` Protocol from one mic. Add concrete capability fields
   to the current profile and extract common interfaces only when the
   second production mic lands.

---

## Target Vocabulary

These names are conceptual first; implementation can be smaller.

### `MicCapability`

Facts about a mic family or detected mic instance:

- stable family id and display name
- detection identifiers: ALSA card, VID/PID, channel count, firmware
  variant, optional serial
- stream inventory: ASR beam, conference beam, raw mic channels,
  hardware-AEC beams, reference tap if any
- native sample rate/channel shape
- whether it needs software reference for AEC3
- whether it has hardware AEC and what profile writes enable it
- known resource implications
- safe fallback mode if preferred mode fails

For XVF3800 this starts as additions around `jasper/mics/xvf3800.py`,
not a new generic base class.

### `DacCapability`

Facts about the output device relevant to AEC:

- output device id and display name
- sample rate / format used by outputd
- whether the output path can receive the exact final speaker buffer
- whether the DAC is expected to be frequency-coherent with the
  XVF3800 USB-IN reference domain
- chip-AEC viability: `unknown`, `validated`, `failed`, or
  `not_applicable`
- validation artifact path and timestamp
- fallback recommendation

For USB DACs, descriptors and `/proc/asound/*/stream0` are hints. The
decisive gate is still measured drift/delay stability, as documented in
[CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md).

### `AudioProfile`

A declarative runtime profile:

| Profile | Purpose |
|---|---|
| `auto` | Fresh-install default. Resolves to `xvf_chip_aec` when 6-channel XVF3800 chip-AEC is available; otherwise falls back to `xvf_software_aec3` / direct mic as the reconciler can support. |
| `xvf_chip_aec` | Recommended XVF3800 hardware-AEC path: chip ASR beams, outputd USB-IN reference, no double-AEC/raw/DTLN stacking. |
| `xvf_chip_aec_testing` | Explicit operator validation path for running chip-AEC on an unapproved DAC. Same physical mic/reference path as `xvf_chip_aec`, but surfaces gate status as `testing` and never affects `auto` approval. Validation artifacts still use the physical `xvf_chip_aec` profile key. |
| `xvf_software_aec3` | XVF fallback path: raw-ish/ASR mic into WebRTC AEC3 with raw wake fallback, DTLN off by default. |
| `direct_mic` | Basic custom-hardware path with the AEC bridge disabled. |
| `custom` | Expert/corpus mode. Low-level `JASPER_WAKE_LEG_*` booleans own the leg set directly. |
| `generic_usb_software_aec3` | Generic mic path: mono mic + outputd/reference into WebRTC AEC3. |
| `corpus_comparison` | Test-only profile that records many legs from the same utterance. |
| `dac_validation` | Test-only profile for drift/delay/reference health measurement. |

Each profile should declare:

- required mic capability
- required DAC/reference capability
- produced wake legs
- session/heartbeat carrier
- corpus-only legs
- mutually exclusive legs
- required services and env vars
- expected resource cost
- validation requirements before "recommended" status
- provider-facing input contract: raw vs processed, echo-cancelled,
  denoised, beamformed, gain-controlled, and the provenance for any
  provider preprocessing decision

### `ValidationArtifact`

Small JSON written under `/var/lib/jasper/audio-validation/` as
immutable timestamped files. Status surfaces load the newest matching
schema-v1 artifact through `latest.json` only when that pointer is
valid, fresh, and matches the requested profile/hardware filters;
otherwise they fall back to the timestamped history. The pointer is a
convenience; the durable record is the timestamped artifact.

```json
{
  "schema_version": 1,
  "validated_at": "2026-06-01T16:00:00Z",
  "hardware": {
    "mic_id": "xvf3800",
    "dac_id": "apple_usb_c_dongle"
  },
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

This artifact is cheap to read from `/aec`, corpus metadata, and
`jasper-doctor`. Missing/stale validation stays advisory unless the
requested profile depends on chip-AEC.

Operator commands:

```sh
jasper-audio-validate --stdout
jasper-audio-hw-validate --dry-run
sudo jasper-audio-hw-validate --duration-seconds 10 --stdout
sudo jasper-audio-hw-validate --long-window --stdout
sudo jasper-audio-hw-validate --profile hifiberry_dac8x_outputd_stability --long-window --stdout
```

`jasper-audio-hw-validate` is explicit and bounded. It never runs from
doctor, `/aec`, deploy, service startup, or the reconciler. The default
10-second `--duration-seconds` value is the passive outputd/bridge
observation window, not a hard total wall-clock cap: bounded read-only
XVF profile/convergence subprocesses may add time. The command samples
already-running outputd/bridge state, reads schema-v1 runtime facts, and
polls XVF read-only convergence/profile state only after chip-AEC
runtime/reference health is good. It refuses when chip-AEC is not
requested and active unless `--force` is passed.
The `hifiberry_dac8x_outputd_stability` profile is the narrower DAC8x
content-pipeline soak: it does not require chip-AEC, bridge stats, XVF
readback, wake legs, or an active voice provider, so a parked
`jasper-voice` cannot turn an outputd stability result into a chip-AEC
failure.
`--dry-run`/`--report-only` writes nothing and skips the observation
sleep. Observation windows above 120 seconds require `--allow-long` or
`--long-window`; the long-window preset is 30 minutes. The command does
not generate audio, does not open capture loops, and does not call
`SAVE_CONFIGURATION`, `REBOOT`, or any other XVF write path.

The current hardware runner is passive evidence, not complete acoustic
proof. `measured_drift_delay` remains `not_run` until an explicit
operator-confirmed playback/capture probe is added, so the raw artifact
may stay `status=warn` with
`recommendation=run_drift_delay_validation` even when runtime, outputd,
bridge, and chip readback checks are clean. That distinction is
intentional: doctor reports the known XVF3800 + HiFiBerry DAC8x
`xvf_chip_aec` path as OK when the required passive hardware checks pass,
while any unknown/new DAC remains a warning until drift/delay evidence
exists.
Passive `AEC_AECCONVERGED=0` is reported as `not_observed`, not failure,
because no explicit far-end stimulus may have been present.
If the flag reaches `1` and later returns to `0` in the same window,
the convergence check is `warn`; stable convergence means no later
valid poll contradicted the first converged sample.

---

## Existing Surfaces To Unify

When implementation starts, audit these first and resist adding a new
parallel truth:

| Surface | What it owns today | Risk |
|---|---|---|
| `jasper/mics/xvf3800.py` | XVF identity, firmware, mixer, channel constants | Needs richer capabilities, but should not become a fake generic interface. |
| `jasper/wake_legs.py` | Stable leg identity and ports | Good spine; future profile code should reference this instead of strings. |
| `deploy/bin/jasper-aec-reconcile` | Intent → env/service runtime policy | Bash duplicates mic/chip facts and owns mutual exclusion. |
| `deploy/bin/jasper-audio-hardware-reconcile` | Output DAC role convergence and `/run/jasper-output-hardware/output_hardware.json` | Correct output-side policy owner; should keep profile data minimal and publish state instead of making each UI/doctor surface probe hardware. |
| `jasper/cli/aec_init.py` | Volatile XVF profile application/readback | Correct place for chip writes; should emit profile/readback state. |
| `jasper/cli/aec_bridge.py` | Leg producers, mic/ref capture, corpus streams | High risk for scattered profile flags. |
| `rust/jasper-outputd` | Final output/DAC timing + chip reference fanout | Correct place for DAC/reference health counters. |
| `/aec` + `/wake/` | Operator status and toggles | Should show capability + validation state, not just config booleans. |
| `jasper-doctor` | Install/runtime diagnostics | Should understand "requested profile cannot be validated" vs service failure. |
| `wake_events` | Per-wake evidence | Should persist active profile/hardware/validation state for later analysis. |
| `wake-corpus` | Same-utterance capture | Should use declared profiles so test labels match production semantics. |

### Phase 0 Inventory Snapshot — 2026-06-01

Initial read-only audit against current `origin/main` found these
specific duplication seams. This is the starting checklist for the next
code pass:

| Fact / policy | Current places | Desired owner |
|---|---|---|
| XVF3800 identity, firmware, mixer names, capture channels | `jasper/mics/xvf3800.py`; duplicated in bash comments/logic inside `deploy/bin/jasper-aec-reconcile`; inspected again in `jasper-doctor` | `jasper/mics/xvf3800.py` remains canonical; bash can keep minimal copies but should emit/report against profile-derived names where practical. |
| Stable wake leg tokens and ports | `jasper/wake_legs.py`; bridge `OUT_PORT*` constants; corpus tests; wake telemetry columns | `jasper/wake_legs.py` is canonical; producers/tests should keep deriving from or cross-checking it. |
| User intent for AEC/raw/DTLN/chip-AEC | `/var/lib/jasper/aec_mode.env`; `/wake/` writers; `jasper-aec-reconcile`; doctor reads | Keep the env file as intent; add read-only profile state that distinguishes intent from applied runtime. |
| Chip-AEC mutual exclusion with raw/DTLN | `deploy/bin/jasper-aec-reconcile::write_leg_env`; mirrored in docs/tests | Declarative audio profile should own this; reconciler applies it. |
| Chip-AEC volatile XVF profile writes | `jasper/cli/aec_init.py` corpus and production chip profiles; tests in `tests/test_aec_init.py` | `aec_init.py` remains owner; state helper should consume "applied/read-back verified" result once persisted/exposed. |
| Outputd chip-reference PCM / UDP reference env | `deploy/bin/jasper-aec-reconcile`; `rust/jasper-outputd/src/config.rs`; `/wake-corpus/` env writer | Audio profile should declare desired reference outputs; outputd config remains execution detail. |
| Outputd reference health counters | `rust/jasper-outputd/src/main.rs` logs; `rust/jasper-outputd/src/state.rs` state JSON | Outputd remains owner; state helper/doctor should classify health rather than scraping logs first. |
| `/aec` / `/wake/` displayed mic state | `jasper-control` server helpers; tests in `tests/test_control_aec_state.py` and `tests/test_web_wake_setup.py` | First read-only consumer of `audio_profile_state`; UI should stop reconstructing profile semantics independently. |
| `/state.aec` audio-profile snapshot | Additive mirror of `/aec` inside `jasper-control`'s one-shot state payload | Same `audio_profile_state` payload as `/aec`, for dashboard/doctor/CLI consumers that need one request. |
| Doctor wake/AEC checks | `jasper/cli/doctor.py` functions around AEC mode, wake legs, bridge, XVF firmware, DTLN | Doctor should consume the same read-only profile state and add validation-artifact checks. |
| Corpus comparison profile | `jasper/web/wake_corpus_setup.py`; bridge corpus flags; tests in `tests/test_wake_corpus_setup.py` | Corpus profile should be a test-only `AudioProfile` superset, not a separate flag vocabulary. |

The high-value first code change is therefore **not** to add another
toggle. It is to add one import-cheap, side-effect-free state builder
that reports:

- requested intent (`auto`, raw, DTLN, chip-AEC);
- detected mic and firmware;
- selected/applied profile (`xvf_software_aec3`, `xvf_chip_aec`,
  `xvf_chip_aec_testing`,
  `direct_mic`, `degraded`, etc.);
- active wake legs expected vs observed;
- outputd reference outputs and health;
- latest validation artifact status, if any.

That helper can be consumed by `/aec` first, then doctor, then `/wake/`
and `/state`.

---

## Phased Plan

### Phase 0 — Inventory And Contract

Goal: make the current truth explicit without changing runtime behavior.

- Add this doc and route it through README/docs-impact.
- Inventory the duplicated hardware truths in reconciler, bridge, init,
  doctor, `/aec`, `/wake/`, and corpus mode.
- Decide the first minimal fields for mic capability, DAC capability,
  audio profile, and validation artifacts.
- Name the invariants that must not move:
  - wake leg tokens/ports remain stable;
  - chip writes remain volatile and read-back-verified;
  - chip-AEC and raw/DTLN remain mutually exclusive on XVF;
  - WebRTC AEC3 remains the fallback path.

### Phase 1 — Read-Only Runtime State

Goal: expose a single "what is true right now" view.

- Add a small runtime state builder that reads:
  - configured intent from `/var/lib/jasper/aec_mode.env`;
  - detected mic/firmware;
  - active wake legs;
  - outputd chip-ref health;
  - latest validation artifact if present;
  - relevant service state.
- Feed that state to `/aec`, `/wake/`, `/state`, and doctor.
- Keep it read-only; do not yet let it mutate hardware.

### Phase 2 — Validation Artifacts

Goal: turn lab checks into durable product state.

- Add a DAC validation command/report for chip-AEC viability:
  - play controlled source through outputd fanout;
  - measure ref→air→mic drift over short and long windows;
  - measure fixed delay and delay stability;
  - watch outputd chip-ref canaries.
- Add a mic readiness report:
  - input present;
  - channel/rate sane;
  - RMS not silent;
  - clipping/AGC suspicion;
  - software AEC3 baseline if applicable.
- Store timestamped JSON artifacts under `/var/lib/jasper/`.
- Doctor warns on missing/stale/failed validation only when the user has
  requested a profile that depends on it.

### Phase 3 — Declarative Profiles

Goal: make mode transitions predictable.

- Introduce a small profile declaration table.
- Reconciler maps user intent + capability + validation into one
  chosen profile.
- Bridge/init/outputd consume profile-derived env, not independent
  ad-hoc flags.
- `/wake/` shows profile intent, chosen profile, fallbacks, and why.

### Phase 4 — Corpus And Onboarding Reuse Profiles

Goal: make evidence collection match production.

- Wake-corpus profile selection should be a superset of production
  profiles, not a separate vocabulary.
- Every corpus clip records the active profile, hardware fingerprint,
  validation status, and per-leg health. First additive implementation:
  `jasper/web/wake_corpus_setup.py` now writes `audio_context` and
  `selected_legs` while keeping old sessions loadable.
- Future guided onboarding can reuse the same primitives:
  - say wake word several times;
  - JTS plays quiet/medium/loud noise or music;
  - score candidate profiles;
  - recommend chip-AEC, software AEC3, or fallback.

### Phase 5 — Second Mic / Second DAC Expansion

Goal: only now extract broader interfaces.

- Add the second real mic profile.
- Compare it against XVF and extract only the common surface that both
  need.
- Add DAC-specific validation notes only where measurement proves they
  differ.
- Keep unsupported hardware graceful: "works as direct mic",
  "software AEC only", or "chip-AEC not validated."

---

## Validation Gates

### Chip-AEC DAC Gate

Use before recommending chip-AEC with a new or otherwise unproven DAC.
The known XVF3800 + HiFiBerry DAC8x path already has passive production
evidence and does not need to block normal operation on this optional
advanced probe.

Pass criteria:

- outputd chip-reference writer has no sustained queue-full, xrun, or
  write-failed events;
- ref→air→mic drift remains bounded over at least 30 minutes;
- fixed delay is measurable and stable enough for the chip AEC tail;
- wake tests show chip beams add recall without unacceptable false
  accepts.

Fail behavior:

- keep or return to `xvf_software_aec3`;
- mark chip-AEC unavailable for `auto` and surface the fallback reason;
- preserve the validation artifact explaining why.

### Generic Mic Software-AEC Gate

Use before recommending a non-XVF mic.

Pass criteria:

- capture is stable at supported rate/channel shape;
- levels are usable without clipping or severe hardware AGC pumping;
- software AEC3 receives a valid reference;
- wake scores separate real wakes from hard negatives well enough for
  the chosen model/threshold;
- CPU/RAM cost fits the target Pi.

Fail behavior:

- offer direct mic/no-AEC mode only if user accepts the limitation;
- otherwise mark unsupported with clear remediation.

---

## Observability Requirements

Every profile transition should emit stable structured logs. Only
`voice.input_policy` and `voice.input_policy.warning` are shipped
today (`jasper/voice/daemon_main.py`); the rest are plan-stage names
with no code emitting them yet:

- `event=audio_profile.intent` (not yet built)
- `event=audio_profile.selected` (not yet built)
- `event=audio_profile.fallback` (not yet built)
- `event=audio_validation.loaded` (not yet built)
- `event=audio_validation.stale` (not yet built)
- `event=audio_profile.apply_failed` (not yet built)
- `event=voice.input_policy` (shipped)
- `event=voice.input_policy.warning` (shipped)

Wake events and corpus metadata should include, once fields exist.
Corpus metadata has the first version of this shape as `audio_context`;
wake-event parity remains future work:

- active audio profile
- detected mic family / firmware
- output DAC id plus observed-vs-active output hardware state
- validation artifact id or timestamp
- per-leg audio path completeness
- per-leg health counters where available

Doctor should distinguish:

- not configured
- configured and healthy
- configured but unvalidated
- configured but failed validation
- configured but hardware absent
- degraded fallback active

---

## Dashboard / Onboarding Direction

The UI should eventually make hidden state visible without making the
user learn the architecture:

- **Microphone card:** detected mic, firmware/channel count, active
  capture profile, wake legs, and whether hardware AEC is available.
- **Output/DAC card:** detected DAC, sample rate, chip-AEC validation
  state, last validation time.
- **Mode card:** production / corpus test / validation / degraded
  fallback.
- **Action buttons:** validate DAC, validate mic, enter corpus mode,
  return to production mode.

Do not build a heavy onboarding wizard until Phases 1-3 exist. Once
they do, the guided flow can be simple: say the wake word several times
while JTS plays controlled noise/music, then pick the profile that wins
against clear metrics.

---

## Immediate Next Sprint

1. **Add optional acoustic drift/delay validation for new hardware.**
   The `hifiberry_dac8x_outputd_stability` profile now isolates outputd
   xrun/clipping/progress health, and the known DAC8x chip-AEC path can
   run on clean passive evidence. Future work should keep playback
   explicit and add bounded short/long drift plus delay-stability
   evidence when the operator approves hardware-coupled probes, mainly
   for new DAC qualification or deep tuning.
2. **Promote richer DAC identity.** The readiness snapshot records the
   configured outputd PCM today; a future DAC capability pass should
   persist stable USB/ALSA descriptor facts without trusting browser or
   hotplug labels blindly.
3. **Teach profile selection to consume validation.** Missing/stale
   chip-AEC validation remains advisory today. The current `auto` profile
   uses live hardware readiness; a later selector should fold validation
   freshness into recommendation text and fallback warnings.
4. **Extend wake-event parity.** Corpus metadata now carries validation
   status; wake-event rows should eventually include the same artifact id
   or timestamp for production telemetry analysis.

---

## Non-Goals

- No PipeWire dependency.
- No broad `MicProfile` Protocol before a second real mic forces one.
- No chip-AEC on hardware that lacks a validated XVF chip beam plan and
  reference path, and no stacked software AEC/raw/DTLN under the
  chip-AEC profile.
- No DTLN-by-default on small Pis without measured value and resource
  budget.
- No production mode that depends on corpus-only legs.
- No persistent XVF chip writes during routine tuning or validation.

---

Last verified: 2026-07-15 (DAC8x-only automatic crossover-commissioning
capability and its two-way product gate checked hardware-free; output hardware
`usb_data_role` boundary rechecked;
prior 2026-06-26 `/aec` applied-runtime status contract rechecked
against `jasper/audio_profile_state.py`, `jasper/control/aec_endpoints.py`,
and `tests/test_control_aec_state.py`. Prior pass 2026-06-25: chip-AEC gate
vocabulary rechecked against `jasper/chip_aec_policy.py`, `/aec`, and
`jasper-aec-reconcile`).
