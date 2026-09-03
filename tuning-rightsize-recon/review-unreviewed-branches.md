# Review — 7 unreviewed tuning-rightsize branches (issue #3769)

Method: `git fetch origin`; per branch `mb=$(git merge-base origin/main origin/<b>)`,
`/code-review` (medium) over `git diff $mb origin/<b>`; plus caller verification by
`git grep` at `origin/main`, `git merge-tree --write-tree origin/main <b>`, and an
AST-with-docstrings-stripped comparison for every prose branch (I re-verified the
orchestrator's claim independently; it holds for all four, and also for #3835).

Mergeability against current `origin/main`: only **#3836 conflicts**. The other six
merge clean and `origin/main` has touched none of their files since their merge-bases.

---

## 1. `1-2a-as-dead` (PR #3836) — dead-code deletion — **HOLD**

Every deleted public symbol (`restore_pending_candidate_apply`,
`promote_isolated_driver_capture`, `prepare_summed_commissioning_config`,
`automatic_summed_excitation`, `prepare_automatic_driver_level_match`,
`restore_automatic_driver_level_match`, `AdmittedCaptureCallbackResult`,
`CommissioningFreshReadback`, `CommissioningLiveContext`, `SummedGraphRequest`,
`AutomaticSummedConfigRestoreError`, `failure_status`, `reservation_is_finished`) and
every deleted constant (`ACTIVE_STARTUP_CONFIG_NAME`, `RESERVED_CROSSOVER_EVENTS`,
`POST_APPLY_CAPTURE_SOURCE`, the sweep-dir / summed-config env names and their literal
values) was grepped at `origin/main` across `jasper/`, `deploy/`, `scripts/`,
`pyproject.toml`, systemd units and `deploy/assets/*.js`. Confirmed: no production
reference other than the ones listed below. `restore_pending_candidate_apply` really has
no caller — `_restore_failed_mutation_locked` (the sibling the doctrine names beside it)
survives and is still called from `_apply_measured_candidate_owned`. Deleted tests all
test deleted code. No non-negotiable clamp pin was removed.

1. **BLOCKER — `jasper/active_speaker/__init__.py`: conflicts with PR #3766's lazy map,
   and the naive resolution leaves two dead entries.** The branch edits the *eager*
   import block that no longer exists on `main`; `git merge-tree` conflicts on this file
   only. On `main` the deleted symbols live in `_LAZY_ATTRS` at **line 25**
   (`"ACTIVE_STARTUP_CONFIG_NAME": "camilla_yaml"`) and **line 194**
   (`"prepare_summed_commissioning_config": "staging"`). Because `__all__ = sorted(_LAZY_ATTRS)`
   and `__getattr__` resolves lazily, keeping them yields no import-time error but an
   `AttributeError` on first access and an `__all__`/`dir()` that advertises symbols that
   do not exist. *Fix:* rebase onto `origin/main` and delete exactly those two dict
   entries; no other `_LAZY_ATTRS` key is affected.
2. **HIGH (owner triage) — the deletion contradicts a recorded ruling.**
   `docs/REFACTOR-TUNING-2026-08.md:771` (row 4g) states the commissioning lane is
   "**being REPAIRED, not abandoned**", names `record_driver_capture` →
   `promote_isolated_driver_capture` as "production's only admitted-capture door", and
   records that "the free −2,089-line deletion this plan previously booked **stays
   withdrawn**". This branch deletes that promoter and the summed lane. No ADR supersedes
   4g. *Fix:* either get the owner's explicit retirement of 4g and update that row (and
   `docs/cutover-briefs-acceptance.md:638`) in this PR, or drop
   `promote_isolated_driver_capture` from the deletion set.
3. **MEDIUM — dangling doctrine pointer.** `docs/measurement-loop-doctrine.md:244` names
   "`_restore_failed_mutation_locked` / `restore_pending_candidate_apply`" as what removes
   an applied graph. *Fix:* in this PR, drop the second name from that sentence.
4. **LOW — comments pointing at a deleted function.**
   `jasper/active_speaker/web_commissioning.py:1860` ("mirrors `_play_capture_sweep`'s
   duration_s + 5.0 above") and `tests/test_active_speaker_commissioning_admission.py:515`
   ("the sibling call sites at `_play_capture_sweep`/`play_sweep`"). *Fix:* re-word both to
   name the surviving call site, or delete the clause.
5. **LOW — `RESERVED_CROSSOVER_EVENTS` removed while its doc still reserves the names.**
   `docs/active-crossover-information-design.md:1906,1919` still document
   `correction.crossover_proposal_ready` / `_level_locked` / `_level_failed` as reserved.
   The deletion itself is right (the constant and its whole-tree scan test were a guard
   against a hypothetical, exactly what AGENTS.md demotes). *Fix:* one line in that doc
   noting the names are reserved in prose only.

**Verdict: hold** — blocker 1 must be resolved on rebase and finding 2 needs an owner call.

---

## 2. `1-4-t1-source-pins` (PR #3834) — source-text pins → behavioral pins — **mergeable after listed fixes**

Direction is right and matches AGENTS.md ("never assert on source text"). For each
deleted assertion I checked for a surviving sibling. No hearing/limiter/excitation/SPL/XVF
pin was touched (grep of every removed line for `volume_limit|spl|limiter|clamp|xvf|SAVE_CONFIGURATION|set_volume_db`
returns nothing). The blend pair (`test_no_curve_however_hot_produces_a_boost_or_breaks_a_ceiling`:291,
`test_the_emitter_refuses_a_boost_rather_than_clamping_it`:312) that the rewritten
`test_the_blend_stage_charges_no_headroom` now points at both exist. The ramp
single-emitter pin's behavioral companion survives.

1. **MEDIUM — a deleted test with no surviving sibling.**
   `tests/test_doctor_core.py` (base:609) removed
   `test_endpoint_uses_cached_oneshot_not_inprocess_doctor` with no replacement. What
   survives pins the *unit file* and the MANAGED_UNITS membership; nothing now pins the
   **handler** side — that `jasper/control/server.py` starts `jasper-doctor-json.service`
   with `--no-block` and serves `/run/jasper-control/doctor-result.json` rather than
   spawning `jasper-doctor` in-process (which runs non-root and reports ~7 false
   failures). *Fix:* one behavioral test on the `/system/diagnostics` handler spying on
   the subprocess/systemctl seam, in `tests/test_control_server.py`.
2. **MEDIUM — test name asserts something the test does not.**
   `tests/test_audio_measurement_alignment.py:82`
   `test_a_ten_second_sweep_correlates_in_well_under_a_second` asserts
   `elapsed < 10.0` (line 98). *Fix:* rename to match the budget (e.g.
   `..._correlates_far_faster_than_a_naive_correlator`) or tighten the bound; the comment's
   "~50x the FFT path's measured cost" should name the measured cost so the budget is
   re-derivable.
3. **LOW — two tests now spawn interpreters.**
   `tests/test_audio_measurement_admitted_playback.py:1181` and the playback twin run
   `sys.executable -c "import …; print(sys.modules…)"`. Stronger than the old AST scan
   (transitive pulls count) but they now depend on `jasper` being importable from the
   test process's environment and add subprocess cost to the hardware-free lane. Accept,
   but note it; `stderr=subprocess.STDOUT` will surface an import failure as a confusing
   string-compare failure.
4. **LOW — lost positive-direction pin.** The old
   `test_the_explicit_operator_arm_is_untouched` also pinned that the auto path calls
   `converge_active_endpoint(reason=args.reason)`. The rewrite pins only the negative
   cases; the positive case is exercised at :355 but the `reason=` threading is not
   asserted anywhere. *Fix:* assert the captured `reason` in the :355 test.
5. **LOW —** `tests/test_interference_nulls.py:2037` calls `monkeypatch.undo()` mid-test,
   which reverts *all* patches for that test. Harmless today (one patch); fragile.

---

## 3. `1-4-t2-heavy-tests` (PR #3835) — `tests/test_spatial_combine.py` — **mergeable as-is**

Not a rewrite: it is a pure prose reduction. Verified mechanically —
- test function names: **91 before, 91 after, zero removed, zero added**;
- top-level defs: 112 → 112;
- `assert` statements inside test bodies: **540 → 540**, and *per-test* counts are
  identical for every one of the 91;
- `@pytest.mark.parametrize` argvalue counts identical per test (so the collected count
  does not move); `pytest.raises` blocks 13 → 13;
- **AST with docstrings stripped is byte-identical to base.**

Prose sampled at the highest-risk docstrings (rahmonic calibration, both measured-gap
floors, the power-mean hand-computation, the raised-window rahmonic lock). All compressions
are truthful; the load-bearing measured figures (2908 / 0.9955 / 439 / 2.7899) survive, and
the old text's own internal contradiction ("why it is 2.0" vs "the constant moved 2.0 → 1.65")
is gone rather than propagated.

1. **LOW — one dropped scope qualifier worth keeping.** The rahmonic-calibration docstring
   lost "The three corpus IRs … are deliberately not here: this test must run in CI, where
   the corpus is absent", plus its pointer to `test_detect_echo_finds_the_corpus_bounce`.
   That is a non-derivable reason a population is smaller than a reader expects.
   *Fix:* one sentence back.

---

## 4. `1-3-p3-packet-coord-refusal` (PR #3833) — prose — **mergeable after listed fixes**

AST-identical after docstring strip (5 files). No new `:func:`/`:mod:` reference in the
added prose fails to resolve at HEAD; no new `docs/…` or `ADR-…` pointer is dangling.

1. **MEDIUM — a counterfactual compressed into a false absolute.**
   `jasper/active_speaker/crossover_v2/coordinator.py:8`: "**The only module here that
   changes the speaker** — it calls seams that act." The base carried the same claim but
   immediately hedged it ("This module's own text said 'every other module in this package
   is side-effect-free' … that was too strong, and the narrower claim is the one worth
   checking"). Stripped of the hedge it now reads as a checked fact, and it is false at
   HEAD: `session_graph.py`, `program_transaction.py`, `playback_transaction.py`,
   `door.py` and `composition.py` in the same package also take the DSP writer lock /
   `SetConfig`. *Fix:* "the only module in the ROUND TAIL that changes the speaker", or
   drop the superlative.
2. **MEDIUM — lost non-derivable constraints on an absent value.**
   `jasper/active_speaker/crossover_v2/verification.py:1784`
   (`_verify_frame_from_tracking`) dropped four facts that cannot be re-derived from the
   code: it renders on *every* outcome; `None` means "no frame was measured", **never**
   "the frames matched"; tilt-removed keys are omitted individually rather than defaulted
   to their raw twins (a beside-number equal to its twin would read as a measurement);
   and `pivot_hz`/`n_bins`/`band_hz` travel because a two-parameter fit over few bins is
   ill-conditioned and `frame_fit` reports the span instead of a confidence policy.
   *Fix:* restore those as ~3 lines.
3. **LOW — `refusal_copy.py:5` dropped the `SCREEN_KIND_REASONS` coverage rule** (keyed by
   `capture_dispatch.CAPTURE_SCREEN_KINDS`, covering it exactly, so a new rung in either
   owner cannot ship without household copy). It is still pinned by
   `tests/test_crossover_v2_spatial.py:1234` and pointed at from `capture_dispatch.py:25`,
   so this is discoverability, not truth. Optional one-liner.
4. **LOW — package convention drift.** `refusal_copy.py`, `coordinator.py` and
   `__init__.py` lost the "Dependency direction … no `jasper.web` import and nothing from
   `crossover_v2_flow`" line that fourteen siblings still carry. It is test-enforced, so
   removing it everywhere is defensible; removing it in three files is not. Either finish
   the removal in a follow-up or put it back here.

---

## 5. `1-3-p9b-capture-session` (PR #3830) — prose — **mergeable as-is**

AST-identical after docstring strip (16 files); no unresolved new references. The
highest-risk content survived compression intact: `blend_correction.py:1` keeps the
honesty-mask rule ("the only structural protection against cutting an interference null
instead of a real excess", `uncalibrated_below_hf_floor` below 4 kHz, "a bin the mask
removed is not a bin this module may cut"), the damped fixed point with `k = 0.7`, and the
`BLEND_NO_INCUMBENT` exception. `sweep_spec.build_crossover_sweep_spec` keeps every
parameter contract including `reverify_lead` and `default_setup_calibration`'s
`crossover_v2_uncalibrated_capture` consequence. `capture_dispatch._gate_moved_rms_db`
keeps its `None` semantics and the "only interpretable beside `gate_floor_source`" rule.

1. **LOW — dropped measured expectations.** `blend_correction.py` lost "roughly 0.5–1.5 dB
   of region rms, not the closing of a 4.2 dB notch" *and* the reason it matters ("why the
   grading in `round_evidence` is region-scoped: a localized win of that size is invisible
   inside a full-spectrum pooled average"). The second half is a why-pointer, not history.
2. **LOW —** `session_graph.py` lost the measurement that justifies the module
   (`Δ1 ≈ 489 ms + Δ2 ≈ 454 ms ≈ 0.94 s` of pure duck ramp per swapping stimulus).
   Defensible as history; note it in the PR body so it is recoverable from git.

---

## 6. `1-3-p12b-as-measurement-side` (PR #3831) — prose — **mergeable after listed fixes**

AST-identical after docstring strip (1 file).

1. **MEDIUM — garbled sentence introduced.**
   `jasper/active_speaker/capture_provenance.py:25`: "an overnight campaign carried the
   **two two lines apart** disagreeing by 8.712 dB". The base read "carried those two
   fields two lines apart". *Fix:* "carried the two fields two lines apart".
2. **LOW — lost scope qualifier on a disclosure.** The module docstring no longer says
   `volume_fields_agree` runs only on records this module **observes** — "which is not
   every capture, because observation is bought only while capture retention is on" — nor
   that `observe_capture_provenance` is what logs the WARN when a retained record
   self-contradicts. The new text is not false, but a reader will over-read the coverage.
   *Fix:* one clause.

Dropped forensic figures (+7…+15 dB per-branch, −27.5 dB fader, 2026-08-19 jts3) are
history the base itself flagged as "its observation, not a property this module measures";
dropping them is correct under AGENTS.md.

---

## 7. `1-3-p13a-cli` (PR #3832) — prose — **mergeable as-is**

AST-identical after docstring strip (3 files); no unresolved references. `null_door.py`'s
`_gap_ceiling_db` keeps the formula, the "gap is what the level match REMOVED" inversion
and all three branches. `measure.py`'s `--candidate-id` rule survives on
`REFUSE_CANDIDATE_ID_REQUIRED` (line 44) and in `_require_candidate_id` (line 342).

1. **LOW — a docstring reduced to a restatement of its own name.**
   `jasper/cli/crossover_prescriber.py:362` `_scope` is now
   `"""What this prescription's filters were bounded BY, in one phrase."""`, having lost
   the only content it had (naming the wrong bound misleads an operator into reading a
   crossover region into a per-driver document). AGENTS.md says a comment that narrates
   the name should be deleted, not kept: either restore the one-clause *why* or drop the
   docstring.
2. **LOW —** `measure.py`'s module docstring lost the `Usage::` block; the argparse epilog
   (line 1075) carries only one of the four examples, and `--specs` — the flag the removed
   prose existed to explain — is no longer shown anywhere. *Fix:* add the `--specs`
   example to the epilog.

---

## Verdicts

| PR | Branch | Verdict |
|---|---|---|
| #3836 | `1-2a-as-dead` | **hold** (lazy-map conflict + REFACTOR-TUNING §4g owner call) |
| #3834 | `1-4-t1-source-pins` | mergeable after listed fixes (#1, #2) |
| #3835 | `1-4-t2-heavy-tests` | mergeable as-is |
| #3833 | `1-3-p3-packet-coord-refusal` | mergeable after listed fixes (#1, #2) |
| #3830 | `1-3-p9b-capture-session` | mergeable as-is |
| #3831 | `1-3-p12b-as-measurement-side` | mergeable after listed fixes (#1) |
| #3832 | `1-3-p13a-cli` | mergeable as-is |
