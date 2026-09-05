# ADR-0169: the outputd ordering guard compares recorded instants, not computed ages

Date: 2026-08-26
Status: accepted

## Context

`jasper-audio-hardware-reconcile` writes `/var/lib/jasper/outputd.env` and kicks
`jasper-outputd` and `jasper-aec-reconcile` as two separate `--no-block`
transactions, and udev starts the reconciler in a transaction of its own. Across
passes, therefore, `jasper-aec-init.service`'s `After=jasper-outputd.service`
orders nothing: the live outputd can still be answering `STATUS` with the
previous geometry and final-edge sample format while init samples the writer and
certifies an alignment against it.

`require_outputd_env_loaded` (`jasper/cli/aec_init.py`) closes that by comparing
outputd's `ExecMainStartTimestamp` against the env file's mtime. The obvious
implementation — turn each stamp into an age against "now" and compare the ages
— is wrong on this hardware. The Pi has no RTC. At boot, systemd-timesyncd
routinely steps CLOCK_REALTIME forward by hours while CLOCK_MONOTONIC keeps
counting from zero, so a realtime-derived age and a monotonic-derived age are
not comparable. The failure direction is the bad one: the step makes a stale
outputd look freshly started, and the guard certifies exactly the edge it exists
to catch. It fails **open**.

Two adjacent details have the same shape. systemd retains
`ExecMainStartTimestamp` after a unit stops, so a stopped outputd is still
caught; only a unit that never started this boot reports an empty value. And a
guard that cannot run — a non-zero `systemctl` exit, an unparseable stamp, a
non-`ENOENT` `OSError` — is indistinguishable at every downstream surface from
a guard that ran and passed.

## Decision

The guard compares two recorded realtime **instants** — outputd's
`ExecMainStartTimestamp` and the env file's mtime. "Now" is not an input to the
comparison on either side, and no age is computed from either stamp. Past the
comparison the guard waits a bounded 10 s for a queued restart, then exits `3`
rather than certify a stale edge.

An inert guard is disclosed, not silent: any of the three failure-to-run cases
emits `event=chip_aec_init.ordering_probe` at WARN.

A unit that never started this boot leaves the guard inert by design; there
`collect_reference_queue`'s writer-not-ready path owns the diagnosis, and
duplicating it here would produce two names for one condition.

## Consequences

The residual is a backward realtime step that lands between the two stamps and
exceeds their separation, which inverts them. That window is inherent to two
stamps written by two other processes and cannot be closed by unit ordering.
fake-hwclock restores a past time at boot, so NTP's first correction goes
forward — the harmless direction — and the residual needs a backward step from
an already-correct clock to bite.

A stale ordering is an ordering race, not a moved artifact, so exit `3` does not
ask for a recommission. Per [ADR-0101](0101-proven-once-disclose-on-change.md) the
reconciler runs software AEC3, publishes `disclosed_stale` with the action "wait
for jasper-outputd to restart", and the box keeps hearing.

Operational detail lives in HANDOFF-aec.md (retired, see ADR-0199).
