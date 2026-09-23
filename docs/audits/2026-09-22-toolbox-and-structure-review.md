# 2026-09-22 agent toolbox and structure review

| field | value |
|---|---|
| audited SHA | `7111e55d0` (origin/main, 2026-09-22 22:20 UTC) |
| scope | (1) the tuning toolbox as an agent-facing product ("REW for agents"): tool surface, object model, output contracts, boundaries, refusals on the analysis → prescription → apply path; (2) god files, dead code, duplication and stale prose in `jasper/active_speaker/`, `jasper/audio_measurement/`, `jasper/sound/`; (3) platform zones the 2026-09-22 codebase-health refresh did not cover (web wizards, control, mux, multiroom, non-tuning CLIs) |
| method | 6 read-only Claude agents (R0 REW research — Sonnet with web access; R1 toolbox, R2 refusals, R3 tuning structure, R4 tuning duplication/dead code/prose, R5 platform — Opus), then 2 owner-question investigations (legacy views; jasper-measure vs jasper-round) |
| tracking issue | #5658 |
| evidence | the six reviewer reports are comments on #5658 ([ADR-0284](../adr/0284-audits-are-frozen-reports-and-issues-are-the-ledger.md)) |

**This is a frozen snapshot** of `7111e55d0`. Disposition lives on the tracking issue and in issues labelled `audit` and `audit-2026-09-22-toolbox`. Finding ids are the per-agent ids (`R1-nn`, R2 `Fnn`, `R3-nn`, R4 `Dn`/`Pn`/`Cn`/`Sn`, `R5-nn`).

---

## Bottom line

1. **The toolbox has good bones.** One CLI failure/answer contract (ADR-0237), one program/layout registry, one authoring-contract owner, one composer, sets keyed by capture basis (ADR-0299), a generated tool menu. The middle is one machine.
2. **The agent does not yet get one object model.** A take has one id, but views addressed it seven ways; rounds were addressed only by path; no verb listed rounds; five modules walk the stores; two documents were both "the packet" (R1-08, R1-09, R1-13).
3. **About 15.5k product lines in the tuning zone were unreachable** (R3). About 12.5k are the old "Gen A" v2-flow engine: `CrossoverV2Session.consume_capture` lost its last production caller in `7258864de` (ADR-0296, 2026-09-11), but the session object stayed live as a state bag the web run host reads (including the SPL-stop and driver-cap inputs). ~65 test files kept the dead tree green.
4. **Five other deletions stopped at the entry point** (R4): the WAV tone lane, the web session pause, the driver-check record's writers, the verify-route helpers, the jasper-null confirm loop — about 2.5k lines alive only through their own tests. Two more (R3): the staged-startup hold chain and the fan-in `ring_topology_ready` gate.
5. **145 refusals on the analysis → apply path: 84 honest data refusals, 20 non-negotiable, 32 nannies, 9 stacked** (R2). One apply gate (`boost_over_declared_bound`) could only fire on legacy files; level admission refused where it could clamp; prescription doors refused on evidence echoes, author fields and a self-referential lobe.
6. **Against REW** (R0, R1): no phase / impulse / group-delay / decay view, no general A−B compare (six special cases), no predicted-vs-measured overlay; the rear "auto-EQ" was an off-menu laptop script. Seven views read only legacy files; of those, **directivity and round-to-round repeatability are real diagnostics worth porting**, the other five are covered or dead.
7. **Three measurement executors** (R1-03/04): the daemon plan runner, `jasper-measure` (ADR-0296's one-release exception, lapsed) and `jasper-null`.
8. **Platform** (R5): one real bug (Spotify manual mode paste form had no CSRF token → always 403); `control/airplay_health.py` owns speaker-wide fan-in/Camilla evidence under an AirPlay name; the multiroom root oneshot doubles as a probe library; unit-name literals, `dbus-send` without child reaping, and a drifting truthy vocabulary duplicate owned primitives.

---

## 1. Scorecard

At the audited SHA (the run's scorecard script, posted on #5658; run from the repo root):

```
sha: 7111e55d0  date: 2026-09-22T22:21:51Z
tracked files: 2768
root tracked files: 15  root dirs: 14
jasper/ py lines: 353177   files: 879   flat top-level modules: 126
tests/ py lines: 499437   files: 951
docs md lines: 59962   files: 369   top-level docs: 26
product files > 1500 lines: 36
product py files > 1000 lines: 89
test files > 2000 lines: 28
function-local first-party imports without # lazy (excl cli/doctor): 504
duplicate private helper defs (finite/positive_int/sha256/fingerprint/state_path/read_json/as_int/coerce): 66
top-level plan docs (docs/*plan*.md): 5
docs/historical lines: 18651   docs/research lines: 11319
largest 12 product py files:
    3357 jasper/active_speaker/crossover_v2_flow.py
    2942 jasper/active_speaker/runtime_contract.py
    2168 jasper/active_speaker/graph/active_verifier.py
    2096 jasper/active_speaker/linearization_fit.py
    2067 jasper/volume_coordinator.py
    2062 jasper/multiroom/reconcile.py
    1921 jasper/wake_corpus/recording_backend.py
    1893 jasper/active_speaker/crossover_v2/verification.py
    1889 jasper/audio_hardware/reconcile.py
    1868 jasper/active_speaker/baseline_profile.py
    1857 jasper/cli/doctor/aec.py
    1771 jasper/control/airplay_health.py
```

On main after this run's merges (`856143354`; the figures also include the platform cleanup session's PRs of the same night, #5642):

```
sha: 856143354  date: 2026-09-23T15:21:44Z
tracked files: 2732
root tracked files: 15  root dirs: 14
jasper/ py lines: 320352   files: 851   flat top-level modules: 125
tests/ py lines: 456525   files: 941
docs md lines: 58629   files: 374   top-level docs: 25
product files > 1500 lines: 29
product py files > 1000 lines: 73
test files > 2000 lines: 24
function-local first-party imports without # lazy (excl cli/doctor): 349
duplicate private helper defs (finite/positive_int/sha256/fingerprint/state_path/read_json/as_int/coerce): 58
top-level plan docs (docs/*plan*.md): 4
docs/historical lines: 18651   docs/research lines: 11319
largest 12 product py files:
    2437 jasper/active_speaker/runtime_contract.py
    2193 jasper/active_speaker/graph/active_verifier.py
    2052 jasper/active_speaker/linearization_fit.py
    1968 jasper/volume_coordinator.py
    1966 jasper/wake_corpus/recording_backend.py
    1936 jasper/multiroom/reconcile.py
    1889 jasper/audio_hardware/reconcile.py
    1857 jasper/cli/doctor/aec.py
    1757 jasper/fanin/coupling_reconcile.py
    1744 jasper/mux.py
    1610 jasper/voice_daemon.py
    1533 jasper/audio_validation.py
```

## 2. The toolbox against REW (R0, R1)

REW's model: a measurement is one object with a stable id; every view is a pure function of (measurements, parameters) that shows its window/smoothing/band; predicted and measured never share a trace; averaging, trace arithmetic and alignment are explicit named operations; EQ is a document checked against a device profile; one machine-readable API describes everything.

| REW tool | JTS at `7111e55d0` |
|---|---|
| FR + phase | magnitude only; complex curves exist in take records, no phase view |
| impulse / step | none (`gate_impulse_response` internal) |
| group delay | internal to the feature classifier |
| decay / RT60 / waterfall / spectrogram | none (the seat `late_energy` ratio only) |
| distortion | yes (`distortion`, bass harmonics) |
| averaging | room median only |
| trace arithmetic A−B | six program-shaped special cases |
| alignment | four answers (packet `alignment`, `delay-landscape`, `jasper-null`/`delay-confirm`, `repeat --set`) |
| auto-EQ to target | `speaker-fit`, `bass-fit-table`; room authored with `--preview --vary`; rear = off-menu script |
| predicted vs measured | none |
| directivity | only through legacy-only `per-seat --include directivity` |

Target shape (R1 §F): layers measure → store → analyze → prescribe → compose → apply, one-way; objects round / set / take / candidate / document / layout; every view takes `--set`/`--take` and a round id or path; one answer envelope carrying `subject`, `parameters` (smoothing, window, band, calibration, reference) and a schema version per view; `jasper-round list|show` over one catalog owner; add `compare` (A−B and predicted-vs-measured), `impulse`, `group-delay`, `rear-fit`.

## 3. Findings and what happened to them

139 findings across the six reports. At the time of writing: **113 dispositioned** — landed in a merged PR, kept by an explicit decision, or handed to the platform cleanup session (#5642) — and **26 open**, filed as issues #5659–#5668 on #5658.

Landed (one row per PR; a finding can span PRs):

| PR | what landed | findings |
|---|---|---|
| #5582 | Carry the session CSRF token into Spotify's manual paste form | R5-01 |
| #5583 | Delete dead Wi-Fi scan paths and a never-flipped flag | R5-12 |
| #5584 | Delete the dead boost apply gate and tuning trial state | R2-F4, R2-F23 |
| #5585 | Hide the two duplicate measurement programs in the plan picker | R4-D9 |
| #5586 | Delete the stranded WAV-artifact tone lane | R4-D1, R4-S1 |
| #5587 | Clamp measurement levels instead of refusing; re-base calibrated seat anchors | R2-F1, R2-F2, R2-F3, R2-F18, R2-F19 |
| #5588 | Delete the standalone null-confirm timing loop (jasper-null, delay-confirm) | R1-04, R4-D4 |
| #5589 | Centralize shared systemd unit names in service_units | R5-06 |
| #5590 | Keep web/_common.py to shared primitives | R5-09, R5-11 |
| #5591 | Unify tuning CLI output on the shared contract; move document builders to their owner | R1-06, R1-07, R1-12, R1-19, R1-21, R2-F8, R2-F22 |
| #5592 | Delete the unused fan-in ring topology gate | R3-04 |
| #5593 | Delete the orphaned staged-startup hold chain | R3-03 |
| #5594 | Platform prose, a new-developer map, and a locked peering save | R5-11, R5-13, R5-14, R5-15, R5-17 |
| #5595 | Prescription doors disclose evidence echoes and lobe residuals instead of refusing | R2-F5, R2-F6, R2-F7 |
| #5596 | Remove obsolete DSP and playback name aliases | R4-D8 |
| #5597 | Fit verdict repeat spread from the round's own mark pairs (ADR-0341); group fit poses by both angles | R1-01 |
| #5598 | Recover a stranded session volume before measurement capture | R2-F10 |
| #5599 | Route shairport D-Bus calls through the leak-free busctl boundary | R5-07 |
| #5600 | Small tuning fixes: atomic ramp state write, import order, history comments | R3-16, R3-17, R3-20 |
| #5601 | Retire the v2 flow's dead capture consumer; expose the live run context | R3-01, R3-02, R3-13, R3-14, R3-15 |
| #5602 | Retire jasper-measure: one measurement path (ADR-0342) | R1-03 |
| #5603 | Room cut floor is a disclosure, not a refusal (ADR-0343) | R2-F13 |
| #5604 | Give the Camilla rate-storm detector its own control owner | R5-02 |
| #5605 | Fix stale comments in coupling_reconcile | R5-11 |
| #5606 | One catalog owner for banked rounds; jasper-round list\|show; round ids in views | R1-08, R1-09 |
| #5607 | Move read-side grouping probes out of the multiroom root oneshot | R5-03 |
| #5608 | One prescription-judge scaffold for alignment and topology | R4-C6, R4-P1 |
| #5609 | Port directivity and cross-round repeat onto current sets; delete the legacy-only views | R1-02 |
| #5610 | Wake-corpus recorder on the shared web seam: facade deleted, shared CSRF | R5-04, R5-05, R5-11 |
| #5611 | Tuning-zone path, hash and JSON I/O copies call their owners | R4-P2, R4-P5, R4-P9 |
| #5612 | One public owner for the power-mean dB, dBFS and floor primitives | R4-P8, R4-P10 |
| #5613 | Remove the tone lane's orphaned module and installer state directory | R4-D1 |
| #5614 | Split the output contract out of runtime_contract | R3-05 |
| #5615 | Capture views answer with the good captures and list the omitted ones | R2-F9 |
| #5616 | Docs outside the tuning guide point at the tools instead of restating them | R1-25, R1-26, R1-27, R1-28, R1-29, R1-31, R1-32, R1-33, R2-F12, R4-S2, R4-S5 |
| #5617 | Product modules stop hosting test-only code | R3-23, R4-D5, R4-D6, R4-S1 |
| #5618 | Delete the capture plan and spec's dead walk-shape, ui block and readers | R3-23, R4-D5, R4-D7, R4-S1 |
| #5619 | The crossover web door sheds its dead pause, tombstone and uncoded refusals | R2-F17, R2-F21, R2-F24, R4-D2, R4-S1, R5-10 |
| #5620 | Retire the write-dead driver-check record | R3-23, R4-D3 |
| #5621 | Dissolve web_commissioning into its owners | R3-22 |
| #5622 | Identical candidates are one candidate; a blend boost is refused first | R2-F15, R2-F28 |
| #5626 | repeat --set statistics live in the engine | R1-24 |
| #5627 | Delete the measurement paths the driver-check record left dead | R4-D3 |
| #5628 | The band registry names every fixed band | R4-C2 |
| #5630 | One name per number-parsing rule | R4-P3, R4-P4, R4-P7 |
| #5633 | Drop the lenient finite_float alias | R4-P3, R4-P7 |
| #5637 | One answer envelope for every round view (ADR-0344) | R1-08, R1-10 |
| #5639 | Delete the Gen A engine (step 2): cloud pipeline, planner, grading | R3-01, R3-08, R3-09, R3-10, R3-11, R3-12, R3-13, R3-14, R3-18, R3-19 |
| #5652 | Delete what Gen A left test-only; guard a live fit helper; honest door next actions | R3-12 |
| #5656 | Analysis views never write into a round's evidence (ADR-0346) | R1-11 |

Kept by decision: **R2-F16** — the apply pre-assert of the declared tweeter floor stays. It carries the specific refusal code the generic emit gate lacks, and the `applied_tune` pre-asserts guard the EQ-page path that never compiles. Enforcing one rule twice blocks nothing extra.

Handed to the platform session (#5642): R5-08, R5-16, R5-18, R5-19, R5-20, R5-21, R5-22, R2-F25, R4-D10.

Open, as issues: #5659 REW-parity views (R1-14, R1-15, R1-16, R1-17); #5660 one evidence packet (R1-13, R4-C5); #5661 one formula per figure (R4-C1, R4-C3, R4-C4); #5662 baseline_profile split (R3-06, R3-07); #5663 toolbox surface (R1-18, R1-20, R1-22, R1-23); #5664 hygiene (R4-P6, R4-S4, R4-S7); #5665 owner decisions (R1-05, R1-30/R4-S3, R2-F11, R2-F20, R2-F27); #5666 cuts outside a declared band (R2-F14); #5667 the delta probe's commanded-axis half (R2-F26); #5668 evidence-packet follow-ups found during the cleanup.

### The jts3 smoke test (#5632)

A parallel session ran the whole tuning program on jts3 on main `d2af284d4` (which already carried most of this run's PRs): speaker, rear, bass and room each went run → judge/compose → trial → apply end to end, and the original tune was restored byte for byte. Its 13 findings were fixed the same day: F1 capture overruns (#5634, #5640), F3 timing reset advice (#5654, ADR-0345), F4/F7 trial plan selection (#5635), F5/F9/F13 page (#5638), F6/F10 views (#5655), F8/F11 prescriber and hand-off (#5653); F2 anchor ambiguity is in PR #5657. The rest are listed on #5632.

## 4. Owner decisions taken during the run

| decision | ruling |
|---|---|
| jasper-null + delay-confirm (d16) | delete; `delay-landscape` stays as the prediction; confirmation = candidate variants + `jasper-round trial` |
| jasper-measure | retire (ADR-0342): one measurement path, `jasper-round run` / `trial` → `plan_run.run_plan`; one-spot compare = `--poses 0 --candidates base,A,B` |
| seven legacy-only views | port directivity and round-to-round repeatability onto current sets; drop the other five |
| fit verdict repeat spread | from the round's own mark pairs (ADR-0341); fit poses grouped by (horizontal, vertical) |
| dead driver-check record (`active_speaker_measurements.json`) | retire |
| room cut floor | disclosure, not refusal (ADR-0343) |
| builders | mid-run: stop dispatching Codex; Opus 5.5 / Sonnet builders; `/simplify` and `/code-review` on each PR |
| builders, later | the owner opted out of throttling for the shared plan budget; builders commit early so a limit hit is recoverable |
| zones | a second cleanup session took the platform zone (#5642); this run kept the tuning zone |
| timing take on a cardioid (ADR-0345) | the conductor's pick, owner may override: the summed timing take mutes the rear like it drops bass; consequence: the cardioid entry baseline measures the front drivers only (#5665 item 6) |

## 5. What only hardware or runtime can prove

- **Proved on jts3** (#5632, main `d2af284d4`): all four programs end to end through the browser and CLI loop, including the level clamps (#5587), the run-context surface (#5601), the door/CLI admission split (#5619) and the record retirement (#5620); the original tune restored byte for byte.
- **Owed — the #5632 re-run** once #5657 lands: count `capture_overrun`, `anchor_ambiguous` and `summed_sweep_heard` per take with the crossover page open and `jasper-round wait` running (#5634, #5640, #5657); the seats 2–3 placement card (#5638); a hand bass trial (#5635; the near-field pose may trip the 85 dB stop early, which is safe); `/proc/asound/UMIK2/pcm0c/sub0/hw_params` shows `buffer_size: 32768`.
- **Owed — a cardioid timing round** after #5654: the verdict reads `comparable`, the rear is silent in the timing take.

## Landing note (2026-09-23)

This run merged **56 PRs** (#5582–#5656) — **+11,356 / −85,751 lines, net −74,395** — including the Gen A engine deletion (#5601 + #5639, ~37k lines) and four ADRs: 0341 (fit repeat spread from the round's mark pairs), 0342 (one measurement path; `jasper-measure` retired), 0343 (the room cut floor is a disclosure), 0344 (one answer envelope for every round view), plus 0345 (timing comparability, #5654) and 0346 (analysis views never write a round's evidence, PR #5656). Reviews ran on every PR: `/code-review` for normal changes and the adversarial review for the non-negotiable tier (the SPL stop, graph doors, install, measurement-graph gains); reviewers caught real defects before merge in #5615, #5619, #5622, #5637, #5638, #5654, #5655 and #5656. Current state lives on #5658.
