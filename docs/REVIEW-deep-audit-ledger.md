# Deep Audit — live findings ledger

> **This is the live tracker.** Immutable evidence + full text for each id lives in
> [REVIEW-deep-audit-2026-07-11.md](REVIEW-deep-audit-2026-07-11.md); this file tracks current
> disposition against `main`. Update the Status/PR columns as work lands. `DA-NNNN` ids are stable.

Last reconciled against `main` (`dfdd498c`): 2026-07-12.

## Status counts

- **open**: 382
- **fixed**: 282
- **in-progress**: 0
- **mooted**: 12
- **deferred**: 1

## Ledger

| id | file :: anchor | sev | wave | status | disposition / owning PR |
|---|---|---|---|---|---|
| DA-0006 | `deploy/bin/jasper-wifi-guardian` :: nmcli stderr capture and cleanup; guardian unit `PrivateTmp` | should-fix | W5 | **fixed** | private temp capture + `PrivateTmp=true` — #1236 |
| DA-0010 | `jasper/active_speaker/commission_ramp.py` :: _record_ramp_state:198 | should-fix | W5 | **fixed** | ramp-state temp file now mode 0640 — #1236 |
| DA-0015 | `jasper/correction/autolevel.py` :: AutolevelController.run._graceful_stop | should-fix | W5 | **fixed** | graceful-stop volume-set failures logged + pinned — #1240 |
| DA-0016 | `jasper/cues/manager.py` :: AudioCueManager.regenerate :186 | should-fix | W5 | **fixed** | per-cue regeneration failure isolation + regression — #1240 |
| DA-0017 | `jasper/measurement/volume_guard.py` :: _set_snapcast_snapshot | should-fix | W5 | **fixed** | best-effort restore attempts every client — #1239 |
| DA-0019 | `jasper/multiroom/snapcast_rpc.py` :: _ProbeCache.read | should-fix | W5 | **fixed** | blocking probe moved outside cache lock + threaded test — #1239 |
| DA-0020 | `jasper/peering/daemon.py` :: ARBITRATE_RPC_TIMEOUT_SEC | should-fix | W5 | **fixed** | RPC timeout derived above arbitration-window ceiling — #1238 |
| DA-0021 | `jasper/peering/state.py` :: _on_peer_claim | should-fix | W5 | **fixed** | unrelated foreign claims no longer clobber ACTIVE — #1238 |
| DA-0024 | `jasper/voice/openai_session.py` :: OpenAIRealtimeConnection._dispatch_function_call | should-fix | W5 | **fixed** | tool-result serialization/send failures contained + pinned — #1240 |
| DA-0027 | `jasper/web/_common.py` :: read_form | should-fix | W5 | **fixed** | bounded body + malformed Content-Length guard — #1236 |
| DA-0029 | `jasper/web/google_setup.py` :: _handle_start / _exchange_code / _build_flow | should-fix | W5 | **fixed** | one-time expiring OAuth nonce binds account + PKCE state — #1236 |
| DA-0030 | `jasper/web/home_assistant_setup.py` :: do_POST (`/discover`, `/ready`, `/verify`) | should-fix | W5 | **fixed** | read guards added to all three POST probes — #1236 |
| DA-0034 | `jasper/web/sync_flow.py` :: handle_play | should-fix | W5 | **fixed** | post-spawn race revalidation + loser termination — #1239 |
| DA-0035 | `jasper/web/transit_setup.py` :: _apply_save / _bus_card_html / _citibike_card_html | should-fix | W5 | **fixed** | BusTime key scrubbed from three broad-except log/UI surfaces — #1236 |
| DA-0036 | `jasper/web/weather_setup.py` :: _seed_transit_from_weather_if_missing | should-fix | W5 | **fixed** | weather/transit writers serialized under shared flock — #1239 |
| DA-0037 | `jasper/web/wifi_setup.py` :: _readable_nmcli_error used by connect_new and do_POST | should-fix | W5 | **fixed** | echoed PSK scrubbed before logging/browser response — #1236 |
| DA-0039 | `jasper/xvf/xvf_host.py` :: DEFAULT_TIMEOUT_MS | should-fix | W5 | **fixed** | control timeout bounded below systemd start ceiling — #1240 |
| DA-0040 | `rust/jasper-fanin/src/config.rs` :: env_u32 / Config::from_env sample_rate, period_frames | should-fix | W5 | **fixed** | zero load-bearing dimensions fall back safely + warn — #1237 |
| DA-0041 | `rust/jasper-fanin/src/host_compliance.rs` :: RevalidationTracker::step | should-fix | W5 | **fixed** | fresh-lock phantom strike re-arm removed + regression — #1237 |
| DA-0003 | `jasper/route_latency/pairing.py` :: pair_events | should-fix | W5 | **fixed** | ambiguous mic candidates cannot re-enter certifiable matches — #1292 |
| DA-0004 | `LICENSE` :: §6 Trademarks / §9 Accepting Warranty or Additional Liability | should-fix | W5 | **fixed** | canonical Apache-2.0 terms restored + packaged-license integrity guard — #1293 |
| DA-0005 | `capture-page/js/main.js` :: boot():604-616 | should-fix | W5 | **open** | — |
| DA-0007 | `deploy/systemd/jasper-usbsink.service` :: Slice=jts-audio.slice membership | should-fix | W5 | **fixed** | doctor derives and checks every protected audio/mic unit — #1287 |
| DA-0008 | `firmware/satellite-amoled/src/main.cpp` :: tryConnectStored | should-fix | W5 | **open** | — |
| DA-0009 | `firmware/satellite-amoled/src/main.cpp` :: tryConnectStored / improvConnect | should-fix | W5 | **open** | — |
| DA-0011 | `jasper/audio_runtime_plan.py` :: _route_policy_errors | should-fix | W5 | **fixed** | coherent shm-ring plans preserve computed capture/playback mismatches — #1286 |
| DA-0012 | `jasper/cli/aec_bridge.py` :: _aec_loop dtln runtime-crash handler (~2147- | should-fix | W5 | **fixed** | runtime DTLN failure now withdraws the leg from live health/capture-plan truth while primary AEC3 continues — #1295 |
| DA-0013 | `jasper/cli/xvf_firmware_update.py` :: update | should-fix | W5 | **fixed** | bounded download + pre-flash budget + recovery/unit deadline contract — #1285 |
| DA-0014 | `jasper/control/server.py` :: _make_handler / class Handler | should-fix | W5 | **open** | — |
| DA-0018 | `jasper/multiroom/reconcile.py` :: _ensure_unit_active:1226-1274 | should-fix | W5 | **fixed** | shared reset-failed helper + start OSError containment; recovery probes now fail closed — #1299 |
| DA-0023 | `jasper/voice/gemini_session.py` :: _open_session / _receive_loop | should-fix | W5 | **fixed** | obsolete object-identity INFO probes removed; concise lifecycle timing retained — #1302 |
| DA-0025 | `jasper/wake_corpus/bridge_session.py` :: voice_daemon_active:2107 | should-fix | W5 | **fixed** | bounded strict unit-state probe fails closed before corpus-mode mutation — #1304 |
| DA-0026 | `jasper/wake_events.py` :: _STAGE_TO_COLUMN | should-fix | W5 | **open** | — |
| DA-0028 | `jasper/web/bluetooth_setup.py` :: _start_pair_stream / _drive | should-fix | W5 | **fixed** | unexpected pair-driver failures now emit one structured error with traceback — #1301 |
| DA-0031 | `jasper/web/rooms_setup.py` :: _self_addresses / _lan_target / _post_groupi | should-fix | W5 | **fixed** | package-shared rooms control helpers now expose a tested public boundary — #1309 |
| DA-0032 | `jasper/web/sound_setup.py` :: :177 | should-fix | W5 | **fixed** | all 92 structured sound events use canonical rendering with fidelity guards — #1310 |
| DA-0033 | `jasper/web/sound_setup.py` :: Handler.do_POST | should-fix | W5 | **fixed** | POST dispatch failures contained without secondary responses; invalid lengths rejected before reads — #1312 |
| DA-0038 | `jasper/web/wifi_setup.py` :: do_POST | should-fix | W5 | **fixed** | mutation outcomes observable; malformed bodies rejected and secret-bearing failures scrubbed — #1317 |
| DA-0042 | `scripts/_extract_wake_corpus.py` :: main --force deletion boundary | should-fix | W5 | **fixed** | positive ownership proof + lexical/resolved/filesystem-identity guards — #1291 |
| DA-0043 | `scripts/doc-freshness.sh` :: :28-37, epoch_days_ago | should-fix | W5 | **fixed** | help is parsed before the optional threshold and prints the complete doc block — #1306 |
| DA-0044 | `scripts/multiroom-spike.sh` :: usage() :528-532 | should-fix | W5 | **fixed** | help/no-action paths print the complete safety and operation block — #1306 |
| DA-0045 | `scripts/s0-sync-bench.sh` :: :660,:674 | should-fix | W5 | **fixed** | help/no-action paths share complete portable usage extraction — #1306 |
| DA-0046 | `scripts/s0-sync-measure.py` :: run_soak:304 | should-fix | W5 | **fixed** | drained or unavailable buffer telemetry now fails clock lock without crashing — #1307 |
| DA-0047 | `capture-page/js/level-events.js` :: LevelStreamer | should-fix | W1 | **fixed** | level-ramp protocol wired into the shipped capture page — #1202 |
| DA-0048 | `deploy/assets/sound-profile/js/main.js` :: fmtDbfs :193, toneSummary :1600, outputStart | should-fix | W1 | **fixed** | declaration-only commissioning helpers and permanently inert client state removed — #1313 |
| DA-0049 | `deploy/install.sh` :: camilla_config_has_safe_volume_limit | should-fix | W1 | **fixed** | unreachable installer guard removed; checked-in Camilla YAML safety pin made fail-closed — #1314 |
| DA-0050 | `jasper/accessories/registry.py` :: Device / KNOWN_DEVICES | should-fix | W1 | **fixed** | obsolete migration aliases removed; plural RemoteProfile identity remains sole registry boundary — #1316 |
| DA-0051 | `jasper/accessories/registry.py` :: RemoteProfile.capabilities / CAP_* | should-fix | W1 | **open** | — |
| DA-0052 | `jasper/active_speaker/readiness.py` :: build_playback_readiness | should-fix | W1 | **fixed** | superseded playback-readiness module removed; commission ramp remains live owner — #1318 |
| DA-0053 | `jasper/active_speaker/runtime_contract.py` :: running_graph_violations | should-fix | W1 | **fixed** | unused lossy graph projection removed; structured graph classification retained — #1315 |
| DA-0054 | `jasper/active_speaker/tone_plan.py` :: build_safe_tone_plan | should-fix | W1 | **fixed** | unreachable preset-era tone planner removed; live protected planners retained — #1321 |
| DA-0055 | `jasper/active_speaker/topology_tone.py` :: build_topology_tone_plan | should-fix | W1 | **fixed** | unreachable per-driver planner removed; summed-crossover planner remains live owner — #1318 |
| DA-0056 | `jasper/audio_measurement/quality.py` :: :32-39 | should-fix | W1 | **fixed** | unused threshold compatibility aliases removed; QualityModel remains sole threshold boundary — #1319 |
| DA-0057 | `jasper/bluetooth/adapter.py` :: start_discovery | should-fix | W1 | **fixed** | ephemeral-client discovery helpers removed; long-lived BluetoothEngine owns discovery — #1320 |
| DA-0058 | `jasper/capture_relay/spec.py` :: build_level_ramp_spec | should-fix | W1 | **fixed** | correction flow now builds and runs the relay level ramp — #1202 |
| DA-0059 | `jasper/control/aec_endpoints.py` :: _chip_aec_available / _mic_status | should-fix | W1 | **fixed** | dead wrappers removed; live capability reuses one probe and validated registry plan — #1323 |
| DA-0060 | `jasper/correction/target.py` :: HOUSE_CURVE_PRESETS | should-fix | W1 | **fixed** | unreachable duplicate preset table removed; strategy registry owns profile policy — #1328 |
| DA-0061 | `jasper/mics/xvf3800.py` :: ensure_capture_open | should-fix | W1 | **fixed** | unreachable Python mutator removed; Bash reconciler remains sole runtime writer — #1322 |
| DA-0062 | `jasper/sound/camilla_yaml.py` :: validate_camilla_config | should-fix | W1 | **fixed** | unused boolean validator shim removed; dsp_apply retains typed validation ownership — #1326 |
| DA-0063 | `jasper/sound/runtime.py` :: stage_lean_capture_config | should-fix | W1 | **mooted** | lean runtime pipeline deleted on main — #1200 |
| DA-0064 | `jasper/volume_coordinator.py` :: VolumeCoordinator.__init__ :202-204,243-245; | should-fix | W1 | **fixed** | never-populated observer-task state removed; VolumeObserver retains task ownership — #1324 |
| DA-0065 | `jasper/volume_coordinator.py` :: _busctl_call_method | should-fix | W1 | **fixed** | dead AirPlay DBus helper removed; subprocess prohibition pinned behaviorally — #1290 |
| DA-0066 | `jasper/volume_persistence.py` :: regress_if_stale | should-fix | W1 | **fixed** | unreachable legacy dB boot regressor removed; listening-level path remains canonical — #1325 |
| DA-0067 | `jasper/wake_corpus/bridge_session.py` :: enable_bridge_outputs_for_session:1173 | should-fix | W1 | **fixed** | additive legacy bridge writer removed; exact-state capture-plan seam retained — #1329 |
| DA-0068 | `jasper/web/sound_setup.py` :: _active_speaker_arm_payload | should-fix | W1 | **fixed** | orphaned arm helper removed; live audible paths arm at their safety owners — #1327 |
| DA-0069 | `multiroom-spike/stats-flac-300ms.json` :: multiroom-spike/ (6 files) | should-fix | W1 | **fixed** | invalid device-bearing spike artifacts deleted and root outputs ignored — #1330 |
| DA-0070 | `rust/jasper-host-clock/src/lib.rs` :: HostClock::raw_demand_ppm | should-fix | W1 | **fixed** | inert private field removed; raw local demand still drives L1/L2 — #1288 |
| DA-0071 | `rust/jasper-outputd/src/core.rs` :: OutputCore::add_reference_consumer / drain_r | should-fix | W1 | **fixed** | retired named reference-consumer API removed; real side outputs unchanged — #1332 |
| DA-0072 | `rust/jasper-outputd/src/reference.rs` :: ReferenceFanout::add_consumer/drain_consumer | should-fix | W1 | **fixed** | unused reference queue registry and statistics removed — #1332 |
| DA-0073 | `scripts/aec-probe-usb-delay.sh` :: module:1-440 | should-fix | W1 | **fixed** | unsafe orphan USB-delay probe deleted; canonical bounded timing tools retained — #1331 |
| DA-0074 | `scripts/airplay-receiver-timing-proof.py` :: main:738 | should-fix | W1 | **fixed** | experiment converted to bounded passive proof with topology/privacy/health gates — #1341 |
| DA-0075 | `tests/js/sound_profile_harness.mjs` :: measurementAudioPreamble / import stripping | should-fix | W1 | **fixed** | stale mic shim removed; known-import stripping narrowed + self-probed — #1289 |
| DA-0152 | `AGENTS.md` :: Profile guardian — self-heal after filesyste | should-fix | W3 | **fixed** | duplicated guardian narrative compressed to operational contract with canonical handoff link — #1335 |
| DA-0153 | `c/jts-ring-ioplug/jts_ring_shm.c` :: jts_ring_writer_open / jts_ring_reader_open | should-fix | W3 | **fixed** | create/attach state machine consolidated with bounded live-creator and reclaim races — #1349 |
| DA-0154 | `deploy/assets/bluetooth/bluetooth.css` :: .spinner / @keyframes bt-spin:208-227 | should-fix | W3 | **fixed** | shared spinner geometry/motion promoted to canonical app stylesheet — #1337 |
| DA-0155 | `deploy/assets/correction/js/crossover/main.js` :: fetchJSON():44 / postWav():54 | should-fix | W3 | **fixed** | duplicated response parsing migrated to shared getJSON protocol owner — #1338 |
| DA-0156 | `deploy/assets/home-assistant/home-assistant.css` :: .row-version / .discover-list:15,44,50,57-58 | should-fix | W3 | **fixed** | undefined Home Assistant token fallbacks replaced with canonical tokens/literals — #1336 |
| DA-0157 | `deploy/assets/sound-profile/sound.css` :: .toggle block, lines 836-852 | should-fix | W3 | **fixed** | competing sound-page toggle removed in favor of canonical native-checkbox component — #1339 |
| DA-0158 | `deploy/assets/tools/js/main.js` :: load/onToggle/onApply | should-fix | W3 | **fixed** | duplicated tools mutation/restart state machine extracted into one helper — #1340 |
| DA-0159 | `deploy/assets/wake-corpus/js/main.js` :: LEG_LABELS | should-fix | W3 | **fixed** | Python-owned leg labels serialized through escaped island; browser duplicate removed — #1342 |
| DA-0160 | `deploy/bin/jasper-camilla-crossover-guard` :: :95-148 (inline probe + REPAIR block) | should-fix | W3 | **fixed** | duplicated Camilla dead-pipe repair extracted into one staged guard library — #1351 |
| DA-0161 | `deploy/bin/jasper-dac-init` :: detect_apple_cards() / resolve_cards() (:20- | should-fix | W3 | **fixed** | Apple dongle detection and card resolution moved to one sourced library — #1343 |
| DA-0162 | `deploy/install.sh` :: install_deps / install_streambox_deps | should-fix | W3 | **fixed** | shared native renderer dependency set now has one install owner — #1348 |
| DA-0163 | `deploy/lib/install/systemd-units.sh` :: install_systemd_units | should-fix | W3 | **fixed** | full and streambox profiles converge on shared support and transactional staging owners — #1344 |
| DA-0164 | `deploy/nginx-jasper-streambox.conf` :: location /wifi/ | should-fix | W3 | **fixed** | both nginx profiles now cover the complete bounded Wi-Fi rollback window — #1346 |
| DA-0165 | `deploy/systemd/jts-audio.slice` :: :8 | should-fix | W3 | **fixed** | canonical OOM map covers explicit units/drop-ins and exact no-swap slice membership — #1347 |
| DA-0166 | `firmware/dial/src/discovery.cpp` :: discoverControlEndpoint | should-fix | W3 | **fixed** | dial/AMOLED control discovery moved behind one manifest-owned firmware library — #1350 |
| DA-0167 | `jasper/active_speaker/camilla_yaml.py` :: emit_active_speaker_driver_domain_config (du | should-fix | W3 | **fixed** | solo-baseline and driver-domain emitters share one correction safety gate — #1354 |
| DA-0168 | `jasper/active_speaker/crossover_preview.py` :: _ACTIVE_ROLE_PAIRS | should-fix | W3 | **fixed** | one ordered role-pair contract now feeds design gating and preview generation — #1352 |
| DA-0169 | `jasper/active_speaker/design_draft.py` :: _normalise_driver | should-fix | W3 | **open** | — |
| DA-0170 | `jasper/active_speaker/measurement.py` :: _latest_current_summed_records / _latest_cur | should-fix | W3 | **mooted** | crossover pairing made the selectors intentionally divergent — #1345/#1353 |
| DA-0171 | `jasper/active_speaker/runtime_contract.py` :: classify_camilla_graph:1568-1589 | should-fix | W3 | **open** | — |
| DA-0172 | `jasper/active_speaker/safe_playback.py` :: _atomic_write_json | should-fix | W3 | **open** | — |
| DA-0173 | `jasper/active_speaker/staging.py` :: _atomic_write_json:114-126 | should-fix | W3 | **open** | — |
| DA-0174 | `jasper/active_speaker/staging.py` :: driver_commission_audible_evidence:651-683 / | should-fix | W3 | **open** | — |
| DA-0175 | `jasper/active_speaker/staging.py` :: stage_protected_startup_config:1722-1821 / p | should-fix | W3 | **open** | — |
| DA-0176 | `jasper/active_speaker/startup_load.py` :: _atomic_write_json / _base_state / _commissi | should-fix | W3 | **open** | — |
| DA-0177 | `jasper/active_speaker/web_measurement.py` :: capture_preset (line 255) | should-fix | W3 | **open** | — |
| DA-0178 | `jasper/audio_runtime_plan.py` :: _resolve_profile_floor_int / _resolve_output | should-fix | W3 | **open** | — |
| DA-0179 | `jasper/audio_validation.py` :: write_artifact:663 / write_latest_pointer:69 | should-fix | W3 | **fixed** | both diagnostic publishers use one domain serializer and canonical atomic writer — #1360 |
| DA-0180 | `jasper/cli/aec_bridge.py` :: _ref_thread (1095-1214) / _outputd_ref_udp_t | should-fix | W3 | **open** | — |
| DA-0181 | `jasper/cli/aec_tune.py` :: main (--mic-device default) | should-fix | W3 | **fixed** | registry-derived XVF capture identity and WAV-layout validation — #1412 |
| DA-0182 | `jasper/cli/doctor/audio.py` :: _read_status_socket:978 | should-fix | W3 | **open** | — |
| DA-0183 | `jasper/cli/wake_score.py` :: walk_corpus/parse_quadrant vs recording_back | should-fix | W3 | **fixed** | shared condition mapping restores ambient traversal and scoring — #1362 |
| DA-0184 | `jasper/cli/xvf_firmware_update.py` :: _write_state | should-fix | W3 | **fixed** | firmware status uses canonical atomic writer with deterministic 0644 mode — #1363 |
| DA-0185 | `jasper/control/aec_endpoints.py` :: _LEG_DEFAULT_RAW | should-fix | W3 | **fixed** | wake-leg defaults derived from control owner and pinned across reconciler/installer writers — #1364 |
| DA-0186 | `jasper/control/server.py` :: _post_grouping_set | should-fix | W3 | **fixed** | optional grouping scalars parsed through one pure typed request seam — #1365 |
| DA-0187 | `jasper/control/server.py` :: _sync_aec_module / _sync_dial_module / _sync | should-fix | W3 | **fixed** | mutable control mirrors removed; state remains at subsystem owners — #1366 |
| DA-0188 | `jasper/control/shairport_supervisor.py` :: run / start_supervisor / snapshot | should-fix | W3 | **fixed** | three supervisors share bounded runtime mechanics while retaining local policy — #1368 |
| DA-0189 | `jasper/control/volume_ops.py` :: SPOTIFY_OAUTH_CALLBACK_BASE / _spotify_redir | should-fix | W3 | **fixed** | Spotify OAuth module now owns callback base and default redirect construction — #1367 |
| DA-0190 | `jasper/correction/acceptance.py` :: _env_float / _env_int | should-fix | W3 | **open** | — |
| DA-0191 | `jasper/correction/evidence.py` :: _position_summary | should-fix | W3 | **open** | — |
| DA-0192 | `jasper/correction/runtime_integrity.py` :: MEM_AVAILABLE_WARN_MB | should-fix | W3 | **open** | — |
| DA-0193 | `jasper/correction/session.py` :: SessionConfig.peq_max_filters/peq_max_cut_db | should-fix | W3 | **open** | — |
| DA-0194 | `jasper/correction/session.py` :: on_capture_uploaded / on_repeat_capture_uplo | should-fix | W3 | **open** | — |
| DA-0195 | `jasper/correction/session.py` :: prepare_and_play_sweep / prepare_and_play_re | should-fix | W3 | **open** | — |
| DA-0196 | `jasper/cues/factory.py` :: build_cue_tts_backend :38 | should-fix | W3 | **open** | — |
| DA-0197 | `jasper/fanin/coupling_auto.py` :: usb_gadget_stack_present | should-fix | W3 | **open** | — |
| DA-0198 | `jasper/fanin/coupling_reconcile.py` :: _arm_ring | should-fix | W3 | **open** | — |
| DA-0199 | `jasper/fanin/coupling_reconcile.py` :: _transport_pipe_shape_ok | should-fix | W3 | **mooted** | transport-pipe path and shape validator deleted; legacy value now fails safe to loopback — #1266 |
| DA-0200 | `jasper/home_assistant.py` :: read_ha_env_file | should-fix | W3 | **open** | — |
| DA-0201 | `jasper/multiroom/active_leader_config.py` :: _camilla | should-fix | W3 | **open** | — |
| DA-0202 | `jasper/multiroom/runtime_balance.py` :: PAIR_BALANCE_FILTER | should-fix | W3 | **open** | — |
| DA-0203 | `jasper/music_sources.py` :: SOURCE_TO_ACTIVE_KEY | should-fix | W3 | **open** | — |
| DA-0204 | `jasper/output_topology.py` :: save_output_topology | should-fix | W3 | **open** | — |
| DA-0205 | `jasper/peering/config.py` :: _read_env_file | should-fix | W3 | **open** | — |
| DA-0206 | `jasper/sound/camilla_yaml.py` :: RING_FLAT_CONFIG_NAME | should-fix | W3 | **open** | — |
| DA-0207 | `jasper/sound/profile.py` :: _atomic_write_text | should-fix | W3 | **open** | — |
| DA-0208 | `jasper/speaker_name.py` :: write_state | should-fix | W3 | **open** | — |
| DA-0209 | `jasper/transit/providers/nyc_bus.py` :: NYC_BBOX | should-fix | W3 | **open** | — |
| DA-0210 | `jasper/volume_coordinator.py` :: _bluez_alsa_active_transport_path :2186-2207 | should-fix | W3 | **open** | — |
| DA-0211 | `jasper/volume_coordinator.py` :: _dispatch :607-677; apply_active_source_tran | should-fix | W3 | **fixed** | repeated source-push-failure transactions consolidated behind one fail-closed helper — #1369 |
| DA-0212 | `jasper/wake_corpus/bridge_session.py` :: set_bridge_outputs_for_session:1245 | should-fix | W3 | **fixed** | shared recorder env write/restart/rollback transaction centralized — #1370 |
| DA-0213 | `jasper/web/bluetooth_setup.py` :: _read_json | should-fix | W3 | **fixed** | bounded object reader preserves local caps and response policy — #1376 |
| DA-0214 | `jasper/web/correction_setup.py` :: _dispatch_balance :3230-3244 / _dispatch_syn | should-fix | W3 | **fixed** | balance/sync starts share one lock-scoped active-session blocker — #1371 |
| DA-0215 | `jasper/web/correction_setup.py` :: _reset_accepts_target_config_path / _auto_re | should-fix | W3 | **fixed** | reset/auto-revert capability checks consolidated into one helper — #1372 |
| DA-0216 | `jasper/web/google_setup.py` :: _redirect | should-fix | W3 | **fixed** | Google/Spotify legacy query-message redirects share one compatibility helper — #1373 |
| DA-0217 | `jasper/web/sources_setup.py` :: _send_json | should-fix | W3 | **open** | — |
| DA-0218 | `jasper/web/wake_setup.py` :: _read_json_body | should-fix | W3 | **open** | — |
| DA-0219 | `rust/jasper-fanin/src/impulse_tap.rs` :: module (whole file) vs rust/jasper-usbsink-a | should-fix | W3 | **mooted** | solo USB capture and second impulse-tap module deleted; fan-in is the sole remaining copy — #1209 |
| DA-0220 | `rust/jasper-fanin/src/xrun_log.rs` :: escape_json (:232) | should-fix | W3 | **fixed** | control bytes now use one canonical JSON escaping path — #1379 |
| DA-0221 | `rust/jasper-host-clock/src/lib.rs` :: HostClock::begin_probe / HostClock::restart_ | should-fix | W3 | **open** | — |
| DA-0222 | `rust/jasper-outputd/src/alsa_backend.rs` :: AlsaBackend::read_content_available (L220) / | should-fix | W3 | **open** | — |
| DA-0223 | `rust/jasper-resampler/src/lib.rs` :: minimum_safe_fill_frames :88 | should-fix | W3 | **fixed** | outputd delegates its resampler fill floor to the canonical formula — #1385 |
| DA-0224 | `scripts/_audit_wake_events.py` :: load_wav():29-40, rms():43-46 | should-fix | W3 | **fixed** | offline wake analyzers share one numerically pinned RMS metric — #1387 |
| DA-0225 | `scripts/_run_wake_training_phase0.py` :: _safe_to_remove_output:95 | should-fix | W3 | **fixed** | three destructive wake callers share fail-closed self-bound ownership guard — #1374 |
| DA-0226 | `scripts/chip-aec-baseline-check.sh` :: daemon_set_mode/set_bypass/prompt | should-fix | W3 | **fixed** | shared experiment lifecycle + signal-safe restoration — #1391 |
| DA-0227 | `scripts/multiroom-spike.sh` :: make_chirp_remote() :247-271 | should-fix | W3 | **fixed** | canonical raw/WAV broadband click generator — #1395 |
| DA-0228 | `scripts/prepare-wake-livekit-smoke.sh` :: :20-33 | should-fix | W3 | **fixed** | offline wake wrappers share one worktree-aware Python resolver — #1388 |
| DA-0229 | `scripts/ring-proto/arm-ring-a.sh` :: :373 | should-fix | W3 | **fixed** | Ring A forwards the selected capture PCM under the builder-owned env key — #1378 |
| DA-0230 | `scripts/s0-sync-measure.py` :: read_wav_mono:72-108 | should-fix | W3 | **open** | — |
| DA-0231 | `scripts/switch-gemini-model.sh` :: :28-29 | should-fix | W3 | **fixed** | laptop Pi scripts share canonical host/user resolution through `_lib.sh` — #1381 |
| DA-0232 | `scripts/switch-gemini-model.sh` :: :35-36 | should-fix | W3 | **fixed** | Gemini aliases resolve from the installed catalog and update the effective selector owner — #1389 |
| DA-0233 | `tests/test_active_speaker_bringup.py` :: _topology | should-fix | W3 | **fixed** | canonical active-speaker topology fixture — #1394 |
| DA-0234 | `tests/test_active_speaker_startup_load.py` :: _topology:59 | should-fix | W3 | **fixed** | canonical active-speaker topology fixture — #1394 |
| DA-0235 | `tests/test_build_wake_negative_feature_bank.py` :: FakeExtractor / _write_wav / _write_bundle | should-fix | W3 | **open** | — |
| DA-0236 | `tests/test_correction_session.py` :: _make_session | should-fix | W3 | **fixed** | isolated shared session fixture with lazy config ownership — #1393 |
| DA-0237 | `tests/test_install_core_audio_graph_loop.py` :: EXPECTED_DSTS | should-fix | W3 | **fixed** | asserted destinations cover the full shared install table with set equality — #1203 |
| DA-0238 | `tests/test_voice_daemon_defects.py` :: :19-34 (httpx/sounddevice/rapidfuzz sys.modu | should-fix | W3 | **fixed** | removed import-time dependency poisoning + AST ratchet — #1398 |
| DA-0239 | `tests/test_wake_corpus_setup.py` :: _serve_in_thread helper, 23 call sites e.g.  | should-fix | W3 | **fixed** | function-scoped live-server fixture + bounded teardown — #1396 |
| DA-0240 | `tests/test_web_airplay_setup.py` :: _FakeHandler | should-fix | W3 | **fixed** | faithful shared wizard handler double — #1397 |
| DA-0241 | `jasper/route_latency/mic_readers.py` :: build_mic_reader | should-fix | W4 | **fixed** | direct UDP/ALSA factory and reader contract coverage — #1399 |
| DA-0242 | `deploy/assets/chat/js/views.js` :: dateValueToSince():122 / sinceToDateValue(): | should-fix | W4 | **fixed** | exact-source Node coverage for Gregorian/local-date conversions — #1400 |
| DA-0243 | `deploy/bin/jasper-deploy-health` :: main() / REQUIRED_ACTIVE_UNITS / _status_jso | should-fix | W4 | **fixed** | profile/source-intent-aware bounded deploy health — #1404 |
| DA-0244 | `jasper/accessories/wiim_remote_mic.py` :: _find_voice_characteristic | should-fix | W4 | **fixed** | fail-closed unique WiiM voice-report identity — #1403 |
| DA-0245 | `jasper/active_speaker/commission_wiring.py` :: resolve_commission_inputs:70 | should-fix | W4 | **fixed** | explicit-preset, ready-design, and blocked-fallback contract coverage — #1408 |
| DA-0246 | `jasper/bluetooth/engine.py` :: _auto_stop_scan | should-fix | W4 | **fixed** | bounded fail-closed scan lifecycle and serialized shared-bus recovery — #1409 |
| DA-0247 | `jasper/bluetooth/scan.py` :: DeviceObserver | should-fix | W4 | **fixed** | bounded device-observer subscriptions, stale-callback guards, and battery-task cleanup — #1405 |
| DA-0248 | `jasper/cli/aec_tune.py` :: DELAY_FILE / main | should-fix | W4 | **fixed** | diagnostic-only tuning with bounded transactional cleanup and volatile rollback — #1412 |
| DA-0249 | `jasper/cli/doctor/audio.py` :: check_loopback:263 / check_fanin_binary_inst | should-fix | W4 | **fixed** | hardware-free loopback and fan-in installation-check contracts — #1406 |
| DA-0250 | `jasper/cli/doctor/satellites.py` :: check_dial_heartbeat | should-fix | W4 | **fixed** | exception, never-seen, recent, and long-idle activity contracts — #1410 |
| DA-0251 | `jasper/peering/discovery.py` :: PeerDiscovery._handle_change | should-fix | W4 | **fixed** | fail-soft record parsing and FIFO identity-change bookkeeping — #1411 |
| DA-0252 | `jasper/transit/_mta_stations.py` :: load_stations | should-fix | W4 | **fixed** | real missing/open/decode fallback tests and truthful permissive-CSV contract — #1413 |
| DA-0253 | `jasper/voice/openai_session.py` :: OpenAIRealtimeConnection._reconnect_with_bac | should-fix | W4 | **fixed** | exact-threshold OpenAI reconnect escalation and rate-limit contract — #1414 |
| DA-0254 | `jasper/web/speaker_setup.py` :: _apply_name / _write_bluez_main_conf_name | should-fix | W4 | **open** | — |
| DA-0255 | `jasper/web/sync_flow.py` :: handle_start / active_phase / handle_status | should-fix | W4 | **open** | `handle_play` now directly covered by race and happy-path tests in #1239; start/analyze/active-phase coverage remains |
| DA-0256 | `rust/jasper-dual-dac-lab/src/main.rs` :: fill_identity (:1029-1062) | should-fix | W4 | **open** | — |
| DA-0257 | `rust/jasper-fanin/src/impulse_tap.rs` :: RESERVED_TAP_DIR_BASENAMES (:148) | should-fix | W4 | **open** | — |
| DA-0258 | `rust/jasper-outputd/src/state.rs` :: OutputdState::snapshot_json | should-fix | W4 | **open** | — |
| DA-0259 | `rust/jasper-outputd/src/state.rs` :: StateServer::handle_connection | should-fix | W4 | **open** | — |
| DA-0260 | `rust/jasper-resampler/src/lib.rs` :: AudioRing::push_interleaved :335-349 | should-fix | W4 | **open** | — |
| DA-0261 | `tests/test_doctor.py` :: (whole file) | should-fix | W4 | **open** | — |
| DA-0262 | `tests/test_laptop_onboarding_scripts.py` :: repo_env_local | should-fix | W4 | **open** | — |
| DA-0263 | `tests/test_multiroom_reconcile.py` :: test_main_fresh_solo_first_reconcile_never_t | should-fix | W4 | **open** | — |
| DA-0264 | `tests/test_multiroom_runtime_balance.py` :: apply_local_trim | should-fix | W4 | **open** | — |
| DA-0265 | `tests/test_tools_spotify.py` :: spotify_queue | should-fix | W4 | **open** | — |
| DA-0266 | `tests/test_web_correction_setup.py` :: test_known_post_routes_reach_csrf_guard | should-fix | W4 | **open** | — |
| DA-0267 | `jasper/route_latency/tap_client.py` :: TapClient.status | nit | W5 | **open** | — |
| DA-0268 | `PLAN.md` :: 150 | nit | W5 | **open** | — |
| DA-0269 | `capture-page/js/main.js` :: renderCalibration():247-351 | nit | W5 | **open** | — |
| DA-0270 | `capture-page/js/render.js` :: :118 | nit | W5 | **open** | — |
| DA-0271 | `deploy/assets/app.css` :: .ico--lg:207 | nit | W5 | **open** | — |
| DA-0272 | `deploy/assets/chat/js/main.js` :: state.csrfToken:26 / csrfPresent:83 | nit | W5 | **open** | — |
| DA-0273 | `deploy/assets/chat/js/views.js` :: buildPage():98 | nit | W5 | **open** | — |
| DA-0274 | `deploy/assets/correction/js/main.js` :: chartPayload:1977 | nit | W5 | **open** | — |
| DA-0275 | `deploy/assets/sound-profile/js/main.js` :: import block :22-47 | nit | W5 | **open** | — |
| DA-0276 | `deploy/assets/sound-profile/js/main.js` :: patchActiveSpeaker call sites, e.g. :5242, : | nit | W5 | **open** | — |
| DA-0277 | `deploy/assets/tools/js/render.js` :: toolCard | nit | W5 | **open** | — |
| DA-0278 | `deploy/assets/wake-corpus/js/main.js` :: refreshSessions | nit | W5 | **open** | — |
| DA-0279 | `deploy/install.sh` :: main() / install_profile_legacy_marker_migra | nit | W5 | **open** | — |
| DA-0280 | `deploy/lib/install/systemd-units.sh` :: install_grouping_unit_files | nit | W5 | **open** | — |
| DA-0281 | `deploy/lib/install/systemd-units.sh` :: install_systemd_units | nit | W5 | **open** | — |
| DA-0282 | `deploy/systemd/jts-mic.slice` :: :32 | nit | W5 | **open** | — |
| DA-0283 | `deploy/udev/99-jasper-audio-hardware-reconcile.rules` :: ENV{PRODUCT}=="05ac/110a/*" | nit | W5 | **open** | — |
| DA-0284 | `docs/HANDOFF-audio-graph-consolidation.md` :: 48 | nit | W5 | **open** | — |
| DA-0285 | `docs/HANDOFF-calibration-agent.md` :: 790,817,1192 | nit | W5 | **open** | — |
| DA-0286 | `docs/HANDOFF-fan-in-daemon.md` :: 1037 | nit | W5 | **open** | — |
| DA-0287 | `docs/HANDOFF-mic-quality-v2.md` :: 950-951 | nit | W5 | **open** | — |
| DA-0288 | `docs/HANDOFF-usb-low-latency.md` :: 84-87 | nit | W5 | **open** | — |
| DA-0289 | `experiments/aec3-v2-deep-tune-spike/sweep.py` :: :29,36-37 | nit | W5 | **open** | — |
| DA-0290 | `firmware/dial/src/display.cpp` :: display_init | nit | W5 | **open** | — |
| DA-0291 | `firmware/satellite-amoled/src/display.cpp` :: displayShowStatus | nit | W5 | **open** | — |
| DA-0292 | `firmware/satellite-amoled/src/main.cpp` :: :38-40 | nit | W5 | **open** | — |
| DA-0293 | `jasper/active_speaker/commission_ramp.py` :: _record_ramp_state:201 | nit | W5 | **open** | — |
| DA-0294 | `jasper/active_speaker/commissioning_coordinator.py` :: _combined_group_view | nit | W5 | **open** | — |
| DA-0295 | `jasper/active_speaker/graph_evidence.py` :: driver_mute_name | nit | W5 | **open** | — |
| DA-0296 | `jasper/active_speaker/playback.py` :: start_tone_playback / TonePlaybackBackend | nit | W5 | **open** | — |
| DA-0297 | `jasper/active_speaker/playback_route.py` :: active_playback_route_capability / _route_ca | nit | W5 | **open** | — |
| DA-0298 | `jasper/active_speaker/staging.py` :: _passive_mains_with_sub_preset:1119 | nit | W5 | **open** | — |
| DA-0299 | `jasper/active_speaker/staging.py` :: lines 463-822 (safety-evidence functions) vs | nit | W5 | **open** | — |
| DA-0300 | `jasper/active_speaker/staging.py` :: stage_protected_startup_config:1715 / prepar | nit | W5 | **open** | — |
| DA-0301 | `jasper/active_speaker/startup_load.py` :: build_startup_load_preflight / stop_control_ | nit | W5 | **open** | — |
| DA-0302 | `jasper/active_speaker/startup_load.py` :: build_startup_load_preflight, physical_ident | nit | W5 | **open** | — |
| DA-0303 | `jasper/active_speaker/web_commissioning.py` :: SUMMED_COMMISSION_TONE_BACKEND | nit | W5 | **open** | — |
| DA-0304 | `jasper/active_speaker/web_commissioning.py` :: _commission_tone_mux_command | nit | W5 | **open** | — |
| DA-0305 | `jasper/active_speaker/web_commissioning.py` :: _ensure_commission_startup_anchor | nit | W5 | **open** | — |
| DA-0306 | `jasper/aec_engines/dtln.py` :: DTLNEngine.process | nit | W5 | **open** | — |
| DA-0307 | `jasper/assistant_loudness.py` :: AssistantLoudnessProfile.phrase_hash | nit | W5 | **open** | — |
| DA-0308 | `jasper/audio_runtime_plan.py` :: FaninOutputBufferTarget.detail | nit | W5 | **open** | — |
| DA-0309 | `jasper/audio_validation.py` :: HardwareValidationRun.latest_path:182 | nit | W5 | **open** | — |
| DA-0310 | `jasper/audio_validation.py` :: SCHEMA_VERSION:57 / CHIP_AEC_CALIBRATION_REQ | nit | W5 | **open** | — |
| DA-0311 | `jasper/audio_validation.py` :: lines 213-599 vs 888-2992 | nit | W5 | **open** | — |
| DA-0312 | `jasper/audio_validation.py` :: route_latency_gate_status:287,310 | nit | W5 | **open** | — |
| DA-0313 | `jasper/bluetooth/adapter.py` :: _close_pairing_window | nit | W5 | **open** | — |
| DA-0314 | `jasper/bluetooth/models.py` :: UUID_AVRCP | nit | W5 | **open** | — |
| DA-0315 | `jasper/bluetooth/models.py` :: _MAC_ALIAS_RE | nit | W5 | **open** | — |
| DA-0316 | `jasper/calibration_agent/actions.py` :: _run_one_action | nit | W5 | **open** | — |
| DA-0317 | `jasper/calibration_agent/response.py` :: _policy_allows | nit | W5 | **open** | — |
| DA-0318 | `jasper/capture_relay/alignment.py` :: cross_correlation_alignment | nit | W5 | **open** | — |
| DA-0319 | `jasper/citibike.py` :: CitiBikeClient.resolve_label | nit | W5 | **open** | — |
| DA-0320 | `jasper/cli/aec_bridge.py` :: LegEmitter.engine_token :581, add_emitter(.. | nit | W5 | **open** | — |
| DA-0321 | `jasper/cli/aec_bridge.py` :: MIC_DEVICE :157 | nit | W5 | **open** | — |
| DA-0322 | `jasper/cli/aec_bridge.py` :: _aec_loop setup preamble (~1592-1906) vs loo | nit | W5 | **open** | — |
| DA-0323 | `jasper/cli/doctor/__init__.py` :: probe_aec_ref_path | nit | W5 | **open** | — |
| DA-0324 | `jasper/cli/doctor/audio.py` :: check_fanin_binary_installed..check_aec_cloc | nit | W5 | **open** | — |
| DA-0325 | `jasper/cli/doctor/audio.py` :: check_outputd_service:2311 | nit | W5 | **open** | — |
| DA-0326 | `jasper/cli/doctor/privsep.py` :: MANIFEST (jasper-wiim-remote-mic) | nit | W5 | **open** | — |
| DA-0327 | `jasper/cli/route_latency_harness.py` :: _cmd_run / _add_analyze_args / _add_schedule | nit | W5 | **open** | — |
| DA-0328 | `jasper/cli/system_soak.py` :: :253 | nit | W5 | **open** | — |
| DA-0329 | `jasper/cli/wake_enroll.py` :: record_window / quadrant_dirs | nit | W5 | **open** | — |
| DA-0330 | `jasper/config.py` :: 255-257,684-689 | nit | W5 | **open** | — |
| DA-0331 | `jasper/config.py` :: 318-319,330-331 | nit | W5 | **open** | — |
| DA-0332 | `jasper/config.py` :: Config.camilla2_host / camilla2_port / camil | nit | W5 | **open** | — |
| DA-0333 | `jasper/control/aec_endpoints.py` :: _atomic_rewrite_env | nit | W5 | **open** | — |
| DA-0334 | `jasper/control/server.py` :: :484, :487, :490, :496, :497, :506 | nit | W5 | **open** | — |
| DA-0335 | `jasper/control/server.py` :: _aec_full_status | nit | W5 | **open** | — |
| DA-0336 | `jasper/control/state_aggregate.py` :: _camilla_status | nit | W5 | **open** | — |
| DA-0337 | `jasper/control/system_metrics.py` :: SystemSampler.stop | nit | W5 | **open** | — |
| DA-0338 | `jasper/control/volume_ops.py` :: _build_spotify_router_or_none / _spotify_emp | nit | W5 | **open** | — |
| DA-0339 | `jasper/control/wifi_guardian_state.py` :: snapshot / _active_ssid / _last_guardian_eve | nit | W5 | **open** | — |
| DA-0340 | `jasper/correction/acoustic_quality.py` :: _capture_summary / build_acoustic_quality_re | nit | W5 | **open** | — |
| DA-0341 | `jasper/correction/fir_runtime.py` :: stage_fir_artifact | nit | W5 | **open** | — |
| DA-0342 | `jasper/correction/runtime_safety.py` :: _issue_detail | nit | W5 | **open** | — |
| DA-0343 | `jasper/correction/session.py` :: SessionEvent / self._events / _emit | nit | W5 | **open** | — |
| DA-0344 | `jasper/correction/session.py` :: _ensure_bundle_dir / _existing_bundle_depend | nit | W5 | **open** | — |
| DA-0345 | `jasper/cues/manager.py` :: AudioCueManager.status :168 | nit | W5 | **open** | — |
| DA-0346 | `jasper/fanin/buffer_reconcile.py` :: :112 | nit | W5 | **open** | — |
| DA-0347 | `jasper/fanin/coupling_reconcile.py` :: _resolved_fanin_ring_slots | nit | W5 | **open** | — |
| DA-0348 | `jasper/fanin/coupling_reconcile.py` :: reconcile_auto | nit | W5 | **open** | — |
| DA-0349 | `jasper/fanin/coupling_reconcile.py` :: reconcile_coupling | nit | W5 | **open** | — |
| DA-0350 | `jasper/flight_recorder.py` :: RingFlushHandler.flush_buffer | nit | W5 | **open** | — |
| DA-0351 | `jasper/google_creds.py` :: GoogleRegistry.save | nit | W5 | **open** | — |
| DA-0352 | `jasper/mics/__init__.py` :: PROFILES | nit | W5 | **open** | — |
| DA-0353 | `jasper/mics/xvf3800.py` :: is_recommended_firmware | nit | W5 | **fixed** | independently dead firmware predicate removed alongside unreachable mixer mutator — #1322 |
| DA-0354 | `jasper/multiroom/config.py` :: _format_roster | nit | W5 | **open** | — |
| DA-0355 | `jasper/multiroom/reconcile.py` :: _write_outputd_env:1066-1101 | nit | W5 | **open** | — |
| DA-0356 | `jasper/music_sources.py` :: is_music_source | nit | W5 | **open** | — |
| DA-0357 | `jasper/mux.py` :: :1020 | nit | W5 | **open** | — |
| DA-0358 | `jasper/output_hardware.py` :: _find_controller | nit | W5 | **open** | — |
| DA-0359 | `jasper/output_hardware.py` :: main | nit | W5 | **open** | — |
| DA-0360 | `jasper/output_topology.py` :: OutputTopology.output_layout | nit | W5 | **open** | — |
| DA-0361 | `jasper/output_topology.py` :: SpeakerChannel / channel_identity_report | nit | W5 | **open** | — |
| DA-0362 | `jasper/peering/daemon.py` :: _spawn_send | nit | W5 | **open** | — |
| DA-0363 | `jasper/research/providers/openai_research.py` :: import reconnect_backoff_delay | nit | W5 | **open** | — |
| DA-0364 | `jasper/ring_negotiation.py` :: accept | nit | W5 | **open** | — |
| DA-0365 | `jasper/sound/profile.py` :: ProfileLibraryEntry.to_dict | nit | W5 | **open** | — |
| DA-0366 | `jasper/tools/packs.py` :: register_packs | nit | W5 | **open** | — |
| DA-0367 | `jasper/tools/spotify.py` :: _resolve_for_play :470 | nit | W5 | **open** | — |
| DA-0369 | `jasper/voice/daemon_main.py` :: _build_registry | nit | W5 | **open** | — |
| DA-0370 | `jasper/voice/openai_session.py` :: OpenAIRealtimeTurn.__init__:248 | nit | W5 | **open** | — |
| DA-0371 | `jasper/voice/openai_session.py` :: OpenAIRealtimeTurn.send_text_context | nit | W5 | **open** | — |
| DA-0372 | `jasper/voice_daemon.py` :: :4198-4245 | nit | W5 | **open** | — |
| DA-0373 | `jasper/voice_daemon.py` :: :84 | nit | W5 | **open** | — |
| DA-0374 | `jasper/volume_diagnostics.py` :: PUSH_UNSUPPORTED | nit | W5 | **open** | — |
| DA-0375 | `jasper/wake_corpus/bridge_session.py` :: :1237 | nit | W5 | **open** | — |
| DA-0376 | `jasper/wake_corpus/bridge_session.py` :: :153 | nit | W5 | **open** | — |
| DA-0377 | `jasper/wake_corpus/bridge_session.py` :: _enabled_legs_from_metadata:1552 | nit | W5 | **open** | — |
| DA-0378 | `jasper/wake_corpus/bridge_session.py` :: voice_daemon_active:2109 | nit | W5 | **fixed** | redundant function-local subprocess import removed — #1304 |
| DA-0379 | `jasper/wake_corpus/recording_backend.py` :: RecordingBackend._write_active_session_marke | nit | W5 | **open** | — |
| DA-0380 | `jasper/wake_corpus/recording_backend.py` :: RecordingBackend.begin_session | nit | W5 | **open** | — |
| DA-0381 | `jasper/weather.py` :: WeatherClient._get_json | nit | W5 | **open** | — |
| DA-0382 | `jasper/web/correction_setup.py` :: _handle_interpret / _handle_propose tuning-s | nit | W5 | **open** | — |
| DA-0383 | `jasper/web/correction_setup.py` :: _handle_start :1345,1385 | nit | W5 | **open** | — |
| DA-0384 | `jasper/web/correction_setup.py` :: log_event event names :1246,1358,1886,1919 | nit | W5 | **open** | — |
| DA-0385 | `jasper/web/sound_setup.py` :: _live_draft_profile | nit | W5 | **open** | — |
| DA-0386 | `jasper/web/tools_setup.py` :: _handle_toggle_pack | nit | W5 | **open** | — |
| DA-0387 | `jasper/web/transit_setup.py` :: _apply_save | should-fix | W5 | **fixed** | duplicate of DA-0035; BusTime key scrubbed from the same broad-except surface — #1236 |
| DA-0388 | `jasper/web/transit_setup.py` :: _handle_geocode | nit | W5 | **open** | — |
| DA-0389 | `jasper/web/voice_setup.py` :: _load_state | nit | W5 | **open** | — |
| DA-0390 | `jasper/web/weather_setup.py` :: main | nit | W5 | **open** | — |
| DA-0391 | `jasper/web/wifi_setup.py` :: connect_new | nit | W5 | **open** | — |
| DA-0392 | `jasper/wifi_guardian_persistence.py` :: _parse_env_line | nit | W5 | **open** | — |
| DA-0393 | `jasper/xvf/xvf_host.py` :: PARAMETERS | nit | W5 | **open** | — |
| DA-0394 | `jasper_aec3/setup.py` :: module | nit | W5 | **open** | — |
| DA-0395 | `rust/jasper-clock/src/htimestamp.rs` :: HtimestampGuard (re-exported lib.rs:73-75) | nit | W5 | **open** | — |
| DA-0396 | `rust/jasper-clock/src/lib.rs` :: Dll::update_lock | nit | W5 | **open** | — |
| DA-0397 | `rust/jasper-dual-dac-lab/src/main.rs` :: configure_pcm (:590,594) vs validate_run_con | nit | W5 | **open** | — |
| DA-0398 | `rust/jasper-fanin/src/config.rs` :: cushion_decay_floor_default derivation (:609 | nit | W5 | **open** | — |
| DA-0399 | `rust/jasper-fanin/src/lane_resampler.rs` :: LaneResamplerObservability::armed | nit | W5 | **open** | — |
| DA-0400 | `rust/jasper-fanin/src/lane_resampler.rs` :: max_prime_periods | nit | W5 | **open** | — |
| DA-0401 | `rust/jasper-fanin/src/mixer.rs` :: DirectCapture subsystem (DirectCapture, Dire | nit | W5 | **open** | — |
| DA-0402 | `rust/jasper-fanin/src/state.rs` :: snapshot_json | nit | W5 | **open** | — |
| DA-0403 | `rust/jasper-host-clock/src/lib.rs` :: HostClock::tick_locked (anti-windup gates) | nit | W5 | **open** | — |
| DA-0404 | `rust/jasper-host-clock/src/lib.rs` :: SlopeEstimator::update | nit | W5 | **open** | — |
| DA-0405 | `rust/jasper-outputd/src/aec_clock.rs` :: SroEstimator::verdict_reason (L250) | nit | W5 | **open** | — |
| DA-0406 | `rust/jasper-outputd/src/config.rs` :: chip_ref_tee_path (L173, L676) | nit | W5 | **open** | — |
| DA-0407 | `rust/jasper-outputd/src/main.rs` :: _period_samples | nit | W5 | **open** | — |
| DA-0408 | `rust/jasper-outputd/src/tts.rs` :: FlushSummary::from_events | nit | W5 | **open** | — |
| DA-0409 | `rust/jasper-ring/src/layout.rs` :: Geometry::bytes_per_sample | nit | W5 | **open** | — |
| DA-0410 | `rust/jasper-ring/src/writer.rs` :: RingWriter::publish | nit | W5 | **open** | — |
| DA-0411 | `rust/jasper-tts-protocol/src/lib.rs` :: read_command | nit | W5 | **open** | — |
| DA-0412 | `rust/jasper-tts-protocol/src/lib.rs` :: read_command (GAIN branch) | nit | W5 | **open** | — |
| DA-0413 | `rust/jasper-tts-protocol/src/loudness.rs` :: AssistantLoudness::decide_gain | nit | W5 | **open** | — |
| DA-0414 | `rust/jasper-tts-protocol/src/loudness.rs` :: KWeightedWindow::short_lufs / window_lufs | nit | W5 | **open** | — |
| DA-0416 | `rust/jasper-usbsink-audio/src/main.rs` :: TapPublisher::poll (lines 1306-1347) | nit | W5 | **open** | — |
| DA-0417 | `rust/jasper-usbsink-audio/src/main.rs` :: handle_preempt_request (lines 1050-1060) | nit | W5 | **open** | — |
| DA-0418 | `rust/jasper-usbsink-audio/src/main.rs` :: write_http_json (line 1186) | nit | W5 | **open** | — |
| DA-0419 | `scripts/_audit_baseline_events.py` :: module | nit | W5 | **open** | — |
| DA-0420 | `scripts/_extract_wake_corpus.py` :: main():556-575 | nit | W5 | **open** | — |
| DA-0421 | `scripts/aec-probe-pinknoise.sh` :: :53 | nit | W5 | **open** | — |
| DA-0422 | `scripts/claim-librespot.sh` :: cleanup, :52-59 and :98 | nit | W5 | **open** | — |
| DA-0423 | `scripts/onboard.sh` :: :447-452 | nit | W5 | **open** | — |
| DA-0424 | `scripts/rename-speaker.sh` :: :1 | nit | W5 | **open** | — |
| DA-0425 | `scripts/wake-rate-test.sh` :: OUT_REMOTE:75 | nit | W5 | **open** | — |
| DA-0426 | `scripts/xvf-interrogate.sh` :: usage:43-47 | nit | W5 | **fixed** | usage extraction now includes the complete final state-restoration text — #1306 |
| DA-0497 | `capture-page/index.html` :: :187 | nit | W3 | **open** | — |
| DA-0498 | `deploy/assets/correction/js/main.js` :: pollState:2919 | nit | W3 | **open** | — |
| DA-0499 | `deploy/assets/correction/js/main.js` :: reportIssueList:1766 vs renderQuality:868 /  | nit | W3 | **open** | — |
| DA-0500 | `deploy/assets/sound-profile/js/main.js` :: prepareSummedTest :5266, stopSummedTest :538 | nit | W3 | **open** | — |
| DA-0501 | `deploy/bin/jasper-render-asound-conf` :: :15 OUTPUT= | nit | W3 | **open** | — |
| DA-0502 | `deploy/install.sh` :: ensure_output_hardware_state | nit | W3 | **open** | — |
| DA-0503 | `deploy/install.sh` :: find_card | nit | W3 | **open** | — |
| DA-0504 | `firmware/dial/platformio.ini` :: [env:crowpanel-128-rotary-hmi] | nit | W3 | **open** | — |
| DA-0505 | `firmware/dial/src/main.cpp` :: postJson / postVolumeAdjust | nit | W3 | **open** | — |
| DA-0506 | `jasper/_oom_adj.py` :: EXPECTED | nit | W3 | **open** | — |
| DA-0507 | `jasper/accessories/bridge.py` :: _TapCounter._dispatch / _log_key_action | nit | W3 | **open** | — |
| DA-0508 | `jasper/accessories/reconcile.py` :: _unwrap / _variant_value | nit | W3 | **open** | — |
| DA-0509 | `jasper/accounts.py` :: Registry.save | nit | W3 | **open** | — |
| DA-0510 | `jasper/active_speaker/baseline_profile.py` :: _finite_float:138 | nit | W3 | **open** | — |
| DA-0511 | `jasper/active_speaker/commissioning_coordinator.py` :: build_commissioning_view | nit | W3 | **open** | — |
| DA-0512 | `jasper/active_speaker/driver_protection.py` :: auto_level_decision | nit | W3 | **open** | — |
| DA-0513 | `jasper/active_speaker/playback.py` :: _bounded_int | nit | W3 | **open** | — |
| DA-0514 | `jasper/active_speaker/runtime_contract.py` :: _statefile_config_path | nit | W3 | **open** | — |
| DA-0515 | `jasper/active_speaker/startup_load.py` :: SCHEMA_VERSION (line 64) | nit | W3 | **open** | — |
| DA-0516 | `jasper/active_speaker/tone_plan.py` :: _clamp_int | nit | W3 | **fixed** | lossy target-output helpers removed with obsolete preset-era planner — #1321 |
| DA-0517 | `jasper/active_speaker/web_commissioning.py` :: _commission_tone_signal_plan | nit | W3 | **open** | — |
| DA-0518 | `jasper/active_speaker/web_commissioning.py` :: _issue | nit | W3 | **open** | — |
| DA-0519 | `jasper/aec_sweep.py` :: write_aec3_sweep_config | nit | W3 | **open** | — |
| DA-0520 | `jasper/audio_hardware/__init__.py` :: :8 | nit | W3 | **open** | — |
| DA-0521 | `jasper/audio_input_view.py` :: _fusion_view | nit | W3 | **open** | — |
| DA-0522 | `jasper/audio_measurement/deconv.py` :: deconvolve (:156-158) | nit | W3 | **open** | — |
| DA-0523 | `jasper/audio_measurement/quality.py` :: _dbfs (:107-110) | nit | W3 | **open** | — |
| DA-0524 | `jasper/avahi_service.py` :: render_service | nit | W3 | **open** | — |
| DA-0525 | `jasper/bluetooth/adapter.py` :: :77 | nit | W3 | **open** | — |
| DA-0526 | `jasper/calibration_agent/cli.py` :: main | nit | W3 | **open** | — |
| DA-0527 | `jasper/calibration_agent/correction_advisor.py` :: _model_kwargs | nit | W3 | **open** | — |
| DA-0528 | `jasper/calibration_agent/proposal_sim.py` :: _curve_arrays | nit | W3 | **open** | — |
| DA-0529 | `jasper/capture_relay/__init__.py` :: __all__ | nit | W3 | **open** | — |
| DA-0530 | `jasper/capture_relay/health.py` :: relay_base_from_env | nit | W3 | **open** | — |
| DA-0531 | `jasper/cli/aec_bridge.py` :: _aec_loop: 5 safe-engine-process blocks (~20 | nit | W3 | **open** | — |
| DA-0532 | `jasper/cli/airplay_mode.py` :: _read_mode / jasper/web/airplay_setup.py:_cu | nit | W3 | **open** | — |
| DA-0533 | `jasper/cli/doctor/audio.py` :: check_apple_dongle_audio:788 | nit | W3 | **open** | — |
| DA-0534 | `jasper/cli/doctor/grouping.py` :: _parse_env_file | nit | W3 | **open** | — |
| DA-0535 | `jasper/cli/doctor/grouping.py` :: _parse_systemd_environment | nit | W3 | **open** | — |
| DA-0536 | `jasper/cli/wake_enroll.py` :: main | nit | W3 | **open** | — |
| DA-0537 | `jasper/control/uds.py` :: _voice_socket_command / _mux_socket_command | nit | W3 | **open** | — |
| DA-0538 | `jasper/correction/acoustic_quality.py` :: _round | nit | W3 | **open** | — |
| DA-0539 | `jasper/correction/level_match.py` :: _env_float | nit | W3 | **open** | — |
| DA-0540 | `jasper/correction/runtime_integrity.py` :: FANIN_CONTROL_SOCKET | nit | W3 | **open** | — |
| DA-0541 | `jasper/correction/status.py` :: session_snapshot | nit | W3 | **open** | — |
| DA-0542 | `jasper/fanin/coupling_reconcile.py` :: _recover_to_loopback | nit | W3 | **open** | — |
| DA-0543 | `jasper/home_assistant.py` :: 583 | nit | W3 | **open** | — |
| DA-0544 | `jasper/multiroom/reconcile.py` :: _unit_is_enabled:818, _unit_is_active:835 | nit | W3 | **fixed** | shared tri-state systemd probe consolidates the duplicate wrappers — #1299 |
| DA-0545 | `jasper/multiroom/reconcile.py` :: main():1560-1568,1639-1647 | nit | W3 | **open** | — |
| DA-0546 | `jasper/multiroom/runtime_balance.py` :: active_endpoint | nit | W3 | **open** | — |
| DA-0547 | `jasper/mux.py` :: _busctl | nit | W3 | **open** | — |
| DA-0548 | `jasper/mux.py` :: _busctl | nit | W3 | **open** | — |
| DA-0549 | `jasper/output_topology.py` :: OutputTopology.status | nit | W3 | **open** | — |
| DA-0550 | `jasper/output_topology.py` :: _require_id / _optional_id / _text / _bool / | nit | W3 | **open** | — |
| DA-0551 | `jasper/output_topology.py` :: set_channel_identity_verified / set_channel_ | nit | W3 | **open** | — |
| DA-0552 | `jasper/peering/config.py` :: _parse_mode | nit | W3 | **open** | — |
| DA-0553 | `jasper/research/state.py` :: _runtime_provider | nit | W3 | **open** | — |
| DA-0554 | `jasper/tool_state.py` :: write_disabled_tools | nit | W3 | **open** | — |
| DA-0555 | `jasper/tools/calendar.py` :: _no_account_error / _no_credentials_error | nit | W3 | **open** | — |
| DA-0556 | `jasper/tools/spotify.py` :: spotify_play / spotify_play_latest_by_artist | nit | W3 | **open** | — |
| DA-0557 | `jasper/tools/transport.py` :: _mpris_now_playing | nit | W3 | **open** | — |
| DA-0559 | `jasper/voice/daemon_main.py` :: _tts_ready_detail / _warn_if_research_model_ | nit | W3 | **open** | — |
| DA-0560 | `jasper/voice/openai_session.py` :: ConnectionState / _noisy_transitions | nit | W3 | **open** | — |
| DA-0561 | `jasper/voice/openai_session.py` :: OpenAIRealtimeTurn._mark_server_vad / OpenAI | nit | W3 | **open** | — |
| DA-0562 | `jasper/voice_daemon.py` :: WakeLoop.for_tests | nit | W3 | **open** | — |
| DA-0563 | `jasper/voice_daemon.py` :: _arbitrate_acquire_drain | nit | W3 | **open** | — |
| DA-0564 | `jasper/volume_coordinator.py` :: VolumeCoordinator.prepare_source_handoff :69 | nit | W3 | **open** | — |
| DA-0565 | `jasper/weather.py` :: WeatherClient.get_weather | nit | W3 | **open** | — |
| DA-0566 | `jasper/web/bluetooth_setup.py` :: _read_json (package-wide) | nit | W3 | **open** | — |
| DA-0567 | `jasper/web/correction_setup.py` :: _dispatch_sync :3306 | nit | W3 | **open** | — |
| DA-0568 | `jasper/web/rooms_setup.py` :: _get_member_grouping (~886-920) / _get_membe | nit | W3 | **fixed** | bounded remote JSON GET transport consolidated behind one helper — #1309 |
| DA-0569 | `jasper/web/rooms_setup.py` :: _lan_target (~line 692-719) vs jasper/multir | nit | W3 | **fixed** | one IPv4-only LAN predicate now owns validation and outbound SSRF policy — #1309 |
| DA-0570 | `jasper/web/sound_setup.py` :: :2139 | nit | W3 | **open** | — |
| DA-0571 | `jasper/web/sound_setup.py` :: _active_speaker_play_summed_commission_tone | nit | W3 | **open** | — |
| DA-0572 | `jasper/web/speaker_setup.py` :: _systemctl / _unit_active | nit | W3 | **open** | — |
| DA-0573 | `jasper/web/transit_setup.py` :: _mask_key | nit | W3 | **open** | — |
| DA-0574 | `jasper/web/transit_setup.py` :: _value_for | nit | W3 | **open** | — |
| DA-0575 | `jasper/web/wifi_setup.py` :: _send / _send_json | nit | W3 | **open** | — |
| DA-0576 | `jasper/wifi_scan_repair.py` :: _write_state | nit | W3 | **open** | — |
| DA-0577 | `jasper_aec3/src/aec3_binding_v2.cpp` :: Aec3V2::process():191-228 | nit | W3 | **open** | — |
| DA-0578 | `pyproject.toml` :: :125 ([project.optional-dependencies].dev),  | nit | W3 | **open** | — |
| DA-0579 | `rust/jasper-dual-dac-lab/src/main.rs` :: json_string/json_array (:1114-1144) | nit | W3 | **open** | — |
| DA-0580 | `rust/jasper-fanin/src/config.rs` :: env_str/env_u32/env_f32/env_optional (:924-1 | nit | W3 | **open** | — |
| DA-0581 | `rust/jasper-fanin/src/config.rs` :: input_resampler_enabled/_cushion_decay_enabl | nit | W3 | **open** | — |
| DA-0582 | `rust/jasper-fanin/src/lane_resampler.rs` :: try_lock (also decay::tick) | nit | W3 | **open** | — |
| DA-0583 | `rust/jasper-fanin/src/mixer.rs` :: read_input :3684 / read_into_resampler_and_r | nit | W3 | **open** | — |
| DA-0584 | `rust/jasper-fanin/src/mixer.rs` :: service_host_compliance :1760 (PROBE_RESULT_ | nit | W3 | **open** | — |
| DA-0585 | `rust/jasper-fanin/src/state.rs` :: escape_json | nit | W3 | **open** | — |
| DA-0586 | `rust/jasper-outputd/src/dac_content.rs` :: Biquad | nit | W3 | **open** | — |
| DA-0587 | `rust/jasper-outputd/src/ledger.rs` :: PlayoutSegment / PlayoutEvent | nit | W3 | **open** | — |
| DA-0588 | `rust/jasper-outputd/src/state.rs` :: :1265-1270 | nit | W3 | **open** | — |
| DA-0589 | `rust/jasper-outputd/src/state.rs` :: push_kv_str/push_kv_u64/push_kv_bool/push_kv | nit | W3 | **open** | — |
| DA-0590 | `rust/jasper-outputd/src/tts.rs` :: db_to_linear | nit | W3 | **open** | — |
| DA-0591 | `rust/jasper-ring/src/lib.rs` :: TestRingWriter::create_or_attach / try_publi | nit | W3 | **open** | — |
| DA-0592 | `rust/jasper-ring/src/lib.rs` :: TestRingWriter::drop | nit | W3 | **open** | — |
| DA-0593 | `rust/jasper-tts-protocol/src/loudness.rs` :: KWeightingChannel::new / lufs_from_energy | nit | W3 | **open** | — |
| DA-0596 | `rust/jasper-usbsink-audio/src/main.rs` :: HostClockActuator::open (line 1521) | nit | W3 | **open** | — |
| DA-0597 | `scripts/_extract_wake_corpus.py` :: LEGS:99 | nit | W3 | **open** | — |
| DA-0598 | `scripts/_make_wake_test_track.py` :: load_env():40-53 | nit | W3 | **open** | — |
| DA-0599 | `scripts/_prepare_wake_training_workdir.py` :: _safe_to_remove_output:105 | nit | W3 | **open** | — |
| DA-0600 | `scripts/_waveform_fusion_experiment.py` :: :54 | nit | W3 | **open** | — |
| DA-0601 | `scripts/aec-probe-xvf-ref-level.sh` :: restore_services:76 | nit | W3 | **open** | — |
| DA-0602 | `scripts/airplay-receiver-timing-proof.py` :: estimate_lag:428 | nit | W3 | **open** | — |
| DA-0603 | `scripts/build-wake-negative-feature-bank.sh` :: :15-22 | nit | W3 | **open** | — |
| DA-0604 | `scripts/capture-chip-mic.sh` :: :41-45 | nit | W3 | **open** | — |
| DA-0605 | `scripts/capture-reference-condition.sh` :: :189-216 | nit | W3 | **open** | — |
| DA-0606 | `scripts/check-firmware-builds.sh` :: :35-37 | nit | W3 | **open** | — |
| DA-0607 | `tests/js/relay_worker_test.mjs` :: :28,662-701 | nit | W3 | **open** | — |
| DA-0608 | `tests/js/sound_profile_harness.mjs` :: summedSummary (:3162) | nit | W3 | **open** | — |
| DA-0609 | `tests/test_active_speaker_environment.py` :: _valid_config | nit | W3 | **open** | — |
| DA-0610 | `tests/test_aec_bridge_systemd.py` :: _value_for | nit | W3 | **open** | — |
| DA-0611 | `tests/test_aec_reconcile.py` :: _fake_outputd_status_socket | nit | W3 | **open** | — |
| DA-0612 | `tests/test_aec_reconcile.py` :: _fake_systemctl | nit | W3 | **open** | — |
| DA-0613 | `tests/test_camilla_pipe_guard_script.py` :: _runtime_safe_graph_script / _write_statefil | nit | W3 | **open** | — |
| DA-0614 | `tests/test_capture_relay_session.py` :: FakeRelayBackend | nit | W3 | **open** | — |
| DA-0615 | `tests/test_control_server.py` :: class FakePopen (L833, 911, 943, 979, 1012,  | nit | W3 | **open** | — |
| DA-0616 | `tests/test_control_systemd.py` :: _value_for | nit | W3 | **open** | — |
| DA-0617 | `tests/test_cues_generator.py` :: test_cues_are_provider_agnostic | nit | W3 | **open** | — |
| DA-0618 | `tests/test_doctor.py` :: _citibike_cfg | nit | W3 | **open** | — |
| DA-0619 | `tests/test_doctor.py` :: _mock_nmcli_proc | nit | W3 | **open** | — |
| DA-0620 | `tests/test_doctor.py` :: _patch_asound_conf | nit | W3 | **open** | — |
| DA-0621 | `tests/test_doctor_memory_resilience.py` :: _make_oom_run / _make_start_limit_action_run | nit | W3 | **open** | — |
| DA-0622 | `tests/test_doctor_secrets_manifest.py` :: _supp_groups / _user (vs test_doctor_privsep | nit | W3 | **open** | — |
| DA-0623 | `tests/test_doctor_usbsink.py` :: test_usbsink_state_active_no_state_file (rep | nit | W3 | **open** | — |
| DA-0624 | `tests/test_gemini_session.py` :: _SC / _Resp | nit | W3 | **open** | — |
| DA-0625 | `tests/test_install_outputd_ready_nonfatal.py` :: _install_text | nit | W3 | **open** | — |
| DA-0627 | `tests/test_peering_uds.py` :: _short_socket_path | nit | W3 | **open** | — |
| DA-0628 | `tests/test_research_scheduler.py` :: _wait_for | nit | W3 | **open** | — |
| DA-0629 | `tests/test_route_latency_tap_transport.py` :: short_sock_path | nit | W3 | **open** | — |
| DA-0630 | `tests/test_sound_setup.py` :: test_active_speaker_protection_and_stage_con | nit | W3 | **open** | — |
| DA-0631 | `tests/test_sound_setup_commission.py` :: test_summed_test_audio_path_loads_plays_roll | nit | W3 | **open** | — |
| DA-0632 | `tests/test_system_setup.py` :: _http_post / _http_post_json / _http_post_wi | nit | W3 | **open** | — |
| DA-0633 | `tests/test_tool_budget.py` :: test_model_facing_descriptions_stay_under_bu | nit | W3 | **open** | — |
| DA-0634 | `tests/test_tools_gmail.py` :: _make_clients | nit | W3 | **open** | — |
| DA-0635 | `tests/test_tools_transport.py` :: FakeRouter | nit | W3 | **open** | — |
| DA-0636 | `tests/test_voice_daemon_end_turn_reentry.py` :: class _FakeTurn | nit | W3 | **open** | — |
| DA-0637 | `tests/test_wake_corpus_setup.py` :: test_index_html_is_valid_shape, test_render_ | nit | W3 | **open** | — |
| DA-0638 | `tests/test_wake_corpus_setup.py` :: test_metadata_written_per_session:418 (+9 re | nit | W3 | **open** | — |
| DA-0639 | `tests/test_web_speaker_setup.py` :: _FakeHandler | nit | W3 | **fixed** | duplicate of DA-0240; subsumed by shared wizard handler — #1397 |
| DA-0640 | `tests/test_web_wifi_setup.py` :: _make_request | nit | W3 | **open** | — |
| DA-0641 | `tests/test_wifi_setup_guardian_hooks.py` :: _mock_proc / _scripted_nmcli | nit | W3 | **open** | — |
| DA-0642 | `tests/voice_eval/regression/test_spotify.py` :: _playback_skip / _ha_action_skip / _require_ | nit | W3 | **open** | — |
| DA-0643 | `c/jts-ring-ioplug/Makefile` :: bench | nit | W4 | **open** | — |
| DA-0644 | `deploy/systemd/camillagui.service` :: :20 | nit | W4 | **open** | — |
| DA-0645 | `jasper/active_speaker/audible_policy.py` :: audible_role_block_code:35 | nit | W4 | **open** | — |
| DA-0646 | `jasper/bluetooth/handlers/__init__.py` :: pick | nit | W4 | **open** | — |
| DA-0647 | `jasper/cli/aec_sweep_config.py` :: main / _restart_bridge | nit | W4 | **open** | — |
| DA-0648 | `jasper/cli/chip_aec_policy.py` :: main / _query_outputd_status / _shell_assign | nit | W4 | **open** | — |
| DA-0649 | `jasper/cli/usbsink_volume_main.py` :: main / _run | nit | W4 | **open** | — |
| DA-0650 | `jasper/fanin/coupling_auto.py` :: read_boot_config_gadget_present | nit | W4 | **open** | — |
| DA-0651 | `jasper/web/correction_hub.py` :: section_tabs | nit | W4 | **open** | — |
| DA-0652 | `jasper/web/tools_setup.py` :: module docstring | nit | W4 | **open** | — |
| DA-0653 | `rust/jasper-fanin/src/playout.rs` :: PlayoutLedger::enforce_cap (:249) | nit | W4 | **open** | — |
| DA-0654 | `rust/jasper-fanin/src/state.rs` :: snapshot_json:919 | nit | W4 | **open** | — |
| DA-0655 | `rust/jasper-host-clock/src/lib.rs` :: ProbeResult::as_str | nit | W4 | **open** | — |
| DA-0656 | `rust/jasper-resampler/src/lib.rs` :: clamp_i16 :146-148 | nit | W4 | **open** | — |
| DA-0657 | `tests/fixtures/balance_trim_parity_fixture.json` :: :4-5 (trim_db_min/trim_db_max) | nit | W4 | **open** | — |
| DA-0658 | `tests/js/sound_profile_harness.mjs` :: :12 (const modulePath = process.argv[2];) | nit | W4 | **open** | — |
| DA-0659 | `tests/js/sound_profile_harness.mjs` :: :4355 (final console.log) | nit | W4 | **open** | — |
| DA-0660 | `tests/js/sound_profile_harness.mjs` :: dispatchToggle (:590-601) | nit | W4 | **open** | — |
| DA-0661 | `tests/test_active_speaker_cli.py` :: :315 | nit | W4 | **open** | — |
| DA-0662 | `tests/test_active_speaker_setup_status.py` :: import:21 | nit | W4 | **open** | — |
| DA-0663 | `tests/test_analyze_wake_corpus_quality.py` :: pytest_approx | nit | W4 | **open** | — |
| DA-0664 | `tests/test_audio_hardware_reconcile.py` :: 105 | nit | W4 | **open** | — |
| DA-0665 | `tests/test_dependency_groups.py` :: module scope | nit | W4 | **open** | — |
| DA-0666 | `tests/test_doctor.py` :: test_run_async_parallelizes_blocking_checks_ | nit | W4 | **open** | — |
| DA-0667 | `tests/test_doctor_usbsink.py` :: 1180 | nit | W4 | **open** | — |
| DA-0668 | `tests/test_sound_setup.py` :: :1 | nit | W4 | **open** | — |
| DA-0669 | `tests/test_sound_setup.py` :: test_apply_profile_rolls_back_when_reload_fa | nit | W4 | **open** | — |
| DA-0670 | `tests/test_sound_setup.py` :: test_reconcile_current_dsp_skips_unknown_con | nit | W4 | **open** | — |
| DA-0671 | `tests/test_sources_setup_usbsink.py` :: _patch_config | nit | W4 | **open** | — |
| DA-0672 | `tests/test_tools_spotify.py` :: test_revoked_then_relinked_recovers_without_ | nit | W4 | **open** | — |
| DA-0673 | `tests/test_usbsink_volume_bridge.py` :: test_run_retries_discovery_after_transient_m | nit | W4 | **open** | — |
| DA-0674 | `tests/test_volume_diagnostics.py` :: build_volume_policy_snapshot | nit | W4 | **open** | — |
| DA-0675 | `tests/test_wake_corpus_setup.py` :: :1-4307 (whole file) | nit | W4 | **open** | — |
| DA-0676 | `tests/test_web_rooms_setup.py` :: test_post_swap_rollback_failure_is_surfaced | nit | W4 | **open** | — |
| DA-0677 | `tests/voice_eval/regression/test_barge_in_openai.py` :: truncate_lines / _AUDIO_END_MS_RE | nit | W4 | **open** | — |
| DA-0002 | `jasper/capture_relay/alignment.py` :: assert_alignment_confident | blocker | W0 | **deferred** | owner-decision: per-flow seam, require=False first — #1214 (body) |
| DA-0001 | `deploy/assets/rooms/js/main.js` :: makeBondCard().sync() — appendChildren call  | blocker | W0 | **fixed** | ReferenceError fixed + regression test — #1214 |
| DA-0076 | `AGENTS.md` :: "Cue regeneration" paragraph, Voice provider | should-fix | W2 | **fixed** | doc corrected — #1216 |
| DA-0077 | `AGENTS.md` :: Transit "Adding transit" checklist item 7 (~ | should-fix | W2 | **fixed** | migration ownership and CITY_PACKS extension guidance corrected and contract-tested — #1333 |
| DA-0078 | `BRINGUP.md` :: lines 988, 1045-1051, 1114-1115 (XVF firmwar | should-fix | W2 | **fixed** | doc corrected — #1216 |
| DA-0079 | `PLAN.md` :: "T5.1 — StartLimitAction=reboot (shipped PR  | should-fix | W2 | **fixed** | doc corrected — #1216 |
| DA-0080 | `PLAN.md` :: :239 (## Wake-word reliability — AEC tuning  | should-fix | W2 | **fixed** | stale threshold TODO replaced with shipped 0.30 contract and parity guard — #1334 |
| DA-0081 | `README.md` :: "What's installed and at what cost" table +  | should-fix | W2 | **fixed** | doc corrected — #1216 |
| DA-0082 | `SECURITY.md` :: "Current Security Model" paragraph (~line 40 | should-fix | W2 | **fixed** | doc corrected — #1216 |
| DA-0083 | `c/jts-ring-ioplug/pcm_jts_ring.c` :: :26-28 | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0084 | `capture-page/README.md` :: :31-44,66-78 | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0085 | `capture-page/README.md` :: :33-43 (Modules), :68-78 (Test) | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0086 | `deploy/lib/install/env-migrations.sh` :: migrate_grouping | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0087 | `docs/CHIP-AEC-EXPERIMENT.md` :: :369 (Plug-in contract table, row 4) | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0088 | `docs/CHIP-AEC-EXPERIMENT.md` :: :938 and :1059 (journalctl one-liner) | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0089 | `docs/HANDOFF-active-speaker-dsp.md` :: :223 (crossover-preview plug-in point) | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0090 | `docs/HANDOFF-aec.md` :: :1137-1156 (Open work streams, item C — "Dua | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0091 | `docs/HANDOFF-aec.md` :: :2277-2284 ("We haven't run the wake-word re | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0092 | `docs/HANDOFF-aec.md` :: :2301 (File map), also :1696 and :1742 (Less | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0093 | `docs/HANDOFF-audible-feedback.md` :: ## What's in the registry today | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0094 | `docs/HANDOFF-audio-graph-consolidation.md` :: ### A. USB ingress -> A3. Lean-FIFO lane row | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0095 | `docs/HANDOFF-barge-in.md` :: ## Config & observability -> Threshold: | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0096 | `docs/HANDOFF-control-plane-auth.md` :: :32 (TL;DR) and :368 (Audit result) | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0097 | `docs/HANDOFF-correction.md` :: :1219 | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0098 | `docs/HANDOFF-correction.md` :: Schema and version compatibility table :1852 | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0099 | `docs/HANDOFF-distributed-active.md` :: :855-857 and :950-952 | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0100 | `docs/HANDOFF-fan-in-daemon.md` :: "### Systemd unit (deploy/systemd/jasper-fan | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0101 | `docs/HANDOFF-homeassistant.md` :: "## File map" table, line 246: `jasper/cli/d | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0102 | `docs/HANDOFF-install-update-transaction.md` :: "The broken-vs-idle seam (Workstream C)" sec | should-fix | W2 | **fixed** | doc corrected — #1215 |
| DA-0103 | `docs/HANDOFF-mic-fusion-architecture.md` :: §2.2 "jasper/wake_legs.py — the leg registry | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0104 | `docs/HANDOFF-mic-quality-v2.md` :: "Current state — what's actually deployed (2 | should-fix | W2 | **fixed** | doc corrected — #1220 |
| DA-0105 | `docs/HANDOFF-multiroom.md` :: :535-536, :1063-1068 (cf. jasper/cli/doctor/ | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0106 | `docs/HANDOFF-peering.md` :: 2. Architecture in one diagram | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0107 | `docs/HANDOFF-peering.md` :: :519-526 | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0108 | `docs/HANDOFF-privilege-separation.md` :: :249, :458 | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0109 | `docs/HANDOFF-prompting.md` :: :27-28, :533-534 | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0110 | `docs/HANDOFF-remote-updates.md` :: :379-409 ("Integration points already in pla | should-fix | W2 | **fixed** | doc corrected — #1215 |
| DA-0111 | `docs/HANDOFF-remote-updates.md` :: :45-47, :410-411 (cf. .github/workflows/test | should-fix | W2 | **fixed** | doc corrected — #1215 |
| DA-0112 | `docs/HANDOFF-resilience.md` :: "#### What the rest of Stage 2 would still a | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0113 | `docs/HANDOFF-usb-low-latency.md` :: :84-87 ("Best values to keep for the Apple U | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0114 | `docs/HANDOFF-usbsink.md` :: :1111 (§4.8 "jasper-doctor checks") | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0115 | `docs/HANDOFF-usbsink.md` :: :177 (Executive summary) vs :15-16 (top call | should-fix | W2 | **fixed** | doc corrected — #1220 |
| DA-0116 | `docs/HANDOFF-voice-providers.md` :: :523 ("Idle anchor + tool rounds") | should-fix | W2 | **fixed** | doc corrected — #1217 |
| DA-0117 | `docs/HANDOFF-volume.md` :: :153-208 ('/state` gain-chain ledger' sectio | should-fix | W2 | **fixed** | doc corrected — #1215 |
| DA-0118 | `docs/HANDOFF-wake-training-experiment.md` :: §12 :1545 ("Phase 0a... not started") and Ch | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0119 | `docs/active-crossover-information-design.md` :: "Current Product Gaps" :296-300 | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0120 | `docs/audit-pending-followups.md` :: whole-file (no "Last verified:" anywhere); s | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0121 | `docs/barge-in-build-prompts.md` :: :69-320 (Step 2/3/4 prompt blocks) vs docs/H | should-fix | W2 | **fixed** | doc corrected — #1215 |
| DA-0122 | `docs/calibration-agent/README.md` :: :188 | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0123 | `docs/doc-map.toml` :: :646-659 (room-correction-and-calibration su | should-fix | W2 | **fixed** | doc corrected — #1220 |
| DA-0124 | `docs/doc-map.toml` :: [[subsystem]] id = "audio-routing-and-render | should-fix | W2 | **fixed** | doc corrected — #1220 |
| DA-0125 | `docs/doc-map.toml` :: [[subsystem]] id = "room-correction-and-cali | should-fix | W2 | **fixed** | doc corrected — #1220 |
| DA-0126 | `docs/doc-map.toml` :: multiroom-grouping stanza requires_docs_when | should-fix | W2 | **fixed** | doc corrected — #1220 |
| DA-0127 | `docs/dumb-endpoint-bringup.md` :: L93-95, L111-120, L384-406, L480-556 (vs. th | should-fix | W2 | **fixed** | doc corrected — #1215 |
| DA-0128 | `docs/satellites.md` :: :552, :588, :606 | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0129 | `docs/satellites.md` :: Test harness shape (Pi-side scaffolding), ~l | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0130 | `jasper/active_speaker/web_commissioning.py` :: _play_summed_commission_tone | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0131 | `jasper/audio_measurement/__init__.py` :: :13-26 | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0132 | `jasper/calibration_agent/response.py` :: _correction_bounds | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0133 | `jasper/camilla.py` :: crossover_controller | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0134 | `jasper/cli/doctor/aec.py` :: _AEC_DRIFT_WARN_THRESHOLD | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0135 | `jasper/cues/cli.py` :: _make_manager :46 (also module docstring :19 | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0136 | `jasper/cues/generator.py` :: module docstring :19-20 | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0137 | `jasper/google_creds.py` :: save_token | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0138 | `jasper/multiroom/__init__.py` :: Module layout | should-fix | W2 | **fixed** | doc corrected — #1218 |
| DA-0139 | `jasper/research/scheduler.py` :: module docstring | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0140 | `jasper/voice/openai_session.py` :: DEFAULT_TEMPERATURE / OpenAIRealtimeConnecti | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0141 | `jasper/web/correction_setup.py` :: module docstring :24-49 vs _POST_ROUTES :181 | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0142 | `jasper/web/rooms_setup.py` :: _peer_label (~line 268-275), _discover_speak | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0143 | `relay/README.md` :: :33-41 (Endpoints table) | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0144 | `rust/jasper-dual-dac-lab/Cargo.toml` :: :12-14 | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0145 | `rust/jasper-dual-dac-lab/src/main.rs` :: :10 | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0146 | `rust/jasper-fanin/src/impulse_tap.rs` :: MAX_EVENTS_CEILING / AUTO_DISARM_MIN_CEILING | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0147 | `rust/jasper-resampler/examples/golden_vector.rs` :: :5-26 (module doc) | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0148 | `rust/jasper-resampler/src/lib.rs` :: :25 (Provenance) / :100 / resample_i16 :718  | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0149 | `scripts/_dtln_aec_offline.py` :: main():82-86 | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0150 | `tests/voice_eval/README.md` :: "Architecture in 60 seconds" — traced_regist | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0151 | `tests/voice_eval/README.md` :: "Cost notice — read first" table / harness.p | should-fix | W2 | **fixed** | doc corrected — #1219 |
| DA-0427 | `AGENTS.md` :: "Page behaviour ships as static ES modules"  | nit | W2 | **fixed** | doc corrected — #1216 |
| DA-0428 | `CONTRIBUTING.md` :: "Tests" section, Rust audio-daemon gate bull | nit | W2 | **fixed** | doc corrected — #1216 |
| DA-0429 | `CONTRIBUTING.md` :: "Voice-eval suite" bullet (~line 174-177) | nit | W2 | **fixed** | doc corrected — #1216 |
| DA-0430 | `README.md` :: "jasper-web alone hosts fourteen URL surface | nit | W2 | **fixed** | doc corrected — #1216 |
| DA-0431 | `README.md` :: :521 (## Documentation map), :550 | nit | W2 | **fixed** | doc corrected — #1216 |
| DA-0432 | `deploy/bin/jasper-camilla-recover` :: usage() (:68) vs. load_core_graph_park_units | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0433 | `deploy/install.sh` :: :7 | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0434 | `deploy/jasper-web.service` :: route-map comment above ExecStart | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0435 | `deploy/systemd/jasper-fanin.service` :: :77 | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0436 | `docs/CHIP-AEC-EXPERIMENT.md` :: :385-388 (Policy carve-out callout) | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0437 | `docs/HANDOFF-active-speaker-dsp.md` :: :1695-1708 (Last verified footer) | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0438 | `docs/HANDOFF-aec.md` :: :1872-2017 ("What we found about chip-side A | nit | W2 | **fixed** | doc corrected — #1220 |
| DA-0439 | `docs/HANDOFF-audible-feedback.md` :: :263 | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0440 | `docs/HANDOFF-audio-capability-platform.md` :: ## Observability Requirements | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0441 | `docs/HANDOFF-barge-in.md` :: ## Config & observability -> Doctor: / event | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0442 | `docs/HANDOFF-calibration-agent.md` :: ## Provider selection (through L827) / ANTHR | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0443 | `docs/HANDOFF-chip-aec-portability.md` :: :5-7 (also README.md:647) | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0444 | `docs/HANDOFF-chip-aec-portability.md` :: :83 (cites rust/jasper-outputd/src/state.rs: | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0445 | `docs/HANDOFF-correction-revision-plan.md` :: §3.4, provider bullet | nit | W2 | **fixed** | doc corrected — #1215 |
| DA-0446 | `docs/HANDOFF-homeassistant.md` :: "## File map" table, lines 253-256 (test cou | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0447 | `docs/HANDOFF-multiroom.md` :: :2365-2367 (cf. :58-59) | nit | W2 | **fixed** | doc corrected — #1220 |
| DA-0448 | `docs/HANDOFF-persistent-live-session.md` :: :144 | nit | W2 | **fixed** | doc corrected — #1220 |
| DA-0449 | `docs/HANDOFF-pricing-editor.md` :: :389 (File touchpoints) | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0450 | `docs/HANDOFF-remote-updates.md` :: "Failure surface + rollback strategy" (:415- | nit | W2 | **fixed** | doc corrected — #1215 |
| DA-0451 | `docs/HANDOFF-transit-citibike.md` :: "## Testing" section, "tests/test_citibike.p | nit | W2 | **fixed** | doc corrected — #1217 |
| DA-0452 | `docs/HANDOFF-volume-control-redesign.md` :: :1-13 (top Status callout) | nit | W2 | **fixed** | doc corrected — #1220 |
| DA-0453 | `docs/HANDOFF-wake-telemetry.md` :: :40 ("Companion docs" bullet) | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0454 | `docs/audit-pending-followups.md` :: :83-87 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0455 | `docs/calibration-agent/targets/house-curves.md` :: L19-22 'What JTS Does Today' | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0456 | `docs/doc-map.toml` :: :183-231 (audio-routing-and-renderers `code` | nit | W2 | **fixed** | doc corrected — #1220 |
| DA-0457 | `docs/multi-user-spotify.md` :: end of file (no footer) | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0458 | `firmware/dial/include/config.h` :: VOLUME_STEP_DB | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0459 | `firmware/dial/src/main.cpp` :: :1 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0460 | `jasper/active_speaker/__init__.py` :: :5-11 (module docstring) | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0461 | `jasper/active_speaker/_common.py` :: module docstring:14 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0462 | `jasper/active_speaker/runtime_contract.py` :: :7 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0463 | `jasper/active_speaker/startup_load.py` :: module docstring, lines 5-12 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0464 | `jasper/bluetooth/__init__.py` :: :12 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0465 | `jasper/calibration_agent/__init__.py` :: :5 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0466 | `jasper/cli/doctor/privsep.py` :: OUT_OF_SCOPE scope comment / module docstrin | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0467 | `jasper/fanin/coupling_auto.py` :: :122 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0468 | `jasper/fanin/coupling_reconcile.py` :: :1257 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0469 | `jasper/fanin/coupling_reconcile.py` :: reconcile_coupling | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0470 | `jasper/librespot_state.py` :: session_active | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0471 | `jasper/multiroom/reconcile.py` :: module docstring:24-27 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0472 | `jasper/mux.py` :: :33 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0473 | `jasper/peering/uds.py` :: :162 | nit | W2 | **fixed** | doc corrected — #1218 |
| DA-0474 | `jasper/sound/profile.py` :: estimate_headroom_db | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0475 | `jasper/spotify_routing.py` :: :11 | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0476 | `jasper/transit/__init__.py` :: :72 | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0477 | `jasper/transit/base.py` :: ProviderKind | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0478 | `jasper/transit/providers/nyc_bus.py` :: enumerate_live_routes | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0479 | `jasper/transit/state.py` :: read_state | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0481 | `jasper/voice/trace.py` :: :18-21 (module docstring) | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0482 | `jasper/wake_corpus/bridge_session.py` :: :109 | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0483 | `jasper/web/_common.py` :: write_env_file | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0484 | `jasper/web/pair_flow.py` :: resolve_pair :33 | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0485 | `jasper/web/rooms_setup.py` :: module docstring, lines 13 and 33 ('docs/HAN | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0486 | `jasper/web/sound_setup.py` :: Handler.do_GET | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0487 | `jasper_aec3/setup.py` :: _absl_via_pkgconfig():95-129 | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0488 | `rust/jasper-fanin/src/config.rs` :: module doc comment (:5-9) | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0489 | `rust/jasper-fanin/src/mixer.rs` :: DirectTapHook::tap_over_read :3075-3082 | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0490 | `rust/jasper-host-clock/Cargo.toml` :: :9-13 | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0491 | `rust/jasper-outputd/src/shm_ring_source.rs` :: module doc / pub mod shm_ring_source | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0492 | `rust/jasper-ring/Cargo.toml` :: package doc comment / description | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0493 | `scripts/chip-aec-capture-comparison.sh` :: daemon_set_mode comment, :68-70 | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0494 | `scripts/ring-proto/make-camilla-ring-config.sh` :: :44-48 | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0495 | `tests/test_doctor.py` :: :5 | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0496 | `tests/test_usbsink_volume_bridge.py` :: test_read_switch_value_returns_muted_on_unpa | nit | W2 | **fixed** | doc corrected — #1219 |
| DA-0022 | `jasper/usbsink/output_mode_reconcile.py` :: set_output_mode | should-fix | W5 | **mooted** | main deleted the file — #1200/#1209 |
| DA-0368 | `jasper/usbsink/state_publisher.py` :: _write_state | nit | W5 | **mooted** | main deleted the file — #1200/#1209 |
| DA-0415 | `rust/jasper-usbsink-audio/src/impulse_tap.rs` :: TapState::arm / positive_u64 | nit | W5 | **mooted** | main deleted the file — #1200/#1209 |
| DA-0480 | `jasper/usbsink/audio_bridge.py` :: :34 | nit | W2 | **mooted** | main deleted the file — #1200/#1209 |
| DA-0558 | `jasper/usbsink/state_publisher.py` :: _atomic_write | nit | W3 | **mooted** | main deleted the file — #1200/#1209 |
| DA-0594 | `rust/jasper-usbsink-audio/src/impulse_tap.rs` :: TAP_PATH_DIR / DEFAULT_TAP_PATH | nit | W3 | **mooted** | main deleted the file — #1200/#1209 |
| DA-0595 | `rust/jasper-usbsink-audio/src/impulse_tap.rs` :: json_escape_str | nit | W3 | **mooted** | main deleted the file — #1200/#1209 |
| DA-0626 | `tests/test_mux_lean_lane.py` :: _FakeHandoff / _FakeVolumeCoordinator / patc | nit | W3 | **mooted** | main deleted the file — #1200/#1209 |

## Duplication consolidation clusters (169)

- **Hand-rolled atomic file writes → jasper.atomic_io.atomic_write_text** [adopt-existing-helper/M] -> `jasper/atomic_io.py::atomic_write_text (docstring: canonical impl; supports mode= and group_from_parent=)` — 13 sites — Highest payoff: 13 sites, a documented canonical helper already exists, and the drift is a real permission-hardening bug. One mechanical PR per 2-3 files keeps each reviewable.
- **Per-file _read_json body readers → shared _common helper** [extract-shared-helper/M] -> `new read_json_body() in jasper/web/_common.py` — 12 sites — ~12 near-identical readers with drifted DoS caps and inconsistent hardening; _common.py is the house home for wizard primitives. High payoff, per-file migration is safe.
- **fanin control-socket path** [single-constant/M] -> `one FANIN_CONTROL_SOCKET honoring JASPER_FANIN_CONTROL_SOCKET override (as jasper/mux.py does)` — 8 sites — One socket path hardcoded in ~9 files with inconsistent env-override support; a shared constant makes the override uniform.
- **Rust SSOT/twin findings already pinned by contract/guard tests or deliberately documented — no action** [leave-it/S] -> `existing canonical + existing test that pins it; keep as-is` — 8 sites — Verifier notes on each confirm the duplication is real but load-bearing-inert: existing tests or explicit in-code deferrals already hold the SSOT contract. Consolidating is gold-plating.
- **CLI logging.basicConfig → setup_cli_logging()** [extract-shared-helper/S] -> `new setup_cli_logging() in a shared jasper module` — 7 sites — Identical basicConfig format string copied across 7 CLI entry points with no shared helper anywhere in jasper/.
- **Hand-rolled JSON string escaper reimplemented 7× across 4 rust crates, already drifted (0x20 vs is_control)** [extract-shared-helper/M] -> `new jasper_tts_protocol::json escape_json (adopt the is_control() variant — superset that also escapes DEL/C1); within jasper-fanin hoist to one module shared by state.rs+xrun_log.rs` — 7 sites — Not audio-path, but 7 copies of one escaper with two divergent control-char rules is a classic SSOT hazard; tts-protocol is already a shared dep of all callers.
- **KEY=VALUE env-file parsers → jasper.env_load.parse_env_file** [adopt-existing-helper/S] -> `jasper/env_load.py::parse_env_file / parse_env_text` — 6 sites — Stdlib-only canonical parser already exists and identity_state.py uses it correctly; the 'avoids the import' justification doesn't hold. Consolidation removes a real quote-handling divergence.
- **renderer active-flag key strings (SOURCE_TO_ACTIVE_KEY dead SSOT)** [adopt-existing-helper/M] -> `jasper/music_sources.py::SOURCE_TO_ACTIVE_KEY / MusicSourceSpec.renderer_active_key` — 6 sites — The SSOT was explicitly added ('not a scavenger hunt') but is dead while 5 files hardcode the exact strings it centralizes; adopting it fulfills the original intent.
- **Destructive rm-rf safety guard (_safe_to_remove_output/_non_empty/marker sniff) reimplemented in 6 wake-training scripts** [extract-shared-helper/M] -> `new scripts/_wake_pipeline_common.py parametrized on owner-dir, subdir name, marker predicate` — 6 sites — 6 near-identical copies of a delete-safety guard — exactly the kind of invariant that must not silently diverge. Each has its own test proving intent; fold into one helper.
- **Shell cross-reference / local-idiom duplications where a comment or local dedup beats a new abstraction** [leave-it/S] -> `cross-reference comments / local scope; no new shared home` — 6 sites — Six singletons where extraction costs more than it saves; a comment or a one-line local fix is cleaner than a new abstraction for 2 call sites.
- **5 scripts hand-roll the legacy PI_HOST/PI_USER default chain instead of sourcing scripts/_lib.sh** [adopt-existing-helper/S] -> `scripts/_lib.sh (already sourced by switch-voice-provider.sh:33 and scripts/use:28)` — 5 sites — _lib.sh's own comment documents this as legacy debt awaiting migration; two siblings already migrated. Straightforward `. _lib.sh` adoption.
- **Bounded env-var reader _env_float/_env_int** [extract-shared-helper/M] -> `new env_float/env_int in a shared jasper module (e.g. jasper/env_load.py)` — 4 sites — Byte-identical parse/range-check/default logic in 3-4 files; a single validated reader removes silent drift in tuning thresholds.
- **_finite_float duplicate helper (active_speaker)** [extract-shared-helper/S] -> `jasper/active_speaker/_common.py::finite_float` — 4 sites — Identical `_finite_float(value)->float|None` in 4+ package files; _common.py already exists as the house location for such primitives.
- **CamillaController-from-env factory _camilla()** [extract-shared-helper/S] -> `new shared _camilla() in jasper/multiroom/ (config or a shared module)` — 4 sites — 6 byte-identical copies with docstrings cross-referencing each other, past rule-of-three; the mutual cross-refs prove intent to share.
- **aplay child-kill terminate/kill/swallow pattern** [extract-shared-helper/S] -> `local _terminate_aplay(proc) helper in jasper/web/sound_setup.py` — 4 sites — Same terminate→kill→swallow-ProcessLookupError sequence copied 7x in one file with inconsistent except clauses; a single helper fixes the drift.
- **volume_coordinator push-failed guard/record/log block** [extract-shared-helper/M] -> `local helper in jasper/volume_coordinator.py` — 4 sites — 'push failed→guard camilla→record+log' block repeated near-verbatim 4x across two methods.
- **OutputTopology fixture builder (bench_mono / mono_2way)** [extract-shared-helper/L] -> `tests/conftest.py :: mono_2way_topology(**kwargs) (or reuse test_active_speaker_startup_load._topology as the seed)` — 4 sites — Highest payoff: one builder collapses ~15 hand-rolled copies plus T006's re-inlined dict. conftest.py is the honored home and several sibling files already import a peer _topology, so a canonical builder is clearly warranted.
- **Web-wizard BaseHTTPRequestHandler fake (_FakeHandler / no-socket handler driver)** [extract-shared-helper/M] -> `tests/_web_test_helpers.py :: superset FakeHandler + handler-driver scaffold` — 4 sites — _web_test_helpers.py already exists as this suite's shared web-helper home yet 7+ files reinvent the same socketless handler. Extract only the common BytesIO stub; leave the deliberately-different real-handler bypasses alone.
- **Two single-value drifts: streambox nginx /wifi/ timeout 60s (vs documented 120s) and arm-ring-a.sh forwards wrong capture-device env var** [align-drifted-twins/S] -> `nginx-jasper.conf:271 (120s, documented worst-case ~50s); arm.sh:465 forwarding pattern (JASPER_RING_PROTO_ALSA_DEVICE)` — 4 sites — Two independent should-fix single-value drifts with concrete failure modes; grouped as quick copy-the-canonical-value edits, no abstraction.
- **Audio-measurement scripts reinvent WAV open/validate + RMS/peak dBFS + read_wav_mono** [extract-shared-helper/L] -> `jasper/wake_training/feature_bank.py read_wav_int16 (existing, already reused by 2 sibling scripts) for loaders; new scripts/_wav_stats.py for the identical rms()/dBFS quick-stats` — 4 sites — Family of read-WAV/compute-stats reimplementations; extract the truly-identical rms + quick-stats, adopt the existing read_wav_int16 where shapes match, leave divergent loaders. Largest effort, moderate payoff.
- **_clamp_int/_bounded_int duplicate helper (active_speaker)** [extract-shared-helper/S] -> `jasper/active_speaker/_common.py::clamp_int` — 3 sites — Three byte-identical copies of the same clamp scattered across the package; _common.py already hosts shared active_speaker primitives (issue, etc.).
- **Paste-key setup helpers _value_for/_KEY_VALID_RE/_mask_key** [extract-shared-helper/S] -> `jasper/web/_common.py (add _value_for/_KEY_VALID_RE; reuse existing mask_secret)` — 3 sites — Byte-identical value/validator helpers across 3 setup wizards plus a redundant mask wrapper; _common.py already owns mask_secret.
- **Private-IPv4 LAN/SSRF predicate** [extract-shared-helper/M] -> `one shared is_private_lan_ipv4() (jasper/multiroom/config.py or a net util)` — 3 sites — config.py's own comment already acknowledges the overlap ('same as peer_addr'); one predicate keeps the SSRF rule and the persisted-value validators from disagreeing.
- **JSON-schema validation micro-DSL (_require_id/_bool/_enum/_float…)** [extract-shared-helper/L] -> `new jasper module hosting the id/text/bool/enum/float validators + _ID_RE` — 3 sites — A whole validation DSL reimplemented ~3x; worth a shared module, but the divergent third copy makes this the highest-effort cluster — split the PR.
- **outputd-cutover-ring.yml config-name (cross-language)** [single-constant/S] -> `one RING_FLAT_CONFIG_NAME, with deploy/install.sh pinned by a test bridge` — 3 sites — One filename with 3 sources of truth incl a shell script; the Python constant is even declared-but-unused. Consolidate Python + test-pin the shell copy.
- **session sweep PREPARING→SWEEPING state transition** [extract-shared-helper/M] -> `local _run_sweep_transition() in jasper/correction/session.py` — 3 sites — Identical state-transition sequence reimplemented 3x differing only in string labels and terminal state.
- **bridge_session write-env/restart/restore block** [extract-shared-helper/M] -> `local helper in jasper/wake_corpus/bridge_session.py` — 3 sites — ~25-30 line 'write env→restart→on-fail restore+retry→warn→re-raise' block repeated 3x differing only in extra units and a log word.
- **spotify 'Connect not linked' error dict** [extract-shared-helper/S] -> `local helper in jasper/tools/spotify.py` — 3 sites — Same 'Spotify Connect isn't linked' dict copied 3x; one helper.
- **active-speaker capture-preset 3-tier resolution** [extract-shared-helper/M] -> `one resolve_active_speaker_capture_preset() in jasper/active_speaker/` — 3 sites — Same 3-tier capture-preset fallback hand-rolled in 3 files (the 'pair 4/5 circled but never named'); one resolver removes the divergence.
- **correction status payload builders repeat session.X keys** [extract-shared-helper/M] -> `one shared payload-assembly helper in jasper/correction/status.py` — 3 sites — ~20 identical session.X mappings repeated across three payload builders; a shared base dict keeps them from drifting.
- **systemd Key=Value unit-directive parser (_value_for)** [extract-shared-helper/M] -> `tests/_systemd_unit_helpers.py :: value_for(unit_text, key)` — 3 sites — 7 near-identical copies with silent semantic drift is exactly the case for one documented helper. Migrate the identical stripping copies; reconcile the last-wins ones in a follow-up so drift stops.
- **Wake feature-bank scaffolding (FakeExtractor/_write_wav/_write_bundle)** [extract-shared-helper/M] -> `tests/_wake_feature_bank_fixtures.py (mirror correction_bundle_fixtures.py)` — 3 sites — Three near-copies of fixture scaffolding is the textbook shared-fixtures case, with an existing mirror to follow.
- **AF_UNIX short-socket-path helper (sun_path length workaround)** [extract-shared-helper/S] -> `tests/conftest.py :: short_sock_path fixture (keep macOS sun_path comment)` — 3 sites — Same OS-workaround copied verbatim in 5 files; conftest.py is the natural single home.
- **AF_UNIX single-shot JSON status-socket server** [extract-shared-helper/M] -> `one parameterizable serve_status_json_socket() helper (tests/ shared module)` — 3 sites — Three from-scratch reimplementations of 'serve one JSON payload over a short-lived unix socket'; two are cleanly shareable, the third is intentionally timing-specific.
- **Gemini fake-SDK response dataclasses (_SC/_Resp)** [extract-shared-helper/M] -> `tests/_gemini_test_helpers.py (connection keeps its superset fields)` — 3 sites — Mirrors the _web_test_helpers.py precedent; one canonical dataclass prevents three fakes from drifting apart, though leaving it is acceptable given size.
- **Mux test doubles/fixtures (_FakeHandoff/_FakeVolumeCoordinator/patched_probes/_stub_probes)** [extract-shared-helper/M] -> `tests/_mux_fakes.py (or conftest.py); test_mux.py keeps its richer superset` — 3 sites — Two files carry byte-identical fixtures while the main file holds a superset; move the shared 55 lines, leave the richer copy.
- **Shared UI primitives redefined per-page in CSS instead of app.css (toggle switch, spinner, design tokens)** [extract-shared-helper/M] -> `deploy/assets/app.css (documented single source for cross-page components/tokens)` — 3 sites — Three CSS SSOT violations against app.css's own stated layering rule; #41 is a live visible regression, spinner is 4 copies, tokens silently never resolve. All resolve to app.css.
- **jts_ring_shm.c writer-open and reader-open create/attach/torn-reclaim paths duplicated near-verbatim** [extract-shared-helper/M] -> `shared static helpers in c/jts-ring-ioplug/jts_ring_shm.c (ftruncate+mmap+header-init+magic-publish; fstat+mmap+wait_for_magic+geometry-check; 8-attempt retry loop) filling common fields via out-params` — 3 sites — reader-side comment literally says 'byte-identical to init_created'; reader_fill_common already models the out-param pattern. Real but delicate; validate on hardware.
- **JS: three sites should adopt existing shared modules instead of inlining (HTTP error-extraction, tools onToggle/onApply, LEG_LABELS py->JS)** [adopt-existing-helper/M] -> `deploy/assets/shared/js/http.js parseResponse (export for GET); a shared tools/js helper parameterized by base path; the wake-corpus-config JSON island seeds LEG_LABELS` — 3 sites — All three have an existing shared home or drift-proof island pattern already half-applied; adopting them removes near-verbatim JS and copy that has already visibly drifted.
- **Stale membership/topology docs: jts-audio.slice header + HANDOFF-resilience OOM ladder, and AGENTS.md Wi-Fi guardian restates HANDOFF** [align-drifted-twins/M] -> `docs/HANDOFF-resilience.md as the incident/topology SSOT; slice header and AGENTS.md link to it` — 3 sites — Three under-enumerations of the current audio-slice membership plus a 120-line doc restatement violating AGENTS.md's own SSOT rule; refresh + link.
- **JS: local micro-dedups within wizard/report/test files (action bracket, li-list mapper, summedSummary, repeated ternary)** [extract-shared-helper/M] -> `per-file local helpers: runActiveSpeakerAction() in sound-profile/js/main.js; a small <li>-list mapper + hoisted var in correction/js/main.js; hoist summedSummary() in the harness` — 3 sites — Cluster of local, low-value-but-real templating dupes; each stays within its file. Grouped so a maintainer does them in one JS pass.
- **firmware toolchain/boilerplate: platformio board block + build.sh brick paragraph + pio resolver + postJson/postVolumeAdjust** [leave-it/S] -> `deliberate toolchain-parity coupling (documented in both .ini files); optional PlatformIO extra_configs base [env] and a local jasperPost() helper if these areas are reworked` — 3 sites — Documented-deliberate coupling plus low-value boilerplate; not worth new build-system indirection now. (Contrast with discovery.cpp, split into its own extract cluster.)
- **_round(value,digits) duplicate helper** [extract-shared-helper/S] -> `one shared _round in jasper/correction/ (or _common)` — 2 sites — Two byte-identical copies in the same correction package; trivial dedup.
- **busctl subprocess wrapper** [extract-shared-helper/M] -> `new _busctl() helper (e.g. jasper/dbus_util.py or jasper/mux.py exported)` — 2 sites — Same spawn-busctl-2s-timeout pattern reimplemented 8x; a single helper removes drift risk in system-bus calls.
- **UDS socket-command client (control/uds)** [extract-shared-helper/S] -> `one _socket_command() in jasper/control/uds.py` — 2 sites — ~10 lines of identical connect/write/drain/readline/close boilerplate in the same file, differing only in timeout and message.
- **JSON-response boilerplate → _common.send_proxy_json** [adopt-existing-helper/S] -> `jasper/web/_common.py::send_proxy_json` — 2 sites — send_proxy_json already exists and sibling wake_setup.py uses it; wifi_setup's copy is byte-identical.
- **web _systemctl/_unit_active subprocess wrappers** [extract-shared-helper/S] -> `new _systemctl/_unit_active in jasper/web/_common.py` — 2 sites — Two web files each define their own systemctl wrapper with divergent semantics; _common.py is the house home.
- **_redirect legacy ?msg→flash-cookie shim** [extract-shared-helper/S] -> `new _common.py redirect_with_flash() helper` — 2 sites — ~20-line compat shim duplicated between two OAuth setup flows; _common.py is the wizard-primitive home.
- **Bounded-body/GET proxy request boilerplate (rooms_setup)** [extract-shared-helper/S] -> `local helper in jasper/web/rooms_setup.py` — 2 sites — Two same-file functions share ~10 lines of request-build + except boilerplate before diverging; a one-line local helper suffices.
- **systemd `Environment` output parser** [extract-shared-helper/S] -> `one shared parse_systemctl_environment() (jasper/env_load.py)` — 2 sites — Two parsers for `systemctl show -p Environment`; the shlex one is correct, the split one is buggy — consolidating fixes a latent parse bug.
- **arg-or-env-or-default statefile path resolver** [extract-shared-helper/S] -> `one shared statefile-path reader` — 2 sites — Same arg||env||default resolution idiom reimplemented across siblings; one reader prevents path drift.
- **active-speaker role-pair dict** [single-constant/S] -> `one shared ROLE_PAIRS in jasper/active_speaker/` — 2 sites — Two byte-identical dicts maintained independently with no pin test; one constant removes silent-divergence risk in crossover config.
- **correction_substream ALSA device constant** [single-constant/S] -> `one module constant in jasper/web/sound_setup.py` — 2 sites — Two differently-named constants hardcode the identical `correction_substream` literal; a rename of one silently diverges.
- **NYC metro bounding box constant** [single-constant/S] -> `one NYC_BBOX (e.g. jasper/transit/providers/ shared)` — 2 sites — Identical BoundingBox literal in two providers with an in-comment cross-ref and no equality test; one constant is trivially safer.
- **pair_balance_trim filter-name constant** [single-constant/S] -> `the existing emitter/verifier SSOT for the driver-domain filter name` — 2 sites — A third, untested copy of a filter name that the emitter/verifier already guard with a round-trip test; the consumer can silently drift.
- **AEC wake-leg default gains** [single-constant/M] -> `one _LEG_DEFAULT_RAW SSOT in jasper/control/aec_endpoints.py` — 2 sites — Three copies with a comment asserting they must agree but no test enforcing it — exactly the drift the comment fears.
- **Spotify OAuth redirect-URI base** [single-constant/M] -> `one SPOTIFY_OAUTH_CALLBACK_BASE constant` — 2 sites — 5 independent copies of a value that must be byte-exact for OAuth to work; centralizing removes a real breakage mode.
- **Open-turn + drain-acquire-buffer sequence (voice_daemon)** [extract-shared-helper/M] -> `local helper in jasper/voice_daemon.py` — 2 sites — Identical begin_turn_output_episode→chirp→begin_turn→drain_acquire_buffer sequence written twice with near-identical bookkeeping.
- **correction_setup byte-identical accept functions** [delete-redundant-copy/S] -> `jasper/web/correction_setup.py (keep one, call it from both)` — 2 sites — Two functions with byte-identical bodies in the same file.
- **correction_setup 'refuse if room-correction active' guard** [extract-shared-helper/S] -> `local guard helper in jasper/web/correction_setup.py` — 2 sites — Same active-session refusal guard copied verbatim between two dispatch branches in one file.
- **server.py config-constant copies + _sync_*_module** [leave-it/L] -> `n/a (architectural — submodules delegate but server keeps master copies)` — 2 sites — Genuine two-names-one-fact hazard, but the fix is an architectural ownership inversion, not a small PR; flag for a dedicated effort, not this consolidation pass.
- **audio_runtime_plan knob resolvers (_resolve_profile_floor_int / _resolve_outputd_content_buffer_int)** [extract-shared-helper/M] -> `one parametrized resolver in jasper/audio_runtime_plan.py` — 2 sites — Two ~110-line near-duplicate resolvers with identical override/precedence/warning shape.
- **session on_capture_uploaded identical tails** [extract-shared-helper/S] -> `local helper in jasper/correction/session.py` — 2 sites — Two upload handler tails are byte-identical logic; extract the shared tail.
- **coupling_reconcile _recover_to_loopback vs _disarm** [extract-shared-helper/S] -> `shared daemon-op sequence in jasper/fanin/coupling_reconcile.py` — 2 sites — Recovery tail reimplements _disarm's 3-call sequence but drops per-daemon failure detail, so double-failures log an opaque recovered=False.
- **transport_pipe low-latency budget math** [extract-shared-helper/M] -> `shared helper/constants (jasper/fanin/ or a shared audio module)` — 2 sites — Same activation-gate budget math near-verbatim in reconciler and doctor with an in-code admission of the duplication.
- **coupling_reconcile _arm/_arm_ring failure block** [extract-shared-helper/M] -> `local _fail_and_recover() helper in jasper/fanin/coupling_reconcile.py` — 2 sites — 'run check→on fail recover+log+return CouplingResult(ok=False)' block repeated 11x; a helper taking the check + strings collapses it.
- **aec_bridge capture threads L+R→mono→resample→HPF pipeline** [adopt-existing-helper/M] -> `shared pipeline helper + existing jasper/cli/aec_bridge.py::_DropLogDebouncer` — 2 sites — ~35 lines of identical mono/resample/HPF logic in two threads, plus a hand-rolled debouncer the file's own class already implements and uses elsewhere.
- **ConnectionState enum + _noisy_transitions (openai/gemini sessions)** [extract-shared-helper/S] -> `jasper/voice/_supervisor.py (already holds provider-agnostic reconnect primitives)` — 2 sites — 8-member enum + frozenset copied verbatim between two providers when _supervisor.py already exists for exactly these provider-agnostic primitives.
- **openai_session test-only private wrappers** [delete-redundant-copy/S] -> `call the public twins directly from tests` — 2 sites — One-line wrappers around their own public twins, called only by tests — dead production surface.
- **tweeter-protection safety assertion (staging off/on-device)** [extract-shared-helper/M] -> `local shared assertion in jasper/active_speaker/staging.py` — 2 sites — ~30-line tweeter-protection assertion copied verbatim between the two safety gates; divergence could let one gate pass a config the other rejects.
- **staging classify+validate config sequence** [extract-shared-helper/M] -> `local shared step in jasper/active_speaker/staging.py` — 2 sites — ~50-line classify→ceiling-gate→fold-issues→validate sequence duplicated near-verbatim between two top-level entry points.
- **_bluez_alsa_active_transport_path duplicate** [adopt-existing-helper/S] -> `jasper/volume_coordinator.py (import into volume_observers, which already imports from it)` — 2 sites — Byte-for-byte duplicate where the import edge already exists — just delete the copy and import.
- **reconcile fall-back-to-solo block** [extract-shared-helper/S] -> `local helper in jasper/multiroom/reconcile.py` — 2 sites — Byte-identical solo-fallback block duplicated at two fail-safe gates in main().
- **reconcile _unit_is_enabled/_unit_is_active** [delete-redundant-copy/S] -> `one _unit_state(verb) in jasper/multiroom/reconcile.py` — 2 sites — Two functions identical but for 'is-enabled' vs 'is-active' — parametrize the verb.
- **output_topology set_channel mutator skeleton** [extract-shared-helper/S] -> `local _replace_channel() in jasper/output_topology.py` — 2 sites — Both mutators duplicate an identical find-match-then-rebuild-tuple skeleton including the match comprehension.
- **OutputTopology.status hand-synced vs derived** [align-drifted-twins/M] -> `derive status structurally from evaluate_output_topology() (drop the stored field)` — 2 sites — A stored status is a second representation kept correct only by convention (mutators resetting to draft); structural derivation removes the convention dependency.
- **camilla_yaml correction-validation clamp block** [extract-shared-helper/S] -> `local helper in jasper/active_speaker/camilla_yaml.py` — 2 sites — 21-line correction-validation clamp block byte-for-byte duplicated between two emitters.
- **_oom_adj EXPECTED SSOT not pinned by CI** [align-drifted-twins/S] -> `add a test tying .service unit files to jasper/_oom_adj.py::EXPECTED` — 2 sites — Module docstring calls EXPECTED the SSOT and warns .service files 'MUST be updated separately', but nothing enforces it and tests re-hardcode the literals.
- **airplay mode env parser _read_mode vs _current_mode** [align-drifted-twins/S] -> `one shared airplay-mode reader` — 2 sites — Two independent parsers of airplay_mode.env with divergent fail-safe defaults; one reader removes the disagreement.
- **evidence _position_summary position_count key mismatch** [align-drifted-twins/S] -> `read the actual key written by artifacts.py::write_position_analysis_json` — 2 sites — A reader and writer disagree on the on-disk key so a real reported field is silently always None — a genuine bug, not just style.
- **design_draft _normalise_driver / _normalise_manual_driver** [extract-shared-helper/M] -> `shared normalizer in jasper/active_speaker/design_draft.py` — 2 sites — Two near-duplicate driver normalizers differing only in three axes; a parametrized normalizer removes the drift.
- **driver_protection auto_level_decision duplicate branches** [delete-redundant-copy/S] -> `merge the branches in jasper/active_speaker/driver_protection.py` — 2 sites — Two branches duplicate the identical decision tree verbatim; collapse with reason-string params.
- **calendar/gmail identical error text** [extract-shared-helper/S] -> `one shared error-text constant/helper in jasper/tools/` — 2 sites — Two hand-synced copies of identical error text with no SSOT.
- **MPRIS metadata parsing (transport vs renderer)** [extract-shared-helper/M] -> `one MPRIS metadata parser (shared module)` — 2 sites — Same MPRIS Metadata property parsed by two hand-written regex sets; a shared parser prevents field-extraction drift.
- **tool_state dead write_disabled_* + inline reimpl** [adopt-existing-helper/M] -> `jasper/tool_state.py::write_disabled_tools/write_disabled_packs (make tools_setup call them)` — 2 sites — Canonical writers exist but are dead while their only caller reimplements them inline at 4 sites.
- **shairport polling-supervisor lifecycle scaffolding** [extract-shared-helper/L] -> `a shared supervisor base/mixin` — 2 sites — Cold-start/jittered-loop/crash-isolation/dedicated-loop-thread scaffolding duplicated near-verbatim across 3 supervisor modules — worth a base class but high-effort.
- **CurveJSON attr-or-dict coercion preamble** [extract-shared-helper/S] -> `one curve-coercion helper in jasper/calibration_agent/` — 2 sites — Same attr-or-dict CurveJSON coercion reimplemented twice and already divergent.
- **_dbfs conversion helper** [extract-shared-helper/S] -> `jasper/audio_measurement/quality.py::_dbfs (or a shared audio util)` — 2 sites — Same floor+20log10 conversion reimplemented in two modules; import the one.
- **deconv two power-of-2 rounding algorithms** [delete-redundant-copy/S] -> `one next-pow2 helper in jasper/audio_measurement/deconv.py` — 2 sites — Two different next-power-of-2 implementations in one file — pick one helper.
- **peering _parse_mode token lists vs doctor** [adopt-existing-helper/S] -> `jasper/peering/config.py::read_state()/state_enabled()` — 2 sites — doctor reimplements the on/off token parsing that config.py's public API already owns and that a sibling web surface reuses correctly.
- **capture_relay relay_base_from_env duplicated in state_aggregate** [adopt-existing-helper/S] -> `jasper/capture_relay/health.py::relay_base_from_env (+ DISABLED_RELAY_BASE_VALUES)` — 2 sites — state_aggregate hand-duplicates the env parsing and a literal copy of the sentinel set that health.py already exports.
- **dbus_next Variant-unwrap one-liner** [extract-shared-helper/M] -> `one variant_value() helper (shared dbus util)` — 2 sites — The Variant-unwrap one-liner is duplicated in-file and reimplemented across many modules; one shared helper is overdue.
- **wake_score CONDITIONS tuple vs wake_conditions SSOT** [adopt-existing-helper/S] -> `jasper/wake_conditions.py::CONDITIONS = ('quiet','ambient','music')` — 2 sites — A local 2-tuple diverges from the declared 3-value SSOT (sibling uses it correctly), silently skipping valid corpus conditions — a real scoring bug.
- **TTS provider dispatch (openai/gemini/grok)** [extract-shared-helper/M] -> `one provider→TTS-generator dispatch helper` — 2 sites — 3-way provider→TTS dispatch duplicated almost verbatim, but only one copy is test-guarded for completeness; sharing extends the guard.
- **research/state provider lookup vs catalog.provider_by_id** [adopt-existing-helper/S] -> `jasper/research/catalog.py::provider_by_id` — 2 sites — state.py reimplements catalog's provider_by_id inline while that function sits unused — adopt it to give the SSOT a caller.
- **USB-gadget dtoverlay probe (config.txt)** [extract-shared-helper/S] -> `one usb_gadget_stack_present() helper` — 2 sites — Same USB-gadget dtoverlay scan reimplemented in 3 files with no shared function or pinning test.
- **measurement _latest_current_summed_records/_tests identical bodies** [delete-redundant-copy/S] -> `one parametrized function in jasper/active_speaker/measurement.py` — 2 sites — Two functions with byte-identical bodies differing only in name.
- **httpx sys.modules stub guard (naive 'not in sys.modules' check)** [adopt-existing-helper/M] -> `tests/conftest.py :: hoist existing _stub_if_missing and route all 5 files through it` — 2 sites — 5 copies of a guard that can't distinguish 'absent' from 'not-yet-imported' is a genuine hazard, not just duplication. A shared _stub_if_missing installs the fake only when httpx is truly missing.
- **capture-relay backend double (FakeRelayBackend)** [extract-shared-helper/M] -> `tests/capture_relay_fixtures.py :: superset FakeRelayBackend` — 2 sites — Two copies of the same in-memory double have already diverged; consolidating on the superset stops the drift with modest effort.
- **Fake CompletedProcess / subprocess-result builder** [extract-shared-helper/M] -> `tests/conftest.py :: fake_completed_process(returncode=0, stdout='', stderr='') on subprocess.CompletedProcess` — 2 sites — One conceptual fake is spelled 4 ways in test_doctor.py and copied verbatim across wifi files. A single CompletedProcess-based helper is the obvious SSOT; the odd bespoke shapes can stay.
- **Fake systemctl bash stub + log reader (_fake_systemctl/_systemctl_log)** [extract-shared-helper/S] -> `tests/_systemctl_fake.py` — 2 sites — Byte-identical fake shared by exactly two files; small dedicated module is clean and low-risk.
- **Camilla guard-script fixture writers (_write_statefile/_pipe_config)** [extract-shared-helper/S] -> `tiny shared module for the byte-identical writers; keep _runtime_safe_graph_script per-file` — 2 sites — Two byte-identical writers shared by exactly two files; the diverging runtime-safe-graph body must stay per-file, so a minimal extraction is the right scope.
- **Spotify/transport tool fakes (FakeRouter/FakeAccountClient)** [extract-shared-helper/M] -> `tests/_spotify_fakes.py (FakeRenderer stays per-file)` — 2 sites — Duplication is explicitly acknowledged in the tests; extracting the two truly-shared doubles while leaving the divergent renderer is the correct boundary.
- **Google API client stubs (_FakeExecutable/_make_clients)** [extract-shared-helper/S] -> `one shared _make_google_clients helper module` — 2 sites — Same Google-API stub scaffolding in two files; a small shared helper is a clean non-blocking dedup.
- **LiveTurn test double (_FakeTurn)** [extract-shared-helper/S] -> `shared tests helper module :: parametrizable _FakeTurn` — 2 sites — 4 slightly-varied copies of the same double; one parametrizable fixture future-proofs protocol changes, but local doubles are also fine given size.
- **JS standalone test-runner harness** [extract-shared-helper/S] -> `tests/js/_test_runner.mjs :: export async run(tests)` — 2 sites — 7 byte-identical ~15-line runners; a single importable runner is trivial, though 'leave-it' is reasonable given the standalone-execution design goal.
- **Shipped-tool-count literal (32) vs EXPECTED_TOOL_NAMES SSOT** [single-constant/S] -> `import len(tests._tool_pack_contract.EXPECTED_TOOL_NAMES)` — 2 sites — A magic 32 duplicated against an existing SSOT is a clear single-constant fix that prevents silent staleness when the tool set changes.
- **CamillaConfigValidationResult VALID stub (_valid_config)** [adopt-existing-helper/S] -> `import _valid_config from tests.test_active_speaker_startup_load` — 2 sites — Identical stub redefined verbatim in two files while a sibling already exports it; adopt the existing one.
- **systemd unit-identity parser (User=/SupplementaryGroups=)** [leave-it/S] -> `optional: shared unit-directive parser used only by the two identity files (following install_surface.py precedent)` — 2 sites — Trivially small and only two call sites with slightly different key handling; a local leave-it is cleaner than a new abstraction unless the _value_for cluster lands, in which case it can reuse that helper.
- **install.sh full-profile hand-inlines the streambox installer's support-file/audio-graph helpers** [adopt-existing-helper/M] -> `deploy/lib/install/systemd-units.sh: install_jasper_support_files + install_local_audio_graph_unit_files (already called by install_streambox_systemd_units)` — 2 sites — Highest payoff: not cosmetic — the default profile is missing a crash-safety ExecStopPost binary and a legacy-dropin cleanup. Replacing the inlined block with the two existing helpers both dedups and fixes the drift.
- **Renderer/Bluetooth/AirPlay apt-get package block byte-duplicated across install_deps and install_streambox_deps** [extract-shared-helper/S] -> `new _install_renderer_native_deps() in deploy/install.sh, called by both` — 2 sites — Byte-for-byte identical ~25-package list in two profiles; a future shairport-sync dep bump risks landing in only one. Small extraction, real drift-prevention value.
- **detect_apple_cards/resolve_cards + APPLE_DONGLE_REGEX byte-identical in jasper-dac-init and jasper-headphone-monitor** [extract-shared-helper/S] -> `new deploy/lib/jasper-apple-dongle.sh, sourced sibling-first/installed-fallback like jasper-env-file.sh` — 2 sites — diff of the two function bodies is empty. Co-installed, co-gated, but not co-sourced — the exact shape the repo already fixed once via jasper-core-graph-park-units.sh. Clean lib extraction.
- **Camilla crossover-guard inlines pipe-guard's repair_statefile() + FIFO reader-presence probe** [extract-shared-helper/M] -> `new deploy/lib/jasper-camilla-guard-common.sh (parameterized on detail= message) + a contract test mirroring test_core_graph_park_units_contract.py` — 2 sites — ~40 lines duplicated including the runtime-safe-graph invocation and repaired_to sed, differing only in a log string. Shared-lib+test is the pattern the codebase already established for this failure mode.
- **14-line repo-python (.venv / git-common-dir / python3) resolution block copy-pasted across 9 scripts; one copy dropped the worktree fallback** [extract-shared-helper/M] -> `new resolve_repo_python() in scripts/_lib.sh` — 2 sites — 9 byte-identical copies of nontrivial worktree-aware resolution, none in the existing _lib.sh, and already drifted in one. Clear single-home win.
- **firmware dial and satellite-amoled ship byte-identical discovery.cpp/.h with no shared firmware lib** [extract-shared-helper/M] -> `shared firmware dir referenced via PlatformIO lib_extra_dirs / -I; dial's copy is canonical` — 2 sites — diff produces zero output on both files; any mDNS change must be hand-applied twice. Worth the one-time PlatformIO wiring.
- **switch-gemini-model.sh hardcodes the two Gemini model IDs instead of reading jasper.voice.catalog** [adopt-existing-helper/M] -> `jasper.voice.catalog PROVIDERS[gemini].models (resolved over SSH, like switch-voice-provider.sh)` — 2 sites — Project invariant (registry is SSOT) already codified for sibling scripts; this one violates it with a drift-prone dated ID suffix.
- **alsa_backend content-capture read: EAGAIN/EPIPE/ESTRPIPE classify + IoCounters bookkeeping duplicated across two structs** [extract-shared-helper/M] -> `make rust/jasper-outputd/src/alsa_backend.rs AlsaBackend::read_content_available canonical; PairedCompositeSink::read_content_period reuses it` — 2 sites — Same Ok/EAGAIN/EPIPE/ESTRPIPE arms and 5 identical IoCounters increments in both read paths; only the outer wrapper differs. Genuine should-fix, hardware-gated.
- **chip-aec daemon lifecycle (kill/wait/SIGKILL/restart) + set_bypass + prompt copy-pasted and drifting across two chip-aec experiment scripts** [extract-shared-helper/M] -> `new scripts/_chip_aec_experiment_lib.sh (mirroring _lib.sh); chip-aec-capture-comparison.sh is the fuller/canonical version` — 2 sites — Full daemon-lifecycle block duplicated for the same daemon, already diverging; both are throwaway experiment scripts but the drift is user-visible. Shared source lib.
- **host_clock begin_probe and restart_probe_wait share a verbatim 9-line probe-state-reset block** [extract-shared-helper/S] -> `new private reset_probe_measurement(&mut self, actions) in rust/jasper-host-clock/src/lib.rs; each caller keeps its own transition/log line` — 2 sites — Identical 9-line reset that must be hand-kept-in-sync; clean private-helper extraction with each caller retaining its distinct transition/log.
- **content_bridge recomputes minimum_safe_fill_frames inline while the crate claims a single source of truth** [adopt-existing-helper/S] -> `jasper_resampler::minimum_safe_fill_frames (already delegated to by fanin config.rs:605 and lane_resampler.rs:924)` — 2 sites — lib.rs's SSOT claim is literally false for this one call site; the fix is a one-line delegation matching the other two callers.
- **outputd tts.rs private db_to_linear duplicates the sanitizing shared gain_db_to_linear** [adopt-existing-helper/S] -> `crate::mixer::gain_db_to_linear (re-export of jasper_tts_protocol::loudness::gain_db_to_linear, already used by core.rs:10 and fanin/tts.rs)` — 2 sites — Private copy lacks the sanitize guard; adopting the crate's own re-exported helper both dedups and hardens the duck-gain input.
- **outputd snapshot_json: 2 fields inline format! instead of push_kv helpers; all 9 push_kv_* repeat the same key-preamble** [extract-shared-helper/S] -> `rust/jasper-outputd/src/state.rs push_kv_f64/push_kv_f64_opt (existing) + new push_key(buf,key) called first in each of the 9 variants` — 2 sites — Local single-file dedup with byte-identical output; low risk, tidies the one function that already has the right helpers.
- **outputd PlayoutEvent structurally clones all 16 PlayoutSegment fields; as_event() hand-copies each** [align-drifted-twins/S] -> `rust/jasper-outputd/src/ledger.rs — make PlayoutEvent a type alias for PlayoutSegment (or derive From), collapse as_event() to self.clone()` — 2 sites — Two identical 16-field structs with a hand-copy converter; a type alias eliminates the drift surface entirely.
- **Synthetic-signal generators/fork: click-track generator duplicated + estimate_lag FFT cross-corr forked** [extract-shared-helper/S] -> `new scripts/_make_click_track.py (raw|wav) for the click generator; add 'keep in sync with aec-probe-timing.py::estimate_lag' cross-ref for the lag fork` — 2 sites — Duplicated acoustic test-signal logic with copy-pasted magic constants; extract the identical generator, cross-reference the intentional fork.
- **aec-probe systemd stop-and-restore block duplicated across two probe scripts, missing explanatory comments** [align-drifted-twins/S] -> `scripts/aec-probe-latency.sh:69-126 (carries the StartLimitAction=reboot + shairport-restart rationale comments)` — 2 sites — Function bodies already identical and drifting only in documentation; the minimal honest fix is copying the two rationale comments, deferring a lib.
- **impulse_tap module ~93% verbatim-duplicated between jasper-fanin and jasper-usbsink-audio** [extract-shared-helper/L] -> `new shared crate (ALSA-independent core: ImpulseDetector/TapState/TapConfig/JSON helpers), consumed by both daemons — mirroring jasper-host-clock/jasper-resampler` — 2 sites — Largest single duplication (~1200 lines) but highest-risk/effort; the repo has precedent (host-clock/resampler crates). Flag as a deliberate cross-tile effort, not a quick PR.
- **Cross-crate DSP primitive leads: Direct-Form-I Biquad and BS.1770 K-weighting/LUFS duplicated (rust internal + rust<->python)** [leave-it/M] -> `if pursued: hoist a shared Biquad into jasper-tts-protocol; for K-weighting add cross-ref comments (ITU-fixed constants) between loudness.rs and assistant_loudness.py` — 2 sites — Genuine DSP duplication but constants are standard and stable; the pragmatic move is cross-reference comments, not a new shared abstraction across a language boundary.
- **UDS STATUS-socket client (doctor/audio)** [adopt-existing-helper/S] -> `existing private status-socket helper in jasper/cli/doctor/audio.py` — 1 sites — A private helper for exactly this protocol already exists in the same file; four later copies just re-hand-rolled it and drifted.
- **Memory-pressure threshold constants** [align-drifted-twins/S] -> `the documented SSOT for memory-pressure thresholds` — 1 sites — Hardcoded absolute MB thresholds that diverged from the codebase's stated single source for memory-pressure limits.
- **SCHEMA_VERSION stamped on two artifact shapes** [single-constant/S] -> `two independent version constants (startup-load vs commission-load)` — 1 sites — One constant version-stamps two differently-shaped artifacts, so a change to one falsely bumps the other; split into two.
- **WakeLoop.for_tests hand-mirrored constructor** [leave-it/L] -> `n/a (test scaffolding)` — 1 sites — Real maintenance hazard but no clean shared abstraction for a test-only constructor mirror; leave-it unless a builder/dataclass refactor of WakeLoop is undertaken separately.
- **/sync/analyze WAV upload cap magic number** [single-constant/S] -> `named module constant in jasper/web/correction_setup.py` — 1 sites — Inline magic number where every sibling cap is a named constant; name it for consistency and SSOT.
- **_post_grouping_set parse-or-400 field stanza** [extract-shared-helper/M] -> `local typed-field parser helper in jasper/control/server.py` — 1 sites — ~75 lines of the same parse-or-400 shape repeated 7x in the file's largest handler; a small field-descriptor helper collapses it.
- **Apple dongle USB vendor:product id** [adopt-existing-helper/S] -> `the DacProfile registry that already owns the id` — 1 sites — A hardware id hardcoded as a regex when a registry already owns it; adopting removes a second source of truth for dongle detection.
- **SessionConfig unused PEQ-tuning fields** [delete-redundant-copy/S] -> `remove the dead fields (or wire them to the real strategy)` — 1 sites — Fields the design pipeline never reads but that get reported as authoritative — silent divergence from the actual strategy; remove or wire.
- **aec_bridge safe-engine-process blocks** [extract-shared-helper/S] -> `local helper in jasper/cli/aec_bridge.py` — 1 sites — Same try engine.process/except log+disable+empty-emit/else emit copied 5x in the AEC loop.
- **prepare_source_handoff SourceHandoff construction boilerplate** [leave-it/M] -> `n/a (local return-shape repetition)` — 1 sites — Repeated field boilerplate at 10 return points, but each return legitimately varies 2-4 fields; a local default-dict helper is optional polish, not a clear win — leave-it or tackle only if the function is refactored anyway.
- **classify_camilla_graph identical-body if branches** [delete-redundant-copy/S] -> `merge the two guards in jasper/active_speaker/runtime_contract.py` — 1 sites — Two adjacent if-blocks with identical bodies differing only in a jointly-exhaustive guard.
- **audio_input_view disabled_reason ternary + string** [single-constant/S] -> `local constant/helper in jasper/audio_input_view.py` — 1 sites — Same 4-line ternary and a user-facing string repeated verbatim; factor into a local constant/helper.
- **aec_tune --mic-device default literal** [adopt-existing-helper/S] -> `jasper.mics.xvf3800.alsa_card_name()` — 1 sites — Hardcoded ALSA card literal where the package's own resolver (used by sibling aec_init.py) enumerates the real card.
- **voice_daemon → daemon_main re-export shims** [delete-redundant-copy/S] -> `jasper/voice/daemon_main.py (single canonical path)` — 1 sites — Legacy re-export shims that duplicate import paths purely for tests; migrate tests and drop the dead ones.
- **build_commissioning_view recomputed int expressions** [extract-shared-helper/S] -> `local variable in jasper/active_speaker/commissioning_coordinator.py` — 1 sites — Identical captured/required int(...) expressions recomputed in two result dicts; a shared local var avoids drift.
- **runtime_balance active_endpoint inline predicate** [adopt-existing-helper/S] -> `jasper/multiroom/config.py::is_active_member(cfg)` — 1 sites — Reimplements the shared is_active_member predicate inline that sibling predicates already reuse for consistency.
- **calibration no-key copy string** [adopt-existing-helper/S] -> `jasper/calibration_agent/key_provisioning.py::_NO_KEY_NUDGE` — 1 sites — A declared single-source no-key message exists in the same package but correction_advisor hardcodes its own divergent copy.
- **calibration cli JSON-load-with-error block** [extract-shared-helper/S] -> `local helper in jasper/calibration_agent/cli.py` — 1 sites — Same JSON-load-with-error block duplicated between two CLI branches.
- **audio_hardware __init__ re-export drift** [align-drifted-twins/S] -> `jasper/audio_hardware/dac.py::__all__` — 1 sites — Package re-export list drifted from dac.py's __all__, hiding 3 publicly-exported symbols.
- **web_commissioning local _issue vs shared _common.issue** [adopt-existing-helper/S] -> `jasper/active_speaker/_common.py::issue` — 1 sites — A local _issue duplicates the package's shared _common.issue that sibling modules already adopt.
- **capture_relay __all__ drift (missing build_level_ramp_spec)** [align-drifted-twins/S] -> `jasper/capture_relay/__init__.py (add the missing builder)` — 1 sites — One of five shipped capture-kind builders is missing from both the import block and __all__ with nothing guarding sync.
- **bluetooth adapter MessageBus connect/disconnect wrapper** [extract-shared-helper/M] -> `a context manager / decorator in jasper/bluetooth/adapter.py` — 1 sites — Same connect/try/finally-disconnect wrapper repeated verbatim in all 8 public functions; an async ctx manager collapses it.
- **accessories bridge log-field dict** [adopt-existing-helper/S] -> `jasper/accessories/bridge.py::_log_key_action (already centralizes the shape)` — 1 sites — _dispatch rebuilds the log-field dict inline 3x when a helper for exactly that shape already exists in the file.
- **weather geocode branch duplication** [extract-shared-helper/S] -> `local helper in jasper/weather.py` — 1 sites — Two branches repeat the identical geocode-and-check shape; one helper.
- **Live-HTTP-server teardown boilerplate (_serve_in_thread -> fixture)** [extract-shared-helper/M] -> `tests/test_wake_corpus_setup.py pytest fixture (yield port+helper), mirroring sibling live_server/running_server` — 1 sites — 22 bodies hand-roll identical try/finally teardown while the fixture idiom already exists in sibling files. A single fixture removes leak risk and shrinks the file materially.
- **EXPECTED_DSTS drifted twin of JASPER_CORE_AUDIO_GRAPH_INSTALL_ROWS** [align-drifted-twins/S] -> `tests/test_install_core_audio_graph_loop.py::EXPECTED_DSTS (restore all 17) + assert attempted==set(EXPECTED_DSTS)` — 1 sites — A drifted safety table that no longer covers every install destination is a real coverage hole, not cosmetics. Cheap to realign and pin so it can't silently re-drift.
- **MeasurementSession construction (_make_session)** [extract-shared-helper/M] -> `tests/correction_session_fixtures.py (mirror correction_bundle_fixtures.py) :: make_session(**kwargs, sessions_dir=?)` — 1 sites — 6 near-identical private copies with accidental variations already exist; correction_bundle_fixtures.py sets the exact precedent for a sibling fixtures module.
- **subprocess.Popen recorder stub (FakePopen)** [extract-shared-helper/S] -> `tests/test_control_server.py local _popen_recorder() -> (FakePopenCls, calls) ` — 1 sites — Identical 3-line stub written 7 times in one file; a single local factory is a clean local dedup without a new abstraction layer.
- **Async poll-until-condition helper (wait_until/_wait_for)** [extract-shared-helper/S] -> `tests/conftest.py :: async wait_until(predicate, *, timeout) on one monotonic clock` — 1 sites — 5 reimplementations with clock inconsistency; one conftest helper is cheap and removes a subtle flakiness source, though acceptable to leave pre-launch.
- **JS summedSummary hoist (intra-file)** [extract-shared-helper/S] -> `tests/js/sound_profile_harness.mjs :: hoist summedSummary() beside topologyPayload()` — 1 sites — A helper built to replace a shape is defined 73% into the file while earlier tests hand-roll it; hoisting and reusing is a clean local dedup.
- **Cues provider-agnostic forbidden-word list (drifted twin)** [delete-redundant-copy/S] -> `keep the fuller documented registry version in the sibling; delete the weaker copy in test_cues_generator.py (or share one tuple)` — 1 sites — Same invariant implemented twice with already-drifted word lists; collapse to the stronger copy or one shared constant.
- **installer_text() reimplemented as _install_text()** [adopt-existing-helper/S] -> `from tests.install_surface import installer_text` — 1 sites — An exact copy of an existing shared helper; swap in the import and delete the local duplicate.
- **session metadata-file read block (intra-file helper)** [extract-shared-helper/S] -> `tests/test_wake_corpus_setup.py local _read_session_metadata(tmp_path)->dict beside _use_tmp_bridge_env` — 1 sites — Same two-line read repeated 10x in one file; a tiny local helper is the right-sized dedup, no new module needed.
- **wake_corpus vs web_wake_corpus shell/CSRF split not honored** [delete-redundant-copy/S] -> `tests/test_web_wake_corpus_setup.py owns shell/CSRF/placeholder/CSRF_HEADER/radio asserts (per its own docstring)` — 1 sites — The claimed ownership split between the two files isn't honored; deleting the duplicated shell/CSRF asserts restores the intended boundary.
- **CSRF-token acquisition preamble (_http_post family)** [extract-shared-helper/S] -> `tests/test_system_setup.py local _csrf_session(base)->(opener,token) (or add meta-tag variant to _web_test_helpers.py)` — 1 sites — Identical CSRF preamble tripled within one file; a local _csrf_session collapses it, or fold into the existing web-helpers module.
- **doctor usbsink Path-patch block (intra-file)** [leave-it/S] -> `optional local _patch_target_path(check_fn, blob) in tests/test_doctor_usbsink.py` — 1 sites — 18 identical 6-line patch blocks in one file; a local helper is optional and self-contained blocks read fine, so leave-it unless the file is otherwise touched.
- **doctor memory-resilience systemctl-show mock builders** [leave-it/S] -> `optional single _make_systemctl_show_run(prop_maps) in tests/test_doctor_memory_resilience.py` — 1 sites — Only two near-identical builders that each read clearly; the payoff of merging is below the readability cost. Leave it.
- **doctor _citibike_cfg composition on _fresh_cfg** [adopt-existing-helper/S] -> `rebuild tests/test_doctor.py::_citibike_cfg on _fresh_cfg, forwarding vars via **vars_` — 1 sites — _citibike_cfg reimplements the env-drop/set/Config.from_env shape that _fresh_cfg owns and _routes_cfg already composes on; align it for consistency.
- **doctor _patch_asound_conf / _patch_shairport_conf merge** [extract-shared-helper/S] -> `one tests/test_doctor.py local helper(monkeypatch, module, real_path, conf_text, tmp_path) keeping optional stale-topology arg` — 1 sites — Two near-identical single-file helpers differing only in target module/path; a parameterized local merge is a clean small dedup.
- **sound_setup_commission summed-test setup (intra-file)** [leave-it/M] -> `optional local _summed_test_stubs(monkeypatch, tmp_path, controller) in tests/test_sound_setup_commission.py` — 1 sites — 5 functions share ~30-50 line setup, but it's test-only boilerplate with meaningful per-test variation; a local helper is optional and leave-it is acceptable.
- **sound_setup env-setup boilerplate (intra-file)** [leave-it/S] -> `optional small env-setup helper for the topology/draft/preview setenv trio in tests/test_sound_setup.py` — 1 sites — After the inline topology dict is replaced by the shared builder, the remaining recurring setenv trio is minor boilerplate that's acceptable to leave.
- **voice_eval spotify env-gated skip predicates** [extract-shared-helper/S] -> `tests/voice_eval/regression/harness.py :: skip_if_playback_disabled() / require_google(harness)` — 1 sites — Env-var-gated skips copied per-file; hoisting the predicates into harness.py centralizes the env-var name, but the per-file design makes it optional.

## Env-flag dispositions (64 adjudicated)

- `JASPER_AEC_CHIP_AEC_ENABLED / Config.aec_chip_aec_enabled` [config-field-live] -> keep — getattr(cfg, "aec_chip_aec_enabled", False) in jasper/voice/input_policy.py:96, called via build_effective_speech_input_policy(cfg) from jasper/voice/daemon_main.py:201,560 in production. Phase-0 'read=0' claim was getattr-blind and wrong.
- `JASPER_HA_VERIFY_SSL / Config.ha_verify_ssl` [config-field-live] -> keep — getattr(cfg, "ha_verify_ssl", True) in jasper/home_assistant.py:583 (build_ha_client, called from jasper/voice/daemon_main.py:663) AND jasper/cli/doctor/integrations.py:152. Phase-0 'read=0 in production' claim was getattr-blind and wrong.
- `JASPER_MIC_DEVICE_DTLN / Config.mic_device_dtln` [config-field-live] -> keep — Read via getattr(cfg, _LEG_DEVICE_ATTR["dtln"]) in jasper/voice_daemon.py:704,723 (_configured_wake_legs), called from jasper/voice/daemon_main.py:968 in production. String-keyed dispatch invisible to literal grep; Phase-0 was wrong.
- `JASPER_MIC_DEVICE_CHIP_AEC_150 / Config.mic_device_chip_aec_150` [config-field-live] -> keep — Read via getattr(cfg, _LEG_DEVICE_ATTR["chip_aec_150"]) in jasper/voice_daemon.py:705,723, called from jasper/voice/daemon_main.py:968. Same string-keyed dispatch pattern as mic_device_dtln; live, not dead.
- `JASPER_MIC_DEVICE_CHIP_AEC_210 / Config.mic_device_chip_aec_210` [config-field-live] -> keep — Read via getattr(cfg, _LEG_DEVICE_ATTR["chip_aec_210"]) in jasper/voice_daemon.py:706,723, called from jasper/voice/daemon_main.py:968. This was explicitly in the Phase-0 contested list; confirmed live via getattr dispatch.
- `JASPER_CAMILLA2_HOST / Config.camilla2_host` [config-field-dead] -> keep — cfg.camilla2_host has zero consumers (literal+getattr checked). Only other reader is jasper/camilla.py crossover_controller(), which has zero callers repo-wide and is self-documented "INERT today" pending a future jasper-camilla-crossover.service reconciler PR. Deliberate staged scaffolding, not an accident.
- `JASPER_CAMILLA2_PORT / Config.camilla2_port` [config-field-dead] -> keep — Same as camilla2_host: zero consumers, only other reader (crossover_controller()) is confirmed dead/inert-by-design. Do not delete — it's intentional forward scaffolding per its own docstring.
- `JASPER_CAMILLA2_STATEFILE / Config.camilla2_statefile` [config-field-dead] -> investigate — cfg.camilla2_statefile has zero consumers, but unlike host/port the underlying env var IS actively read in production via active_leader_config.py:crossover_statefile_path(), called from multiroom/reconcile.py:1947. See findings for suggested fix.
- `JASPER_GOOGLE_WEB_HOST / Config.google_web_bind_host` [config-field-dead] -> investigate — cfg.google_web_bind_host has zero consumers anywhere (literal+getattr). Real consumer jasper/web/google_setup.py:1080-1081 reads the env var directly via os.environ, bypassing Config entirely. See findings.
- `JASPER_GOOGLE_WEB_PORT / Config.google_web_bind_port` [config-field-dead] -> investigate — cfg.google_web_bind_port has zero consumers. jasper/web/google_setup.py:1084-1085 and jasper/web/__main__.py:459 (WizardSpec) both read the env var directly, bypassing Config.
- `JASPER_SPOTIFY_WEB_HOST / Config.spotify_web_bind_host` [config-field-dead] -> investigate — cfg.spotify_web_bind_host has zero consumers. jasper/web/spotify_setup.py:1467 reads the env var directly via os.environ, bypassing Config entirely.
- `JASPER_SPOTIFY_WEB_PORT / Config.spotify_web_bind_port` [config-field-dead] -> investigate — cfg.spotify_web_bind_port has zero consumers. jasper/web/spotify_setup.py:1468 and jasper/web/__main__.py:455 (WizardSpec) both read the env var directly, bypassing Config.
- `JASPER_AUDIO_TOPOLOGY` [doc-only-dead] -> fix-doc-name — Read nowhere (jasper/rust/deploy/scripts/c). Explicitly confirmed retired: jasper/cli/doctor/audio.py:1206-1214 calls it 'the retired dmix/fanin switcher'; deploy/lib/install/env-migrations.sh:1054 retire_audio_topology_switch() deletes the leftover state file on every install. Strongest possible confidence — code self-documents its own retirement.
- `JASPER_DLNA_RENDERER` [doc-only-dead] -> delete-from-doc — Zero hits anywhere outside docs/HANDOFF-dlna.md. DLNA renderer feature was never implemented; the doc describes a design with no corresponding code.
- `JASPER_DLNA_SUPERVISOR` [doc-only-dead] -> delete-from-doc — Zero hits outside docs/HANDOFF-dlna.md, same unimplemented DLNA design as JASPER_DLNA_RENDERER.
- `JASPER_DLNA_UUID` [doc-only-dead] -> delete-from-doc — Zero hits outside docs/HANDOFF-dlna.md, same unimplemented DLNA design.
- `JASPER_DLNA_NAME` [doc-only-dead] -> delete-from-doc — Zero hits outside docs/HANDOFF-dlna.md, same unimplemented DLNA design.
- `JASPER_DLNA_PREEMPT_PORT` [doc-only-dead] -> delete-from-doc — Zero hits outside docs/HANDOFF-dlna.md, same unimplemented DLNA design.
- `JASPER_MUX_DLNA_PREEMPT` [doc-only-dead] -> delete-from-doc — Zero hits outside docs/HANDOFF-dlna.md; part of the same never-shipped DLNA preemption design as the JASPER_DLNA_* family.
- `JASPER_FANIN_USB_RESAMPLER_MAX_ADJUST_PPM` [rename-drift] -> fix-doc-name — docs/HANDOFF-usb-low-latency.md:87 only. Live sibling JASPER_FANIN_INPUT_RESAMPLER_MAX_ADJUST_PPM (no USB_ infix) is read at rust/jasper-fanin/src/config.rs:566 and jasper/audio_runtime_plan.py:98.
- `JASPER_FANIN_USB_RESAMPLER_RING_FRAMES` [rename-drift] -> fix-doc-name — docs/HANDOFF-usb-low-latency.md:86 only. Live sibling JASPER_FANIN_INPUT_RESAMPLER_RING_FRAMES read at rust/jasper-fanin/src/config.rs and jasper/audio_runtime_plan.py.
- `JASPER_FANIN_USB_RESAMPLER_TARGET_FRAMES` [rename-drift] -> fix-doc-name — docs/HANDOFF-usb-low-latency.md:84 only. Live sibling JASPER_FANIN_INPUT_RESAMPLER_TARGET_FRAMES confirmed read in jasper_bare + rust.
- `JASPER_FANIN_USB_RESAMPLER_WARMUP_CUSHION_FRAMES` [rename-drift] -> fix-doc-name — docs/HANDOFF-usb-low-latency.md:85 only. Live sibling JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES confirmed read in jasper_bare + rust.
- `JASPER_TTS_GAIN_DB` [doc-only-dead] -> keep — docs/HANDOFF-persistent-live-session.md:35 already correctly annotates this inline: 'Historical snapshot... Today there is no such env var: jasper-fanin matches assistant loudness... See audio-paths.md.' Doc is already self-correcting; no action needed.
- `JASPER_WAKE_MODEL_RAW` [doc-only-dead] -> delete-from-doc — docs/HANDOFF-wake-training-experiment.md:1063 only, hedged with 'probably... or just' (speculative, not asserted as shipped). Actual code reads JASPER_WAKE_MODEL + per-leg JASPER_WAKE_LEG_* booleans, not per-leg model overrides.
- `JASPER_WAKE_MODEL_AEC` [doc-only-dead] -> delete-from-doc — docs/HANDOFF-wake-training-experiment.md:1064 only, same speculative/hedged mention as WAKE_MODEL_RAW.
- `JASPER_WAKE_MODEL_DTLN` [doc-only-dead] -> delete-from-doc — docs/HANDOFF-wake-training-experiment.md:1064 only, same speculative/hedged mention as WAKE_MODEL_RAW.
- `JASPER_GO_LIBRESPOT_URL` [doc-only-dead] -> keep — Only in docs/historical/CLEANUP-moode-removal.md, already correctly filed under historical/ as part of the moode-removal writeup. No action needed — correctly archived.
- `JASPER_RENDERER_BACKEND` [doc-only-dead] -> keep — Only in docs/historical/CLEANUP-moode-removal.md, already correctly filed as historical. No action needed.
- `JASPER_AEC_RS_SNR_THRESHOLD` [rename-drift] -> fix-doc-name — docs/HANDOFF-mic-quality-v2.md:950 TODO only. Shipped equivalent JASPER_AEC_DND_SNR_THRESHOLD read at jasper/cli/aec_bridge.py:867.
- `JASPER_AEC_RS_HOLD_MS` [rename-drift] -> fix-doc-name — docs/HANDOFF-mic-quality-v2.md:950 TODO only. Conceptually closest shipped equivalent JASPER_AEC_DND_HOLD_DURATION read at jasper/cli/aec_bridge.py:870 (unit/semantics not identical — doc names it _MS, shipped field is a block count).
- `JASPER_AEC_RS_SUBBAND_NEAREND` [rename-drift] -> fix-doc-name — docs/HANDOFF-mic-quality-v2.md:951 TODO only. Nearest shipped equivalents are the JASPER_AEC_NEAREND_MASK_HF_* family (aec_bridge.py:852-864), which covers the same near-end subband-suppression goal under different names.
- `JASPER_AEC_RS_HIGH_BANDS_MAX_GAIN` [doc-only-dead] -> investigate — docs/HANDOFF-mic-quality-v2.md:951 TODO only; no shipped equivalent found. The nearest AEC3Config field (high_bands_suppression.max_gain_during_echo) is noted in experiments/aec3-v2-deep-tune-spike/README.md as hard-clamped to 1.0 by upstream Validate() and inaccessible without a WebRTC field-trial override, so it was never exposed as an env var.
- `JASPER_AEC_NORMAL_MAX_DEC_LF` [rename-drift] -> fix-doc-name — experiments/aec3-v2-deep-tune-spike/README.md:233 example only. Shipped as JASPER_AEC_MAX_DEC_LF (no NORMAL_ infix) at jasper/cli/aec_bridge.py:797,847.
- `JASPER_CALIBRATION_AGENT_MAX_USD_PER_SESSION` [doc-only-dead] -> investigate — docs/HANDOFF-calibration-agent.md:817,1192 only. No session-level USD cap was ever built for the calibration agent; only a per-call output-token budget exists (jasper/calibration_agent/model_client.py). Worth confirming if this safety gap is still wanted.
- `JASPER_CALIBRATION_AGENT_PROVIDER` [rename-drift] -> fix-doc-name — docs/HANDOFF-calibration-agent.md:790 only. Shipped as JASPER_CALIBRATION_ADVISOR_PROVIDER (jasper/calibration_agent/model_client.py:100), which is also in test_env_vars_codified.py's _UNCODIFIED allowlist as intentionally-internal.
- `ANTHROPIC_API_KEY` [doc-only-dead] -> keep — docs/HANDOFF-calibration-agent.md:596 explicitly frames this as a proposed 'net-new variable' with 'no Anthropic key in the project today' — accurately describes itself as not-yet-implemented. No action needed.
- `JASPER_GROUPING_LEADER_CONTENT_LANE` [doc-only-dead] -> delete-from-doc — docs/HANDOFF-multiroom.md:1211 only, described as 'the master gate' for a feature that was never built. No code reads it.
- `JASPER_USBSINK_LEAN_FIFO_PATH` [rename-drift] -> fix-doc-name — docs/HANDOFF-audio-graph-consolidation.md:48 only. Shipped as JASPER_USBSINK_FIFO_PATH (no LEAN_) at jasper/usbsink/daemon.py:195 and .env.example:907.
- `JASPER_MIC_CARD` [doc-only-dead] -> delete-from-doc — docs/REVIEW-google-oss-readiness.md:297-298 only, proposed as a future replacement for ALSA card-name string matching. Never implemented; still string-matching as of HEAD.
- `JASPER_DAC_CARD` [doc-only-dead] -> delete-from-doc — docs/REVIEW-google-oss-readiness.md:297-298 only, same never-implemented proposal as JASPER_MIC_CARD.
- `JASPER_OPENAI_IDLE_TIMEOUT_SEC` [doc-only-dead] -> keep — docs/audit-pending-followups.md:473-474 explicitly lists this as 'Solution C' in a rejected-alternatives list; the doc recommends 'Do nothing' (option D) instead. Correctly documented as rejected, no action needed.
- `JASPER_GEMINI_IDLE_TIMEOUT_SEC` [doc-only-dead] -> keep — Same rejected-alternative context as JASPER_OPENAI_IDLE_TIMEOUT_SEC in docs/audit-pending-followups.md:473-474. No action needed.
- `JASPER_GROK_IDLE_TIMEOUT_SEC` [doc-only-dead] -> keep — Same rejected-alternative context as JASPER_OPENAI_IDLE_TIMEOUT_SEC in docs/audit-pending-followups.md:473-474. No action needed.
- `JASPER_FOO` [illustrative] -> keep — docs/HANDOFF-vad-experiments.md:266 — a generic placeholder name inside a 'how to override env vars on this systemd version' code-pattern example, alongside JASPER_BAZ. Not a real flag.
- `JASPER_BAZ` [illustrative] -> keep — docs/HANDOFF-vad-experiments.md:267 — same illustrative placeholder pair as JASPER_FOO.
- `JASPER_VOLUME_IDLE_THRESHOLD_SEC` [doc-only-dead] -> investigate — PLAN.md:150 only. Distinct from the shipped JASPER_VOLUME_REGRESS_AFTER_SEC (boot-time stale-volume clamp); idle-dimming-during-session was never built. Confirm still wanted or strike from PLAN.md.
- `JASPER_VOLUME_IDLE_DEFAULT_PCT` [doc-only-dead] -> investigate — PLAN.md:150 only, same never-built idle-dimming roadmap item as JASPER_VOLUME_IDLE_THRESHOLD_SEC.
- `JASPER_TTS_ENV_FILE` [write-only] -> remove-field — Set only in tests/test_audio_hardware_reconcile.py:105's fixture dict; deploy/bin/jasper-audio-hardware-reconcile never reads it, unlike its four genuinely-consumed *_ENV_FILE siblings in the same dict.
- `JASPER_USBSINK_ROUTE` [illustrative] -> fix-doc-name — Only in tests/test_doctor_usbsink.py:1180 as fixture file content; the real reconciler key is JASPER_USBSINK_OUTPUT_MODE (output_mode_reconcile.py). Test only checks file mtime so this stale key name is harmless but should be renamed.
- `JASPER_FANIN_HOST_CLOCK_TARGET` [illustrative] -> keep — tests/test_fanin_host_clock_contract.py:149-153 is a NEGATIVE contract test asserting this key must NEVER appear in config — a deliberate guard-rail against introducing it, not a dead flag.
- `JASPER_AEC_NOT_REAL` [illustrative] -> keep — tests/test_aec_sweep.py:96 — deliberately fake name used to test that the sweep mechanism validates/rejects unknown env-override keys.
- `JASPER_ARGV_CAPTURE` [illustrative] -> keep — tests/test_dsp_apply.py:32,48,103 — test-double shell stub captures argv via $JASPER_ARGV_CAPTURE; pure test scaffolding, not a product config surface.
- `JASPER_BARGE_IN_BOGUS` [illustrative] -> keep — tests/test_provider_state.py:158,171 tests resolve_barge_in_enabled with a fake provider id 'bogus' to verify unknown providers resolve to False. Confirms the real JASPER_BARGE_IN_<PROVIDER> dynamic dispatch (barge_in_env_key() f-string in jasper/voice/provider_state.py:236, called from voice_daemon.py:3231 in production) generalizes correctly.
- `JASPER_BARGE_IN_OPENAI` [config-field-live] -> keep — Never appears as a literal string (dynamically built by f"JASPER_BARGE_IN_{provider.upper()}" in jasper/voice/provider_state.py:236), so a naive grep misses it. read_barge_in_enabled() is called from jasper/voice_daemon.py:3231 in production for whichever provider is active, including openai.
- `JASPER_BARGE_IN_GROK` [config-field-live] -> keep — Same dynamic f-string construction as JASPER_BARGE_IN_OPENAI; live for the grok provider via the identical read_barge_in_enabled() production path.
- `JASPER_FANIN_OTHER` [illustrative] -> keep — tests/test_ring_proto_scripts.py:622,642 — placeholder 'keep unrelated keys untouched' test case for an env-file-pruning script, not a real var.
- `JASPER_FANIN_SOMETHING` [illustrative] -> keep — tests/test_ring_proto_scripts.py:616,641 — same placeholder pattern as JASPER_FANIN_OTHER.
- `JASPER_OUTPUTD_ANOTHER_VAR` [illustrative] -> keep — tests/test_ring_proto_scripts.py:563,596 — same 'preserve unrelated keys' placeholder pattern.
- `JASPER_GEMINI_API_KEY` [illustrative] -> keep — tests/test_cues_cli.py:120 — defensive env-clear list entry using the wrong (JASPER_-prefixed) name; the real key is bare GEMINI_API_KEY (jasper/config.py:446). Harmless test-hygiene leftover, not a real flag.
- `JASPER_LIBRESPOT_DEVICE` [illustrative] -> keep — tests/test_doctor.py:4543-4625 — synthetic ${...}-style placeholder token used to test an ALSA error-message-humanizing function; no real .template/.conf file contains this token anywhere in the repo.
- `JASPER_BLUEALSA_DEVICE` [illustrative] -> keep — tests/test_doctor.py:4549-4595 — same synthetic placeholder-token pattern as JASPER_LIBRESPOT_DEVICE, not a real template mechanism or env var.
- `JASPER_FAKE_AMIXER_LOG (representative of FAKE_APLAY_LISTING, FAKE_RUNTIME_BLOCK, FAKE_SYSTEMCTL_RC, GUARDIAN_LOG/RC, JOURNALCTL_LOG/KERNEL, NMCLI_ACTIVE/ALL_PROFILES/CONNECT_RC/CONNECT_STDERR/LOG/PROFILE_DETAILS/UP_RC, PYTHON_LOG/RC, RENDER_LOG, SYSTEMCTL_LOG, TEST_LOG, TEST_HA_STATUS_JSON, VOICE_OOM)` [illustrative] -> keep — ~20-name family confirmed test-only: env vars injected by test doubles to redirect fake-binary output/log paths for deploy/bin/*.sh script tests (see tests/test_audio_hardware_reconcile.py's ~15-entry extra_env dict). None correspond to production JASPER_* config surface; per-name verification would be repetitive of this confirmed pattern.
- `JASPER_WAKE_CORPUS_AEC3_SWEEP_AEC3_VARIANT_1/2/3_PORT` [config-field-live] -> keep — Dynamically built via f"JASPER_WAKE_CORPUS_AEC3_SWEEP_{leg.upper()}_PORT" in jasper/web/__main__.py:153, legs sourced from wake_ports.DEFAULT_AEC3_SWEEP_PORTS (aec3_variant_1/2/3, defined in jasper/aec_sweep.py:131-153). Live production wiring for the wake-corpus wizard, invisible to literal-string grep.

## Duplication consolidation triage (feedback point 3)

Do NOT execute the 169 clusters as one wave. Classify each:

- **Correctness / safety drift → do now.** Clusters where copies have already diverged in a way that affects behavior or a safety invariant (e.g. divergent Wi-Fi timeout values, PSK-redaction paths, atomic-write correctness).
- **Shared invariant, multiple writers → do soon.** A value/contract that MUST agree across sites and is only kept in sync by hand (leg-label tables, role-pair dicts, bbox constants, the three-place wake-leg defaults).
- **Cosmetic duplication → leave alone** unless the owning file is already being edited for another reason. Extracting a helper for two call sites that will never drift is churn on a production speaker.

Apply this filter to `clusters.json`; only the first two classes earn a PR now.

## Open owner decisions

1. **Relay alignment-gate wiring** (DA blocker, deferred): recommended seam is per-flow inside each relay `run_and_consume` (after `run_and_store`, before `on_capture_uploaded`); land `require=False` with `event=capture_relay.alignment` logging, flip `require=True` per-flow only after the 0.40 threshold is validated on jts3/jts5. Do NOT gate the shared `on_capture_uploaded` (same-origin uploads use it).
2. **`os.environ`-bypassed Config fields**: `google/spotify_web_bind_host/port`, `camilla2_statefile` — remove or route through typed `Config`.
3. **`jasper/bluetooth/` HANDOFF**: doc-map temporarily routes it to the nearest operational doc; it needs its own canonical doc.
4. **Two unverified README invariants** now have guard tests in W5: outputd sole-DAC-writer; peering exactly-one-winner. If either invariant is not actually enforced in code, the test must fail loud (no tautology).

## Validation still owed (runtime blind spot — feedback point 6)

Static fixes on the audio path ship unproven. Owed on jts3/jts5 before trusting: fan-in zero-dimension rejection, XVF control-transfer timeout, peering winner-election under real multi-speaker wake, any volume/measurement race fix (interacts with merged #1213).
