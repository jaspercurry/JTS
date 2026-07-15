# HANDOFF: active speaker DSP commissioning

> **Status: planning baseline.** Created 2026-05-25 from three local
> deep-research reports on DIY DSP speaker commissioning; updated
> 2026-05-26 with the proposal-v3 active speaker commissioning
> methodology. This is the canonical handoff for JTS speakers where
> CamillaDSP directly drives woofer, midrange, and/or tweeter
> amplifier channels. JTS3 is currently using the active-speaker
> baseline path on a HiFiBerry DAC8x; other production hardware may
> still use the stereo passthrough path.

> Product behavior, manual-versus-measured parameter ownership, the guided user
> journey, and delivery acceptance criteria are canonical in
> [`active-crossover-information-design.md`](active-crossover-information-design.md).
> This handoff owns the lower-level DSP, topology, and hardware-safety contracts.

> **Implementation status, 2026-06-03:** A0 schema substrate has
> started. `jasper.active_speaker` now defines import-cheap,
> side-effect-free preset, channel-map, safety-envelope, crossover
> region, and speaker-baseline profile models with validation and
> tests. A1 template work has also started:
> `jasper.active_speaker.camilla_yaml.emit_active_speaker_startup_config`
> emits muted/protected CamillaDSP startup templates with explicit
> active-hardware playback device input, `volume_limit: 0.0`, startup
> headroom, tweeter protective HP, per-driver mute, and per-driver
> limiter chains. `/sound/` has a collapsed **Speaker setup** entry point
> with one primary **Active crossover setup** walkthrough over
> `/sound/output-topology`. The UI renders detected physical outputs,
> speaker groups, assigned/unassigned lanes, safety evidence, and no-audio
> setup templates for mono/stereo passive, mono/stereo active 2-way, and
> mono/stereo active 3-way wiring. The active walkthrough step labels are **Choose speaker
> layout**, **Add driver and crossover values**, **Confirm outputs**, **Test
> each driver**, and **Validate and apply**. Subwoofer is an optional add-on to the
> current draft rather than a duplicated template matrix: when an unused
> physical output exists, the UI adds one `subwoofer` group and records it
> in `routing.subwoofer_group_ids`. Saving that speaker layout only persists
> the output topology JSON and runs backend validation; it does not load
> CamillaDSP or emit sound. The saved topology may describe more physical DAC
> outputs than the current protected active-speaker runtime can test/apply:
> `/sound/output-topology` also publishes
> `jts_active_speaker_playback_route_capability`, which separates physical DAC
> output count from active outputd route width. As of 2026-06-15, the product
> outputd route is a daemon-owned lane, not an audible test-tone writer. The
> topology can still prepare bounded no-audio diagnostic WAV artifacts for
> mono active 2-way, mono active 3-way, and stereo active 2-way layouts;
> stereo active 3-way and active subwoofer add-ons remain modeled but are
> disabled/blocked until outputd, staging, baseline compilation, and tests are
> widened together. The UI organizes this work as collapsible task
> cards — choose layout, add driver and crossover values, confirm outputs, test drivers,
> validate the summed crossover, then save/apply the active profile. It
> defaults to the first unfinished task card,
> keeps one task card open at a time, prevents opening future prerequisite-gated
> cards, and uses only transient browser intent when the operator advances or
> reopens a card; it does not create a separate persisted wizard-progress source
> of truth, and earlier cards remain editable. The driver-test card has one
> product audio path: active 2/3-way groups use the single-audio-path commission
> routes (`commission-load`, `commission-ramp-step`, `commission-ramp-ack`,
> `commission-ramp-abort`). Passive/full-range groups render a "No active driver
> test" card and continue through the normal listening path; there is no
> separate direct-DAC driver test in the product UI. The old public setup probes
> (`arm`, `playback-readiness`, `tone-targets`, `tone-plan`) and their
> superseded per-driver planners have been removed. Per-driver test planning is
> commission-ramp-owned; the summed topology planner remains only for the live
> combined-crossover test.
> `/sound/active-speaker/channel-identity` now exposes and updates
> operator-confirmed physical channel identity evidence for the saved
> topology. The Confirm outputs UI can run a guarded quiet **Play** audition for
> an assigned driver before identity is confirmed; the backend treats that as
> identity-audition mode and still requires the saved topology, staged protected
> config, software guards, path-safety evidence, calibration floor, Stop/session
> control, and CamillaDSP rollback gates. Marking or clearing identity evidence
> still does not grant playback permission; tweeter protection and later path
> safety remain separate blockers.
> The bottom **Reset speaker setup** recovery action posts to
> `/sound/output-topology/reset`: it stops any active-speaker tone/session,
> resets the saved topology to the detected passive hardware draft, kicks
> audio-hardware reconcile, and clears active-speaker setup/evidence JSON
> artifacts (design draft, crossover preview, staged config, path safety,
> startup/commission/ramp state, measurements, and baseline candidate). It does
> not play sound and does not delete generated CamillaDSP YAML files.
> `jasper.active_speaker.safe_playback` remains the backend confirmation ledger
> and Stop substrate, but the browser no longer exposes a separate Arm step.
> Active crossover commissioning arms through `commission-load` and uses the
> protected active graph for every audible driver step. `jasper.active_speaker.playback`
> now owns only no-audio artifacts and an explicit lab `aplay` hook for
> experiments; it is not a product direct-DAC writer and does not touch live
> volume.
> Lab audible tests are explicit-only: lab builds must set
> `JASPER_AUDIO_LAB_TONE_BACKEND=aplay` and
> `JASPER_AUDIO_LAB_TEST_PCM` to a dedicated test PCM that is not a
> daemon-owned CamillaDSP/outputd lane. `outputd_active_content_playback`,
> `outputd_content_capture`, `outputd_active_content_capture`, `outputd_dac`,
> and `jasper_out` are forbidden as test writers. Even then, lab audible tests
> are clamped, selected-topology-gated, driver-protection-gated, and
> Stop/session-gated. Product audible commissioning never writes directly to a
> physical DAC PCM.
> The shared
> `driver_protection_auto_level_v1` policy is recorded in driver-test signal
> plans, commission evidence, playback results, and generated artifact metadata. Woofer, mid,
> and subwoofer targets use the normal commissioning envelope. Tweeter/
> high-frequency targets can become eligible only when a protection profile is
> accepted (`present` or `software_guard_requested`), the tone has the required
> high-pass, and the driver-specific auto-level cap is respected. The playback
> backend recomputes the protection envelope from code-owned policy and refuses
> missing high-pass evidence or plan-provided cap/allow overrides. The backend
> also enforces its own artifact envelope (48 kHz max, 64 channels max,
> 100-500 ms, -80..-30 dBFS) and keeps a small rolling set of generated
> artifacts so `/var/lib/jasper` cannot grow without bound during repeated
> checks. The safe-session state now owns a quiet-start lifecycle:
> every armed session starts in `floor_required`; an audible test above the
> calibration floor is rejected until the same session and target has a
> successful floor-level audible result; Stop, expiry, or target change resets
> the lifecycle back to `floor_required`.
> The product flow is deliberately narrow: active crossover groups use the
> commission ramp; passive/full-range groups have no separate active driver
> test. The sound-producing product boundary is the commission ramp, which
> revalidates the selected topology target, route policy, driver protection
> policy, calibration bounds, and protected active graph state. The old
> readiness report and per-driver topology-tone planner are deleted rather than
> retained as a second, non-authoritative safety checklist.
> A stored startup-load state with an explicit current-config mismatch still
> fails closed; a freshly loaded state without that optional live-match field is
> accepted so the UI does not dead-end after the protected setup just succeeded.
> Tweeter/high-frequency targets are governed by the shared driver-test signal
> policy plus the protected commissioning graph; there is no separate
> readiness-packet authority. The backend also exposes a read-only
> **Commissioning view** from
> `jasper.active_speaker.commissioning_coordinator` and
> `/sound/active-speaker/commissioning-view` (the surface the `/sound/` UI
> fetches). It composes the durable setup state from saved speaker layout
> through safe-session/floor readiness into one UI view model without
> playing sound, reloading CamillaDSP, or storing wizard progress; target
> selection, artifact verification, and floor-audio confirmation remain
> explicit operator-selected actions.
> `/sound/active-speaker/driver-measurement` and
> `/sound/active-speaker/summed-validation` (by-ear confirmations), plus the
> HTTPS browser mic-capture path `/correction/crossover/` driver-capture
> (`correction_crossover_backend` →
> `web_measurement.record_driver_capture`), persist
> the first product-grade measurement evidence through
> `jasper.active_speaker.measurement` at
> `/var/lib/jasper/active_speaker_measurements.json` with kind
> `jts_active_speaker_measurements`. (The old `/sound/active-speaker/driver-capture`
> + `/summed-capture` mic routes were a verbatim duplicate of the
> `web_measurement` capture path that nothing reached after the move to
> `/correction/`; they were deleted — Codex-week review C4a-1. `/sound/` is plain
> HTTP and cannot `getUserMedia`.) As of P7 (2026-07-03), extended by the
> repeat/SNR controller on 2026-07-12, driver captures can also ride the
> **phone-mic relay transport**:
> `POST /correction/crossover/relay-capture` (the third `RelayCaptureKind`
> caller) plays the same capture sweep on `armed` and feeds the verified WAV into
> `record_driver_capture` analysis. The Jasper relay is the normal product
> transport; explicitly disabling `JASPER_CAPTURE_RELAY_BASE` retains the local
> fallback. It reads the
> play payload's real shape (`status` + nested `playback.audio_emitted`,
> top-level `test_level_dbfs`/`sweep_meta`) and refuses while room/balance/sync
> is active (server-computed at POST, re-checked when the phone arms). The
> isolated-driver playback leg now uses
> `active_speaker.commissioning_admission`: one bounded Shared writer lock spans
> transient graph load, fresh graph/volume proof (including the exact admitted
> per-output commissioning gain and a graph ceiling at the locked listening
> volume), unique persisted generation and playback
> admission, a cancellation-safe profile cooldown bounded to five seconds so
> ambient + the longest sweep + graph/relay work fit the phone deadline,
> exact role-bounded WAV playback, a post-play volume-drift refusal, and exact
> restore.
> The Crossover page now keeps a visible Stop for both relay level and sweep
> runs (`POST /correction/crossover/relay-cancel`). Stop cooperatively signals
> the exact relay owner, holds status at `stopping`, and withholds another
> action until player reap, graph/volume restoration, and relay cleanup finish;
> then it publishes `stopped`. A restored level ramp enters non-stoppable
> `committing` directly. A sweep first enters non-stoppable `finishing` for
> phone upload, then `committing` for evidence persistence. Explicit Stop is
> cancellation, not a failure-cue event, and the phone renders it as such. The
> exact boundary and low-level lifecycle are canonical in
> [phone-mic-relay-plan.md](phone-mic-relay-plan.md).
> The returned playback-role handoff is a server-only argument to capture
> persistence; browser JSON cannot mint it. Existing bundles without a Shared
> authority marker remain historical. Legacy browser/direct combined capture is
> refused before graph load with
> `active_summed_persisted_admission_unavailable`; its legacy evidence cannot be
> promoted. The production relay's `kind=summed` branch is different: it accepts
> no browser DSP fields, persists an explicit signed per-region geometry
> attestation, validates the comparison-set calibration and recorder hash, and
> supplies the real WAV to the typed internal host. That host alone owns the
> normal/reverse/delay operation, transient graph, admission, attempt, ordinal,
> commit, and exact restore. The
> `crossover_sweep` capture spec's stimulus length derives from the protected
> per-driver signal plan (12 s woofer/subwoofer, 8 s midrange, 4 s tweeter;
> one sweep definition; the deconv
> reference is always regenerated from the played `sweep_meta`, so the phone is a
> pure recorder). Each driver recording includes a 14-second controlled quiet
> interval before playback; a signal locator excludes pre-armed audio. The
> phone's hard deadline is 45 s and the Pi's `sweep_complete` event
> remains normal recorder completion. The safe probe owns only non-clipping level. The
> deconvolved per-band sweep-versus-ambient verdict and the server-owned
> three-repeat aggregator own evidence admission; one bounded fourth attempt is
> allowed, and a durable measurement is written only after at least two repeats
> pass. The former raw-WAV `/crossover/driver-capture` route was deleted because
> it had no product caller and could not satisfy either contract; relay capture
> is the single driver-evidence ingress. `GET /correction/crossover/envelope`
> (`active_speaker/crossover_envelope.py`) is a pure sequential screen envelope:
> protected speaker setup → mic/calibration + one automatic near-field level per
> driver → each driver's stationary near-field repeat sequence → keep the mic
> fixed on the tweeter reference axis while each driver gets a separate safe
> level and a target of three gated repeats → attest signed path geometry → run
> server-selected normal/reverse/bounded-delay combined captures → evidence
> complete. Candidate apply/verification remain later slices. One bounded
> fourth attempt may
> replace a rejected capture, but automatic
> apply still requires three accepted repeats for both geometries. The lower
> kernel can retain a two-accepted reduced-confidence aggregate for diagnosis;
> it is not apply-eligible. The
> browser has no second measurement
> state machine or local recorder; passive
> (`full_range_passive`) speakers get `active=False` (no driver/summed targets),
> so Layer A stays hidden. The acoustic proof (real-driver sweep + phone
> `getUserMedia`/CSP path) is parked as H2. Driver evidence is bound to the current
> saved physical target fingerprint: topology id, detected hardware, active
> speaker group/mode, driver role, assigned DAC output, and current identity
> confirmation. The mic-capture path accepts a bounded browser WAV upload (or a
> local path inside the active-speaker capture store for tests/server-side
> capture), prunes the raw WAV store by count and bytes as new captures arrive,
> analyzes it through `commissioning_capture.record_driver_acoustic_capture`,
> and records the real `driver_acoustics` verdict block. A `present` verdict
> still requires a matching
> accepted floor-level safe-playback result and non-clipping mic evidence before
> it counts as captured; unusable/clipped captures record nothing and leave the
> baseline locked. If the saved speaker layout or output assignment changes, old
> records stay in the file for audit but stop counting toward readiness. Summed
> validation is speaker-group-specific and bound to the current set of driver
> target fingerprints; the product success path requires a current audible
> combined-driver test and an explicit result for that same test. A phone-mic
> summed capture analyzed through
> `commissioning_capture.record_summed_acoustic_capture` records richer acoustic
> evidence when available, but the household flow can also accept
> `operator_listening_check` for **Sounds right** after an audible combined
> test. Artifact-only tests, stale test ids, or free-floating "sounds good"
> clicks still cannot unlock the active profile. As of 2026-06-23, `/sound/`
> does not expose the browser mic-capture buttons; mic-backed crossover leveling
> should live in the HTTPS measurement/correction experience. The UI presents
> this as the next human task after confirming outputs: test each driver by ear,
> run the combined test at a bounded selectable level, record what the operator
> heard, then save/apply the active profile when backend permissions allow it.
> `/sound/` also includes manual crossover settings for active-crossover
> planning. The visible fields are the product source of truth: driver
> names, sensitivity, safe low test limits, per-driver level trim, and active
> crossover point/filter/slope. The optional AI helper generates a prompt
> from the current output roles and accepts a pasted JSON object with kind
> `jts_active_crossover_driver_research`, but using that helper only fills
> visible values for operator review; saving never lets hidden imported JSON
> overwrite user-edited fields.
> `/sound/active-speaker/design-draft` persists those operator-entered driver
> names, manual crossover settings, notes, the bounded research JSON when
> present, and a snapshot of the saved output topology as
> `/var/lib/jasper/active_speaker_design_draft.json` with kind
> `jts_active_speaker_design_draft`. The draft is intentionally
> non-authoritative: it does not emit sound, load CamillaDSP, authorize
> playback, trust imported research as measurement evidence, translate it into
> filters, or apply anything. Its purpose is to carry the user's intended
> build and visible starting points into the crossover compiler/review
> step without making the LLM or browser rummage through topology internals.
> `jasper.active_speaker.crossover_preview` and
> `/sound/active-speaker/crossover-preview` now turn that saved design draft
> into a persisted, versioned, no-audio crossover preview at
> `/var/lib/jasper/active_speaker_crossover_preview.json` with kind
> `jts_active_speaker_crossover_preview`. The preview proposes bounded
> low-pass/high-pass filter intent for active 2-way and 3-way speaker groups,
> raises a candidate up to the upper driver's recommended-highpass / usable-range
> soft floor (`_upper_recommended_floor`) with a warning, and — fail closed — **blocks**
> a crossover sitting at or below the upper driver's `do_not_test_below_hz`
> protection line (`crossover_below_do_not_test_floor`), dropping its filter
> intent so a compression/horn driver is never crossed on or under its
> do-not-test floor,
> prefers operator-entered manual settings over imported research when both are
> present, surfaces missing crossover settings and low-confidence candidates as
> evidence, and records whether a later protected-staging step may consume it.
> It now also
> carries the curated driver facts needed to compile a protected staging preset
> without re-parsing the design draft or trusting browser internals. The preview
> source carries a design-draft fingerprint; loading the saved preview against
> the current draft marks it `stale` and clears
> `may_prepare_protected_startup_config` if the operator changes topology,
> driver research, or operator inputs after preparation. It still does not emit
> CamillaDSP YAML, load CamillaDSP, apply filters, authorize playback, or treat
> external research as measurement truth.
> `jasper.active_speaker.baseline_profile` and
> `/sound/active-speaker/baseline-profile` now compile that saved topology,
> design draft, fresh/ready crossover preview, driver-test evidence, and summed
> validation into a durable active-speaker baseline candidate at
> `/var/lib/camilladsp/configs/active_speaker_baseline.yml`. Compilation is
> explicit and no-audio: it writes YAML plus
> `/var/lib/jasper/active_speaker_baseline_profile.json`, but does not load
> CamillaDSP. If a baseline is already applied, the new candidate is written to
> a content-addressed sibling instead of overwriting the running/statefile-owned
> config; the state retains one small `applied_recomposition_profile` anchor
> until the explicit apply succeeds. The emitter
> (`jasper.active_speaker.camilla_yaml.emit_active_speaker_baseline_config`)
> requires an explicit active playback device, keeps `devices.volume_limit`
> non-positive, emits `active_baseline_headroom` at `0.0 dB` by default,
> keeps per-driver limiters, rejects positive correction gain, bounds
> delay/polarity corrections, and records a source comment in the YAML. The
> active preference-EQ path keeps boosts at unity, matching the ordinary
> `/sound` path; explicit output trim or match-loudness attenuation is the
> only preference-layer global attenuation folded into
> `active_baseline_headroom`.
> Per-driver gain provenance is explicit: `operator_pinned` wins, while a
> `research_estimate` or UI `sensitivity_estimate` is only a provisional
> starting value that a comparable acoustic measurement supersedes. Legacy
> manual values without provenance migrate conservatively as `operator_pinned`.
> When research gives no estimate but declares sensitivities,
> `_derive_corrections` fail-safes by attenuating the hotter drivers down to the
> least-sensitive (reference) driver by the sensitivity gap (e.g. a 108.5 dB horn
> next to an 83.3 dB woofer is trimmed −25.2 dB) so a high-sensitivity
> compression driver never compiles at full level relative to the woofer; the
> trim is surfaced as `driver_gain_derived_from_sensitivity` and measurement
> refines it later. Summed validation must reference the latest
> combined-driver test record for the same speaker-group fingerprint; artifact
> generation alone is not enough to unlock the durable baseline because the
> accepted result must come from an audible combined-driver test plus either
> mic-backed evidence or an explicit operator listening check.
> `/sound/active-speaker/baseline-profile/apply`
> is the first user-facing "this is now your active speaker profile" step, but
> it is currently enabled only when the generated baseline targets an
> outputd-owned active lane. Today that product handoff is declared by profiles
> with an outputd active lane; layouts without such a lane remain modeled but
> fail closed for active driver commissioning and durable baseline apply. Ready
> candidates apply through the
> shared `dsp_apply` transaction and record applied
> state. Optional subwoofer groups and multi-group group-specific delay
> correction still fail closed or warn until later slices add a verified
> compiler path for them (superseded for the manual region-level
> polarity/delay case — see the 2026-07-11 update below).
> The clock-domain gate now distinguishes the normal single-device
> path from the dual-Apple USB-C DAC 4-channel pair. The latter is the
> `dual_apple_usb_c_dac_4ch` hardware profile: exactly two Apple child
> DACs, one speaker-local stereo pair per DAC, four physical outputs total,
> and current reconciler observation showing the two children on the expected
> same USB controller/bus. Stored 900 s common-clock drift evidence is
> validation evidence, not the only source of hardware identity; missing
> evidence is surfaced as a warning, while failed evidence blocks. Missing
> or partial live hardware observation blocks. This does not authorize sound
> by itself and does not permit generic ALSA/CamillaDSP multi-device
> aggregation.
> `jasper.active_speaker.staging` now provides protected startup staging for
> saved active-speaker designs. The product route
> `/sound/active-speaker/stage-config` consumes the current saved design draft
> plus a fresh `jts_active_speaker_crossover_preview`; stale, missing, blocked,
> or topology-mismatched previews block staging. Preview-derived staging can
> compile mono or stereo active 2-way and 3-way main speaker groups from the
> saved topology, including saved role/output mapping, while still requiring the
> active outputs to occupy a contiguous block starting at DAC output 1. A routed
> local subwoofer is now armed into the protected startup graph alongside the
> mains: its output is band-limited (LR4 low-pass at the bass-management corner),
> excursion-limited, and starts MUTED via the per-output commission mask — exactly
> like a driver — while each main's lowest driver carries the complementary LR4
> high-pass. A sub that cannot be pinned to the next contiguous output after the
> mains fails closed (`active_subwoofer_output_not_contiguous` /
> `subwoofer_staging_unresolved`); staging never silently drops the sub and stages
> a mains-only graph. The packaged
> Epique/F110M preset remains the no-audio fallback substrate for lower-level
> tests/CLI/default-preset work, not the product route's implicit answer once a
> design draft exists. `/sound/active-speaker/channel-protection`
> records either physical compression-driver protection evidence or a
> software-guarded bring-up request. The normal UI path does not expose this as
> a separate "protection" choice; after the operator confirms a high-frequency
> output and then confirms that named driver in the Confirm outputs card, the
> page records the software-guard request internally before checking readiness. The
> software-guard state is
> deliberately still a topology/playback blocker; it only lets
> `/sound/active-speaker/stage-config` write a no-load muted/protected
> CamillaDSP candidate plus
> `/var/lib/jasper/active_speaker_staged_config.json` evidence. That route
> refuses missing guard intent, non-contiguous output assignments, and missing
> active playback route evidence. Registered DAC profiles that support the
> active outputd lane resolve staging to that lane; staging does not silently
> default to `hw:<card>,0` on outputd-owned hardware. Route capacity is checked
> separately from physical DAC output count, so layouts that require an
> assigned active output beyond the current active outputd lane fail closed
> before protected startup YAML or baseline YAML can be consumed for playback.
> For software-guarded bring-up it
> also records evidence that the generated graph contains startup mute,
> protective high-pass, startup headroom, limiter, and no-load/no-playback
> guarantees. It does not load the config, reload CamillaDSP, emit sound, or
> grant playback permission.
> `jasper.active_speaker.runtime_contract` is the runtime safety boundary
> for saved roleful topologies. `jasper.output_topology` remains the
> declarative source of truth for which physical DAC output is a woofer,
> midrange, tweeter, full-range driver, or subwoofer; `runtime_contract`
> classifies that topology against candidate/running CamillaDSP YAML and is
> the shared owner for safe fallback selection. Flat
> `/etc/camilladsp/outputd-cutover.yml` and `/etc/camilladsp/v1.yml` are
> normal full-range graphs only. Once the saved topology assigns any DAC
> output to `tweeter`, any `protection_required=true` role, or a subwoofer
> roleful output, flat full-range fallback is illegal. Deploy/install must
> call `jasper-active-speaker runtime-safe-graph` instead of writing the
> outputd statefile itself: unconfigured or explicit stereo full-range layouts
> can select the flat outputd graph, while active/protected layouts must
> preserve or select a validated all-muted active startup graph. Guarded
> commissioning graphs may be legal for an active test session, but they are
> not legal persisted deploy/restart fallbacks. Explicit mono full-range
> layouts must not be driven by a wider flat stereo graph. If no legal graph
> matches the saved topology/guard evidence, the result is fail-closed rather
> than silently restoring flat stereo. `jasper-doctor` uses the same classifier
> and fails when a saved tweeter/protected topology is running a flat
> full-range graph. Correction start/apply/reset paths ask
> `jasper.correction.runtime_safety`, which delegates roleful graph policy back
> to this runtime contract.
> **Recovery — drift back to a passive speaker.** A saved topology can drift
> from physical reality: e.g. a physically passive single-DAC box left carrying
> a leftover `active_2_way` (roleful/tweeter-protected) topology from an old
> experiment. That stale topology makes this fail-closed gate correctly refuse a
> flat graph and can BLOCK a deploy at the install-time outputd-statefile
> contract check. The blessed one-command fix is
> `sudo /opt/jasper/.venv/bin/jasper-output-topology-reset` (add `--yes` for
> non-interactive use, `--dry-run` to preview). It rewrites
> `/var/lib/jasper/output_topology.json` to a clean passive draft built by
> `output_topology.new_topology_draft()` from the **detected** hardware
> (`speaker_groups=[]` → `requires_roleful_graph` false), then kicks
> `jasper-audio-hardware-reconcile` so the running graph converges to the
> flat/passive path — leaving a consistent passive-topology + flat-graph box the
> L0 gate accepts. It uses only the supported generator/persistence functions
> (never hand-edited JSON) and is safe-by-construction: it never produces the
> dangerous roleful-topology + flat-graph combination. `/sound/output-topology`
> GET/POST carries a content revision; a browser page loaded before the reset
> gets `409 Conflict` instead of being allowed to replay the old active topology.
> Implementation:
> `jasper/cli/output_topology_reset.py`.
> `jasper.active_speaker.bringup` and
> `/sound/active-speaker/bringup-preflight` now make the product fork explicit:
> **manual guarded bring-up** can continue without a microphone for an operator
> with a known plan, while **guided calibration** requires working microphone
> capture. Manual guarded bring-up still requires saved topology, verified
> physical output identity, a ready active-speaker environment/load gate,
> staged guard evidence, Stop availability, and the calibration level at the
> floor. A calibrated mic upgrades guided confidence; an unchecked or clipping
> mic blocks guided calibration without pretending manual setup is calibrated.
> `jasper.active_speaker.startup_load` and
> `/sound/active-speaker/startup-load` now add the first guarded CamillaDSP
> reload boundary for this workstream. The UI can show the current
> startup-load preflight/state, POST
> `/sound/active-speaker/load-startup-config` to load the staged
> muted/protected startup graph, and POST
> `/sound/active-speaker/rollback-startup-config` to restore the config that
> was active before the load. Loading is blocked unless the staged candidate
> exists, validates as a JTS active-speaker startup config, path-safety
> evidence is hardware-probe-backed, assigned physical outputs are verified,
> the staged metadata still matches the current saved topology, the software
> guard evidence is intact, the calibration level is at the floor, Stop is
> available, no tone playback is active, and the current CamillaDSP config
> path is a readable bounded JTS rollback anchor. The rollback anchor does not
> need to already be an active-speaker protected baseline on first use; the
> staged muted/protected candidate owns driver protection before any tone can
> play. Unknown/custom DSP, missing rollback files, unreadable rollback files,
> and rollback configs with unsafe positive gain still fail closed. This is a
> DSP reload slice only: it does
> not generate samples, open ALSA directly, raise volume, or authorize
> playback. The load transaction persists a rollback anchor during the shared
> `dsp_apply` persist phase, so a missing rollback breadcrumb causes immediate
> rollback instead of leaving CamillaDSP pointed at an active startup graph
> with no recovery state.
> `/sound/active-speaker/check-path-safety` now writes the first
> hardware-probe-backed path-safety evidence artifact at
> `/var/lib/jasper/active_speaker_path_safety.json` (or
> `JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE`). This is still a no-audio
> proof step: it inspects the saved topology, staged protected candidate,
> backend calibration-level guard, and current CamillaDSP config path. The
> artifact binds that proof to the topology identity, target assignments,
> staged config path and hash, and rollback config path and hash. The evidence
> scope is `load_only_no_audio`; a passing check may unlock the startup-load
> path but never tones or normal playback by itself. Startup load rejects stale
> evidence if the saved topology, staged candidate, or rollback anchor changed
> after the proof; the product prepare endpoint refreshes this evidence as part
> of the chosen-driver setup path. The
> check is deliberately honest about rollback: normal bounded JTS stereo can be
> used as the first-run restore target, but unknown/custom DSP or a rollback
> config without the non-positive `devices.volume_limit` contract stays blocked.
> `jasper-active-speaker startup-template` can write one of these
> candidate templates from a preset JSON file and run
> `camilladsp --check` when the binary is available. The guarded web load
> path above is now the only product route that may reload this staged graph,
> and it still does not authorize sound. The first packaged
> worked-example preset is
> `jasper/active_speaker/presets/bc_de250_dayton_e150he44_v1.json`; the
> current no-audio default preset is the Epique/F110M safe bring-up profile
> above because it matches Jasper's immediate cabinet build.
> `jasper-active-speaker path-audit` now exposes the deterministic
> audible-path safety checklist and can evaluate operator evidence,
> but operator evidence is not enough to permit active config loading.
> `jasper-active-speaker environment-probe` is the first read-only
> environment evidence pass: it inspects ALSA playback devices, the
> current CamillaDSP statefile/config shape, `devices.volume_limit`,
> output channel count, optional `camilladsp --check`, and optional
> path-safety evidence. It now also reports a `safe_playback` block
> whose `playback_allowed` value is always `false` in the current
> implementation. It never plays audio, reloads CamillaDSP, or mutates
> state. `ok_to_load_active_config` can be true only when an active
> startup candidate, valid CamillaDSP preflight, and hardware-probe-backed
> path-safety evidence all pass; even that does **not** authorize tone
> playback until physical channel identity and a level-limited tone
> generator with emergency stop exist.
> Current next step: use the guarded startup-load preflight on hardware, verify
> rollback, then exercise the lab-gated floor-level test path first on a
> woofer/mid/sub target and then on a protected high-frequency target. Any
> high-frequency playback must remain high-passed, level-bounded,
> Stop-controlled, microphone-aware when available, and start at the
> test-level floor.
>
> **Update, 2026-06-16:** `jasper.active_speaker.driver_acoustics` adds the
> mic-backed analysis half of the Consumer Wizard Triad below. It is a pure,
> stateless module that reuses the room-correction sweep/deconvolution/analysis
> primitives (`jasper.audio_measurement.{sweep,deconv,analysis,quality}`, imported
> lazily so the wizard import stays numpy-free): `write_driver_sweep_wav` emits a
> channel-targeted multichannel sweep (the one thing `jasper.audio_measurement.sweep`
> can't do — it is mono only), `analyze_driver_capture` returns a per-driver
> `present`/`out_of_band`/`silent`/`unusable_capture` verdict plus a real
> `observed_mic_dbfs` from the capture RMS (the value `measurement.record_driver_
> measurement` already consumes), and `analyze_summed_crossover` flags a
> cancellation suckout at the crossover. This implements the *gated-summed flat
> check* (triad item 3 — a deep null in the normal summed response means a
> polarity/delay problem), **not** the rigorous *inverted null-depth
> optimization* (triad item 2, "Delay, Phase, and Null Verification" below).
> **Update, 2026-06-18:** the hardware-free product path is now wired after the
> Stage-5 driver ramp: `/driver-capture` and `/summed-capture` accept bounded
> WAV evidence with bounded raw-file retention, call the
> `commissioning_capture` bridge, and record real acoustic verdicts into
> measurement state; the then-current `/sound/` UI used the shared
> `measurement-audio.js` recorder for those submissions and kept the bounded
> combined-test level control. (**Superseded 2026-06-20 and narrowed 2026-06-23** — see the phone-optional
> update below: the combined check originally removed the by-ear "manual success"
> button to force mic evidence; it now re-offers a by-ear "Sounds right"
> gated on an audible combined test, and `/sound/` no longer exposes mic capture
> in the core setup flow.) This
> is still not a JTS3 acoustic validation: the real sweep playback/capture timing,
> live phone mic behavior, room noise, and driver response must be verified on
> hardware.
> Separately,
> the `/sound/` active-crossover setup copy was de-jargoned so no backend
> vocabulary (CamillaDSP/YAML, "protected"/"safe path", rollout "slice", raw
> snake_case codes) reaches the household; `friendlySetupReason` now collapses
> unmapped code-like strings to one actionable sentence instead of echoing them.
> **Update, 2026-06-20 (L1 measured level match):** the per-driver near-field
> capture now refines the datasheet sensitivity trim with a MEASURED one.
> `driver_acoustics.analyze_driver_capture(overlap_fcs=…)` records each driver's
> deconvolved level **at** the crossover Fc (the matched −6 dB Linkwitz-Riley
> shoulder cancels, leaving the relative driver sensitivity), and
> `baseline_profile._measured_level_trims` chains the driver-to-driver overlap
> deltas into a per-driver attenuation that OVERRIDES `_derive_corrections`'
> interim estimate. Manual apply keeps operator ownership (operator pin >
> measured > estimate > datasheet); only the explicit automatic-replacement
> action makes measured data override the prior pin. Each capture owns a
> sweep-peak + commissioning-gain ledger. The automatic level tone and ESS use
> one −12 dBFS source-peak constant; the ESS role gain comes from the current
> immutable applied Layer-A snapshot, never the quiet by-ear identity-test
> level. Exact ledger normalization compares drivers at a common effective
> excitation. A stale/missing applied snapshot or mismatched excitation evidence
> fails closed before playback/recording instead of treating playback gain as
> driver sensitivity. When automatic measurement must emit the all-muted
> startup/rollback anchor, it uses that same frozen applied preset rather than
> rereading the mutable design-draft preview. The first automatic
> product slice preserves the applied
> crossover frequency, slope, delay, and polarity; it replaces attenuation-only
> driver trims. Combined ESS remains an optional diagnostic, not an apply gate.
> Magnitude only — never a phase/delay decision. Fail-closed: a
> silent/clipped/low-SNR/missing capture keeps the datasheet trim and marks the
> baseline `provisional` (`corrections_source`, `level_match`, and `provisional`
> on the baseline payload; `active_speaker_output_safety.level_match_provisional`
> on jasper-control `/state`). Attenuation-only + the 0 dB ceiling are preserved,
> so the emitted baseline still passes the runtime_contract tweeter guard.
> Commissioning serializes against room correction / balance / sync cooperatively
> via [`jasper/web/active_speaker_flow.py`](../jasper/web/active_speaker_flow.py)'s
> self-expiring `active_phase()` (it can't hold `measurement_window` across its
> per-request flow). Canonical home for the L1 product tier:
> [HANDOFF-audio-measurement-core.md](HANDOFF-audio-measurement-core.md)
> "L1 measured level match". On-Pi (jts3) audible pass still owed.
> **Phone measurement is optional for safe normal playback.** A household can
> commission and apply an active baseline with zero phone use: each driver is
> confirmed by ear (yielding a provisional estimate), and the combined crossover check offers a by-ear
> "Sounds right" path in `/sound/`. The by-ear positive is still gated on an
> AUDIBLE combined test (you can't certify a blend you didn't hear). Mic-backed
> level and summed validation live in the HTTPS measurement/correction experience
> as an optional automatic tuning path. Safety is unaffected — a bad blend verdict
> is a quality issue (a suckout at the crossover), not a hazard: the crossover,
> tweeter high-pass, limiters, and 0 dB ceiling are in the graph regardless.
> **Room-correction prerequisite, corrected 2026-07-10:**
> `active_speaker.setup_status` is the SSOT. Layer B requires a safe,
> topology-current, full-domain immutable Layer-A snapshot; that snapshot may be
> owned by either explicit manual tuning or automatic microphone tuning. Current
> microphone evidence is quality/observability, not authorization to use Room.
> Passive speakers return allowed/not-required. `/correction/start` consumes this
> decision before reserving a session and directs incomplete active setups to
> `/correction/crossover/`.
> The explicitly applied Layer-A profile is the immutable playback/safety
> anchor. New driver captures create a mutable candidate and do not invalidate
> normal output or send the household back through `/sound/`; level evidence is
> keyed to that protected profile, not the candidate fingerprint. Room
> correction continues against that applied anchor while newer measurements are
> only candidates. Applied legacy profiles without a recomposition snapshot offer
> **Keep current manual crossover** (preserving its exact applied corrections) or
> **Tune automatically**. The latter automatically performs that same exact
> preservation transaction at level-match start when
> `manual_preservation.ready` is true, then refreshes the applied snapshot before
> relay registration; an unsafe/stale source refuses before audio. Automatic
> measurements create a candidate and only the
> explicit **Replace manual trims with automatic levels** action changes the
> owner. The frozen view preserves its `provisional` quality flag. Near-field automatic leveling
> restores normal listening volume immediately and reasserts its retained target
> only inside that driver's measurement window. If exact restoration is rejected,
> JTS applies a −60 dB emergency fallback before releasing the measurement window
> and reports the failure instead of resuming household audio silently at the
> measurement level.

> **Update, 2026-07-11 (polarity/delay persistence, Slice 0; corrected
> 2026-07-12):** the "multi-group
> group-specific delay correction still fail closed or warn" claim in the
> 2026-06-03 entry above is now stale for the MANUAL (region-level, symmetric)
> case. `CrossoverRegion` (`jasper.active_speaker.profile`) now carries polarity
> (existing `lower_polarity`/`upper_polarity`) and a first-class `delay_ms`
> working value, set on the crossover candidate/preview and carried through
> `jasper.active_speaker.baseline_profile._derive_corrections` to `corrections`
> on every apply — including a stereo (multi-group) topology, which previously
> zeroed both unconditionally via the `group_specific_delay_not_applied` guard.
> Manual tuning never consults measured alignment evidence for these two
> sub-parameters, mirroring the existing gain-pin rule. Automatic polarity may
> change only from a complete normal/reverse pair admitted by
> `crossover_contract.summed_decision_evidence_state` in the current protected
> profile context. A scalar `delay_ms` carried by any capture is never an apply
> input; only the bounded, repeatable Lane-F null walk may author a measured
> delay. Each applied sub-parameter's source is reported in a new
> `corrections_provenance` block (`manual`/`measured`/`recommended_start`/
> `preserved`). MEASUREMENT-DERIVED multi-group polarity emission remains
> deferred: `group_specific_alignment_not_applied` fires when more than one
> group has summed alignment evidence for automatic tuning. See
> [`active-crossover-information-design.md`](active-crossover-information-design.md)
> "Slice 0" for the product framing.

> **Update, 2026-07-12 (manual polarity/delay authoring, P2a):** the
> 2026-06-23 entry's manual-field enumeration above ("driver names,
> sensitivity, safe low test limits, per-driver level trim, and active
> crossover point/filter/slope") is now stale — `/sound/`'s manual crossover
> editor gained a collapsed **Alignment (advanced)** section per crossover
> region exposing lower/upper polarity and relative delay + target driver,
> so the visible field set now matches everything Slice 0 already persisted
> server-side. No schema or validation changed (`design_draft.py`'s
> `_normalise_candidate` already accepted these fields); this closes the
> "no `/sound/` UI for polarity/delay authoring" gap named in
> [`active-crossover-information-design.md`](active-crossover-information-design.md)
> "Current implementation gap summary".

> **Update, 2026-07-13 (Wave 1 commissioning contracts; silent and inert):**
> the `/sound/` design draft now carries a revisioned hardware-research and
> driver-safety contract without changing an audible path. The server builds a
> version-1 `jts_active_crossover_driver_research_request` for every physical
> active-driver target. A version-2 research result must echo the request and
> the exact target id/fingerprint, role, and make/model. Legacy version-1
> research remains advisory prefill only. The operator reviews and may edit all
> safety fields in `/sound/`; confirmation freezes one version-1
> `jts_active_speaker_driver_safety_profile` per current target set. Changes to
> target/topology/output, driver style, make/model, or visible values make the
> confirmation stale. Research provenance is retained only when the visible
> value still equals the researched value; an edited value is an explicit
> operator override. The profile's `authorizes_playback` is always `false`.
>
> This confirmed safety profile is not the older code-owned
> `driver_protection_profile` used to bound tone/ramp policy, and it is not the
> physical/software-guard fact stored by `/active-speaker/channel-protection`.
> Future audible adapters must intersect all applicable authorities and prove
> fresh protected graph state; Wave 1 does not wire the new profile to signal
> generation, playback, graph loading, or normal output.
>
> `jasper.audio_measurement.excitation_admission` adds the pure second half of
> the boundary: strict, content-addressed request/limits/protection-evidence and
> allow/refuse values bound to the exact target, confirmed profile, composed
> authority, excitation plan, band, effective peak, duration, and repeat count.
> Fingerprints are content identities, not signatures. The trusted Active
> adapter still has to intersect code/profile/plan limits, bind normalized
> generator and effective-peak inputs, derive fresh protection evidence from
> readback, and rerun admission immediately before playback. There are no live
> producers or consumers in this slice.
>
> The Active-owned commissioning lifecycle now defines the nine states
> `unconfigured`, `protected`, `measured`, `candidate_ready`,
> `applied_unverified`, `verified`, `blocked`,
> `blocked_live_state_unknown`, and `rolled_back`, with typed evidence on every
> positive transition. `blocked_live_state_unknown` is deliberately distinct:
> after a mutation call begins, an attempted or unknown outcome cannot recover
> through ordinary pre-mutation `blocked` and forget an uncertain live graph;
> it can leave only through exact restore evidence.
>
> The exact positive `CommissioningEligibilityReceipt` derives its required
> combined-speaker targets from a current `OutputTopology` whose evaluated
> status is `verified`; blocked or physically unverified output maps cannot
> create target authority. Each target must
> have exactly three distinct, admitted, fixed-reference-axis post-apply
> captures in one commissioning session and threshold profile, plus a passing
> typed verdict. The receipt binds the confirmed safety profile, applied
> candidate, expected/fresh-readback normalized graph, exact predecessor, and
> an honest retained-apply rollback outcome bound to the same operation,
> mutation, and observed applied graph. Attempted/unknown mutation and
> failed or performed rollback cannot mint a positive receipt. Exact rollback
> state reuses `null_walk.DspPredecessor`; no generic graph-transaction
> framework landed. The later Active integration now owns writer-locked
> candidate apply, fresh graph/path/volume readback, and exact predecessor
> restoration; post-apply verification, receipt issuance/persistence, and Room
> consumption remain the later integration lane.
>
> Current `active_speaker/bundles.py` evidence remains forensic/fail-soft. The
> new lifecycle is not current `/state`, while `active_speaker.setup_status`
> now owns a versioned Room eligibility projection. A topology-current immutable
> snapshot with explicit manual apply ownership is
> `manual_applied_profile` authority only after CamillaDSP's fresh running
> `active_raw` readback matches snapshot-derived recomposition under Active's
> semantic Layer-A fingerprint. Output-device
> settings and the full driver-domain mixer/pipeline/filter suffix are bound;
> the mutable pre-split Room/preference prefix is excluded. A mismatch blocks
> Room and requires explicit crossover reapply. An automatic applied snapshot stays
> incomplete until Active issues and exposes the exact receipt-backed result.
> Room consumes that one decision and neither inspects the graph nor historical
> B2b evidence. Automatic authority still requires fresh excitation-admitted
> captures plus the measured delay walk.

> **Update, 2026-07-14 (Wave 3 durable run;
> hardware-free):** `jasper.active_speaker.commissioning_run` is now the bounded
> control-plane store for one current automatic commissioning run. The
> correction-web integration starts it only after the exact authoritative
> comparison set has a fresh production-bundle session id and fingerprint. It
> persists the exact session/run/process-owner-generation identity, immutable
> generation-bound target attempts, and a bounded hash-chained journal of typed
> nine-state transitions under an atomic, advisory-locked file. Service startup
> claims the owner generation beside the repeat and level-run owners, so a prior
> process's callbacks are stale. `/correction/crossover/status` exposes the safe
> `commissioning_run` projection as `not_started`, exact `current`, comparison-
> `stale`, or fail-closed `unavailable`; `current` additionally requires the
> comparison's complete schema/fingerprint and current topology/protected-
> profile binding. It never exposes the process owner id.
> The web-created run starts `unconfigured`; no browser route reserves attempts.
> The typed internal host reserves generation-bound region attempts and advances
> an exact synthetic-admitted composition through `protected` to `measured`.

> **Update, 2026-07-14 (Wave 3 per-region evidence contract;
> hardware-free):** `jasper.active_speaker.commissioning_evidence` now owns the
> immutable pure shape for authoritative group-by-region capture sets. The plan
> keeps both crossover regions of a three-way distinct and binds the exact typed durable-run handle,
> topology, preset, protected profile, comparison, threshold profile, and
> session. Mono plans require exactly one mono active group; stereo plans
> require exactly left and right active groups, with exact way-count modes and
> complete driver-role sets. Normal and reverse each require three fresh one-shot captures from
> one typed reserved attempt; every coordinate in the exact Shared bounded
> coarse-plus-refinement schedule requires five fresh
> one-shot captures from its own attempt. Each capture binds exact graph,
> placement, generated-WAV, generation/playback protection, and canonical
> generation/playback admission identities, with cross-role replay refused. A
> typed operator attestation binds the signed geometry seed, and a complete-plan
> aggregate enforces one region per target plus global artifact, admission, and
> attempt uniqueness.
> The same plan now owns a distinct strict isolated-driver aggregate: exactly
> three admitted fixed-axis captures per physical driver, one semantic durable
> attempt per driver, globally unique capture/admission/artifact identities,
> and one canonical run-scoped complete artifact whose store reopens every
> child byte and both admission decisions. The production fixed-axis driver
> relay now populates this authority from its real recorder WAVs and exact live
> admission handoffs; resumable accepted/required progress and the complete
> fingerprint are projected from `/crossover/status`. Fixed-axis attempt four
> refuses below three accepted captures, and status repairs only write-once
> derived anchors after the typed captures are already durable. Historical and current
> fail-soft driver records cannot satisfy or be migrated into this contract.
> The positive receipt now likewise requires a unique one-shot generation and
> playback pair for each of its three post-apply captures, with raw,
> analysis-input, quality, generation, and playback identities and paths unique
> across the complete receipt; its admitted-capture,
> post-apply-target, and receipt containers are explicitly schema version 2.
> Shared also exposes a pure `select_scheduled_delay()` final evaluator that
> requires the exact schedule and reuses the exhaustive selector's
> repeatability, plateau, and tie policy. A store-backed pure deterministic
> evaluator now consumes exact complete isolated and summed evidence and
> derives an attenuation/polarity/delay-only electrical candidate. The
> production Active service now invokes it after the final summed capture,
> persists and strictly reopens one generation-scoped candidate, binds the exact
> run's `candidate_ready` transition to that artifact, and exposes a compact
> review of retained Fc/family/order, measured attenuation and delay, retained
> polarity proof, and source evidence identities. A POST-only recovery route
> resumes the same deterministic publication after an interruption. A
> deterministic evaluator refusal is persisted as exact failure evidence,
> transitions the run through the existing `candidate_scoring_failed` blocked
> path, and requires a fresh complete measurement sequence rather than a futile
> retry of immutable evidence.
> Post-apply verification/receipt and Room consumption remain unavailable.
> Real summed capture transport is now composed through the correction relay, but
> live JTS3 playback and acoustic capture remain unvalidated.

> **Update, 2026-07-15 (measured-candidate apply boundary; hardware-free):**
> the candidate review now offers one explicit Apply action carrying the exact
> reviewed candidate fingerprint. Active recompiles that candidate through the
> existing baseline emitter, requiring the retained preset to match exactly and
> using only its measured attenuation, polarity, and absolute per-role delay.
> One existing DSP writer lock spans the final authority recheck, exact
> graph/path/listening-volume predecessor snapshot, existing bounded
> `apply_dsp_config` load, fresh readback, protected-graph classification, and
> any exact restore. The run sidecar distinguishes pre-mutation release,
> pending/unknown mutation, proved restore, and a freshly proved retained
> graph. Apply proof and compiler/apply artifacts are write-once under the exact
> issuance. A cancellation, load/readback/protection failure, or unproved
> retained-sidecar write restores graph, path, and volume before the lock is
> released and replaces the shared DSP apply result with that restored outcome.
> A crash-interrupted pending mutation drains writer admission and exact restore
> despite request cancellation, records the same shared outcome, and is
> recoverable from its exact predecessor pointer. Once the retained proof is
> durable, retry performs only
> baseline-state and lifecycle finalization and never re-applies audio.
> `/crossover/status` projects `candidate_ready`,
> `apply_finalization_required`, `apply_rolled_back`, `restore_required`,
> `restore_finalization_required`, or `applied_unverified` from that same
> run/evidence authority. A transient writer-lock collision leaves the exact
> reviewed candidate retryable. The durable pre-apply plan remains the evidence
> authority after the measured graph becomes Layer A; it is revalidated against
> the retained mutation's exact predecessor instead of being rebuilt from the
> new applied graph. The browser owns
> no mutation state machine. This slice has synthetic/contract coverage only;
> post-apply fixed-axis verification, receipt issuance, Room handoff, and the
> first live JTS3 candidate apply remain outstanding.

## Current Operational Truth

Active speaker DSP is a separate layer from room correction and from
preference voicing. Room correction asks, "what should be compensated
at this listening position?" Preference voicing asks, "what tonal
tilt does this listener like?" Active speaker commissioning asks,
"what should this speaker be before the room is considered?"

For JTS, that means:

- The current `/correction/` wizard must not rewrite crossover,
  polarity, per-driver gain, driver delay, or limiter policy.
- Active speaker commissioning is **Layer A: speaker baseline**:
  per-driver linearization, baffle-step compensation, acoustic-target
  crossover, polarity, time alignment, gain trim, and per-driver
  limiters. It is measured with room-immune or quasi-anechoic
  techniques and stored as a versioned speaker-baseline profile.
- Room correction is **Layer B**: modal-region EQ and listening-area
  compensation. It is measured at the listening position(s), lives in
  the stereo domain, and must not silently alter Layer A.
- Preference voicing is **Layer C**: house curve, bass/tilt choices,
  and subjective "brighter / warmer / more bass" tuning. It is
  reversible taste shaping, stored separately from both Layer A and
  Layer B.
- In active mode, "flat" must mean **protected speaker baseline with
  room/preference EQ bypassed**, not an identity full-range
  `/etc/camilladsp/v1.yml` path. Resetting to identity can send
  full-range content to a tweeter.
- A future active speaker profile is a versioned speaker baseline,
  not a room-correction session. Room correction and preference EQ
  stack with that baseline, but in a CamillaDSP graph they normally
  live on the stereo input pair before the per-driver split.
- Every measurement bundle should eventually record the active
  speaker profile ID so later analysis knows what acoustic baseline
  was measured.
- A fresh bundle-backed automatic comparison now also owns one durable
  `commissioning_run` identity on the crossover status surface. Treat it as
  fail-closed control-plane correlation only: `current` does not mean measured,
  candidate-ready, applied, verified, or Room-eligible.
- The strict per-region evidence values define what fresh normal, reverse, and
  delay capture authority must contain. The hardware-facing summed runtime now
  accepts only a typed server-owned request bound to one exact current adjacent
  region. The normal graph stays emitter-owned. Reverse adds one target-scoped
  zero-gain inversion lane. Delay first adds target-scoped offsets that equalize
  the two emitter-owned totals, then uses two zero-relative candidate lanes;
  neither transform changes the same-role channels in a sibling speaker group.
  The full scheduled envelope, not only the current coordinate, must retain
  headroom under Shared's 20 ms ceiling. Transformed YAML preserves its proven
  emitter source. Before the limiter, the shared classifier requires the
  topology-derived emitter chain shape and order: optional bass-management HP,
  matched adjacent-role LR crossover filters, canonical Delay, non-positive
  Gain, then exactly one canonical Limiter, grouped across the role's current
  outputs. Cumulative post-split delay on each physical output is capped at
  20 ms. The existing 400 Hz tweeter floor and 40-200 Hz/order-4 local-sub
  envelope are re-proved. Every roleful graph retains exactly one active split
  (driver-domain adds only its channel-select mixer); guarded commissioning ends
  each exact grouped protection chain with one per-output mute and permits no
  post-mute tail. After a baseline limiter the classifier permits only the runtime's named finite
  non-positive Gain and bounded Delay lanes; any other appended tail filter is
  unsafe, and the supplied normal graph cannot predeclare that reserved
  namespace. Every fresh applied readback is reclassified against the current
  topology before capture.
  It sets and freshly verifies the safe listening level before applying an
  audible graph, then holds the existing bounded writer boundary through fresh
  graph/path/listening-volume readback, the supplied admitted capture callback,
  and exact predecessor restoration. It requires host-owned mutation-journal
  callbacks around the live mutation and exposes a locked exact-predecessor
  recovery operation. The typed internal host supplies the production caller,
  freshly re-emits and binds the exact preset graph identity (including
  crossover IDs, Fc, and order), pins one normalized applied-baseline and
  microphone-calibration context across the complete evidence program, rejects
  graph-identity reuse across normal, reverse, and every delay coordinate, and
  persists a cross-process issuance CAS plus
  issuance-scoped predecessor/restore/commit artifacts. A crash-released
  execution mutex spans runtime through canonical capture commit. Restart either completes
  the exact restored capture commit, aborts a restored no-capture issuance, or
  blocks the run as `blocked_live_state_unknown` when restoration is uncertain.
  Predecessor cleanup re-proves the exact graph and path before restoring its
  potentially louder listening volume; failed graph restoration retains the
  attenuated measurement volume.
  Canonical stereo filter steps may group only outputs sharing one driver role;
  mixed-role groups fail closed, while isolated-driver admission stays
  singleton-only. Zero-delay capture reuses its freshly proved zero-relative
  graph instead of applying identical YAML twice.
  Cancellation drains
  the transaction; cleanup failure outranks cancellation, and possible mutation/
  audio is never reported as pre-audio certainty. The adapter schedules nothing
  and grants no evidence or candidate authority by itself.
- The shipped 350 Hz lower crossover now has a reviewed bounded schedule
  contract: its 29-coordinate fine grid becomes 15 symmetric coarse
  coordinates plus at most two adjacent fine refinements around an explicit
  coarse anchor. The exhaustive runner remains capped at 25. A separate final
  evaluator requires that exact schedule and applies the same winner policy.
  The internal host consumes both; this runtime never selects a delay.

The existing deployed audio topology now has the runtime substrate for
the constrained dual Apple active-output profile, but commissioning
still must satisfy the safety gates before sound-emitting active use:

- All output profiles route TTS/cues to `jasper-fanin`. Fan-in applies
  voice ducking to renderer/program lanes, mixes TTS after the duck, and
  sends the complete signal through CamillaDSP crossover/protection.
  The dual Apple active-output profile then has `jasper-outputd` split
  the resulting four-channel lane to two pinned Apple DAC PCMs.
- Active crossover output needs a stable multi-output speaker layout. For a
  mono active cabinet this is at least two physical outputs
  (`woofer`, `tweeter`). For a stereo active pair this is four
  physical outputs (`left_woofer`, `left_tweeter`,
  `right_woofer`, `right_tweeter`).
- The active-speaker preset `channel_map` remains a **logical CamillaDSP
  output contract**, not the user-facing source of physical speaker wiring.
  The `/sound/` Active crossover setup surface owns the physical layout draft:
  it accepts detected device lanes, lets users group lanes into speakers, swap
  left/right through role assignment, mark passive speakers, assign active
  driver roles up to 3-way, and identify subwoofer outputs. Do not bake
  HiFiBerry DAC-X8, Apple dongle, or any other DAC-specific physical
  assumption into `test_signal_plan` or the dry playback artifact backend.
- That product surface uses the backend substrate:
  `jasper.output_topology` and `/sound/output-topology` persist a
  versioned, complete-replacement topology draft at
  `/var/lib/jasper/output_topology.json`. It can describe physical DAC
  lanes, speaker groups, passive/active modes, up-to-3-way driver
  roles, subwoofers, approximate placement, identity verification, and
  tweeter protection status. Its hardware shape comes from the static
  DAC profile registry, so known profiles such as Apple USB-C, DAC8x,
  and dual-Apple 4ch get consistent labels/output counts and clock-domain
  reporting. It deliberately has no audio side effects; active-speaker tone
  playback and active CamillaDSP loading must still pass through their own
  safety gates.
- The current `/sound/` UI composes one optional subwoofer add-on with
  any mono/stereo draft when a spare physical output exists. Keep that
  compositional model; do not add separate `stereo_active_2way_plus_sub`
  template families unless the topology contract itself changes.
- The topology substrate now has a separate channel-identity report and
  update route (`/sound/active-speaker/channel-identity`). Treat that
  evidence as an operator-confirmed fact about physical wiring, not as
  permission to emit sound. Marking a tweeter channel verified does not
  satisfy tweeter protection, path safety, startup/reload safety, or
  future level/mic gates.
- The topology substrate also records tweeter/compression-driver
  protection evidence via `/sound/active-speaker/channel-protection`.
  Marking protection present is a human/operator fact about the physical
  build. A software-guarded bring-up request is also an explicit operator
  fact: it may let JTS stage a muted no-load startup candidate, but it remains a
  playback blocker until later guard/load/level evidence passes. Neither state
  loads DSP or authorizes playback by itself.
- Before tweeter hardware is connected, all audible paths must be
  proven to pass through the same protected crossover path. A TTS
  bypass into a raw active amp channel is a driver-damage hazard.
  This applies to renderers, TTS/cues, `/correction/` sweeps,
  autolevel/test tones, USB Audio Input, startup/reload states, and
  any direct `jasper_out` rollback path.

## Single audio path commissioning

> **Status: design-of-record, 2026-06-16 — partially built.** This is the
> agreed target architecture for the `/sound/` active-crossover flow. It
> replaces the earlier two-path model (a direct-DAC diagnostic bypass for test
> tones plus an outputd lane for durable apply) with **one** audio path.

**Principle.** Commission and validate the speaker *through the production
audio path* — the outputd-owned active CamillaDSP graph — and "save" simply
freezes the commissioned config as the durable profile. There is no separate
validation path. Validating on a path you won't run is both pointless (it has to
be re-set-up for production anyway) and unsafe: the old direct-DAC bypass wrote a
tone straight to `hw:CARD=…,DEV=0`, so a tweeter test had **no** protective
high-pass. Through the production graph, every driver is tested behind its own
crossover/limiter exactly as it will run.

**Why the old direct-DAC path was useless.** Durable apply has always required an
outputd-owned active lane, so a config tested via direct-DAC could be compiled
but **never applied** (`compiled_apply_blocked`). It could not produce a working
speaker — dead-end functionality.

**Two facts that shrink the work** (verified, vs an earlier mistaken framing):
TTS already enters at fan-in (`jasper-voice.service` → `/run/jasper-fanin/tts.sock`,
pre-CamillaDSP), so voice rides the crossover at every width — the active output
transport needs **no** TTS lane. And the AEC reference is mono at both consumers
(software AEC3 sums L+R→mono; the chip USB-IN producer downmixes), so the
reference is a clip-proof mono sum of the driven lanes — no per-DAC L/R fold.

### Critical path

1. **DAC-agnostic active-output transport** (the hard prerequisite). The single
   path needs the speaker's DAC to have an outputd active lane. The product
   profiles that declare one today are the single Apple USB-C dongle (2ch),
   DAC8x/DAC8x Studio (8ch), and the dual-Apple 4-channel composite. The
   transport dispatches on clock-domain *shape* (coherent single / paired
   composite) with width + channel map as data, so future coherent DACs ride it
   without per-DAC code. Full design + change set:
   [HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md)
   "DAC-agnostic active-output transport (design-of-record)".

2. **Commissioning orchestration** (browser/API capture submission wired;
   live sweep playback/capture validation pending). The target live sequence is:
   per driver, compile the preset from the saved crossover preview
   (`staging.compile_preset_from_crossover_preview`), emit the production graph
   with a per-output mute mask
   (`camilla_yaml.emit_active_speaker_commissioning_config`, **built + wired**:
   as of Stage 2b `staging.stage_protected_startup_config` stages the production
   graph with `audible_outputs=frozenset()` — the all-muted boot config — instead
   of the unmasked startup emitter; the unmasked
   `emit_active_speaker_startup_config` is kept for the `startup-template` CLI),
   load it muted through the guarded
   path, open the protected playback window, play an ESS sweep through the
   production fan-in lane, capture the phone mic in the HTTPS browser flow with
   [`measurement-audio.js`](../deploy/assets/shared/js/measurement-audio.js),
   return the bounded WAV through `/correction/crossover/relay-capture`, analyze with
   `active_speaker.driver_acoustics.analyze_driver_capture`, and record via
   `commissioning_capture.record_driver_acoustic_capture` →
   `measurement.record_driver_measurement`. Advance per driver; then run the
   combined-driver test, submit the summed relay leg
   (`analyze_summed_crossover`), and freeze the commissioned config as the
   durable profile (`baseline_profile.*`) when the measurement gates are
   complete. The server/core path above is covered with synthetic capture fixtures;
   the implemented hardware-free slice is the bounded WAV submit/analyze/record
   and gate progression, not proof that JTS3 has emitted and captured the sweep.
   The Wave 3 control plane adds a durable bundle-backed run identity, startup
   owner-generation claim, fail-closed status projection, cross-process issuance
   CAS, and a typed internal host that can reserve exact region attempts and
   advance synthetic-admitted evidence to `measured` after exact restoration.
   Legacy direct/browser summed ingress remains refused. The production relay
   now supplies real recorder WAVs plus generation-specific signed geometry to
   that host without accepting browser operation policy.
   The live playback window, browser mic timing, and actual speaker acoustics
   still need on-device validation. Per-driver isolation is the CamillaDSP
   **mute mask**, not a channel-targeted WAV — so
   `driver_acoustics.write_driver_sweep_wav` is superseded and removed when this
   lands. **Filters are designed to *acoustic* LR targets from the measured
   per-driver response — not blindly inserted as electrical LR biquads** (the
   sweep/deconv measurement is what makes the acoustic target achievable).

3. **Delete the direct-DAC path** (completed 2026-06-17). The product UI no
   longer has `prepare-driver-test`, `play-tone`, `floor-audio-result`, or
   direct-DAC readiness controls. `DirectDacTonePlaybackBackend`, the
   `DIRECT_DAC_SOURCE` route variant, and the DAC8x final-output
   `JASPER_OUTPUT_DAC_ROUTE` renderer path were removed together. Passive /
   full-range groups now state that there is no active driver test in the
   product UI; active 2/3-way groups use only the commission ramp through the
   protected active graph. The remaining generic audio hook is explicit lab
   `aplay`, guarded by `JASPER_AUDIO_LAB_TONE_BACKEND=aplay` and a
   dedicated `JASPER_AUDIO_LAB_TEST_PCM`.

### Staged, hardware-verified build sequence (each independently green; deletion last)

jts3 = DAC8x + real bi/tri-amp speaker + live drivers + phone mic
(`PI_HOST=jts3.local`). **Every stage has a red/abort condition and rollback.**

- **Stage 0 — HW-free Python.** `_common.py` diagnostics vocabulary (`_issue` in
  16 files, `_finite_float` 9, …); `DacProfile.dac_channel_map` +
  `is_coherent_single()` + import-time guards (**0.1/0.2 landed**);
  `OutputLayout`/`OutputTransportPlan`; resolvers → `OutputTopology`; collapse the
  resolver self-shadow; `ActivePlaybackRouteCapability` reads `OutputLayout`
  (**0.3 landed** — the data model lives in
  [`jasper/output_topology.py`](../jasper/output_topology.py); every physical-DAC
  PCM is forced through `stable_card_pcm` → `hw:CARD=<name>` and the
  `OutputTransportPlan` boundary rejects any numeric-index/`plug`/`plughw` form, so
  the card-index drift class is fail-closed before Stage 1/2 ride the plan). *Red:*
  any `test_dac_*`/`test_output_topology`/`test_active_speaker_*`/`test_output_layout`
  or `ruff` failure.
- **Stage 1 — Rust transport, fake backend.** `SinkMode` rename + parse alias
  (**1a landed**: `DualApple`→`Composite` shape, `DualAppleBackend`→
  `PairedCompositeSink`, wire value held stable); runtime DAC width carried as
  data at the `SingleAlsaSink` open/read/write sites via
  `JASPER_OUTPUTD_ACTIVE_CHANNELS` (2..=8), `fold_reference` (N-lane clip-proof
  1/N mono → stereo reference), real clip accounting replacing the hardwired 0
  (**1b landed**: a coherent single DAC of any width — DAC8x 8ch — rides the
  single path; width-2 is byte-identical; the wide path fails closed against the
  stereo-only bridge/fifo/tts features); `RuntimeAlsaSink` folds the composite
  path into the same `run_alsa` loop as single ALSA (**1c landed / Stage-7 code
  cleanup**: `run_alsa_dual_apple` deleted, `downmix_dual_active_reference`
  renamed to `fold_reference_pairwise_composite`, composite now records real
  full-scale sample counts through `state.mark_period(..., clipped)`, while the
  pairwise `[avg(ch0,ch1), avg(ch2,ch3)]` reference stays byte-identical).
  `test_outputd_wiring.py` now pins the unified-loop contracts. *Red:*
  `cargo test --locked` failure (built on a Linux+ALSA box — jts3 — since the
  crate needs system ALSA); the byte-identical width-2 and pairwise-composite
  regression tests must pass.
- **Stage 2 — reconciler + wide content lane, dry-run.** `kind`-dispatched
  `OutputTransportPlan` env; route from `dac_channel_map`; the
  `__OUTPUTD_ACTIVE_CONTENT_CHANNELS__` wide lane, **ban `type plug`/`plughw:`**,
  width-exact `hw:`. *Red:* `reconcile --print-env` diff non-empty for dual-Apple
  or DAC8x-stereo; `aplay -D outputd_dac` not resolvable as the renderer user.
  **2a landed (transport plumbing; drive-what-we-use width):**
  `jasper-audio-hardware-reconcile` emits the active single env
  (`JASPER_OUTPUTD_SINK=single_alsa`, `JASPER_OUTPUTD_ACTIVE_CHANNELS=W`,
  `JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture`) for a recognized
  coherent single DAC when the loaded CamillaDSP config's playback width W is a
  valid active width **within the DAC's cap** (`2 ≤ W ≤
  active_outputd_lane_channels`) — the `active_graph_status` gate (renamed from
  `dual_apple_active_graph_status`) reads W and returns it; the reconciler emits
  **that actual W** so outputd opens the DAC at exactly the outputs the speaker
  drives (a DAC8x running a 2-way drives 2, not 8). A config exceeding the cap
  fails closed (`active_graph_width_out_of_range got=W cap=N`); otherwise
  byte-identical stereo. The active content lane (snd-aloop substream 5) is raw
  `type hw` (card/device/subdevice only — the `hw` plugin rejects
  channels/rate/format; width is set by the openers and locked by snd-aloop),
  `type plug`/`plughw:` banned. The active-capable product `DacProfile`s declare
  `supports_active_outputd_lane=True` (Apple USB-C dongle cap 2,
  DAC8x/DAC8x-Studio cap 8, dual-Apple composite cap 4). Because the gate accepts
  the config's actual width, the existing
  per-speaker emitters (driver-count configs) engage active mode directly — **no
  full-width-padding producer is needed.** **Load-bearing hardware fact (#741) —
  DAC capability VERIFIED on jts3:** `aplay --dump-hw-params` on the raw DAC8x
  reports `CHANNELS: [2 8]`, so opening the DAC at W < its physical channel count
  is hardware-supported (no native-width `DacProfile` property is needed). The
  remaining bench item is exercising that open + idle-undriven-outputs behaviour
  *through the active outputd lane* at Stage 4 (a staged active config, not the
  base cutover.yml jts3 has been running). A DAC that required native-width opens
  would declare that per-profile rather than forcing universal padding.
  Production automatic crossover commissioning is a narrower capability:
  `DacProfile.supports_active_crossover_commissioning` is true only for the base
  DAC8x today, and the production service additionally requires a two-way preset.
  DAC8x Studio, Apple, composite, and three-way paths fail before capture rather
  than inheriting launch authority from the broader active-output lane.
  **2b landed (masked commissioning emitter wired):**
  `stage_protected_startup_config` now stages the production graph via
  `emit_active_speaker_commissioning_config(..., audible_outputs=frozenset())` —
  the all-muted boot config — instead of the unmasked startup emitter. The
  software guard (`_software_guard_evidence`) proves the tweeter is muted by its
  per-output `as_out{idx}_commission_mute` (the per-role `as_tweeter_startup_mute`
  is gone) while still asserting the protective high-pass + limiter wrap the
  tweeter channel, and a `staged_candidate_fully_muted` gate enforces the
  crash-recovery-MUTED invariant on every staged boot config. The unmasked
  `emit_active_speaker_startup_config` is retained for the `startup-template` CLI.
- **Stage 3 — jts3, DAC8x as 2ch single, NO drivers at risk.** Prove music + TTS
  (via fan-in) + AEC reference + honest ledger + real clip counter through
  `SingleAlsa` width-2. **Load-test Pi-5 multichannel headroom here.** *Red:*
  `jasper-doctor` not green, XRUNs under load, wake fails during a quiet sweep →
  fix headroom before proceeding.
- **Stage 4 — jts3, masked active load, drivers connected, speaker SILENT.** *Red:*
  any audible output → fail closed, do not unmute.
- **Stage 5 — per-driver floor unmute, woofer→tweeter, operator-confirmed
  (built; runnable via `jasper-active-speaker commission-ramp` **or** the
  `/sound/` Speaker setup → "Confirm outputs" step, which embeds the guarded
  per-driver Play/Stop/"I hear <role>" controls next to the DAC-channel mapping
  for an active 2/3-way group; passive/full-range groups have no separate
  active driver test —
  POST `/active-speaker/commission-{load,ramp-step,ramp-ack,ramp-abort}` +
  read-only GET `/active-speaker/commission-state`).** A commission
  load still exists internally: it arms a driver at the protected floor (gain
  −120 dB, mute off — silent). The browser does not expose this as a separate
  operator step; pressing **Start tone** first ensures/re-opens that silent load
  if needed, then `commission_ramp.ramp_audible_step` raises that per-output gain
  (the threaded `audible_gain_db`) one bounded, gated step at a time toward the
  Stop-controlled ramp ceiling (`MIN_TEST_LEVEL_DBFS` →
  `COMMISSION_RAMP_MAX_LEVEL_DBFS`, ≤ `AUDIBLE_RAMP_STEP_DB`/step). The transient
  per-driver graph uses
  `COMMISSIONING_HEADROOM_DB=0` so that this bounded ramp is the actual audible
  test envelope; the all-muted staged boot/rollback graph keeps the separate
  `STARTUP_HEADROOM_DB=40` crash-recovery headroom. The `/sound/` commission
  ramp pairs that graph load with one bounded continuous sine into
  `correction_substream` (currently a 35 s `aplay` session, reused across the
  browser's ramp). The sine frequency is planned by
  `active_speaker.test_signal_plan` from the same compiled preset/crossover
  edges and tweeter-protection policy that emitted the active CamillaDSP graph,
  but the identity tone is role-native: a low-pass-only woofer prefers a normal
  woofer tone derived from roughly one-third of the low-pass edge and clamped to
  about 120-250 Hz before final band safety clamping, subwoofer prefers 50 Hz,
  midrange stays near the geometric center of its passband, and tweeter remains
  above the strictest crossover/protective high-pass edge. If no
  margin-bounded in-band tone exists, the planner blocks before WAV generation
  or fan-in selection. The tone enters fan-in and then the protected active
  CamillaDSP graph; if the tone backend fails, the endpoint rolls back to the
  all-muted staged config and does not leave a pending by-ear confirmation.
  The gate (`build_stage5_ramp_gate`, fails closed) **re-asserts the protective
  high-pass on the RUNNING graph before any tweeter step** (not just the config
  file, via `running_commission_evidence`), bounds the gain, asserts the 0 dB
  ceiling + the audible driver's limiter, and enforces woofer-before-tweeter.
  Each step records a `ramp.pending` entry with the active role, gain, playback
  id, and tone frequency. The browser's automatic louder step does **not** clear
  pending with a hidden `silent` ACK; it calls `commission-ramp-step` with
  `auto_retry_pending=true`, and the backend replaces the same-driver pending
  step in place and updates safe-playback's pending playback id, so "I hear
  <role>" always confirms the latest audible tone while the tone is playing. The
  operator confirms by ear (`commission-ramp ack`) → `floor_confirmed`; in the
  web flow the ACK also promotes output identity when needed and writes the
  durable `measurement.record_driver_measurement` operator-only driver check
  used by "Validate and apply", then re-mutes the transient graph. Automatic
  mic capture may reuse that durable floor proof after the volatile safe session
  expires, but only through `measurement.current_driver_floor_evidence`: the
  record must remain captured and blocker-free, then independently exact-match
  the current topology's target id, fingerprint, group, role, output, playback
  id, and accepted embedded confirmation. The automatic post-capture record
  boundary re-runs this check and has no volatile safe-session fallback, so a
  topology change between play and upload rejects the acoustic record. Stale or
  malformed embedded confirmation still refuses before audio. Automatic driver
  capture is an outer DSP transaction: it may use the all-muted staged graph as
  the inner commissioning rollback anchor, but it restores the file-backed
  production config path from entry after success, playback failure, exception,
  cancellation, or post-anchor load refusal. A transient unsaved inline audition
  is deliberately not resurrected; the durable production config wins. The
  shared `CamillaController` bounds each synchronous websocket worker attempt,
  and cancellation aborts then drains that worker before the controller lock is
  released; it never retries the cancelled mutation. This transport property is
  necessary but not itself rollback: `set_config_file_path` is two sequential
  commands, so this outer transaction's retained production anchor remains the
  authority after an ambiguous response. The ramp's
  `confirmed_roles` remains only ordering memory for woofer-before-tweeter; the
  `/sound/` card treats measurement-backed driver checks as the product truth,
  so stale ramp state alone cannot complete the
  card. Start-tone is single-flight in the browser so rapid clicks cannot open
  duplicate commission loads under one continuous tone. Abort/rollback re-mutes
  and clears the safe-playback floor-pending state before another driver test
  begins. `too_loud`/`heard_wrong_driver` stop the tone and re-mute; CLI/lab
  `silent` remains available for explicit retry flows. The swept
  measurement is Stage 6. **"Subsonic/DC protection present" is satisfied by the
  protections already in the graph** — the bounded commissioning gain envelope,
  the 0 dB ceiling, and the per-driver limiter — **not a dedicated woofer
  high-pass** (a deliberate deferral; see "Resolved decisions"). *Red:* HP not
  confirmed live, or any sibling audible → abort, re-mute. **On-device ch1
  woofer ramp is bench-gated on jts3 (amps off until confirmed); validation plan
  in the gap-1 increment.**
- **Stage 6 — sweep + AEC-reference validation (gate that can fail the feature).**
  Per-driver and summed `driver_acoustics` are now wired through the `/sound/`
  browser/API path and baseline gates with synthetic captures. The remaining
  proof is live: actual Stage-5 playback handoff into the sweep, phone-mic
  capture quality/timing, and real driver/summed acoustic behavior on JTS3.
  **Pre-gate (check before Stage 6, not during):**
  confirm there is **no sub latency outside CamillaDSP's alignment** — a plate-amp
  with its own DSP, a sealed-sub correction stage, anything downstream of the
  reference tap that adds group delay the fold can't see. That is the specific
  thing that breaks the single-filter AEC model `mic ≈ ref·h` (see "Resolved
  decisions"). Then validate the clip-proof mono reference with the **right
  metric: low-band ERLE + delay-estimator stability, NOT aggregate ERLE**
  (aggregate can look fine while the sub band quietly leaks). *Red ladder:* if
  low-band convergence / delay stability regresses → (a) high-pass the sub's
  contribution to the reference (band-match to mic sensitivity), else (b) drop
  the sub from the reference fold, else (c) per-lane/per-band reference weighting
  (deferred end-state). *Red:* `/state.output.clipped_samples` not real/≈0, or
  low-band wake regression unresolved by (a)/(b).
- **Stage 7 — freeze + delete the fork + dual-Apple regression.** Code cleanup
  landed 2026-06-17: `run_alsa_dual_apple` is gone, composite opens through
  `RuntimeAlsaSink`, and the 4ch path now shares the single ALSA loop's
  reference/clip/state accounting. Remaining hardware work: freeze baseline;
  reboot → deterministic boot into the active config with TTS; re-run dual-Apple
  end-to-end on a dual-Apple Pi to prove `PairedCompositeSink` didn't regress the
  4ch path (now also gaining ledger + clip). *Red:* `/state` mismatch across
  reboot, or dual-Apple regression.

### Resolved decisions + scope

- **TTS stays at fan-in** — *positively* correct, two independent reasons, not
  merely unchanged: (1) it keeps voice band-routed across drivers like music (a
  post-crossover path would send full-range voice into a tweeter lane and bypass
  per-driver gain/delay/correction); (2) because the reference is folded
  post-mix, TTS automatically lands in the AEC reference — which is what lets the
  speaker hear a **barge-in during its own spoken response** (real assistant
  value, not incidental). *Latency budget to watch:* TTS now eats the crossover's
  group delay on every response — negligible with IIR (LR biquads), but if
  crossovers ever go linear-phase FIR (tens of ms) that adds directly to
  perceived response latency. Not a reason to change — there is no safe way to
  bypass the crossover for voice — just a budget to track.
- **Subwoofer folds into the AEC reference: yes, default — and the determinant
  is delay alignment, not bass.** AEC3 fits a single filter `mic ≈ ref·h`; a
  wideband sub+mains reference sum admits one `h` only if the sub and mains reach
  the mic through ~the same bulk delay. An active crossover *already* delay-aligns
  every lane in CamillaDSP for acoustic summation, so if all lane delay lives
  inside CamillaDSP the summed reference is phase-coherent and AEC3 converges. The
  failure mode is the Stage-6 **pre-gate** above: a sub path with latency
  CamillaDSP doesn't see breaks `h_main ≈ h_sub` and no single filter fits.
  Confine the risk-check to **low-band ERLE + delay-estimator stability** (AEC3's
  ~8 kHz band split keeps any exposure in band 0); the software path already
  high-passes its reference at 125 Hz, so verify only the chip path's reference
  band + mic roll-off.
- **M>2 / 3+-device composite is out of scope** — it would generalize
  `classify_output_cards`' `len(apple)==2`, `dual_apple_runtime_mapping`, and
  `apply_observed_composite_policy`, which the new data fields do not reach. Named
  as a limitation, not implied free.
- **Do NOT collapse `safe_playback`'s floor tri-state to a bool** —
  `floor_pending_operator` ("tone played, awaiting ACK") is load-bearing for
  Stage 5: a process that deliberately unmutes drivers one at a time needs
  "not-yet-operator-confirmed" to be **distinct from both "muted" and "confirmed
  safe."** Consolidate ownership, preserve the state space. *Landed:*
  `commission_ramp.ramp_audible_step` drives this tri-state for the per-DRIVER
  floor confirmation — the floor step (or a `silent`-retry step) →
  `floor_pending_operator`; `commission-ramp ack heard_correct_driver` →
  `floor_confirmed`. The per-STEP gate is a separate thing (`ramp.pending`): a
  louder step on an already-confirmed driver releases `ramp.pending` while the
  driver stays `floor_confirmed` — conflating the two wedged the second step up,
  now fixed. The session is **armed as a precondition** (on a static ready
  report; the Stage-5 gate, not `probe_active_speaker_environment`, is the
  authority) and **fails closed** if it cannot arm. It is not replaced by a bool.
- **"Subsonic/DC protection present" (Stage-5 gate) = assert the EXISTING
  protections, no dedicated woofer high-pass (decided 2026-06-17).** The active
  graph has a protective high-pass only on the tweeter; the woofer/low path has
  none (its crossover is a low-pass, which passes DC). Rather than add a woofer
  subsonic HP — whose corner is driver/enclosure-specific and which would touch
  the production graph + the acoustic baseline — the Stage-5 gate stands on what
  is already there: the 0 dB volume ceiling, the per-driver limiter, and the
  startup headroom (all re-asserted live, plus the band-limited commissioning
  tone). A real woofer subsonic/DC-block HP is a deliberate deferral; revisit if
  a direct-amp woofer's excursion at commissioning level proves it necessary.
  The gate names its checks for what they enforce — it does not claim a subsonic
  filter exists.
- **Crash recovery from any commissioning step lands MUTED** (the same safety
  property as the tri-state + the muted-by-default startup config). A power loss
  or crash partway through commissioning must reboot into everything-muted — never
  into a tweeter unmuted at level with no crossover loaded. Therefore the
  per-driver unmute states are **transient and never frozen as the boot config**;
  only the final, all-validated freeze step persists a loadable active config.
  When wiring the maskable emitter (the Stage-2 keystone), guard against any path
  where a partially-commissioned unmute state could persist as "safe."
  - **Mechanism — the transient load is `set_active_config_raw`, never
    `set_config_file_path` (gap-1 slice 2b-ii).** `jasper-camilla` runs CamillaDSP
    with `--statefile outputd-statefile.yml` and no `-c`, so the statefile's
    `config_path` is what it boots into, and `SetConfigFilePath` *repoints* it.
    The guarded per-driver load
    (`active_speaker.startup_load.load_driver_commissioning_config`) therefore
    applies the commissioning config **inline** (`CamillaController.set_active_config_raw`
    of the file's contents — CamillaDSP's `SetConfig`, which leaves the persisted
    path untouched), so crash-recovery-MUTED is **structural**: the running graph
    holds the per-driver config while the statefile still points at the all-muted
    staged boot config. The same transaction shape as
    `load_protected_startup_config` (snapshot anchor → preflight gate →
    `apply_dsp_config` load with rollback) but with two differences: (1) the inline
    transport above; (2) because the persisted path no longer reflects the running
    graph, the post-load gate reads the **running graph** back over the websocket
    (`CamillaController.get_active_config_raw` → `active_raw`) and re-asserts the
    mask + protective high-pass with `staging.running_commission_evidence` — a
    `yaml.safe_load`-based check robust to CamillaDSP's block-style
    re-serialization, run inside the apply lock so a drift or a stale-graph
    read-back rolls back to the staged anchor and fails closed. A precondition gate
    refuses to run unless the staged boot config is already the active graph.
    Because the commission state file can outlive the transient Camilla graph,
    `/sound/active-speaker/commission-state` overlays live runtime status from
    `active_raw`; if a saved `loaded` session no longer matches the running graph
    (or lacks the saved mask evidence from older builds), the UI reports it as
    stale and the next Start-tone POST reopens the driver test instead of
    trusting stale JSON.
    Built + hardware-free tested (`tests/test_active_speaker_commission_load.py`,
    incl. the S3 guard that the durable statefile still points at the all-muted
    staged config after a commissioning load). **The operator trigger now
    exists**: `jasper-active-speaker commission-load --group … --role …`
    (single-flight, `--dry-run` preflight) + `commission-rollback` wire the real
    `CamillaController` seams (inline `set_active_config_raw` loader,
    `get_active_config_raw` read-back, `get_config_file_path` anchor). **The
    inline-transport S3 property is proven on jts3** (a `/tmp` reload probe
    against real CamillaDSP 4.x confirmed `set_active_config_raw` left the
    statefile untouched). **Still pending the bench:** the full per-driver flow
    end-to-end + the active-lane-open at W < physical *through* a staged active
    config (jts3 was running the base `outputd-cutover.yml`, not an active-speaker
    staged config; the #741 hardware capability itself is verified — see the Stage
    2b fact). Per-driver *audible* unmute is the Stage-5 gain ramp, not this load:
    the load arms the target at the protected floor (`{gain:-120, mute:False}`) —
    a commission load is silent until Stage 5.

Every on-device step starts at the protected quiet floor: `volume_limit: 0.0`,
per-driver limiters, and the protective tweeter high-pass are preserved by the
commissioning emitter (guarded by
`test_commissioning_config_preserves_production_safety`).

## Layer Boundary

Keep DSP ownership separate, and be explicit about logical ownership
versus physical CamillaDSP placement. The v3 plan uses this model:

- **Layer A: speaker baseline.** Driver linearization, BSC, acoustic
  crossover, polarity, delay, gain, and per-driver limiters. Measured
  with near-field, null-depth, gated summed response, plus designer
  bench measurements. BSC may be physically pre-split; crossover,
  EQ, delay, and limiters are per-driver after split.
- **Layer B: room correction.** Modal-region EQ and listening-area
  correction. Measured at listening position(s). Lives on the stereo
  input pair before split.
- **Layer C: preference voicing.** Target tilt, house curve, and
  subjective bass/treble taste. Derived from published targets and
  user feedback. Lives on the stereo input pair as a reversible
  profile.

The practical CamillaDSP shape for active hardware is:

```text
source/renderers
  -> stereo-domain guards: rumble HP, headroom
  -> Layer B: room correction when enabled
  -> Layer C: target/preference voicing when enabled
  -> Layer A pre-split pieces: baffle-step / global baseline EQ
  -> N-way routing / channel map
  -> Layer A per-driver pieces: crossover, driver EQ, delay, polarity, gain
  -> per-driver limiter / protection guard
  -> physical outputs
```

**Implementation status (PR-3 + 2026-06-24 room-correction path, solo
speaker).** Layer C preference EQ and Layer B room PEQs now land on the active
graph for a SOLO active baseline. `/sound` preference apply and `/correction`
measurement/apply/reset recompose the baseline (via
[`recompose_applied_baseline_yaml`](../jasper/active_speaker/baseline_profile.py))
strictly from the immutable snapshot stored by the explicit Layer-A apply, with
room and preference filters wired on the program channels `[0, 1]` **strictly
before the split mixer** — upstream of every per-driver crossover, limiter, and
tweeter high-pass. Preference boosts ride at unity, matching the ordinary
`/sound` path; explicit `output_trim_db` and any positive room-correction boost
fold into the single `active_baseline_headroom` gain. The recomposed graph
re-proves as
`GRAPH_APPROVED_ACTIVE_RUNTIME` (the protection contract is independently
re-verified — see
[HANDOFF-dsp-graph-carrier.md](HANDOFF-dsp-graph-carrier.md)). During
measurement, `/correction/start` uses the same carrier with `room_peqs=[]` and
preference EQ disabled, so the sweep hears the raw room through the protected
speaker baseline. Active×grouping
runtime behavior is now owned by
[HANDOFF-distributed-active.md](HANDOFF-distributed-active.md): solo active
graphs keep this contract, while bonded active members use the driver-domain
Layer-A re-entry path described there.

The speaker baseline is the thing that makes the box a coherent
speaker. It should be commissioned once per hardware build and changed
deliberately. Room correction is re-run for a room/listening area.
Preference EQ is user taste and should always be reversible.
Baffle-step compensation is a speaker-baseline decision even when it
is physically placed before the 2-to-4 or 2-to-6 split on the stereo
pair. The profile schema must represent both logical ownership and
physical filter placement.

Do not confuse active-speaker `channel_map` ownership with the outputd final
DAC writer. The old `JASPER_OUTPUT_DAC_ROUTE=mono:N` / `stereo:L,R` DAC8x
alias has been removed; outputd renders directly to the detected output DAC,
and active-speaker commissioning uses the protected active graph rather than a
physical-DAC bypass. A loaded active-speaker baseline owns a zero-indexed
CamillaDSP channel map, per-driver filters, limiters, startup mutes, and the
safety gates that must protect direct-connected drivers. The persisted output
topology sits between hardware detection and the generated active graph: it
names which physical DAC lane belongs to which speaker/driver role, but it is
not itself a CamillaDSP config and cannot authorize playback.
It also records a clock-domain report for the detected final-output
device. The supported clock-domain shapes are intentionally narrow:
JTS can describe a coherent DAC8x/DAC8x Studio or single Apple output
device, and it can now describe the `dual_apple_usb_c_dac_4ch` topology
when the observed hardware has exactly two Apple child DACs on the same
USB controller/bus and exactly four physical outputs. That dual path is
one-DAC-per-speaker only and requires one `jasper-outputd` process to
open both hardware PCMs. The topology layer still has no playback
authority; it only lets downstream staging choose the active outputd
transport lane declared by the DAC profile. Generic USB DAC aggregation through
ALSA `multi`/`dmix`/`plug` or CamillaDSP multi-device playback remains
unsupported.

## Hard Safety Rules

These are not UX polish; they are anti-smoke rules.

- Do not connect the tweeter until channel identity, gain staging,
  and protective high-pass routing have been proven at low level.
- Treat a physical series protection capacitor on the tweeter as the
  preferred bench-safety path when available, but do not design the product as
  if most users will have one. If the operator does not have hardware
  protection, the supported product path is **software-guarded bring-up**, not a
  fake "physical protection present" checkbox: JTS may stage a
  no-load/no-playback candidate only after proving startup mute, protective
  high-pass, startup headroom, limiter, and volume ceiling evidence. Later
  audible slices must continue from that evidence, reset the test level to the
  floor, and keep Stop available before allowing any compression-driver tone.
- A CamillaDSP high-pass and limiter do not protect against wrong
  wiring, wrong channel maps, startup pops, DC faults, `jasper-camilla`
  not running, or a bypass path.
- Start with conservative output gain, tweeter muted, room correction
  disabled, and a temporary protective tweeter high-pass above the
  planned crossover.
- Never sweep a raw tweeter below its safe range just because an LLM
  or simulator says it is probably fine.
- A CamillaDSP limiter is a final clip stop, not a full thermal or
  excursion model. Driver protection needs gain structure, crossover
  limits, physical protection, and measured validation.
- New active configs should load with all physical outputs muted or
  routed to dummy loads until binding-post/channel identity is
  verified electrically.

## Default Commissioning Stance

The 2026-05-26 v3 proposal makes this a preset-first system. The
product does not ask end users to design crossovers from scratch.
Instead, a speaker designer creates a driver-set preset once, using
the engineering workflow below; the consumer wizard refines that
preset for the specific unit and room.

The default stance:

- Support both 2-way and 3-way active speakers through the same
  generic preset schema and N-way CamillaDSP template.
- Use an acoustic Linkwitz-Riley target by default. LR4 is the
  normal starting point; LR2 is rare and polarity-sensitive; LR8 is
  reserved for drivers that need stronger out-of-band isolation.
- Treat that as an acoustic target, not merely "insert electrical LR4
  biquads." The drivers, cabinet, protection capacitor, baffle
  diffraction, horn/waveguide, and acoustic center all shape the
  final acoustic slopes.
- Use IIR biquads for the first production baseline: low latency,
  simple CPU budget, inspectable filters, and no pre-ringing.
- Reserve FIR (`Conv`) for explicit later modes: global excess-phase
  correction, linear-phase experiments, or non-minimum-phase
  driver-inverse work after latency, CPU, headroom, and pre-ringing
  are all audited.
- Choose crossover frequency from the actual drivers and enclosure:
  tweeter safe operating range and distortion, woofer breakup and
  directivity, center-to-center spacing, baffle geometry, target SPL,
  and off-axis behavior. Do not hard-code a universal frequency.
- Store every accepted baseline as a versioned `speaker_baseline`,
  distinct from room-correction sessions and preference profiles.

For Jasper's own active bring-up build, the current no-audio default preset
is a Dayton Epique E150HE-44 plus Eminence F110M-8 2-way:
2.5 kHz LR4, non-inverted, woofer delay search range 0.0-0.6 ms,
physical compression-driver protection preferred, software-guarded no-load
staging allowed, and startup-muted outputs.
That preset is intentionally conservative for first power-up; final
crossover frequency, polarity, delay, gain trim, limiter thresholds, and EQ
must come from measurement with the actual horn/waveguide, baffle, enclosure,
amplifier gain, and microphone. The data-only default lives at
`jasper/active_speaker/presets/epique_e150he44_eminence_f110m8_safe_v1.json`.

The proposal-v3 worked example remains a B&C DE250 plus Dayton Epique
E150HE-44 2-way: 1.6 kHz LR4,
non-inverted, likely woofer delay around 0.05-0.30 ms, large tweeter
trim, conservative tweeter limiter, and a temporary protective
tweeter HP around 2x Fc during commissioning. Treat those as worked
example values, not project-wide defaults. They become defaults only
inside a named preset for that exact driver/horn/baffle/amp/channel
map combination. The first data-only version lives at
`jasper/active_speaker/presets/bc_de250_dayton_e150he44_v1.json`; it
is a worked example, not commissioned evidence.

## Measurement Protocol

Proposal v3 splits measurements into two paths: the engineering path
that creates presets, and the consumer wizard that verifies/refines a
known preset on a real speaker.

### Consumer Wizard Triad

The in-room wizard uses three complementary measurements. None is
sufficient alone; together they provide a practical room-immune Layer
A check.

1. **Near-field per-driver capture** measures individual driver
   magnitude and diagnostic phase while overwhelming room reflections.
   The mic is placed very close to the radiating surface: cone/dust
   cap for woofer or mid, dome/ribbon surface for tweeter, horn mouth
   for a compression-driver horn. This is not a free-field response
   and does not prove the acoustic sum, but it catches driver and
   assembly deviations against the preset envelope.
   The shipped relay wizard uses one fixed 3 cm on-axis capsule distance for
   this comparison, requires an explicit driver-specific placement
   acknowledgement before playback, and level-matches each role independently
   with a preset-derived tone inside that driver's protected passband. The
   resulting per-driver digital-volume locks and the shared microphone identity
   are persisted in one comparison set. Old captures without that proof, or
   captures from different sets, cannot replace a manual crossover.

   Crossover level checks and driver/summed sweeps share one durable listening-
   volume intent owned by `CrossoverLevelLease`. Before the first CamillaDSP
   volume mutation, it atomically records the finite non-positive entry volume
   in `/var/lib/jasper/active_speaker_crossover_volume_safety.json`. Normal
   cleanup first restores that exact level, then uses −60 dB only as an
   emergency fallback; setter acknowledgement is insufficient, so either result
   needs a fresh finite CamillaDSP readback within 0.05 dB. The resolved
   tombstone is written before the in-process gate clears. A crash during an
   active transition, an unreadable state file, failed readback, or failed
   tombstone write hydrates fail-closed. All new crossover level, capture,
   playback, and apply actions remain blocked until the recovery-only wizard
   action confirms exact restore or emergency attenuation. The web layer owns
   the CamillaDSP ports; the lease owns persistence and the recovery decision.
   This contract is hardware-free verified; the incremental jts3/Chrome/UMIK-2
   run remains the B2b hardware gate.
2. **Null-depth optimization** proves polarity and relative delay at
   each crossover. With the planned crossover active, invert one
   adjacent driver through the mixer and sweep the crossover band.
   Walk delay in small steps and maximize the inverted-polarity null.
   For a healthy LR4 preset, a centered null above roughly 25 dB is a
   strong pass signal; under roughly 20 dB should trigger delay,
   polarity, wiring, or hardware investigation.
3. **At-position summed measurement** is an optional diagnostic, not part of the
   first automatic product flow and not an apply gate. A future higher-rigor
   design must model every crossover region independently before summed evidence
   may authorize frequency, phase, or delay changes.

Frequency budget:

- Above roughly 500-700 Hz in normal rooms: near-field, null-depth,
  and gated summed data can validate crossover behavior.
- Around 300-500 Hz: confidence is lower. A 3-way lower crossover in
  this region must lean harder on the engineering preset and should
  be labeled reduced-confidence unless the room geometry supports a
  longer gate.
- Below roughly 300 Hz: do not pretend in-room single-position data is
  a clean speaker baseline. Hand fine work to Layer B room correction.

### Engineering Path For Presets

Every curated preset is generated once by the speaker designer using
the higher-rigor path:

- impedance / bench data where available, so protection and excursion
  assumptions are not guessed from SPL alone;
- per-driver in-box measurements with no crossover: gated far-field
  on the design axis plus near-field captures for low-frequency
  extension;
- NF/FF merge with baffle diffraction modeling, e.g. VituixCAD
  Merger, to create an anechoic-equivalent reference response;
- crossover simulation against acoustic targets, including vertical
  polar prediction and deep-null simulation;
- CamillaDSP YAML generation and `camilladsp --check` validation;
- re-measurement with the actual CamillaDSP profile loaded;
- distortion / level escalation for conservative limiter settings;
- preset freeze with expected envelopes, safe sweep ranges, delay
  ranges, polarity, limiter values, BSC parameters, and safety
  thresholds.

### Browser And Phone Capture Requirements

The phone is a smart microphone, not the analysis engine. The DSP host
generates sweeps, receives raw PCM, deconvolves/gates/analyzes, and
stores the session. The browser streams lossless binary PCM over
WebSocket. Do not use WebRTC/Opus for measurement transport.

The first wizard step must verify:

- selected input device and selected calibration file;
- echo cancellation, AGC, and noise suppression requested off and
  behaviorally sanity-checked;
- received sample rate / channel count / level are plausible;
- known-level test tone produces clean capture with enough SNR;
- the loaded calibration curve is displayed before proceeding.

Missing or wrong microphone calibration is a blocking error, not a
warning.

## Delay, Phase, and Null Verification

Delay alignment is measured, not guessed.

- Do not assume "delay the tweeter." Delay whichever acoustic source
  arrives earlier after measurement. A horn can make the tweeter
  acoustically later, which may mean the woofer receives delay.
- Compare woofer and tweeter phase traces through at least roughly
  one octave around the crossover.
- Use summed response and reverse-polarity null depth as practical
  validation. After alignment, invert one driver; a strong, centered
  null around the crossover is the clearest quick proof that the
  branches are meeting as intended on the design axis.
- Validate off-axis after the on-axis null looks good. A crossover can
  sum acceptably on-axis while creating vertical holes in the
  listening window.
- Group delay, impulse response, and step response are supporting
  views; they are not substitutes for phase-aware summation.
- Suggested acceptance gate: no "commissioned" label until the
  measured in-phase sum and the reverse-polarity null are both
  captured after loading the actual CamillaDSP profile.

> **Implementation (L2, landed 2026-06-21, corrected 2026-06-21).** The
> calibrated-mic **polarity** proposal that implements this section lives in
> [`jasper/active_speaker/crossover_alignment.py`](../jasper/active_speaker/crossover_alignment.py)
> (the `phase_aware` gate + the reverse-vs-in-phase null-margin polarity call) and
> `driver_acoustics`'s calibrated capture (`analyze_summed_crossover(expect_null=…)`).
> The operational write-up + the `/active-speaker/crossover-alignment` preview route
> are in [HANDOFF-audio-measurement-core.md](HANDOFF-audio-measurement-core.md) "L2
> calibrated crossover alignment". **The delay VALUE is deliberately NOT proposed
> from per-driver IR arrivals** — JTS's near-field captures are browser-recorded
> with no sample-sync to playback, so an arrival delta is capture jitter, not
> time-of-flight (consistent with "impulse response … [is] not [a] substitute for
> phase-aware summation" above). The delay value comes from the timing-locked
> reverse-polarity null **walk**, the documented follow-up; L2 ships the polarity
> proposal + the in-phase-null delay *status* that flags when to run it.
>
> **Update, 2026-07-11 (crossover-builder Slice 0 — alignment-SNR gate).**
> `propose_crossover_alignment` gained a SECOND gate on top of the
> `phase_aware` calibrated-mic gate above: `alignment_snr_ok` (derived by
> `commissioning_capture.build_crossover_alignment_proposal` from the summed
> capture's per-band SNR verdict — see
> [active-crossover-information-design.md](active-crossover-information-design.md)
> "Level control and SNR") and `null_depth_capped` (a measured null deeper
> than the overlap-band SNR can prove, per
> `jasper.audio_measurement.snr_policy.cap_null_depth_db`). Either
> condition, when it fires, forces `keep`/`invert` down to `review` and
> `delay_status` down from `aligned` to `unknown` — a calibrated mic alone is
> the phase_aware gate's promise, not a promise that the overlap band had
> enough SNR to trust a specific null depth. Both new parameters default to
> "no evidence" (`None`/`False`) and DELIBERATELY do not degrade at their
> default. "No evidence" here means no *real per-band* reading
> (`noise_band_report`) — no caller supplies that yet. A scalar reading is
> NOT equivalent: `correction_crossover_flow.py` already bolts a scalar
> `noise_floor_dbfs` onto every summed capture today, and the split SNR
> policy treats a scalar as insufficient evidence for an alignment call, so
> it reads as the SNR block's overall verdict `"unknown"`
> (`worst_relevant=None`). `commissioning_capture._summed_alignment_snr`
> maps that to `alignment_snr_ok=None` (no degrade) — fixed 2026-07-11 after
> a bug briefly mapped it to `False` instead, which silently downgraded
> every live summed capture's `keep`/`aligned` result to `review`/`unknown`.
> `False` is reserved for a confirmed-insufficient REAL per-band reading —
> every existing margin/blend proposal stays unchanged until a caller
> supplies one. **Automatic apply is stricter than preview:** baseline
> composition requires `alignment_snr_ok is True` and an uncapped null before
> it may mutate polarity. Unknown SNR remains visible as a proposal only.
>
> **Update, 2026-07-12 (Slice 2 — paired summed evidence + multi-region
> proposals).** `measurement.py` retained only ONE summed record per group
> regardless of polarity, so a reverse-polarity capture (`acoustic.
> expect_null=True`) recorded after an in-phase blend check could silently
> overwrite it — both can read `validated=True`/`verdict="blend_ok"`, since a
> formed reverse null IS the pass for a reverse capture. `latest_summed_by_
> group` / `latest_summed_validations` are now specifically the latest
> IN-PHASE record per group; a new `latest_summed_pairs_by_group` retains
> BOTH polarities, keyed per crossover region (`"<lower_role>:<upper_role>"`),
> resolved from a `region` block `commissioning_capture.
> record_summed_acoustic_capture` stamps at record time. `_summed_alignment_
> snr` now takes the pair (in-phase + reverse) and combines conservatively
> (either side's confirmed-insufficient SNR or capped null degrades the
> region). `build_crossover_alignment_proposal` iterates every crossover
> region sorted by fc — not only the lowest — and returns `{status,
> speaker_group_id, mode, proposals, proposal}`; `proposals` is one
> `{region, proposal}` entry per region (each independently phase_aware-
> gated on ITS OWN contributing captures' calibration), and the top-level
> `mode`/`proposal` stay the lowest region's dict for backward compatibility.
> Pairing is comparison-set scoped: the newest polarity capture anchors a
> region to its server-normalized `placement_proof.comparison_set_id`, and an
> older run may not fill the missing side. This prevents a moved microphone or
> new commissioning attempt from fabricating a same-position null margin out
> of two individually valid captures. Legacy records without placement proof
> pair only with other legacy records. A record that carries a malformed
> modern proof is not treated as legacy: it anchors the region as invalid and
> contributes no paired decision evidence.
> **Hardening, 2026-07-12.** The paired summary is historical/indexing state,
> not its own authorization boundary. `crossover_contract.
> summed_decision_evidence_state` now admits each polarity independently only
> when the record has a blocker-free, outcome-consistent analyzer result; a
> completed audible summed-test artifact; the normalized ESS played-excitation
> ledger exactly matching the immutable applied topology, baseline, per-role
> gain, delay, and polarity; a full `capture_proof_valid` binding to the active
> comparison set (including its profile fingerprint); and region
> roles plus both stamped acoustic/region Fc matching the current preset. The
> expected polarity slot must also match `acoustic.expect_null`. Legacy,
> stale-profile/set, incomplete-proof, wrong-slot/Fc, and old
> `summed_listening_position_v1` records remain visible history but supply no
> null. Automatic baseline composition consumes this same admitted proposal;
> it also requires the current preset and pre-alignment corrections (gain,
> polarity, and delay) to equal the protected profile's immutable recomposition
> snapshot, so a same-Fc family/order/trim/polarity/delay edit invalidates the
> pair. It never reads a record-supplied delay. The current wizard exposes one
> generic server-selected combined-capture action; the existing internal host
> runs the normal/reverse sequence, transient reverse graph, and bounded delay
> walk without accepting candidate metadata from the browser.
> `summed_reference_axis_v1`
> standardizes the fallback placement at approximately 1 m on the tweeter axis,
> level with the tweeter or horn-mouth center, with an explicit promise not to
> move the microphone or speaker between normal/reverse captures.
> `jasper.active_speaker.crossover_alignment.propose_crossover_alignment`
> itself is unchanged — this is wiring persisted paired evidence around the
> already-shipped proposer, per
> [active-crossover-information-design.md](active-crossover-information-design.md)
> "Slice 2: automatic alignment". The delay *walk* (a measured value) and
> post-apply verification remain separate, not-yet-built pieces of Slice 2.

## CamillaDSP Profile Architecture

The future active speaker path should use bounded profile templates,
not freeform YAML generated by an LLM.

Baseline profile shape for 2-way and 3-way speakers:

```text
stereo source
  -> optional Layer B / C stereo-domain filters
  -> Layer A pre-split baseline filters such as BSC
  -> explicit split_2way or split_3way mixer
  -> per-driver crossover filters
  -> per-driver EQ needed to hit the acoustic target
  -> per-driver delay / polarity / gain trim
  -> per-driver limiter / protection block
  -> output device
```

For a 2-way stereo speaker pair, the split maps stereo input to four
outputs: woofer L/R and tweeter L/R. For a 3-way pair, it maps to six
outputs: woofer L/R, mid L/R, and tweeter L/R. A mono cabinet can use
the same schema with a one-input variant, but the first JTS schema
should not special-case mono at the expense of clarity.

Per-driver chain order is fixed unless a named preset explicitly
overrides it:

```text
crossover(s) -> in-band driver EQ -> delay -> gain trim -> limiter
```

Important implementation implications:

- Channel labels must be explicit and persisted.
- A commissioning-safe profile should start with tweeter outputs
  muted or heavily protected.
- Polarity inversion belongs in the mixer mapping (`inverted: true`),
  not as an implicit negative gain hidden in a filter list.
- The midrange chain in a 3-way preset normally has both a high-pass
  at the lower crossover and a low-pass at the upper crossover.
- Generated configs should be validated before load, and rollback
  should be obvious.
- Candidate primitive set to preserve in schemas/tests:
  `BiquadCombo` with `LinkwitzRileyLowpass` /
  `LinkwitzRileyHighpass`, `Biquad` PEQ/shelf filters, `Delay` in
  milliseconds or samples, `Gain`, mixer `inverted: true` for
  polarity, `Limiter` with `clip_limit` / `soft_clip`, `Compressor`
  as the separate attack/release dynamics tool, and `Conv` for
  future FIR.
- Limiters belong last in each per-driver chain so they see the
  signal actually headed to the DAC/amp. Reserve negative headroom
  before positive EQ, BSC, or driver-linearization boosts.
- Bypassing a mixer that changes channel count can break a CamillaDSP
  pipeline; the profile should make bypass points intentional.
- The active baseline profile should live separately from room
  correction profiles under `/var/lib/jasper`, with its own bundle
  metadata and accepted/rejected state.

Current no-apply template command:

```sh
jasper-active-speaker startup-template ./preset.json \
  --playback-device hw:MultiChannelDAC \
  --output ./active_speaker_startup.yml
```

This command writes a candidate YAML file and runs `camilladsp
--check` if the binary is installed. A missing validator is reported
as `Validation: missing` and does not load or apply anything.

Current no-hardware path safety command:

```sh
jasper-active-speaker path-audit --requirements
jasper-active-speaker path-audit ./path_safety_evidence.json
jasper-active-speaker path-probe \
  --current-config ./active_speaker_startup.yml \
  --output ./path_safety_evidence.json
jasper-active-speaker environment-probe --json
jasper-active-speaker environment-probe \
  --config ./active_speaker_startup.yml \
  --path-safety-evidence ./path_safety_evidence.json
```

The evidence form must pass before a future loader is allowed to
touch active hardware, but a passing operator checklist is not itself
permission to load an active config. `path-audit` reports both
`requirements_met` and `ok_to_load_active_config`; the latter is true
only when evidence is marked as hardware-probe-backed. Evidence must
declare `"evidence_source": "operator"` or `"hardware_probe"` so future
loaders never infer trust level from a missing field. `path-probe` and the
`/sound/active-speaker/check-path-safety` route now populate the first
hardware-probe-backed form for the startup-load preflight. It is a local
state inspection, not an audio probe: it does not reload CamillaDSP, open ALSA,
or play tones. It can still block on missing staged evidence, unverified
physical outputs, an unreadable calibration-level guard, a missing/unreadable
rollback anchor, an unknown/custom rollback config, or a rollback config that
violates the non-positive volume-limit contract. The startup loader also
rechecks the evidence binding against the current staged candidate and rollback
anchor; stale evidence is reported as `evidence_stale` and must be refreshed
before loading. `environment-probe` adds
real read-only ALSA and CamillaDSP
config/statefile inspection plus a `safe_playback` readiness block.
`safe_playback` is not a permission grant: it reports environment readiness
but never authorizes audio by itself.
The superseded playback-readiness diagnostic and per-driver topology-tone
planner are removed. The user-facing Measure Drivers step uses the commission
ramp for active 2/3-way groups and renders no separate driver test for
passive/full-range groups. Target selection, software-guard repair,
protected graph load, and audible ramp state are owned by
`commission-load`/`commission-ramp-step`/`commission-ramp-ack`/
`commission-ramp-abort`. The probe still does not perform physical channel
verification or generate hardware-probe-backed path-safety evidence by itself.

`jasper.active_speaker.safe_playback` is the no-audio session substrate for
commission-ramp confirmation state. It writes
`/var/lib/jasper/active_speaker_safe_playback.json` by default, reports
`playback_allowed: false` in every state, expires armed sessions, and makes
Stop idempotent. The public `/sound/active-speaker/arm` route has been removed;
`commission-load` arms sessions internally. Public
`/sound/active-speaker/stop` still stops any existing session. Stop does not play
tones, reload CamillaDSP, or change volume. The persisted environment summary
stores config classification and filename only, not full local paths. The
commission state carries the current target and ramp acknowledgement state;
changing target, stopping, aborting, or letting the session expire clears that
evidence.

`jasper.active_speaker.tone_plan` retains the shared artifact vocabulary,
timing bounds, and preset loader,
but the preset-era public routes (`/sound/active-speaker/tone-targets` and
`/sound/active-speaker/tone-plan`) have been removed. Product active-driver
playback goes through the commission ramp and protected graph, not a
channel-targeted WAV writer. The reusable driver-test signal policy lives in
`jasper.active_speaker.test_signal_plan`: the production adapter consumes a
compiled `ActiveSpeakerPreset` for active 2/3-way main speakers, while the
edge-level helper already covers a future subwoofer low-pass plus subsonic floor
once the subwoofer compiler/staging slice exists. Until then, optional subwoofer
groups still fail closed before active startup staging or baseline compile.
`jasper.active_speaker.topology_tone` owns only the still-live summed-crossover
plan.

`jasper.active_speaker.playback` is now the no-audio artifact / explicit-lab
backend seam, not a product direct-DAC path. The default backend writes bounded
WAV + JSON metadata under `/var/lib/jasper/active_speaker_tone_artifacts`
(overridable in tests). `aplay` emission requires
`JASPER_AUDIO_LAB_TONE_BACKEND=aplay` and `JASPER_AUDIO_LAB_TEST_PCM` pointing
at a dedicated non-daemon test PCM. The daemon-owned outputd/CamillaDSP lanes
are rejected as test writers because they are sinks/readers in the product
route.
This remains useful for lab experiments, target-selection tests, channel count,
level clamp, artifact schema, logging, retention, and Stop semantics; product
driver commissioning uses the protected commission graph.

`jasper.active_speaker.calibration_level` owns the commissioning test-signal
level contract. It deliberately separates test volume from normal system
volume: the operator controls the requested test level, JTS clamps it to a
small safe envelope, and the default is the quietest setting (`-80 dBFS`). As of
2026-06-03 the level is a backend-owned persisted guard at
`/var/lib/jasper/active_speaker_calibration_level.json` (test override:
`JASPER_ACTIVE_SPEAKER_CALIBRATION_LEVEL_STATE`). The `/sound/` card updates
that state through `/sound/active-speaker/calibration-level`; upward movement
is limited to one 1 dB manual `set` transition. Product-facing
`raise_toward_audible` / `ramp` transitions may move by the larger bounded
audible-step constant (`AUDIBLE_RAMP_STEP_DB`, currently 10 dB) so the operator
is not forced through dozens of clicks, while lowering, reset, Stop, and
mic-clipping resets can return directly to the floor. The same route also
accepts `action=observe` with an operator-observed capture dBFS reading; that
records the coarse mic-meter status (`unmeasured`, `too_quiet`, `low`,
`usable`, `too_loud`, `clipping`) without changing the requested test level
unless clipping forces a floor reset. The web endpoint no longer resolves a
saved driver target or accepts `action=auto_step`; target-specific ramp
decisions live in the commission-ramp session. The browser cannot supply its
own auto-level cap or target protection verdict. No current code raises
listening volume, writes live CamillaDSP volume, or treats a mic observation as
permission to play. The active-speaker commission path has a cancellable
continuous tone session for the browser's driver-identification ramp. This is
still not real microphone capture or calibrated SPL; it is the
operator-observed feedback loop the first audible slice can consume.

`jasper.active_speaker.bringup` owns the read-only preflight packet for the
high-frequency bring-up product decision. It composes output topology, channel identity,
staged software-guard evidence, calibration-level floor state, safe-session
state, tone-backend status, and coarse microphone readiness into two bounded
modes:

- **Manual guarded bring-up**: available without a microphone for users who
  already know the crossover plan, but only after topology, output identity,
  active-speaker environment/load gate, staged guard evidence, Stop, and
  level-floor gates pass.
- **Guided calibration**: requires the same gates plus working microphone
  capture. A calibrated mic enables absolute guidance; an uncalibrated but
  working mic can provide relative safety feedback only. JTS must not label
  unmeasured/manual work as calibrated.

## Deterministic Tooling Roadmap

Code should eventually own:

1. Active topology detection: output channel count, named physical
   channel map, and "all audible paths are crossover-protected" gate.
2. Preset schema loading: way count, driver roles, expected
   near-field envelopes, crossover regions, safe sweep ranges, delay
   ranges, polarity, gain trims, limiter values, BSC parameters, and
   pass/fail thresholds.
3. Commissioning-safe CamillaDSP profile generation for 2-way and
   3-way templates.
4. Channel identification: quiet band-limited tone per output, with
   DMM/oscilloscope or dummy-load verification before drivers are
   connected, then operator confirmation with low-level band-limited
   tones.
5. Per-driver measurement mode: isolate woofer/mid/tweeter, enforce
   safe sweep range and level, and record active filters.
6. Null-depth delay/polarity search per crossover region.
7. Gated summed-response verification through crossover regions.
8. Measurement import: REW/VituixCAD FRD/IR imports first; REW local
   API integration is plausible later.
9. Provenance in bundles: driver, angle, axis, distance, timing
   reference, mic calibration, gate/window, active profile, sweep
   voltage/SPL, amp gain, output channel map, protection-cap state,
   protective-HP state, smoothing, ZMA/impedance files, and raw FRD /
   IR / capture paths.
10. Crossover candidate compiler: structured crossover/filter/delay/
   gain/limiter data to validated CamillaDSP YAML.
11. Delay/polarity checks: predicted sum, measured sum, inverted-null
   depth, phase tracking, and group-delay plots.
12. Acceptance gates: no "commissioned" label without timing-valid
   driver measurements and at least minimal off-axis validation.
13. Rollback and A/B: accepted speaker baseline, previous baseline,
    room correction bypass, preference EQ bypass.
14. Thermal/level validation: step up in small increments, monitor
    woofer excursion, tweeter distortion, limiter activation, digital
    clipping, and Pi underruns at the intended sample rate/chunk size.

Updated execution plan:

1. **Substrate slice**: implement data models and validation for
   speaker presets, active channel maps, and baseline profiles without
   loading them onto hardware yet. Started 2026-06-01 as
   `jasper.active_speaker`; current scope is validation plus muted
   startup-template generation only, not live DSP loading.
2. **Safe config slice**: generate 2-way and 3-way CamillaDSP
   templates with explicit muted/protected startup state, validate
   them, and make rollback mechanical. Started 2026-06-01 as a
   no-apply startup-template emitter and `jasper-active-speaker
   startup-template` CLI. The CLI writes candidate YAML from preset
   JSON and runs `camilladsp --check` when available. Expanded
   2026-06-03 with `jasper.active_speaker.staging`,
   which binds the saved output topology to the Epique/F110M safe
   bring-up preset and writes a protected startup candidate plus
   evidence metadata without loading CamillaDSP or emitting sound.
   Expanded 2026-06-11 so the product `/sound/active-speaker/stage-config`
   route stages from the fresh crossover-preview artifact instead of silently
   falling back to the packaged preset, with mono/stereo active 2-way and 3-way
   support for contiguous low output blocks.
   Expanded 2026-06-04 with `jasper.active_speaker.startup_load`, which
   can load that staged startup graph through the shared DSP apply
   lifecycle only after deterministic gates pass, persists the prior
   config as a rollback anchor, and exposes rollback through `/sound/`.
   This is still not a playback slice.
3. **Engineering interop slice**: import REW/VituixCAD measurement
   artifacts and freeze the first named preset before attempting an
   end-user wizard. Started 2026-06-01 with a data-only DE250 +
   E150HE-44 worked-example preset; added the Epique E150HE-44 +
   Eminence F110M-8 safe bring-up preset on 2026-06-03 for Jasper's
   immediate mono cabinet build. Real engineering artifacts, expected
   envelopes, and limiter thresholds are still future work. Expanded
   2026-06-11 with a no-audio design-draft and crossover-preview layer
   that captures operator driver intent, bounded external research JSON,
   and deterministic starting filter intent before any CamillaDSP YAML is
   generated.
4. **Channel and path safety slice**: prove every audible source
   path, including TTS/cues and test tones, flows through the active
   baseline and cannot bypass tweeter protection. Started 2026-06-01
   with `jasper.active_speaker.path_safety` and `jasper-active-speaker
   path-audit`, which encode and evaluate the required evidence but
   do not probe hardware yet. Expanded 2026-06-02 with
   `jasper.active_speaker.environment` and `jasper-active-speaker
   environment-probe`, which inspect ALSA playback devices and the
   current/provided CamillaDSP config without playback, reload, or
   mutation. Manual/operator evidence can pass the checklist, but
   future loading remains blocked until hardware-probe-backed evidence
   exists and the active startup candidate validates. Expanded again with
   `jasper.active_speaker.safe_playback`, which provides no-audio arm/stop
   session bookkeeping for the future tone path without authorizing playback.
   Historically expanded with `jasper.active_speaker.tone_plan`, which prepared
   preset-derived, clamped channel-test plans while still forbidding playback.
   That standalone planner was later removed when product per-driver playback
   converged on `test_signal_plan` plus the protected commission ramp;
   `tone_plan` now retains only shared artifact vocabulary, timing bounds, and
   preset loading.
   Expanded with `jasper.active_speaker.playback`, an artifact-first backend
   seam that renders bounded logical-output WAV artifacts and, only with
   explicit audio-lab env selection, can run the generated artifact through
   `aplay` for woofer, mid, and subwoofer topology targets only.
   Historical, now superseded: expanded with `jasper.active_speaker.readiness`, a read-only
   playback-readiness gate that evaluates one requested output target across
   safe-session, output topology, channel identity, tweeter protection,
   clock-domain, active-config/path safety, calibration-level, Stop evidence,
   and tone-backend status. It was the contract the lab backend evaluated
   before the product converged on protected commission-load/ramp playback. Default installs returned
   `playback_allowed: false`; the lab `aplay` backend can make woofer, mid, and
   subwoofer targets eligible only after protected startup load evidence is
   current.
5. **Consumer W0 slice**: prototype phone-as-mic raw PCM WebSocket
   capture, calibration blocking, browser processing sanity checks,
   and resumable server-side session state.
6. **Consumer W4-W7 slice**: add per-driver near-field checks,
   null-depth delay search, and gated summed verification against the
   preset envelopes.

This deliberately avoids starting with an LLM-guided active wizard.
The first product value is deterministic safety and repeatability.

## LLM Boundary

An LLM advisor can help explain, sequence, and translate:

- explain why a crossover dip is not a room mode;
- ask whether timing reference was used;
- explain null-test results and off-axis concerns;
- suggest which deterministic check to run next;
- generate user-facing summaries and audit-log narration.

The LLM must not:

- emit arbitrary CamillaDSP YAML;
- decide to remove tweeter protection;
- invent limiter thresholds without driver/amp data;
- call magnitude-only data valid for phase alignment;
- silently fold room correction or taste EQ into the speaker baseline.

## Open Questions

- What exact output hardware will the active build use: multi-channel
  USB DAC, HAT, DSP amp board, or separate amp chain?
- Is the target a mono active cabinet with two outputs, a stereo
  active pair with four outputs, or both?
- What exact woofer, tweeter, horn/waveguide, baffle, enclosure, and
  center-to-center spacing will Jasper's first active build use?
- How should TTS/cues be routed so they always pass through the same
  crossover protection as music?
- Which parts should be in-product JTS tooling versus external
  REW/VituixCAD workflow with imports?
- Does the active wizard use the current SciPy/NumPy ESS code path,
  adopt `pyfar`, or wrap both behind one analysis interface?
- How reliable is external USB/Lightning microphone enumeration and
  raw `getUserMedia` capture on current iOS Safari and Android Chrome
  when EC/AGC/noise suppression are disabled?
- Does the deployed CamillaDSP version expose the limiter/filter
  primitives we want, or do we need a compatibility layer?
- What profile schema should represent speaker baseline versus room
  correction versus preference EQ?
- For 3-way speakers with a lower crossover around 250-500 Hz, what
  pass/fail language accurately communicates reduced in-room gating
  confidence without blocking useful commissioning?
- What exact startup sequencing, amp standby/relay behavior, and
  subsonic/rumble high-pass should be mandatory before active output
  is considered safe?

## Failure Modes To Keep Visible

- Full-range signal reaches tweeter due to wrong channel map,
  disabled crossover, daemon crash, or bypass audio path.
- On-axis-only optimization creates vertical lobing/nulls around the
  crossover.
- USB mic measurement without timing reference is used for delay
  alignment.
- Listening-position room correction hides a speaker-baseline
  crossover problem.
- Limiter threshold is treated as thermal/excursion safety when it is
  only a digital peak guard.
- Protection capacitor or horn path changes acoustic response, but
  measurements were taken without the final hardware in place.
- FIR is enabled without latency, CPU, headroom, and pre-ringing
  checks.
- Startup/reboot/USB-clock pops reach amplifiers before CamillaDSP is
  running and protected.
- A user edits only the woofer low-pass or bypasses a filter for
  comparison, accidentally leaving the tweeter full range.
- WebRTC or browser voice processing touches the measurement stream,
  making deconvolution/level data untrustworthy.
- The wrong microphone calibration file is loaded, or no calibration
  file is loaded, and the wizard treats it as a soft warning.
- A 3-way lower crossover in the 250-500 Hz region is judged with an
  indoor gate that cannot support that frequency range.

## Source Reports

This handoff distills three raw research artifacts archived under
[`docs/research/2026-05-25-calibration-agent/`](research/2026-05-25-calibration-agent/README.md):

- [`active-speaker-dsp-commissioning-architecture.md`](research/2026-05-25-calibration-agent/raw/active-speaker-dsp-commissioning-architecture.md)
- [`active-crossover-measurement-workflow.md`](research/2026-05-25-calibration-agent/raw/active-crossover-measurement-workflow.md)
- [`jts-two-way-camilladsp-commissioning-plan.md`](research/2026-05-25-calibration-agent/raw/jts-two-way-camilladsp-commissioning-plan.md)

It also incorporates the 2026-05-26 proposal-v3 methodology supplied
in the working session: generic 2-way/3-way active commissioning,
three-layer DSP separation, near-field/null-depth/gated measurement
triad, preset-first architecture, phone-as-mic raw PCM transport, and
the DE250 + E150HE-44 worked example.

Key external prior-art families named by the reports:

- REW for measurement, timing reference, impulse/phase/group-delay
  inspection, and possible local API integration.
- VituixCAD for crossover simulation, near/far merge, directivity,
  listening-window, and polar validation.
- CamillaDSP for active routing, IIR biquads, FIR convolution,
  gain/delay/polarity, and limiter/compressor primitives.
- Linkwitz/Riley/Vanderkooy/Lipshitz crossover literature for
  non-coincident driver integration.
- rePhase / DRC-FIR and CamillaFIR if verified as later FIR
  references, not first implementation defaults.
- Charlie Hughes / Voice Coil measurement geometry, Purifi and Rod
  Elliott acoustic-center/BSC cautions, Klippel-style protection
  thinking, miniDSP active-cap guidance, Hypex Filter Design UI
  patterns, `pyCamillaDSP`, `camillagui`, `pyCamillaDSP-plot`,
  `wirrunna/CamillaDSP-Building-a-Config`, and
  `mdsimon2/RPi-CamillaDSP`.

Last verified: 2026-07-15 (Wave 1 target-bound research, visible confirmed
driver-safety profile, excitation admission, nine-state lifecycle, exact
eligibility receipt, reachable isolated-driver persisted admission under one
bounded writer transaction, legacy direct summed-endpoint pre-audio refusal,
the recorder-only production summed relay, signed geometry persistence, durable
calibration/device binding, and the typed summed graph/apply/capture/restore
runtime with exact graph/path/listening-volume
readback, exact adjacent-group binding, group-scoped transforms, safe-volume-
before-graph ordering, a transient graph ceiling no louder than the admitted
measurement volume, full-envelope 20 ms delay headroom, fresh transformed-
graph safety reproof, expiry of the raw transport's playback capability when
transport ends, required mutation-journal callbacks, locked predecessor
recovery, and cancellation-drained cleanup,
strict group-by-region
normal/reverse/delay evidence values with typed run/attempt and geometry
authority, the bounded low-frequency coarse-plus-refinement schedule,
schedule-aware final evaluator,
complete-plan replay guards, production fixed-axis relay population of the
strict three-repeat-per-driver aggregate, the store-backed pure deterministic electrical
candidate evaluator, exact candidate persistence/readback and `candidate_ready`
review projection, writer-locked measured-candidate compiler/apply, fresh
protected graph/path/volume readback, exact cancellation/failure/restart
restore, retained-proof finalization, receipt schema-v2 one-shot roles, and the
durable bundle-backed commissioning-run store/start/status boundary and service owner-
generation claim; no live post-apply automatic verification/receipt producer.
The versioned passive/manual-applied Room decision and fail-closed automatic
branch were checked contract-only; the candidate apply boundary was checked
hardware-free and no hardware behavior was revalidated in that pass. Frozen applied-preset startup anchor, durable
crossover-volume intent, confirmed recovery,
and relay lease ownership checked; bounded CamillaDSP worker cancellation checked
against the outer commissioning rollback transaction; superseded readiness and
per-driver topology-tone planner removal checked against the protected commission ramp and retained
summed-crossover planner; prior 2026-07-11 pass covered per-driver protected level tones and gain locks,
relay placement acknowledgement + durable comparable capture-set contract,
automatic excitation SSOT, room readiness, applied
Layer-A snapshot, and relay crossover flow; prior 2026-06-24 room-correction
start/apply/reset on solo active
baselines checked against `jasper.web.correction_setup`,
`jasper.sound.graph_carrier`, `jasper.active_speaker.camilla_yaml`,
`jasper.active_speaker.baseline_profile`, and
`jasper.active_speaker.runtime_contract`; prior 2026-06-23 pass covered
active-speaker `/sound/` commissioning UX checked against
`deploy/assets/sound-profile/js/main.js`, `jasper.web.sound_setup`,
`jasper.active_speaker.calibration_level`, and the focused `/sound/` tests for
channel selectors, cancellable combined-test Stop, phone-mic removal from the
core flow, one-intent save/apply, and the 10 dB audible ramp step. Prior
2026-06-22 recheck covered topology
reset recovery and stale `/sound/output-topology` POST guard against
`jasper.web.sound_setup`; active-speaker commissioning state against the focused
`/sound/` tests.)
