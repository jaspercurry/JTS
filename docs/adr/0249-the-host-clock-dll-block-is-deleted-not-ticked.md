# ADR-0249: The host-clock `dll` block is deleted, not ticked

- **Date:** 2026-09-07
- **Status:** Accepted
- **Context:** `jasper-host-clock` instantiated a `jasper_clock::Dll` that the
  live control law never ticked. `ObsMode::Correction` — the sole live mode —
  drives a pure-integral law instead, so `dll_err_frames` and `dll_locked` were
  published to `/state` frozen at their construction values for the life of the
  process. [ADR-0109](0109-the-combo-host-clock-servo-observes-resampler-correction.md)
  records the same fact ("the `dll` block reads diagnostic zeros in `Correction`
  mode — it is not the controller there") while also citing a ≥10× cascade
  bandwidth separation derived from that DLL's configuration — a margin for a
  loop that does not run. No consumer ever read either field: neither is
  referenced anywhere in `jasper/`, `deploy/`, `scripts/`, `c/` or the web
  assets.
- **Decision:** Delete the `Dll` instance, its two accessors, and the `dll`
  object from the host-clock status fragment. The outer loop's justification is
  the plant analysis on `CORRECTION_INTEGRAL_GAIN` — near-unity DC gain through
  the inner resampler's lag, and why a third-order loop limit-cycles against it
  — not a bandwidth ratio against an unticked controller.
- **Consequences:** `/state`'s `host_clock` fragment loses `dll.err_frames` and
  `dll.locked`; its top-level key count goes 16 → 15 and nothing else moves.
  The byte-exact fragment pin moves with it. `jasper-host-clock` no longer
  depends on `jasper-clock`; the crate is still reached through
  `jasper-resampler`, so the deploy staging list is unchanged.
  This supersedes ADR-0109's cascade-separation clause and ADR-0214's inclusion
  of `dll.*` among the gauges that freeze during a hold — both remain accurate
  about everything else they say.
  Given up: a place to hang a future outer DLL without re-adding the field. If
  the outer law ever becomes a DLL, its diagnostics are a new decision with a
  live reader, not a revival of a frozen one.
