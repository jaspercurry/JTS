# ADR-0241: The install runs as a transient unit and the deploy exits on its status

- **Date:** 2026-09-06
- **Status:** Accepted
- Refs: #4190. Neighbours, not superseded:
  [ADR-0172](0172-full-a-b-install-generations-stay-deferred.md) (the build
  manifest is still the last mutation),
  [ADR-0173](0173-post-deploy-health-is-surfaced-never-gating.md) and
  [ADR-0240](0240-post-deploy-health-is-the-core-doctor-run-in-a-transient-unit.md).

## Context

`deploy/install.sh` ran as a child of the deploy's ssh session, making that
session the install's lifeline. On jts4 a mid-install `device-reapply` on wlan0
severed the session and SIGHUPped a half-applied install (#4190). ssh reported
255 — both "the transport died" and a legal remote exit status — so the wrapper
could only print DEPLOY OUTCOME UNKNOWN. Every answer it wanted (did the
install finish, with what status, is it still running) was a Pi-side fact.

## Decision

**`install.sh` runs on the Pi as the transient unit `jts-install.service`, and
the deploy's exit code is that unit's `ExecMainStatus`.**

`run_install_detached` launches it with `systemd-run --unit=jts-install -p
RemainAfterExit=yes -p RuntimeMaxSec=<INSTALL_POLL_CEILING_SEC> -p
StandardOutput=append:~/.jts-install.log`. Before rsync, an unprivileged probe
reads the unit's `SubState` and `InvocationID`: a live unit refuses the deploy
(`event=deploy.install_busy`) rather than rewriting the tree it is building
from, and the recorded invocation means a previous deploy's finished unit can
never be read as this one's result.

The wrapper polls every `INSTALL_POLL_INTERVAL_SEC`, asking `systemctl show`
for `LoadState SubState Result ExecMainCode ExecMainStatus InvocationID` —
parsed by key, because the reply comes in D-Bus order, not `-p` order — and
reading the transcript by byte offset behind a `\nJTS_EOT` terminator, so a
truncated reply advances nothing. An ended unit gives `ExecMainStatus`, an
`ExecMainCode` of 2 or 3 (`waitid(2)`: killed, dumped) mapping to
`128 + status`; `LoadState=not-found`, or an ended unit still carrying the
pre-rsync invocation, is `event=deploy.install_lost`; the ceiling is
`event=deploy.install_timeout`.

`RemainAfterExit=yes` is load-bearing — a collected unit loses its
`ExecMainStatus` before the next poll can read it. Busy is detected by the
launch's exit code, never by systemd's error text. The ssh rc-255 branch is
deleted: ssh's status no longer carries the install's.

## Consequences

Rejected, worth remembering: a `setsid`/`nohup` child writing an rc marker file
(tried first in this lane and dropped — a reboot or a killed poll leaves no
marker and no one to ask whether the child still lives); keeping the ssh child
and reporting rc 255 as ambiguous (the state the incident produced); a shipped
permanent `jts-install.service` (a unit file to install and keep in sync to
express three properties of one call, and a name outliving the deploy).

- The transcript is `~/.jts-install.log`, owned by the deploying user and
  readable without sudo; the install's output does not reach the journal, so
  `journalctl -u jts-install` shows only systemd's own start/stop lines.
- Output arrives in 10 s batches, one poll interval at a time, not as a live
  stream; a dropped poll logs a reconnect and is not a verdict.
- A second deploy against a busy Pi is refused before rsync, not after it.
- The ceiling is 7200 s — a Zero 2 W cold Rust build, the slowest supported
  install — and the unit carries the same number, so an install this wrapper
  gave up on cannot run on past the answer.

**Supersession condition:** if an install must survive a Pi reboot, or be
adopted by a later deploy rather than merely observed by this one, a transient
unit is the wrong shape — that wants durable state it does not have.
