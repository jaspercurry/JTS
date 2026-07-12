# HANDOFF — correction measurement hub at `/correction/`

> If you are picking this up across sessions: this is the canonical
> planning + design document for the HTTPS correction measurement surface. Read
> the **Status** and **Architecture decisions** sections first. The
> phased plan is the work tracker — when a phase ships, mark it ✅ and
> update the Status. The "Things to ignore" sections are deliberate
> scope discipline, not omissions.

> The crossover builder's user journey, manual/automatic replacement semantics,
> and parameter ownership are canonical in
> [`active-crossover-information-design.md`](active-crossover-information-design.md).
> This handoff owns the HTTPS measurement surface and room-correction behavior.

## Status

- ✅ **P2 — relay-closed level-match ramp (hardware + software complete).**
  The settle-based `RampController` /
  `MeasurementRamp` kernel lives in
  [`jasper/audio_measurement/ramp.py`](../jasper/audio_measurement/ramp.py)
  (quiet-start staircase → pre-window freeze → buffered settle read →
  mid-window jump → k-confirm lock; the shared dynamic cap is strictly the
  lower of `original+12`, the configured absolute ceiling (default −3 dB),
  and 0 dB — there is no floor
  that can jump a quiet listening setting upward; clip/trust/feed-liveness/
  derived safety timeout; exact — never cap-clamped — restore of the user's
  pre-ramp volume). `MAXED_OUT` is a failed attempt, stores no lock, restores
  immediately, and asks the household to raise the external amplifier. The
  shared kernel defaults to that fail-closed policy. The active-crossover
  near-field lease and the room/verification listening-position session opt
  into `bounded_low_level`: at the cap they may store an explicitly degraded
  lock after fresh post-latency samples prove frozen AGC, live delivery, no
  clipping, the existing noise-floor margin, <=1.5 dB spread, and <=20 dB
  preferred-window shortfall. Room's listening-position owner allows a
  +15 dB rise up to the unchanged 0 dB hard ceiling because its measurement
  stimulus is already −12 dBFS; crossover/near-field keeps the shared +12/−3
  cap. The owning flow surfaces the shortfall and
  downstream sweep-quality gates still decide whether the evidence is usable.
  The
  correction adapter
  ([`jasper/correction/level_match.py`](../jasper/correction/level_match.py))
  adds the per-geometry `MeasurementLevelLock` store, the raw-band
  uniform-shift drift check, the armed gate, the run-token-scoped
  `RelayLevelFeed`, and terminal host events re-posted until the relay
  echoes them; the phone streams batched level samples from
  `capture-page/js/level-events.js` (`kind="level_ramp"` spec). The
  browser-locked `AutolevelController` remains the no-relay local
  fallback (`run_autolevel` unchanged; `MeasurementSession.
  run_level_match` is the additive relay seam). It now RETAINS the
  running `LevelMatchSession` in a **single-flight**, identity-guard-
  cleared slot (unlike `_autolevel_controller`, a permanent controller,
  this is per-run — an overlapping run is refused, never a stomped
  slot) and exposes `lock_level_match` / `cancel_level_match`, so a
  manual controller seams remain available to trusted adapters, while the
  shipped phone flow refuses *before playing a tone* when the browser cannot
  prove AGC is disabled. The relay validates the selected mic/calibration once,
  freezes a compact setup binding, and waits for a token-scoped rolling ambient
  median (ten finite 200 ms samples / two seconds) before the tone starts. A
  failed ramp exposes received/finite/trusted/drop counts plus maximum observed
  RMS, peak, and signal-over-noise margin, so `no_usable_samples` identifies the
  exact admission gate instead of hiding behind a generic error; one
  USB startup block can never become the noise-floor source of truth. It fails
  closed on every CamillaDSP gain write. A successful lock retains the
  target but restores the user's original listening volume immediately. Each
  room/verify/crossover sweep reasserts the target only inside
  `measurement_window()` and restores the original in that window's `finally`,
  before renderers resume. Ensure/restore share one async transition lock;
  safety does not depend on an in-memory expiry timer. All
  synthetic — H1 (on-device settle cadence + iOS/Android AGC-freeze
  confirmation) supplies the real threshold values; the `JASPER_RAMP_*`
  env knobs in `.env.example` are documented placeholders until then.
  Design of record:
  [HANDOFF-correction-revision-plan.md](HANDOFF-correction-revision-plan.md) §3.1.
- 🧪 **P4 — deterministic verify-acceptance loop (hardware-free complete,
  on-device threshold-tuning pending H1).** After a correction is applied
  and re-measured, deterministic code — never a model, never the user's
  optimism — decides **accept / surface / auto-revert**. The pure
  `AcceptanceEvaluator`
  ([`jasper/correction/acceptance.py`](../jasper/correction/acceptance.py))
  takes the pre-correction curve (position-1 matched basis, spatial-average
  fallback), the re-measured verify curve, and the shared target, and returns
  a typed verdict `{accept | surface | revert_pending_confirm | revert}` +
  per-band table + reasons. It NEVER writes CamillaDSP; the session acts. The
  statistical rules (revision plan §4 P4, born of a red-team that killed the
  naive per-band rule — the "before" is an N-position average, the verify is
  one position, and 4–6 dB seat-to-seat std is normal per `spatial.py`):
  (1) aggregate to **1/3-octave smoothed bands** before any per-band verdict
  (never raw per-bin); (2) **"clear regression" = a band worse beyond the
  repeatability floor AND overall band-RMS-error worse beyond a noise margin**
  — neither alone; (3) **matched basis** — the verify is captured at position 1
  and compared against the stored position-1 curve (pinned by a
  divergent-seats multi-position test where the two bases give different
  verdicts); (4) **one confirmatory re-measure before auto-revert, strictly
  adjacent** — a first clear regression is `revert_pending_confirm` (the flow
  offers "Measure again to confirm" via `/verify`, and while it is pending the
  envelope never offers `/start` as the primary action); only the verify
  IMMEDIATELY after it, concordantly regressed, escalates to `revert` — a
  clean confirmatory verify clears the flag, so regress→clean→regress pends
  again rather than reverting off a stale flag. `auto_revert()` rolls back
  through the **existing** `reset()` reversal (Layer B removed, speaker DSP +
  preference preserved) and records the completed outcome as fact
  (`session.auto_revert_outcome = ok|failed`): a successful revert lands in
  IDLE where the envelope announces "Reverted — the room says no…" until the
  next `/start`; a failed one keeps the result screen honest — "STILL
  APPLIED" with the manual Reset pointer — never claiming a removal that
  didn't happen. The verdict lands on `session.acceptance`, in the envelope
  (schema v2 `verdict` block + outcome-driven `verdict_text` — the only field
  the shipped client renders), in `result.json`/`info.json`/status (bundle
  schema v5, alongside the `position1` matched-basis curve and
  `auto_revert_outcome`), and in `event=correction_acceptance.{verdict,
  auto_revert,auto_revert_outcome}` logs. Thresholds are env-tunable
  `JASPER_ACCEPT_*` knobs seeded from `spatial.py`'s 4–6 dB std constants —
  **conservative placeholders retuned at H1** from real on-device
  repeatability (revision plan §5 H1); a floor-level spectrally-smooth-noise
  sweep pins that no single sweep can terminal-revert at those seeded floors
  and documents the measured pend rates as the H1 target. All synthetic (real
  evaluator against ground-truth curves + session/handler-level integration
  through the real verify path). Design of record:
  [HANDOFF-correction-revision-plan.md](HANDOFF-correction-revision-plan.md) §4 P4.
- 🧪 **P7 — active-crossover measurement flow (hardware-free complete,
  on-device pending H2).** The Layer-A commissioning *flow* now rides
  the shared substrate. After protected speaker setup, one server envelope
  exposes the real product choice: keep/edit the applied manual crossover and
  continue to Room, or enter automatic driver level matching (mic/calibration +
  one near-field level per driver → each driver sweep → explicit trim replacement)
  and
  then continue to Room. The browser is a thin
  renderer/dispatcher: it has no local recorder and reads one envelope snapshot,
  so relay state and the one next action cannot disagree. `POST
  /crossover/relay-capture` carries driver/summed sweeps over the **same**
  phone-mic relay transport + `record_*_capture` analysis seam the room/sync
  flows use. The consume path
  reads the play payload's REAL shape (top-level `status` + nested
  `playback.audio_emitted`, top-level `test_level_dbfs`/`sweep_meta`/
  `playback_id`), and measurement mutual-exclusion is
  server-computed twice: refused at POST while room/balance/sync is
  active, re-checked when the phone arms (never client-supplied). The
  obsolete raw-WAV `POST /crossover/driver-capture` route was deleted when
  the repeat controller landed: it had no product caller and could provide
  neither the relay's stored ambient prefix nor its server-owned repeat
  sequence. Driver evidence has one production ingress; the direct summed
  diagnostic route remains separate.
  `crossover_sweep` capture spec's stimulus length derives from the
  kernel-side per-driver signal plan (12 s woofer/subwoofer, 8 s midrange,
  4 s tweeter; one sweep definition — the phone copy matches the sweep the Pi plays; the
  deconv reference always regenerated from the played `sweep_meta`, so
  phone is a pure recorder). Driver recordings begin with a 13-second silent
  ambient window and their hard deadline is 45 s; the phone's
  `duration_ms` remains only a backstop because normal completion follows the
  Pi's `sweep_complete` event. The generic builder retains the 30 s floor like `room_sweep`'s
  `hard_timeout_ms` (the normal stop is the Pi's `sweep_complete` relay
  event; the deadline is only the backstop). The same pass fixed the
  pre-existing **sync** relay bugs of that class: `sync_flow.
  relay_run_and_consume` now publishes `sweep_started`/`sweep_complete`
  (after the marker playback truly ends) and `build_sync_marker_spec`
  gained the same 30 s deadline floor — without both, the capture page
  deadline-killed every sync relay capture. `GET /crossover/envelope`
  ([`jasper/active_speaker/crossover_envelope.py`](../jasper/active_speaker/crossover_envelope.py))
  is the pure sequential `{screen, verdict_text, nudges, next_action, progress,
  relay}` composer over crossover status. Passive
  (`full_range_passive`) speakers get `active=False` on
  `/crossover/status` + the envelope (no driver/summed targets) so
  Layer A is skipped and the flow points directly to Room (revision plan §1).
  Relay state is flow-filtered (room/sync links cannot leak into crossover),
  and the gain lease is context-bound to the current protected baseline. The
  L0 emit gate + graph safety + commission ramp Stop-gates remain intact. **H2**
  is the acoustic proof only — the
  phone-mic `getUserMedia`/CSP path + the driver/summed sweep playback
  on real drivers are not exercised hardware-free (same status as the
  room/sync relay). Design of record:
  [HANDOFF-correction-revision-plan.md](HANDOFF-correction-revision-plan.md) §4 P7.
  The first JTS3 H2 attempt exposed and closed a comparison-validity bug:
  the migrated relay screen had weakened the canonical per-driver placement
  from the same ~2–5 cm near-field geometry to merely “close to the speaker
  baffle,” so incomparable woofer/tweeter captures could replace a manual trim.
  Driver captures now use one fixed 3 cm capsule-to-radiating-surface geometry,
  name the exact driver/horn target, and require a role-specific acknowledgement
  before the Pi may play. The acknowledgement is capture-protocol v2 data,
  bound to the relay session and verified before `on_armed`; a normalized
  server-owned proof is persisted with the record. A successful crossover level
  sequence starts one durable comparison set binding the protected profile,
  setup digest, microphone identity hash, calibration, and one passband-safe
  digital-volume lock per driver. Every driver capture must belong to that same
  set. Optional summed diagnostics may reference it but do not gate the first
  product apply. Starting a new sequence invalidates the prior set, and
  legacy/mixed-set records remain
  historical but cannot produce or apply a new automatic crossover. Manual
  crossover preservation/application is unchanged; an applied automatic profile
  exposes **Level-match drivers again**, which starts a fresh set and
  keeps the current safe profile live until the updated profile is explicitly
  applied. The level-check screen renders the Pi-owned geometry steps after
  microphone setup. Crossover level matching walks every active driver in order,
  binds each tone frequency to that driver's protected applied-preset passband,
  and names the canonical 3 cm position for that exact radiator instead of
  referring to instructions on another page. The driver ESS playback graph and
  its analysis preset are both frozen from that same immutable applied snapshot;
  mutable `/sound/` draft edits cannot change an in-flight measurement. The
  explicit crossover apply route is also refused while the shared relay slot is
  starting or awaiting the phone, so graph apply cannot race measurement
  playback or its rollback. The
  public page release that
  implements this contract, including UMIK-2 model/mode preselection (the
  serial is still entered and validated once; there is no automatic
  calibration-file match), is
  `capture_page_build=20260712.2`, supporting
  protocols 1 and 2; publish it
  before deploying a Pi that emits v2 specs.
  **Pending release gate (verified 2026-07-12):** the public page still reports
  `capture_page_build=20260711.4`. Publish the repo's `20260712.2` page only
  after this code merges, and do so before the matching Pi deploy.
  Relay room level setup temporarily suspends the local browser's 120-second
  upload watchdog while the human completes mic permission, calibration,
  placement, and auto-level, then restores a fresh bound for the actual room
  capture. Every room-relay completion page (level, position sweep, and verify)
  returns directly to `/correction/room/`; `/correction/` remains only the
  legacy local-microphone preflight. Room verification's armed callback is
  state-aware (a required `state` parameter), so the relay passes the frozen
  setup binding through the zero-argument compatibility seam before playback.
- ✅ **Phone-mic capture relay path (fresh-install default,
  JTS3-verified).** As of 2026-07-02 fresh installs default to an
  alternative capture transport that moves the room capture setup/recording page
  to a trusted cloud origin
  (`capture.jasper.tech`) and pulls the WAV back through a stateless
  E2E-encrypted relay, feeding **this same** MeasurementSession analysis. It is
  seeded by default as `JASPER_CAPTURE_RELAY_BASE=https://relay.jasper.tech` /
  `JASPER_CAPTURE_ORIGIN=capture.jasper.tech`; setting the relay base to
  `disabled` (or `off` / `0` / `none`) keeps the on-Pi same-origin flow below
  byte-identical and makes `POST /relay/capture` return a clear "not configured"
  error. Blank legacy relay values are repaired to the public defaults during
  install/update so stale Pis do not silently fall back to local HTTPS.
  The relay exists because phone browsers only expose `getUserMedia` on a secure
  context with a publicly trusted HTTPS certificate: a LAN Pi self-signed cert is
  fragile on iOS and blocked for microphone access by Android Chrome.
  The transport + the `correction_setup.py` adapter
  (`jasper/capture_relay/correction_adapter.py`) are hardware-free tested; the
  room relay now guides mic choice, calibration choice, and position count on the
  phone during the level check. The Pi validates the full setup once; later
  position links carry only its bounded digest and open directly on one Start
  action, so calibration contents and the position count are not re-entered or
  re-posted per sweep. Successful capture-only positions slide a 20-minute idle
  expiry, while a fixed two-hour absolute privacy lifetime tied to the setup
  binding never moves. The page publishes its build and supported wire versions
  at `https://capture.jasper.tech/version.json`; every phone event carries that
  identity and the Pi refuses/logs an incompatible page before a setup or armed
  callback can play a tone. Publish the compatible Pages build and verify that
  URL before deploying a Pi protocol bump (the exact release procedure lives in
  `capture-page/README.md`). It performs the ambient-baselined automatic level check
  before any sweep, captures passive room noise, and records until the
  Pi publishes `sweep_complete` through the relay. The Pi also includes a
  local `return_url` in each relay spec, so once the phone upload finishes the
  capture page shows a **Back to speaker** CTA to the originating local
  management page (for example `http://jts5.local/correction/room/`). On
  2026-07-11, JTS3 completed the full UMIK-2 flow on the production relay:
  guided setup without a calibration file, automatic level lock, protected room
  sweep, four-filter apply, and post-apply verification to terminal `verified`.
  The live `getUserMedia`/CSP/Wake-Lock path and adapter playback therefore have
  on-device evidence; other speaker/browser combinations remain ordinary
  hardware coverage, not an architectural blocker. Single source of truth for the design,
  deploy, and remaining work:
  [phone-mic-relay-plan.md](phone-mic-relay-plan.md). Do not restate it here.
- ✅ **HTTPS measurement hub shell.** As of 2026-06-23,
  `/correction/` is the secure measurement hub for `room`, `crossover`,
  and `bass`. `/correction/` and `/correction/room/` render the existing
  room-correction workflow and keep
  `deploy/assets/correction/js/main.js` intact. `/correction/crossover/`
  is a correction-native active-crossover microphone surface: correction
  web modules own HTTPS/browser routing, while
  `jasper.active_speaker.web_commissioning` owns safe driver and optional summed
  playback orchestration and `jasper.active_speaker.web_measurement`
  owns bounded browser WAV evidence plus acoustic-analysis recording.
  This page is also the ownership boundary between manual and automatic
  crossover tuning. A safe applied manual crossover is a valid Layer-A
  prerequisite for Room and remains editable under `/sound/`. Automatic tuning
  is optional: mic/calibration, driver-specific automatic levels, and driver
  captures run sequentially, then an explicit apply replaces manual attenuation
  trims while preserving crossover frequency and slope. Legacy
  applied profiles offer **Keep current manual crossover** or **Tune
  automatically** instead of forcing the user into a microphone flow. Starting
  automatic level matching transparently runs the same exact-preservation apply
  when `manual_preservation.ready` is true, so a legacy speaker reaches the mic
  relay in one intent; unsafe preservation refuses before relay registration.
  `/correction/bass/` is a READ-ONLY bass-management display (P5): it
  renders the live bass-management state (crossover corner, its owner —
  active-speaker local vs wireless sub, sub-present, mains-HP status) from
  `jasper.bass_management.resolve_bass_management` via `GET /bass/status`,
  and points to the Room tab where the bass-region measurement lives. It
  owns no corner control (the speaker layer owns the corner). The plain-HTTP
  preflight accepts
  `?next=/correction/...` so HTTP-only setup flows can link directly to a
  secure subflow after showing the certificate warning; its Proceed
  button has a no-JS fallback through `/correction/proceed[/subflow]`.
  When JavaScript is available, the page validates the `next` path against
  the correction subflow allowlist and goes directly to the final
  `https://<current-host>/correction/...` URL with a per-page-load `jts_cb`
  cache-bust token. The nginx fallback redirects are temporary and carry
  strong no-cache headers so non-default hostnames survive the scheme switch
  without teaching mobile browsers a permanent rule or reusing a stale
  handoff URL.
- ✅ **Bonded-follower delegation.** As of 2026-06-15, active bonded
  followers do not run local room-correction, balance, or sync
  measurement flows. `GET /correction/`, `/correction/balance`, and
  `/correction/sync` render a leader-owned notice, and all mutating
  correction/balance/sync POST routes return HTTP 409 while the speaker
  is a follower. These are content-calibration surfaces for the paired
  playback image; run them from the leader. Driver-DSP/crossover work
  remains local to the box that owns the DAC path and is documented in
  the sound/active-speaker handoff.
- ✅ **Phase 0 — TLS + skeleton wizard.** PR #40 merged 2026-05-09.
  Self-signed cert + iOS trust dance documented; mic-permission
  page with `getSettings()` constraint verify lands at
  `https://jts.local/correction/`.
- ✅ **Phase 0.1 — HTTP preflight before HTTPS interstitial.**
  Implemented 2026-05-28; hostname-safe proceed redirect added
  2026-06-24; JS-enabled direct HTTPS handoff added 2026-06-26.
  `http://jts.local/correction/` now serves a static preflight page that
  explains the browser's self-signed-cert warning. Its default OK button
  targets `/correction/proceed` with a build-token fallback query string;
  JavaScript replaces that with a fresh `jts_cb` token on each preflight
  page load and a direct HTTPS URL for the current host, with optional
  `/room`, `/crossover`, or `/bass` suffixes when a safe
  `?next=/correction/...` target is present. Nginx keeps temporary,
  strongly non-cacheable `/correction/proceed` redirects for no-JS fallback,
  so `jts3.local` and other configured hostnames do not depend on hard-coded
  `jts.local` or sticky 308 state in Safari. The landing
  page links to the HTTP preflight, and the HTTPS correction page's Home
  link points back to
  `http://jts.local/` so relative navigation does not inherit the
  HTTPS origin and hit the 443 catch-all. The 443 catch-all now
  temporarily redirects non-correction paths back to HTTP instead of returning
  404, so accidental `https://jts.local/voice/` style navigation recovers.
- ✅ **Phase 1 — single-position end-to-end PEQ.** PR #41 merged
  2026-05-09. Sweep generation (Novak 2015) → playback through
  CamillaDSP → AudioWorklet capture → deconvolution → 1/48-octave
  smoothing → greedy peak-fit PEQ design (≤5 cuts, 20-350 Hz) →
  YAML emit (preserving master_gain mixer) → CamillaDSP hot-swap.
  Coordinator pauses renderers + voice loop via UDS; voice_daemon
  has `MEASURE_PAUSE` / `MEASURE_RESUME` with 2-min auto-clear
  safety timer. 37 new tests, all passing on synthetic data.
- ✅ **Phase 2 — multi-position sweeps + verify pass.** PR #42 merged
  2026-05-09. Power-mean spatial averaging across 5 positions;
  post-Apply re-measurement with deviation metrics; target-curve
  choice (flat / warm / bright). 12 new tests, all passing.
- ✅ **Phase 2.2 — survive `jasper-camilla` restarts.** Merged
  2026-05-11 (#62) + hotfix 2026-05-11; rechecked for the outputd
  mainline topology 2026-05-28. The systemd unit passes a CamillaDSP
  `--statefile` and intentionally *omits the positional CONFIGFILE
  arg*. The initial #62 version included the positional v1.yml as a
  "fallback" — which made the whole feature a no-op because
  CamillaDSP overwrites the statefile with the positional path on
  every start when both are given. The hotfix removes the positional.
  In the outputd topology, Camilla reads
  `/var/lib/camilladsp/outputd-statefile.yml` seeded to
  `/etc/camilladsp/outputd-cutover.yml`; the normal
  `/var/lib/camilladsp/statefile.yml` is preserved for pre-outputd rollback.
  Subsequent `set_config_file_path()` calls from the wizard update the
  active statefile in place; future restarts read it back. Recovery
  from a bad correction without hand-editing the statefile: add
  `--no_config` to the ExecStart args, restart, fix or re-measure,
  remove the flag.
- ✅ **Phase 2.1 — current-correction visibility + per-session debug
  bundles.** Merged 2026-05-11.
  - `GET /status` now includes a `current_correction` descriptor
    (`{path, session_id, applied_at_epoch, peq_count}`) parsed from
    `CamillaController.get_config_file_path()`. The page banner at
    the top of `/correction/` reads this on load so the user knows
    what's loaded before measuring.
  - `POST /start` now loads a generated measurement baseline BEFORE the
    first sweep. The baseline is derived from the currently loaded graph via
    `jasper.sound.graph_carrier`: room correction (Layer B) is cleared,
    preference EQ (Layer C) is bypassed, and topology-owned speaker DSP
    (crossovers, driver EQ, delay, gain, limiters, and protected outputs) is
    preserved. Ordinary full-range stereo still measures the raw room; saved
    active/protected topology measures through its protected speaker baseline
    instead of swapping to flat stereo. The generated graph is checked by
    `jasper.correction.runtime_safety.assert_correction_graph_safe()` before
    sweep playback. The prior correction descriptor is preserved in the
    session's `current_correction_at_start` for the bundle.
  - Each session writes a self-contained debug bundle at
    `/var/lib/jasper/correction/sessions/<session_id>/` —
    `info.json` (params + state), `result.json` (chart curves +
    verify), `captures/p<N>.wav` (per-position WAVs),
    `verify.wav` (post-Apply re-measurement, if run), and
    `applied.yml` (copy of the CamillaDSP config that was applied).
    Default ON; opt-out via `JASPER_CORRECTION_SAVE_BUNDLES=0`.
  - `GET /sessions` (debug endpoint) lists the 20 most-recent
    bundles for `curl`-based debugging / future history UI.
  - Covered by the correction regression suite.
- ✅ **Phase 2.3 — calibrated measurement mic substrate.**
  Implemented 2026-05-25. Adds an input-device picker, first-class mic
  calibration registry, server-side serial lookup for Dayton Audio
  iMM-6 / iMM-6C / UMM-6 and miniDSP UMIK-1 / UMIK-2, manual
  calibration-file upload fallback for unsupported mics, and
  per-session bundle metadata (`input_device`, `mic_calibration`,
  `bundle_schema_version`). Calibration files normalize to one
  internal additive `correction_db` curve and are applied in
  `MeasurementSession._smooth_capture` before target normalization
  and PEQ design. Storage lives under
  `/var/lib/jasper/correction/calibration_mics/`.
- ✅ **Phase 2.4 — observability and quality floor.**
  Implemented 2026-05-25. Adds shared bundle helpers
  (`jasper.correction.bundles`), capture-quality assessment before
  deconvolution (`jasper.audio_measurement.quality`), explicit browser /
  clipping / low-level / uncalibrated-mic warnings in the UI and
  bundle, atomic `info.json` writes on failed analysis paths, and
  `jasper-doctor` correction checks for the socket, state dirs,
  current CamillaDSP config path, and newest bundle.
- ✅ **Phase 2.5 — correction strategy + design audit substrate.**
  Implemented 2026-05-26. Adds `jasper.correction.strategy`, a
  deterministic policy layer around PEQ generation. The room
  correction wizard now exposes target-profile metadata and bounded
  correction strategies (`safe`, `balanced`, `assertive`), persists
  `strategy_choice`, `correction_strategy`, `target_profile`, and
  `design_report` into `info.json` / `result.json`, and renders a
  compact design audit explaining the selected band, the *predicted*
  RMS change (a model estimate under `design_report.predicted` — never
  labelled "improvement", since it is not re-measured), warnings, and
  per-filter rationale. The honest *measured* before/after only appears
  once a verify sweep lands (`verify_before_after`, computed on the Pi
  over the same 50–350 Hz band as `verify_metrics`; the browser fills
  the before→after gap green/amber and headlines the measured delta).
  The read-only
  calibration-agent intake tool surfaces the same report so a future
  LLM can explain and recommend bounded strategy changes without
  reverse-engineering the filters.
- ✅ **P5 — room correction reads the bass-management corner.** The
  designer READS the active crossover corner (via
  `jasper.bass_management.active_crossover_corner_hz`; it never picks it —
  the speaker layer owns the corner) and, in boost-capable strategies,
  excludes boosts within ±1/3 octave of Fc (cuts stay allowed): an LR4 sum
  is flat there by design, so a dip AT the corner is the crossover, not a
  room mode, and boosting it fights the crossover. `design_report` gains a
  `crossover_region` annotation (corner, no-boost band, excluded boosts) and
  the envelope's REVIEW `verdict_text` + a `crossover_region_dip_not_boosted`
  nudge carry the crossover-vs-room-mode distinction (envelope schema v3).
- ✅ **Phase 2.6 — first-pass measurement confidence report.**
  Implemented 2026-05-26. Adds `jasper.correction.confidence`, a
  deterministic confidence summary derived from existing facts:
  completed position count, calibrated-mic presence, input-device
  metadata, capture-quality issues, per-position variance in the
  correction band, and strategy gates for `safe` / `balanced` /
  `assertive`. The report is persisted into `info.json` /
  `result.json`, embedded in the design audit, returned from the
  upload/status path, and exposed through the read-only
  calibration-agent intake tools. This is deliberately a v1
  instrument panel; SNR, repeatability, and research-tuned thresholds
  remain future refinements.
- ✅ **Phase 2.7 — confidence UI + per-position analysis artifact.**
  Implemented 2026-05-26. Adds a simple `/correction/` confidence
  card showing score, findings, position-variance summary, and
  allowed/blocked correction strategies. Each completed design now
  writes `position_analysis.json` with per-position magnitude curves,
  spatial average, and per-frequency variance arrays so future FIR and
  LLM tooling can inspect seat-to-seat behavior without re-running
  deconvolution.
- ✅ **Phase 2.8 — multi-position confidence detail.** Implemented
  2026-05-27. Adds shared spatial-spread helpers, per-band
  multi-position summaries (`sub_bass`, `bass`, `upper_bass`,
  `transition`, and the active correction band), deep-null and
  high-variance feature flags, and per-filter spatial-confidence
  annotations in `design_report`. The full report is persisted in
  `position_analysis.json` and summarized in `result.json`, giving
  deterministic code and future LLM tools the same explanation-ready
  facts about which features were accepted, avoided, or too unstable
  for aggressive correction.
- ✅ **Phase 2.9 — browser audio-path confidence substrate.**
  Implemented 2026-05-27. Adds `jasper.correction.browser_audio`, a
  deterministic preflight report built from `getUserMedia()`
  metadata: sample rate, mono channel count, processing flags, granted
  input-device identity, and calibrated-mic presence. The report is
  shown inline in `/correction/`, returned from start/status/upload
  endpoints, server-enforced at `/start`, persisted in `info.json` /
  `result.json`, and folded into the confidence model so browser
  processing or sample-rate failures block correction before a user
  wastes time measuring. This is still metadata confidence, not an
  acoustic loopback proof; real phone/Pi capture smoke testing remains
  outstanding.
- ✅ **Phase 2.10 — correction visualization + confidence UX.**
  Implemented 2026-05-28. `/correction/` results now expose the
  measurement facts that already drive the deterministic engine:
  display smoothing controls, correction-band shading, spatial-spread
  overlay, filter-effect overlay, measured/target/predicted/verify
  curves, PEQ markers, rejected-feature markers, band-confidence
  summaries, confidence/strategy gates, runtime-integrity status, and a
  deterministic recommended next action. The implementation stays
  dependency-free in the socket-activated web process: one canvas and
  small JSON summaries rather than a plotting framework.
- ✅ **Phase 2.11 — durable evidence bundle contract + runtime integrity.**
  Implemented 2026-05-28. Every new measurement session is a
  self-describing, replayable evidence packet rather than a set of
  files known by convention. New bundles use bundle schema v3 and
  include `artifact_manifest.json` with checksums, kinds, schemas,
  provenance, dependencies, sensitivity, and recomputability flags for
  raw captures and derived artifacts. They also write
  `runtime_integrity.json`: system load/memory snapshots,
  capture sample-count sanity, fan-in xrun deltas, and CamillaDSP
  runtime counters around each sweep/verify pass. Runtime warnings and
  failures feed the same confidence report and bundle validator.
  Treat `captures/p<N>.wav` and `verify.wav` as canonical private raw
  evidence; every curve, confidence report, PEQ, and future FIR/agent
  judgment should be reproducible from those recordings plus sweep
  metadata, calibration, algorithm settings, and runtime-integrity
  evidence. Keep the design file-based and Pi-cheap: no database, no
  continuous telemetry daemon, no unbounded retention.
- ✅ **Phase 2.12 — bundle inspection / REW export substrate.**
  Implemented 2026-05-28. Adds `jasper-correction-bundle`, a small
  operator CLI over `jasper.correction.bundle_tools`: `inspect`
  validates manifest checksums, summarizes confidence/runtime evidence,
  and can replay raw captures into derived curves; `export` writes
  REW-friendly `.frd` / `.txt` frequency-response files plus float32
  impulse-response WAVs recomputed from raw captures and `sweep_meta`.
  This is intentionally not a new correction path. It is a forensic and
  interoperability bridge around the existing bundle contract.
- ✅ **Phase 2.13 — agent-readiness evidence packet + acoustic trust.**
  Implemented 2026-05-28. Adds `acoustic_quality.json`, a compact
  derived trust report built from capture quality, pre-sweep room-noise
  WAVs, banded dBFS SNR estimates, direct-arrival/pre-arrival evidence,
  and optional same-seat repeatability. The browser flow now records
  `noise/p<N>_pre.wav` before each sweep and can repeat the main seat
  into `repeat_captures/p0_r1.wav` without counting that repeat as
  another listening position. Low SNR and weak repeatability feed the
  confidence model, assertive-strategy gate, and future-FIR readiness
  gate. `jasper.correction.evidence` builds a deterministic,
  read-only evidence packet for human and future LLM review. The
  calibration agent CLI now renders an audio-engineer-style review:
  what happened, what looks trustworthy/suspicious, what JTS refused
  to correct, what to do next, and what evidence is missing. `/status`
  also reports whether the active CamillaDSP config is JTS-managed,
  preference-only, room-corrected, or a custom advanced config that
  JTS cannot safely preserve.
- ✅ **Phase 2.13b — pair time-of-arrival substrate.**
  Implemented 2026-06-13. Stereo-pair acoustic sync belongs under the
  correction/calibration umbrella, not as a local endpoint trick:
  Snapcast remains the distributed clock/transport sync engine, while
  measured listening-seat arrival differences are rendered by the
  leader as static CamillaDSP `Delay` filters in the room chain. The
  shared delay emitter is gainless; non-zero per-channel delays require
  explicit L/R room chains, so a solo config stays byte-identical and a
  right-channel delay can never silently apply to both channels. The
  `/sync` flow shares the correction measurement window with
  `/correction` and `/balance`, generates deterministic L/R markers,
  accepts a browser-recorded WAV, estimates arrival delta by
  correlation, and recommends positive-only channel delay. Source
  synthesis lives in
  [research/balance-sync-calibration.md](research/balance-sync-calibration.md).
  Browser phone-mic plumbing that is safe to share now lives in
  `/assets/shared/js/measurement-audio.js`; `/sync/` and `/balance/`
  import it for mono capture, AudioWorklet setup, no-monitoring graph
  cleanup, RMS conversion, and WAV encoding. `/correction/` intentionally
  remains on its existing capture path until an on-device browser pass can
  re-verify calibrated mic selection, `getSettings()` validation, capture
  quality evidence, and bundle upload behavior together.
- ✅ **Phase 2.14 — read-only measurement report surface + schema
  version docs.** Implemented 2026-05-28. `/correction/` now includes
  a small on-demand history panel backed by `GET /session-report?id=...`.
  It renders the deterministic evidence packet for one bundle: what
  happened, what looks trustworthy, what looks suspicious or missing,
  what JTS refused to correct, and the artifact versions observed.
  The endpoint returns metadata and derived evidence only; raw WAVs
  remain private bundle artifacts for CLI/operator workflows. This
  phase also pins the compatibility expectations for `info.json`,
  `result.json`, `runtime_integrity.json`, `acoustic_quality.json`,
  `artifact_manifest.json`, and evidence packets.
- ✅ **Phase 2.15 — replay-grade artifacts + FIR Stage 0 readiness.**
  Implemented 2026-05-28. Successful measurements now write compact,
  manifest-tracked derived artifacts under `analysis/`: per-capture
  impulse-response WAVs plus response JSON containing raw FFT
  magnitude, 1/48-octave smoothing, the final analysis curve,
  calibration/normalization metadata, direct-arrival evidence, and
  deconvolution settings. These artifacts are recomputable from raw
  WAVs and make future FIR/agent review faster without making the
  browser expose raw recordings. `jasper.correction.fir_runtime` adds
  a no-apply FIR substrate: inspect FIR WAVs for sample-rate, taps,
  latency, memory, max gain, and required headroom; optionally stage
  safe imported coefficients into a bundle as evidence. The evidence
  packet is now schema v2 with explicit `capability_permissions` and
  `missing_evidence` so humans, the CLI, and future LLM tools consume
  the same deterministic permission surface.
- ✅ **Phase 2.16 — minimal bundle history + privacy/delete UX.**
  Implemented 2026-05-28. The `/correction/` read-only measurement
  history now lists each recent bundle with size, state, position
  count, result presence, and an explicit private-raw-recordings badge
  when the bundle contains `captures/`, `noise/`,
  `repeat_captures/`, or `verify.wav` audio. Users can delete an old
  bundle from the same panel; the server resolves the session ID
  through the browser-safe bundle resolver, refuses deletion of an
  active in-progress or ready-to-apply measurement session, removes
  the filesystem bundle, and logs a structured
  `event=correction_session_bundle_deleted` entry. This is
  intentionally not an archive product: no database, no pinning, no
  automatic pruning, and no browser exposure of raw audio.
  `jasper-doctor` also summarizes correction-bundle observability:
  latest bundle, total parseable bundle storage, private raw-audio
  artifact count/bytes, latest evidence completeness, and an
  informational note when old raw recordings are still present.
- ✅ **Phase 2.17 — LLM-ready advisor context packet.**
  Implemented 2026-05-29. `jasper.calibration_agent.advisor_context`
  builds a versioned, redacted `llm_ready_advisor_context` from the
  deterministic evidence packet plus target/strategy summaries,
  acoustic/runtime/repeatability/spatial confidence, rejected or
  caution features, bass residuals, corpus snippets, and the current
  sound-profile DSP shape. It deliberately excludes raw audio,
  absolute paths, secrets, raw mic serials, browser labels, and
  user-entered profile names. The packet is **read-only-first, not
  read-only-forever**: it carries permissions for explain, recommend
  remeasurement, propose bounded preference-EQ auditions, and request
  user-approved preference-profile commits when JTS confidence gates
  allow them. It always prohibits raw-audio access, unconstrained
  CamillaDSP YAML, direct filter apply, FIR tap generation, safety-gate
  overrides, volume control, and silent room/preference layer merging.
- ✅ **Phase 2.18 — advisor prompt + bounded action contract.**
  Implemented 2026-05-29. `jasper.calibration_agent.prompt` emits a
  provider-neutral `jts_advisor_prompt_package` containing the system
  instructions, response contract, and redacted advisor context; it
  makes no model call. `jasper.calibration_agent.response` validates a
  future model's `jts_advisor_response` into a `validated_action_plan`.
  The first action set is deliberately narrow: explain evidence,
  recommend remeasurement, propose an ephemeral preference-EQ audition
  through the existing `/sound/` substrate, or request a persistent
  preference-profile save after explicit user confirmation. The
  validator marks every action with `execution_ready` so valid
  evidence explanations and ephemeral auditions cannot be confused
  with a persistent profile write that is still awaiting user
  confirmation. Validated profile payloads carry DSP shape only
  (`enabled`, `curve_id`, simple EQ, PEQ bands); profile identity and
  timestamps are owned by JTS. It rejects raw audio, FIR
  coefficients/taps, CamillaDSP YAML, volume authority, shell/command
  authority, unknown actions, and out-of-bounds preference EQ.
- ✅ **Phase 2.19 — human-in-the-loop advisor action runner.**
  Implemented 2026-05-29. `jasper.calibration_agent.actions` consumes
  a validated advisor action plan and runs only known, execution-ready
  actions. Explain and remeasure actions become presentation payloads.
  Preference auditions and user-approved profile commits require
  explicit executor callables supplied by the future web/voice surface;
  without those executors they remain pending human-listening actions
  and produce no DSP side effects. Executor failures are returned as
  structured run issues. The run envelope states the human-in-the-loop
  principle: preference tuning is subjective, JTS can propose safe
  options, and the listener decides what sounds better.
- ✅ **Phase 2.20 — first advisor model-call adapter + sound audition
  executor.** Implemented 2026-05-31. `jasper.calibration_agent.model_client`
  adds an opt-in, stdlib OpenAI Responses adapter behind
  `jasper-calibration-agent --call-advisor`. It sends only the redacted
  advisor prompt package, requests structured JSON, sets `store: false`,
  logs only provider/model/status/elapsed-time metadata, and still treats local
  `response.validate_advisor_response` as the safety gate. No model call
  happens unless the operator explicitly passes `--call-advisor`, and
  the model id remains explicit (`--advisor-model` or
  `JASPER_CALIBRATION_ADVISOR_MODEL`). `jasper.calibration_agent.sound_actions`
  wires validated `propose_preference_eq_audition` actions into the
  existing `/sound/` audition backend only when `--audition-sound` is
  passed. That path emits/loads `sound_audition.yml`, preserves room
  PEQs, never persists a sound profile, never controls volume, and
  returns a debug-safe action result with config basename rather than
  raw paths.
- ✅ **Phase 3 — power-user pass-through.** Already shipped as part
  of v1 — `camillagui.service` runs at port 5005, linked from the
  landing page. No additional work required for the originally
  scoped Phase 3.
- ⏳ **Phase 4 — REW interop.** Partially started. Generic export
  landed in Phase 2.12; import/upload of external filter designs and a
  documented REW round-trip remain outstanding.
- ⏳ **Phase 5 — FIR filter ladder.** Stage 0 substrate started:
  inspect/stage imported FIR coefficients and report runtime readiness,
  but do not generate or apply FIR filters yet.
- ✅ **First hardware end-to-end pass + hardening (2026-06-04).** The
  initial real-device run (iPhone Safari + Dayton iMM-6C) surfaced four
  bugs, now fixed: (a) the Dayton serial lookup never worked — the
  calibration filename is in a query param, not the URL path
  (`_extract_links`); (b) capture silently used the iPhone built-in mic
  instead of the USB mic; (c) a session stranded in `awaiting_capture`
  wedged forever; (d) the auto-level "maxed out" copy hardcoded a wrong
  ceiling. Two new operator-visible invariants landed:
  - **Stranded-capture watchdog.** A session parked in any state that waits
    on an automatic browser upload — `needs_noise_capture` (pre-sweep room
    noise) and the three `awaiting_*_capture` states (the post-sweep
    recording) — is abandoned to `FAILED` after `AWAITING_CAPTURE_TIMEOUT_SEC`
    (120 s, `_CAPTURE_TIMEOUT_STATES` in `jasper/correction/session.py`), so
    `/start` is never permanently blocked. The user-paced
    `needs_next_position` / `needs_repeat_capture` states are deliberately NOT
    guarded (the user may take minutes to reposition). A **Cancel measurement**
    button (shown in those waiting/needs states, including
    `needs_noise_capture`) and `POST /reset` recover it manually. No
    measurement window is open during `needs_noise_capture`, and the window
    closes before `awaiting_capture`, so a wedge never leaves the speaker
    muted. Logs `event=correction_capture_timeout`.
  - **Calibration↔device mismatch reject.** `POST /start` returns 400 when
    a vendor measurement-mic calibration (Dayton / miniDSP — derived from
    `calibration.SUPPORTED_MODELS`) is loaded but the captured input device
    looks like the phone built-in mic (`_calibration_device_mismatch`).
    Defended in depth: a browser guard plus this server backstop a
    stale/bypassed client can't evade. Logs
    `event=correction_start_rejected reason=calibration_device_mismatch`.
    The device picker now forces an explicit `deviceId` and re-enumerates
    on `devicechange` so the USB mic appears reliably on iOS.
  - **Capture upload limit.** A capture WAV is ~1-2 MB; the nginx
    `/correction/` location sets `client_max_body_size 32m` (matching the
    backend `MAX_WAV_BODY_BYTES`) so the upload reaches the app and is capped
    with a clean error instead of a raw nginx 413. Guarded by a static test in
    `tests/test_correction_systemd_unit.py`.
  - **Mic-picker UX.** The page auto-detects microphones on landing
    ("Refresh microphones" re-detects), infers the calibration model from the
    device label (`iMM-6`/`UMM-6`/`UMIK`), and remembers the serial of a
    successful fetch in the browser's `localStorage` (raw serials stay off the
    Pi) — auto-filling and auto-fetching it next time so a repeat measurement
    needs no re-typing or Fetch tap.
  - **Relay guided-setup preflight.** The public capture page still cannot talk
    directly to `jts.local`; it posts `{setup_validate:true, setup:{...}}` through
    the relay and waits for the Pi to answer with
    `host_event.phase="setup_validated"` before showing Start. Dayton/miniDSP
    serial misses and uploaded calibration parse errors therefore surface on the
    calibration step instead of after the user starts a measurement. The later
    `armed` event still carries setup as the backstop and playback trigger.

  Backend logic is unit-covered; the iPhone device-picker, Cancel button,
  Wake Lock, auto-level copy, and the mic-picker UX still need an on-device
  confirmation pass.

## Task C: `MeasurementSession` decomposition status (2026-06-17)

Task C is the current maintainability campaign for
[`jasper/correction/session.py`](../jasper/correction/session.py).
The goal is not to change room-correction behavior. The goal is to
keep the shipped safety stack intact while moving cohesive pieces out
of the large session orchestrator so future correction, REW, FIR, and
advisor work can land without every change editing one long file.

Why this matters:

- `MeasurementSession` owns a safety-critical state machine: browser
  capture, sweep playback, CamillaDSP apply/reset, bundle writes,
  confidence/evidence generation, and autolevel volume restore.
- The file is too broad, but broad refactors are the risk. This speaker
  is hardware, not a web toy: a wrong reset/apply/volume path can leave
  the room loud, corrected when it should be flat, or wedged while the
  household expects the speaker to recover.
- The decomposition bar is therefore "extract ownership, preserve
  behavior." Each PR should remove one cohesive responsibility,
  keep `MeasurementSession` as the orchestration boundary, and pin the
  behavior with hardware-free tests.

What has shipped:

- ✅ **Auto-level controller extraction.** PR #788 (merged 2026-06-17)
  moved the autolevel ramp controller and listening-volume restore
  mechanics into
  [`jasper/correction/autolevel.py`](../jasper/correction/autolevel.py).
  `MeasurementSession` still exposes the public methods the web handler
  calls, but the ramp state, lock/cancel events, retained `main_volume`
  setter, cap math, and idempotent restore live with the controller.
  Do not rework this slice unless tests or review find a concrete bug.
- ✅ **State guard extraction.** PR #790 (merged 2026-06-17) moved the
  stranded-capture watchdog and reset-busy predicate into
  [`jasper/correction/state_guard.py`](../jasper/correction/state_guard.py).
  `MeasurementSession._set_state()` still owns state mutation, state
  events, logging, and best-effort `info.json` writes. The guard owns
  only timeout task cancellation/arming, `event=correction_capture_timeout`,
  and reset-busy membership. Tests pin `needs_noise_capture`, all
  `awaiting_*_capture` timeout states, timeout cancellation on upload,
  the log event fields, reset-busy rejection, and failed-measurement
  autolevel restore.
- ✅ **Status/snapshot serializer extraction.** The 2026-06-17
  follow-up moved current-config descriptors plus live `/status`,
  `info.json`, and `result.json` payload construction into
  [`jasper/correction/status.py`](../jasper/correction/status.py).
  `MeasurementSession.snapshot()` remains the public wrapper for the web
  handler, and `SessionArtifacts` still owns filesystem writes and
  manifests, but payload shape now has one owner. Tests pin the
  populated snapshot/info/result keys so future decomposition does not
  silently change report or bundle consumers.

Do not disturb these guardrails while decomposing:

- **Autolevel ceiling and restore behavior.** Autolevel may raise
  CamillaDSP `main_volume` above listening level for measurement SNR.
  Apply/reset handlers and failed/verify terminal paths must restore
  the original listening level.
- **Stranded-capture watchdog.** Automatic browser-upload states must
  self-abandon to `FAILED` after `AWAITING_CAPTURE_TIMEOUT_SEC` and log
  `event=correction_capture_timeout`. User-paced states
  (`needs_next_position`, `needs_repeat_capture`) must remain unguarded
  and cancellable.
- **Cuts-only correction.** Room correction remains restrained PEQ by
  default. Do not widen this campaign into boost, FIR generation, or
  phase correction.
- **CamillaDSP output safety.** Preserve `devices.volume_limit=0.0`
  and the outputd-safe baseline assumptions in generated configs.
- **Raw-room-before-measure invariant.** `/start` must clear the current room
  correction and bypass preference EQ before the first sweep so every
  measurement captures the raw room, not correction-on-correction. The
  measurement graph is generated by the graph carrier from the live topology:
  passive/full-range speakers become the ordinary uncorrected stereo graph;
  active/protected speakers keep Layer A speaker DSP and only zero Layer B/C.
  Every generated measurement graph is checked against the saved topology
  before playback.
- **Task B surfaces are live.** Confidence reports, evidence packets,
  replay artifacts, FIR-runtime inspection/staging, and calibration-agent
  advisor surfaces are not dead code. Do not delete or bypass them
  while shrinking `session.py`.
- **PEQ math boundary.** Do not touch `_bell_response_db` or
  `_estimate_q` as part of Task C. The Q-estimation warning was
  previously misattributed; see the project memory
  `project_correction_pr2_fix_bell_not_estimate_q`.

Good next slices:

1. **Capture analysis orchestration.** Higher value, higher risk. The
   repeated "capture arrived -> set ANALYZING -> smooth -> record
   artifacts -> refresh confidence/acoustic evidence -> transition"
   paths for measurement, repeat, and verify could become a typed
   collaborator. Take this only after mapping every artifact write and
   terminal-state restore. Tests need to cover normal measurement,
   repeat capture, verify capture, capture-quality failure, and runtime
   evidence writes.
2. **Camilla apply/reset orchestration.** Useful but audio-safety
   sensitive. If extracted, the collaborator must preserve flat reset,
   generated-config apply, failed apply behavior, and autolevel restore
   exactly. This slice needs the tightest review.

Probably stop if the next slice feels like architecture for its own
sake. The purpose is to make future work safer, not to make
`MeasurementSession` tiny at any cost.

### Pickup prompt for the next Task C session

Use this when starting a fresh agent/session:

```text
You are continuing the JTS room-correction Task C decomposition campaign.

Repo: jaspercurry/JTS. Start by finding the repo, run
`git fetch --prune origin`, and verify current `origin/main`.

Read first:
- AGENTS.md
- docs/HANDOFF-correction.md, especially "Task C: MeasurementSession
  decomposition status"
- docs/HANDOFF-calibration-agent.md
- Memory entries:
  - project_correction_pr2_fix_bell_not_estimate_q
  - project_correction_design_hub
  - feedback_review_workflows_clobber_shared_worktree
  - feedback_always_use_pr_flow

Current Task C status:
- PR #788 merged 2026-06-17: extracted `jasper.correction.autolevel`.
- PR #790 merged 2026-06-17: extracted
  `jasper.correction.state_guard.SessionStateGuard`.
- 2026-06-17 follow-up: extracted `jasper.correction.status` serializers.
- Do not rework those slices unless tests or review find a concrete issue.
- Do not touch `_bell_response_db` or `_estimate_q`.
- Do not treat confidence/evidence/FIR/calibration-agent surfaces as dead code.

Mission:
- Continue decomposing `jasper/correction/session.py` in one small PR.
- Prefer the next lowest-risk slice from the updated handoff.
- Preserve the safety stack verbatim:
  - autolevel ceiling and restore behavior
  - stranded-capture watchdog
  - cuts-only correction
  - CamillaDSP `devices.volume_limit=0.0`
  - `/start` loads a topology-preserving measurement baseline before
    measuring: room/preference layers bypassed, speaker DSP preserved
  - failed/verify measurement restores `main_volume` to listening level

Working rules:
- Short-lived `codex/` branch off current `origin/main`.
- Diagnose by reading existing session/web/tests before patching.
- Keep `MeasurementSession` as the orchestration boundary; extract only one
  cohesive collaborator.
- Use `/Users/jaspercurry/Code/JTS/.venv/bin/pytest` if this worktree lacks
  `.venv`.
- Run targeted pytest, `ruff check .`, `scripts/docs-impact.py`, and
  `scripts/docs-linkcheck.py`.
- Commit before adversarial review; any review agents must be isolated or
  read-only.
- Open a small PR and ask before merging unless explicitly granted.
```

**Current sequencing note (2026-05-28):** after the latest research
intake, the next room-correction priority is still measurement trust
before more filter types. The multi-position confidence layer,
browser-audio metadata substrate, correction visualization surface,
durable runtime-integrity bundle evidence, acoustic-quality evidence,
agent-readiness packet, and bundle inspect/export tooling have landed.
Replay-grade analysis artifacts, explicit evidence permissions, and
FIR runtime inspection/staging have also landed. The advisor harness
now has its first model-call adapter and reversible sound-audition
executor, but generated/applied room correction remains deterministic
and hardware-gated. The next software/hardware boundary is acoustic
browser smoke testing and then threshold tuning for the native
SNR/repeatability evidence; generated or applied FIR should still wait
until the measurement substrate can prove capture quality, runtime
health, spatial stability, and headroom.
The rationale and source links live in
[`docs/calibration-agent/jts-specific/implementation-ladder.md`](calibration-agent/jts-specific/implementation-ladder.md#2026-05-27-sequencing-update).

**Correction / preference composition note:** `/correction/` owns room
measurement and room PEQ design; `/sound/` owns stock sound curves,
user preference EQ, Bypass / Applied / Draft auditioning, and the
combined CamillaDSP config ordering when both layers are present. Both
surfaces re-emit through `jasper.sound.graph_carrier` so full-range,
passive-crossover, and solo active-speaker topologies preserve their loaded
speaker structure while changing only program-domain room/preference layers.
Current operational truth for that composition lives in
[docs/HANDOFF-sound-preferences.md](HANDOFF-sound-preferences.md) and
[docs/HANDOFF-dsp-graph-carrier.md](HANDOFF-dsp-graph-carrier.md).

**Outstanding Phases 0-2.12 hardware verification** (see "Hardware
test checklist" below) — the math is validated on synthetic IRs;
the integration with real CamillaDSP / iPhone Safari / aplay /
voice_daemon UDS is unverified and is the gating step before
declaring v2 shippable.

## Goal

A measurement-and-correction loop that runs from a phone at the
listening position. Start at `http://jts.local/correction/` (or the
speaker's actual hostname, such as `http://jts3.local/correction/`),
read the plain-HTTP warning preflight, then tap through the
hostname-safe `/correction/proceed` redirect to the secure browser-mic
page.
Optionally pick a calibrated USB measurement mic, the speaker plays a
sweep, the phone records it, the Pi designs a PEQ filter set,
hot-reloads CamillaDSP, and the next song plays through the corrected
pipeline. Two audiences served by one tool: a WiiM-Home-style novice
flow ("press a button, hear the difference") and a power-user surface
(calibrated mic files, raw `.frd` exports, CamillaDSP YAML, REW
interop, optional FIR later).

Concrete success criterion for v2 ship: Jasper measures from the
couch with an iPhone, the bass mode at his listening position
audibly tightens, the YouTube demo records itself.

## Hardware constraints — load-bearing

These are the facts the design has to honor. Don't redesign around
them; design **with** them.

| Constraint | Source | What it forces |
|---|---|---|
| Raspberry Pi 5 **1 GB** target | User decision (2026-05-09): "see how far we can get on 1 GB" | PEQ is comfortable. FIR stays 1 GB-aware and research-gated: pause renderers during expensive generation, measure real memory before enabling mixed-phase / FDW paths. |
| **Apple USB-C dongle**, stereo, 48 kHz | [README.md](../README.md) Hardware table | Filters are 2-channel. No multi-driver crossover work in scope. |
| Pure ALSA: **snd-aloop + fan-in + outputd**, no PipeWire | [docs/audio-paths.md](audio-paths.md) | Sweep injection point is `correction_substream`, a dedicated fan-in lane. CamillaDSP captures from `pcm.jasper_capture` (dsnoop on summed `hw:Loopback,1,7`), processes, writes to `outputd_content_playback`, and jasper-outputd owns the DAC. |
| `master_gain` mixer **already exists** as identity | [deploy/camilladsp/outputd-cutover.yml](../deploy/camilladsp/outputd-cutover.yml) | The EQ slot is reserved. We add filters in front of it, leave it alone. |
| CamillaDSP websocket **no auth, 127.0.0.1 only** | [PLAN.md](../PLAN.md) | `pycamilladsp` calls stay loopback. Web UI never proxies CamillaDSP WS to the LAN. |
| Volume coordination is **canonical and persistent** | [docs/HANDOFF-volume.md](HANDOFF-volume.md), [jasper/volume_coordinator.py](../jasper/volume_coordinator.py) | Sweep playback should set its own absolute level (not via VolumeCoordinator), restore previous on exit. |
| `Ducker` is **the only writer** to `main_volume` for voice | [jasper/camilla.py](../jasper/camilla.py) + `Ducker` | Measurement coordinator must coexist; voice session during measurement should be impossible (we pause WakeLoop). |
| Existing settings pages on **plain HTTP port 80** | [deploy/nginx-jasper.conf](../deploy/nginx-jasper.conf) | We add HTTPS as an additive 443 server block. Existing routes stay HTTP. |
| `getUserMedia` **requires HTTPS** (browser policy) | Web spec | Cannot avoid TLS for this one feature. Private CA + iOS trust profile is the path. |
| Existing web wizards are **stdlib `ThreadingHTTPServer`** | [jasper/web/voice_setup.py](../jasper/web/voice_setup.py), [jasper/web/dial_setup.py](../jasper/web/dial_setup.py) | We mirror this — no FastAPI / aiohttp. Browser state uses polling today. |
| Cross-daemon coordination is **UDS commands to voice_daemon** | [jasper/control/server.py](../jasper/control/server.py) + `_voice_socket_command` | We extend with `MEASURE_PAUSE` / `MEASURE_RESUME`, mirror the `/cue/play` shape. |

## Architecture decisions

These are the load-bearing decisions. Each has been considered and
the rejected alternatives are recorded so we don't relitigate.

### Decision 1 — TLS via additive nginx HTTPS, private CA

**Decision:** Add `listen 443 ssl` server block to nginx with a
private-CA-issued cert for `jts.local`, but keep the existing port-80
server as the default navigation surface. `http://jts.local/correction/`
serves a preflight page; only the measurement UI at
`https://jts.local/correction/` runs over TLS. Document the iOS
Settings → General → About → Certificate Trust Settings dance as a
one-time onboarding step in [BRINGUP.md](../BRINGUP.md) Phase Z
(post-install).

**Why not stay HTTP?** `getUserMedia` only works on HTTPS or
localhost. There is no workaround for this in any browser. The
existing GitHub Pages bounce trick worked for Spotify because the
Pi was never the OAuth redirect target — the bounce ran on a
trusted public origin. There is no equivalent trick for live mic
capture; the secure context has to *be* the page running the
JavaScript.

**Why not Tailscale or ngrok?** Both depend on internet + an extra
install on every household device. The private CA is one-time on the
Pi, and the trust profile is one-time per device.

**Why not skip iPhone Safari and use desktop Chrome only?** The
product story is "couch + iPhone." That's the demo. Desktop-only
loses the YouTube hook.

**Concrete shape as shipped in Phase 0 / 0.1:**
- `install.sh` generates and preserves `/var/lib/jasper/ca/ca.{crt,key}`.
- `install.sh` reissues `/etc/nginx/ssl/jts.local.{crt,key}` from
  that CA for the configured `JASPER_HOSTNAME`, its wildcard, the
  historical `jts.local`, and `127.0.0.1`.
- Port 80 serves `http://jts.local/correction/` as a static preflight
  page, `/correction/proceed[/room|/crossover|/bass]` as no-JS
  `302` redirects to `https://$host/...`, and
  `http://jts.local/jts-root-ca.crt` with `application/x-x509-ca-cert`.
  The preflight HTML, fallback proceed redirects, and HTTPS catch-all
  redirects use `no-store, no-cache, max-age=0, must-revalidate` plus
  legacy `Pragma` / `Expires` headers. The Proceed link carries a
  build-token fallback plus a JavaScript-generated `jts_cb` token; JS-enabled
  browsers go directly to the final HTTPS measurement URL for the current
  host, so a phone cannot keep an old hard-coded or wrong-scheme target after
  deploy.
- Port 443 proxies only `/correction/` to `127.0.0.1:8770` and serves
  `/assets/` statically. The measurement UI's canonical look links
  `/assets/app.css` + its ES module by absolute path; without an `/assets/`
  location on 443 those subresources fall through to the catch-all, redirect
  down to HTTP, and browsers block them as mixed content (unstyled page,
  dead JS). Rationale + caching live in
  [HANDOFF-management-ui.md](HANDOFF-management-ui.md) ("`/assets` is served
  on both the HTTP and HTTPS server blocks"). All other HTTPS paths redirect
  back to their HTTP equivalents.
- README, BRINGUP, and this handoff document the trust/preflight flow.
  No HSTS header is configured.

**Out of scope:** redirecting HTTP → HTTPS for existing routes.
The Spotify and dial flows do not benefit from being moved to
HTTPS, and breaking them is a regression risk.

### Decision 2 — Web framework: stdlib `ThreadingHTTPServer` + polling

**Decision:** Build `jasper/web/correction_setup.py` as another
`BaseHTTPRequestHandler` subclass colocated in `jasper-web`,
listening on `127.0.0.1:8770`. The browser polls `GET /status`
for sweep / generation state. Audio capture uploads as a single
`POST /upload-capture` with `Content-Length`.

**Why not FastAPI?** Codebase precedent: every existing web wizard
is stdlib `ThreadingHTTPServer`. Adding FastAPI introduces a new
runtime dependency, a new ASGI server (uvicorn), a new systemd
unit shape, and breaks the "one jasper-web process owns all the
settings ports" pattern in [jasper/web/__main__.py](../jasper/web/__main__.py).

**Why not WebSockets?** The original brief assumed bidirectional
WS for "real-time during-sweep visualization." V1 explicitly punts
that (post-hoc viz only). Without it, polling plus discrete REST
actions (`POST /start`, `POST /upload-capture`, `POST /apply`) is
good enough and simpler to debug on iOS Safari.

**Why not aiohttp?** Same reason as FastAPI — new dep, breaks the
pattern. The existing async work in [control/server.py](../jasper/control/server.py)
uses `asyncio.run()` per request to bridge stdlib HTTP into async
coordinator code; we do the same here for the
`measurement_window()` async context manager.

**Concrete shape (current):**
```
HTTP port 80:
GET  /correction/            static preflight explaining the HTTPS warning;
                             OK button links to /correction/proceed, or
                             /correction/proceed/<subflow> for safe
                             ?next=/correction/... targets; JS validates the
                             target and rewrites the link to the final HTTPS
                             URL with a fresh jts_cb token
GET  /correction/proceed     redirect to https://$host/correction/$is_args$args
GET  /correction/proceed/room|crossover|bass
                             redirect to the matching https://$host/correction/
                             subflow, preserving query args
GET  /jts-root-ca.crt        download private root CA for iOS trust

HTTPS port 443 after nginx strips /correction/:
GET  /                       room page render (stdlib HTML + room JS module)
GET  /room                   same room-correction page as /
GET  /crossover              active-crossover mic measurement page
GET  /crossover/status       active-speaker targets + measurement evidence
                             ({..., active: bool — false for a
                             full_range_passive speaker, Layer A hidden})
GET  /crossover/envelope     commissioning screen envelope (dumb frontend):
                             {schema_version, screen, active, steps,
                             verdict_text, nudges, next_action, progress, relay}
GET  /bass                   bass/subwoofer tuning placeholder page
GET  /healthz                liveness — "ok"
GET  /status                 session + currently-loaded correction snapshot
                             ({state, peqs, autolevel, input_device,
                             mic_calibration, target_profile,
                             correction_strategy, design_report,
                             current_correction: {path, session_id,
                             applied_at_epoch, peq_count} | null})
GET  /sessions               debug: 20 most-recent session bundles
GET  /session-report?id=<id> read-only evidence report for one bundle
GET  /calibration/models     supported calibrated mic providers/models
POST /start                  first checks setup_status.room_correction_allowed
                             (passive speakers allowed; incomplete active Layer A
                             gets 409 + /correction/crossover/ before reservation),
                             then resets to base config, begins noise capture, returns session_id;
                             body: {total_positions, target_choice,
                             strategy_choice?, noise_floor_db?,
                             calibration_id?, input_device?,
                             repeat_main_position?}
POST /next-position          advance to position[N+1] pre-sweep noise capture
POST /repeat-position        play the optional same-seat repeat sweep
POST /upload-noise           body = WAV (audio/wav); pre-sweep room noise
POST /upload-capture         body = WAV (audio/wav); per-position, repeat, OR verify capture
POST /calibration/fetch      body: {model, serial, orientation?}; server-side
                             Dayton/miniDSP lookup, normalized + stored
POST /calibration/upload     body: {filename, content, model?, label?,
                             orientation?, sign_convention?}; manual fallback
POST /apply                  → SetConfig(correction_<id>_<unixtime>.yml) + Reload
POST /reset                  → SetConfig(topology-safe reset graph) + Reload
POST /verify                 fresh single-position sweep for the verify pass
POST /relay/level-match      ambient-baselined, gradual listening-position ramp;
                             stores a bounded gain lease or restores on failure
POST /relay/capture          relay room sweep using the bound mic + gain lease
POST /relay/verify           relay post-apply sweep; restores the lease and lands
                             on the terminal result (or pending-confirm loop)
POST /session/delete         delete one historical measurement bundle
POST /interpret              P6 tuning LLM (ONE PAID CALL, per-tap, spend-cap
                             gated → 429 at the household daily cap): plain-
                             language narration of the server-computed result
POST /propose                P6 tuning LLM (ONE PAID CALL, per-tap, spend-cap
                             gated → 429 at the household daily cap): bounded
                             correction/target proposals, simulated server-
                             side; applies NOTHING
POST /propose/apply          NO paid call: re-validate + re-simulate a user-
                             confirmed PEQ proposal (confirm:true required),
                             then the same apply path as POST /apply
                             → design: HANDOFF-calibration-agent.md
                             "The P6 tuning surface"
POST /test-tone              5-second 1 kHz tone through music chain
POST /autolevel/start        ramp main_volume while tone plays
POST /autolevel/lock         freeze main_volume at current ramp value
POST /autolevel/cancel       abort ramp, restore pre-autolevel volume
POST /crossover/level-match  guided mic/calibration + near-field automatic level
POST /crossover/apply        atomically apply measured Layer A; restore gain lease
POST /crossover/driver-capture body = WAV (audio/wav); analyze + record
                             active-speaker per-driver acoustic evidence
POST /crossover/summed-capture body = WAV (audio/wav); analyze + record
                             active-speaker summed-crossover evidence
POST /crossover/relay-capture body: {kind: driver|summed, speaker_group_id,
                             role?}; phone-mic relay transport for one
                             crossover sweep (same record_*_capture analysis;
                             refuses while room/balance/sync
                             is active — server-computed at POST and
                             re-checked when the phone arms). ON-DEVICE:
                             not exercised hardware-free — H2.
HTTPS fallback              non-/correction/ paths 302 + no-store back to HTTP
```

Browser polls `GET /status` every 500 ms; SSE was considered but never
landed because polling is simpler in stdlib and the latency budget
allows it.

### Decision 3 — URL: `/correction/`, plus entry on the landing page

**Decision:** `http://jts.local/correction/` is the user-facing entry
route. It serves a static preflight page on port 80, then the
measurement flow switches to `https://<current-host>/correction/`
because browser microphone capture requires a secure context. The JS-enabled
path goes directly to the final HTTPS URL after allowlist validation; the
no-JS fallback uses nginx's `/correction/proceed` temporary redirect to
`https://$host/...`. `$host` is important for non-default speakers such as
`jts3.local`; the preflight must not hard-code `jts.local`. The nginx
port-80 landing page at `/usr/share/jasper-web/index.html` links to the
preflight instead of
directly to HTTPS. The 443 catch-all redirects non-correction paths back
to HTTP with a temporary, non-cacheable redirect — the one exception is
`/assets/`, served statically so the measurement UI's CSS/JS aren't
mixed-content-blocked; it does not proxy any extra wizard upstreams over
HTTPS.

**Why not `/room/` or `/measure/`?** User specified `/correction/`
in feedback (2026-05-09).

### Decision 4 — Coordinator: extend voice_daemon UDS, no new daemon

**Decision:** Add two commands to `voice_daemon`'s control socket
([jasper/voice_daemon.py](../jasper/voice_daemon.py)):
- `MEASURE_PAUSE` → set in-process `_measurement_active` event;
  pause `WakeLoop` (block on the event before pulling the next
  audio chunk); pause outputd's content loudness meter so sweeps do
  not become assistant loudness baselines; cancel any active
  `Ducker.duck()` and skip future ones; pause the voice daemon's 1 Hz Camilla
  drift reconciler so it cannot overwrite the quiet-start/ramp/restore
  transaction;
  return JSON `{"result": "ok"}`.
- While the measurement window remains open, the coordinator repeats
  idempotent `MEASURE_PAUSE` every 60 seconds to renew the voice daemon's
  120-second crash-recovery timer. A dead coordinator stops renewing, so the
  speaker still self-recovers.
- `MEASURE_RESUME` → clear the event and reconcile guard, restart trackers,
  return JSON.

The HTTP coordinator at `jasper/correction/coordinator.py` is an
async context manager:
```python
async with measurement_window():
    # 1. systemctl stop librespot shairport-sync bluealsa-aplay
    # 2. UDS MEASURE_PAUSE → voice_daemon
    # 3. yield (caller does the sweep + analysis + filter design)
    # 4. UDS MEASURE_RESUME → voice_daemon  (in finally)
    # 5. systemctl start librespot shairport-sync bluealsa-aplay
```

**Why not a new `jasper-coordinator` daemon?** The patterns we
need already exist:
- "Pause renderers" = `systemctl stop`. Done.
- "Pause voice loop" = UDS command (mirrors `/cue/play` shape).
- "Pause AEC bridge" = if enabled, the bridge re-converges in
  ~200 ms after the sweep stops; no pause needed.
- A new daemon adds startup time, IPC plumbing, systemd shape,
  and another thing that can fail. The work doesn't justify it.

**Why scoped under `jasper/correction/` not top-level
`jasper/coordinator/`?** The "pause everything for X" pattern is
not currently reused anywhere else. Push-to-talk doesn't need it
(the dial drives the WakeLoop directly). Snapcast (v5) is far
enough out that we'll know its requirements when we get there.
Keep it scoped until we have a second caller. **YAGNI.**

### Decision 5 — Filter ladder for 1 GB Pi

**Decision:** PEQ-only as v1. FIR work stays staged and
measurement-backed: convolution import/export first, then short
minimum-phase magnitude correction, then FDW / mixed-phase only after
real 1 GB profiling. Expensive filter generation should extend the
measurement window through design, not just measurement. Surface a
"this filter type needs 2 GB" message at runtime if `/proc/meminfo`
shows insufficient free memory *after* pausing.

**Why not require 2 GB?** User decision (2026-05-09): "see how
far we can get on 1 GB."

**Concrete RAM budget on 1 GB after pause:** Per the explore-agent
audit of running processes, steady total is 500-620 MB. Pausing
librespot, shairport-sync, bluez-alsa, and the Gemini Live SDK
(via `MEASURE_PAUSE` + `systemctl stop`) frees an estimated
130-200 MB. That's enough headroom for PEQ (negligible) and likely
enough for short minimum-phase FIR prototypes, but Phase 5 should
measure peak memory before enabling mixed-phase or FDW generation in
the user-facing flow.

**Defaults that keep us safely on the safe side:**
- Match range: 20-350 Hz (project-safe modal/transition-region
  boundary; not a direct Toole magic number)
- ≤5 PEQ filters
- Cuts only by default (Floyd Toole's "first do no harm")
- Max boost +3 dB (toggle in advanced drawer)
- Max cut -10 dB
- Q range 1.0-8.0
- Overall max boost 0 dB (preserve digital headroom) — the cuts-only
  default guarantees this; when boosts are enabled it is *enforced* at
  emit time by a `room_headroom` preamp the shared emitter derives from
  the worst-case additive room boost (`total_positive_boost_db`), so a
  corrected boost can never exceed unity. See
  [HANDOFF-sound-preferences.md](HANDOFF-sound-preferences.md) "Gain
  staging — boosts boost".

These mirror Jasper's known-good REW workflow (per the engineering
brief).

### Decision 6 — Sweep generation: in-house synchronized swept-sine

**Decision:** Use the in-house NumPy/SciPy synchronized swept-sine
generator in `jasper.audio_measurement.sweep` (Novak 2015), not vanilla
Farina ESS. 10 s sweep, 20 Hz - 20 kHz, -12 dBFS. The deconvolution
path performs FFT-based regularized inversion at IR-extract time; no
precomputed inverse filter is shipped.

**Why not vanilla Farina?** Synchronized variant places harmonic
distortion impulses at integer-fraction offsets of the IR, making
them trivial to discard. The shipped generator implements that
synchronization directly so the runtime dependency surface stays
small.

### Decision 7 — Spatial averaging: fixed multi-position sweeps, power mean

**Decision:** Phase 1 ships single-position. Phase 2 adds fixed
multi-position sweeps and power-mean magnitude averaging. This is not
true MMM: moving-mic measurement usually means pink-noise/RTA-style
averaging over a small listening volume and does not preserve IR/phase
data. Strict future spatial analysis should retain complex H(f) per
position and may vector-average below the transition region while
power-averaging above, but the shipped code does power-mean
everywhere.

### Decision 8 — Mic compensation: calibrated external mic first

**Decision:** Treat per-unit calibrated measurement mics as the
trusted path. The wizard supports browser input-device selection,
server-side serial lookup for known Dayton Audio and miniDSP mics,
and manual calibration-file upload for unsupported mics or vendor
lookup failures. A built-in phone mic path may remain as a degraded
fallback, but it must be labeled lower confidence and should not
unlock future FIR or agent recommendations that depend on small dB
distinctions.

**Why not a per-phone curve database?** No published cross-model
compensation database exists. HouseCurve and AudioTool both ship
single generic curves. A generic phone curve is useful for demos and
rough bass-region work, but the long-term substrate for serious room
correction is a known calibrated mic with bundle provenance.

### Decision 9 — Power-user pass-through: reverse-proxy `camillagui-backend` at `/camilla/`

**Decision:** Phase 3 drops in HEnquist's `camillagui-backend`
v0.7.x as a systemd service on `127.0.0.1:5000`. Reverse-proxy
`https://jts.local/camilla/*` → `127.0.0.1:5000/*` in the new
nginx 443 server block.

**Why not build our own YAML upload UI?** camillagui-backend is
written by the CamillaDSP author, the AVS/ASR community already
knows the UI on sight, it has FIR coefficient upload, statefile
management, level meters, pipeline visualization, theming via CSS
variables. Building this ourselves is 4+ days of work for a worse
result.

**Statefile coordination:** measurement coordinator is the writer
of `/var/lib/camilladsp/state.yml`. camillagui reads + suggests,
only pushes on explicit user action.

## Audio path: where the sweep enters

From [docs/audio-paths.md](audio-paths.md):

```
MUSIC chain
    renderers / correction sweeps → private fan-in lanes
              → jasper-fanin → pcm.jasper_capture
              → jasper-camilla (main_volume + filters)
              → outputd_content_playback
              → jasper-outputd → outputd_dac → amp → speakers
```

**Sweep injection point: `correction_substream`.** This puts the
sweep on the same path music takes — through jasper-fanin, through
CamillaDSP, through any active correction filter, through outputd to
the dongle —
without borrowing a renderer's private lane. So:

1. Pre-correction measurement = sweep through current pipeline.
2. Apply candidate filter set.
3. Post-correction measurement = sweep through the new pipeline.

**This is critical:** the sweep MUST go through CamillaDSP.
Otherwise we measure the speaker+room raw, apply a correction,
and never verify it actually changed anything. The previous TTS-
bypass-of-CamillaDSP pattern (TTS → outputd directly, or legacy
TTS → `pcm.jasper_out`) is *wrong* for measurement.

**Volume during sweep:** Set CamillaDSP `main_volume` to a known
absolute level (the brief suggests -12 dBFS sweep at user-controlled
analog volume; we set `main_volume` to whatever the user picked
during the volume calibration screen, default -10 dB). On exit,
restore via VolumeCoordinator.

**Music ducking interaction:** Ducker MUST be skipped during the
measurement window (see Decision 4). Otherwise a wake event
mid-sweep would attenuate the sweep itself.

## File map — current correction code

```
jasper/
├── active_speaker/
│   ├── crossover_envelope.py            commissioning screen envelope (aligned
│   │                                    with the room envelope pattern; passive
│   │                                    gate) — composes commissioning_coordinator
│   ├── web_commissioning.py             secure crossover playback orchestration
│   │                                    through active-speaker safety state
│   └── web_measurement.py               browser WAV storage + acoustic evidence
│                                        bridge into measurement state
│
├── audio_measurement/                   P1b: shared measurement kernel extracted
│   │                                    from jasper/correction/ (commit 4001755b)
│   ├── __init__.py
│   ├── sweep.py                         NumPy/SciPy synchronized swept-sine
│   ├── deconv.py                        IR extraction
│   ├── analysis.py                      smoothing, spatial avg, deviation metrics
│   ├── calibration.py                   calibration parser + Dayton/miniDSP providers
│   ├── quality.py                       capture quality gates + issue schema
│   ├── quality_model.py                 parameterized capture-quality thresholds
│   │                                    shared across tuning layers
│   └── ramp.py                          settle-based level-match ramp controller
│                                        (shared measurement kernel, P2)
│
├── correction/
│   ├── __init__.py
│   ├── coordinator.py                   measurement_window() async CM
│   ├── playback.py                      sweep → correction_substream via aplay
│   ├── peq.py                           greedy PEQ design (≤5 filters, cuts)
│   ├── target.py                        Harman / flat / house-curve interpolant
│   ├── bundles.py                       debug-bundle listing / validation helpers
│   ├── bundle_tools.py                  inspect/replay/export helpers for bundles
│   ├── runtime_integrity.py             Pi/runtime health evidence around sweeps
│   ├── runtime_safety.py                runtime graph safety re-checked against the
│   │                                    saved output topology contract
│   ├── acoustic_quality.py              SNR/repeatability/direct-arrival trust evidence
│   ├── replay_artifacts.py              compact derived IR/response artifacts
│   ├── artifacts.py                     per-session bundle writer / manifest owner
│   ├── status.py                        current-config + status/bundle payload serializers
│   ├── autolevel.py                     auto-level ramp controller + volume restore
│   ├── state_guard.py                   capture watchdog + reset-busy guard
│   ├── fir_runtime.py                   FIR coefficient inspect/stage substrate
│   ├── evidence.py                      deterministic human/agent evidence packet
│   ├── acceptance.py                    deterministic verify-acceptance verdict (P4)
│   ├── browser_audio.py                 browser audio-path preflight report
│   ├── confidence.py                    deterministic confidence report for measurements
│   ├── envelope.py                      server-computed screen envelope (dumb-frontend
│   │                                    / smart-backend contract)
│   ├── interop.py                       REW-compatible export (delimited freq-response
│   │                                    text, mono WAV impulse responses)
│   ├── level_match.py                   correction-side adapter for the relay-closed
│   │                                    level-match ramp (P2); pure math lives in
│   │                                    audio_measurement.ramp
│   ├── spatial.py                       shared spatial-spread helpers for
│   │                                    multi-position measurements
│   ├── strategy.py                      correction strategy and target-profile
│   │                                    orchestration (raw math -> product policy)
│   └── session.py                       measurement state machine + DSP orchestration
│                                        (delegates auto-level ramping, state guards,
│                                        and status serialization)
│
├── cli/
│   └── doctor.py                        correction socket / bundle / config checks
│
├── web/
│   ├── correction_report.py             read-only report payload helpers
│   ├── correction_hub.py                room/crossover/bass tab chrome
│   ├── correction_crossover_backend.py  active-speaker acoustic evidence bridge
│   ├── correction_crossover_flow.py     /correction/crossover/ page + routes
│   ├── correction_bass_flow.py          /correction/bass/ placeholder
│   └── correction_setup.py              mirrors voice_setup.py shape
│                                        ThreadingHTTPServer on 127.0.0.1:8770
│                                        polling status, POST for upload+apply,
│                                        and correction subflow dispatch
│
├── voice_daemon.py                      MEASURE_PAUSE / MEASURE_RESUME
│                                        UDS commands; gate WakeLoop +
│                                        outputd content meter on _measurement_active
│
└── camilla.py                           CamillaController config-path switch +
                                         reload helpers used by /correction/

deploy/
├── nginx-jasper.conf                    443 server block for /correction/
├── assets/correction/                   room + crossover static CSS/ES modules
├── jasper-correction-web.service        socket-activated worker, private umask
├── jasper-correction-web.socket         systemd socket on 127.0.0.1:8770
└── install.sh                           private CA, state dirs, unit install

docs/
└── HANDOFF-correction.md                THIS FILE

tests/
├── test_correction_sweep_deconv.py      sweep + deconvolution fixtures
├── test_correction_peq.py               PEQ design on known curves
├── test_sound_camilla_yaml.py           live DSP YAML emit; preserves room PEQs
├── test_correction_coordinator.py       pause/resume contract
├── test_correction_session.py           session bundle + measurement flow
├── test_correction_setup.py             correction web handler
├── test_correction_calibration.py       mic calibration parser/providers
├── test_correction_quality.py           capture quality gates
├── test_correction_bundles.py           bundle listing / validation helpers
├── correction_bundle_fixtures.py        golden synthetic bundle helper
└── test_correction_systemd_unit.py      unit/install invariants

/usr/share/jasper-web/index.html         EDIT — add /correction/ entry card
```

**Naming consistency check:** subpackage is `jasper.correction` (not
`jasper.room`) per Decision 3. Web wizard module follows the existing
suffix convention (`voice_setup`, `dial_setup` → `correction_setup`).

## Phased build plan

Each phase is a feature branch + PR per the standing rule. Each
phase has a runtime exit criterion, not a "looks right in the
diff" criterion.

### Phase 0 — TLS + skeleton (1.5 days)

**Goal:** open `https://jts.local/correction/` on iPhone Safari,
after one-time cert trust, see "Hello mic" page with a working live
mic level. Nothing else.

Concrete changes:
- `deploy/install.sh`: generate/preserve the private CA at
  `/var/lib/jasper/ca/ca.{crt,key}`; issue the correction server cert
  into `/etc/nginx/ssl/jts.local.{crt,key}`; copy the root certificate
  to `/usr/share/jasper-web/jts-root-ca.crt` for download.
- `deploy/nginx-jasper.conf`: add `listen 443 ssl` server block;
  `location /correction/ { proxy_pass http://127.0.0.1:8770/; }`;
  `location /jts-root-ca.crt { ... }` on **port 80** (chicken-
  and-egg: user has to download CA before HTTPS works); serve
  `http://jts.local/correction/` as the plain-HTTP preflight before
  the HTTPS measurement UI.
- `jasper/web/correction_setup.py`: minimal handler returning a
  static "Hello mic" page that requests `getUserMedia({audio: ...})`
  and shows a level meter via AudioWorklet.
- `jasper/web/__main__.py`: register port 8770.
- `BRINGUP.md` Phase Z: document the iOS trust dance.

**Exit criterion (must verify on iPhone, not just types):**
1. `curl -k https://jts.local/correction/` returns the correction page.
2. Open in iPhone Safari (after cert trust): page loads, mic
   permission prompt appears on first interaction.
3. After granting: level meter responds to voice within ~50 ms.
4. **Read back `getUserMedia` track settings** in the page — verify
   `sampleRate === 48000`, `echoCancellation === false`,
   `noiseSuppression === false`, `autoGainControl === false`.
   Show a red banner if any constraint didn't take effect. (This
   is the load-bearing iOS Safari verify step the sanity-check
   pass flagged.)

### Phase 1 — Vertical slice: 1 position, PEQ, end-to-end (3 days)

**Goal:** Jasper sits on the couch, hits "Measure," hears the sweep,
sees a chart, taps "Apply," next song plays through corrected DSP.
5-minute YouTube demo recordable.

Concrete changes:
- `jasper/correction/coordinator.py`: `measurement_window()` async
  context manager. Calls `systemctl stop` on all music source daemons
  that can write into fan-in. Sends `MEASURE_PAUSE` over UDS to voice_daemon.
  On exit (including exceptions): sends `MEASURE_RESUME`,
  `systemctl start ...`.
- `jasper/voice_daemon.py`: handle `MEASURE_PAUSE` / `MEASURE_RESUME`
  in `_handle_command()`. Set `self._measurement_active = asyncio.Event()`.
  WakeLoop's main loop awaits `not self._measurement_active.is_set()`
  before pulling each audio chunk. Outputd's content meter is paused
  via the TTS control socket, and `Ducker.duck()` is a no-op when set.
- `jasper/audio_measurement/sweep.py`: in-house NumPy/SciPy synchronized
  swept-sine, 10 s, 20 Hz - 20 kHz, -12 dBFS, S16_LE WAV output.
  Cache on disk — it's deterministic. (Moved out of `jasper/correction/`
  into the shared measurement kernel in P1b; all three tuning layers reuse it.)
- `jasper/correction/playback.py`: shell out to
  `aplay -D correction_substream sweep.wav`. Wait for completion.
- `jasper/audio_measurement/deconv.py`: take phone-uploaded WAV + sweep
  metadata, perform regularized FFT inversion → mono float32 IR.
- `jasper/audio_measurement/analysis.py`: 1/48-octave magnitude smoothing
  → JSON-serializable curve (frequency, dB).
- `jasper/correction/peq.py`: greedy peak-fit on 20-350 Hz residual
  vs target. ≤5 PEQ filters. Cuts only. Q ∈ [1.0, 8.0]. Max -10 dB.
- YAML emit (live apply path): `jasper/correction/session.py` asks
  `jasper.sound.graph_carrier` to re-emit the currently loaded topology with
  fresh room PEQs and the saved sound profile. For ordinary stereo this still
  lands in `jasper.sound.camilla_yaml.emit_sound_config(...)`; for solo active
  baselines it recomposes the active speaker graph pre-split so crossovers and
  driver protection stay in place. Writes to
  `/var/lib/camilladsp/configs/correction_<session_id>_<ts>.yml`.
- Extend `jasper/camilla.py` `CamillaController` with:
  - `set_config_path(path: str) -> bool` — calls
    `c.config.set_file_path(path)` then `c.general.reload()`.
  - `reload() -> bool` — bare reload of current config path.
- `jasper/web/correction_setup.py`: full route table from Decision 2.
- Frontend: stdlib-served HTML in `jasper/web/correction_setup.py`.
  Vanilla JS + AudioWorklet captures into Int16, accumulates, and
  posts WAV blobs. The canvas chart shows measured / target /
  predicted curves. "Apply Correction" button → `POST /apply`.
- Frontend: Wake Lock during sweep (`navigator.wakeLock.request('screen')`).
- Frontend: "Rotate your phone 180°, lay flat, no case" instruction
  screen (WiiM RoomFit UX pattern).

**Exit criterion:**
1. Tap Measure → sweep audible at the speaker, no music interruption
   beyond the planned pause.
2. Capture upload completes within 2 s of sweep end.
3. Magnitude chart renders within 5 s.
4. Tap Apply → CamillaDSP swaps config without audio dropout (verify
   by playing music continuously across the apply boundary;
   `aplay -D correction_substream white_noise.wav` is the easiest
   no-streaming-service way to verify mid-stream).
5. Re-running Measure shows a different curve (filter actually
   reaches the speaker).
6. **Manual A/B verification:** play a familiar bass-heavy track
   before/after; the modal peak audibly tightens.

### Phase 2 — Multi-position sweeps + verify pass (3 days)

- 5-position UI with diagrams. Shipped implementation does
  power-mean magnitude averaging everywhere; true moving-mic MMM and
  vector-below-transition averaging are future refinements.
- "Re-measure" step at end; before/after overlay on same chart.
- Robust error states: mic permission denied, sample-rate mismatch
  (force-reject), sweep clipping detected (rerun with -3 dB lower),
  ambient too loud (>50 dB pre-sweep, prompt user).
- Named target selector (Flat / Neutral / Warm / Bright), with the
  house-curve profiles interpolating around Flat ↔ Harman.
- Calibrated external mic metadata and correction curve applied when
  available; built-in phone mic compensation is a degraded fallback,
  not the high-confidence path.

### Phase 3 — Power-user pass-through (1.5 days)

- `apt install camillagui-backend` or unpack v0.7.x bundle.
- Configure `/etc/camillagui/camillagui.yml` to point at
  `127.0.0.1:1234` (CamillaDSP) and the same statefile we use.
- New systemd unit `jasper-camillagui.service`.
- nginx `location /camilla/ { proxy_pass http://127.0.0.1:5000/; }`
  in the 443 server block.
- Nav link from `/correction/` → `/camilla/` "Power user mode."
- Statefile coordination: measurement coordinator is the only
  writer; camillagui reads + suggests.

### Phase 4 — REW import/export and measurement interop (PARTIAL)

- ✅ `.frd` export (REW-compatible: `Hz dB phase`, 1/48-oct
  underlying). Current bundle curves are magnitude-only, so exported
  phase is explicitly `0.0` and the header says so.
- ✅ REW `.txt` export with frequency + magnitude columns.
- ✅ `.wav` IR export (mono float32, unnormalized by default) for REW
  / external FIR tooling. IRs are recomputed from raw capture WAVs and
  `sweep_meta`, not copied from a stale cache.
- ✅ Bundle inspect/replay CLI:
  `jasper-correction-bundle inspect <bundle> [--recompute]` and
  `jasper-correction-bundle export <bundle> --output <dir>`.
- Document the round-trip workflow: measure here → export `.frd` →
  open in REW → REW's CamillaDSP YAML export (V5.20.14+) → upload
  back at `/camilla/`.
- Keep calibration lookup provider adapters narrow and observable.
  Dayton and miniDSP serial lookup already landed in Phase 2.3; Phase
  4 should preserve manual upload as the fallback and focus on import /
  export paths, provenance, and clear operator errors when vendor
  endpoints drift.

### Phase 5 — Filter sophistication (research-gated, 1 GB-aware)

- Rung 2: CamillaDSP convolution import/export, bundle storage,
  latency reporting, and headroom accounting.
- Rung 3: short minimum-phase FIR for magnitude correction under the
  same conservative target / boost rules as PEQ. Prototype internal
  SciPy-based design and compare against proven tools before deciding
  the shipped generator.
- Rung 4: frequency-dependent-windowed FIR after evaluating DRC-FIR,
  rePhase, REW, CamillaDSP-native workflows, and CamillaFIR if
  verified as a concrete current reference.
- Rung 5: mixed-phase / excess-phase correction. Opt-in only, guarded
  by measurement quality, latency profile, and pre-ringing audit.
- If any rung needs expensive generation, run it under the existing
  measurement-window pause discipline and refuse to start if
  `/proc/meminfo` shows insufficient free memory after renderers have
  paused.
- Extend the existing `jasper-doctor` correction checks with
  FIR-specific readiness: convolution artifact presence, latency /
  headroom accounting, newest measurement timestamp, and last FIR
  generation result.

**1 GB enforcement:** runtime check at filter-design entry.
Surface "this filter type needs 2 GB Pi or more aggressive
process pausing" if fail.

## What we're NOT building (and why)

Scope discipline. Each of these has a real-world reason; if you're
about to add one mid-phase, stop and re-read.

- **Real-time during-sweep visualization.** Not in V1 (sanity-check
  pass agreed). The post-hoc chart is enough to demo and tune. Adds
  WS dependency, AudioWorklet → main-thread streaming, frame-rate
  decisions. Defer to a hypothetical V3.
- **LLM critique layer.** Not in V1 or V2. Deterministic safety
  checks (max-boost, max-Q-vs-frequency, phase-coherence) cover the
  failure modes. Adds user-supplied API key plumbing, prompt eval,
  cost. Defer to V3+.
- **Manual draggable-PEQ overlay.** Not in V1. The auto-fit handles
  the modal range competently. Manual editing is a power-user feature
  that lives behind the `/camilla/` pass-through anyway. Defer.
- **PWA / service worker.** Not relevant; not building a PWA.
- **Auto-detect Schroeder transition from RT60.** Not in V1. Hard-
  coded 350 Hz boundary aligns with Toole defaults and most living
  rooms. Add a power-user toggle in V2.1 if needed.
- **"Contribute your iPhone profile" community calibration database.**
  Not in V1 or V2. Out of scope for a personal-hobby project.
- **Anything above 350 Hz by default.** Per user feedback (2026-05-09):
  "hold the line on phase one for the lower frequencies." Above-
  Schroeder correction is a deliberate opt-in toggle, not a default,
  per Toole's "treat the room with acoustic treatment, EQ minimally
  above transition" doctrine.
- **Multi-driver crossover / wireless sub.** Not in V2. v6 territory
  per [PLAN.md](../PLAN.md).
- **Bypassing CamillaDSP for the sweep.** Wrong by design — see
  "Audio path" section. The sweep MUST traverse the same chain
  music does, otherwise corrections are unverifiable.

## Things we adopt from the briefs (with attribution)

From the engineering brief:
- CamillaDSP `SetConfig` + `Reload` for atomic hot-swap.
- PEQ-only as v1 default; FIR ladder for power users later.
- 20-350 Hz match range, fixed multi-position sweeps, and a future
  Schroeder-aware vector/power averaging refinement.
- Dual-audience principle: same engine, novice flow + power-user
  export buttons.
- `.frd` export for REW round-trip.
- iPhone built-in mic compensation curve, single bundled curve,
  acknowledge inaccuracy.

From the sanity-check pass:
- Phase 1 = thin vertical slice. Single position, PEQ-only, end-to-end.
- Pin 48 kHz on both ends; reject any AudioContext that didn't
  honor the request.
- `getSettings()` verify that EC/NS/AGC actually got disabled.
- Synchronized swept-sine (Novak 2015) implemented in-house with
  NumPy/SciPy primitives.
- WiiM "rotate phone 180°, lay flat, no case" mic-placement UX.
- Wake Lock during sweep window.
- Reverse-proxy `camillagui-backend` at `/camilla/`.
- Evaluate DRC-FIR, rePhase, REW, CamillaDSP convolution, and
  CamillaFIR if verified before choosing a FIR generator; do not bind
  Phase 5 to one subprocess until a prototype proves the trade-off.
- Drop fft.js — compute RMS in AudioWorklet (32 multiplies, no FFT
  needed for level meter).
- Keep pyfar / pyrato as strong references for future RT60 /
  Schroeder work; add runtime dependencies only when the shipped
  feature needs them.
- In-house canvas chart for the current measured / target /
  predicted curves. Revisit uPlot only if the history or waterfall
  UI needs denser interactive plotting.

## Things we reject from the briefs (with attribution)

From the engineering brief:
- **FastAPI + uvicorn.** Wrong for this codebase — see Decision 2.
  Stdlib `ThreadingHTTPServer` + polling.
- **WebSockets for sweep state.** Not needed; `GET /status` polling
  covers the shipped state surface.
- **HiFiBerry DAC8x assumption.** This speaker uses an Apple USB-C
  dongle; the brief was written without reading our README.
- **PipeWire fanout assumption.** This stack is pure ALSA snd-aloop
  + dmix. The brief's PipeWire coordination paragraphs don't apply.
- **`Speaker Activator` naming.** Project is JTS / Jasper Tech
  Speaker.

From the sanity-check pass:
- **ECharts for V2 waterfall plots.** Premature optimization.
  Stay on the in-house canvas chart until there's a concrete need
  for a plotting dependency.
- **LLM critique layer in V2.** Defer to V3+. Per "What we're NOT
  building."
- **`pyrirtool`, `PORC` references.** Both unmaintained; pyfar +
  pyrato cover the territory.
- **2 GB Pi recommendation.** User wants to see how far 1 GB can
  go. Decision 5 explicitly addresses RAM headroom via process
  pausing.

## Open questions

Honest list. Each needs a decision before the relevant phase ships,
not before this doc lands.

1. **iPhone mic compensation curve source.** HouseCurve doesn't
   publish theirs. Faber Acoustical published older measurements
   (`blog.faberacoustical.com`). Need to either pick one published
   reference (with citation) or measure ours during Phase 2 dev.
   **Decision needed by Phase 2.**
2. **camillagui-backend version pinning.** v0.7.x tracks CamillaDSP
   3.0.x. We'll pin to a specific tag at Phase 3 start to insulate
   against upstream churn. **Decision needed by Phase 3.**
3. **Sweep level for compromised analog volumes — resolved.** The phone-backed
   level check measures a 1 kHz tone while the bounded controller raises JTS's
   main volume and locks the measurement reference. Tone and ESS source peaks
   share `AUTOMATIC_MEASUREMENT_STIMULUS_PEAK_DBFS` (−12 dBFS). Automatic
   crossover ESS adds the current immutable applied Layer-A role gain; it never
   inherits a quiet by-ear driver-test floor. A missing/stale applied snapshot
   or mismatched played-excitation ledger fails closed. The combined crossover
   ESS uses a transient validated recompose of the entire applied Layer-A graph,
   not the old combined listening-test level, and restores the prior DSP graph
   afterward.
4. **What does the openWakeWord pause actually look like?**
   ([jasper/voice_daemon.py](../jasper/voice_daemon.py) is large;
   need to grep `openwakeword` and identify the right gate point.)
   **Decision needed early in Phase 1.**
5. **AEC bridge interaction.** If the bridge is enabled, does the
   sweep through the music chain become an AEC reference and drive
   the bridge into a weird state during measurement? Two paths:
   (a) test it (most likely fine, bridge re-converges in ~200 ms);
   (b) explicitly stop `jasper-aec-bridge.service` during measurement.
   **Decision needed by Phase 1 exit; default to (b) defensively.**
6. **Where do correction profiles persist?** **Resolved (Phase 2.1):**
   each apply emits a new file at
   `/var/lib/camilladsp/configs/correction_<session_id>_<unixtime>.yml`.
   Files are never deleted by JTS — they're cheap (a few KB), and a
   future "restore previous correction" feature can pick from this
   directory. The currently-loaded path is read via `CamillaController.
   get_config_file_path()` and surfaced to the UI banner via
   `parse_current_correction()`.
7. **What does "Reset correction" do?** **Resolved (Phase 1+2.1; active
   speaker safety tightened 2026-06-18; topology-preserving reset/apply
   shipped 2026-06-24):**
   if a measurement is in progress, reset restores the graph that was active
   before `/start`. After a correction has been applied, reset means "remove
   Layer B": re-emit the current graph through `jasper.sound.graph_carrier`
   with `room_peqs=[]` while preserving topology-owned speaker DSP and the
   current preference profile. If graph-carrier re-emit is unavailable, the
   fallback target still comes from `jasper.correction.runtime_safety` and
   must be legal for the saved topology.

## Risk register

What can actually go wrong, ordered by likelihood × impact.

1. **iOS Safari `echoCancellation: false` constraint silently
   ignored.** Real per WebKit Bug 179411. Mitigation: read back
   `getSettings()` after `getUserMedia()`, show red banner if
   not honored. Documented in Phase 0 exit criterion.
2. **AudioContext sample rate locks to 44.1 kHz on Bluetooth
   headset connect.** Real per WebKit Bug 274507 (iPadOS 17.5
   regression, fixed in later releases but still latent).
   Mitigation: re-check `audioContext.sampleRate` immediately
   before sweep start; bail if changed. Documented in Phase 1
   sweep_capture.ts.
3. **CamillaDSP YAML emit corrupts the master_gain placeholder
   and breaks ducking.** Mitigation: round-trip test
   ([tests/test_sound_camilla_yaml.py](../tests/test_sound_camilla_yaml.py))
   that loads our emitted YAML, runs the existing
   [test_camilla_ducker.py](../tests/test_camilla_ducker.py) tests
   against it.
4. **measurement_window() leaves a music source daemon in a stopped
   state on crash.** Mitigation: `try/finally` in coordinator;
   systemd restart policies on the renderers; explicit
   `jasper-doctor` checks for source-daemon health.
5. **iOS user gives up on cert trust dance.** Mitigation: extremely
   clear onboarding instructions (screenshots, not just text) on
   the port-80 landing page. Cert download served at HTTP-only URL
   so user can land there before HTTPS works.
6. **WakeLoop deadlock on the measurement_active event during
   exception.** Mitigation: voice_daemon's MEASURE_RESUME is
   idempotent and is also called from a 2-minute-after-PAUSE
   safety timer (server-side) in case the coordinator crashes
   without sending RESUME.
7. **Filter design exceeds RAM on 1 GB after pause.** Mitigation:
   pre-flight `/proc/meminfo` check; refuse with clear message;
   suggest 2 GB upgrade. Don't OOM the Pi.

## Hardware test checklist

These items can only be verified on real hardware. Deploy with
`bash scripts/deploy-to-pi.sh`, then run on the Pi:

### Phase 0 (TLS + skeleton)
- [ ] `systemctl is-active jasper-correction-web.socket` → `active`
      (the service itself may be inactive when idle; socket
      activation is the liveness contract).
- [ ] `jasper-doctor` reports `correction web`, `correction state
      dirs`, and `current correction` as ok/warn with no fail.
- [ ] `curl http://jts.local/correction/` returns the preflight page.
- [ ] `curl -k https://jts.local/correction/healthz` → `ok`.
- [ ] `nginx -t` → ok.
- [ ] On iPhone after cert trust: page loads with no "Connection
      not private" warning; mic permission prompt appears on first
      tap; constraint table reads `✓ ok` for all 5 rows.
      **Critical**: if `echoCancellation` / `noiseSuppression` /
      `autoGainControl` read `✗ bad`, that's an iOS Safari version
      regression we have to work around — file an issue.

### Phase 1 (single-position end-to-end)
- [ ] Tap **Run measurement** → music pauses, sweep audible at the
      speaker, completes in ~10 s, no audio glitch when renderers
      come back. Watch `journalctl -u jasper-voice` for
      `MEASURE_PAUSE` / `MEASURE_RESUME` events.
- [ ] AudioWorklet capture uploads cleanly (browser network tab
      shows POST /upload-capture with audio/wav body).
- [ ] Chart renders within ~5 s of upload; PEQ list shows 0-5
      filters with reasonable freq/Q/gain.
- [ ] Tap **Apply** → CamillaDSP swaps config without audio dropout
      (verify by playing music continuously across the apply
      boundary, e.g. `aplay -D correction_substream white_noise.wav`
      in another shell).
- [ ] **Audibility check**: play a familiar bass-heavy track
      before/after Apply — modal peak should audibly tighten.
- [ ] Tap **Reset correction** on a normal full-range stereo topology →
      CamillaDSP removes room PEQs cleanly while preserving the current sound
      profile. On saved active/protected topology, verify reset keeps the
      active speaker baseline and only clears room PEQs.
- [ ] AEC bridge interaction (if enabled): no permanent drift after
      a measurement; bridge re-converges in ~200 ms.

### Phase 2 (multi-position + verify)
- [ ] 5-position flow: NEEDS_NEXT_POSITION prompt visible after
      each capture; **Continue** advances to next sweep with
      ~3-5 s of dead air (renderer pause/restart cycle).
- [ ] Move phone between positions; verify the prompt shows the
      correct position number.
- [ ] After 5 positions: PEQ design produces a result; chart shows
      the AVERAGED measured curve (not the last-position one).
- [ ] **Verify with re-measurement** after Apply: new measurement
      runs, purple dashed curve overlays on chart, RMS / max
      deviation summary appears.
- [ ] Verify deviation should be SMALLER post-correction than
      pre-correction was. If it isn't, the correction didn't work
      — check journals for clues.
- [ ] Target choice: flat vs warm produces different PEQ sets and
      audibly different results.

### Phase 2.4 (observability + quality)
- [ ] Deliberately quiet capture warns in the UI and bundle instead
      of silently producing a high-confidence chart.
- [ ] Deliberately clipped capture blocks analysis, shows
      "Measurement blocked" in the UI, and leaves `capture_quality`
      in `info.json`.
- [ ] `curl -sk https://jts.local/correction/sessions | jq` shows
      `has_result`, calibration artifact flags, and the latest
      `capture_quality` fields.
- [ ] `jasper-doctor` reports the latest bundle and warns when the
      newest completed measurement had no calibrated mic.

### Things to watch for in journals
- `journalctl -u jasper-correction-web -f` — measurement state
  transitions, handler exceptions.
- `journalctl -u jasper-voice -f` — `MEASURE_PAUSE` / RESUME
  events; auto-clear safety-timer warning if a coordinator crashes.
- `journalctl -u jasper-camilla -f` — config reload events;
  any parse errors on the emitted YAML.

## Debug bundles — operator reference

Every measurement session writes a self-contained bundle at
`/var/lib/jasper/correction/sessions/<session_id>/`. Layout:

```
sessions/<session_id>/
├── info.json        session params, target_choice, autolevel state,
│                    noise_floor_db, peqs, timestamps,
│                    input_device, mic_calibration public metadata,
│                    current_correction_at_start, sweep_meta,
│                    capture_quality, verify_quality (self-identifying
│                    reports with capture_kind / position_index /
│                    artifact_path),
│                    confidence_report, acoustic_quality summary,
│                    runtime_integrity summary,
│                    bundle_schema_version
├── artifact_manifest.json
│                    bundle schema v5 integrity manifest: relative
│                    paths, artifact kinds, schema versions, SHA-256,
│                    byte sizes, generator provenance, dependencies,
│                    sensitivity class, and recomputability
├── result.json      measured / target / predicted curves; verify_curve
│                    + verify_metrics when /verify ran; repeats
│                    input_device, mic_calibration public metadata,
│                    capture_quality, verify_quality, confidence_report,
│                    acoustic_quality summary, runtime_integrity summary
├── acoustic_quality.json
│                    SNR/acoustic trust summary derived from capture
│                    quality + browser-measured noise floor
├── runtime_integrity.json
│                    lightweight runtime snapshots/counters around
│                    measurement and verify sweeps
├── position_analysis.json
│                    per-position curves, spatial spread, confidence
│                    bands, high-variance/deep-null feature flags
├── captures/        per-position WAVs (p0.wav, p1.wav, ...)
├── noise/           pre-sweep silence WAVs (p0_pre.wav, ...)
├── repeat_captures/ optional same-seat repeat WAVs (p0_r1.wav, ...)
├── mic_calibration.json
│                    selected calibration public metadata + parsed curve
├── mic_calibration.txt
│                    exact uploaded/fetched calibration file used
├── verify.wav       single-position re-measurement (if /verify ran)
└── applied.yml      copy of the CamillaDSP config that was applied
                     (so the bundle is self-contained even if the
                     configs/ directory is later cleaned up)
```

`info.json` is rewritten atomically on each state transition and on
failed analysis paths (cheap; a few hundred bytes). `result.json`
lands after design / verify.
`applied.yml` is copied (not symlinked) in `apply()` so the bundle
remains valid after a user-driven cleanup.

### Durable evidence contract

Raw measurement recordings are the canonical origin for a correction
session. Keep `captures/p<N>.wav`, `noise/p<N>_pre.wav`,
`repeat_captures/p0_r<N>.wav`, and `verify.wav` by default and treat
them as private user data: they may contain room noise, speech, or
household sounds around the sweep. Derived artifacts should be
recomputable from:

- raw capture WAVs;
- exact sweep metadata;
- selected mic calibration file and parsed curve;
- browser audio-path report;
- correction algorithm settings and software/build provenance;
- target/strategy choices;
- runtime health snapshots taken around the sweep.

New bundles use schema v3 and write an `artifact_manifest.json` beside
`info.json`. Each artifact entry includes:

- relative path and artifact kind (`raw_capture`,
  `session_metadata`, `analysis_result`, `position_analysis`,
  `mic_calibration_raw`, `mic_calibration_metadata`,
  `camilladsp_config`, etc.);
- artifact schema/version where applicable;
- SHA-256 checksum and byte size;
- manifest record timestamp;
- generator provenance (module/function or tool name);
- input dependencies by artifact path;
- sensitivity class (`private_raw_audio`, `private_metadata`,
  `debug_safe`, etc.);
- whether the artifact is recomputable.

`jasper.correction.bundles.validate_bundle` now validates manifest
shape, missing files, size/checksum drift, missing dependency entries,
runtime-integrity issues, acoustic-quality issues, and current-schema
bundles that still rely only on filename conventions. Full SHA-256
re-verification of every artifact runs only on the on-demand forensic
CLI (`jasper-correction-bundle inspect`, i.e.
`validate_bundle(max_sha_verify_bytes=None)`); the default callers
(jasper-doctor, the evidence packet, and agent intake) skip the re-hash
for artifacts larger than `DEFAULT_MAX_SHA_VERIFY_BYTES` (1 MiB —
i.e. raw-capture WAVs) and gate those on byte-size drift instead, to
keep CPU/I/O bounded on the 1 GB Pi.

### Schema and version compatibility

The bundle contract is intentionally versioned without introducing
heavyweight JSON Schema yet. Consumers should branch on explicit
version fields and feature presence, not inferred filenames.

Current versions:

| Artifact | Version field | Current value | Compatibility expectation |
|---|---:|---:|---|
| `info.json` | `bundle_schema_version` | `5` | Required for bundle identity/state. New optional summaries may appear; older bundles may omit newer fields. |
| `result.json` | `bundle_schema_version` | `5` | Optional until a session reaches `ready` / `applied` / `verified`. Consumers must tolerate absence on failed or in-flight bundles. |
| `artifact_manifest.json` | `manifest_schema_version` | `1` | Required for new schema-v5 bundles. Legacy bundles without it may be inspected but are lower trust. |
| `runtime_integrity.json` | `artifact_schema_version` | `1` | Optional derived evidence. Missing means runtime evidence unavailable, not that the sweep was healthy. |
| `acoustic_quality.json` | `artifact_schema_version` | `1` | Optional derived evidence. Missing means SNR/repeatability evidence unavailable, not invalid. |
| `analysis/<capture>_response.json` | `artifact_schema_version` | `1` | Optional derived replay artifact. Recomputable from raw capture WAV, sweep metadata, calibration, and deconvolution settings. |
| `fir/<label>.json` | `artifact_schema_version` | `1` | Optional FIR-runtime metadata for imported/staged coefficients. This is evidence only, not an apply path. |
| `jasper.correction.evidence` packet | `artifact_schema_version` | `2` | Read-only review envelope for humans and future LLMs; no side effects and no raw audio. v2 adds `capability_permissions` and `missing_evidence`. |
| `jasper.calibration_agent.advisor_context` packet | `artifact_schema_version` | `1` | Redacted LLM-ready context envelope derived from the evidence packet. Excludes raw audio, absolute paths, raw serials, untrusted browser labels, and user-entered profile names; carries read-only-first bounded-action permissions/prohibitions. |
| `jasper.calibration_agent.prompt` package | `artifact_schema_version` | `1` | Provider-neutral prompt package for a future model call. Contains system instructions, response contract, and redacted advisor context; no model call and no side effects. |
| `jasper.calibration_agent.model_client` call | `artifact_schema_version` | `1` | Opt-in provider-call envelope for a candidate advisor response. Contains provider/model/status/elapsed-time/usage and parsed advisor JSON only; no raw provider text, no secrets, and no DSP side effects. |
| `jasper.calibration_agent.response` validation | `artifact_schema_version` | `1` | Deterministic validation envelope for future advisor JSON. Produces a safe action plan or rejects unsafe fields/actions; persistence remains user-confirmation-gated and model profile payloads are DSP-shape-only. |
| `jasper.calibration_agent.actions` run | `artifact_schema_version` | `1` | Deterministic run envelope for a validated advisor plan. Presentation actions can complete immediately; audition/commit actions require caller-owned executors and keep subjective listener judgment explicit. |

Compatibility rules:

- Treat `info.json` as the minimum bundle identity surface. It must
  contain `session_id`, `state`, and `bundle_schema_version` for a
  bundle to be useful.
- Treat `result.json`, `runtime_integrity.json`,
  `acoustic_quality.json`, `analysis/*`, and `fir/*` metadata as
  optional capability surfaces. Missing derived evidence should lower
  confidence and guide remeasurement, not crash report rendering.
- Treat `artifact_manifest.json` as the integrity surface for new
  bundles. If it is present, validate checksums, sizes, dependency
  paths, sensitivity classes, and artifact schema versions before
  trusting derived artifacts. The default `validate_bundle` path
  (doctor / evidence / agent intake) SHA-verifies only artifacts ≤
  `DEFAULT_MAX_SHA_VERIFY_BYTES` (1 MiB); large raw-capture WAVs are
  gated on byte-size there, with full hashing reserved for
  `jasper-correction-bundle inspect`.
- Do not expose raw WAVs in browser report surfaces. Raw recordings
  are private evidence and stay in `captures/`, `noise/`,
  `repeat_captures/`, and `verify.wav` for CLI/operator workflows.
- When bumping any version, keep the old reader path long enough for
  copied-off bundles from previous releases to produce a useful
  "limited evidence" report.

Replay-grade `analysis/` artifacts are deliberately compact and
file-based. They currently include per-capture deconvolved impulse
responses, window/deconvolution settings, magnitude response before
display smoothing, 1/48-octave smoothed response, calibration-applied
analysis curve, and the normalization band. Raw capture WAVs remain the
canonical origin. Future additions that require phase/group-delay or
complex transfer functions should extend this artifact family rather
than teaching browser routes to recompute DSP facts.

Runtime health is lightweight and bounded, not a new monitoring daemon.
`runtime_integrity.json` records a small per-measurement health packet:
monotonic/wall-clock timing, CPU/load and memory snapshots, CamillaDSP
config/status where available, fan-in xrun deltas, and capture
sample-count sanity. This feeds a separate **runtime integrity**
verdict, distinct from **capture quality** and **acoustic quality**.
Hard capture failures such as clipping, sample-rate mismatch, or
too-short WAV still block analysis; runtime warnings lower confidence
unless they directly prove the recording is invalid.

`acoustic_quality.json` is the current acoustic trust layer. It records
capture waveform summaries, pre-sweep room-noise summaries from
`noise/p<N>_pre.wav`, estimated broadband and modal-band dBFS SNR,
direct-arrival/pre-arrival evidence, and optional same-seat repeat
quality from `repeat_captures/p0_r1.wav`. This is a useful engineering
guardrail, not calibrated acoustic SPL. Low SNR or weak repeatability
lowers confidence and blocks assertive strategy recommendations;
missing evidence keeps read-only review possible but should not unlock
stronger FIR/agent claims. Future hardware validation should tune the
thresholds against real phone/mic/Pi captures.

Do not introduce a database, unbounded recording retention, or
continuous telemetry for this phase. The browser history panel may
delete old filesystem bundles and clearly labels raw recordings as
private. Keep anything heavier, such as pinning, automatic pruning, or
debug-safe export/share workflows, out of the product until hardware
usage proves they carry their weight.

To pull a bundle off the Pi for debugging:

```sh
ssh pi@jts.local 'ls -t /var/lib/jasper/correction/sessions/ | head -5'
scp -r pi@jts.local:/var/lib/jasper/correction/sessions/<id> ./
```

To list bundles without ssh, hit the debug endpoint:

```sh
curl -sk https://jts.local/correction/sessions | jq
```

To view one read-only browser-safe evidence report:

```sh
curl -sk 'https://jts.local/correction/session-report?id=<id>' | jq
```

To inspect, replay, or export one copied bundle locally:

```sh
jasper-correction-bundle inspect ./<session_id> --recompute
jasper-correction-bundle export ./<session_id> --output ./rew-export
```

To inspect a FIR coefficient WAV without applying it, or stage a safe
imported FIR into a copied bundle as evidence:

```sh
jasper-correction-bundle fir-inspect ./coefficients.wav --mode minimum_phase
jasper-correction-bundle fir-stage ./<session_id> ./coefficients.wav \
  --label imported-minphase --mode minimum_phase
```

The export command writes:

- `<session_id>-measured.frd`, `target.frd`, `predicted.frd`, and
  `verify.frd` where available, with `Hz dB phase` columns. Because
  current bundles store smoothed magnitude curves rather than acoustic
  phase, the phase column is `0.0` and the header says so.
- matching `.txt` files with `Hz dB` columns;
- `<session_id>-p<N>-ir.wav` and `<session_id>-verify-ir.wav` impulse
  responses recomputed from raw captures and exact sweep metadata.

Default ON. Opt out via `JASPER_CORRECTION_SAVE_BUNDLES=0` in
`/etc/jasper/jasper.env` — captures then fall back to the legacy
flat `captures/` directory and no per-session artifacts are written.

## Known limitations / Phase 3+ refinements

- **Window cycles per position.** Each /start, /next-position,
  /verify call opens a fresh measurement_window — renderers
  pause/restart per sweep. ~3-5 s of dead air per position
  transition. Tolerable but not great. Phase 3 could keep the
  window open across multi-position runs (~30 LOC of additional
  state in the session).
- **Strict Schroeder-aware spatial averaging.** Phase 2 does
  power-mean everywhere. Strict implementation would do vector-mean
  below ~350 Hz (preserves phase) and power-mean above. Requires
  keeping complex H(f) per position rather than just magnitude_db
  — a refactor of the analysis pipeline.
- **Built-in phone mic compensation curve.** Not bundled; if the user
  measures with the phone's internal mic, the captured response still
  includes that mic's frequency response. Phase 2.3 adds external
  calibrated-mic ingest and manual calibration upload, but a built-in
  iPhone/Android compensation database is still not shipped. Treat
  phone-internal captures as quick checks, not the trust floor for
  future FIR or agent recommendations.
- **Vendor lookup must fall back cleanly.** Dayton and miniDSP serial
  lookup is intentionally behind provider adapters. If a vendor form
  changes, blocks server-side fetches, or a serial cannot be found,
  the UI should keep the user in flow via manual calibration upload.
- **Concurrent /start protection.** Resolved 2026-05-28. The backend
  atomically reserves `/start` before replacing the session, so a
  second handcrafted request is refused even in the narrow gap before
  the first background sweep task has visibly moved the new session out
  of `IDLE`. It also refuses `/start` while an existing session is
  preparing, sweeping, awaiting capture, awaiting next position,
  analyzing, or verifying.
- **SPL-calibrated room-noise check.** Phase 2.4 catches obviously
  weak captures and logs low RMS / low peak warnings, but it is not
  a calibrated ambient SPL measurement. A loud room can still pass
  the gate if the sweep level is also high; future agent/FIR flows
  should treat `capture_quality` as a floor, not a lab-grade SNR
  proof.

## Cross-session notes

If you're a future Claude or future Jasper picking this up:

- **Read this doc top to bottom first.** The architecture decisions
  encode reasons that would otherwise get re-litigated.
- **Look at the actual codebase before changing the file map.**
  This doc was written after a careful read of [voice_setup.py](../jasper/web/voice_setup.py),
  [_common.py](../jasper/web/_common.py), [control/server.py](../jasper/control/server.py),
  [audio-paths.md](audio-paths.md),
  [outputd-cutover.yml](../deploy/camilladsp/outputd-cutover.yml),
  [camilla.py](../jasper/camilla.py), and [nginx-jasper.conf](../deploy/nginx-jasper.conf).
  If your read disagrees with something here, the code is right
  and this doc is stale — fix the doc.
- **The `master_gain` mixer is the EQ slot.** Don't replace it;
  insert filters in front of it.
- **Phase order matters.** Phase 0 (TLS) is a hard prereq for
  everything else; getUserMedia won't run without it. Phases 1-2
  are the demo. Phases 3-5 are polish.
- **PR per phase.** Don't bundle.
- **Don't introduce FastAPI.** If you find yourself reaching for
  it, re-read Decision 2.
- **Don't add a top-level coordinator daemon.** Re-read Decision 4.
- **The brief and sanity-check pass are inputs, not specs.** This
  doc is the spec. Where they diverge, this doc wins.

## References

External:
- [SciPy](https://scipy.org/) — shipped correction implementation
  uses NumPy/SciPy primitives for sweep/deconvolution/filter design.
- [pyfar](https://pyfar.org) / [pyrato](https://pyrato.readthedocs.io)
  — useful room-acoustics references for future RT60 / Schroeder work;
  not runtime dependencies today.
- [camillagui-backend](https://github.com/HEnquist/camillagui-backend)
  — power-user pass-through (Phase 3).
- [CamillaFIR](https://github.com/VilhoValittu/CamillaFIR) —
  possible FIR / FDW workflow reference; verify current status before
  relying on it for Phase 5.
- [REW](https://roomeqwizard.com) — `.frd` format reference, REW
  YAML export workflow.
- [HouseCurve file formats](https://housecurve.com/docs/manual/file_formats)
  — practical frequency/dB calibration-curve text format.
- [Dayton Audio microphone calibration tool](https://support.daytonaudio.com/MicrophoneCalibrationTool)
  — serial lookup source for Dayton Audio calibration files.
- [miniDSP UMIK-1](https://www.minidsp.com/products/acoustic-measurement/umik-1?format=pdf&type=raw)
  and [UMIK-2 manual](https://www.minidsp.com/images/documents/miniDSP%20UMIK-2-User%20Manual.pdf)
  — serial lookup and orientation-specific calibration-file references.
- Novak et al. 2015, "Synchronized Swept-Sine: Theory, Application,
  and Implementation," J. Audio Eng. Soc. 61.
- Olive 2013, AES 8994 "Listener Preferences for In-Room Loudspeaker
  and Headphone Target Responses."
- Toole, *Sound Reproduction*, 3rd ed.

Internal:
- [README.md](../README.md) — speaker hardware + architecture.
- [PLAN.md](../PLAN.md) — v2 priority context.
- [docs/audio-paths.md](audio-paths.md) — sweep injection point.
- [docs/HANDOFF-volume.md](HANDOFF-volume.md) — VolumeCoordinator
  pattern to mirror.
- [docs/HANDOFF-aec.md](HANDOFF-aec.md) — AEC bridge interaction risk.
- [jasper/camilla.py](../jasper/camilla.py) — CamillaController to
  extend.
- [jasper/web/voice_setup.py](../jasper/web/voice_setup.py) — web
  wizard pattern to mirror.
- [jasper/control/server.py](../jasper/control/server.py) — UDS
  coordinator pattern to mirror.

---

Last verified: 2026-07-11 (driver-specific crossover level sequence,
authenticated capture-page protocol v2 control data, and the placement/
comparison-set contract reviewed against the relay, persistence, envelope, and
baseline-apply paths; prior JTS3 UMIK-2 on-device level-ramp evidence,
renewable measurement-scoped voice-reconciler guard, and the 12 dB / −3 dB
dynamic-cap defaults; prior 2026-07-10 Jasper relay
room/crossover sequential flow,
ambient-baselined automatic level, exact bounded gain leases, mic/calibration
binding, safe applied manual-or-automatic Layer-A room prerequisite, explicit
automatic replacement of manual pins, capture-page compatibility handshake,
privacy-bounded setup reuse, and immutable applied Layer-A
recomposition; prior 2026-07-07 phone-mic relay guided-setup preflight:
room-sweep specs set `setup_validation=true`, the capture page posts setup
validation requests before showing Start, and `jasper-correction-web` answers
via `host_event.phase="setup_validated"` / `"setup_validation_failed"`;
phone-mic relay config fallback: blank legacy `JASPER_CAPTURE_RELAY_BASE` /
`JASPER_CAPTURE_ORIGIN` values migrate to the public relay defaults on
install/update, explicit `disabled`/`off`/`0`/`none` keeps the old local HTTPS
path; verified against
`capture-page/js/main.js`, `jasper/capture_relay/spec.py`,
`jasper/capture_relay/session.py`, `jasper/web/correction_setup.py`,
`deploy/lib/install/python-runtime.sh`, `jasper/capture_relay/health.py`,
`jasper/control/state_aggregate.py`, and live `http://jts.local/correction/room/`
rendering `data-capture-relay-enabled="1"`).
Prior 2026-07-06 (P6: the three tuning-LLM POST routes —
`/interpret`, `/propose`, `/propose/apply` — added to the route table,
verified against `jasper/web/correction_setup.py`'s `_POST_ROUTES` +
handlers; the two paid routes are now spend-cap gated → 429 at the household
daily cap, per the tuning-spend ledger; design canon for that surface is
HANDOFF-calibration-agent.md "The P6 tuning surface" / "Cost discipline"). Prior 2026-07-03 (P7: crossover relay transport
`/crossover/relay-capture`, `crossover_sweep` stimulus-length alignment to
`driver_acoustics.DEFAULT_DURATION_S`, `run_level_match` lock/cancel
retention seam, `/crossover/envelope` + passive `active` gate — all
hardware-free, H2 pending; verified against
`jasper/web/correction_crossover_flow.py`,
`jasper/active_speaker/crossover_envelope.py`, and
`jasper/capture_relay/spec.py`). Prior 2026-06-26 (`/correction/` hub
routing, HTTP preflight `?next=/correction/...` allowlist + JS direct HTTPS
handoff with fresh `jts_cb` tokens, `/correction/proceed` temporary strongly
non-cacheable fallback redirects, and HTTPS asset serving rechecked
against `deploy/correction-preflight.html`, `deploy/nginx-jasper.conf`,
`deploy/nginx-jasper-streambox.conf`, and `tests/test_landing_page_html.py`;
prior 2026-06-24 pass covered
topology-preserving correction start/apply/reset behavior,
correction-native active-crossover playback/capture routing, and the mapped
runtime files listed in that verification pass; prior 2026-06-18 pass covered
topology-safe correction reset/start behavior; prior 2026-06-17 pass covered
auto-level controller ownership, state guards, and status/bundle payload
ownership; prior 2026-06-15 pass covered bonded-follower delegation.)
