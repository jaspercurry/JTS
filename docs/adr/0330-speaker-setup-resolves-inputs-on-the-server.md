# ADR-0330: The server resolves speaker setup inputs

- **Date:** 2026-09-21
- **Status:** Accepted

## Context

The JTS3 commissioning smoke test found that the page told the owner to measure
before saving a working base, marked an unmeasured base as measured, and lost its
tuning menu after apply. Browser code also copied research into manual settings,
calculated trims, and built output layouts independently of the backend.

## Decision

The speaker page has its own entry module and one backend setup view. Named
setup operations call the existing topology, draft, and DSP writers and return
the fresh view. Measurements remain optional after base setup. Applying a base
does not establish measurement evidence.

Keep the existing draft store. Research is normalized evidence;
`manual_settings` contains installation facts and explicit edits. A shared
resolver combines them by physical driver target for preview, protection, and
apply. Resolved values and form progress are not persisted. Legacy values keep
their existing authority: unspecified gain provenance stays pinned, and an
ambiguous role-only declaration does not prove per-target protection.

Extend ADR-0323's prompt context only with declared installation facts, such as
enclosure, pad hardware, horn, and coil wiring. Do not include earlier crossover
choices, protection limits, or measured corrections. Research cannot overwrite
installation facts. Reply binding remains target and model based, without
request tokens, fingerprints, or copy prerequisites.

The default page shows the output map, driver details, research exchange, and
base save. Full values and manual edits are available under Details. File paths,
candidate identities, and commands are not normal user-facing status. Tuning
prompts retain the technical information the external assistant needs.

## Consequences

The browser no longer owns trim math, research merge policy, topology templates,
or measurement completion rules. Existing stores and the current apply path
remain authoritative. No new workflow store, daemon, approval gate, or recovery
system is introduced.
