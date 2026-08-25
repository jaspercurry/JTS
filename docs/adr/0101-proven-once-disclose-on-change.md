# ADR-0101: Proven once, disclose on change — validity-proof gates stop parking working systems

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

Commissioning and validation artifacts across the repo (chip-AEC alignment
K, topology fingerprints, `audio_validation.py` certification records with
staleness/future-skew windows, commissioning receipts) treat a passed proof
as bound to exact conditions: an upstream change — geometry, firmware, even
entering a hardware *fact* — invalidates the proof and **parks** the
affected path until re-proven. Flagships: chip-AEC-or-park, and the #2935
class where a rotated topology fingerprint blocked a working box. The deep
audit verified this machinery is wired and consumed, but whether
park-until-proven is the right *product policy* was the owner's question,
answered 2026-08-26: "things should just keep working… if something I did
upstream breaks it, we should loudly complain… we shouldn't stop stuff from
working because it hasn't been proven yet." Plus the portability vision:
hardware tested once by the owner should just work for a downstream user
with the same hardware, no re-commissioning.

## Decision

A proof that passed for a hardware class stays valid until something
**observably breaks**. An upstream change demotes it to **disclosed-stale**
— the path keeps running, and doctor/`/state` carry a warning naming what
changed and the exact re-commission command — never to a park. Parking on
unproven-ness is reserved for the non-negotiables (the commissioning SPL
stop, measurement-volume proofs, driver caps, brick hazards, and don't-guess
refusals that could mis-route into a protected driver). Chip-AEC-or-park
becomes chip-AEC-or-disclose; re-commissioning is an optimization the
doctor recommends, not an admission gate. Commissioning artifacts are keyed
to hardware class and shippable as in-repo defaults, so a fresh install on
recognized hardware starts from the shipped profile and discloses only if
its own observations deviate.

Zone split: the voice/mic/AEC/audio-validation sweep is the audit
campaign's; the measurement-zone fingerprint parks are re-adjudicated by
the tuning program under this ADR in its doctrine wave — its
CLAMP/INTEGRITY/DISCLOSURE taxonomy is this same ruling, applied to
measurement.

## Consequences

Behavioral win first, lines second: this is more conversion than deletion
(proof machinery becomes disclosure plumbing). Accepted risk: a
degraded-but-running state (e.g. stale AEC alignment) persists until
someone reads the doctor — mitigated by loud disclosure, and consistent
with the charter's fix-forward posture. BRINGUP's AEC section gets a second
edit when the code flips (its first edit documents today's behavior).
Rejected: keeping park-until-proven outside the clamp list for the sake of
gold-plated guarantees a solo hobbyist project does not need.
