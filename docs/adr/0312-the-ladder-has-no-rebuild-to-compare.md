# ADR-0312: The ladder has no rebuild to compare

Date: 2026-09-13
Status: Accepted
Supersedes: ADR-0195

Commissioning reviews use `compile_commissioning_profile` and the applied record.
The banked applied candidate is the base; a new speaker uses its declared candidate.
Both use the same tuning graph composer, so there is no second rebuild to compare.
The ladder reports the applied fingerprint, apply time, and configuration path.
Review readiness and refusal codes come from the composer.
An identity mismatch from `reviewed_candidate_refusal` is disclosed as stale.
That disclosure does not revoke an applied proof or block a ready review.
Delete the second candidate builder and its unused apply entry points.
Delete `_revalidation_payload`, `_applied_profile_proves_driver_targets`, and the
`finalize()` fields for revalidation, standing profiles, and borrowed driver proof.
Retire automatic candidate review: the speaker program owns measured trims;
`level_trim.declared_driver_gains` owns declared trims.
