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
`test_seat_level*`, `test_spatial*`), and the tuning docs
(`HANDOFF-crossover-measurement-v2.md`, `HANDOFF-correction*.md`,
`tuning-master-plan.md`, `measurement-loop-doctrine.md`,
`llm-operator-runbook.md`, the wave/plan docs). Its detailed plan is its
own; owner shares the boundary overview when defined.

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
§2 coverage ledger).

## Cloud waves

**Wave 0 — correctness & safety (first, small PRs)**
- [ ] BRINGUP.md Phase 5 rewrite: profile-first AEC flow, `jasper-aec-commission`,
      replace both config-truncating `tee` recipes (BLOCKER; validate on a Pi)
- [ ] `install.sh` `install_nginx_site`: `return 1` on failed `nginx -t`
- [ ] `User=` + hardening for jasper-correction-web / -bluetooth-web /
      -system-web units
- [ ] `/wake-corpus/`: call `guard_mutating_host` inside `_check_csrf`
- [ ] Delete the phantom `acquire_lock` knob through `dsp_apply.py` (13 signatures)

**Wave 0.5 — baseline instrumentation (report script, not CI gates)**
- [ ] `scripts/right-size-report.sh`: comment-to-code ratio, test-vs-product
      LOC, dead-code scan (vulture / cargo lints / knip), per-zone; run and
      commit the baseline numbers so the diet is provable. CI *budgets* come
      only at campaign end (research Stage 3), each via ADR.

**Wave 1 — verified dead code (~8.4K lines; one PR per row of audit §4.1)**
- [ ] orbs.js bundle (+tests +CI step +css tokens)
- [ ] multiroom/channel_split.py + always-None param chain
- [ ] TtsPlayout PortAudio body collapse
- [ ] five dead wizard main()s + 2 orphan console_scripts
- [ ] wake_setup.py dead layer branches; bass_alignment.py; aec3-spike code
      files; control/__main__.py; chip_aec dead fields; CaptureActivityProbe;
      dead voice-protocol members

**Wave 2 — over-engineering (~9–12K)**
- [ ] rust host-compliance/prime machinery (host_compliance.rs + mixer/lane
      branches + 36 contract assertions)
- [ ] doctor never-fail consolidation (install-settings-drift family)
- [ ] CI classifier shrink (~120-line path predicate + ~150-line tests)
- [ ] env-migrations retirement (GATE: owner confirms every live Pi deployed
      since the migrations ran)
- [ ] nginx: proxy-header snippet + merge streambox conf
- [ ] wake-events WAV cap default 1 GiB → 128 MiB; move wake_training/ out of
      the shipped package

**Wave 3 — test right-sizing (~15–20K)**
- [ ] test_lint_contracts.py: keep 8 tests + constants, strip ~1,700 comment
      lines
- [ ] doctor tests → one file per domain, verdict + stable remediation codes
      (keep aec_probe isolation tests verbatim)
- [ ] CSRF altitude collapse via test_web_wizard_conventions sweep
- [ ] tests/js/_loader.mjs + _dom.mjs; convert the 12 strip-loader files
- [ ] delete 962 redundant asyncio markers
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
- [ ] PULL_REQUEST_TEMPLATE: drop doc-impact ritual to match charter
- [ ] doc-freshness / docs-impact CI → link check only (keep doc-map pruned
      or fold)

**Wave 5 — structural convergence (opportunistic; only when the file is
already open)**
- Duck lease replacing GRAPH_SWAP_DUCK_DB carve-out · systemctl-through-
  broker or honest docstring · sound_setup 502-helper · `git mv`
  subway/citibike/bus into transit/providers/ · fold jasper/measurement/ ·
  split test_control_server along its own handler boundary · point the 4
  hand-rolled atomic writers at atomic_io

**Owner-decision gates (blocked until answered)**
- [ ] #1738: wire or delete bass-extension bench half (→ local agent's zone)
- [ ] capture-relay proportionality (~27K lines for phone-mic calibration)
- [ ] experiments/usb-turntable: promote into jasper/ or accept the anomaly
- [ ] env-migrations fleet confirmation (unblocks Wave 2 item)

## Definition of done (every PR)
`scripts/test-fast` green (full lane in CI); docs PRs also pass
`scripts/docs-linkcheck.py`; deletion PRs carry the verifier's
negative-proof; after each wave touching runtime code, deploy to a spare Pi
and run `jasper-doctor` + a music/voice smoke test. Campaign ends when
Waves 0–4 are checked, the right-size report shows the deltas, and this
file is deleted.
