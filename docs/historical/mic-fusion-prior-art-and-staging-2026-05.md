# Mic-fusion prior art and staging plan (2026-05)

> **Status: historical.** The prior-art grounding behind the wake-fusion
> design, the staged Phase 0–5 execution plan, and the Phase 0 PR slicing —
> extracted from HANDOFF-mic-fusion-architecture.md when that doc trimmed to
> an architecture spine. Phase 0 and Phase 1.0–1.3a are merged; the later
> phases are gated proposals, not commitments. Read this for why the
> architecture is shaped the way it is and what the staging was; current
> architecture and current state live in
> [HANDOFF-mic-fusion-architecture.md](../HANDOFF-mic-fusion-architecture.md).

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
  [HANDOFF-wake-training-experiment.md](../HANDOFF-wake-training-experiment.md).
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
> [HANDOFF-wake-training-experiment.md](../HANDOFF-wake-training-experiment.md).
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
  first-class stage, *not* an afterthought). **The seam is landed:**
  `WakeFuser.verify()` + its `_handle_wake_frame` hook, default always-fire
  (behavior-identical to the OR-gate) and fail-open by contract. **Remaining
  inside the seam** are the corroboration rules — a shared VAD veto +
  cross-leg corroboration (require the AEC-on leg to confirm during TTS to
  kill `tts_bleed`; require ≥2 legs for the raw/chip-direct FP classes) —
  each gated on measuring FA/h against a fresh corpus window before
  tightening. The verifier is what makes *adding* recall legs (more beams, a
  4th arm) safe rather than FA-inflating — so the seam lands before, not
  after, the leg count grows.
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
- **Gate:** Phase 0 technically unblocks this, but product sequencing
  now gates it on the chip-AEC telemetry pass. Do not start Phase 2 just
  because the abstraction is attractive; the current fastest product win
  is validating the already-deployed chip-AEC legs.
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

**Why it still matters:** Phase 2 is the path to a lower-cost,
vendor-resilient open-source build. It should reduce the hard dependency
on the ~$70 XVF3800 by making a generic USB mic a supported production
profile. But the expected result is "plumbing and observability work,"
not guaranteed parity: `HANDOFF-usb-mic-wake.md` currently shows the
cheap USB path as useful evidence but weaker than the XVF path.

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
  never added to `_bg_tasks` (WakeLoop treats any done `_bg_tasks` task
  as turn-over via the background completion watcher and session-frame
  backup);
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

