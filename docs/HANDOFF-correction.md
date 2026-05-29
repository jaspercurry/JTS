# HANDOFF — room correction at `/correction/`

> If you are picking this up across sessions: this is the canonical
> planning + design document for the v2 room-correction feature. Read
> the **Status** and **Architecture decisions** sections first. The
> phased plan is the work tracker — when a phase ships, mark it ✅ and
> update the Status. The "Things to ignore" sections are deliberate
> scope discipline, not omissions.

## Status

- ✅ **Phase 0 — TLS + skeleton wizard.** PR #40 merged 2026-05-09.
  Self-signed cert + iOS trust dance documented; mic-permission
  page with `getSettings()` constraint verify lands at
  `https://jts.local/correction/`.
- ✅ **Phase 0.1 — HTTP preflight before HTTPS interstitial.**
  Implemented 2026-05-28. `http://jts.local/correction/` now serves a
  static preflight page that explains the browser's self-signed-cert
  warning and links to `https://jts.local/correction/` for the actual
  measurement UI. The landing page links to the HTTP preflight, and
  the HTTPS correction page's Home link points back to
  `http://jts.local/` so relative navigation does not inherit the
  HTTPS origin and hit the 443 catch-all. The 443 catch-all now
  redirects non-correction paths back to HTTP instead of returning 404,
  so accidental `https://jts.local/voice/` style navigation recovers.
- ✅ **Phase 1 — single-position end-to-end PEQ.** PR #41 merged
  2026-05-09. Sweep generation (Novak 2015) → playback through
  CamillaDSP → AudioWorklet capture → deconvolution → 1/48-octave
  smoothing → greedy peak-fit PEQ design (≤5 cuts, 20-350 Hz) →
  YAML emit (preserving master_gain mixer) → CamillaDSP hot-swap.
  Coordinator pauses renderers + voice loop via UDS; voice_daemon
  has `MEASURE_PAUSE` / `MEASURE_RESUME` with 2-min auto-clear
  safety timer. 37 new tests, all passing on synthetic data.
- ✅ **Phase 2 — multi-position sweeps + verify pass.** PR #42 merged
  2026-05-09. Power-mean spatial averaging across 5 positions;
  post-Apply re-measurement with deviation metrics; target-curve
  choice (flat / warm / bright). 12 new tests, all passing.
- ✅ **Phase 2.2 — survive `jasper-camilla` restarts.** Merged
  2026-05-11 (#62) + hotfix 2026-05-11; rechecked for the outputd
  cutover branch 2026-05-28. The systemd unit passes a CamillaDSP
  `--statefile` and intentionally *omits the positional CONFIGFILE
  arg*. The initial #62 version included the positional v1.yml as a
  "fallback" — which made the whole feature a no-op because
  CamillaDSP overwrites the statefile with the positional path on
  every start when both are given. The hotfix removes the positional.
  On this cutover branch, Camilla reads
  `/var/lib/camilladsp/outputd-statefile.yml` seeded to
  `/etc/camilladsp/outputd-cutover.yml`; the normal
  `/var/lib/camilladsp/statefile.yml` is preserved for main rollback.
  Subsequent `set_config_file_path()` calls from the wizard update the
  active statefile in place; future restarts read it back. Recovery
  from a bad correction without hand-editing the statefile: add
  `--no_config` to the ExecStart args, restart, fix or re-measure,
  remove the flag.
- ✅ **Phase 2.1 — current-correction visibility + per-session debug
  bundles.** Merged 2026-05-11.
  - `GET /status` now includes a `current_correction` descriptor
    (`{path, session_id, applied_at_epoch, peq_count}`) parsed from
    `CamillaController.get_config_file_path()`. The page banner at
    the top of `/correction/` reads this on load so the user knows
    what's loaded before measuring.
  - `POST /start` auto-resets CamillaDSP to
    `/etc/camilladsp/outputd-cutover.yml` BEFORE the first sweep, so
    every measurement traverses the raw room (not the corrected
    pipeline).
    The prior correction descriptor is preserved in the session's
    `current_correction_at_start` for the bundle.
  - Each session writes a self-contained debug bundle at
    `/var/lib/jasper/correction/sessions/<session_id>/` —
    `info.json` (params + state), `result.json` (chart curves +
    verify), `captures/p<N>.wav` (per-position WAVs),
    `verify.wav` (post-Apply re-measurement, if run), and
    `applied.yml` (copy of the CamillaDSP config that was applied).
    Default ON; opt-out via `JASPER_CORRECTION_SAVE_BUNDLES=0`.
  - `GET /sessions` (debug endpoint) lists the 20 most-recent
    bundles for `curl`-based debugging / future history UI.
  - Covered by the correction regression suite.
- ✅ **Phase 2.3 — calibrated measurement mic substrate.**
  Implemented 2026-05-25. Adds an input-device picker, first-class mic
  calibration registry, server-side serial lookup for Dayton Audio
  iMM-6 / iMM-6C / UMM-6 and miniDSP UMIK-1 / UMIK-2, manual
  calibration-file upload fallback for unsupported mics, and
  per-session bundle metadata (`input_device`, `mic_calibration`,
  `bundle_schema_version`). Calibration files normalize to one
  internal additive `correction_db` curve and are applied in
  `MeasurementSession._smooth_capture` before target normalization
  and PEQ design. Storage lives under
  `/var/lib/jasper/correction/calibration_mics/`.
- ✅ **Phase 2.4 — observability and quality floor.**
  Implemented 2026-05-25. Adds shared bundle helpers
  (`jasper.correction.bundles`), capture-quality assessment before
  deconvolution (`jasper.correction.quality`), explicit browser /
  clipping / low-level / uncalibrated-mic warnings in the UI and
  bundle, atomic `info.json` writes on failed analysis paths, and
  `jasper-doctor` correction checks for the socket, state dirs,
  current CamillaDSP config path, and newest bundle.
- ✅ **Phase 2.5 — correction strategy + design audit substrate.**
  Implemented 2026-05-26. Adds `jasper.correction.strategy`, a
  deterministic policy layer around PEQ generation. The room
  correction wizard now exposes target-profile metadata and bounded
  correction strategies (`safe`, `balanced`, `assertive`), persists
  `strategy_choice`, `correction_strategy`, `target_profile`, and
  `design_report` into `info.json` / `result.json`, and renders a
  compact design audit explaining the selected band, predicted RMS
  improvement, warnings, and per-filter rationale. The read-only
  calibration-agent intake tool surfaces the same report so a future
  LLM can explain and recommend bounded strategy changes without
  reverse-engineering the filters.
- ✅ **Phase 2.6 — first-pass measurement confidence report.**
  Implemented 2026-05-26. Adds `jasper.correction.confidence`, a
  deterministic confidence summary derived from existing facts:
  completed position count, calibrated-mic presence, input-device
  metadata, capture-quality issues, per-position variance in the
  correction band, and strategy gates for `safe` / `balanced` /
  `assertive`. The report is persisted into `info.json` /
  `result.json`, embedded in the design audit, returned from the
  upload/status path, and exposed through the read-only
  calibration-agent intake tools. This is deliberately a v1
  instrument panel; SNR, repeatability, and research-tuned thresholds
  remain future refinements.
- ✅ **Phase 2.7 — confidence UI + per-position analysis artifact.**
  Implemented 2026-05-26. Adds a simple `/correction/` confidence
  card showing score, findings, position-variance summary, and
  allowed/blocked correction strategies. Each completed design now
  writes `position_analysis.json` with per-position magnitude curves,
  spatial average, and per-frequency variance arrays so future FIR and
  LLM tooling can inspect seat-to-seat behavior without re-running
  deconvolution.
- ✅ **Phase 2.8 — multi-position confidence detail.** Implemented
  2026-05-27. Adds shared spatial-spread helpers, per-band
  multi-position summaries (`sub_bass`, `bass`, `upper_bass`,
  `transition`, and the active correction band), deep-null and
  high-variance feature flags, and per-filter spatial-confidence
  annotations in `design_report`. The full report is persisted in
  `position_analysis.json` and summarized in `result.json`, giving
  deterministic code and future LLM tools the same explanation-ready
  facts about which features were accepted, avoided, or too unstable
  for aggressive correction.
- ✅ **Phase 2.9 — browser audio-path confidence substrate.**
  Implemented 2026-05-27. Adds `jasper.correction.browser_audio`, a
  deterministic preflight report built from `getUserMedia()`
  metadata: sample rate, mono channel count, processing flags, granted
  input-device identity, and calibrated-mic presence. The report is
  shown inline in `/correction/`, returned from start/status/upload
  endpoints, persisted in `info.json` / `result.json`, and folded into
  the confidence model so browser processing or sample-rate failures
  block correction before a user wastes time measuring. This is still
  metadata confidence, not an acoustic loopback proof; real phone/Pi
  capture smoke testing remains outstanding.
- ✅ **Phase 2.10 — correction visualization + confidence UX.**
  Implemented 2026-05-28. `/correction/` results now expose the
  measurement facts that already drive the deterministic engine:
  display smoothing controls, correction-band shading, spatial-spread
  overlay, filter-effect overlay, measured/target/predicted/verify
  curves, PEQ markers, rejected-feature markers, band-confidence
  summaries, confidence/strategy gates, runtime-integrity status, and a
  deterministic recommended next action. The implementation stays
  dependency-free in the socket-activated web process: one canvas and
  small JSON summaries rather than a plotting framework.
- ✅ **Phase 2.11 — durable evidence bundle contract + runtime integrity.**
  Implemented 2026-05-28. Every new measurement session is a
  self-describing, replayable evidence packet rather than a set of
  files known by convention. New bundles use bundle schema v3 and
  include `artifact_manifest.json` with checksums, kinds, schemas,
  provenance, dependencies, sensitivity, and recomputability flags for
  raw captures and derived artifacts. They also write
  `runtime_integrity.json`: system load/memory/process snapshots,
  capture sample-count sanity, fan-in xrun deltas, and CamillaDSP
  runtime counters around each sweep/verify pass. Runtime warnings and
  failures feed the same confidence report and bundle validator.
  Treat `captures/p<N>.wav` and `verify.wav` as canonical private raw
  evidence; every curve, confidence report, PEQ, and future FIR/agent
  judgment should be reproducible from those recordings plus sweep
  metadata, calibration, algorithm settings, and runtime-integrity
  evidence. Keep the design file-based and Pi-cheap: no database, no
  continuous telemetry daemon, no unbounded retention.
- ✅ **Phase 2.12 — bundle inspection / REW export substrate.**
  Implemented 2026-05-28. Adds `jasper-correction-bundle`, a small
  operator CLI over `jasper.correction.bundle_tools`: `inspect`
  validates manifest checksums, summarizes confidence/runtime evidence,
  and can replay raw captures into derived curves; `export` writes
  REW-friendly `.frd` / `.txt` frequency-response files plus float32
  impulse-response WAVs recomputed from raw captures and `sweep_meta`.
  This is intentionally not a new correction path. It is a forensic and
  interoperability bridge around the existing bundle contract.
- ✅ **Phase 2.13 — agent-readiness evidence packet + acoustic trust.**
  Implemented 2026-05-28. Adds `acoustic_quality.json`, a compact
  derived trust report built from capture quality, pre-sweep room-noise
  WAVs, banded dBFS SNR estimates, direct-arrival/pre-arrival evidence,
  and optional same-seat repeatability. The browser flow now records
  `noise/p<N>_pre.wav` before each sweep and can repeat the main seat
  into `repeat_captures/p0_r1.wav` without counting that repeat as
  another listening position. Low SNR and weak repeatability feed the
  confidence model, assertive-strategy gate, and future-FIR readiness
  gate. `jasper.correction.evidence` builds a deterministic,
  read-only evidence packet for human and future LLM review. The
  calibration agent CLI now renders an audio-engineer-style review:
  what happened, what looks trustworthy/suspicious, what JTS refused
  to correct, what to do next, and what evidence is missing. `/status`
  also reports whether the active CamillaDSP config is JTS-managed,
  preference-only, room-corrected, or a custom advanced config that
  JTS cannot safely preserve.
- ✅ **Phase 2.14 — read-only measurement report surface + schema
  version docs.** Implemented 2026-05-28. `/correction/` now includes
  a small on-demand history panel backed by `GET /session-report?id=...`.
  It renders the deterministic evidence packet for one bundle: what
  happened, what looks trustworthy, what looks suspicious or missing,
  what JTS refused to correct, and the artifact versions observed.
  The endpoint returns metadata and derived evidence only; raw WAVs
  remain private bundle artifacts for CLI/operator workflows. This
  phase also pins the compatibility expectations for `info.json`,
  `result.json`, `runtime_integrity.json`, `acoustic_quality.json`,
  `artifact_manifest.json`, and evidence packets.
- ✅ **Phase 2.15 — replay-grade artifacts + FIR Stage 0 readiness.**
  Implemented 2026-05-28. Successful measurements now write compact,
  manifest-tracked derived artifacts under `analysis/`: per-capture
  impulse-response WAVs plus response JSON containing raw FFT
  magnitude, 1/48-octave smoothing, the final analysis curve,
  calibration/normalization metadata, direct-arrival evidence, and
  deconvolution settings. These artifacts are recomputable from raw
  WAVs and make future FIR/agent review faster without making the
  browser expose raw recordings. `jasper.correction.fir_runtime` adds
  a no-apply FIR substrate: inspect FIR WAVs for sample-rate, taps,
  latency, memory, max gain, and required headroom; optionally stage
  safe imported coefficients into a bundle as evidence. The evidence
  packet is now schema v2 with explicit `capability_permissions` and
  `missing_evidence` so humans, the CLI, and future LLM tools consume
  the same deterministic permission surface.
- ✅ **Phase 2.16 — minimal bundle history + privacy/delete UX.**
  Implemented 2026-05-28. The `/correction/` read-only measurement
  history now lists each recent bundle with size, state, position
  count, result presence, and an explicit private-raw-recordings badge
  when the bundle contains `captures/`, `noise/`,
  `repeat_captures/`, or `verify.wav` audio. Users can delete an old
  bundle from the same panel; the server resolves the session ID
  through the browser-safe bundle resolver, refuses deletion of an
  active in-progress or ready-to-apply measurement session, removes
  the filesystem bundle, and logs a structured
  `event=correction_session_bundle_deleted` entry. This is
  intentionally not an archive product: no database, no pinning, no
  automatic pruning, and no browser exposure of raw audio.
  `jasper-doctor` also summarizes correction-bundle observability:
  latest bundle, total parseable bundle storage, private raw-audio
  artifact count/bytes, latest evidence completeness, and an
  informational note when old raw recordings are still present.
- ✅ **Phase 2.17 — LLM-ready advisor context packet.**
  Implemented 2026-05-29. `jasper.calibration_agent.advisor_context`
  builds a versioned, redacted `llm_ready_advisor_context` from the
  deterministic evidence packet plus target/strategy summaries,
  acoustic/runtime/repeatability/spatial confidence, rejected or
  caution features, bass residuals, corpus snippets, and the current
  sound-profile DSP shape. It deliberately excludes raw audio,
  absolute paths, secrets, raw mic serials, browser labels, and
  user-entered profile names. The packet carries explicit read-only
  advisor permissions: explain, recommend remeasurement, and suggest
  bounded PEQ only when JTS confidence gates allow it; it always
  prohibits raw-audio access, unconstrained CamillaDSP YAML, filter
  apply, FIR tap generation, safety-gate overrides, and silent
  room/preference layer merging.
- ✅ **Phase 3 — power-user pass-through.** Already shipped as part
  of v1 — `camillagui.service` runs at port 5005, linked from the
  landing page. No additional work required for the originally
  scoped Phase 3.
- ⏳ **Phase 4 — REW interop.** Partially started. Generic export
  landed in Phase 2.12; import/upload of external filter designs and a
  documented REW round-trip remain outstanding.
- ⏳ **Phase 5 — FIR filter ladder.** Stage 0 substrate started:
  inspect/stage imported FIR coefficients and report runtime readiness,
  but do not generate or apply FIR filters yet.

**Current sequencing note (2026-05-28):** after the latest research
intake, the next room-correction priority is still measurement trust
before more filter types. The multi-position confidence layer,
browser-audio metadata substrate, correction visualization surface,
durable runtime-integrity bundle evidence, acoustic-quality evidence,
agent-readiness packet, and bundle inspect/export tooling have landed.
Replay-grade analysis artifacts, explicit evidence permissions, and
FIR runtime inspection/staging have also landed. The next
software/hardware boundary is acoustic browser smoke testing and then
threshold tuning for the native SNR/repeatability evidence; generated
or applied FIR should still wait until the measurement substrate can
prove capture quality, runtime health, spatial stability, and
headroom.
The rationale and source links live in
[`docs/calibration-agent/jts-specific/implementation-ladder.md`](calibration-agent/jts-specific/implementation-ladder.md#2026-05-27-sequencing-update).

**Correction / preference composition note:** `/correction/` owns room
measurement and room PEQ design; `/sound/` owns stock sound curves,
user preference EQ, Bypass / Applied / Draft auditioning, and the
combined CamillaDSP config ordering when both layers are present. Current
operational truth for that composition lives in
[docs/HANDOFF-sound-preferences.md](HANDOFF-sound-preferences.md).

**Outstanding Phases 0-2.12 hardware verification** (see "Hardware
test checklist" below) — the math is validated on synthetic IRs;
the integration with real CamillaDSP / iPhone Safari / aplay /
voice_daemon UDS is unverified and is the gating step before
declaring v2 shippable.

## Goal

A measurement-and-correction loop that runs from a phone at the
listening position. Start at `http://jts.local/correction/`, read the
plain-HTTP warning preflight, then tap through to
`https://jts.local/correction/` for the secure browser-mic page.
Optionally pick a calibrated USB measurement mic, the speaker plays a
sweep, the phone records it, the Pi designs a PEQ filter set,
hot-reloads CamillaDSP, and the next song plays through the corrected
pipeline. Two audiences served by one tool: a WiiM-Home-style novice
flow ("press a button, hear the difference") and a power-user surface
(calibrated mic files, raw `.frd` exports, CamillaDSP YAML, REW
interop, optional FIR later).

Concrete success criterion for v2 ship: Jasper measures from the
couch with an iPhone, the bass mode at his listening position
audibly tightens, the YouTube demo records itself.

## Hardware constraints — load-bearing

These are the facts the design has to honor. Don't redesign around
them; design **with** them.

| Constraint | Source | What it forces |
|---|---|---|
| Raspberry Pi 5 **1 GB** target | User decision (2026-05-09): "see how far we can get on 1 GB" | PEQ is comfortable. FIR stays 1 GB-aware and research-gated: pause renderers during expensive generation, measure real memory before enabling mixed-phase / FDW paths. |
| **Apple USB-C dongle**, stereo, 48 kHz | [README.md](../README.md) Hardware table | Filters are 2-channel. No multi-driver crossover work in scope. |
| Pure ALSA: **snd-aloop + fan-in + outputd**, no PipeWire | [docs/audio-paths.md](audio-paths.md) | Sweep injection point is `correction_substream`, a dedicated fan-in lane. CamillaDSP captures from `pcm.jasper_capture` (dsnoop on summed `hw:Loopback,1,7`), processes, writes to `outputd_content_playback`, and jasper-outputd owns the DAC. |
| `master_gain` mixer **already exists** as identity | [deploy/camilladsp/outputd-cutover.yml](../deploy/camilladsp/outputd-cutover.yml) | The EQ slot is reserved. We add filters in front of it, leave it alone. |
| CamillaDSP websocket **no auth, 127.0.0.1 only** | [PLAN.md](../PLAN.md) | `pycamilladsp` calls stay loopback. Web UI never proxies CamillaDSP WS to the LAN. |
| Volume coordination is **canonical and persistent** | [docs/HANDOFF-volume.md](HANDOFF-volume.md), [jasper/volume_coordinator.py](../jasper/volume_coordinator.py) | Sweep playback should set its own absolute level (not via VolumeCoordinator), restore previous on exit. |
| `Ducker` is **the only writer** to `main_volume` for voice | [jasper/camilla.py](../jasper/camilla.py) + `Ducker` | Measurement coordinator must coexist; voice session during measurement should be impossible (we pause WakeLoop). |
| Existing settings pages on **plain HTTP port 80** | [deploy/nginx-jasper.conf](../deploy/nginx-jasper.conf) | We add HTTPS as an additive 443 server block. Existing routes stay HTTP. |
| `getUserMedia` **requires HTTPS** (browser policy) | Web spec | Cannot avoid TLS for this one feature. Private CA + iOS trust profile is the path. |
| Existing web wizards are **stdlib `ThreadingHTTPServer`** | [jasper/web/voice_setup.py](../jasper/web/voice_setup.py), [jasper/web/dial_setup.py](../jasper/web/dial_setup.py) | We mirror this — no FastAPI / aiohttp. Browser state uses polling today. |
| Cross-daemon coordination is **UDS commands to voice_daemon** | [jasper/control/server.py](../jasper/control/server.py) + `_voice_socket_command` | We extend with `MEASURE_PAUSE` / `MEASURE_RESUME`, mirror the `/cue/play` shape. |

## Architecture decisions

These are the load-bearing decisions. Each has been considered and
the rejected alternatives are recorded so we don't relitigate.

### Decision 1 — TLS via additive nginx HTTPS, private CA

**Decision:** Add `listen 443 ssl` server block to nginx with a
private-CA-issued cert for `jts.local`, but keep the existing port-80
server as the default navigation surface. `http://jts.local/correction/`
serves a preflight page; only the measurement UI at
`https://jts.local/correction/` runs over TLS. Document the iOS
Settings → General → About → Certificate Trust Settings dance as a
one-time onboarding step in [BRINGUP.md](../BRINGUP.md) Phase Z
(post-install).

**Why not stay HTTP?** `getUserMedia` only works on HTTPS or
localhost. There is no workaround for this in any browser. The
existing GitHub Pages bounce trick worked for Spotify because the
Pi was never the OAuth redirect target — the bounce ran on a
trusted public origin. There is no equivalent trick for live mic
capture; the secure context has to *be* the page running the
JavaScript.

**Why not Tailscale or ngrok?** Both depend on internet + an extra
install on every household device. The private CA is one-time on the
Pi, and the trust profile is one-time per device.

**Why not skip iPhone Safari and use desktop Chrome only?** The
product story is "couch + iPhone." That's the demo. Desktop-only
loses the YouTube hook.

**Concrete shape as shipped in Phase 0 / 0.1:**
- `install.sh` generates and preserves `/var/lib/jasper/ca/ca.{crt,key}`.
- `install.sh` reissues `/etc/nginx/ssl/jts.local.{crt,key}` from
  that CA for the configured `JASPER_HOSTNAME`, its wildcard, the
  historical `jts.local`, and `127.0.0.1`.
- Port 80 serves `http://jts.local/correction/` as a static preflight
  page and `http://jts.local/jts-root-ca.crt` with
  `application/x-x509-ca-cert`.
- Port 443 proxies only `/correction/` to `127.0.0.1:8770`; other
  HTTPS paths redirect back to their HTTP equivalents.
- README, BRINGUP, and this handoff document the trust/preflight flow.
  No HSTS header is configured.

**Out of scope:** redirecting HTTP → HTTPS for existing routes.
The Spotify and dial flows do not benefit from being moved to
HTTPS, and breaking them is a regression risk.

### Decision 2 — Web framework: stdlib `ThreadingHTTPServer` + polling

**Decision:** Build `jasper/web/correction_setup.py` as another
`BaseHTTPRequestHandler` subclass colocated in `jasper-web`,
listening on `127.0.0.1:8770`. The browser polls `GET /status`
for sweep / generation state. Audio capture uploads as a single
`POST /upload-capture` with `Content-Length`.

**Why not FastAPI?** Codebase precedent: every existing web wizard
is stdlib `ThreadingHTTPServer`. Adding FastAPI introduces a new
runtime dependency, a new ASGI server (uvicorn), a new systemd
unit shape, and breaks the "one jasper-web process owns all the
settings ports" pattern in [jasper/web/__main__.py](../jasper/web/__main__.py).

**Why not WebSockets?** The original brief assumed bidirectional
WS for "real-time during-sweep visualization." V1 explicitly punts
that (post-hoc viz only). Without it, polling plus discrete REST
actions (`POST /start`, `POST /upload-capture`, `POST /apply`) is
good enough and simpler to debug on iOS Safari.

**Why not aiohttp?** Same reason as FastAPI — new dep, breaks the
pattern. The existing async work in [control/server.py](../jasper/control/server.py)
uses `asyncio.run()` per request to bridge stdlib HTTP into async
coordinator code; we do the same here for the
`measurement_window()` async context manager.

**Concrete shape (current):**
```
HTTP port 80:
GET  /correction/            static preflight explaining the HTTPS warning;
                             OK button links to https://<host>/correction/
GET  /jts-root-ca.crt        download private root CA for iOS trust

HTTPS port 443 after nginx strips /correction/:
GET  /                       page render (stdlib HTML + inline AudioWorklet, no SPA)
GET  /healthz                liveness — "ok"
GET  /status                 session + currently-loaded correction snapshot
                             ({state, peqs, autolevel, input_device,
                             mic_calibration, target_profile,
                             correction_strategy, design_report,
                             current_correction: {path, session_id,
                             applied_at_epoch, peq_count} | null})
GET  /sessions               debug: 20 most-recent session bundles
GET  /session-report?id=<id> read-only evidence report for one bundle
GET  /calibration/models     supported calibrated mic providers/models
POST /start                  reset to base config, begin noise capture, returns session_id;
                             body: {total_positions, target_choice,
                             strategy_choice?, noise_floor_db?,
                             calibration_id?, input_device?,
                             repeat_main_position?}
POST /next-position          advance to position[N+1] pre-sweep noise capture
POST /repeat-position        play the optional same-seat repeat sweep
POST /upload-noise           body = WAV (audio/wav); pre-sweep room noise
POST /upload-capture         body = WAV (audio/wav); per-position, repeat, OR verify capture
POST /calibration/fetch      body: {model, serial, orientation?}; server-side
                             Dayton/miniDSP lookup, normalized + stored
POST /calibration/upload     body: {filename, content, model?, label?,
                             orientation?, sign_convention?}; manual fallback
POST /apply                  → SetConfig(correction_<id>_<unixtime>.yml) + Reload
POST /reset                  → SetConfig(/etc/camilladsp/outputd-cutover.yml) + Reload
POST /verify                 fresh single-position sweep for the verify pass
POST /session/delete         delete one historical measurement bundle
POST /test-tone              5-second 1 kHz tone through music chain
POST /autolevel/start        ramp main_volume while tone plays
POST /autolevel/lock         freeze main_volume at current ramp value
POST /autolevel/cancel       abort ramp, restore pre-autolevel volume
HTTPS fallback              non-/correction/ paths 308 back to HTTP
```

Browser polls `GET /status` every 500 ms; SSE was considered but never
landed because polling is simpler in stdlib and the latency budget
allows it.

### Decision 3 — URL: `/correction/`, plus entry on the landing page

**Decision:** `http://jts.local/correction/` is the user-facing entry
route. It serves a static preflight page on port 80, then the
measurement flow switches to `https://jts.local/correction/` because
browser microphone capture requires a secure context. The nginx
port-80 landing page at `/usr/share/jasper-web/index.html` links to
the preflight instead of directly to HTTPS. The 443 catch-all redirects
non-correction paths back to HTTP; it does not proxy any extra wizard
upstreams over HTTPS.

**Why not `/room/` or `/measure/`?** User specified `/correction/`
in feedback (2026-05-09).

### Decision 4 — Coordinator: extend voice_daemon UDS, no new daemon

**Decision:** Add two commands to `voice_daemon`'s control socket
([jasper/voice_daemon.py](../jasper/voice_daemon.py)):
- `MEASURE_PAUSE` → set in-process `_measurement_active` event;
  pause `WakeLoop` (block on the event before pulling the next
  audio chunk); pause `TtsVolumeTracker` (skip the `playback_rms`
  poll); cancel any active `Ducker.duck()` and skip future ones;
  return JSON `{"result": "ok"}`.
- `MEASURE_RESUME` → clear the event, restart trackers, return JSON.

The HTTP coordinator at `jasper/correction/coordinator.py` is an
async context manager:
```python
async with measurement_window():
    # 1. systemctl stop librespot shairport-sync bluealsa-aplay
    # 2. UDS MEASURE_PAUSE → voice_daemon
    # 3. yield (caller does the sweep + analysis + filter design)
    # 4. UDS MEASURE_RESUME → voice_daemon  (in finally)
    # 5. systemctl start librespot shairport-sync bluealsa-aplay
```

**Why not a new `jasper-coordinator` daemon?** The patterns we
need already exist:
- "Pause renderers" = `systemctl stop`. Done.
- "Pause voice loop" = UDS command (mirrors `/cue/play` shape).
- "Pause AEC bridge" = if enabled, the bridge re-converges in
  ~200 ms after the sweep stops; no pause needed.
- A new daemon adds startup time, IPC plumbing, systemd shape,
  and another thing that can fail. The work doesn't justify it.

**Why scoped under `jasper/correction/` not top-level
`jasper/coordinator/`?** The "pause everything for X" pattern is
not currently reused anywhere else. Push-to-talk doesn't need it
(the dial drives the WakeLoop directly). Snapcast (v5) is far
enough out that we'll know its requirements when we get there.
Keep it scoped until we have a second caller. **YAGNI.**

### Decision 5 — Filter ladder for 1 GB Pi

**Decision:** PEQ-only as v1. FIR work stays staged and
measurement-backed: convolution import/export first, then short
minimum-phase magnitude correction, then FDW / mixed-phase only after
real 1 GB profiling. Expensive filter generation should extend the
measurement window through design, not just measurement. Surface a
"this filter type needs 2 GB" message at runtime if `/proc/meminfo`
shows insufficient free memory *after* pausing.

**Why not require 2 GB?** User decision (2026-05-09): "see how
far we can get on 1 GB."

**Concrete RAM budget on 1 GB after pause:** Per the explore-agent
audit of running processes, steady total is 500-620 MB. Pausing
librespot, shairport-sync, bluez-alsa, and the Gemini Live SDK
(via `MEASURE_PAUSE` + `systemctl stop`) frees an estimated
130-200 MB. That's enough headroom for PEQ (negligible) and likely
enough for short minimum-phase FIR prototypes, but Phase 5 should
measure peak memory before enabling mixed-phase or FDW generation in
the user-facing flow.

**Defaults that keep us safely on the safe side:**
- Match range: 20-350 Hz (project-safe modal/transition-region
  boundary; not a direct Toole magic number)
- ≤5 PEQ filters
- Cuts only by default (Floyd Toole's "first do no harm")
- Max boost +3 dB (toggle in advanced drawer)
- Max cut -10 dB
- Q range 1.0-8.0
- Overall max boost 0 dB (preserve digital headroom)

These mirror Jasper's known-good REW workflow (per the engineering
brief).

### Decision 6 — Sweep generation: in-house synchronized swept-sine

**Decision:** Use the in-house NumPy/SciPy synchronized swept-sine
generator in `jasper.correction.sweep` (Novak 2015), not vanilla
Farina ESS. 10 s sweep, 20 Hz - 20 kHz, -12 dBFS. The deconvolution
path performs FFT-based regularized inversion at IR-extract time; no
precomputed inverse filter is shipped.

**Why not vanilla Farina?** Synchronized variant places harmonic
distortion impulses at integer-fraction offsets of the IR, making
them trivial to discard. The shipped generator implements that
synchronization directly so the runtime dependency surface stays
small.

### Decision 7 — Spatial averaging: fixed multi-position sweeps, power mean

**Decision:** Phase 1 ships single-position. Phase 2 adds fixed
multi-position sweeps and power-mean magnitude averaging. This is not
true MMM: moving-mic measurement usually means pink-noise/RTA-style
averaging over a small listening volume and does not preserve IR/phase
data. Strict future spatial analysis should retain complex H(f) per
position and may vector-average below the transition region while
power-averaging above, but the shipped code does power-mean
everywhere.

### Decision 8 — Mic compensation: calibrated external mic first

**Decision:** Treat per-unit calibrated measurement mics as the
trusted path. The wizard supports browser input-device selection,
server-side serial lookup for known Dayton Audio and miniDSP mics,
and manual calibration-file upload for unsupported mics or vendor
lookup failures. A built-in phone mic path may remain as a degraded
fallback, but it must be labeled lower confidence and should not
unlock future FIR or agent recommendations that depend on small dB
distinctions.

**Why not a per-phone curve database?** No published cross-model
compensation database exists. HouseCurve and AudioTool both ship
single generic curves. A generic phone curve is useful for demos and
rough bass-region work, but the long-term substrate for serious room
correction is a known calibrated mic with bundle provenance.

### Decision 9 — Power-user pass-through: reverse-proxy `camillagui-backend` at `/camilla/`

**Decision:** Phase 3 drops in HEnquist's `camillagui-backend`
v0.7.x as a systemd service on `127.0.0.1:5000`. Reverse-proxy
`https://jts.local/camilla/*` → `127.0.0.1:5000/*` in the new
nginx 443 server block.

**Why not build our own YAML upload UI?** camillagui-backend is
written by the CamillaDSP author, the AVS/ASR community already
knows the UI on sight, it has FIR coefficient upload, statefile
management, level meters, pipeline visualization, theming via CSS
variables. Building this ourselves is 4+ days of work for a worse
result.

**Statefile coordination:** measurement coordinator is the writer
of `/var/lib/camilladsp/state.yml`. camillagui reads + suggests,
only pushes on explicit user action.

## Audio path: where the sweep enters

From [docs/audio-paths.md](audio-paths.md):

```
MUSIC chain
    renderers / correction sweeps → private fan-in lanes
              → jasper-fanin → pcm.jasper_capture
              → jasper-camilla (main_volume + filters)
              → outputd_content_playback
              → jasper-outputd → outputd_dac → amp → speakers
```

**Sweep injection point: `correction_substream`.** This puts the
sweep on the same path music takes — through jasper-fanin, through
CamillaDSP, through any active correction filter, through outputd to
the dongle —
without borrowing a renderer's private lane. So:

1. Pre-correction measurement = sweep through current pipeline.
2. Apply candidate filter set.
3. Post-correction measurement = sweep through the new pipeline.

**This is critical:** the sweep MUST go through CamillaDSP.
Otherwise we measure the speaker+room raw, apply a correction,
and never verify it actually changed anything. The previous TTS-
bypass-of-CamillaDSP pattern (TTS → outputd directly, or legacy
TTS → `pcm.jasper_out`) is *wrong* for measurement.

**Volume during sweep:** Set CamillaDSP `main_volume` to a known
absolute level (the brief suggests -12 dBFS sweep at user-controlled
analog volume; we set `main_volume` to whatever the user picked
during the volume calibration screen, default -10 dB). On exit,
restore via VolumeCoordinator.

**Music ducking interaction:** Ducker MUST be skipped during the
measurement window (see Decision 4). Otherwise a wake event
mid-sweep would attenuate the sweep itself.

## File map — current correction code

```
jasper/
├── correction/
│   ├── __init__.py
│   ├── coordinator.py                   measurement_window() async CM
│   ├── sweep.py                         NumPy/SciPy synchronized swept-sine
│   ├── playback.py                      sweep → correction_substream via aplay
│   ├── deconv.py                        IR extraction
│   ├── analysis.py                      smoothing, spatial avg, deviation metrics
│   ├── peq.py                           greedy PEQ design (≤5 filters, cuts)
│   ├── target.py                        Harman / flat / house-curve interpolant
│   ├── camilla_yaml.py                  PyYAML emit; preserves master_gain placeholder
│   ├── calibration.py                   calibration parser + Dayton/miniDSP providers
│   ├── quality.py                       capture quality gates + issue schema
│   ├── bundles.py                       debug-bundle listing / validation helpers
│   ├── bundle_tools.py                  inspect/replay/export helpers for bundles
│   ├── runtime_integrity.py             Pi/runtime health evidence around sweeps
│   ├── acoustic_quality.py              SNR/repeatability/direct-arrival trust evidence
│   ├── replay_artifacts.py              compact derived IR/response artifacts
│   ├── fir_runtime.py                   FIR coefficient inspect/stage substrate
│   ├── evidence.py                      deterministic human/agent evidence packet
│   └── session.py                       bundle writer + measurement state machine
│
├── cli/
│   └── doctor.py                        correction socket / bundle / config checks
│
├── web/
│   ├── correction_report.py             read-only report payload helpers
│   └── correction_setup.py              mirrors voice_setup.py shape
│                                        ThreadingHTTPServer on 127.0.0.1:8770
│                                        polling status, POST for upload+apply
│
├── voice_daemon.py                      MEASURE_PAUSE / MEASURE_RESUME
│                                        UDS commands; gate WakeLoop +
│                                        TtsVolumeTracker on _measurement_active
│
└── camilla.py                           CamillaController config-path switch +
                                         reload helpers used by /correction/

deploy/
├── nginx-jasper.conf                    443 server block for /correction/
├── jasper-correction-web.service        socket-activated worker, private umask
├── jasper-correction-web.socket         systemd socket on 127.0.0.1:8770
└── install.sh                           private CA, state dirs, unit install

docs/
└── HANDOFF-correction.md                THIS FILE

tests/
├── test_correction_sweep_deconv.py      sweep + deconvolution fixtures
├── test_correction_peq.py               PEQ design on known curves
├── test_correction_camilla_yaml.py      YAML round-trip with master_gain preserved
├── test_correction_coordinator.py       pause/resume contract
├── test_correction_session.py           session bundle + measurement flow
├── test_correction_setup.py             correction web handler
├── test_correction_calibration.py       mic calibration parser/providers
├── test_correction_quality.py           capture quality gates
├── test_correction_bundles.py           bundle listing / validation helpers
├── correction_bundle_fixtures.py        golden synthetic bundle helper
└── test_correction_systemd_unit.py      unit/install invariants

/usr/share/jasper-web/index.html         EDIT — add /correction/ entry card
```

**Naming consistency check:** subpackage is `jasper.correction` (not
`jasper.room`) per Decision 3. Web wizard module follows the existing
suffix convention (`voice_setup`, `dial_setup` → `correction_setup`).

## Phased build plan

Each phase is a feature branch + PR per the standing rule. Each
phase has a runtime exit criterion, not a "looks right in the
diff" criterion.

### Phase 0 — TLS + skeleton (1.5 days)

**Goal:** open `https://jts.local/correction/` on iPhone Safari,
after one-time cert trust, see "Hello mic" page with a working live
mic level. Nothing else.

Concrete changes:
- `deploy/install.sh`: generate/preserve the private CA at
  `/var/lib/jasper/ca/ca.{crt,key}`; issue the correction server cert
  into `/etc/nginx/ssl/jts.local.{crt,key}`; copy the root certificate
  to `/usr/share/jasper-web/jts-root-ca.crt` for download.
- `deploy/nginx-jasper.conf`: add `listen 443 ssl` server block;
  `location /correction/ { proxy_pass http://127.0.0.1:8770/; }`;
  `location /jts-root-ca.crt { ... }` on **port 80** (chicken-
  and-egg: user has to download CA before HTTPS works); serve
  `http://jts.local/correction/` as the plain-HTTP preflight before
  the HTTPS measurement UI.
- `jasper/web/correction_setup.py`: minimal handler returning a
  static "Hello mic" page that requests `getUserMedia({audio: ...})`
  and shows a level meter via AudioWorklet.
- `jasper/web/__main__.py`: register port 8770.
- `BRINGUP.md` Phase Z: document the iOS trust dance.

**Exit criterion (must verify on iPhone, not just types):**
1. `curl -k https://jts.local/correction/` returns the correction page.
2. Open in iPhone Safari (after cert trust): page loads, mic
   permission prompt appears on first interaction.
3. After granting: level meter responds to voice within ~50 ms.
4. **Read back `getUserMedia` track settings** in the page — verify
   `sampleRate === 48000`, `echoCancellation === false`,
   `noiseSuppression === false`, `autoGainControl === false`.
   Show a red banner if any constraint didn't take effect. (This
   is the load-bearing iOS Safari verify step the sanity-check
   pass flagged.)

### Phase 1 — Vertical slice: 1 position, PEQ, end-to-end (3 days)

**Goal:** Jasper sits on the couch, hits "Measure," hears the sweep,
sees a chart, taps "Apply," next song plays through corrected DSP.
5-minute YouTube demo recordable.

Concrete changes:
- `jasper/correction/coordinator.py`: `measurement_window()` async
  context manager. Calls `systemctl stop` on all music source daemons
  that can write into fan-in. Sends `MEASURE_PAUSE` over UDS to voice_daemon.
  On exit (including exceptions): sends `MEASURE_RESUME`,
  `systemctl start ...`.
- `jasper/voice_daemon.py`: handle `MEASURE_PAUSE` / `MEASURE_RESUME`
  in `_handle_command()`. Set `self._measurement_active = asyncio.Event()`.
  WakeLoop's main loop awaits `not self._measurement_active.is_set()`
  before pulling each audio chunk. TtsVolumeTracker checks the
  event before each `playback_rms` poll. Ducker.duck() is a no-op
  when set.
- `jasper/correction/sweep.py`: in-house NumPy/SciPy synchronized
  swept-sine, 10 s, 20 Hz - 20 kHz, -12 dBFS, S16_LE WAV output.
  Cache on disk — it's deterministic.
- `jasper/correction/playback.py`: shell out to
  `aplay -D correction_substream sweep.wav`. Wait for completion.
- `jasper/correction/deconv.py`: take phone-uploaded WAV + sweep
  metadata, perform regularized FFT inversion → mono float32 IR.
- `jasper/correction/analysis.py`: 1/48-octave magnitude smoothing
  → JSON-serializable curve (frequency, dB).
- `jasper/correction/peq.py`: greedy peak-fit on 20-350 Hz residual
  vs target. ≤5 PEQ filters. Cuts only. Q ∈ [1.0, 8.0]. Max -10 dB.
- `jasper/correction/camilla_yaml.py`: build a new pipeline that
  inserts the PEQ filter chain BEFORE the existing `master_gain`
  mixer. Preserves the master_gain placeholder so future revisions
  don't conflict. Writes to
  `/var/lib/camilladsp/configs/correction_<ts>.yml` via ruamel.yaml.
- Extend `jasper/camilla.py` `CamillaController` with:
  - `set_config_path(path: str) -> bool` — calls
    `c.config.set_file_path(path)` then `c.general.reload()`.
  - `reload() -> bool` — bare reload of current config path.
- `jasper/web/correction_setup.py`: full route table from Decision 2.
- Frontend: stdlib-served HTML in `jasper/web/correction_setup.py`.
  Vanilla JS + AudioWorklet captures into Int16, accumulates, and
  posts WAV blobs. The canvas chart shows measured / target /
  predicted curves. "Apply Correction" button → `POST /apply`.
- Frontend: Wake Lock during sweep (`navigator.wakeLock.request('screen')`).
- Frontend: "Rotate your phone 180°, lay flat, no case" instruction
  screen (WiiM RoomFit UX pattern).

**Exit criterion:**
1. Tap Measure → sweep audible at the speaker, no music interruption
   beyond the planned pause.
2. Capture upload completes within 2 s of sweep end.
3. Magnitude chart renders within 5 s.
4. Tap Apply → CamillaDSP swaps config without audio dropout (verify
   by playing music continuously across the apply boundary;
   `aplay -D correction_substream white_noise.wav` is the easiest
   no-streaming-service way to verify mid-stream).
5. Re-running Measure shows a different curve (filter actually
   reaches the speaker).
6. **Manual A/B verification:** play a familiar bass-heavy track
   before/after; the modal peak audibly tightens.

### Phase 2 — Multi-position sweeps + verify pass (3 days)

- 5-position UI with diagrams. Shipped implementation does
  power-mean magnitude averaging everywhere; true moving-mic MMM and
  vector-below-transition averaging are future refinements.
- "Re-measure" step at end; before/after overlay on same chart.
- Robust error states: mic permission denied, sample-rate mismatch
  (force-reject), sweep clipping detected (rerun with -3 dB lower),
  ambient too loud (>50 dB pre-sweep, prompt user).
- House-curve preset slider (warm / neutral / bright) interpolating
  Flat ↔ Harman.
- Calibrated external mic metadata and correction curve applied when
  available; built-in phone mic compensation is a degraded fallback,
  not the high-confidence path.

### Phase 3 — Power-user pass-through (1.5 days)

- `apt install camillagui-backend` or unpack v0.7.x bundle.
- Configure `/etc/camillagui/camillagui.yml` to point at
  `127.0.0.1:1234` (CamillaDSP) and the same statefile we use.
- New systemd unit `jasper-camillagui.service`.
- nginx `location /camilla/ { proxy_pass http://127.0.0.1:5000/; }`
  in the 443 server block.
- Nav link from `/correction/` → `/camilla/` "Power user mode."
- Statefile coordination: measurement coordinator is the only
  writer; camillagui reads + suggests.

### Phase 4 — REW import/export and measurement interop (PARTIAL)

- ✅ `.frd` export (REW-compatible: `Hz dB phase`, 1/48-oct
  underlying). Current bundle curves are magnitude-only, so exported
  phase is explicitly `0.0` and the header says so.
- ✅ REW `.txt` export with frequency + magnitude columns.
- ✅ `.wav` IR export (mono float32, unnormalized by default) for REW
  / external FIR tooling. IRs are recomputed from raw capture WAVs and
  `sweep_meta`, not copied from a stale cache.
- ✅ Bundle inspect/replay CLI:
  `jasper-correction-bundle inspect <bundle> [--recompute]` and
  `jasper-correction-bundle export <bundle> --output <dir>`.
- Document the round-trip workflow: measure here → export `.frd` →
  open in REW → REW's CamillaDSP YAML export (V5.20.14+) → upload
  back at `/camilla/`.
- Keep calibration lookup provider adapters narrow and observable.
  Dayton and miniDSP serial lookup already landed in Phase 2.3; Phase
  4 should preserve manual upload as the fallback and focus on import /
  export paths, provenance, and clear operator errors when vendor
  endpoints drift.

### Phase 5 — Filter sophistication (research-gated, 1 GB-aware)

- Rung 2: CamillaDSP convolution import/export, bundle storage,
  latency reporting, and headroom accounting.
- Rung 3: short minimum-phase FIR for magnitude correction under the
  same conservative target / boost rules as PEQ. Prototype internal
  SciPy-based design and compare against proven tools before deciding
  the shipped generator.
- Rung 4: frequency-dependent-windowed FIR after evaluating DRC-FIR,
  rePhase, REW, CamillaDSP-native workflows, and CamillaFIR if
  verified as a concrete current reference.
- Rung 5: mixed-phase / excess-phase correction. Opt-in only, guarded
  by measurement quality, latency profile, and pre-ringing audit.
- If any rung needs expensive generation, run it under the existing
  measurement-window pause discipline and refuse to start if
  `/proc/meminfo` shows insufficient free memory after renderers have
  paused.
- Extend the existing `jasper-doctor` correction checks with
  FIR-specific readiness: convolution artifact presence, latency /
  headroom accounting, newest measurement timestamp, and last FIR
  generation result.

**1 GB enforcement:** runtime check at filter-design entry.
Surface "this filter type needs 2 GB Pi or more aggressive
process pausing" if fail.

## What we're NOT building (and why)

Scope discipline. Each of these has a real-world reason; if you're
about to add one mid-phase, stop and re-read.

- **Real-time during-sweep visualization.** Not in V1 (sanity-check
  pass agreed). The post-hoc chart is enough to demo and tune. Adds
  WS dependency, AudioWorklet → main-thread streaming, frame-rate
  decisions. Defer to a hypothetical V3.
- **LLM critique layer.** Not in V1 or V2. Deterministic safety
  checks (max-boost, max-Q-vs-frequency, phase-coherence) cover the
  failure modes. Adds user-supplied API key plumbing, prompt eval,
  cost. Defer to V3+.
- **Manual draggable-PEQ overlay.** Not in V1. The auto-fit handles
  the modal range competently. Manual editing is a power-user feature
  that lives behind the `/camilla/` pass-through anyway. Defer.
- **PWA / service worker.** Not relevant; not building a PWA.
- **Auto-detect Schroeder transition from RT60.** Not in V1. Hard-
  coded 350 Hz boundary aligns with Toole defaults and most living
  rooms. Add a power-user toggle in V2.1 if needed.
- **"Contribute your iPhone profile" community calibration database.**
  Not in V1 or V2. Out of scope for a personal-hobby project.
- **Anything above 350 Hz by default.** Per user feedback (2026-05-09):
  "hold the line on phase one for the lower frequencies." Above-
  Schroeder correction is a deliberate opt-in toggle, not a default,
  per Toole's "treat the room with acoustic treatment, EQ minimally
  above transition" doctrine.
- **Multi-driver crossover / wireless sub.** Not in V2. v6 territory
  per [PLAN.md](../PLAN.md).
- **Bypassing CamillaDSP for the sweep.** Wrong by design — see
  "Audio path" section. The sweep MUST traverse the same chain
  music does, otherwise corrections are unverifiable.

## Things we adopt from the briefs (with attribution)

From the engineering brief:
- CamillaDSP `SetConfig` + `Reload` for atomic hot-swap.
- PEQ-only as v1 default; FIR ladder for power users later.
- 20-350 Hz match range, fixed multi-position sweeps, and a future
  Schroeder-aware vector/power averaging refinement.
- Dual-audience principle: same engine, novice flow + power-user
  export buttons.
- `.frd` export for REW round-trip.
- iPhone built-in mic compensation curve, single bundled curve,
  acknowledge inaccuracy.

From the sanity-check pass:
- Phase 1 = thin vertical slice. Single position, PEQ-only, end-to-end.
- Pin 48 kHz on both ends; reject any AudioContext that didn't
  honor the request.
- `getSettings()` verify that EC/NS/AGC actually got disabled.
- Synchronized swept-sine (Novak 2015) implemented in-house with
  NumPy/SciPy primitives.
- WiiM "rotate phone 180°, lay flat, no case" mic-placement UX.
- Wake Lock during sweep window.
- Reverse-proxy `camillagui-backend` at `/camilla/`.
- Evaluate DRC-FIR, rePhase, REW, CamillaDSP convolution, and
  CamillaFIR if verified before choosing a FIR generator; do not bind
  Phase 5 to one subprocess until a prototype proves the trade-off.
- Drop fft.js — compute RMS in AudioWorklet (32 multiplies, no FFT
  needed for level meter).
- Keep pyfar / pyrato as strong references for future RT60 /
  Schroeder work; add runtime dependencies only when the shipped
  feature needs them.
- In-house canvas chart for the current measured / target /
  predicted curves. Revisit uPlot only if the history or waterfall
  UI needs denser interactive plotting.

## Things we reject from the briefs (with attribution)

From the engineering brief:
- **FastAPI + uvicorn.** Wrong for this codebase — see Decision 2.
  Stdlib `ThreadingHTTPServer` + polling.
- **WebSockets for sweep state.** Not needed; `GET /status` polling
  covers the shipped state surface.
- **HiFiBerry DAC8x assumption.** This speaker uses an Apple USB-C
  dongle; the brief was written without reading our README.
- **PipeWire fanout assumption.** This stack is pure ALSA snd-aloop
  + dmix. The brief's PipeWire coordination paragraphs don't apply.
- **`Speaker Activator` naming.** Project is JTS / Jasper Tech
  Speaker.

From the sanity-check pass:
- **ECharts for V2 waterfall plots.** Premature optimization.
  Stay on the in-house canvas chart until there's a concrete need
  for a plotting dependency.
- **LLM critique layer in V2.** Defer to V3+. Per "What we're NOT
  building."
- **`pyrirtool`, `PORC` references.** Both unmaintained; pyfar +
  pyrato cover the territory.
- **2 GB Pi recommendation.** User wants to see how far 1 GB can
  go. Decision 5 explicitly addresses RAM headroom via process
  pausing.

## Open questions

Honest list. Each needs a decision before the relevant phase ships,
not before this doc lands.

1. **iPhone mic compensation curve source.** HouseCurve doesn't
   publish theirs. Faber Acoustical published older measurements
   (`blog.faberacoustical.com`). Need to either pick one published
   reference (with citation) or measure ours during Phase 2 dev.
   **Decision needed by Phase 2.**
2. **camillagui-backend version pinning.** v0.7.x tracks CamillaDSP
   3.0.x. We'll pin to a specific tag at Phase 3 start to insulate
   against upstream churn. **Decision needed by Phase 3.**
3. **Sweep level for compromised analog volumes.** If the user's
   amp is at very low or very high gain, -12 dBFS digital might
   be too quiet (poor SNR) or too loud (damage risk). Phase 1
   ships -12 dBFS hardcoded; Phase 2 adds the calibration step
   from the brief (play 1 kHz tone, ask user to set comfortable
   loudness, persist as the measurement reference level).
4. **What does the openWakeWord pause actually look like?**
   ([jasper/voice_daemon.py](../jasper/voice_daemon.py) is large;
   need to grep `openwakeword` and identify the right gate point.)
   **Decision needed early in Phase 1.**
5. **AEC bridge interaction.** If the bridge is enabled, does the
   sweep through the music chain become an AEC reference and drive
   the bridge into a weird state during measurement? Two paths:
   (a) test it (most likely fine, bridge re-converges in ~200 ms);
   (b) explicitly stop `jasper-aec-bridge.service` during measurement.
   **Decision needed by Phase 1 exit; default to (b) defensively.**
6. **Where do correction profiles persist?** **Resolved (Phase 2.1):**
   each apply emits a new file at
   `/var/lib/camilladsp/configs/correction_<session_id>_<unixtime>.yml`.
   Files are never deleted by JTS — they're cheap (a few KB), and a
   future "restore previous correction" feature can pick from this
   directory. The currently-loaded path is read via `CamillaController.
   get_config_file_path()` and surfaced to the UI banner via
   `parse_current_correction()`.
7. **What does "Reset to flat" do?** **Resolved (Phase 1+2.1):**
   `set_config_file_path('/etc/camilladsp/outputd-cutover.yml')` +
   `reload()` on this branch. Also automatically invoked at the start
   of every measurement (so sweeps capture the raw room, not the
   corrected pipeline). Reset is also exposed from the page banner so a
   user can clear the speaker without running a measurement.

## Risk register

What can actually go wrong, ordered by likelihood × impact.

1. **iOS Safari `echoCancellation: false` constraint silently
   ignored.** Real per WebKit Bug 179411. Mitigation: read back
   `getSettings()` after `getUserMedia()`, show red banner if
   not honored. Documented in Phase 0 exit criterion.
2. **AudioContext sample rate locks to 44.1 kHz on Bluetooth
   headset connect.** Real per WebKit Bug 274507 (iPadOS 17.5
   regression, fixed in later releases but still latent).
   Mitigation: re-check `audioContext.sampleRate` immediately
   before sweep start; bail if changed. Documented in Phase 1
   sweep_capture.ts.
3. **CamillaDSP YAML emit corrupts the master_gain placeholder
   and breaks ducking.** Mitigation: round-trip test
   ([tests/test_correction_camilla_yaml.py](../tests/test_correction_camilla_yaml.py))
   that loads our emitted YAML, runs the existing
   [test_camilla_ducker.py](../tests/test_camilla_ducker.py) tests
   against it.
4. **measurement_window() leaves a music source daemon in a stopped
   state on crash.** Mitigation: `try/finally` in coordinator;
   systemd restart policies on the renderers; explicit
   `jasper-doctor` checks for source-daemon health.
5. **iOS user gives up on cert trust dance.** Mitigation: extremely
   clear onboarding instructions (screenshots, not just text) on
   the port-80 landing page. Cert download served at HTTP-only URL
   so user can land there before HTTPS works.
6. **WakeLoop deadlock on the measurement_active event during
   exception.** Mitigation: voice_daemon's MEASURE_RESUME is
   idempotent and is also called from a 2-minute-after-PAUSE
   safety timer (server-side) in case the coordinator crashes
   without sending RESUME.
7. **Filter design exceeds RAM on 1 GB after pause.** Mitigation:
   pre-flight `/proc/meminfo` check; refuse with clear message;
   suggest 2 GB upgrade. Don't OOM the Pi.

## Hardware test checklist

These items can only be verified on real hardware. Deploy with
`bash scripts/deploy-to-pi.sh`, then run on the Pi:

### Phase 0 (TLS + skeleton)
- [ ] `systemctl is-active jasper-correction-web.socket` → `active`
      (the service itself may be inactive when idle; socket
      activation is the liveness contract).
- [ ] `jasper-doctor` reports `correction web`, `correction state
      dirs`, and `current correction` as ok/warn with no fail.
- [ ] `curl http://jts.local/correction/` returns the preflight page.
- [ ] `curl -k https://jts.local/correction/healthz` → `ok`.
- [ ] `nginx -t` → ok.
- [ ] On iPhone after cert trust: page loads with no "Connection
      not private" warning; mic permission prompt appears on first
      tap; constraint table reads `✓ ok` for all 5 rows.
      **Critical**: if `echoCancellation` / `noiseSuppression` /
      `autoGainControl` read `✗ bad`, that's an iOS Safari version
      regression we have to work around — file an issue.

### Phase 1 (single-position end-to-end)
- [ ] Tap **Run measurement** → music pauses, sweep audible at the
      speaker, completes in ~10 s, no audio glitch when renderers
      come back. Watch `journalctl -u jasper-voice` for
      `MEASURE_PAUSE` / `MEASURE_RESUME` events.
- [ ] AudioWorklet capture uploads cleanly (browser network tab
      shows POST /upload-capture with audio/wav body).
- [ ] Chart renders within ~5 s of upload; PEQ list shows 0-5
      filters with reasonable freq/Q/gain.
- [ ] Tap **Apply** → CamillaDSP swaps config without audio dropout
      (verify by playing music continuously across the apply
      boundary, e.g. `aplay -D correction_substream white_noise.wav`
      in another shell).
- [ ] **Audibility check**: play a familiar bass-heavy track
      before/after Apply — modal peak should audibly tighten.
- [ ] Tap **Reset to flat** → CamillaDSP rolls back to outputd-cutover.yml
      cleanly.
- [ ] AEC bridge interaction (if enabled): no permanent drift after
      a measurement; bridge re-converges in ~200 ms.

### Phase 2 (multi-position + verify)
- [ ] 5-position flow: NEEDS_NEXT_POSITION prompt visible after
      each capture; **Continue** advances to next sweep with
      ~3-5 s of dead air (renderer pause/restart cycle).
- [ ] Move phone between positions; verify the prompt shows the
      correct position number.
- [ ] After 5 positions: PEQ design produces a result; chart shows
      the AVERAGED measured curve (not the last-position one).
- [ ] **Verify with re-measurement** after Apply: new measurement
      runs, purple dashed curve overlays on chart, RMS / max
      deviation summary appears.
- [ ] Verify deviation should be SMALLER post-correction than
      pre-correction was. If it isn't, the correction didn't work
      — check journals for clues.
- [ ] Target choice: flat vs warm produces different PEQ sets and
      audibly different results.

### Phase 2.4 (observability + quality)
- [ ] Deliberately quiet capture warns in the UI and bundle instead
      of silently producing a high-confidence chart.
- [ ] Deliberately clipped capture blocks analysis, shows
      "Measurement blocked" in the UI, and leaves `capture_quality`
      in `info.json`.
- [ ] `curl -sk https://jts.local/correction/sessions | jq` shows
      `has_result`, calibration artifact flags, and the latest
      `capture_quality` fields.
- [ ] `jasper-doctor` reports the latest bundle and warns when the
      newest completed measurement had no calibrated mic.

### Things to watch for in journals
- `journalctl -u jasper-correction-web -f` — measurement state
  transitions, handler exceptions.
- `journalctl -u jasper-voice -f` — `MEASURE_PAUSE` / RESUME
  events; auto-clear safety-timer warning if a coordinator crashes.
- `journalctl -u jasper-camilla -f` — config reload events;
  any parse errors on the emitted YAML.

## Debug bundles — operator reference

Every measurement session writes a self-contained bundle at
`/var/lib/jasper/correction/sessions/<session_id>/`. Layout:

```
sessions/<session_id>/
├── info.json        session params, target_choice, autolevel state,
│                    noise_floor_db, peqs, timestamps,
│                    input_device, mic_calibration public metadata,
│                    current_correction_at_start, sweep_meta,
│                    capture_quality, verify_quality (self-identifying
│                    reports with capture_kind / position_index /
│                    artifact_path),
│                    confidence_report, acoustic_quality summary,
│                    runtime_integrity summary,
│                    bundle_schema_version
├── artifact_manifest.json
│                    bundle schema v3 integrity manifest: relative
│                    paths, artifact kinds, schema versions, SHA-256,
│                    byte sizes, generator provenance, dependencies,
│                    sensitivity class, and recomputability
├── result.json      measured / target / predicted curves; verify_curve
│                    + verify_metrics when /verify ran; repeats
│                    input_device, mic_calibration public metadata,
│                    capture_quality, verify_quality, confidence_report,
│                    acoustic_quality summary, runtime_integrity summary
├── acoustic_quality.json
│                    SNR/acoustic trust summary derived from capture
│                    quality + browser-measured noise floor
├── runtime_integrity.json
│                    lightweight runtime snapshots/counters around
│                    measurement and verify sweeps
├── position_analysis.json
│                    per-position curves, spatial spread, confidence
│                    bands, high-variance/deep-null feature flags
├── captures/        per-position WAVs (p0.wav, p1.wav, ...)
├── noise/           pre-sweep silence WAVs (p0_pre.wav, ...)
├── repeat_captures/ optional same-seat repeat WAVs (p0_r1.wav, ...)
├── mic_calibration.json
│                    selected calibration public metadata + parsed curve
├── mic_calibration.txt
│                    exact uploaded/fetched calibration file used
├── verify.wav       single-position re-measurement (if /verify ran)
└── applied.yml      copy of the CamillaDSP config that was applied
                     (so the bundle is self-contained even if the
                     configs/ directory is later cleaned up)
```

`info.json` is rewritten atomically on each state transition and on
failed analysis paths (cheap; a few hundred bytes). `result.json`
lands after design / verify.
`applied.yml` is copied (not symlinked) in `apply()` so the bundle
remains valid after a user-driven cleanup.

### Durable evidence contract

Raw measurement recordings are the canonical origin for a correction
session. Keep `captures/p<N>.wav`, `noise/p<N>_pre.wav`,
`repeat_captures/p0_r<N>.wav`, and `verify.wav` by default and treat
them as private user data: they may contain room noise, speech, or
household sounds around the sweep. Derived artifacts should be
recomputable from:

- raw capture WAVs;
- exact sweep metadata;
- selected mic calibration file and parsed curve;
- browser audio-path report;
- correction algorithm settings and software/build provenance;
- target/strategy choices;
- runtime health snapshots taken around the sweep.

New bundles use schema v3 and write an `artifact_manifest.json` beside
`info.json`. Each artifact entry includes:

- relative path and artifact kind (`raw_capture`,
  `session_metadata`, `analysis_result`, `position_analysis`,
  `mic_calibration_raw`, `mic_calibration_metadata`,
  `camilladsp_config`, etc.);
- artifact schema/version where applicable;
- SHA-256 checksum and byte size;
- manifest record timestamp;
- generator provenance (module/function or tool name);
- input dependencies by artifact path;
- sensitivity class (`private_raw_audio`, `private_metadata`,
  `debug_safe`, etc.);
- whether the artifact is recomputable.

`jasper.correction.bundles.validate_bundle` now validates manifest
shape, missing files, size/checksum drift, missing dependency entries,
runtime-integrity issues, acoustic-quality issues, and current-schema
bundles that still rely only on filename conventions.

### Schema and version compatibility

The bundle contract is intentionally versioned without introducing
heavyweight JSON Schema yet. Consumers should branch on explicit
version fields and feature presence, not inferred filenames.

Current versions:

| Artifact | Version field | Current value | Compatibility expectation |
|---|---:|---:|---|
| `info.json` | `bundle_schema_version` | `3` | Required for bundle identity/state. New optional summaries may appear; older bundles may omit newer fields. |
| `result.json` | `bundle_schema_version` | `3` | Optional until a session reaches `ready` / `applied` / `verified`. Consumers must tolerate absence on failed or in-flight bundles. |
| `artifact_manifest.json` | `manifest_schema_version` | `1` | Required for new schema-v3 bundles. Legacy bundles without it may be inspected but are lower trust. |
| `runtime_integrity.json` | `artifact_schema_version` | `1` | Optional derived evidence. Missing means runtime evidence unavailable, not that the sweep was healthy. |
| `acoustic_quality.json` | `artifact_schema_version` | `1` | Optional derived evidence. Missing means SNR/repeatability evidence unavailable, not invalid. |
| `analysis/<capture>_response.json` | `artifact_schema_version` | `1` | Optional derived replay artifact. Recomputable from raw capture WAV, sweep metadata, calibration, and deconvolution settings. |
| `fir/<label>.json` | `artifact_schema_version` | `1` | Optional FIR-runtime metadata for imported/staged coefficients. This is evidence only, not an apply path. |
| `jasper.correction.evidence` packet | `artifact_schema_version` | `2` | Read-only review envelope for humans and future LLMs; no side effects and no raw audio. v2 adds `capability_permissions` and `missing_evidence`. |
| `jasper.calibration_agent.advisor_context` packet | `artifact_schema_version` | `1` | Redacted LLM-ready context envelope derived from the evidence packet. Excludes raw audio, absolute paths, raw serials, untrusted browser labels, and user-entered profile names; carries explicit read-only advisor permissions/prohibitions. |

Compatibility rules:

- Treat `info.json` as the minimum bundle identity surface. It must
  contain `session_id`, `state`, and `bundle_schema_version` for a
  bundle to be useful.
- Treat `result.json`, `runtime_integrity.json`,
  `acoustic_quality.json`, `analysis/*`, and `fir/*` metadata as
  optional capability surfaces. Missing derived evidence should lower
  confidence and guide remeasurement, not crash report rendering.
- Treat `artifact_manifest.json` as the integrity surface for new
  bundles. If it is present, validate checksums, sizes, dependency
  paths, sensitivity classes, and artifact schema versions before
  trusting derived artifacts.
- Do not expose raw WAVs in browser report surfaces. Raw recordings
  are private evidence and stay in `captures/`, `noise/`,
  `repeat_captures/`, and `verify.wav` for CLI/operator workflows.
- When bumping any version, keep the old reader path long enough for
  copied-off bundles from previous releases to produce a useful
  "limited evidence" report.

Replay-grade `analysis/` artifacts are deliberately compact and
file-based. They currently include per-capture deconvolved impulse
responses, window/deconvolution settings, magnitude response before
display smoothing, 1/48-octave smoothed response, calibration-applied
analysis curve, and the normalization band. Raw capture WAVs remain the
canonical origin. Future additions that require phase/group-delay or
complex transfer functions should extend this artifact family rather
than teaching browser routes to recompute DSP facts.

Runtime health is lightweight and bounded, not a new monitoring daemon.
`runtime_integrity.json` records a small per-measurement health packet:
monotonic/wall-clock timing, CPU/load and memory snapshots, CamillaDSP
config/status where available, fan-in xrun deltas, and capture
sample-count sanity. This feeds a separate **runtime integrity**
verdict, distinct from **capture quality** and **acoustic quality**.
Hard capture failures such as clipping, sample-rate mismatch, or
too-short WAV still block analysis; runtime warnings lower confidence
unless they directly prove the recording is invalid.

`acoustic_quality.json` is the current acoustic trust layer. It records
capture waveform summaries, pre-sweep room-noise summaries from
`noise/p<N>_pre.wav`, estimated broadband and modal-band dBFS SNR,
direct-arrival/pre-arrival evidence, and optional same-seat repeat
quality from `repeat_captures/p0_r1.wav`. This is a useful engineering
guardrail, not calibrated acoustic SPL. Low SNR or weak repeatability
lowers confidence and blocks assertive strategy recommendations;
missing evidence keeps read-only review possible but should not unlock
stronger FIR/agent claims. Future hardware validation should tune the
thresholds against real phone/mic/Pi captures.

Do not introduce a database, unbounded recording retention, or
continuous telemetry for this phase. The browser history panel may
delete old filesystem bundles and clearly labels raw recordings as
private. Keep anything heavier, such as pinning, automatic pruning, or
debug-safe export/share workflows, out of the product until hardware
usage proves they carry their weight.

To pull a bundle off the Pi for debugging:

```sh
ssh pi@jts.local 'ls -t /var/lib/jasper/correction/sessions/ | head -5'
scp -r pi@jts.local:/var/lib/jasper/correction/sessions/<id> ./
```

To list bundles without ssh, hit the debug endpoint:

```sh
curl -sk https://jts.local/correction/sessions | jq
```

To view one read-only browser-safe evidence report:

```sh
curl -sk 'https://jts.local/correction/session-report?id=<id>' | jq
```

To inspect, replay, or export one copied bundle locally:

```sh
jasper-correction-bundle inspect ./<session_id> --recompute
jasper-correction-bundle export ./<session_id> --output ./rew-export
```

To inspect a FIR coefficient WAV without applying it, or stage a safe
imported FIR into a copied bundle as evidence:

```sh
jasper-correction-bundle fir-inspect ./coefficients.wav --mode minimum_phase
jasper-correction-bundle fir-stage ./<session_id> ./coefficients.wav \
  --label imported-minphase --mode minimum_phase
```

The export command writes:

- `<session_id>-measured.frd`, `target.frd`, `predicted.frd`, and
  `verify.frd` where available, with `Hz dB phase` columns. Because
  current bundles store smoothed magnitude curves rather than acoustic
  phase, the phase column is `0.0` and the header says so.
- matching `.txt` files with `Hz dB` columns;
- `<session_id>-p<N>-ir.wav` and `<session_id>-verify-ir.wav` impulse
  responses recomputed from raw captures and exact sweep metadata.

Default ON. Opt out via `JASPER_CORRECTION_SAVE_BUNDLES=0` in
`/etc/jasper/jasper.env` — captures then fall back to the legacy
flat `captures/` directory and no per-session artifacts are written.

## Known limitations / Phase 3+ refinements

- **Window cycles per position.** Each /start, /next-position,
  /verify call opens a fresh measurement_window — renderers
  pause/restart per sweep. ~3-5 s of dead air per position
  transition. Tolerable but not great. Phase 3 could keep the
  window open across multi-position runs (~30 LOC of additional
  state in the session).
- **Strict Schroeder-aware spatial averaging.** Phase 2 does
  power-mean everywhere. Strict implementation would do vector-mean
  below ~350 Hz (preserves phase) and power-mean above. Requires
  keeping complex H(f) per position rather than just magnitude_db
  — a refactor of the analysis pipeline.
- **Built-in phone mic compensation curve.** Not bundled; if the user
  measures with the phone's internal mic, the captured response still
  includes that mic's frequency response. Phase 2.3 adds external
  calibrated-mic ingest and manual calibration upload, but a built-in
  iPhone/Android compensation database is still not shipped. Treat
  phone-internal captures as quick checks, not the trust floor for
  future FIR or agent recommendations.
- **Vendor lookup must fall back cleanly.** Dayton and miniDSP serial
  lookup is intentionally behind provider adapters. If a vendor form
  changes, blocks server-side fetches, or a serial cannot be found,
  the UI should keep the user in flow via manual calibration upload.
- **Concurrent /start protection.** Resolved 2026-05-28. The backend
  atomically reserves `/start` before replacing the session, so a
  second handcrafted request is refused even in the narrow gap before
  the first background sweep task has visibly moved the new session out
  of `IDLE`. It also refuses `/start` while an existing session is
  preparing, sweeping, awaiting capture, awaiting next position,
  analyzing, or verifying.
- **SPL-calibrated room-noise check.** Phase 2.4 catches obviously
  weak captures and logs low RMS / low peak warnings, but it is not
  a calibrated ambient SPL measurement. A loud room can still pass
  the gate if the sweep level is also high; future agent/FIR flows
  should treat `capture_quality` as a floor, not a lab-grade SNR
  proof.

## Cross-session notes

If you're a future Claude or future Jasper picking this up:

- **Read this doc top to bottom first.** The architecture decisions
  encode reasons that would otherwise get re-litigated.
- **Look at the actual codebase before changing the file map.**
  This doc was written after a careful read of [voice_setup.py](../jasper/web/voice_setup.py),
  [_common.py](../jasper/web/_common.py), [control/server.py](../jasper/control/server.py),
  [audio-paths.md](audio-paths.md),
  [outputd-cutover.yml](../deploy/camilladsp/outputd-cutover.yml),
  [camilla.py](../jasper/camilla.py), and [nginx-jasper.conf](../deploy/nginx-jasper.conf).
  If your read disagrees with something here, the code is right
  and this doc is stale — fix the doc.
- **The `master_gain` mixer is the EQ slot.** Don't replace it;
  insert filters in front of it.
- **Phase order matters.** Phase 0 (TLS) is a hard prereq for
  everything else; getUserMedia won't run without it. Phases 1-2
  are the demo. Phases 3-5 are polish.
- **PR per phase.** Don't bundle.
- **Don't introduce FastAPI.** If you find yourself reaching for
  it, re-read Decision 2.
- **Don't add a top-level coordinator daemon.** Re-read Decision 4.
- **The brief and sanity-check pass are inputs, not specs.** This
  doc is the spec. Where they diverge, this doc wins.

## References

External:
- [SciPy](https://scipy.org/) — shipped correction implementation
  uses NumPy/SciPy primitives for sweep/deconvolution/filter design.
- [pyfar](https://pyfar.org) / [pyrato](https://pyrato.readthedocs.io)
  — useful room-acoustics references for future RT60 / Schroeder work;
  not runtime dependencies today.
- [camillagui-backend](https://github.com/HEnquist/camillagui-backend)
  — power-user pass-through (Phase 3).
- [CamillaFIR](https://github.com/VilhoValittu/CamillaFIR) —
  possible FIR / FDW workflow reference; verify current status before
  relying on it for Phase 5.
- [REW](https://roomeqwizard.com) — `.frd` format reference, REW
  YAML export workflow.
- [HouseCurve file formats](https://housecurve.com/docs/manual/file_formats)
  — practical frequency/dB calibration-curve text format.
- [Dayton Audio microphone calibration tool](https://support.daytonaudio.com/MicrophoneCalibrationTool)
  — serial lookup source for Dayton Audio calibration files.
- [miniDSP UMIK-1](https://www.minidsp.com/products/acoustic-measurement/umik-1?format=pdf&type=raw)
  and [UMIK-2 manual](https://www.minidsp.com/images/documents/miniDSP%20UMIK-2-User%20Manual.pdf)
  — serial lookup and orientation-specific calibration-file references.
- Novak et al. 2015, "Synchronized Swept-Sine: Theory, Application,
  and Implementation," J. Audio Eng. Soc. 61.
- Olive 2013, AES 8994 "Listener Preferences for In-Room Loudspeaker
  and Headphone Target Responses."
- Toole, *Sound Reproduction*, 3rd ed.

Internal:
- [README.md](../README.md) — speaker hardware + architecture.
- [PLAN.md](../PLAN.md) — v2 priority context.
- [docs/audio-paths.md](audio-paths.md) — sweep injection point.
- [docs/HANDOFF-volume.md](HANDOFF-volume.md) — VolumeCoordinator
  pattern to mirror.
- [docs/HANDOFF-aec.md](HANDOFF-aec.md) — AEC bridge interaction risk.
- [jasper/camilla.py](../jasper/camilla.py) — CamillaController to
  extend.
- [jasper/web/voice_setup.py](../jasper/web/voice_setup.py) — web
  wizard pattern to mirror.
- [jasper/control/server.py](../jasper/control/server.py) — UDS
  coordinator pattern to mirror.

---

Last verified: 2026-05-29
