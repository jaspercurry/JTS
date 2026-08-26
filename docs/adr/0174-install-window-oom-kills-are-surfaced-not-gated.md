# ADR-0174: Install-window OOM kills are surfaced, not gated

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

A build on a 1 GB Pi OOM-killed nginx *and* jasper-voice, and the deploy
tooling exited silently — the operator learned about it from a dead speaker.
The plan's ask was that the tooling "say so, not exit silently." The obvious
over-correction is to fail the deploy on any production-daemon OOM kill, which
would bite exactly the box the feature exists for: a 1 GB Pi taking a large
update, where systemd has usually already restarted the victim before the
deploy finishes.

## Decision

**Scan the install window for OOM kills on success *and* failure, surface a
production-daemon kill as a loud per-unit `✗`, and let the end-state gates own
pass/fail.** `report_oom_collateral` captures the Pi's clock before install and
scans the kernel log for that window afterwards. An OOM is *history*; whether
the box is fine *now* is decided by the management-surface probe,
`verify_manifest_advanced`, and the advisory doctor report ([ADR-0173](0173-post-deploy-health-is-surfaced-never-gating.md)).
A victim that is still down gets caught there; one systemd already restarted
does not fail an otherwise-healthy deploy.

Victims are parsed two ways because neither field is sufficient alone: the
cgroup `task_memcg=/system.slice/<unit>` names the **systemd unit** reliably (a
venv console-script daemon is execve'd as `python3`, so its `comm` is
misleading), while `(comm)` / `task=comm` gives the human-readable process name
and is the *only* signal for build tools, which run in a transient ssh scope
rather than a named `.service`.

## Consequences

- Silence is no longer a possible outcome, which was the actual failure; a
  false deploy failure — the inverse trap — is also avoided.
- A build-tool OOM (cc1plus, cargo) is printed as context, not as a verdict; it
  already shows up as an install failure under `set -e`.
- The parsers (`oom_killed_units`, `oom_killed_comms`,
  `oom_unit_is_production` in `scripts/_lib.sh`) are pure functions unit-tested
  against captured kernel-log text, so the classification can be trusted
  without a Pi.
- Kernels without `*_memcg` fields degrade to comm-only classification. Pi OS
  Trixie is cgroup-v2, so this is a compatibility fallback, not the main path.
