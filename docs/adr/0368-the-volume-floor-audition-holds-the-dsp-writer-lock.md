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
   life.** It takes it with `atomic_io.advisory_file_lock_async`, inside the
   writers' 10 s admission budget, before the fader moves. The one restore
   funnel lets it go only once fader and mute are back: on a stop (the page's
   pagehide stop included), on the runner's error or 10-minute limit, and on a
   start that fails (an unreachable CamillaDSP is a `RuntimeError` for the
   route) or is cancelled. A lock won after a cancelled start has left is let
   go at once. The kernel drops it if jasper-web dies.
2. **No lock, no tone.** A missing lock directory, or a lock not won inside
   the budget, refuses the start before anything moves.
3. **`RECONCILE_DUCK_SKIP_DB` and `_deep_quiet_skip` are deleted.** Both
   reconciler clients stand down on ADR-0213's probe while the audition plays,
   and on an open measurement window: jasper-voice on `MEASURE_PAUSE`, the
   settings save on jasper-control's hold — the copy of that window
   jasper-voice adopts at startup — which it also counts as open when it
   cannot read it. Any other quiet drift nobody announced is corrected to the
   household level. The stand-down is logged once per episode, and `/settings`
   answers `volume_reconciled: false` for it.

## Consequences

- **The visible cost:** every DSP writer that lands during an audition — a
  `/sound` save or live draft, a bass or correction apply, a multiroom graph
  push — waits the 10 s admission budget and refuses. The honest answer is
  "stop the tone first", and the page does not say so yet. Saving the floor
  itself keeps the setting, reports the refused re-apply, and defers its
  volume reconcile; the reconciler lands the new floor once the tone stops.
- A page that vanishes without its pagehide stop keeps the lock, and every
  DSP writer refusing, until the tone's 10-minute limit.
- While the tone plays the reconciler corrects it in neither direction: a
  floor auditioned above a low household level is no longer pulled down to
  it within a second, but holds at the audition's own level — at most
  −9.975 dB, the 1% step on the −10 dB top floor.
- The reconciler now repairs what the carve-out stranded, to the household
  level and never above it: the 0 dB ceiling, the `set_volume_db` clamp and
  `devices.volume_limit` are untouched.
- A settings save made while jasper-control cannot be reached leaves the new
  floor for jasper-voice's reconciler to land.
- **Rejected:** a `MEASURE_PAUSE` window for the audition (attempt 2), for the
  wake deafness and the reach above; failing open without a lock directory
  (attempt 1), since with the carve-out gone an unannounced audition is
  corrected up on the next tick.
