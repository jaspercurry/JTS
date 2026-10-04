# ADR-0442: A level probe reads each burst it heard at its anchor

- **Date:** 2026-10-03
- **Status:** Accepted. Amends [ADR-0365](0365-a-drivers-pose-finds-its-level-with-a-probe.md) §3
  (which bursts a probe reads, and where, and what a probe that read none names) and, for probes
  only, [ADR-0364](0364-a-takes-level-is-read-from-its-located-sweeps-in-their-band.md) §1 ("each
  located sweep").
- **Context:** From 18:54Z on 10-03, every jts3 seat probe stopped at `snr_floor`: rounds
  5afc4a589575 and 5396c351def1, three takes each, on the graph and program of round 4c4d6b9a2185,
  which passed. The room noise and the burst levels were the same to about 1 dB. The room's
  arrivals changed: arrivals 1 to 30 ms after the first, 0.5 to 1.2 dB under it, gave each burst's
  correlation peak a margin of 0.00 to 0.23 over its neighbours. That is under
  `SWEEP_LOCATE_CONFIDENCE_FLOOR` (0.3), so no burst was read. The passing round was at 0.30. That
  margin measures the room's arrivals, not whether the burst played. Cases built from those takes
  also showed that the margin passes a room knock in a burst's window as that burst. With the
  program absent, or with a playback dropout over the top burst, the old rule solved up to 15.9 dB
  louder than the take's own level.
- **Decision:**
  1. **Heard.** A probe's burst is heard when its best match near its staircase anchor
     (`scheduled_start` ± `SEGMENT_SEARCH_S`) is at least `BURST_PRESENCE_RATIO` (3.0) times its
     best match 0.1 to 0.5 s away (`BURST_FAR_LAGS_S`), above the room's modal tails (200 Hz and
     up; its own band when it stops below 200 Hz). The match is the cosine similarity at each lag
     over that lag's own energy, which the staircase locate also uses (`locate.burst_presence`). A
     burst that the capture does not hold whole is not heard.
  2. **Read.** Each heard burst is read at its anchor, as ADR-0364 reads a sweep. Other programs
     keep the locate-confidence gate and the located start.
  3. **Judged heard.** A probe is a take the microphone heard when it read a burst, since it reads
     exactly the bursts heard at their anchors (`capture_dispatch._stimulus_locate_ok`). One that
     read none is judged as any take not heard, never levelled: a frame fault retakes it the same,
     and otherwise it names `locate_failed` (`level_unreachable` when its SPL watch did not stop it,
     ADR-0422; `measurement_output_muted` when the output is muted). Other programs keep the locate
     floor (`LOCATE_MIN_CONFIDENCE`, 0.1).
  4. **Log.** Each probe analysis logs `event=program_analysis.level_probe_presence` with the
     figure of each burst in schedule order.
- **Consequences:**
  - On the 7 jts3 takes, the heard bursts at −48 dBFS and up read 4.84 to 13.71 times (the −42 and
    −36 dBFS bursts 6.26 times and up). The passing take's solve moves 0.004 dB (−35.847 to
    −35.842 dBFS, `retake_louder`). Each of the 6 stopped takes now reads 3 or 4 steps, the top two
    trusted, and solves `retake_quieter` at −37.25 to −37.44 dBFS (74.25 to 74.44 dB SPL at
    −36 dBFS).
  - Windows with no burst read 1.47 times at most. That is 6,300 windows of each take's own room
    (shaped noise, mirrored and hard-tiled room), and 1,260 windows with a step, a 40 Hz thump, a
    click or a knock 10 to 30 dB over the room at a burst's start (1.2 times at most). The full band
    does not separate them: there a thump read up to 2.8 times, and heard −48 dBFS bursts read
    2.36 times.
  - The anchor needs no gate of its own. An anchor 0.1 s or more off puts each burst in its far
    lags (0.9 times at most; 0.3 for the −42 and −36 dBFS bursts), so nothing is read. Where an
    anchor off by less still passes, the −42 and −36 dBFS readings stay within 1.5 dB of their true
    windows. The located read had the same jitter.
  - A room sound no longer stands in for a burst. In 70 cases built from the 7 takes (silence, room
    only, room with a thump at each burst, a dropout over the top burst with a thump and a knock,
    200 ms of frames lost or inserted, a capture 40 ms late), the probe refuses with no reading
    (`locate_failed`), or solves from the highest burst it heard, at most 1.37 dB from the take's
    own solve (ADR-0411's step spread).
  - A probe heard only through arrivals that leave each burst a locate confidence under 0.1 is now
    levelled. Before, it stopped at `locate_failed`, or it stopped the run as `level_unreachable`
    when no stop ended its play.
  - A probe that read no burst now names `locate_failed` ("couldn't hear the speaker"), and the
    output mute guard reads first. Before, it named `snr_floor` when one burst located at 0.1 or
    more. Both ask the operator to try again, with no gain.
  - **What stays:** VERIFY's sweep locate and its `summed_sweep_heard` check, and every other
    take's locate floor. The probe envelope, ADR-0411's rule and bound, the 15 dB raise limit and
    the 85 dB stop stay.
  - Rejected:
    - Reading every held burst at its anchor with no check. A knock in the window of a dropped top
      burst then reads trusted and low, and the solve rises one step (6 dB).
    - The peak's margin over lags 5 to 50 ms away (0.01 to 0.05 on the stopped takes).
    - The full-band match (a thump at a burst's start passes it).
