# ADR-0195: A rebuild that knows less is not a supersede

- **Date:** 2026-08-29
- **Status:** Accepted

## Context

`build_baseline_profile_candidate` re-derives an active-speaker profile from
current evidence rather than trusting the saved JSON, and fingerprints the
inputs it was handed (`_source_payload`). One of those inputs,
`measured_candidate_fingerprint`, is only ever passed by the two APPLY paths
(`correction_crossover_v2.handle_v2_apply`,
`commissioning_apply.finalize_retained_candidate_apply`). Every read-side
rebuild — the commissioning view, setup status, the `/sound/` cards — passes
none, so its `source` is the applied profile's minus that one key, and the
composite fingerprint therefore differs for every measured profile ever
applied.

`_revalidation_payload` decided staleness by comparing those composites, and
listed `changed` from a hand-written subset of the source keys that never
included `measured_candidate_fingerprint`. On JTS3, 2026-08-29, with a
measured profile applied and verify passing, the commissioning view reported
`status: ready_to_save_profile`, `next_action: Save active profile`, and
`revalidation: {required: true, reason: applied_profile_superseded,
changed: []}`. The door behind that action emits the basic profile — no
linearization, no blend — so the wizard's own recommended next step reverted a
measured tune, and stated no reason for asking.

The rebuild's own `status` cannot be repaired into saying "applied": its cache
gate compares the source fingerprint, the tuning owner (`manual` on every
read-side call, `automatic` on the record) and the graph-context snapshot, and
a measured profile differs on all three.

## Decision

1. **Staleness compares the source keys, never the composite fingerprint, and
   `changed` is derived from the source payload.** A key on the applied record
   that no rebuild can reproduce — the closed set
   `_REBUILD_BLIND_SOURCE_KEYS`, today `{measured_candidate_fingerprint}` —
   means the rebuild knows less. Any OTHER saved-only key is incomparable and
   supersedes: one honest re-check beats a silent "unchanged". A key the
   rebuild carries and the record does not is a real change. No hand-listed
   subset: a subset stops detecting whatever `_source_payload` grows next.
2. **One verdict, computed at the producer.** `build_baseline_profile_candidate`
   attaches `applied_profile_stands` — a retained applied record
   (`applied_recomposition_profile`, the artifact
   `load_applied_baseline_profile_state` serves) with nothing superseding it.
   The view and the `/sound/` card consume that field; neither re-derives it,
   and neither reads the rebuild's own status, which cannot reach "applied"
   for a measured profile.
3. **Whether the speaker still PLAYS it is a separate question, asked once by
   the loader.** Only `APPLIED_PROFILE_DISPLACED` revokes the claim. The
   verdicts that mean "could not check" — `APPLIED_PROFILE_PATH_UNKNOWN`,
   `APPLIED_PROFILE_RUNNING_UNKNOWN`, and `APPLIED_PROFILE_CONFIG_MISSING`
   from a fresh stat of the recorded path, since the record's own
   `config.exists` is frozen at apply time — disclose a caveat and keep the
   recovery door offered. Conflating them is the mistake that reader exists to
   prevent.
4. **A standing applied profile carries its own driver-target proof, under
   compared-and-clean guards.** `build_baseline_profile_candidate` grants it
   directly from the applied record when the record has a source fingerprint
   (it was compared at all), nothing changed (so the topology is unchanged),
   and it is not `provisional` (sensitivity-derived trims are not
   measurement). The `applied_profile_revalidation_...` relaxer is untouched
   and stays the required-branch mechanism; without this grant a measured
   phone-path profile would newly fail the audible combined-test gate and the
   validation writer, which both read `driver_target_proof.complete`.
5. **The basic door is never the recommendation over a measured profile, and
   never hidden.** In the applied steady state it is a secondary action named
   for what it replaces. When something genuinely superseded a measured
   profile, the primary points at re-measuring — that is what carries the tune
   forward — and the named basic door stays beside it. No gate: the endpoint
   is unchanged.
6. **A terminal status may not stand beside an outstanding action.** The view
   reports `applied` only when nothing is left to offer; an applied profile
   with an unvalidated combined check reports the check and still marks the
   profile step done. A candidate blocker does not un-apply a profile; it only
   means the view must not dead-end.

## Consequences

Easier: one fact, one source. The server view and the `/sound/` profile card
now agree, and the card names the applied config rather than the candidate the
rebuild happened to compile. The wizard stops recommending the basic door over
a live tune, and a nag that does fire names the key that changed.

Harder: the applied record's identity now depends on a field the rebuild does
not carry, so anyone adding a key to `_source_payload` must ask which callers
pass it. The pin in
`test_a_write_free_rebuild_knowing_less_does_not_supersede_the_applied_profile`
asserts the blind set is exactly `{measured_candidate_fingerprint}` and fails
if it grows.

Considered and not taken, both left open on purpose:

- **Recover the measured candidate and tuning owner from the saved anchor** so
  the cache gate hits and the rebuild reports `applied` once for everyone. The
  source fingerprint also names the emitted candidate file, so making a rebuild
  reproduce it changes which file a subsequent compile writes — an apply-path
  change, not a status fix.
- **Carry `measured_candidate_fingerprint` forward on `write=False` only.** It
  repairs the read surfaces at the source with no apply-path change, and it is
  the seam to reach for if a second blind key ever appears. It was not taken
  here because it makes a rebuild claim an input it was not handed, and the
  blind-set rule above is honest about what the rebuild knows.

Known gap, pre-existing and untouched: source staleness does not cover graph
inputs outside `_source_payload` — `bass_extension_profile` and the
`candidate_graph_context` fields (that function's own "same filename,
different bytes" blind spot). A change to one of those leaves the applied
profile reading as standing. Under the old always-required bug an operator
could stumble into a save affordance that happened to rebuild them; the named
secondary door now serves that need deliberately rather than by accident.

Nothing here is a gate: `baseline-profile/save-and-apply` is unchanged and an
operator who wants the basic profile can still apply it.
