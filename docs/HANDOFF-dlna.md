# HANDOFF — DLNA/UPnP media input (`jasper-dlna`)

**Status**: design (not yet implemented)
**Owner**: Jasper

## Why DLNA, not Google Cast

Google Cast uses hardware-fused device authentication — the
receiver must cryptographically prove it holds a private key burned
into genuine Chromecast SoC silicon, signed by Google's Root CA.
Every commercial sender app (YouTube Music, Spotify, Podcasts)
enforces this via the Cast SDK. No open-source project has solved
this for mobile-app-initiated audio casting:

- **shanocast** (rgerganov) exploits Chrome's
  `enforce_nonce_checking=false` for Chrome tab mirroring only —
  not phone apps.
- **go-cast** (tristanpenman, March 2025) requires certificates
  extracted from a physically rooted Chromecast.
- **Balena Sound** gave up after years of feature requests
  (issues #102, #364, #504, #615).
- **NymphCast** uses its own protocol — phones don't see it.

Google's "Cast for Audio" certification program (GC4A 2.0) is
available only to commercial hardware partners under NDA. No
hobbyist path exists.

**DLNA/UPnP** is the open alternative. It fills the same user need
— cast audio from a phone to the speaker over the LAN — using a
standardised protocol (UPnP AV / DLNA) that any controller app
can speak. Android users install BubbleUPnP (free) or similar;
BubbleUPnP can also bridge Cast-only apps (YouTube Music,
Podcasts) to DLNA endpoints via its local proxy, substantially
closing the Cast gap. Windows has native "Play To" / "Cast to
Device" support. iPhone users already have AirPlay.

**Matter Casting** (Matter spec v1.3, May 2024) is an emerging
open standard. No phone apps support it as a sender yet; no
reference receiver exists for Pi-class hardware. Worth watching
for 2027+; not actionable today.

---

## Status & scope

DLNA/UPnP audio rendering becomes a fifth music source alongside
AirPlay, Spotify Connect, Bluetooth A2DP, and USB Audio Input.
The speaker appears on the LAN as a UPnP Media Renderer; any
DLNA controller app can discover it and stream audio to it.

**In scope**
- UPnP/SSDP advertisement — speaker appears as "JTS" (or
  configured name) in any DLNA controller
- Audio playback of all common codecs (FLAC, MP3, AAC, OGG, WAV,
  ALAC, WMA) via GStreamer
- Audio output to `pcm.jasper_renderer_in` (the renderer dmix) —
  same path as AirPlay, Spotify Connect, Bluetooth, USB Audio
- Latest-source-wins arbitration via `jasper-mux` with UPnP
  `Stop` action as the preemption mechanism
- On/off toggle at `http://jts.local/sources/`
- Enabled by default (zero hardware dependency; network-only)
- Volume coordination: camilla-as-master (same model as AirPlay
  and USB Audio Input)
- State reporting to `jasper-control`'s `/state` endpoint
- `jasper-doctor` health checks
- Structured `event=dlna.*` logging
- Tier 2 resilience (systemd `Restart=always` + rate limiting)

**Out of scope (explicit non-goals)**
- Google Cast protocol compatibility — see "Why DLNA, not
  Google Cast" above
- Video rendering — audio-only; gmrender-resurrect supports
  video but we don't expose it
- DLNA server / media sharing — we are a renderer (target),
  not a server (source)
- UPnP volume push-mode — the sender app's volume slider is
  an upstream trim; the Pi's canonical volume lives on
  CamillaDSP's `main_volume` (same decision as AirPlay, same
  rationale documented in `HANDOFF-volume.md`)
- Multi-room / Snapcast integration — DLNA is single-room
- DLNA controller wizard — the user installs BubbleUPnP or
  equivalent on their phone; no JTS-side UI needed beyond the
  `/sources/` toggle
- Metadata display on the `/system/` dashboard — deferred;
  gmrender exposes track metadata via UPnP but surfacing it
  requires subscribing to `LastChange` events on the
  `AVTransport` service, which is Phase 2 work

## Executive summary

The DLNA source reuses the existing renderer-into-Loopback
pattern. A single external C binary (`gmediarender` from the
gmrender-resurrect project) handles UPnP advertisement, protocol
negotiation, audio decoding (via GStreamer), and ALSA output. A
thin Python sidecar daemon (`jasper-dlna`) monitors gmrender's
UPnP state and publishes it to a state file that `jasper-mux`,
`jasper-control`, and the sources wizard read.

```
Phone (BubbleUPnP / Windows "Play To" / any DLNA controller)
    │
    │ UPnP/SSDP discovery + SOAP media control + HTTP audio fetch
    ▼
gmediarender (UPnP Media Renderer, C binary)
    │
    │ GStreamer → alsasink
    ▼
pcm.jasper_renderer_in (plug → dmix)
    │
    ▼
hw:Loopback,0,0 → CamillaDSP → pcm.jasper_out → speakers
```

The sidecar is structurally identical to `jasper-usbsink`'s
`state_publisher` — it polls gmrender's UPnP `AVTransport`
service at 1 Hz for `TransportState` (PLAYING / PAUSED /
STOPPED), applies hysteresis, and writes
`/run/jasper-dlna/state.json` atomically. No audio passes
through the sidecar; it is a pure observer.

Total RAM when enabled: **~13-20 MB Pss** (gmrender C binary +
GStreamer audio pipeline + Python sidecar). Total RAM when
disabled: **0 MB** (both services stopped, no resident cost).

---

## 1. Technology choice: gmrender-resurrect

[hzeller/gmrender-resurrect](https://github.com/hzeller/gmrender-resurrect)
(1.1k+ stars, actively maintained through 2024+) is a C-based
UPnP/DLNA Media Renderer explicitly designed for Raspberry Pi.

### Why this, not alternatives

| Option | RAM | Verdict |
|---|---|---|
| **gmrender-resurrect** | ~8-15 MB (C + GStreamer audio-only) | Best fit: lightweight, Pi-optimized, ALSA output, headless |
| upmpdcli + mpd | ~30-50 MB (two daemons) | Overkill; mpd is a full music server, not just a renderer |
| NymphCast server | ~20 MB + FFmpeg | Custom protocol; phones can't discover it natively |
| VLC with UPnP | ~80-120 MB | Far too heavy for 1 GB Pi; designed for desktop |

### Package availability

**`gmediarender` is in Debian Trixie arm64.** Version 0.3-1,
maintained by Tobias Frost. 206 kB installed, 69 kB download.

```sh
sudo apt-get install -y gmediarender \
    gstreamer1.0-alsa gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly
```

Runtime deps pulled automatically: `libupnp17t64`, `libglib2.0`,
`libgstreamer1.0-0`. The GStreamer plugin packages cover FLAC,
MP3, AAC, OGG, WAV, ALAC, and WMA decoding. Source-build
fallback (autotools; `libupnp-dev`, `libgstreamer1.0-dev`,
`gstreamer1.0-plugins-base`) only needed if the Trixie version
is inadequate — unlikely since 0.3 matches upstream v0.3.1.

### Key command-line flags

```sh
gmediarender \
    --friendly-name "JTS" \
    --uuid "${JASPER_DLNA_UUID}" \
    --port 49494 \
    --gstout-audiosink alsasink \
    --gstout-audiodevice jasper_renderer_in \
    --logfile /dev/stderr
```

Note: no `-f` flag exists; omitting `--daemon` / `-d` keeps
the process in the foreground (correct for `Type=simple` systemd).

| Flag | Purpose |
|---|---|
| `--friendly-name` | Name shown in DLNA controller apps |
| `--uuid` | Stable UPnP device UUID (generated once at install, persisted) |
| `--port` | HTTP port for UPnP (default 49494; range 49152-65535) |
| `--gstout-audiosink` | GStreamer audio sink element (e.g. `alsasink`) |
| `--gstout-audiodevice` | ALSA device for the sink — routes to our renderer dmix |
| `--interface-name` | Optional: bind to specific NIC (e.g. `wlan0`) |
| `--mime-filter` | Optional: `audio` restricts to audio-only MIME types |

### UPnP services exposed

gmrender-resurrect exposes three standard UPnP services:

| Service | Control URL | Purpose |
|---|---|---|
| `AVTransport:1` | `/upnp/control/rendertransport1` | Play/Pause/Stop, transport state, track metadata |
| `RenderingControl:1` | `/upnp/control/rendercontrol1` | Volume (0-100), mute |
| `ConnectionManager:1` | `/upnp/control/renderconnmgr1` | Protocol info (what formats are supported) |

All are queryable via standard UPnP SOAP actions on
`127.0.0.1:49494` (configurable via `--port`). The sidecar
uses SSDP discovery to find the control URL at startup, so it
adapts if the port changes.

### Known gotcha: ALSA device hold after pause

gmrender does NOT release the ALSA device after a UPnP `Pause`
— only after `Stop`. Most phone apps send Pause, not Stop, when
the user taps pause. This is a non-issue for JTS because all
renderers write to `pcm.jasper_renderer_in` (a dmix), which
allows concurrent access. dmix never blocks other writers.
If JTS ever moves to `hw:` direct output (it won't — dmix is
architectural), this would need a watchdog that sends Stop after
N seconds of pause.

---

## 2. Architecture

### Component diagram

```
                              ┌──────────────────────┐
                              │  jasper-dlna.service  │
                              │  (Python sidecar)     │
                              │                       │
                              │  UPnP SOAP poll (1Hz) │
                              │  → state_publisher    │
                              │  → /run/jasper-dlna/  │
                              │    state.json         │
                              └───────────┬───────────┘
                                          │ reads UPnP
                                          │ AVTransport
                                          ▼
┌──────────────┐   SOAP    ┌──────────────────────────┐   ALSA
│ DLNA         │ ────────► │  gmediarender.service     │ ────────►
│ controller   │           │  (C binary, gmrender)     │           pcm.jasper_
│ (phone app)  │ ◄──────── │                           │           renderer_in
│              │   UPnP    │  port: dynamic (libupnp)  │
└──────────────┘  events   └──────────────────────────┘
                                          ▲
                                          │ UPnP STOP
                              ┌───────────┴───────────┐
                              │  jasper-mux            │
                              │  (preemption)          │
                              └───────────────────────┘
```

### Data flow

1. **Discovery**: gmrender advertises `urn:schemas-upnp-org:device:MediaRenderer:1` via SSDP multicast (UDP 1900). Phone app discovers it.
2. **Session**: Phone sends `SetAVTransportURI` + `Play` SOAP actions. gmrender fetches audio from the URL in the action, decodes via GStreamer, outputs to `alsasink device=jasper_renderer_in`.
3. **State**: jasper-dlna sidecar polls `GetTransportInfo` on gmrender's `AVTransport` control URL every 1 s. Writes `{playing, transport_state, updated_at}` to `/run/jasper-dlna/state.json`.
4. **Mux**: jasper-mux reads `dlna_playing()` from the state file (same pattern as `usbsink_playing()`). On transition to playing, pauses other active sources. On preemption by another source, sends `Stop` SOAP action to gmrender's `AVTransport` control URL.
5. **Volume**: Camilla-as-master. The phone app's volume slider is an upstream trim (handled by gmrender internally); the Pi's canonical `listening_level` lives on CamillaDSP's `main_volume`. No push-mode integration needed.

### Volume model decision

DLNA is **camilla-as-master**, same bucket as AirPlay and USB
Audio Input. Rationale:

- gmrender handles UPnP `RenderingControl` volume internally
  (the phone's volume slider adjusts gmrender's internal gain)
- The Pi's canonical volume is CamillaDSP's `main_volume`
- The sender app's volume slider is an upstream trim, just like
  AirPlay sender volume
- No observer needed — inbound UPnP volume changes stay inside
  gmrender and don't propagate to the coordinator
- This avoids the echo-prevention complexity that Spotify and
  Bluetooth push-mode require

If a future use case requires the phone's DLNA volume slider to
drive `listening_level` (like USB Audio Input's host slider does),
add a `VolumeObserver` that polls `GetVolume` on
`RenderingControl` and feeds
`VolumeCoordinator.observe_source_volume(Source.DLNA, pct)`. The
coordinator's echo-prevention window handles the feedback loop.
Deferred until observed user demand.

---

## 3. Integration points

### 3.1 Source state probe (`jasper/source_state.py`)

Add `dlna_playing() -> bool`:

```python
async def dlna_playing() -> bool:
    try:
        data = _read_json(Path("/run/jasper-dlna/state.json"))
        return bool(data.get("playing", False))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
```

Same fail-soft pattern as `usbsink_playing()`: returns `False`
on any transport error, logged at debug level.

### 3.2 Mux preemption (`jasper/mux.py`)

Add `Source.DLNA` to the `Source` enum. Preemption sends a UPnP
`Stop` action via SOAP POST to gmrender's `AVTransport` control
URL:

```python
async def _pause_dlna(self) -> bool:
    soap_body = DLNA_STOP_ENVELOPE  # AVTransport:1 Stop action
    try:
        resp = await self._http.post(
            self._dlna_control_url,
            content=soap_body,
            headers={"SOAPAction": DLNA_STOP_ACTION},
            timeout=2.0,
        )
        return resp.status_code == 200
    except Exception:
        return False
```

The control URL is discovered at mux startup via SSDP query for
the local gmrender instance, or read from the sidecar's state
file. Escape hatch: `JASPER_MUX_DLNA_PREEMPT=disabled` (same
pattern as `JASPER_USBSINK_PREEMPT`, `JASPER_SHAIRPORT_SUPERVISOR`).

### 3.3 Volume coordinator (`jasper/volume_coordinator.py`)

Add `Source.DLNA` to the `Source` enum. Add to
`_camilla_carries_level()`:

```python
return source in (Source.IDLE, Source.AIRPLAY, Source.USBSINK, Source.DLNA)
```

No `_set_dlna()` dispatcher needed — camilla carries the level,
same as AirPlay.

### 3.4 Renderer aggregation (`jasper/renderer.py`)

Add `dlna_playing()` to `active_renderers()`:

```python
async def active_renderers(self) -> dict[str, bool]:
    spot, ap, bt, usb, dlna = await asyncio.gather(
        spotify_playing(...), airplay_playing(),
        bluetooth_playing(), usbsink_playing(), dlna_playing(),
    )
    return {
        "aplactive": ap, "btactive": bt,
        "spotactive": spot, "usbsinkactive": usb,
        "dlnaactive": dlna,
    }
```

### 3.5 Sources wizard (`jasper/web/sources_setup.py`)

Add `"dlna"` to `VALID_SOURCES`. Toggle maps to
`systemctl enable/disable --now gmediarender.service`.

Availability check: always `True` (no hardware dependency;
DLNA is network-only). The toggle is live as soon as the
package is installed.

### 3.6 Control daemon `/state` (`jasper/control/server.py`)

Add `renderers.dlna` section:

```json
{
  "dlna": {
    "playing": true,
    "transport_state": "PLAYING",
    "updated_at": "2026-05-24T18:30:42.123Z"
  }
}
```

Fail-soft: returns `null` if the state file is missing or the
service is disabled.

### 3.7 Doctor checks (`jasper/cli/doctor.py`)

Two checks:

1. **`check_dlna_renderer()`** — is `gmediarender.service`
   active? Warn if enabled but not running.
2. **`check_dlna_gstreamer()`** — can GStreamer find the
   `alsasink` element? (`gst-inspect-1.0 alsasink`). Catches
   missing GStreamer plugin packages.

### 3.8 Logging

Follow the `event=` structured convention:

```
event=dlna.state state=PLAYING
event=dlna.state state=STOPPED
event=dlna.preempt action=stop reason=preempted_by_spotify
event=dlna.probe_ok control_url=http://...
event=dlna.probe_fail err=connection_refused
```

Filterable via `journalctl -u jasper-dlna | grep 'event=dlna'`.

---

## 4. Systemd services

### 4.1 `gmediarender.service` (the renderer binary)

```ini
[Unit]
Description=DLNA/UPnP Media Renderer (gmrender-resurrect)
After=sound.target network-online.target
Wants=network-online.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=jasper-dlna
Group=audio
EnvironmentFile=-/etc/jasper/jasper.env
EnvironmentFile=-/var/lib/jasper/dlna.env
ExecStart=/usr/bin/gmediarender \
    --friendly-name "${JASPER_DLNA_NAME:-JTS}" \
    --uuid "${JASPER_DLNA_UUID}" \
    --port 49494 \
    --gstout-audiosink alsasink \
    --gstout-audiodevice jasper_renderer_in \
    --mime-filter audio
Restart=always
RestartSec=2
Nice=-10
MemoryHigh=40M
MemoryMax=60M

[Install]
WantedBy=multi-user.target
```

**Key decisions:**
- `Type=simple` (gmrender doesn't do sd_notify; same as
  librespot, shairport-sync)
- `Restart=always` (same rationale as all renderer services:
  clean exits should restart because the DLNA endpoint must
  always be advertised)
- `User=jasper-dlna` (dedicated system user in the `audio`
  group; mirrors `shairport-sync:shairport-sync`)
- `Nice=-10` (same as shairport-sync and CamillaDSP; audio
  paths get scheduling priority)
- `MemoryHigh=40M` / `MemoryMax=60M` (C binary + GStreamer
  audio pipeline; well within the 8-15 MB expected Pss, with
  headroom for GStreamer plugin loading)

### 4.2 `jasper-dlna.service` (the Python sidecar)

```ini
[Unit]
Description=JTS DLNA state monitor
After=gmediarender.service
BindsTo=gmediarender.service
PartOf=gmediarender.service

[Service]
Type=notify
NotifyAccess=main
WatchdogSec=30s
TimeoutStopSec=5s
EnvironmentFile=-/etc/jasper/jasper.env
ExecStart=/opt/jasper/.venv/bin/jasper-dlna
Restart=on-failure
RestartSec=2
RuntimeDirectory=jasper-dlna
RuntimeDirectoryMode=0755
MemoryHigh=24M
MemoryMax=32M

[Install]
WantedBy=multi-user.target
```

**Key decisions:**
- `Type=notify` + `WatchdogSec=30s` (Tier 1+2 resilience;
  sidecar uses `Heartbeat.bump()` on each successful UPnP poll)
- `BindsTo=gmediarender.service` (sidecar lifecycle follows
  gmrender — if gmrender stops, sidecar stops too)
- `PartOf=gmediarender.service` (propagates restart from
  gmrender to sidecar)
- `RuntimeDirectory=jasper-dlna` (creates `/run/jasper-dlna/`
  for the state file; cleaned up on stop)

### 4.3 Lifecycle

```
systemctl enable gmediarender.service   # auto-starts sidecar via BindsTo
systemctl disable gmediarender.service  # stops both
```

The `/sources/` wizard toggle operates on `gmediarender.service`
only; `BindsTo` + `PartOf` propagates to the sidecar
automatically.

---

## 5. install.sh integration

### 5.1 Package installation

```bash
install_dlna_renderer() {
    if [[ -x /usr/bin/gmediarender ]]; then
        echo "  gmediarender already installed"
        return 0
    fi
    if apt-cache show gmediarender >/dev/null 2>&1; then
        apt-get install -y gmediarender
    else
        # Source build fallback
        local tmpdir
        tmpdir="$(mktemp -d)"
        apt-get install -y libupnp-dev libgstreamer1.0-dev \
            gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
            gstreamer1.0-plugins-ugly gstreamer1.0-alsa
        git clone --depth 1 \
            https://github.com/hzeller/gmrender-resurrect.git \
            "${tmpdir}/gmrender"
        (
            cd "${tmpdir}/gmrender"
            autoreconf -fi
            ./configure
            make -j4
            make install
        )
        rm -rf "${tmpdir}"
    fi
    echo "  Installed gmediarender"
}
```

### 5.2 System user creation

```bash
if ! getent group jasper-dlna >/dev/null 2>&1; then
    groupadd -r jasper-dlna
fi
if ! getent passwd jasper-dlna >/dev/null 2>&1; then
    useradd -r -M -s /usr/sbin/nologin -g jasper-dlna \
        -G audio jasper-dlna
fi
```

### 5.3 UUID generation (one-time)

```bash
if [[ ! -f /var/lib/jasper/dlna.env ]]; then
    install -d -m 0755 /var/lib/jasper
    local uuid
    uuid="$(uuidgen)"
    printf 'JASPER_DLNA_UUID=%s\nJASPER_DLNA_NAME=JTS\n' \
        "${uuid}" > /var/lib/jasper/dlna.env
    chmod 0644 /var/lib/jasper/dlna.env
fi
```

The UUID is generated once and persisted. This ensures the
speaker keeps a stable identity across reboots — DLNA
controllers that have "remembered" the speaker will reconnect
without re-discovery.

### 5.4 Unit installation

```bash
install -m 0644 "${REPO_DIR}/deploy/systemd/gmediarender.service" \
    "${SYSTEMD_DIR}/gmediarender.service"
install -m 0644 "${REPO_DIR}/deploy/systemd/jasper-dlna.service" \
    "${SYSTEMD_DIR}/jasper-dlna.service"
systemctl daemon-reload
systemctl enable gmediarender.service
```

### 5.5 GStreamer plugin verification

```bash
if ! gst-inspect-1.0 alsasink >/dev/null 2>&1; then
    apt-get install -y gstreamer1.0-alsa
fi
```

---

## 6. RAM budget

### On a 1 GB Pi 5 (AEC on)

Current baseline: ~330 MB jasper-* + ~80 MB system = ~410 MB.
Headroom: ~200 MB with AEC on.

| Component | Pss estimate | Notes |
|---|---|---|
| gmediarender (C binary) | ~3-5 MB | Idle; libupnp + SSDP |
| GStreamer pipeline (audio-only, playing) | ~5-8 MB | Loaded on first play; plugins lazy-loaded |
| jasper-dlna sidecar (Python) | ~5-8 MB | Minimal: stdlib HTTP client, json, watchdog |
| **Total when playing** | **~13-20 MB** | |
| **Total when idle** | **~8-13 MB** | GStreamer pipeline not yet instantiated |
| **Total when disabled** | **0 MB** | Both services stopped |

**Headroom after DLNA on 1 GB Pi**: ~180-190 MB with AEC on.
Acceptable; leaves room for GStreamer plugin loading spikes and
the OS page cache.

### Comparison with existing sources

| Source | RAM (Pss) | Language |
|---|---|---|
| shairport-sync (AirPlay) | ~12-18 MB | C |
| librespot (Spotify) | ~25-35 MB | Rust |
| bluealsa-aplay (BT) | ~8-12 MB | C |
| jasper-usbsink (USB) | ~18-22 MB | Python |
| **DLNA (proposed)** | **~13-20 MB** | **C + Python sidecar** |

DLNA fits squarely in the middle of the existing source costs.

---

## 7. Sidecar implementation (`jasper-dlna`)

### 7.1 Entry point (`jasper/cli/dlna_main.py`)

Mirror `jasper/cli/usbsink_main.py`: parse env, instantiate
daemon, install signal handlers, run event loop.

```
pyproject.toml [project.scripts]
    jasper-dlna = "jasper.cli.dlna_main:main"
```

### 7.2 Daemon class (`jasper/dlna/daemon.py`)

Follows the `UsbSinkDaemon` pattern:

```python
@dataclass
class DlnaConfig:
    state_path: str = "/run/jasper-dlna/state.json"
    control_url: str = "http://127.0.0.1:8780"
    poll_interval_sec: float = 1.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "DlnaConfig": ...
```

Subsystems (started in order, cleaned up in reverse):
1. UPnP discovery (find gmrender's `AVTransport` control URL)
2. Heartbeat (`jasper.watchdog.Heartbeat`)
3. State publisher task (1 Hz poll → state.json)

No audio bridge, no preempt listener, no volume bridge. The
sidecar is a pure observer.

### 7.3 UPnP discovery

At startup, the sidecar discovers the local gmrender instance
by querying SSDP for `urn:schemas-upnp-org:device:MediaRenderer:1`
on localhost. It parses the device description XML to extract the
`AVTransport:1` control URL.

If gmrender isn't running yet (sidecar started first due to
systemd ordering race), retry with exponential backoff up to
30 s, then let the watchdog restart the sidecar.

### 7.4 State publisher (`jasper/dlna/state_publisher.py`)

Mirror `jasper/usbsink/state_publisher.py`:

- Poll gmrender's `GetTransportInfo` SOAP action every 1 s
- Extract `CurrentTransportState`: `PLAYING`, `PAUSED_PLAYBACK`,
  `STOPPED`, `NO_MEDIA_PRESENT`
- Apply hysteresis: 1 s active debounce, 2 s inactive debounce
  (same timings as usbsink)
- Write `/run/jasper-dlna/state.json` atomically (tempfile +
  `os.replace`)

State file schema:

```json
{
  "playing": true,
  "transport_state": "PLAYING",
  "updated_at": "2026-05-24T18:30:42.123Z"
}
```

### 7.5 Heartbeat

Call `heartbeat.bump()` after each successful UPnP poll. If
gmrender becomes unreachable (network partition, crash), polls
fail, bump stops firing, systemd's `WatchdogSec=30s` expires
and restarts the sidecar. When gmrender comes back (via its
own `Restart=always`), the sidecar rediscovers it.

---

## 8. Resilience

### Tier 1+2: systemd watchdog (sidecar)

The sidecar uses `jasper.watchdog.Heartbeat` with the standard
5 s stale threshold / 10 s heartbeat interval. `WatchdogSec=30s`
on the service unit. Same pattern as `jasper-voice`,
`jasper-aec-bridge`, `jasper-usbsink`.

### Tier 2: restart policy (gmrender)

gmrender uses `Restart=always` + `StartLimitBurst=5` /
`StartLimitIntervalSec=60`. Same pattern as `librespot.service`
and `shairport-sync.service`.

### Tier 3: protocol supervisor (deferred)

If gmrender develops a protocol-level wedge pattern (alive but
not accepting new sessions — similar to shairport-sync's AP2
wedge), add a supervisor modeled on `shairport_supervisor.py`:

- Probe: UPnP `GetTransportInfo` SOAP action (already in the
  sidecar's poll loop)
- Gate: only restart if `TransportState != PLAYING`
- Rate limit: one restart per 600 s
- Escape hatch: `JASPER_DLNA_SUPERVISOR=disabled`

Deferred until the wedge pattern is observed in production.
gmrender-resurrect is more mature than shairport-sync's AP2
implementation and may not exhibit this failure mode.

### Failure modes

| Failure | Detection | Recovery |
|---|---|---|
| gmrender crashes | systemd `Restart=always` | Automatic restart within 2 s; sidecar rediscovers via `BindsTo` restart cascade |
| gmrender wedges (alive, not responding) | Sidecar UPnP poll fails; `playing` stays false; no user-visible effect unless mid-session | Sidecar watchdog restarts sidecar; if gmrender is truly wedged, Tier 3 supervisor (when wired) restarts it |
| Network drops (WiFi blip) | gmrender loses SSDP advertisement; phone can't discover speaker | gmrender re-advertises on network return; no restart needed |
| GStreamer plugin missing | gmrender starts but can't decode audio; GStreamer logs error | `jasper-doctor` `check_dlna_gstreamer()` catches at install time; runtime: gmrender sends UPnP error response to controller |
| Phone sends unsupported codec | GStreamer pipeline fails to negotiate | gmrender returns UPnP `AVTransport` error; phone app shows error; other codecs still work |
| ALSA device unavailable | gmrender can't open `jasper_renderer_in` | Renderer dmix is always available (snd-aloop + dmix are always loaded); if somehow gone, systemd restart retries |

---

## 9. File map

### New files

| Path | Purpose | LoC est. |
|---|---|---|
| `jasper/dlna/__init__.py` | Package | ~5 |
| `jasper/dlna/daemon.py` | Sidecar daemon lifecycle | ~120 |
| `jasper/dlna/state_publisher.py` | UPnP poll + state.json writer | ~150 |
| `jasper/dlna/upnp.py` | SSDP discovery + SOAP action helpers | ~120 |
| `jasper/cli/dlna_main.py` | Entry point | ~40 |
| `deploy/systemd/gmediarender.service` | Renderer unit | ~25 |
| `deploy/systemd/jasper-dlna.service` | Sidecar unit | ~25 |
| `tests/test_dlna_state.py` | State publisher tests | ~100 |
| `tests/test_dlna_upnp.py` | UPnP discovery + SOAP tests | ~80 |
| `docs/HANDOFF-dlna.md` | This doc | ~500 |

### Modified files

| Path | Change |
|---|---|
| `jasper/source_state.py` | Add `dlna_playing()` probe |
| `jasper/mux.py` | Add `Source.DLNA`, preemption via UPnP STOP |
| `jasper/volume_coordinator.py` | Add `Source.DLNA` to enum + `_camilla_carries_level()` |
| `jasper/renderer.py` | Add `dlna_playing()` to `active_renderers()` |
| `jasper/web/sources_setup.py` | Add `"dlna"` toggle |
| `jasper/control/server.py` | Add `renderers.dlna` to `/state` |
| `jasper/cli/doctor.py` | Add `check_dlna_renderer()`, `check_dlna_gstreamer()` |
| `deploy/install.sh` | Add `install_dlna_renderer()`, user creation, UUID seeding, unit installation |
| `pyproject.toml` | Add `jasper-dlna` script entry point |
| `README.md` | Add DLNA to architecture diagram, resource table, documentation map |
| `AGENTS.md` | Add DLNA section |

### Total new code

~665 LoC of Python, ~50 LoC of systemd unit config. About
half the size of `jasper-usbsink` (the sidecar has no audio
bridge, no preempt listener, no volume bridge).

---

## 10. Phased delivery

### Phase 1: Core integration (~1 day)

- Install gmrender-resurrect (apt or source-build)
- Create system user `jasper-dlna`
- Write `gmediarender.service` + `jasper-dlna.service`
- Implement `jasper/dlna/` sidecar (daemon, state_publisher,
  upnp discovery)
- Add `dlna_playing()` to `source_state.py`
- Add `Source.DLNA` to mux + volume coordinator
- Add to `/sources/` toggle
- Add doctor checks
- Add to install.sh
- Verify: play audio from BubbleUPnP, hear it through speakers,
  see `playing: true` in `/state`, mux preempts on AirPlay start

**Acceptance criteria:**
- Audio plays through CamillaDSP chain
- Mux preempts DLNA when another source starts
- `/state` shows DLNA renderer status
- `jasper-doctor` reports DLNA health
- Toggle at `/sources/` enables/disables cleanly
- RAM stays under 20 MB Pss

### Phase 2: Polish (~half day)

- Track metadata in state.json (poll `GetPositionInfo` for
  `TrackURI` and `TrackMetaData`)
- Surface metadata in `/state` renderers section
- Add `renderers.dlna` to `get_currentsong()` in `renderer.py`
  (so voice tools can report what's playing via DLNA)
- Structured logging review (ensure all paths have `event=dlna.*`)
- HANDOFF doc `Last verified` update

### Phase 3: Optional enhancements (deferred)

- Tier 3 protocol supervisor (when/if wedge pattern observed)
- Volume observer (phone slider → `listening_level`) if
  requested by users
- Device name configuration in a settings wizard
- Audible failure cue (`CueDef` for "DLNA renderer is down")
- Wake-event telemetry: add DLNA playing context to wake events
  (for false-fire analysis during DLNA playback)

---

## 11. Open questions (updated 2026-05-24)

1. ~~**Debian Trixie package status.**~~ **Resolved.**
   `gmediarender` 0.3-1 is in Trixie arm64. `apt-get install`
   works. Source-build fallback retained in install.sh but
   unlikely to be needed.

2. ~~**GStreamer plugin set.**~~ **Resolved.**
   `gstreamer1.0-plugins-good` + `-bad` + `-ugly` +
   `gstreamer1.0-alsa` covers FLAC, MP3, AAC, OGG, WAV, ALAC,
   WMA. DSD is NOT supported (confirmed by upstream maintainer,
   issue #213). Not a concern for DLNA streaming use cases.

3. **SSDP port conflict.** Avahi uses mDNS (UDP 5353); SSDP
   is UDP 1900. No known conflict with existing JTS daemons.
   Verify at implementation time.

4. **Enabled by default?** Current plan: enabled by default
   (no hardware dependency). Alternative: disabled by default
   to match USB Audio Input's conservative stance. Enabled is
   more user-friendly since DLNA is purely software and costs
   0 MB when idle (gmrender only loads GStreamer on first play).

---

## 12. Decision record

### Why a sidecar, not patching gmrender

Three options were considered for state detection:

| Option | Pros | Cons |
|---|---|---|
| **A: Python sidecar** (chosen) | No upstream patches; pure observer; same pattern as usbsink | Extra ~5-8 MB; one more service to manage |
| B: SOAP polling from source_state.py | No extra daemon; simplest | Couples source_state to HTTP client; adds latency to every mux tick; no watchdog |
| C: Patch gmrender with `--onevent` hook | Zero extra RAM; librespot pattern | Upstream maintenance burden; fork divergence; C code changes |

Option A wins on maintainability: the sidecar is a standard
JTS Python daemon with the same lifecycle, watchdog, and
logging patterns as every other daemon. It requires no upstream
fork and no C development. The ~5-8 MB cost is well within the
RAM budget.

### Why camilla-as-master for volume

Same rationale as AirPlay: the sender app's volume slider is
an upstream trim. The Pi's canonical volume is CamillaDSP's
`main_volume`. Diverging from this model would require:

- A `VolumeObserver` polling `GetVolume` at 1 Hz
- A `_set_dlna` dispatcher in the coordinator
- Echo-prevention logic (500 ms window)
- Testing across multiple DLNA controller apps

All for a feature no user has requested. Defer until observed
demand.

### Why `Restart=always`, not `Restart=on-failure`

Same rationale as librespot and shairport-sync: renderer
services must always be advertised on the network. A clean exit
(status=0) with `Restart=on-failure` would leave the DLNA
endpoint invisible until the next reboot. `Restart=always` +
`StartLimitBurst=5` catches config errors (5 rapid failures →
systemd gives up) while ensuring the endpoint is always present
for discovery.

---

Last verified: 2026-05-24
