# JTS cleanup and ownership review — 2026-10-02

Frozen observations at `2290b8924baafb9ad57f03277fe90fc64951b11f` on `main`.
The remote main ref was checked again after the review and still named this SHA.
Six read-only investigators covered voice/AEC, measurement/tuning, control/web,
Rust/deploy, docs, and dead code/tests; the conductor verified and deduplicated
the findings. This report records the review, not a live implementation ledger.
No implementation changes, GitHub posts, hardware operations, or paid sessions
were performed.

## Scope and limits

This was a targeted cleanup review with repository-wide inventories and reference
searches, not a line-by-line review of every file. The dead-code investigator
screened 918 production Python modules, 942 test/helper modules, and 7,724
production top-level definitions using AST/reference searches. The measurement
investigator screened 328 modules and directly read 35 source files in full or
in excerpts. The docs investigator directly inspected 17 current/governing
documents and ran `python scripts/docs-linkcheck.py --all`: 459 Markdown files,
no failures. Link validity does not establish semantic accuracy.

Candidates were checked against callers, package exports, registries, entry
points, dynamic loading, deployment and CI references as applicable. The
conductor independently checked the principal findings; a second investigator
specifically challenged the repeat-store and saved-gate deletions. Production
tests were not run. Runtime behavior, hardware compatibility, timing, and the
contents of deployed state files were not established by this review.

P2 below means worthwhile cleanup or ownership work, not a demonstrated runtime
failure. P3 means smaller debt. Confidence refers to the observation; an
extraction's implementation shape still needs ordinary review.

## Findings

### F01 · P2 · Retired repeat-admission machinery remains on startup

`jasper/active_speaker/repeat_admission.py:155-200` only aborts old reservations.
The module has no current reservation-producing API and no production consumer
of those results. Its only production caller is the startup claim in
`jasper/web/correction_setup.py:734-743`. The installer still provisions its lock
at `deploy/lib/install/state-and-secrets.sh:124`; the old budget has a dedicated
test in `tests/test_active_speaker_repeat_reservation_sinks.py:9-14`.

Delete the obsolete module, startup claim, subject-only tests, lock row, and the
stale owner reference in `jasper/playback_state/capture_protocol.py:88-93`.
Existing state can remain inert. Preserve the adjacent live session-volume and
neutral-graph startup recovery. Retarget the installer's symlink/hardlink tests
to a current lock; their generic path-safety coverage is still useful.
High confidence; independently verified. Fits the existing no-backward-support
direction in #2902 and ADR-0381.

### F02 · P2 · Dead APIs are kept alive by tests, or have no references at all

| Subject | Definition | Evidence |
|---|---|---|
| `excitation_covered_bands`, `apply_noise_band_fallback` | `jasper/audio_measurement/snr_policy.py:183-267` | Only test callers; current SNR path is `program_analysis/response.py:262-300` |
| `apply_gate_fragment` | `jasper/audio_measurement/gating.py:738-770` | Only three tests; current response path uses `gate_impulse_response` |
| `assert_alignment_confident`, `AlignmentError` | `jasper/audio_measurement/alignment.py:165-190,37-43` | Only test callers; live signal analysis calls `cross_correlation_alignment` directly |
| `rollback_candidate` | `jasper/web/correction_crossover_v2_status.py:32-53` | Sole caller is `tests/test_crossover_v2_pin_apply_rollback.py:66` |
| `cap_capture_tail` | `jasper/audio_measurement/deconv.py:76-101` | Definition only |
| `_is_exact_version` | `jasper/audio_measurement/bundles.py:211-214` | Definition only |
| `take_phase_composition` | `jasper/active_speaker/crossover_v2/position_cycle.py:71-87` | Definition only |
| `CaptureFailed` | `jasper/active_speaker/crossover_v2/capture_source.py:57-58` | Definition only |
| `bounded_int` | `jasper/active_speaker/_common.py:99` | No live consumer found |

Delete these subjects and tests specific to retired behavior. Preserve scientific
invariants on active primitives: in particular, the gate ending at reflection
onset rather than reflection arrival (`test_audio_measurement_gating.py:672-686`)
should stay covered through the current window builder. Keep live correlation
primitives and `DEFAULT_CONFIDENCE_THRESHOLD`. Do not delete the entire rollback
test file: its remaining apply tests exercise live behavior. Its stored
`previous_*` lineage needs a separate semantic-consumer check before deletion.
High confidence in absence of production callers; tests alone are not a reason
to retain a retired production API.

### F03 · P3 · Refusal registry contains codes with no producers

`crossover_v2/refusal_copy.py:1321-1331,1404-1423` retains
`verify_crossover_region` and `cloud_geometry_locked`, with no production
producer outside the registry. The latter keeps the entire 22-line
`crossover_v2/spatial/group_floor.py` and its package export alive.

Remove the entries, constants, group-floor module/export, and subject-only tests.
Historical persisted strings would use generic unknown-failure display; this is
a deliberate presentation change, not proof that no old record exists. Fits
#2902; the prior audit also referenced #1868 for the crossover-region code.

### F04 · P2 · Voice restart decisions are repeated by their consumers

`jasper/control/service_restart.py:36-44` decides whether a provider or follower
state prevents restart, but returns only `SKIPPED`. The web adapter explicitly
repeats both conditions (`jasper/web/tools_setup.py:628-655`), while the CLI
rereads provider state to infer the reason and its decision order
(`jasper/cli/settings.py:136-147`).

Return the outcome and reason from the restart owner. CLI and web should render
that result, preserving their existing response formats and throttling behavior.
High confidence. Existing settings and tools-web behavior tests cover the
relevant surfaces; no new generic restart framework is needed.

### F05 · P2 · Wi-Fi operations and HTTP presentation have one owner

`jasper/web/wifi_setup.py` has 1,353 lines. NetworkManager command handling starts
at 141, scan/repair orchestration occupies 507-692, credentials/persistence
791-900, and connection/rollback 903-1047. Rendering begins at 1097 and HTTP
handlers at 1181. The nmcli stdin constraint is additionally validated inside
the HTTP handler at 1266-1273.

Move concrete networking operations into `jasper/net/wifi.py`, beside the
existing scan-repair and guardian-persistence modules. Keep HTTP parsing and
rendering in the web module; keep nmcli protocol validation with the operation.
High confidence in the mixed responsibility. Preserve credential handling,
rollback order and timeouts; the secrets review tier applies to implementation.

### F06 · P2 · Large test suites also serve as fixture libraries

`tests/test_active_speaker_runtime_contract.py` is 5,236 lines and is imported
through 74 statements in 35 other test/helper files. Even
`tests/control_server_fixtures.py:95` imports it for a topology builder.
`tests/test_plan_run.py:72-74` imports `crossover_v2_fixtures`, whose autouse
fixture imports `fake_program_baselines` back from that test at
`tests/crossover_v2_fixtures.py:858-865`.

Move shared builders into the existing `tests/active_speaker_fixtures.py` or a
focused helper sibling. Move `fake_program_baselines` out of the test suite and
remove the reverse dependency. Consolidate the duplicate dual-Apple builders
at `test_output_contract.py:33-79` and
`test_active_speaker_runtime_contract.py:1958-1986`, plus active/subwoofer copies
in `test_ring_active_endpoint.py`. Preserve assertions and fixture scopes.
High confidence; conductor independently reproduced the import counts. Verify
collection, affected behavior, and isolation when implementing. `test_plan_run`
also overlaps active PR #6156, so that small move should follow its landing.

### F07 · P2 · The AEC startup config does not fully own startup settings

`jasper/aec/bridge_config.py:7-9` promises a single env-reading surface, but
`jasper/cli/aec_bridge.py:798-815,860,991-1012` separately reads gain, watchdog,
debug and corpus/chip settings. `jasper/aec/bridge_corpus_lanes.py:439,461,512,594`
rereads flags that main has already parsed.

Finish the existing `BridgeConfig` boundary: resolve bridge-level startup
settings once and pass resolved values to builders. Keep engine-specific
configuration with its engine. High confidence; this is hidden configuration
coupling, not a demonstrated live race or performance defect. Preserve current
startup and no-silent-deafness behavior.

### F08 · P2 · An AEC source selector no longer selects an implementation

`jasper/aec/bridge_config.py:81-86,367-398` retains the retired `alsa` token and
its translation/refusal machinery. Current writers in
`jasper/aec/reconcile/runtime.py:256` and
`jasper/wake_corpus/capture_plan.py:463` only emit `outputd_udp`; the implemented
reference transport is `bridge_reference.outputd_ref_udp_thread`.

Make that source a fixed fact, retaining endpoint configuration and runtime
reference health/provenance. Remove the obsolete selector axis, its writes,
legacy resolver and tests. Do not replace tolerance with a new microphone park.
High confidence in the obsolete axis; deployed state was not inspected. Fits
#2902's existing direction.

### F09 · P3 · ActiveProviderState computes an unused second model answer

`jasper/voice/provider_state.py:112,191-196` carries and calculates `.model` from
only the provider file. All production consumers of this state use its provider
or diagnostic fields. Model consumers already use the merged-file resolver at
203-236; only tests read the old field.

Delete the field and its calculation/constructor arguments/tests. Keep the
merged-file resolver and its precedence tests. High confidence. Besides dead
work, this removes an API that could give a different answer when the operator
base file alone selects a model.

### F10 · P2 follow-up · Turn state crosses its declared ownership boundary

`jasper/voice/turn_lifecycle.py:91-92,159-184` declares and allocates turn/input
state, while `jasper/voice_daemon.py:1095-1195` directly changes it during manual,
endpointed and continuous input. The investigator counted 12 direct writes to
seven lifecycle fields, plus nested speech-state writes.

A focused input-state owner would let the wake loop feed frames/events and the
lifecycle begin/end the input and consume its outcome. Confidence is high in
the coupling, medium in that proposed extraction shape. Preserve timing,
barge-in, manual input, gap handling and failed-turn cleanup. This is larger
than a deletion and does not establish a current functional bug.

### F11 · P2 follow-up · TTS transport and playout policy remain mixed

`jasper/runtime/tts_playout.py:189-542` owns socket locking, poisoning, bounded
I/O and flush acknowledgements; the same module owns PCM/pacing/drain accounting
at 1044-1250 and profile recording at 1252-1299. Control retry logic is duplicated
at 917-963 and 1014-1039. `_OutputdStreamAdapter` actually connects to
`FANIN_TTS_SOCKET` (603).

Move the existing socket adapter/protocol helpers into a concrete fan-in TTS
client module and share the duplicated control retry path. Keep playout policy
in `TtsPlayout`. Preserve bounded synchronous measurement-pause semantics and
partial-write/flush/cancellation behavior. High confidence in the responsibility
split; no generic transport layer is warranted.

### F12 · P3 · Camilla Delay YAML has two identical emitters

`jasper/active_speaker/camilla_yaml/filters.py:85-92` duplicates
`jasper/audio_routes/camilla_emit.py:140-153`, whose module is already imported.
Use `emit_delay_filter` at the four callers, passing explicit zero where needed,
and delete the local helper. High confidence. Existing emitted-graph fixtures
should remain identical; applicable output-path review still applies.

### F13 · P2 small · JSON sanitization creates an unnecessary analysis dependency

`jasper/active_speaker/run_manifest.py:289` imports `finite_json` from
`crossover_v2/capture_provenance.py:25-38`; its own lazy-import comment says this
loads the analysis stack. The helper only walks mappings/sequences and replaces
non-finite floats.

Move it unchanged into existing `jasper/platform/json_fields.py`. Do not blindly
merge the script helper in `scripts/analyze-correction-diagnostic.py`: it has
additional NumPy/key-conversion behavior. High confidence; modest cleanup.

### F14 · P3 · The same attenuation floor is declared twice

`jasper/audio_measurement/program_analysis/model.py:182-186` explicitly mirrors
the -60 dB rule in `jasper/active_speaker/level_trim.py:18-20`.
Share the rule from an existing lightweight lower-level owner; do not introduce
an upward kernel-to-active-speaker import. High confidence in duplication;
the values agree today.

### F15 · P3 · Retirement framework keeps capabilities no shipped row uses

`deploy/lib/install/retirements.sh:25-31` has one `file` row. Unit, directory,
and env-key branches at 51-96 remain exercised by synthetic test rows in
`tests/test_install_helpers.py:3444-3640`.

Keep the required file retirement and its removal condition. Delete unused
capabilities and subject-only tests. Do not remove the actual file cleanup until
its stated fleet condition is met. High confidence; fits #2902. Installer and
secret-path review obligations apply to implementation.

### F16 · P3 · The installer copies a derived reconciliation timeout

`deploy/lib/install/systemd-units.sh:1257` hardcodes `2737s`, while
`jasper/platform/source_intent_units.py:410-415` owns the calculation. A pure
import from the pinned checkout confirmed 2727 seconds for systemd and 2737
for the outer/broker budget: there is no current numeric mismatch.

Read the owner through the existing installer budget-loading pattern at
`systemd-units.sh:43-55`; avoid a new knob or generator. High confidence in
duplication, low priority.

### F17 · P2/P3 · Current documentation contains semantic drift

| Current text | Contradiction at the audited SHA | Minimal correction |
|---|---|---|
| `docs/testing-tooling.md:200-203` advertises removed stop, calibration-level, commission and summed-validation POST routes | Complete POST table is `jasper/web/sound_setup.py:791-813`; nginx forwards to it | Delete stale inventory and link to the current route/runbook owner |
| `docs/bringup.md:592-602` calls Camilla main_volume the universal software-volume owner | `playback_state/music_sources.py:42-74` distinguishes push sources from Camilla-master; HTTP calls VolumeCoordinator | Describe canonical user volume and link to `docs/audio-paths.md:234-239` |
| `docs/testing-tooling.md:57-60` limits all-features to host-clock | `scripts/check-rust.sh:142-145` uses workspace/all-targets/all-features | Remove obsolete per-crate exception |
| `jasper/multiroom/reconcile_plan.py:202-221` documents active_endpoint/env writes in snapclient_argv | Its signature/body does neither; the actual owner is `grouping_env.py:109-160` | Delete the misplaced copied block |

High confidence from direct code comparison. These are current-authority
problems, not reasons to erase historical ADRs, audits or research evidence.

## Healthy structure and rejected candidates

- `jasper/` has only three top-level Python modules plus `__init__.py`:
  `config.py`, `voice_daemon.py`, and `mux.py`. The broad package reorganization
  has landed. Another top-level reshuffle has no demonstrated benefit here.
- Provider adapters share LiveConnection/LiveTurn and their base/supervisor;
  wizard specs, common HTTP helpers, tool packs and control handlers have
  identifiable owners. These do not need new frameworks.
- Large Rust files often include extensive inline tests. For example,
  outputd/state tests start at line 997 of 2533; config tests at 814 of 1984;
  fanin/tts tests at 1443 of 3263. Length alone is not a god-file finding.
- Fan-in and outputd TTS serve distinct pre/post-DSP paths. Shared protocol,
  parser, server and flush types already live in jasper-tts-protocol. The
  separate engines are not interchangeable duplicate code.
- No unused dependency or orphan whole production module was verified by the
  general orphan scan. Initial candidates resolved to legitimate entry points,
  callbacks, doctor decorators, or dynamic round-view imports.
- A suspected room-window documentation mismatch was rejected after tracing
  the reader: room views deliberately use the banked ungated curve.
- Existing issues #2902, #4816, #5929 and #5982 were consulted. Old unchecked
  checklist text is not current source evidence: the narrow-wire symbols are
  absent, and shared Rust flush types already live in
  `rust/jasper-tts-protocol/src/flush.rs`. Do not re-file those as new findings.
- Active PR #6156 changes branch leveling and its plan-run tests. No finding
  above requires altering that branch-level calculation during this review.

## Verification after any implementation

Use existing affected behavior tests and the repository's normal merge/review
rules. Deletion of unused code does not warrant a new permanent tree-scanning
gate. Test-fixture moves need collection/isolation checks. Output-path,
credential and installer changes retain their applicable non-negotiable review
requirements. Hardware behavior remains a separate verification obligation;
this static review supplies no claim about live audio, wake availability or Pi
performance.
