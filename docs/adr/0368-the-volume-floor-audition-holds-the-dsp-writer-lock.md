# ADR-0368: The volume-floor audition holds the DSP writer lock, so the reconciler has no quiet carve-out

- **Date:** 2026-09-26
- **Status:** Accepted. Supersedes (partial)
  [ADR-0213](0213-the-reconciler-asks-the-dsp-writer-lock-before-it-corrects-the-fader.md):
  the consequence that keeps `RECONCILE_DUCK_SKIP_DB` for the volume-floor
  audition, the follow-up that record left open. Its decision stands: the
  reconciler asks the writer lock, fails open, and no durable cross-process
  claim store is built.

## Context

ADR-0213 made the DSP writer lock the fact a reconciler asks before it
corrects the fader, and left one client on the dB carve-out: the `/sound`
volume-floor audition parks the fader at a proposed floor under a
`COMMISSIONING` claim in jasper-web that no reconciler can see — not
jasper-voice's 1 Hz observer, and not the fresh coordinator a settings save
builds in jasper-web itself
(`sound_profile_apply._reconcile_volume_curve_after_settings`). So every quiet
drift of 10 dB or more was skipped, a duck stranded by a killed swap and a
floor left by a jasper-web that died mid-tone included.

Two attempts failed review (#3038). The first held the flock but leaked it
when `CamillaUnavailable` escaped the restore, kept a lock its worker won after
a cancelled acquire, and played unannounced when the lock directory was absent.
The second opened a `MEASURE_PAUSE` window, which drops every wake frame for up
to the tone's 10 minutes with no cue, and still never reaches the settings
save's coordinator.

## Decision

1. **The audition holds `CANONICAL_DSP_WRITER_LOCK_PATH` for its whole
   life,** admitted through `dsp_apply.dsp_writer_lock` like any writer (source
   `volume_floor_tone`, the writers' 10 s budget) before the fader moves. The
   one restore funnel lets it go after it has put fader and mute back — and
   after a restore that failed too, so a reconciler can repair what it left.
   The funnel runs on a stop (the page's pagehide stop included, whatever the
   runner's own stop does), on the runner's error or 10-minute limit, and on a
   start that fails for any reason (an unreachable CamillaDSP is a
   `RuntimeError` for the route) or is cancelled. A start stopped while it
   waits never moves the fader, and a lock won after its start has left is let
   go at once. The kernel drops it if jasper-web dies.
2. **No lock, no tone.** A missing lock directory, a lock not won inside the
   budget, or a lock thread that dies first refuses the start before anything
   moves.
3. **`RECONCILE_DUCK_SKIP_DB` and `_deep_quiet_skip` are deleted**, and what
   the carve-out bought is kept without reading ownership into a dB gap. Both
   reconciler clients stand down on ADR-0213's probe while the audition plays;
   jasper-voice also stands down on `MEASURE_PAUSE`. Before a write that makes
   the speaker louder, every reconciler asks jasper-control's measurement
   hold — the copy of the window that outlives a pause which never landed or
   lapsed — and waits while it is held or cannot be read. A write that makes
   it quieter never waits on it. Any other drift is corrected to the
   household level. A stand-down is logged once per episode and reason, and
   `/settings` answers `volume_reconciled: false` for it.
4. **The floor never reaches the graph** (`output_trim_db` ignores it), so a
   `/settings` save of the floor alone re-emits nothing and takes no writer
   lock, and only a save that changes the floor reconciles the fader.

## Consequences

- **The visible cost:** every other DSP writer that lands during an audition —
  any other `/sound` save, a live draft, a bass or correction apply, a
  multiroom graph push — waits the 10 s admission budget and refuses. The
  honest answer is "stop the tone first", and the page does not say so yet.
  Saving the floor, the card's own flow, neither waits nor refuses; its
  volume reconcile defers to the tone.
- A deferred floor lands on jasper-voice's next reconcile tick where that
  daemon runs. A streambox runs it only while an accessory mic is published;
  without it the floor lands at the next volume change or the next save that
  changes the floor.
- A page that vanishes without its pagehide stop keeps the lock, and every
  DSP writer refusing, until the tone's 10-minute limit.
- While the tone plays the reconciler corrects it in neither direction: a
  floor auditioned above a low household level is no longer pulled down to
  it within a second, but holds at the audition's own level — at most
  −9.975 dB, the 1% step on the −10 dB top floor.
- The reconciler now repairs what the carve-out stranded, to the household
  level and never above it: the 0 dB ceiling, the `set_volume_db` clamp and
  `devices.volume_limit` are untouched. While jasper-control cannot be reached
  it lowers but does not raise.
- **Rejected:** a `MEASURE_PAUSE` window for the audition (attempt 2), for the
  wake deafness and the reach above; failing open without a lock directory
  (attempt 1), since with the carve-out gone an unannounced audition is
  corrected up on the next tick.
