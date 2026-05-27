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
  2026-05-11 (#62) + hotfix 2026-05-11. The systemd unit passes
  `--statefile /var/lib/camilladsp/statefile.yml` to CamillaDSP
  and intentionally *omits the positional CONFIGFILE arg*. The
  initial #62 version included the positional v1.yml as a
  "fallback" — which made the whole feature a no-op because
  CamillaDSP overwrites the statefile with the positional path on
  every start when both are given. The hotfix removes the
  positional; `install.sh` instead seeds
  `/var/lib/camilladsp/statefile.yml` with `config_path:
  /etc/camilladsp/v1.yml` on first install so a fresh Pi has a
  config to load. Subsequent `set_config_file_path()` calls from
  the wizard update the statefile in place; future restarts read
  it back. Recovery from a bad correction without hand-editing the
  statefile: add `--no_config` to the ExecStart args, restart, fix
  or re-measure, remove the flag.
- ✅ **Phase 2.1 — current-correction visibility + per-session debug
  bundles.** Merged 2026-05-11.
  - `GET /status` now includes a `current_correction` descriptor
    (`{path, session_id, applied_at_epoch, peq_count}`) parsed from
    `CamillaController.get_config_file_path()`. The page banner at
    the top of `/correction/` reads this on load so the user knows
    what's loaded before measuring.
  - `POST /start` auto-resets CamillaDSP to
    `/etc/camilladsp/v1.yml` BEFORE the first sweep, so every
    measurement traverses the raw room (not the corrected pipeline).
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
- ✅ **Phase 3 — power-user pass-through.** Already shipped as part
  of v1 — `camillagui.service` runs at port 5005, linked from the
  landing page. No additional work required for the originally
  scoped Phase 3.
- ⏳ **Phase 4 — REW interop.** Not started. UMIK/Dayton
  calibration lookup moved earlier into Phase 2.3 because calibrated
  input is prerequisite substrate for both PEQ and future agent/FIR
  work.
- ⏳ **Phase 5 — FIR filter ladder.** Not started.

**Current sequencing note (2026-05-27):** after the latest research
intake, the next room-correction priority is still measurement trust
before more filter types. The multi-position confidence layer and
browser-audio metadata substrate have landed. The remaining near-term
work is acoustic browser smoke testing, SNR/repeatability evidence,
room-correction visualization polish, and FIR readiness validation.
The rationale and source links live in
[`docs/calibration-agent/jts-specific/implementation-ladder.md`](calibration-agent/jts-specific/implementation-ladder.md#2026-05-27-sequencing-update).

**Outstanding Phases 0-2.9 hardware verification** (see "Hardware
test checklist" below) — the math is validated on synthetic IRs;
the integration with real CamillaDSP / iPhone Safari / aplay /
voice_daemon UDS is unverified and is the gating step before
declaring v2 shippable.

## Goal

A measurement-and-correction loop that runs from a phone at the
listening position. Tap a button on `https://jts.local/correction/`,
optionally pick a calibrated USB measurement mic, the speaker plays a
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
| Pure ALSA: **snd-aloop + fan-in + dmix**, no PipeWire | [docs/audio-paths.md](audio-paths.md) | Sweep injection point is `correction_substream`, a dedicated fan-in lane. CamillaDSP captures from `pcm.jasper_capture` (dsnoop on summed `hw:Loopback,1,7`), processes, writes to `pcm.jasper_out` (dmix on dongle). |
| `master_gain` mixer **already exists** as identity | [deploy/camilladsp/v1.yml](../deploy/camilladsp/v1.yml) | The EQ slot is reserved. We add filters in front of it, leave it alone. |
| CamillaDSP websocket **no auth, 127.0.0.1 only** | [PLAN.md](../PLAN.md) | `pycamilladsp` calls stay loopback. Web UI never proxies CamillaDSP WS to the LAN. |
| Volume coordination is **canonical and persistent** | [docs/HANDOFF-volume.md](HANDOFF-volume.md), [jasper/volume_coordinator.py](../jasper/volume_coordinator.py) | Sweep playback should set its own absolute level (not via VolumeCoordinator), restore previous on exit. |
| `Ducker` is **the only writer** to `main_volume` for voice | [jasper/camilla.py](../jasper/camilla.py) + `Ducker` | Measurement coordinator must coexist; voice session during measurement should be impossible (we pause WakeLoop). |
| Existing settings pages on **plain HTTP port 80** | [deploy/nginx-jasper.conf](../deploy/nginx-jasper.conf) | We add HTTPS as an additive 443 server block. Existing routes stay HTTP. |
| `getUserMedia` **requires HTTPS** (browser policy) | Web spec | Cannot avoid TLS for this one feature. mkcert + iOS trust profile is the path. |
| Existing web wizards are **stdlib `ThreadingHTTPServer`** | [jasper/web/voice_setup.py](../jasper/web/voice_setup.py), [jasper/web/dial_setup.py](../jasper/web/dial_setup.py) | We mirror this — no FastAPI / aiohttp. Browser state uses polling today. |
| Cross-daemon coordination is **UDS commands to voice_daemon** | [jasper/control/server.py](../jasper/control/server.py) + `_voice_socket_command` | We extend with `MEASURE_PAUSE` / `MEASURE_RESUME`, mirror the `/cue/play` shape. |

## Architecture decisions

These are the load-bearing decisions. Each has been considered and
the rejected alternatives are recorded so we don't relitigate.

### Decision 1 — TLS via additive nginx HTTPS, mkcert-issued cert

**Decision:** Add `listen 443 ssl` server block to nginx with a
mkcert-issued cert for `jts.local`. Keep the existing port-80 server
unchanged. Document the iOS Settings → General → About → Certificate
Trust Settings dance as a one-time onboarding step in
[BRINGUP.md](../BRINGUP.md) Phase Z (post-install).

**Why not stay HTTP?** `getUserMedia` only works on HTTPS or
localhost. There is no workaround for this in any browser. The
existing GitHub Pages bounce trick worked for Spotify because the
Pi was never the OAuth redirect target — the bounce ran on a
trusted public origin. There is no equivalent trick for live mic
capture; the secure context has to *be* the page running the
JavaScript.

**Why not Tailscale or ngrok?** Both depend on internet + an extra
install on every household device. mkcert is one-time on the Pi,
and the trust profile is one-time per device.

**Why not skip iPhone Safari and use desktop Chrome only?** The
product story is "couch + iPhone." That's the demo. Desktop-only
loses the YouTube hook.

**Concrete steps in Phase 0** to make this real:
- `apt install libnss3-tools` (mkcert prereq for iOS trust)
- Build mkcert binary or `apt install mkcert` if available on Trixie
- `mkcert -install` (creates root CA in `/var/lib/jasper/mkcert/`)
- `mkcert -cert-file /etc/nginx/ssl/jts.local.pem
  -key-file /etc/nginx/ssl/jts.local-key.pem jts.local
  *.jts.local 127.0.0.1`
- Update nginx config: new `server { listen 443 ssl; ... }` block
  with `/correction/` location only. Existing routes stay on 80.
- Serve the root CA at `http://jts.local/jts-root-ca.pem` so the
  user can download + trust on iOS via Safari.
- README + BRINGUP doc on the trust dance.

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

**Concrete shape (as shipped after Phase 2.3):**
```
GET  /                       page render (stdlib HTML + inline AudioWorklet, no SPA)
GET  /healthz                liveness — "ok"
GET  /jts-root-ca.crt        download mkcert root for iOS trust (HTTP only — chicken-and-egg)
GET  /status                 session + currently-loaded correction snapshot
                             ({state, peqs, autolevel, input_device,
                             mic_calibration, target_profile,
                             correction_strategy, design_report,
                             current_correction: {path, session_id,
                             applied_at_epoch, peq_count} | null})
GET  /sessions               debug: 20 most-recent session bundles
GET  /calibration/models     supported calibrated mic providers/models
POST /start                  reset to base config, begin measurement, returns session_id;
                             body: {total_positions, target_choice,
                             strategy_choice?, noise_floor_db?,
                             calibration_id?, input_device?}
POST /next-position          advance to position[N+1] sweep
POST /upload-capture         body = WAV (audio/wav); per-position OR verify capture
POST /calibration/fetch      body: {model, serial, orientation?}; server-side
                             Dayton/miniDSP lookup, normalized + stored
POST /calibration/upload     body: {filename, content, model?, label?,
                             orientation?, sign_convention?}; manual fallback
POST /apply                  → SetConfig(correction_<id>_<unixtime>.yml) + Reload
POST /reset                  → SetConfig(/etc/camilladsp/v1.yml) + Reload
POST /verify                 fresh single-position sweep for the verify pass
POST /test-tone              5-second 1 kHz tone through music chain
POST /autolevel/start        ramp main_volume while tone plays
POST /autolevel/lock         freeze main_volume at current ramp value
POST /autolevel/cancel       abort ramp, restore pre-autolevel volume
```

Browser polls `GET /status` every 500 ms; SSE was considered but never
landed because polling is simpler in stdlib and the latency budget
allows it.

### Decision 3 — URL: `/correction/`, plus entry on the landing page

**Decision:** `https://jts.local/correction/` is the route. The
nginx port-80 landing page at `/usr/share/jasper-web/index.html`
gains a card linking to it (with a note that the first visit will
require trusting the cert).

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
              → pcm.jasper_out (dmix on dongle)
              → dongle → amp → speakers
```

**Sweep injection point: `correction_substream`.** This puts the
sweep on the same path music takes — through jasper-fanin, through
CamillaDSP, through any active correction filter, to the dongle —
without borrowing a renderer's private lane. So:

1. Pre-correction measurement = sweep through current pipeline.
2. Apply candidate filter set.
3. Post-correction measurement = sweep through the new pipeline.

**This is critical:** the sweep MUST go through CamillaDSP.
Otherwise we measure the speaker+room raw, apply a correction,
and never verify it actually changed anything. The previous TTS-
bypass-of-CamillaDSP pattern (TTS → `pcm.jasper_out` directly)
is *wrong* for measurement.

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
│   └── session.py                       bundle writer + measurement state machine
│
├── cli/
│   └── doctor.py                        correction socket / bundle / config checks
│
├── web/
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
└── install.sh                           mkcert, state dirs, unit install

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
- `deploy/install.sh`: install `mkcert` (apt or build); generate
  cert into `/etc/nginx/ssl/`; create root CA at
  `/var/lib/jasper/mkcert/rootCA.pem`; copy to
  `/usr/share/jasper-web/jts-root-ca.pem` for download.
- `deploy/nginx-jasper.conf`: add `listen 443 ssl` server block;
  `location /correction/ { proxy_pass http://127.0.0.1:8770/; }`;
  `location /jts-root-ca.pem { ... }` on **port 80** (chicken-
  and-egg: user has to download CA before HTTPS works).
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

### Phase 4 — REW import/export and measurement interop (2 days)

- `.frd` export (REW-compatible: `Hz dB phase`, 1/48-oct underlying).
- `.wav` IR export (mono float32, normalized) for REW / external FIR
  tooling.
- REW `.txt` export.
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

1. **mkcert availability on Trixie.** Need to verify whether
   `apt install mkcert` works on RPi OS Trixie or if we have to
   build the binary. **Decision needed by Phase 0 start.** If
   build, add to `install.sh` as Go-source compile (~2 min on Pi 5).
2. **iPhone mic compensation curve source.** HouseCurve doesn't
   publish theirs. Faber Acoustical published older measurements
   (`blog.faberacoustical.com`). Need to either pick one published
   reference (with citation) or measure ours during Phase 2 dev.
   **Decision needed by Phase 2.**
3. **camillagui-backend version pinning.** v0.7.x tracks CamillaDSP
   3.0.x. We'll pin to a specific tag at Phase 3 start to insulate
   against upstream churn. **Decision needed by Phase 3.**
4. **Sweep level for compromised analog volumes.** If the user's
   amp is at very low or very high gain, -12 dBFS digital might
   be too quiet (poor SNR) or too loud (damage risk). Phase 1
   ships -12 dBFS hardcoded; Phase 2 adds the calibration step
   from the brief (play 1 kHz tone, ask user to set comfortable
   loudness, persist as the measurement reference level).
5. **What does the openWakeWord pause actually look like?**
   ([jasper/voice_daemon.py](../jasper/voice_daemon.py) is large;
   need to grep `openwakeword` and identify the right gate point.)
   **Decision needed early in Phase 1.**
6. **AEC bridge interaction.** If the bridge is enabled, does the
   sweep through the music chain become an AEC reference and drive
   the bridge into a weird state during measurement? Two paths:
   (a) test it (most likely fine, bridge re-converges in ~200 ms);
   (b) explicitly stop `jasper-aec-bridge.service` during measurement.
   **Decision needed by Phase 1 exit; default to (b) defensively.**
7. **Where do correction profiles persist?** **Resolved (Phase 2.1):**
   each apply emits a new file at
   `/var/lib/camilladsp/configs/correction_<session_id>_<unixtime>.yml`.
   Files are never deleted by JTS — they're cheap (a few KB), and a
   future "restore previous correction" feature can pick from this
   directory. The currently-loaded path is read via `CamillaController.
   get_config_file_path()` and surfaced to the UI banner via
   `parse_current_correction()`.
8. **What does "Reset to flat" do?** **Resolved (Phase 1+2.1):**
   `set_config_file_path('/etc/camilladsp/v1.yml')` + `reload()`.
   Also automatically invoked at the start of every measurement
   (so sweeps capture the raw room, not the corrected pipeline).
   Reset is also exposed from the page banner so a user can clear
   the speaker without running a measurement.

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
- [ ] Tap **Reset to flat** → CamillaDSP rolls back to v1.yml
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
│                    bundle_schema_version
├── result.json      measured / target / predicted curves; verify_curve
│                    + verify_metrics when /verify ran; repeats
│                    input_device, mic_calibration public metadata,
│                    capture_quality, verify_quality
├── captures/        per-position WAVs (p0.wav, p1.wav, ...)
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

To pull a bundle off the Pi for debugging:

```sh
ssh pi@jts.local 'ls -t /var/lib/jasper/correction/sessions/ | head -5'
scp -r pi@jts.local:/var/lib/jasper/correction/sessions/<id> ./
```

To list bundles without ssh, hit the debug endpoint:

```sh
curl -sk https://jts.local/correction/sessions | jq
```

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
- **Concurrent /start protection.** If two clients hit /start
  simultaneously during an in-flight sweep, both sweeps could try
  to run aplay through the loopback. The UI prevents this in
  practice (run-measurement button is disabled during a measurement)
  but a hand-crafted request could trigger it. Mitigation: add a
  lock around the start handler in Phase 3.
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
  [audio-paths.md](audio-paths.md), [v1.yml](../deploy/camilladsp/v1.yml),
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

Last verified: 2026-05-27
