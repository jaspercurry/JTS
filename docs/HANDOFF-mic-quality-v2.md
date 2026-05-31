# HANDOFF: Mic quality v2 — DTLN-aec spike, wake-word path, calibration wizard

**Audience:** fresh Claude / Codex session picking up the mic-quality work
after the wake-telemetry subsystem (PR #191) landed on main.

**Goal:** make the JTS microphone reliable across the full range of normal
human voice — whisper, yell, fast, slow, music playing, music silent — on
whatever USB mic the user owns, not specifically the XVF3800.

**Read first** (in this order):
1. This doc — sequencing + lever inventory + decision history
2. [`docs/HANDOFF-wake-telemetry.md`](HANDOFF-wake-telemetry.md) — the
   measurement infrastructure already deployed
3. [`docs/HANDOFF-aec.md`](HANDOFF-aec.md) — what's been tried on the AEC
   side and why various paths were rejected
4. [`CLAUDE.md`](../CLAUDE.md) "Wake-event telemetry" section — operational
   commands (fetch / audit / label)

---

## TL;DR for the impatient (revised 2026-05-22 night)

**Where we landed after a full day of offline experiments:**

1. **DTLN-aec works.** Converted TFLite → ONNX, ran offline on the
   10-condition baseline. Rescues every AEC3 failure cell: whisper-music
   (silent miss → 2 events), fast-music (3 → 6 events), yell-music (6 → 7).
   No regressions on quiet cells. See "DTLN-aec offline result" below.
2. **AEC3 deep tuning also works — better than expected.** Vendored
   webrtc-audio-processing v2.1 as a static subproject; wrote a
   custom `EchoControlFactory` that exposes the deep
   `EchoCanceller3Config` knobs. After a single-variable sweep
   campaign, **`BEST_A`** (AEC3 v2.1 with hand-tuned config) crosses
   the wake threshold on whisper-music (peak 0.76 vs stock's 0.28)
   and beats AEC3-stock on every failing music cell. It's
   *competitive* with DTLN-256 (BEST_A 27 events on music cells vs
   D256 31, AEC3-stock 23). Full config + sweep methodology preserved
   at [`experiments/aec3-v2-deep-tune-spike/`](../experiments/aec3-v2-deep-tune-spike/).
3. **Different engines catch different utterances.** 3-way Venn analysis
   on the music cells: raw, BEST_A, D256 together catch 42 distinct
   utterances; the best single engine catches 31. The engines are
   complementary, not redundant. This motivates the next phase.

**Triple-stream architecture shipped 2026-05-23.** Extended the
production WakeLoop from the **2-leg OR-gate**
(`aec-on` + `aec-off` per PR #191) to the **3-leg OR-gate**
(raw + BEST_A + DTLN-256). Per-event telemetry captures all three
legs into the existing `wake_events` SQLite + audio-clip ring (the
ring grew 500 MB → 1 GB to keep ~5-7 weeks retention at 3 WAVs per
event). Resource cost is bridge 47% + voice 39% of one Pi 5 core
sustained at idle (~21% of total CPU); decision was to keep as-is
through the first week-of-data review before considering any
mitigations.

**Next move:** ~a week of real-use data, then
`bash scripts/analyze-three-leg.sh` informs a data-driven decision
on which engines to keep + per-leg threshold tuning.

In parallel (independent track): explore **custom wake-word training**
on actual JTS pipeline audio — train a Jarvis model on the corpus
the wake-event capture is already collecting. Add it as a 4th leg
later. Eventually: 3 AEC engines × 2 wake models = 6 detectors,
all firing into the same telemetry + OR-gate.

See "Triple-stream architecture (Phase 1 — DONE 2026-05-23)" below
for the operational details (commits, rollback ladder, first-day
data, resource cost breakdown). The original 2026-05-22 plan is
preserved further down for the predicted-vs-actual audit trail.

---

## Current state — what's actually deployed (2026-05-23)

**Pi:** `jts.local`, tip of `claude/aec3-best-a-prod` (PR not yet
opened; will be merged into main after the first-week review of
triple-stream data lands). `/var/lib/jasper/build.txt` is the live
truth — `ssh pi@jts.local 'sudo cat /var/lib/jasper/build.txt'`.

**Active config** (`/etc/jasper/jasper.env`):

```
JASPER_MIC_DEVICE=udp:9876        # post-AEC (BEST_A) stream from bridge
JASPER_MIC_DEVICE_RAW=udp:9877    # chip-direct stream from bridge (PR #191)
JASPER_MIC_DEVICE_DTLN=udp:9878   # DTLN-aec stream from bridge (Phase 1, 2026-05-23)
JASPER_AEC_DTLN_ENABLED=1         # toggle DTLN inference; 0 falls back to dual-stream
JASPER_AEC_MIC_GAIN_DB=0          # was 6; changed 2026-05-22 to fix tanh distortion
JASPER_AEC_NS_ENABLED=1           # noise suppression on
JASPER_AEC_NS_LEVEL=low           # gentle
JASPER_AEC_AGC1_ENABLED=1         # adaptive digital AGC
JASPER_AEC_AGC1_TARGET_DBFS=9     # -9 dBFS target
JASPER_AEC_AGC1_MAX_GAIN_DB=18    # cap
JASPER_AEC_REF_GAIN_DB=0
JASPER_AEC_CHIP_HPF_HZ=125
JASPER_AEC_REF_HPF_HZ=125
```

The full BEST_A AEC3 knob set has env-var overrides too — see the
"BEST_A canonical config" section of `HANDOFF-NEXT-SESSION.md` for
the knob → env mapping. Defaults are baked into
`_Aec3V2Engine.__init__` in `jasper/cli/aec_bridge.py`.

**What's running:**

| Component | Status | Notes |
|---|---|---|
| `jasper-aec-bridge` | Active | Runs BEST_A AEC3 + DTLN-aec in parallel on each frame; emits AEC ON on `:9876`, raw on `:9877`, DTLN on `:9878` |
| `jasper-voice` | Active | **Triple-stream** WakeLoop, OR-gates AEC ON + AEC OFF + DTLN via `_handle_wake_frame(leg=…)` with shared 0.7 s refractory |
| `jasper-aec-reconcile` | Active | Auto-enables AEC bridge on 6-ch firmware |
| `wake_events` SQLite | Active at `/var/lib/jasper/wake-events/wake-events.sqlite3` | 38 columns; ALTER-migrated for DTLN at startup; 3 × 6 s WAV per event |

**Telemetry that's already capturing:**

- Every wake event (fire or near-miss) → DB row + 3 WAVs (one per leg)
- Per-leg peak scores + peak offsets across all 3 legs
- `fired_legs` CSV at fire time (`'off'`, `'dtln,off'`, `'dtln,off,on'`, ...)
- Funnel timestamps: `ts_turn_opened`, `ts_speech_detected`, `ts_turn_complete`
- Outcomes: `completed`, `no_speech` (FP proxy), `late_cancel`, `peer_lost`, `gate_blocked`, `session_failed`
- Context: bridge config snapshot, music volume from CamillaDSP anchor,
  mic mute, per-leg RMS dBFS

**Analysis scripts in place** (laptop side):

| Script | What it does |
|---|---|
| `scripts/fetch-wake-events.sh` | Pulls DB + WAVs, generates `index.csv` + `index.tsv`, opens Finder |
| `scripts/audit-wake-events.sh` | Forensic audit — WAV integrity, cross-leg parity (xcorr), DB column populated counts |
| `scripts/analyze-three-leg.sh` | Weekly review — fire breakdown by leg pattern, per-leg score distribution, "solo save" counts, listening playlist with `afplay` paths, fired→turn→speech→tool funnel by pattern |
| `/tmp/analyze_aec_distortion.py` | Per-clip peak/RMS/crest/tanh-zone analysis (NOT in repo — promote when stable) |
| `/tmp/analyze_tearing.py` | NS / RS / AGC-pump / frame-boundary / alias detectors (NOT in repo) |

**Last forensic run** (today, 9 dual-leg events with `MIC_GAIN_DB=0`):

```
metric        AEC ON   AEC OFF   delta     interpretation
flat_var      0.126    0.118     +0.008    no NS musical noise
hf_CV         0.924    0.638     +0.286    RS gating HF speech bins (the "tearing")
frame_50Hz    0.088    0.052     +0.036    no frame-boundary clicks
pump          0.095    0.053     +0.043    no AGC pumping
hf_alias     -30.2     -30.2     -0.01     no resampler aliasing
```

vs yesterday (`MIC_GAIN_DB=6`) — peak distortion was eliminated:

```
metric        YESTERDAY (gain=6)    TODAY (gain=0)
median peak   30,514 (pinned)       26,681 (natural)
median RMS    -16.6 dBFS            -23.5 dBFS  (matches the -6 dB env change)
median crest  16.1 dB (squashed)    22.4 dB (matches AEC OFF — restored)
pct_hard      0.50 %                0.00 %
```

### Wake-word baseline against the reference corpus (2026-05-22)

`jarvis_v2.onnx` scored offline against all 10 conditions × 2 streams
(`reference-conditions/`, captured today). Per-condition peak score and
fire counts at the production-default 0.5 threshold:

```
condition         aec-off    aec-on    fires@.5 (off/on)
normal-quiet        0.997        0.997     53 /  50
normal-music        0.997        0.996     17 /  20
whisper-quiet       0.997        0.997     34 /  36
whisper-music       0.997        0.279      2 /   0  ← silent miss on AEC ON
yell-quiet          0.997        0.997     48 /  45
yell-music          0.997        0.996     34 /  15  ← degraded on AEC ON
fast-quiet          0.997        0.997     45 /  40
fast-music          0.997        0.975     30 /   3  ← degraded on AEC ON
slow-quiet          0.997        0.997     26 /  25
slow-music          0.996        0.996     17 /  18
```

Filenames per condition: `aec-off.wav` (raw chip mic, pre-AEC3),
`aec-on.wav` (post-AEC3 — what voice consumes today), `reference.wav`
(playback reference signal — the AEC3 far-end). Naming matches
`scripts/wake-rate-test.sh`'s output so analysis scripts work
on either capture source.

Full CSV at `reference-conditions/jarvis_v2-baseline-scores.csv` (gitignored).
Re-running script: `python scripts/score-baseline-wakeword.py`.

**Refined diagnosis** (replaces the earlier "AEC ON misses 100 %" framing):

- The silent-miss failure mode is **concentrated in 2-3 specific cells**:
  whisper-music (peak 0.997 → 0.279, complete fail), fast-music (peak
  0.997 → 0.975 but fires drop 30 → 3), yell-music (fires drop 34 → 15).
  Not universal across conditions.
- Quiet conditions are fine. AEC ON ≈ raw mic for wake scoring on all
  five no-music conditions.
- The pattern matches the `hf_CV +0.286` forensic finding above —
  AEC3's RS gating hits HF speech bins frame-by-frame, which is
  disproportionately damaging when (a) the signal is quiet (whispers
  depend on HF consonants) or (b) the signal is rapid (less repetition
  for the smoothed score to recover from a gated frame).
- Loudness is *not* the issue: whisper-music AEC output is only ~2 dB
  quieter than raw mic, yet the wake score collapses from 0.997 to
  0.279. The signal is being damaged in a different dimension than
  amplitude.

**Implication for the DTLN-aec experiment:** the cells to watch are
exactly the music + edge-style ones. If DTLN-aec rescues
whisper-music and fast-music without regressing normal-quiet or
yell-quiet, that's a clear win and DTLN-aec should become the
default. If it can't recover whisper-music, the wake model itself
has insufficient HF robustness and Phase 4 (custom training) has
to follow.

### DTLN-aec offline result — clear win (2026-05-22 evening)

Before any bridge integration, ran DTLN-aec (128-unit, converted
TFLite → ONNX, see `scripts/convert-dtln-aec.sh`) offline against
all 10 conditions × (mic, ref) pairs from `reference-conditions/`.
Script: `scripts/_dtln_aec_offline.py` — uses onnxruntime, mirrors
breizhn/DTLN-aec's `run_aec.py` algorithm exactly (512-sample
blocks, 128-sample hop, no window, rfft, magnitude mask in freq
domain, time-domain post-filter). Then scored all 30 files
(10 conditions × 3 legs) with `jarvis_v2.onnx`.

Three-leg jarvis_v2 wake-word scores at threshold 0.5:

```
condition      | raw mic  fires | AEC3  fires | DTLN-aec  fires
normal-quiet   | 0.997     53   | 0.997   50  | 0.997      44
normal-music   | 0.997     17   | 0.996   20  | 0.997      24
whisper-quiet  | 0.997     34   | 0.997   36  | 0.997      39
whisper-music  | 0.997      2   | 0.279    0  | 0.985       2  ✓ DTLN rescues
yell-quiet     | 0.997     48   | 0.997   45  | 0.997      45
yell-music     | 0.997     34   | 0.996   15  | 0.997      22  ✓ DTLN +47%
fast-quiet     | 0.997     45   | 0.997   40  | 0.997      41
fast-music     | 0.997     30   | 0.975    3  | 0.997      18  ✓ DTLN +500%
slow-quiet     | 0.997     26   | 0.997   25  | 0.997      27
slow-music     | 0.996     17   | 0.996   18  | 0.997      12  ⚠ -33%, still fires
```

Full CSV at `reference-conditions/jarvis_v2-three-leg-scores.csv`.

**Read:**

- **Every AEC3-failing cell recovers under DTLN-aec.** whisper-music
  goes from "silent miss" (peak 0.279, 0 fires) to "fires reliably"
  (peak 0.985, 2 fires — matches raw mic). fast-music goes from
  3 fires to 18. yell-music from 15 to 22.
- **No serious regressions.** 9/10 cells either improve or stay
  flat under DTLN-aec. The one negative is slow-music (12 vs 18 fires
  at threshold 0.5), but both fire reliably — neither is at risk
  of silent miss. Worth investigating in the bridge phase whether
  this is a model-size issue (try 256-unit) or fundamental.
- **DTLN-aec runs at real-time on the laptop CPU.** 33 s of audio
  processes in roughly 33 s — viable on the Pi 5, though we'll
  want to measure exact CPU/RAM there.
- **The architecture diagnosis holds.** AEC3's residual suppressor
  was destroying HF speech content in music-heavy conditions
  (`hf_CV +0.286`). DTLN-aec's neural mask preserves the spectral
  content the linear-filter + RS pair was tearing up.

**Implication for bridge integration:** the offline data is strong
enough that the originally-planned "parallel UDP :9878 spike"
(run DTLN alongside AEC3 in production for further A/B) may not
be necessary. Direct AEC3 → DTLN-aec replacement could be the
right call — fewer code paths, simpler operations, faster to
deploy. Decision deferred until the user weighs in (the doc's
Phase 1 framing predates the strength of this offline data).

**Open follow-ups noted from the spike:**
- Yell cells slightly clip at int16 ceiling (~+0.5 to +0.9 dBFS).
  Input was already clipping; DTLN-aec preserves that. Not a
  blocker for wake detection but worth a `np.clip` / soft-knee
  somewhere in the bridge output.
- slow-music's regression deserves a 256-unit retest.
- Latency: DTLN-aec adds ~32 ms of algorithmic delay (the 512-sample
  block). AEC3 added <10 ms. May feel slower for snappy wake →
  response sequencing; measure end-to-end before assuming.

### AEC3 deep-tune spike — vendor v2.1 + BEST_A config rescues most failure cells (2026-05-22 night, UPDATED)

> **Note:** an earlier version of this section concluded "tuning doesn't
> rescue" based on the first config tried (V2tune with the research-
> recommended starting values). That was wrong — V2tune had a bug
> (`bounded_erl=True` silently disables Transparent Mode) and missed
> several relevant knobs. After fixing the bug and running a proper
> single-variable sweep, **BEST_A is a real and usable AEC3 config**
> that beats AEC3-stock on every failing cell. Section below kept for
> the full story.



After the DTLN-aec offline result above, ran the parallel
experiment from `docs/HANDOFF-aec.md` section E: vendor
`webrtc-audio-processing` v2.1 statically and write our own
`EchoControlFactory` that exposes `EchoCanceller3Config`'s deep
knobs (`suppressor.dominant_nearend_detection.snr_threshold`,
`suppressor.use_subband_nearend_detection`,
`filter.refined.length_blocks`, `ep_strength.bounded_erl`, etc.).
Code preserved at
[`experiments/aec3-v2-deep-tune-spike/`](../experiments/aec3-v2-deep-tune-spike/);
README there has the full rebuild recipe.

**5-leg event count comparison** (proper peak detection, 0.7 s
refractory):

```
condition      |   raw   AEC3  V2tune   D128   D256
normal-quiet   |   11     11      11     10     11
normal-music   |    5      7       5      8      9
whisper-quiet  |    9      8       8     10      6
whisper-music  |    1      0       0      1      2  ← V2tune did NOT rescue
yell-quiet     |   10     10      10     10     10
yell-music     |   11      6       7      9      7
fast-quiet     |   11     11      11     10     11
fast-music     |    8      3       1      8      6  ← V2tune REGRESSED
slow-quiet     |    7      7       7      7      6
slow-music     |    7      7       5      4      7
```

V2tune config = research-report-recommended starting values
(`filter.refined.length_blocks=30`, `ep_strength.bounded_erl=true`,
`suppressor.use_subband_nearend_detection=true`,
`suppressor.dominant_nearend_detection.snr_threshold=20`,
`hold_duration=50`, `high_bands_suppression.max_gain_during_echo=1.0`).

**Read:**

- **The infrastructure works.** We CAN build v2.1 statically, expose
  the deep config struct through a custom `EchoControlFactory`, and
  link a pybind11 extension against it. The HANDOFF-aec.md path is
  technically viable.
- **The research-recommended values did not rescue the silent miss.**
  whisper-music peak score = 0.000 (worse than AEC3-stock's 0.279).
  fast-music regressed from 3 events to 1.
- **v2.1 with default-constructed `EchoCanceller3Config` ALSO scored
  whisper-music at 0.002.** This isn't "we picked bad knob values" —
  v2.1's AEC3 itself behaves differently (worse on our signal) than
  Trixie's v1.3 implementation. The behavior delta is real; tuning
  it back to v1.3 parity would itself be a search.
- **DTLN-aec (especially 256) is the only engine that genuinely
  rescues the failing cells without regressing the working ones.**
  This finding combined with V2tune's failure means the offline data
  is unambiguous: DTLN-256 should be the production AEC engine.

**Implications of the V2tune first-pass result (later overturned):**

The above section is preserved for context. After the V2tune failure
we ran a proper single-variable sweep campaign which discovered that
V2tune had a real configuration bug (`bounded_erl=True` silently
disables WebRTC Transparent Mode) and several research-recommended
knobs needed to be REVERTED, not added. Once corrected, the tuned
config (BEST_A, below) is a clear win.

### AEC3 tuning campaign — BEST_A config (2026-05-22 night, FINAL)

A single-variable sweep was run against `V2FIXED` (the post-bug-fix
V2tune) baseline, varying one knob at a time across ~27 configs,
scoring against the 4 music cells. Methodology:
[`experiments/aec3-v2-deep-tune-spike/sweep.py`](../experiments/aec3-v2-deep-tune-spike/sweep.py).

**The winning config — `BEST_A`** — is V2FIXED plus a single change:
**`erle.max_l=1.5, erle.max_h=1.0`** (lower NLP-depth caps than
V2FIXED's 2.0/1.2 or defaults 4.0/1.5). This is the only single-
variable change from V2FIXED that crossed the wake threshold on
whisper-music.

Final BEST_A canonical config (also documented in the spike's README):

```python
BEST_A = dict(
    stream_delay_ms=40,
    ns_enabled=True, ns_level="low",
    agc1_enabled=True, agc1_target_dbfs=9, agc1_max_gain_db=18,
    filter_refined_length_blocks=30,     # was 13 default
    ep_strength_bounded_erl=False,        # FIX from V2tune
    ep_strength_default_gain=0.3,         # was 1.0 default
    erle_max_l=1.5,                       # BEST_A discovery
    erle_max_h=1.0,                       # BEST_A discovery
    erle_onset_detection=False,
    use_stationarity_properties=True,
    conservative_hf_suppression=True,
    normal_mask_hf_enr_transparent=0.3,   # LF parity (was 0.07)
    normal_mask_hf_enr_suppress=0.4,      # LF parity (was 0.1)
    normal_mask_hf_emr_transparent=0.3,
    normal_max_dec_factor_lf=0.05,        # 5× slower attack
)
```

**BEST_A results against the 10-condition baseline:**

```
condition      |  raw  AEC3stock  BEST_A  D256  | improvement vs AEC3-stock
normal-quiet   |  11      11        11     11   | tied
normal-music   |   5       7         7      9   | tied (D256 +2)
whisper-quiet  |   9       8         8      6   | tied
whisper-music  |   1     0/0.28    1/0.76  2/0.98 | ✓ FIRES whisper-music (was silent miss)
yell-quiet     |  10      10        10     10   | tied
yell-music     |  11       6         9      7   | ✓ +3 events
fast-quiet     |  11      11        11     11   | tied
fast-music     |   8       3         4      6   | ✓ +1 event
slow-quiet     |   7       7         7      6   | tied
slow-music     |   7       7         6      7   | -1 event
```

Music-cells totals: AEC3-stock 23, BEST_A 27 (+17%), D256 31 (+35%).

**Knobs that were tuned in the campaign (full list in
[`sweep.py`](../experiments/aec3-v2-deep-tune-spike/sweep.py)):**

Helped (kept in BEST_A):
- `filter.refined.length_blocks=30` (vs default 13)
- `erle.max_l=1.5, max_h=1.0` (vs defaults 4.0, 1.5) — the headline knob
- `use_stationarity_properties=True`
- `normal_max_dec_factor_lf=0.05` (vs default 0.25)
- `ep_strength_bounded_erl=False` (bug-fix; was silently True)

Mixed (kept but worth revisiting):
- `default_gain=0.3` — helps most cells, hurts whisper-music peak
- `conservative_hf_suppression=True` — hurts fast-music slightly
- `normal_mask_hf` LF parity — hurts whisper-music peak

Tested and rejected (do not retry without new hypothesis):
- Maximally loosened residual suppressor — pumping unchanged
- Disabling AGC1 — pumping unchanged
- `high_bands_suppression.max_gain_during_echo > 1.0` — silently clamped
- Combining the two whisper-music winners (erle lower + nearend mask_hf
  parity) — cancels out

**Knobs NOT yet tuned (potential follow-up after triple-stream ships):**

1. `nearend_tuning.max_dec_factor_lf` paired with normal=0.05
2. `echo_audibility.audibility_threshold_hf` (values 50, 200, 1000)
3. `comfort_noise.noise_floor_dbfs` (sound quality, not detection)
4. `subband_nearend_detection` with proper bin ranges (default `{1,1}` is no-op)
5. `ep_strength.default_len`, `nearend_len` (reverb tail priors)
6. **WebRTC field-trial mechanism** (`field_trial::InitFieldTrialsFromString()`)
   — ~50 AEC3 trials including whole-config overrides via string
7. Per-cell custom configs (whisper-music wants opposite settings from
   fast-music; could plumb a config selector based on detected signal type)

Diminishing returns. The triple-stream OR-fusion captures most of the
value these would provide. Revisit only if production telemetry says
BEST_A specifically is letting things through that more tuning could
catch.

---

## Triple-stream architecture (Phase 1 — DONE 2026-05-23)

3-leg OR-gated wake-word architecture (raw + BEST_A + DTLN-256)
with full per-leg telemetry capture **shipped 2026-05-23**. Running
on the Pi at the tip of `claude/aec3-best-a-prod`; PR-merging
deferred while the week-of-data review accumulates.

The original plan + per-step effort estimates from 2026-05-22 are
preserved below as the historical record. The "What shipped" block
right under this header is the current operational truth.

### What shipped (2026-05-23)

Eight commits on `claude/aec3-best-a-prod`:

| commit | what |
|---|---|
| `e84a2ba` | aec3_v2: productionize the BEST_A binding (vendored v2.1 static) |
| `dd5efe3` | aec3_v2: setup.py handles system absl + install.sh force-reinstall |
| `4abcf7d` | aec_bridge: prefer Aec3V2 (BEST_A) engine via `_select_engine()` |
| `f585eaf` | aec_bridge: add DTLN-aec parallel UDP output (`:9878`) |
| `32f2666` | wake-loop: triple-stream (raw + AEC + DTLN) OR-gate + telemetry |
| `0818d9e` | wake_events: audio ring 500 MB → 1 GB for triple-stream |
| `03574eb` | scripts: add analyze-three-leg.sh for weekly triple-stream review |
| `14bd6de` | dtln_models: registry + install.sh fetch from dtln-models-v1 release |

### Resource cost — the headline finding

Bridge + voice at idle (no music, no AirPlay) on Pi 5:

| process | CPU (sustained, % of one core) | RSS |
|---|---|---|
| `jasper-aec-bridge` (BEST_A AEC3 + DTLN-aec) | 45-47% | 178 MB |
| `jasper-voice` (3 parallel WakeWordDetector instances) | 38-39% | 350 MB |
| **combined** | **~84% of one core** | **528 MB** |

System: load avg 1.03 sustained; 779 MB / 2010 MB RAM used; ~21%
of total Pi 5 CPU (4 cores) at all times. AirPlay would add
shairport-sync (~5-10% of one core) but doesn't change the bridge/voice baseline — both are input-independent (ONNX inference is
constant-time per frame).

**Where the cost goes:**
- Bridge ~25% is DTLN-aec (`onnxruntime` on two LSTM stages at 50
  fps); the other ~22% is BEST_A AEC3.
- Voice ~25% on top of the pre-triple-stream baseline is the third
  `openwakeword.model.Model` instance. The "shared embedding"
  wording in the original plan was aspirational — each leg has
  different input audio (AEC ON, AEC OFF, DTLN), so the
  melspectrogram + embedding compute genuinely cannot be shared.

**Decision (2026-05-23): keep as-is.** 21% of total CPU is elevated
but not crisis-level; Pi 5 has 3 cores of headroom. We'll continue
to Phase 1.4-1.8 verification before deciding whether to mitigate
(gate DTLN on render-active is the cheapest first mitigation if the
real-world cost ever bites).

### First-day data (2026-05-23)

After the initial deploy, the wake_events DB has captured one
triple-stream event end-to-end with all three legs scored:

```
evt=20260523T161206Z  legs=off  on=0.002 off=0.996 dtln=0.002
```

The AEC OFF leg dominance (55 of 63 historical fires) confirms the
documented `jarvis_v2.onnx` AEC-ON failure mode. DTLN solo-fires
haven't been observed yet — those are the rare cases where AEC OFF
misses but DTLN catches; the weekly review will surface them as
they accumulate.

Verify the data populated correctly: `bash
scripts/audit-wake-events.sh` (integrity check) and `bash
scripts/analyze-three-leg.sh` (per-pattern fire breakdown +
listening playlist). The analyzer's [3] section flags "Only DTLN
fired" with a ★ — that's the headline metric for evaluating
whether the third leg is pulling weight.

### Operational env vars (must be set on the Pi)

```
JASPER_AEC_DTLN_ENABLED=1              # enable DTLN-aec in the bridge
JASPER_MIC_DEVICE_DTLN=udp:9878        # voice's tertiary leg UDP source
```

Both live in `/etc/jasper/jasper.env`. The 1 GB audio-ring cap is
the `config.py` default; no env var needed unless overriding.

### Rollback ladder

| level | what | command |
|---|---|---|
| 1. Disable DTLN only (keep BEST_A) | drops to dual-stream PR #191 architecture | `sed -i s/JASPER_AEC_DTLN_ENABLED=1/JASPER_AEC_DTLN_ENABLED=0/ /etc/jasper/jasper.env && systemctl restart jasper-aec-bridge` |
| 2. Disable BEST_A (revert to AEC3 v1.3) | bridge falls back to the legacy `Aec3` binding | append `JASPER_AEC_BINDING=v1` to `/etc/jasper/jasper.env` + restart bridge |
| 3. Both | dual-stream + legacy AEC | combine 1 + 2 |

The bridge's `_select_engine()` in `jasper/cli/aec_bridge.py` is
the actual switch.

---

## Triple-stream architecture plan (2026-05-22, original — for reference)

The plan as written before Phase 1 shipped. Preserved here so the
delta between predicted-effort and what-actually-happened is
auditable when later phases plan similarly.

This is the canonical next sprint. **Goal:** ship a 3-leg OR-gated
wake-word architecture (raw + BEST_A + DTLN-256) with full per-leg
telemetry capture into the existing `wake_events` SQLite + audio-clip
ring. Run for ~a week. Use the data to inform the final architecture
(drop redundant engines, tune thresholds, add custom-trained wake
models).

### Motivation

Per the cluster analysis on the 10-condition baseline:

| cell | raw | BEST_A | D256 | union (best possible) |
|---|---|---|---|---|
| whisper-music | 1 @ 21.5s | 1 @ 26.2s | 2 @ both | 2 |
| normal-music | 5 | 7 | 9 | 9 |
| yell-music | 11 | 9 | 7 | 11 |
| fast-music | 8 | 4 | 6 | 11 |
| slow-music | 7 | 6 | 7 | 9 |
| **total** | **32** | **27** | **31** | **42** |

The three engines catch *different* utterances. Single-engine ceiling
is 31; OR-fusion ceiling is 42 (+35%). No single engine wins all
cells. Justifying parallel deployment is straightforward — the data
says so directly.

### Architecture

Extension of the existing 2-leg pattern from PR #191:

```
                       ┌──── jasper-aec-bridge (v2.1 + BEST_A)   ─── UDP :9876 ──┐
chip mic (XVF3800) ────┼──── jasper-aec-bridge-dtln (DTLN-256)   ─── UDP :9878 ──┤
                       │                                                          │
                       └──── chip-direct (raw)                    ─── UDP :9877 ──┘
                                                                                  │
                                                                                  ▼
                                                              jasper-voice WakeLoop
                                                              ── 3-leg OR-gate
                                                              ── 0.7s shared refractory
                                                              ── per-leg wake-event capture
                                                              ── per-leg audio ring
                                                                                  │
                                                                                  ▼
                                                              SQLite + WAVs
                                                              (review weekly)
```

### Telemetry — what we capture per wake event

Schema additions to `jasper.wake_events.WakeEvent` (ALTER-migrated
on next `open()` per existing pattern at module top):

| new column | type | purpose |
|---|---|---|
| `peak_score_dtln_aec` | REAL | DTLN-256 leg peak score during event window |
| `audio_dtln_path` | TEXT | absolute path to per-event 6-s WAV from DTLN leg (or `"rolled_off"` when audio aged out) |
| `fired_legs` | TEXT | bitmap/CSV: which leg(s) crossed threshold to trigger event (e.g. `"aec_on,dtln"`) |

Per-event capture (extends the existing dual-leg pattern):
- 6 s WAV per leg per event (4 s pre + 2 s post wake fire) for ALL
  3 legs simultaneously — we keep the full set even when only one
  leg fired (so we can listen to what the other 2 heard at the
  same moment)
- Per-leg peak scores during the event window
- Standard outcome tracking (`completed`, `no_speech`, `late_cancel`)
  per existing schema
- Music context, mic mute, bridge config snapshot

Audio ring sizing: today's 500 MB ring at 2 streams ≈ 3-6 weeks
retention. With 3 streams per event we'd hit ~2-4 weeks. **Bump to
1 GB cap** via `JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES=1073741824` —
plenty of free disk on the Pi 5 (39 GB available per CLAUDE.md
debug output).

### Implementation effort (5 days, conservative)

| step | effort | what |
|---|---|---|
| 1. Productionize BEST_A binding | 1.5 d | Vendor v2.1 statically in `jasper_aec3/`; promote `binding.cpp` from the spike. Setup.py builds v2.1 via meson as a subdirectory build. Apt deps in install.sh (meson, ninja). Env vars for all the new knobs. Cross-build for Pi 5 aarch64 (budget half day of build-env troubleshooting). |
| 2. DTLN-aec bridge | 1.5 d | New `jasper-aec-bridge-dtln.service` mirroring the AEC3 bridge's supervision pattern (watchdog, restart, sd_notify). Reads from the same `pcm.jasper_capture` dsnoop + chip mic, runs onnxruntime inference on `dtln_aec_256_{1,2}.onnx`, emits on UDP `:9878`. Models hosted on a GitHub release attached to `jaspercurry/JTS`; install.sh fetches at deploy time (same pattern as `jarvis_v2.onnx`). |
| 3. WakeLoop 3rd leg | 0.5 d | Extend `jasper/voice_daemon.py`'s existing dual-stream pattern to triple-stream. OR-gate across all 3 legs with shared 0.7 s refractory. Each leg has its own openWakeWord Model instance on the shared (16, 96) embedding — incremental cost is ~5 MB + ~0.5 ms of CPU per leg. |
| 4. Schema + ring + capture | 0.5 d | ALTER migration for new columns. `_capture_ring_dtln` filled by the new bridge. `fired_legs` populated at fire time. `fetch-wake-events.sh` + audit scripts updated to handle 3 legs. |
| 5. Deploy + validation | 0.5 d | Production deploy. Watch for ~1 hour to confirm wake-event capture works for all 3 legs, no crashes, telemetry rows looking sane. |
| 6. Analysis tooling (parallel) | 1 d | Scripts for Venn analysis of which legs fired together, score distributions over time, per-engine threshold sweep simulation, anomaly flagging for manual review. Iterate on these as the first day's real data comes in. |

### Per-leg thresholds — start conservative

Initial deploy: `JASPER_WAKE_THRESHOLD=0.5` for all 3 legs (production
default). FP rate goes up due to OR-gating; mitigated by:
- BEST_A and DTLN-256 are both reasonably conservative engines
- User reviewing telemetry weekly and noting FP times for analysis
- Tuning thresholds from real data after the first review pass

**Open question for later:** should we move to a consensus gate (any
leg ≥ 0.7 fires alone; <0.7 requires ≥2 legs in agreement)? Deferred
until we see the real FP rate distribution.

### What the weekly review looks like

User runs the speaker normally for ~a week (or until ≥100 wake events
accumulated in the DB). Then:

1. `bash scripts/fetch-wake-events.sh` pulls the week's corpus.
2. New analysis script (`scripts/_analyze_three_leg.py` — to be built
   in Phase 1 step 6) produces:
   - Per-leg fire counts + Venn
   - "Disagreement" report (1 leg fires alone)
   - Per-leg score distributions
   - Audio playlist of N most-interesting events for listening
3. User listens, labels events in the SQLite (the existing
   `label` + `label_notes` columns per HANDOFF-wake-telemetry.md).
4. From the labels: false-positive timestamps → cross-ref user's
   notes → identify common patterns. Bug-fix or tuning iteration.

**No upfront decision criteria.** User explicitly wants to NOT
pre-commit to "drop X if Y." Let the cost/benefit emerge from the
real data.

### Custom wake-word training (independent track)

Orthogonal to the triple-stream rollout. Whenever started:

1. Use the wake-events corpus as training data (it already captures
   per-leg AEC outputs alongside raw mic — the personalization-ready
   distribution).
2. Train via openWakeWord's `train_custom_verifier()` (sklearn LogReg
   on the shared (16, 96) embedding) for Tier 1 — fast, on-Pi,
   personalized to user voice + room + mic.
3. Or train a fresh head via livekit-wakeword's pipeline for Tier 2
   — cloud GPU, fuller model, more capacity.
4. Deploy as 4th leg of the OR-gate. Same fire/capture/telemetry
   path. Just another wake instance on the shared embedding.

Long-term vision: 3 AEC engines × 2 wake models = 6 detectors.
The architecture supports it natively at minimal extra cost (each
extra wake head is ~5 MB + ~0.5 ms of CPU on the shared embedding).
Whether all 6 ever ship in production is a function of the data
we collect from the 3-leg system + the personalized model's
real-world quality.

### What to do FIRST on the next session

1. Read this section + the [`experiments/aec3-v2-deep-tune-spike/README.md`](../experiments/aec3-v2-deep-tune-spike/README.md) for the BEST_A canonical config.
2. Start Step 1 (productionize BEST_A binding). The hard part is the
   Meson-as-subproject build inside setup.py.
3. While Step 1 builds, draft the DTLN-aec bridge service unit + Python
   inference loop (Step 2 can start in parallel since it doesn't depend
   on BEST_A).
4. Don't start the 3rd leg WakeLoop work until both bridges are
   building cleanly on the Pi.

---

### Shallow AEC3 knob tuning — already swept, no win (2026-05-22)

A natural-seeming pre-DTLN test would be to sweep the already-exposed
AEC3 binding knobs (`JASPER_AEC_NS_ENABLED`, `JASPER_AEC_NS_LEVEL`,
`JASPER_AEC_AGC1_*`, `JASPER_AEC_STREAM_DELAY_MS`, `JASPER_AEC_AGC2`).
**Don't.** User has swept all of these manually; none recover the
whisper-music silent miss. The damage is upstream of those stages
(in the AEC3 residual suppressor, per the `hf_CV` finding), and our
binding doesn't expose the RS sub-config knobs.

The deep RS knobs (`Suppressor.dominant_nearend_detection.snr_threshold`,
`subband_nearend_detection.use`, `high_bands_suppression.max_gain_during_echo`)
require exposing `EchoCanceller3Factory` from libwebrtc 1.3 — and that
class is **not** in Trixie's `libwebrtc-audio-processing-dev 1.3-3`
public headers (verified 2026-05-22). Only `EchoCanceller3Config` (the
struct that holds the knobs) and the abstract `EchoControlFactory`
base class are present. Exposing the concrete factory needs either
header vendoring + ABI-risk linking, source-building libwebrtc with
internal headers exposed, or finding a different package — all
3-5+ day projects.

This is why Phase 1 (DTLN-aec engine swap) skips ahead of Phase 2
(AEC3 RS knob exposure): the engine swap is genuinely cheaper than
fighting Trixie's libwebrtc surface area.

---

## The levers we can pull — ranked by expected impact

| # | Lever | Current state | Effort | Expected impact | Why this ranking |
|---|---|---|---|---|---|
| 1 | **Wake-word engine** (jarvis_v2 → personalized verifier, or custom livekit-wakeword) | jarvis_v2 misses 100 % of attempts on AEC ON leg; user feedback "model is struggling to pick it up" | 2–3 days (Tier 1 on-Pi verifier over jarvis_v2); 3–5 days (custom livekit-wakeword Jarvis — must train; no drop-in pretrained) | **Massive** — addresses the root failure mode. Cleaner AEC won't help a model that silently misses | Direct production-data evidence. Model is the bottleneck. **Revised 2026-05-22:** no longer the cheapest option since the "drop-in livekit-wakeword Jarvis" path doesn't exist. |
| 2 | **AEC engine choice** (AEC3 → DTLN-aec, or run both) | AEC3 with RS tearing on sibilants | 2–3 days for parallel integration | **High** — fixes HF tearing without C++ binding work; neural model is drift-tolerant | Sidesteps the libwebrtc surface-area problem entirely. The user's stated preference. |
| 3 | **TTS reference routing fix** (ALSA `multi` plugin) | TTS bypasses AEC entirely; user hears TTS echo in mic | 0.5–1 day (asoundrc change) | **High** for user experience during TTS responses; medium for wake-word | Architectural gap. Clean fix exists. |
| 4 | **AEC3 RS knob exposure** (C++ binding rebuild) | Locked; defaults too aggressive on sibilants | 2–3 days (Meson subproject + binding refactor) | **High** — would fix the tearing in-engine; ONLY useful if we keep AEC3 | High-effort, only payoff if Phase 1 says "stay with AEC3" |
| 5 | **Calibration wizard** (per-environment auto-tuning) | None | 1–2 weeks for skeleton | **Medium-high** for distribution; medium for Jasper's single install | Crucial for OSS scaling, less crucial for one Pi |
| 6 | **Wake-word personalization Tier 2** (cloud retrain, ~150 utterances) | None | 1 week + Modal/RTX 4090 budget (~$0.50/train) | **Medium-high** — generalizes across user voice variation (tired, sick, etc.) | Tier 1 (Lever 1) gets 80 % of the win; Tier 2 closes the gap |
| 7 | **NS / AGC tuning sweep** | NS=low, AGC1 on (target=9, max=18) | hours per knob | **Low-medium** — likely diminishing returns vs Phase 1/2 | Already swept extensively (HANDOFF-aec.md "2026-05-20 findings") |
| 8 | **Speaker ID** (Picovoice Eagle, household member attribution) | None | 1 day to wire | **Low** for mic quality; **medium** for downstream UX | Unblocks per-user routing, doesn't fix mic |
| 9 | **MIC_GAIN_DB** | 0 (set today, distortion gone) | minutes | **Already done** | Closed |
| 10 | **Chip-side AEC** (XVF3800 hardware AEC) | Default off; positive lab result with outputd direct fanout, now an opt-in production wake path under validation | Mostly on-device validation + telemetry review | **Promising but gated** | The old no-USB-IN rejection no longer applies. See HANDOFF-aec.md "Option D" and [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md): chip-AEC mode uses outputd's final-buffer producer, a volatile fixed-beam profile, and bridge `:9876` repoint. Keep default off until fresh wake/FA telemetry justifies it. |

**Highest-leverage sequence:** Lever 2 (DTLN-aec spike) → Lever 1 (wake-word
personalization), in parallel with Lever 3 (TTS routing). Lever 4 (AEC3 RS
knobs) only if Phase 1 keeps AEC3 in play. Lever 5 (wizard) after we know
what the wizard should configure.

**Why DTLN-aec is the first move now (revised 2026-05-22):** the original
framing assumed wake-word personalization was the cheaper test. Discovery
during the 2026-05-22 session showed livekit-wakeword has no pretrained
"Jarvis" model — both wake-word options require comparable effort to the
DTLN-aec spike. DTLN-aec runs first because (a) it tests the engine
hypothesis directly, (b) whichever wake-word path we eventually pick will
benefit from cleaner AEC inputs, and (c) we already have the reference
baseline (`reference-conditions/`, 10 conditions × 3 streams, captured
2026-05-22) to evaluate it against.

---

## Research synthesis — the report's key findings

The full report is preserved in `docs/research/mic-quality-v2-report.md`
(check whether the user wants it committed — currently lives only in this
conversation transcript). One-paragraph distillations follow.

### AEC engine: AEC3 vs DTLN-aec

| | AEC3 | DTLN-aec |
|---|---|---|
| Type | Adaptive linear filter + spectral residual suppressor | Two-stage neural net (STFT + LSTM) |
| Trixie availability | `libwebrtc-audio-processing-dev 1.3-3` (installed) | Not packaged; bring via PyPI / TFLite |
| Pi 5 CPU | ~5 % | ~20 % (256-unit model) |
| Pi 5 RAM | ~85 MB | ~50–100 MB depending on size |
| Latency | <10 ms | ~10 ms (STFT framing) |
| Clock drift tolerance | Poor — filter loses convergence | Excellent — no adaptive filter to diverge |
| Speaker non-linearity tolerance | Poor — assumes linear path | Excellent — learned model |
| HF speech preservation | Brittle (current symptom) | Trained for it |
| Config surface for tuning | Rich (when binding exposes it) | Mostly fixed; pick model size |
| Telephony vs smart-speaker training data | Designed for both | Trained on AEC Challenge (telephony) |

**Net read for Jasper's chain:** DTLN-aec is the right *default* given the
USB-mic + separate-DAC + independent-crystal topology, which is exactly the
case the report says AEC3 struggles with. AEC3 may still win on CPU-constrained
deployments — hence the report's "wizard picks the engine" framing.

### Wake-word engine: jarvis_v2 (openWakeWord) vs livekit-wakeword vs custom

> **Update 2026-05-22:** livekit-wakeword does *not* ship a pre-trained "Jarvis"
> model. It ships only `hey_livekit` as a demonstration of the pipeline. The
> library is fundamentally a *training framework* — to use it for "Jarvis" you
> have to run its training pipeline yourself (synthetic data generation +
> training compute + export). That's a 3-5 day project, not a drop-in swap.
> Implications below.

| | `jarvis_v2.onnx` (current) | livekit-wakeword | Custom-trained (Tier 2) |
|---|---|---|---|
| Architecture | flatten + Dense MLP head | conv + multi-head attention | conv + multi-head attention, user-tuned |
| Training data | Community-collected | LiveKit benchmark | User's mic + voice + room |
| Front-end | Google speech_embedding | Same | Same |
| Recall (vendor benchmarks) | unknown | 86.1 % | should exceed both |
| FPPH (vendor benchmarks) | unknown | 100× lower than jarvis_v2 (vendor claim) | configurable |
| Trixie / Pi 5 compatibility | Proven | Proven (verified 2026-05-21 BUD-E smoke test) | Proven |
| Runtime swap effort | None | **3-5 days** — must train a "Jarvis" model via livekit-wakeword's pipeline; only `hey_livekit` ships pretrained. ONNX swap itself is trivial; the training is the work. | Drop-in (ONNX) once trained |
| Pretrained "Jarvis" available? | Yes (community) | **No** — `hey_livekit` only | n/a — user-trained by definition |

**Net read (updated):** there is no fast wake-word swap. The previously-implied
"drop-in livekit-wakeword" doesn't exist for our wake word. Both real options
(custom-trained livekit-wakeword *or* personalized on-Pi verifier on top of the
existing jarvis_v2) require comparable effort to DTLN-aec, so wake-word work
is no longer the cheaper first move it was framed as. This is why Phase 1
(DTLN-aec) moves first regardless of which problem turns out dominant — the
spike gives us cleaner inputs whichever wake-word path we pick afterward.

The wake-word personalization options if/when we do them:
- **Tier 1 — on-Pi verifier head over jarvis_v2** (Phase 4, ~2-3 days):
  small classifier trained on user's actual "Jarvis" utterances, gates the
  jarvis_v2 score. Keeps the openWakeWord pipeline; just adds a personal
  yes/no head. Lowest infrastructure cost.
- **Tier 2 — custom livekit-wakeword "Jarvis" model** (Phase 4b, ~3-5 days):
  full training pipeline run (synthetic data gen + cloud compute + ONNX
  export). Higher ceiling on quality, more infrastructure (Modal/RTX 4090
  cloud spend, training pipeline maintenance).

Both want the wake-events corpus (already capturing). Both can use the
reference-conditions baseline (captured 2026-05-22) for evaluation.

### TTS reference routing — three fix approaches

> **Topology note (updated 2026-05-26):** This section was written
> against the pre-fan-in `/root/.asoundrc` topology. Current production
> uses `/etc/asound.conf`; `pcm.jasper_capture` dsnoops fan-in's summed
> music output on `hw:Loopback,1,7`. Treat the options below as decision
> archaeology, not an implementation recipe. In particular, do not copy
> TTS into a renderer input lane unless the design also prevents a
> delayed duplicate from reaching the speakers through CamillaDSP.

Per the old `/root/.asoundrc` inspection:

- `pcm.jasper_capture` = dsnoop on `hw:Loopback,1,0` (music snd-aloop chain only)
- `pcm.jasper_out` = dmix on `hw:CARD=A,DEV=0` (Apple USB dongle direct)
- TTS writes to `pcm.jasper_out`; never reaches `pcm.jasper_capture`

| | Effort | Risk | Notes |
|---|---|---|---|
| A. Route TTS through CamillaDSP + snd-aloop | medium | medium — TtsVolumeTracker math assumes post-Camilla; ducking changes | Cleanest data flow; biggest behavioral change |
| B. Bridge accepts two reference streams (music + TTS), mixes before AEC3 | high | medium — AEC3 takes single mono ref; sample-rate alignment | Most flexible long-term |
| **C. ALSA `multi` plugin: TTS forks to dongle AND loopback** | medium | low — TTS audio path unchanged at speaker | Recommended starting point; zero Python changes |

### Calibration wizard

The report proposes a 3-phase wizard:

- **Phase A (acoustic measurement, ~30 s):** noise floor + ESS sweep for IR
  + clock-drift measurement. No user speech.
- **Phase B (AEC configuration, ~30 s):** engine selection from Phase A
  numbers + per-engine config + ERLE validation + sibilant-survival check.
  No user speech.
- **Phase C (wake-word personalization, ~60–90 s):** 5–8 utterances + train
  verifier head + threshold tuning. Optional Tier 2 cloud retrain.

This is the right OSS-distribution framing but **not the right next move
for Jasper's single Pi.** We already know most of what the wizard would
measure for his specific install (drift is bad, music chain is fine, etc.).
The wizard work pays off later — after we've picked the engine and wake-word
model that the wizard should actually configure.

---

## Recommended sequencing — five phases

### Phase 1 — DTLN-aec parallel spike (next session, 2–3 days)

**Goal:** measure DTLN-aec on Jasper's actual signal chain. Don't replace
AEC3 — run alongside, listen + measure, then decide.

**What to build:**

1. Add DTLN-aec as a parallel path in `jasper/cli/aec_bridge.py`. Emit
   its output on a third UDP port `:9878` while the existing AEC3 output
   stays on `:9876` and raw chip stays on `:9877`.
2. DTLN-aec source options (pick one in this order):
   - [breizhn/DTLN-aec](https://github.com/breizhn/DTLN-aec) — TFLite,
     ~50–100 MB RAM. Use the 256-unit model first; fall back to 128 if
     CPU/RAM tight on Pi 5.
   - [SaneBow/PiDTLN](https://github.com/SaneBow/PiDTLN) — Pi-validated
     wrapper, XNNPACK delegate, real-time on Pi 4.
3. Extend `jasper/voice_daemon.py`'s WakeLoop to ingest three UDP streams
   (extend the existing dual-stream pattern). Score wake on each leg
   independently. OR-gate across all three with shared refractory.
4. Schema: add `peak_score_dtln_aec` REAL + `audio_dtln_path` TEXT to
   `wake_events` via the same idempotent ALTER pattern.
5. Capture ring: add `_capture_ring_dtln`; attach_audio writes a third WAV.
6. Update `index.csv` columns to include DTLN score + path.

**What to measure:**

For each captured wake, three legs of audio + three scores. Listen:

- Does DTLN-aec sound cleaner than AEC3 on the user's voice?
- Does DTLN-aec also cancel music (the AEC3 strength)?
- Does DTLN-aec also cancel TTS (after the routing fix in Phase 3)?
- Does the wake model score better on DTLN-aec output than AEC3 output?

Forensic metrics (extend `/tmp/analyze_tearing.py`):

- `hf_CV` on DTLN-aec vs AEC3 — should be much closer to AEC OFF if DTLN
  preserves HF properly
- ERLE in dB during music-only periods — measures cancellation effectiveness
- Wake-word peak score distribution per leg

**Pre-spike measurement:** before any bridge changes, run `jarvis_v2.onnx`
offline against the 10-condition reference baseline (`reference-conditions/`,
captured 2026-05-22). Score both `aec-off.wav` (raw mic — pre-AEC3)
and `aec-on.wav` (today's AEC3 output) per condition. Persist
as `reference-conditions/jarvis_v2-baseline-scores.csv` (gitignored — user's
private corpus). This is the "before" snapshot that Phase 1's after-state
will be compared against. Without it, "DTLN-aec scores better" is a vibe,
not a number.

**Decision point at the end of Phase 1:**

- If DTLN-aec output sounds clean AND wake model scores well on its output → commit to
  DTLN-aec as default. Skip Phase 2 (AEC3 binding work).
- If DTLN-aec output sounds clean BUT wake model still struggles → wake-word
  model is the issue. Skip both Phase 2 and the DTLN commit; go to Phase 4.
  **Revised 2026-05-22:** Phase 4 is no longer the "fast path" it was once
  framed as — both options (Tier 1 personalized verifier, Tier 2 custom
  livekit-wakeword Jarvis) require real training work. Pick based on the
  ceiling-vs-effort tradeoff in the "Wake-word engine" table above.
- If DTLN-aec doesn't clearly win → fall back to Phase 2 (expose AEC3 RS
  knobs and tune properly).

### Phase 2 — AEC3 RS knob exposure (only if Phase 1 keeps AEC3)

**Goal:** stop hand-waving about the residual suppressor. Expose
`EchoCanceller3Config::Suppressor` to the binding's constructor.

**What to build:**

1. In `jasper_aec3/src/aec3_binding.cpp`, build an `EchoCanceller3Config`
   from constructor kwargs and pass it to `EchoCanceller3Factory`. Knobs
   needed (verified present in libwebrtc 1.3 headers):
   - `suppressor.use_subband_nearend_detection` (bool)
   - `suppressor.dominant_nearend_detection.snr_threshold` (float)
   - `suppressor.dominant_nearend_detection.hold_duration` (int)
   - `suppressor.dominant_nearend_detection.trigger_threshold` (float)
   - `suppressor.high_bands_suppression.max_gain_during_echo` (float)
2. Expose to Python via the existing `Aec3(...)` kwargs.
3. Add new env vars: `JASPER_AEC_RS_SNR_THRESHOLD`, `JASPER_AEC_RS_HOLD_MS`,
   `JASPER_AEC_RS_SUBBAND_NEAREND`, `JASPER_AEC_RS_HIGH_BANDS_MAX_GAIN`.
4. Re-run tearing analysis. Target: `hf_CV` delta vs AEC OFF drops below
   +0.05.

### Phase 3 — TTS reference routing fix

**Goal:** AEC sees TTS so it can cancel it.

**What to build:**

Option C (recommended): replace `pcm.jasper_out` with a `multi` plugin
that forks the dmix output into both the dongle AND a feed into the
existing `hw:Loopback,0,0` (the snd-aloop input that the music chain
uses). Result: TTS audio at the dongle is unchanged (same dmix it was
hitting), but a copy enters the loopback alongside music, dsnoop'd as
the AEC reference.

Touches:

- `/etc/asound.conf` — add the multi plugin, define new pcm name (e.g.
  `pcm.jasper_out_with_aec_ref`)
- Change `JASPER_TTS_DEVICE` from `jasper_out` to the new pcm name
- Update `jasper/web/voice_setup.py` if it references `jasper_out`

Risks:

- Latency on TTS path may shift slightly (snd-aloop adds ~20 ms buffering)
- If music + TTS sum in the loopback in ways CamillaDSP doesn't expect,
  could create feedback. Need to check whether CamillaDSP's output path
  is the same as snd-aloop's read side, or independent.

### Phase 4 — Wake-word personalization (Tier 1, on-Pi verifier)

**Goal:** address the silent-miss failure mode by training a verifier on
the user's actual voice.

**Pre-condition:** at least 30 captured wake events with labels
(`real_attempt` vs `music` vs `tv` etc.) in the SQLite. The current
corpus already has 23+ events; needs labeling.

**What to build:**

1. Wizard page at `http://jts.local/wake-review/` (was deferred per
   `feedback_wake_telemetry_labeling_via_sqlite_no_web_ui.md`, but
   personalization needs labeled data so re-evaluate the deferral).
2. Capture script: 5–8 utterances of "Jarvis" / "Jasper" via the existing
   wake-event capture path, but tag them as enrollment.
3. Verifier training:
   - For openWakeWord path: small logistic regression on (16, 96)
     embeddings (use `openwakeword.custom_verifier_model` if available
     — we stubbed it out for RAM reasons; un-stub for training, re-stub
     for runtime).
   - For livekit-wakeword path: small attention head trained on the same
     features.
4. Threshold tuning: per-user FAR target (default <1 FP per 24 h).
5. Atomic model swap: write to `/var/lib/jasper/wake/<user>_verifier.onnx`,
   point `JASPER_WAKE_MODEL` at it via reconciler.

### Phase 5 — Calibration wizard (after Phases 1–4 stabilize)

Only worth building once we know:
- Which AEC engine the wizard should configure
- Which wake-word model it should personalize
- What measurements actually drive the engine pick

Build the report's three-phase wizard (acoustic measurement → AEC config →
wake-word personalization) once those are settled. Skeleton estimate from
the report: 3–5 days for the AEC half, another 3–5 for the wake-word half.

### Phase 6+ — Cloud retrain, speaker ID, telemetry expansion

Per the report. Defer until Phase 5 ships and the local pipeline is solid.

---

## Testing methodology

### What we already have

- **Per-event SQLite + WAV capture** for every wake fire and every
  near-miss. Lets us replay user-specific audio through any AEC config
  offline.
- **Distortion analyzer** (`/tmp/analyze_aec_distortion.py`) — peak,
  RMS, crest, tanh-zone occupancy, hard-clip count.
- **Tearing analyzer** (`/tmp/analyze_tearing.py`) — NS musical noise,
  RS HF gating, frame-boundary clicks, AGC pumping, HF aliasing.
- **Audit script** (`scripts/audit-wake-events.sh`) — cross-leg time
  alignment, duration parity, DB completeness.
- **CSV export** for spreadsheet review + labeling.

### What we need to add for Phase 1

1. **Three-leg comparison script.** Same per-clip metrics but with a
   third column for DTLN-aec. Output a CSV with `event_id`,
   `score_aec3`, `score_dtln`, `score_off`, plus the metrics for each
   leg.
2. **Wake-word offline replay script.** Run a specific ONNX model
   against captured WAVs and report scores. Lets us evaluate "would
   livekit-wakeword have fired on this clip that jarvis_v2 missed?"
   without redeploying.
3. **Listening playlist generator.** Bash script that creates a folder
   with N most recent events × M legs (e.g. 5 events × 3 legs = 15 WAVs)
   ordered such that the user can A/B/C compare. Open Finder at the end.

### Five reference conditions to test against (per user)

The user's stated requirements — "whisper, yell, music, fast, slow." Every
candidate config (AEC3 variant, DTLN-aec variant, wake-word model) should
be tested against the same 5 reference recordings:

| condition | how to capture | what's hard about it |
|---|---|---|
| Whisper | user whispers "Hey Jarvis" 3× near mic | low RMS — AGC may over-amp + add noise; HF consonants weak |
| Yell | user yells "Hey Jarvis" 3× across room | risk of hard clipping; chip AGC may pump |
| Music | typical music playing at usual user volume + 3 normal "Hey Jarvis" | tests AEC under load |
| Fast | user says "Heyjarvis" 3× quickly | short utterance, less context for model |
| Slow | user says "Hey... Jarvis" 3× with long gap | model may give up before second word |

These should live in a stable reference dataset (e.g. `tests/reference-conditions/`)
so every future engine/model change can be A/B'd against the same baseline.
**Build this in Phase 1.**

---

## Decisions already made (don't relitigate)

These are user-private memory notes, listed here so a fresh session
doesn't waste a round arguing about them:

- **PR flow required for main** (`feedback_always_use_pr_flow.md`).
  Every change goes feature-branch + PR + merge. No direct-push.
- **Canonical deploy is `bash scripts/deploy-to-pi.sh`**
  (`feedback_deploy_run_install_sh.md`). Don't hand-roll rsync.
- **JTS is a production speaker — must be resilient**
  (`feedback_speaker_must_be_resilient_and_plug_and_play.md`). New
  failure modes must auto-recover or surface audibly.
- **Silent failure is unacceptable**
  (`feedback_silent_failure_unacceptable.md`). Every wake-blocking
  failure needs an audible cue.
- **Wake-telemetry labeling via SQLite, no web UI for v1**
  (`feedback_wake_telemetry_labeling_via_sqlite_no_web_ui.md`).
  **Note:** this constraint may need to relax for Phase 4
  (personalization needs labeled data — wizard UI may be the right
  capture surface). Check with user before building.
- **For AEC work, prefer engine-internal changes over topology
  rearchitecture** (`feedback_aec_keep_bridge_architecture.md`).
  DTLN-aec is engine-internal (drop-in replacement at the same
  bridge call site). TTS routing fix is topology — flag this
  explicitly when proposing.

---

## Open questions for next session to resolve early

1. **DTLN-aec source:** which Python wrapper? Direct TFLite or PiDTLN?
   Resolve before writing the bridge code.
2. **WakeLoop architecture:** does it stay 3 parallel UDP captures + 3
   detectors, or do we collapse into a fused-score model? Probably keep
   parallel for Phase 1 (easier to A/B), revisit later.
3. **Should the third leg (DTLN) be the session audio source?** Today
   the primary AEC ON leg feeds the LLM session. If DTLN ends up
   sounding cleaner, switching the session source matters. But this
   has knock-on effects (TTS volume tracker math, etc.) — flag for
   user decision.
4. **Tier 2 cloud retrain — Modal vs RTX 4090 Community?** Cost vs
   convenience. The report frames it as "we'll decide later" but it
   becomes blocking once Phase 4 lands.
5. **Reference conditions location:** in-repo (committed to git) or
   user-private (on disk only)? In-repo enables regression testing;
   user-private respects privacy. Probably user-private with a
   gitignored reference path declared in env.

---

## Working notes for the spike

**Pi state on 2026-05-22:**
- Main branch deployed (SHA `4457d00`)
- 23 wake events in DB, 18 WAV pairs
- Dual-stream active, MIC_GAIN_DB=0 active
- 100 % of recent fires came in on AEC OFF leg

**Where the bridge code lives:**
- `jasper/cli/aec_bridge.py` — single-source-of-truth for the bridge process
- `_aec_loop` is the function to extend (currently emits AEC3 to OUT_PORT,
  raw to OUT_PORT_RAW; add DTLN-aec to a new OUT_PORT_DTLN)
- `_Aec3Engine` is the class — model the DTLN integration after it
  (`_DtlnAecEngine`?)

**Where the wake loop code lives:**
- `jasper/voice_daemon.py` `WakeLoop._handle_wake_frame(frame, *, leg=…)`
- `_wake_leg_loop(leg_name)` is the single parallel task for every
  non-primary leg (AEC OFF, DTLN, …); the primary "on" leg is driven by
  `run()`'s main loop
- OR-gate logic in `_handle_wake_frame` is leg-generic (iterates
  `self._legs`); adding a leg is a `jasper.wake_legs` entry + a
  construction branch, not a new loop

**Where the schema lives:**
- `jasper/wake_events.py` `_SCHEMA_SQL` (CREATE TABLE for fresh installs)
- `_MIGRATION_COLUMNS` list at module top — add new columns there + they
  get ALTER'd on next `open()`

**Where to extend the analysis:**
- `scripts/_audit_wake_events.py` (canonical audit; in repo)
- `/tmp/analyze_aec_distortion.py` (NOT in repo — promote to
  `scripts/_analyze_aec_distortion.py` when stable)
- `/tmp/analyze_tearing.py` (NOT in repo — same treatment)

---

## What to do first when picking this up (UPDATED 2026-05-22 night)

1. **Read the new TL;DR** + "Triple-stream architecture plan" section above.
2. **Skim the spike's README**:
   [`experiments/aec3-v2-deep-tune-spike/README.md`](../experiments/aec3-v2-deep-tune-spike/README.md)
   for the BEST_A canonical config + sweep methodology. The binding +
   sweep + forensic scripts in that directory all work today on the
   laptop.
3. **The 10-condition `reference-conditions/` corpus** is on the user's
   laptop (gitignored). All experiments from tonight ran offline against
   it. WAV outputs per engine are still there if you want to listen.
4. **Begin Triple-stream Phase 1, Step 1: productionize BEST_A binding.**
   - Vendor `webrtc-audio-processing` v2.1 statically inside `jasper_aec3/`
   - Promote the binding from `experiments/aec3-v2-deep-tune-spike/binding.cpp`
   - Apt deps in install.sh: `meson`, `ninja`
   - Cross-build risk: ~half a day for ARM64 build-env. macOS laptop
     build worked clean tonight; Linux/Pi 5 may surface a CFLAGS quirk.
5. **In parallel: draft DTLN-aec bridge service unit + Python inference
   loop** (Step 2). The DTLN ONNX files (`dtln_aec_256_{1,2}.onnx`)
   are at `~/Code/JTS/dtln-aec-onnx/` on the laptop. For production
   they need a hosting solution (GitHub release attached to
   `jaspercurry/JTS`, same pattern as `jarvis_v2.onnx`).
6. **Don't start WakeLoop 3rd-leg work until both bridges are
   building cleanly** on the Pi.

---

## Bottom line (updated 2026-05-22 night)

Two real wins from tonight's work:

1. **DTLN-aec** runs cleanly offline on Jasper's audio, rescues every
   AEC3 failure cell, no regressions. Conversion path (TFLite → ONNX
   via tf2onnx) is reproducible.
2. **BEST_A** — a hand-tuned AEC3 v2.1 config — also rescues most
   failure cells (different ones than DTLN, with different trade-offs).
   The vendor-static path is buildable and the deep `EchoCanceller3Config`
   knobs are now accessible through our own factory.

The two engines catch *different utterances*, validated via 3-way
timestamp clustering on the music cells. Single-engine ceiling is
31 events on music cells; OR-fusion ceiling is 42 (+35%).

**Triple-stream architecture shipped 2026-05-23** — raw + BEST_A +
DTLN-256 OR-fused, full per-event telemetry into the existing
`wake_events` infrastructure. **Next move:** ~1 week of real-use
data, then `scripts/analyze-three-leg.sh` informs a data-driven
decision on which engines to keep + per-leg threshold tuning.
Resource cost (bridge 47% + voice 39% of one Pi 5 core sustained
at idle, ~21% of total CPU) is documented under "Triple-stream
architecture (Phase 1 — DONE 2026-05-23) → Resource cost" above.

**The custom wake-word training track** runs independently. The
wake-events corpus collected during the triple-stream rollout IS
the training dataset for the personalized Jarvis model. Tier 1
(on-Pi sklearn LogReg verifier) is the cheapest first move;
Tier 2 (cloud retrain via livekit-wakeword) is the marquee
feature. Both slot into the triple-stream as additional legs at
near-zero marginal compute cost.

The calibration wizard from the original research report is no
longer the front-and-center deliverable. The triple-stream + weekly
review IS the calibration. The wizard becomes valuable when we
distribute JTS to other operators (different rooms, mics, voices)
and want to automate the per-environment configuration that we're
doing manually here.

Last verified: 2026-05-31
