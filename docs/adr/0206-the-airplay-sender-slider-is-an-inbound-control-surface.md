# ADR-0206: The AirPlay sender slider is an inbound control surface — shairport's volume hook drives the master fader

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
first-class push surface for it. In the source this repo pins —
`SHAIRPORT_SYNC_VERSION="4.3.7"`, commit `0b1c4391` in `deploy/install.sh` —
`general.run_this_when_volume_is_set` is unconditional (no `#ifdef`;
`shairport.c:880-881`, `common.c:1078-1090`). shairport invokes it as
`<command> <float>` with the raw AirPlay volume on every volume message
(`rtsp.c:3640`) and once at stream start (`player.c:2258-2260`). The float
is dB over `[-30, 0]`, with `-144` the documented mute sentinel — sent both
at session edges and when the user mutes at the sender.
`ignore_volume_control = "yes"` makes shairport apply unity gain
(`fix_volume = 65536`) while the hook still fires.
`sessioncontrol.run_this_before_play_begins` (`shairport.c:1079`) fires
ahead of the connect-time volume push, which is the only signal that
distinguishes a session snapshot from a user's nudge.

Two behaviours of real senders shape the design. macOS emits a **burst** of
volume messages during a slider drag or a held volume key, and it flips
`-144.0 ↔ 0.0` around session start and stop — both observed in jts3's
journal.

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

**`-144` is a mute request, delivered as `percent: 0`.** ADR-0176's reading
of the sentinel — clamp it up to `AIRPLAY_DB_MIN` and treat it as effective
silence — is kept exactly, and needs no branch in the hook: `-144` clamps to
`-30 dB`, which is `0 %`, which is the level at which the coordinator asserts
Camilla `main_mute` (`_main_mute_for_level`). So a sender's mute button
really does silence the speaker, and it does so through the coordinator's own
mute machinery rather than a source-specific sentinel — ADR-0176's ruling on
*that* point is preserved verbatim.

`/volume/mute` was rejected as the carrier: it takes no `source`, so an
AirPlay session ending would mute whatever had already taken over the
speaker. Routing the sentinel through `/volume/set` keeps arbitration, which
is what makes the session-edge `-144 ↔ 0.0` flips harmless — they land only
while AirPlay is the active source, and no audio is flowing at those
instants.

**A session's first observation is marked `observation_initial`.**
`run_this_before_play_begins` drops a marker in the runtime directory; the
volume hook consumes it (an `rm` that succeeds is the test) and sets the flag
on that one POST. The coordinator already defers an initial observation while
a mute is latched — the flag USBSINK's bridge uses — so connecting a sender
no longer clears a mute the owner asserted at the speaker, while
volume-up-while-muted still works, because only the session's first post
carries the flag.

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
- **Session start can still be loud on an unmuted box.** shairport fires the
  hook once at stream start, so a sender sitting at 100% takes the master to
  100% on connect. `observation_initial` guards the *muted* box only — that
  is the flag's existing meaning, and widening it is not this ADR's business.
  No clamp is regressed: `devices.volume_limit` stays `0.0`, `set_volume_db`
  still clamps positive writes, and 100% is the same designed maximum the JTS
  UI already reaches. It is the first thing to watch on the hardware run.
- **A sender mute persists past the session.** `-144` at session stop leaves
  the canonical level at 0%, which is where the next source starts until
  something raises it. That is the existing behaviour of every
  source-observed camilla-master level (USB sink's host slider does the same
  at zero), and it is visible and recoverable at every JTS surface — unlike
  the alternative, where a deliberate mute at the sender did nothing at all.
- **Persistence write amplification is accepted.** Each accepted observation
  writes `speaker_volume.json`. The coalescer already thins a drag to roughly
  one write per 200 ms, which is the same order as a human working the web
  slider, so no separate debounce machinery was added.
- **Hook children inherit the unit's `Nice=-10`.** They are short-lived
  `sh`/`awk`/`curl` invocations, so this is recorded for awareness rather
  than acted on; a hook that ever grows real work should renice itself.
- **The stale widget is gone in one direction only.** The sender's slider now
  leads; it still does not follow. Moving volume at the speaker leaves the
  phone's slider where it was — ADR-0176's trade, unchanged.
- **Rejected: routing this through the 1 Hz `VolumeObserver` poll.** The
  reading is already there and enabling it would have been a smaller diff, but
  it costs a second of lag on every nudge, it burns a busctl subprocess per
  tick for a value the sender pushes for free, and `ignore_volume_control`
  changes shairport's own volume handling — the pushed value is the one we can
  reason about.
- **Rejected: posting every message.** Each POST builds a `VolumeCoordinator`
  and talks to CamillaDSP, so a drag would open a burst of those concurrently
  on a 1 GB Pi. The hook publishes its value, takes an `flock`, and re-reads
  the published value each pass, so a burst becomes at most one post per
  200 ms and ends on the newest value.
  `tests/test_airplay_volume_hook.py` fires 21 messages over ~1 s against a
  stub jasper-control and pins the properties rather than a count: fewer
  posts than messages, monotonic through the drag, and the last post equal to
  the last value. A slower box coalesces harder, and the holder re-reads
  after releasing the lock, so both hold either way.
- **Rejected: a knob for any of this.** Per ADR-0176: a switch that sometimes
  treats AirPlay as push-mode is two competing contracts on one source.
  Rollback is deleting three lines from
  `deploy/shairport-sync.conf.template` (the two hooks and
  `ignore_volume_control`), which returns the box to ADR-0176 behaviour
  exactly.
- **Failure is silent by design.** The hook exits 0 on any parse, lock, or
  HTTP failure. A lost volume nudge is harmless and the user simply nudges
  again; a hook that blocks shairport is not harmless. Nothing in the audio
  path depends on it, so there is no cue to play. The one exception to
  "drop it" is a rejected write: `last` advances only on a 2xx, so a 409 or
  a timeout retries the same value on the next pass rather than being
  recorded as delivered.
- **2026-09-01, from the hardware run: session start is a fade-up, not one
  message.** The connect-time push this ADR expected to watch arrived on jts3
  as ~10 messages 200 ms apart, animating the sender's slider from the bottom
  of its scale up to the level the user had actually left it at; replaying it
  walked the master ~28 dB down and back over two seconds. While the
  session-start marker is present the hook now waits for that burst to settle
  — two unchanged 200 ms passes — and adopts the settled level in one post,
  still flagged `observation_initial`. A sender's nudge, and the same ramp
  shape on mute→unmute, carry no marker and still track step by step.
- **2026-09-01, same run: holding the animation is only half of it — restore
  first.** The Mac sends the mute sentinel at session *teardown*, so the
  speaker really is at 0 % between sessions. Holding the next session's
  fade-up then kept it there for the ~2 s the animation takes, with content
  already flowing: silence, then audio. The hook now remembers the last
  non-zero percent it delivered (`airplay-restore.pct`, beside the published
  value) and posts it the moment it claims the session-start marker, before
  the hold — so audio returns at the level the owner last listened at, and the
  settled level is normally that same value and is skipped as already
  delivered. Both of a session's writes carry `observation_initial`. That
  still defers against a mute latched *at the speaker*, which is what the flag
  is for; it does not defer against the teardown's own 0 %, because an
  accepted observation writes a canonical level and clears `pre_mute_level`
  rather than latching a mute. The trade: a user who deliberately muted at the
  sender and then disconnected comes back unmuted at their last real level.
  Accepted — opening a fresh session is play intent, and the mute is one press
  away at either end.
