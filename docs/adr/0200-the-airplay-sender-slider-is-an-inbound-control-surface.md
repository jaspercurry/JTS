# ADR-0200: The AirPlay sender slider is an inbound control surface — shairport's volume hook drives the master fader

- **Date:** 2026-08-31
- **Status:** Accepted. Supersedes
  [ADR-0176](0176-the-airplay-sender-slider-is-not-a-control-surface.md) for
  the **inbound** direction only. ADR-0176 stands unchanged for outbound.

## Context

ADR-0176 closed AirPlay↔master volume in both directions after 2026-05-14
hardware validation showed AirPlay 2 sessions supply no `DACP-ID` /
`Active-Remote`, making `RemoteControl.SetAirplayVolume` a silent no-op.
That finding is about the **back-channel**: JTS cannot write the sender's
slider. ADR-0176 then closed inbound as well, on the grounds that honouring a
sender reading would make the canonical level track the phone while Camilla —
the actual master — did something else.

Inbound does not depend on the back-channel, and shairport-sync offers a
first-class push surface for it. In our pinned 4.3.7 source,
`general.run_this_when_volume_is_set` is unconditional (no `#ifdef`;
`shairport.c:880-881`, `common.c:1078-1090`). shairport invokes it as
`<command> <float>` with the raw AirPlay volume on every volume message
(`player.c:3551`) and once at stream start (`player.c:2258-2260`). The float
is dB over `[-30, 0]`, with `-144` the documented mute sentinel.
`ignore_volume_control = "yes"` makes shairport apply unity gain
(`fix_volume = 65536`) while the hook still fires.

Two behaviours of real senders shape the design. macOS emits a **burst** of
volume messages during a slider drag or a held volume key, and it flips
`-144.0 ↔ 0.0` around session start and stop — both observed in jts3's
journal on 2026-08-31.

## Decision

**AirPlay sender volume is an inbound control surface. Outbound stays
closed.**

The one inbound path is `deploy/bin/jasper-airplay-volume`, wired from the
rendered `/etc/shairport-sync.conf` as `run_this_when_volume_is_set`. It maps
`[-30, 0]` dB linearly onto the canonical percent scale and POSTs
`{"percent": N, "source": "airplay"}` to jasper-control's existing
`/volume/set`. That is the same source-attributed observation surface the USB
gadget's host slider already uses, so `_active_source()` arbitration, the
echo window, the measurement hold and the CamillaDSP clamps all apply with no
new machinery. `VolumeCoordinator` gained one branch — AirPlay is no longer
excluded from `observe_source_volume` — and nothing else.

**`-144` is dropped by the hook, never converted.** The macOS session-start /
session-stop flip would otherwise mute the whole speaker every time a Mac
connects or disconnects. Mute remains the coordinator's own latch through
Camilla `main_mute`, never a source sentinel — ADR-0176's ruling on that
point is preserved, only its clamp-to-`-30` reading is retired.

**Outbound remains closed and unconditional.** A JTS-side volume change moves
CamillaDSP and leaves the sender's slider where it was. ADR-0176's rejection
of a knob that re-enables `SetAirplayVolume`, and its list of what evidence
would reopen that direction, both still hold.

**The DBus `AirplayVolume` poll stays diagnostics-only.** Two inbound writers
for one concern is the failure mode ADR-0176 was written to prevent;
`VolumeObserver` reads and logs, and does not dispatch.

## Consequences

- **The user gets what they reach for.** A Mac's volume keys and the AirPlay
  slider now move the speaker's master volume, through CamillaDSP's 400 ms
  ramp. Every JTS surface (web, voice, accessory) still reads and writes the
  same canonical level, so the sender and the speaker agree — in the one
  direction that is physically available.
- **A side benefit on the S16 lane.** With `ignore_volume_control = "yes"`
  shairport stops applying its own gain, so the AirPlay fan-in lane stays
  full-scale and attenuation happens once, in CamillaDSP's float master. The
  per-sample gain steps shairport's own softvol produced in 16 bits are gone.
- **Session start can be loud, and it clears a mute.** shairport fires the
  hook once at stream start, so a sender sitting at 100% takes the master to
  100% on connect — and because an accepted observation clears
  `pre_mute_level`, it does so even if the speaker was muted. This is the
  approved contract rather than a regression of a clamp:
  `devices.volume_limit` stays `0.0`, `set_volume_db` still clamps positive
  writes, and 100% is the same designed maximum the JTS UI already reaches.
  It is the first thing to watch on the hardware run. The narrower fix, if
  the connect jump proves unwanted, is for the hook to send
  `observation_initial: true`, which defers an observation while a mute is
  latched — deliberately not taken here, because the hook cannot tell a
  session-start message from a user's nudge and that flag would then make
  volume-up-while-muted do nothing.
- **The stale widget is gone in one direction only.** The sender's slider now
  leads; it still does not follow. Moving volume at the speaker leaves the
  phone's slider where it was — ADR-0176's trade, unchanged.
- **Rejected: routing this through the 1 Hz `VolumeObserver` poll.** The
  reading is already there and enabling it would have been a smaller diff, but
  it costs a second of lag on every nudge, it burns a busctl subprocess per
  tick for a value the sender pushes for free, and `ignore_volume_control`
  changes shairport's own volume handling — the pushed value is the one we can
  reason about.
- **Rejected: posting every message.** A drag would open a burst of concurrent
  coordinator constructions on a 1 GB Pi. The hook coalesces behind an atomic
  `mkdir` mutex and re-reads its published value each pass, so a burst becomes
  one post per 200 ms and always ends on the newest value.
- **Rejected: a knob for any of this.** Per ADR-0176: a switch that sometimes
  treats AirPlay as push-mode is two competing contracts on one source.
  Rollback is deleting two lines from
  `deploy/shairport-sync.conf.template`, which returns the box to ADR-0176
  behaviour exactly.
- **Failure is silent by design.** The hook exits 0 on any parse, lock, or
  HTTP failure. A lost volume nudge is harmless and the user simply nudges
  again; a hook that blocks shairport is not harmless. Nothing in the audio
  path depends on it, so there is no cue to play.
