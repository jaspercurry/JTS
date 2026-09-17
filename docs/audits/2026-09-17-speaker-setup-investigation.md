# Speaker setup investigation (code-only), 2026-09-17

- **Audited SHA:** `25d049f37` (`origin/main`, merge of #5265). Frozen report
  under [ADR-0284](../adr/0284-audits-are-frozen-reports-and-issues-are-the-ledger.md);
  findings that are not fixed inline are filed as `audit` +
  `audit-2026-09-17` issues (list at the end).
- **Scope:** the path from a blank speaker to a playing speaker on
  `/sound/speaker/`: output topology, design draft, driver research loop,
  driver safety profile, crossover preview, baseline profile apply, the staged
  startup anchor, commissioning steps, reset, and what the box boots with.
- **Method:** every in-scope file was read at HEAD (server, client, tests,
  ADRs). Five non-overlapping sub-agents supplied breadth (tests, ADR/docs,
  runtime/startup, hardware reconcile, git archaeology of each gate); every
  line reference below was re-checked in the tree by the author. No Pi was
  touched; nothing in this report is a hardware observation.
- **Non-negotiables honoured as the only legitimate "safety" (AGENTS.md):**
  CamillaDSP `volume_limit` 0.0 and the graph doors that refuse a positive
  limit; never `SAVE_CONFIGURATION`; secrets; deploy script; renderer ALSA
  rule; no silent deafness; paid tests; protected `main`. Everything else on
  this path that calls itself safety is graded below.

Line references are `path:line` at the audited SHA. Bare file names
resolve as: `.js` files under `deploy/assets/sound-profile/js/`;
`sound_setup.py`, `sound_active_speaker.py`, `correction_crossover_v2_apply.py`
and `_common.py` (web) under `jasper/web/`; `output_topology.py`, `camilla.py`,
`camilla_config_contract.py` and `dsp_apply.py` under `jasper/`; `reconcile.py`
and `dac.py` under `jasper/audio_hardware/`; everything else under
`jasper/active_speaker/`.

## 0. Summary

1. **The box boots silent after an Apply because the boot graph selector
   demands a per-lane "identity confirmed" bit that Apply never sets.** Apply
   loads the baseline straight into CamillaDSP over the websocket
   (`jasper/web/correction_crossover_v2_apply.py:104-119`), bypassing
   `safe_graph_for_current_topology`. The next reconcile or boot re-decides
   from scratch and the two rungs that would keep the applied graph are gated
   on `roleful_identity_confirmed`
   (`jasper/active_speaker/runtime_contract.py:4502-4557`), which is true
   only after the operator presses "Confirm output" for every assigned lane.
   Unconfirmed, a roleful box falls to the staged all-muted anchor (only
   commissioning writes it) or to `parked_muted`. Section 7.
2. **The driver vocabulary is spelled in at least nine places** across
   `_common.py`, `driver_safety.py`, `design_draft.py`, `main.js`,
   `driver-model.js` and `driver-fields.js`; #5257 was the second drift of one
   of them. Section 1.
3. **Every "stale"/fingerprint gate on the research loop except one is a
   nanny.** The request fingerprint, the result fingerprint, the
   `_validate_v2_research_prefill` comparison, the design-draft
   `expected_revision`, and the client's `invalidateDriverResearchBinding`
   protect a two-tab race on a one-owner box; #5262 and #5259 were caused by
   those gates, not prevented by them. Section 4.
4. **~230 lines of the web layer and ~770 lines of `web_commissioning.py`
   are dead:** the summed-test and commission-tone session machinery has no
   writer and no route since ADR-0230 / the callerless-route deletion
   (`git c958a173f`). Section 8.
5. **The client re-derives server facts** (research targets, identity report,
   step state, preview readiness, confidence ranking, style floors,
   vocabulary validation) and then sends the server back its own artefacts
   to be re-validated (`driver_research_request`, `expected_revision`,
   `topology_revision`, `detected_hardware_identity`,
   `expected_candidate_fingerprint`). Section 2 and 5.
6. **The cardioid template is two shapes on the client and one on the
   server:** the server stores it as `active_2_way` plus a `rear` woofer
   variant (`jasper/output_topology.py:944-951`), the client's template table
   agrees (`topology.js:262`), but the axes UI shows it under "Active 3-way"
   (`main.js:1414`). Section 1.
7. **Section 4 grades 50 gates: 22 keep, 28 removable.** The ranked cut
   list in section 10 removes roughly 5,400 product lines, all 28 removable
   gates and the reboot park, without touching a non-negotiable.

## 1. Single source of truth

### 1a. Driver vocabulary and allowed keys

| # | Holder | File:line | Content | Authoritative today? |
|---|---|---|---|---|
| 1 | `MANUAL_DRIVER_FIELDS` | `jasper/active_speaker/_common.py:61-88` | 25 per-driver keys the manual form may send | **Yes** for stored manual drivers |
| 2 | `MANUAL_SETTINGS_FIELDS` | `_common.py:60` | `drivers`, `crossover_candidates`, `driver_spacing_mm` | Yes |
| 3 | `LEGACY_DROPPED_DRIVER_FIELDS` | `_common.py:55-58` | two retired keys every gate tolerates | Yes (append-only) |
| 4 | `_V2_RESEARCH_DRIVER_FIELDS` | `jasper/active_speaker/driver_safety.py:774-806` | 27 keys a research reply may carry (= #1 minus `installation`, `source`, plus `target_fingerprint`, `unknowns`, `field_provenance`) | Second copy of #1 |
| 5 | `_V2_RESEARCH_TOP_LEVEL_FIELDS` / `_V2_RESEARCH_CANDIDATE_FIELDS` | `driver_safety.py:765-773`, `807-819` | reply envelope and candidate keys | Only holder of the envelope; candidate keys are a copy of #7 |
| 6 | `_MANUAL_CANDIDATE_FIELDS` | `driver_safety.py:69-82` | 12 candidate keys | Copy of #7 |
| 7 | `_CANDIDATE_FIELDS` | `jasper/active_speaker/design_draft.py:64-77` | 12 candidate keys | Copy of #6 (byte-identical sets) |
| 8 | `_V2_RESEARCH_COMPARABLE_FIELDS` | `design_draft.py:858-885` | 18 keys compared between reply and visible values | Third per-driver list |
| 9 | profile target allow-list | `driver_safety.py:2136-2166` | 22 keys a stored safety-profile target may carry | Fourth per-driver list |
| 10 | `operator_declared_context` allow-list | `driver_safety.py:1120-1124` | `MANUAL_DRIVER_FIELDS ∪ {operator_notes} ∪ LEGACY` | derived since #5257 (was a fifth hand copy) |
| 11 | `_OPERATOR_INPUT_FIELDS` | `design_draft.py:78-86` | role text boxes + `notes` + `target_models` | Only holder |
| 12 | `manualSettingsPayload` field list | `deploy/assets/sound-profile/js/main.js:892-903` | 8 numeric keys the page sends | Client copy of #1 |
| 13 | `applyDriverResearchToManualSettings` field list | `main.js:1061-1070` | 7 keys folded from a reply | Client copy of #4 |
| 14 | `applyDriverSafetyToSetting` | `main.js:985-1035` | bands, filters, cabinet, limits, class, pad | Client copy of the safety-field half |
| 15 | `driverEchoBackFields` | `deploy/assets/sound-profile/js/driver-model.js:866-923` | 7 keys echoed back | Client union of `_PROMPT_PROVENANCE_KEYS` (`driver_safety_prompt.py:41-47`) and `_profile_core`'s `safety_field_names` (`driver_safety.py:1729-1735`) |
| 16 | `summarizeDriverResearchPayload` | `driver-model.js:580-651` | client re-validation of reply shape (kind, version, notes length, filter pairs, fingerprint regex) | Client copy of `validate_driver_research_result_shape` (`driver_safety.py:822-881`) |
| 17 | `hfDriverStyles` floors | `driver-model.js:120-129` | 6 tweeter styles with Hz floors | Display copy of `driver_protection._STYLE_HIGH_PASS_HZ` |
| 18 | `driverClasses`, `enclosureKinds`, `padKinds` | `driver-model.js:131-139`, `driver-fields.js:110-118`, `driver-model.js:163-170` | enum vocabularies | Client copies of `DRIVER_CLASSES` (`_common.py:36-43`), `SUPPORTED_ENCLOSURE_KINDS` (`driver_safety.py:61-68`), `PAD_KINDS` |

There are **25** `_reject_unknown_keys` call sites with literal sets across
`driver_safety.py` and `design_draft.py` (`grep -n "_reject_unknown_keys("`),
and the helper itself exists twice (`driver_safety.py:356-364`,
`design_draft.py:119-127`), as do `_text`, `_finite_float`,
`_positive_float`, `_sequence` (`driver_safety.py:269-353` vs
`design_draft.py:130-204`). The comment at `design_draft.py:866-867` cites a
pin `test_component_entry_fields_present_in_all_four_allowlist_gates` that
does not exist in `tests/` (the nearest is
`tests/test_active_speaker_driver_safety.py:759`, which walks three).

**Verdict:** authoritative = #1 + #3 + #11 on the server. Lists #4, #6-#10
should be derived from #1 or deleted with the gate that owns them; #12-#18
should be replaced by the server payload's own keys (the page already
receives the normalised driver record and only needs to round-trip it).

### 1b. Request, result and profile fingerprints

| # | Fingerprint | Written by | Compared by | Authoritative? |
|---|---|---|---|---|
| 1 | `request_fingerprint` (sha256 of request core) | `build_driver_research_request` `driver_safety.py:957-975` | `validate_driver_research_request` `driver_safety.py:1174-1195` (with legacy re-stamp `1152-1195`), `validate_research_result_binding` `1227-1235`, `_rebound_to_restamped_request` `design_draft.py:1096-1124`, client regex `driver-model.js:634-636` | Server; **stale-check now ignores the only field the reply changes** (`_without_declared_context`, `1217-1224`, #5262) |
| 2 | `target_fingerprint` (per physical channel) | `measurement.physical_driver_target` `jasper/active_speaker/measurement.py:286` | request validation `driver_safety.py:1085-1094`, result binding `1236-1263`, profile eval `2585-2606`, profile shape `2172-2181` | Server |
| 3 | `result_fingerprint` | `finalise_research_result` `driver_safety.py:1381-1390` | nothing on the setup path (stored into the profile's `research` block `1927-1929`, validated only as "is a sha" `2126-2131`) | Write-only |
| 4 | `profile_fingerprint` + `confirmation.confirmed_fingerprint` | `build_driver_safety_profile` `driver_safety.py:2015-2035` | `evaluate_driver_safety_profile` `2567-2578`, `2661-2667` | Server; the two are always equal by construction |
| 5 | design-draft `revision` (int) | `save_design_draft` `design_draft.py:1531` | `save_design_draft` `1506-1515` (409), page `main.js:3806`, tuning handoff `main.js:2201-2206` | Server |
| 6 | `topology_revision` (`sha256:` of file bytes) | `load_output_topology_snapshot` `jasper/output_topology.py:2221` | `verify_revision` `sound_active_speaker.py:315-322`, `_verified_detected_hardware` `404-427` | Server |
| 7 | `detected_hardware_identity` | `output_hardware.detected_hardware_identity` `jasper/output_hardware.py:344-386` | `_verified_detected_hardware` `sound_active_speaker.py:420-426` | Server |
| 8 | `design_draft_fingerprint` (crossover) | `crossover_design_fingerprint` `crossover_preview.py:84-95` | `_validate_preview_freshness` `crossover_preview.py:748-756`, `_source_payload` `baseline_profile.py:485-489` | Server |
| 9 | `preview_fingerprint` | `crossover_preview_fingerprint` `crossover_preview.py:98-123` | `_validate_preview_freshness` `724-736`, `_source_payload` `499-501`, `_measured_level_trims` `580-589` | Server |
| 10 | `topology_config_fingerprint` (+ legacy variant) | `output_topology.py:770-807` | `topology_fingerprint_matches` `810-818`, statefile stamps `2058-2192`, boot gate `deploy/bin/jasper-camilla-topology-gate` | Server; **two hashes for one fact** kept "until no fleet box carries an anchor older than #2500" (`794-799`) |
| 11 | baseline `source.fingerprint`, `candidate_fingerprint`, `measured_candidate_fingerprint`, `driver_protection_fingerprint`, `measurement_summary_fingerprint`, `candidate_graph_context_fingerprint`, config `sha256` | `baseline_profile.py:474-521`, `247-274`, `1573-1655` | `reviewed_candidate_refusal` `277-281` (called twice per apply: `sound_active_speaker.py:1737-1740` and `correction_crossover_v2_apply.py:85-89`), `persist_applied_baseline_profile` `1666-1670` | Server |
| 12 | v2 state `applied`, `previous_candidate_fingerprint`, `accepted_sound_candidate_fingerprint` | `jasper/web/correction_crossover_v2_state.py:215-256` | `rollback_candidate` `correction_crossover_v2_status.py:32-54` | Second record of "what is applied" |
| 13 | client `tuningHandoff.copiedRevision` | `main.js:3704` | `tuningHandoffStale` `main.js:2201-2206` | Client |

Four independent sha256-over-canonical-JSON helpers exist for these:
`driver_safety._fingerprint` (`132-137`), `output_topology.canonical_fingerprint`
(`758-767`, consumed by `baseline_profile`), `crossover_preview`'s two inline
`json.dumps(..., default=str)` hashes (`84-95`, `98-123`) and
`measurement._fingerprint` (`measurement.py:108-110`).

### 1c. "What is applied / loaded / staged"

| # | Store | Path | Writer | Readers on this path |
|---|---|---|---|---|
| 1 | CamillaDSP statefile (the boot pointer) | `DEFAULT_CAMILLA_STATEFILE` (`runtime_contract.py`) | CamillaDSP itself after every websocket load (`baseline_profile.py:1712-1716` comment), `write_camilla_statefile` `runtime_contract.py:4736-4767` from the reconciler | `safe_graph_for_current_topology` `4441-4455`, `applied_profile_displacement` `baseline_profile.py:1066-1117`, boot gate script |
| 2 | statefile topology stamps (`.topology`, `.topology.unproved`) | beside #1 | `stamp_statefile_topology` / `stamp_statefile_convergence` `output_topology.py:2137-2192` | `jasper-camilla-topology-gate` (shell) |
| 3 | applied baseline record | `/var/lib/jasper/active_speaker_baseline_profile.json` (`state_paths.py:18`) | `persist_applied_baseline_profile` `baseline_profile.py:1658-1680`, `_baseline_apply_result` on failure `1258-1275` | 30 modules (`grep -rl load_applied_baseline_profile_state jasper/`), incl. the boot selector via `candidate_kind="applied_baseline"` `runtime_contract.py:4458-4466` |
| 4 | canonical baseline YAML copy | `/var/lib/camilladsp/configs/active_speaker_baseline.yml` (`baseline_profile.py:79`) | `promote_applied_baseline_candidate` `1691-1740` (a byte copy of the content-addressed sibling) | doctor, multiroom follower; **not** the boot pointer |
| 5 | crossover v2 state | `/var/lib/jasper/active_speaker_crossover_v2_state.json` (`crossover_v2/durable_state.py:80`) | `observe_apply_success` `correction_crossover_v2_state.py:215-256`, `apply_candidate` `correction_crossover_v2_apply.py:106-116` | `rollback_candidate`, `crossover_v2_status_block`, `previous_candidate_fingerprint` on `/active-speaker/baseline-profile` (`sound_active_speaker.py:1663`) |
| 6 | staged startup anchor (metadata + YAML pair) | `/var/lib/jasper/active_speaker_staged_config.json` + `configs/active_speaker_staged_startup.yml` (`staging.py:93-94`) | `stage_protected_startup_config` `staging.py:1566-1612`, reached only from `web_commissioning._ensure_commission_startup_anchor` `406-536` and `startup_load.py:1159` | boot selector `runtime_contract.py:4604-4641`, `_active_graph_allowed` `3336-3363`, reset `reset.py:66` |
| 7 | startup load state | `/var/lib/jasper/active_speaker_startup_load.json` (`state_paths.py:22`) | `startup_load._record_state` `142-156` | commissioning view, `web_commissioning.start_summed_test` (dead), doctor |
| 8 | commission load state + ramp state | `active_speaker_commission_load.json`, `active_speaker_commission_ramp.json` (`state_paths.py:26`, `commission_ramp.py:65`) | commissioning lane | `_active_speaker_commission_state_payload` `sound_active_speaker.py:1506-1586` |
| 9 | startup hold marker (`/run`) | `startup_hold.py` | `load_protected_startup_config` | selector rung `runtime_contract.py:4527-4542`, released on apply `baseline_profile.py:1663` |
| 10 | commissioning view (derived, not stored) | – | `build_commissioning_view` `commissioning_coordinator.py:112-204` | page `activeSpeaker.commissioningView`; `applied_profile.stands` is the page's notion of "applied" (`main.js:1265-1274`) |
| 11 | page memory | – | `ingestOutputTopology`, `ingestDesignDraft`, `ingestCrossoverPreview`, `patchActiveSpeaker` | render |

Authoritative today: #1 for what plays now (that is what `applied_profile_displacement`
says, `baseline_profile.py:1073-1093`), #3 for what the operator applied, #6
for what a roleful box boots with when identity is unconfirmed. #5 duplicates
#3's "applied" and "previous" facts; #4 duplicates #3's bytes; #10 merges #1,
#3 and #7 for display.

### 1d. Topology mode names and the cardioid shape

| # | Holder | File:line | Notes |
|---|---|---|---|
| 1 | `SUPPORTED_GROUP_MODES`, `REQUIRED_ROLES_BY_MODE`, `SUPPORTED_OUTPUT_VARIANTS` | `jasper/output_topology.py:83-101`, `60` | **Authoritative** (`full_range_passive`, `active_2_way`, `active_3_way`, `subwoofer`; variants `primary`/`rear`) |
| 2 | rear-variant rule (rear only on a woofer of a mode that requires a woofer; rear starts muted) | `output_topology.py:944-951`, `971-972` | Authoritative for the cardioid shape |
| 3 | `ACTIVE_CROSSOVER_ROLE_PAIRS` | `_common.py:30-33` | pairs per mode; a second answer to "which roles does a mode have" |
| 4 | `_way_count_for_mode` / `_active_mode_for_way` | `staging.py:329-338` | mode ↔ way-count, a third spelling |
| 5 | `required_driver_roles(way_count)` | `jasper/active_speaker/profile.py` | roles per way count, a fourth |
| 6 | `CONTRACT_ACTIVE_MONO_2WAY` … `CONTRACT_ACTIVE_STEREO_3WAY` | `runtime_contract.py:234-243` | a fifth vocabulary for the same shapes |
| 7 | `_active_main_groups` mode set | `playback_route.py:49-55` | sixth |
| 8 | `outputTemplateDefinition` | `deploy/assets/sound-profile/js/topology.js:257-295` | client table; cardioid = `active_2_way` + pushed `rear` woofer (`262`, `279-281`) — agrees with #2 |
| 9 | `outputTemplateKindFromAxes` | `topology.js:248-256` | cardioid keyed off `speakerMode === 'active_3way' && cardioid` |
| 10 | `outputTemplateAxesForTopology` | `main.js:1390-1415` | reverse map; a saved cardioid renders as `speakerMode: 'active_3way'` (`1414`) although its mode is `active_2_way` — the checkbox at `main.js:1503-1508` only appears under "Active 3-way" |
| 11 | `activeCrossoverPairs` | `topology.js:191-207` | client copy of #3 |
| 12 | `driverResearchRoles` order, `_topology_roles` order, `roleOrder` | `driver-model.js:52-55`, `design_draft.py:928`, `959`, `topology.js:129`, `main.js:1996` | five role-order dicts |
| 13 | `physical_target_id` / `physicalTargetId` | `output_topology.py:127-128` / `topology.js:94-96` | target-id spelling duplicated across the wire |
| 14 | `SUB_CROSSOVER_HZ_*`, `DEFAULT_SUB_CROSSOVER_HZ` | `output_topology.py:77-78`, `profile.py`, `active-speaker-ui.js:118-120` | sub corner bounds in three places (pinned by `test_sub_crossover_bounds_match_python`) |

**Verdict:** #1/#2 are the source of truth. The user-facing "cardioid" is a
client-only label (#8-#10) for "active two-way with a rear woofer", and the
client renders it inconsistently with its own storage (#10). #3-#7 are five
server-side derivations of #1 that could be one function on the topology.

### 1e. DAC output assignment

| # | Holder | File:line | Notes |
|---|---|---|---|
| 1 | `SpeakerChannel.physical_output_index` | `output_topology.py:459-556` | **Authoritative**, persisted in the topology JSON |
| 2 | duplicate check | `evaluate_output_topology` `output_topology.py:1004-1013`; re-raised at save by `_refuse_duplicate_physical_outputs` `sound_active_speaker.py:287-302` | one rule, one implementation (#5261) |
| 3 | contiguity from output 1 | `_bind_preset_to_topology` `staging.py:1119-1140` (`active_outputs_must_be_contiguous`), role order `1141-1178` (waived for preview-derived presets `1150`), sub "next contiguous" `_local_subwoofer_from_topology` `staging.py:549-562` | enforced only at staging/commissioning time, **not at topology save** |
| 4 | lane width | `_required_output_width` `runtime_contract.py:3386-3395`, `_highest_assigned_output` `playback_route.py:69-77`, `active_playback_route_capability` `playback_route.py:206-217` (`required_outputs = highest + 1`) | three derivations of "how many lanes" |
| 5 | client default assignment | `outputChannel(role, index)` `topology.js:376-385`, `outputTemplateGroups` `296-318` (sequential fill), `firstUnusedOutputIndex` `145-153` | the page pre-assigns outputs 1..n in template order; the server never re-derives this |
| 6 | client peer swap | `setOutputChannelAssignment` `main.js:3358-3372` | still swaps the peer in a two-channel group after #5261 removed the "swaps with" hint; the three-channel group gets no swap and relies on #2 |
| 7 | client identity report | `identityReportFromTopology` `main.js:777-808` | recomputes `channel_identity_report` (`output_topology.py:1197-1279`) when the payload lacks it |
| 8 | reconciler lane width env | `jasper/audio_hardware/reconcile.py:1120-1125` writes `JASPER_OUTPUTD_ACTIVE_CHANNELS` from `outputd_active_lane_decision` (`runtime_contract.py:4111`), whose width is the **live CamillaDSP graph's** `playback_channels` (`4101-4108`), while the ring width is `active_ring_channels_for_topology` (`709-756`: `max(index)+1`, refused unless contiguous) — two derivations that must agree | the only enforcement of contiguity outside staging |

## 2. Separation of concerns

Rating: **S** = server-owned, **C** = client-owned, **X** = split (both compute).

| Concern | Server | Client | Rating | Sync mechanism |
|---|---|---|---|---|
| Which drivers exist (research targets) | `driver_research_targets` `driver_safety.py:140-160` (+ `measurement.active_driver_targets`) | `driverResearchTargets` `driver-model.js:57-85` | **X** — the page computes target ids, roles, output labels and driver styles itself; the server payload carries the same in `driver_protection_policy_view.targets` and `driver_research_request.targets` | none; both read the topology |
| Prompt readiness | none (request build refuses missing models `driver_safety.py:900-906`) | `driverResearchPromptReady` `driver-model.js:383-391` (model + tweeter style or enclosure) | **C** — the enclosure/style precondition exists only on the client | none |
| Preview readiness | `_summary` + `status` `design_draft.py:964-1241`; `build_crossover_preview` blockers | `driverResearchHasPreviewInputs` `driver-model.js:347-361`, `savedDriverResearchHasPreviewInputs` `main.js:839-847`, `driverResearchCanPreparePreview` `1139-1144` | **X** | none; client gates the button, server refuses again |
| Best candidate per pair | `_candidate_map` `crossover_preview.py:154-180` | `applyDriverResearchToManualSettings` `main.js:1100-1135` + `CANDIDATE_CONFIDENCE_RANK` `driver-model.js:544` | **X** (rank table copied) | comment says "mirror"; no pin |
| Reply shape validation | `validate_driver_research_result_shape` `driver_safety.py:822-881` + `normalise_driver_research` | `summarizeDriverResearchPayload` `driver-model.js:580-651` | **X** | none |
| Crossover vocabulary | `declaration_vocabulary` via island `sound_setup.py:178-212` | `manualCrossoverVocabularyValidationError` `driver-model.js:433-465` | **X** (values from island, rule re-implemented) | island |
| Delay target rule | `_normalise_candidate` `design_draft.py:495-511` | `manualCrossoverDelayValidationError` `driver-model.js:412-424` | **X** | none |
| Identity report | `channel_identity_report` `output_topology.py:1197-1279` | `identityReportFromTopology` `main.js:777-808` | **X** | payload carries `channel_identity`; client falls back to its own |
| Step state / next action | `build_commissioning_view` `commissioning_coordinator.py:153-193` | `activeSpeakerStepState`, `defaultActiveSpeakerStep` `active-speaker-ui.js:67-84`, `outputStepState` `main.js:1315-1321` (uses the backend only when nothing is dirty) | **X** | `commissioning-view` fetch after every mutation |
| Safety profile status copy | `evaluate_driver_safety_profile` reasons (`driver_safety.py:2495-2672`) | `driverSafetyReviewHint` `driver-model.js:942-990`, `SAFETY_RELATIONSHIP_TEXT` `679-696`, `renderDriverResearchSummary` `main.js:1584-1594` | **C** phrasing over server codes | codes |
| Template availability | none at save except `_refuse_undrivable_layout` `sound_active_speaker.py:250-284` and route width in staging | `outputTemplateUnavailableReason` `topology.js:328-353` (mismatch, output count, route width, sub support) | **C** | `active_playback_route` in the topology payload |
| Hardware mismatch | `declared_hardware_mismatch` `output_topology.py:1857-1932` | reads `payload.hardware_mismatch` (`topology.js:70-79`) | **S** (correctly) | payload |
| Style floors | `driver_protection` | `hfDriverStyles` `driver-model.js:120-129` | **X** (display copy, pinned) | test |
| Sub corner bounds | `output_topology.py:77-78` | `active-speaker-ui.js:118-120` | **X** (pinned) | test |

**What the client sends back for the server to re-check:** `driver_research_request`
(the server's own artefact, `main.js:3805`), `expected_revision` (`3806`),
`topology_revision` (`3910`, `3973`, `4039`), `detected_hardware_identity`
(`3974`, `4040`), `expected_candidate_fingerprint` (`4153`), and the whole
`output_topology` draft including `identity_verified` flags the server then
strips again (`_with_server_owned_identity` `output_topology.py:2235-2290`).

**Where page memory diverges from the server, and what keeps it in sync:**

| Page record (`state.js`) | Server store | Divergence | Sync |
|---|---|---|---|
| `outputTopology.draft` / `.payload` / `.dirty` (`state.js:15-26`) | topology JSON | `draft` is a clone edited locally (`setOutputDraft` `main.js:3304-3320`); `dirty` blocks every other action (`3746`, `3866`, `4066`, `driverResearchPromptReady`) | `ingestOutputTopology` after GET/POST/409 (`3141-3166`) |
| `driverResearch.settings` / `.inputs` / `.dirty` / `.safetyDirty` / `.editedDriverTargets` / `.researchRequest` / `.promptCopy` (`state.js:28-45`) | design draft JSON | the form is a per-target flattened projection built by `ingestDesignDraft` (`1160-1233`); `dirty` suppresses re-ingest (`1164`) — the #5259 bug; `researchRequest` is dropped on any edit (`invalidateDriverResearchBinding` `driver-model.js:392-396`) | GET after topology save/reset/refresh; `force` after save/reset |
| `crossoverPreview.payload` (`state.js:46`) | preview JSON | nulled on any topology edit (`3317`) and after a draft save (`3818`); `renderCrossoverPreviewCardBody` hides a prepared preview while dirty (`1755-1757`) | GET after refresh/topology save; POST result |
| `activeSpeaker.commissioningView` / `.baselineProfile` / `.commission` (`main.js:206-213`) | derived view, compiled candidate, load states | `baselineProfile` is only refreshed on page refresh and after apply/restore (`3196-3200`, `4159`), so `candidate_fingerprint` can lag a draft save — the double fingerprint gate then refuses | `refreshCommissioningView` after every mutation |
| `outputPage.stepOverride` / `.templateDraftAxes` (`state.js:79-89`) | none | pure UI | reset by `refreshCommissioningView` when `next_action` changes (`3232-3234`) |
| `tuningHandoff` (`main.js:217`) | none | `copiedRevision` vs draft revision (`2201-2206`) | none |

## 3. State machine and half-finished flows

### 3a. The states

| Artefact | States (as the code spells them) | Where decided |
|---|---|---|
| Topology | `missing` (no file; `new_topology_draft` seeded from observed hardware, `output_topology.py:843-869`, revision `"missing"` `2213`) · **client draft** (`outputTopology.dirty`) · saved `draft` (zero groups → runtime `CONTRACT_UNCONFIGURED`, parked `runtime_contract.py:4425-4430`) · saved `valid` · saved `blocked` · saved `verified` (every lane `identity_verified`) · overlay: `hardware_mismatch` (`1857-1932`), `hardware_repin` offer | `evaluate_output_topology` `output_topology.py:931-1123` |
| Design draft (driver form) | `not_saved` · **client dirty** (`driverResearch.dirty`, `.safetyDirty`) · `blocked` · `needs_research` · `ready_for_review` · `unreadable`; overlay: safety profile evaluation `missing`/`incomplete`/`stale`/`malformed`/`confirmed` (`driver_safety.py:2495-2672`) | `build_design_draft` `design_draft.py:1231-1241` |
| Research request | none · **held in page** (`driverResearch.researchRequest`, minted by POST `driver-research-request`, `main.js:3684`) · saved with the draft (`driver_research_request`) · **binding invalidated** (client nulls it on any edit, `driver-model.js:392-396`; server nulls a legacy one on load `design_draft.py:1305-1338`) | client + `validate_driver_research_request` |
| Research reply | none · **pasted, parsed** (client `importText`/`parsed`/`importedPayload`) · saved v2 (`driver_research` with `result_fingerprint`) · saved v1 (advisory) · **dropped at save** because the binding was invalidated (`main.js:3767-3772`, silent apart from a status line) | client + `normalise_driver_research` |
| Crossover preview | `not_prepared` · `ready_for_protected_staging` · `blocked` · `not_applicable` · `stale` (three codes, computed on every load `crossover_preview.py:720-756`) · `unreadable`; client nulls its copy on any topology edit (`main.js:3317`) and hides it while the form is dirty (`1755-1757`) | `build_crossover_preview` |
| Staged startup anchor | `not_staged` · `staged` · `blocked` (**the metadata is written even when blocked**, `staging.py:1858-1866`) · stale vs topology (`runtime_contract.py:3280-3299`) · hold active (`/run` marker, `startup_hold.py`) | `stage_protected_startup_config` |
| Baseline profile | none · `ready_to_compile` (every GET recompiles in memory, `baseline_profile.py:292-339`) · `ready_to_apply` (POST compile, **no page caller**) · `blocked` · `applied` · `apply_failed` · overlay verdicts `applied_profile_displaced` / `running_config_path_unknown` / `applied_profile_config_missing` (`1055-1117`, `commissioning_coordinator.py:253-275`) | `compile_commissioning_profile`, `persist_applied_baseline_profile` |
| Running graph | `parked_muted` · `select_flat` · `select_active_startup` (all-muted) · `select_active_baseline` / `preserve_current` (approved active runtime) · `blocked` (statefile untouched) | `safe_graph_for_current_topology` `runtime_contract.py:4388-4736` |
| outputd | running · parked by ExecCondition (missing card) · parked at exit 78 (`deploy/systemd/jasper-outputd.service:31`) | reconciler, daemon |
| Commissioning view | `needs_layout` · `needs_driver_values` · `needs_driver_safety_profile` · `needs_first_experiment` · `ready_to_save_profile` · `blocked` · `applied` · `not_required`; steps `layout/research/experiment/profile` each `todo/active/done/not_required`; `next_action.id` ∈ {`declare_speaker`, `save_driver_values`, `preview_crossover`, `run_speaker_program`, `apply_candidate`, `run_program`, `copy_prompt`} | `build_commissioning_view` `commissioning_coordinator.py:112-204` |

### 3b. Transitions

"Owner" is who decides the transition. **Implied** marks a transition that no
code names: it happens as a side effect of another one.

| From | Event | To | Owner | File:line | Implied? |
|---|---|---|---|---|---|
| Topology missing | page boot (`loadLocalHardware`) | page holds a hardware-seeded draft, `dirty=false` | server `new_topology_draft` | `output_topology.py:843-869`, `main.js:4216-4219` | — |
| any topology | pick layout axis / checkbox / channel select / tweeter style / sub add | client draft, `dirty=true`; **design draft marked dirty, research binding dropped, preview copy nulled** | client | `setOutputDraft` `main.js:3304-3320` | implied (one click on "Stereo" invalidates the research state) |
| client draft | Save | saved (park → commit → converge → reconcile) | server | `_save_output_topology_payload` `sound_active_speaker.py:305-401` | — |
| client draft | Save with stale `topology_revision` | 409, page re-ingests server copy, **local edits lost** | server | `315-322`, `main.js:3893-3899`, `3945-3951` | — |
| client draft | tweeter style select (`data-save-driver-style`) | saves the topology immediately and jumps to the research step | client | `main.js:2967-2976` | implied (a form select becomes a topology save) |
| saved topology | POST design-draft / crossover-preview / staging | tweeter `required_missing` → `software_guard_requested` **written back to the topology** | server | `web_commissioning.py:189-217`, callers `sound_active_speaker.py:334`, `1102`, `1212` | implied (three writers mutate the topology from unrelated routes) |
| saved topology | Confirm output (per lane) | `identity_verified=true` | server | `_active_speaker_channel_identity_save_payload` `681-777` | — |
| saved verified | Change (un-confirm) | `identity_verified=false` **and the box is parked** | server | `706-741` | — |
| saved topology | Reset | `draft` (zero groups) + nine state files deleted + parked | server | `443-546`, `reset.py:59-73` | — |
| saved topology | hardware changed (reconciler) | `hardware_mismatch` overlay; templates disabled on the client | reconciler / client | `output_topology.py:1857-1932`, `topology.js:328-334` | — |
| design draft any | type in any driver field | client dirty; `researchRequest=null`; echo panel hidden for that target | client | `setManualDriverField` `main.js:855-874`, input handlers `2819-2870` | — |
| client dirty | Save values | draft rebuilt from topology + form; safety profile rebuilt and self-confirmed; preview copy nulled | server | `build_design_draft` `design_draft.py:1127-1302`, `main.js:3802-3822` | implied (saving is confirming, `driver_safety.py:1993-2051`) |
| client dirty | Save values with pasted v2 reply but `researchRequest` null | reply **dropped**, draft saved without it | client | `main.js:3767-3772` | implied |
| client dirty | Save values with stale `expected_revision` | 409; page keeps local edits if dirty | server + client | `design_draft.py:1506-1515`, `main.js:3834-3846` | — |
| client dirty | reload page | **all edits lost** (no local persistence) | — | `state.js:28-45` | implied |
| research none | Copy prompt | request minted server-side, prompt copied, `researchRequest` held in page memory only | server + client | `sound_active_speaker.py:1017-1063`, `main.js:3674-3694` | — |
| held request | any edit | request dropped (Copy reads "Copy prompt" again) | client | `driver-model.js:392-396` | — |
| held request | reload | **request lost**; a pasted v2 reply can never be saved until Copy is pressed again | — | – | implied |
| held request | Save values | request saved in draft | server | `design_draft.py:1156-1165` | — |
| saved request | topology save | request stays saved but the page nulls its copy; server re-validates the saved one on the next draft save and refuses if targets moved | client + server | `main.js:3924`, `driver_safety.py:1020-1105` | implied |
| reply pasted | Load information | folded into visible fields; `dirty`, `safetyDirty` | client | `parseDriverResearchImport` `main.js:3714-3734`, `applyDriverResearchToManualSettings` `1037-1138` | — |
| draft `ready_for_review` + safety `confirmed` | Preview crossover | preview file written; hidden until refresh if the form is dirty | server | `_active_speaker_crossover_preview_save_payload` `1203-1231` | — |
| preview ready | any draft save | preview `stale` on next load (`design_draft_fingerprint` moved) | server | `crossover_preview.py:748-756` | implied |
| preview ready | any topology save | preview `stale` (topology inside the draft fingerprint) | server | `84-95` | implied |
| preview ready | commissioning (crossover page) or `_ensure_commission_startup_anchor` | staged anchor written + loaded; hold marker | server | `web_commissioning.py:406-536`, `staging.py:1566-1612` | — |
| staged | topology save | staged metadata mismatches topology → boot selector ignores it (`active_staged_metadata_mismatch`) | server | `runtime_contract.py:3280-3299`, `3336-3363` | implied |
| staged | Reset | metadata deleted (YAML kept) | server | `reset.py:66`, `9-16` | — |
| baseline compiled (`ready_to_compile`) | Save and apply | compile → proof → validate → websocket load → applied record + v2 state + canonical copy + base-trim bank + hold release | server | `_active_speaker_finish_commissioning_payload` `1722-1800`, `apply_candidate` `correction_crossover_v2_apply.py:37-136`, `persist_applied_baseline_profile` `baseline_profile.py:1658-1680` | — |
| baseline compiled | Save and apply with a `candidate_fingerprint` the page fetched before the last draft save | `baseline_candidate_fingerprint_mismatch` (twice: `sound_active_speaker.py:1737-1740`, `correction_crossover_v2_apply.py:85-89`) | server | – | — |
| applied | reconcile / deploy / reboot | re-decided by the selector; **stays applied only if every lane is identity-confirmed and the graph re-proves**; else all-muted anchor or parked | reconciler | `runtime_contract.py:4502-4557`, `reconcile.py:1388-1409` | implied — the central defect (section 7) |
| applied | out-of-band graph change (`reconcile-current-dsp`, EQ apply) | record `displaced`; page shows "not active" | server | `baseline_profile.py:1066-1117` | — |
| applied | Restore previous tune | previous candidate re-applied | server | `handle_v2_apply(previous=True)` | — |
| applied | Reset | record deleted; parked | server | `reset.py:72` | — |
| applied | draft edit + save | commissioning view stays `applied`; `tuning handoff` shows "declarations changed" | server/client | `commissioning_coordinator.py:167-169`, `main.js:2201-2206` | — |
| commissioning view | any mutation | recomputed from all stores (GET `commissioning-view`) | server | `load_commissioning_view` `207-250` | — |

### 3c. What happens if the user…

| … | Loses work? | Parks the speaker? | Client/server disagree? | Evidence |
|---|---|---|---|---|
| **leaves halfway** through the layout step (draft not saved) | yes, the draft (page memory only) | no (nothing was committed) | no | `state.js:15-26` |
| leaves halfway through the driver form | yes, every typed value and any pasted reply | no | no | `state.js:28-45` |
| leaves after Copy prompt, before Save values | the held request; a v2 reply pasted later is refused ("Target-bound research was invalidated") until Copy is pressed again — which re-mints an identical request | no | no | `main.js:3767-3772`, `3684` |
| leaves after Save values, before Preview | nothing; but a later reload shows "working proposal" until Preview is pressed | no | no | `main.js:1750-1794` |
| leaves after Preview, before Apply | nothing | **yes, the box was already parked at the topology save** unless it was passive or already applied+confirmed | no | `runtime_convergence.py:240-327` |
| **reloads the page** | client-only state (above); `outputPage.stepOverride`; `tuningHandoff.copiedRevision` | no | no — every server store is re-fetched (`refreshOutputTopology` `main.js:3175-3219`) | – |
| reloads while a paste sits in the box | the paste | no | no | – |
| **presses Reset** | everything except the topology's hardware block: draft, preview, staged metadata, path safety, startup/commission/ramp state, measurements, **applied baseline** (`reset.py:59-73`); the crossover v2 state, base-trim bank and candidate bank are **not** cleared | **yes** (`commit_unconfigured` → parked) | briefly: `patchActiveSpeaker(null …)` then re-fetch (`main.js:3979-3999`); a failed draft fetch leaves `status: unreadable` in memory | `443-546` |
| Reset while the detected hardware token moved | 409 `detected_hardware_changed`, nothing cleared | no | page re-ingests | `404-427` |
| **redeploys** | nothing on disk | the reconciler re-runs `converge_boot_statefile` on every deploy (`deploy/lib/install/systemd-units.sh` invokes it with `--reason install`); with unconfirmed lanes the statefile is repointed at the all-muted anchor or parked graph, so the applied baseline stops playing at the next `jasper-camilla` restart | no | `reconcile.py:1388-1409`, `runtime_contract.py:4502-4557` |
| **reboots** | nothing on disk | same as redeploy; additionally the topology gate can refuse CamillaDSP if the proved/unproved stamps differ | no | `deploy/bin/jasper-camilla-topology-gate` |
| un-confirms one lane | nothing | yes, immediately and durably | no | `sound_active_speaker.py:706-741` |
| saves the topology with the same shape (e.g. re-picks "Mono") | nothing on disk, but the design draft is marked dirty on the client, the research binding is dropped, the preview is hidden, the safety profile reads "save your current edits" | topology save parks then re-converges: a passive box comes back on the flat graph; an active box comes back only through `select_active_baseline` (identity-gated) | yes — the page believes the draft is dirty while the server draft is unchanged | `main.js:3304-3320` |

The three findings in this section that cost work today: (1) `setOutputDraft`
marks the driver form dirty for every topology edit, including no-op
re-picks (`main.js:3315-3317`); (2) the held research request lives only in
page memory between Copy and Save (`main.js:3684`), so any reload or edit
turns a pasted reply into a silent drop; (3) the topology save parks a
roleful box and only identity confirmation lets it come back
(`runtime_contract.py:4502`), which the page never explains at the point of
saving ("Saved speaker layout." `sound_active_speaker.py:389`).

## 4. Nannies and gates

Grading rule, as briefed: a gate earns KEEP only when it protects a
non-negotiable, a named incident, or an ADR ruling; a gate that guards a
hypothetical is a nanny and is graded REMOVABLE. Origins come from the
archaeology pass (`git log -S` on each gate's first commit) and were
spot-checked. "Wave 1" is #1446 (`ecd6c9d88`, 2026-07-13), which introduced
the design-draft `expected_revision`, the request/result fingerprints, the
per-driver allow-lists and `confirmed_and_current` in one PR, none of them
after an incident.

### 4a. Client (page) gates

| # | Gate (file:line) | What it checks | What it protects | Verdict |
|---|---|---|---|---|
| C1 | `topology.js:308-333` `outputTemplateUnavailableReason` | disables a layout template when observed outputs, `transport_channel_count` or sub support fall short | duplicate of `_refuse_undrivable_layout` (S4) and the topology blockers; the server refuses the same save | REMOVABLE (show the server's message) |
| C2 | `main.js:3178` confirm "Refresh hardware and lose the unsaved speaker layout draft?" | the page-memory draft would be replaced | nothing durable (`state.js:15-26`) | REMOVABLE |
| C3 | `main.js:3426-3427` confirm "Replace the unsaved speaker layout draft?" | same | dead: the only caller passes `skipDirtyConfirm: true` (`3498`) | REMOVABLE (dead) |
| C4 | `main.js:3745`, `3859`, `4066`; `driver-model.js:363-364` "Save the speaker layout before…" | refuses draft save, preview, confirm-output and the prompt while the layout draft is unsaved | the server re-checks target bindings itself (S14); the mismatch is hypothetical | REMOVABLE (one status line at most) |
| C5 | `main.js:1165` `ingestDesignDraft` early return while `driverResearch.dirty` | a server refresh must not clobber typed values | incident #5259 | KEEP |
| C6 | `driver-model.js:371-375` `invalidateDriverResearchBinding`, called from every field edit (`383`, `main.js:855-874`), and the silent reply drop `main.js:3767-3772` | a pasted v2 reply is refused after any edit since Copy prompt | hypothetical "reply answers a stale prompt"; #5262 already stopped the server comparing the declared context | REMOVABLE (cut 4) |
| C7 | `driver-model.js:361-369` `driverResearchPromptReady` | Copy prompt disabled until every target has a model and a style or enclosure | prompt quality, not safety; the prompt can carry "unknown" | REMOVABLE |
| C8 | `driver-model.js:397-418` `manualCrossoverDelayValidationError` and the vocabulary mirrors listed in section 2 | client re-runs server validators before sending | exists because `manualSettingsPayload` (`main.js:892-903`) drops a delay without a target; the server door `design_draft.py:268-281` refuses the same | REMOVABLE (send the value, show the server error) |
| C9 | `driver-model.js:552` `summarizeDriverResearchPayload` (`main.js:1213`, `3717`, `3768`) | re-parses a pasted reply into a summary before the server does | mirror of `normalise_driver_research` (`design_draft.py:549`) | REMOVABLE (keep `extractDriverResearchJson` `1049`, which only finds the JSON) |
| C10 | `main.js:3963-3965` "Reset speaker setup?" | deletes nine state files including the applied baseline (`reset.py:59-73`) | irreversible | KEEP |
| C11 | `main.js:4023-4031` "Pin the new DAC?" | composite child re-pin; parks the box | incident #2814/#2819 (composite dongle serials moved) | KEEP (composite only, not on the DAC8x path) |
| C12 | `main.js:4085` "Confirm that X is wired…?" and "Mark X as not confirmed? The speaker goes silent…" | a second click on a button already labelled Confirm; un-confirm parks | the park is S8's side effect | REMOVABLE (both halves go with S8) |
| C13 | `main.js:4120-4130` and again `4139-4142` `may_apply` | page repeats the server's `permissions.may_apply`, twice | the server decides again in `_active_speaker_finish_commissioning_payload` (`sound_active_speaker.py:1722-1800`); nothing changes between the two copies but the dialog | REMOVABLE |
| C14 | `main.js:4132-4137` "Save and apply … your normal speaker profile" | confirm before apply | reversible: "Restore previous tune" (`handle_v2_apply(previous=True)`) | REMOVABLE |

### 4b. Server gates, save through apply

| # | Gate (file:line) | What it checks | What it protects | Verdict |
|---|---|---|---|---|
| S1 | `sound_active_speaker.py:315-322` `topology_revision` must equal the saved revision, else 409 | optimistic concurrency on the topology file | hypothetical second editor; origin `fdc871475` (2026-06-22), no incident; the 409 path discards the page's edits (`main.js:3893-3899`) | REMOVABLE |
| S2 | `sound_active_speaker.py:404-427` `_verified_detected_hardware`: reset and repin need `topology_revision` + `detected_hardware_identity`, else 409 `detected_hardware_changed` | the DAC the page saw is still the DAC observed | hypothetical swap between page load and click (#2719/#2819); both routes re-read the observation after the park anyway (`471-475`, `581-586`) | REMOVABLE (section 5 graded the repin copy a guard; the post-park re-read is the guard, the token adds nothing) |
| S3 | `OutputTopology.from_mapping` `output_topology.py:667` and the nested parsers (`198`, `275`, `322`, `441`, `491`, `572`, `633`) | schema door on the saved topology | a file the code cannot parse must not drive lanes | KEEP |
| S4 | `_refuse_undrivable_layout` `sound_active_speaker.py:250-284` | roleful layout on a DAC with no active outputd lane | incident #2130 (InnoMaker: structurally mute with every daemon healthy); ADR-0178, ADR-0100 | KEEP |
| S5 | `_refuse_duplicate_physical_outputs` `287-302` over the `duplicate_physical_output` blocker (`output_topology.py:1004-1013`) | two drivers on one DAC channel | incident #5261; two drivers on one amplifier channel is a real wiring fault | KEEP (one copy: the blocker) |
| S6 | `rear_must_start_muted` `output_topology.py:977-978` | a cardioid rear lane carries `startup_muted = true` | no client writer of `startup_muted` exists, so it can only fire on a hand-edited file | REMOVABLE with the field (a constant is not a declaration) |
| S7 | tweeter protection bookkeeping: `protection_status` `required_missing` / `software_guard_requested` (`output_topology.py:505`, `1045-1060`, `1220`) plus the auto-writers `request_missing_software_guards` / `ensure_missing_software_guards` (`web_commissioning.py:184-217`) called from `sound_active_speaker.py:334`, `1102`, `1212` | a tweeter with no physical cap must carry the flag before draft, preview and staging routes proceed; the routes set the flag themselves | the emitted software high-pass is real (S15); the flag is a value the code writes for itself | REMOVABLE bookkeeping (derive "software guard" from "no physical cap declared" at compile time); keep the guard |
| S8 | identity gate on the two approved boot rungs `runtime_contract.py:4507`, `4547` (`roleful_identity_confirmed` `604-626`) and the un-confirm durable park `sound_active_speaker.py:706-741` | a proven, applied graph is selected at boot only if every roleful lane was "Confirm output"-ed | composite re-pin incident #2814/#2819 (child serials moved); on a single fixed DAC the lanes cannot move; this gate is why the box parks after reboot (section 7). `runtime_convergence.py:249-253` still calls the selector "deliberately identity-blind": the comment is wrong | REMOVABLE for single-device topologies (cut 1); the composite re-pin route already parks with `stay_parked` (`596-601`) and Apply re-arms |
| S9 | park-before-commit `park_and_commit_topology` `runtime_convergence.py:322-427`, decision at `240-327` | every topology save, reset, repin and un-confirm parks CamillaDSP first, commits, then re-selects | a lane change under an active crossover could route full-range into a tweeter | KEEP when the roleful lane map changes; REMOVABLE when `topology_config_fingerprint` is unchanged (today a no-op re-pick parks and, unconfirmed, never comes back) |
| S10 | `_i2s_hat_collision_warnings` `sound_active_speaker.py:124-155` | an I²S HAT overlay clash | review finding #3922, hypothetical; the reconciler owns the same warning (ADR-0234) | REMOVABLE |
| S11 | `expected_revision` `design_draft.py:1487-1515`, 409 on mismatch | optimistic concurrency on the design draft | Wave 1; hypothetical two-tab race | REMOVABLE |
| S12 | `_reject_unknown_keys`, 25 call sites (`design_draft.py:119`, `driver_safety.py:356`; table 1a) | payload keys outside an allow-list are refused | hypothetical; a stray key would otherwise be ignored | REMOVABLE (24 of 25 sites); keep one at the paste boundary (`normalise_driver_research` `549`) derived from `MANUAL_DRIVER_FIELDS` (`_common.py:60`) |
| S13 | compiler vocabulary door `design_draft.py:210-281` (`_crossover_filter_type`, `_crossover_slope_db_per_octave`, `_delay_ms`, `_role`) | values CamillaDSP cannot compile | a wrong slope is a graph that will not build | KEEP |
| S14 | `validate_manual_target_bindings` `driver_safety.py:1417-1483` | manual settings are keyed by a `target_id` that exists in the topology | a stale target id would silently drop one driver's values | KEEP |
| S15 | tweeter guard in `_bind_preset_to_topology` `staging.py:1207-1245` and `assert_crossover_honours_declared_floor` `crossover_declaration.py:364` | the compiled graph carries a high-pass at or above the declared tweeter floor | non-negotiable 2 (declared driver caps in DSP paths) | KEEP |
| S16 | `build_driver_research_request` declared context `driver_safety.py:885-976` and `validate_driver_research_request` `978-1215` | the held request's fingerprint must still match topology and targets at save | Wave 1; hypothetical; #5262 already excluded the declared context from the comparison | REMOVABLE whole (cut 4) |
| S17 | `validate_research_result_binding` `driver_safety.py:1227-1291` | the pasted reply names the same `target_id`s and models; its `request_fingerprint` equals the held request's | a reply for a different speaker is a real paste mistake; the fingerprint half re-checks S16 | KEEP the target/model half; REMOVABLE the fingerprint half |
| S18 | `validate_research_low_limit_plausibility` `driver_safety.py:1342-1385` | refuses an implausibly low LLM low-frequency limit | incident #2874, ADR-0227 §1 | KEEP |
| S19 | `_validate_v2_research_prefill` `design_draft.py:888-923` | the reply's echoed visible fields equal the draft's | hypothetical | REMOVABLE |
| S20 | `_rebound_to_restamped_request` `design_draft.py:1092-1125`, `_demote_legacy_driver_research_binding` `1305-1339` | compatibility for research saved under an older request stamp | exists only because S16 exists | REMOVABLE with S16 |
| S21 | the safety profile as a stored, re-validated artefact: `build_driver_safety_profile` `driver_safety.py:1979-2030` (self-confirms at save), `_validate_driver_safety_profile_shape` `2054-2384` (330 lines), `evaluate_driver_safety_profile` `2495-2672` (`stale` / `malformed` / `incomplete` / `confirmed`) | the profile stored at save must re-parse, and its fingerprint, topology id and targets must still match at read | Wave 1; there has been no separate confirm act since "Saving the declaration IS declaring it" (`main.js:1668-1670`); `result_fingerprint` is stored and never compared (section 8) | REMOVABLE: recompute from the draft on read (cut 7); keep the issues list (S22) |
| S22 | `_target_issues` `driver_safety.py:1594-1649` makes the profile `incomplete`, and Apply refuses `driver_safety_profile_not_confirmed` (`measurement_emit.py:76-82`) | `level_duration_limits` (`1619`), `measurement_band` (`1607`), `hard_excitation_band` (`1605`) must be declared before the box may play its baseline | these are measurement-protocol inputs; playing needs only the tweeter floor (S15) | REMOVABLE from the Apply path (move to measurement admission); keep `required_highpass_missing` (`1631-1636`) |
| S23 | crossover preview blockers `crossover_preview.py:540` `design_draft_not_ready`, `548` `design_draft_needs_research`, `317` `crossover_candidate_frequency_missing` | a preview needs numbers | a graph cannot be compiled without an Fc | KEEP |
| S24 | `crossover_frequency_above_lower_driver_range` blocker `crossover_preview.py:389` | Fc above the research's "usable to X Hz" | an advisory LLM claim; the sibling `crossover_below_declared_protection_floor` (`343`) is already a warning | REMOVABLE as a blocker (make it a warning) |
| S25 | `_validate_preview_freshness` `crossover_preview.py:720-756`, three `stale` codes on the persisted preview | the file must match the draft it was built from | the apply path never reads the file: `measurement_emit.py:83` rebuilds `build_crossover_preview(draft)` | REMOVABLE with the file (cut 8) |
| S26 | contiguity in `_bind_preset_to_topology` `staging.py:1138-1157` | active outputs contiguous from DAC output 1 | defines the ring and outputd width (`runtime_contract.py:709-756`: `max(index)+1`); a gap is a real lane mismatch | KEEP |
| S27 | role order `staging.py:1166-1205` (already waived through `allow_mapped_role_order` `1301-1314`) | woofer→tweeter output order for the "first protected slice" | the compiled graph maps roles by index; order is a convention | REMOVABLE |
| S28 | `reviewed_candidate_refusal` twice: `sound_active_speaker.py:1738` and `correction_crossover_v2_apply.py:89` (display copy `commissioning_coordinator.py:144`) | the page's `candidate_fingerprint` equals the one compiled now | Wave 1; hypothetical (one operator); Apply compiles from the saved draft regardless | REMOVABLE (both) |
| S29 | `classify_bass_extension_graph(desired)` proof `correction_crossover_v2_apply.py:74-77` | the compiled graph re-proves `volume_limit`, declared floors and muting | `volume_limit` is non-negotiable 1; floors are S15; the remainder re-checks the compiler that just ran | KEEP the `volume_limit` and floor parts; the rest is a compiler self-check (last on the cut list) |
| S30 | `validate_camilla_config` → `camilladsp --check` `correction_crossover_v2_apply.py:95-96`, `dsp_apply.py:244`, with `check_volume_limit` (`camilla_config_contract.py:147`) | the graph loads in the real binary; `devices.volume_limit` is 0.0 | non-negotiable 1 and the only external proof | KEEP |
| S31 | `_ensure_commission_startup_anchor` `web_commissioning.py:422-536` (one caller, `939`) | the all-muted anchor is staged before commissioning tones | a mid-commission reboot must come up muted | KEEP for commissioning; it is not the reboot fix (section 7) |
| S32 | CSRF header guard `sound_setup.py:557` / `_common.py:1083-1114`; follower block `sound_setup.py:984-1003` | browser origin; a multiroom follower cannot change DSP | shared with every wizard | KEEP (out of scope) |

### 4c. Boot-time gates and the non-negotiable doors

| # | Gate (file:line) | What it checks | What it protects | Verdict |
|---|---|---|---|---|
| B1 | `active_staged_metadata_mismatch` `runtime_contract.py:3280-3299`, `3336-3363` | a staged anchor is ignored when its metadata topology differs from the current one | an anchor compiled for another lane map is the wrong graph | KEEP |
| B2 | topology stamps `runtime_contract.py:4806-4817` and `deploy/bin/jasper-camilla-topology-gate` (148 lines) | CamillaDSP starts only on a statefile stamped for the current topology | ADR-0283 (owner ruling; the scenario has not been observed) | KEEP per the ADR; it carries no removal condition and should |
| B3 | hold rung `runtime_contract.py:4521-4540` | the commissioning anchor is preserved while the `/run` hold marker exists | commissioning | KEEP |
| B4 | commissioning SPL stop `commission_wiring.py:136-149`; `volume_limit` doors `environment.py:351-368`, `staging.py:1448`, `check_volume_limit` `camilla_config_contract.py:147`, `set_volume_db` `camilla.py:687-696` | the hearing ceiling | non-negotiable 1 | KEEP |

### 4d. Tally

50 gates graded: **22 KEEP** and **28 REMOVABLE** (first word of the
verdict column). Three KEEP rows are partial (S9, S17, S29 keep one half
and drop the other) and one REMOVABLE row keeps a single site (S12). What
the KEEP rows protect: non-negotiable 1 (S29, S30, B4); non-negotiable 2
(S15); incidents (S4 #2130, S5 #5261, S18 #2874, C5 #5259, C11
#2814/#2819); an ADR ruling (B2, ADR-0283); structural correctness of the
graph the box will play (S3, S13, S14, S23, S26, B1); commissioning (S31,
B3, S9's lane-change half); an irreversible action (C10); shared browser
security (S32).

Nothing graded REMOVABLE touches a non-negotiable: every `volume_limit`
door, the SPL stop, the tweeter floor proof and `camilladsp --check` are
KEEP. The removable set is, almost entirely, Wave 1's fingerprints and
revisions, the identity ceremony, and client mirrors of server decisions.

## 5. Contracts

All routes live under the `/sound/speaker/` nginx location and are served by
`jasper/web/sound_setup.py` (`_GET_ROUTES` `1011-1029`, `_POST_ROUTES`
`1031-1055`). Every POST passes the CSRF header guard first
(`dispatch_post(guard="header")` `sound_setup.py:557`, `_common.py:1083-1114`)
and the follower block (`_run_post_route` `984-1003`, content-DSP paths only).
Bodies are bounded JSON objects (64 KiB, `read_json_object` `_common.py:490-553`);
an unreadable body is `400 {"error": …}` (`sound_setup.py:922-924`). Unless
noted, a builder exception is `502 {"error": …}` via `send_route_failure`
(`_common.py:1375-1402`). Every builder also emits a `log_event` line named
in `_GET_JSON_ROUTES` (`sound_setup.py:335-389`).

**Client-held state a request depends on** is marked ⚑ and graded: **guard**
(a real concurrency guard for a real writer race) or **nanny**.

### `GET /output-topology` → `_output_topology_payload` (`sound_active_speaker.py:226-248`)

Response:
```
{ output_topology: OutputTopology.to_dict(include_evaluation=True)   # artifact_schema_version, kind, topology_id, name, status, hardware{device_id, device_label, physical_output_count, clock_domain_id, clock_domain_label, outputs[{index, human_label, terminal_label, state}], card_id?, child_devices?}, speaker_groups[{id,label,kind,mode,position{x,y,rotation_degrees},channels[{role, physical_output_index, identity_verified, startup_muted, protection_required, protection_status, driver_style?, output_variant?, human_output_label?, crossover_fc_hz?}]}], routing{main_left_group_id, main_right_group_id, mono_group_id, subwoofer_group_ids[]}, pairing_intent, safety{...}, evaluation{status, assigned_output_count, unused_output_count, blockers[], warnings[], safety{}}
  topology_revision: "sha256:…" | "missing",
  output_hardware: OutputHardwareState.to_dict() | null,
  hardware_adoption: {allowed: bool, identity: str},
  hardware_mismatch: {saved_label, current_label, saved_count, current_count, clock_blockers[], message} | null,
  hardware_repin: {child_count, replaced_child_count, reverify_output_indexes[], reverify_output_labels[]} | null,
  i2s_hat: {visibility, available, shared_usb_data_port, reason, intent_error, profiles[], desired_profile_id, detected_profile_id, detected_label, warnings[], restart_required},
  channel_identity: {kind, status, topology_status, assigned_channel_count, verified_channel_count, unverified_channel_count, sound_tests_allowed:false, targets[{id, speaker_group_id, speaker_label, speaker_kind, speaker_mode, role, driver_style, physical_output_index, human_output_label, assigned, identity_verified, startup_muted, protection_required, protection_status, sound_test_blockers[], output_variant?}], next_step},
  clock_domain: {kind, status, clock_domain_id, clock_domain_label, clock_domain_count, coherent_physical_output_count, multi_device_aggregate_supported, composite_clock_supported?, future_multi_device_lab_path, sound_tests_allowed:false, issues[], notes[], recommendation, child_devices?, observed_hardware?},
  active_playback_route: {kind, playback_device, playback_device_source, transport_channel_count, required_active_output_count, active_group_count, subwoofer_group_count, subwoofer_supported, fits_required_outputs, ready, issues[]} }
```
Of these eleven keys the page reads seven; `clock_domain` and `channel_identity`
feed two "hardware details" rows and the per-lane identity buttons; `i2s_hat`
is for `/sound/output/`. A blank speaker (no file) returns a hardware-seeded
draft with `topology_revision: "missing"`.

### `POST /output-topology` → `_save_output_topology_payload(require_revision=True)` (`305-401`)

Request: `{ output_topology: <full topology dict as above, evaluation ignored>, topology_revision: "sha256:…" ⚑ }`
(the page sends `outputTopology.draft` verbatim, `main.js:3908-3911`).

Checks in order: `topology_revision` must equal the current file's byte
hash (`315-322` → 409 with a fresh topology payload plus `error`,
`sound_setup.py:698-708`) ⚑ **nanny** on a one-owner box — the page is the only
writer and the same page already refuses to save while `dirty`; the revision
exists to protect a second tab. Then `OutputTopology.from_mapping` (400 on
`OutputTopologyError`), `_refuse_undrivable_layout` (400, `250-284`),
`_refuse_duplicate_physical_outputs` (400, `287-302`), software-guard request
(`334`), stop tones, park, commit, converge, reconcile.

Response: the GET payload plus
`{ runtime_convergence: {ok, decision{status, selected_config_path, reason, …}, live_applied, error}, reconcile: {ok, converging, error?}, save: {status: "saved"|"converging"|"needs_attention", message} }`.
The saved copy strips any `identity_verified: true` the page sent unless the
server already held it for the same lane and device (`output_topology.py:2235-2290`).

### `POST /output-topology/reset` → `_reset_output_topology_payload` (`443-546`)

Request: `{ topology_revision: "sha256:…" ⚑, detected_hardware_identity: str ⚑ }`
(both required, `404-427`; 400 if missing, 409 `{…, conflict: "topology_changed"|"detected_hardware_changed"}` if moved).
The identity token is a hash of the reconciler's observed-hardware record
(`output_hardware.py:344-386`). ⚑ **nanny**: the reset adopts whatever
hardware is observed *at commit time* anyway (`477-481`), so echoing the token
only refuses a reset when the hardware moved between render and click, which
the outcome would have reflected correctly regardless.

Effect: park → save zero-group topology with adopted hardware → delete nine
state files (`reset.py:59-73`) → reconcile. Response: GET payload plus
`{ reset: {status: "reset"|"converging"|"needs_attention", message}, saved: true, runtime_convergence, reconcile }`.

### `POST /output-topology/repin` → `_repin_output_topology_payload` (`549-656`)

Same request shape as reset ⚑ (here the identity token is load-bearing: the
re-pin rewrites child serials from the observed record, `_composite_repin_pairs`
`output_topology.py:1690-1750`) — **guard**, but composite-DAC only (dual
Apple); a single-DAC box never sees the offer (`hardware_repin` is `None`).
Response: GET payload plus `{ repin: {status, message}, saved: true, runtime_convergence, reconcile }`; the box stays parked (`598-605`).

### `GET /active-speaker/design-draft` → `_active_speaker_design_draft_payload` (`999-1014`)

Response: `load_design_draft(topology)` (`design_draft.py:1341-1477`) wrapped by
`installation_view` (`installation.py:78-79`):
```
{ artifact_schema_version:1, kind:"jts_active_speaker_design_draft", status: "not_saved"|"unreadable"|"blocked"|"needs_research"|"ready_for_review", revision:int, path,
  created_at?, updated_at?, topology{…with evaluation}, operator_inputs{full_range?,woofer?,mid?,tweeter?,subwoofer?,notes?,target_models{}},
  driver_research_request: <request> | null, driver_research: <reply> | null,
  driver_safety_profile: <profile> | null, driver_safety_profile_evaluation{status, confirmed_and_current, profile_fingerprint, reasons[], authorizes_playback:false},
  driver_protection_policy_view{policy_version, targets[{target_id, role_class, max_auto_level_dbfs, low_limit_hz, low_limit_provenance, low_limit_summary}]},
  manual_settings{drivers[{target_id, role, model?, …safety fields…, pad?, installation?, source:"manual_settings"}], crossover_candidates[…], driver_spacing_mm} | null,
  summary{speaker_group_count, topology_roles[], required_driver_info_roles[], required_driver_target_ids[], driver_count, research_roles[], missing_research_roles[], missing_research_target_ids[], extra_research_roles[], crossover_candidate_count, manual_driver_count, manual_crossover_candidate_count, manual_roles[], manual_target_ids[], missing_driver_info_target_ids[], missing_driver_info_roles[], missing_crossover_candidate_pairs[], candidate_frequencies_hz[], warning_count},
  permissions{…six may_* booleans…}, safety{…seven booleans…}, issues[{severity, code, message}], next_step,
  installation{fields{…}, source, provenance, authorizes_playback:false, drivers[{target_id, role, inputs{}, amplifier_estimate, acoustic_limit}]} }
```
`permissions` and `safety` are eleven constant booleans nothing reads.

### `POST /active-speaker/design-draft` → `_active_speaker_design_draft_save_payload` (`1066-1145`)

Request (exact allow-list `1079-1085`):
```
{ operator_inputs: {full_range?, woofer?, mid?, tweeter?, subwoofer?, notes?, target_models{target_id: model}},
  manual_settings: {drivers[…], crossover_candidates[…], driver_spacing_mm} | null,
  driver_research_request: <the server's own minted request> ⚑ | null,
  driver_research: <pasted reply> | null,
  expected_revision: int ⚑ (required, 1090-1099) }
```
Checks: unknown top-level key → 400; `expected_revision` ≠ stored revision →
409 with the current draft plus `error` (`design_draft.py:1506-1515`,
`sound_setup.py:604-609`) ⚑ **nanny** (same one-owner argument; the page
already refuses to overwrite a dirty form, `main.js:3834-3846`); software
guards requested and **written to the topology** (`1102`); then
`build_design_draft`: unknown `target_models` targets → 400; manual driver
bindings; `validate_driver_research_request` against the current topology,
models and notes (`driver_safety.py:978-1214`, "stale for the current visible
inputs" `1207-1211`) ⚑; a v2 reply requires the request (`design_draft.py:1167-1174`)
and must echo its fingerprint and every target (`driver_safety.py:1227-1263`) ⚑;
low-limit plausibility (`1339-1378`); `_validate_v2_research_prefill`
(`design_draft.py:888-919`); candidates' filter/slope must be in the compiler
vocabulary (`227-282`); safety profile rebuilt. Response: the GET shape (same
`installation_view`). Note the page **always** sends `driver_research_request`
(its held copy or null), so a form edit after Copy silently saves the draft
without a request and a later paste is refused.

### `POST /active-speaker/driver-research-request` → `_active_speaker_driver_research_request_payload` (`1017-1063`)

Request: `{ operator_inputs: {…as above…}, manual_settings: {…as above…} | null }` (allow-list `1030`).
Nothing is persisted. Response:
```
{ request: {artifact_schema_version:1, kind:"jts_active_crossover_driver_research_request", topology_id, hardware{…}, targets[{target_id, target_fingerprint, speaker_group_id, speaker_group_mode, role, driver_style, physical_output_index, physical_output_label, manufacturer_and_model, operator_declared_context{…normalised safety fields…}?}], build_notes?, request_fingerprint},
  prompt: str,
  safety: {no_audio:true, loads_camilla:false, applies_filters:false, authorizes_playback:false, research_is_advisory:true} }
```
Requires a model for every target (400 otherwise, `driver_safety.py:900-906`)
and at least one target (`948-951`). The page keeps `request` in memory and
sends it back on the next draft save ⚑ — see section 6.

### `GET /active-speaker/crossover-preview` → `_active_speaker_crossover_preview_payload` (`1184-1200`)

Response: `load_crossover_preview(current_design_draft)` (`crossover_preview.py:759-829`):
```
{ artifact_schema_version:2, kind:"jts_active_speaker_crossover_preview", status: "not_prepared"|"unreadable"|"blocked"|"not_applicable"|"ready_for_protected_staging"|"stale", path, created_at, updated_at,
  source{design_draft_status, topology_id, design_draft_updated_at, design_draft_fingerprint, preview_fingerprint},
  drivers{role: <merged, low-limit-derived driver>}, summary{speaker_group_count, active_crossover_count, ready_crossover_count, blocker_count, warning_count},
  groups[{group_id, label, kind, mode, crossovers[{id, between_roles[2], status, source, candidate{}, proposed_frequency_hz, lower_polarity?, upper_polarity?, delay_ms?, delay_target_role?, declared_protection_floor_hz, filters[{role, filter, frequency_hz, filter_type, slope_db_per_octave, channel{}}], issues[]}]}],
  permissions{may_explain, may_prepare_protected_startup_config, …}, safety{…}, issues[], next_step }
```
`stale` is computed on every read against the current draft's fingerprint
(`720-756`) — the file on disk never says `stale`.

### `POST /active-speaker/crossover-preview` → `_active_speaker_crossover_preview_save_payload` (`1203-1231`)

Request: `{}` (body ignored). The server reloads the draft, requests software
guards (**writes the topology**, `1212`), rebuilds the draft against the
current topology (`1213-1221`), and writes the preview. Response: the GET
shape. The same sequence exists a second time as
`web_commissioning.regenerate_crossover_preview_from_current_draft` (`220-262`).

### `GET /active-speaker/baseline-profile` → `_active_speaker_baseline_profile_payload(write=False)` (`1652-1672`)

Response: `compile_commissioning_profile` (`baseline_profile.py:292-339`) — a full
in-memory compile of the candidate graph on every GET — merged with
`{ previous_candidate_fingerprint, tuning_programs: PROGRAM_ENTRIES }`:
```
{ artifact_schema_version:1, kind:"jts_active_speaker_baseline_profile_candidate", status: "ready_to_compile"|"blocked", permissions{may_apply:false, may_compile}, issues[],
  candidate_artifact_path, source{topology_id, topology_fingerprint, design_draft_updated_at, crossover_preview_updated_at, crossover_preview_fingerprint, measurements_updated_at, measurement_summary_fingerprint, measured_candidate_fingerprint, driver_protection_fingerprint?, fingerprint},
  config{path, basename, sha256, exists, playback_device, domain}, corrections{role:{gain_db, delay_ms, inverted}}, linearization{}, corrections_source{}, gain_provenance{}, corrections_provenance{}, level_match{}, automatic_candidate{}, linearization_outcome, trim_decision{}, tuning_owner, blend_correction[], room_correction{}, recomposition_snapshot{…}, timing?, candidate_fingerprint,
  previous_candidate_fingerprint: str|null, tuning_programs[{id,title,description}] }
```
The page reads `permissions.may_apply`, `candidate_fingerprint`, `config`,
`issues`, `previous_candidate_fingerprint`, `tuning_programs`, and the
corrections for the level-match card.

### `POST /active-speaker/baseline-profile` (compile + write, `632-643`) and `POST /active-speaker/baseline-profile/apply` (`644-655`)

Request `{}` and `{ expected_candidate_fingerprint ⚑ }` respectively. **No page
calls either** (grep of `deploy/assets`); the page uses `save-and-apply`.

### `POST /active-speaker/baseline-profile/save-and-apply` → `_active_speaker_finish_commissioning_payload` (`1722-1800`)

Request: `{ expected_candidate_fingerprint: str ⚑ }` (the page sends
`profile.candidate_fingerprint` from its last GET, `main.js:4148-4154`).
Checks: recompile and compare fingerprints (`reviewed_candidate_refusal`,
`1737-1740`); then `apply_candidate` recompiles **again** and compares
**again** (`correction_crossover_v2_apply.py:85-89`), proves the graph
(`69-71`, the volume-limit door is inside `classify_bass_extension_graph`),
validates with `camilladsp --check` (`91-93`), loads over the websocket,
persists the applied record, banks the trim, promotes the canonical copy,
updates v2 state, restores the mux source. ⚑ **nanny** as a client-held
fingerprint: the same request could simply apply "the current candidate";
the page's copy goes stale whenever the draft is saved between the GET and
the click, which then refuses with `baseline_candidate_fingerprint_mismatch`.

Response (HTTP 200 whatever happened; the page reads `status`):
```
{ status: "applied"|"blocked"|"apply_failed", profile{…applied record…}, apply{result, active_config_path, prior_config_path, rollback_*…}|null, issues[],
  declaration_update{status}, expected_post_apply_offset_db, source_selection_restore{status, reason, state?|error?},
  commissioning_cleanup{status | summed_test, ramp}, output_safety{safety_muted, reason, active_config_path} }
```

### `POST /active-speaker/baseline-profile/restore` (`656-665`)

Request `{}`; runs `apply_candidate(previous=True)`; 400 `refusal_envelope`
on `CrossoverV2Refused` (`_common.py:147-172`), else the apply response.

### `POST /active-speaker/channel-identity` → `_active_speaker_channel_identity_save_payload` (`681-777`)

Request: `{ speaker_group_id|group_id: str, role: str, output_variant?: "primary"|"rear", identity_verified: bool }`.
Effect: writes the flag; un-confirming a lane of a roleful topology parks the
box durably (`706-741`). Response: GET topology payload plus
`identity_park: {parked: bool, message}` when a park was attempted.

### `GET /active-speaker/commission-state` → `_active_speaker_commission_state_payload` (`1506-1586`)

Response: `{ kind:"jts_active_speaker_commission_state", commission_load{status, target{}, rollback_available, runtime_status{}, issues[]}, ramp{confirmed_roles[], pending}, floor{status, floor_audio_confirmed, last_level_dbfs, last_operator_result{}} }`.
The page stores it as `activeSpeaker.commission` and reads only
`ramp.pending` (`commissionPendingStep`, `main.js:2082-2086`) to abort a ramp
before an identity change.

### `GET /active-speaker/commissioning-view` → `_active_speaker_commissioning_view_payload` (`1589-1626`)

Response: `build_commissioning_view` (`commissioning_coordinator.py:112-204`) plus `timing`, `active_summed_test` (always `{active:false}`, section 8):
```
{ artifact_schema_version:1, kind:"jts_active_speaker_commissioning_view", status, steps[{id, label, status, message}], current_step, next_action{id, label, enabled, endpoint, method, body, program?, round_dir?},
  first_experiment{…, complete}, combined_groups:[], applied_profile{stands, verdict, exists, candidate_fingerprint, record, applied_at, config_path, disclosures[]},
  review{ready, may_apply, status, issues[]}, driver_values{complete, design_ready, preview_ready, safety_profile_confirmed}, output_identity{assigned_channel_count, unverified_channel_count, complete},
  driver_target_proof{…}, driver_spacing_mm, driver_checks{…}, summed_validation{…}, test_level{}, runtime{commission{}, startup_load{}}, timing{saved, verification}, active_summed_test{active:false} }
```
Loading it recompiles the baseline candidate (`load_commissioning_view` `227`)
and reads seven stores. The page reads `steps`, `current_step`,
`next_action`, `applied_profile`, `review.may_apply`, `timing`.

### `GET /active-speaker/measurements` → `_active_speaker_measurements_payload` (`1629-1649`)

Response: `load_measurement_state(topology)`; the page stores it and reads
nothing from it on the setup path (`activeSpeaker.measurements` has no reader
in `main.js` after `patchActiveSpeaker`).

### `GET /active-speaker/tuning-handoff?program=` (`sound_setup.py:502-518`)

Response: `{ status:"ready"|"not_ready", reason, binding{speaker_name, hostname, declaration_url, crossover_url, design_draft_revision, applied_candidate_fingerprint, applied_record, applied_at, latest_round_dir}, driver_spacing_mm, programs[], program, prompt }`. Post-apply only.

### Read-only routes with no page caller

`GET /active-speaker/environment`, `/safe-playback`, `/calibration-level`
(+ POST), `/bringup-preflight`, `/startup-load`, `/staged-config`,
`/channel-identity` (GET), `POST /active-speaker/commission-ramp-abort`
(one caller, `main.js:4082`, before an identity change). The seven GETs are
reachable only from tests (`grep -rn "active-speaker/" deploy/assets` names
only `main.js` and `seat-level.js`).

### Summary of client-held dependencies

| Endpoint | Depends on | Verdict |
|---|---|---|
| `POST /output-topology` | `topology_revision` | nanny (one writer, page already blocks on dirty) |
| `POST /output-topology/reset` | `topology_revision`, `detected_hardware_identity` | nanny (reset re-reads hardware at commit) |
| `POST /output-topology/repin` | same two | guard, composite-only |
| `POST /active-speaker/design-draft` | `expected_revision`, held `driver_research_request` | nanny; the held request is the cause of #5259, #5262 and the silent reply drop |
| `POST …/save-and-apply` | `expected_candidate_fingerprint` (checked twice) | nanny |
| `POST …/channel-identity` | none | — |

## 6. The research loop

**1. What the request builder puts in the prompt.** `build_driver_research_request`
(`driver_safety.py:884-975`) walks the physical targets
(`driver_research_targets` `140-160`: the active drivers, or the passive
full-range drivers) and for each emits `target_id`, `target_fingerprint`,
`speaker_group_id`/`_mode`, `role`, `driver_style` (from the topology
channel, `driver_safety.py:892-897`), `physical_output_index`/`_label`,
`manufacturer_and_model` (from `operator_inputs.target_models`, falling back
to the per-role text box when the role is unique, `900-906`) and
`operator_declared_context` (`923-930`). The core also carries
`topology_id`, the whole `hardware` block and `build_notes`; the fingerprint
hashes all of it (`957-975`). The prompt (`driver_safety_prompt.py:138-292`)
embeds a *projection* — only `_PROMPT_TARGET_KEYS` (`28-37`) plus
`build_notes` — and asks for one fenced JSON block whose drivers echo
`target_id`, `target_fingerprint`, `model` and the request fingerprint
(`RESULT SHAPE`, `236-268`), with a LIMITS section derived from the style
plausibility band (`103-135`).

**2. Why manual values ride along as "operator declared context".**
`operator_declared_context` is `normalise_driver_safety_fields(visible)`
(`driver_safety.py:923-930`): the operator's current safety fields
(minimum crossover, bands, filters, cabinet, limits) for that target. The
prompt tells the assistant to "Treat operator_declared_context as
authoritative; if an installation choice is undeclared, leave it unknown"
(`driver_safety_prompt.py:172`). Its stated purpose is that the reply must
not override an installation fact (enclosure, class, pad), and #5259 showed
the cost: a stale form re-sent old limits as context. But the page strips
`cabinet.enclosure_kind`, an explicitly chosen `driver_class` and `pad` from
a reply anyway (`main.js:1085-1093`), and `_profile_core` reads only the
visible values, never the reply (`driver_safety.py:1705-1921`). So the
context's only remaining function is to *prefill the assistant with the
answer* — the same numbers the reply is supposed to research. Until #5262 it
was also part of the staleness comparison, which is what made "paste, then
save" impossible on a fresh speaker.

**3. What the reply is allowed to change.** Everything in
`_V2_RESEARCH_DRIVER_FIELDS` (`774-806`) that survives the client fold:
model (only when the target has none, `main.js:1055-1057`), impedance,
sensitivity, minimum crossover + slope, low-pass, `do_not_test_below_hz`,
gain offset, bands, protection filters, cabinet geometry (not
`enclosure_kind`), level limits, `driver_class` only if the operator left it
unknown, radiating diameter, plus one crossover candidate per pair (highest
confidence, `1100-1135`) and a sensitivity-derived trim proposal
(`proposeSensitivityTrims` `driver-model.js:552-565`). On the server a reply
outside the style plausibility band is refused at intake
(`validate_research_low_limit_plausibility` `1339-1378`, ADR-0227 §1); a
reply whose targets, models or fingerprint differ from the saved request is
refused (`1227-1263`).

**4. How the reply is folded into the visible form.** "Load information"
(`parseDriverResearchImport` `main.js:3714-3734`) recovers the JSON from a
pasted chat reply (`extractDriverResearchJson` `driver-model.js:1043-1071`),
summarises it (`580-651`), writes the fields into `driverResearch.settings`
(`applyDriverResearchToManualSettings` `1037-1138`) and marks the form dirty.
The reply itself stays in `importText` and is re-parsed and re-sent as
`driver_research` on "Save values" (`3765-3773`) — but only if
`driverResearch.researchRequest` is still held; otherwise it is dropped with
"Target-bound research was invalidated by a visible edit" (`3769-3772`).
Server-side the saved draft then carries the reply verbatim (normalised) and
`_validate_v2_research_prefill` refuses the save if any comparable field of
the reply differs from the visible value for that target
(`design_draft.py:888-919`) — i.e. the reply is only storable when the form
still equals it, which is exactly the state "Load information" left the form
in, minus anything the operator corrected. A corrected value therefore means
the whole reply is dropped (client) or refused (server), never "kept with
one override".

**5. How the safety profile is derived.** `build_design_draft` calls
`build_driver_safety_profile(topology, manual_settings, driver_research,
saved_at)` (`design_draft.py:1244-1258`). `_profile_core`
(`driver_safety.py:1705-1921`) takes, per physical target, the **visible**
values (target-specific first, legacy per-role second, `230-254`), resolves
the low limit and derives the hard band, measurement band and protective
high-pass from it (`1788-1836`), copies provenance from the reply only where
the visible value equals the reply's (`1738-1760`), stamps the code-owned
policy, and lists issues (`_target_issues` `1596-1650`: missing bands,
limits, filters, model). No issues → status `confirmed` with a
`confirmation` block that is a copy of the profile fingerprint (`2015-2035`;
"Saving the declaration IS declaring it"). The reply contributes only
provenance badges and the two digests in `research` (`1922-1929`). The
evaluation (`2495-2672`) then re-derives the same issues and compares them to
the stored ones, refuses on any fingerprint or canonical-form mismatch, and
reports `stale` when the topology's targets moved.

**6. Is the prompt-then-paste coupling needed?** No. What the reply actually
has to match is the *speaker* (which targets, which models) — and that is
already re-derived from the topology and the form on every save
(`validate_research_result_binding` compares target ids, fingerprints, roles
and models, `1236-1263`). The request fingerprint adds nothing to that
binding except the `hardware` block and `build_notes`, neither of which the
reply is asked to echo. Concretely:

- The prompt could carry only `targets[{target_id, role, driver_style,
  manufacturer_and_model}]` and `build_notes`; the RESULT SHAPE already asks
  for `target_id` and `model` to be echoed (`driver_safety_prompt.py:238-243`).
- The paste could be accepted whenever every echoed `target_id` is a current
  target and every echoed `model` equals the current model — the check that
  exists at `1236-1263` — with no stored request, no `request_fingerprint`,
  no `operator_declared_context`, no `_rebound_to_restamped_request`
  (`design_draft.py:1096-1124`), no `_demote_legacy_driver_research_binding`
  (`1305-1338`), no `researchRequest` in page memory and no
  `invalidateDriverResearchBinding`.
- Manual limits become a review step *after* the paste: the fold already
  writes the reply into the visible fields and the echo panel already shows
  each value with its badge (`driver-fields.js:604-668`); the operator edits
  what is wrong and saves. The one thing lost is the server refusing a
  stored reply that disagrees with the visible values
  (`_validate_v2_research_prefill`), which today forces "drop the reply"
  rather than "keep the reply, override one field"; provenance for an
  overridden field already degrades to "operator-entered" in `_profile_core`
  (`1748-1760`), so nothing downstream needs the refusal.

The plausibility refusal at intake (`1339-1378`) is the one gate in this loop
with a documented incident (ADR-0227 §1, #2874) and it does not depend on the
request; it survives the simplification unchanged.

## 7. Startup and reboot

**The premise is half right.** The boot path does read the applied baseline
as a first-class candidate: `converge_boot_statefile` passes
`applied_baseline_path` and `consider_applied_baseline=True`
(`jasper/active_speaker/runtime_convergence.py:96-135`), the selector
classifies it as `preferred_graph` (`runtime_contract.py:4458-4466`) and has
a rung that selects it, `select_active_baseline` (`4544-4557`). The docs
agree that this is the intended boot graph (ADR-0193:33-37 "a reboot …
reverts by doing nothing"; ADR-0227:116-119 "a commissioned box always comes
back to audio"; ADR-0184:38-40). What actually happens on jts3 is decided by
the conjuncts on that rung and its sibling:

```
runtime_contract.py:4502   identity_confirmed = roleful_identity_confirmed(topology, contract)
runtime_contract.py:4507   … and current_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME and identity_confirmed   (preserve_current)
runtime_contract.py:4547   … and preferred_graph.classification == GRAPH_APPROVED_ACTIVE_RUNTIME and identity_confirmed   (select_active_baseline)
runtime_contract.py:623-626  all(channel.identity_verified for … if channel.physical_output_index is not None)
```

`identity_verified` has one writer: `set_channel_identity_verified`
(`output_topology.py:1629-1656`), reached only from the page's per-lane
"Confirm output" button (`POST /active-speaker/channel-identity`,
`sound_active_speaker.py:681-777`). Any topology save that merely claims the
flag is stripped (`_with_server_owned_identity` `2235-2290`); a re-pin
clears it (`1935-2015`). The four builder steps
(`commissioning_coordinator.py:153-165`) report `output_identity.complete`
but never require it, so "profile applied" is reachable with every lane
unconfirmed.

**Apply bypasses the selector.** `apply_candidate` loads the graph through
`camilla.set_config_file_path` (`jasper/web/correction_crossover_v2_state.py:210-212`,
`correction_crossover_v2_apply.py:104-119`, `baseline_profile.py:189-244`,
`dsp_apply.apply_dsp_config`). `CamillaController.set_config_file_path`
checks only the hearing ceiling (`jasper/camilla.py:891-925`) and CamillaDSP
persists the new pointer in its statefile. Nothing on the apply path calls
`safe_graph_for_current_topology`, `roleful_identity_confirmed`,
`stage_protected_startup_config` or `stamp_statefile_topology` (grep of
`baseline_profile.py`). So the applied graph plays until the next pass that
re-decides from disk.

**Every boot and deploy re-decides.** `jasper-audio-hardware-reconcile.service`
runs before `jasper-outputd` on every boot (`deploy/systemd/jasper-audio-hardware-reconcile.service:5,28,36,41`;
the `--changed` skip stamp lives under `/run`, so a boot always runs a full
pass) and `install.sh` runs the same convergence with `--reason install`
(`deploy/install.sh:744-764`, `deploy/lib/install/systemd-units.sh:1371,1636`).
`Pass.converge_runtime_graph` (`reconcile.py:1388-1409`) calls
`converge_boot_statefile(write_statefile=True)`, which
`write_camilla_statefile`s whatever the selector chose
(`runtime_contract.py:4770-4818`) and stamps the topology fingerprint
beside it (`output_topology.py:2137-2166`); `jasper-camilla.service` then
starts on that pointer (`ExecCondition jasper-camilla-topology-gate`,
`ExecStart camilladsp --statefile …` with no positional config).

**Where an unconfirmed roleful box lands.** With both approved rungs refused
the ladder tries the staged all-muted anchor (`4604-4641`), whose locator is
`/var/lib/jasper/active_speaker_staged_config.json` (`staging.py:94`).
Only two callers ever write it: `web_commissioning._ensure_commission_startup_anchor`
(`406-536`, reached from the crossover page's commissioning and from the
dead summed-test lane) and `startup_load.reemit_staged_startup_anchor`
(`startup_load.py:1159`). A box that went topology → drivers → preview →
Apply never staged one, so the ladder falls to `parked_muted` (`4685-4712`),
the reconciler repoints the statefile at
`configs/active_speaker_parked.yml`, and CamillaDSP boots into a File-sink
all-muted graph. A staged anchor would only change `parked_muted` into
`select_active_startup` (`4630-4641`), which is also silent. **Staging is
not the missing piece; identity confirmation is**, and the gate that
demands it was added for the composite-DAC re-pin hazard
(`output_topology.py:1945-1960`, `runtime_convergence.py:249-265`), not for
a single-DAC box.

The second-order refusals that can also park a re-decided applied graph:
the applied record must name an exact `config.path` that exists
(`_candidate_locator` `3812-3826`); the YAML must re-prove as
`GRAPH_APPROVED_ACTIVE_RUNTIME` through `_active_graph_evidence`
(`2488-3269`), which on a cardioid layout additionally requires every rear
output terminally muted (`2517-2526`, issue #5161); a
linearization-headroom regression blocks instead of parking (`4622-4638`).
`jasper-active-speaker runtime-safe-graph --json` (`jasper/cli/active_speaker.py:351-370`)
prints the decision and `preferred_graph.issues` without touching anything.

**Why the staged anchor is gated behind commissioning.** It is the rollback
anchor for the per-driver commission loads (`commission_load.py:85`,
`785`): a transient audible graph is loaded over it and rolled back to it.
Its metadata is what `_active_graph_allowed` demands for the
all-muted/guarded classes (`3336-3363`) — never for the approved runtime
class. So it has no role on the apply path and none in a reboot of an
applied box; it is commissioning bookkeeping.

**What "Apply also makes the box come back playing" would take.** One of:

1. **Make Apply confirm identity.** After a successful apply the operator has
   heard the speaker play through the crossover; treat that as the audition
   the per-lane button records: in `persist_applied_baseline_profile`
   (`baseline_profile.py:1658-1680`) or in the web finish payload
   (`sound_active_speaker.py:1722-1800`), mark every assigned lane
   `identity_verified` through `set_channel_identity_verified` inside
   `output_topology_mutation`. Cost: ~15 lines; the re-pin path still clears
   the flags it needs to clear. The test pin to move is
   `tests/test_active_speaker_runtime_contract.py:5212-5235`.
2. **Drop the identity conjunct from the two approved rungs**
   (`runtime_contract.py:4507`, `4547`) and keep `identity_unverified` as the
   warning it already is in `evaluate_output_topology`
   (`output_topology.py:1016-1023`). This removes the durable half of the
   re-pin park (`runtime_convergence.py:355-367` says so); a re-pinned
   composite would then resume audio at the next `jasper-camilla` bounce.
   Acceptable for a single-DAC box; the composite case would need the
   re-pin to clear the applied record instead.
3. **Gate Apply on `roleful_identity_confirmed`** — the strict option; it
   keeps the gate and moves the surprise from reboot to the Apply button,
   where the page can say "confirm each output first".

Option 1 is the smallest change that honours ADR-0227's "a commissioned box
always comes back to audio". None of the three touches the hearing ceiling
(`camilla.py:891-925` stays as the door on every load).

## 8. Smells

Counts per file. "History" = comment lines that narrate history, cite a PR
or issue number as the reason, or address a reviewer (script:
`grep` for issue numbers, "used to", "no longer", "retired", "legacy",
dates, "ruling", "reviewer"; hits verified by reading). Dead = no reference
anywhere in `jasper/ deploy/ scripts/ tests/`.

| File | Lines | Funcs > 150 lines | History / reviewer comment lines | Dead code | Duplicated helpers | Impossible-input guards | Notes |
|---|---|---|---|---|---|---|---|
| `jasper/web/sound_active_speaker.py` | 1800 | 0 | 7 (`384`, `511`, `615` "#3094" ×3, `724`, `1130`) | **~235 lines**: summed-test/commission-tone session block `1234-1469` (session globals never assigned except to `None`, `1265-1268`, `1309-1312`; `SUMMED_TEST_*` `1253-1256`; `_summed_test_session_active`, `_active_summed_test_snapshot`, `_attach_active_summed_test`, `_stop_summed_test_tone_locked`, `_active_speaker_stop_summed_test_tone`, `_stop_commission_tone_locked`, `_active_speaker_stop_commission_tone`); their callers (`339-343`, `455-458`, `587-590`, `898`, `1746-1748`, `1620-1621`) always see `idle`; `_SUMMED_TEST_ARM_REPORT` `1261-1268` duplicates `web_commissioning.py:161-168`; the 7 read-only diagnostic route builders (`780-980`) have no page caller | `_active_speaker_path_safety_evidence_path` `800-807` = `web_commissioning._path_safety_evidence_path` `299-306`; `_active_speaker_crossover_preview_save_payload` `1203-1231` = `regenerate_crossover_preview_from_current_draft` `220-262`; `_active_speaker_output_safety_from_config_path` `1706-1719` vs `setup_status.py:40-43` | 0 | `apply_measured_crossover_geometry` `1148-1181` lives in the web module but is imported by the crossover page and the apply path |
| `jasper/web/sound_setup.py` | 1090 | 2 (`_make_handler` 620, `_dispatch_post_route` 426) | 2 | 10 routes with no page caller (`1018-1027`, `1041`, `1046-1047`) | route tables restated twice (`_GET_JSON_ROUTES` `335-389` and `_GET_ROUTES` `1011-1029`) | 0 | 71% of the file is prose (page HTML + docstrings) |
| `jasper/active_speaker/design_draft.py` | 1548 | 1 (`build_design_draft` 176) | 11 | `_MAX_SOURCES` `67` unused | `_reject_unknown_keys`, `_text`, `_finite_float`, `_positive_float`, `_sequence`, `_mapping` duplicate `driver_safety.py` (`119-204` vs `269-364`); `_CANDIDATE_FIELDS` `64-77` = `driver_safety._MANUAL_CANDIDATE_FIELDS` `69-82`; `_topology_roles` and `_required_driver_info_roles` carry two role-order dicts (`928`, `959`); `declared_driver_sensitivities` and `declared_effective_driver_sensitivities` are the same loop twice (`723-748`, `751-805`) | `load_design_draft` returns four near-identical "unreadable" envelopes (`1361-1450`) | comment `866-867` cites a test that does not exist; comment `536` cites `docs/active-crossover-information-design.md` "Slice 0" |
| `jasper/active_speaker/driver_safety.py` | 2672 | 4 (`_validate_driver_safety_profile_shape` 330, `validate_driver_research_request` 237, `_profile_core` 217, `evaluate_driver_safety_profile` 178) | 25 | `result_fingerprint` is computed and stored but never compared (`1381-1390`, `2126-2131`); `DriverSafetyProfileEvaluation.authorizes_playback` constant | five per-driver allow-lists (section 1a); `_canonical_json`/`_fingerprint` `132-137` vs `output_topology.canonical_fingerprint`; legacy re-stamp machinery `1039-1045`, `1139-1148`, `1174-1195` for two retired keys | `_reject_bool_tree` at seven sites; `_validate_driver_safety_profile_shape` re-derives what `build_driver_safety_profile` just wrote and `build_driver_safety_profile` then re-evaluates its own artefact (`2043-2049`) | the "confirmation" block is a copy of the profile fingerprint with a constant `method` (`2015-2035`) |
| `jasper/active_speaker/driver_safety_prompt.py` | 292 | 1 (`build_driver_research_prompt` 155) | 3 | 0 | 0 | 0 | prompt text is ~120 lines of the file |
| `jasper/active_speaker/crossover_preview.py` | 873 | 2 (`_build_crossover` 230, `build_crossover_preview` 192) | 8 | 0 | two inline sha256 hashes (`84-95`, `98-123`); `_merged_design_inputs` re-merges research and manual by role (`183-209`) after `design_draft._summary` already resolved targets | 0 | `permissions`/`safety` constant blocks `667-685` |
| `jasper/active_speaker/staging.py` | 2263 | 4 (`prepare_driver_commissioning_config` 379, `_preset_from_crossover_preview` 265, `_stage_protected_startup_config_locked` 258, `_bind_preset_to_topology` 256) | 17 | `_way_count_for_mode`/`_active_mode_for_way` re-spell the mode table (`329-338`) | `_anchor_lock_contended_payload` `1496-1563` is a 68-line copy of the staged payload shape; gates/issues are emitted in pairs everywhere (`_gate` + `_issue` for the same fact, e.g. `1119-1140`) | 0 | comment blocks at `1994-2006`, `2105-2135`, `2229-2240` narrate PR history at length |
| `jasper/active_speaker/baseline_profile.py` | 1793 | 2 (`_measured_level_trims` 220, `_bank_applied_base_trim` 213) | 21 | `PROVENANCE_PRESERVED` `143` unused; `revalidation` field always `not_required` (`1674`) | the config sha256 is computed at three sites (`174`, `212`, `318`); `_load_saved_state`/`_frozen_applied_profile` vs `applied_identity.py` | 0 | 25% of the file is comment |
| `jasper/active_speaker/runtime_contract.py` | 4903 | 4 (`_active_graph_evidence` 782, `safe_graph_for_current_topology` 348, `_flat_graph_allowed` 171, `classify_camilla_graph` 152) | 19 | 0 top-level | `_subwoofer_groups` `497` = `output_topology.subwoofer_speaker_groups`; `_staged_path` `3272` = `startup_load._staged_config_path`; `_staged_matches_topology` `3280` = `startup_load._staged_topology_payload`; `_required_output_width` `3386` vs `playback_route._highest_assigned_output`; two running-graph hashes (`3738` vs `commissioning_admission.py:284`) | `classify_bass_extension_graph` `3843-3879` refuses seven caller-argument shapes for five in-repo callers | 60% comment; 12 unmarked function-local imports |
| `jasper/active_speaker/web_commissioning.py` | 1310 | 0 | 4 | **~770 lines**: everything from `_commission_tone_target_key` `548` to the end except `attempt_graph_restore`, `request_missing_software_guards`, `ensure_missing_software_guards`, `regenerate_crossover_preview_from_current_draft`, `commission_status_payload`, `_stage_startup_config` and `_ensure_commission_startup_anchor`; `start_summed_test` `1182-1310` and `play_summed_capture_sweep` `1123-1157` have no caller; `COMMISSION_TONE_RESTART_MARGIN_S`, `COMMISSION_TONE_STARTUP_CHECK_S` `95-96` unused | issue factories `311-359` exist "for the AST copy guard in tests/test_sound_setup.py" | 0 | the module docstring describes a service two surfaces share; one surface is gone |
| `jasper/active_speaker/reset.py` | 237 | 0 | 3 | 0 | `_design_draft_state_path` `55-58` = `design_draft._design_draft_path` `102-106` | `_staged_anchor_unlink_guard` `129-149` takes a cross-process lock for one of nine unlinks | 60% comment |
| `jasper/active_speaker/_common.py` | 168 | 0 | 4 | `region_key`, `gate`, `bounded_int` are used elsewhere (kept) | 0 | 0 | — |
| `jasper/output_topology.py` | 2459 | 1 (`evaluate_output_topology` 193) | 9 | `_legacy_topology_config_fingerprint` `794-807` with its own removal note | `subwoofer_speaker_groups` duplicated in `playback_route` and `runtime_contract` | `_load_failures` transition log `2384-2420` for a corrupt file | dual-Apple composite machinery (`174-235`, `872-928`, `1339-1500`, `1690-2015`) is roughly a quarter of the file and irrelevant to a single-DAC box |
| `jasper/audio_hardware/reconcile.py` | 1899 | 1 (`execute` 152) | 12 (`297-300`, `348-349`, `1679`, `993-1001`, `1742-1743`, `1484-1485`, …) | `JASPER_OUTPUTD_CONTENT_PCM` heal block `993-1002` whose removal condition is already met (no reader anywhere) | three restart wrappers `1548`/`1574`/`1630`; the env stage→apply→commit round runs twice (`1711-1739`, `1771-1785`); composite `4` literal at `939`/`1051` | `if not dac_id` at `723`, `756` | 48% comment |
| `jasper/audio_hardware/dac.py` | 1110 | 0 (143 max) | 15 | `ChannelMapEntry` + `dac_channel_map` (`130-148`, `192`, `344-376`), `supports_active_crossover_commissioning` (`191`, `337-343`), `validation_profile` (`194`) — no product reader; `DAC8X_OUTPUTD_STABILITY_PROFILE` duplicated in `audio_validation.py:77` | — | `if child is None` `1085` after `_build_index` refused it | 45% comment; prose at `549-555` predates #5264 |
| `deploy/assets/sound-profile/js/main.js` | 4247 | 0 (longest: `saveDriverResearchDraft` 119, `renderOutputGroupsCard` 88) | 12 | `baselineProfileNeedsRevalidation`/`baselineProfileRevalidation` `1275-1282` and the "recheck" copy `2163-2166` (server always `not_required`); `baseline_subwoofer_not_supported` copy `2107-2109` (no producer); `compiled_apply_blocked` status `2099` (no producer); `identityReportFromTopology` `777-808` (server always sends `channel_identity`); `swapPeer` `3358-3372` (see 1e) | `outputTemplateAxesForTopology` vs `outputTemplateKindFromAxes`; five copy-to-clipboard fallbacks `3564-3673` | `selected !== null && !isFinite(selected)` `3346` on a `<select>` value | 62% of the file is setup flow (section 9) |
| `deploy/assets/sound-profile/js/driver-model.js` | 1126 | 0 | 33 | `hfDriverStyleEntry`/`driverStyleLabel` used; `driverSafetyNoteRoles` used once for a sentence | client copies of server validation (section 2) | 0 | 18% comment, mostly history |
| `deploy/assets/sound-profile/js/driver-fields.js` | 752 | 0 | 10 | 0 | `renderCrossoverPreviewRows` vs `renderWorkingCrossoverRows` (`690-752`) | 0 | — |
| `deploy/assets/sound-profile/js/topology.js` | 402 | 0 | 1 | exports with no importer: `activeCommissionRoles` `17-25`, `activeOutputGroups` `240-244`, `outputChannelGuardReady` `233-238` | `physicalTargetId` (server twin) | 0 | — |
| `deploy/assets/sound-profile/js/active-speaker-ui.js` | 440 | 0 | 4 | exports with no importer: `commissionGateReason` `354-370`, `SENSITIVITY_TRIM_EPS_DB`, `MAX_DRIVER_ATTENUATION_DB`, `NEARFIELD_LEVEL_MATCH_GUIDANCE` `396-398`, `localSubwooferGroup`, `subwooferCrossoverBand` (tests only) | `SUB_CROSSOVER_HZ_*` (server twin) | 0 | `commissionIssueReason` `263-349` maps ~20 codes to sentences for the per-driver tone lane; on this page only `commission-ramp-abort` and `restore` reach `commissionPayloadFailure` (`postCommission` callers `main.js:4082`, `4175`) |
| `deploy/assets/sound-profile/js/state.js` | 170 | 0 | 3 | 0 | 0 | 0 | — |
| `deploy/assets/sound-profile/js/installation.js` | 49 | 0 | 0 | 0 | 0 | 0 | clean |
| `deploy/assets/shared/js/http.js` | 273 | 0 | 2 | 0 | 0 | 0 | clean; shared |

**Tests that assert on prose** (from the test sub-agent, spot-checked):
`tests/test_active_speaker_driver_safety.py` 93 sites (28 error sentences via
`match=`, 22 issue messages, 43 prompt substrings) plus two that read
`main.js` and `driver_safety.py` as text (`1812-1827`, `2551-2597`);
`tests/test_active_speaker_design_draft.py` 19; `tests/test_sound_setup.py`
34 plus **18 tests that read `main.js`, `active-speaker-ui.js`, `sound.css`,
a systemd unit or the nginx conf as strings** (`1197`, `1208`, `1330`,
`1382` (79 `not in js` tombstones), `1709`, `1766`, `1836`, `2913`, `4935`,
`5022`, `5057`, `5070`, `5087`, `5111`, `5124`, `5136`, `5176`, `5189`);
`tests/test_output_topology.py` 10; `tests/test_active_speaker_staging.py` 5;
`tests/js/sound_profile_harness.mjs` 132 prose checks in 53 of 92 scenarios;
`tests/js/active_speaker_ui_test.mjs` 34. The refusal gates of the research
loop can only be pinned by prose because `DriverSafetyProfileError` and
`ActiveSpeakerDesignDraftError` carry no code. 28 behaviours are pinned at
two or more altitudes (module, HTTP handler, harness); the reconcile
verdict sentences are pinned eight times.

## 9. Size

| File | Lines | Of which comment/docstring |
|---|---|---|
| `jasper/web/sound_active_speaker.py` | 1,800 | 13% |
| `jasper/web/sound_setup.py` | 1,090 | 71% (page HTML counted as prose) |
| `jasper/active_speaker/design_draft.py` | 1,548 | 10% |
| `jasper/active_speaker/driver_safety.py` | 2,672 | 12% |
| `jasper/active_speaker/driver_safety_prompt.py` | 292 | 26% |
| `jasper/active_speaker/crossover_preview.py` | 873 | 9% |
| `jasper/active_speaker/staging.py` | 2,263 | 14% |
| `jasper/active_speaker/baseline_profile.py` | 1,793 | 25% |
| `jasper/active_speaker/runtime_contract.py` | 4,903 | 60% |
| `jasper/active_speaker/web_commissioning.py` | 1,310 | 9% |
| `jasper/active_speaker/reset.py` | 237 | 60% |
| `jasper/active_speaker/_common.py` | 168 | 26% |
| `jasper/output_topology.py` | 2,459 | 17% |
| `jasper/audio_hardware/reconcile.py` | 1,899 | 48% |
| `jasper/audio_hardware/dac.py` | 1,110 | 45% |
| **Server total** | **24,417** | |
| `deploy/assets/sound-profile/js/main.js` | 4,247 | 6% |
| `deploy/assets/sound-profile/js/driver-model.js` | 1,126 | 18% |
| `deploy/assets/sound-profile/js/driver-fields.js` | 752 | 11% |
| `deploy/assets/sound-profile/js/topology.js` | 402 | 4% |
| `deploy/assets/sound-profile/js/state.js` | 170 | 24% |
| `deploy/assets/sound-profile/js/active-speaker-ui.js` | 440 | 18% |
| `deploy/assets/sound-profile/js/installation.js` | 49 | 4% |
| `deploy/assets/shared/js/http.js` | 273 | 34% |
| **Client total** | **7,459** | |
| `tests/test_sound_setup.py` | 7,547 | 156 tests |
| `tests/js/sound_profile_harness.mjs` | 7,014 | 92 scenarios |
| `tests/test_active_speaker_driver_safety.py` | 4,015 | 77 tests |
| `tests/test_active_speaker_runtime_contract.py` | 5,421 | 174 tests |
| `tests/test_active_speaker_design_draft.py` | 1,417 | 60 tests |
| `tests/test_active_speaker_staging.py` | 1,490 | 41 tests |
| `tests/test_output_topology.py` | 2,047 | 71 tests |
| other in-scope test files (11) | 7,015 | |
| **Tests total** | **35,966** | |

**`main.js` by concern** (functions classified by name and body; the event
block split by line):

| Concern | Lines | Share |
|---|---|---|
| Speaker setup flow (topology, drivers, research, preview, identity, baseline, commissioning view, reset/repin, handoff) | 2,639 | 62% |
| EQ editor (`/sound/eq/`: bands, curves, live draft, profile library) | 777 | 18% |
| Output page settings (`/sound/output/`: volume floor tone, headroom, loudness, I2S HAT) | 361 | 9% |
| Shared (imports, `render`, `status`, event wiring, boot) | 470 | 11% |

The setup flow alone in the client is 2,639 + 1,126 + 752 + 402 + 440 + 49
≈ **5,400 lines** for four steps and eleven endpoints.

## 10. Smallest simplification that removes the most

Ranked by lines removed per line added, with the reboot park fixed first
because it is the one defect the owner is living with. Each cut is one PR.
"Replaces with" names the single thing that takes over; where it says
"nothing", the behaviour is already provided elsewhere. Line counts are
estimates from the ranges cited in sections 1, 4 and 8, product code only
(tests come off on top). No cut adds an abstraction that does not replace
at least two existing ones.

| Rank | Cut | Delete (file / function) | Replaces with | Gates removed | Lines (approx.) |
|---|---|---|---|---|---|
| 1 | **Apply is the proof; delete the identity ceremony** (section 7, option 2) | the `identity_confirmed` conjunct at `runtime_contract.py:4507` and `4547`; `roleful_identity_confirmed` `604-626`; the un-confirm park `sound_active_speaker.py:706-741` and the rest of `_active_speaker_channel_identity_save_payload` `681-777`; `_active_speaker_channel_identity_payload` `671-679` and the `channel-identity` GET/POST routes; `set_channel_identity_verified` `output_topology.py:1666`, `_with_server_owned_identity` `2236`, the `identity_verified` field and the `verified` topology status (`931-1123`); `updateOutputChannelIdentity` `main.js:4064-4115`, the Confirm/Change buttons and `commission-ramp-abort` call `4082`; `identityReportFromTopology` `777-808`; the pin at `tests/test_active_speaker_runtime_contract.py:5212-5235` | `select_active_baseline` (`runtime_contract.py:4544-4557`) fires on an applied, re-proved baseline with no identity conjunct; the composite re-pin route keeps `stay_parked` (`sound_active_speaker.py:596-601`) and the operator re-arms by pressing Apply. If the owner wants the smaller change instead, `persist_applied_baseline_profile` (`baseline_profile.py:1658-1680`) sets every lane's `identity_verified` (section 7, option 1: ~10 lines, removes nothing) | S8, C12 | ~450 |
| 2 | **Delete the dead summed-test and commission-tone lane** (section 8) | `sound_active_speaker.py:1234-1469` (session globals, `SUMMED_TEST_*`, `_summed_test_session_active`, `_active_summed_test_snapshot`, `_attach_active_summed_test`, `_stop_summed_test_tone_locked`, `_active_speaker_stop_summed_test_tone`, `_stop_commission_tone_locked`, `_active_speaker_stop_commission_tone`); `web_commissioning.py:548-1310` except the seven live functions named in section 8; `_SUMMED_TEST_ARM_REPORT` and `start_summed_test`; their tests | nothing (ADR-0230 moved commissioning tones to the crossover page; `git c958a173f` deleted the routes) | — | ~1,000 |
| 3 | **Delete the routes the page never calls** (section 5) | `GET /active-speaker/environment`, `/safe-playback`, `/calibration-level` (+ POST), `/bringup-preflight`, `/startup-load`, `/staged-config`, `POST /active-speaker/baseline-profile` (compile) and `/apply`, with their builders in `sound_active_speaker.py` (`780-981`, `1652-1704`) and rows in `sound_setup.py` (`1018-1027`, `1041`, `1046-1047`, and the restated `_GET_JSON_ROUTES` `335-389`); their tests | nothing; a diagnostic the owner wants back is one `jasper-doctor` line | — | ~600 |
| 4 | **Cut the research-request coupling** (section 6) | `build_driver_research_request`'s context assembly `driver_safety.py:885-976`, `validate_driver_research_request` `978-1215`, `_without_declared_context` `1217-1225`, the fingerprint half of `validate_research_result_binding` `1227-1291`; `_validate_v2_research_prefill` `design_draft.py:888-923`, `_rebound_to_restamped_request` `1092-1125`, `_demote_legacy_driver_research_binding` `1305-1339`, `expected_revision` `1487-1515`; the `driver-research-request` POST (`sound_active_speaker.py:1017-1063`, becomes a GET returning the prompt text); `driverResearch.researchRequest` and `promptCopy` (`state.js`), `invalidateDriverResearchBinding` `driver-model.js:371-375`, the reply drop `main.js:3767-3772`, the 409 handling `3834-3846` | the reply binds by its echoed `target_id` and model (S17's surviving half) | S11, S16, S19, S20, S17 (half), C6 | ~750 |
| 5 | **Delete the revision and hardware-identity echo tokens; skip the park on a no-op save** | `topology_revision` CAS `sound_active_speaker.py:315-322`; `_verified_detected_hardware` `404-427` and `_reset_request_hardware` `430-441`; the tokens in the save, reset and repin payloads and the 409 handlers `main.js:3893-3899`, `3945-3951`; in the same PR, `park_and_commit_topology` returns early when `topology_config_fingerprint` is unchanged | the post-park re-read the routes already do (`471-475`, `581-586`) | S1, S2, S9 (half) | ~150 |
| 6 | **One driver vocabulary** (section 1a) | the 17 non-authoritative holders in table 1a; 24 of the 25 `_reject_unknown_keys` sites; the duplicate `_text` / `_finite_float` / `_positive_float` / `_sequence` / `_reject_unknown_keys` in `driver_safety.py:273-366`; `_CANDIDATE_FIELDS` `design_draft.py:64-77`; `driver-fields.js` field tables and the client vocabulary validators | `MANUAL_DRIVER_FIELDS` (`_common.py:60`) plus one JSON copy of it in the page payload | S12 (24 of 25), C8 | ~400 |
| 7 | **Stop storing the safety profile; compute it** (section 6) | `_validate_driver_safety_profile_shape` `driver_safety.py:2054-2384`, `_require_canonical_text_field` `2032`, `_superseded_typed_highpass` `2386`, `_retired_fields_present` `2450`, `_stale_low_limit_rebuild_issues` `2465`, the `stale` / `malformed` branches of `evaluate_driver_safety_profile` `2495-2672`, the confirmation block and `result_fingerprint` in `build_driver_safety_profile` `1979-2030`; the `driver_safety_profile` key in the draft file; the `driver_safety_profile_not_confirmed` refusal `measurement_emit.py:76-82` narrowed to "tweeter floor declared"; `level_duration_limits` / `measurement_band` / `hard_excitation_band` move to measurement admission | `_profile_core` (`1705-1922`) + `_target_issues` (`1594-1649`) as one pure function of (draft, topology), evaluated on read | S21, S22 | ~700 |
| 8 | **Stop persisting the crossover preview** | `save_crossover_preview` `crossover_preview.py:835`, `load_crossover_preview` `759`, `_validate_preview_freshness` / `_stale_preview` `698-756`, `crossover_design_fingerprint` / `crossover_preview_fingerprint` `84-124`, the preview file and its `reset.py` unlink, the POST `crossover-preview` route (`sound_active_speaker.py:1203-1231`), `regenerate_crossover_preview_from_current_draft` (`web_commissioning.py`), the client's preview nulling `main.js:3317`, `1755-1757`; downgrade S24 to a warning | `build_crossover_preview(draft)` on GET, which is what `measurement_emit.py:83` already does at Apply | S24, S25 | ~350 |
| 9 | **Render server facts; drop client re-derivations and dead client state** (sections 2, 8) | `swapPeer` `main.js:3358-3372`; the dead revalidation copy `1275-1282`, `2163-2166`; `compiled_apply_blocked` `2099`; `baseline_subwoofer_not_supported` `2107-2109`; the `may_apply` double check `4120-4142` and the save-and-apply confirm `4132-4137`; the dead confirm `3426-3427` and `3178`; `setOutputDraft`'s research-dirty side effect `3315-3317`; `driverResearchTargets`, step-state and readiness re-derivations in `driver-model.js` (`361-369`, `552`); the no-importer exports in `topology.js` (`17-25`, `233-244`) and `active-speaker-ui.js` (`354-370`, `396-398`); `outputTemplateUnavailableReason` `topology.js:308-333` | the fields the server already sends (`channel_identity`, `research_targets`, `commissioning_view.steps`, `permissions`) | C1, C2, C3, C4, C7, C9, C13, C14 | ~500 |
| 10 | **Collapse tweeter-protection bookkeeping** | the `required_missing` / `software_guard_requested` transitions (`output_topology.py:505`, `1045-1060`, `1220`), `request_missing_software_guards` / `ensure_missing_software_guards` (`web_commissioning.py:184-217`) and the three call sites (`sound_active_speaker.py:334`, `1102`, `1212`), the `tweeter_software_guard_requested` skip in `staging.py:1044` | `protection_status` in {`present`, `absent`} set from the declaration; staging emits the software guard whenever it is `absent` (what it does today after the flag flip) | S7 | ~120 |
| 11 | **Delete the verified dead server code** (section 8) | `_MAX_SOURCES` `design_draft.py:67`; `PROVENANCE_PRESERVED` `baseline_profile.py:143` and the constant `revalidation` field `1674` with its client copy; `_legacy_topology_config_fingerprint` `output_topology.py:794-807`; the `JASPER_OUTPUTD_CONTENT_PCM` heal block `reconcile.py:993-1002`; `ChannelMapEntry` / `dac_channel_map` / `supports_active_crossover_commissioning` / `validation_profile` in `dac.py` (`130-148`, `191-194`, `337-376`) and the duplicated `DAC8X_OUTPUTD_STABILITY_PROFILE`; `_way_count_for_mode` / `_active_mode_for_way` `staging.py:329-338`; `_anchor_lock_contended_payload` `1496-1563`; `rear_must_start_muted` and the `startup_muted` field (S6); `_i2s_hat_collision_warnings` (S10); the role-order gate (S27); the second `reviewed_candidate_refusal` (S28, both copies) | nothing | S6, S10, S27, S28 | ~350 |
| 12 | **One fingerprint helper** (section 1b) | the four sha256 helpers in table 1b and the three config-sha sites `baseline_profile.py:174`, `212`, `318` | `driver_safety._fingerprint` (`132-138`) | — | ~60 |
| 13 | **Fix the cardioid axes rendering** | the `active_3way` branch at `main.js:1414` | the server's `active_2_way` + `rear` (`topology.js:262`, `output_topology.py:944-951`); one behaviour pin in the harness | — | ~5 |
| 14 | **Tests** (section 8) | the 18 source-text tests in `tests/test_sound_setup.py` and the two in `tests/test_active_speaker_driver_safety.py` (`1812-1827`, `2551-2597`); the clusters that pin cuts 4, 5, 7 and 8; the 28 duplicate-altitude groups down to one altitude each | one pin per behaviour: after Apply, `safe_graph_for_current_topology` selects the applied baseline with no identity bits (cut 1); save-and-apply with `may_apply` false is refused at HTTP; reset stops audio before it unlinks | — | ~2,000 (test lines) |

Totals: roughly 5,400 product lines and 2,000 test lines removed; 28 of the
50 graded gates go (every REMOVABLE row in section 4 is covered by a cut
above); every KEEP row stays. Cuts 1–3 need no design decision. Cuts 4, 7
and 8 change what the page shows and are the ones to read section 6 before
starting.

What this list deliberately does not propose: a new state machine, a new
"setup session" object, or a client-side store. The page becomes a renderer
of six server documents (topology, design draft, computed safety issues,
computed preview, commissioning view, baseline profile) with five POSTs
(save layout, reset, save values, apply, restore), and that is the entire
contract a rewriter needs (section 5).

## Appendix — issues filed

One `audit` + `audit-2026-09-17` issue per finding not fixed inline; the
tracking issue lists them. Numbers are filled in below at filing time.

ISSUES_PLACEHOLDER
