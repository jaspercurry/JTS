# ADR-0322: Rear calibration is a candidate section, spliced ahead of the role chain

- **Date:** 2026-09-16
- **Status:** Accepted
- **Extends:** ADR-0316, ADR-0318

## Context

ADR-0318 gave the rear calibration document its schema and its stage builder
(`jasper.active_speaker.rear_calibration`), but left runtime adoption —
where the document lives, where its compiled stage splices into the emitted
graph, and how the existing safety proofs treat it — for the #5161 adoption
lane (`docs/adr/0316-*.md`'s pending rear mute). This ADR closes that gap.

## Decision

**Storage.** The rear calibration document is a fingerprinted section of the
candidate/prescription document: `MeasuredCrossoverCandidate.rear_calibration`,
mirroring `bass_extension` field-for-field (default `{}`, validated in
`__post_init__` only when non-empty, folded into `_core()`/`to_dict()`/
`from_mapping()`/`compile_candidate_config()`). It is not a wizard-owned
store under `/var/lib/jasper/`. There is no second writer; the section
registry (`SECTION_KINDS`, `compose_candidate`) governs how it is proposed
and adopted, the same as every other candidate section.

**Authoring path.** An authored electrical document enters as the
`rear_calibration` section of a `jts_prescription` document
(`DOCUMENT_KIND`, `prescription_document.py`).
`jasper-crossover-prescriber compose --base saved|<fingerprint>` banks it
onto a `MeasuredCrossoverCandidate`. The existing baseline-profile apply
(`jasper-round apply <fingerprint>`) is the only path that carries a banked
candidate onto the box; the wizard's editing panel calls this same
judge/compose/apply path in-process rather than writing around it.

**Splice point.** The compiled stage (`compile_rear_stage`) splices
immediately after the `Mixer split_active_Nway` step and upstream of the
per-role filter chain. The rear output therefore stays a dynamic-bass
owner: `_channels_for_role` still groups it with the front woofer, so
`_owner_limiter_step` still emits exactly one Limiter per owner tuple, and
every downstream clamp the role chain already applies — crossover, role
delay, non-positive baseline gain, the soft-clip limiter, tweeter
protection — continues to see the rear output and is never bypassed by the
calibration stage.

**Runtime delay is whole-sample.** `compile_rear_stage` currently hardcodes
`subsample: true` on every emitted `Delay` filter (`rear_calibration.py`).
Runtime adoption must instead emit plain millisecond delay for the branch
path, with no `subsample`. This amends ADR-0318's "fractional delay uses
`subsample: true`" for that compiled path: the wording continues to govern
the acoustic-task/electrical-document convention, but the emitted
CamillaDSP stage rounds to the sample grid, because the consumers of that
grid (`branch_peak.py`, `summed_alignment.py`, `graph_prediction.py`)
already reject `subsample: true` as an unsupported graph. The residual is
bounded (≤ 10.4 µs at 48 kHz, ≤ 0.75° of phase error at 200 Hz) and
acceptable for the branch-cancellation use case; sub-sample timing belongs
to the FIR alternative path, not the branch path, and is not modeled here.

**Headroom.** The unmuted rear output sums two correlated branches (bass
and cancellation), which can add up to +6 dB worst case. Each chain's gain
is bound to ≤ 0 dB (narrowing `rear_calibration.py`'s general ±150 dB
CamillaDSP range for the runtime path), and the worst-case branch sum
(`max(0, 20·log10(10^(bass_db/20) + 10^(cancel_db/20)))`) is charged into
`program_headroom_db` beside the existing room-PEQ term, under the
existing `MAX_PROGRAM_HEADROOM_DB` ceiling.

**The `rear_output_not_muted` proof.** `_active_graph_evidence`'s blocker
changes shape, not intent. With no rear calibration document on the
candidate, the terminal-mute proof is unchanged: every `rear` output
variant must still satisfy `graph_safety.output_terminally_muted`. With a
document present, the proof instead requires that the compiled calibration
stage is well-formed (Filter/Mixer only, matching `compile_rear_stage`'s
own contract) *and* that the downstream role chain — crossover, delay,
gain, limiter, protection — still resolves for that output. A rear output
is never unmuted by the mere presence of a document; it is unmuted only
when both halves of that proof hold.

**Mixer-sequence proof.** The same evidence's mixer-sequence check
(`active_graph_mixer_sequence_invalid`) admits exactly the compiled
stage's `rear_out{N}_split` and `rear_out{N}_sum` mixers immediately after
`split_active_Nway` when a rear calibration document is present on the
candidate; with no document, the expected sequence is unchanged (the
active split alone, no post-split mixer).

**Measurement excitation.** The front/rear/both take does not run
through the protected-neutral measurement graph; it runs through the
candidate-branches (baseline-shaped) emitter, so the applied crossover,
protection, delay, non-positive gain, and limiter are already present on
the shared woofer chain. The take's explicitly excited target ids are
threaded into that emitter, so an undocumented rear the take names is
emitted un-muted, while a household/applied baseline with no document and
no take still mutes it byte-identically. Those excited ids reach the mute
door only from program admission, never from the on-disk snapshot:
admission refuses an excited-but-muted output, and the door refuses an
excited rear that is not on its role's grouped chain with the limiter
(`excited_rear_unprotected`), refuses a program-shaped graph by name
(`active_graph_program_shape_unproven`), and refuses two outputs of one
role confirming different protection sections
(`role_protection_sections_disagree`, never a union). This is the routing
and polarity qualification path ADR-0316 asked for.

## v1 limits

- **Mono only.** A stereo cardioid document is refused explicitly at the
  candidate boundary. Two documents for one stereo pair is deferred.
- **`fir` mode is refused at runtime.** `rear.mode == "fir"` validates under
  `read_rear_calibration` (the format accepts it), but `compile_rear_stage`
  as adopted by the candidate boundary in v1 only compiles `branches`. FIR
  compiles for its own tests but is not offered to the runtime graph yet.
- **Chain gains ≤ 0 dB**, per the Headroom decision above.
- **Wall-gap mismatch is a disclosed warning, never a park.** When the
  document's `geometry.cabinet_back_wall_m` disagrees with the value
  declared for that cabinet (`jasper-declare-geometry`), the candidate
  records a warning issue (`candidate.analysis["issues"]`); it does not
  block adoption or park the graph. This follows ADR-0101: a proof that
  passed stays valid until something observably breaks, and a
  provenance mismatch is disclosed, not gated.
- **Room-band overlap with the cancellation band is disclosed only.** No
  block. The room correction layer and the rear cancellation branch are
  not reconciled against each other in v1; an operator sees the overlap
  in disclosure, not a refusal.

## Consequences

Runtime adoption reuses every existing role-chain safety proof instead of
duplicating it for a third output kind — the calibration stage is plumbing
ahead of protection, not a parallel protected path. This makes the v1 scope
small (branches only, mono only, ≤ 0 dB chains) at the cost of deferring
FIR, stereo, and per-driver rear safety declarations to later work, per the
conductor's decisions on the open questions in the adoption plan. The
whole-sample runtime delay gives up sub-microsecond cancellation precision
in exchange for staying inside the existing `Delay`/`branch_peak.py`
contract, which already rejects `subsample: true`; a future FIR path can
recover that precision without touching this ADR. Disclosing (rather than
blocking on) wall-gap and room-band mismatches keeps a working graph
running per ADR-0101, at the cost of trusting the operator to read the
disclosed issue.
