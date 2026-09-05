# JTS quality review — consolidated findings register

Built from 6 Phase-0 cartography reports and all 38 Phase-1 tile reports, at SHA `2d571e6b8`.
Row detail (evidence, fix, LOC estimate, sources, territory, verify flag) lives in
[`register.csv`](register.csv) — **269 distinct findings** after dedup. Phase-2 lens reports
(`p2-*.md`) are deliberately **not** merged here; that is a later pass.

Severity is normalized against AGENTS.md's bar, not against what each tile called it. The CSV
keeps both columns, so every disagreement is visible. Tiles reported **23** Blockers; **13**
survive normalization. **16 were demoted** — a god file, a dead field, a duplicated constant, a
flat barrel and an unshipped ADR rule are debt, not correctness (R-047…R-050, R-059, R-075,
R-099…R-102, R-105, R-137, R-174…R-176, R-240). **6 were promoted** (R-008…R-013): each is a
silent failure — the system goes deaf, or reports healthy while broken — which AGENTS.md's bar
names explicitly even where the tile filed it as Should-fix.

---

## (a) Counts by severity × theme

| theme | Blocker | Should-fix | Nit | Earns-keep | total |
|---|---:|---:|---:|---:|---:|
| duplicate-primitives | 0 | 52 | 0 | 1 | **53** |
| dead-code | 1 | 31 | 0 | 0 | **32** |
| resilience | 5 | 22 | 0 | 1 | **28** |
| god-files | 0 | 27 | 0 | 1 | **28** |
| boundaries-cycles | 0 | 24 | 0 | 0 | **24** |
| observability | 3 | 18 | 1 | 1 | **23** |
| deploy-integrity | 2 | 15 | 2 | 0 | **19** |
| single-writer | 0 | 16 | 0 | 0 | **16** |
| config-knobs | 0 | 13 | 2 | 0 | **15** |
| tests-pinning-internals | 0 | 13 | 1 | 1 | **15** |
| tuning-zone-structure | 1 | 7 | 0 | 0 | **8** |
| prose | 0 | 5 | 1 | 0 | **6** |
| secrets | 1 | 0 | 0 | 1 | **2** |
| **total** | **13** | **243** | **7** | **6** | **269** |

By territory (per issue #4030 / #4085): steward-general 93, tuning 78, hardware-input 55,
doctor-state-resilience 22, web-ui 21.

**73 rows are flagged `verify=Y`** — every Blocker, every deletion over 100 LOC, every
"X is dead/unreachable", and every claim that contradicts the previous audit or an open PR.
Those need an independent skeptic before anyone acts on them.

Net `loc_delta_estimate` across rows carrying one: **≈ −52,000 lines**. Roughly half of that is
concentrated in eight rows (R-179 bass bench 2,173; R-156 throwaway benches 2,150; R-203 landed
docs 3,021; R-223 web migration guards 2,500; R-050 `_active_speaker_*` 2,800; R-122 record
boilerplate 1,200; R-202 Rust dead arms 700; R-233 prose 8,000). Treat the number as a scale
signal, not a budget: the estimates are the tiles' own and were not re-derived.

---

## (b) Top 40 by impact

`V` = needs independent verification. Severity: **B**locker / **S**hould-fix.

| id | sev | title | files | fix | in-flight | V |
|---|---|---|---|---|---|---|
| R-001 | **B** | Secret redactor misses every JASPER_*_PSK / _API_KEY shape | jasper/secret_redaction.py:24-30 | Anchor _KEY_VALUE_RE on a name suffix like scripts/_diagnostic_redaction.sh:18 does; add a parametrized behavior pin… | — | Y |
| R-002 | **B** | SKIP_INSTALL=1 silently bypasses the identity and deploy-direction guar… | scripts/deploy-to-pi.sh:590-602,674-689 | Run identity + direction guards and hostname resolution unconditionally before rsync; gate only the install.sh invoca… | — | Y |
| R-003 | **B** | A polkit-denied unit action returns HTTP 200 and reports success | jasper/control/handlers/system.py:499-514; jasper/control/server.py:133; depl… | Route every unit mutation through restart_broker.manage_units/reset_then_manage; add a test that local_source_*_units… | — | Y |
| R-004 | **B** | Streambox runs the LAN wizard surface as root, barely hardened | deploy/jasper-web-streambox.service:11-44 vs deploy/jasper-web.service:126-173 | Delete jasper-web-streambox.service; keep one jasper-web.service and let the socket be the only profile-varying file | — | Y |
| R-005 | **B** | Clip-stop retry apparatus rearms forever with no attempt cap | jasper/wake_corpus/recording_backend.py:1404-1628 | Replace the 250-LOC generation/Timer apparatus with one bounded blocking acquire plus a capped attempt count and an a… | #4030 (wake_corpus split) | Y |
| R-006 | **B** | info!/warn! and a String allocation on the SCHED_FIFO audio thread | rust/jasper-fanin/src/mixer.rs:1143,1784,1962,2550,2606; mixer/ring_capture.r… | Route mixer-thread events through a bounded sync_channel to a log-writer thread - the pattern the crate already uses… | — | Y |
| R-007 | **B** | v1 commissioning apply's terminal readback cannot pass on real hardware | jasper/active_speaker/commissioning_apply.py:888-905 | Owner re-ruling required: repair or delete the v1 lane (21,667 LOC). Until then nothing new should be built on it | ADR-0228 row 9 / PR #3836… | Y |
| R-008 | **B** | Wake in the acquire window during MEASURE_PAUSE: deaf 120 s, no cue | jasper/voice/output_gate.py:85-102; jasper/voice_daemon.py:2612,5073 | Give begin_turn() a bounded wait on the MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC budget returning None, and treat None as… | — | Y |
| R-009 | **B** | Wake dropped after a research-confirmation cancel timeout, WARN only, n… | jasper/voice_daemon.py:3468-3478 | Play INTERNAL_ERROR_CUE_SLUG before the return and emit event= instead of prose | — | N |
| R-010 | **B** | Corpus test mode stops jasper-voice and nothing outside the page knows | jasper/web/wake_corpus_setup.py:849; jasper/wake_corpus/recording_backend.py:… | Emit wake_corpus.test_mode on both edges and add a doctor check on TEST_MODE_MARKER age | #4031 | Y |
| R-011 | **B** | Per-device HID reader tasks die permanently while status reports healthy | jasper/accessories/bridge.py:643-663,727-738; jasper/accessories/supervisor.p… | Add a done-callback that logs event=knob.reader_failed and re-arms via _maybe_start after a bounded backoff; count it… | — | Y |
| R-012 | **B** | Unbounded D-Bus calls on the /speaker/ save request path | jasper/speaker_name_discovery.py:161-186; jasper/web/speaker_setup.py:195 | Wrap the body in `async with asyncio.timeout(bluetooth_timeout + 2)` | — | Y |
| R-013 | **B** | CLEAR_CONFIGURATION is a reachable write-only chip-config command (bric… | jasper/xvf/xvf_host.py:88 | Delete the entry; add CLEAR_CONFIGURATION to _FORBIDDEN_COMMANDS | — | Y |
| R-014 | S | The two live graph-write doors have no volume_limit check (non-negotiab… | jasper/camilla.py:900-931,1000-1039; jasper/web/sound_setup.py:1406 | Run parse_camilla_devices_config in set_active_config_raw and refuse a missing or >0 volume_limit (the predicate dsp_… | — | Y |
| R-050 | S | 55 _active_speaker_* functions (2,784 LOC, 56% of function mass) duplic… | jasper/web/sound_setup.py:2924,4089,4112,4173-4358 vs jasper/active_speaker/w… | web_commissioning.py is the owner (/correction/ already uses it, it has the factored helpers, it is in the right pack… | #4085 owner call 1 | Y |
| R-049 | S | A 7,165-line domain engine lives in jasper/web with no routes | jasper/web/correction_crossover_v2.py (7,165 LOC) | Move whole to jasper/active_speaker/crossover_v2/host/. Phase D.5a-b only moves the durable-state and session-volume… | #4031 Phase D.5 | Y |
| R-048 | S | runtime_contract holds six unrelated concerns; only one is about active… | jasper/active_speaker/runtime_contract.py (5,152 LOC) | Four modules: output_contract.py, ring_lane.py (leaves beside output_topology), graph/active_verifier.py, runtime/gra… | — | Y |
| R-073 | S | The full/streambox install fork is expressed by copy in ~8 places | deploy/install.sh:2242-2335; deploy/lib/install/systemd-units.sh:1302-1877 | One STEPS table (name / profiles / fn / plan-phrase) that main() iterates and --dry-run prints; deletes both heredocs… | — | Y |
| R-239 | S | No cargo workspace: the crate roster is spelled in five places | rust/ (8 crates, 8 Cargo.lock, no [workspace]); .github/workflows/tests.yml:4… | Add rust/Cargo.toml with [workspace] + [workspace.dependencies] and ONE [profile.release]; keep -p separation for the… | — | Y |
| R-240 | S | ADR-0233 rule 5 unshipped: the third health tool still gates deploy | deploy/bin/jasper-deploy-health (900 LOC); deploy/install.sh:2173; scripts/de… | Land the --core registry subset with lazy per-module imports; make it the CLI's default shape with --all behind a fla… | ADR-0233 rule 5 | Y |
| R-105 | S | Five filter-magnitude implementations, only one guarded pair | jasper/sound/profile.py:905; deploy/assets/sound-profile/js/eq-math.js:35; ja… | Vectorize sound/profile._biquad_coeffs plus its magnitude evaluator into jasper/dsp_numpy.py; route linearization_fit… | — | Y |
| R-108 | S | atomic_io has 234 call sites and ~19 files still hand-roll the write | jasper/atomic_io.py vs jasper/dsp_apply.py:311, correction/replay_artifacts.p… | Route all of them onto atomic_io; add one atomic_write_wav(); make wifi_guardian_persistence call atomic_write_text(m… | — | N |
| R-112 | S | The canonical systemd reader has one consumer and three rivals | jasper/service_units.py:167 vs control/system_metrics.py:713-737, web/_unit_s… | Give read_unit_states an optional properties= tuple; port system_metrics and _unit_snapshot onto it (keeping their da… | #4085 item 4 | Y |
| R-204 | S | Only 27% of the JASPER_* surface is a knob anything turns | jasper_env_ledger.csv (829 tokens); .env.example:293-313,396-399; deploy/lib/… | Delete the 40 dead tokens, the tombstone block and the two live-but-unread assignments; replace the scrub with a one-… | — | Y |
| R-206 | S | Four dormant wake legs are the biggest dormant-branch surface in the tr… | JASPER_WAKE_LEG_{DTLN,CHIP_AEC,CHIP_AEC_150,CHIP_AEC_210}; jasper/wake_legs.p… | Delete the four, or move them behind one JASPER_WAKE_LAB_LEGS list key | — | Y |
| R-074 | S | The 98-module import cycle exists only via function-local imports | jasper/active_speaker/*, jasper/sound/camilla_yaml.py:90,281,563; jasper/outp… | Say it out loud in the doctrine, then retire named deferred edges one at a time. Highest-value: move the flat-graph v… | — | Y |
| R-086 | S | The four ruled-for-deletion flow barrels are still at HEAD | jasper/active_speaker/crossover_v2_flow.py:255-355,456-830 | Delete the 93 test-only doors and repoint those suites at the owning organ; repoint the 26 production names at crosso… | REFACTOR-CUTOVER S6 delet… | Y |
| R-085 | S | 143 of the 207 lazy re-export doors have zero readers anywhere | jasper/active_speaker/__init__.py:20-229 | Delete the 143 entries; the test shrinks with its subject and stops importing every heavy submodule to prove doors no… | — | Y |
| R-099 | S | Two session engines run in one request; the new one is subordinate | jasper/web/correction_crossover_v2.py:6286-6305; crossover_v2_flow.py:874; cr… | Land W5-b: make TuningSession the only session object and delete CrossoverV2Session's capture loop | REFACTOR-CUTOVER W5-b | Y |
| R-101 | S | Two live level ramps; the kernel-backed one is unreachable from Room | jasper/correction/session.py:2244-2444; jasper/correction/autolevel.py (whole) | Delete the autolevel.py ramp and wire /autolevel/* to LevelMatchSession, or delete session's level-match half. Not bo… | — | Y |
| R-179 | S | Five bench modules with zero importers; three outside ADR-0018's park | jasper/bass_extension/bench/{executor.py (1200), stimulus.py (285), live_proo… | Not a unilateral delete (ADR-0018 sets the bar) - but the three unnamed modules are outside the park's boundary and n… | ADR-0018 | Y |
| R-175 | S | The aplay tone backend is unreachable; ~330 LOC guards it | jasper/active_speaker/playback.py:84-155,263-375,900-995,1016-1102; audible_p… | Delete the backend, the gate ladder, FORBIDDEN_TEST_PCM_TOKENS, jasper/audio_lab.py and both JASPER_AUDIO_LAB_* knobs… | — | Y |
| R-176 | S | The whole mDNS-browse and STATUS half of peering is dead | jasper/peering/discovery.py (all); daemon.py:125,282-345,422-436; uds.py:20-2… | Delete discovery.py, the status= param and STATUS/PING branches, _known_peers/_prune_stale_peers/_last_decision/STALE… | — | Y |
| R-173 | S | Nine to ten unreachable wizard main()s kept alive by a meta-test | jasper/web/{voice,google,wake,transit,home_assistant,wifi,rooms,sound,spotify… | Delete the main()s and their argparse/__main__ tails, drop jasper-web and jasper-sound-web from [project.scripts], dr… | — | Y |
| R-193 | S | ~450 LOC for a future_fir feature that does not exist | fir_runtime.py (301); confidence.py:270-283; evidence.py:362-366,384; bundle_… | Delete the future_fir gate/permission/readiness (keep stage_fir_artifact only if the owner uses the CLI today); eithe… | — | Y |
| R-221 | S | Python contract tests regex-match Rust source, and have bent it | tests/test_ring_wire_format_contract.py:136,152,178; test_route_latency_tap_t… | Delete the source-scraping halves; the Rust unit tests already pin the same behaviour. The genuinely cross-language c… | — | Y |
| R-223 | S | ~3,700 lines of web tests assert raw markup/CSS text as completed-migra… | tests/test_web_design_system.py (610); test_web_wizard_conventions.py (1,090)… | Fold the still-meaningful pins (secret absence, route surface, CSRF presence) into the primary test file and delete t… | #4031 Phase C/E tax | Y |
| R-256 | S | The doctor's hottest facts are read 24 times per run, unmemoized | jasper/cli/doctor/_shared.py:313-321; grouping.py:537,617,718,854,1001,1068,1… | One line: _parked_follower_result calls evidence.parked_bonded_follower(); evidence.get('grouping_config', load_confi… | #4127 (points the wrong w… | Y |
| R-258 | S | 51% of doctor rows cannot fail, including two security-posture regressi… | 172 registered checks; 87 with no reachable fail; privsep.py:610; web.py:346;… | Promote the two security rows to fail; make the skipped rows say skipped; generate the privsep ten in a loop; fix the… | — | Y |
| R-233 | S | Prose mass concentrated in the tuning stack and the DSP control plane | jasper/: 21% of 424k lines are comment+docstring; worst packages transit/ 0.9… | Delete history, dates, PR numbers and reviewer-addressed text; keep the non-derivable constraint sentence and a why-p… | #4106/#4113 (scope unconf… | N |

---

## (c) Premises refuted

Places where a tile corrected the brief, Phase 0, `docs/DEEP-AUDIT-2026-08-25.md`, an issue, an
ADR, or another tile. Each of these was believed going in and is false at HEAD.

| # | The premise | What the evidence says | Source |
|---|---|---|---|
| 1 | "A 34-module import cycle in the audio-routing core" (p0-inventory §6/§11.1) | **The top-level import graph of `jasper/` is acyclic.** Tarjan over module-level imports gives a largest SCC of 6 (`config`/oauth) with no `active_speaker` module in it. The cycle is **98–99 modules and exists only when function-local imports are counted** — the codebase keeps the static graph clean by pushing edges into function bodies. No single edge frees it (best single removal: 99→86). | T14-2 C5, T14-3 C11, T14-1 F6, T14-4 C6 |
| 2 | `jasper/web/correction_crossover_v2.py` has a 994-LOC `_make_handler` (brief) | There is **no `_make_handler` in that file at all** — it has zero routes, zero HTML, zero CSRF. The 994-LOC one is `correction_setup.py:3902`; `sound_setup.py:4702` is 741. Both measure CC 1: they only define a class and return it. The repo's most complex web functions are `_post_apply_grade` (CC 66), `handle_v2_apply` (CC 66) and `prepare_v2_session` (CC 57). | T16-1 A |
| 3 | "35 `_active_speaker_*` functions in `sound_setup.py`" (#4085 owner call 1) | **55** (35 sync + 20 async), **2,784 LOC, 56 % of the file's function-body mass** — measured, not estimated. A second tile independently counted 54. | T16-2 extra, T16-3 E |
| 4 | `rust/jasper-fanin` host-compliance / prime machinery is open work (DEEP-AUDIT :101,:179) | **Deleted on 2026-08-26 by PR #2989** (`ea6f5d753`), one day after the audit's date. No `host_compliance.rs`, `HostComplianceState` or `service_host_compliance` exists at HEAD. The surviving `prime_periods` in `lane_resampler.rs` is unrelated live machinery — a bounded startup-priming fallthrough. | T19-1 E, T19-2 A |
| 5 | `jasper-wake-corpus-web` is an orphan console script (p0-orphans §3, medium conf.) | **Not an orphan.** Its module docstring documents direct standalone invocation (`sudo /opt/jasper/.venv/bin/jasper-wake-corpus-web`) as a deliberate alternative to the lazily-bound route. Both paths are real. | T17-1 E |
| 6 | `jasper-aec-sweep-config` is an orphan CLI (p0-orphans §3, medium conf.) | **Not an orphan.** It is an SSH/operator-invoked stdlib-only door, structurally identical to `declare_geometry`, `control_token` and `output_topology_reset` — all of which have zero in-repo callers *by design*. The real orphan signal is "registered in pyproject but the running path bypasses it" (the `jasper-web` case), not "nothing in-repo calls it". | T17-1 E |
| 7 | `c/jts-ring-ioplug/ring_{writer,reader}_bench.c` are dead (p0-orphans §8) | **Keep.** Read in full: each is a self-documented on-hardware interop tool covering a real gap — cross-language byte-level interop through a real mmap, which no CI test exercises — at the cost of two cheap compiles. | T22 C2 |
| 8 | `jasper/bass_extension/targets.py` ↔ `adapters/base.py` is an import cycle (p0-inventory §6, cycle 2) | **Not circular** — `adapters/base.py:14-15`'s `targets` import is `TYPE_CHECKING`-only. The real cycle is one hop over, *inside* `adapters/`: `base.py` imports its three siblings at the bottom of the file to build `ADAPTERS` while each imports `base` at the top. It works by file-order luck. | T15 C2 |
| 9 | `jasper/mics/` has zero test coverage (p0-tests §1 attribution table) | False alarm, stated as such by its own author: `xvf3800.py` is tested under the `chip_aec`/`xvf` attribution bucket. | p0-tests §1 |
| 10 | The repo has ~30 days of history (brief) | **~3 days.** 1,140 commits, 2026-09-02 → 2026-09-05, i.e. ~380 commits/day. No tracked file is stale by any threshold, because the whole visible history is younger than one. | p0-inventory §8 |
| 11 | `jasper/cli/doctor/` runs 175 checks (ADR-0233, `jasper-doctor-json.service:18`, #4043, #4082) | **172.** Stale in three places. The previous audit's "90 of 169 checks cannot fail" is confirmed and essentially unchanged at **87 of 172 (51 %)**. | T10 headline |
| 12 | `jasper/control/` has 96 `systemctl` sites (previous audit) | **74 argv sites**, of which ~28 are read-only probes and ~36 mutate; 13 of the 36 live inside `jasper/control/` itself. The 96 counted *mentions*, not sites. | T08 C10 |
| 13 | `scripts/ci-classify.py` is 545 lines with 1,087 test lines (prior-phase note) | **354 and 583** at this SHA (937 combined, not 1,632) — either the earlier count was wrong, measured another SHA, or #4030's rightsizing already landed. Current size is proportionate; do not cut further. | T27 C11 |
| 14 | `docs/REFACTOR-CUTOVER-2026-08.md` §6.2 is a usable execution map | Stale in a way that would misdirect an executor: it cites `EngineSeams` as five fields at `session_seams.py:299-303`; at HEAD the file is 133 lines and the dataclass has four (`graph, volume, records, play`). `recommend` does not exist. It also claims `correction_crossover_v2.py` is 8,088 lines and `crossover_v2_flow.py` 9,228; actual 7,165 and 4,636. | T13-1 A/E, T16-1 E |
| 15 | `jasper/web/correction_crossover_v2.py`'s ~146 lazy `active_speaker` imports defer a numpy cost | They buy nothing: the one **eager** `jasper.active_speaker` import at `:76-89` already pulls `crossover_v2.journey → branch_chain → numpy` (verified by import traceback). Its own comment concedes the module went from ~0.05 s to ~0.34 s. Only the 9 `jasper.web` ones are real cycle dodges. | T16-1 C2 |
| 16 | `crossover_v2/contracts.py` is the numpy-free leaf consumers can take cheaply | False at HEAD, and three separate comments justify duplications by it: `contracts.py:25` imports `CrossoverSection` from `branch_chain`, which imports numpy at module level. The cost is paid anyway. | T13-1 C1 |
| 17 | `service_units.py` is where jasper-control's samplers, the doctor and the soak read unit state (its own docstring) | **Half false.** `read_unit_states` has exactly one consumer (`cli/doctor/_evidence.py:187`); soak and the sampler share the roster and parser but rebuild the command by hand and drop `UnitFileState`. | T18 §1 |
| 18 | `restart_broker`/`jasper-control` is "the single mediated systemctl boundary" (`server.py:2253`) | The tree contradicts itself: `restart_broker.py:41` says "**NOT brokered, by design** — not the tree's only systemctl". The `main()` claim is the false one, and the 13 unaudited in-tile mutating sites are how R-003 became possible. | T08 C10 |
| 19 | `spatial_combine._analytic_envelope` exists because "scipy is not a JTS dependency" (its docstring) | `pyproject.toml:27,80` pin `scipy>=1.13`. Three more DSP primitives in the same file duplicate in-package owners for no reason. | T12-2 C5 |
| 20 | `jasper/multiroom/` is "Later" per PLAN.md | **Live, not speculative.** `deploy/install.sh:1564` enables `jasper-grouping-reconcile.service` on every install and every boot on both profiles; only `snapserver`/`snapclient` binaries are opt-in. "Later" means no new features, not unshipped code. | T09 E |
| 21 | The `jasper-usbmic`/`outputd` `ExecCondition` `backend=fake` escape covers a real no-DAC mode | Nothing in-tree produces it: the unit hardcodes `JASPER_OUTPUTD_BACKEND=alsa` and no writer sets `fake`. | T20 E |
| 22 | `outputd`'s `config.rs:246` says no in-tree writer arms the dac-content ring key | Exactly backwards: `jasper/multiroom/reconcile.py:586` writes `DAC_CONTENT_LANE_ENV: "1"`, and the same function writes the **FIFO** key empty in all three branches — so the FIFO, not the ring, is the orphan. | T20 C6, C7 |
| 23 | `jasper/web/wake_corpus_setup.py`'s facade "keeps every `NAME` resolving for existing callers" (its docstring) | The only production caller, `jasper/web/__main__.py:193-201`, uses **three** names; 62 of 135 are never referenced in the file at all, and tests patch `bridge_session` directly 29 times against 2 through the shim. | T16-1 C6, T02 F |
| 24 | `test_lint_contracts.py`'s line ceiling forced the `correction_crossover_v2_republish.py` split (its docstring) | The file is 7,165 against a **10,000** ceiling; every entry has 30–220 % headroom. The rule's own stated removal condition ("when every file here is under 5,000 lines, delete the rule") is one file away. | T16-1 C14 |
| 25 | `jasper/config.py` has dead fields (grep-level suspicion) | **None.** All 94 are reachable; five reach their consumer only through string dispatch (`voice_daemon._LEG_DEVICE_ATTR`) or `getattr` (`voice/input_policy`), which is why grep says dead. | p0-config §4, T18 note |
| 26 | `host_compliance`, `bass_alignment.py`, `channel_split.py`, `orbs.js`, `program_analysis.py`, the wake-events 1 GiB cap — DEEP-AUDIT's verified deletions | All six re-checked and **already executed**. The 11-day-old audit has been substantially actioned; that is a signal the process works, and a reason this register should feed a ledger rather than become a second untracked snapshot. | p0-docs §3 |

---

## (d) Cross-tile duplicates merged

Where several reports found the same thing, one row carries the best evidence and every source id.
The `sources` column in the CSV is the audit trail.

| Register row | Merged from | What the merge resolved |
|---|---|---|
| R-001 secret redactor | T18 F1, T26-2 C2, p0-tests §11.1 | p0-tests found "zero test references"; T18 **ran** the function and found the `\b` bug; T26-2 independently reproduced it and named the correct regex shape in `_diagnostic_redaction.sh`. Kept all three: the gap, the mechanism, the fix. |
| R-002 SKIP_INSTALL | T23 C1, T26-1 C1, T26-2 C1 | Three tiles found it independently from two directions (deploy/lib and scripts/). T26-2 added the detail the others missed: unlike the sudo skip, this path prints **no notice at all**. |
| R-074 the "34-module cycle" | p0-inventory §6/§11.1, T14-1 F6, T14-2 C5, T14-3 C11, T14-4 C6 | Four tiles independently ran Tarjan and agreed the module-level graph is acyclic. Kept T14-2's framing (a list of named deferred edges, not a knot) plus T14-1's and T14-3's specific highest-value cuts. |
| R-105 filter magnitude | p0-dup §5, T05 E | p0 found five implementations and the CI-guarded pair; T05 added `camilla_config_contract.py:494-500` naming the JS twin as a third evaluator that must agree on `SHELF_Q`. |
| R-108 / R-109 atomic writes | p0-dup §4, T18 F2+F9, T12-2 C2, T14-1 F11, T11, T07 C8, T17-2 C5/C12 | p0 listed 15 bypasses; T18 found the ratchet **hole** that lets 19 escape and showed the guard catches the *safe* hand-rolls while missing the unsafe fixed-`.tmp` ones. Split into two rows: the bypasses, and the guard. |
| R-107 `_utc_now` | p0-dup §10, T14-1 F5, T14-3 C7, T14-4 C7 | p0 counted 20 repo-wide with three formats; the active_speaker tiles counted 12–22 within one package and found two modules importing a *sibling's private* copy. |
| R-112 systemd readers | T18 F4, p0-dup §17, T06 E, T16-3 C8 | p0 and T18 found the rival readers; T06 added six more "is this unit active" implementations; T16-3 found a third one inside `jasper/web` in a file that already imports the batched probe. |
| R-113/R-114/R-115/R-116/R-117 fanin↔outputd twins | T19-1 F5, T19-2 C1–C3, T20 C1/C2/C8/C9/C14, T21 C9 | Both daemon tiles plus the shared-crate tile listed overlapping twins. Merged into five rows by *what* is twinned, and kept T20's finding that the fork has already cost correctness (outputd silently lost fanin's TTS protocol-error and stale-command counters) and T19-2's that outputd still ships the accept-loop bug fanin fixed. |
| R-118 canonical-JSON fingerprints | T12-1 C2, T12-2 C11, T14-3 C12, T14-4 C9 | Two tiles counted 4 and 6 inside `audio_measurement`; the active_speaker tiles found 7–12 more and — the load-bearing part — that three are **semantically different** (`default=str` collides distinct objects; another emits invalid `NaN`). |
| R-141/R-144/R-145 bash twins | p0-dup §12, T26-1 C3–C5, T26-2 C6/C7/C10 | The two scripts tiles each found half of the ssh/log/die/journal duplication; p0 found the env-file read fork and the two verbatim bootstrap copies. |
| R-147 web handler boilerplate | T16-3 C11, T16-2 extra, T16-1 C8, p0-dup §14 | Three web tiles each found a slice (19 `log_message`, 13 `_send_json` delegates, three JSON-body readers, three `main()` blocks). One row, one fix. |
| R-173/R-174 wizard `main()`s | p0-orphans §3, p0-dup §1, p0-config §6a, T16-3 C6, T17-1 E | p0 found the dead `main()`s; p0-dup found that `jasper-web` starts only the Spotify wizard; p0-config found the ~16 orphaned `*_WEB_PORT` knobs that die with them; T17-1 confirmed the two entry points are safe to delete and refuted two adjacent orphan claims. |
| R-219/R-221/R-222/R-223/R-224 source-text tests | 12 tiles | Grouped by *what is being scraped* (Python subjects, Rust source, `install.sh`, web markup, whole-tree AST) rather than by tile, because the fix differs per group. T19-1 supplied the smoking gun: `config.rs:620-626` is a comment forbidding production code from using a constant *because a Python test scrapes the literal*. |
| R-233/R-235 prose | 9 tiles | Merged the per-tile percentages into one scale row, and split out the ~15 comments that are provably **false** — a different and more urgent problem than volume. |
| R-256/R-257 doctor ↔ /state | T10 C2–C8, T08 C7 | T10 owns the doctor half, T08 the `/state` half of the same three duplicated facts; the memoization row and the duplication row are separate because they have different fixes. |
| R-014 / R-036 non-negotiable 1 | T05 (both), T12-1 C4 | T05 verified the clamp holds at every emitter and found the two unguarded live doors; T12-1 found a second spelling of the `0.0` constant in `delay_graph.py`. |
| R-179 bass bench | T15 C4/C5, p0-orphans §1a, p0-tests §11.9 | p0 reported the ADR-0018 park at package granularity; T15 found that **three** dead modules sit *outside* the park's named boundary and that `bench/__init__.py`'s own map claims an integration `runner.py`'s import list disproves. |
| R-050 `_active_speaker_*` | T16-2 C1–C4, T16-3 E, T14-1 F2 | T16-2 measured the count and found the three stale forks and the divergent summed-test flow; T16-3 confirmed the count independently; T14-1 found the 12-name private boundary from the other side. Two tiles converged on the same owner call. |

Also deduped without a merged row, because one tile refuted the other: T22 kept the C ring
benches p0-orphans proposed deleting; T17-1 kept two CLIs p0-orphans flagged; T15 corrected
p0-inventory's `bass_extension` cycle. Those are in section (c).

---

## (e) Earns-its-keep — consolidated

Everything a reviewer tried to cut and could not. Six CSV rows (R-264…R-269) carry these; the
report credits them here so a later pass does not re-open them.

**Guard tests that are the house style** — `test_cue_registry_coverage.py` (bidirectional
registry↔play-site cross-check, no allowlist, NN6); `test_xvf_host.py:17-30` and
`test_aec_probe_xvf_ref_level_script.py:38-49` (forbidden-command AST/fragment scans, NN2);
`test_doctor_renderers.py:704-716` (the exact `aplay` argv pinned **as a list** with a comment
naming non-negotiable 5); `test_camilla_systemd_unit.py:32-70` (directives parsed as structured
fields, cross-checked against sibling units, citing the incident it guards);
`test_launch_blocker_docs_exist.py` (cites the PRIVACY.md-silently-dropped incident).

**The paid-lane isolation** — four independent layers (`voice_eval/conftest.py`'s session-scoped
key check, `test-merge --ignore`, a required CI step with no keys in the workflow to find, and an
empty `test-fast` routing arm), plus `test_voice_eval_registry.py`, a hardware-free regression
test for a paid-only code path that caught a real bug.

**Converged seams a naive duplicate sweep would re-open** — the two CamillaDSP emitters over one
primitive layer (the active emitter *calls* `emit_sound_config`); `analysis.smooth_fractional_octave`
and `deconv.magnitude_response` used by 23 modules across three packages; the doctor's one `_run`
behind 40 call sites, one registry, one `CheckResult`; `jasper-clock` (pure DLL) vs
`jasper-host-clock` (ALSA servo composing it) — a real split, not duplication; `deploy/assets/shared/js/`
(only 2 CSS selectors in ≥3 files; `escapeHtml`/`getJson`/`fetchJson`/`debounce` have zero
reimplementations outside it); one artifact-manifest implementation with 8 consumers; the
JS↔Python↔CamillaDSP PEQ parity triple, which is the model R-105's five forks should follow;
the ring golden-layout contract spelled in C, Rust and Python (no shared-type mechanism exists
across a raw-shared-memory FFI boundary, so the three spellings plus their cross-checks *are* the
mechanism).

**Machinery that looks like ceremony and is not** — `camilla.py`'s shield + `_abort_active_websocket`
dance (pycamilladsp is sync; asyncio cancellation does not stop a thread); `dsp_apply`'s task-keyed
re-entrancy (deleting it deadlocks); `StatefileCamillaController` (converges a box with CamillaDSP
down, cited incident, bounded); `openwakeword_guard`'s measured 78 MiB RSS table;
`env_load.ENV_FILES` + `test_env_load_mirrors_unit.py` (reads `deploy/systemd/*.service` — the best
boundary guard in the platform tile); `LaneFade`'s provably ≤1.0 gain and the complementary-pair
argument; `DirectOpener` (`snd_pcm_open` blocks and cannot run in a 5.33 ms budget; the `parked:
Vec<PCM>` even guards against `Drop` closing on the render thread); `narrow_period_i24_le`;
`content_fill.rs` (answers "deaf right now", which no cumulative counter can); the C
`jts_ring_pace_refill_tokens` overflow bound with its written proof, exercised to `UINT64_MAX`;
`ha_status_cache` + `ha_probe_child` (keeps httpx out of a resident daemon *and* returns
non-blocking); ADR-0225's accessory supervisor (systemd's restart unit is the process; a BLE
fault must not stop the HID button path); `PeriodPacer` (guards a documented `status=9/KILL`);
`mux.py`/`renderer.py`'s `asyncio.timeout()`-not-`wait_for` notes (a real CPython ≤3.11 defect
with a live 3.11 target); `volume_persistence.operation_lock`'s cancel-during-`flock` dance.

**Large subjects that survived a split attempt** — `commissioning_run.py` (bounded on every axis,
validates a whole-file fingerprint on every read, kernel releases the lock on exit so a restart
self-recovers); `cli/wiim_remote_ce.py` (every choice backed by an on-hardware measurement or a
cited kernel behaviour); `scripts/run-crossover-round.py` (composes product CLIs rather than
reimplementing them; one honored exit-code contract); `scripts/_first_party_arm64_release.py`
(fail-closed throughout, stdlib-only *by design* so it works pre-venv); `gating.py` (33 % prose,
nearly all measured operating points); `excitation.py` (13 lines, one constant, 17 importers —
exactly the right shape); `attribution/` (imported by two different top-level packages, which is
what a small shared package is for); `cli/round_views/` (`_FAMILIES` + a uniform `add_parser`
**is** the dispatch table the brief asked about); `crossover_v2/admission.py` (the two-function
split is a *lock* boundary); `sweep_spec.py` (a hostile-input boundary where strictness is the
product); `build-sandbox.sh` (the OOM-victim inversion is the only thing between a 1 GB Pi build
and a dead daemon); `jasper-core-graph-park-units.sh` (sourced by both installer and runtime with
a test pinning that no copy survives — the model for R-161 and R-162);
`wake_training/feature_bank.py`; `sound-profile/js/main.js` (7,655 LOC, deliberately *not* split
until the live-draft band-drag path is exercised on real Pi hardware — "do not defend
hypotheticals" cuts both ways).

**Secrets handling** — clean everywhere it was checked except the redactor itself: `wifi_setup`
scrubs echoed-back PSKs out of nmcli stderr and routes every PSK-bearing argv through
`_run_nmcli_secret`; provider exceptions are replaced key-by-key before reaching a flash and
`log_event` records only the exception class name; the two compartments have one wizard writer
each with modes re-asserted every deploy and a doctor audit; BusTime keys go through
`scrub_secrets` at every interpolation; `rooms_setup` redacts the control token out of a peer's
error body. One residual: `google_creds.py:283` logs the google-auth `RefreshError` verbatim.

**Discipline that holds completely** — zero `TODO`/`FIXME`/`XXX`/`HACK` markers across
`jasper/ rust/ c/ deploy/ scripts/` (all 19 naive matches are `mktemp` templates or prose about
another project); zero `xfail`, zero `@pytest.mark.flaky`, and every one of 45 `skipif`s carries a
reason; only 14 module-level env reads in all of `jasper/`, none of them a wizard-owned value.

---

## (f) Coverage ledger

Tile inventory: **38 tiles, 1,145 files, 573,044 lines**. Every tile reported its own coverage
section; the numbers below are theirs, not re-measured.

| tile | LOC | read fully | read structurally | skipped (stated) |
|---|---:|---:|---:|---|
| T01 voice-loop | 14,745 | ~3,000 | ~4,500 of 6,900 | nothing in tile |
| T02 assistant-tools | 16,940 | ~9,600 | ~4,300 | nothing in tile |
| T03 integrations | 13,043 | ~11,400 | ~1,650 | nothing in tile |
| T04 sources-volume | 14,990 | ~5,000 | ~7,500 | nothing (tests grepped only) |
| T05 dsp-control | 15,446 | ~8,300 | ~7,100 | nothing in tile |
| T06 output-hardware | 15,810 | ~5,900 | ~2,800 of 8,600 | `audio_io` TtsPlayout bodies; 20 `audio_validation` check builders; the 2,197-line reconciler bash (grepped) |
| T07 aec-mic | 14,635 | ~11,400 | ~3,200 | 2 `.cpp` files not in the tile list |
| T08 control | 18,959 | ~6,600 | ~4,900 | nothing in tile |
| T09 multiroom-peering-accessories | 13,727 | 13,700 (37/37) | — | ~9.8k tile-test LOC (grepped for pin styles) |
| T10 cli-doctor | 19,723 | ~5,900 (22 files) | ~5,600 of 13,800 | 21k doctor test suite; `jasper-deploy-health` (900) not read |
| T11 correction | 13,872 | ~11,400 (26 files) | 2,444 | ~40 correction test files; correction JS |
| T12-1 audio-measurement-1 | 12,740 | ~1,550 | ~5,750 | nothing (math skimmed, AST census 100 %) |
| T12-2 audio-measurement-2 | 12,687 | ~6,100 | ~4,900 | nothing in tile |
| T13-1 crossover-v2-1 | 22,119 | ~9,600 | ~7,500 | nothing in tile |
| T13-2 crossover-v2-2 | 22,115 | ~9,400 | ~8,000 | ~89 `test_crossover_v2_*` files; the two god files beyond cited ranges |
| T14-1 active-speaker-1 | 24,216 | ~9,400 | ~10,000 | pure DSP/numeric bodies (skimmed per brief) |
| T14-2 active-speaker-2 | 24,200 | ~11,500 | ~12,700 | nothing in tile |
| T14-3 active-speaker-3 | 24,237 | ~19,000 | (incl. above) | nothing in tile |
| T14-4 active-speaker-4 | 24,208 | ~3,600 | ~8,000 + AST over all 28 | solver/statistics/deconvolution interiors |
| T15 bass-attr-latency | 14,273 | ~2,400 | ~11,700 | full bodies of 6 bench modules (reachability answered by import graph) |
| T16-1 web-1 | 15,073 | ~4,000 + 3 partials | 3,478 code lines of the 7,165-LOC file | 3,258 prose lines (sampled ~600); 3 function bodies |
| T16-2 web-2 | 15,041 | ~3,000 | ~5,500 | ~20 sub-100-LOC `_active_speaker_*` payload builders (relocation recommended wholesale) |
| T16-3 web-3 | 15,069 | ~4,200 | ~5,000 of 10,900 | HTML f-string builders in `voice_setup`/`wake_setup`; `wifi_setup`'s nmcli parsers |
| T17-1 cli-1 | 8,979 | ~7,470 (23/24) | 1,508 | nothing (doctor + AEC are the sibling tile) |
| T17-2 cli-2 | 8,978 | ~4,000 | ~5,000 | library internals of the packages the CLIs wrap |
| T18 platform-rest | 9,309 | ~9,250 (57/59) | — | nothing (nothing over 1,500 LOC) |
| T19-1 rust-fanin-1 | 13,246 | ~2,900 | ~4,300 of 7,282 prod | 5,964 test LOC at signature level; sibling's half |
| T19-2 rust-fanin-2 | 13,239 | ~7,650 prod | 5,935 test | sibling's half |
| T20 rust-outputd | 17,751 | ~5,300 | ~4,700 | ~250 LOC of composite-recovery unit tests beyond names |
| T21 rust-shared | 14,811 | ~2,700 | ~7,800 | ~6,000 LOC of `#[cfg(test)]` bodies (names enumerated exhaustively) |
| T22 c-experiments | 10,550 | ~6,500 | 4,033 (`test_ring_core.c`) | nothing; did not build or run the C |
| T23 deploy-install | 9,206 | 9,206 (22/22) | — | nothing; `test_install_helpers.py` bodies sampled by grep |
| T24 deploy-units | 8,862 | ~7,400 (108/109) | 1,551 (`index.html`) | nothing; test bodies beyond cited assertions |
| T25-1 deploy-assets-1 | 15,509 | ~7,850 | ~2,650 of 7,655 | ~390 mid-size `sound-profile` function bodies (signatures read) |
| T25-2 deploy-assets-2 | 15,504 | ~10,960 (37 files) | ~1,640 of 4,543 | nothing; a font licence file |
| T26-1 scripts-1 | 13,792 | 13,850 (46/46) | — | nothing in tile |
| T26-2 scripts-2 | 13,791 | 13,791 (46/46) | — | nothing in tile |
| T27 repo-tooling | 1,680 | 1,680 (8/8) | — | nothing; +~1,650 lines of supporting files read |

**Totals.** ~**300k lines read fully** and ~**175k structurally** (module docstring, every
signature, every body > 40–100 LOC, plus every persistence / subprocess / thread / socket /
exception path), i.e. **~83 % of the 573k-line tile inventory touched at one of the two
altitudes**. **No tile skipped a file in its own list.** Every skip above is a *body* inside a
structurally-read file, and every one was declared.

Systematic residue, by kind:
- **Test bodies.** The ~585k-line `tests/` tree is not in the tile inventory. Tiles grepped it by
  module name for the tests lens; only ~15 test files were read in full anywhere. p0-tests
  analysed it mechanically (AST, no `pytest --collect-only` — the sandbox proxy 403s the pinned
  `camilladsp` GitHub archive), so every count there is a static lower bound.
- **Pure math interiors.** The brief permitted skimming; ~12 tiles used it. Control flow, bounds
  and refusal paths were read; coefficient correctness was not verified anywhere.
- **`docs/`.** 8 named ADRs spot-checked plus 6 more opened; **~145 of 157 ADRs not opened**, and
  no semantic duplicate-decision clustering was run.
- **Two large bash files** were grepped rather than read line by line:
  `deploy/bin/jasper-audio-hardware-reconcile` (2,197) and `deploy/bin/jasper-deploy-health` (900).
- **No runtime or hardware verification anywhere.** Every finding is static, except three
  executed checks: `redact_secrets` against real key shapes (twice, two tiles), the
  `quality_model` profile identity, and `python3 -X importtime` on the chip-AEC import chain.

**What Phase 0 measured mechanically over 100 % of the tree** (not sampled): LOC for all 2,569
tracked text files; `ast` defs/classes/function length/McCabe CC/nesting for every `.py` in
`jasper scripts tests deploy`; a string- and comment-aware brace pass over every `.rs`; the full
import graph (module-level **and** function-local, relative imports resolved) over 749 `jasper`
modules with Tarjan SCC; 4,304 public defs cross-referenced against a whole-repo token index then
AST-disambiguated; `git log --name-only` over all 1,140 commits for churn and staleness; 874 raw
`JASPER_*` and 45 `JTS_*` tokens classified through an AST env-call pass plus regex passes over
`.rs`, bash, systemd, udev, nginx, ALSA conf and Markdown; 281 distinct `/var/lib/jasper*`,
`/run/jasper*`, `/etc/jasper` path literals; every systemd `Exec*=` target, every nginx
`location`, every `[project.scripts]` entry, every `deploy/assets` file; 3,290 Python functions
≥8 lines through a normalized-AST exact + shingle-Jaccard duplicate scan (plus a bash variant
over 352 functions); `tokenize`-based comment/docstring ratios for all 788 `jasper/*.py`;
19,461 `def test_*` and their parametrize decorators; every markdown link and backtick path in
`docs/**` and the top-level docs (7,515 candidates narrowed to 260 and hand-verified).

Known method limits, stated by the phase that owned them: CC numbers are p0's own McCabe count,
not radon-calibrated (radon/lizard/vulture/jscpd/cargo-udeps could not be installed — no network
egress); the Rust `pub`-item scan is token-based and may over-report common method names; test
env injected as dict literals inflates the `knob-nobody-turns` class; dynamic env-key composition
(`f"JASPER_DEBUG_{sub.upper()}"`) means a few ledger tokens are only docstring spellings.

---

## (g) Proposed package restructurings, side by side

Five independent proposals overlap. They are compatible in intent and **conflict in three
specific places**, marked ⚠. The synthesis has to pick one plan per zone before any `git mv` lands.

**1. `jasper/` top level → 8 packages (T18 §4).** 115 modules, 0 left over. `platform/` (19),
`net/` (8), `identity/` (4), `wake/` (12), `audio/` (31), `volume/` (8), `sources/` (13),
`assistant/` (20). One mechanical rename PR per package, `git mv` + import rewrite only.
T18 notes `audio/` at 24k LOC still wants a later `output/` vs `capture/` split.

**2. `jasper/active_speaker/` root (107 flat files, 94,582 LOC) — four proposals.** All four tiles
independently concluded the root is a flat bag whose intended hierarchy is already in the
filenames, and `crossover_v2/` proves the subpackage shape works.

| T14-1 | T14-2 | T14-3 | T14-4 |
|---|---|---|---|
| `declaration/` | `driver/` | — | `declare/` |
| `graph/` | — | `compile/` | `emit/` |
| `commissioning/` | `staging/` | — | `commissioning/` |
| `measurement/` | `spec/` | `capture/` | — |
| `linearization/` | — | — | `fit/` |
| `runtime/` | — | — | `session/` |
| `web/` | `lab/` | `evidence/`, `safety/` | (`wizard_client` → `jasper/`) |

⚠ **Conflict A:** the same files land differently — T14-1 puts `driver_safety` in `declaration/`,
T14-2 in `driver/`, T14-3 in `safety/`. **Conflict B:** T14-3 proposes `safety/` and `evidence/`
as admission-rule packages ("`safety/` may not import `compile/` or `capture/`"), which is a
*layering* rule the other three do not state; it is also the one proposal that would actually
break the deferred-import mesh (R-074). **Conflict C:** T14-3 moves `crossover_envelope_v2.py`
(3,497 LOC of household copy) into `crossover_v2/envelope.py`; the others leave it at root.

Common ground worth banking first, regardless of plan: `_common.py` + `state_paths.py` stay the
root leaves and grow (`utc_now`, `resolve_state_path`); `bench/` stays; `wizard_client.py` and
`speech_stimulus.py` leave the package entirely.

**3. `crossover_v2/` — four systems in one flat 64-module directory (T13-1 A).** Engine
(doors-and-banks) / Conductor (old flow's helpers) / Offline instruments / Prescriptions. No cycle
between them and the layering is honest, but nothing in the hierarchy tells a newcomer which of
the four a file belongs to. T13-2 adds the harder point: **this cannot be settled by moving files
while two session engines run in one request** (R-099). Sequence W5-b first.

**4. `jasper/web/` (T16-1/-2/-3, #4031 Phase D).** All three tiles reached the same conclusion by
different routes: three of the largest "pages" are not pages. `correction_crossover_v2.py`
(7,165) → `active_speaker/crossover_v2/host/`; `correction_crossover_backend.py` (1,698) →
`active_speaker/crossover_level_lease.py`; `volume_floor_tone.py` (620) → `jasper/sound/`;
`correction_tuning.py` (335) → `jasper/calibration_agent/`; `sound_setup.py`'s 2,784 LOC of
`_active_speaker_*` → `active_speaker/web_commissioning.py`. ⚠ **The Phase D ledger has no row for
`correction_setup.py`** (5,127 LOC, six pages, 54 routes) and D.5's section-by-section move of
`correction_crossover_v2` leaves ~4,500 lines of engine behind — the plan is under-scoped on both.

**5. `rust/` → one workspace (T19-1 F6, T19-2 E, T21 C1).** Three tiles converged. It is the
precondition, not a parallel task: a shared `jasper-daemon` crate for the fanin↔outputd twins
(R-113…R-117) stops needing a seventh lockfile and a seventh `stage_rust_crate` line. T21 adds
that `jasper-host-clock` (3,804 LOC, one consumer, one `ObsMode` variant, a DLL it never ticks)
should probably fold into `jasper-fanin/src/host_clock.rs`, its only adapter — and that
`jasper-env` (60 code lines) is a workspace bug wearing a crate. **The blocker people assume does
not exist:** both daemons' `[profile.release]` are identical field-for-field and the six shared
crates declare none.

**Smaller relocations, no conflicts:** `route_latency/status_socket.py` → `jasper/` top level
(12+ consumers, wrong package); `correction/level_match.py` → `audio_measurement/`;
`audio_measurement/calibration.py`'s HTTP half → `jasper/correction/`;
`audio_measurement/delay_graph.py` → `active_speaker/`; `transport_coherence.py` → `jasper/fanin/`;
`dsp_numpy.py` out of the CamillaDSP tile; `audio_io.py` → `voice/`; `audio_input_view.py` →
`jasper/web/`; `experiments/usb-turntable/` → `tools/usb-turntable/` (rename only — the subprocess
boundary is the safety mechanism and must not be collapsed into an import).

---

## (h) Open owner decisions the tiles surfaced

1. **The v1 commissioning lane — repair or delete (R-007, R-102).** 21,667 LOC, 23 % of
   `active_speaker/`, whose apply step carries a comment saying it cannot succeed on real
   hardware (#2202), and whose replacement is already shipping beside it. ADR-0228 row 9 records
   the owner's "repair, not abandon" ruling **and** that both its premises no longer hold at HEAD
   (ADR-0188 parked the relay, ADR-0197 deleted the sibling); PR #3836 awaits re-ruling. Every
   other finding in that zone is a day's work; this one is the zone's shape.
2. **`bass_extension/`'s park boundary (R-179).** ADR-0018 parks the package and sets an explicit
   bar ("an orphan sweep or an audit finding is not sufficient authority"). But three modules
   (`bench/{stimulus,live_proof,excitation}.py`, 620 LOC) have zero importers *even from the dead
   executor chain* and are named by neither the ADR nor `bench/__init__.py`'s own map. Are they
   inside the park or accidental orphans?
3. **`#4085` owner call 1 — who owns commissioning-in-the-web (R-050, R-077).** Two tiles
   converged on the same answer (`web_commissioning.py`; `/sound/` keeps route glue), and a third
   file — `correction_crossover_backend.py` — poses the identical question. Settle both together.
4. **Whether `assertive` room-correction ships (R-193).** The only boost-capable preset is
   unreachable from any surface, two publishers grade it, and `camilla_stereo_prefix.py` reserves
   headroom for it. Open the surface or delete the preset.
5. **The dac-content FIFO retirement (R-202).** ADR-0220 states the removal condition explicitly:
   "the FIFO spelling, its park trigger and outputd's FIFO reader retire together after a bonded
   pair plays on metal". Did that run happen? ~430 LOC plus a `/state` surface waits on the answer.
6. **The four dormant wake legs (R-206).** Delete, or make them one declared lab knob? ~240
   occurrences across six production modules turn on this.
7. **The 30 AEC3/WebRTC tuning knobs and 10 UDP ports (R-212).** A real lab surface with a
   registry (`aec_sweep.py:115`) that is today indistinguishable from operator knobs. Declare them
   a self-parsed lab pack per `docs/extensibility.md`, or delete?
8. **`_test_lane.sh` / `test-fast` / `tests.yml` prose (R-238, T27 C10).** 68 % / 33 % / 26 %
   comment lines documenting genuinely non-obvious bash-signal and pytest-routing traps, with
   dates and incident narration AGENTS.md forbids. Two tiles judged the content load-bearing and
   the *form* non-compliant. ADR or keep?
9. **Two doctor rows that are security-posture regressions and cannot fail (R-258).** The
   device-to-device `/grouping/set` auth gate having "silently fail-safe-OPENED", and an
   unauthenticated root-backed CamillaDSP config editor being LAN-reachable, are both `warn`-only
   and so never move the exit code `install.sh:2173` reads. Promote to `fail`?
10. **PR #4127's direction (R-256).** It omits both crossover-v2 doctor checks on streambox; the
    actual defect is that one reader takes >15 s *and is called twice*, so the full speaker keeps
    paying it. Memoizing is the smaller diff that fixes both boxes. Push back or land?
11. **Five files that no tile owned.** `scripts/{test-fast,test-merge,use,jasper-pipe-probe,rust-ci-needed}`
    are in `scripts/` but in neither T26 tile list (verified by `comm`). T27 read `test-fast`,
    `test-merge` and `_test_lane.sh` as supporting files; `use`, `jasper-pipe-probe` and
    `rust-ci-needed` have had no reviewer at all.
