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
3. Decides whether the source is **push-mode** (it has a slider we
   can drive — camilla pinned at 0 dB) or **camilla-as-master** (we
   can't drive its slider — camilla carries listening_level on the
   −50..0 dB scale). The decision lives in
   `_camilla_carries_level(source)`:
   - **IDLE** → camilla-as-master
   - **AIRPLAY** → camilla-as-master *always* (see "AirPlay is always
     camilla-as-master" below for the why)
   - **SPOTIFY** → push-mode (Web API)
   - **BLUETOOTH** → push-mode (AVRCP via bluez-alsa)
4. Pushes the level to the right attenuator:
   - **AirPlay** → CamillaDSP `main_volume` (linear over −50..0 dB)
   - **Spotify** → Spotify Web API `PUT /me/player/volume` via the multi-account `spotify_router` (librespot 0.8.0 has no local control HTTP, so we go through Spotify's cloud → spirc → librespot, ~200-800ms latency, also propagates to all Spotify clients so the app slider visibly moves)
   - **Bluetooth** → `org.bluez.MediaTransport1.Volume` property on the active a2dpsnk path (uint16 0..127)
   - **Idle** → CamillaDSP `main_volume`
5. In push-mode, **CamillaDSP `main_volume` is pinned at 0 dB** so
   there's no double-attenuation. In camilla-as-master mode (idle or
   AirPlay), `main_volume` IS the user-facing knob.

### Inbound observation

A 1 Hz poller (`jasper.volume_observers.VolumeObserver`) reads each
source's current value and feeds detected changes into
`coordinator.observe_source_volume(...)`:

- AirPlay: `busctl get-property` for `AirplayVolume` (read but
  unconditionally ignored downstream — see exception below)
- Spotify: read `/run/librespot/state.json` (written atomically by librespot's `--onevent` hook on every player event; `volume` field is raw 0-65535, mapped to percent)
- Bluetooth: `bluealsa-cli list-pcms` to find the transport, then
  `busctl get-property` for `Volume`

When the user moves the Spotify app slider or BT volume, the next
poll picks it up (sub-second latency) and the coordinator updates
`listening_level` accordingly.

**Exception: AirPlay observations are unconditionally skipped.** The
sender's slider sits *upstream* of camilla in the audio chain —
honoring it as the user's master-volume intent would mean the
canonical level bounces around with whatever the phone/Mac is
showing, disconnected from what camilla (the actual master) is
doing. So we ignore the iPhone/Mac AirPlay slider and let the dial
and voice tools own the canonical JTS speaker level. The sender
slider remains upstream trim, not the JTS volume source of truth.

### Echo prevention

When the coordinator writes to a source, the source emits a property-
changed event that the observer also sees. We don't want this to look
like user input. So every outbound write timestamps itself per source
(`_OutboundStamp`), and on observation:

```
if observed.source.was_written_by_us within ECHO_WINDOW_SEC (500 ms):
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

## AirPlay is always camilla-as-master

shairport-sync exposes `SetAirplayVolume` as a method that should
forward volume changes back to the AirPlay sender via the legacy DACP
back-channel. In modern AirPlay 2 sessions, this is not a reliable
control surface. Real hardware validation on 2026-05-14 showed both
macOS and iOS sessions reporting:

```
RemoteControl.Available = false
SETUP AP2 no Active-Remote information
SETUP AP2 doesn't include DACP-ID string information
```

Calling `SetAirplayVolume` returned success at the DBus layer but left
`AirplayVolume` unchanged and did not move the sender UI or audible
level. This matches upstream shairport-sync issue #1822: iOS 17.4 /
macOS 14.4 stopped providing the DACP-ID / Active-Remote headers in
AirPlay 2 mode, so DBus/MPRIS receiver-originated commands are ignored
or impossible. shairport-sync's AirPlay 2 documentation also states
that modern remote-control facilities are not implemented.

So instead of trying to drive the AirPlay sender's slider, we
attenuate at camilla — `main_volume` sits *downstream* of
shairport's receiver in the audio chain (shairport → snd-aloop →
camilla → DAC), so it reduces what the speakers actually emit
regardless of what the sender chose to send. The dial behaves like
a master volume on every source.

**Trade:** the iPhone/Mac AirPlay slider on the sender does not
visibly move when the dial turns. The audio at the speaker does.
Voice volume control and the rotary dial share the same coordinator
path, so both remain reliable during AirPlay.

**The four transitions** at the camilla-as-master / push-mode
boundary all flow through `apply_active_source_transition`:

| Edge | What happens |
|---|---|
| camilla-as-master → push-mode (e.g. AirPlay → Spotify) | camilla → 0 dB (clear residual), then push level to new source |
| push-mode → camilla-as-master (e.g. Spotify → AirPlay) | camilla → percent_to_db(level) (take over) |
| push → push (e.g. Spotify → BT) | camilla already at 0 dB; push level to new source |
| camilla → camilla (idle ↔ AirPlay) | no change; camilla already carries level |

The first edge is the one users notice: without it, the residual
camilla attenuation from AirPlay mode would compound with the new
source's own slider when they switch (e.g. start Spotify Connect
and find the speaker mysteriously twice as quiet as expected).

**Cross-process staleness fix.** `apply_active_source_transition`
calls `_refresh_from_disk()` before dispatch. The control daemon
(dial / HTTP) writes listening_level to disk on every twist; without
the refresh, voice_daemon's in-memory cache lags and a transition
that fires between voice operations would dispatch a stale level.

If the sender slider is below 100%, it pre-attenuates upstream of
camilla and the dial position stops being a 1:1 read of perceived
loudness. JTS still responds audibly; the user may just turn the dial
further. Do not add hidden fallback behavior that sometimes treats
AirPlay as push-mode — that recreates two competing product contracts.

A previous iteration tried to make AirPlay push-mode by calling
`SetAirplayVolume` and treating `AirplayVolume` observations as
canonical. That worked in unit tests but failed on real iOS/macOS
AirPlay 2 sessions for the protocol reasons above. The code now leans
fully into the robust contract: AirPlay is camilla-as-master.

### Future research: Bose/HomePod-style reflection

Commercial AirPlay 2 speakers such as Bose can reflect receiver-side
hardware volume back into the iPhone/Mac slider. That does not appear
to use shairport-sync's legacy DACP/DBus path. Public reverse-
engineering points to the modern AirPlay 2 control plane: `/info`
capabilities such as `initialVolume` / `volumeControlType`,
`POST /command`, event/data channels, HomeKit/HAP-derived encryption,
and MRP-style protobuf messages.

If we revisit AirPlay slider reflection, keep it separate from the
production volume path and test it as protocol research:

1. Capture Bose or HomePod traffic while changing physical speaker
   volume and compare it with JTS/shairport.
2. Look specifically for `/info` volume capability fields,
   `POST /command`, event/data-channel traffic, and MRP volume messages.
3. Check whether shairport-sync receives, ignores, or never establishes
   the needed channel.
4. Prototype below the Python coordinator layer, likely as a
   shairport-sync patch or AirPlay 2 sidecar. Do not reintroduce
   `SetAirplayVolume` as the normal JTS AirPlay volume path unless
   hardware proves receiver-originated slider reflection works on
   current iOS/macOS.

Useful references:

- https://github.com/mikebrady/shairport-sync/issues/1822
- https://github.com/mikebrady/shairport-sync/blob/master/AIRPLAY2.md
- https://emanuelecozzi.net/docs/airplay2/rtsp/
- https://emanuelecozzi.net/docs/airplay2/protocols/
- https://pyatv.dev/documentation/protocols/
- https://openairplay.github.io/airplay-spec/audio/volume_control.html

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
