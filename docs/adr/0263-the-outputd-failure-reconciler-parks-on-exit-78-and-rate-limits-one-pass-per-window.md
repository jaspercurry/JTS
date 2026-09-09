# ADR-0263: The outputd failure reconciler parks on exit 78 and rate-limits one pass per window

- **Date:** 2026-09-09
- **Status:** Accepted. Supersedes
  [ADR-0141](0141-outputd-parks-out-of-band-rather-than-riding-its-restart-limit-to-a-reboot.md).
- **Context:** ADR-0141 recorded a lane-specific remedy keyed on the failing
  PCM name: it counted consecutive content-lane opens and parked on the 4th,
  choosing between a width fix (passive lane) and re-arming the ring (ACTIVE
  lane). `deploy/bin/jasper-outputd-failure-reconcile` at HEAD carries none of
  that — no streak counter, no lane classification, no per-lane remedy.
- **Decision:** `ExecStopPost` runs one reconcile per `RECONCILE_WINDOW_SEC`
  (300 s) window across every failure class, deduped by a `/run` stamp. A
  `SERVICE_RESULT` of `success` or `exec-condition` is skipped outright.
  `EXIT_STATUS=78` (`RestartPreventExitStatus=78`) gets its own branch: one
  reconcile plus an explicit `reset-failed` + `--no-block restart`; any other
  outcome, including a repeated 78 inside a window another class already
  spent, leaves the unit parked and writes the park record read by the
  doctor and `/state`. Every other failing exit runs the plain reconciler
  with `--no-restart` and does not park.
- **Consequences:** The park path is uniform across failure classes instead
  of lane-keyed, and driven by exit status rather than a counted streak — the
  streak state and its lane classifier are gone along with the two remedies.
  The reboot escalation still stops being the system's answer to a stuck
  unit; the doctor and `/state` reader are unchanged (they only read the
  park record, not the window).
