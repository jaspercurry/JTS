# 2026-09-09 deep audit — current baseline

| field | value |
|---|---|
| audited SHA | `53a883808` |
| date | 2026-09-09 |
| method | [docs/DEEP-AUDIT-PLAYBOOK.md](../DEEP-AUDIT-PLAYBOOK.md) |
| agents | 114 (7 Phase 0, 46 product tiles, 33 test tiles incl. 4 gap re-runs, 8 lenses, 6 scenarios, 10 verifiers, 2 challengers, 2 Phase 4) |
| coverage | 97.1% of tracked files (2,620 of 2,699); `tests/fixtures/` (25 files, 13,742 LOC) unopened |
| grades | Security A-, Docs A-, Observable B+, Clean B, Boundaries B, Performance B, Tests B, Hardware-safe B-, Deploy integrity B-, Resilient C+ |

**This is a frozen snapshot.** Every section below describes what was true
at `53a883808`; it is not edited after landing work — see
`docs/DEEP-AUDIT-PLAYBOOK.md`'s "immutable snapshot vs live ledger" rule.
Disposition of every finding lives in GitHub issues labelled
`audit-2026-09-09` (plus the generic `audit` label), tracked on issue
#4775, never in edits to this file. The one exception is the appendix, a
dated note written once and not maintained. Evidence tarball: release tag
[`audit-evidence-2026-09-09`](https://github.com/jaspercurry/JTS/releases/tag/audit-evidence-2026-09-09).
See [docs/audits/README.md](README.md) for how the archive works.

---

## 1. The eight Blockers

Full evidence, reachability argument and verifier reasoning for each id is
in the Phase-4 evidence tarball on the tracking issue. This table is the
frozen finding as it stood at `53a883808`; PR numbers are in the appendix
and live disposition is on issue #4775.

| id | file:function | what | fix size |
|---|---|---|---:|
| F-T27-1 / R-008 | `jasper/voice/output_gate.py:89-107 begin_turn` | Two awaits have no timeout; a measurement pause can hold the wake path deaf up to 120 s with no cue. | +20 |
| F-T33-1 | `jasper/voice/daemon_main.py:1261-1270` | `SpeechVADSetupError` exits 78 with no `_announce_park_at_boot`; the unit makes 78 a permanent, silent park. | +1 |
| F-T30-1 / R-005 | `jasper/wake_corpus/recording_backend.py:1452` | `_stop_retry_attempts` increments forever; `min()` bounds only the delay, not the count. | ~-250 |
| R-012 / F-T20-8 | `jasper/identity/speaker_name_discovery.py:148-210 find_bluetooth_conflicts` | Four D-Bus calls with no timeout run on the `/speaker/` POST handler thread. | +2 |
| F-S5-1 | `jasper/peering/state.py:288-294,409-413`, `jasper/peering/daemon.py:290-296` | Three terminal paths return `[]` with an RPC in flight; the future expires into `decision="WIN"`, so both speakers answer one wake. | +9/-3 |
| F-S2-1 | `deploy/lib/install/systemd-units.sh:867 park_audio_clients_for_core_graph_restart` | Stops 11 units and records nothing; an abort in the unguarded tail leaves the graph parked with no unpark. | +3 |
| F-S6-1 | `deploy/systemd/jasper-audio-hardware-reconcile.service`, `jasper-aec-reconcile.service` | Neither sets `StartLimitIntervalSec=0`; a DAC replug burst spends the start budget and the terminal pass never runs. | +6 unit, +12 test |
| F-S4-1 | `jasper/control/handlers/volume.py:108,151,295` | Three volume handlers and the voice tools bypass the measurement hold; only `/volume/set` with a `source` field declined. | +20/-2 |

F-T27-1 / R-008 is the one Blocker the audit left as an owner decision
rather than a fix to open — see §5.

---

## 2. Delete-now list

§2.1 of the Phase-4 report priced about 2,100 LOC of product/Rust/JS/CSS/CI
deletable with no owner ruling, plus roughly 1,600 LOC of test code that
falls out with it. What each item became is in the appendix.

| what the audit priced as deletable | register / finding |
|---|---|
| aplay tone backend and `AplayTonePlaybackBackend` (-893 with its tests) | R-175 |
| dead web JS + `jasper/web/sound_setup.py` GET/POST route tables | F-T35 / F-T36 |
| 145 dead `_LAZY_ATTRS` rows + 12 (not 48; count corrected during landing) barrel aliases | R-085 / R-086 |
| dead `jasper/audio_measurement/` code (-529); `compression_curve` and `band_levels_from_magnitude` kept (bass-plan §7.5 seams) | F-T24-2 |
| `OutputTransportPlan` + runtime-contract dead code (-476) | F-C2 |
| verified dead and nanny tests | §3 items |
| small verified dead code + env-file owner headers | misc |
| `FingerprintedRecord` mixin hoisting 23 `to_dict` bodies | R-122 |
| service-unit name literals converged to constants | duplication theme |

---

## 3. Test-suite verdict

| metric | value |
|---|---:|
| test files | 959 python, 1,004 incl. js/mjs |
| test functions | 18,775 |
| test LOC | 573,003 (product `jasper/` is 404,089) |
| test:product ratio | 1.42 : 1 |
| assertions | 59,666 (3.18 per test) |
| mock-shape asserts | 225 — **0.37% of asserts. Not a problem.** |
| `tests/test_laptop_onboarding_scripts.py` | **IS collected** (178 `self.assert*` calls). `unittest.TestCase` subclasses collect regardless of `python_classes`. Deleting it would have been the most damaging action available in Phase 0. |
| log-text substring pins | 783 sites, 132 files |
| mechanically convertible (→ `event_field_maps`) | 597 sites, 87 files |
| `pytest.raises(..., match=<prose>)` | 653 sites, 156 files — **blocked on the product**: needs a typed code field per exception class before it can be fixed test-side |
| private-attribute asserts | 1,784, 228 files — actionable subset ~400-600 where a public `_status_payload()`/`/state` twin exists |
| realistic recoverable LOC | ~5,500, **0.96% of test LOC** |
| recoverable-LOC roll-up (two independent counts) | 8,400 (raw sum) → 7,677 (dedup reports) → 7,637 (dedup filenames, 959 files, only 119 non-zero); 1,607 LOC of the figure this report first published is unattributed — re-run with a published script before quoting either number |

**Verdict:** the "pinned to bullshit" hypothesis does not survive the count.
The real debt is altitude — log-text and private-attribute pins — not
volume, and under 1% of the suite is removable.

---

## 4. 2026-09-05 register verification

The register (`docs/codebase-quality-review-2026-09-05/register.csv`) has
269 rows and no status column. Below are the rows this round independently
re-checked at HEAD.

### 4.1 Rows verified FIXED at HEAD

| row | evidence at HEAD | verifier |
|---|---|---|
| R-001 | `secret_redaction.py:16,23-25` suffix-anchored patterns, cites ADR-0243; `tests/test_secret_redaction.py` exists | V1 |
| R-207 | `grep -rn "tts_transport\|TTS_TRANSPORT\|tts_device\|REASON_TTS_DEVICE"` returns zero | V1 |
| R-230 | `grep -n "for_tests\|_UNSET" jasper/voice_daemon.py` returns zero; moved to `tests/_wake_loop.py` | V1 |
| R-034 | `commissioning_runtime.py` absent; deleted at `b08881d1c`, ADR-0230 | V2 |
| R-002 (Blocker) | `scripts/deploy-to-pi.sh:811 verify_or_record_peer_id` and `scripts/deploy-to-pi.sh:841 preflight_deploy_direction` both run unconditionally before rsync; pinned by `tests/test_laptop_onboarding_scripts.py:625` | V5 |
| R-022 | No `src/xrun_log.rs`; `Input::note_xrun` is two relaxed atomics | V5 |
| R-024 | Shared `jasper_tts_protocol` server: read timeout, write timeout, `TTS_MAX_CLIENTS` cap with drop-and-count | V5 |
| R-042 | `alsa_backend.rs:1650-1653 log_dac_write_failed` emits structured fields | V5 |
| R-239 | `Cargo.toml` is one workspace, one `Cargo.lock` | V5 |
| R-250 | `docs-linkcheck.py` runs inside the required `ci` aggregate | V5 |
| R-013, R-016 | Independently re-checked, both fixed | V4 |
| R-137 | `jasper/calibration_agent/` gone at `ce093fecc` | V4 |
| R-173 | No wizard `main()`s remain (docstring prose only) | V3 |
| R-068, R-152 | `correction/js/main.js` monolith gone; split modules exist | V3 |

**Partially fixed — do not close.** R-107 (13 `_utc_now` + 1 `_now_iso`
survivors remain), R-182 (`VOLUME_MIN_DB` survives, test-only reader),
R-100 (`POSITION_AXES` still duplicated but the tests now assert parity),
R-201 (three unread fields remain), R-134 (`make_singleton` never added),
R-055 (59 of 215 `_server.` sites remain), R-112 (`web/_service_state.py`
still hand-rolls `systemctl()`), R-145 (one fork remains, not a drop-in).

### 4.2 Rows citing stale or missing files

| row | what is stale | correction |
|---|---|---|
| R-012 | Cites `jasper/speaker_name_discovery.py:161-186` | Moved to `jasper/identity/` by `08716f69e`; still open, path-based re-check reads as fixed |
| R-257 | Cites `jasper/control/wifi_guardian_state.py:70-230` | File does not exist; `network.py:34` already shares the parser |
| R-034, R-137 | Subject modules deleted | Close |
| `p3-deletions.md:39,358` | Reasons about `bass_extension/__init__.py` as 730 LOC, six readers | It is 10 lines at HEAD |

### 4.3 Rows with wrong LOC estimates or bad citations

| row | issue | correction |
|---|---|---|
| R-049 | Says 7,165 LOC | `jasper/web/correction_crossover_v2.py` is 6,089 at HEAD |
| R-145 | "safe-to-rm guard exists FOUR times" | Six files delegate to a shared helper; one fork remains — do not budget -90 |
| R-156 | Estimates -2,150 | `scripts/s0-sync-*` no longer exist; restate as -1,176 |
| R-209 | Count 14 | 13 `JASPER_RAMP_*` knobs |
| R-122 | Estimates -1,200 to -1,300 | Real fix is `to_dict` only, **-62**, or ~-140 with a helper; `__post_init__` is not removable |
| R-048 | Publishes a concrete 6-way split | Introduces two import cycles plus one unlisted edge; not LOC-neutral (+150 to +200) |
| R-005, R-008, R-009, R-047 | Line numbers in `voice_daemon.py` | File is 2,956 lines; several cited lines are past EOF |
| R-105 | "five implementations" of one filter magnitude | Six sites, two differ **by design** (shelf Q); close this row |
| `verify` column | Two tiles read `verify=Y` as "fixed" | It means "needs an independent skeptic" — the opposite |

---

## 5. Owner decisions open on 2026-09-10 (tracked in #4775 and the `owner-decision` label; not updated here)

| decision | scope | note |
|---|---:|---|
| R-008 / output-gate bound | +20 LOC once designed | Barge-in redesign. Three designs failed adversarial review: preempt-without-cancel interleaves two TTS segments; duck-restore gated on `is_current` leaves music ducked; a flat 10 s bound breaks turns that used to succeed. |
| `jasper/active_speaker/commissioning_run.py` live-mutation API | ~953 LOC across 4 files | Does the run journal still have a job? Nothing calls `start()`; cascade wider than first priced (`jasper/active_speaker/commissioning_isolated_producer.py`, `CommissioningCaptureService`) |
| 2026-09-05 review `prompts/` tree | 3,544 lines, 14 files | One-time sub-agent inputs for a frozen review; nothing outside references them by path |
| `JASPER_RAMP_*` knobs | 13 knobs, keep ≤2 | Nothing in `deploy/` or `scripts/` sets any of the other 11 |
| avahi reload | polkit allowlist gap | One ungranted `systemctl reload avahi-daemon` |
| ADR-0259 bass-extension orphans | ~303 LOC | `jasper/bass_extension/apply_intent.py` + 6 read sites + `jasper/sound/graph_carrier.py:615` |
| multiroom spike harness | ~1,176 LOC + docs | Is the zero-follower bring-up block in `dumb-endpoint-bringup.md:851-866` still current? |
| `test_rust_runtime_panic_freedom.py` | 532 LOC | Add panic-freedom to the non-negotiable list, or accept as a documented exception |
| `ROUTE_BITPERFECT_DECLARED` | 45 LOC | Deletes a named refusal into a silent fallback unless the `.env.example` enum value drops too |
| v1 commissioning apply | 21,667 LOC | Unadjudicated since the 2026-09-05 register (a seventh, separate ruling) |

---

## 6. What only hardware or runtime can prove

No test suite, no `pytest --collect-only`, no network, no `gh`, no hardware
ran during this audit. Everything below was left explicitly unresolved by
the agent that raised it.

**Needs two speakers on one LAN** — the three peering double-answer
interleavings behind F-S5-1; whether `rank()`'s six tiers pick the right
room; the bonded snapcast lifecycle end to end.

**Needs the DAC or a dongle in hand** — F-S6-1's six-replug-in-10s burst;
whether `jasper-dongle-recover.service` self-aborts through
`jasper-camilla.service`'s `Requires=`; the XVF3800 DFU procedure in
`BRINGUP.md`; whether the accessory-bridge doctor row goes red when a HID
reader dies.

**Needs a wedged or slow dependency** — R-012's wedged `bluetoothd`;
F-T27-1's `MEASURE_PAUSE` landing inside the acquire window; a `systemd-run`
that blocks behind the 8-worker control pool's one lock.

**Needs a real deploy** — F-S2-1's abort in the install tail after the
park; `RuntimeMaxSec=7200` expiring and whether the `EXIT` trap runs (bash
semantics say it does not); whether `jasper-doctor` on a live Pi reports
what the docs promise.

**Needs a Pi under audio load** — the per-chunk `snd_pcm_hw_params_malloc`
plus ioctl on the SCHED_FIFO render thread; whether journald back-pressure
at a stall edge costs a render deadline; the cost of two BlueZ sessions
where one would do.

**Needs a measurement in progress** — F-S4-1: turn a HID volume knob during
a sweep and see whether the driver goes above its declared cap; the
`_deep_quiet_skip` `mute_drift` corner; which persisted pre-window level
wins after the 120 s autoclear.

**Needs the test suite to actually run** — the pytest descriptor leak that
pushed the CI timeout from 30 to 45 minutes, no issue, no expiry; whether
the heavy non-negotiable tests fail when the clamp, guard or redactor is
broken (no mutation testing has ever run).

**Needs a judgment call, not a machine** — whether the two bass-extension
resumption commits are functionally correct (the doc drift raised as
F-L8-1 / F-L8-2 in the Phase-4 synthesis §4.2 "drifted docs", on the
tracking issue's evidence tarball, is about docs lagging the decision, not
about the commits).

---

## 7. Critic's residual risk

The Phase-4 completeness critic re-derived every quotable number in the
report from `git ls-files` and the audit's own files, and re-read the
source behind all eight Blockers. Its headline: the audit's *reading*
coverage is real and better than most of its own hedges, but its
*arithmetic* is not — seven quotable numbers did not reproduce, three by
more than 20%, and one Blocker's stated evidence was flatly false against
the tree before correction (folded into §1-§4 above). Two Should-fix
findings lived only in the "rejected, do not re-argue" appendix and were
moved back in.

What the critic could not check: no test run, no network, no `gh`, no
hardware — the same constraints as the audit itself. It re-derived eight
Blockers, six dead-code rows and eleven counts from source; the remaining
~90 Should-fix rows and all 380 Nits were taken on the verifiers' word.
`tests/fixtures/` (25 files, 13,742 LOC, including the CamillaDSP emission
goldens behind the "hearing clamp holds at all 7 emitters" claim) was
opened by nobody in either the audit or the critic pass — the single
largest unresolved coverage gap this baseline carries forward.

---

## Appendix — landing note, written once on 2026-09-10 (not maintained; the ledger is issue #4775)

Everything above is frozen at `53a883808`. This appendix is the single
place where later work is named, and it is a dated snapshot too: it was
written on 2026-09-10 and is never refreshed. For current state read issue
#4775 and the `audit-2026-09-09` label, not this note.

**Blockers (§1).** F-T30-1 / R-005 was fixed independently of the audit by
PR #4671 (`claude/triage-0909-voice`), commit `4d38711ce` on 2026-09-09 —
after the audited commit, from an unrelated voice-triage lane, not from the
audit-landing wave. R-012 / F-T20-8 → PR #4687. F-S5-1 → PR #4688.
F-S4-1 → PR #4689. F-S6-1 → PR #4690. F-S2-1 → PR #4692. F-T33-1's boot-park
cue was branch `audit/voice-deafness-cues` (PR #4694, commit `11cc6a80a`),
and F-T27-1 / R-008 had no fix. **R-008 and the boot-park cues (#4694) were
open at the time of writing — see #4775.**

**Delete-now list (§2).** #4693 (aplay tone backend), #4695 (dead web JS and
`sound_setup.py` route tables), #4697 (`_LAZY_ATTRS` rows and barrel
aliases), #4696 (dead `audio_measurement` code), #4698 (`OutputTransportPlan`
and runtime-contract dead code), #4745 (dead and nanny tests), #4746 (small
verified dead code and env-file owner headers), #4752 and #4759
(`FingerprintedRecord` `to_dict` hoist, parts 1-2), #4748 (service-unit name
constants). One item did **not** land: `jasper/audio_lab.py` was priced for
deletion but survives at 11 lines, an env-name contract module — #4693 did
not remove it.

**Landing-lane volume.** 36 merges of `audit/*` branches reached `main`
between 2026-09-09 and the 2026-09-10 close-out (`git log origin/main
--merges | grep -c 'from jaspercurry/audit/'`), which includes the two
close-out PRs (#4776 ADR-0284, #4784 voice-ledger deletion). Beyond the
deletions above the lane carried doc-drift fixes (#4740), resilience and
observability small fixes (#4741), `_utc_now`/env-helper convergence
(#4742), privileged-action honesty (`RestartOutcome`, #4744), shared JS
helpers (#4753), bounded `MessageBus` connects (#4754), wizard restart
accuracy (#4755), the measurement hold named on every `/volume` response
(#4756), and env-file owner headers (#4762). All three boxes (jts3, jts4,
jts.local) ran `main` at `62b07672a` / `c6ee5d886` with a clean
`jasper-doctor` during this lane.

**caplog migration.** Ten PRs — #4743, #4750, #4757, #4760, #4761,
#4763, #4764, #4765, #4766, #4767 (there was no batch 7). Measured with
`git grep -c 'caplog\.text' <rev> -- tests`: **645 sites in 86 files at
`53a883808` → 182 sites in 59 files at `e589bc726`** (`main` on
2026-09-10).

**Owner decisions (§5).** The parked bass bench (~2,413 LOC) left the open
list: PR #4751, commit `2b3e3b6c0` on 2026-09-10, deleted
`jasper/bass_extension/bench/`.
