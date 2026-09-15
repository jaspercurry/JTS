# ADR-0316: Rear woofer outputs have a physical variant identity

- **Date:** 2026-09-15
- **Status:** Accepted
- **Amends:** ADR-0258

## Decision

A rear woofer is another physical output of the woofer role. It does not
increase the acoustic way count. `output_variant` defaults to `primary`;
`rear` is supported only on `woofer`. Each side retains its primary outputs
and can declare one rear output. DAC indices remain explicit assignments.

Topology, preset and baseline documents containing a rear output use schema
version 2. Primary-only documents keep their version-1 serialization. Legacy
physical target IDs remain `group:role`; the rear ID is `group:woofer:rear`.
Variant participates in physical fingerprints, audition evidence and staged
path signatures. Changing a front assignment to rear cannot inherit the
front's physical verification, even on the same DAC index.

Model specifications may be shared. Installed acoustic transfers and driver
corrections are physical-source facts: the same driver model does not establish
the same loading, amplifier gain or response. This narrows ADR-0258's
measured-once wording; it does not create a second calibration store.

## Incremental boundary

The first implementation of #5161 carries these identities through preset
binding, existing CamillaDSP emission and independent runtime proof. Every
rear output ends in a hard mute, including commissioning, measurement and
normal/follower emission. Existing primary filters and protection remain.
Stereo program ingress and the existing topology-derived ACTIVE ring width
are unchanged; two-way mono/stereo can carry three/six physical outputs.

Remove the pending mute only when the existing prescription/compiler path
admits the fitted rear transfer, physical protection and routing/polarity
qualification. A front-woofer correction, acoustic-model seed or identity
flag alone cannot remove it. The editable reinforcement/cancellation branches,
FIR alternative and acoustic calibration format follow the section registry
being completed in #5073 M2. The setup UI follows W2/W3.

This boundary provides no measured directivity claim or hardware channel-map
qualification. It does not migrate the installed topology or adopt a tune.
