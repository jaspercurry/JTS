# Handoff: pluggable-mic boundary + multi-channel wake fusion architecture

> **Status: living draft — design + execution plan, updated as phases
> land (first written 2026-05-29; prior-art sweep folded in 2026-05-31).
> Phase 0 (leg registry + `LegRuntime`, #366/#369/#381) and Phase
> 1.0–1.3a (condition taxonomy, per-fire telemetry, the `WakeFuser`
> seam, live-condition refresh — #385/#390) are merged. Remaining on the
> wake-precision phase: 1.3b (corpus-tuned offsets) and the now
> first-class 1.4 verifier (§2.6). Phases 2–5 are planned. Not a record
> of shipped state — verify against code.** This
> doc owns the *architecture* of the mic-swap boundary and the
> leg-count-agnostic wake-fusion layer: the interfaces, the staging,
> and the named decisions. It is the architectural companion to the
> empirical mic-quality workstream in
> [HANDOFF-mic-quality-v2.md](HANDOFF-mic-quality-v2.md) (which owns
> "which engines/thresholds actually win on real data"). Schema lives
> in [HANDOFF-wake-telemetry.md](HANDOFF-wake-telemetry.md); engine
> internals in [HANDOFF-aec.md](HANDOFF-aec.md); the parked cheap-USB
> path in [HANDOFF-usb-mic-wake.md](HANDOFF-usb-mic-wake.md); custom
> model training in
> [HANDOFF-wake-training-experiment.md](HANDOFF-wake-training-experiment.md).
> Code references use function/class names; re-confirm against live
> code at implementation time (a few daemon docstrings are known
> stale — see §9).

---

## TL;DR

1. **The mic-swap boundary is narrow because the AEC reference is
   already mic-independent.** The bridge sources its echo-cancellation
   reference from the playback fan-in (`jasper_ref` →
   `jasper_capture` → `hw:Loopback,1,7`), not from the mic. So the
   three software legs — **aec3, raw, dtln** — run against *any* mic
   that delivers one mono 16 kHz voice frame. "Always have those three
   lines no matter the mic" is nearly free today. A mic only needs a
   profile describing *how to get a mono voice frame (and optionally a
   raw frame) out of the hardware*.

2. **Make the leg set DATA, not hardcoded string literals.** Today
   `on/off/dtln` is duplicated across ~10 surfaces. A single
   `jasper/wake_legs.py` registry (`LegSpec` frozen dataclass + `by_*`
   lookups, modeled on the transit-provider pattern) makes "3 legs,"
   "4 legs," and "replace AEC3 with chip-AEC" all *declarations*. This
   is the keystone refactor; it is justified by present pain with the
   one mic we have and does not require a second mic.

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

5. **What's already shipped (don't re-pitch it):** the 3-leg detector
   fleet, a lock-race+refractory OR-gate better than naive `any()`,
   and 38-column per-leg telemetry with per-leg WAVs and
   `analyze-three-leg.sh`. The real unbuilt delta is **per-leg +
   per-condition thresholds** (today: one global threshold, zero
   differential) and **multi-condition training augmentation** (the
   single highest-ROI accuracy lever per the research review).

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
   sweep (§4.2) confirmed the direction is sound — every mechanism has
   shipped (Amazon, Home Assistant, the KWS literature); the
   *integration* is the novel, open-source part.

---

## 1. The core insight: what is mic-dependent vs mic-independent

The reconnaissance found that almost nothing downstream of the raw
capture is actually coupled to the XVF3800:

| Component | Mic-dependent? | Why |
|---|---|---|
| AEC3 reference signal | **No** | From the snd-aloop playback tap, not the mic (`_ref_thread` reads `jasper_ref`). Exists for any mic. |
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
   playback fan-in (music being played)
   hw:Loopback,1,7 ─▶ jasper_ref ──────────────┐  MIC-INDEPENDENT reference
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

```python
@dataclass(frozen=True)
class LegSpec:
    name: str            # human/code name: "aec3" | "raw" | "dtln" | "chip_aec" | "raw0" ...
    token: str           # FROZEN wire/DB token: "on" | "off" | "dtln" | "chip" | "raw0"
    udp_port: int        # 9876 | 9877 | 9878 | ...
    kind: LegKind        # SOFTWARE_AEC | RAW | NEURAL_AEC | HARDWARE_AEC | CORPUS
    wake_input: bool     # True = consumed by WakeFuser; False = corpus-only (raw0/ref/usb_*/sweep)
    telemetry_prefix: str # column stem, e.g. "aec_on" / "aec_off" / "dtln_aec"
    default_threshold_offset: float = 0.0  # added to base threshold for this leg
```

**Back-compat invariant (load-bearing).** The existing telemetry
corpus, `fired_legs` CSV, `trigger_kind` (`fire_aec_on` etc.), the
SQLite per-leg columns, and `analyze-three-leg.sh` all key off the
tokens `on`/`off`/`dtln`. The registry's `name` may be more
descriptive, but `token`, `udp_port`, and `telemetry_prefix` for the
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
with the primary `on` leg always present. The `profile.does_hardware_aec`
branch and the `cfg.wake_leg_dtln` toggle shown above are the Phase-2
shape — neither exists yet. Two small token→vocabulary maps stay in their
consumers rather than on the frozen registry: `_LEG_DEVICE_ATTR`
(token→`cfg` device field) in `voice_daemon.py`, and `_TOGGLE_TO_TOKEN`
(operator `raw`↔`off`) in `control/server.py`.

This is the literal answer to *"design for 4 as the harder expected
path; swaps fall out easier."* Four legs is a longer dict. Replacing
AEC3 with chip-AEC is `legs = [CHIP_AEC, RAW, DTLN]` (drop AEC3 when
`needs_software_reference is False`). Reordering or toggling
already-registered legs touches neither the fuser, the telemetry spine,
nor the consumption layer. Introducing a genuinely *new* leg type costs
a bit more than the topology line: it also needs a `_LEG_DB`
telemetry-column entry in `voice_daemon.py` and the matching additive
`wake_events` columns (those columns are physical + irregular, so they
can't be data-driven away — see §10's PR-plan caveat).

### 2.5 `WakeFuser` — the stable, leg-count-agnostic interface

```python
class WakeFuser(Protocol):
    def decide(self, scores: dict[str, float], ctx: ConditionContext) -> FuseResult: ...
    #  scores: {leg_name: latest_score}    ctx: music flag, noise floor/SNR, ...
    #  FuseResult: fired: bool, winner: str | None, fired_legs: list[str]
```

Every fusion stage (§5) is a different `WakeFuser` implementation
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
promotion is its first real exercise — its session-source decision (keep
`:9876` as the session/heartbeat carrier and forward the chip beam into
it, rather than double-AEC the `on` leg) is scoped in the chip-AEC
promotion plan.

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
| 3-leg detector fleet (one OWW per leg, same model) | **Shipped** | `WakeLoop` in `voice_daemon.py` |
| Fusion = lock-race + shared 0.2 s refractory + `fired_legs` (better than naive OR) | **Shipped** | `_handle_wake_frame` |
| Per-leg telemetry: peak score, peak offset, mic RMS, WAV-per-leg | **Shipped** | `wake_events.py` (`begin_event`, `_finalize_event_audio`) |
| Music context (proxy) + bridge DSP config snapshot per event | **Shipped** | 38-col schema |
| Corpus pull + audit + reset + `analyze-three-leg.sh` (incl. a threshold-tuning hint engine) | **Shipped** | `scripts/` |
| Mic-independent AEC reference | **Shipped** | `_ref_thread` / asoundrc |
| Cheap-USB capture (resample + AEC3 + DTLN) | **Prototype** (corpus-only legs `usb_*`) | `_usb_mic_thread` |
| Per-leg / per-condition thresholds | **Missing** (one global threshold) | — |
| Data-driven leg set | **Missing** (hardcoded ×10 surfaces) | — |
| Mic capability model / second profile | **Missing** | — |
| Automatic condition class (quiet/music/noise) + SNR | **Missing** (manual `label` + a same-chain music proxy) | — |

---

## 4. Prior-art grounding

Two reviews inform this design: an earlier engagement (§4.1) and a
focused 2026-05-31 web sweep (§4.2) that validated the direction and
surfaced the refinements now folded into §2 and §5.

### 4.1 The earlier research review

**Adopt as-is (it matches the codebase or is straightforwardly right):**

- *Always keep the raw channel in the fusion; never use a denoised
  channel alone.* Already honored (the `off`/raw leg is always OR-ed
  in). Keep this invariant when the fuser gets smarter.
- *One multi-condition (MCT) model across all legs until ~1000+ real
  utterances; specialist ensembles need far more data.* Already true
  (same model on all legs) and aligned with the
  `HANDOFF-wake-training-experiment.md` custom-model effort.
- *The highest-ROI lever is multi-condition training augmentation
  (Amazon playback-interference recipe, ~30–45% relative FRR
  reduction, zero runtime cost) — cheaper than any fusion cleverness.*
  Agree — and it's the biggest single lever. But it's a **parallel
  track**, not a phase here: your `/wake-corpus/` tool collects the
  data and training happens off-box, both owned by
  [HANDOFF-wake-training-experiment.md](HANDOFF-wake-training-experiment.md).
  This architecture only *supports* it (see the §5 callout).
- *Per-leg thresholds (Yandex "ensemble" grid-search) beat a single
  model.* This is the concrete Phase 1 win — we have **zero**
  per-leg differential today.
- *Decide DTLN's fate with data.* Already the plan in
  mic-quality-v2 ("~a week of data, then `analyze-three-leg.sh`").

**Correct / reframe:**

- ❌ *"A USB mic with no reference can't run AEC3."* False here — the
  reference is the playback tap, not the mic (§1). A cheap USB mic
  keeps all three software legs.
- 🔄 *"Stage 0: instrument first."* Largely done. The real instrumentation
  gap is **condition labeling** (quiet/music/noise) and **SNR/noise
  floor**, plus disambiguating music from our own TTS (the
  `music_active` flag is a same-chain proxy; `music_renderer` is in the
  schema but currently unwritten — wire it from `RendererClient`).
- 🔄 *"Stage 1: ship heuristic score fusion."* The OR-gate exists and is
  better than naive `any()`. The unbuilt part is *per-condition
  thresholds*, not the fusion plumbing.
- 🔄 *Feature-level attention is "the accuracy endgame."* Reframe: its
  more relevant property here is that it **decouples CPU cost from leg
  count** (run the embedding backbone once on a fused feature stream
  instead of once per leg). That matters specifically because you want
  legs to grow to 4+. It's still a big lift (fork openWakeWord to
  expose embeddings) and likely unnecessary on a Pi 5 at ≤4 legs —
  hence Phase 5, CPU-gated.

**Heed these caveats (the review states them; they bind us):**

- The cited FRR/FA numbers come from 4–7-mic *arrays* + huge
  proprietary corpora. Ours are *processing channels* off one mic with
  100–500 utterances. Treat their figures as directional, never as
  targets.
- Small-corpus overfitting is the dominant risk in learned fusion
  (Phase 4): strong L2, ≤~10 features, k-fold CV, and a genuinely
  held-out *fresh capture session* are mandatory; report intervals,
  not point estimates.

### 4.2 The 2026-05-31 web prior-art sweep — what it validated and changed

A five-angle web review (full agent report archived in the session)
checked each pillar against shipped systems and the literature.
**Verdict: the direction is well-grounded, not speculative — every
mechanism has shipped somewhere; the novel part is the *integration* and
the open-source packaging.** No OSS project was found assembling a
data-declared capture profile + N-leg OR-fusion + per-turn session-source
ladder + decoupled liveness in one place.

**Validated (prior art directly supports):**

- **OR-fusion is openWakeWord's intended use** — `predict()` returns a
  per-model score dict; caller-side gating is by design
  ([openWakeWord](https://github.com/dscripka/openWakeWord)).
  Multichannel KWS literally **max-pools per-beam scores** (= our
  OR-gate) and beats single-channel in noise
  ([arXiv:2507.15558](https://arxiv.org/pdf/2507.15558)). Per-leg
  thresholds are standard.
- **Keeping a raw channel alongside processed legs** mirrors
  multichannel-KWS's "omni channel as undistorted reference" — we
  already do this (`off`/raw always OR-ed).
- **Wake-vs-session split is near-verbatim prior art** — HA 2026.6
  dual-mic source + VOCAL's two-channel wake/STT design
  ([ESPHome](https://esphome.io/components/voice_assistant/),
  [VOCAL](https://vocal.com/echo-cancellation/aec-barge-in/)).
- **Per-turn session-source select + lock + wake-informed selection** is
  shipped by Amazon (US9734822B1); beam-selection-for-ASR is the
  production norm.
- **DTLN downstream of hardware AEC is explicitly sanctioned** by its
  author — so our parallel DTLN *leg* is not harmful double-AEC
  ([PiDTLN](https://github.com/SaneBow/PiDTLN/blob/main/README.md)).
- The chip's **own routing is data-driven** (`AUDIO_MGR_OP_*`
  (category, source) pairs) and the **single-mode constraint is
  documented upstream** ("both focused beams must be fixed; not possible
  to fix only one",
  [XMOS datasheet](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/datasheet/03_audio_pipeline.html))
  — so modeling it as a profile *mode* is correct, not a workaround.

**Changed / added to the plan (the refinements, now folded into §2/§5):**

1. **Recall → verify is now first-class** (§2.6, Phase 1.4) — flat OR's
   union-FAR is *the* reason every production stack adds a precision
   stage; up to 7–8× FA reduction cited
   ([arXiv:2304.03416](https://arxiv.org/pdf/2304.03416)).
2. **1 GB budget: ~1 detector ≈ 1 leg, not "20 models free."** The
   famous openWakeWord figure assumes a *shared* mel+embedding backbone;
   our legs run on *different* streams, so the frontend is **not**
   shared. Budget per-leg; if RAM bites, share the frontend across legs
   on the *same* stream (the documented cheaper path,
   [arXiv:2507.15558](https://arxiv.org/pdf/2507.15558)). This sharpens
   §3's CPU note and is the single most important budget caveat.
3. **Two-level profile structure (mode ⊃ streams).** PipeWire/ALSA-UCM
   separate a mutually-exclusive device **profile/mode** from the
   **streams** readable within it
   ([PipeWire](https://docs.pipewire.org/page_pulseaudio.html)). The XVF
   single-mode quirk lives in the *mode* layer; raw/processed/beam tags
   live in the *stream* layer (§2.1 grows this split as it gains
   capability fields).
4. **The dynamic session-source rung is gated on measurement** (§2.7) —
   DSP Concepts does the opposite, Amazon uses SIR not wake-likelihood,
   and the chip exposes real DOA.
5. **Double-AEC tripwire as a profile invariant:** a stream already
   hardware-AEC'd must never be designated as input to a host
   software-AEC stage. Our design is currently safe (parallel legs, not
   stacked); bake the invariant so we can't *re-introduce* the hazard
   ([MS Teams AEC thread](https://techcommunity.microsoft.com/t5/microsoft-teams/acoustic-echo-cancellation-aec-for-teams-rooms-integration/td-p/1364592)).
6. **Resilience: `reset()` on leg reconnect** + heartbeat asserts every
   leg is fed (§2.8) — openWakeWord's stale-buffer false fire maps to
   our mic-vanish/return edge.
7. **Single-process leg topology is validated:** Wyoming users running
   detectors in separate processes hit mic-device contention
   ([wyoming-satellite #275](https://github.com/rhasspy/wyoming-satellite/issues/275))
   — our one-process, one-capture-pipeline design (§2.3) avoids it.

**Naming adopted (borrow, don't coin):** **profile** for the device mode
(PipeWire/ALSA-UCM); **`direct`/`processed`** stream tags +
**`directionality`/`orientation`**
([Android `MicrophoneInfo`](https://developer.android.com/reference/android/media/MicrophoneInfo));
**`ConflictingDevices`** for mutually-exclusive modes + **`Priority`**
for "recommended session source"
([ALSA UCM](https://www.alsa-project.org/alsa-doc/alsa-lib/group__ucm__conf.html));
Wyoming's **`installed`/`attribution`/`models[]`** (already echoed in
`wake_models.py`). Keep **"leg"** and **"session source"** (no
established term) — but never call a leg a "model" (collides with
openWakeWord's per-keyword model).

**Diagnose-before-encoding flags (do NOT bake as fact):**

- **Verify the "XVF fixed 150°/210°, single-mode" claim against the
  *pinned* `_6chl` firmware before encoding it as profile data.** Public
  docs are ambiguous (they also describe concurrent multi-beam +
  auto-select + AEC, and *dynamic* DOA azimuths). Our on-hardware
  observation is the authority — but confirm it's a property of the
  specific firmware variant we flash, and say so in the profile comment.
  Both fixed beams come as a *pair*, and 2↔6-ch is a *firmware flash*,
  not a runtime toggle — the profile must encode the loaded mux layout.
- **Thin claims flagged, not settled:** the Amazon SIR 46%/39% figures
  (abstract-level — verify before quoting), HA's exact per-stage
  channel-selection logic (product behavior, not a published spec), and
  the precise XVF simultaneity limit (verify on firmware). The
  cross-vendor generality of "single-mode" is *our inference* (an
  XVF-class observation, not a law). Novelty is stated as "no OSS
  equivalent found," not "first ever."

---

## 5. Staged execution plan

Each phase lists its **gate** (what unlocks it) and **verify** (the
runtime signal that says it's done — per the repo's close-the-loop
rule). Phases 0–2 are validatable on current or cheaply-bought
hardware; Phase 3 is data-gated, Phase 4 is trigger-gated, Phase 5 is
CPU-gated.

> **Parallel track (NOT a phase): wake-model training augmentation.**
> The single highest-ROI accuracy lever per the research review —
> re-train one multi-condition model on JTS-pipeline audio with
> playback-interference + RIR + music/noise augmentation (~30–45%
> relative FRR reduction, zero runtime cost). It is **not a numbered
> phase here**: data collection is your `/wake-corpus/` tool and
> training happens off-box, both owned by
> [HANDOFF-wake-training-experiment.md](HANDOFF-wake-training-experiment.md).
> This architecture only *supports* it — Phase 0's leg registry makes
> "add the trained model as another detector arm" a one-line
> declaration (the mic-quality-v2 "engines × models = N detectors"
> vision), and Phase 1's `condition_class` / SNR metadata makes the
> corpus you collect more useful for training. Run it whenever you have
> enough data; it gates nothing here and nothing here gates it.

### Phase 0 — Leg registry + `LegRuntime` refactor  *(keystone; do first)*
- **Gate:** none — justified by present pain with the one mic.
- **Build:** `jasper/wake_legs.py` (`LegSpec` + registry + `by_*`);
  refactor `WakeLoop` to `self._legs: dict[str, LegRuntime]` with one
  generic loop + one generic fire path; migrate the Python leg-name
  consumers (`control/server.py` `/aec/leg` + `/state`,
  `web/wake_setup.py`, `aec_bridge.py` output stats,
  `wake_ports.py` → shim) to import the registry. Preserve every wire/DB
  token (back-compat invariant in §2.2).
- **Behavior change:** none (pure refactor).
- **Verify:** full hardware-free suite green, especially extended
  `test_voice_daemon_wake_triple_stream.py` (winner attribution,
  `fired_legs`, per-leg telemetry kwargs unchanged); deploy and confirm
  `analyze-three-leg.sh` output is byte-comparable on the same corpus;
  `/state` `legs` block unchanged.

### Phase 1 — Wake precision: per-condition recall + verifier  *(the real "Stage 1" delta)*
- **Gate:** Phase 0.
- **Landed, all behavior-preserving:** **1.0** the condition-taxonomy SSOT
  (`jasper/wake_conditions.py`); **1.1a** the `condition_class` column + the
  `music_renderer` `_MIGRATION_COLUMNS` backfill (the to-do below — done);
  **1.1b** the runtime estimator (`jasper/wake_condition_context.py`
  `classify_condition`) recording `condition_class` per fire — all merged in
  #385; **1.2** the thin `effective_threshold(leg, condition)` decision point
  (`jasper/wake_fusion.py` `WakeFuser`, wired into both threshold compares in
  `_handle_wake_frame`; empty offsets ⇒ today's OR-gate); **1.3a** the
  live-condition refresh (`WakeLoop._maybe_refresh_condition`, ~1 Hz off the
  per-frame path via `CONDITION_REFRESH_SEC`) so the gate keys on a current
  condition the moment offsets exist. Production fires are condition-labelled
  and the fuser seam is live. **Remaining:** **1.3b** — fill `WakeFuser`'s
  per-(leg, condition) offsets from the corpus (the **only data-gated**
  recall step: a `WakeFuser(offsets={...})` change, no hot-path or signature
  edits, derived from per-(leg, condition) false-fire / miss rates in the
  labelled corpus); and **1.4 — the verifier / corroboration stage (§2.6)**,
  the committed precision half of recall→verify (decided 2026-05-31, a
  first-class stage, *not* an afterthought). 1.4 builds inside the same
  `WakeFuser` seam: begin with a shared VAD veto + cross-leg corroboration
  (require the AEC-on leg to confirm during TTS to kill `tts_bleed`; require
  ≥2 legs for the raw/chip-direct FP classes), **fail open on the wake path**,
  and measure FA/h against a fresh corpus window before tightening. The
  verifier is what makes *adding* recall legs (more beams, a 4th arm) safe
  rather than FA-inflating — so it lands before, not after, the leg count
  grows.
- **Build:** `default_threshold_offset` per `LegSpec`; a lightweight
  `ConditionContext` estimator (music flag from the **playback-ref RMS
  the bridge already computes**; noise floor / SNR proxy) — *done in 1.1b,
  via a fire-time capture-ring low-percentile RMS rather than a per-frame
  VAD-negative EMA, so there's no hot-loop cost*; a `ConditionAwareFuser`
  that picks per-leg thresholds by condition (quiet → trust raw at base θ;
  media playing → lower the aec3 θ; noisy → lean dtln but still OR raw) —
  *the seam (`jasper/wake_fusion.py` `WakeFuser`) shipped in 1.2; 1.3 fills
  its offsets*. Wire `music_renderer` + a derived `condition_class`
  into telemetry — *done*.
- **Verify:** a fresh `reset-wake-events.sh` window; `analyze-three-leg.sh`
  shows per-condition FRR improvement with no FA/h regression; if any
  single leg ever beats the fused result in a condition, simplify that
  branch (the review's own stop rule).
- **Guardrail — verified safe for `/wake-corpus/`:** the corpus
  recorder shares no code with the fuser, never reads `wake_events`, and
  runs while `jasper-voice` is stopped — per-leg thresholds and the
  condition-aware fuser cannot reach it. One real to-do surfaced while
  checking this — **fixed in 1.1a**: `music_renderer` was in the
  `CREATE TABLE` body but missing from `_MIGRATION_COLUMNS`, so
  already-deployed Pis never got the column (and dropped telemetry, since
  the INSERT names it). Both `music_renderer` and `condition_class` are now
  in `_MIGRATION_COLUMNS`, so the idempotent ALTER backfills existing DBs.

### Phase 2 — Capture-profile capabilities + de-hardcode the bridge  *(prep the swap)*
- **Gate:** Phase 0 (independent of Phase 1).
- **Build:** capability fields on `xvf3800.py` (§2.1); bridge reads
  `MIC_DEVICE`/channels/voice-channel/native-rate from the profile;
  promote `_usb_mic_thread`'s resample path from corpus-only to a
  production capture so a cheap USB mic is a real, supported mic (legs
  stay aec3/raw/dtln). **Do not** extract a Protocol; **do not**
  Python-ize the reconciler yet.
- **Guardrail — `/wake-corpus/` RISK (verified):** the
  `usb_raw`/`usb_webrtc`/`usb_dtln` legs (9881–9883) and
  `_usb_mic_thread` are **shared** with the corpus recorder, and the
  `raw0` leg extracts a **hardcoded** `indata[:, 2]` (not
  `MIC_CHANNEL_INDEX`) that IndexErrors under a profile with no
  channel 2. So: (1) make the raw0 channel index profile-driven and
  skip raw0 when the profile lacks it; (2) do **not** repurpose
  `_usb_mic_thread` in place — add a separate production capture path
  (or version leg provenance in the session sidecar), keeping the
  16 kHz mono int16 / 1280-sample frame format identical; (3) preserve
  the `JASPER_AEC_CORPUS_USB_ENABLED` gate and ports 9881–9883, or
  update the bridge + `wake_ports.py` + the recorder's leg constants in
  the same change (they are duplicated copies of one contract).
- **Verify:** plug in a $20 USB mic, set its profile key, confirm wake
  fires on all three legs via `analyze-three-leg.sh`; XVF path
  unchanged; **run one `/wake-corpus/` session and confirm raw0 +
  `usb_*` WAVs still record** (the corpus-regression check).

### Phase 3 — Learned fusion (logistic regression)  *(data-gated)*
- **Gate:** Phase 1 heuristics plateau **and** ~150–500 labeled
  utterances exist with condition labels.
- **Build:** an L2-regularized logistic-regression `WakeFuser` over
  `[per-leg scores, playback energy, SNR, condition one-hot, score×music
  interactions]`, trained offline, shipped as coefficients; per-condition
  Platt calibration; thresholds at target FA/h. Same `WakeFuser`
  interface — drop-in.
- **Verify:** 5-fold CV beats the Phase 1 heuristic on a held-out
  session; adopt a gradient-boosted-tree variant only if it beats LR
  under CV (else keep LR). Report confidence intervals.

### Phase 4 — Second mic / hardware-AEC 4th arm  *(trigger-gated)*
- **Gate:** a second physical mic in hand.
- **Build:** the second `CaptureProfile`; **now** diff the two real
  profiles and extract the `jasper/mics/base.py` Protocol (the README's
  named trigger); set `does_hardware_aec=True` → `legs_for()` adds the
  `chip_aec` leg automatically (4 arms) or drops AEC3 if the chip
  replaces it; resolve the **bash-reconciler** coupling (§7 decision).
  Add the chip_aec leg's telemetry columns from the registry.
- **Verify:** both mics select correctly via the reconciler;
  `analyze-three-leg.sh` (now N-leg) shows the chip_aec leg's
  solo-save contribution; doctor checks both profiles.

### Phase 5 — Feature-level attention fusion  *(optional; CPU-gated)*
- **Gate:** leg count growth makes per-leg embedding backbones a Pi 5
  CPU problem, **or** accuracy plateaus with ≥1000 utterances.
- **Build:** fork openWakeWord to expose embeddings; a small attention
  net over per-leg embeddings feeding one classifier (Tencent/Yandex
  shape). Caps CPU regardless of leg count.
- **Verify:** RTF lower than N independent detectors at equal/better
  FRR. Likely never needed at ≤4 legs on a Pi 5 — documented endgame,
  not a commitment.

---

## 6. Telemetry & evaluation deltas

- **Condition metadata to add:** a derived `condition_class`
  (quiet/music/noise) + a noise-floor/SNR estimate, and write the
  existing-but-empty `music_renderer` (from `RendererClient`) to
  disambiguate music from our own TTS bleed. These feed both Phase 1
  thresholds and Phase 4 features.
- **N-leg columns:** when the 4th leg lands, add its columns via the
  existing additive `_MIGRATION_COLUMNS` mechanism and extend
  `_analyze_three_leg.py`'s `LEGS`/`SCORE_COLS`/`AUDIO_COLS` + the
  canonical `fired_legs` order. The `fired_legs` CSV is already
  leg-count-agnostic — it's the spine.
- **Labeling stays SQLite-only.** No `/wake-review/` web UI (explicit
  prior decision). Extend `analyze-three-leg.sh`, don't build a new
  tool (testing-tooling.md rule).
- **Eval metric discipline (from the review):** FRR at a fixed FA/h,
  per-condition × per-distance breakdowns, DET curves; FA/h only
  measurable on hours of negative audio; split held-out by
  session/speaker, never within a session.

---

## 7. Open decisions (need a call before/at the relevant phase)

**Resolved 2026-05-31 (recorded here; detail in §2 / §4.2):** (a) the
wake pipeline is **recall → verify**, with the verifier a *first-class,
committed* stage rather than something deferred into learned fusion
(§2.6, Phase 1.4); (b) **session source** is a per-turn selection over
profile-declared streams via a pin → dynamic → default ladder,
AEC'd-by-default but user-overridable-with-warning, locked per turn
(§2.7); (c) **naming** borrows established vocabulary (`profile`,
`direct`/`processed`, `ConflictingDevices`, `Priority`) instead of
coining new terms (§4.2). The items below remain open.

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
- **Stale-doc fix-in-passing items noticed during recon (now resolved):**
  the `WakeLoop` class docstring (was "Dual-stream") was rewritten in
  Phase 0.3 to describe the registry-driven multi-leg design; the README
  doc-atlas already describes HANDOFF-wake-telemetry.md as "Triple-stream."

---

## 10. Phase 0 — PR plan

**Slicing principle:** the wire / on-disk tokens (`on`/`off`/`dtln`),
ports, `wake_events` columns, and `trigger_kind`s are frozen, so the
cross-process consumers keep working untouched. That lets the risky
in-process refactor land without a big-bang multi-file change, and the
consumer cleanup is optional follow-up.

| PR | Scope | Daemon edit? | Status |
|---|---|---|---|
| 0.1 | `jasper/wake_legs.py` registry + `wake_ports` derives its `DEFAULT_*_PORT` from it + `tests/test_wake_legs.py` | no | ✅ **merged (#366)** |
| 0.2 | Collapse `WakeLoop` onto a `LegRuntime` dict + one generic `_wake_leg_loop` (fold the two leg loops + the `if leg==…` ladders) | yes | ✅ **merged (#369)** |
| 0.3 | Build legs from registry + config at the `run()` wiring site via `AsyncExitStack` + the pure `_configured_wake_legs()`; `WakeLoop.__init__` takes a `legs` list instead of the discrete `mic_off`/`detector_off`/… params | yes | ✅ **implemented (this PR)** — Pi smoke-test pending |
| 0.4 | `aec_bridge.py` stat-dict keys derive from `wake_legs.REGISTRY`; `control/server.py` leg-toggle validation routes through a documented `_TOGGLE_TO_TOKEN` (`raw`→`off`) map. The web `/layer/*` toggle vocab, the `/aec` response shape, and the bash reconciler are **intentionally unchanged** (frozen operator/wire contracts; reconciler is the Phase 4 decision) | no | ✅ **implemented (this PR)** |

**Separable quick win** (a Phase 1 dependency, *not* Phase 0): add
`music_renderer` + `condition_class` to `_MIGRATION_COLUMNS` in
`jasper/wake_events.py` so already-deployed Pis backfill the columns.
Independent of the leg refactor — land anytime.

**Landmines for 0.2–0.3 (verified in-code; preserved through 0.3, keep preserving):**
- the leg loops stay standalone tasks cancelled in `run()`'s `finally`,
  never added to `_bg_tasks` (the session-frame handler treats any done
  `_bg_tasks` task as turn-over);
- post-fire, `.reset()` **every** leg's detector (openWakeWord smoothing);
- only the `off` leg runs `_shadow_vad_score_raw` in SESSION state —
  generalize via a per-leg flag, don't drop it;
- keep the `begin_event` kwargs contract identical — the triple-stream
  test's *assertions* stay unchanged (only its `__new__` fixture changes
  to populate `_legs`); unchanged assertions + green = behavior preserved.

**Test strategy:** this worktree has no local `.venv`; run the suite
against the main checkout's venv with `PYTHONPATH` set to the worktree
root so `jasper` resolves to the worktree. Establish a green baseline of
the wake cluster before each daemon-touching PR.

---

Last verified: 2026-05-31
