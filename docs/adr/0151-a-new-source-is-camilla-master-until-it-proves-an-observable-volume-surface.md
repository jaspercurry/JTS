# ADR-0151: A new source is Camilla-master until it proves an observable volume surface

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-source-capabilities.md was
  trimmed to its operational spine)

## Context

Exactly one attenuator may carry the user-facing `listening_level` for the
active source. Either JTS pushes the level into the source's own volume surface
(`VolumeMode.PUSH`), or JTS holds it in CamillaDSP and leaves the source at
unity (`VolumeMode.CAMILLA_MASTER`).

Push mode is nicer when it works: the phone's slider and the speaker agree.
It is also the mode that can be wrong in the loud direction. If JTS believes a
source accepted a low level and it did not — the write silently failed, the
device went away, the surface reports a different scale — the content plays at
the source's own level with Camilla wide open.

## Decision

**A new source declares `CAMILLA_MASTER` unless it proves a reliable,
observable, user-facing volume surface that JTS can write. Push is earned, not
assumed.**

Earning it means all three: JTS can *write* the level, JTS can *observe* what
the source actually holds, and the surface is cheap enough to read without
network calls inside a loop.

The shipped assignment in `jasper/music_sources.py`:

| Source | Mode | Why |
|---|---|---|
| Spotify Connect | `PUSH` | Web API write, `/run/librespot/state.json` observation |
| Bluetooth | `PUSH` | AVRCP `MediaTransport1.Volume` write and observation |
| AirPlay | `CAMILLA_MASTER` | sender volume is observable but diagnostic-only |
| USB sink | `CAMILLA_MASTER` | host owns volume; JTS observes one-way, never writes |

A failed push does not silently become Camilla-master forever. It lands in the
degraded-safe guard — quieter than intended, never louder — and that degraded
state is surfaced in `/state.audio.volume_policy` rather than absorbed.

## Consequences

- **The default is the safe direction.** A source integrated without volume
  homework behaves like AirPlay: Camilla carries the level, and nothing about
  the new source can make the speaker louder than the user asked.
- **AirPlay observation stays diagnostic until hardware says otherwise.**
  Observing a sender's volume is not the same as being able to honour it;
  promoting AirPlay to push requires hardware evidence, not a code change.
- **Degraded push is visible, not silent.** The guard state, its dB, and its
  reason are readable from `/state` without SSH — which is what makes "the app
  says 100% but the speaker is quiet" diagnosable at the user's surface.
- **The cost is a duplicated slider.** In Camilla-master mode the source's own
  slider does nothing useful, which is mildly confusing on a phone. That
  confusion is the price of never being loud by accident, and it is the trade
  taken deliberately.
