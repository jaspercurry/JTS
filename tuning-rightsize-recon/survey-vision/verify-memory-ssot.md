# verify-memory-ssot — LANDING §1 "Round directory as memory" + HANDOFF-NEXT §4 "One source of truth"

Read-only. Every citation opened at HEAD `f4ff89731` (LANDING cites `5d32f683d`, so
its line numbers are ±2 in places; each row below gives the HEAD line).

---

## 1. `applied-profile.json` and the trim decision

**Verdict: LANDING is TRUE on `committed_side` and `anchor_drift_db`, FALSE on
"`trim_strategy` is what's serialized". None of the three fields reach any file on
disk at HEAD.**

Trace:

| Step | file:line | What it holds |
|---|---|---|
| `TrimDecision` (dataclass) | `jasper/active_speaker/crossover_v2/plan_assembly.py:122` | not `intervention.py`, as the brief/LANDING assumed |
| `TrimDecision.anchor_drift_db` | `plan_assembly.py:130` | float, required |
| `TrimDecision.committed_side` | `plan_assembly.py:138` | derived `@property` off `strategy` |
| minted | `crossover_v2/intervention.py:784` | `anchor_drift_db=drift_db` |
| **log event only** | `intervention.py:1513` (`"committed": trim.committed_side`), `intervention.py:1515` (`"drift_db": round(...)`) | the sole consumers of both |
| `LinearizationState` | `crossover_v2/candidates.py:53`; `trim_strategy` `:78`; `anchor_drift_db` `:84`; carried `:99-100` | in-process value, per #2291 |
| strategy → prose | `crossover_v2/proposal.py:76-82` | the drift only survives as text inside `trim_rationale` |
| `InterventionProposal` | `crossover_v2/contracts.py:526` (`trim_strategy`), serialized `:676-677` in `_core()`, `to_dict()` `:692` | **never written to a file** |
| proposal held in memory | `crossover_v2_flow.py:3406`, accessor `:1658-1660` | zero production readers; only `tests/test_crossover_v2_proposal.py:470,509,563` |

What actually lands on disk is the coarser `linearization_outcome` string
(`""`/`fitted`/`trim_rejected`/`ineligible_*`/`fit_failed`):

- applied profile: built at `jasper/active_speaker/baseline_profile.py:2929`
  (`build_baseline_profile_candidate`, `:2025`), **write site
  `baseline_profile.py:3628`** (`atomic_write_text` in
  `persist_applied_baseline_profile`, `:3556`); re-emitted on read at `:1360`.
- `state.json`: `crossover_v2/durable_state.py:895`.
- `candidate.json`: `measured_crossover_candidate.py:382,400`.

`applied-profile.json` beside a round is a byte copy of that SSOT
(`round_bank.py:107-109` + filename `crossover_v2/round_inputs.py:62`; default path
`state_paths.py:18`), so it inherits exactly the same fields.

Consequence: `"fitted"` cannot distinguish an anchored commit from a resolved one —
`proposal.py:88-92` says so in its own fallback. The round directory therefore
cannot answer "which trim pair shipped, and how far the scan sat from the anchor"
after the process exits.

*Smallest honest change:* add `trim_strategy`, `anchor_drift_db` (and let
`committed_side` stay derived) to the candidate dict `build_baseline_profile_candidate`
already spreads beside `linearization_outcome` at `baseline_profile.py:2929`, plus the
matching allowlist entry at `:1360`. **Size: small.**

---

## 2. `position_cycle.json`

**Verdict: LANDING TRUE, in full.** `git grep -n position_cycle` over the whole tree:
1 writer, 0 production readers, and the bank path does not write it.

| Role | file:line |
|---|---|
| filename constant | `crossover_v2/position_cycle.py:36` |
| document builder | `position_cycle.py:454` (`position_cycle_document`) |
| **only writer** | `scripts/run-crossover-round.py:741` (`bank_position_cycle`), write at `:765`, **gated `if args.angles:` at `:1121`** — so even the laptop transport skips it on an un-angled round |
| strict reader | `position_cycle.py:496` (`read_position_cycle`) — **zero production callers**; only `tests/test_crossover_v2_position_cycle.py:27` |
| on-box `jasper-round bank` | `jasper/active_speaker/round_bank.py:168` (`bank_round`) — zero `position_cycle` hits in the file |
| not even named as missing | absent from `ARTIFACT_BY_VIEW`, `jasper/cli/round_views/_common.py:79-104`, so `inventory` (`round_views/inventory.py:50-60`) never reports it |

The other module-level uses of `position_cycle` (`evidence_packet.py:47,913,1021`,
`round_views.py:1827,1963`, `delay_landscape.py:41`, `forward_model.py:29`,
`feature_classifier.py:769`, `cli/null_door.py:227`) all import *take readers*
(`read_lateral_take`, `parse_curve_complex`, `read_take_curves`), never the index.

`bank_round` has every input: it copies the bundle to `target/bundle/<session>`
(`round_bank.py:233-237`), which is exactly `_BANKED_BUNDLE_GLOB = "bundle/*"`
(`position_cycle.py:42`, used `:442`).

*Smallest honest change:* after the copytree in `round_bank.py:233-237`, call
`position_cycle_document(target)` and write `POSITION_CYCLE_FILENAME` best-effort,
naming a failure in `provenance.json`'s `missing` the way an absent SSOT doc already
is. **Size: tiny.**

---

## 3. The five round artifacts

| Artifact | Writer (file:line) | Reader | Content or presence |
|---|---|---|---|
| `proposal.json` | `jasper/active_speaker/bundles.py:939` (`record_apply`, `:897`) — payload is `dict(candidate)`, `kind="candidate_profile"` | `bundles.py:977` (`has_proposal`) | **presence only**; zero content readers repo-wide |
| `apply.json` | `bundles.py:951` (same call, only when `apply_state is not None`) | `bundles.py:978` (`has_apply`) | **presence only** |
| `candidate.json` | `crossover_v2/record_store.py:126` (`_ROUTES[CANDIDATE_KIND]`, verify `_verify_candidate` `:80`) | `evidence_packet.py:1759`; `round_views.py:647-649`; `candidate_bank.py:70` (glob `:31`) | **content**, three independent readers |
| `packet.json` | `jasper/cli/crossover_prescriber.py:208` (`_cmd_packet`), path `:225`, write `:231` | `crossover_prescriber.py:139` (`_read_packet_file`, `:108`) via `--packet` for `propose`/`stage`/`status` | **content** (cross-invocation, same tool) |
| `delay_confirmation.json` | `jasper/cli/round_views/delay.py:161` → `_bank` `:93` | none in code; only `inventory` presence via `_common.py:93` | **written, never read back** — LLM only |

`expected_delta_db` / `declared_tilt_db_per_octave` ride **inside `candidate.json`**, at
`linearization[<role>].prescribed_by`:

- field names: `crossover_v2/driver_prescription.py:149-150`
- stamped: `driver_prescription.py:1488-1499` (`pre_registration` folded into each role's
  `prescribed_by`), also `:547-548` on the prescription's own `to_dict`
- read back a round later: `crossover_v2/round_views.py:652-664`
  (`_banked_pre_registration`, off `_round_candidate` `:646`), surfaced on
  `FrozenReferenceResult` `:692-693`, emitted `:715-716`.

So LANDING's "TARGET conflates `proposal.json` with the prescription; the expectation
fields ride in `candidate.json`" is **TRUE**, and `expected_delta_db`'s cross-session
round trip is real.

*Smallest honest change (if any):* none needed for correctness; if the two
presence-only files are to stay, say so in their `write_json_artifact` `kind=` rather
than implying a reader. **Size: tiny.**

---

## 4. σ `kind`

**Verdict: LANDING TRUE — two labelled registers, four unlabelled producers — and
YES, the LLM reads unlabelled σ.**

| Producer | file:line at HEAD | Labelled? |
|---|---|---|
| `LAB_ROW_UNCERTAINTY` | `crossover_v2/feature_classification.py:170` (`"kind"` at `:172`, `:186`) | **yes** (`kind` + `of`) |
| `_CROSS_SEAT_SIGMA_UNCERTAINTY` | `crossover_v2/evidence_packet.py:547` (`"kind"` `:550`), published `:722` | **yes** (`unseparated`, `feature_classification.py:162`) |
| `BandSpread.sigma_db` / `.max_sigma_db` | `jasper/audio_measurement/spatial_combine.py:436`, fields `:452-453` (LANDING said `:452` — the module is under `audio_measurement/`, not `crossover_v2/`) | **no** — the random/structure distinction is docstring prose at `:438-446` |
| cloud `band_spread` serialization | `crossover_v2/planning.py:296-306` (`exclusion_evidence_json`, `:273`) | **no** |
| `sigma_db_by_rung`, `sigma_growth_ratio` | `crossover_v2/gate_sweep.py:565-567` (`_axis_sigma_row`, `:554`) | **no** |
| pooled `sigma_db` per rung | `crossover_v2/feature_classifier.py:2057` | **no** |

Does the LLM read an unlabelled σ? **Yes, at least twice:**

- `gate_sweep.json` — written by `jasper-round-views gate-sweep`
  (`jasper/cli/round_views/sweeps.py:95-98`), carrying `sigma_db_by_rung` verbatim.
  This is the *attribution* artifact ("room vs speaker"), so the unlabelled number is
  the one the LLM reasons about hardest.
- `candidate.json`'s `exclusion_evidence.band_spread[].sigma_db` / `max_sigma_db`
  (`planning.py:301-302`), which `evidence_packet.py:745` explicitly notes "reach only
  candidate.json's exclusion_evidence".

*Smallest honest change:* give `gate_sweep`'s document the same `{kind, of}` register
the packet already declares, reusing `feature_classification.UNCERTAINTY_*`
(`:161-170`) rather than a third vocabulary. **Size: small.**

---

## 5. `jasper-null` play-and-capture vs the wired capture kernel

**Verdict: HANDOFF §4 TRUE, and the admission-gate half is TRUE.** The kernel is
`jasper/web/correction_crossover_v2_wired.py`, as LANDING said (not `cli/measure.py`,
which only *binds* it at `:663,717`).

| | `jasper-null` | wired kernel |
|---|---|---|
| body | `jasper/cli/null_door.py:369-426` — **58 lines** | `WiredStimulusCapture.around` `:404-443` (40) + `_recorder_for` `:456-473` (18) + `_mint_and_place` `:475-498` (24) + `mint_wired_answer` `:302-345` (44) + `make_wired_recorder` `:347-368` (22) = **148 lines** |
| channel selection | `select_capture_channel` (`null_door.py:425`) | same, `:351` inside the minter |
| **zero-run scan** | **absent** | `scan_zero_runs` (`correction_crossover_v2_wired.py:352`; impl `audio_measurement/wired_capture.py:434`) |
| **integrity report** | **absent** | `build_capture_integrity_report` (`:355-360`) → rides the answer into `capture_integrity`, whose refusal vocabulary is `crossover_v2/verification.py:203-205` |
| device identity / per-channel RMS | **absent** | `:361-371` (`channel_selected`, `channel_rms_dbfs`) |
| stored calibration reference | **absent** (row stamps `"calibrated": False`, `null_door.py:530`) | `_wired_setup_reference` (`:343`) |
| channel count | hardcoded `channels=2` (`null_door.py:409`) | from `SUPPORTED_MODELS[model_key].capture_channels` (`:356-359`) |
| recorder budget | inline arithmetic `:410` | `_recorder_for` `:465-471`, same two named allowances |
| refusal on a dead/silent capture | only downstream, at analysis: `summed_capture_curve` returns `None` → `NullDoorRefused(REFUSE_UNUSABLE_CAPTURE)` (`null_door.py:453-459`; gate in `driver_acoustics.py:269-282`) | same analysis gate *plus* the frame-ledger/zero-run evidence banked with the take |

So the precise deficit: **null lacks the zero-run scan, the capture-integrity report,
the device/channel metadata and the calibration reference. It does still refuse an
unheard sweep**, but through the quality gate only — a capture with ALSA dropouts that
passes quality is graded as intact, and nothing on disk says otherwise. The banked row
(`null_door._row`, `:484-556`) carries `wav_sha256` and nothing about how the capture
went.

Same excitation admission gate: **confirmed.** Both funnel through
`play_program` (`jasper/active_speaker/program_playback.py:86`), which asserts the
session volume then re-admits from the rendered WAV bytes (`:100-102`):
- null: `null_door.py:378-402` binds `bind_program_playback_seams`
  (`crossover_v2/composition.py:142`, `readmit_program_from_wav` at `:167-182`) and
  awaits `play_program` at `:419`.
- wired/measure: `crossover_v2/program_transaction.py:198-205` awaits the same
  `play_program` inside `capture.around(_play, …)` (`:210-212`).

*Smallest honest change:* replace `null_door.py:405-426` with a call to
`make_wired_recorder` + `mint_wired_answer`, banking `capture_integrity` and the
device block on the row. **Size: medium** (it makes `cli` → `web` a sixth allowlisted
edge, `tests/test_correction_boundary_ssot.py:276`, unless the minter moves to
`audio_measurement/wired_capture.py` first).

---

## 6. Dead code, with verdicts

`CrossoverLevelLease` is at `jasper/web/correction_crossover_backend.py:137`. Grep over
`jasper scripts deploy experiments pyproject.toml tests docs`:

| Symbol | Definition | Hits (total) | Production callers | Test callers | Verdict |
|---|---|---|---|---|---|
| `.run_level_match` | `correction_crossover_backend.py:441` | **37** (incl. the unrelated `MeasurementSession.run_level_match`, `correction/session.py:2244`) | **0** — `git grep -n "\.run_level_match" -- jasper` returns **nothing** | `tests/test_correction_level_match.py:1177,1227,1307` (lease) + `:838…1136` (session) | **SUPERSEDED** — the live singleton (`:1054`, `level_lease()` `:1061`) is used in production only for `unresolved_volume_safety` and `invalidate_comparison_context` (`correction_setup.py:4305-4312`, `correction_crossover_backend.py:1104-1112`); a level-matched take now reads banked evidence (`jasper/cli/measure.py:497-514`, `measured_level_trims`) instead of running a ramp. Note the same verdict falls on `MeasurementSession.run_level_match` — it too has zero production callers. |
| `.configure_targets` | `correction_crossover_backend.py:379` | **3** | **0** | `tests/test_correction_level_match.py:1195`, `tests/test_crossover_driver_level_domain.py:90` | **SPENT** — only reachable as `run_level_match`'s pre-step |
| `.driver_sweep_locked_main_volume_db` | `correction_crossover_backend.py:594` | **9** | **0** | `tests/test_correction_level_match.py:1205,1248`; `tests/test_crossover_driver_level_domain.py:125-174` | **SPENT** — reads `self._outcomes`, which only `run_level_match` fills |

No dynamic escape hatch: zero `getattr(...lease...)` sites in `jasper/`, and zero
mentions of any of the three names under `deploy/`.

`FaninGateContext` — `jasper/active_speaker/web_commissioning.py:118`. Grep: **17 hits.**

- Threaded as `FaninGateContext | None = None` through
  `web_commissioning.py:638,661,701,739,1652,1971` and
  `correction_crossover_backend.py:1650,1667`.
- **Never constructed in production**: the only constructions are
  `tests/test_active_speaker_web_commissioning.py:623,669,850,884`.
- Worse than "nested mode is dead": its only non-`None` entry point,
  `correction_crossover_backend.play_driver_capture_sweep` (`:1643`), has **zero
  callers anywhere** — 23 hits for `play_driver_capture_sweep` across the tree, all
  either the two definitions, the backend's own call into `web_commissioning`
  (`:1661`), one comment (`web_measurement.py:1189`), or tests that call the
  `web_commissioning` function directly.
- **Verdict: SPENT** for the nested mode *and* for the backend wrapper above it. The
  dataclass itself would go with them; the standalone (`None`) path is live.

*Smallest honest change:* delete the three lease methods with their tests, and delete
`correction_crossover_backend.play_driver_capture_sweep` + the
`fanin_gate_context` parameter threaded from it. **Size: medium** (two separate
single-concern PRs; the second touches 6 signatures).

---

## 7. Duplicated helpers

`git grep -nE "^\s*def (_utc_now|_sha256\w*|_finite_\w+)\(" -- jasper`:

| Name | HANDOFF said | At HEAD | Bodies |
|---|---|---|---|
| `_utc_now` | ×12 | **16** | 15 of 16 are byte-identical `return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())` — checked `baseline_profile.py:213`, `calibration_level.py:41`, `dsp_apply.py:165`, `output_hardware.py:433`. The **outlier** is `jasper/voice/model_discovery.py:55`, which uses `datetime.now(UTC)…isoformat()` — same output shape, different implementation. |
| `_sha256*` | ×10 | **17** (12 spelled exactly `_sha256`) | Mostly **already thin adapters** over a shared home: `commissioning_evidence.py:111`, `excitation_safety_plan.py:64`, `measured_candidate.py:167` all just call `require_sha256_hex` with their own error type. The **outlier** is `audio_measurement/evidence_identity.py:42`, which re-implements the regex check inline. The rest (`_sha256_file`, `_sha256_fd`, `_sha256_text`) hash different *things* and are not duplicates. |
| `_finite_*` | ×40 | **34** | Two families. The raising `_finite_number(…, field=…)` variants (`blend_prescription.py:683`, `alignment_prescription.py:188`, `topology_prescription.py:225`, …) differ only in error type/reason and are legitimate per-module adapters. The **reading** `_finite_or_none` family is genuine duplication: `driver_prescription.py:628` is logic-identical to `jasper.json_fields.finite_float:23`; `verification.py:1327` and `attribution/position_evidence.py:229` differ only by an extra `None` guard. |

Shared homes already exist: `jasper/json_fields.py:23` (`finite_float`, strict),
`jasper/active_speaker/_common.py:99` (`finite_float`, coercing — the difference is
documented at `:100-103`) and `_common.py:125` (`require_sha256_hex`). There is no
`jasper/_time.py` and no `jasper/hashing`.

**Recommendation — mostly leave, with one real defect to fix.** The `_finite_number`
adapters and the `_sha256` wrappers already consume a shared home; converging them
further would only trade a two-line wrapper for an argument-passing error type, and
`_utc_now` is a one-line stdlib call whose 15 clones cannot drift meaningfully. The
one change worth making is narrow: **`crossover_v2/evidence_packet.py:2338`'s
`_finite_or_none` omits the `math.isfinite` check** that every other member of its
family has, so a `NaN`/`inf` `gain_db` passes straight through at `:2320-2324` into
the packet, and `json.dumps` then emits bare `NaN` — invalid strict JSON for any
downstream reader. That file **already imports** `json_fields.finite_float` at `:37`,
so the fix is deleting the local helper and using the import. **Size: tiny.** If a
second cleanup is wanted, retire the other three `_finite_or_none` clones
(`driver_prescription.py:628`, `blend_prescription.py:556`,
`attribution/position_evidence.py:229`) onto the same import. **Size: small.**

HANDOFF's "103 helper names defined in two or more modules" is **COULD-NOT-DETERMINE**:
at HEAD I count 214 module-level private names defined in ≥2 modules (286 including
nested defs), and 61 in ≥3 — no threshold reproduces 103, so the methodology behind
that number is unrecoverable. The *direction* is TRUE.

---

## 8. Did PR #4024 change any of the above?

`git show --stat 2614bb94d` — "Fold the five evidence publishers into RecordStore
(ADR-0227 §12)", merged as `d6a52055c`, an ancestor of HEAD. The PR is two commits,
`2614bb94d` + `30b608565`, touching:

`crossover_v2/record_store.py`, `attribution/__init__.py`, `attribution/storage.py`,
`web/correction_crossover_v2.py`, and four test files.

- **Item 3: yes, one row.** `candidate.json` and `round_receipt.json` are no longer
  written by per-kind publishers in `web/correction_crossover_v2.py`; they bank through
  `_ROUTES` in `record_store.py`. LANDING's `record_store.py:127,128` are
  **`:126` (candidate) and `:130` (receipt)** at HEAD, and the route table gained
  `routing_keys` (`:68-70`) so the findings route's injected `phase` is stripped from
  the file. Nothing about *which fields* land changed.
- **Items 1, 2, 4, 5, 6, 7: no.** None of `baseline_profile.py`, `bundles.py`,
  `round_bank.py`, `position_cycle.py`, `run-crossover-round.py`, `plan_assembly.py`,
  `intervention.py`, `candidates.py`, `contracts.py`, `null_door.py`,
  `correction_crossover_v2_wired.py`, `correction_crossover_backend.py`,
  `web_commissioning.py`, `gate_sweep.py` or `feature_classification.py` is in the
  diff.
- Side effect worth knowing: the PR **deleted** `attribution.storage.publish_finding_set`
  (`attribution/storage.py`, −20 lines) in favour of the store route
  (`record_store.py:137-141`).
