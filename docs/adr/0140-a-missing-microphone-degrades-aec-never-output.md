# ADR-0140: A missing microphone degrades AEC, never output

- **Date:** 2026-08-26
- **Status:** Accepted (ratified on the 2026-07-10 JTS3 repair; recorded here
  when HANDOFF-hotplug-resilience.md was trimmed to its operational spine)

## Context

`jasper-outputd` writes a playback reference into the XVF3800's USB-IN PCM so
the chip can cancel echo. On JTS3 (2026-07-10) that write was on outputd's
startup path: an absent microphone made outputd's start **fail**, and the
reconciler retry that followed left CamillaDSP writing the active lane while
outputd read the passive one. A missing optional input device had silenced the
speaker's output.

## Decision

**The chip reference is a side branch, never a playback prerequisite.**
`jasper-outputd` opens and primes the physical DAC first. An unavailable
XVF3800 USB-IN PCM moves only `reference_outputs.chip_ref_writer` to
`degraded` and starts bounded background retries; the DAC keeps playing. A
non-recoverable worker or configuration fault is `failed` and calls for an
outputd restart after correction — it still does not stop playback.

The mic's own owners take the matching split: voice parks clean (ADR-0139)
and `jasper-doctor` warns about the AEC degradation while the outputd playback
check stays healthy.

## Consequences

- Unplugging the microphone costs echo cancellation and the wake word. It
  cannot cost music, TTS, or cues.
- Replug converges without a playback restart: outputd reconnects its reference
  writer in the background rather than being bounced.
- Two health verdicts exist where there was one, and they must not be merged:
  "outputd is playing" and "the chip has a reference" are separately reported.
  A future check that fails outputd for a degraded reference re-opens this ADR.
- The degraded state is bounded-retry, not silent: the `degraded`/`failed`
  distinction is what tells an operator whether to wait or to intervene.
