# ADR-0409: outputd's stop hook records the park and starts the reconcile unit

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
- **Decision:** On a failing stop, `deploy/bin/jasper-outputd-failure-reconcile`
  starts `jasper-audio-hardware-reconcile.service` without waiting. On exit 78,
  which `RestartPreventExitStatus=78` holds, it first writes the park record. It
  runs no pass itself. The unit runs outside outputd's sandbox. Its
  `ExecCondition=` skips the pass when the inputs match its last good pass, and a
  pass that finds the park record retries outputd after its repair.
- **Consequences:** The 300 s window, its stamp and the inline passes are gone. An
  exit 78 whose inputs have not changed stays parked, as before, and the doctor's
  park check names the remedy. A crash loop can only queue gated starts of one
  oneshot, which systemd merges. Rejected: adding `/etc` paths to outputd's
  `ReadWritePaths=` (the pass also writes CamillaDSP and ALSA config, so this would
  undo the sandbox), and skipping unchanged writes (a changed value would still
  fail).
