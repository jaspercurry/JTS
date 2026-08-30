# JTS roadmap

JTS is a working smart speaker. This roadmap lists only active direction.
Shipped work belongs in [CHANGELOG.md](CHANGELOG.md). Implementation history
belongs in Git, issues, pull requests, and
[decision records](docs/adr/).

## Now

- Complete the active tuning cutover around the production session path:
  `open` → `measure` → `close`. The web flow drives those calls. The
  doors-and-banks tools analyze banked evidence, recommend the next action,
  and record the round, as defined by
  [ADR-0198](docs/adr/0198-the-unwired-engine-verb-half-is-deleted.md).
- Run the full operator flow on hardware: establish the measurement level,
  capture the position set, inspect the banked evidence, apply an accepted
  change explicitly, and verify the result. The current procedure is in the
  [tuning operator runbook](docs/tuning-operator-runbook.md).
- Fix only gaps shown by that run. Keep the captured bundle as evidence and
  keep the hardware constraints and measurement semantics unchanged.

## Next

- Remove any duplicate tuning state or dead path exposed by the hardware run
  after its active consumer has moved.
- Continue bass-extension commissioning only after the parked plan's limiter
  evidence and hardware gates are satisfied. See
  [ADR-0018](docs/adr/0018-bass-extension-stays-parked.md).
- Improve wake-word performance with measured corpus work and a custom model.

## Later

- Extend multiroom behavior when another household use case earns the added
  hardware and operating cost.
- Add music sources only when their memory, credential, and maintenance costs
  fit the Raspberry Pi budget.

For current architecture and engineering references, start at
[docs/README.md](docs/README.md).
