# Source-aware voice transport + Spotify routing

How the voice-control surface (volume, transport, Spotify play) is wired
across the music sources (AirPlay 2, Spotify Connect, Bluetooth A2DP, USB
sink). All of it is shipped; this doc keeps the routing rules and the
non-obvious cases.

| File | Role |
|---|---|
| `jasper/music_sources.py` | Canonical source IDs, `renderer_active_key`s, wizard keys, volume mode (`push` vs `camilla_master`) |
| `jasper/renderer.py` | `RendererClient` — per-source state queries (DBus / state-file / subprocess) |
| `jasper/mux.py` | Latest-source-wins/manual source policy plus guarded source handoff before fan-in selection |
| `jasper/tools/transport.py` | `make_transport_tools` / `make_transport_dispatcher` — source-aware next/prev/pause/play |
| `jasper/tools/spotify.py` | `make_spotify_tools` — search-and-play, incl. the AirPlay-carrying-Spotify shortcut |
| `jasper/spotify_routing.py` | `resolve_target` / `_match_track` — picks the Spotify device for `start_playback` |
| `jasper/spotify_router.py` | Multi-account `Router` for per-household-member Spotify routing |
| `jasper/volume_coordinator.py` | Source-aware volume coordinator ([HANDOFF-volume.md](HANDOFF-volume.md)) |

New music integrations should keep provider/catalog logic separate from
source/renderer capability logic: Spotify account routing and search are
provider concerns, Spotify Connect volume and transport are source concerns.
The Sources contract itself is
[HANDOFF-source-capabilities.md](HANDOFF-source-capabilities.md); the owners
that must not be bypassed are `jasper-mux` for audible source policy,
`VolumeCoordinator` for volume, and this doc for voice transport.

## 1. Volume

Goes through `VolumeCoordinator` ([HANDOFF-volume.md](HANDOFF-volume.md) has
the full design). It dispatches to whichever source's slider is active:

- AirPlay → CamillaDSP `main_volume` as the JTS speaker volume
  (shairport-sync's AirPlay 2 receiver-originated volume reflection is not
  reliable on modern iOS/macOS)
- Spotify Connect → Spotify Web API per the active account
- Bluetooth A2DP → DBus to bluez-alsa
- USB sink → CamillaDSP `main_volume` (host-side volume is observed one-way
  by `jasper-usbsink`; JTS does not push volume back to the host)
- Idle (no source) → CamillaDSP `main_volume`

At 0% the coordinator also asserts CamillaDSP `main_mute`, so content mute is
actual silence. `main_volume` is otherwise reserved for the daemon's ducking
and for camilla-master user volume (idle / AirPlay / USB). The `master_gain`
mixer in the outputd cutover config is identity and is not the ducker.

## 2. Transport (next / previous / pause / resume)

`make_transport_dispatcher(renderer, router).dispatch(action)` asks mux for
`renderer.selected_source()` first, so manual source selection and guarded
handoff policy decide the backend. If mux is unavailable it falls back to
`renderer.active_renderers()`:

| Active source | Backend |
|---|---|
| AirPlay (`aplactive`) | AirPlay-carrying-Spotify is short-circuited via the title-match path (below); otherwise shairport-sync MPRIS/DACP, only when the sender exposes remote control |
| Spotify Connect (`spotactive`) | spotipy `next_track()` / `previous_track()` / `pause_playback()` against the user's account |
| Bluetooth (`btactive`) | BlueZ AVRCP via the active `org.bluez.MediaPlayer1` object. Requires the phone/player to expose a BlueZ player object. |
| USB sink (`usbsinkactive`) | Not supported — the host computer owns its player transport |
| No active source | Returns a "nothing is playing" error so the model says something concrete instead of silently no-op'ing |

Voice transport and source preemption deliberately have different AirPlay
semantics. A voice "pause" uses MPRIS/DACP and keeps the sender session alive
so it can resume. When another source wins, `jasper-mux` first completes the
fan-in handoff and then tears the AirPlay session down with shairport-sync's
receiver-owned `DropSession` (falling back to MPRIS `Stop`). Keeping these
paths separate prevents a transport command from becoming a source-policy
decision.

## 3. Spotify play (`spotify_play(query, kind)`)

Search-and-play. The non-obvious case: the user's iPhone is playing Spotify,
casting to the Pi over AirPlay, their account is OAuth'd in `spotify_router`,
and they say "play Kanye West".

`resolve_target` notices that the AirPlay title metadata matches what the
Spotify Web API reports the account is currently playing, and targets the
**iPhone's** Spotify Connect device rather than the Pi's librespot. So
`start_playback` rides the existing AirPlay stream: the iPhone changes track,
the Pi keeps receiving the same session, and the command works without the Pi
having AirPlay control at all.

`_match_track` is title-only after normalization, because Spotify and AirPlay
routinely disagree on artist strings for collaborations, remasters, and
compilations. A paused session elsewhere with the same title can't fool it —
matching requires `is_playing=True`.

## Multi-account Spotify routing

Each household member has their own OAuth refresh token under
`/var/lib/jasper-intsecrets/spotify/caches/`; `accounts.json` is only the
registry index. `Router.resolve_for_transport` decides whose account a voice
command targets by cross-referencing the AirPlay sender's ClientName (from
shairport-sync MPRIS) against each account's currently-playing track. Full
design: [multi-user-spotify.md](multi-user-spotify.md).

## Failure modes

- **AirPlay active but the sender isn't Spotify** (Apple Music, a podcast,
  YouTube Music) → the dispatcher tries the title-match short-circuit, then
  checks shairport-sync's `RemoteControl.Available`. If DACP is available,
  MPRIS forwards the command; if not, it returns a concrete "the AirPlay
  sender doesn't accept remote control" error.
- **No active source, "play Kanye West"** → `start_playback` targets the Pi's
  librespot endpoint using the shared speaker display name from `/speaker/`.
  If that endpoint isn't visible to the account, the error names the fix
  (link the account, or cast to the speaker once to register it).
- **Bluetooth active** → BlueZ AVRCP when a `MediaPlayer1` object exists under
  the active A2DP device; otherwise a concrete "AVRCP player not available".
- **USB sink active** → host-owned player; control playback on the computer.

## Anti-patterns

- Don't bypass `ToolRegistry` (`jasper/tools/__init__.py`) — it is how every
  provider gets its function declarations. The Tool contract is in
  [extensibility.md](extensibility.md).
- Don't set CamillaDSP `main_volume` from a tool — it is the ducking knob, the
  camilla-master user-volume surface, and the 0% content-mute carrier. Use
  `VolumeCoordinator`.
- Don't assume AirPlay remote control is available. Try the Spotify
  title-match path first; call MPRIS/DACP only after shairport-sync reports
  `RemoteControl.Available=true`.

Tool wording and system-prompt conventions belong to
[HANDOFF-prompting.md](HANDOFF-prompting.md); `SYSTEM_INSTRUCTION` lives in
`jasper/voice/prompt.py`, not in the daemon.

---

Last verified: 2026-08-26 (`renderer_active_key`s, the dispatcher's
mux-first selection, `DropSession`-with-MPRIS-fallback preemption,
`_match_track`'s `is_playing` guard, and `resolve_for_transport` rechecked
against the tree; the system-prompt pointer corrected — `SYSTEM_INSTRUCTION`
moved to `jasper/voice/prompt.py`. Prior 2026-07-22: voice-pause versus
mux-preemption boundary; 2026-06-26: full transport-dispatch matrix.)
