# JTS documentation

This index points to current public references. Production code and deployed
state are authoritative when prose conflicts with either.

## Start here

- [Project overview and architecture](../README.md)
- [Quick start](../QUICKSTART.md)
- [Full hardware bring-up](../BRINGUP.md)
- [Roadmap](../PLAN.md)
- [Contribution guide](../CONTRIBUTING.md)
- [Security policy](../SECURITY.md) and [privacy policy](../PRIVACY.md)

## Current engineering references

- [Agent and contributor rules](../AGENTS.md)
- [Architecture decision records](adr/)
- [Audio paths](audio-paths.md)
- [Extension contracts](extensibility.md)
- [Testing and measurement tools](testing-tooling.md)
- [Design language](design-language.md)
- [Web IA](web-ia.md)
- [Multi-user Spotify](multi-user-spotify.md)
- [Third-party license notices](../LICENSE-third-party.md)
- [Documentation impact map](doc-map.toml)

ADRs are append-only. They own durable decisions and their reasons. Current
references describe how the repository works now.

## Tuning and measurement

Read these current sources in order:

1. [Measurement loop doctrine](measurement-loop-doctrine.md)
2. [Tuning methodology](tuning-methodology.md)
3. [Tuning operator runbook](tuning-operator-runbook.md)
4. [Active-crossover product contract](active-crossover-information-design.md)
5. [Room-correction product contract](room-correction-information-design.md)
6. [Tuning layers](active-speaker-tuning-layers-design.md)

The production tuning session uses `open` and `close` for its lifetime and
exposes `measure` as its one tuning operation. Its four `EngineSeams` fields
own the graph, volume claim, records, and playback transaction. Doors-and-banks
tools analyze banked evidence, recommend the next action, and persist their own
accounting; [ADR-0198](adr/0198-the-unwired-engine-verb-half-is-deleted.md)
records that boundary. Apply remains an explicit operator action followed by
verification.

[The bass-extension plan](HANDOFF-bass-extension-plan.md) remains the parked
plan and authorization source under
[ADR-0018](adr/0018-bass-extension-stays-parked.md). It is not a statement that
bass extension is active.

## Plans, research, and history

[PLAN.md](../PLAN.md) owns current ordering. Other files named `plan`,
`proposal`, `research`, `review`, or `audit` are inputs or records, not current
operating references unless a current document says otherwise.

- [Research material](research/)
- [Historical records](historical/)

Historical files preserve evidence and provenance. They do not describe the
current repository or deployed speaker.
