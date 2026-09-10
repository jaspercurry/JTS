# JTS Deep Audit — 2026-08-25

**Audited tree:** `9fcda9ee5339a8e7bbfc98a3f8229f01d5cf108f` (origin/main at audit start).
**Method:** the five-phase comb from `docs/DEEP-AUDIT-PLAYBOOK.md`, run end-to-end: Phase 0 cartography (7 discovery agents), Phase 1 tiled deep read (50 tiles + 1 re-run, every non-census file opened), Phase 2 cross-cutting lenses (9 agents), Phase 3 adversarial verification (11 skeptic agents over the 83 highest-stakes findings), Phase 4 synthesis + completeness critic. ~79 read-only subagents, ~28M tokens.
**Scope decision by the owner:** the tuning/measurement stack (`jasper/active_speaker/`, `jasper/audio_measurement/`, `jasper/correction/`, the crossover_v2/correction web+CLI surfaces, and their tests) was **census-only** — counted and structurally profiled, not deep-reviewed — because another agent is actively working it. Everything else, including the repo's own doctrine (AGENTS.md, the process machinery), was fair game.

> **This is a frozen snapshot.** Findings carry stable IDs (`tile#n` / `lens#n`). Current disposition (open/fixed/deferred) belongs in a separate ledger, not in edits to this file.

---

## 1. Executive summary

JTS is **two different codebases wearing one repo**. At the seam level it is genuinely well-engineered: registries, single-writer env discipline, atomic IO, socket-activated wizards, incident-pinned tests, working doc-freshness tooling. The adversarial verification pass repeatedly *upheld* machinery the first-pass audit wanted to cut — the resilience layers mostly defend real, dated incidents, and the process guards mostly hold. **The problem is not that the machinery is bad. It is that there is a commercial product team's worth of it wrapped around a hobbyist speaker, and it is narrated in place.**

The three structural facts that dominate everything else:

1. **The tuning stack is the repo.** 57% of `jasper/` (~261K of 455K lines including its web/CLI surface) plus 42% of the test suite (244K lines) belongs to the measurement/crossover/correction program. No plan that ignores it can meaningfully shrink the repo.
2. **Prose is the single largest cuttable mass.** `jasper/` carries 135,886 comment/docstring lines against 274,456 code lines (0.50 ratio; 0.61 inside the tuning stack). Add 65 HANDOFF docs (73.5K lines), AGENTS.md (3,534 lines, ~1,780 of which restate the HANDOFFs it links), and test files whose docstrings narrate incident history at essay length. Much of this prose is *load-bearing constraint documentation* — but a verified large fraction is PR archaeology, superseded-value changelogs, and per-ratchet diaries.
3. **The suite tests more than the product ships.** 617K lines of tests against ~490K of product code. Verifiers found the tests overwhelmingly *honest* (evidence-grounded, mostly non-tautological) — the bloat is altitude (the same behavior pinned at unit + endpoint + conductor level), 11,084 literal-substring assertions welding tests to source text, and meta-tests defending process rather than behavior.

**Can it be half the size with zero feature loss?** Not from the deep-read zone alone. The evidence-backed, verified cut list outside the census zone totals **~45–60K lines**. Adding the same discipline inside the tuning stack (prose + test-altitude trims only, no behavior changes — verified estimates ~30K prose + ~25K test docstrings/fixtures, plus its ~2K verified orphan) reaches **~110–140K lines (~9–12% of the tree)** with essentially zero risk. Halving the repo requires *product decisions*, not cleanup: shrinking the tuning program's shipped surface, moving research tooling out of the product tree, and choosing one transport where a migration currently ships both. Those are listed in §6 as owner decisions, priced separately.

**Grades** (COAH bar, hobbyist-calibrated; static analysis only — see §7 for what only hardware can prove):

| Attribute | Grade | Confidence | One-line justification |
|---|---|---|---|
| Hardware/audio safety | **A−** | high | 0 dB ceiling, brick guards, park-not-guess policies all present and tested; the one blocker is a *doc* that can truncate a config file. |
| Observable | **A−** | high | `event=` logs, `/state`, doctor are pervasive and disciplined; flight recorder pins DEBUG globally (nit). |
| Available / resilient | **A** (quality) | medium | Supervisors/reconcilers defend real dated incidents; verification refuted most "cut this" claims. Quantity is a proportionality issue, not a correctness one. |
| Clean — separation & SSOT | **C+** | high | Great seams, then bypassed: 96 direct `systemctl` sites beside the broker; 19 hand-rolled wizard `main()`s with real behavior drift; five live severity vocabularies; god files (WakeLoop 4,770 lines / 94 methods; coupling_reconcile 5,614; sound_setup 6,240). |
| Right-sized for a hobbyist product | **C−** | high | 1.42M tracked lines for a speaker; doctor alone is 41.7K lines (90 of 169 checks cannot fail); a 2.5K-line Rust state machine exists to deliver ~10 ms of latency 2.5 minutes earlier. |
| Tests | **B−** | high | Honest and comprehensive; oversized in altitude and welded to literals; 962 redundant asyncio markers; meta-test recursion. |
| Docs | **C+** | high | Governance tooling genuinely works (9 stale of 173); corpus is oversized, AGENTS.md violates its own single-source rule, README's repo map omits 9 top-level dirs, and BRINGUP's AEC section is dangerously wrong. |
| Newcomer followability (the owner's stated goal) | **C−** | high | The audio/voice pipelines are real and singular, but reading them requires threading 43%-function-local imports, 20 stacked env directives on the busiest daemon, and prose archaeology. |

**Overall: B− engineering quality, C− proportionality.** The repo does not need rescue; it needs an editor.

---

## 2. Coverage ledger

Stated precisely, per the completeness critic's independent re-measurement (its full report is preserved with the audit artifacts):

- 2,411 tracked files at the audited SHA; the tile plan assigned **100%** of them (no untracked files, no symlinks, no off-plan file classes — `.github/` and binary assets included). **89%** of files are cited somewhere in the audit artifacts; **65%** are named in an agent's explicit `files_read` list; 31 files are never named anywhere (all census-zone or lockfiles). "The audit read the deep-read zone" is accurate; "the audit read the codebase" would not be.
- Census zone (owner decision, not deep-reviewed): `jasper/active_speaker/`, `jasper/audio_measurement/`, `jasper/correction/`, the correction/crossover_v2 web pages, **their `jasper/cli/` front-ends** (`active_speaker*.py`, `seat_level.py`, `correction_bundle.py`, `measurement_mic.py`, etc.), and tuning-stack tests. **One honest overshoot:** the census *test* filter (a name regex) also swept in roughly a dozen tests that pin core seams — `test_ring_active_endpoint.py`, `test_camilla_crossover_unit.py`, `test_camilla_crossover_guard_script.py`, `test_aec_commission.py`, `test_aec_sweep.py`, `test_commission_tone_single_owner.py` among them. Several high-severity flow findings concern exactly those Ring/CamillaDSP/AEC contracts, and those test files were never opened. This is the audit's one rationalized-past corner; a follow-up pass should read them before acting on §4's transport/duck items.
- One thin tile (`jasper-control-1`, 1 of 28 files read) was detected by assigned-vs-read reconciliation and fully re-run. Three tiles (`jasper-audio-sub-0`, `jasper-tools-0`, `tests-main-5`) returned findings but no `earns_keep` list — their pass judgments are present in prose only.
- Docs: 217 files censused; 12 load-bearing claims verified against code (10 true, 1 confirmed drift, 1 meta-gap) — a 5.5% sample, not a per-file read.
- Phase 3 adversarially verified the **83 highest-stakes findings** (~19% of the ~432 raised): **20 confirmed, 2 upgraded, 48 downgraded, 13 refuted**. Every first-pass "blocker" was re-adjudicated; exactly one survived. Coverage was not even: the `config-sprawl` and `runtime-perf` lenses received zero verification — and the one count the critic found unreproducible ("19 env layers"; actual: 17 `EnvironmentFile=` + 3 inline `Environment=` directives) sits in `config-sprawl`. Counts from those two lenses carry an implicit ±.
- The critic independently re-ran 11 headline counts never verified in Phase 3: **8 reproduced exactly** (96 systemctl sites / 39 files; 20 wizard `main()`s; 962 asyncio markers; 53/32/17 Apple-dongle identifiers; 42/26/26/36 guard sites; 19×11 unit-name literals; 43/17/26/2 systemd census; 1,236-line / 99-block nginx pair), 2 were overstated in the audit's favor (`status_socket.py` has 5 importers, not 3; one stale test-file denominator: 896, not 886), 1 was a historical figure reused as current (the unrun-JS-harness family has since grown to 14 files / 4,203 lines). Its judgment: *"Nothing here suggests the audit inflated its findings… the failure mode is the opposite and milder."*
- Unverified findings beyond the 83 are nits and small should-fixes; their LOC estimates are **not** counted in any total in this report.
- Known limits: git history in the audit clone was truncated (50 commits), so file-age/staleness analysis was unavailable; runtime behavior, audio quality, and long-uptime resilience are out of scope (§7).

---

## 3. The one blocker, and the near-misses

### BLOCKER — BRINGUP.md Phase 5 documents an unreachable outcome and a config-destroying command
`doctrine-root-0#0` — **confirmed, upgraded in severity by verification.**
BRINGUP.md's "bring AEC online" tells the operator to expect `active=xvf_software_aec3` on the project's own recommended XVF3800 hardware — an outcome `jasper/audio_profile_state.py` makes unreachable (managed XVF resolves to chip-AEC-or-park; `requested_profile` is hard-set). BRINGUP never mentions `jasper-aec-commission`, which README says the install "must first pass," so an operator following the bringup doc lands in "Chip-AEC parked" with no path out. Worse: the "Optional: Software AEC bridge" section's `printf 'JASPER_AEC_MODE=auto\n' | sudo tee /var/lib/jasper/aec_mode.env` **truncates the file that also carries `JASPER_AUDIO_INPUT_PROFILE` and the `JASPER_WAKE_LEG_*` keys** — following the doc destroys the operator's profile and wake-leg config.
**Fix (~40 doc lines):** rewrite Phase 5 to the profile-first model (`jasper-aec-reconcile` → `sudo jasper-aec-commission` → expect `xvf_chip_aec` or a named park reason), replace both truncating `tee` recipes with non-destructive edits or the `/wake/` page, keep `JASPER_AEC_MODE` only as a labelled rollback note.

### Near-misses (verified should-fix, do these in the first wave)
- **`install.sh` ships a broken nginx config as a green deploy** (`flows-web-deploy#4`, confirmed). `install_nginx_site` prints a WARNING and falls through when `nginx -t` fails — no `return 1` — so `write_build_manifest` still stamps `status=ok` and deploy verification passes against the *old* config. The streambox twin does it correctly. **Fix: one line.**
- **Three wizard services run as root** (inverse finding surfaced by the verifier of `architecture#0`). `jasper-correction-web.service`, `-bluetooth-web.service`, `-system-web.service` have no `User=`, no `CapabilityBoundingSet=`, no `SystemCallFilter=` — while `jasper-web.service` runs hardened as `jasper-web`. **Fix: add the same hardening block to the three units.**
- **`/wake-corpus/` lacks the shared host/Origin guard axis** (`jasper-web-1#0`, downgraded from blocker). It is a test-pinned, sanctioned exception with a sound server-held token and no live exploit path — but it is the only mutating wizard without `guard_mutating_host`. **Fix: two lines** (call `guard_mutating_host` first inside `_check_csrf`), not the rewrite the first pass proposed.
- **A phantom lock knob in the DSP-apply path** (`jasper-toplevel-1#1`, confirmed). `apply_dsp_config(acquire_lock=...)` is accepted, forwarded, and never used; callers pass computed values as if it mattered (`jasper/sound/runtime.py` passes `not writer_lock_held`). Exactly the misreading that produces a concurrent-writer bug someday. **Fix: delete the parameter through 13 signatures (~58 lines).**
- **Cross-process duck coordination by magic number** (`flows-audio-voice#1`, downgraded but real). `GRAPH_SWAP_DUCK_DB = 40.0` exists so the 1 Hz volume reconciler will "read it as somebody's duck and leave it alone." Replace the carve-out with an explicit duck lease `{owner, depth_db, expires_at}` (~200 lines net).

---

## 4. Verified findings by theme

Each item below survived adversarial re-reading; LOC figures are the *verifier's* independent estimates, not the first-pass claims. IDs refer to the audit's finding registry.

### 4.1 Dead code — verified deletions, zero feature loss (~8.4K lines incl. tests)

| What | Verified negative | Lines |
|---|---|---|
| `jasper/bass_extension/` parked half: `limiter_evidence.py`, `bench/executor.py` (+ their test files); `__init__` apply-pathway optionally parked with them | Only reachable from tests; the one CLI that could wire it in unconditionally raises `SystemExit` naming issue #1738; deadness is *structurally enforced by its own tests* | ~4,600 (+715 optional) `jasper-periphery-0#0/#1/#2/#3` |
| `deploy/assets/shared/js/orbs.js` + `tests/test_web_orbs_module.py` + `tests/js/orbs_test.mjs` + its CI step + `app.css` orb tokens | No page imports it — its entire consumer set is its own tests, which run in CI | ~2,075 `deploy-assets-0#0` (upgraded) |
| `jasper/multiroom/channel_split.py` + the always-`None` `channel_split` parameter through `sound/camilla_yaml.py` | Bonded members get their channel via `outputd_grouping_env`; the split path is never non-None in production | ~900 `jasper-rooms-1#0` |
| Five dead wizard `main()`s + 2 orphan console_scripts (`airplay`, `sources`, `speaker`, `weather`, `tools` setup pages) | All 15 wizards run via `jasper-web.socket` → `python -m jasper.web`; the per-wizard units were deliberately retired by the installer itself | ~177 `jasper-web-2#0` |
| `TtsPlayout`'s PortAudio body (base class of the only real playout) | `Config.validate` and `make_tts_playout` both raise on the only path that could reach it | ~230 `jasper-toplevel-1#0` |
| `wake_setup.py` dead `"aec"`/`"chip_aec"` layer branches | The JS layer vocabulary no longer contains those tokens; nginx `no-cache` policy makes the stale-bundle defense void | ~85 `jasper-web-1#4` (upgraded) |
| `jasper/bass_alignment.py` + test | Test-only importers; the live sibling is `active_speaker/alignment_walk.py` | ~120 `jasper-toplevel-4#0` |
| `experiments/aec3-v2-deep-tune-spike/` code files (keep README — 3 live citations) | Output already shipped into `jasper_aec3`; no runtime/CI role | ~614 `edge-dirs-0#1` |
| `jasper/bass_extension/bench/excitation.py` | Zero references of any kind, including tests | ~85 `jasper-periphery-2#0` |
| Small verified: `control/__main__.py` (9), `chip_aec_policy` 3 dead emitted fields + `..._DAC_GATE_ACTION` end-to-end (~15), `CaptureActivityProbe` (52), dead voice-protocol members (`last_chunk_played_at`, `supports_provider_vad`, `interrupted()`, `AudioOutChunk.kind`, `_create_response_only`, `DEFAULT_TEMPERATURE`) (~115) | Each proven no-caller incl. dynamic dispatch | ~190 |

> **Correction (2026-08-25, #2970):** two rows above were refuted on
> re-execution of their negative proofs — `bass_alignment.py` (consumed by
> tuning-zone contract tests) and `AudioOutChunk.kind` (live: `turn_playback`
> → `segment_kind` → fanin `SegmentKind` AEC-reference accounting). Do not
> delete either; see merged
> [PR #2970](https://github.com/jaspercurry/JTS/pull/2970).

Also verified dead but leave-in-place unless already editing: `wake_fusion.py`'s inert threshold-offset seam (74 lines, has a documented future consumer). The `rust/jasper-host-clock` `ObsMode::Fill` machinery listed here (`rust-misc-0#0`) has since been deleted.

### 4.2 Over-engineering — verified right-sizing (~9–12K non-census lines)

- **`rust/jasper-fanin` host-compliance/prime machinery** (`rust-jasper-fanin-0#0` + `rust-jasper-fanin-1#0`, both confirmed): a persisted, schema-versioned, two-strike cross-session proof system — ~1,500 production + ~1,400 test lines — whose entire payoff, per the code's own comment, is that ~10 ms of standing resampler cushion decays away 2.5 minutes earlier on repeat USB sessions. The descent reaches the same floor unaided. **Delete** (`host_compliance.rs`, `HostComplianceState` + `service_host_compliance` in `mixer.rs`, prime-aware branches in `lane_resampler.rs::decay`).
- **Doctor never-fail checks** (`safety-systems#0`, downgraded from a 25K claim to a real ~1.2K): 90 of 169 registered checks contain no fail branch — a scrolling report, not a gate. Consolidate the pure install-settings-drift family into one check; keep the rest (the verifier confirmed exit semantics and per-check value elsewhere).
- **CI classifier machinery** (`test-value#1`): 545-line classifier + 1,087-line classifier tests to pick between two narrow lanes and "full." Collapse to a ~120-line fail-closed path predicate + ~150 lines of tests (~1,600 net).
- **`env-migrations.sh` aged relocations** (`safety-systems#3` + `flows-web-deploy#2`): retire the ~11 pure one-shot relocation loops (~690 lines) once the owner confirms every live Pi has deployed since they ran; **keep** the seed blocks and permanent permission reassertions the verifier identified as live.
- **nginx conf twins** (`flows-web-deploy#1`, confirmed): 243 copies of the same 4–5 `proxy_set_header` lines across two files that differ by 9 meaningful lines. Snippet + merge (~500).
- **`test_lint_contracts.py`** (`test-value#0` / `tests-main-2#0`, confirmed twice): 2,159 lines, 88% comment — a per-PR ratchet diary for eight integers, including a line-count "ceiling" for `crossover_v2_flow.py` that has been narrated *upward* from ~3.5K to 13,459 lines without ever forcing the split it was created to force. Keep all 8 tests and constants; strip the changelog (~1,700).
- **Wake-event telemetry defaults** (`flows-audio-voice#4`): the hot-path design is fine (verifier refuted the "research DB on the hot path" framing) but the **1 GiB default WAV cap on the product SD card** is the real cost — drop to ~128 MiB or default capture off; move `jasper/wake_training/` + offline analysis out of the shipped package (~1,500).
- **AGENTS.md** (`doctrine-root-0#2`, confirmed — "strongest-evidenced finding in the group"): ~1,780 of 3,534 lines are per-subsystem tutorials restating the HANDOFF each section links, in direct violation of the file's own rules ("don't expect this file to restate README"; "each concept lives in exactly one file"). Hold each section to its stated charter: the gotcha, the ownership line, the single-writer name, the HANDOFF link (~1,050 cut).

### 4.3 Duplication — build-the-helper-then-bypass-it (~4–6K, mostly mechanical)

The repo's characteristic defect (verified across three lenses): a good shared seam exists, and siblings hand-roll beside it.

- **96 direct `["systemctl", ...]` call sites across 39 files** beside `restart_broker.py`, which bills itself "the single mediated systemctl boundary" — inside `jasper/control/` itself, 5 files bypass it while 3 call sites use it (`jasper-control-1-rerun#1`). Either route in-process privileged calls through the broker (mechanical — jasper-control is already an authorized client) or correct the docstring's SSOT claim.
- **`tests/js`: 12 hand-rolled ES-module loaders, 12 `element()` DOM stubs, 25 `globalThis.document` stubs** (`duplication#0`, confirmed). One `_loader.mjs` + `_dom.mjs` (~1,200 across the family).
- **4 genuine hand-rolled atomic writers** beside `atomic_io.py` (verifier corrected the claimed "20+" to 4: `output_hardware.write_state`, `assistant_loudness`, `audio_quality` ×2, `wifi_guardian_persistence`) — point them at the helper; skip the proposed read-side framework (`duplication#1`).
- **CSRF/host-guard pinned at three altitudes**, ~103 near-identical rejection tests across 32 files (`test-value#2`, corrected counts): extend the existing `test_web_wizard_conventions.py` sweep and delete per-wizard duplicates (~1,000).
- **`sound_setup.py`'s 31 identical 502-error blocks** — one file's habit, one small helper (`architecture#1`).
- **962 redundant `@pytest.mark.asyncio` markers** with `asyncio_mode="auto"` already set — mechanical deletion.
- **Five live severity vocabularies** for "how bad is this" and the `PHASE_CHECK` / `PROGRAM_PHASE_CHECK` collision pinned *disjoint* by a test instead of merged (`duplication` lens): consolidation candidates when touched.
- **Voice sessions**: `gemini_session.py` (1,718) and `openai_session.py` (2,393) re-implement the same supervisor machinery while `grok_session.py` proves the 122-line-subclass route works. *Note:* deleting Gemini was **refuted** (it is the headline, 12×-cheaper provider) — the opportunity is refactoring the shared supervisor, not dropping a provider.

### 4.4 Prose — the discipline, not the sweep

Verification **rejected** the blanket "strip PR archaeology" sweep (`prose-density#4` refuted: dated citations are the owner's documented house style, and most dense prose is genuine WHY-constraint documentation). What survives, category-scoped and by hand:

- Superseded-value changelogs in code ("Earlier values: …") and incident narratives for bugs the constant does not fix — verified example set in `voice_daemon.py`/`camilla.py` (~600–2,400 depending on appetite).
- Self-labeled archaeology blocks ("SUPERSEDED — kept for archaeology") — delete opportunistically.
- Test-docstring essays: selective trim where the docstring restates a module docstring; verified realistic yield ~3,000 on the worst 44-file tile alone (`tests-main-5#0`).
- The ~10 independently-maintained epitaphs for the removed `rate_match`/`transport_pipe` transports → one place (~120).
- README: add the 9 missing top-level dirs to the repo-layout tree; the 766-line doc bibliography → collapse historical entries (~600).

### 4.5 The census zone (handoff to the tuning agent — not audited in depth, structurally profiled)

- 57% of `jasper/`; `crossover_v2_flow.py` at 13,459 lines is the largest file in the repo; tests 244K lines (2.05× its source; crossover_v2 tests 2.5×); prose ratio 0.61 (68.9K prose lines — nearly as much prose as the entire rest of `jasper/` on half the files).
- Verified orphan inside it: `commissioning_capture_producer.py` (1,235 + 854 test) — zero non-test importers, while `docs/HANDOFF-audio-measurement-core.md` still describes it as live (doc drift compounding dead code).
- The verifier's guidance on the "just delete the tuning stack's journeys" idea: **refuted** — the flows are live and cross-wired; the zero-feature-loss cuts here are prose (~30K) and test docstrings/fixture-dedup (~25K), plus the orphan.
- The line-count ratchet diary in `test_lint_contracts.py` chronicles this zone's god-file growth; the split it kept deferring is this stack's job to finally take.

### 4.6 Refuted — do NOT cut these (verification protected you here)

Each of these was a first-pass "cut" claim that failed adversarial re-reading; listed so the fix plan doesn't resurrect them:

- **Merging the 21 web listeners into one process** — the separation carries real privilege boundaries (root vs `jasper-web`) and per-panel memory ceilings; the *actual* gap is the three root-running units (§3).
- **Deleting/merging `jasper-outputd`** — 43% of its lines are tests; the AEC reference invariant ("reference must equal final DAC content, TTS-inclusive") requires the post-DSP tap; moving it upstream would subtract the wrong signal.
- **Dropping the Gemini adapter** — headline provider, ~12× cheaper per minute than OpenAI, wired through wizard/doctor/cues/install.
- **Merging `jasper-aec-bridge` into jasper-voice** — the process boundary carries root/RT scheduling vs unprivileged daemon separation, each mechanism tied to a dated incident.
- **Deleting the loopback transport** ("two live transports" `flows-audio-voice#0`) — loopback is the shipped default for a live box class; the ring migration is an *active tracked campaign* (`docs/HANDOFF-audio-graph-consolidation.md`) with its own retirement step (P9-E). Finish the campaign on its schedule; don't pre-empt it.
- **Collapsing the three health systems** — `jasper-deploy-health` exists precisely for the case where the venv (and thus jasper-doctor) is broken.
- **Removing `StartLimitAction=reboot`** — 5 units, not 8; nuanced history; a docs correction, not a code cut.
- **Swapping the WiFi netlink repair for `nmcli radio` bounces** — documented-and-rejected regression.
- **One merged settings file** — the secrets compartments and single-writer-per-file discipline are load-bearing.
- **Splitting `WakeLoop` / `coupling_reconcile.py` as a project** — both are genuinely god-sized, but the verifiers showed the proposed extractions either already exist as imports or would break single-writer/lock invariants. Take only the cheap mechanical slices (pure-function statics; the CLI/flock entrypoint) *when already editing*, and stop growing them.
- **A central registry for the 179 state/marker paths** — cross-language agreement is already pinned by targeted tests, which a Python registry structurally cannot reach.

---

## 5. What's genuinely strong (earned, not flattery)

- **The audio data plane really is one pipeline** (renderers → fanin → CamillaDSP → outputd → DAC), and the voice plane really is one loop with pluggable providers behind a held Protocol seam — the "one system per concern" goal is *architecturally achieved*; it's the control planes and the prose around them that sprawl.
- **Safety engineering is real**: the 0 dB ceiling is enforced at four layers (single-sourcing it is the remaining nit); XVF brick hazards are documented and coded around; chip-or-park never guesses.
- **The test suite is honest.** Verifiers repeatedly found tests pinning real, dated incidents with hand-derived expected values, subprocess tests delivering real signals because in-process fakes would miss the bug, and cross-language contract tests (`test_renderer_ring_lanes.py` checks Python, Rust, ALSA conf, systemd and installers agree on one fact).
- **Verification-friendly culture**: almost every count the audit's first pass claimed reproduced exactly on re-measurement; the repo's own docs admit its failure modes candidly (six severity sets, the extensibility gap) — rare and valuable.
- **Right-sized exemplars exist in-tree to copy**: `tests/test_atomic_io_conventions.py` (96 lines, one rule, two-sided ratchet), `jasper/control/measurement_hold.py`, `tests/test_calibration_agent_model_client.py`, `handlers/measurement.py`.

---

## 6. The fix plan

Sequenced for payoff-per-risk; every wave keeps CI green and features identical. LOC are verified estimates.

**Wave 0 — correctness & safety (1 day, ~5 small PRs).**
BRINGUP Phase 5 rewrite (blocker); `install_nginx_site` `return 1`; `User=`+hardening for the three root wizard units; `/wake-corpus/` host-guard two-liner; delete the `acquire_lock` phantom knob.

**Wave 1 — verified dead code (~8.4K lines, mechanical, one PR per row of §4.1).**
Each PR body pastes the verifier's negative-proof. The bass_extension cluster is one park-or-delete decision under issue #1738 — decide once, apply to all four pieces.

**Wave 2 — over-engineering removals (~9–12K).**
host_compliance/prime machinery; orbs.js bundle; CI classifier shrink; env-migrations retirement (after fleet confirmation); nginx snippet+merge; doctor never-fail consolidation; wake-events cap default; wake_training out of the shipped package.

**Wave 3 — test right-sizing (~15–20K).**
`test_lint_contracts.py` strip; doctor tests → one file per domain asserting verdict + stable remediation *codes* (keep the aec_probe isolation tests verbatim); CSRF altitude collapse via the conventions sweep; `tests/js` `_loader.mjs`/`_dom.mjs`; 962 asyncio markers; delete the dated staleness-sweep and pure-prose-pinning tests; convert the two worst literal-welded wiring test files to executing their subjects.

**Wave 4 — prose discipline (~10–20K non-census, category-scoped, by hand).**
AGENTS.md restructure to its own charter (~1,050); README layout + atlas; superseded-value changelogs and self-labeled archaeology in code; epitaph consolidation; test-docstring trims on the worst tiles. Adopt the one-line-reason convention for new lazy imports.

**Wave 5 — structural convergence (opportunistic, when a file is already open).**
Duck lease replacing the magic-number carve-out; systemctl-through-broker (or an honest docstring); `sound_setup` 502-helper; `git mv` `subway/citibike/bus.py` into `transit/providers/` (kills a documented import-cycle hazard); fold `jasper/measurement/` into its one consumer; `test_control_server.py` split along the handler boundary its own meta-test enforces; the four hand-rolled atomic writers.

**Owner decisions (priced, not scheduled):**
1. **Issue #1738** — wire the bass-extension bench or delete the parked half (±5.3K incl. tests).
2. **Tuning-stack participation** — apply Waves 3–4 discipline inside the census zone (~55K available; coordinate with the other agent; the `commissioning_capture_producer` orphan + its doc drift is theirs to take).
3. **Phone-mic capture relay proportionality** — ~27K lines of E2E-encrypted relay infrastructure for one calibration feature; every piece individually earns its keep, but it is the largest optional subsystem in the repo. Keep, or accept a simpler capture path and reclaim most of it.
4. **`experiments/usb-turntable`** — real production dependency living under `experiments/`, deployed to the Pi, vendored code, 2.3× test ratio: either promote it into `jasper/` with normal rules or accept the anomaly consciously.
5. **Process weight** — the adversarial-gate-on-every-PR and multi-agent doctrine in AGENTS.md is the most expensive standing rule in the repo. This audit is evidence it catches real over-reach (16% of its own findings were refuted); whether that price fits a hobbyist cadence is a values call only the owner can make.

**Standing rule to stop the regrowth** (the cheapest fix in this report): new code gets prose only for constraints; incident narrative goes to the HANDOFF/git history; every new helper's introduction PR migrates at least its own siblings. The ratchet diary showed what happens otherwise — a guard that documents debt instead of stopping it.

---

## 7. What only hardware/runtime can prove

Static analysis cannot verify: actual AEC convergence and wake rates; audio artifacts across transport migration states; supervisor behavior under real starvation; the deploy pipeline against a live Pi (including the interactive-sudo path where four guards silently skip — `flows-web-deploy#3`, and whether a broken nginx conf really survives a deploy); nginx/socket behavior under the 2,693-second reconcile budget; memory headroom on 1 GB boxes with the full daemon set; every RAM/latency/CPU figure quoted from the `runtime-perf` lens; whether any of the 90 never-fail doctor checks would in fact fail on real broken hardware; which tests actually execute in each CI lane; the `deploy/provenance.toml` SHA-256 pins (praised for intent, never fetched and checked — the audit's one supply-chain claim rests on reading the pinning file, not validating it); and the dated incidents (two false reboots, the 2026-05-23 WiFi wedge) whose sole evidence is repo prose. Wave 0's BRINGUP rewrite should be validated by a real bring-up on the recommended mic.

---

*Audit artifacts (tile reads, lens reports, verification verdicts, coverage manifests) are preserved off-repo in the session workspace; finding IDs in this report key into them.*
