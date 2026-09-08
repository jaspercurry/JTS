# Brief: right-size the JTS tuning zone (cleanup agent)

You are the cleanup agent for the tuning zone of the JTS repo (`jaspercurry/JTS`).
Your job is to make the speaker-tuning package smaller, flatter, and single-owner
per concern **with zero behavior change**, following the owner's standing
right-size program. A separate agent owns the room-correction / Bank-style
architecture work and the bass program; a third agent owns tuning-flow features.
You do not touch their concerns (see "Out of scope").

## 0. Read first, in this order

1. `AGENTS.md` at HEAD. Its non-negotiables and defaults bind every PR.
2. `git fetch origin claude/tuning-rightsize/recon-reports` (never merge it) and read
   from that branch: `tuning-rightsize-recon/PLAN.md` (plan of record, §5 rows,
   §6 owner decisions, §9 status), `PLAN-FRESH-EYES.md` (the current ordering),
   `TARGET.md` (the toolbox shape at HEAD), `AUDIT-FRESH-EYES.md`, `EXEC-W3.md`
   (sub-agent mechanics; reuse it), `HANDOFF-W4.md` §3 (mechanics that cost time),
   `web-twin-map.md` (row 2.4's ten steps).
3. ADRs: 0018 (bass extension stays parked), 0199 (no handoff-doc tier), 0229
   (bass plan docs exempt), 0231 §5 (room correction and speaker tuning are two
   products; shared math lives only in `jasper/audio_measurement/`), 0237
   (stdout is the answer; one exit vocabulary).
4. Open PRs whose branch starts with `claude/tuning-rightsize/`. PR #4138
   (`w9-wired-kernel`) is open: do not touch its files until it lands.

Every `file:line` in this brief was read at main `27c892b3f` (2026-09-08). The
tree wins: verify each premise with grep at your HEAD before acting, and if a
premise is false, stop that row and report it rather than forcing the change.

## 1. What the evidence says the bloat is (and is not)

Two audits were run on 2026-09-08. Their findings set your priorities:

- **Duplication is small.** Only ~425 lines are genuinely duplicated (rows A1–A5,
  B1 below). Claims of "two PEQ fitters", "three spatial combiners", "two bundle
  schemas", "many apply transactions" were checked and refuted: `design_peq` is
  defined once with two front-ends; gate floors are declared once in
  `audio_measurement/gating.py`; the speaker layer has one apply transaction
  (`apply_baseline_profile`); `audio_measurement/bundles.py` is the shared
  manifest primitive both products correctly build on. Do not chase these.
- **Ceremony is real, and mostly off the LLM's path.** On the two hot paths
  (measure one pose via the web route; apply a candidate) about 40% of executed
  lines are bookkeeping or prose, but SAFETY and INTEGRITY hops there are real and
  tied to non-negotiables. The larger mass never executes for the LLM operator:
  the `commissioning_*` program (~15k lines, reached only from the v1 lane),
  wizard screen/refusal copy (~4.5k), the evidence packet (3.1k), write-only
  records, 21 round views of which five render the same curves, and 176k test
  lines against 137k product lines.
- **`jasper-measure` is the proof of size.** It reaches the same `TuningSession`,
  excitation-safety plan, admission gates and bundle in ~1,000 lines that the web
  route wraps in `prepare_v2_session` (926 lines), `CrossoverV2Session.__init__`
  (391), `build_conductor_state` (562), 15 bound seams and a 283-line findings
  readback whose only reader is its writer.

## 2. Rules that do not bend

- **Non-negotiables** (AGENTS.md list): `devices.volume_limit` 0.0 and the
  positive-volume clamp; commissioning SPL stop; driver protection bands / HPF /
  limiter; excitation caps; no XVF3800 `SAVE_CONFIGURATION`; secrets; deploy
  path; no silent deafness. A diff touching `camilla_yaml.py` emitters,
  `driver_safety.py`, `driver_protection.py`, `excitation_safety_plan.py`,
  `program_admission.py`, `dsp_apply.py`, `set_volume_db`, or the excitation
  ledger is **NN tier**: `/adversarial-review` plus an owner hardware pass, and
  the PR stays open until the owner confirms the pass.
- **Zero behavior change.** Fingerprint values, banked artifact formats that
  have readers, refusal codes, exit codes, and CamillaDSP output are byte-stable
  unless a row says otherwise. Do not converge the lossy fingerprint family
  (`baseline_profile.py:226`, `measurement.py:111`, `crossover_preview.py:88,116`,
  `capture_geometry.py:420`) — it changes persisted values; leave it and note it.
- **One concern per PR**, branch `claude/tuning-rightsize/w10-<slug>` from a fresh
  `origin/main`, target < 400 changed lines except pure deletions. A rewrite PR
  deletes the old unit in the same PR; never two copies alive across a merge.
- **Deletions need a verdict** in the PR body: SPENT / SUPERSEDED / PROMOTE, with
  the grep that proves no caller (registries, `pyproject.toml` entry points,
  systemd `ExecStart`, `deploy/bin`, udev, CI, `importlib`/`getattr`, the
  generated tool menu). "No importer" alone is not enough: the LLM invokes CLIs
  from the shell.
- **Relocations** carry AST identity against the merge-base (`astsame.py` on the
  recon branch) and resolve every relative and deferred import with
  `importlib.util.resolve_name` + `find_spec` before push. Two-pass
  `/code-review` on every relocation (deferred imports killed measurement
  windows once before).
- **Tests:** pin behavior, never source text or prose. One parametrized family
  per behavior. Delete a test when its subject is deleted. Keep the
  non-negotiable files heavy.
- **Prose:** the prose program on `crossover_v2/` is closed. Trim prose only
  inside a PR already touching the file; every prose change gets a constants
  review (compression falsified one sentence per accepted prose PR last wave).
- **Process per row:** verify premise → implement → `scripts/test-fast` plus the
  module's own tests (`scripts/test-merge` for import-structure changes; trust
  only the final `==> <lane>: N passed` sentinel) → `/simplify` → `/code-review`
  medium → fix what is real → `git fetch origin` → push `-u` → open the PR with
  line delta, verdicts and proof in the body. Never `git stash`; commit explicit
  paths, never `git add -A`; remove worktrees when done; no model identifiers in
  commits or PR text.
- **No new machinery:** no new `JASPER_*` knobs, no new doc tier, no handoff
  documents, no database, no memory service. Decisions go to `docs/adr/`.
- Model convention from the plan of record: Opus for code and relocations,
  Sonnet for tests, docs and prose review.

## 3. Out of scope (another agent owns these — do not touch)

- `jasper/correction/` algorithm and session, `jasper/audio_measurement/room_boundary.py`,
  the browser-capture deletion (`correction/browser_audio.py` and its plumbing,
  `deploy/assets/correction/js/*`, `measurement-audio.js`), and any transition-
  frequency or spatial-combination change. These are the Bank-style rows.
- `jasper/bass_extension/`, `docs/HANDOFF-bass-extension-plan.md`,
  `docs/bass-extension-waves/**` (parked by ADR-0018; owner works this directly).
- New tuning-flow capabilities (new views, new flags on the walk, new artifacts).
  If a cleanup row needs one, stop and report.
- Files of any open `claude/tuning-rightsize/` PR until it merges.

## 4. The work, in waves

Each row: **concern → evidence → keeper → proof → gate**. Run rows inside a wave
in parallel on disjoint files; merge in a batch after the owner's triage.

### Wave A — helper convergence and write-only records (low risk)

| Row | Concern | Evidence | Keeper / action | Proof | Gate |
|---|---|---|---|---|---|
| A1 | 12 byte-identical `_utc_now()` copies | `active_speaker/{baseline_profile, calibration_level, crossover_preview, design_draft, driver_base_trim, measurement, model_error_store, path_safety, seat_level_reference, staging, startup_load, web_measurement}.py`, all `time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())` | `jasper/json_fields.py:47` `utc_now_iso`; import swap at the 39 call sites | grep shows zero `_utc_now` defs in `active_speaker/`; tests unchanged | code-review |
| A2 | `_finite_or_none` ×3 and `_finite_number` ×2 | `crossover_v2/blend_prescription.py:547`, `driver_prescription.py:628`, `attribution/position_evidence.py:229`; `alignment_prescription.py:188`, `topology_prescription.py:225` | `jasper/json_fields.py:31` `finite_float` (identical semantics: rejects bool, catches OverflowError, isfinite) | door tests unchanged | code-review |
| A3 | six hand-rolled canonical-JSON-bytes helpers | `commissioning_evidence_store.py:152`, `driver_safety.py:159`, `commissioning_receipt.py:145`, `null_walk.py:172`, `excitation_artifacts.py:228`, `excitation_admission.py:84` | `audio_measurement/evidence_identity.py:78` `json_fingerprint` / `_freeze_json` — **only** where the resulting sha is not persisted as an identity; if it is persisted, hold and list it | each site's tests unchanged; no persisted-value change | code-review (`driver_safety.py` site: NN adjacency, adversarial-review) |
| A4 | `POSITION_AXIS_*` re-declared | `crossover_v2/contracts.py:1340-1342` (comment says owner is `spatial.POSITION_AXES`); owner at `crossover_v2/spatial.py:606-616` | import and re-export from `contracts.py`; `session.py:23`, `measure_spec.py:30` unchanged | one behavior pin that the two agree | code-review |
| A5 | power-mean-in-dB inlined ×4 | `spatial_combine.py:1389,1527`, `interference_nulls.py:507`, `crossover_v2/round_views.py:1573` | `audio_measurement/analysis.py:251` `spatial_average_db`; if `spatial_combine`'s `1e-12` floor differs, make the floor a parameter rather than silently changing it | numeric parity test on a fixture cloud | code-review |
| A6 | write-only records | `check.json` written by `bind_evidence_publishers` (`web/correction_crossover_v2.py:2835`); `refs["check_artifact"]`, `refs["round_receipt_artifact"]`, `refs["position_artifacts"]` in the durable state — repo-wide readers are the writer, one docstring, `scripts/derive-crossover-incident-fixture.py`, and tests | delete with SPENT verdicts; delete their tests; keep `positions/*.json`, `candidate.json`, `round_receipt.json`, cloud artifacts (real readers: `position_cycle.py:226`, `evidence_packet.py:832,2726`, `controllability_ledger.py:105`) | grep proof in PR body; `jasper-round-views inventory` still lists every artifact that has a reader | code-review |
| A7 | findings readback for a wizard sentence | `_bind_findings_publisher` / `_publish_findings` / `_bank_household_findings` (`web/correction_crossover_v2.py:3057`, ~283 lines) reads back what it just wrote | keep the banked `findings_<phase>.json` (read by `jasper-round-views findings`); delete the readback and the sentence render | `round-views findings` test still passes on a fixture round | code-review |
| A8 | dead defs already flagged | `crossover_v2/priors.candidate_priors` (own docstring: tests only); `web/correction_crossover_backend.begin_commissioning_run` (zero callers); `attempts_loop.DECISIONS` (owner-approved delete); fold `crossover_v2/attempt_grading.py` (31 lines of constants) into `verification.py` | delete / fold; **check first whether wave 9 already landed these** | grep proof | code-review |

### Wave B — the prescription parser pair (medium)

| Row | Concern | Evidence | Action | Proof | Gate |
|---|---|---|---|---|---|
| B1 | `blend_prescription.py` (1,261) and `driver_prescription.py` (1,845) share 400 identical lines across ~15 parallel helpers (`_refuse`, `_finite_number`, `_parse_filters`, `_check_bounds`, `_check_composed`, `_prescriber`, `_rationale`, `_parse_prescription`, `read_*`, `*_route`, `*_to_candidate_fields`, `*_from_mapping`, `*_response_format`); `driver_prescription._refuse` already raises `BlendPrescriptionRefused`; a third `_refuse` at `prescription_spool.py:190` | extract `crossover_v2/_prescription_parse.py` with the shared helpers; domain checks (`_check_shelf_placement`, `_check_displaced`, `_check_classification`, `_boosts_in_crossover_overlap`) stay per module; in the same PR fix the known escape where a legal JSON `10**400` leaves the alignment and topology doors as a bare `OverflowError` (it must become a refusal with a code) | all four door test suites pass unchanged; one new parametrized pin over the four doors for the overflow case; refusal codes byte-stable | code-review high; adversarial-review if a declared-band clamp path is touched |

### Wave C — views and CLI surface (medium)

| Row | Concern | Evidence | Action | Proof | Gate |
|---|---|---|---|---|---|
| C1 | five renderings of the same banked curves | `jasper-round-views frequency`, `per-seat`, `agreement`, `directivity`, `co-metrics` (`cli/round_views/`, `ARTIFACT_BY_VIEW` at `round_views/_common.py:91-123`) | one verb with flags (or two at most) that writes the same named artifacts; keep every `ARTIFACT_BY_VIEW` row that has a reader; regenerate the menu (`scripts/generate-tuning-tool-menu.py`); runbook pointers | menu `--check` green; `inventory` unchanged; `tests/test_cli_exit_vocabulary.py` green | code-review |
| C2 | exit vocabulary / stdout shape drift | ADR-0237; `cli/_refusal.py:35-49`; audit found `null_door`, `measure`, `round_bank`, `basic_profile`, `crossover_prescriber` re-declaring or inverting codes | verify whether fresh-eyes row 1.6 landed; if any tuning CLI still deviates, put it on `_refusal.py` | the parametrized exit-set test covers every tuning `main()` | code-review |
| C3 | second derivation of the box declaration | `cli/measure.py:194-330` `read_box_declaration` duplicates `resolve_conductor_context`'s readers (`crossover_v2/conductor_context.py:303`) | if fresh-eyes row 1.7 has not landed, make `measure` consume `resolve_conductor_context`; do not merge the CLI host with the web host (row 2.4 owns that) | measure's own tests unchanged | two-pass code-review |

### Wave D — the three rewrites worth un-dropping (high; one at a time; contract-first)

The fresh-eyes plan dropped Phase 5 as "size for its own sake". These three are
different: they are the layers the LLM's own walk threads through. Each is a
rewrite against the unit's existing behavior tests, old unit deleted in the same
PR, hardware pass where marked.

| Row | Concern | Evidence | Action | Gate |
|---|---|---|---|---|
| D1 | web-twin dissolution | `web/correction_crossover_v2.py` (6,050 lines, registers no route, ~4.9k lines of engine work); `web-twin-map.md` steps 2–10; step 1 landed (#3950) | continue the ten steps in order (durable state before assembly/publishers/playback/apply); steps 6 (playback, excitation ceiling) and 10 (level lease, `set_volume_db`) are NN. **First check who holds this**; if a wave-9 branch already carries a step, do not double-run | two-pass code-review; NN steps adversarial-review + hardware pass |
| D2 | evidence packet | `crossover_v2/evidence_packet.py` 3,143 lines; consumers are `jasper-crossover-prescriber packet/status/propose/stage` and `round-views`; the frozen-snapshot contract (propose and stage reuse one packet) must survive | rewrite against those consumers to ~1.3k; no curve arrays on stdout (ADR-0237); packet `--out` defaults beside the round | contract suite from the existing prescriber tests; adversarial-review (the packet feeds the doors' clamps) |
| D3 | the commissioning evidence machine | `commissioning_evidence.py` 3,498, `commissioning_evidence_store.py`, `commissioning_receipt.py`, `commissioning_run.py`, `controllability_ledger.py`, the rest of `commissioning_*` (14 files, ~15k lines) — the trace found `commissioning_apply/receipt/evidence/capture` reached only from the v1 lane at `web/correction_crossover_backend.py:1047` (`_commissioning_capture_service`); `runtime_contract.py` (5,152) is touched by the v2 apply path at one site (`baseline_profile.py:3984`, 144 lines) | **Step 1, no code change:** produce a caller census: for every `commissioning_*` and `commission_*` module, is it reachable from the v2 path, the v1 lane only, deploy/systemd, or nothing. Post it as the PR-less report and ask the owner for decision **D13** (PLAN.md §6). **Step 2, only after the owner rules:** if D13 retires the v1 lane, delete it and the machine with SUPERSEDED verdicts (its deadness tests die in the same PR); if not, rewrite the machine against contract to ~3k. Either way `runtime_contract.py`'s v2-facing 144 lines move next to their emitter and the rest is scoped to its real callers | owner decision, then adversarial-review + hardware pass |

### Wave E — tests and prose (low, wide; Sonnet lanes)

| Row | Concern | Action | Proof |
|---|---|---|---|
| E1 | 183 test files / 176k lines for `active_speaker` | per PLAN row 1.4: one-line docstrings; delete non-clamp source-text and prose `match=` pins, naming the sibling behavior pin that covers the contract; parametrize sibling clusters (80 clusters / 507 tests were counted); de-dup the fake-Camilla and builder fixtures into `tests/active_speaker_fixtures.py`. Keep the 15 non-negotiable files heavy | collected assertion count unchanged for parametrization; `scripts/test-merge` green |
| E2 | prose | only inside a PR already touching the file; constants review on every prose diff | AST identity vs merge-base |

## 5. Coordination

- Before each wave: list open `claude/tuning-rightsize/` PRs and read PLAN.md §9
  on the recon branch. Append your wave's outcome to §9 in the same format
  (date, PRs, line deltas, what landed, what was refuted).
- The Bank-style agent will land: the wired-only ADR and the room product's move
  onto `audio_measurement/wired_capture.py`; the derived transition frequency;
  median cloud combination and the crossfade; a vertical view over the elevation
  poses; runbook sequencing. If one of your rows collides with those files, the
  smaller PR rebases; say so in the PR body.
- Conflict with an open PR: avoid its files; otherwise the smaller PR rebases.

## 6. Reporting

At the end of each wave, report one table: row | PR | Δ lines | verdict
(SPENT/SUPERSEDED/PROMOTE/RELOCATED/CONVERGED) | proof | gate result. Then a
list "premises found false at HEAD" with the grep that refuted each, and a list
"needs the owner" (D13, any NN hardware pass, any fingerprint-value change you
declined to make). Line deltas are the product; a wave with no deletions and a
long report is a failed wave.
