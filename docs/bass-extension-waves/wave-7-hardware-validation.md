# Wave 7 — hardware validation + docs (operator program, Codex assists)

Read `docs/bass-extension-waves/README.md` first. Prereqs: all prior
waves merged and deployed to a lab box via
`bash scripts/deploy-to-pi.sh`.

**Executor split:** the measurement program is the operator's (with a
Claude session driving the Pi). Codex takes the paper-trail items at
the bottom, each as a small scoped task.

## Operator program (in order)

1. **Transition audibility re-check** on final code: repeat Wave-0
   Spike 1 through the real scheduler (dial spins across anchors
   while music plays; capture the reference tap; listen).
2. **Sealed commission end-to-end** on the lab box: full wizard run,
   default mode. Then immediately run **deep mode** on the same
   speaker and compare: derived/interpolated anchors vs fully
   measured ones. If any derived anchor is >1 rung optimistic, file
   the calibration issue (plan §14.4) before any threshold tuning.
3. **Deliberately-wrong runs** (each must refuse, not degrade):
   bump the mic mid-ladder (`MIC_MOVED_BETWEEN_RUNGS`); commission
   with an iOS browser that won't attest AGC (ramp's AGC gate);
   ported box declared as sealed (fit-order refusal); pull the
   capture phone mid-rung (capture failure path + restore).
4. **Ported and passive-radiator commissions** on whatever boxes are
   available (buying one cheap PR bookshelf speaker is explicitly
   worth it). Check the fb/notch landmarks against a REW/DATS
   impedance measurement; record agreement % (feeds plan §14.6).
5. **Sustain-test calibration**: at the sweep-clean ceiling, run
   longer holds (2–3 min) than the commissioned 60 s and re-sweep —
   does the 60 s verdict predict the longer-hold behavior? Adjust
   `MarginPolicy` sustain durations only with this data in hand.
6. **A week of daily listening** with the profile armed: watch
   `journalctl | grep event=bass_ext`, `/state.bass_extension`, and
   the doctor; note any perceived pumping/tonal steps with timestamps
   so they can be matched to `target_change` events.
7. **Latency cert re-run** (`route latency evidence` doctor path) —
   expected delta zero; the cert gate is the proof.

## Codex-assist tasks (one small PR each, normal charter rules)

- **Operational doc**: write `docs/HANDOFF-bass-extension.md`
  (current-state-first, <400 lines, `Last verified:` footer) from the
  as-built system; add the historical tag to
  `HANDOFF-bass-extension-plan.md` pointing at it; update the README
  atlas entry and `docs/doc-map.toml` (add the `jasper/bass_extension/**`
  glob to the correction/measurement subsystem or a new subsystem
  entry — whichever the doc-map's granularity note prefers).
- **Threshold tuning PRs** from operator data: `MarginPolicy` /
  mic-moved-correlation / sustain-duration changes, each bumping
  `BASS_EXTENSION_ALGORITHM_VERSION` and updating the pinned
  change-detector tests in the same PR.
- **`.env.example`**: prose-commented entries for the two path
  overrides (profile state, session state) if not already present.
- **BRINGUP.md note**: one short section pointing at the wizard, if
  the operator confirms the flow is household-ready.

## Exit criteria

Plan §12's Wave-status table marked done; the operational HANDOFF is
canonical; at least one sealed AND one ported-or-PR speaker
commissioned on hardware with the week-long soak logged; every
deliberately-wrong run refused with its typed refusal.
