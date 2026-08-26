# ADR-0112: Assistant audio never rides the synced stream; a bonded member mixes its own, post-round-trip

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

Synchronized playback needs a playout buffer of roughly 300–500 ms. That is
inaudible for music and unacceptable for conversation: an assistant that answers
a buffer late feels broken. The owner also ruled (2026-06-09) that the assistant
replies on the speaker it was addressed from, not house-wide — so there is no
requirement that would force TTS onto the buffered path.

The placement question is not just latency. JTS hears "Hey Jarvis" over music by
subtracting its own output from the microphone, using the exact bytes outputd
hands the DAC as the AEC reference. On a bonded member the music arrives delayed
by the round trip while voice does not, so if assistant audio mixes *after* the
reference tap, the speaker's own voice bleeds into the mic and false-fires wake
or breaks barge-in.

## Decision

**Music is what gets synced; assistant audio stays local.** Voice, wake, and TTS
never traverse the Snapcast transport.

**A passive bonded non-sub member mixes its own assistant audio at
`jasper-outputd`** — after the round trip, before the reference is published, so
the reference still equals what the DAC plays (the invariant that keeps
wake-during-music working). The grouping reconciler points voice's TTS socket at
outputd's and writes `JASPER_TTS_MIX_STAGE=post_dsp`; `PROGRAM_DUCK` rides the
same socket, so ducking is member-local too. Because that lane is already
post-DSP, outputd treats downstream gain as a structural zero rather than
applying fan-in's pre-DSP compensation.

**Active endpoints are the deliberate exception:** their TTS stays on fan-in,
upstream of the local crossover/protection graph, so assistant audio is split
and protected at the endpoint's active width. Wireless sub followers park voice
and keep the outputd TTS socket unarmed, so full-range speech never reaches a
sub.

## Consequences

- Restoring an outputd TTS mixer for bonded roles was net-new Rust in a
  reboot-on-fail daemon, not a flag flip — accepted because a sample-locked
  member needs a post-buffer mix point and outputd is the final output owner.
  Solo stays fan-in-owned and byte-identical.
- The route matrix is derived once per role and written to both ends, and a
  doctor check catches drift between them — including the worst shape, voice
  targeting a socket outputd never armed (a silent assistant).
- The leader's local assistant sits roughly one buffer ahead of the music
  beneath it. Accepted: the V1 promise is music sync, not assistant sync.
- Deliberately deferred: whole-house, time-synced spoken announcements (a timer
  ringing everywhere at once). That is a separate feature that would route
  announcements through the buffered path — it does not change this rule for
  conversational replies.
