# Handoff: voice-controlled audio (volume + music transport + Spotify)

You're picking up an in-progress voice-assistant project (JTS = "Jasper Smart Speaker"). The hardware bringup, the persistent Gemini Live session, the wake-word + Silero end-of-utterance pipeline, and the basic ducking are all working. **This handoff is for the next phase: making the voice loop actually useful for music control.**

## Repo and branch

```
git@github.com:jaspercurry/JTS.git
branch: claude/camilla-dsp-voice-plan-QRdsE
```

All recent work has been merged onto this branch (last merge commit: `4bb4b15`). Pull it. Read recent commits via `git log --oneline -20` to understand the trajectory before you start writing.

## Read first, in order

1. **`CLAUDE.md`** (root) — project context, model strategy (`gemini-3.1-flash-live-preview` is the active model; switch via `bash scripts/switch-gemini-model.sh`), debugging philosophy, log-fetching patterns.
2. **`BRINGUP.md`** (root) — full hardware architecture. Pi 5 + moOde 10.x + always-on CamillaDSP + Apple USB-C dongle as DAC + ReSpeaker XVF3800 as mic. AirPlay/MPD/Spotify Connect all flow through CamillaDSP. Read the failure-modes table and the "Tuning AUDIO_MGR_SYS_DELAY" section even if you skip the rest.
3. **`docs/audit-pending-followups.md`** — what's intentionally deferred vs what's actively planned. Especially the "Tier 2 — gated on hardware AEC working" and "Future UX work (post-AEC)" sections. Don't accidentally do those — they're queued for after this work.
4. **`jasper/voice_daemon.py`** — main loop. The `WakeLoop` class, `_handle_session_frame`, end-of-utterance detection, ducking calls, `_begin_turn` / `_end_turn`. The `SYSTEM_INSTRUCTION` at the top is what Gemini sees — extending tool-call rules will go here.
5. **`jasper/voice/gemini_session.py`** — persistent Gemini Live connection. Tool-dispatch lives in `_handle_tool_call`. Turn lifecycle, manual VAD, reconnect supervisor.
6. **`jasper/tools/`** directory — existing tool implementations:
   - `audio.py` — `make_audio_tools(camilla)` already wraps a `CamillaController` (volume, ducking)
   - `transport.py` — `make_transport_tools(moode)` already exists; check what it covers
   - `spotify.py` — `make_spotify_tools(...)` already exists; check what it covers
   - `weather.py`, `subway.py` — for reference patterns
7. **`jasper/tools/__init__.py`** — `ToolRegistry` (how tools register). `function_declarations()` builds the schema sent to Gemini.
8. **`jasper/camilla.py`** — `CamillaController` for volume, `Ducker` for the duck/restore primitive.
9. **`jasper/spotify_routing.py`** + **`jasper/moode.py`** — existing client glue. `MoodeClient` talks to moOde's HTTP API and MPD for transport. `build_spotify(cfg)` returns an authenticated spotipy client.

## What you're building

Three voice-controllable areas. Some of the underlying tool functions may already exist — check first, extend / wire up; don't reinvent.

### 1. Volume control via tool calls

Voice patterns to support:
- "Volume up" → bump by some step (try 10%)
- "Volume down" → drop by 10%
- "Volume 30 percent" / "set volume to 80" → absolute
- "Mute" / "unmute" — optional but easy

The volume control point is **moOde's volume**, not CamillaDSP's `master_gain` (that's reserved for the daemon's ducking and shouldn't be touched by user volume). Check `MoodeClient` for an existing volume endpoint; moOde has an HTTP API (`/command/?cmd=set_volume <N>` is roughly the shape, verify in `jasper/moode.py` and moOde's source).

There's currently a `make_audio_tools(camilla)` registry — that's CamillaDSP, which is the wrong target for user volume. You'll likely need a `make_volume_tools(moode)` or extend `make_transport_tools` to include volume. Decide based on what's cleanest.

After implementing the tool, extend the system instruction in `voice_daemon.py:SYSTEM_INSTRUCTION` with terse guidance like: `"For volume requests ('turn it up', 'volume 30', 'mute'), call set_volume directly — don't ask for confirmation."` Match the existing few-shot example style.

### 2. Music transport (next/previous/pause/play)

Voice patterns:
- "Next song" / "skip" / "next" → `next`
- "Previous song" / "go back" / "previous" → `previous`
- "Pause" / "stop" → `pause`
- "Resume" / "play" / "keep playing" → `play`

This needs to dispatch to the **right player** based on what's currently playing:
- If **AirPlay** is the active source → there's no transport API for AirPlay (it's controlled by the sender). Honest answer: you can't `next` AirPlay from the daemon. Tell the user explicitly. (Possibly look at `shairport-sync-metadata` if there's a control hook, but expect to say no.)
- If **MPD/local** is the active source → `MoodeClient` should have transport methods that talk to MPD via `python-mpd2`.
- If **Spotify Connect** is the active source → `spotipy`'s `next_track()` / `previous_track()` / `pause_playback()` / `start_playback()` against the active device.

There's existing logic in `jasper/spotify_routing.py` that cross-checks AirPlay metadata against Spotify currently-playing — it's the precedent for the "which source is actually playing" detection. Reuse that pattern. The handoff in this doc's #3 below is the more interesting case where the user intent should *override* this routing.

### 3. Spotify voice control with the AirPlay-bridge case

This is the one with the interesting twist. The use case:

- User has **iPhone** playing **Spotify**, casting to the Pi via **AirPlay**.
- AirPlay is what the Pi sees on its audio input (the Pi receives an audio stream, not metadata or Spotify state).
- BUT the user's Spotify account is the same one we have OAuth'd with via `spotipy`.
- User says "Hey Jarvis, play Kanye West."

What should happen: the daemon issues a `start_playback` command **to Spotify (via the API), targeting the iPhone**. The iPhone's Spotify client receives the command, starts playing Kanye West. The iPhone's audio output is still going to AirPlay → Pi. From the Pi's perspective, AirPlay is still the audio source — but now the content is Kanye West instead of whatever it was. Net effect: voice command works seamlessly even though the Pi has zero direct control over AirPlay.

The key insight: **the active Spotify Connect device doesn't have to be the Pi.** `spotipy.start_playback(device_id=...)` lets you target any device the user's Spotify account knows about — including their phone.

Implementation sketch:
1. On `play_artist(name)` / `play_song(query)` / `play_playlist(name)` tool calls, look up the artist/track/playlist via Spotify search.
2. Determine the target device. Decision logic:
   - If Spotify says the user has an active session on a device → use that device (likely the iPhone, since that's what's playing through AirPlay).
   - If no active Spotify session and the Pi has Spotify Connect set up via librespot → use the Pi.
   - If neither → fail gracefully, tell the user "no active Spotify device."
3. Call `spotipy.start_playback(device_id=target, context_uri=...)` (or `uris=[...]` for a single track).

`jasper/tools/spotify.py:make_spotify_tools(...)` already takes `cfg.spotify_device_name` as a parameter (defaulting to `moode`) — this is the historical "always send to the Pi" path. You'll need to extend the device-targeting logic to fall back to "currently active device" when one exists. Look at `spotipy.devices()` — returns the list with `is_active` flag.

A few specific tool functions to think about:
- `play_artist(name: str)` — top tracks or radio for an artist
- `play_song(query: str)` — search-and-play first match
- `play_album(query: str)` — exact-match preferred
- `play_playlist(name: str)` — own user's playlists (search via `spotipy.current_user_playlists()`)
- `queue_song(query: str)` — adds to queue without interrupting current
- Maybe: `what_is_playing()` — already covered by `get_now_playing` in `jasper/tools/transport.py`?

System-instruction additions for these tools should be terse. A few-shot style: `"User: 'Play some Kanye West.' → [play_artist tool] (says nothing, music starts)."`

## Out of scope for this work session

- Hardware AEC / dual-output ALSA topology — see `docs/audit-pending-followups.md` "Tier 2." Don't touch the asoundrc fan-out attempt.
- Removing `NO_INTERRUPTION` from `gemini_session.py:_build_config` — gated on AEC working.
- Conversational follow-up window — gated on AEC working.
- Un-duck-on-`turn_complete` UX fix — queued for later.
- Wake-word threshold tuning, refractory, end-of-utterance silence, no-speech-abort — already tuned for the current acoustic state. Don't fiddle.
- Pre-roll, idle context reset, generation config (`temperature=0.3`, `thinking_level=low`) — all set; don't change.

## Useful context, briefly

- **Persistent Gemini Live session** with manual VAD + activity_start/end. **Multi-turn within one connection works** — context is shared until `JASPER_LIVE_CONTEXT_RESET_SEC` (currently 60 s) of idle, then auto-resets. Don't disable this; the cross-context bleed it prevents was a real bug.
- **`unack_activity_ends` counter** with 30 s age-out — handles the case where Gemini silently drops a turn (no `turn_complete` from server). Don't remove.
- **Tool calls work today** — `weather`, `subway`, `time` (via system-instruction injection, no tool needed), `audio` (CamillaDSP volume — wrong knob for *user* volume), some `transport` and `spotify` infrastructure exists. Verify what's actually wired before writing new code.
- **The system instruction has a "do not preface, do not ask follow-ups, do not invite further conversation" block + 5 positive few-shots and 3 negative counter-examples.** Match that style when adding new tool guidance.
- **Spend cap is at $10/day** in `/etc/jasper/jasper.env`. Plenty of headroom.
- **Wake threshold is 0.3** — very permissive. Music vocals can fire it; the daemon recovers via the 5 s no-speech-abort.
- **Daemon log location:** `/etc/systemd/system/jasper-voice.service` runs `/opt/jasper/.venv/bin/jasper-voice`. Source of truth at `/opt/jasper/jasper/`. Logs via `journalctl -u jasper-voice`.

## Pi access

- SSH key already deployed: `ssh pi@jasper.local` (no password)
- sudo password if you need it: `pipass`
- The harness has Production-Read denials on `/etc/jasper/jasper.env` — to change env vars, write a small script locally, `scp` it to `/tmp/`, then `ssh pi@jasper.local "echo 'pipass' | sudo -S bash /tmp/script.sh"`. See `scripts/switch-gemini-model.sh` for the canonical shape.

## Deploy loop

```sh
# laptop → Pi sync
rsync -avz --delete --exclude .venv --exclude __pycache__ --exclude '.git/' --exclude 'logs/*' ./ pi@jasper.local:/home/pi/jts/
# Pi → /opt/jasper deploy + restart
ssh pi@jasper.local "echo 'pipass' | sudo -S rsync -a --delete --exclude __pycache__ /home/pi/jts/jasper/ /opt/jasper/jasper/ && echo 'pipass' | sudo -S systemctl restart jasper-voice"
# tail
ssh pi@jasper.local "echo 'pipass' | sudo -S journalctl -u jasper-voice -f"
```

Or use the helpers: `bash scripts/fetch-pi-logs.sh`, `bash scripts/tail-pi-logs.sh`.

## Definition of done

- "Volume up", "volume down", "volume 30 percent", and "mute" all work and adjust moOde's volume (verifiable in moOde web UI or via `/command/?cmd=get_volume`).
- "Next song", "previous song", "pause", "resume" work when Spotify Connect or MPD is the active source. Graceful failure (with a spoken explanation) when AirPlay is active and the user asks for transport.
- "Play Kanye West" / "play [song name]" / "play [playlist name]" works in BOTH cases:
  - Pi is the active Spotify Connect device → music plays via Pi → CamillaDSP → dongle → speakers.
  - Phone is the active Spotify Connect device, AirPlay-cast to the Pi → command targets the phone, phone changes track, AirPlay stream content updates seamlessly.
- All new tools have terse, anti-conversational system-instruction additions following the existing pattern.
- New tests under `tests/` for any non-trivial routing logic (e.g. the "which device is active" decision tree). Don't add hardware-dependent tests.
- Commit to `claude/camilla-dsp-voice-plan-QRdsE` with clear messages. Don't push unless explicitly asked.

## Anti-patterns to avoid

- Don't reinvent `MoodeClient` or `spotipy` calls — they exist; extend them.
- Don't bypass the `ToolRegistry` — every tool goes through it; that's how Gemini sees the function declarations.
- Don't change CamillaDSP's `master_gain` from a tool — that's the daemon's ducking knob, not user volume.
- Don't add tools that ask for confirmation ("Are you sure you want to play Kanye West?"). The system instruction explicitly forbids it; voice should feel direct.
- Don't try to control AirPlay — you can't from the daemon. Be honest with the user.
