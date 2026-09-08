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
- [Architecture decision records](adr/README.md)
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

Start with the [runbook entry contract](tuning-operator-runbook.md#entry-contract),
then the selected tool's `--help`. Read further only for the question at hand:

- [Doctrine](measurement-loop-doctrine.md): authority and layer boundaries.
- [Methodology](tuning-methodology.md): optional scientific interpretation.
- [Crossover](active-crossover-information-design.md) and
  [Room](room-correction-information-design.md): their product boundaries.
- [Layers](active-speaker-tuning-layers-design.md): fitting and composition rationale.

The capture session owns `open`, `measure`, and `close`; separate tools analyze
and bank evidence ([ADR-0198](adr/0198-the-unwired-engine-verb-half-is-deleted.md)).
Adoption is explicit. Runtime readback checks the applied graph; a new acoustic
capture is a separate experiment.

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
