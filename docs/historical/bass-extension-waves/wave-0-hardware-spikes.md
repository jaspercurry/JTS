# Wave 0 — hardware decision spikes (operator runbook, NOT a Codex prompt)

**Executor:** the operator (Jasper) with a Claude session driving a
lab box (jts3 recommended). ~2 days. Nothing in later waves is
blocked on Codex here, but Wave 3 assumes this memo exists and Wave 5
cannot start without it.

**Deliverable:** a short decision memo committed as
`docs/research/2026-XX-XX-bass-extension-spikes/README.md` answering
the four questions below with measurements, plus a one-line verdict
each. Update `HANDOFF-bass-extension-plan.md` §12's Wave-status table.

## Spike 1 — transition mechanism (R1 vs R2)

Question: is a live `PatchConfig` coefficient step on a
`LinkwitzTransform` biquad audibly clean, or do we need the
parallel-branch Aux-fader crossfade?

Method sketch: hand-write a test CamillaDSP config carrying a named
`LinkwitzTransform` biquad on both channels (any lab graph is fine —
this is not the product emitter). Play (a) a 45 Hz sine, (b) pink
noise, at a moderate level. Drive transitions with
`CamillaController.patch_config` between two family-adjacent
parameter sets (e.g. 61→52 Hz corner, Q 0.65):

- single hard swap;
- 6-step interpolation of `(freq_target, q_target)` over ~600 ms;
- for contrast, a `set_config_file_path` reload of the same change.

Capture the electrical output at the `:9891` reference tap for each,
locate the transition instants, and measure the worst
sample-to-sample discontinuity / any transient burst relative to the
signal. Listen too. Verdict: R1 clean / R1 marginal / need R2.

## Spike 2 — `PatchConfig` semantics

Confirm on the running product graph: does a patched filter parameter
survive (a) `set_volume_db` writes, (b) websocket `Reload`, (c) an
`apply_dsp_config` full swap? Expected: survives (a), reset by
(b)/(c). Record actuals — Wave 5's reconciler tests encode this.

## Spike 3 — harmonic extraction sanity

Run the Δt_n = L·ln(n) extraction (a scratch notebook using
`regularized_deconvolution_full` output is fine — Wave 1 productizes
it) on one real nearfield woofer sweep. Check: H2/H3 impulse images
appear at the predicted offsets; the derived THD-vs-frequency curve
is plausible against REW's distortion analysis of the same capture.

## Spike 4 — nearfield mic ceiling

At the loudest level the ladder would plausibly reach on the lab
speaker, does a phone mic at 1–2 cm from the cone clip? Establish a
recommended mic distance (and whether ported boxes need a
"port-exit vs cone" distance note). This becomes wizard copy in
Wave 6 and possibly a rung stop-condition tweak in Wave 4.

## Safety notes

Use the existing measurement-window + ramp machinery where possible;
start every level walk from quiet; keep a hand on Stop. Nothing here
requires exceeding ordinary listening levels except the top of
Spike 4, which should stop at the first clip indication.
