# ADR-0240: Post-deploy health is the core doctor, run in a transient unit

- **Date:** 2026-09-06
- **Status:** Accepted
- Refs: #4194. Executes rule 5 of
  [ADR-0233](0233-one-reader-per-fact-two-surfaces-one-doctor.md); supersedes
  decision 5 of
  [ADR-0217](0217-a-streambox-runs-the-assistant-only-while-a-mic-bearing-remote-is-paired.md);
  closes D4 of
  [ADR-0235](0235-attached-hardware-one-owner-per-fact-and-no-facts-in-shell.md).

## Context

`deploy/bin/jasper-deploy-health` was a 900-line second implementation of the
doctor's questions, reached on two paths: the installer ran it instead of the
doctor when `build_swap_required` said the box was under 1.2 GB, and the deploy
wrapper picked between it and the FULL doctor from a bare `1200000` compared
against the Pi's own `/proc/meminfo`. Its stated reason — a stdlib-only probe
for a box whose venv is broken — was unreachable on the installer path, which
returned early unless `/opt/jasper/.venv/bin/jasper-doctor` was executable.
ADR-0233 rule 5 made its retirement conditional on `--core` carrying its unique
rows; #4220 landed the last of them (`_REQUIRED_ACTIVE_UNITS`, the
no-provider-configured skip, and a `--core` run that builds no voice `Config`).

## Decision

**One health surface on every box and every tier: `jasper-doctor --core`.**

1. The installer's `run_doctor_summary` runs it under
   `systemd-run --quiet --wait --pipe --collect -p MemoryMax=96M -p
   TimeoutStartSec=60`. The bound is a TRANSIENT unit, not the shipped oneshot
   ADR-0233 rule 5 described: nothing new enters `SYSTEMD_SUPPORT_FILES`, the
   caller keeps the exit code, and the memory ceiling that motivated the
   low-memory branch now applies on every box instead of below 1.2 GB. The
   memory threshold survives only in the build sandbox, which is where it
   describes a real cost.
2. The deploy wrapper's `gate_core_health` runs the same subset over
   `run_remote_sudo` and returns its exit code. `--core`'s contract is
   warn-tolerant by construction (`jasper/doctor_contract.py` `summarize`,
   `jasper/cli/doctor/_cli.py`): fails exit 1, warns exit 0, skips count for
   neither — so a bare exit-code gate needs no reason allow-list. The doctor
   itself prints `event=deploy.health status= fail= warn= rows= speaker_silent=`
   under `--core`; the installer adds `event=deploy.health rc=` for the run it
   bounded.
3. **The gate is advisory in this change.** Both installer call sites and the
   wrapper call site swallow the result with `|| true`. Deleting the wrapper's
   swallow is ADR-0173's removal condition firing, and waits on the three-box
   hardware read. ADR-0173's decision otherwise stands, under the wrapper
   function's new name: `surface_system_health` is `gate_core_health`.
4. The streambox voice expectation that was decision 5 of ADR-0217 is now
   `resilience.check_voice_unit_running`, which skips on an unpaired box and
   warns on a paired one. The gap ADR-0217 recorded in its consequences — a
   dead `jasper-voice.service` visible only to the health script — is closed by
   the same row.

## Consequences

Five rows the health script printed have no `--core` equivalent and are
knowingly given up. Only one is replaced: the USB Audio Input status row was
the sole reader of `/run/jasper-source-intent/status.json`, so a failed
install-time replay now says so directly —
`event=source_intent.replay_failed`, logged beside the existing WARN in
`deploy/lib/install/systemd-units.sh`. The fan-in xrun delta needs a rate the
fan-in does not publish (outputd has `xrun_rate_per_hour`; fan-in has nothing),
so it is a Rust-plus-Python follow-up under #4194, not a row lost silently.
The other three are dropped outright: outputd's `empty_periods`/`eagain_count`
deltas were printed and never verdicted, and a one-second idle window is
exactly the false-positive class ADR-0173 exists to avoid; outputd's
`uptime_seconds` monotonicity has no doctor reader and a bouncing outputd is
already covered over the whole boot by `check_service_runtime_state` and
`check_outputd_failure_reconcile_park`; the source-intent stability check
guarded the health script's own two-phase read and has no subject once that
read is gone.

Severity softens in one place on purpose. The health script FAILed when fan-in
or outputd progress was older than 2000 ms; the doctor WARNs at
`FANIN_STALE_MS`/`OUTPUTD_STALE_MS`. The 2000 was an unshared third copy of a
threshold two owners already publish — the duplication rule 5 exists to end.

What this makes harder: a box whose venv is genuinely broken now reports
nothing at all where the health script would have printed a stdlib-only report.
That fallback was dead on the installer path, which returned early when
`jasper-doctor` was not executable, and on the wrapper path it covered only a
broken venv UNDER 1.2 GB — above the threshold the wrapper already ran the
doctor that could not start. A venv that cannot run the doctor also cannot run
the daemons the report would describe, and the install fails earlier and louder.

Rejected: a shipped `jasper-deploy-health.service` oneshot carrying the
resource bounds (a unit file, an install step and a `SYSTEMD_SUPPORT_FILES` row
to express two properties of one synchronous call); and keeping the low-memory
branch with the doctor on both sides of it (the branch existed to choose
between two tools, and there is now one).
