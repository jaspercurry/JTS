# Tuning bench — execution plan (the Codex work order)

> **Status: historical.** Snapshot from 2026-07-27 when the tuning bench was
> specified as a prescriptive PR-B1..B4 implementation ladder. Preserved for
> primary-source archaeology and implementation traps verified at that
> snapshot date but expected to drift; do not execute it as the current plan.
> The owner replaced its hardcoded lexicon, universal analysis schema, and
> narrow overlay model with the LLM-native workbench direction in
> [`llm-native-tuning-workbench-plan.md`](llm-native-tuning-workbench-plan.md).

**Process contract (every PR in this ladder):**

- Branch from current `origin/main`; confirm
  `git merge-base --is-ancestor origin/main HEAD` before first edit.
- Serial test lanes only (`-p no:randomly`, one lane at a time — macOS
  forks SIGSEGV under concurrent pytest). Linux CI is the full-suite
  authority.
- Every behavior change pinned by a test in the same PR. Every refusal
  path has an `event=` log and honest copy. Prose never claims wider
  than measured.
- Commit messages end `Co-Authored-By:` per repo convention; PR bodies
  end with the standard generated-with footer. **No auto-merge** — an
  independent adversarial review gates every merge (the coordinator
  session runs it; iterate to 0 blockers / 0 should-fixes).
- Do not touch: `jasper/mics/PROFILES` (voice-input registry),
  `JASPER_MIC_DEVICE_CANDIDATES` seeds, limiter/protection/delay/mixer
  blocks in any CamillaDSP config, canonical config files
  (`sound_current.yml`, `active_speaker_*.yml`, the base config).
- Absolute paths in every sub-brief; agents have historically wandered
  into the wrong checkout.

---

## PR-B1 — capture + analysis (`jasper/tuning_bench/`)

New package `jasper/tuning_bench/` + console script
`jasper-tuning-bench` (add to `[project.scripts]` in `pyproject.toml`;
`install.sh` pip-installs `/opt/jasper`, so it appears in the Pi venv
automatically — but this tool is laptop-side; keep heavy imports
(numpy/scipy/matplotlib) lazy so the package is inert on the Pi).

### Verbs (each a thin composition; NO new DSP math anywhere)

**`preflight`** — laptop mic sanity:
- sox missing → error naming `brew install sox`.
- sox nonzero exit → remediation naming
  `sox -t coreaudio -d -d trim 0 0.1` and
  `system_profiler SPAudioDataType` for device discovery.
- Digital-silence refusal: record ~2 s, compute per-channel PEAK dBFS
  (stdlib `wave`+`struct`, numpy-free); refuse below
  `SILENCE_FLOOR_DBFS = -70.0` (adopt the S0 kit's derived value and
  its rationale comment: permission-blocked digital silence reads
  ≈ −96.3 dBFS; a live UMIK in a quiet room reads well above −70).
  Remediation text must name that **the terminal app (or Claude/Codex
  hosting it) needs the macOS microphone grant**.
- Mic channel identification: argmax of per-channel RMS on a stereo
  capture.

**`stimulus`** — generate + push:
- Sweep via `jasper.audio_measurement.sweep.synchronized_swept_sine`
  (defaults 20 Hz–20 kHz, −12 dBFS, 10 s) + band-limited pink
  (500–2000 Hz, −24 dBFS default).
- Per-target level-match gains **baked into the stimulus WAV** — never
  speaker volume (jasper-control quantizes ~0.5 dB per listening-level
  percent, which is the whole tolerance budget; stimulus gain is exact
  and restoration is a no-op). Save the UNGAINED sweep as the
  deconvolution reference (the scalar cancels in the normalized
  magnitude).
- **Single-channel drive safety invariant**: stereo WAV, signal in one
  channel, EXACT digital zero in the other; no both-channel variant
  exists that could reach the wrong cabinet. Contract-tested.
- Push: `ssh pi@<host> "mkdir -p /tmp/tuning-bench-<session>"` + `scp`.

**`level`** — play pink on one target, report band RMS at the mic
(500–2000 Hz), iterate gains to a stated tolerance (default 0.25 dB);
record `match_residual_db`. Trim 0.5 s off each stimulus end before
band-RMS so fades don't drag the average.

**`sweep`** — one capture:
- Record via `sox -q -t coreaudio <device> -b 32 -e floating-point
  -r 48000 -c 2 <path> trim 0 <N>` — **float32/stereo** (the phone-wire
  mono/int16 the S0/e0 kits used is a relay contract, not a mic
  property), **Popen** (recorder must be running before the tone), and
  **`trim 0 N` bounds the file at the sox layer** — never kill sox (a
  killed sox leaves an unfinalized WAV). Lead 1.0 s / tail 1.5 s / gap
  1.0 s / repeats 3 (defaults).
- Playback: `ssh pi@<host> "aplay -D correction_substream -q <wav>"`,
  blocking, timeout = stimulus + 60 s. `correction_substream` is the
  music-chain test lane (`deploy/alsa/asoundrc.jasper` →
  `hw:Loopback,0,4` → fan-in → CamillaDSP), so the bench measures the
  same DSP path the household hears, and `main_volume` applies.
- **Take the mux test lease.** `jasper/mux.py`:
  `MuxDaemon.select_test_fanin_label(label, owner)` over the mux
  control socket — `TEST_SELECT correction <owner>` /
  `TEST_RELEASE <owner>`, 60 s lease (`FANIN_TEST_LEASE_SEC`), renew
  every ~20 s while playing. Add `"tuning-bench"` to
  `FANIN_TEST_OWNERS` (one-line mux edit, the one Pi-side repo change
  in B1, with its own test). The iLoud kit skipped this and worked
  only because JTS3 was idle; while the gate is held, mux refuses
  source selection and no renderer lane enters the fan-in sum
  (`mixer.rs::input_selected` — the correction lane passes
  unconditionally).
- **Pause voice for the sweep.** Send `MEASURE_PAUSE` /
  `MEASURE_RESUME` on voice's UDS (`/run/jasper/voice.sock` — the
  same commands `jasper/correction/coordinator.py::measurement_window`
  uses; voice self-heals via a 120 s server-side auto-clear if the
  bench dies). KNOWN LEAK the bench inherits either way: cue playback
  and timer announcements check only the output gate, not the
  measurement flag — a firing timer can speak into a capture. The
  repeats+spread machinery is the mitigation (an announced-over
  capture shows as a spread outlier); note it in the HANDOFF.
- JSON sidecar per capture: device, mic channel, gains, timestamps.

**`analyze`** — WAVs → canonical JSON + charts. The exact primitive
chain, in order (file → function):
1. `jasper/audio_measurement/deconv.py` →
   `regularized_deconvolution_full(captured, sweep, rate,
   epsilon_relative=1e-3)` (the FULL IR — harmonics live pre-arrival),
   then `direct_arrival_window(...)` → `apply_arrival_window`.
2. `gating.py` → `gate_impulse_response(linear_ir, rate,
   t_max_ms=common_gate_ms)` → `(gated_ir, fragment)`; common gate =
   min across ALL captures of ALL targets; `f_valid_floor_hz`.
3. `deconv.py` → `magnitude_response(ir, rate, normalize=False)` —
   **`normalize=False` is load-bearing**; the default destroys level.
4. `calibration.py` → `parse_calibration_text(text,
   sign_convention="response")` → `apply_calibration_curve` — make
   `sign_convention` a **required argument** of the bench's loader;
   miniDSP files are response curves and the parser's default is wrong
   for them.
5. `analysis.py` → `smooth_fractional_octave` (1/6 gated, 1/3
   in-room) → `resample_log(freqs, mag, f_min=20.0, f_max=20000.0,
   n_points=480)` — **the product grid; interpolation, never stride**.
6. `analysis.py` → `spatial_average_db` (repeat power-mean),
   `normalize_to_band` (anchor band explicit — 300–3000 Hz default,
   stated in the payload; the primitive's own 200–1000 default must be
   overridden), `band_levels_from_magnitude` (band table),
   `thd_curve` (+ `extract_harmonic_ir`/`harmonic_magnitude_response`
   per order).
7. Spec grading (optional): `jasper/active_speaker/flat_spec.py` →
   `evaluate_flat_spec(...)` → `.to_dict()`; `spec_flatness_gauge`.

**`compare`** — two-source A/B: common-gate negotiation across all
captures, difference curves (`difference_sign:
"reference_minus_dut"` stated), band deltas, and the ruled-out
inputs: drive-level delta re-measure (−10 dB), THD, ETC (hand-rolled
`scipy.signal.hilbert` envelope — no JTS primitive exists; keep it in
the bench).

**`ledger`** (B1 ships `open`/`record`; `verify-restore`/`close`
complete in B2) — the session state ledger written as an evidence
bundle via `jasper/audio_measurement/bundles.py`:
- **`info.json` first** (carrying `bundle_schema_version: 1`, `kind:
  "jts_tuning_bench_bundle"`) — `write_json_artifact` raises without
  it. Writes are atomic per file but NOT concurrency-safe: serialize.
- `write_json_artifact(bundle_dir, rel, payload, *, kind, sensitivity,
  recomputable, generated_by, ...)`; `record_artifact` for
  already-written files (WAVs, charts). Paths bundle-relative
  (absolute paths raise). Sensitivity vocabulary in use:
  `private_raw_audio` (WAVs), `debug_safe` (charts), `derived`
  (analysis JSON). `generated_by` = dotted path of the writer.
- Layout: `captures/<session>/bundle/{info.json,
  artifact_manifest.json, analysis/<tag>.json, charts/*.png,
  stimuli/*, sweeps/<target>/*.wav}`.

### The canonical analysis JSON — `jts_tuning_bench_analysis` v1

The complete literal schema lives in the research record and is
restated here as the contract; the implementer copies it exactly.
Top-level: `schema_version: 1`, `kind`, `generated_at`, then blocks
`meta` (session/targets/mic/grid — mic carries `sign_convention`,
`serial_hash` never the raw serial, `calibration_sha256`; grid carries
`{kind: "log", f_min_hz: 20.0, f_max_hz: 20000.0, n_points: 480,
method: "resample_log", smoothing_fraction_gated: 6,
smoothing_fraction_in_room: 3}`), `stimulus` (verbatim
`SweepMeta.to_dict()` + `drive_channel` + `single_channel_invariant` +
`level_match_gains_db` + pink params), `gating` (per-capture verbatim
`gate_impulse_response` fragment dicts + `common_gate_ms` +
`f_valid_hz` + **`gate_saturated_at_search_bound`** + `in_room_window_ms`),
`curves` (shared 480-pt `freqs_hz`; `normalization` block with the
anchor band STATED; `gated`/`in_room` per target;
`difference_gated`/`difference_in_room` + `difference_sign`), `bands`
(`table_id: "perceptual_v1"`; rows carry `band_id`, `f_lo_hz`,
`f_hi_hz`, per-target `levels_db` (power-mean), `delta_db`,
`below_gate_validity`, `material`; `material_threshold_db`; optional
`spec` = verbatim `FlatSpecReport.to_dict()` + `flatness`), `thd`
(`requested_orders` vs per-target `orders`/`orders_missing`/
`comparable` + `cross_target_comparable`; ratio→pct; NaN→null), `level`
(band, per-target dBFS, `match_residual_db`, `match_tolerance_db`,
`applied_via: "stimulus_gain"` — literal, never `"speaker_volume"`),
`repeats` (counts, paths, per-target spread p50/p90/max + curve,
`combiner: "spatial_average_db"`), `attribution` (B3's schema; empty
scaffold in B1), `delta_probe: null` (reserved for PR-L5's
classification vocabulary), `charts` (bundle-relative paths).

### B1 traps (each becomes a test or a load-bearing comment)

1. `sweep.read_wav_mono` **averages stereo** — wrong for bench captures
   (mic on one channel). Bench owns a channel-selecting reader; note
   that `impulse_response_from_capture` inherits the averaging bug for
   stereo inputs.
2. Cal-sign: required argument, asserted in the payload.
3. `magnitude_response(normalize=True)` default destroys level.
4. THD orders go silently asymmetric across targets
   (`extract_harmonic_ir` can raise per order) — carry
   `orders_missing`/`comparable` and refuse cross-target THD claims
   when they differ (the real 2026-07-27 session had jts3=[2,3] vs
   iloud=[2] and nothing flagged it).
5. A 7 ms common gate usually means **the searcher's ceiling**
   (`gating.SEARCH_T_MAX_MS = 7.0`), not a measured reflection —
   `floor_source` per capture + the saturation flag prevent
   over-trusting `f_valid_hz`.
6. `normalize_to_band` and `before_after_delta` have bass-era defaults
   (200–1000 / 50–350 Hz) — always explicit, always stated.
7. `thd_curve` returns ratios with possible NaN — convert to null
   (bare NaN is invalid JSON).
8. Never address ALSA by card index (JTS3's live indices are gapped:
   0,1,2,3,6); `plughw:CARD=<name>` only.
9. The `correction_substream` literal is hardcoded in ≥8 repo sites
   (a known deep-audit finding) — the bench cites ONE
   (`jasper/correction/playback.py:DEFAULT_ALSA_DEVICE`) and does not
   add a ninth copy.

**Acceptance:** hardware-free tests over fixture WAVs (synthesize
sweep + fake room via scipy, round-trip the whole analyze path); the
schema round-trips `json.loads(json.dumps(...))`; one on-hardware
smoke against JTS3 documented in the PR body (owner-scheduled if the
implementer lacks hardware access — state it plainly).

---

## PR-B2 — the overlay lifecycle + ledger close (`jasper/dsp_overlay.py`)

Pi-side primitive + `jasper-dsp-overlay` console script, driven by the
bench over SSH (`ssh pi@host 'sudo /opt/jasper/.venv/bin/jasper-dsp-overlay …'`
— the intended path; root gets the configs dir, the writer lock, the
loopback websocket, and the state files), later callable as a Seat-2
executor.

**Correct paths (the design doc's §6 named a wrong one):** the apply
machinery is **`jasper/dsp_apply.py`** (repo root of the package). Key
constants: `DEFAULT_DSP_APPLY_STATE_PATH =
/var/lib/jasper/dsp_apply_state.json`, `CANONICAL_CAMILLA_CONFIG_DIR =
/var/lib/camilladsp/configs`, lock at `<dir>/.dsp_apply.lock`
(root:jasper 0660, flock, 10 s timeout, asyncio-task re-entrancy).

### The seam map (verbatim contract)

| Verb | Calls |
|---|---|
| `snapshot` | `CamillaController.get_config_file_path()`; sha256 of that file; `CamillaController.get_volume_db()`; `listening_level` from `/var/lib/jasper/speaker_volume.json`; `dsp_apply.dsp_write_epoch()`; applied-profile fingerprint from `jasper/active_speaker/baseline_profile.py`. Write the overlay state file FIRST (fail-closed: intent durable before mutation). |
| `design` | `jasper/sound/profile.py` filter evaluator + `jasper.camilla_config_contract.FilterSpec` (post-L2: shelves are explicit-Q; `SHELF_Q` from the contract module). Prints the headroom/makeup-gain disclosure ("this correction costs N dB of maximum level") — disclosed, never silently capped (owner doctrine). |
| `apply` | `dsp_apply.apply_dsp_config(source="tuning_bench", candidate_path=<overlay>, load_config=cam.set_config_file_path, get_current_config_path=…, persist=None, expected_candidate_sha256=…)`; then the makeup gain via **`POST http://<host>:8780/volume/set {"db": …}`** — never `cam.set_volume_db` directly for the household master (bypassing `VolumeCoordinator` desyncs `listening_level` and the next control change snaps volume back). KNOWN QUANTIZATION: 8780's compatibility `db` field is converted to the 0–100 listening level (~0.5 dB steps; no raw-dB HTTP endpoint exists). Acceptable for makeup gain; the ledger records requested vs realized dB, and precision level-matching stays in stimulus gain where it belongs. |
| `refine` | Same `apply_dsp_config` against the SAME overlay path; master untouched. |
| `revert` | **Lower the master first, then repoint the config** (apply is the mirror: config first, then raise) — there is no atomic primitive spanning both transports, and the wrong order lands makeup gain on an un-tilted graph (the loud-transient trap). Then unlink the overlay; re-read and verify path/sha/volume/fingerprint; write the `restoration` block. |
| `status` | `jasper/correction/status.py:describe_current_config(active)` + `dsp_apply.last_dsp_apply_state()` — detects the reconcile/restart backstop instead of assuming. |

Overlay config: `/var/lib/camilladsp/configs/tuning_overlay.yml`
(sibling of `sound_audition.yml`). State file:
`/var/lib/jasper/dsp_overlay_state.json` (0640, group-from-parent,
`jasper.atomic_io.atomic_write_text`), shape
`{schema_version, kind: "jts_dsp_overlay_state", updated_at, source,
snapshot{config_file_path, config_sha256, main_volume_db,
listening_level_percent, volume_mode, applied_profile_fingerprint,
dsp_write_epoch, captured_at}, overlay{config_path, config_sha256,
makeup_gain_db, target_main_volume_db, filters, applied_at, op_id},
restoration: null|{path_ok, sha_ok, volume_ok, fingerprint_ok,
verified_at}}`. Prefer whole-dict rewrites over key-by-key clears (the
crossover-v2 `observe_restore` key-by-key pattern has a documented
history of missed fields).

### Required integrations (each with its test)

1. **`describe_current_config` registration** — a branch classifying
   `tuning_overlay.yml` so `/correction/` says "Tuning-bench overlay
   active" instead of "a config JTS did not generate". Test against
   the EMITTED bytes: `yaml.safe_dump` strips comments, so the
   `# Source:` content-match branch will not fire (this exact footgun
   hit the real session).
2. **`reconcile_current_dsp` skip branch** — mirror the
   `active_audition` skip (`jasper/sound/runtime.py`) for the overlay
   filename. Without it, **`install.sh` runs the reconcile on every
   deploy and silently reverts the overlay**. `overlay status` must
   detect that backstop and the ledger must NOT count it as a
   verified restoration.
3. **Doctor**: one advisory line when the overlay is the active config
   ("tuning-bench overlay active since <ts> — revert with …").
4. **Ledger close**: `verify-restore` asserts the restoration table;
   `close` refuses without a verified restoration or an explicit
   `--hold` the owner requested.

### Scope-fence contract tests (the validator will NOT save you)

`validate_camilla_config` enforces exactly two things — the
`devices.volume_limit` ≤ 0 ceiling (a MISSING key is a hard reject;
round-tripped YAML must preserve it) and camilladsp `--check` (which
**silently passes when the binary is absent** — `MISSING` ⇒ ok). It
knows nothing of pipeline shape. Therefore the overlay owner's own
contract tests are the ONLY enforcement of: canonical config names
unreachable from the write path; limiters/protection/delays/mixers/
devices untouchable; per-driver linearization gain edits bounded to
`[commissioned value, 0]`; pre-split insertion only.

### B2 traps

- `BassExtensionApplyPending` from `dsp_writer_lock` must propagate —
  never pass `allow_pending_bass_extension_recovery=True`.
- Lock re-entrancy is per-asyncio-task: run each CLI step in its own
  `asyncio.run(...)`; never spawn a sub-task inside the lock.
- `set_volume_db` clamps silently at 0 dB — compute and disclose
  headroom in `design`; keep the kit's posture of staying ≥1 dB under
  the ceiling.
- `jasper-correction-web` runs as **root** today; do not build
  ownership assumptions on its de-rooted future (that migration is
  tracked in HANDOFF-privilege-separation.md; use stat-preserving
  atomic writes and both futures work).

---

## PR-B3 — attribution + lexicon (pure data + schema wiring)

- `jasper/tuning_bench/attribution.py`: the layer vocabulary
  (`linearization`, `integration`, `bass`, `room`, `preference` — ids
  from `active-speaker-tuning-layers-design.md`) plus non-DSP causes
  (`hardware`, `placement`, `level`, `source`). First code instance
  lives here; promotion to a shared module waits for the second
  consumer (rule-of-two). Avoid "correction"/"envelope" in
  identifiers (both three-way overloaded in-repo).
- The attribution record (embedded in analysis JSON + ledger):
  `complaint{verbatim, captured_at, anchoring: independent|anchored}`,
  `hypotheses[{id, cause, band_hz?, source: lexicon|agent|owner}]`,
  `discriminators[{kind, artifact_ref, result, supports, rules_out}]`,
  `verdict{attributed_to, confidence, no_correlate_found, notes}`.
  `anchoring` is load-bearing honesty: words captured after a chart
  was shown are confirmatory, not independent.
- `jasper/tuning_bench/lexicon.py`: pure-data
  `{term, synonyms, hypotheses[{cause, band_hz, direction}],
  discriminators[kinds], notes}`, seeded from the proven session
  mappings (dull/shine→3–16 kHz level; harsh→4–8 kHz level OR
  distortion OR resonance; boxy→100–300; nasal→500–1k or baffle-step;
  thin→LF extension; presence→3–4 kHz). Module docstring states the
  honesty contract: **the lexicon generates hypotheses to test, never
  conclusions**; every captured term maps to a measured correlate or
  an explicit `no_correlate_found` (itself a finding).
- The discriminator catalog wires to what shipped: level match,
  gated-vs-in-room agreement, drive-level re-measure, THD, ETC,
  config A/B (overlay bypass), per-driver-vs-summed (bundle read),
  cross-position spread (cloud), delta-probe (reserved, PR-L5).
- Report template: two-register `{disclosure, expert}` grammar,
  measured-vs-expected-with-tolerance sentences, honest-absence
  vocabulary.

---

## PR-B4 — product-flow integration (session-trigger + bundle ingest)

The two verbs that connect the bench to the phone instrument. All
endpoint facts below are verified against the live route tables.

**`session start [--tier express|full]`** — mints a guided phone
session and prints the tap link + QR:
- `POST http://<host>/correction/crossover/v2/session` body `{"tier":
  "express"}` (the ONLY body key read; absent → full; unknown → 400).
  **nginx strips the `/correction/` prefix** — the loopback backend
  route is `/crossover/v2/session` on `127.0.0.1:8770`; a bench on
  the Pi hitting the port directly must drop the prefix.
- **CSRF dance (no bypass exists, by design):** GET the wizard page
  first with a cookie jar (mints `jts_csrf` + the `<meta
  name="jts-csrf">` token), then POST with the jar + `X-CSRF-Token`
  header. Host header must pass the management allowlist. 403s are
  byte-identical for host vs token failures — the journal's
  `event=http.reject reason=` disambiguates; say so in the error
  copy.
- Response: `{"relay": {"tap_link": <str>, "status":
  "awaiting_phone"}}` — the tap link IS the QR payload (session id,
  upload token, key, MAC all ride the URL fragment; the relay never
  sees them).
- Room-correction variant: the three-step sequence
  `POST /correction/start` (`total_positions` ∈ {1,3,6},
  `target_choice`, `strategy_choice`) → `/correction/relay/level-match`
  → `/correction/relay/capture`.
- **`session status`** polls `GET /correction/crossover/status`
  (`.crossover_v2.{phase,tier,applied,cloud}` + `.relay.status`) —
  NOT `jasper-control`'s `/state`, which has no correction block at
  all. **Poll gently and know the side effect**: the status/envelope
  GETs run the stale-volume-ceiling enforcement; and the room flow's
  session is in-process memory with no state file (unlike
  crossover-v2's), so a bench that stalls between steps can still
  lose an in-flight room session. Since #1860 the level-match ramp
  step itself (`/correction/relay/level-match`) holds the idle-exit
  tracker for its own duration — the same mechanism #1854/#1856 gave
  crossover-v2 — so a slow bench WITHIN that one step will not trip
  the 600 s exit; every other room step (sweep, repeat, verify)
  remains unheld and as vulnerable to the same 600 s stall as before.

**`ingest`** — read product bundles for interpretation:
- Room bundles: `jasper-correction-bundle inspect [--recompute]
  [--json]` (stdout-only) + `jasper.correction.interop.
  impulse_response_from_capture(capture_path, sweep_meta=<plain dict
  from info.json["sweep_meta"]>)` — note it returns an
  arrival-WINDOWED IR (harmonics need
  `regularized_deconvolution_full` instead) and reads WAVs via the
  stereo-averaging `read_wav_mono` (fine for the product's mono
  captures).
- Crossover-v2 evidence:
  `/var/lib/jasper/active_speaker/sessions/<bundle_id>/evidence/v1/
  artifacts/crossover_v2/<relay_session_id>/…` — `check.json`,
  `candidate.json`, `positions/*.json`, `cloud_measure.json` /
  `cloud_verify.json`. **Two distinct ids** (bundle dir =
  `uuid4().hex[:12]`; relay session = `cap_…`); neither derivable
  from the other — read the durable state file to join them. Use
  `CommissioningEvidenceStore.identify_artifact` +
  `.reopen_json_artifact` (sha-verified), not the hand-rolled
  path-build some product code uses. `jasper-correction-bundle`
  CANNOT read these (different schema) — don't try.

**Protocol + packaging (absorbs the design's B4):**
`docs/HANDOFF-tuning-bench.md` — the canonical agent entry (Codex-
compatible: the full session discipline — owner's words verbatim
before charts, ping before any sound, level-match before comparison,
ledger open/verified-restore close — plus every verb with worked
examples); `.claude/commands/tuning-bench.md` as the thin skill;
`docs/testing-tooling.md` rows ("capture a calibrated sweep",
"apply/revert an EQ overlay"); README atlas + doc-map registration.

---

## Deferred by design (recorded here so nobody improvises them)

- **`--mic pi` (UMIK on the Pi's USB).** The transport seam ships in
  B1 as a `MicReader`-style protocol (mirror
  `jasper/route_latency/mic_readers.py`: Protocol + per-transport
  impls + a loud `MicSourceUnavailableError`, dispatcher keyed by
  `--mic laptop|pi:<card>`); only the laptop impl lands. When the Pi
  impl is scheduled: extend `jasper/audio_measurement/calibration.py`
  `SUPPORTED_MODELS` with device-identity fields
  (`alsa_card_matches`, `capture_rate_hz`, `capture_channels`) —
  NEVER `jasper/mics/PROFILES`, NEVER the candidates allowlist — plus
  a non-intersection contract test between the measurement registry
  and the voice-input surfaces. Operational constraints to document:
  plugging any sound card fires the udev→AEC-reconcile path whose
  `restart_voice` is unconditional (~10–15 s voice restart on every
  plug/unplug — connect the UMIK before the session, never hot-plug
  mid-measurement), and `JASPER_MIC_DEVICE=UMIK-2` is a trap that
  disables the household's AEC (pinned by
  `test_reconcile_respects_custom_mic_device`). Capture on the Pi is
  cheap (stream over SSH stdout like `scripts/capture-chip-mic.sh` —
  no Pi-side file); analysis stays laptop-side.
- **Excitation admission for bench sweeps.** The product's
  `admit_excitation` double-boundary contract is deliberately NOT
  wired into the bench's `aplay` path in v1 (the bench is an
  operator-supervised instrument at conservative default levels; the
  product path keeps its own admission). Revisit when the bench
  drives sessions unattended.
- **Seat-2 (in-product agent) widening** — recorded in the design
  doc §8.2; waits for the product-walkthrough design session.

## Ladder acceptance

A fresh agent session, driven ONLY by `docs/HANDOFF-tuning-bench.md`
and the CLI, reproduces the 2026-07-27 iLoud loop end-to-end on JTS3 —
compare → attribute → overlay → verify → verified restore — with zero
hand-rolled code. The owner listens; the owner's ear is the final
gauge.

Last verified: 2026-07-27
