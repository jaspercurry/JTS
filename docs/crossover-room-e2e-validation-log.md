# Active-crossover + room-correction — end-to-end hardware validation log

> **Status: session artifact (living log).** A from-scratch, on-hardware
> validation of the full speaker-calibration story on JTS3, run 2026-07-17.
> Not an operational reference — it records what was exercised, what worked,
> and UX/API findings worth follow-up. Screenshots live outside the repo
> (`~/jts-e2e-shots/`, `<flow>-<NN>-<step>.png`).

## Hardware under test

- **Speaker:** JTS3 — HiFiBerry DAC8x (8 outputs, single clock domain).
- **Drivers (active 2-way, group `main`, mono):**
  - Woofer → **DAC output index 0** — Dayton Audio **Epique E150HE-44** (sens 83.3 dB, 8 Ω, HP 40 Hz, hard band 45–4000 Hz).
  - Tweeter → **DAC output index 1** — B&C **DE250-8** compression driver (sens 108.5 dB, 8 Ω, protective HP **2000 Hz ≥24 dB/oct**, hard band 1600–20000 Hz, HF level cap −65 dBFS).
  - Crossover: **Linkwitz-Riley, 2000 Hz, 24 dB/oct** (Fc aligned to the compression-driver protective-HP floor).
- **Measurement mic:** miniDSP **UMIK-2 #810-8494**, plugged into the operator's Mac Studio (same machine driving the browser). getUserMedia capture confirmed (secure context, AGC/echo/NS off, 48 kHz).

## Goal

Reset to scratch → re-commission the active crossover from zero → measure with
the UMIK-2 → align → apply → run room correction, capturing a labeled screenshot
at every step, and fix any UX-honesty issues found. Every code change goes
through an adversarial Opus review gate (0 blockers + 0 should-fixes) + green CI
before merge, then deploy + hardware re-validate.

## Preamble fix — W3.3 honest stepper ✅ (merged #1576, deployed, validated)

**Bug:** the crossover commissioning stepper rendered non-monotonically —
`①done ②③④pending ⑤done` — whenever a crossover was *applied* but the
measurement run hadn't happened (`done_manual` / `choose_tuning` screens).
Durable *applied-state* was conflated into per-*run* step status.

**Fix:** two pure helpers at the single envelope output boundary in
`jasper/active_speaker/crossover_envelope.py` — `_project_run_steps` (monotonic
frontier; a step is "done" only if every earlier step is, and a step being
*redone* is excluded from the prefix) and `_applied_chip` (durable applied-state
→ its own `applied` envelope field, rendered as a status chip). Screen logic +
safety reads untouched; schema 5→6. Pinned by a pre-fix repro + a general
"steps are always monotonic" invariant guard.

**Gate:** Opus adversarial review found 1 should-fix (the automatic `done`
terminal regressed `apply`→pending because the automatic path skips the
`alignment` measurement step); fixed at the source (backfill `alignment` +
terminal `"complete"` sentinel) + added the completeness guard test; re-review
→ 0/0. CI green (one known FD-exhaustion flake rerun). Squash-merged, deployed
to JTS3, re-captured on hardware: `done_manual` now renders `speaker_setup=done,
rest pending` + a "Manual crossover applied" chip. (`_evidence-stepper-{before,after}-fix.png`)

## Journey — manual commissioning (no mic) ✅

Driven autonomously via the `/sound/` CSRF-authenticated API (this lane uses no
mic). Operator authorized proceeding without human-ear confirmation (low-risk
bench); each confirmation grounded in the flow's own live software evidence.

| step | result | shot |
|---|---|---|
| Nuclear reset → passive | `output-topology/reset`; wiped to draft, no groups, reconcile ok, silent | `xover-00-reset-to-passive` |
| Declare topology + drivers | group `main` active_2_way verified; drivers confirmed, safety profile `confirmed`, 0 issues | `xover-01-declaration` |
| Protected speaker setup | per-driver ramp confirm: woofer 250 Hz @ −80 dBFS (out 0); **tweeter 5 kHz @ −80 dBFS (out 1) with `tweeter_protected_while_audible: True`** (highpass verified live, woofer muted) | `xover-02-protected-setup` |
| Combined test (safety) | summed test validated `blend_ok` 1/1 | — |
| Apply base profile | `applied`; levels seeded from sensitivity spec, **provisional pending mic measurement** | `xover-03-manual-applied` |

End state: a working active crossover applied from scratch; `done_manual` with
the honest monotonic stepper + "Manual crossover applied" chip; tweeter now
protected by the *applied* crossover highpass (passive-window exposure over).

## Journey — mic / crossover-config flow (UMIK-2) ⏳ in progress

The automated mic flow (`/correction/crossover/`, browser getUserMedia) refines
the provisional sensitivity-derived levels into measured ones: mic + level →
measure each driver → align → apply the *measured* crossover. Driven from a
headed browser on the operator's Mac Studio with the UMIK-2. **Next.**

## Room correction (UMIK-2) ⏳ pending

Full room pass on the applied active profile: measure → analyze → apply →
accept. Ingests the UMIK-2 calibration by serial (`/correction/calibration/fetch`,
`minidsp_umik2`, `810-8494`).

## Observations & findings

1. **W3.3 stepper honesty — fixed** (#1576). The primary deliverable; validated
   on hardware.
2. **Combined-test (`/active-speaker/summed-test`) raw-HTTP contract is awkward
   — candidate follow-up.** The endpoint is a *blocking long-poll*: it plays a
   looping speech stimulus and does not return until stopped (or a 10-minute
   watchdog). Whether the test counts as "confirmed" is carried entirely by an
   **undocumented, load-bearing magic string** — `/summed-test/stop` with
   `reason:"operator_confirmed"` sets `captured/audio_emitted=true`; the
   endpoint's own default `reason:"operator_stop"` (and a plain stop, and the
   watchdog) do **not**, so the later `summed-validation` blocks with
   `summed_validation_test_missing`. A client that abandons the request without
   the confirming stop holds the single global summed-test lane for up to 10
   minutes. Nothing in the endpoint docstring surfaces that `reason` is a
   semantic confirm flag — a raw-HTTP driver can only discover it from the
   source or the browser JS. (Matches the pre-existing "combined test can wedge"
   note.) The shipped browser UI does this rendezvous correctly; the finding is
   about the raw contract's discoverability + the wedge lane.
3. **Manual protected-setup is fully API-drivable, no mic** — confirmed the exact
   ordered sequence end to end (topology → channel-identity ×2 →
   channel-protection → design-draft → crossover-preview → per-driver
   commission-load/ramp-step/ramp-ack → summed-test/stop(operator_confirmed)/
   summed-validation(blend_ok) → baseline-profile → baseline-profile/save-and-apply).
   The audible ramp step has a short playback TTL, so ramp-step→ack must be
   near-atomic (multi-turn delay expires the step).
4. **Tweeter fail-closed protection verified live** — the commissioning graph
   reports `tweeter_protected_while_audible: True` and only emits an in-band
   (5 kHz) tone on the tweeter output with the woofer muted. The safety floor
   works as designed on real hardware with a real compression driver.
5. **UMIK-2 browser capture works** for the local-device measurement path
   (headed Chrome, HTTPS `/correction/`, deviceId-selected UMIK-2, clean
   settings) — enables driving the mic flow autonomously.

## Method / gate

Fable-led orchestration: diagnose → tight closed spec → Sonnet implements in an
isolated worktree (tests same-change, pre-fix repro) → separate Opus adversarial
review (0 blockers + 0 should-fixes) → green CI → squash-merge → deploy →
hardware re-validate. Safety floor (graph_safety, driver_safety,
excitation_safety_plan, volume clamps, restore paths) never touched.

_Last updated: manual commissioning applied; mic flow next._
