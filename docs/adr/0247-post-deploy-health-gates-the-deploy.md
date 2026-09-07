# ADR-0247: Post-deploy health gates the deploy

- **Date:** 2026-09-07
- **Status:** Accepted
- Refs: #4027, #4194. Supersedes
  [ADR-0173](0173-post-deploy-health-is-surfaced-never-gating.md), and the wrapper
  half of decision 4 of
  [ADR-0242](0242-post-deploy-health-is-the-core-doctor-run-in-a-transient-unit.md)
  together with decision 1's HW-4 precondition, while correcting its decision 3. It
  is the "advisory doctor report"
  [ADR-0174](0174-install-window-oom-kills-are-surfaced-not-gated.md) defers to.

## Context

ADR-0173 kept the doctor advisory until missing-hardware daemons read as idle; HW-3
read clean on jts.local, jts3 and jts4 at `e39461ebb` (#4027). Two things change
with it: `systemctl start jasper-aec-reconcile.service` queues a `--no-block` voice
restart nothing waits for; and no exit code carries the verdict ADR-0242 decision 3
read off it — `systemd-run --wait` folds `timeout`, `oom-kill` and a bus failure
alike into 1 (v252-v257 `run.c`).

## Decision

1. **The verdict is the doctor's own line, not the exit code.** The wrapper `tee`s
   the run to the transcript and reads the line starting `event=deploy.health `:
   `status=fail` fails the deploy, `status=ok` passes it, an ssh that died fails it
   as `unreachable` (the deploy exits non-zero on anything it could not verify), and
   any other rc with no verdict line passes as `no_verdict` — a fired bound, an OOM
   kill, an unrunnable venv and a downed bus are not "the speaker is broken". One
   event name, one line per result: `event=deploy.core_health status= rc=`.
2. **The pass waits for the box to go quiet first**, in ONE remote loop over
   `systemctl list-jobs` bounded at 90 s (systemd's `DefaultTimeoutStartSec`, past
   which a `Type=notify` start has failed on its own). Pending JOBS, not a unit
   pattern: a queued restart passes through `deactivating` too, and the doctor's set
   is not all `jasper-*`. It prints `event=deploy.settle_wait`, never suppresses the
   gate, and goes when that restart drops `--no-block` or the doctor waits itself.
3. Only the WRAPPER's run gates. `install.sh`'s `run_doctor_summary` stays advisory:
   it runs before the wrapper's restarts, and its copy is the journal's.
4. HW-4's `memory.peak` read is no longer owed before arming: an OOM of the
   transient unit leaves no verdict line, and `no_verdict` is green, so an unproven
   96M ceiling cannot fail a deploy. It stays a #4027 row calibrating 96M, whose
   RAISE CONDITION is unchanged.

## Consequences

A deploy fails on a broken speaker instead of printing rows nobody reads, and stays
green — labelled — when the doctor could not run at all. The rows stay on stdout,
where they were: the wrapper `tee`s them to a scratch file and parses that, because
capturing them instead would swallow an attended sudo prompt.
