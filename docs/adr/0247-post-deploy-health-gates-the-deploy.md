# ADR-0247: Post-deploy health gates the deploy

- **Date:** 2026-09-07
- **Status:** Accepted
- Refs: #4027, #4194. Supersedes
  [ADR-0173](0173-post-deploy-health-is-surfaced-never-gating.md), and decision 4 of
  [ADR-0242](0242-post-deploy-health-is-the-core-doctor-run-in-a-transient-unit.md)
  while correcting decision 3 of it. It is the "advisory doctor report"
  [ADR-0174](0174-install-window-oom-kills-are-surfaced-not-gated.md) defers to.

## Context

ADR-0173 kept the doctor advisory until missing-hardware daemons read as idle; HW-3
read clean on jts.local, jts3 and jts4 at `e39461ebb` (#4027). Two things change
with it: `systemctl start jasper-aec-reconcile.service` queues a `--no-block` voice
restart nothing waits for; and no exit code carries the verdict ADR-0242 decision 3
read off it — `systemd-run --wait` folds `timeout`, `oom-kill` and a bus failure
alike into 1 (v252-v257 `run.c`).

## Decision

1. **The verdict is the doctor's own line, not the exit code.** The wrapper reads
   `event=deploy.health status=ok|fail` out of the run's stdout, which it relays to
   the transcript: `fail` fails the deploy, `ok` passes, an ABSENT line passes with
   `event=deploy.core_health rc= reason=no_verdict` — a fired bound, an OOM kill,
   an unrunnable venv and an unreachable bus are not "the speaker is broken".
2. **The pass waits for the box to go quiet first**, in ONE remote loop over
   `systemctl list-jobs` bounded at 90 s (systemd's `DefaultTimeoutStartSec`, past
   which a `Type=notify` start has failed on its own). Pending JOBS, not a unit
   pattern: a queued restart passes through `deactivating` too, and the doctor's set
   is not all `jasper-*`. It prints `event=deploy.settle_wait`, never suppresses the
   gate, and goes when that restart drops `--no-block` or the doctor waits itself.
3. HW-4's `memory.peak` read stays what ADR-0242 decision 1 made it: a #4027 row
   calibrating the 96M RAISE CONDITION, not a precondition for this gate.

## Consequences

A deploy fails on a broken speaker instead of printing rows nobody reads, and stays
green — labelled — when the doctor could not run at all. Its rows now reach the
transcript through a `tee`, on stderr: a bare capture would swallow a sudo prompt.
