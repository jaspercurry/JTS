# Handoff: pluggable-mic boundary + multi-channel wake fusion architecture

> **Status: living draft — design + execution plan, updated as phases
> land (first written 2026-05-29). Phase 0.1 (leg registry, #366) and
> Phase 0.2 (the LegRuntime refactor, #369) are merged; Phase 0.3
> (registry-driven construction at the run() wiring site) + Phase 0.4
> (consumer migration to the registry) are implemented and in review;
> the rest is planned. Not a record of shipped state — verify against
> code.** This
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
   through the same interface.

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
   → Phase 2 capture-profile + cheap-USB capture → Phase 3 learned
   fusion (data-gated) → Phase 4 second mic / 4th arm (trigger-gated)
   → Phase 5 attention fusion (CPU-gated, probably never on a Pi 5 ≤4
   legs). Wake-model training augmentation is the top accuracy lever but
   is a **parallel track, not a phase** — it lives in
   [HANDOFF-wake-training-experiment.md](HANDOFF-wake-training-experiment.md).

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
                                  │   S1 OR+refractory → S2 per-cond θ →     │
                                  │   S3 logistic-regression → S4 attention  │
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
behind this one interface. Stage 1 is the *current* lock-race OR-gate,
re-expressed as a fuser that reads per-leg thresholds from the
registry. The interface never changes as legs grow or fusion gets
smarter — that orthogonality is the design's whole payoff.

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

## 4. Engagement with the research review

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

### Phase 1 — Per-leg + per-condition thresholds  *(the real "Stage 1" delta)*
- **Gate:** Phase 0.
- **Landed** (branch `claude/wake-fuser`, all behavior-preserving): **1.0**
  the condition-taxonomy SSOT (`jasper/wake_conditions.py`); **1.1a** the
  `condition_class` column + the `music_renderer` `_MIGRATION_COLUMNS`
  backfill (the to-do below — done); **1.1b** the runtime estimator
  (`jasper/wake_condition_context.py` `classify_condition`) recording
  `condition_class` per fire. Production fires are now condition-labelled,
  feeding the tuning. **Remaining:** the thin
  `effective_threshold(leg, condition)` decision point (1.2) and the
  corpus-tuned values (1.3).
- **Build:** `default_threshold_offset` per `LegSpec`; a lightweight
  `ConditionContext` estimator (music flag from the **playback-ref RMS
  the bridge already computes**; noise floor / SNR proxy) — *done in 1.1b,
  via a fire-time capture-ring low-percentile RMS rather than a per-frame
  VAD-negative EMA, so there's no hot-loop cost*; a `ConditionAwareFuser`
  that picks per-leg thresholds by condition (quiet → trust raw at base θ;
  media playing → lower the aec3 θ; noisy → lean dtln but still OR raw) —
  *the 1.2/1.3 work*. Wire `music_renderer` + a derived `condition_class`
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

4. **Doc home / routing.** This doc is a new design doc, not yet wired
   into the README doc-atlas or `doc-map.toml` — intentionally, until
   the plan is agreed (so we don't create an orphan we then restructure).
   On agreement: add the atlas line and a `doc-map.toml` entry under
   `aec-and-mic` / `wake-and-wake-corpus`.

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

Last verified: 2026-05-30
