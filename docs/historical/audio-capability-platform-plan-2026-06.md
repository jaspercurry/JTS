# Historical: the audio hardware capability platform plan (2026-06-01)

Archived 2026-08-26 from `docs/HANDOFF-audio-capability-platform.md`, which
was created 2026-06-01 as a "living architecture plan" and has since been
right-sized to the shipped contract. This file keeps the roadmap that produced
it: the product framing, the conceptual vocabulary that was never built as
written, the Phase 0 duplication audit, the phased plan, and the UI direction.

**None of this is current truth.** What shipped is in
`docs/HANDOFF-audio-capability-platform.md`; the managed-XVF policy is
[ADR-0175](../adr/0175-a-managed-xvf-is-chip-aec-or-parked.md).

## The product framing

JTS should become an **audio hardware capability system**, not a set of
mic/DAC special cases — able to answer, from one small set of canonical facts:
what mic family is present; what streams it can produce; which AEC modes are
viable; what DAC/output path is active; whether that DAC is compatible with
chip-AEC's clock/reference requirements; which wake legs should be armed,
scored, or captured; what resource cost and reliability risk each profile
carries; and what has been validated on this device, and when.

The three product directions were: different DACs should be usable (detect and
commission chip-AEC viability); different mics should be usable (a principled
software-AEC path for mics with no hardware AEC, plus a guided onboarding flow
that has the user say the wake word under controlled conditions); and
production, corpus, and onboarding should share one profile vocabulary so
evidence collected in corpus mode applies to the real path.

## The conceptual vocabulary

Named as concepts first, with implementation expected to be smaller.

### `MicCapability`

Facts about a mic family or detected instance: stable family id and display
name; detection identifiers (ALSA card, VID/PID, channel count, firmware
variant, optional serial); stream inventory (ASR beam, conference beam, raw
channels, hardware-AEC beams, reference tap); native rate/channel shape;
whether it needs a software reference for AEC3; whether it has hardware AEC and
what profile writes enable it; known resource implications; and the safe
disposition if the preferred mode fails.

For XVF3800 this was to start as additions around `jasper/mics/xvf3800.py`, not
a new generic base class.

### `DacCapability`

Output-device facts relevant to AEC: device id and display name; rate/format
used by outputd; whether the path can receive the exact final speaker buffer;
whether it is expected to be frequency-coherent with the XVF3800 USB-IN
reference domain; chip-AEC viability as `unknown` / `validated` / `failed` /
`not_applicable`; validation artifact path and timestamp; and failure
disposition. USB descriptors and `/proc/asound/*/stream0` are hints; the
decisive gate was always measured drift/delay stability.

### Profiles that were named but not built

Alongside the six real profiles, the plan's table listed three that do not
exist in `jasper/audio_profile_state.py`:

| Profile | Intended purpose |
|---|---|
| `generic_usb_software_aec3` | Generic mic path: mono mic + outputd/reference into WebRTC AEC3. |
| `corpus_comparison` | Test-only profile recording many legs from the same utterance. |
| `dac_validation` | Test-only profile for drift/delay/reference health measurement. |

Each profile was to declare required mic capability, required DAC/reference
capability, produced wake legs, session/heartbeat carrier, corpus-only legs,
mutually exclusive legs, required services and env vars, expected resource
cost, validation requirements before "recommended" status, and the
provider-facing input contract.

### Observability names that were never emitted

`event=audio_profile.intent`, `audio_profile.selected`,
`audio_profile.fallback`, `audio_profile.apply_failed`,
`audio_validation.loaded`, `audio_validation.stale`. Only
`voice.input_policy` and `voice.input_policy.warning` were ever shipped
(`jasper/voice/daemon_main.py`). The plan also wanted wake-event rows to carry
active profile, mic family/firmware, output DAC id, validation artifact id,
and per-leg completeness/health; corpus metadata got that shape as
`audio_context`, wake-event parity did not.

## Phase 0 inventory snapshot — 2026-06-01

A read-only audit against `origin/main` at the time found these duplication
seams. It was the starting checklist for the code passes that followed.

| Fact / policy | Places it lived | Desired owner |
|---|---|---|
| XVF3800 identity, firmware, mixer names, capture channels | `jasper/mics/xvf3800.py`; duplicated in bash inside `deploy/bin/jasper-aec-reconcile`; inspected again in `jasper-doctor` | `xvf3800.py` canonical; bash keeps minimal copies but reports against profile-derived names. |
| Stable wake leg tokens and ports | `jasper/wake_legs.py`; bridge `OUT_PORT*` constants; corpus tests; wake telemetry columns | `wake_legs.py` canonical; producers derive from or cross-check it. |
| User intent for AEC/raw/DTLN/chip-AEC | `/var/lib/jasper/aec_mode.env`; `/wake/` writers; `jasper-aec-reconcile`; doctor reads | Env file stays intent; add read-only profile state distinguishing intent from applied runtime. |
| Chip-AEC mutual exclusion with raw/DTLN | `jasper-aec-reconcile::write_leg_env`; mirrored in docs/tests | Declarative profile owns it; reconciler applies it. |
| Chip-AEC volatile XVF profile writes | `jasper/cli/aec_init.py`; `tests/test_aec_init.py` | `aec_init.py` owner; state helper consumes the applied/read-back result. |
| Outputd chip-reference PCM / UDP reference env | `jasper-aec-reconcile`; `rust/jasper-outputd/src/config.rs`; `/wake-corpus/` env writer | Profile declares desired reference outputs; outputd config is execution detail. |
| Outputd reference health counters | outputd `main.rs` logs; `state.rs` state JSON | Outputd owner; doctor classifies health instead of scraping logs. |
| `/aec` / `/wake/` displayed mic state | `jasper-control` server helpers; `tests/test_control_aec_state.py`, `tests/test_web_wake_setup.py` | First read-only consumer of `audio_profile_state`. |
| `/state.aec` audio-profile snapshot | Additive mirror of `/aec` in the one-shot state payload | Same `audio_profile_state` payload as `/aec`. |
| Doctor wake/AEC checks | `jasper/cli/doctor.py` AEC/leg/bridge/firmware/DTLN functions | Consume the same read-only profile state, add validation-artifact checks. |
| Corpus comparison profile | `jasper/web/wake_corpus_setup.py`; bridge corpus flags; `tests/test_wake_corpus_*.py` | Corpus profile as a test-only `AudioProfile` superset, not a separate flag vocabulary. |

The conclusion — that the high-value first change was one import-cheap,
side-effect-free state builder rather than another toggle — is what became
`jasper/audio_profile_state.py`.

Surfaces the plan flagged as risks to audit before adding parallel truth:
`jasper/cli/aec_bridge.py` (scattered profile flags), `deploy/bin/jasper-aec-reconcile`
(bash duplicating mic/chip facts and owning mutual exclusion), `/aec` + `/wake/`
(showing config booleans rather than capability + validation state), and
`jasper-doctor` (needing to tell "requested profile cannot be validated" from
service failure).

## The phased plan

- **Phase 0 — inventory and contract.** Make current truth explicit without
  changing runtime behavior; name the invariants that must not move (leg
  tokens/ports stable; chip writes volatile and read-back-verified; chip-AEC
  and raw/DTLN mutually exclusive on XVF; AEC3 a non-XVF or explicit-`custom`
  path).
- **Phase 1 — read-only runtime state.** One "what is true right now" builder
  reading intent, detected mic/firmware, active legs, outputd chip-ref health,
  latest validation artifact, and service state; fed to `/aec`, `/wake/`,
  `/state`, and doctor.
- **Phase 2 — validation artifacts.** A DAC validation command (play a
  controlled source through outputd fanout; measure ref→air→mic drift over
  short and long windows; measure fixed delay and its stability; watch outputd
  chip-ref canaries) and a mic readiness report (present, sane channel/rate,
  not silent, clipping/AGC suspicion, AEC3 baseline). Doctor warns on
  missing/stale/failed validation only when the requested profile depends on it.
- **Phase 3 — declarative profiles.** A small profile declaration table; the
  reconciler maps intent + capability + validation to one chosen profile;
  bridge/init/outputd consume profile-derived env.
- **Phase 4 — corpus and onboarding reuse.** Corpus profile selection as a
  superset of production profiles; every clip records profile, hardware
  fingerprint, validation status, per-leg health. Guided onboarding reuses the
  same primitives: say the wake word several times while JTS plays
  quiet/medium/loud noise, score candidate profiles, recommend.
- **Phase 5 — second mic / second DAC.** Only then extract broader interfaces,
  and only the surface both real mics need.

## Dashboard / onboarding direction

The UI should eventually make hidden state visible without making the user
learn the architecture: a **microphone card** (detected mic, firmware/channel
count, active capture profile, wake legs, hardware-AEC availability); an
**output/DAC card** (detected DAC, sample rate, chip-AEC validation state, last
validation time); a **mode card** (production / corpus test / validation /
parked managed XVF / degraded non-XVF fallback); and **action buttons** to
validate DAC, validate mic, enter corpus mode, return to production. No heavy
onboarding wizard before Phases 1-3 exist.

## The "immediate next sprint" as of the last plan revision

1. Add optional acoustic drift/delay validation for new hardware, keeping
   playback explicit and bounded, mainly for new DAC qualification.
2. Promote richer DAC identity — persist stable USB/ALSA descriptor facts
   rather than trusting browser or hotplug labels.
3. Keep commissioning evidence authoritative; enrich recommendation text for
   new hardware but never add a testing/software/direct bypass.
4. Extend wake-event parity so production telemetry carries the same
   validation artifact id or timestamp corpus metadata already does.
