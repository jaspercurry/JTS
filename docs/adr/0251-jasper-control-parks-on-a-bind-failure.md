# ADR-0251: jasper-control parks on a bind failure instead of rebooting the box

- **Date:** 2026-09-07
- **Status:** Accepted
- Refs: #4195 (P3 resilience lane, row R2), finding R-009.
- **Context:** `jasper-control.service` carries `StartLimitAction=reboot`
  (Tier 4.5: control is the recovery surface, so an unreachable dashboard is
  the case where a clean reboot beats waiting). Until now `main()`
  (`jasper/control/server.py`) had no handler around its `build_server()`
  call, and `ControlHTTPServer.__init__` re-raises the bind `OSError` after
  shutting its executor down. A refused listen socket — port 8780 held by a
  stale process, a bind host that does not resolve on this box, a privileged
  port after an edit — therefore reached the interpreter as an unhandled
  traceback and exited 1. With `RestartSec=2` and `StartLimitBurst=4` that
  spent the whole burst in about eight seconds and rebooted the Pi, which
  frees nothing: the same config comes back and the ladder runs again. Four
  other units escalate to reboot the same way; only `jasper-voice` had a park
  code, and its `SuccessExitStatus=66 78` / `RestartPreventExitStatus=66 78`
  pair (`deploy/systemd/jasper-voice.service`) is the idiom copied here.
- **Decision:** A refused bind is permanent configuration, not a transient
  crash, so jasper-control parks. `main()` catches the `OSError` from
  `build_server()`, emits exactly one `event=control.bind_failed` at ERROR
  carrying `host`, `port`, `errno` and `error` (no secrets — an address and a
  strerror), and returns `CONTROL_BIND_FAILED_EXIT`, which is `os.EX_CONFIG`
  (78) rather than a second literal. The unit lists 78 in both
  `SuccessExitStatus` and `RestartPreventExitStatus`, so systemd leaves the
  service `inactive` with the journal line standing instead of restarting it,
  and `RestartSec` widens from 2 s to 5 s so control is no longer the tightest
  restart ladder on the box.
- **Consequences:** The reboot ladder stays for every other fault. Its reason
  is unchanged and still good: a wedged accept loop, a watchdog expiry
  (`WatchdogSec=30s`), an OOM kill or a crash inside a handler are all states a
  fresh process or a fresh box can actually clear, and while control is down
  the household has no dashboard and no remote. Only the one fault class that
  a restart provably cannot clear is carved out. What this gives up: a box
  whose port 8780 is held by something that *would* have exited on its own now
  stays parked until someone restarts the unit — the journal event and the
  `inactive` state are the recovery handle, and `deploy/bin/jasper-bootloop-guard`
  no longer has to be the thing that catches this case. NN-6 does not apply:
  jasper-control is not on the wake path. Wake detection, the mic and cue
  playback all live in jasper-voice, which keeps running and keeps answering
  wake words while control is parked; nothing about this park makes the
  speaker deaf, so no cue is owed. **Removal condition:** delete the two
  `*ExitStatus` lines and the handler when `jasper-control.service` stops
  carrying `StartLimitAction=reboot` — with no reboot ladder there is nothing
  to park out of, and an ordinary `Restart=on-failure` loop on a permanent
  fault is merely noisy.
