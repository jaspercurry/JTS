# HANDOFF — Apple Music integration (research + plan)

Research-only doc. No implementation yet. Captures the feasibility
analysis, architecture decision, chosen integration path, and
sequenced build plan for adding Apple Music as a voice-controllable
music source alongside the existing Spotify / AirPlay / Bluetooth /
USB Audio sources.

> **Audio topology note (updated 2026-05-26):** JTS now uses the
> fan-in renderer topology. Any future native Apple Music player should
> write to its own private fan-in lane, not directly to
> `hw:Loopback,0,0`. The older raw-loopback examples below have been
> updated where they describe the planned implementation path.

## TL;DR

Apple Music has no Spotify Connect equivalent (no librespot, no
public playback API, no device-targeting REST endpoint). The only
proven path to headless streaming on a Pi is the approach
[Music Assistant](https://github.com/music-assistant/server) ships:
authenticate via MusicKit JS, fetch encrypted tracks from Apple's
private `webPlayback` API, decrypt with Widevine L3 CDM credentials
via `pywidevine`, and pipe through ffmpeg's standard `-decryption_key`
CENC decryption.

**Chosen path: Path C** — vendor MA's streaming/decryption code
(~300 lines, Apache-2.0) into `jasper/apple_music/`, build all JTS
plumbing (player, queue, state file, mux integration, voice tools,
wizard) ourselves. When Apple breaks the private API, cherry-pick
MA's fix rather than solo-maintaining the Apple-side surface.

**Pre-implementation gate: run the MA spike** — install MA via Docker
on the Pi 5, configure Apple Music with CDM credentials, play a
catalog track. If it works, the CDM-on-aarch64 + webPlayback +
ffmpeg-CENC chain is proven on our hardware. If it doesn't, no
amount of vendoring saves us. 1 hour, zero code written.

## Why not the obvious paths

### No librespot equivalent exists

Nobody has reverse-engineered Apple Music's streaming protocol the
way librespot reverse-engineered Spotify Connect. The architectural
barriers:

- **No open device-registration protocol.** Apple has no Spotify
  Connect equivalent. AirPlay 2 is push-only (sender initiates;
  receiver cannot request content).
- **FairPlay DRM** locks lossless (ALAC) and Atmos content to
  Apple platforms. No open-source implementation exists.
- **Widevine L3** protects the AAC (lossy, 256 kbps) tier in
  browsers and Android. L3 is software-only and has been
  reverse-engineered — this is the tier MA exploits.
- **Apple's commercial partner model** (Sonos SMAPI, Google Cast,
  Tesla MusicKit) is bilateral and closed. No public partner API
  or certification program.

### AirPlay is already working (but can't cold-start)

shairport-sync IS the Apple Music renderer today. When someone
AirPlays Apple Music from their iPhone, the audio path is:

```
iPhone → AirPlay 2 → shairport-sync → shairport_substream
       → jasper-fanin → CamillaDSP → dongle → speakers
```

Transport (next/prev/pause) already works via DACP/MPRIS. Volume
works via the iPhone slider. Mux already detects AirPlay. **The
only gap is cold-start voice playback** — "play Beyoncé" from
silence, with no phone involved.

### Cider RPC (requires always-on Mac)

[Cider](https://cider.sh/) is an Electron-based Apple Music client
for desktop platforms (Windows, macOS, Linux x86) that exposes a
REST API on port 10767:

```
GET  /api/v1/playback/now-playing
GET  /api/v1/playback/queue
GET  /api/v1/playback/volume
POST /api/v1/playback/playpause
POST /api/v1/playback/next
POST /api/v1/playback/previous
POST /api/v1/playback/seek               {"position": seconds}
POST /api/v1/playback/volume             {"volume": 0.0-1.0}
POST /api/v1/playback/play-item-href     {"href": "..."}
POST /api/v1/playback/play-next          {"id": "...", "type": "song"}
POST /api/v1/playback/play-later         {"id": "...", "type": "song"}
POST /api/v1/playback/queue/change-to-index
POST /api/v1/playback/queue/move-to-position
POST /api/v1/playback/queue/remove-by-index
POST /api/v1/amapi/run-v3               (Apple Music API proxy)
```

Plus Socket.io events for real-time state (`API:Playback` →
`nowPlayingStatusDidChange`, `playbackStateDidChange`, etc.).
Auth is a token in the `apptoken` HTTP header.

**Blocker:** Cider requires CastLabs Electron (Widevine-enabled).
No ARM/aarch64 Linux build exists — cannot run on the Pi. Would
require a Mac or x86 Linux desktop on the LAN as the audio source,
with Cider AirPlaying to shairport-sync. Viable for households
with an always-on Mac; not a standalone solution.

Source: [`ciderapp/Cider-Remote`](https://github.com/ciderapp/Cider-Remote)
(protocol reverse-engineered from the iOS remote app's Swift source).

### Running Music Assistant as a sidecar service — rejected

MA as a sibling systemd service on the Pi would avoid owning the
Apple-side streaming code, but adds ~150-200 MB RAM (the full MA
server with all providers), Docker overhead (or managing MA's
Python deps alongside JTS's), a WebSocket-not-REST control API,
and loopback AirPlay routing complexity (MA → shairport-sync on
the same box). On a 2 GB Pi already at ~770 MB baseline, the RAM
cost is prohibitive. **Rejected.**

## The Music Assistant streaming pipeline (what we're vendoring)

MA's Apple Music provider
([`music_assistant/providers/apple_music/`](https://github.com/music-assistant/server/tree/stable/music_assistant/providers/apple_music),
Apache-2.0) is the only proven headless Apple Music streaming
implementation on Linux. The pipeline:

### Authentication (two-token system)

1. **`MUSIC_APP_TOKEN`** (Apple Developer JWT) — signed with a
   MusicKit private key from an Apple Developer account ($99/year).
   Expires every 6 months. MA distributes theirs via a private
   `appvars` repo and a GitHub Action that rotates daily. For JTS,
   the user generates their own in their Apple Developer dashboard.

2. **`MUSIC_USER_TOKEN`** (Apple Music subscription token) — obtained
   via a one-time browser flow: MusicKit JS `authorize()` opens
   Apple's OAuth consent, returns a `music_user_token`. Stored in
   config. **Expires ~180 days, no automatic refresh.** User must
   re-authenticate in browser. The wizard should show days-until-
   expiry on the status card; the voice tool should check token age
   and return a structured `auth_expired` error the LLM can speak
   gracefully. Manual fallback: paste the `media-user-token` cookie
   from `music.apple.com` in a browser's dev tools.

### Stream resolution

```
POST https://play.music.apple.com/WebObjects/MZPlay.woa/wa/webPlayback
  body: {"salableAdamId": "<catalog_id>"}       # catalog tracks
     or {"universalLibraryId": "<id>", "isLibrary": true}  # library tracks
  headers:
    Authorization: Bearer <MUSIC_APP_TOKEN>
    Music-User-Token: <MUSIC_USER_TOKEN>
    User-Agent: (Chrome spoof)
    Origin: https://music.apple.com
```

Response contains `songList[].assets[]` — each asset has a `flavor`
and a `URL`. The `28:ctrp256` flavor is 256 kbps AAC (the only
quality tier available via Widevine; lossless is FairPlay-locked).

**Library tracks** (IDs matching `[ailp]\.\w+`) return
**unencrypted** URLs from `webPlayback`. No CDM needed. This is the
low-friction MVP path — prove the audio chain works here first.

**Catalog tracks** (numeric IDs, `pl.*`) return encrypted HLS
playlists requiring Widevine decryption.

### HLS playlist parsing

Fetch the `.m3u8` at the `ctrp256` URL. Extract:
- The MP4 segment URL (base path + segment filename)
- The `#EXT-X-KEY` URI (contains the Widevine key ID)

MA uses a custom string parser (~50 lines), not the `m3u8` PyPI
package. A `PlaylistItem` dataclass with `.path` and `.key`
attributes, populated by `.split()` / `.strip()` — no regex, no
external dependency.

### Widevine key exchange (catalog tracks only)

1. Build a PSSH (Protection System Specific Header) from the key ID
2. Create a `pywidevine.Device` from the CDM credentials
   (`client_id.bin` + `private_key.pem`, `DeviceTypes.ANDROID`,
   `security_level=3`)
3. Generate a license challenge via `Cdm.get_license_challenge()`
4. POST the challenge to Apple's `hls-key-server-url` (returned in
   the `webPlayback` response)
5. Parse the license response, extract the `CONTENT` key
6. Return the key as a hex string

The CDM is used **only for key exchange** — it calls
`get_license_challenge`, `parse_license`, `get_keys`. It does NOT
call `Cdm.decrypt()` on audio data. pywidevine never touches the
actual audio.

### ffmpeg decryption

The content key (hex string) is passed to ffmpeg:

```
ffmpeg -decryption_key <hex> -i <encrypted_segment_url> \
       -f s24le -ar 48000 -ac 2 pipe:1
```

ffmpeg's MOV/MP4 demuxer handles ISO Common Encryption
(CENC/AES-128-CTR, ISO/IEC 23001-7) natively via its
`-decryption_key` option. This is a standard ffmpeg feature, not a
custom build. MA's actual production code (`controllers/streams/
audio.py` ~line 1060):

```python
if stream_type == StreamType.ENCRYPTED_HTTP:
    assert streamdetails.decryption_key is not None
    extra_input_args += ["-decryption_key", streamdetails.decryption_key]
```

Verified: ffmpeg does the decryption, not pywidevine.

### Widevine CDM credentials

The hardest user-facing requirement. `pywidevine` needs
`client_id.bin` + `private_key.pem` extracted from a real Android
device's L3 Widevine implementation.

- **Cannot be legally obtained or redistributed.** The only sources
  are extracting your own from a rooted Android phone (using tools
  like `dumper` or similar), or gray-market repos that are
  constantly DMCA'd.
- **MA bundles theirs** in Docker images from a private `appvars`
  repo. Self-hosters must supply their own.
- **For JTS**, the wizard should be explicit: "this requires CDM
  credentials we cannot provide; here's how to extract your own
  from a rooted Android device" with a doc link. Many users will
  close the tab.
- **Without CDM credentials**, library tracks still work
  (unencrypted). Catalog tracks fail with a clear error.

## Architecture — where Apple Music fits in JTS

Apple Music audio should go through a dedicated private fan-in lane
→ `jasper-fanin` → CamillaDSP → dongle — the **same architecture as
librespot**. Mux, volume, ducking, AEC all work unchanged.

```
jasper-voice
    │
    ├── jasper/apple_music/streaming.py  (vendored from MA: webPlayback + Widevine key exchange)
    ├── jasper/apple_music/client.py     (REST client for api.music.apple.com/v1/)
    ├── jasper/apple_music/auth.py       (MusicKit JS token management)
    ├── jasper/apple_music/player.py     (ffmpeg lifecycle, queue, state file)
    ├── jasper/tools/apple_music.py      (voice tools: apple_music_play, apple_music_queue)
    └── jasper/web/apple_music_setup.py  (wizard at /apple-music/)
         │
         ▼
    ffmpeg -decryption_key <hex> -i <url> -f s24le -ar 48000 -ac 2 pipe:1
         │
         ▼
    apple_music_substream  ──  snd-aloop  ──►  jasper-fanin
                                                     │
                                                     ▼
    jasper-camilla (CamillaDSP, main_volume ducking)
         │
         ▼
    pcm.jasper_out (dmix on Apple dongle) → amp → speakers
```

### Mux integration

Fifth source: `Source.APPLE_MUSIC`. State detection via
`/run/jasper-apple-music/state.json` (atomic tmp+rename, same
pattern as librespot's `--onevent` hook). Pause = kill the ffmpeg
subprocess (we own the player; no remote API needed).

### Volume

Camilla-as-master (same as AirPlay, USB sink, idle). CamillaDSP
`main_volume` carries `listening_level`. No upstream protocol slider
to drive.

### Transport routing

When `apple_music_playing()` returns True, transport tools
(`next_track`, `pause`, `resume`, `get_now_playing`) dispatch to
the `AppleMusicPlayer` instance. No MPRIS, no Web API — direct
control of the player we own.

### Voice tool routing

```
"play X" → spotify_play if Spotify active or configured-and-default;
           apple_music_play if Apple Music active or configured-and-default.
           If both configured and neither active, prefer Spotify (existing behavior).
           Never call both.
```

### Token expiry UX

When the 180-day Music User Token expires and the user says "play
Beyoncé on Apple Music," the voice tool should return:

```python
{"error": "auth_expired",
 "message": "Apple Music auth expired — visit jts.local/apple-music to reconnect."}
```

The LLM speaks this gracefully. The `/system/` dashboard and
`/apple-music/` wizard both show days-until-expiry.

## Implementation plan (sequenced)

### Pre-step: MA spike (1 hour, zero JTS code)

Install MA via Docker on the Pi 5. Configure Apple Music with CDM
credentials. Play a catalog track. If it works, the
CDM-on-aarch64 + webPlayback + ffmpeg-CENC chain is proven. If it
doesn't, Path C has no chance either and we've lost an hour instead
of a week.

### Step 1: Vendor MA's streaming code

Copy the streaming/decryption pipeline from MA
(`providers/apple_music/` — the `webPlayback` call,
`_get_decryption_key`, HLS playlist parsing) into
`jasper/apple_music/streaming.py` with Apache-2.0 attribution.

**Keep vendored files structurally aligned with upstream.** The
cherry-pick strategy only works if MA's streaming code and ours
stay similar enough that `git diff` on MA's provider produces
something we can read and port. Don't rename methods or restructure
classes. Treat the vendored file as a foreign body maintained in
sync with upstream, not as JTS code to be refactored.

### Step 2: Library-track MVP

Search user library → unencrypted URL → ffmpeg → private fan-in lane.
No CDM needed. Proves the full audio chain end-to-end: MusicKit JS
auth → Apple Music API search → `webPlayback` → ffmpeg → ALSA →
CamillaDSP → speakers. This is the cheapest possible validation of
the JTS-side plumbing.

### Step 3: Catalog track decryption

Layer `_get_decryption_key()` + ffmpeg `-decryption_key` on top of
step 2. Requires CDM credentials on disk. Validates the full
Widevine pipeline on aarch64.

### Step 4: Player + queue (~700 lines)

ffmpeg subprocess lifecycle, track-end detection (read EOF on
stdout pipe → spawn next), context-aware "next" (album tracks,
playlist tracks, flat queue), mixed catalog + library tracks,
"add to queue" mid-playback. State file at
`/run/jasper-apple-music/state.json`.

**Ship v1 without gapless.** One ffmpeg per track means an audible
gap at track-end while the next subprocess spawns, opens HTTPS,
fetches the playlist, and starts decoding. True gapless requires
either pre-spawning the next ffmpeg ~2 s before track-end and
crossfading, or running two parallel ffmpegs and concatenating PCM
output. Both add real complexity. Document as a known limitation;
revisit if users complain. Most won't notice on 256 kbps AAC pop;
they will on live albums.

### Step 5: Mux + volume + source_state integration

Add `Source.APPLE_MUSIC` to `mux.py`, `volume_coordinator.py`,
`source_state.py`, `renderer.py`. Same patterns as USB sink
(state file detection, camilla-as-master volume, direct pause).

### Step 6: Voice tools + transport routing

`jasper/tools/apple_music.py`: `apple_music_play(query, kind)`,
`apple_music_queue(query)`. Transport dispatch in `transport.py`.
Cross-tool routing rules in `SYSTEM_INSTRUCTION`.

### Step 7: Wizard at `/apple-music/`

Three-state wizard (mirrors `/ha/` shape):

1. No config → setup instructions (Developer JWT, CDM creds, browser
   auth)
2. Credentials present, no user token → serve MusicKit JS auth page
3. Configured → status card (token expiry countdown, test button,
   disconnect)

Persists to `/var/lib/jasper/apple_music.env`.

### Step 8: Tests + voice-eval scenario

Unit tests for client, streaming, player (mocked network + ffmpeg).
Voice-eval regression scenario for `apple_music_play`.

## File map

```
jasper/
  apple_music/                          NEW — Apple Music client + streaming
    __init__.py                         Package init, re-exports
    client.py                           REST client for api.music.apple.com/v1/
    streaming.py                        Vendored from MA: webPlayback + Widevine key exchange
    auth.py                             MusicKit JS token management
    player.py                           ffmpeg lifecycle, queue manager, state file
    state.py                            State file reader (mirrors librespot_state pattern)

  tools/
    apple_music.py                      NEW — voice tools

  web/
    apple_music_setup.py                NEW — wizard at /apple-music/

  source_state.py                       EDIT — add apple_music_playing()
  renderer.py                           EDIT — add to active_renderers
  mux.py                                EDIT — add Source.APPLE_MUSIC
  volume_coordinator.py                 EDIT — add Source.APPLE_MUSIC (camilla-as-master)
  config.py                             EDIT — add apple_music_* fields
  voice_daemon.py                       EDIT — register tools, SYSTEM_INSTRUCTION routing

deploy/
  systemd/                              No new unit — player runs in-process in jasper-voice

tests/
  test_apple_music.py                   NEW — unit tests
  test_apple_music_streaming.py         NEW — decryption pipeline tests (mocked)
  voice_eval/regression/                NEW — apple_music_play scenario
```

## Constraints and risks

| Constraint | Impact | Mitigation |
|---|---|---|
| 256 kbps AAC ceiling | No lossless, no Atmos | Lossless uses FairPlay — no path. Accept the ceiling. |
| Music User Token expires ~180 days | Manual re-auth in browser | Wizard shows countdown; voice tool returns structured `auth_expired` |
| CDM credentials required | User must extract from rooted Android | Wizard is explicit about this; library tracks work without CDM |
| Private `webPlayback` API | Apple could break it any time | Vendor from MA; cherry-pick their fixes (~2-4 incidents/year per MA commit history) |
| pywidevine is GPL-3.0 | License conflict with Apache-2.0 JTS | Optional runtime dep; user installs separately; Apple Music module loaded dynamically |
| Apple Developer account $99/year | Cost for the MusicKit signing key | Required; no workaround |
| No gapless playback (v1) | Audible gap between tracks | Known limitation; document it; revisit if users complain |
| Queue manager complexity | ~700 lines, not trivial | Budget accurately; librespot handles this internally for Spotify |
| Single Apple ID | No multi-user routing (unlike Spotify) | AirPlay sessions are single-sender anyway; accept for v1 |

## Prior art surveyed

| Project | What it does | Useful for JTS? |
|---|---|---|
| [Music Assistant](https://github.com/music-assistant/server) | Full media server, streams Apple Music via Widevine L3 on headless Linux | **Yes — vendoring the streaming pipeline** |
| [Cider](https://cider.sh/) + [Cider-Remote](https://github.com/ciderapp/Cider-Remote) | Electron Apple Music client with REST API on :10767 | Alternative path if household has always-on Mac |
| [pyatv](https://github.com/postlund/pyatv) | Python AirPlay/HomeKit control library | Metadata enrichment only; can't initiate Apple Music playback |
| [apple-music-python](https://github.com/mpalazzolo/apple-music-python) | Apple Music REST API wrapper | Catalog search only; no playback, no user token |
| [Sidra](https://github.com/wimpysworld/sidra) | Electron Apple Music wrapper for Linux | GUI-only, no ARM, no headless |
| [Volumio Apple Music plugin](https://github.com/stacywebb/apple_music_volumio) | Abandoned proof-of-concept | Incomplete; same MusicKit JS blocker |
| Sonos SMAPI / Google Cast / Tesla MusicKit | Commercial partner integrations | Private bilateral agreements; not available to third parties |

## Feedback investigation (2026-05-24)

External review of the implementation plan produced several claims.
Investigation against MA's actual source code:

| Claim | Verdict | Evidence |
|---|---|---|
| "ffmpeg `-decryption_key` probably doesn't work; MA uses `Cdm.decrypt()`" | **Wrong.** MA uses ffmpeg `-decryption_key`. | `controllers/streams/audio.py` ~line 1060: `extra_input_args += ["-decryption_key", streamdetails.decryption_key]`. pywidevine is used solely for key exchange, not audio decryption. ffmpeg's MOV/MP4 demuxer handles ISO CENC natively. |
| "Missing dep: `m3u8` PyPI package" | **Wrong.** MA uses custom string parsing. | `helpers/playlists.py`: `PlaylistItem` dataclass, `.split()`/`.strip()` parsing, no `import m3u8`. ~50 lines. |
| "Library tracks are unencrypted — start there for MVP" | **Correct.** | MA code: `if is_library_id(item_id): return StreamDetails(stream_type=StreamType.HTTP, ...)` — no `decryption_key`. |
| "Queue management is ~600-800 lines, not ~300" | **Correct.** | Track-end detection, context-aware next, gapless pre-fetch, mixed catalog+library, mid-playback add-to-queue. Budget ~700 lines. |
| "44.1/16 → 24/48 is zero-pad" | **Correct but irrelevant.** | Matches librespot's behavior (source-native decode → 48 kHz fan-in lane → output dmix). Consistency across sources > bit-perfect. |
| "Run MA spike first" | **Correct.** | Cheapest possible validation of the full chain on aarch64. Do before any code. |

Last verified: 2026-05-26
