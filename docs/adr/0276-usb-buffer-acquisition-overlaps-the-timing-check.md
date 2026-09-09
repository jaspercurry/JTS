# ADR-0276: USB buffer acquisition overlaps the timing check

- **Date:** 2026-09-09
- **Status:** Accepted. Supersedes (partial) ADR-0275's sequential acquisition
  and clock-rail pause policy.
- **Context:** JTS3 took 51–68 seconds to reach Low. About 21 seconds drained
  the buffer after a 23–25 second timing check. The slower run then spent
  17 seconds at 13.5 ms because clock corrections kept resetting the wait.
- **Decision:** On an observed USB connection, buffer acquisition runs during
  the full timing check. Its existing 0.2% rate adjustment stays unchanged.
  Reduction pauses when measured fill falls below the working floor minus
  the shared DLL frame margin; it resumes as that reserve recovers. A clock
  correction limit alone does not restart acquisition or invalidate reuse.
  Only a passed timing check and uninterrupted playback at the settled target
  allow reuse. Failed checks still restore the acquisition buffer; underfills
  still raise the working floor. The buffer owner publishes `buffer_low` as
  its pause reason; the page shows adjustment and timing checks together.
- **Consequences:** Timing validation and buffer motion share elapsed time,
  without a shorter probe, a larger pitch shift, or a second rate controller.
