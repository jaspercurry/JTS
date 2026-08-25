# ADR-0106: A verification artifact's identity is never migrated in place — a changed edge re-measures

- **Date:** 2026-08-25
- **Status:** Accepted

## Context

`/var/lib/jasper/chip-aec-alignment.json` is the chip-AEC commissioning
artifact: it records the hardware identity that was measured and the `K` the
measurement produced, and `jasper-aec-init` refuses to apply a delay when the
live box does not match that identity. It is a verification record, not a
configuration file.

Its identity schema has grown twice, and the two cases were handled
differently.

Schema v1 → v2 added live hardware identity fields to an existing artifact
**by enrichment**: the file was rewritten with the new fields while
preserving its proven `K=248`. That was not a recommission and not a code
migration, and it could not certify anything false, because the added fields
described hardware that had not moved and was not about to.

Schema v2 → v3 added `dac.format` — the final-edge sample format outputd
negotiated and reports as `dac.format`. That field exists precisely to guard
the electrical edge that the outputd native-format write moves. Enriching it
would mean writing a value nobody re-measured into the field whose whole job
is to prove the edge was measured.

## Decision

**When a field is added to a verification artifact's identity, every existing
artifact fails the identity check and the box re-measures. No enrichment, no
migration code.**

Enrichment machinery at a guard boundary is a fail-open mechanism at exactly
the point the guard has to be trustworthy: it hands a box a "valid" artifact
for an edge nobody re-measured. At the current fleet size (two lab boxes) one
foreground recommission per box is cheaper and safer than shipping migration
code that weakens the guard permanently.

The v1→v2 enrichment is not a precedent to generalise from. The
discriminator is whether the new field asserts something that was measured:
fields that only *describe* hardware that has not moved can be filled in;
fields that *certify* an edge cannot.

## Consequences

- Adding an identity field is a fleet event, not a silent deploy. The first
  `jasper-aec-init` after the deploy parks the whole managed-XVF stack, and a
  human runs `sudo jasper-aec-commission` at the speaker.
- That cost scales with fleet size and is the reason to add identity fields
  deliberately rather than opportunistically.
- Read the scope of a field like `output_format` precisely before trusting
  it: it is outputd's own CLIENT edge, read back from the `hw_params`
  installed on the PCM outputd opened. On a raw `hw:` device — every
  commissioned box today — the client edge is the hardware edge. Through an
  ALSA `plug` it is not: the plug installs the client's request client-side
  and converts on the slave side, so the readback agrees by construction and
  cannot see the DAC. The identity field is not a substitute for pinning the
  slave format, or for deleting the conversion layer.
- Deliberately given up: rolling-deploy compatibility for this artifact. A
  mixed-version fleet parks the older boxes rather than aging their proofs
  forward.
