# Handoff: pluggable-mic boundary + multi-channel wake fusion architecture

> This doc owns the *architecture* of the mic-swap boundary and the
> leg-count-agnostic wake-fusion layer: the interfaces, the seams, and the
> named decisions. Phase 0 (leg registry + `LegRuntime`), Phase 1.0–1.3a
> (condition taxonomy, per-fire telemetry, the `WakeFuser` seam, live-condition
> refresh), the chip-AEC producer/wake legs, and the profile-first input policy
> are merged and deployed. Remaining near-term work is empirical: capture fresh
> wake/false-accept telemetry to tune the active chip-AEC profile and per-leg
> thresholds. Phase 2 pluggable-mic / cheap-USB production support remains
> planned, sequenced after the XVF profile stabilizes so it does not split the
> next testing pass.
>
> The prior-art grounding, the staged Phase 0–5 plan, and the Phase 0 PR
> slicing live in
> [historical/mic-fusion-prior-art-and-staging-2026-05.md](historical/mic-fusion-prior-art-and-staging-2026-05.md).
> This is the architectural companion to the empirical mic-quality workstream
> in [HANDOFF-mic-quality-v2.md](HANDOFF-mic-quality-v2.md) (which owns "which
> engines/thresholds actually win on real data"). Schema lives in
> [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md); engine internals in
> [HANDOFF-aec.md](HANDOFF-aec.md); the parked cheap-USB path in
> [HANDOFF-usb-mic-wake.md](HANDOFF-usb-mic-wake.md); custom model training in
> [HANDOFF-wake-training-experiment.md](HANDOFF-wake-training-experiment.md).
> Code references use function/class names; re-confirm against live code at
> implementation time.

---

## TL;DR

1. **The mic-swap boundary is narrow because the AEC reference is
   already mic-independent.** The bridge sources its echo-cancellation
   reference from outputd's speaker monitor, not from the mic, and that
   is its only source. So the
   three software legs — **aec3, raw, dtln** — run against *any* mic
   that delivers one mono 16 kHz voice frame. "Always have those three
   lines no matter the mic" is nearly free today. A mic only needs a
   profile describing *how to get a mono voice frame (and optionally a
   raw frame) out of the hardware*.

2. **Keep the leg set DATA, not hardcoded string literals.** The
   `jasper/wake_legs.py` registry (`LegSpec` frozen dataclass + `by_*`
   lookups, modeled on the transit-provider pattern) is now the shipped
   source of truth for stable wake/corpus leg identity. "3 legs,"
   "chip-AEC beams," and future "replace AEC3 with chip-AEC" policies
   should stay declarations over that registry, not new scattered
   string literals.

3. **The fusion layer is leg-count-agnostic and stable across
   upgrades.** A `WakeFuser` consumes `{leg_name: score}` + a
   `ConditionContext` and returns one decision. Mic-swaps (more/fewer
   legs) and fusion upgrades (OR → per-condition thresholds →
   logistic regression → attention) move on *independent axes*
   through the same interface — which is internally **recall → verify**:
   the OR proposes a fire, a first-class verifier corroborates it before
   the turn opens (§2.6), because flat OR inflates false-accepts with
   every added leg (the prior-art-universal precision stage).

4. **Honor the existing `jasper/mics/` decision.** We do *not* build a
   `MicProfile` Protocol/ABC from one data point. We extend the
   existing `xvf3800.py` profile with capability fields, and extract
   the Protocol only when a second concrete mic lands (the trigger the
   `mics/README.md` already names).

5. **What's already shipped (don't re-pitch it):** the registry-backed
   detector fleet (baseline AEC3/raw/DTLN plus profile-selected chip beams), a
   lock-race+refractory OR-gate better than naive `any()`, and per-leg
   telemetry with per-leg WAVs and `analyze-three-leg.sh`. The real
   unbuilt delta is **per-leg + per-condition thresholds** (today: one
   global threshold, zero differential) and **multi-condition training
   augmentation** (the single highest-ROI accuracy lever per the
   research review).

6. **Staging:** Phase 0 leg registry → Phase 1 per-condition thresholds
   **+ verifier (recall→verify)**
   → Phase 2 capture-profile + cheap-USB capture → Phase 3 learned
   fusion (data-gated) → Phase 4 second mic / 4th arm (trigger-gated)
   → Phase 5 attention fusion (CPU-gated, probably never on a Pi 5 ≤4
   legs). Wake-model training augmentation is the top accuracy lever but
   is a **parallel track, not a phase** — it lives in
   [HANDOFF-wake-training-experiment.md](HANDOFF-wake-training-experiment.md).

7. **Session source ≠ wake legs.** The single stream fed to the LLM per
   turn is a *selection* over the profile's streams (pin → dynamic →
   AEC'd default; §2.7), distinct from the OR-fused wake legs, with a
   liveness heartbeat decoupled from both (§2.8). A 2026-05-31 prior-art
   sweep (in [the staging appendix](historical/mic-fusion-prior-art-and-staging-2026-05.md)) confirmed the direction is sound — every mechanism has
   shipped (Amazon, Home Assistant, the KWS literature); the
   *integration* is the novel, open-source part.

8. **Current priority (2026-05-31): validate chip-AEC before starting
   pluggable mic production work.** The two open streams are not equally
   urgent. Chip-AEC is now built and deployed, so the next valuable step
   is to collect real wake telemetry and decide whether the XVF hardware
   beams should become the recommended XVF mode. Generic USB mic support
   is strategically important for OSS adoption and BOM reduction, but it
   is a follow-up track; current USB evidence says it is useful corpus
   data, not yet a better production path.

---

## 1. The core insight: what is mic-dependent vs mic-independent

The reconnaissance found that almost nothing downstream of the raw
capture is actually coupled to the XVF3800:

| Component | Mic-dependent? | Why |
|---|---|---|
| AEC3 reference signal | **No** | From outputd's speaker monitor, not the mic, and from nothing else. Exists for any mic. |
| AEC3 / DTLN engines | **No** | Pure `process(mono_mic, ref) → bytes` on 16 kHz mono. Already reused for the experimental `usb_webrtc` corpus leg. |
| Voice-daemon consumption | **No** | `make_mic_capture(device, rate, channels)` handles UDP or PortAudio; polyphase-downsamples arbitrary rates. UMIK-2 is a documented working second device. |
| Config layer | **No** | `mic_device`, `mic_capture_rate`, `mic_capture_channels` already parameterize the mic; comments anticipate non-XVF mics. |
| Leg/fusion/telemetry | **No** (after Phase 0) | Keyed off leg *names*, not the mic. |
| **Bridge capture** (`_mic_thread`) | **Yes** | Module constants `MIC_DEVICE`/`MIC_CHANNELS`/`MIC_CHANNEL_INDEX`; hard-assumes 16 kHz-native, 6-ch, ASR-on-channel-1, no resample. |
| **Bash reconciler** | **Yes** | Hardcodes card `Array`, the `== "6"` channel literal, XVF mixer names. Cannot import Python. |
| **Mic "capability" model** | **Missing** | No `does_hardware_aec`, `native_rate`, or `needs_software_reference` field anywhere. |

**Consequence for the boundary:** the only things a new mic must
supply are (a) a way to open it and pull a mono voice frame at a
known rate/channel, and (b) optional metadata (does it do its own
hardware AEC? does it expose a raw channel?). The three software legs
come for free. That is the whole boundary.

---

## 2. The architecture

Three layers, two stable interfaces. ASCII:

```
   outputd speaker monitor (final electrical playback)
   UDP reference ──────────────────────────────┐  MIC-INDEPENDENT reference
                                                │
   ┌───────────────┐   mono voice (+ raw)       ▼
   │ CaptureProfile │ ─────────────▶ ┌──────────────────────────────────────┐
   │  (the mic)     │                │  Capture + Processing (the bridge)     │
   │  · device/cands│                │   raw  ───────────────────▶ leg "raw"  (:9877 tok off)
   │  · native rate │                │   AEC3(mono, ref) ────────▶ leg "aec3" (:9876 tok on)
   │  · voice ch idx│                │   DTLN(mono, ref) ────────▶ leg "dtln" (:9878 tok dtln)
   │  · raw ch idxs │                │   [chip_aec  IFF profile.does_hw_aec] ▶ leg "chip" (:98xx)
   │  · does_hw_aec │                └──────────────────────────────────────┘
   └───────────────┘                              │ N UDP legs (declared by topology)
        declares                                  ▼
                                  ┌───────────────────────────────────────┐
                                  │  WakeFuser  (LEG-COUNT-AGNOSTIC)        │
                                  │   {leg: score} + ConditionContext       │
                                  │   RECALL: per-cond OR-gate (S1→learned) │
                                  │   VERIFY: VAD + cross-leg corrob (§2.6)  │
                                  └───────────────────────────────────────┘
                                                  │ one decision + fired_legs CSV
                                                  ▼
                                            turn / session
```

### 2.1 `CaptureProfile` — the mic capability surface

Extend the existing `jasper/mics/xvf3800.py` (do **not** create a
parallel abstraction). Today it already holds, as loose constants and
a `FirmwareVariant` frozen dataclass: ALSA card name, capture channels,
`MIC_CHANNEL_INDEX` (the ASR beam), `raw_mic_indices`, mixer
invariants, firmware-blob tracking. Add the capability fields that are
missing:

- `native_rate: int` (XVF = 16000; UMIK-class = 48000)
- `does_hardware_aec: bool = False`
- `hardware_aec_channel_index: int | None = None` (where the chip's
  AEC output lands, if any)
- `needs_software_reference: bool = True` (false only for a mic whose
  hardware AEC fully replaces the software legs)

The bridge reads these from the profile object instead of its three
module-level constants. **No `base.py` Protocol yet** — per
`mics/README.md`, extract the shared interface by diffing two *real*
profiles when mic #2 lands (Phase 4), not from one data point.

`Config` holds a profile **key string** (`mic_profile: str`, mirroring
how `wake_model: str` resolves against the `wake_models.py` registry);
the `jasper/mics/` package holds the structured data.

### 2.2 `jasper/wake_legs.py` — the leg registry (single source of truth)

New module, modeled on `jasper/transit/__init__.py` + `base.py` (the
repo's most-documented, most-loved registry). It **subsumes the two
divergent leg vocabularies** that exist today (the daemon's 3-slot
`on/off/dtln` and `wake_ports.build_ports()`'s larger
`on/off/dtln/raw0/ref/usb_*/sweep` map) into one.

> **Shipped as Phase 0.1 (#366).** The 5-field `LegSpec` below is what
> actually landed in `jasper/wake_legs.py`. `telemetry_prefix` and
> `default_threshold_offset` were deferred to later phases — telemetry
> mapping lives in `voice_daemon.py`'s `_LEG_DB` dict, and the
> threshold-offset seam is `WakeFuser(offsets={...})` (§2.4b below).

```python
@dataclass(frozen=True)
class LegSpec:
    name: str            # human/code name: "aec3" | "raw" | "dtln" | "chip_aec" | "raw0" ...
    token: str           # FROZEN wire/DB token: "on" | "off" | "dtln" | "chip" | "raw0"
    udp_port: int        # 9876 | 9877 | 9878 | ...
    kind: LegKind        # SOFTWARE_AEC | RAW | NEURAL_AEC | HARDWARE_AEC | CORPUS
    wake_input: bool     # True = consumed by WakeFuser; False = corpus-only (raw0/ref/usb_*/sweep)
```

**Back-compat invariant (load-bearing).** The existing telemetry
corpus, `fired_legs` CSV, `trigger_kind` (`fire_aec_on` etc.), the
SQLite per-leg columns, and `analyze-three-leg.sh` all key off the
tokens `on`/`off`/`dtln`. The registry's `name` may be more
descriptive, but `token` and `udp_port` for the
three existing legs are **frozen** so the historical corpus and the
analysis tooling keep working. Renaming the wire/DB keys would orphan
the data — non-goal.

Lookup helpers mirror transit/wake-models: `by_name()`, `by_token()`,
`wake_legs()` (where `wake_input`), `all_ports()`. `wake_ports.py`
becomes a thin shim re-exporting from here (or is deleted once callers
migrate).

### 2.3 `LegRuntime` — the in-process collapse

Today `WakeLoop` carries paired per-leg attributes
(`_mic_off`/`_detector_off`/`_recent_score_off`/…/`_capture_ring_off`,
×3 legs) and **two near-duplicated loop bodies** (`_wake_secondary_loop`,
`_wake_tertiary_loop`) plus `if leg == "on"/elif "off"/elif "dtln"`
ladders in `_handle_wake_frame`. Replace with one dataclass held in an
ordered dict:

```python
@dataclass
class LegRuntime:
    spec: LegSpec
    mic: MicCapture | UdpMicCapture
    detector: WakeWordDetector
    capture_ring: deque
    recent_score: float = 0.0
    recent_score_at: float = 0.0
    shadow_vad: SileroVad | None = None   # session-time telemetry, raw/off leg only today
```

`WakeLoop` holds `self._legs: dict[str, LegRuntime]`. One generic
`_wake_leg_loop(leg_name)` replaces the two duplicated bodies; one
generic fire path replaces the ladders. The lock-race + shared
`_refractory_until` + `fired_legs` construction stay exactly as they
are — only the per-leg dispatch becomes a loop over the dict. Adding a
4th leg becomes: register a `LegSpec`, and the topology function
includes it. No new loop body, no new attribute.

> **Why independent detectors stay independent:** openWakeWord `Model`
> carries per-instance prediction-buffer smoothing state, so each leg
> must keep its own detector (today's design is correct — preserve it).

### 2.4 Leg topology — how 3 vs 4 legs is declared

A pure function turns a profile + config into the active leg set:

```python
def legs_for(profile: CaptureProfile, cfg: Config) -> tuple[LegSpec, ...]:
    legs = [AEC3, RAW]                       # universal: reference is mic-independent
    if cfg.wake_leg_dtln:    legs.append(DTLN)
    if profile.does_hardware_aec:            # ← the 4th arm, declared not branched
        legs.append(CHIP_AEC)
    return tuple(legs)
```

**Shipped form (Phase 0.3).** Before `CaptureProfile` exists (Phase 2),
the precursor `_configured_wake_legs(cfg)` in `voice_daemon.py` is the
real version of this function: it iterates `wake_input_legs()` and gates
each optional leg on its `cfg.mic_device_*` device string being non-empty
(the reconciler sets/clears those from the `JASPER_WAKE_LEG_*` booleans),
with the primary `on` leg present in every case but one: a speaker with no
microphone of its own plans **no legs at all** and listens only while a paired
remote's button is held (issue #2205). That is keyed on the AEC reconciler's
published `JASPER_LOCAL_MIC_PRESENT`, never on a device string — see
[HANDOFF-hotplug-resilience.md](HANDOFF-hotplug-resilience.md) Layer 1.
The `profile.does_hardware_aec`
branch and the `cfg.wake_leg_dtln` toggle shown above are the Phase-2
shape — neither exists yet. Two small token→vocabulary maps stay in their
consumers rather than on the frozen registry: `_LEG_DEVICE_ATTR`
(token→`cfg` device field) in `voice_daemon.py`, and `_TOGGLE_TO_TOKEN`
(operator `raw`↔`off`) in `control/server.py`.

**Geometry caveat (2026-06-19, jts5 Flex LINEAR-4).** The shipped
`chip_aec_150` / `chip_aec_210` leg names are the square-board fixed
beam experiment vocabulary. They are not portable proof that a linear
array should use those beam angles. Flex LINEAR-4 must be treated as a
separate capture profile/topology: flash the linear firmware
(`ua-io16-6ch-lin`, ALSA `L16K6Ch`) and start from
`xvf_software_aec3` plus raw-mic corpus legs. Only promote Flex chip
processed outputs into wake/AEC production after a labeled corpus shows
they beat or complement the raw/AEC3 legs.

This is the literal answer to *"design for 4 as the harder expected
path; swaps fall out easier."* Four legs is a longer dict. Replacing
AEC3 with chip-AEC is `legs = [CHIP_AEC, RAW, DTLN]` (drop AEC3 when
`needs_software_reference is False`). Reordering or toggling
already-registered legs touches neither the fuser, the telemetry spine,
nor the consumption layer. Introducing a genuinely *new* leg type costs
a bit more than the topology line: it also needs a `_LEG_DB`
telemetry-column entry in `voice_daemon.py` and the matching additive
`wake_events` columns (those columns are physical + irregular, so they
can't be data-driven away — see the staging appendix's PR-plan caveat).

### 2.5 `WakeFuser` — the stable, leg-count-agnostic interface

```python
class WakeFuser(Protocol):
    def decide(self, scores: dict[str, float], ctx: ConditionContext) -> FuseResult: ...
    #  scores: {leg_name: latest_score}    ctx: music flag, noise floor/SNR, ...
    #  FuseResult: fired: bool, winner: str | None, fired_legs: list[str]
```

Every fusion stage of the staging plan is a different `WakeFuser` implementation
behind this one interface, and `decide()` is internally a **recall →
verify** pipeline (§2.6): a recall stage (leg scores → per-condition
OR-gate) *proposes* a fire, and a verify stage *corroborates* it before
the turn opens. Today's lock-race OR-gate is the recall stage,
re-expressed to read per-leg thresholds from the registry; the verifier
is the next first-class stage (Phase 1.4). The interface never changes
as legs grow or either stage gets smarter — that orthogonality is the
design's whole payoff.

### 2.6 The verifier / corroboration stage (recall → verify)

OR-fusing N legs is a **recall** mechanism: more legs catch more real
wakes, but a flat OR is a *union of error sets* — every leg's
false-accepts pass straight through, so false-accepts rise
**monotonically with N**. This is not a tuning detail; it is the
structural reason every production wake stack pairs a high-recall first
stage with a **precision second stage**. Alexa runs on-device detect →
cloud second-stage verify ([Amazon Alexa: cloud-based wake-word
verification](https://developer.amazon.com/en-US/blogs/alexa/post/b136b3e7-0ba8-4589-aaf9-2a037fc4e9c9/cloud-based-wake-word-verification-improves-alexa-wake-word-accuracy-on-your-avs-product));
the general edge pattern is tiny-recall-model → larger-precision-model
([Picovoice wake-word guide](https://picovoice.ai/blog/complete-guide-to-wake-word/)),
and a published refinement stage cut false alarms **up to 7–8×**
([arXiv:2304.03416](https://arxiv.org/pdf/2304.03416)).

So the JTS wake pipeline is **recall → verify, with the verifier a
first-class, committed stage** — not a someday-inside-learned-fusion
afterthought (decided 2026-05-31). It runs after the OR proposes a
winner and decides whether to actually fire:

- **Where it earns its keep:** the raw and chip-direct legs are exactly
  where `tts_bleed` and `music_vocals` (our telemetry's own labeled FP
  classes) enter — so the union-FAR penalty lands hardest on the very
  legs that buy recall. The verifier is what lets us keep those legs
  *without* paying their false fires.
- **Cheap mechanisms that fit the repo (no cloud, Pi-budget):** a shared
  Silero-VAD veto (openWakeWord already carries one); a per-leg
  confidence floor; **cross-leg corroboration** (require ≥2 legs, or
  require the AEC-on leg to confirm during TTS to kill `tts_bleed`); or
  re-scoring the fired window on the session-source stream. All live
  *inside the `WakeFuser`* (§2.5) — the seam already merged in Phase
  1.2 — so the verifier grows the fuser; it never touches the leg loops.
- **Composes with, not competes with, learned fusion (Phase 3).** The
  logistic-regression fuser is a *smarter recall+verify in one model*;
  the heuristic verifier is what we run until that's data-justified, and
  the fallback if it underperforms.
- **Resilience — fails open, never closed.** The verifier may only ever
  *suppress a marginal* fire; it can never block a confident single-leg
  wake, and any verifier bug must fail toward firing on the wake path,
  never toward deafness (the no-silent-deafness rule, AGENTS.md).

### 2.7 Session source — the per-turn audio handed to the LLM

The legs answer *"did someone say the wake word?"* A distinct question
is *"once we're in a turn, which stream do we feed the speech-to-speech
LLM?"* These are **different jobs with different optima**: wake
detection is a ~1 s pattern match (broad OR for recall), while the
session needs sustained intelligibility across a multi-second command —
the best leg at the wake instant is not necessarily the best stream for
the seconds that follow. Home Assistant's 2026.6 dual-mic source states
the same split ("the more-processed channel for wake… the less-processed
for STT… whichever works best per stage",
[ESPHome voice_assistant](https://esphome.io/components/voice_assistant/));
we generalize their two fixed lanes to **N profile-declared streams**.

**Session source is a per-turn *selection* over the profile's streams,
via a precedence ladder:**

1. **Explicit user pin** — the operator names a stream (advanced
   override).
2. **Dynamic policy** — e.g. use the beam that fired the wake as a
   direction proxy (a beamformer's firing beam ≈ the talker's bearing).
   *This rung is our most novel and least-proven idea, and ships behind
   a measurement gate:* one commercial embedded stack (DSP Concepts)
   does the opposite — it *freezes* spatial adaptation at wake
   ([DSP Concepts AWE](https://documentation.dspconcepts.com/awe-designer/8.D.2.3/wake-word-engine-and-asr-integration-for-awe-core-)) —
   and Amazon's production selector keys on a signal-quality metric
   (SIR), not wake-likelihood
   ([Amazon SIR Beam Selector](https://www.amazon.science/publications/sir-beam-selector-for-amazon-echo-devices-audio-front-end)).
   So treat "firing-beam → session-beam" as a hypothesis to validate
   against the corpus, with an SNR/SIR-scored fallback. The XVF also
   exposes a true DOA over USB (`AEC_AZIMUTH_VALUES`,
   [respeaker host_control](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/blob/master/host_control/README.md)) —
   a cleaner direction signal than inferring it from which software leg
   crossed threshold.
3. **Profile default** — the profile's recommended (echo-cancelled)
   stream.

**Bounds + safety:**

- **Candidates = streams the active profile actually declares,** so the
  choice is self-validating (you cannot pin `chip_aec_150` on a mic with
  no chip) and the wizard shows only the options real for *that*
  hardware.
- **AEC'd is the default, not a gate.** A non-AEC stream is *allowed* as
  the session source but **warned** — it's low-risk today (half-duplex +
  aggressive music ducking), and the echo concern is mostly a *future
  barge-in* one. We keep it open (open-source flexibility) behind a clear
  inline caveat rather than locking it.
- **Lock per turn.** The source is chosen when wake fires and held for
  the whole turn. We *hard-select* one stream for the LLM, so a
  mid-utterance switch would be an audible seam the provider's
  endpointing must absorb — hence the lock. (Soft per-frame attention
  blending avoids the seam but isn't free on a Pi, and isn't how a
  single-stream LLM session is fed.) Amazon's beam-selection patent
  ships this exact "select on wake, hold for the utterance," with our
  rationale verbatim: without the lock, "an extraneous, sudden noise
  event" can swing capture away from the talker mid-utterance
  ([US9734822B1](https://patents.google.com/patent/US9734822B1/en) — JTS
  is convergent prior art; low risk for an OSS project, noted for
  awareness).
- **Barge-in gets a vote later.** Full-duplex barge-in needs a clean echo
  reference to work at all, so when it lands it may constrain or auto-pick
  an AEC'd stream regardless of the transcription pin. We leave the door
  open; we don't build it now.

The session-source plumbing rides the capture-profile work (Phase 2,
since it selects among profile-declared streams); the chip-AEC leg
promotion is its first real exercise. Its session-source decision is now
concrete: keep `:9876` as the session/heartbeat carrier and forward the
selected chip beam into it, rather than double-AEC the `on` leg. The two
beam scoring legs stay on `:9887` / `:9888`.

### 2.8 Liveness heartbeat — decoupled from both wake and session

A third concern historically fused onto the primary leg: the
capture-pipeline **liveness heartbeat** the watchdog reads. It must
track *"are frames flowing from the capture pipeline?"* — not the
identity of any one leg, and not the per-turn session source. Decoupling
it means a user's session-source pin (even a flaky stream) can never
take down wake detection or trip the watchdog, and a single leg stalling
surfaces as *that leg* rather than masquerading as a whole-pipeline
death.

This pairs with a concrete resilience finding from the prior-art sweep:
openWakeWord's most-reported bug is a **stale-buffer false fire after a
stream stalls and resumes**
([openWakeWord #141](https://github.com/dscripka/openWakeWord/discussions/141)) —
exactly our "mic disappears then returns" edge. So the heartbeat asserts
*every configured leg is receiving frames*, and a leg that reconnects
must `reset()` its detector buffer before scoring again. It sits on the
same supervisor/health-probe pattern as the shipped T5.2
`SystemSupervisor` — no new daemon, just the right probe.

---

## 3. What is already built (current state, so we don't re-pitch it)

| Capability | Status | Where |
|---|---|---|
| Registry-backed detector fleet (one OWW per configured production leg, same model) | **Shipped** | `WakeLoop` in `voice_daemon.py` + `jasper/wake_legs.py` |
| Fusion = lock-race + shared 0.2 s refractory + `fired_legs` (better than naive OR) | **Shipped** | `_handle_wake_frame` |
| Per-leg telemetry: peak score, peak offset, mic RMS, WAV-per-leg | **Shipped** | `wake_events.py` (`begin_event`, `_finalize_event_audio`) |
| Music context (proxy) + bridge DSP config snapshot per event | **Shipped** | 38-col schema |
| Corpus pull + audit + reset + `analyze-three-leg.sh` (incl. a threshold-tuning hint engine) | **Shipped** | `scripts/` |
| Operator visibility for active mic/topology | **Shipped** | `/wake/` mic status card, backed by `jasper-control` `/aec` |
| Mic-independent AEC reference | **Shipped** | outputd final-reference UDP, the bridge's only reference transport; converted by `aec_bridge._ReferenceFrameConverter` |
| Profile-first input policy: `/wake/` profile → reconciler → outputd reference fanout / AEC3 fallback / direct mic as appropriate | **Shipped** (`auto`, `xvf_chip_aec`, `xvf_chip_aec_testing`, `xvf_software_aec3`, `direct_mic`, `custom`) | `jasper/audio_profile_state.py`, `jasper/chip_aec_policy.py`, `deploy/bin/jasper-aec-reconcile`, `jasper/control/server.py` |
| Chip-AEC producer path: profile intent → outputd reference fanout → `aec-init` profile → bridge `:9876` repoint + `:9887`/`:9888` beams | **Shipped; `auto` arms it on a managed XVF whose commissioned alignment applies. When it cannot (no codified DAC timing, no beam plan, 2-channel firmware) the box runs software AEC3 and publishes `disclosed_stale` — it does not park, and no `xvf_chip_aec_testing` opt-in is needed to keep hearing (ADR-0101)** | `jasper/mics/xvf3800.py`, `jasper/cli/xvf_profile.py`, `jasper/chip_aec_policy.py`, `deploy/bin/jasper-aec-reconcile`, `jasper/cli/aec_init.py`, `jasper/cli/aec_bridge.py` |
| Cheap-USB capture (resample + AEC3 + DTLN) | **Prototype** (corpus-only legs `usb_*`) | `_usb_mic_thread` |
| Per-leg / per-condition thresholds | **Missing** (one global threshold) | — |
| Profile-derived leg policy | **Shipped for current XVF/direct profiles** | `jasper/audio_profile_state.py` + reconciler |
| Mic capability model / second profile | **Missing** | — |
| Automatic condition class (quiet/music/noise) + SNR | **Missing** (manual `label` + a same-chain music proxy) | — |

---

## 6. Telemetry & evaluation deltas

- **Condition metadata to add:** a derived `condition_class`
  (quiet/music/noise) + a noise-floor/SNR estimate, and write the
  existing-but-empty `music_renderer` (from `RendererClient`) to
  disambiguate music from our own TTS bleed. These feed both Phase 1
  thresholds and Phase 4 features.
- **N-leg review:** new leg columns land via the existing additive
  `_MIGRATION_COLUMNS` mechanism. `fired_legs` stays the canonical
  leg-count-agnostic spine, and `scripts/_analyze_three_leg.py`
  discovers the available production legs from the fetched schema
  rather than hardcoding only `on/off/dtln`.
- **Labeling stays SQLite-only.** No `/wake-review/` web UI (explicit
  prior decision). Extend `analyze-three-leg.sh`, don't build a new
  tool (testing-tooling.md rule).
- **Eval metric discipline (from the review):** FRR at a fixed FA/h,
  per-condition × per-distance breakdowns, DET curves; FA/h only
  measurable on hours of negative audio; split held-out by
  session/speaker, never within a session.

---

## 7. Open decisions (need a call before/at the relevant phase)

**Resolved 2026-05-31 (recorded here; detail in §2 and the staging appendix):** (a) the
wake pipeline is **recall → verify**, with the verifier a *first-class,
committed* stage rather than something deferred into learned fusion
(§2.6, Phase 1.4); (b) **session source** is a per-turn selection over
profile-declared streams via a pin → dynamic → default ladder,
AEC'd-by-default but user-overridable-with-warning, locked per turn
(§2.7); (c) **naming** borrows established vocabulary (`profile`,
`direct`/`processed`, `ConflictingDevices`, `Priority`) instead of
coining new terms. The items below remain open.

1. **N-leg telemetry shape — additive columns vs normalized child
   table.** *Recommendation: additive columns now* (matches the
   existing schema, `analyze-three-leg.sh`, and CSV export; fine for
   3–4 legs). **Trigger to normalize** into a `wake_event_legs` child
   table: if legs exceed ~5 or become truly dynamic per-mic. Normalizing
   is the "right" design but rewrites every analysis query — defer until
   the column count actually hurts. *(Phase 4 decision.)*

2. **Bash reconciler coupling.** `jasper-aec-reconcile` hardcodes the
   XVF card + `6`-channel literal + mixer names and can't import the
   Python registry. Options: (a) keep duplicating constants with
   "keep in sync" comments (current idiom, cheapest, latent-bug-prone);
   (b) extract capability *detection* into a small `jasper-mic-detect`
   Python CLI the bash calls (`--emit-env`), keeping bash for
   systemctl orchestration; (c) per-mic reconciler scripts.
   *Recommendation: (b), but only at Phase 4* — the reconciler is
   safety-critical (it parks voice when the mic is absent), so change it
   behind `test_aec_reconcile.py` when a second mic forces it, not
   speculatively. This is a "significant / needs judgment" change, not
   an inline fix.

3. **Leg `name` vocabulary** (bikeshed; tokens are frozen regardless).
   Proposed names: `aec3`, `chip_direct` (token `off`), `dtln`,
   `chip_aec`, `raw0`. Settle at Phase 0 implementation.

4. **Doc home / routing — resolved.** The plan is agreed (2026-05-31)
   and the doc is wired into the README doc-atlas and `doc-map.toml`
   (under `wake-and-wake-corpus`). No longer open.

---

## 8. Non-goals / out of scope

- **No AEC topology re-architecture.** The "architecture is fixed; swap
  the engine, not the topology" rule (HANDOFF-aec.md) stands — no
  PipeWire echo-cancel, no snd-aloop replacement, no custom firmware.
  This plan changes *leg orchestration and the mic boundary*, not the
  dsnoop→engine→UDP→voice topology.
- **No `MicProfile` Protocol from one data point** (Phase 4 trigger).
- **No `/wake-review/` web UI** (SQLite labeling is sufficient).
- **No speculative second-mic machinery** before the hardware exists.
- **No symmetric provider edits** — scope changes to the observed path.

---

## 9. Test & doc obligations + stale-doc notes

- **Tests a wake change owes:** `WakeLoop.__new__`-bypass logic tests
  (extend `test_voice_daemon_wake_triple_stream.py`), `WakeEventStore`-
  on-`tmp_path` + a migration test for any new column (extend
  `test_wake_events.py`), `UdpMicCapture`/`make_mic_capture` loopback
  tests for capture changes (extend `test_udp_mic_capture.py`),
  reconciler tests (`test_aec_reconcile.py`). voice_eval is **not** owed
  by a wake-fusion change (it tests tool-calling behavior, not wake).
- **Doc obligations:** extend, don't duplicate — schema deltas go in
  HANDOFF-wake-telemetry.md, empirical results in mic-quality-v2.md,
  this doc owns the *architecture*. Bump `Last verified:` on any doc
  re-verified while touching the subsystem.

---

Last verified: 2026-08-26 (triage pass — the chip-AEC producer row was
corrected: an unarmed chip path now runs software AEC3 and discloses rather
than needing an `xvf_chip_aec_testing` opt-in. Prior-art grounding, the staged
plan, and the Phase 0 PR slicing moved to `docs/historical/`. Chip-AEC beam
labels remain square-board vocabulary until a Flex corpus validates them.)
