# ADR-0339: Declared topology and observed hardware have separate owners

- **Date:** 2026-09-22
- **Status:** Accepted
- Refs #5515. Builds on ADR-0235 and ADR-0283.

## Decision

`output_topology` owns declared values, schema, layout validation and
resolution, pure fingerprints, and the pure projection of an explicit
`OutputHardwareState` into declared hardware. Evaluation and serialization
use declared values only.

`output_topology_store` owns saved-intent reads and publication, proof-stamp
IO, and draft creation from observed hardware. `output_topology_observation`
owns clock and hardware comparison, composite identity matching and re-pin,
saved-topology runtime policy, and observed-output assembly. Clock reports
use the caller's hardware snapshot. The observation owner consumes the
existing runtime roleful predicate; it does not define another.

Classification, observed facts, normalization, and hardware-state persistence
stay in `output_hardware`. This ownership split changes no output policy,
refusal distinction, fingerprint projection, or stamp version. Missing saved
intent can use observed child order; unreadable saved hardware refuses dual
runtime mapping while the parking policy leaves malformed intent alone.
