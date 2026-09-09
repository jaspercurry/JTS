# ADR-0267: The AEC bridge's restart ladder matches control's

- **Date:** 2026-09-09
- **Status:** Accepted
- Refs: ADR-0251; supersedes ADR-0224 on the `RestartSec` value only.
- **Context:** `jasper-aec-bridge.service` shares `jasper-control.service`'s
  4-burst/300 s `StartLimitAction=reboot` ladder. ADR-0251 widened control's
  `RestartSec` from 2 s to 5 s because a transient fault at 2 s reboots the
  box in about eight seconds — control's ladder was the tightest on the box.
  That left `jasper-aec-bridge.service` at `RestartSec=2`, so it inherited
  the same tight ladder ADR-0251 moved control away from.
- **Decision:** `jasper-aec-bridge.service`'s `RestartSec` widens from 2 s to
  5 s, matching control's. A transient fault the bridge cannot grow out of
  now spends about twenty seconds, not eight, before
  `StartLimitAction=reboot` fires.
- **Consequences:** The bridge is no longer the tightest restart ladder on
  the box. `StartLimitBurst=4` and `StartLimitAction=reboot` are unchanged;
  only the per-restart backoff moves. `SuccessExitStatus`/
  `RestartPreventExitStatus` (66 78) still park the unit on a permanent
  fault before the ladder is ever spent.
