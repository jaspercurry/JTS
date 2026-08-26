# ADR-0149: A blocking unit-start client waits past PID 1's legal maximum, not a generic ceiling

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-source-lifecycle.md was trimmed
  to its operational spine)

## Context

Source toggles are synchronous: the request writes intent, starts the
coordinator, and reports the real outcome. That only works if the client's wait
is longer than anything systemd may legitimately still be doing. A generic
15-second client ceiling was observed false-timing-out on JTS4 during AirPlay's
NQPTP cold start — the start had not failed, the client had simply stopped
listening, and the user was shown a failure that was not one.

The tempting fix is a big round number everywhere. That hides real hangs.

## Decision

**Every blocking `start` client waits one second longer than every ceiling PID 1
may legally consume for that action — derived, not chosen.**

The derivation is: the target unit's own `TimeoutStartSec`, *plus* the start
ceilings of every unit it both orders `After=` and pulls with
`Requires=`/`Wants=`, because a synchronous start waits for the whole job
transaction rather than the named service alone. Where a pulled unit can be
re-queued mid-transaction, its `RestartSec` counts too. Where a unit declares
several `Exec*` commands, each command's ceiling counts, not one per phase.

Every source-owned unit must therefore declare finite `TimeoutStartSec` and
`TimeoutStopSec` — a unit with no ceiling has no derivable client.

The ladder that falls out for the source coordinator: a 2693-second coordinator
`TimeoutStartSec`, a 2703-second broker wait, and `proxy_read_timeout 5600s` on
`/sources/` and `/bluetooth/` covering the two-call stale-join case plus
response margin. Every other broker unit/verb and every `--no-block` shape keeps
the ordinary 120-second hard ceiling; the broker derives this exception from the
validated request, never from the client's requested number.

## Consequences

- **A timeout means something again.** If a derived client fires, PID 1 is
  genuinely out of legal room — that is a real hang worth a loud failure, not a
  guess about how long hardware feels like taking.
- **Ceilings are evidence-driven where the model was wrong.** The USB gadget is
  modelled at one declared ceiling per `Exec*` command because all 13 of its
  starts on JTS4 since the 2026-08-17 boot ran past the declared five seconds
  without PID 1 terminating any of them. AirPlay's client includes the required
  NQPTP cold-start ceiling plus margin because the old generic client did not.
- **The generous path timeout does not make any child unbounded.** The
  2693-second coordinator bound and the 60/750-second owner bounds stay the
  enforcement points; nginx's number is only "do not cut the wire early".
- **Adding a source dependency is a documented arithmetic change.** Ordering a
  new `Requires=` unit ahead of a source unit changes that source's client wait
  by construction. Skipping the recomputation reintroduces the exact JTS4
  false-timeout.
- **The cost is a number nobody can eyeball.** 2703 looks arbitrary and is not;
  the ladder must be re-derived rather than rounded whenever a unit's ceilings
  move.
