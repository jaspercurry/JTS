# 2026-08 right-sizing refactor — execution plan

> **Campaign doc, not a reference.** Working tracker for the deletion
> refactor. Check boxes as waves land; **delete this file when the campaign
> ends** (its record is git history + the audit report). A fresh session
> picks up work by reading, in order: [AGENTS.md](../AGENTS.md),
> [ADR-0001](adr/0001-operating-model-reset.md),
> [DEEP-AUDIT-2026-08-25.md](DEEP-AUDIT-2026-08-25.md) §4–§6, then the next
> unchecked item here. Evidence for every deletion is in the audit's finding
> registry — paste the verifier's negative-proof into each deletion PR.

## Ownership boundaries

**Cloud session (this campaign):** everything EXCEPT the measurement
program. Explicitly owns all shared/contended surfaces: `AGENTS.md` (frozen
during the campaign), `README.md`, `CONTRIBUTING.md`, `docs/doc-map.toml`,
`scripts/ci-classify.py`, `tests/conftest.py` + shared test helpers,
`.github/`, `deploy/`, `scripts/`, `rust/`, `c/`, and all non-census
`jasper/` + `tests/` + `docs/`.

**Local measurement agent:** `jasper/active_speaker/`,
`jasper/audio_measurement/`, `jasper/correction/`, `jasper/bass_extension/`,
the correction/crossover web pages (`jasper/web/correction_*.py`) and tuning
CLIs (`jasper/cli/active_speaker*.py`, `seat_level.py`,
`correction_bundle.py`, `measurement_mic.py`, `crossover_prescriber.py`,
`driver_trim.py`, `angle_capture.py`, `arm_walk.py`, `noise_capture.py`,
`read_distortion.py`, `round_views.py`, `classify_features.py`), their test
files (prefixes `test_active_speaker*`, `test_audio_measurement*`,
`test_correction*`, `test_crossover*`, `test_bass_extension*`,
`test_seat_level*`, `test_spatial*`), **`tests/crossover_v2_fixtures.py`**
(no `test_` prefix but 1,948 lines / 26 importers — the subject of the
tuning plan's wave 1), `tests/test_lint_contracts.py` and
`tests/test_docs_handoff_freshness.py` (ceded — its plan's wave 7h owns
both), and the tuning docs (`HANDOFF-crossover-measurement-v2.md`,
`HANDOFF-correction*.md`, `tuning-master-plan.md`,
`measurement-loop-doctrine.md`, `llm-operator-runbook.md`, the wave/plan
docs). Its plan: `docs/REFACTOR-TUNING-2026-08.md` +
`docs/REFACTOR-COORDINATION-2026-08.md` (binding on both programs).

**Volume-surface widening (single-owner, for the duration of the tuning
plan's wave 5):** `volume_coordinator.py`, `camilla.py`'s fader/duck paths,
the fader-writer sites in `mux.py`, `control/volume_ops.py`,
`web/sound_setup.py`, `cli/aec_tune.py`, and the suites
`test_volume_coordinator.py`, `test_camilla_controller.py`,
`test_camilla_ducker.py` belong to the tuning program while that wave runs;
this campaign touches none of them without its ack. The duck machinery
(`GRAPH_SWAP_DUCK_DB`, the swap duck, any lease) is single-owner theirs
throughout.

**Census-zone handoff items for the local agent** (verified by the audit):
the `commissioning_capture_producer.py` orphan (1,235 + 854 test lines, its
HANDOFF still claims it live); ~30K prose lines in code; ~25K test-docstring
/ fixture-dedup lines; the `crossover_v2_flow.py` (13,459 lines) split its
own ratchet kept deferring; `jasper/bass_extension/` parked-half decision
rides issue #1738 (cloud defers it to the local agent since the package is
in its zone).

**Rules of engagement:** small single-concern PRs; `git fetch` before start
and before push; neither session edits the other's zone — if a change
requires it, make the smallest possible edit and flag it in the PR body, or
hand it across. The audit's **refuted list** (report §4.6) binds both: do
not merge web listeners, delete outputd or the Gemini adapter, merge the AEC
bridge, drop the loopback transport mid-campaign, collapse the health
systems, or unify settings files. Before acting on transport/duck items,
read the ~dozen core-seam contract tests the census filter swept in (audit
§2 coverage ledger). Standing protocols agreed 2026-08-25: after this
campaign's `rust/jasper-fanin` deletion lands, fanin edits freeze until the
5-case stereo tap re-runs on the bench, and the tuning plan's wave 6 waits
on that tap; `doc-map.toml` row-removals for tuning-zone docs ride the
tuning program's PRs with a notice line (structural doc-map changes stay
here); ADR numbers 0002–0099 are the tuning program's, 0100+ are this
campaign's; every shared-seam deletion needs the other program's explicit
ack, never silence.

## Cloud waves

**Wave 0 — correctness & safety (first, small PRs)**
- [x] CI matrix → the deployed interpreter only: `tests.yml` pytest matrix
      becomes `["3.13"]` (PiOS Trixie ships python3.13 only — see
      `deploy/lib/install/python-runtime.sh`) + the one classifier-test
      assertion that names the version list. Campaign speed measure,
      owner-directed; restoring the full matrix afterward is an owner call
      (record either way via ADR).
- [x] BRINGUP.md Phase 5 rewrite: profile-first AEC flow, `jasper-aec-commission`,
      replace both config-truncating `tee` recipes (BLOCKER; landed
      code-verified from the cloud lane — the Pi walk is still owed: run
      Phase 5 on the next spare-Pi commission, never jts3)
- [x] `install.sh` `install_nginx_site`: `return 1` on failed `nginx -t`
- [x] `User=` + hardening for jasper-correction-web / -bluetooth-web /
      -system-web units
- [x] `/wake-corpus/`: call `guard_mutating_host` inside `_check_csrf`
- [x] Delete the phantom `acquire_lock` knob through `dsp_apply.py` (13 signatures)

**Wave 0.5 — baseline instrumentation (report script, not CI gates)**
- [x] `scripts/right-size-report.sh`: comment-to-code ratio, test-vs-product
      LOC, dead-code scan (vulture / cargo lints / knip), per-zone; run and
      commit the baseline numbers so the diet is provable. The per-zone
      output is also the tuning program's net-lines evidence — one
      instrument, both programs. CI *budgets* come only at campaign end
      (research Stage 3), each via ADR.

**Wave 1 — verified dead code (~8.4K lines; one PR per row of audit §4.1)**
- [x] orbs.js bundle (+tests +CI step +css tokens)
- [x] multiroom/channel_split.py + always-None param chain
- [x] TtsPlayout PortAudio body collapse
- [x] five dead wizard main()s + 2 orphan console_scripts
- [x] wake_setup.py dead layer branches; aec3-spike code files;
      control/__main__.py; chip_aec dead fields; CaptureActivityProbe;
      dead voice-protocol members. Two audit claims REFUTED on
      re-verification — do not re-attempt: bass_alignment.py is consumed by
      tuning-zone contract tests (test_active_speaker_alignment_walk,
      test_audio_measurement_delay_graph) — its fate is handed to the tuning
      program with those tests; AudioOutChunk.kind is live
      (turn_playback → segment_kind → fanin SegmentKind AEC-reference
      accounting)

**Wave 2 — over-engineering (~9–12K)**
- [ ] rust host-compliance/prime machinery (host_compliance.rs + mixer/lane
      branches + 36 contract assertions)
- [ ] doctor never-fail consolidation (install-settings-drift family)
- [x] CI classifier shrink (~120-line path predicate + ~150-line tests)
- [x] env-migrations retirement (GATE: owner confirms every live Pi deployed
      since the migrations ran) — gate cleared 2026-08-25: fleet verified
      (all boxes past every candidate; the Aug-22 gate SHA was the
      extraction commit, not new logic); owner ruling: no legacy-box-support
      code in-tree, stale boxes are fix-forward
- [ ] nginx: proxy-header snippet + merge streambox conf
- [ ] wake-events WAV cap default 1 GiB → 128 MiB; move wake_training/ out of
      the shipped package

**Wave 3 — test right-sizing (~13–18K; lint-contracts ceded to the tuning
program's wave 7h)**
- [x] delete 962 redundant asyncio markers — repo-wide, and FIRST: lands
      before the tuning program's first wave so its rebases stay clean
- [ ] doctor tests → one file per domain, verdict + stable remediation codes
      (keep aec_probe isolation tests verbatim)
- [ ] CSRF altitude collapse via test_web_wizard_conventions sweep
- [ ] tests/js/_loader.mjs + _dom.mjs; convert the 12 strip-loader files
- [ ] convert the two worst literal-welded wiring test files to executing
      their subjects; ban new source-text assertions (charter default)

**Wave 4 — prose & docs (~10–20K in code + docs restructure)**
- [ ] Code comments, category-scoped only: superseded-value changelogs,
      self-labeled archaeology, reviewer-addressed notes; epitaph
      consolidation (rate_match / transport_pipe)
- [ ] HANDOFF triage, one PR per batch: each doc → keep-trimmed (<400-line
      operational spine) / decisions → ADR / appendix → docs/historical/ /
      delete. Census-zone docs belong to the local agent.
- [ ] README rebuild: repo-layout tree with all top-level dirs; atlas
      collapsed (historical entries one line)
- [ ] .env.example: one-line comments; AEC knob table → pointer
- [x] PULL_REQUEST_TEMPLATE: drop doc-impact ritual to match charter
- [ ] doc-freshness / docs-impact CI → link check only (keep doc-map pruned
      or fold)

**Wave 5 — structural convergence (opportunistic; only when the file is
already open)**
- ~~Duck lease~~ **HELD — superseded**: the tuning program deletes the
  swap-duck and the 1 Hz reconciler it coordinated with (its waves 5–6);
  no lease gets built · systemctl-through-broker or honest docstring ·
  sound_setup 502-helper (after the volume-surface widening lifts) ·
  `git mv` subway/citibike/bus into transit/providers/ · fold
  jasper/measurement/ (confirmed non-colliding: it serves `/balance/`) ·
  split test_control_server along its own handler boundary (after the
  widening lifts) · point the 4 hand-rolled atomic writers at atomic_io

**Wave 6 — one audio transport ([ADR-0100](adr/0100-one-audio-transport.md))**
- [ ] Delete the central loopback route + all transition machinery (~6–8K
      incl. tests and the 24 two-route docs): arm/disarm/recovery ladders,
      ring-confirm strikes, transport arbitration in coupling_reconcile /
      fanin_coupling / audio_runtime_plan, Rust transport branches, aloop
      confs. Keep the hard-park refusals (safety, not fallback). Composite
      (the dual-DAC box) parks loudly with the tracked issue number; jts3 is
      NOT that box (owner-confirmed), so nothing gates on the bench.
- [ ] Do NOT pre-fix the ring arming convergence bug — its habitat is the
      deleted ceremony (owner ruling). A surviving defect presents as a
      named park with `event=` logs; fix forward then.
- [ ] GATES: tuning program's explicit ack (ring = its measurement
      transport) + the 5-case stereo tap re-run after the fanin diff.
- [x] File the composite-on-ring tracked issue — #2982.

**Wave 7 — proven once, disclose on change ([ADR-0101](adr/0101-proven-once-disclose-on-change.md))**
- [ ] Recommission-gate demotion sweep (this campaign's zone: voice / mic /
      AEC / audio_validation): inventory every park or refusal triggered by
      staleness or unproven-ness rather than observed breakage; convert
      each to run-and-disclose (doctor warn naming what changed + the exact
      re-commission command). Chip-AEC-or-park → chip-AEC-or-disclose.
      Parks stay only on the non-negotiables.
- [ ] Hardware-class-keyed shipped proofs: commissioning artifacts bank
      against hardware class (extend the DacProfile registry pattern); a
      fresh install on recognized hardware starts from the shipped profile
      and discloses deviation.
- [ ] BRINGUP AEC section gets its second edit when the code flips (the
      Wave 0 edit documents today's behavior). Fold in: doctor's stale
      "Phase 2A.5" pointers (jasper/cli/doctor/aec.py:507, :1598 — no such
      phase exists) and BRINGUP:~955's Flex retune pointer to
      xvf_software_aec3 (intent-only on managed XVF).
- [ ] Notify the tuning program: its topology-fingerprint parks (#2935
      class) are re-adjudicated under ADR-0101 in its doctrine wave — its
      zone, its mechanics.

**Owner-decision gates (blocked until answered)**
- [ ] #1738: wire or delete bass-extension bench half (decision + execution
      owned by the tuning program; verifier's negative-proof rides its PR)
- [x] capture-relay proportionality — owner ruling 2026-08-25: KEEP;
      revisit at campaign end with the right-size report deltas in hand
- [x] experiments/usb-turntable — owner ruling 2026-08-25: anomaly ACCEPTED,
      no path move (it is the tuning program's robotic-arm driver; a move
      would force re-verification for zero behavior win). Its README now
      records the acceptance
- [x] env-migrations fleet confirmation (unblocks Wave 2 item) — answered
      2026-08-25, see Wave 2 line

## Driving PRs (the conductor's standing duties)

Subscribe to every PR you open (`subscribe_pr_activity`) and enable
auto-merge-on-green at creation — nobody waits between green and merge.
Drive-to-green: never end a CI-failure wake without a pushed fix or a
stated blocker. Exactly one re-run is legitimate: a job that died before
any test body ran (checkout, install, runner loss — e.g. the uv
manifest-fetch timeout) gets one `rerun_failed_jobs` after the run
completes; a second failure is real. Never re-run to get past a real
failure; never push empty commits to kick CI; never skip or quarantine a
test for green. Keep an hourly `send_later` check-in armed until every
open PR is merged or closed; re-arm silently when nothing changed. Keep
~3–5 PRs in flight, land oldest-first, rebase followers after each merge.

## Definition of done (every PR)
`scripts/test-fast` green (full lane in CI); docs PRs also pass
`scripts/docs-linkcheck.py`; deletion PRs carry the verifier's
negative-proof; after each wave touching runtime code, deploy to a spare Pi
— **never jts3**, which is the measurement bench holding a deliberate
`blocked/active_baseline_topology_changed` state (applying the bare
`55dee33aa48a` candidate would destroy the tournament winner's corrections);
use jts.local or jts5 — and run `jasper-doctor` + a music/voice smoke test.
Campaign ends when
Waves 0–4 and 6–7 are checked, the right-size report shows the deltas, and
this file is deleted.
