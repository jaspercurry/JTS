# ADR-0270: USB buffer motion is separate from clock correction

- **Date:** 2026-09-09
- **Status:** Accepted. Supersedes ADR-0208's decay-demand subtraction and
  ADR-0214's refill-window cap; preserves ADR-0185's runtime adaptation.
- **Context:** JTS3 passed its USB timing check, then took about six minutes
  to reach the requested 12 ms buffer. A pause restarted that descent.
- **Decision:** A pure buffer controller owns the target, separate from the
  clock servo. Bounded resampler feed-forward moves the target at 0.2%; it
  skips no PCM and leaves the clock servo's authority intact. The probe
  reads clock correction directly. Refilling still suspends its measurements.
  A stable target is kept in memory across idle gaps on a continuously
  observed USB connection. The existing helper thread watches UDC state
  notifications; any connection change or unreadable state discards reuse.
  Capture-handle reopens alone do not. Each resumed session checks timing
  in the background; a failed check restores the acquisition buffer.
  Short underfills raise the working floor by two render periods. That floor
  stays in use for the connection instead of repeatedly trying the failed
  depth. A gap of at least 250 ms is treated as idle: the full acquisition
  buffer cannot bridge it.
- **Consequences:** A normal first start reaches Low in roughly 45 seconds;
  a warm resume starts at the previous working depth. A fresh daemon or USB
  connection starts conservatively. During buffer motion, pitch can change
  by up to 3.5 cents. STATUS exposes motion, working floor, resume count and
  backoffs; the page keeps the user's choice selected and explains extra
  buffering. There is no host database, certificate, new thread or new knob.
