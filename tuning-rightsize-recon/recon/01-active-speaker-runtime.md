# Recon 01 — active_speaker apply / runtime / commissioning (top-level files)

HEAD `c0325038`, branch `claude/busy-goodall-mz0gvv`, read-only. Scope: the 48
files named in the task. **62,456 lines** (`wc -l`), 45,897 code / 7,105
docstring / 4,350 comment / 5,104 blank (script:
`scratchpad/recon/prose.py`, AST + tokenize).

---

## 1. Per-file table

`prose%` = (docstring+comment lines)/total. `prod` = distinct non-test files
importing the module (`scratchpad/recon/consumers.sh`, grep over
`jasper/ scripts/ deploy/ experiments/`). `over` = prose lines sitting in a
block that carries a date, `#NNNN`, `ADR-NNNN`, "ruling", or history language
— i.e. explicitly over the AGENTS.md comment bar.

| file | lines | prose% | over | prod | primary consumers |
|---|---:|---:|---:|---:|---|
| runtime_contract.py | 5529 | 23% | 499 | 35 | cli/active_speaker, cli/doctor/audio, sound/, multiroom/, correction/runtime_safety |
| camilla_yaml.py | 4551 | 32% | 691 | 38 | staging, startup_load, baseline_profile, commission_ramp, sound/graph_carrier, bench/ |
| baseline_profile.py | 4192 | 31% | 724 | 38 | commissioning_*, correction/, multiroom/, cli/measure |
| commissioning_evidence.py | 3501 | 1% | 20 | 6 | commissioning_evidence_store, _host, _service, _isolated_producer, measured_candidate |
| driver_safety.py | 3258 | 22% | 493 | 10 | commissioning_admission/_runtime/_service, web/sound_setup, correction_crossover_v2 |
| web_commissioning.py | 2916 | 11% | 160 | 11 | web/sound_setup, web/correction_* , audition, null_door |
| staging.py | 2498 | 17% | 167 | 22 | startup_load, commission_ramp, web/sound_setup, correction_setup |
| startup_load.py | 2352 | 12% | 98 | 9 | commission_ramp, commissioning_coordinator, web_commissioning, cli/active_speaker |
| commissioning_run.py | 2141 | 5% | 12 | 10 | commissioning_* (all), web/correction_crossover_backend, round_bank |
| commissioning_receipt.py | 1831 | 5% | 46 | 4 | commissioning_verification/_apply/_service, measured_candidate |
| commissioning_capture.py | 1520 | 26% | 80 | 6 | baseline_profile, web_measurement, attempts_loop, crossover_eligibility |
| commissioning_apply.py | 1460 | 3% | 0 | 1 | commissioning_service |
| commissioning_evidence_store.py | 1433 | 4% | 12 | 13 | commissioning_*, correction/storage, web/correction_crossover_* |
| web_measurement.py | 1382 | 9% | 25 | 1 | web/correction_crossover_backend |
| commission_ramp.py | 1366 | 17% | 27 | 5 | reset, web_commissioning, web/sound_setup, cli/active_speaker |
| setup_status.py | 1346 | 23% | 172 | 6 | web/correction_setup, web/server, control/state_aggregate, cli/doctor/audio |
| commissioning_service.py | 1317 | 2% | 4 | 1 | web/correction_crossover_backend |
| session_volume_plan.py | 1291 | 47% | 263 | 20 | seat_level_ramp, volume_latch, program_admission, correction_crossover_v2 |
| playback.py | 1245 | 10% | 90 | 4 | crossover_v2_flow, web_commissioning, web/sound_setup |
| commissioning_admission.py | 1238 | 10% | 54 | 6 | commissioning_isolated_producer, web_measurement, camilla_yaml, cli/seat_level |
| commissioning_coordinator.py | 1218 | 26% | 93 | 1 | web/sound_setup |
| graph_safety.py | 1201 | 34% | 33 | 10 | runtime_contract, camilla_yaml, graph_evidence, bass_extension/bench, fanin/ring_health |
| bundles.py | 1150 | 24% | 80 | 24 | commissioning_*, correction/, round_bank, cli/measure |
| commissioning_runtime.py | 1145 | 6% | 22 | 2 | commissioning_apply, commissioning_service |
| path_safety.py | 1092 | 8% | 45 | 9 | startup_load, staging(via), runtime_contract, web/sound_setup, cli/active_speaker |
| driver_protection.py | 1061 | 49% | 428 | 14 | driver_safety, camilla_yaml, staging, excitation_safety_plan, crossover_v2/ |
| excitation_safety_plan.py | 1001 | 33% | 234 | 11 | program_admission, commissioning_admission/_runtime, bass_extension/bench |
| commissioning_isolated_producer.py | 925 | 2% | 0 | 2 | commissioning_service, web/correction_crossover_backend |
| profile.py | 893 | 11% | 22 | 44 | everything (the preset/profile data model) |
| commissioning_verification.py | 845 | 8% | 39 | 2 | setup_status, commissioning_service |
| environment.py | 780 | 8% | 20 | 24 | runtime_contract, staging, path_safety, multiroom/, cli/ |
| safe_playback.py | 577 | 5% | 8 | 9 | commission_ramp, startup_load, playback, measurement |
| __init__.py | 480 | 3% | 0 | 3 | see §5 — 146/209 `__all__` names never imported |
| bringup.py | 454 | 2% | 0 | 2 | web/sound_setup |
| wizard_client.py | 408 | 40% | 24 | 4 | cli/arm_walk, cli/basic_profile, cli/round, scripts/run-crossover-round.py |
| commissioning_host.py | 405 | 4% | 0 | 3 | commissioning_service, playback, web/correction_crossover_backend |
| playback_route.py | 395 | 37% | 72 | 18 | runtime_contract, output_topology, staging, web/sound_setup, cli/measure |
| commissioning_lifecycle.py | 332 | 2% | 0 | 5 | commissioning_run/_apply/_service/_verification/_isolated_producer |
| volume_latch.py | 317 | 53% | 127 | 11 | seat_level_*, session_volume_plan, correction_crossover_*, aec_* |
| reset.py | 270 | 39% | 58 | 4 | output_topology_runtime, web/sound_setup, correction_crossover_* |
| commission_wiring.py | 236 | 32% | 12 | 8 | web/sound_setup, cli/{measure,seat_level,active_speaker}, seat_level_reference |
| _common.py | 215 | 53% | 88 | 62 | the whole package + jasper/web/*_setup.py |
| startup_hold.py | 193 | 58% | 84 | 4 | runtime_contract, startup_load, baseline_profile, cli/doctor/audio |
| program_playback.py | 179 | 42% | 44 | 5 | crossover_v2/{composition,program_transaction}, correction_crossover_v2 |
| tuning_handoff.py | 145 | 28% | 12 | 1 | web/sound_setup |
| passive_profile.py | 62 | 40% | 0 | 2 | baseline_profile, output_topology |
| restore_wait.py | 58 | 43% | 0 | 2 | audition, web_commissioning |
| revalidation.py | 52 | 25% | 0 | 2 | baseline_profile, commissioning_coordinator |

Shape: 5 files are 39% of the area (runtime_contract, camilla_yaml,
baseline_profile, commissioning_evidence, driver_safety = 21,031 lines).
40 functions exceed 200 lines; 101 exceed 100. The longest:
`baseline_profile.build_baseline_profile_candidate` 986L,
`runtime_contract._active_graph_evidence` 779L,
`web_measurement.record_driver_capture` 534L,
`setup_status.read_active_speaker_setup_status` 513L.

---

## 2. "Is this graph safe?" — who decides

Better than feared: **`runtime_contract.py` is the sole authority.** `GraphSafety(`
is constructed in exactly 9 places, all in `runtime_contract.py`
(`grep -rn "GraphSafety(" jasper/ | grep -v tests/` → 9 hits, 1 file). The
others are layers, not rivals:

| module | what it actually owns | verdict-producing? |
|---|---|---|
| `graph_safety.py` | parses a Camilla config (dict / yaml dict / emitted text) into `GraphView`, plus 15 boolean predicates (`tweeter_guard_present`, `output_terminally_muted`, …) | **no** — predicates only. Misnamed. |
| `environment.py:315 classify_camilla_config_text` | marker/regex classification of config *text* + `volume_limit_ok` | no — a summary dict |
| `runtime_contract.py:3859 classify_camilla_graph` | consumes both of the above → `GRAPH_*` class | no — a class name |
| `runtime_contract.py:{1305,3587,3713}` `_flat/_active/_parked_graph_allowed` | the three `GraphSafety` producers | **yes** |
| `runtime_contract.py:4950 safe_graph_for_current_topology` | the one selector (408L) → `SafeGraphDecision` | **yes, authoritative** |
| `camilla_yaml.py:{666,722,823}` `_assert_*` | emit-time fail-closed re-proof of what was just emitted | yes, but at a different altitude (build time, not load time) |
| `path_safety.py` | a *different* question: has the physical path been proven before an audible load (`REQUIRED_PATHS`, evidence file) | no |
| `driver_protection.py` | per-driver policy constants + low-limit resolution | no |
| `driver_safety.py` | driver *research profile* schema validation + LLM prompt | no; imports `driver_protection`, layered |
| `excitation_safety_plan.py:891 prepare_driver_excitation_plan` | the excitation ledger clamp; 3 real callers | yes, a separate clamp |

**Finding 2.1 — `graph_safety.py` is misnamed** (1,201L, 34% prose). It contains
no safety decision; it is a config→view parser plus predicates.
`jasper/bass_extension/bench/derivation.py:209` already re-implements one of its
private helpers ("Mirrors `graph_safety._running_step_channels`"). *Move:* rename
to `graph_view.py`, export `_running_step_channels`, delete the bass_extension
mirror. ~30 lines, **low** risk, proved by existing `tests/test_active_speaker_graph_safety*`.

**Finding 2.2 — `environment.classify_camilla_config_text` classifies by
docstring marker string** (`"Auto-generated active-speaker startup config" in
text`, `environment.py:325`). The emitter's own header comment is load-bearing
input to the safety classifier. Not a duplicate, but a fragile contract worth an
explicit `source:` key instead. **med** risk to change; note only.

---

## 3. The commissioning cluster — 13 files, and it is really *two* clusters

`commission_*` and `commissioning_*` share a prefix and **never import each
other** (verified: intra-package import lists for all 16 files). They are two
independent machines:

**Lane A — "commission load + audible ramp"** (`/sound/` card + `jasper-active-speaker` CLI)
`staging.py` (2498) → `startup_load.py` (2352) → `commission_ramp.py` (1366),
glued by `commission_wiring.py` (236), held by `startup_hold.py` (193),
substrate `safe_playback.py` (577). ~7.2k lines.

**Lane B — "summed-region evidence machine"** (`/correction/` crossover backend)
| role | file | lines |
|---|---|---|
| pure state machine | `commissioning_lifecycle.py` (states, `_ALLOWED_TRANSITIONS`, `CommissioningTransition`) | 332 |
| pure authority models | `commissioning_evidence.py`, `commissioning_receipt.py` | 5,332 |
| durable journal / store | `commissioning_run.py`, `commissioning_evidence_store.py` (nested inside `bundles.py`) | 3,574 |
| hardware transactions | `commissioning_runtime.py`, `commissioning_admission.py`, `commissioning_apply.py`, `commissioning_isolated_producer.py`, `commissioning_verification.py` | 5,613 |
| composition | `commissioning_host.py`, `commissioning_service.py` | 1,722 |
| adapters (neither A nor B) | `commissioning_capture.py` (phone-mic → measurement record), `commissioning_coordinator.py` (read-only view model for `/sound/`) | 2,738 |

**Which is the state machine:** `commissioning_lifecycle.py` — 332 lines, pure,
no intra-package imports, 5 consumers. It is the only thing in the cluster that
is unambiguously right-sized.

**Could they be 3 files?** Not at today's line count — but the *five* rows above
are the natural boundaries and they are already almost clean. The realistic move
is a subpackage, not a merge: `jasper/active_speaker/commissioning/{lifecycle,
model,store,runtime,service}.py`, with `commissioning_capture.py` renamed
(`driver_capture_bridge.py`, it is not part of lane B) and
`commissioning_coordinator.py` moved next to `setup_status.py` as a web view
model. Renames are ~0 net lines, **low** risk, and buy the owner's "understand it
from the folder structure" directly. Lane A should keep the `commission_*`
prefix or move to `commissioning_load/`.

---

## 4. Dead code (verified: no prod caller anywhere incl. `deploy/`, `scripts/`, `pyproject`, string/importlib refs)

Method: identifier index over every `.py/.sh/.toml/.service/.js/.md/.yml` in the
repo (`scratchpad/recon/dead2.py`, 118,894 identifiers, 1,377 prod files), then a
transitive-reachability pass per module from externally-referenced roots.

| file:line | symbol | lines | kept alive by | risk |
|---|---|---:|---|---|
| web_commissioning.py:1125 | `_load_applied_summed_measurement_config` | 215 | tests only (8 refs) | low |
| web_commissioning.py:2112 | `_play_capture_sweep` | 112 | nothing (superseded by `play_driver_capture_sweep`:2226) | low |
| web_commissioning.py:1546 | `automatic_summed_excitation` | 43 | only the dead loader above | low |
| web_commissioning.py:624 | `_measurement_sweep_wav_path` | 37 | only `_play_capture_sweep` | low |
| web_commissioning.py:{1342,1373,1387,1443,185} | `_restore_applied_summed_previous_config{,_resilient}`, `_rollback_applied_summed_measurement_config`, `_latest_summed_test`, `AutomaticSummedConfigRestoreError` | 65 | the same dead cluster | low |
| web_commissioning.py:2031,2094 | `prepare_/restore_automatic_driver_level_match` | 77 | tests only | low |
| commissioning_runtime.py:181–801 | `SummedGraphRequest`, `_SummedTopologyBinding`, `_ScopedDelayLane`, `_topology_binding`, `_filter_channels`, `_normal_graph`, `_stationary_candidate`, `_append_output_isolation`, `_scoped_lane_names`, `_append_scoped_lane`, `_source_header`, `_dump_graph` | 418 | tests only | low |
| staging.py:2460 | `prepare_summed_commissioning_config` | 39 | `__init__` re-export + 1 test | low |
| commissioning_capture.py:86 | `RESERVED_CROSSOVER_EVENTS` | 6 | 1 test asserting its own contents | low |
| commissioning_verification.py:73 | `POST_APPLY_CAPTURE_SOURCE` | 1 | nothing | low |
| camilla_yaml.py:108 | `ACTIVE_STARTUP_CONFIG_NAME` | 1 | `__init__` re-export only | low |

**Total ≈ 1,015 lines** of production code with no production caller, plus the
tests that pin them (`tests/test_active_speaker_commissioning_runtime.py` is
substantially a test of 418 dead lines). This is the "summed-graph commissioning"
lane: `commissioning_runtime` retains only `apply_summed_commissioning_graph`-side
entries reached from `commissioning_apply`/`_service`; its whole graph-building
half is orphaned. **Proof it is safe:** delete, run `scripts/test-merge`; the
only failures should be the tests that name the deleted symbols directly.

---

## 5. `__init__.py` — a 480-line façade nobody imports through

209 `__all__` entries. Only **63** are ever imported as
`from jasper.active_speaker import X` anywhere in the repo **including tests**;
**146 are never imported through the façade at all**. Only 3 prod call sites use
the flat façade for symbols (`cli/active_speaker.py`, `cli/seat_level.py` →
`ActiveSpeakerConfigError`, `ActiveSpeakerPreset`); everything else imports the
submodule directly. 47 symbols in the area have `__init__.py` as their *only*
external prod referrer — i.e. the façade is the reason they look alive.

*Move:* trim `__init__.py` to the ~63 used names (or to the 2 the CLIs want and
let the rest import submodules). **≈ 330 lines removed**, low risk;
`scripts/test-merge` + `grep -rn "from jasper.active_speaker import"` proves it.

---

## 6. Non-negotiable clamps — exactly where they live

| clamp | authoritative site | re-checks (fine, but named) |
|---|---|---|
| `devices.volume_limit` ≤ 0.0 at emit | **6 copies** inside `camilla_yaml.py` at 2403, 2602, 3028, 3735, 4071, 4319 (`if volume_limit_db > 0: raise ActiveSpeakerConfigError`) — one per emitter | **+ a 7th implementation**: `jasper/camilla_config_contract.py:431 ensure_volume_limit_db`, used by `jasper/sound/camilla_yaml.py:458`, whose docstring says it "Mirrors the guard in `jasper.active_speaker.camilla_yaml`" |
| `volume_limit_ok` read-back | computed once, `environment.py:469` | read as a dict bool in `path_safety.py` ×5 (474,556,687,696,728), `staging.py` ×3, `startup_load.py` ×2, `runtime_contract.py` ×3 — cheap, keep |
| runtime fader clamp | `jasper/camilla.py:687 set_volume_db` → `_coerce_main_volume_db` | `volume_latch.set_and_confirm_volume` (set-and-confirm, not a clamp) |
| limiter clip ceilings | `camilla_yaml.py:132 STARTUP_LIMITER_CLIP_LIMIT_DB=-12.0`, `:136 BASELINE_LIMITER_CLIP_LIMIT_DB=-1.0` | verified at `runtime_contract.py:{1874,2387,2459,3470}`, `commission_ramp.py:322`, and graph_safety's 3 `limiter_clip_ceiling_db` predicates (766/818/882). 3 independent re-proofs — cheap, keep |
| commissioning SPL stop | declared `profile.py:538 SafetyEnvelope.max_commissioning_level_db_spl` (validated 45–85 at :584); **one reader** `commission_wiring.py:155 commissioning_spl_ceiling_db` | enforced on samples at `seat_level_ramp.py:620` and `:1097` (outside this area) |
| excitation ledger | `excitation_safety_plan.py:891 prepare_driver_excitation_plan` — single choke point | 3 callers: `program_admission.py:431`, `commissioning_admission.py:590`, `bass_extension/bench/excitation.py`. Clean. |
| per-driver bands / protective HP | `driver_protection.py` (declared floors/ceilings) + `graph_safety.output_highpass_protected` (proof in the emitted graph) + `camilla_yaml._assert_tweeter_outputs_protected` (emit gate) | 3 tiers, deliberate |

**The one clamp that is genuinely duplicated is the hearing clamp**: seven
implementations of "reject a positive `volume_limit`". Consolidating on
`camilla_config_contract.ensure_volume_limit_db` is a **non-negotiable-tier**
change (AGENTS.md §Review policy) and needs `/adversarial-review`, but it removes
12 lines and, more importantly, removes six places a future edit could weaken.

---

## 7. Prose over the AGENTS.md bar

11,373 prose lines in the area. **5,192 of them (46%) sit in a block that carries
a date, a `#NNNN` issue number, an `ADR-NNNN`, the word "ruling", or history
language** ("used to", "no longer", "retired", "superseded", "deliberately NOT",
"Wave 2 will…"). Counts: 197 issue citations, 31 ADR citations, 85 dates, 64
"ruling"s, 214 history phrases (`grep -ohE`, command in §1).

Worst offenders by ratio: `startup_hold.py` 44% of the *file* is over-bar prose,
`_common.py` 41%, `driver_protection.py` 40%, `volume_latch.py` 40%,
`program_playback.py` 25%, `excitation_safety_plan.py` 23%.

**Quote 1 — `driver_protection.py:195–293`, a 99-line comment run** in front of a
25-line function:
> `# Operator ruling (2026-07-19): driver protection is exactly two invariants…`
> `# The -35.0 dBFS absolute hedge this derivation carried until 2026-08-20 is`
> `# RETIRED, and nothing replaces it in that role. It landed "provisional pending`
> `# W6 bench validation"; W6 never ran…`
> `# Owner ratification, 2026-08-20, verbatim: "i use a fixed gain amp and`
> `# digitally control the volume…"`

Dates, retired-constant archaeology, a verbatim owner quote, and issue numbers
(#2761, #2765). The non-derivable residue is ~8 lines (what bounds the level
now, and the declaration-swap residual). *Move:* keep 8 lines + `See ADR-NNNN`;
the rest belongs in an ADR. **−90 lines**, low risk.

**Quote 2 — `startup_hold.py:5–89`, an 84-line module docstring** for 49 lines of
code (3 one-line functions):
> `THREE units reach the writers, and they are not all root, nor all the same sandbox: * jasper-web (User=jasper-web, ProtectSystem=strict) — writes the hold from…`
> `Without that last one the marker outlives the commission that took it (observed on jts3 after a successful save-and-apply).`

Systemd unit facts restated from `deploy/jasper-web.service`, plus an incident
narrative. AGENTS.md: "Do not restate here, in README, or in code what another
file owns." *Move:* 10-line docstring (what the marker is, fail direction) +
pointer. **−70 lines**, low risk.

**Quote 3 — `_common.py:96–128`, a 33-line comment for a 4-line frozenset**
(`LEGACY_DROPPED_DRIVER_FIELDS`, two entries):
> `# horn_coverage_deg (#2872): collected by /sound/setup/ for the Bessel beamwidth matcher #1675 was going to build. #1675 closed 2026-08-08 having built ka beaming guidance off the woofer's radiating_diameter_mm instead…`

Plus the module docstring's own consolidation diary ("They are consolidated here…
so the dedup is purely structural"; "`commissioning_host.py`'s `_sha256` is
deliberately NOT migrated here"). 113 of 215 lines are prose. *Move:* the
cross-gate warning (the last 10 lines, genuinely non-derivable) stays; the rest
goes. **−80 lines**, low risk.

**Stale prose contradicted at HEAD (specific, not style):**
- `commissioning_receipt.py:5` — "This is an **inert** authority model. A later
  Active integration shell will derive… Nothing here reads files, mutates
  CamillaDSP…" At HEAD it is imported by `commissioning_apply`,
  `commissioning_verification`, `commissioning_service`, `measured_candidate` —
  the shell exists. "Wave 2 must create this separate fail-closed authority
  chain" describes work already done.
- `commissioning_capture.py:5` — "…turns a phone-mic sweep capture into a real
  acoustic verdict, **but it had no caller (the runtime commissioning loop did
  not exist yet)**. This module is that caller." History of a gap that closed.
- `runtime_contract.py:3875` — "…config TEXT in hand (a missing volume_limit).
  Merged, they were…" — a merge narrative.

---

## 8. Duplication, wrong altitude, abstractions that don't earn their keep

| # | evidence | why it is a smell | move | Δlines | risk |
|---|---|---|---|---:|---|
| 8.1 | `camilla_yaml.py` — 7 `emit_active_speaker_*_config` functions repeat: the latency-resolution prelude verbatim ×5 (2389, 3016, 3718, 4056, 4306), the `devices:` YAML template ×4 (2450, 3103, 3882, 4389), the `out_path` write+log tail ×5 (2483, 3137, 3921, 4424, 4530), the volume-limit clamp ×6 | one concern (emit a Camilla file) written seven times; a clamp fix must be made 6× | extract `_resolved_devices(...)`, `_devices_yaml_block(...)`, `_write_config(yaml, out_path, event, **fields)` | −250 | med (touches the emit path → adversarial tier) |
| 8.2 | `commissioning_evidence.py` 55% and `commissioning_receipt.py` 67% of their lines are `__post_init__`/`_core`/`to_dict`/`from_mapping` (14+9 classes each; 3,138 lines total, `scratchpad` AST count). 46 `object.__setattr__` ladders in the first file, 36 in the second, 22 in `commissioning_run.py`, 15 in `excitation_safety_plan.py` | 24 frozen dataclasses with an identical hand-rolled validate→canonicalise→fingerprint→serialise→deserialise quartet; `to_dict` is `return dict(self._core())` in 25 of 50 cases | one field-spec-driven validated-dataclass base (`_sha256`, `_identifier`, `_finite_positive`, `_text`, `_positive_int` are already the shared vocabulary); keep `_core()` key order so fingerprints are byte-identical | −1,500…−1,700 | med (fingerprints are durable state; existing tests pin them) |
| 8.3 | 7 implementations of the `volume_limit ≤ 0` clamp (§6) | a non-negotiable with seven owners | consolidate on `camilla_config_contract.ensure_volume_limit_db` | −12 | **non-negotiable tier** |
| 8.4 | `driver_safety.py:1552–1885` — 327 lines of LLM research-prompt copy (`build_driver_research_prompt` alone is 214L) inside the driver-safety *validation* module; 1 caller (`web/sound_setup.py:2552`) | prompt copy is content, not validation logic; it makes a 3,258-line module out of a ~2,900-line one and hides the schema | move to `driver_research_prompt.py` (or a text resource next to the presets) | ±0 net, −327 from driver_safety | low |
| 8.5 | `web_commissioning.py` (2,916L) + `web_measurement.py` (1,382L) live in the engine package but are web-flow orchestration; consumers are `jasper/web/*` only (plus `audition`/`null_door` reaching *back* into them) | REFACTOR-TUNING §1 target is "two THIN front ends"; here the front end lives inside the engine and the engine imports it back (`audition.py:318 from …web_commissioning import attempt_graph_restore`) | move the 4 genuinely shared primitives (`attempt_graph_restore`, `restore_wait`-style shields, `FaninGateContext`) down; move the rest to `jasper/web/` | ±0 net, boundary fixed | med |
| 8.6 | `graph_safety.py` named for a decision it does not make (§2.1); `bass_extension/bench/derivation.py:209` re-implements its private `_running_step_channels` | three names for one concept | rename `graph_view.py`, export the helper | −30 | low |
| 8.7 | `restore_wait.py` (58L, 2 callers) + `volume_latch.py` (317L, 53% prose) + `session_volume_plan.py` (1,291L, 47% prose) + `program_playback.py` (179L, 42% prose) + `playback.py` + `safe_playback.py` + `playback_route.py` = 7 modules for "play a thing at a safe level" | `restore_wait` is one 12-line idiom in its own file with a 14-line docstring; `program_playback` is one function | fold `restore_wait` into `playback.py`; keep the rest, they have distinct owners | −40 | low |
| 8.8 | `setup_status.read_active_speaker_setup_status` (513L, for `/correction/` + doctor + `/state`) and `commissioning_coordinator.build_commissioning_view` (431L, for `/sound/`) | two backend view models over the same setup journey, one per web surface; both emit `status`/`actions`/`messages` | not obviously mergeable (different inputs) — flag for the owner, do not merge blind | ? | high |
| 8.9 | `passive_profile.py` (62L, 2 funcs), `revalidation.py` (52L, 1 func), `restore_wait.py` (58L), `tuning_handoff.py` (145L, 1 caller) | four modules that are each one function | fold into their single consumer (`baseline_profile`, `baseline_profile`, `playback`, `web/sound_setup`) or into `_common.py` | −60 (file headers/imports) | low |

Not duplication, checked and cleared: `bundles.py` vs
`commissioning_evidence_store.py` (the store writes *inside* a bundle dir —
layered); `driver_safety` vs `driver_protection` (the former imports the latter);
`environment.classify_camilla_config_text` vs
`runtime_contract.classify_camilla_graph` (the latter calls the former at
`runtime_contract.py:3891`).

---

## 9. Ranked top moves

| # | move | Δlines | risk | proof | order |
|---|---|---:|---|---|---|
| 1 | Delete the orphaned summed-graph lane: 12 defs in `commissioning_runtime.py` + 11 in `web_commissioning.py` + `staging.prepare_summed_commissioning_config` + 3 constants, and the tests that only pin them | **−1,015** (+ ~1–2k test lines) | low | `scripts/test-merge`; only the named tests fail | 1 |
| 2 | Trim `__init__.py` to the 63 names actually imported through the façade | **−330** | low | `grep -rn "from jasper.active_speaker import"` + test-merge | 2 |
| 3 | Prose pass on the 6 worst-ratio files (`startup_hold`, `_common`, `driver_protection`, `volume_latch`, `session_volume_plan`, `program_playback`) to the AGENTS.md bar; move the rulings to ADRs | **−700…−900** | low | none needed (comments); `mypy` unaffected | 3 |
| 4 | Fix the stale docstrings in `commissioning_receipt`, `commissioning_capture`, `runtime_contract:3875` | −40 | low | read at HEAD | 3 |
| 5 | `camilla_yaml.py`: extract the shared prelude / devices block / write-tail from the 7 emitters | **−250** | med | golden-config tests + `/adversarial-review` (output path) | 4 |
| 6 | Consolidate the 7 `volume_limit ≤ 0` implementations onto `ensure_volume_limit_db` | −12 | **non-neg** | `/adversarial-review` + the volume-limit tests | 4 |
| 7 | Field-spec-driven validated dataclasses for `commissioning_evidence` + `commissioning_receipt` (+ `_run`, `excitation_safety_plan`) | **−1,500…−1,700** | med | fingerprint-equality tests must pass unchanged; do it as one PR per module | 5 |
| 8 | Rename `graph_safety.py`→`graph_view.py`, export `_running_step_channels`, delete the bass_extension mirror | −30 | low | test-merge | 5 |
| 9 | Move `build_driver_research_prompt` & friends out of `driver_safety.py` | −327 from that file | low | test-merge | 5 |
| 10 | Regroup: `commissioning/` subpackage (lane B, 5 files), `commission_*`+`startup_*`+`staging` as lane A, `commissioning_coordinator`→web view model, `commissioning_capture`→`driver_capture_bridge` | ~0 | low | test-merge; big diff, one mechanical PR | 6 |
| 11 | Fold the four one-function modules (`passive_profile`, `revalidation`, `restore_wait`, `tuning_handoff`) into their consumers | −60 | low | test-merge | 6 |
| 12 | Move `web_commissioning`/`web_measurement` to `jasper/web/`, leaving the shared primitives behind | ~0 | med | test-merge; coordinate with #3724 which is already editing `web/correction_crossover_v2.py` | 7 |

Conservative total: **≈ 4,300–4,700 production lines removed** from 62,456
(7–8%), plus a comparable test-line reduction from move 1, with no behavioural
change and no clamp weakened.

### Uncertainty / overlap with in-flight work
- Moves 1 and 12 touch `web_commissioning.py`, which **#3724** also edits
  (capture-source collapse). Sequence after #3724 merges.
- Move 7's risk is entirely about durable fingerprints. I did not verify that
  `_core()` dict ordering is the only fingerprint input for all 24 classes — a
  reader should confirm per class before touching it.
- 8.8 (two view models) is the one place I could not decide from evidence; it
  needs the owner's intent about `/sound/` vs `/correction/`.
- I did not read `crossover_v2/`, so any symbol I call dead could in principle be
  reached from there — but the identifier index in §4 covered `crossover_v2/`
  files too, so a plain-name reference would have shown up.
