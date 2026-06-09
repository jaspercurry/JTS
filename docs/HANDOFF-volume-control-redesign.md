# HANDOFF — Volume control redesign

> **Superseded by hardware validation on 2026-05-14.** This brief
> proposed treating AirPlay as a normal push-mode renderer via
> shairport-sync `RemoteControl.SetAirplayVolume`. Real iOS/macOS
> AirPlay 2 sessions did not support receiver-originated volume
> reflection through that path: shairport reported missing
> `DACP-ID` / `Active-Remote`, `RemoteControl.Available=false`, and
> `SetAirplayVolume` returned successfully without changing the sender
> slider or audible level. The production architecture is now documented
> in [`docs/HANDOFF-volume.md`](HANDOFF-volume.md): AirPlay is
> camilla-as-master; Spotify and Bluetooth remain push-mode. Keep this
> file only as historical context for the disproven redesign.

This is a transition brief for the next volume-control pass. It is
intentionally split into:

- what the codebase does today,
- the product-level destination,
- the concrete changes needed to get there.

Read this alongside `docs/HANDOFF-volume.md`, which is the canonical
description of the volume system that is currently shipped. That existing
handoff is intentionally not overwritten yet: it explains the code that
the next agent will actually find in `jasper/volume_coordinator.py` and
`jasper/volume_observers.py`. This document is the redesign brief layered
on top of it.

After the redesign is implemented and validated on hardware, this file
should be collapsed into the canonical shipped-state volume document
and the historical/current-state section should be removed.

## How to use this document

For a fresh context window, use this read order:

1. `README.md` for the full system architecture.
2. `docs/HANDOFF-volume.md` for the current source-aware volume
   coordinator, including the AirPlay-as-Camilla exception.
3. `docs/audio-paths.md` for the music-vs-TTS split and why Camilla
   `main_volume` currently affects music but not TTS.
4. This document for the desired destination and migration plan.

The key thing to understand before editing code: `HANDOFF-volume.md`
describes the current implementation, not the desired final architecture.
The final architecture should remove the need for a separate transition
brief like this.

## Current state — what exists today

JTS already has the right core idea: the user sees one speaker volume,
`listening_level` from 0-100, persisted in
`/var/lib/jasper/speaker_volume.json`. The implementation lives mostly in
`jasper/volume_coordinator.py`, with source observations in
`jasper/volume_observers.py`.

The current audio path is:

```text
AirPlay / Spotify Connect / Bluetooth
        -> snd-aloop
        -> CamillaDSP main_volume + filters
        -> pcm.jasper_out / dmix
        -> Apple USB-C DAC
```

TTS now enters `jasper-fanin` before CamillaDSP, and fan-in owns
voice-session ducking so the assistant itself is not attenuated by the
duck. Camilla `main_volume` remains the steady-state listening-level
knob for sources carried by Camilla. Fan-in compensates assistant
loudness by matching to measured pre-duck content rather than assuming
one volume knob explains everything.

### File map

| File | Current role |
|---|---|
| `docs/HANDOFF-volume.md` | Canonical shipped-state explanation of current source-aware volume coordination. Read it first to understand what the code does today. It should be rewritten after this redesign lands. |
| `docs/audio-paths.md` | Explains why music and TTS have separate paths to the DAC |
| `jasper/volume_coordinator.py` | Owns `listening_level`, active-source selection, outbound volume dispatch, echo prevention, mute state, and Camilla handoff rules |
| `jasper/volume_observers.py` | Polls AirPlay, Spotify, and Bluetooth volume surfaces at 1 Hz and feeds changes into the coordinator |
| `jasper/volume_persistence.py` | Atomic persisted volume state, stale-volume regression, `percent <-> dB` mapping |
| `jasper/control/server.py` | HTTP control surface for dial/LAN clients: `/volume`, `/volume/set`, `/volume/adjust`, `/volume/mute` |
| `jasper/tools/audio.py` | Voice tools: `get_volume`, `set_volume`, `adjust_volume`, `mute`, `unmute` |
| `jasper/renderer.py` / `jasper/source_state.py` | Active renderer detection: AirPlay MPRIS, Spotify state file, Bluetooth BlueALSA |
| `jasper/camilla.py` | `CamillaController`, `Ducker`, and cue ducking around `main_volume` |
| `deploy/shairport-sync.conf.template` | AirPlay receiver config into snd-aloop; currently no `ignore_volume_control` setting |
| `deploy/install.sh` | Source-builds shairport-sync 4.3.7 with AirPlay 2, metadata, DBus, and MPRIS enabled |
| `deploy/systemd/librespot.service` | Spotify Connect renderer with softvol/log volume curve and `--onevent` state hook |
| `deploy/systemd/bluealsa-aplay.service.d/jts-output.conf` | Bluetooth A2DP renderer into the shared loopback |
| `deploy/bin/jasper-librespot-event` | Writes `/run/librespot/state.json`, including Spotify volume events |

### Current outbound model

When the dial, voice tool, or LAN API changes volume:

1. `VolumeCoordinator` refreshes `listening_level` from disk.
2. It asks `RendererClient.active_renderers()` which renderer is active.
3. Priority is `airplay > spotify > bluetooth > idle`.
4. It dispatches the new level to the active source.

The dispatch rules are not uniform:

| Active source | Current action |
|---|---|
| Idle | Set CamillaDSP `main_volume` using the `-50..0 dB` mapping |
| AirPlay | Set CamillaDSP `main_volume`; do not push to the AirPlay sender |
| Spotify | Push Spotify Connect volume through Spotify Web API; keep Camilla at `0 dB` |
| Bluetooth | Push BlueZ/BlueALSA AVRCP absolute volume; keep Camilla at `0 dB` |

This means there is one persisted software concept, but not one
protocol-level behavior. Spotify and Bluetooth treat their sender/app
volume as the active session volume. AirPlay treats Camilla as the
active session volume and leaves the iPhone/Mac slider independent.

### Current inbound model

`VolumeObserver` polls:

- AirPlay: `org.gnome.ShairportSync.RemoteControl.AirplayVolume`
- Spotify: `/run/librespot/state.json`, raw `0..65535` mapped to percent
- Bluetooth: `org.bluez.MediaTransport1.Volume`, `0..127`

Spotify and Bluetooth observations update `listening_level` unless they
are echoes of our own recent outbound write.

AirPlay observations are intentionally ignored. The current rationale is
that AirPlay sender volume is upstream of CamillaDSP. If the phone is at
30% and the dial controls Camilla, honoring the phone slider as canonical
would make the persisted level bounce around while Camilla still remains
the real output attenuator.

### How we got here

The current design is a pragmatic source-aware coordinator that evolved
from a simpler Camilla-only volume tool. The old behavior was confusing:
if a source-side slider was already low, "set volume to 80%" only raised
Camilla and still sounded quiet because the upstream sender was
pre-attenuating the signal.

The coordinator fixed that for Spotify and Bluetooth by pushing volume to
the sender/source surface itself:

- Spotify goes through the Spotify Web API because rust `librespot`
  0.8.0 has no local control HTTP.
- Bluetooth goes through the BlueZ/BlueALSA `MediaTransport1.Volume`
  property.

AirPlay was carved out as a special case after empirical testing showed
that shairport-sync accepted `SetAirplayVolume` calls but Apple senders
did not reliably move their slider. The code therefore chose the robust
audible path: attenuate downstream in Camilla, and document that users
should leave the AirPlay sender at 100%.

That made the speaker respond reliably, but it also embedded a hidden
product rule. A normal user will not know that one sender app should be
left at 100% while other sender apps should be treated as the active
volume control. That is the design debt this pass should remove.

### Current concepts that should not survive unchanged

These current concepts exist for understandable reasons, but should be
cleaned up once the redesign is validated:

| Current concept | Why it exists today | What should replace it |
|---|---|---|
| "AirPlay is always camilla-as-master" | Prior testing found `SetAirplayVolume` unreliable, so downstream attenuation was chosen because it always changed speaker loudness | AirPlay should be a normal push-mode adapter using DACP through shairport-sync DBus |
| "Leave the iPhone/Mac AirPlay slider at 100%" | Avoids double attenuation from AirPlay sender volume plus Camilla volume | No hidden user instruction; the AirPlay slider itself is one control surface for JTS volume |
| AirPlay observations are read but ignored | If Camilla is the AirPlay master, honoring sender volume would make `listening_level` disagree with actual output policy | AirPlay observations update `listening_level`, with echo prevention like Spotify/BT |
| Source-transition special cases for AirPlay | AirPlay uses Camilla while Spotify/BT use source sliders, so transitions must clear or restore Camilla differently | One transition model: idle uses Camilla; all active renderers are push-mode |
| Comments saying Camilla is pinned during active playback while AirPlay actually writes Camilla | The file evolved from a cleaner push-mode model, then AirPlay was carved out | Update prose/tests so active playback semantics are true and uniform |
| `HANDOFF-volume.md` as final documentation | It accurately documents shipped behavior today | Rewrite it after implementation so it describes the unified renderer-adapter model |

## Desired destination — product contract

The product contract should be simple:

> JTS has one user-facing speaker volume. Every control surface writes
> and reads that same volume: hardware knob, voice, local HTTP/API, sender
> apps, AirPlay, Spotify Connect, and Bluetooth.

When music is playing, the active renderer's own protocol-level volume
surface should carry the session volume:

| Active source | Destination volume surface |
|---|---|
| AirPlay | AirPlay sender device volume via DACP, exposed by shairport-sync DBus `RemoteControl.SetAirplayVolume(double)` |
| Spotify Connect | Spotify Connect device volume via Spotify Web API / librespot state |
| Bluetooth A2DP | AVRCP absolute volume via `MediaTransport1.Volume` |
| Idle | CamillaDSP `main_volume`, as the remembered level for the next session |

CamillaDSP should become the final device output layer, not the normal
per-source volume substitute. Its responsibilities should be:

- room correction / filters,
- voice ducking,
- hearing-safety clamps,
- idle volume,
- explicit degraded-mode recovery if a renderer cannot satisfy its
  contract.

It should not be the everyday AirPlay volume while the iPhone/Mac slider
is left to drift independently.

### AirPlay contract

The intended AirPlay contract is standard DACP volume sync:

- Sender to receiver: AirPlay sends `SET_PARAMETER volume: <dB>` to
  shairport-sync.
- Receiver to sender: JTS asks shairport-sync to call DACP
  `setproperty?dmcp.device-volume=<dB>` by invoking
  `org.gnome.ShairportSync.RemoteControl.SetAirplayVolume(double)`.
- The AirPlay value uses `0.0` as max, approximately `-30.0` as the
  bottom of the audible slider, and `-144.0` as the mute sentinel.

The repo already builds a compatible shairport-sync:

```text
deploy/install.sh:
  shairport-sync 4.3.7
  --with-airplay-2
  --with-metadata
  --with-dbus-interface
  --with-mpris-interface
```

Upstream shairport-sync 4.3.7 exposes:

```text
org.gnome.ShairportSync.RemoteControl.SetAirplayVolume(double)
org.gnome.ShairportSync.RemoteControl.AirplayVolume
org.gnome.ShairportSync.RemoteControl.Available
```

The redesign should treat this as the AirPlay adapter's contract, not as
an experimental side path. If it fails on real hardware, that is a
renderer health problem to diagnose and surface, not a reason to let two
independent user volumes coexist silently.

### User experience target

The boring, correct experience:

- Turn the physical knob during AirPlay: the iPhone/Mac AirPlay volume
  moves and the speaker loudness follows.
- Move the iPhone/Mac AirPlay slider: the dial/web/voice-reported volume
  reflects the same level.
- Turn the knob during Spotify Connect: the Spotify app's JTS volume
  moves and the speaker loudness follows.
- Move the Spotify app's slider: JTS `listening_level` updates.
- Use Bluetooth phone volume buttons: JTS `listening_level` updates.
- Say "set volume to 40%": the active sender/app volume changes to 40%.
- With no music active: knob/voice changes the remembered idle level;
  the next active source receives that level when it starts.

No source should require a hidden instruction like "leave this slider at
100%."

## Proposed implementation shape

### 1. Introduce explicit renderer volume adapters

Refactor source-specific branches into a small adapter contract:

```text
VolumeAdapter:
  source: Source
  is_active() -> bool
  read_level() -> int | None
  set_level(level: int) -> VolumeWriteResult
  health() -> VolumeAdapterHealth
```

Suggested adapters:

- `AirPlayVolumeAdapter`
- `SpotifyVolumeAdapter`
- `BluetoothVolumeAdapter`
- `IdleCamillaVolumeAdapter`

The coordinator should own policy and persistence. Adapters should own
protocol details.

This gives the code a direct correspondence to the product model:
one canonical volume, multiple protocol adapters.

### 2. Make AirPlay push-mode

Replace the current `_set_airplay -> _set_camilla` behavior with:

1. Map `listening_level` to AirPlay dB using the existing
   `listening_level_to_airplay_db`.
2. Call shairport-sync DBus:

   ```sh
   busctl --system call \
     org.gnome.ShairportSync \
     /org/gnome/ShairportSync \
     org.gnome.ShairportSync.RemoteControl \
     SetAirplayVolume d -- -12.0
   ```

3. Stamp the outbound write for echo prevention.
4. Keep Camilla at calibrated unity for normal AirPlay playback.

The current AirPlay mapping helpers can remain initially:

- `0% -> -30 dB`
- `100% -> 0 dB`
- treat `-144 dB` as mute/silence, not a normal gain value.

Later, if the feel is wrong, adjust the transfer curve as a product
decision. Do not mix that with the architecture change.

### 3. Honor inbound AirPlay volume

Change `observe_source_volume(Source.AIRPLAY, native_value)` from an
unconditional skip into a normal adapter observation:

1. Convert AirPlay dB to `listening_level`.
2. Ignore if it is our own echo inside the echo window.
3. Persist `listening_level`.
4. Do not immediately write it back to AirPlay.

This makes the AirPlay sender slider a first-class control surface, same
as Spotify and Bluetooth.

### 4. Pin Camilla consistently during active playback

In the destination model:

- Idle: Camilla carries `listening_level`.
- Active renderer: active renderer carries `listening_level`; Camilla is
  `0 dB` except for ducking/safety/correction.

That removes the current AirPlay boundary special cases:

- AirPlay -> Spotify no longer needs to clear residual AirPlay Camilla
  attenuation, because AirPlay should not have placed user volume in
  Camilla.
- Spotify -> AirPlay no longer hands Camilla back to AirPlay, because
  AirPlay becomes push-mode.
- Idle -> any active renderer pins Camilla to `0 dB` and pushes the
  remembered level to the renderer.
- Any active renderer -> idle hands the remembered level back to Camilla.

Voice ducking still needs care. `Ducker.restore()` should continue asking
the coordinator for the correct Camilla target:

- `0 dB` while a renderer is active,
- `percent_to_db(listening_level)` while idle.

### 5. Decide what `ignore_volume_control` should mean

The shairport-sync config currently does not set
`ignore_volume_control`. In the destination model, evaluate setting:

```conf
general = {
    ignore_volume_control = "yes";
};
```

The reason to enable it: AirPlay sender volume should become a command
surface for the JTS master level, not an additional PCM attenuation stage
inside shairport-sync. With `ignore_volume_control`, JTS can receive and
mirror sender volume changes while keeping actual output gain controlled
by the unified volume policy.

Validate this carefully on hardware:

- sender slider movement still updates `AirplayVolume`,
- `SetAirplayVolume` still moves the sender slider,
- shairport-sync no longer applies a second software/hardware
  attenuation internally,
- Camilla remains `0 dB` during active AirPlay except for ducking.

If `ignore_volume_control` creates bad initial-volume behavior, solve
that at session start by pushing the current `listening_level` to
AirPlay when `Active` transitions true.

### 6. Productize degraded states instead of hiding them

Do not silently replace a broken renderer-volume contract with another
independent volume forever. That recreates the multi-volume problem.

If an adapter cannot satisfy its contract:

- mark its health as degraded,
- expose it in `/state`,
- surface it in `jasper-doctor`,
- log enough detail for diagnosis,
- recover automatically when the protocol surface becomes available
  again.

Temporary protection is fine; hidden alternate semantics are not. If the
AirPlay adapter cannot move the sender volume, that should be visible as
"AirPlay volume sync degraded," not silently redefined as "AirPlay uses
Camilla and the phone slider is stale."

## Concrete work list

1. Add adapter abstraction and move current source-specific read/write
   logic behind it.
2. Implement AirPlay `set_level` using shairport-sync
   `RemoteControl.SetAirplayVolume(double)`.
3. Change AirPlay observation to update `listening_level` like Spotify
   and Bluetooth.
4. Update active-source transition rules so every active renderer is
   push-mode and only idle is Camilla-as-master.
5. Revisit `deploy/shairport-sync.conf.template` and decide whether to
   set `ignore_volume_control = "yes"` for the final architecture.
6. Add adapter health to `/state`.
7. Add or update tests:
   - AirPlay outbound calls DBus instead of Camilla.
   - AirPlay inbound updates `listening_level`.
   - AirPlay own echoes are ignored.
   - idle -> AirPlay pins Camilla at `0 dB` and pushes level.
   - AirPlay -> idle restores Camilla to persisted level.
   - `Ducker.restore()` returns `0 dB` for every active renderer.
8. Update docs after validation:
   - collapse this transition brief into `docs/HANDOFF-volume.md`,
   - remove the historical "leave AirPlay sender at 100%" guidance,
   - update `docs/audio-paths.md` if Camilla's role changes in prose,
   - update `docs/HANDOFF-voice-music-control.md` so its volume summary
     matches the final model.

## Documentation cleanup after validation

Once the work is implemented and hardware-validated, do a documentation
cleanup pass in the same PR or immediately after:

1. Rewrite `docs/HANDOFF-volume.md` as the final canonical doc.
   It should no longer describe AirPlay as always Camilla-as-master.
2. Move only useful historical context from this file into a short
   "migration note" or delete it if no longer useful.
3. Delete this transition file once the canonical handoff is complete,
   or move it under `docs/historical/` if the implementation history
   remains worth preserving.
4. Update `docs/HANDOFF-voice-music-control.md`; its current volume
   summary already says AirPlay goes through shairport-sync volume, but
   the shipped code currently disagrees. Make it match the validated
   implementation.
5. Update `docs/audio-paths.md` so its description of Camilla is precise:
   idle/final-output/ducking/safety, not normal active-renderer volume.
6. Search for and remove stale phrases:
   - `AirPlay is always camilla-as-master`
   - `leave the sender slider at 100%`
   - `AirPlay observations are unconditionally skipped`
   - `CamillaDSP main_volume is pinned at 0 dB while a source is active`
     if the surrounding text does not account for the old AirPlay
     exception.

## Design principles for the implementation

- One user volume. Multiple protocol adapters.
- Sender sliders are first-class control surfaces, not competing volumes.
- Camilla is final output policy: correction, safety, ducking, idle.
- Failures should be recoverable and observable, not silently papered
  over with different semantics.
- Keep the mapping simple first. Tune perceptual curves only after the
  architecture is correct.

Last verified: 2026-06-08 (superseded status, canonical volume link, and
current fan-in assistant-loudness note checked; redesign body otherwise
intentionally not revalidated)
