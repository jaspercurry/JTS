# ADR-0409: outputd's stop hook only records the park

- **Date:** 2026-10-02
- **Status:** Accepted. Supersedes
  [ADR-0269](0269-the-outputd-failure-reconciler-parks-on-exit-78-and-rate-limits-one-pass-per-window.md).
- **Context:** ADR-0269's hook ran `jasper-audio-hardware-reconcile --no-restart`
  inside `jasper-outputd.service`'s `ExecStopPost=`. That unit has
  `ProtectSystem=full`, so `/etc` is read-only there, and the pass writes
  `/etc/jasper/jasper.env` early. Every inline pass failed at that write
  (`env_write_failed`, exit 1). On exit 78 the hook then parked outputd; on other
  failures it only logged. Recovery always came from another reconciler. Evidence:
  #6147, and the reset and save journals of the #6113 smoke test.
- **Decision:** `deploy/bin/jasper-outputd-failure-reconcile` writes the park
  record on exit 78, which `RestartPreventExitStatus=78` holds, and does nothing
  else. It runs no reconcile pass and starts none. A park ends where it ended
  before: a later reconcile pass (a sound-card event, a layout change, a deploy)
  finds the record and retries outputd after its repair, or the operator runs the
  remedy the doctor prints.
- **Consequences:** No working repair is lost, because the inline pass never
  succeeded. The 300 s window and its stamp go with it. Rejected: having the hook
  start `jasper-audio-hardware-reconcile.service`. Its pass runs `reset-failed` on
  outputd, which zeroes the start-rate counter, so a crash loop could never reach
  `StartLimitAction=reboot` (see ADR-0103). And while the reconcile's degraded
  marker stands, its gate always runs the pass, so a persistent exit 78 would
  retry without bound. Also rejected: adding `/etc` paths to outputd's
  `ReadWritePaths=` (the pass also writes CamillaDSP and ALSA config, so this would
  undo the sandbox).
