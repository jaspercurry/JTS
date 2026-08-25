# ADR-0100: One audio transport — the loopback route and its transition machinery are deleted

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

The central audio path ships two transports (fan-in → CamillaDSP → outputd
over `shm_ring` or over snd-aloop `loopback`) plus the machinery that
migrates a live box between them — arm/disarm ladders, ring-confirm strikes,
recovery-to-loopback rungs, ~6–8K lines across `fanin/coupling_reconcile.py`,
`fanin_coupling.py`, `audio_runtime_plan.py`, the Rust transport branches,
ALSA confs, 157 test functions and 24 two-route docs. The deep audit flagged
the duplication; its verifier blocked immediate deletion because loopback was
the shipped default for ring-ineligible topologies (composite/mono/roleful)
and the only recovery from a failed ring arm
([DEEP-AUDIT-2026-08-25.md](../DEEP-AUDIT-2026-08-25.md) §4.6). On
2026-08-25 a live box fell back to loopback via a defect in the ring arming
convergence — a fallback masquerading as normal operation until an operator
noticed the latency.

Owner rulings, 2026-08-26: fallbacks are not a thing on this project; the
one dual-DAC (composite) speaker may break visibly until ring-composite
support exists ("I'd rather have something that's broken that I know is
broken… than something that's stinking and causing confusion"); and the
arming bug is not fixed first, because its habitat — the transition
ceremony — is what gets deleted ("we're not investing in systems we're
going to be deleting"). jts3, the measurement bench, is not the dual-DAC
box, so the tuning program's proving ground is unaffected.

## Decision

`shm_ring` is the only central transport. The loopback route and all
transition machinery are deleted. A topology the ring cannot serve parks
loudly — doctor FAIL, `/state`, web banner naming the reason and the
tracked issue — never silently and never degraded. Hard-park refusals that
prevent guessing (full-range into a protected driver) survive; they are
safety, not fallback. There is no arming ceremony in a single-transport
world: the ring lane is installed at deploy and present at boot. Recovery
from a bad deploy is `git revert` + redeploy. Composite-on-ring support is
a tracked issue, built later inside the single-transport architecture.

Gates on execution: the tuning program's explicit ack (the ring is its
measurement transport; shared-seam protocol) and the 5-case stereo tap
re-run after the fan-in diff lands.

## Consequences

The dual-DAC speaker parks until composite-on-ring lands. A box whose ring
setup fails parks visibly instead of playing through a high-latency
fallback. If a defect survives the deletion (e.g. flaky ring establishment
at boot) it presents as a named park with `event=` logs and is fixed
forward then. Deleted permanently: the dual-transport arbitration
complexity — the class of code where the 2026-08-25 incident lived.
Rejected: fixing the arming bug pre-deletion; an interim
"loopback-as-degraded-state" surface (observability built onto code with
weeks to live).
