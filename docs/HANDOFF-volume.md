# HANDOFF — Volume coordination

Volume control on a Pi-based smart speaker has more moving parts than
"set the slider" suggests. The user's iPhone, the Spotify app, the
Bluetooth phone, the rotary dial, the voice tool, and the always-on
CamillaDSP all attenuate audio independently. This document explains
how `jasper.volume_coordinator` makes them feel like one knob.

If you're modifying anything in this subsystem, read this first.

## The problem

Several attenuators sit on the audio chain in series:

```
track_loudness × airplay_sender_vol × spotify_connect_vol
    × bt_avrcp_vol × camilla_main_volume → DAC
```

Most of these are **upstream of CamillaDSP**. If the iPhone slider is
at 30%, that's a ~20 dB pre-attenuation. Moving CamillaDSP's
`main_volume` between 0% and 100% only spans the remaining 70% of
perceived loudness.

The pre-coordinator behaviour: voice tool's `set_volume(percent)` only
adjusted CamillaDSP. So "Hey Jarvis, set volume to 80%" with the
iPhone slider at 30% sounded like 24% — a confusing disconnect.

## The model

There is **one canonical `listening_level` (0-100)** persisted in
`/var/lib/jasper/speaker_volume.json`. It's what every input writes
and what every read reports. The coordinator's job is to keep it in
sync with whatever attenuator is actually doing the work.

### Outbound dispatch

When the voice tool / dial / "louder" wants to change volume:

1. Coordinator queries `backend.active_renderers()`.
2. Picks the active source: priority `airplay > spotify > bluetooth > idle`.
3. Pushes the level to that source's own attenuator:
   - **AirPlay** → `org.gnome.ShairportSync` DBus method `SetAirplayVolume(d)` (range −30..0 dB)
   - **Spotify** → Spotify Web API `PUT /me/player/volume` via the multi-account `spotify_router` (librespot 0.8.0 has no local control HTTP, so we go through Spotify's cloud → spirc → librespot, ~200-800ms latency, also propagates to all Spotify clients so the app slider visibly moves)
   - **Bluetooth** → `org.bluez.MediaTransport1.Volume` property on the active a2dpsnk path (uint16 0..127)
   - **Idle** → CamillaDSP `main_volume` (linear over −50..0 dB)
4. While a source is active, **CamillaDSP `main_volume` is pinned at 0 dB**
   so there's no double-attenuation. Idle path returns `main_volume`
   to its role as the user-facing knob.

### Inbound observation

A 1 Hz poller (`jasper.volume_observers.VolumeObserver`) reads each
source's current value and feeds detected changes into
`coordinator.observe_source_volume(...)`:

- AirPlay: `busctl get-property` for `AirplayVolume`
- Spotify: read `/run/librespot/state.json` (written atomically by librespot's `--onevent` hook on every player event; `volume` field is raw 0-65535, mapped to percent)
- Bluetooth: `bluealsa-cli list-pcms` to find the transport, then
  `busctl get-property` for `Volume`

When the user moves their iPhone slider, the Spotify app slider, or
the BT volume, the next poll picks it up (sub-second latency) and
the coordinator updates `listening_level` accordingly.

### Echo prevention

When the coordinator writes to a source, the source emits a property-
changed event that the observer also sees. We don't want this to look
like user input. So every outbound write timestamps itself per source
(`_OutboundStamp`), and on observation:

```
if observed.source.was_written_by_us within ECHO_WINDOW_SEC (500 ms):
    if observed_level matches what we wrote (±1pp):
        ignore
```

500 ms covers DBus round-trip + bus latency on a busy Pi 5 with
generous slack. It's short enough that a real user-touched slider
movement landing just after our write isn't swallowed.

### Why polling, not DBus subscriptions

`busctl get-property` is the proven pattern in this codebase
(`jasper.renderer`, `jasper.mux`). DBus PropertiesChanged
subscriptions would need a new dependency (dbus-next) and a more
complex error model (long-lived subscriptions to manage). For our
use case the ergonomic wins don't materialise: source-side volume
changes happen at finger-touch speed, and 1 Hz polling captures
everything with sub-second latency.

## The boot path

`VolumeCoordinator.initialize()` is called once at voice_daemon
startup:

1. Load `VolumeRecord` from disk (handles v1→v2 migration internally —
   v1 files derive `listening_level` from `main_volume_db` percent).
2. Compute the boot target via `regress_listening_level_if_stale`:
   - **No record** → `first_boot_default_pct` (50% by default).
   - **Fresh** (now − last_used_at < `stale_after_sec`) → use as-is.
   - **Stale + extreme** → clamp into `[safe_low_pct, safe_high_pct]`.
   - **Stale + safe** → use as-is.
3. Apply via dispatch (whichever source is active, or camilla if idle).
4. Persist with `mark_user_change=False` — boot writes do NOT bump
   `last_used_at`. Otherwise every reboot would reset the staleness
   clock and yesterday's bedtime 90% would never get clamped.

`stale_after_sec` is tied to `last_used_at` (last user-initiated
change), not `updated_at` (last write of any kind). This decouples
"how recently the user actually touched volume" from "how recently
the daemon wrote to disk".

## The two consumers

**`jasper.tools.audio.make_audio_tools(coordinator)`** — voice tool
surface. Five tools: `get_volume`, `set_volume`, `adjust_volume`,
`mute`, `unmute`. Each is a thin wrapper around the coordinator's
public API.

**`jasper.control.server`** — HTTP surface for the rotary dial and
LAN automation. Builds a fresh `VolumeCoordinator` per request via
`_with_coordinator` (matches the pre-existing `_toggle_transport`
pattern). Both legacy `delta_db`/`db` payloads and newer
`delta_percent`/`percent` payloads are accepted; the legacy ones
convert at the HTTP boundary.

Both daemons converge through the persistence file. voice_daemon's
coordinator runs the inbound observers; control_daemon's
coordinator does not (it doesn't need them — it's a write surface).

## Hearing-safety belt

The coordinator pushes commands; it doesn't enforce safety on its own.
Multiple guardrails sit on top:

- `regress_listening_level_if_stale` clamps stale + extreme values
  into `[20%, 70%]` by default.
- `TtsPlayout.set_gain_db` enforces a `MAX_TTS_GAIN_DB = -6 dB`
  hardware ceiling on the TTS path independent of any volume math.
- `JASPER_TTS_GAIN_DB` is validated `<= 0` at config-load time.
- `volume_limit: 0` in CamillaDSP YAML — `main_volume` cannot go
  positive.

Don't bypass any of these. The user is volume-sensitive ("don't blow
my eardrums out"); defense in depth is the design.

## What's NOT here

- **No DBus subscription library**. Polling at 1 Hz is the model; if
  someone wants to switch to PropertiesChanged subscriptions later,
  `dbus-next` is the recommended async option.
- **No master-source mute via -144 dB sentinel**. AirPlay
  `AirplayVolume = -144` is a documented "muted" sentinel; we
  treat it as effective silence (clamped to AIRPLAY_DB_MIN → 0%).
  If a future tool wants explicit mute semantics, add a parameter
  to the coordinator's `mute()`.
- **No iPhone-side slider visual update on Jarvis-initiated changes**.
  This is an AirPlay protocol limitation, not a JTS limitation.
  Audio attenuates correctly; the slider widget on the phone shows
  a stale position until the user touches it.

## Changes that need this doc

If you're adding a fourth audio source, hook into:
- `Source` enum in `volume_coordinator.py`
- `_active_source()` priority chain
- One `_set_<source>` dispatcher
- One `_read_<source>_*` observer reader
- Echo-prevention: `_stamp_outbound(Source.NEW, level)` in the
  dispatcher

If you're changing the staleness semantics (idle reset thresholds),
the field of authority is `last_used_at` in the persistence record,
written ONLY on user-initiated changes (set/adjust/observe), NOT
on boot restore.
