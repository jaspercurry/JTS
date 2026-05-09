# HANDOFF — room correction at `/correction/`

> If you are picking this up across sessions: this is the canonical
> planning + design document for the v2 room-correction feature. Read
> the **Status** and **Architecture decisions** sections first. The
> phased plan is the work tracker — when a phase ships, mark it ✅ and
> update the Status. The "Things to ignore" sections are deliberate
> scope discipline, not omissions.

## Status

- ⏳ Not started. Document landed 2026-05-09 from a sanity-check pass
  on two external research briefs. Next action: Phase 0.
- This is **PLAN.md v2** — the highest-value next-feature after the
  v1 voice loop is shipped.

## Goal

A measurement-and-correction loop that runs from an iPhone at the
listening position. Tap a button on `https://jts.local/correction/`,
the speaker plays a sweep, the phone records it, the Pi designs a
PEQ filter set, hot-reloads CamillaDSP, and the next song plays
through the corrected pipeline. Two audiences served by one tool: a
WiiM-Home-style novice flow ("press a button, hear the difference")
and a power-user surface (raw `.frd` exports, CamillaDSP YAML, REW
interop, optional UMIK-2).

Concrete success criterion for v2 ship: Jasper measures from the
couch with an iPhone, the bass mode at his listening position
audibly tightens, the YouTube demo records itself.

## Hardware constraints — load-bearing

These are the facts the design has to honor. Don't redesign around
them; design **with** them.

| Constraint | Source | What it forces |
|---|---|---|
| Raspberry Pi 5 **1 GB** target | User decision (2026-05-09): "see how far we can get on 1 GB" | PEQ + min-phase FIR comfortable; mixed-phase / FDW need aggressive process pausing during filter design (we're already pausing librespot/shairport for measurement — extend through generation). |
| **Apple USB-C dongle**, stereo, 48 kHz | [README.md](../README.md) Hardware table | Filters are 2-channel. No multi-driver crossover work in scope. |
| Pure ALSA: **snd-aloop + dmix**, no PipeWire | [docs/audio-paths.md](audio-paths.md) | Sweep injection point is `plughw:Loopback,0,0` — same point music enters. CamillaDSP captures from `pcm.jasper_capture` (dsnoop on `hw:Loopback,1,0`), processes, writes to `pcm.jasper_out` (dmix on dongle). |
| `master_gain` mixer **already exists** as identity | [deploy/camilladsp/v1.yml:55](../deploy/camilladsp/v1.yml:55) | The EQ slot is reserved. We add filters in front of it, leave it alone. |
| CamillaDSP websocket **no auth, 127.0.0.1 only** | [PLAN.md:281](../PLAN.md:281) | `pycamilladsp` calls stay loopback. Web UI never proxies CamillaDSP WS to the LAN. |
| Volume coordination is **canonical and persistent** | [docs/HANDOFF-volume.md](HANDOFF-volume.md), [jasper/volume_coordinator.py](../jasper/volume_coordinator.py) | Sweep playback should set its own absolute level (not via VolumeCoordinator), restore previous on exit. |
| `Ducker` is **the only writer** to `main_volume` for voice | [jasper/camilla.py:156](../jasper/camilla.py:156) | Measurement coordinator must coexist; voice session during measurement should be impossible (we pause WakeLoop). |
| Existing settings pages on **plain HTTP port 80** | [deploy/nginx-jasper.conf:21](../deploy/nginx-jasper.conf:21) | We add HTTPS as an additive 443 server block. Existing routes stay HTTP. |
| `getUserMedia` **requires HTTPS** (browser policy) | Web spec | Cannot avoid TLS for this one feature. mkcert + iOS trust profile is the path. |
| Existing web wizards are **stdlib `ThreadingHTTPServer`** | [jasper/web/voice_setup.py](../jasper/web/voice_setup.py), [jasper/web/dial_setup.py](../jasper/web/dial_setup.py) | We mirror this — no FastAPI / aiohttp. Stream progress via Server-Sent Events. |
| Cross-daemon coordination is **UDS commands to voice_daemon** | [jasper/control/server.py:185](../jasper/control/server.py:185) `_voice_socket_command()` | We extend with `MEASURE_PAUSE` / `MEASURE_RESUME`, mirror the `/cue/play` shape. |

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

### Decision 2 — Web framework: stdlib `ThreadingHTTPServer` + Server-Sent Events

**Decision:** Build `jasper/web/correction_setup.py` as another
`BaseHTTPRequestHandler` subclass colocated in `jasper-web`,
listening on `127.0.0.1:8770`. Stream sweep / generation progress
via SSE (`Content-Type: text/event-stream`). Audio capture uploads
as a single `POST /upload-capture` with `Content-Length`.

**Why not FastAPI?** Codebase precedent: every existing web wizard
is stdlib `ThreadingHTTPServer`. Adding FastAPI introduces a new
runtime dependency, a new ASGI server (uvicorn), a new systemd
unit shape, and breaks the "one jasper-web process owns all the
settings ports" pattern in [jasper/web/__main__.py](../jasper/web/__main__.py).

**Why not WebSockets?** The original brief assumed bidirectional
WS for "real-time during-sweep visualization." V1 explicitly punts
that (post-hoc viz only). Without it, communication is one-way
push (server → browser progress) plus discrete REST actions
(`POST /start`, `POST /upload-capture`, `POST /apply`,
`GET /events` for SSE). SSE is single-direction, works in stdlib
with three lines, has wider Safari support history than WS.

**Why not aiohttp?** Same reason as FastAPI — new dep, breaks the
pattern. The existing async work in [control/server.py](../jasper/control/server.py)
uses `asyncio.run()` per request to bridge stdlib HTTP into async
coordinator code; we do the same here for the
`measurement_window()` async context manager.

**Concrete shape:**
```
GET  /                       page render (Preact SPA + uPlot, served from /usr/share/jasper-web/correction/dist/)
GET  /jts-root-ca.pem        download mkcert root for iOS trust (HTTP only — chicken-and-egg)
POST /start                  begin measurement session, returns session_id
GET  /events?session=…       SSE stream — sweep_started / sweep_done / analysis_done / filter_ready / applied
POST /upload-capture         multipart, body = WAV blob from AudioWorklet
POST /apply                  body = {session_id} → SetConfig + Reload
POST /reset                  body = {} → restore previous correction or pass-through
GET  /export.frd?session=…   REW-compatible magnitude+phase
GET  /export-yaml?session=…  generated CamillaDSP YAML
```

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

**Decision:** PEQ-only as v1. Min-phase FIR (Rung 2) reachable
within 1 GB. Mixed-phase / FDW (Rungs 4-5) gated behind aggressive
process pausing — extend the measurement window through filter
design, not just measurement. Surface a "this filter type needs
2 GB" message at runtime if `/proc/meminfo` shows < 500 MB free
*after* pausing.

**Why not require 2 GB?** User decision (2026-05-09): "see how
far we can get on 1 GB."

**Concrete RAM budget on 1 GB after pause:** Per the explore-agent
audit of running processes, steady total is 500-620 MB. Pausing
librespot, shairport-sync, bluez-alsa, and the Gemini Live SDK
(via `MEASURE_PAUSE` + `systemctl stop`) frees an estimated
130-200 MB. That's enough headroom for PEQ (negligible),
min-phase FIR at 16k taps (~50 MB peak), even mixed-phase FIR if
we run filter design single-threaded and don't load matplotlib
on the request path.

**Defaults that keep us safely on the safe side:**
- Match range: 20-350 Hz (Toole-aligned modal range only)
- ≤5 PEQ filters
- Cuts only by default (Floyd Toole's "first do no harm")
- Max boost +3 dB (toggle in advanced drawer)
- Max cut -10 dB
- Q range 1.0-8.0
- Overall max boost 0 dB (preserve digital headroom)

These mirror Jasper's known-good REW workflow (per the engineering
brief).

### Decision 6 — Sweep generation: synchronized swept-sine via pyfar

**Decision:** Use `pyfar`'s synchronized swept-sine generator
(Novak 2015), not vanilla Farina ESS. 10 s sweep, 20 Hz - 20 kHz,
-12 dBFS. Inverse filter precomputed and shipped alongside the
sweep WAV.

**Why not vanilla Farina?** Synchronized variant places harmonic
distortion impulses at integer-fraction offsets of the IR, making
them trivial to discard. pyfar implements both; the synchronized
one is what serious tools use in 2024-2026. Same number of lines
of code from our side.

### Decision 7 — Spatial averaging: 5-position MMM, vector below Schroeder, power above

**Decision:** Phase 1 ships single-position. Phase 2 adds 5-position
MMM. Vector-average complex transfer functions below 350 Hz, power-
average magnitudes above. (Standard Toole/Welti/Olive approach;
unchanged since 2017.)

### Decision 8 — Mic compensation: ship one curve, accept the inaccuracy

**Decision:** Bundle a single iPhone built-in-mic compensation
curve (HouseCurve approach). Apply boost compensation below 60 Hz
and above 8-10 kHz. Document explicitly that it's approximate.
Provide UMIK-2 path as the accuracy escape hatch (Phase 4).

**Why not a per-model curve database?** No published cross-model
compensation database exists. HouseCurve and AudioTool both ship
single generic curves. Don't try to be cleverer than the people
selling this for $20.

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
    renderers → hw:Loopback,0,0 → snd-aloop → plughw:Loopback,1,0
              → jasper-camilla (main_volume + filters)
              → pcm.jasper_out (dmix on dongle)
              → dongle → amp → speakers
```

**Sweep injection point: `plughw:Loopback,0,0`.** This puts the
sweep on the same path music takes — through CamillaDSP, through
any active correction filter, to the dongle. So:

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

## File map — new code, mirroring existing patterns

```
jasper/
├── correction/                          NEW SUBPACKAGE
│   ├── __init__.py
│   ├── coordinator.py                   measurement_window() async CM
│   ├── sweep.py                         pyfar synchronized swept-sine + inverse
│   ├── playback.py                      sweep → plughw:Loopback,0,0 via aplay
│   ├── deconv.py                        IR extraction
│   ├── analysis.py                      smoothing, RT60 (via pyrato), Schroeder
│   ├── peq.py                           greedy PEQ design (≤5 filters, cuts)
│   ├── target.py                        Harman / flat / house-curve interpolant
│   ├── camilla_yaml.py                  ruamel.yaml emit; preserves master_gain placeholder
│   ├── exporters.py                     .frd, .wav, REW .txt
│   ├── calibration.py                   iPhone mic comp + UMIK fetch proxy
│   └── data/
│       ├── iphone_mic_comp.csv          single bundled compensation curve
│       └── targets/
│           ├── flat.csv
│           ├── harman.csv
│           └── house_warm.csv
│
├── web/
│   └── correction_setup.py              NEW — mirrors voice_setup.py shape
│                                        ThreadingHTTPServer on 127.0.0.1:8770
│                                        SSE for progress, POST for upload+apply
│
├── voice_daemon.py                      EDIT — add MEASURE_PAUSE / MEASURE_RESUME
│                                        UDS commands; gate WakeLoop +
│                                        TtsVolumeTracker on _measurement_active
│
└── camilla.py                           EDIT — add CamillaController.set_config_path()
                                         and .reload() — ~20 LOC additions

deploy/
├── nginx-jasper.conf                    EDIT — add 443 server block, /correction/
│                                        location only; serve mkcert root at
│                                        http://jts.local/jts-root-ca.pem
├── systemd/
│   └── jasper-correction-init.service   NEW (optional) — mkcert -install on first
│                                        boot if cert missing
└── install.sh                           EDIT — install mkcert, generate cert,
                                         install correction SPA dist, register
                                         port 8770 with jasper-web

frontend/correction/                     NEW — Preact + Vite + uPlot SPA
├── src/
│   ├── App.tsx
│   ├── audio/sweep_capture.ts           AudioWorklet, 48 kHz pinned, RMS in worklet
│   ├── audio/spl_meter.ts
│   ├── components/Chart.tsx             uPlot wrapper
│   └── components/PositionGuide.tsx
├── package.json
├── vite.config.ts
└── dist/                                CHECKED IN — built artifact, served by FastAPI
                                         (no node toolchain on Pi)

docs/
└── HANDOFF-correction.md                THIS FILE

tests/
├── test_correction_sweep.py             NEW — synthesized IR fixtures
├── test_correction_peq.py               NEW — PEQ design on known curves
├── test_correction_yaml_emit.py         NEW — YAML round-trip with master_gain preserved
├── test_correction_coordinator.py       NEW — pause/resume contract
└── fixtures/
    ├── ir_modal_room.wav                synthesized via pyroomacoustics in dev
    └── ir_well_corrected.wav

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
1. `curl -k https://jts.local/correction/` returns the SPA shell.
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
  context manager. Calls `systemctl stop librespot shairport-sync
  bluealsa-aplay`. Sends `MEASURE_PAUSE` over UDS to voice_daemon.
  On exit (including exceptions): sends `MEASURE_RESUME`,
  `systemctl start ...`.
- `jasper/voice_daemon.py`: handle `MEASURE_PAUSE` / `MEASURE_RESUME`
  in `_handle_command()`. Set `self._measurement_active = asyncio.Event()`.
  WakeLoop's main loop awaits `not self._measurement_active.is_set()`
  before pulling each audio chunk. TtsVolumeTracker checks the
  event before each `playback_rms` poll. Ducker.duck() is a no-op
  when set.
- `jasper/correction/sweep.py`: pyfar synchronized swept-sine, 10 s,
  20 Hz - 20 kHz, -12 dBFS, S16_LE WAV output. Cache on disk —
  it's deterministic.
- `jasper/correction/playback.py`: shell out to
  `aplay -D plughw:Loopback,0,0 sweep.wav`. Wait for completion.
- `jasper/correction/deconv.py`: take iPhone-uploaded WAV + known
  inverse filter → mono float32 IR.
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
- Frontend: Preact + Vite + uPlot SPA. AudioWorklet captures into
  Int16, accumulates, posts as WAV blob. uPlot shows measured (red)
  + target (gray dashed). "Apply Correction" button → `POST /apply`.
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
   `aplay -D plughw:Loopback,0,0 white_noise.wav` is the easiest
   no-streaming-service way to verify mid-stream).
5. Re-running Measure shows a different curve (filter actually
   reaches the speaker).
6. **Manual A/B verification:** play a familiar bass-heavy track
   before/after; the modal peak audibly tightens.

### Phase 2 — Multi-position MMM + verify pass (3 days)

- 5-position UI with diagrams (vector avg below 350 Hz, power avg
  above; standard MMM).
- "Re-measure" step at end; before/after overlay on same chart.
- Robust error states: mic permission denied, sample-rate mismatch
  (force-reject), sweep clipping detected (rerun with -3 dB lower),
  ambient too loud (>50 dB pre-sweep, prompt user).
- House-curve preset slider (warm / neutral / bright) interpolating
  Flat ↔ Harman.
- Bundled iPhone mic compensation curve applied automatically.

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

### Phase 4 — REW interop + UMIK (2 days)

- `.frd` export (REW-compatible: `Hz dB phase`, 1/48-oct underlying).
- `.wav` IR export (mono float32, normalized — what CamillaFIR
  consumes).
- REW `.txt` export.
- `CalibrationProxy` — backend `httpx.AsyncClient` GETs UMIK form
  with serial, caches in `/var/lib/jasper/correction/calibrations/`.
- Document the round-trip workflow: measure here → export `.frd` →
  open in REW → REW's CamillaDSP YAML export (V5.20.14+) → upload
  back at `/camilla/`.

### Phase 5 — Filter sophistication (3-5 days, 1 GB-aware)

- Rung 2: minimum-phase FIR for modal range. SciPy `firwin2` +
  Hilbert. ~50 MB peak. Fits 1 GB after pause.
- Rung 4 (mixed-phase): invoke CamillaFIR as subprocess in a cgroup
  with memory limit. Refuse to start if `/proc/meminfo` shows
  < 500 MB free **after** measurement_window pause has freed RAM.
- Rung 5 (FDW): same — CamillaFIR has Adaptive-FDW.
- Add `jasper-doctor` checks: correction subsystem healthy, last
  measurement timestamp, current correction profile.

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
  per [PLAN.md:23](../PLAN.md:23).
- **Bypassing CamillaDSP for the sweep.** Wrong by design — see
  "Audio path" section. The sweep MUST traverse the same chain
  music does, otherwise corrections are unverifiable.

## Things we adopt from the briefs (with attribution)

From the engineering brief:
- CamillaDSP `SetConfig` + `Reload` for atomic hot-swap.
- PEQ-only as v1 default; FIR ladder for power users later.
- 20-350 Hz match range, 5-position MMM, vector below / power above
  Schroeder.
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
- Synchronized swept-sine (Novak 2015) via pyfar.
- WiiM "rotate phone 180°, lay flat, no case" mic-placement UX.
- Wake Lock during sweep window.
- Reverse-proxy `camillagui-backend` at `/camilla/`.
- Treat CamillaFIR as a subprocess for FIR rungs 4-5.
- Drop fft.js — compute RMS in AudioWorklet (32 multiplies, no FFT
  needed for level meter).
- Add `pyrato` (pyfar's room-acoustics sub-package) for RT60 /
  Schroeder.
- uPlot for charts; `pxAlign: 1`, log-scale x-axis via
  `scales.x.distr: 3`.

## Things we reject from the briefs (with attribution)

From the engineering brief:
- **FastAPI + uvicorn.** Wrong for this codebase — see Decision 2.
  Stdlib `ThreadingHTTPServer` + SSE.
- **WebSockets for sweep state.** Not needed; SSE one-way push
  covers it.
- **HiFiBerry DAC8x assumption.** This speaker uses an Apple USB-C
  dongle; the brief was written without reading our README.
- **PipeWire fanout assumption.** This stack is pure ALSA snd-aloop
  + dmix. The brief's PipeWire coordination paragraphs don't apply.
- **`Speaker Activator` naming.** Project is JTS / Jasper Tech
  Speaker.

From the sanity-check pass:
- **ECharts for V2 waterfall plots.** Premature optimization.
  Stay on uPlot until there's a concrete need.
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
7. **Where do correction profiles persist?** Single active profile
   in `/var/lib/camilladsp/configs/correction.yml`, or a profile
   library with timestamps? Phase 1 ships single-profile (overwrite
   on apply, keep one `.bak`). Library UX is a Phase 2 nice-to-have.
8. **What does "Reset to flat" do?** Phase 1: `set_config_path()`
   to a known-good `/etc/camilladsp/v1.yml` (the as-shipped
   passthrough). Verify this works without losing any other
   user-applied filter changes — but since we're the only writer,
   it should.

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
   ([tests/test_correction_yaml_emit.py](../tests/test_correction_yaml_emit.py))
   that loads our emitted YAML, runs the existing
   [test_camilla_ducker.py](../tests/test_camilla_ducker.py) tests
   against it.
4. **measurement_window() leaves librespot/shairport in a stopped
   state on crash.** Mitigation: `try/finally` in coordinator;
   systemd `Restart=always` on the renderers; explicit
   `jasper-doctor` check that all three are running.
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
- [pyfar](https://pyfar.org) — synchronized swept-sine, fractional-
  octave smoothing, filter design primitives.
- [pyrato](https://pyrato.readthedocs.io) — pyfar's room-acoustics
  sub-package: RT60, EDC, Schroeder.
- [camillagui-backend](https://github.com/HEnquist/camillagui-backend)
  — power-user pass-through (Phase 3).
- [CamillaFIR](https://github.com/VilhoValittu/CamillaFIR) — FIR /
  FDW reference + subprocess target (Phase 5).
- [REW](https://roomeqwizard.com) — `.frd` format reference, REW
  YAML export workflow.
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
