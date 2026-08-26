# ADR-0176: The AirPlay sender slider is not a control surface — AirPlay 2 took the back-channel away

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-volume.md was trimmed to its
  operational spine)

## Context

[ADR-0151](0151-a-new-source-is-camilla-master-until-it-proves-an-observable-volume-surface.md)
says a source is Camilla-master until it *proves* a writable, observable
volume surface, and lists AirPlay as Camilla-master. It does not record the
hardware evidence that settled AirPlay specifically, and that evidence is the
only thing standing between this repo and a third attempt at the same design.

shairport-sync exposes `RemoteControl.SetAirplayVolume(double)`, which should
forward a receiver-side volume change back to the sender over the legacy DACP
back-channel. A 2026-05 redesign brief
([historical/volume-control-redesign-2026-05.md](../historical/volume-control-redesign-2026-05.md))
proposed building the whole volume architecture on it: every active renderer
push-mode, Camilla demoted to correction/ducking/safety/idle. It read as the
obviously correct product shape, and it passed in unit tests.

Hardware validation on 2026-05-14 disproved it. Both macOS and iOS AirPlay 2
sessions reported:

```
RemoteControl.Available = false
SETUP AP2 no Active-Remote information
SETUP AP2 doesn't include DACP-ID string information
```

`SetAirplayVolume` returned success at the DBus layer and changed nothing —
not `AirplayVolume`, not the sender UI, not the audible level. This matches
upstream shairport-sync issue
[#1822](https://github.com/mikebrady/shairport-sync/issues/1822): iOS 17.4 /
macOS 14.4 stopped supplying the `DACP-ID` / `Active-Remote` headers in
AirPlay 2 mode, so receiver-originated DBus/MPRIS commands are ignored or
impossible. shairport-sync's own
[AIRPLAY2.md](https://github.com/mikebrady/shairport-sync/blob/master/AIRPLAY2.md)
states that modern remote-control facilities are not implemented.

## Decision

**AirPlay is Camilla-master, in both directions, unconditionally.** JTS never
writes the sender's volume, and JTS never treats a sender volume reading as
canonical.

Outbound: `main_volume` sits *downstream* of shairport-sync in the chain
(shairport → snd-aloop → fan-in → CamillaDSP → outputd → DAC), so attenuating
there reduces what the speakers emit regardless of what the sender chose to
send. JTS controls behave like a master volume on every source.

Inbound: `VolumeObserver` reads `AirplayVolume` for diagnostics and the
coordinator skips it. Honouring it would make the canonical level track
whatever the phone is showing while Camilla — the actual master — is doing
something else. The sender slider is upstream trim, not the source of truth.

**Do not add a hidden fallback that sometimes treats AirPlay as push-mode.**
Two competing product contracts on one source is the failure this ADR exists
to prevent; a per-session or per-capability switch is that failure wearing a
condition.

## Consequences

- **The trade is a stale widget.** The iPhone/Mac AirPlay slider does not move
  when a JTS control changes volume. The audio at the speaker does. Voice,
  web, and remote all share the coordinator path, so they stay reliable during
  AirPlay.
- **A pre-attenuating sender costs the user range.** With the sender below
  100%, the JTS control position stops being a 1:1 read of perceived loudness
  and the user may have to raise JTS further. This is visible and recoverable;
  a silently-ignored volume write is not.
- **`-144` stays a sentinel, not a gain.** AirPlay's documented mute sentinel
  is clamped up to `AIRPLAY_DB_MIN` (−30 dB) and read as effective silence.
  Source-volume 0% mute is owned by `VolumeCoordinator` through Camilla
  `main_mute`, never by a source-specific sentinel value.
- **Rejected: making the disproof configurable.** No knob re-enables
  `SetAirplayVolume`. A knob would let a fresh install rediscover 2026-05.
- **What would reopen this.** Only hardware evidence, and only from the modern
  AirPlay 2 control plane — not the DACP path this ADR closed. Commercial
  AirPlay 2 speakers (Bose, HomePod) do reflect receiver-side volume into the
  sender slider; public reverse-engineering points at `/info` capabilities
  (`initialVolume`, `volumeControlType`), `POST /command`, event/data
  channels, HAP-derived encryption, and MRP-style protobuf messages, none of
  which shairport-sync implements. Reopening means: capture a Bose or HomePod
  session against JTS/shairport, find the volume capability fields and command
  traffic, establish whether shairport-sync receives, ignores, or never opens
  the channel, and prototype *below* the Python coordinator — a shairport-sync
  patch or an AirPlay 2 sidecar. Starting points:
  [rtsp](https://emanuelecozzi.net/docs/airplay2/rtsp/),
  [protocols](https://emanuelecozzi.net/docs/airplay2/protocols/),
  [pyatv](https://pyatv.dev/documentation/protocols/),
  [openairplay volume_control](https://openairplay.github.io/airplay-spec/audio/volume_control.html).
  Until such a capture exists, this ADR holds.
