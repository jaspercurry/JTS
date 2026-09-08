# Phase 0 — Duplicate systems and parallel implementations

Checkout `/home/user/JTS` @ `2d571e6b8`. Read-only. All file:line refs opened.

## Headline

**The repo does not have a duplicate-*subsystem* problem. It has a duplicate-*primitive*
problem.** Every large seam I probed (CamillaDSP emitters, DSP measurement math, the
doctor framework, the Rust crate graph, web CSS/JS, systemd unit reading, artifact
manifests) turned out to be genuinely converged, usually with an ADR behind it. What is
forked is the small stuff that nobody notices forking: timestamp helpers, env-file
readers, atomic writes, number coercion, JSON state files, `main()` boilerplate,
and — the one that matters — biquad magnitude math.

Mechanical scan totals: **3,290 Python functions ≥8 lines / ≥6 statements** in
`jasper/ scripts/ deploy/lib/ experiments/`; exact normalized-AST duplicate clusters
remove only **~700 LOC**. That number is the *proof* the problem is not function-level
copy-paste — the real duplication is same-idea/different-spelling, which hashing misses
and which the concern-by-concern pass below found instead.

Estimated total convergence yield: **~2,600 LOC deletable** (excl. the 2,150-LOC
throwaway-harness fork, which is a deletion not a convergence).

## Concerns ranked by convergence yield

| # | Concern | Impls | Canonical exists? | Removable LOC | Severity |
|---|---|---|---|---|---|
| 1 | Wizard `main()` boilerplate (7 dead, 3 orphan-scripted) | 14 | no | ~600 | Should-fix |
| 2 | `s0-sync-bench` fork of `multiroom-spike` (both "THROWAWAY") | 2 | n/a | ~2,150 (delete) | Should-fix |
| 3 | Session-bundle stores (active_speaker vs correction) | 2 over 1 shared | partial | ~250 | Should-fix |
| 4 | Hand-rolled atomic writes bypassing `atomic_io` | 15 files | **yes** | ~180 | Should-fix |
| 5 | Filter magnitude/biquad math (RBJ + analog prototype) | 5 (4 Py + 1 JS) | no | ~150 | **Blocker-adjacent** |
| 6 | `_finite_float`/`_finite_number`/`_positive_float` | 25 | **yes** (`json_fields`) | ~150 | Should-fix |
| 7 | Single-key env-file readers (Python) | 8 | **yes** (`env_file.read_value`) | ~130 | Should-fix |
| 8 | `commission_load` / `startup_load` statefile skeleton | 2 | no | ~120 | Should-fix |
| 9 | multiroom leader/active-leader/follower stash+apply ladder | 3 | no | ~110 | Should-fix |
| 10 | `_utc_now()` / `_now_iso()` timestamp helpers | 20 (3 formats) | no | ~60 | Should-fix |
| 11 | crossover_v2 prescription twins (blend vs driver) | 2 | no | ~130 | Should-fix |
| 12 | bash `env_file_value` awk readers | 3 (drifted) | half (`jasper-env-file.sh` write-only) | ~60 | Should-fix |
| 13 | `start_supervisor()` in 3 control supervisors | 3 | partial (`supervisor_runtime`) | ~54 | Nit |
| 14 | `_send_json`/`_send_html` 1-line delegations + control's own copy | 14 | **yes** (`_common`) | ~45 | Nit |
| 15 | `/etc/camilladsp/outputd-cutover.yml` + 136 `/var/lib/jasper*` path literals | 255 sites | no | 0 (SSOT) | Should-fix |
| 16 | `event=` emitted by `%s` format instead of `log_event()` | 59 sites | **yes** | 0 (consistency) | Should-fix |
| 17 | `systemctl show` unit readers | 2 | **yes** (`service_units`) | ~30 | Nit |
| 18 | Kind+schema JSON SSOT loaders (`load_*_state`) | 5 | no | ~40 | Nit |
| 19 | Rust `env_u32` same name, opposite contract | 2 | **yes** (`jasper_env`) | ~15 | Should-fix |
| 20 | Wake-corpus script helpers (`_resolve_wav_path` etc.) | 3+2+2 | no | ~65 | Nit |

---

## 1. Wizard `main()` — 742 LOC of boilerplate, ~500 of it unreachable

`jasper-web.service:89` and `jasper-web-streambox.service:25` both run
`python -m jasper.web`, which builds servers via each module's `make_server()`
(`jasper/web/__main__.py:78` `WizardSpec.make_server`). The per-module `main()` is a
second, parallel entry path.

| Module | `main()` LOC | Reached by | Verdict |
|---|---|---|---|
| `jasper/web/spotify_setup.py:1421` | 49 | `jasper-web` console script — **nothing invokes it** | duplicate-to-delete |
| `jasper/web/sound_setup.py:5476` | 46 | `jasper-sound-web` — no caller anywhere | duplicate-to-delete |
| `jasper/web/wake_corpus_setup.py:1263` | 169 | `jasper-wake-corpus-web` — only a docstring mentions it (`:43`) | duplicate-to-delete |
| `jasper/web/google_setup.py:1079` | 44 | none (only `assert callable(...)`, `tests/test_web_google_setup.py:54`) | duplicate-to-delete |
| `jasper/web/home_assistant_setup.py:1328` | 27 | none | duplicate-to-delete |
| `jasper/web/rooms_setup.py:1771` | 20 | none | duplicate-to-delete |
| `jasper/web/transit_setup.py:1656` | 38 | none | duplicate-to-delete |
| `jasper/web/voice_setup.py:1758` | 45 | none | duplicate-to-delete |
| `jasper/web/wake_setup.py:1073` | 39 | none | duplicate-to-delete |
| `jasper/web/wifi_setup.py:1479` | 24 | none | duplicate-to-delete |
| `bluetooth_setup:1303` / `chat_setup:261` / `correction_setup:5048` / `system_setup:209` | 57/52/76/56 | real `ExecStart=` in `deploy/jasper-*-web.service` | canonical (4 survivors) |

**Blocker (debuggability):** `pyproject.toml:176` maps `jasper-web = "jasper.web.spotify_setup:main"`.
Every Pi therefore has `/opt/jasper/.venv/bin/jasper-web`, and running it starts *only the
Spotify wizard* on 8765 while logging `"jasper-web listening on http://%s:%d"`
(`spotify_setup.py:1461`). The actual jasper-web daemon is `python -m jasper.web`. A
human or agent debugging "is jasper-web up" gets a wrong answer from the obvious command.

Mechanical corroboration: `sound_setup.main ↔ voice_setup.main` j=0.78,
`transit ↔ voice` j=0.86, `home_assistant ↔ transit` j=0.84 (AST-shingle Jaccard).

**Convergence:** delete the 10 unreachable `main()`s and the 3 dead `[project.scripts]`
rows; fold the 4 survivors onto one `_systemd.run_wizard(prog, port_env, default_port,
make_server, **paths)`. Rename or drop the `jasper-web` script name either way.

## 2. `scripts/s0-sync-bench.sh` is a fork of `scripts/multiroom-spike.sh`

Both self-declare "*THROWAWAY*" in their own headers (`s0-sync-bench.sh:8`,
`multiroom-spike.sh:8`). They share `usage()` byte-for-byte (j=1.00,
`s0-sync-bench.sh:626` ↔ `multiroom-spike.sh:506`) and `write_server_conf`
(`:293` ↔ `:271`). Their Python halves are likewise a fork:
`scripts/s0-sync-measure.py:22` says "*from `scripts/multiroom-spike-measure.py` acoustic mode*".

Total 2,150 LOC (669+563+422+496) plus `tests/test_multiroom_spike_script.py`,
`tests/test_make_click_track.py`, `tests/test_docs_impact.py`,
`tests/test_script_help_excludes_spdx.py` pinning their `--help` text.
Verdict: **duplicate-to-delete** (both, once their questions are answered — they are
declared throwaway and the tests pin prose, which AGENTS.md forbids anyway).

## 3. Two session-bundle stores over one shared manifest layer

`jasper/audio_measurement/bundles.py` (423 L) is the shared artifact-manifest layer, and
both consumers import it — that half **is** converged (`correction/bundles.py:124`
`record_artifact` and `:170` `write_json_artifact` are thin legacy-schema shims;
justified).

The *store skeleton* above it is forked:

| Function | active_speaker | correction | Same? |
|---|---|---|---|
| `summarize_bundle` | `bundles.py:963` | `bundles.py:207` | same skeleton, different derived flags |
| `list_bundles` | `:996` | `:284` | near-verbatim (dupscan j=0.77 on the dir iterator) |
| `latest_bundle` | `:1010` | `:806` | verbatim |
| sorted-dirs | `_iter_bundle_dirs:297` | `_sorted_bundle_dirs:263` | drifted twin |
| `_bundle_byte_size` | `:286` | `:75` (adds `max_entries` cap) | drifted twin |
| `BundleError` | re-exported | re-exported | shared ✓ |

Roots differ (`/var/lib/jasper/active_speaker/sessions` vs
`/var/lib/jasper/correction/sessions`), so the *stores* are justified-separate; the
skeleton is not.

**Convergence:** move `list_bundles` / `latest_bundle` / sorted-dirs / byte-size into
`audio_measurement/bundles.py`, parameterized by a `summarize(bundle_dir) -> dict`
callback. ~250 LOC.

## 4. Atomic writes: 234 correct uses, 15 files that bypass the module

`jasper/atomic_io.py` is unambiguously canonical (`atomic_write_text:232`,
`atomic_write_json:341`, `locked_update_env_file:478`, `locked_transform_env_file:519`);
234 call sites across `jasper/`. Bypasses:

| Site | What it re-implements | Verdict |
|---|---|---|
| `jasper/dsp_apply.py:312` `_atomic_write_json` | `atomic_write_json` verbatim (`indent=2, sort_keys=True` + `\n`) — **and the file already imports `atomic_write_text` at `:43`** | duplicate-to-delete |
| `jasper/correction/replay_artifacts.py:69` `_atomic_write_json` | same; module never imports `atomic_io` | duplicate-to-delete |
| `jasper/active_speaker/commission_ramp.py:175` | tempfile+chmod 0o640+replace | duplicate-to-delete (`mode=0o640`) |
| `jasper/wifi_guardian_persistence.py:185` | mkstemp+fsync+chmod+replace+unlink | duplicate-to-delete (`durable=True, mode=0o600`) |
| `jasper/voice/model_discovery.py:282` | `os.open`+`O_TRUNC`+json+replace | duplicate-to-delete |
| `jasper/fanin/latency_mode.py:99`, `jasper/cli/airplay_mode.py:55`, `jasper/peering/config.py:200`, `jasper/model_downloads.py:119,286`, `jasper/active_speaker/{speech_stimulus:150,230, web_measurement:311, bundles:631}` | `.tmp`+`os.replace` | duplicate-to-delete |
| `jasper/wake_events.py:268`, `jasper/cues/generator.py:151`, `jasper/cli/noise_capture.py:187`, `jasper/web/volume_floor_tone.py:96` | **4 copies of "write a WAV atomically"** | drifted-twin → one `atomic_write_wav()` |
| `jasper/active_speaker/camilla_yaml.py:4025` `_atomic_write_text` | 1-line mode preset over the canonical | earns-its-keep |
| `jasper/control/server.py:633` `_atomic_rewrite_env` | 1-line delegation, docstring says "*Compatibility patch seam*" | patch seam — delete with the test that needs it |
| `jasper/control/debug_control.py:55` `_atomic_write` | docstring: "*Mirrors `server._atomic_rewrite_env`; duplicated (not imported) to avoid a circular import*" | drifted-twin — the stated reason is stale: `control/` already imports `web._common` at 4 sites |

Also: `jasper/web/_common.py:469 write_env_file` re-implements
`atomic_io._format_env_text:391` (identical newline guard), and `:508 write_json_file`
re-implements `atomic_write_json`.

## 5. Filter-magnitude math: five implementations, one guarded pair — the DSP-path fork

This is the one duplicate that can make two JTS surfaces disagree about what the speaker
will actually do.

| Impl | Domain | Guard |
|---|---|---|
| `jasper/sound/profile.py:905` `_biquad_coeffs` | digital RBJ @ 48 k, all 6 types | CI parity vs JS **and** vs CamillaDSP's own coeffs (`scripts/check-peq-parity.mjs`, `.github/workflows/tests.yml:419`) ✓ |
| `deploy/assets/sound-profile/js/eq-math.js:35` `biquadCoeffs` | same, JS | same guard ✓ |
| `jasper/active_speaker/linearization_fit.py:327` `_highshelf_response_db` | digital RBJ Highshelf, vectorized | **none**. Docstring admits the fork: "*Separate from `sound.profile._filter_response_db` — Highshelf-only, vectorized*" |
| `jasper/active_speaker/crossover_v2/feature_optics.py:99` `biquad_peaking` | digital RBJ peaking → (b, a) | **none** |
| `jasper/bass_extension/alignment.py:119,86,28` `peaking_response_db` / `butterworth_highpass_db` / `linkwitz_transform_params` | **analog prototype**, not digital | **none** — will disagree with the emitted digital filter near Nyquist |

Emission-side primitives (`jasper/camilla_emit.py:105,154,175,192`) are correctly shared
by both YAML emitters — that half earns its keep.

AGENTS.md: "*Two implementations of one concern in reach: converge them or open an issue
— never add a third.*" There are five. Severity is high because the prediction feeds
prescriber decisions and the wizard graph, both of which drive DSP writes.

**Convergence:** vectorize `sound/profile._biquad_coeffs` + its magnitude evaluator into
one `jasper/dsp_numpy.py` (which today has only `butter2_highpass_sos`/`sosfilt`, 159 L),
route `linearization_fit` and `feature_optics` onto it, and either fold
`bass_extension/alignment` onto the digital form or extend `check-peq-parity.mjs` to
cover the analog-vs-digital delta it deliberately accepts.

## 6. Number coercion — 25 hand-rolled copies of a helper that exists

`jasper/json_fields.py:23 finite_float` + `:40 JsonFields` is canonical and used by 21
modules. Twenty-five others hand-roll it with **five** incompatible signatures:

- returns `None`: `active_speaker/{driver_pad:58, measurement_document:46, driver_safety:326, design_draft:196, source_state:282}`, `control/{audio_incidents:53, audio_health:547}`, `bass_extension/{limiter_evidence:321, bench/manifest:164}`
- raises with `field_name`: `active_speaker/{profile:158, camilla_yaml:404, commissioning_service:92, bench/derivation:179}`
- raises with `field` + `reason`: `crossover_v2/{alignment_prescription:188, blend_prescription:683, topology_prescription:225}`
- raises with `field` only: `crossover_v2/driver_prescription:639`, `audio_measurement/{playback:1062, excitation_admission:45}`
- `_positive_float` variants: `driver_pad:44`, `driver_safety:340`, `design_draft:225`, `profile:172`, `limiter_evidence:328`

Confirmed identical bodies by AST hash: `alignment_prescription:188 ↔ topology_prescription:225`
(16 L each), `driver_pad:58 ↔ design_draft:196` (12 L each).

**Convergence:** one `json_fields.finite_number(value, *, field, reason=None,
required=True)`; delete the 25.

## 7. Env-file reading — three canonical modules and eight private forks

Three shared modules, each with a stated scope, all legitimate:

| Module | Scope | Consumers |
|---|---|---|
| `jasper/env_load.py:157` `parse_env_text` / `:250` `load_env_files` | read the systemd-equivalent union into `os.environ` | 29 |
| `jasper/env_file.py:39` `parse_env_lines` / `:62` `read_value` / `:74` `upsert` / `:110` `remove` | order-preserving single-key upsert | 9 |
| `jasper/atomic_io.py:478/519` `locked_*_env_file` | locked whole-file RMW | several |

Eight modules re-implement `env_file.read_value` (last-wins, strip quotes) inline:

| Site | LOC | Notes |
|---|---|---|
| `jasper/fanin/latency_mode.py:84` | 12 | last-wins + quote strip — exactly `read_value` |
| `jasper/audio_quality.py:88` `_read_env_value` | 14 | same, minus quote strip (**drift**) |
| `jasper/mic_mute_persistence.py:52` | 20 | same + bool vocabulary |
| `jasper/tool_state.py:72` | 18 | same + CSV split |
| `jasper/ring_assets.py:240` | 12 | same, builds a dict |
| `jasper/wifi_guardian_persistence.py:93` `_parse_env_line` | 15 | same, deliberately keeps value whitespace (PSKs) — **document, then reuse with a flag** |
| `jasper/enhanced_aec.py:285` | 16 | same, sourced by `deploy/install.sh` too |
| `jasper/accessories/mic_env.py:115` | 12 | docstring says it "*Mirrors `jasper.config._env_mapping`'s validation*" — a fourth vocabulary |

Not duplicates (verified, leave alone): `service_units.py:120` (systemd `show` blocks),
`web/_unit_snapshot.py:74`, `cli/doctor/_shared.py:151` (`systemctl show -p Environment`
shlex tokens), `multiroom/cascade_timeline.py:93`, `route_latency/tap_client.py:144`
(logfmt telemetry), `config.py:143` (`k=v` list values).

Also: `jasper/web/_common.py:429 read_env_file` is a 1-line delegation to
`env_load.read_env_file_or_warn` — fine.

## 8. `commission_load.py` / `startup_load.py` — parallel load/rollback state machines

`jasper/active_speaker/commission_load.py` (1,276 L) and `startup_load.py` (1,422 L).
Confirmed byte-identical after normalization (AST hash, 26 L and 14 L clusters):

- `load_commission_load_state:129` ≡ `load_startup_load_state:129`
- `_record_commission_state:157` ≡ `_record_state:157`
- plus `_utc_now`, `_normalise_issue`, `_base_state`/`_commission_base_state`

The blocker/evidence halves genuinely diverge (per-driver vs whole-graph), so this is a
**drifted-twin**, not a delete. Extract the ~120-LOC statefile skeleton
(`load_kinded_state` / `record_kinded_state`) — which also subsumes concern #18.

## 9. multiroom: three copies of the CamillaDSP role-swap ladder

| | leader | active leader | follower |
|---|---|---|---|
| module | `leader_config.py` (392 L) | `active_leader_config.py` (504 L) | `follower_config.py` (587 L) |
| `_camilla()` | `:76` | `:132` | `:103` |
| `read_stash` | `:97` | — | `:112` |
| `_write_stash` | `:106` | — | `:121` |
| `_clear_stash` | `:110` | — | `:125` |
| precheck / apply / restore | `:151/:225` | `:141/:364/:467` | `:134/:259/:559` |
| `*_sync` wrappers | `:387,:391` | `:495,:499,:503` | `:578,:582,:586` |

`read_stash`/`_write_stash`/`_clear_stash`/`_camilla` are verbatim across leader and
follower (differing only in the `PRIOR_STASH` constant). The three
precheck/apply/restore ladders share shape but not body. Verdict: drifted-twin.
Extract a `RoleSwap(stash_path)` holder for the stash + camilla factory (~110 LOC);
leave the three ladders separate unless a later pass proves them isomorphic.

## 10. `_utc_now()` — 20 copies, 3 formats

15 byte-identical `return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())`:
`active_speaker/{driver_base_trim:102, baseline_profile:213, web_measurement:41,
seat_level_reference:157, calibration_level:41, staging:126, crossover_preview:62,
path_safety:144, model_error_store:97, design_draft:135, startup_load:80, measurement:82}`,
`dsp_apply:165`, `enhanced_aec:85`, `output_hardware:433`.

Three *different* formats alongside them — a real interop hazard for artifacts that get
compared: `voice/model_discovery.py:55` (`datetime.now(UTC)…replace("+00:00","Z")`),
`sound/profile.py:101` (`…isoformat()`, **no `Z`**), `volume_diagnostics.py:49` and
`assistant_loudness.py:527` (`_now_iso`, microseconds retained), `wake_events.py:261`
`_now_iso`. **Convergence:** one `jasper/json_fields.py` (or a 10-line `jasper/clock.py`)
`utc_now_iso()`; pick the `Z` form the artifacts already use.

## 11. crossover_v2 prescription twins

AST-exact and near-exact pairs between `driver_prescription.py` and `blend_prescription.py`:

| Function | driver | blend | Similarity |
|---|---|---|---|
| `_parse_prescription` | `:1252` (65 L) | `:962` (61 L) | j=0.80 |
| `_prescriber` | `:1108` | `:919` | **exact** (24 L) |
| `_rationale` | `:1134` | `:945` | **exact** (17 L) |
| `_finite_number` | `:639` | `:683` | **exact** (19 L) |

Same pattern between `alignment_prescription.py:420` and `topology_prescription.py:264`
(`_read_artifacts`, exact, 31 L) and their `_finite_number`s (`:188`/`:225`, exact, 16 L).
**Convergence:** one `crossover_v2/_prescription_io.py` for parse/prescriber/rationale/
artifact-read; ~130 LOC.

## 12. bash: the env-file *write* half is shared, the *read* half forked three ways

`deploy/lib/jasper-env-file.sh` is canonical for writes (`jasper_env_quote_value:34`,
`jasper_env_file_set:77`, `:135`, `:157`) and is sourced by 5 files, with a good
docstring on why (`printf %q` corrupts ALSA pcm specs). It has **no read helper**, so:

| Site | Impl | Drift |
|---|---|---|
| `deploy/bin/jasper-audio-hardware-reconcile:504` `env_file_value` | awk on global `$ENV_FILE`, strips ws+quotes | — |
| `deploy/bin/jasper-audio-hardware-reconcile:549` `env_file_value_from` | **the same function with a `$file` parameter** (j=0.68) | pure duplication in one file |
| `deploy/bin/jasper-apply-airplay-mode:215` `env_file_value` | different awk; **also strips trailing `#…` comments** | semantic drift on the same file class |

Also verbatim-duplicated bootstrap: `load_env_file_lib` at
`jasper-aec-reconcile:121` ≡ `jasper-audio-hardware-reconcile:208` (j=0.82; differs only
in the program name in the error string), each carrying a 6-line comment explaining why
its lookup order differs from the *other* lib's.

And `resolve_speaker_hostname` is defined twice: `scripts/deploy-to-pi.sh:123`,
`scripts/onboard.sh:350` — despite `scripts/_lib.sh` (649 L) being sourced by 37 of 50
`scripts/*.sh`.

**Convergence:** add `jasper_env_file_get FILE KEY` to `deploy/lib/jasper-env-file.sh`
(one awk, the reconciler's semantics) and `jasper_load_lib NAME` for the bootstrap; move
`resolve_speaker_hostname` into `_lib.sh`.

## 13–20 (short form)

- **13. `start_supervisor()` ×3** — `control/{grouping_supervisor:623, shairport_supervisor:460, system_supervisor:633}`, AST-exact 27 L each. `supervisor_runtime.py` already owns the loop/thread mechanics; the start wrapper is the last un-extracted 54 LOC. Give it `start_supervisor(env_name, factory, name, disabled_event, crash_event)`.
- **14. `_send_json`/`_send_html`** — 13 one-line delegations to `_common.send_json_response`/`send_html_response` across `web/{wifi:1312, spotify:951, sound:4716, bluetooth:528, home_assistant:1020, sources:697, google:755, correction_setup:3907,3932, rooms:492}`. Separately, `control/server.py:1597` **hand-rolls the body** of `_common.send_proxy_json:1458` (identical headers) rather than importing it — and `control/` already imports `web._common` at `usb_gadget_forensics:13`, `debug_control:37`, `aec_endpoints:199,389`, so the usual "different privilege domain" defence does not apply.
- **15. Path constants** — no `jasper/paths.py`. 136 distinct `/var/lib/jasper*` literals over 255 Python sites, plus 89 distinct literals in `deploy/**.sh|.service`. Worst offenders: `"/var/lib/jasper/voice_provider.env"` in 7 places (`env_load:57`, `voice/provider_state:68`, `cli/doctor/privsep:109,148,202,260`, `cli/doctor/env:30`); `/etc/camilladsp/outputd-cutover.yml` in 6 (`active_speaker/runtime_contract:125`, `sound/camilla_yaml:57`, `control/state_aggregate:86`, `correction/session:195`, `correction/status:103`, `web/correction_setup:3691`). This removes no LOC but is the single biggest single-source-of-truth gap I found.
- **16. `event=` bypassing `log_event`** — `jasper/log_event.py:143` is canonical (202 importers), yet 59 sites emit `"event=… %s"` through raw `logger.info/warning`, concentrated in `active_speaker/{commission_load, startup_load, staging, camilla_yaml}`. Those lines skip the logfmt escaper (`_escape_logfmt_text:56`) and JSON mode (`json_mode_enabled:77`), so a value with a space silently breaks the field grammar an agent greps.
- **17. `systemctl show`** — `jasper/service_units.py:167 read_unit_states` is canonical (returns `None` = unknown). `control/system_metrics.py:713 read_service_states` keeps its own props list + subprocess call and returns `{}` on failure — same fact, opposite unknown-vs-empty semantics, in the module that already imports `service_units` at `:40` and delegates two other helpers there (`:704`, `:709`). Drifted twin.
- **18. Kinded JSON SSOT loaders** — AST-exact ×3: `active_speaker/{driver_base_trim:131 load_base_trim, seat_level_reference:161 load_seat_level_reference, repeat_floor:129 load_repeat_floor}` (19 L each: read → `isinstance(dict)` → `kind` check → `artifact_schema_version` check → return). Subsume into the #8 extraction.
- **19. Rust `env_u32`** — `rust/jasper-fanin/src/config.rs:1230` means "non-negative", `rust/jasper-outputd/src/config.rs:922` means "must be > 0 or bail". Same name, opposite contract, both over the shared `jasper_env::env_parse`. Also `outputd:952 env_i64` and `:962 env_bool` hand-roll what `jasper_env::env_parse` does. Rename to `env_u32_nonneg` / `env_u32_positive` (fanin already has `env_u32_positive:1247` with a *third* meaning: falls back with a WARN). The rest of the Rust crate graph is **exemplary** — see below.
- **20. Wake-corpus scripts** — AST-exact: `_resolve_wav_path` ×3 (`scripts/{_audit_wake_corpus:139, _analyze_wake_corpus_quality:164, _waveform_fusion_experiment:205}`, 19 L each), `_safe_to_remove_output` ×2 (`_build_wake_feature_bank:193` / `_build_wake_negative_feature_bank:346`, 15 L), `_feature_file` ×2 (`_prepare_wake_training_workdir:133` / `_prepare_wake_livekit_smoke:112`, 11 L), and `main()` j=0.60 between the two feature-bank builders. `scripts/_wake_pipeline_common.py` already exists as the home for these.

## Other exact-duplicate clusters worth a line

From the AST scan, same-file pairs that are pure copy-paste and cheap to fold:

| Pair | LOC each |
|---|---|
| `active_speaker/commissioning_evidence.py:2444 ↔ :2661` `from_mapping` | 49 |
| `fanin_coupling.py:711 resolve_ring_slots ↔ :866 resolve_outputd_ring_slots` | 34 |
| `web/correction_setup.py:3376 _handle_interpret ↔ :3410 _handle_propose` | 32 |
| `active_speaker/driver_protection.py:230 ↔ :263` (hp floor / lp ceiling) | 31 |
| `web/correction_setup.py:1192 _schedule_measurement_sweep ↔ :1219 _schedule_repeat_sweep` | 23 |
| `web/correction_tuning.py:292 interpret ↔ :316 propose` | 22 |
| `commissioning_evidence_store.py:1170 ↔ :1224` `reopen_complete_*_anchor` | 22 |
| `web/correction_setup.py:2998 _handle_crossover_v2_complete ↔ :3022 _handle_crossover_v2_retake` | 22 |
| `active_speaker/commissioning_evidence.py:184 _strict_object ↔ commissioning_receipt.py:160 _strict_serialized_object ↔ audio_measurement/evidence_identity.py:96` (×3, j=0.90) | 20 |
| `voice/openai_session.py:982 start ↔ gemini_session.py:698 start`; `:1062 ↔ :800 request_reconnect_now` | 17 / 14 |
| `control/audio_incidents.py:617 _duration_label ↔ audio_health.py:1605` | 12 |
| `active_speaker/bundles.py:996 list_bundles ↔ correction/bundles.py:284` | 12 |
| `web/transit_setup.py:269 _validate_google_routes_key ↔ voice_setup.py:1089 _validate_key` | 11 |
| `control/control_token.py:107 ensure_token ↔ household_credential.py:115 ensure` | 28 |
| `audio_measurement/wired_level_meter.py:176 start ↔ wired_capture.py:330 start` | 19 |
| `active_speaker/bench/derivation.py:161 _parse ↔ bass_extension/bench/derivation.py:194 ↔ bass_extension/bench/activation.py:87` (×3) | 10 |
| `cli/audition.py:274 main ↔ cli/measure.py:1126 main` | 15 |

**Tests** (defer to the tests beat; #4030 in flight): same-file example clusters that
should be one `@parametrize` — `test_active_speaker_runtime_contract.py:2602…3048` (×8,
11 L each, 77 L removable), `test_crossover_v2_verification.py:1720…2066` (×5, 52 L),
`test_web_rooms_setup.py:3069…3124` (×4, 48 L), `test_doctor_renderers.py:170…264` (×4),
`test_multiroom_config.py:263…472` (×4). Cross-file: `tests/active_speaker_fixtures.py:372
standard_measurements` ↔ `test_active_speaker_baseline_profile.py:287 _measurements`
(57 L, exact — the fixture module already exists).

## Earns its keep — verified converged, do not "fix"

Reporting these explicitly so a later pass does not re-open them:

- **CamillaDSP emission.** `jasper/camilla_emit.py` (399 L) holds the filter/mixer/pipeline primitives; `camilla_config_contract.py` (742 L) the device defaults and clamps; both `sound/camilla_yaml.py:341 emit_sound_config` and `active_speaker/camilla_yaml.py` import them, and the active-speaker emitter *calls* `emit_sound_config` (`active_speaker/camilla_yaml.py:56`). Two emitters, one primitive layer — correct. (The 7 `emit_active_speaker_*_config` functions inside the 4,028-line module are a file-size problem, not a duplication one.)
- **DSP measurement math.** `audio_measurement/analysis.py:144 smooth_fractional_octave` and `deconv.py:216 magnitude_response` are used by 23 modules across active_speaker/, correction/, bass_extension/. `olive_metrics.py:108` and `crossover_v2/feature_classifier.py:851` both route through them rather than forking.
- **The doctor.** One `_run` (`cli/doctor/_shared.py:129`) behind 40 call sites; one registry (`_registry.py`); one `CheckResult` (`doctor_contract.py`). ADR-0233. (Nit: 3 checks still call `subprocess.run` directly — `correction.py:629`, `renderers.py:953,1016` — with their own timeouts. One-line fixes.)
- **Rust crate graph.** `jasper-clock` (pure spa_dll port) vs `jasper-host-clock` (UAC2 pitch servo that *composes* it) are **not** duplicates — `jasper-host-clock/Cargo.toml` depends on `jasper-clock`. `jasper-ring`, `jasper-resampler`, `jasper-tts-protocol`, `jasper-env` are each consumed by both daemons with a documented rationale in `Cargo.toml`. Best-factored area in the repo.
- **Web CSS/JS.** Across 19 per-page stylesheets + `app.css` (824 L) only **2 selectors** are defined in ≥3 files and **9** identical selector+body rules are duplicated (3 of those within `correction/`'s own two files). `deploy/assets/shared/js/` (1,145 L) is imported by 42 of 54 page modules; `escapeHtml`/`getJson`/`fetchJson`/`debounce` have **zero** reimplementations outside it.
- **Artifact manifests.** One implementation (`audio_measurement/bundles.py:239 read_artifact_manifest`), consumed by 8 modules.
- **PEQ parity.** The JS↔Python↔CamillaDSP triple is guarded in CI (`scripts/check-peq-parity.mjs`, plus `check-balance-trim-parity.mjs` / `check-sensitivity-trim-parity.mjs`, wired at `.github/workflows/tests.yml:419-422`). This is the model the #5 forks should follow.
- **`supervisor_runtime.py`, `service_units.py`, `env_file.py`, `json_fields.py`, `atomic_io.py`, `log_event.py`, `_lib.sh`, `jasper-env-file.sh`** — all exist, all correct, all under-consumed. Nearly every finding above is "a canonical module exists and N callers don't use it", not "no canonical module exists". That is a much cheaper problem than it looks.

## Adjacent observations (other beats' territory)

- **Config sprawl:** 562 distinct `JASPER_*` keys read across `jasper/`; `config.py` names 80; 156 files call `os.environ` directly. `docs/extensibility.md` sanctions three patterns, so this is not per se duplication — but 562 is a number worth someone owning.
- **Boundary leak:** `jasper/control/state_aggregate.py:799` imports `jasper.web.sources_setup._gather_state` — a daemon reaching into a wizard's private function.
- **In flight, not re-reported:** ADR-0235 / #4027 covers the attached-hardware fact duplication (`/proc/asound` read from 21 Python files, `output_hardware` vs `audio_hardware/dac` vs the bash reconciler's `sed` extraction of `output_hardware.json`) — my probes confirmed the gaps that ADR already names (G1–G4), so I have not re-listed them. #4031 covers web UI cleanup; #4030 covers test doubles + doctor split + the tuning-CLI "stdout is the answer" wave, which is why the `cli/_report.py` (8 users) vs 87 raw `json.dumps` and `cli/_logging.py` (10 users) vs 16 `logging.basicConfig` split is listed only here as context, not as a new finding.

## Top 20 (single list, highest value first)

1. Delete the 10 unreachable wizard `main()`s + 3 dead `[project.scripts]`; fix the `jasper-web` script that starts only the Spotify wizard. (`pyproject.toml:176`)
2. Delete `scripts/s0-sync-{bench.sh,measure.py}` and `scripts/multiroom-spike{.sh,-measure.py}` + their 4 test files (2,150 L, self-declared throwaway, one a fork of the other).
3. Converge the five filter-magnitude implementations onto one vectorized RBJ evaluator in `jasper/dsp_numpy.py`; extend the CI parity fixture to cover it. (#5)
4. Route the 15 hand-rolled atomic writes onto `jasper/atomic_io.py`; add one `atomic_write_wav`. Start with `dsp_apply.py:312` and `correction/replay_artifacts.py:69`. (#4)
5. Lift `list_bundles`/`latest_bundle`/sorted-dirs/byte-size into `audio_measurement/bundles.py`. (#3)
6. Migrate the 25 `_finite_*` copies onto `jasper/json_fields.py`. (#6)
7. Migrate the 8 inline env readers onto `jasper/env_file.py:62 read_value`. (#7)
8. Extract the `commission_load`/`startup_load` statefile skeleton; fold in the 3 kinded SSOT loaders. (#8, #18)
9. Create `jasper/paths.py` (or extend `config.py`) for the 136 `/var/lib/jasper*` + 6 `outputd-cutover.yml` literals; teach `deploy/lib/` to read them. (#15)
10. One `utc_now_iso()`; delete 20 copies and settle on one timestamp format. (#10)
11. `crossover_v2/_prescription_io.py` for the driver/blend/alignment/topology prescription twins. (#11)
12. Add `jasper_env_file_get` + `jasper_load_lib` to `deploy/lib/jasper-env-file.sh`; delete the 3 awk readers and 2 bootstrap copies; move `resolve_speaker_hostname` to `_lib.sh`. (#12)
13. Extract the 3 multiroom stash helpers into one `RoleSwap`. (#9)
14. Convert the 59 raw `event=` format strings onto `log_event()`. (#16)
15. `supervisor_runtime.start_supervisor(...)`; delete 3 copies. (#13)
16. Delete the 13 `_send_json`/`_send_html` delegations; point `control/server.py:1597` at `_common.send_proxy_json`. (#14)
17. Delete `system_metrics.read_service_states`; use `service_units.read_unit_states` and decide unknown-vs-empty once. (#17)
18. Rename the Rust `env_u32` twins; delete `outputd`'s `env_i64`/`env_bool` in favour of `jasper_env`. (#19)
19. Fold the wake-corpus script helpers into `scripts/_wake_pipeline_common.py`. (#20)
20. Fold the ~17 same-file copy-paste pairs listed above (largest first: `commissioning_evidence:2444/2661`, `fanin_coupling:711/866`, `correction_setup:3376/3410`).

## Coverage

**Opened and read (not just grepped):** `jasper/{env_load,env_file,atomic_io,log_event,backoff,percentiles,json_fields,dsp_numpy,service_units,camilla_emit}.py`;
`jasper/web/{_common,_systemd,__main__}.py` + the `main()`/`_send_json` region of 14 `*_setup.py`;
`jasper/control/{supervisor_runtime,debug_control,system_metrics,handlers/_base}.py` + `server.py` regions;
`jasper/sound/{camilla_yaml,profile}.py`; `jasper/active_speaker/{camilla_yaml,bundles,round_bank,commission_load,startup_load,driver_base_trim,seat_level_reference,linearization_fit}.py`;
`jasper/{correction,audio_measurement}/bundles.py`; `jasper/audio_measurement/{analysis,olive_metrics}.py`;
`jasper/multiroom/{leader_config,active_leader_config,follower_config}.py`;
`jasper/bass_extension/alignment.py`; `jasper/active_speaker/crossover_v2/feature_optics.py`;
`jasper/cli/{_logging,_report,_refusal}.py`, `jasper/cli/doctor/_shared.py`;
`deploy/lib/jasper-env-file.sh`, `scripts/_lib.sh`, `deploy/bin/{jasper-aec-reconcile,jasper-audio-hardware-reconcile,jasper-apply-airplay-mode}` (regions);
`rust/*/Cargo.toml`, `rust/jasper-{env,clock,host-clock}/src/lib.rs` heads, `rust/jasper-{fanin,outputd}/src/config.rs` env regions;
`deploy/assets/shared/js/` listing + `scripts/check-peq-parity.mjs`; `docs/adr/0235`.

**Scripts written** (in the scratchpad): `dupscan.py` (AST-normalized exact + shingle-Jaccard near-dup, run over `jasper scripts deploy/lib experiments` and again over `tests`), `bashdup.py` (line-shingle Jaccard over 352 bash functions + same-name-across-files), `cli_scan.py` (argparse/logging/report scaffolding census over 95 `jasper/cli` files). Outputs in `dupscan-out.md`, `dupscan-out2.md`.

**Skipped, and why:**
- `jscpd` / `pylint -e R0801` — not installed and installing would touch tracked state; the AST scanner is strictly stronger for Python (it normalizes identifiers and literals, which `R0801` does not).
- `tests/` (960 files, 585 k LOC) — scanned mechanically only; the numbers are handed to the tests beat, and #4030 is in flight.
- `c/jts-ring-ioplug` (7.9 k LOC) — read the crate graph around it but not the C; a ring-buffer-logic comparison against `rust/jasper-ring` needs a C reviewer's pass.
- `jasper/{attribution,route_latency,transit,tools,wake_corpus,research,accessories,bluetooth,xvf,chip_aec}` — covered only by the mechanical scan (no cross-package hits surfaced above threshold); a targeted read of each was out of budget.
- The 7 `emit_active_speaker_*_config` functions inside `active_speaker/camilla_yaml.py` — I confirmed they share the emit primitives but did not diff their bodies pairwise; if a later pass wants file-size reduction, that is where to look.
- `experiments/usb-turntable` — 2.2 k LOC, mechanically scanned, no clusters surfaced.
- Runtime/hardware verification of any claim: none. Every finding is static.
