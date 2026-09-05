# L1-boundaries — the Python import architecture of `jasper/`

Scripts: `scratchpad/L1-boundaries/` (`graph.py` = the graph; `mfas*.py`, `cuts.py`, `layers2.py`,
`localimp*.py`, `privnames.py`, `t18.py`; `jts2.ini` = the import-linter contract; `ilvenv/` =
import-linter 2.15 in a scratch venv). `git status` clean at 2d571e6b8, nothing written to the repo.

## A. Verdict

**`jasper/` has no import-time architecture problem and a severe import-time-avoidance problem.**
The module-level (real, executed-at-import) graph over 749 modules is a DAG except for **one
4-module cycle in `bass_extension/adapters/`** — 3 imports at `adapters/base.py:109-111`, removable
in one PR. Every other cycle anyone has reported exists **only through function-local imports**:
1,708 deferred `import jasper…` statements, which knit 72 modules into one strongly-connected
component whose minimum feedback set is **25–32 edges, not 3–8**. So the question is not "which
imports do I invert" but "which *facts live in the wrong module*" — the deferred imports are the
symptom. Below the module level the picture flips again: at *package* granularity the module-level
graph has a **23-package SCC** (`web ↔ cli ↔ control ↔ voice ↔ active_speaker ↔ tools ↔ config …`),
caused by ~15 nameable module-level edges, ten of which are pure shelving mistakes (`cli/aec_bridge_*`
and `cli/wake_enroll` are libraries; `web/_common` and `web/_systemd` are platform primitives;
`control/{client,uds}` is an IPC client). A 7-layer `import-linter` contract expressing the intended
architecture is **broken by 55 module pairs / 109 chains** at HEAD; three of the four narrow
forbidden contracts are **2–6 direct edges from green** and could be merged as ratchets this week.

## 1. The cycle: three measurements of the same thing

| measurement | modules in the largest SCC | why it differs |
|---|---|---|
| p0-inventory §6 | **34** | its `ImportFrom` handler resolved only `node.module`, never `module + "." + alias`, so `from jasper.multiroom import reconcile` produced an edge to the *package*, not the submodule; relative imports inside `__init__.py` resolved one level too high. Reproduced exactly (`repro_p0.py` → `[34,2,2,2,2]`, 1835 edges). |
| **this lens** | **72** (3,817 edges) | submodule-resolved; TYPE_CHECKING edges excluded; `try:`-wrapped module-level imports counted as module-level |
| p1-T14-3 | 99 | same phenomenon, wider resolution (ancestor-package edges make mine 80 too) |
| **module-level (top-level) imports ONLY** | **4** — `bass_extension.adapters.{base,sealed,ported,passive_radiator}` | this is what actually executes at import time |

T14-3/T14-2/T14-1 say "no top-level cycle"; precisely, there is **exactly one**, and it is not in
`active_speaker/`: `jasper/bass_extension/adapters/base.py:109-111` imports `.passive_radiator`,
`.ported`, `.sealed` at the *bottom* of the file to build a registry, while each of the three imports
`from .base import …` at its top (`sealed.py:23`, `ported.py:22`, `passive_radiator.py:16`). It works
only because line 109 runs after `base`'s definitions.

### The 72-member SCC and its shape

349 internal edges: **180 module-level, 169 function-local**. The module-level subgraph of those 72
nodes is *acyclic*, so every cycle passes through ≥1 deferred import and a feedback set can be drawn
entirely from the 169. Members: 25 × `active_speaker.*`, 11 × `multiroom.*`, 6 × `sound.*`,
5 × `fanin.*`, 2 × `bass_extension.*`, 2 × `audio_hardware.*`, plus `camilla`,
`camilla_config_contract`, `camilla_stereo_prefix`, `dsp_apply`, `output_topology{,_runtime}`,
`output_hardware`, `fanin_coupling`, `ring_assets`, `audio_runtime_plan`, `transport_coherence`,
`tts_routing`, `source_intent`, `accessories.reconcile`, `local_sources.markers`,
`audio_measurement.peq`, `assistant_volume`, `env_load`, `volume_{coordinator,curve,persistence}`,
`cli.active_speaker` (full list in `scratchpad/L1-boundaries/rank.json`).

### Minimum feedback set — measured, not guessed

* **Lower bound 25**: 25 mutually-importing pairs inside the SCC, pairwise edge-disjoint (`mfas2.py`).
* **Best found 32** (Eades-Lin-Smyth + 60k-iteration insertion local search, `mfas3.py`): 23 deferred
  + 9 module-level edges. **The 3–8 edge premise is false for the combined graph.**
* **For the graph that actually executes, the minimum feedback set is 3** — C1 below.

### Ranked cuts, each verified by re-running Tarjan with the edge removed (`cuts.py`)

| # | edge(s) — file:line | concrete refactor | SCC after (cum.) |
|---|---|---|---|
| **C1** | `bass_extension/adapters/base.py:109-111` → `.sealed/.ported/.passive_radiator` (module-level) | move the 3-entry `ADAPTERS` registry to a new `adapters/registry.py` that imports all four | **module-level graph becomes fully acyclic (4 → 0)**; combined 72 |
| C2 | `env_load.py:243,244` → `fanin.coupling_reconcile.OUTPUTD_ENV_PATH`, `multiroom.reconcile.OUTPUTD_GROUPING_ENV_FILE` | move **both path constants down** beside `env_load.BASE_ENV_PATH`; the reconcilers import them. The docstring at `:239-241` already concedes the inversion ("through lazy imports because this module is a leaf"). | 72 → **65** |
| C3 | `camilla_config_contract.py:207-208` → `audio_hardware.dac`, `output_hardware` (vs `audio_hardware/dac.py:19` module-level back) | `_active_camilla_floor` takes `floor_db: float \| None` from its caller; delete the `try/except ImportError` (a defended hypothetical) | 65 |
| C4 | `output_hardware.py:1095,1098` → `active_speaker.runtime_contract`, `output_topology` (vs `output_topology.py:46`) | `_saved_topology_requires_roleful_graph` takes the already-loaded topology + the verdict as parameters | 65 |
| C5 | `volume_curve.py:59` → `sound.settings._settings_path` (vs `sound/settings.py:38` module-level back) | `sound.settings` exports `configured_volume_floor_db()`; `volume_curve` keeps only the curve math. Also fixes the private-name reach (T18 F11 — **confirmed**) | 65 → **61** |
| C6 | `fanin_coupling.py:796` → `multiroom.dac_content_ring.DAC_CONTENT_LANE_ENV` (vs `dac_content_ring.py:41` → `fanin_coupling.RING_SLOT_FRAMES`) | move `DAC_CONTENT_LANE_ENV` to `fanin_coupling` beside the other outputd env keys | 61 |
| C7 | `fanin_coupling.py:981` → `camilla_config_contract.DEFAULT_PLAYBACK_FORMAT` (vs `camilla_config_contract.py:21`) | move `DEFAULT_PLAYBACK_FORMAT` to a format-vocabulary leaf both import | 61 |
| C8 | `fanin/latency_mode.py:365` → `coupling_reconcile.reconcile_auto` | the signature **already** takes `reconcile: Callable \| None`; make it required and pass it from the 2 call sites | 61 → 60 |
| C9 | `source_intent.py:977,1093,1545` → `local_sources.markers`, `accessories.reconcile` | markers/accessories publish into `source_intent`, not the other way; invert both | 60 → **59** |
| C10 | `active_speaker/path_safety.py:666,747` → `environment`, `staging` | pass config text + staged path in (T14-3's find — **confirmed**; `path_safety` then becomes a leaf) | 59 |
| C11 | `active_speaker/runtime_contract.py:925,1066,1203,1250-ish,4028…` (5 deferred edges out) | split `runtime_contract.py` (4,465 LOC) into `runtime_contract/types.py` (the `GRAPH_*` vocabulary + classifiers, 18 inbound edges) and `runtime_contract/queries.py` (the readers that need `sound`/`multiroom`/`bass_extension`) | 59 |

Cheapest-first: **C1** (3 lines, kills the only real cycle), then **C2, C5, C6, C7, C8** (each is
"move one constant" or "pass one argument" — 6 modules leave the SCC for ~40 lines of diff total).
C3/C4/C9/C10 are one-function signature changes. C11 is the only structural one and the only one that
touches a 4k-LOC file. Cuts C1-C11 remove 23 edges and take the SCC 72 → 59: **confirmation that the
remaining knot is `active_speaker ↔ sound ↔ multiroom ↔ fanin` sharing state, not bad imports.**

## 2. Layers: what the code has vs. what it should have

Contract at `scratchpad/L1-boundaries/jts2.ini`, run with import-linter 2.15 from a scratch venv
(`PYTHONPATH=/home/user/JTS lint-imports --config jts2.ini`). **grimp counts function-local imports
too** (3,660 deps vs my 3,817), so import-linter sees the *combined* graph — a layers contract cannot
be made green by deferring an import, which is exactly the property we want.

| layer | members (packages/top-level modules) |
|---|---|
| **L1 platform** | `atomic_io env_load env_file log_event json_fields backoff busctl transition_log flight_recorder debug_mode service_units secret_redaction watchdog doctor_contract percentiles os_fault memory_policy _oom_adj model_downloads http_security` |
| **L2 contracts** (frozen vocabularies, no I/O) | `camilla_config_contract music_sources librespot_state source_state airplay_mode spotify_uri mux_mode_persistence install_profile capture_protocol dsp_numpy wake_legs wake_ports wake_conditions wake_condition_context identity identity_state` |
| **L3 net + hardware** | `mdns avahi_service control_advert usb_network usbgadget wifi_* speaker_name* oauth_redirect *_oauth audio_hardware/ output_hardware mics/ xvf/ usb_mic ring_assets renderer renderer_lanes bluealsa_probe usbsink/ chip_aec/ aec_engines/` |
| **L4 audio-core** | `camilla camilla_emit camilla_stereo_prefix dsp_apply fanin/ fanin_coupling multiroom/ sound/ output_topology* audio_io audio_runtime_* audio_quality audio_profile_state audio_input_view audio_validation* transport_coherence tts_routing measurement_window enhanced_aec aec_sweep route_latency/ local_sources/ volume_* assistant_loudness assistant_volume` |
| **L5 tuning** | `active_speaker/ audio_measurement/ correction/ bass_extension/ calibration_agent/ attribution/ research/` |
| **L6 daemons + integrations** | `voice_daemon config voice/ control/ mux source_* bus spotify_* bluetooth/ accessories/ peering/ wake* wake_corpus/ vad audio_buffer openwakeword_guard aec_ready mic_* cues/ tools/ transit/ usage timers conversation_history accounts google_* home_assistant weather subway citibike location_state tool_*` |
| **L7 surfaces** | `web/ cli/` |

### Violation counts at HEAD (each contract run in isolation)

| contract | type | broken pairs | illegal chains | distance to green |
|---|---|---|---|---|
| `jasper-layers` | layers | 55 | 109 | 40 module-level + 78 deferred upward edges (my graph); after the 4 relocations in §3 it is **25 module-level** |
| `surfaces-are-leaves` (nothing below L7 imports `web`/`cli`) | forbidden, direct-only | 6 | 14 | **6 edges** |
| `platform-is-a-leaf` | forbidden, direct-only | 2 | 2 | **2 edges — both are C2** |
| `contracts-are-leaves` | forbidden, direct-only | 4 | 4 | **4 edges** |
| `no-cycles-in-adapters` | independence | 6 | 7 | **C1** |

The overlap with §1 is exact where it matters: `platform-is-a-leaf`'s only two violations are
`env_load → fanin` and `env_load → multiroom` (**C2**); `contracts-are-leaves` is
`camilla_config_contract → {audio_hardware, output_hardware}` (**C3**), `source_state → bluealsa_probe`,
`source_state → fanin.status`, `wake_ports → aec_sweep`, `identity → peering`;
`no-cycles-in-adapters` is **C1**. `surfaces-are-leaves` is disjoint from §1 — a different disease.

### The 23-package module-level SCC (import-linter's real complaint)

15 module-level edges hold it together; 10 are shelving mistakes:

| edge (module-level) | verdict |
|---|---|
| `wake_corpus/bridge_session.py:51,56,60,78` → `cli.aec_bridge_{config,engines,telemetry}`, `cli.wake_enroll`; `wake_corpus/recording_backend.py:45` → `cli.wake_enroll` | **`cli/aec_bridge_{config,engines,telemetry}.py` (1,246 LOC) and `cli/wake_enroll.py` (673) are libraries**, not CLIs — only `jasper.cli.aec_bridge` and `jasper.cli.wake_enroll:main` are `[project.scripts]` entry points (`pyproject.toml:182,223`). Move the three `aec_bridge_*` to `jasper/aec/`, the enroll library out of `wake_enroll`. |
| `control/debug_control.py:37`, `control/usb_gadget_forensics.py:13`, `wake_corpus/bridge_session.py:83` → `web._common` | `web/_common.py` (1,511 LOC) holds env-file atomics + `systemctl restart` + CSRF/flash plumbing. The non-web parts belong in L1. |
| `cli/doctor/correction.py` → `web._systemd` | `web/_systemd.py` (541) is socket-activation/idle-shutdown — pure platform. |
| `audio_profile_state.py:30` → `cli.aec_bridge_engines`; `audio_validation.py:46` → `cli.aec_bridge_telemetry` | same relocation fixes both |
| `audio_validation.py:53` → `control`/`control.client`; `measurement_window.py:25` → `control.uds`; `usbsink/volume_bridge.py:38` → `control.client`; `accessories/bridge.py` → `control.client` | `control/{client,uds}.py` is an IPC **client**; it belongs in L1 beside `busctl`. |
| `config.py` → `voice.catalog`, `voice.input_policy` | resolved by T18 §3's `VoiceConfig → jasper/voice/config.py` move — **in flight, confirm** |
| `volume_owner.py:80` → `active_speaker.volume_latch` | genuine L4→L5 inversion; move `volume_latch` down to `volume_owner` or invert |
| `google_routes.py` → `tools`; `mic_presence.py` → `voice.input_presence`; `identity.py:42` → `peering` | 3 leaf inversions |
| `fanin/converge.py:215` → `cli.active_speaker` *(function-local)* | a daemon reaching the CLI layer — confirmed from T14-3 |

Two smaller module-level package cycles: `audio_runtime_plan ↔ fanin ↔ transport_coherence`
(`audio_runtime_plan.py:43,55` ↔ `fanin/coupling_{auto,reconcile}.py:42,49`, `transport_coherence.py:17`)
and `local_sources ↔ multiroom ↔ source_intent` (`local_sources/markers.py:21,22,27`,
`multiroom/reconcile.py:40`, `source_intent.py:60`) — both are C9-shaped.

## 3. T18's 8-package regrouping, evaluated against the real graph

**It relocates the cycle; it does not break it** (`t18.py`). Applying T18 §4 verbatim and re-running
Tarjan on the package graph: module-level SCCs `[26, 3]`, combined `[34]` — *worse* than today's
`[23, 3, 3]` / `[76]`, because the proposed `audio/` (31 modules, 24k LOC) becomes mutually dependent
with **nine** other packages: `audio_hardware, audio_measurement, chip_aec, cli, control, fanin,
sources, voice, volume`. T18 flags `audio/` as too big; the graph says it is the SCC wearing a new
name. The `net ↔ identity ↔ peering` 3-cycle survives too (`identity.py:40,42`).

**Adjusted move table.** Keep T18's `platform/`, `net/`, `identity/`, `wake/`; split its `audio/`
along the L2/L3/L4 line above; and add the four relocations the graph demands.

| module(s) | → package | pure `git mv` + import rewrite? |
|---|---|---|
| `atomic_io env_load env_file log_event json_fields backoff busctl transition_log flight_recorder debug_mode service_units secret_redaction watchdog doctor_contract percentiles os_fault memory_policy _oom_adj model_downloads` | `platform/` | **yes** |
| `web/_common.py` (the env-atomics + systemctl half), `web/_systemd.py`, `control/client.py`, `control/uds.py` | `platform/` | **yes** for `_systemd`/`uds`/`client`; `_common` needs the wizard-HTTP half left behind in `web/` |
| `http_security mdns avahi_service control_advert usb_network usbgadget wifi_*` | `net/` | **yes** |
| `identity identity_state speaker_name speaker_name_discovery` | `identity/` | **no** — `identity.py:42 → peering` must be inverted first |
| `wake wake_legs wake_ports wake_conditions wake_condition_context wake_events wake_fusion wake_models vad openwakeword_guard audio_buffer aec_ready` | `wake/` | **yes** (T18's `wake_ports→wake_legs` merge also drops the `wake_ports → aec_sweep` violation) |
| `cli/aec_bridge_{config,engines,telemetry}.py`, the library half of `cli/wake_enroll.py` | `aec/`, `wake/` | **yes** — entry points stay in `cli/` as thin `main()` shims |
| `camilla_config_contract music_sources librespot_state source_state install_profile capture_protocol dsp_numpy airplay_mode spotify_uri mux_mode_persistence` | `contracts/` | **no** — needs **C3** and `source_state → {bluealsa_probe, fanin.status}` inverted |
| `audio_hardware/ output_hardware usb_mic ring_assets renderer renderer_lanes bluealsa_probe mics/ xvf/` | `hardware/` | **no** — needs **C4**, plus `renderer_lanes → audio_measurement.correction_lane` and `ring_assets → fanin_coupling` |
| `camilla* dsp_apply output_topology* audio_io audio_runtime_* audio_validation* transport_coherence tts_routing measurement_window enhanced_aec aec_sweep audio_quality audio_profile_state audio_input_view` | `audio/` | **no** — needs **C6, C7**; `measurement_window`/`audio_validation`/`audio_profile_state` need the `control`/`cli` relocations above |
| `volume_*, assistant_loudness, assistant_volume` | `volume/` | **no** — needs **C5** and `volume_owner → active_speaker.volume_latch` |
| `mux mux_mode_persistence source_events source_intent bus spotify_router spotify_routing` | `sources/` | **no** — needs **C9** |
| `voice_daemon config usage timers conversation_history tool_* accounts google_* home_assistant weather subway citibike location_state *_oauth mic_mute_persistence` | `assistant/` | **yes** once T18 §3's `VoiceConfig` move lands |

`audio_lab.py` should be deleted, not moved (T14-3 finding 1, in flight).

## 4. Function-local imports — 1,708 statements, classified

p0's 1,287 undercounts for the same resolution reason as §1. Mechanical classification of all 1,708
(`localimp2.py`), validated against a hand-read sample of **120 sites across the top-15 offenders**
(`sample_dump.txt`):

| class | statements | of which in `web/`+`cli/` | what it really is |
|---|---:|---:|---|
| **A** target already imported at module level in the same file | 109 | 45 | **pure waste** — delete the local line |
| **B** cycle-dodge (promoting it would close a module-level cycle) | 43 | 7 | the only class that is *load-bearing* today; §1's C2-C11 are drawn from it |
| **C** import-cost, target itself imports numpy/scipy/aiohttp/… | 307 | 111 | justified by ADR-0226 |
| **D** import-cost only transitively | 447 | 286 | justified in `web/`+`cli/` (socket-activated wizards), suspect elsewhere |
| **E** a comment within 8 lines gives a reason | 47 | 16 | keep |
| **F** no mechanical reason found | 755 | 402 | **353 in library code is the habit bucket** |

The hand-read confirms the split and adds what the scan can't see: `web/`+`cli/` follow an unwritten
convention ("stdlib-only at import time so the socket-activated wizard starts fast") stated in only a
few docstrings — `fanin_coupling.py:975-977`, `camilla_config_contract.py:203-205`,
`control/state_aggregate.py:1262-1263` — and nowhere as a rule; `web/correction_setup.py:796`
documents the *opposite* ("these modules never import this module back at import time"). **No
test-patch-seam instances appeared in the 120-site sample** — tests reach private names instead (§5).

**Rule for AGENTS.md** (replaces nothing; it is currently unwritten):

> A `import jasper…` inside a function needs one of three reasons, and the reason goes on the line
> above: (a) the module is a `web/` or `cli/` entry point and the target pulls numpy/scipy/aiohttp —
> say `# lazy: <dep>, ADR-0226`; (b) `# cycle: <other module> imports us at module level` — and open
> an issue, because that is a bug, not a design; (c) the import is genuinely conditional on a runtime
> branch. Anything else goes at module top. Never write a function-local import of a module the file
> already imports at module level.

## 5. Private names crossing module boundaries

`from x import _y` inside `jasper/`: **334** occurrences. Split by whether the boundary is real:

| class | n | verdict |
|---|---:|---|
| private **module** re-exported from its own package (`from . import _systemd`, `_common`, `_shared`, `_common` in `cli/round_views`) | 22 | **conventional, fine** — the underscore marks a package-private module |
| private **name**, importer and target in the same package | 208 | mostly fine; the underscore is wrong where the name has ≥3 importers: `cli/doctor/_shared._run` (13), `cli/round_views/_common._write` (12), `_ROUND_TOOL_ERRORS` (7→8), `_ROUND_DIR_HELP` (7), `_load_round` (7), `_view_out` (6), `control/uds._mux_socket_command`/`_voice_socket_command` (3+3), `bass_extension/adapters/base._curve_arrays` (3) → **rename public** |
| private name **across a package boundary** | **104** | **the import is wrong** |

Top cross-package offenders, all "the import is wrong":

| target._name | reached from | fix |
|---|---|---|
| `active_speaker/crossover_v2/capture_dispatch` — **12 private names in one statement** | `active_speaker/crossover_v2_flow.py:409`, `crossover_v2/diagnostics.py` | `capture_dispatch` should return one `GateDisclosure`/`PilotDiag` dataclass, not 12 private accessors |
| `audio_measurement/alignment._bandlimit` (3), `audio_measurement/program._intersect_band`, `program_analysis._{estimate_drift,global_offset,locate_segments}` | `program_analysis/{check,locate,response}.py`, `crossover_v2/harmonic_evidence.py:279,799` | promote to the `audio_measurement` public surface |
| `sound/profile._filter_response_complex`, `._freq_trig` | `active_speaker/branch_chain.py:26` | the DSP math is public API of `sound.profile` |
| `active_speaker/flat_spec_views._{evaluate_position,exclusion_mask,pool}` | `crossover_v2/round_views.py:30` | rename public |
| `active_speaker/crossover_v2/spatial._geometry_guidance_copy` | `crossover_envelope_v2.py:82` | T14-3 finding 3 — **confirmed** |
| `sound.settings._settings_path` (attribute form) | `volume_curve.py:61` | T18 F11 — **confirmed**; fixed by **C5** |

Attribute-style reaches (`mod._name` via an imported alias): **178** more, dominated by
`control/handlers/{aec,system,voice,volume,grouping}.py` → `server._*` (≈35) — the control/handlers
seam **already in flight under issue #4030**, not new. Tests add **589** private-name imports;
`voice_daemon._build_system_instruction` (17), `_configured_wake_legs` (11),
`control/server._make_handler` (8), `linearization_fit._HIGHSHELF_Q` (8) lead, each a public door the
tests declined to use (re-grades T14-3 finding 15: same disease, wider).

## 6. Top-10 boundary fixes by payoff/cost, each with its CI guard

| # | fix | payoff | cost | CI guard (one contract, no source-scanning test) |
|---|---|---|---|---|
| 1 | **C1** — move the adapter registry out of `bass_extension/adapters/base.py:109-111` | the tree's only import-time cycle disappears; module graph becomes a DAG | 3 lines | `[importlinter:contract] type = independence` over the three adapter modules |
| 2 | Relocate `cli/aec_bridge_{config,engines,telemetry}.py` + `wake_enroll` library, `web/_systemd.py`, the platform half of `web/_common.py`, `control/{client,uds}.py` | kills 10 of the 15 edges holding the 23-package SCC; `surfaces-are-leaves` goes green | `git mv` + import rewrite; `_common` needs a split | `type = forbidden, allow_indirect_imports = True`, sources = every non-surface package, forbidden = `jasper.web`, `jasper.cli` (**6 direct violations today**) |
| 3 | **C2** — `env_load` owns the two outputd env paths | 7 modules leave the SCC; L1 stops importing L4 | ~10 lines | `platform-is-a-leaf` forbidden contract (**2 violations today**) |
| 4 | **C5** — `sound.settings` exports `configured_volume_floor_db()` | 4 modules leave the SCC; removes a private-name reach | ~20 lines | covered by the layers contract (`volume` above `sound`) |
| 5 | **C3 + C6 + C7** — three constants move to the module that owns the vocabulary | `contracts-are-leaves` goes green; `camilla_config_contract` becomes a true leaf with 19 inbound edges | ~30 lines | `contracts-are-leaves` forbidden contract (**4 violations today**) |
| 6 | **C9** + the `audio_runtime_plan ↔ fanin ↔ transport_coherence` pair | removes the two remaining module-level *package* cycles | 2 signature inversions | the `jasper-layers` contract, adopted L1-L4 first |
| 7 | Delete the 109 class-A function-local imports | pure subtraction; the file already imports the target | mechanical | `type = forbidden` cannot express this — use `ruff`'s existing lint, or accept it as a one-off cleanup |
| 8 | **C11** — split `active_speaker/runtime_contract.py` (4,465 LOC) into `types` + `queries` | the 18-inbound-edge hub becomes importable without dragging `sound`/`multiroom`/`bass_extension` | large, single-concern | `jasper-layers` with `active_speaker.runtime_contract.types` pinned into L2 |
| 9 | Collapse `capture_dispatch`'s 12 private accessors into one dataclass | removes 12 of the 104 cross-package private reaches in one PR | medium | none — a layers contract cannot see privacy; this is a review-time rule |
| 10 | Adopt the AGENTS.md deferred-import rule (§4) + land the `jasper-layers` contract in the `scripts/test-merge` lane, starting green at L1-L3 and ratcheting up | stops regression of everything above | one CI step | `jasper-layers` with L4-L7 collapsed into one layer initially, then split as C2-C11 land |

**Do not** add a source-scanning pytest for any of these: import-linter's grimp graph already sees
function-local imports, so a layers contract cannot be evaded by deferring an import — which is the
exact failure mode a regex test would have.

## D. What only hardware/runtime can prove

* Whether the class-C/D deferred imports in `web/`+`cli/` actually buy startup latency on the 1 GB Pi
  (and the Zero 2 W's 415 MB). Every claim here is static; the cheap experiment is to promote them and
  time `systemd-analyze` / first-byte for one socket-activated wizard, per ADR-0226.
* Whether `camilla_config_contract._active_camilla_floor`'s `except ImportError` (`:206-210`) has ever
  fired — static analysis says both targets always exist.
* Whether `bass_extension/adapters`' bottom-of-file registry has a real load-order dependency a
  `registry.py` split would break (trivially reversible: ship and check `jasper-doctor`).
* Whether relocating `control/{client,uds}.py` changes any unit's import cost or `ExecStart`.

## E. Coverage

**Built / executed:** full AST import graph over all 749 `jasper/` modules separating module-level /
`try:`-wrapped / `TYPE_CHECKING` / function-local edges (`graph.py`); Tarjan on both graphs and on
package-collapsed variants; exact reproduction of p0's algorithm to explain the 34 (`repro_p0.py`);
2-cycle lower bound + Eades-LS + 60k-step local search for the feedback arc set; cumulative cut
verification (`cuts.py`); classification of all 1,708 function-local imports plus a 120-site hand
read; private-name scans (`from x import _y` and attribute form); T18-grouping simulation;
import-linter 2.15 in a scratch venv, 5 contracts, run together and individually. Repo never written
to (`git status` clean, verified after).

**Files opened:** `jasper/env_load.py:230-266`, `volume_curve.py:50-70`, `tts_routing.py:50-75`,
`camilla_config_contract.py:200-215`, `output_hardware.py:1090-1105`, `fanin_coupling.py:790-800,
975-990`, `multiroom/dac_content_ring.py:38-44`, `dsp_apply.py:566-576`, `source_intent.py:973-980`,
`fanin/latency_mode.py:360-370`, all four `bass_extension/adapters/*.py` import blocks,
`web/_common.py` + `web/_systemd.py` headers, `pyproject.toml` entry points, 13 package `__init__.py`,
and 120 function-local import sites with context across `active_speaker/{runtime_contract,
web_commissioning,web_measurement}`, `cli/{measure,null_door,doctor/audio,doctor/grouping}`,
`control/state_aggregate`, `fanin/ring_health`, `multiroom/follower_config`, `sound/graph_carrier`,
`web/{correction_crossover_v2,correction_setup,sound_setup,correction_crossover_backend}`.
Read: `p0-inventory.md §6-7`, `p1-T18.md`, `p1-T14-3.md`, `p0_cart/*.py`.

**Skipped:** `rust/`, `c/`, `deploy/`, `scripts/`; `tests/` except the private-name AST scan. Runtime
import cost (§D). The 208 same-package private names were counted, only the ≥3-importer ones judged.
