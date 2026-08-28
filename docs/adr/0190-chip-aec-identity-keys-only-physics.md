# ADR-0190: Chip-AEC alignment identity compares only physics

- **Date:** 2026-08-28
- **Status:** Accepted

## Context

`AlignmentIdentity` carries 13 fields, all walked by `identity_divergence`'s
default comparison. Since #3003/ADR-0101 a divergence never parks — it
discloses and keeps running — but the disclosure still prescribes
`sudo jasper-aec-commission`, so a field with no timing physics still nags a
household toward an unneeded re-commission. Three carry none: `xvf_variant`
is implied by `xvf_firmware` (`bld_msg` is baked into the build), `beam_plan`
is hashed into `fixed_profile`, and `output_format` has no bulk-delay
mechanism — a width flip doesn't move frame-denominated latency, and its one
historical job (flagging a plug-vs-native edge move) is already covered by
`output_pcm`, since outputd writes natively either way. Forcing case:
`jts.local` is pinned on a schema-v2 artifact with no recorded `sys_delay` —
genuinely needs re-measurement, since the drift budget depends on that
recorded value — rejected as structurally "invalid" by the schema-unaware
required-keys check before it can reach the graceful superseded path, while
re-commissioning itself refuses on room ambiguity (peak ratio ~1.02 against
the 1.10 floor, lag centered, twice).

## Decision

Comparison narrows to the 8 fields K is actually measured against:
`xvf_firmware`, `fixed_profile`, `output_id`, `output_pcm`, `output_rate`,
`output_channels`, `output_period`, `output_buffer`. `xvf_serial` and
`output_hardware_key` stay per-unit disclosures, unchanged. `xvf_variant`,
`beam_plan`, and `output_format` become recorded-only forensics — written to
every artifact, never compared. The schema, its 13-field read requirement,
and the migration-free superseded path (ADR-0101, ADR-0106) do not move.
Follow-up PR-B makes the artifact *reader* schema-aware: keys required per
the artifact's own declared schema (a truthful reason for a pre-`sys_delay`
artifact instead of "invalid"), plus a shared-physics-field comparison for
genuinely superseded artifacts.

This supersedes **only ADR-0106's classification of `output_format` as an
edge-certifying identity field** — its Context calls `output_format` the
field that "exists precisely to guard" the electrical edge, and its
Consequences says to "read the scope of a field like `output_format`
precisely before trusting it." Under this ADR `output_format` is
recorded-only: still read back and stored, but it certifies nothing and is
never compared. Everything else in ADR-0106 stands unchanged: the
no-enrichment/no-migration rule for a schema bump, the v1→v2 enrichment
precedent, and the artifact-migration mechanics generally. ADR-0106 is not
edited — append-only, and still correct about the fields it was right about.

## Consequences

Staleness nags now name real physics only; editing `output_format`,
`xvf_variant`, or `beam_plan` alone no longer nags the fleet. Accepted risk:
an exotic DAC whose format flip changes USB alt-setting latency goes
unflagged by the identity — mitigated by a bench spot-check (commission at
S16, redeclare S32, re-run timing trials, expect `SYS_DELAY` within ±2
samples). `xvf_firmware` and `fixed_profile` now carry the full load of
catching a pipeline edit. A step-4 passive estimation of format's timing
effect was considered and parked.
