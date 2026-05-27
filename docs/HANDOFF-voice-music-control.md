# Source-aware voice transport + Spotify routing

How the voice-control surface (volume, transport, Spotify play) is
wired across the music sources (AirPlay 2, Spotify Connect,
Bluetooth A2DP, and USB sink). All of this is implemented and shipped; this doc
explains the design and the non-obvious cases.

## File map

| File | Role |
|---|---|
| `jasper/music_sources.py` | Canonical source IDs, fan-in labels, wizard keys, and volume mode (`push` vs `camilla_master`) |
| `jasper/renderer.py` | `RendererClient` — per-source state queries (DBus / state-file / subprocess) |
| `jasper/mux.py` | Latest-source-wins/manual source policy plus guarded source handoff before fan-in selection |
| `jasper/tools/transport.py` | `make_transport_tools(renderer, router)` and `make_transport_dispatcher` — source-aware next/prev/pause/play |
| `jasper/tools/spotify.py` | `make_spotify_tools(...)` — search-and-play, with the AirPlay-carrying-Spotify shortcut |
| `jasper/spotify_routing.py` | `resolve_target` and `_match_track` — picks the right Spotify device for `start_playback` |
| `jasper/spotify_router.py` | Multi-account `Router` for routing voice commands to the right household member's Spotify account |
| `jasper/volume_coordinator.py` | Source-aware volume coordinator (see [HANDOFF-volume.md](HANDOFF-volume.md)) |

## Three voice-controllable areas

### 1. Volume

Goes through `VolumeCoordinator` (see
[HANDOFF-volume.md](HANDOFF-volume.md) for the full design).
The coordinator dispatches to whichever source's slider is active:
- AirPlay → CamillaDSP `main_volume` as the JTS speaker volume
  (shairport-sync's AirPlay 2 receiver-originated volume reflection is
  not reliable on modern iOS/macOS)
- Spotify Connect → Spotify Web API per the active account
- Bluetooth A2DP → DBus to bluez-alsa
- USB sink → CamillaDSP `main_volume` as the JTS speaker volume
  (host-side volume is observed one-way by `jasper-usbsink`; JTS does
  not push volume back to the host)
- Idle (no source) → CamillaDSP main_volume

CamillaDSP `main_volume` is reserved for the daemon's ducking
(and for IDLE/AirPlay user volume per the VolumeCoordinator). The
`master_gain` mixer in v1.yml is identity and not the ducker.

### 2. Transport (next / previous / pause / resume)

`make_transport_dispatcher(renderer, router).dispatch(action)`
queries `renderer.active_renderers()` and picks the right backend:

| Active source | Backend |
|---|---|
| AirPlay (`aplactive`) | shairport-sync MPRIS over busctl. AirPlay-carrying-Spotify gets short-circuited via the title-match path (see below). |
| Spotify Connect (`spotactive`) | spotipy `next_track()` / `previous_track()` / `pause_playback()` against the user's account |
| Bluetooth (`btactive`) | Not supported — no transport API on bluez-alsa A2DP sink. Returns a spoken explanation. |
| USB sink (`usbsinkactive`) | Not supported — the host computer owns its player transport. Returns a spoken explanation if exposed through future tools. |
| No active source | Returns "nothing is playing" error so the model can tell the user something concrete instead of silently no-op'ing. |

### 3. Spotify play (`spotify_play(query, kind)`)

Search-and-play. The non-obvious case:

- User has **iPhone** playing **Spotify**, casting to the Pi via
  **AirPlay**.
- AirPlay is the active source on the Pi.
- The user's Spotify account is OAuth'd in our `spotify_router`.
- User says "Hey Jarvis, play Kanye West."

What happens: `resolve_target` notices that AirPlay metadata
(title + artist) matches what Spotify Web API reports the user is
currently playing. It targets the **iPhone's** Spotify Connect
device (not the Pi's librespot), so `start_playback` rides the
existing AirPlay stream — the iPhone changes track, the Pi just
keeps receiving the same AirPlay session. Net effect: voice
command works seamlessly without the Pi having direct AirPlay
control.

The matcher in `_match_track` is conservative — title AND artist
must align after normalisation. A paused Spotify session on the
user's laptop with the same song title coincidentally won't fool
it (we require `is_playing=True`).

## Multi-account Spotify routing

The household has multiple Spotify users, each with their own
OAuth refresh token under `/var/lib/jasper/spotify/accounts.json`.
`Router.resolve_for_transport` decides whose account a voice
command targets by cross-referencing the AirPlay sender's
ClientName (from shairport-sync MPRIS) against each account's
currently-playing track.

See [docs/multi-user-spotify.md](multi-user-spotify.md) for the
full design.

## Failure modes

- **AirPlay active, user asks "next song" but the source is NOT
  Spotify** (Apple Music, podcast, YouTube Music, etc.) → the
  dispatcher tries the title-match short-circuit, fails, returns
  "AirPlay transport isn't supported — control playback on your
  phone." Spoken back to the user.
- **No active source, user asks "play Kanye West"** →
  `start_playback` targets the Pi's librespot endpoint
  using the shared speaker display name from `/speaker/` (default
  "JTS"). If that endpoint isn't visible to the user's Spotify
  account, returns "no
  Spotify target device available — visit `<management URL>` to
  link your account or open Spotify and cast to the speaker once
  to register it."
- **Bluetooth active, user asks for transport** → returns
  "Bluetooth transport isn't supported — control playback on your
  phone."
- **USB sink active, user asks for transport** → host-owned player;
  control playback on the computer.

## System-instruction guidance

Tool-use rules in `voice_daemon.py:SYSTEM_INSTRUCTION` are terse
and anti-conversational. Match the existing pattern when adding
new tools — don't ask for confirmation, don't preface, don't
invite further conversation.

## Anti-patterns to avoid

- Don't bypass the `ToolRegistry` — every tool goes through it;
  that's how Gemini sees function declarations.
- Don't change CamillaDSP's `main_volume` directly from a tool —
  that's the daemon's ducking knob and the IDLE/AirPlay user volume
  surface, not something a transport or source tool should drive. Use the
  `VolumeCoordinator` instead.
- Don't try to control AirPlay generically — only the
  AirPlay-carrying-Spotify case has a workaround. Be honest with
  the user about other AirPlay sources.

---

Last verified: 2026-05-27 (source registry + guarded handoff path rechecked)
