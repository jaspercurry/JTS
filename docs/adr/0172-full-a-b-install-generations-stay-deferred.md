# ADR-0172: Full A-B install generations stay deferred

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

The install/update resilience work asked for "the cheapest version that meets
*never worse than before* on a 1 GB Pi." Full A-B generations — two
`/opt/jasper` trees with a symlink flip on verified success — is the textbook
answer, and it is expensive here: roughly twice the heavy venv on disk, plus
symlink-flip surgery across every unit path, `StateDirectory`, reconciler, and
wizard. Four much cheaper pieces were already in hand or nearly free.

## Decision

**Ship the cheap four; defer full A-B until a reboot-inside-a-failed-update
window is an observed failure mode.** The four:

| Piece | Cost | Why it counts |
|---|---|---|
| Honest build manifest, written last | ~0 | The box never advertises a build it is not running, so the deploy direction-guard is trustworthy. |
| No restart on failed install | 0 | Live daemons keep serving the old code in RAM through a failed update. |
| Idempotent, resumable install | 0 | Fingerprint caches, guarded creates, and check-before-write migrations make "resume" mean "re-deploy". |
| Surfaced failure + OOM collateral | small | The operator knows immediately and can act. |

A-B's only marginal benefit over these is protecting the narrow window where a
**failed update is followed by a reboot before the operator re-deploys** — the
one case where partially-updated `/opt/jasper` gets loaded fresh.

## Consequences

- Accepted residual risk, named: reboot during a failed-update window. Revisit
  A-B, or a cheaper atomic swap of just the Python tree via a staging path, if
  that is ever observed rather than imagined.
- "Never worse than before" holds in the immediate term but not across a
  reboot, and the doc says so rather than implying a stronger guarantee.
- Bounded-cohort rollback is still worth it where it is cheap: full-profile
  systemd unit generation snapshots its destinations and restores them as one
  cohort. That is deliberately not a claim that the whole install is A-B.
- The analysis does not have to be re-derived on each memory-pressure incident;
  only "has the trigger fired yet?" needs answering.
