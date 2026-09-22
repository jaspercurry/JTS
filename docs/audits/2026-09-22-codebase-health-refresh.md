# 2026-09-22 codebase-health refresh

Frozen report of one audit run (ADR-0284). Audited SHA: `3ebdd1b7f` (origin/main
at 05:25 UTC, 2026-09-22). Written once; disposition of every finding lives in
GitHub issues labelled `audit` and `audit-2026-09-22`, tracked on the tracking
issue named in `docs/audits/README.md`.

## 1. Headline

Three prior audits (08-25, 09-05, 09-09) and the 09-11 paydown lane already
removed most dead code and converged most duplicated primitives. What this
refresh found at `3ebdd1b7f`:

- **The remaining structural debt is ownership, not volume.** Nine platform
  modules directly under `jasper/` (2,590 → 972 lines each) hold two to five
  concerns apiece; the concerns have callers of their own and, in four cases,
  an existing owner elsewhere. Splits by ownership are mechanical for
  `ring_assets` (binary ABI vs ALSA text), `output_hardware` (record vs
  acquisition), `audio_validation` (stored evidence vs live collection) and
  `source_intent` (unprivileged client vs root reconciler). Splits by size
  alone (`volume_coordinator`, `config`, `camilla`, `atomic_io`) would only
  move lines and were rejected with evidence.
- **The reconciler family does not want a shared skeleton.** Five reconcilers
  (AEC, audio-hardware, fan-in coupling, multiroom, source-intent) share a
  purpose but not a state machine; the only copied calculation is broker
  timeout arithmetic (+15/−45). The 2,268-line AEC reconciler is Bash outside
  the package; a package-owned pass is real work (+900/−1,700) that needs
  hardware.
- **Duplicate primitives are largely paid down.** Owners exist
  (`json_fields`, `atomic_io`, `paths`, `service_units`, `config.env_bool`);
  four small copies remain outside the held zones. Most function-local
  imports in the largest runtime files guard real startup/memory boundaries
  (wizard branches, actuator construction, route probes), not cargo cult.
- **Four reproduced behavior faults** hid in files that "earned their keep":
  `PREEMPT airplay` did no cleanup; an unreadable correction config was
  reported as zero-delay correction; the AirPlay connect grace discarded
  CamillaDSP evidence; the AEC doctor labelled unattenuated activity "idle".
- **The repository root** carried operator manuals, community policies and a
  changelog beside the build contracts, with readers pinning exact root paths
  and a documentation map that had grown a mandatory classification layer.
  `docs/historical/` and `docs/research/` are dated evidence cited by ADRs,
  not a handoff tier, and stay.
- **Held zones** (measurement/tuning, voice while its PRs were open) were
  inventoried but not worked: two test-only measurement modules
  (`commissioning_capture.py`, `crossover_v2/proposal.py`) and 52 duplicate
  helper definitions belong to that owner.

## 2. Method

Five read-only GPT-6 (Codex) investigators, one tile each, dispatched from a
pinned checkout with the same preamble (evidence before judgment; verify every
"done" against the tree; check registries, entry points, systemd, udev, CI and
`importlib`/`getattr` before calling anything dead; name the larger issue each
finding is a symptom of; no lint, gate or guard test as a fix). Their full
reports are attached to the tracking issue.

| tile | scope | opened |
|---|---|---|
| I1 | repository root, `docs/` top level, `historical/`, `research/`, small top-level dirs, `.github/`, `.claude/` | 93 direct subjects in full; 77 historical/research files by head + inbound references |
| I2 | nine platform modules under `jasper/` + `camilla.py` (hearing, report only) | 17,640 lines in full; 14 test files mapped |
| I3 | duplicate private helpers, repeated literals, function-local imports (repo-wide inventory; builds outside held zones) | AST inventory of 1,810 files; 101 definitions (52 held); 297 non-test local-import sites |
| I4 | reconciler family, `deploy/bin`, `deploy/systemd`, installer libraries, `control/airplay_health`, `wake_corpus/recording_backend`, `cli/doctor/aec` | 13 long files in full (~23k lines); 24/24 bin scripts and 68/68 units traced to an install path |
| I5 | cartography: orphans, dead flags, vanished-subject tests, unreferenced assets, doc/tree drift, debt markers | whole tracked tree (2,695 files); 5,859 test imports; 685 debt-marker lines |

Not run: hardware, paid tests, write-producing lanes. The conductor built,
reviewed and merged fixes the same night; §5 records what landed by the time
this report was frozen, and nothing else.

## 3. Scorecard at the audited SHA

```
sha: 3ebdd1b7f  date: 2026-09-22T05:38:29Z
tracked files: 2695
root tracked files: 21  root dirs: 15
jasper/ py lines: 353721   files: 816   flat top-level modules: 119
tests/ py lines: 503234   files: 944
docs md lines: 58129   files: 355   top-level docs: 23
product files > 1500 lines: 45
product py files > 1000 lines: 95
test files > 2000 lines: 31
function-local first-party imports without # lazy (excl cli/doctor): 563
duplicate private helper defs (finite/positive_int/sha256/fingerprint/state_path/read_json/as_int/coerce): 71
top-level plan docs (docs/*plan*.md): 6
docs/historical lines: 18649   docs/research lines: 11176
largest 12 product py files:
    5073 jasper/active_speaker/runtime_contract.py
    3960 jasper/active_speaker/camilla_yaml.py
    3465 jasper/active_speaker/crossover_v2_flow.py
    2715 jasper/active_speaker/crossover_v2/evidence_packet.py
    2590 jasper/volume_coordinator.py
    2423 jasper/active_speaker/crossover_v2/feature_classifier.py
    2173 jasper/audio_validation.py
    2112 jasper/active_speaker/crossover_v2/spatial.py
    2096 jasper/active_speaker/linearization_fit.py
    2093 jasper/output_topology.py
    2086 jasper/multiroom/reconcile.py
    2085 jasper/active_speaker/staging.py
```

(`scripts` used: the conductor's scorecard over `git ls-files`; the same script is re-run in the landing note.)

## 4. Findings

Severity uses the playbook taxonomy. "Disposition" is the state when this
report was frozen; the live state is the issue.

Full evidence for every row is in the investigator reports attached to the tracking issue.

| id | severity | where | finding | larger issue | disposition at freeze |
|---|---|---|---|---|---|
| I2-01 | Should-fix (reproduced) | `jasper/mux.py` `_control_preempt` | `PREEMPT airplay` called `_pause(AIRPLAY)`, which has no AirPlay branch, and reported success | one renderer, two cleanup paths | fixed, #5506 |
| I2-02 | Should-fix (reproduced) | `jasper/audio_runtime_plan.py` correction evaluator | unreadable supplied config → eligible, min-phase, delay 0 | missing evidence became a positive latency claim | fixed, #5504 |
| I2-03 | Should-fix | `jasper/volume_push_sources.py` | helpers took the whole coordinator and mutated private echo state | a split on disk, not in the contract | fixed, #5505 |
| I2-04 | Should-fix (hearing) | `jasper/output_topology.py` strict vs snapshot loaders | two owners of one admission rule | duplicated policy on a fail-closed path | PR #5522 (adversarial review: sound) |
| I2-05 | Should-fix | `jasper/ring_assets.py` (1,700) | ALSA text and binary ring formats share one owner | two file formats, one module | stage 1 PR #5518; stages 2–3 #5515 |
| I2-06 | Should-fix | `jasper/audio_validation.py` (2,173) | reading stored evidence imports live validation machinery | storage and collection share an owner | building (artifacts split) |
| I2-07 | Should-fix | `jasper/source_intent.py` (1,686) | unprivileged client and root reconciler in one module | missing privilege boundary | built; review pending |
| I2-08 | Should-fix | `jasper/output_hardware.py` (1,389) | classification owns Linux probing | observation vs acquisition | built; review pending |
| I2-09 | Should-fix | `jasper/audio_runtime_plan.py` (2,037) | setting policy embedded in plan assembly | primitive policy has no owner | building |
| I2-10 | Nit | `jasper/config.py` 589–609, 668–677 | rollout narration; PortAudio/dmix comments for a fan-in IPC path | comments as history | building (prose job) |
| I2-11/12 | earns its keep | `jasper/camilla.py`, `jasper/config.py` | hearing boundary intact; one composition root | — | keep |
| I3-01..04 | Should-fix / Nit | finite parsers, `wake_training` sha256, `enhanced_aec` JSON publication, deploy SSH options | four copies of owned primitives | drift between copies | built; review pending |
| I3-05..07 | Nit / earns its keep | function-local imports in the largest runtime files | most guard real startup/memory boundaries | — | keep; add trailing reasons when touched |
| I3-08 | Should-fix | `jasper/tools/spotify.py` ↔ `tools/transport.py` | peer tool modules own each other's work | — | building (prose job, commit 2) |
| I3-10 | — | `jasper/atomic_io.py` ↔ `env_file.py` | parser/writer cycle; no size split warranted | secrets-path caller migration | #5513 |
| I3-11 | Nit (hearing) | `/var/lib/camilladsp/configs` literal | several owners | — | #5513 |
| I4-01 | Should-fix | `jasper/audio_hardware/reconcile.py` env lock | failed lock still publishes | — | **by design**, ADR-0235 R7 |
| I4-02 | Should-fix | `jasper/control/airplay_health.py` | connect grace discarded Camilla evidence | source-specific rule over speaker-wide evidence | fixed, #5503 |
| I4-03 | Should-fix | `jasper/cli/doctor/aec.py` | unattenuated activity reported as idle | activity and quality as one fact | PR #5524 |
| I4-04/08/13 | Should-fix | `jasper/wake_corpus/recording_backend.py` | retry keyed on error prose; test-only wrappers; false import rationale | no structured error identity | fixed, #5501 |
| I4-05/11/12 | Should-fix (deploy) | `deploy/lib/install/systemd-units.sh` | staging performs live changes; duplicated lists and copy blocks | publication vs activation | #5509 |
| I4-06 | Should-fix | `deploy/bin/jasper-aec-reconcile` (2,268 lines of Bash) | product orchestration outside the package | — | #5510 |
| I4-07 | owner | `deploy/install.sh` 308–334 | second acceptance for an explicit profile change | duplicate consent | #5511 |
| I4-14 | small | broker timeout arithmetic in two reconcilers | one calculation, three homes | — | building |
| I4 family | earns its keep | the five reconcilers | no shared skeleton wanted | — | recorded |
| I5-01..04 | Should-fix | `Config.duck_db`, `tests/volume_coordinator_trace.py`, `docs/testing-tooling.md`, aec3 spike README | unread field, orphan harness, stale names, runbook drift | — | fixed, #5507 |
| I5-05/06 | Nit | `.env.example` history blocks; BRINGUP 1 Hz claim | seed file as changelog | — | built (root/docs branch) |
| I5-07/08 | Should-fix | `active_speaker/commissioning_capture.py`, `crossover_v2/proposal.py` | test-only product modules in the held zone | — | #5514 |
| I5 | earns its keep | 24/24 bin scripts, 68/68 units, all assets, every test import resolve | — | — | recorded |
| I1-01 | Should-fix (deploy) | `BRINGUP.md` | a second, Pi-local install recipe | two owners of deployment instructions | built (root/docs branch) |
| I1-02 | Should-fix | `QUICKSTART.md`, onboarding command | streambox assistant claims drifted | repeated capability prose | built (root/docs branch) |
| I1-03 | Should-fix | `docs/doc-map.toml` + `scripts/docs-impact.py` | classification layer enforced registration, not truth; copied command lists | routing table as second process owner | built (root/docs branch) |
| I1-04/08 | Should-fix / Nit | conversation-history and tool-platform plans; deep-audit command | plans carrying landed work; duplicated audit rules | — | #5516; prose job |
| I1-05 | Nit (owner's ask) | repository root | six operator/community documents beside build contracts | — | built (root/docs branch, ADR-0334) |
| I1-06 | Should-fix (deploy) | `experiments/usb-turntable/` | production device control under `experiments/` | — | #5512 |
| I1-09/10 | earns its keep | `docs/historical/`, `docs/research/`, `wake_training/`, `release/`, `jasper_aec3/` | dated evidence and real separate owners | — | keep |
| review find | — | `jasper/output_topology.py` `from_mapping` | three malformed shapes bypass the typed refusal | — | #5523 |


## 5. Landing note (written once)

Written once on 2026-09-22 when the audit night closed; not maintained. Live state: #5521 and the issues it lists.

**Landed by the time this report was frozen** (all built by GPT-6 Astra (Codex) from Fable briefs, reviewed by a Claude finder pass, merged on green CI):

| PR | change | finding |
|---|---|---|
| #5501 | recording stop failures typed; test-only wrappers and false import rationale removed | I4-04/08/13 |
| #5503 | AirPlay connect grace scoped to shairport evidence (fan-in suppression kept after review) | I4-02 |
| #5504 | unreadable correction config reports unknown evidence, not zero-delay correction | I2-02 |
| #5505 | push-source helpers take their inputs; coordinator stamps success | I2-03 |
| #5506 | `PREEMPT airplay` releases the session through the existing owner | I2-01 |
| #5507 | `Config.duck_db`, an orphan test harness, stale tooling names, the aec3 spike README | I5-01..04 |
| #5518 | ring binary header/ABI → `ring_header.py` (stage 1 of the `ring_assets` split) | I2-05 |
| #5519 | `StationInfo` alias and retired AEC poll-rate prose | #5061 queue |
| #5522 | one strict topology reader (hearing tier; Opus adversarial pass: sound) | I2-04 |
| #5524 | AEC doctor separates activity from attenuation | I4-03 |
| #5525 | repository root: six documents out, doc map reduced to routing, setup prose corrected, ADR-0334 | I1-01/02/03/05, I5-05/06 |
| #5527 | four primitive copies converge on `json_fields` / `atomic_io` / `_lib.sh` | I3-01..04 |
| #5528 | output-hardware probing → `audio_hardware/output_probe.py` | I2-08 |
| #5533 | broker timeout bound owned by `restart_broker` | I4-14 |
| #5534 | stale rollout/restore/RTTIME prose deleted; deep-audit command deduped; Spotify tool reads now-playing from the renderer owner | I2-10, I4-09/10, I1-08, I3-08 |
| #5536 | validation artifact storage → `audio_validation_artifacts.py` (2,173 → 1,534 + 716) | I2-06 |
| #5529 | root source reconciliation → `local_sources/reconcile.py` (1,686 → ~550 + 1,122) | I2-07 |
| #5537 | runtime setting policy → `audio_runtime_settings.py` (2,037 → 1,339 + 769) | I2-09 |

**Scorecard after the last merge** (same script as §3):

```
sha: 19bc6b7f6  date: 2026-09-22T07:46:44Z
tracked files: 2704
root tracked files: 15  root dirs: 14
jasper/ py lines: 353325   files: 826   flat top-level modules: 122
tests/ py lines: 502640   files: 945
docs md lines: 60181   files: 363   top-level docs: 27
product files > 1500 lines: 43
product py files > 1000 lines: 95
test files > 2000 lines: 30
function-local first-party imports without # lazy (excl cli/doctor): 565
duplicate private helper defs (finite/positive_int/sha256/fingerprint/state_path/read_json/as_int/coerce): 69
top-level plan docs (docs/*plan*.md): 6
docs/historical lines: 18651   docs/research lines: 11176
largest 12 product py files:
    5073 jasper/active_speaker/runtime_contract.py
    3960 jasper/active_speaker/camilla_yaml.py
    3465 jasper/active_speaker/crossover_v2_flow.py
    2713 jasper/active_speaker/crossover_v2/evidence_packet.py
    2587 jasper/volume_coordinator.py
    2423 jasper/active_speaker/crossover_v2/feature_classifier.py
    2112 jasper/active_speaker/crossover_v2/spatial.py
    2096 jasper/active_speaker/linearization_fit.py
    2085 jasper/active_speaker/staging.py
    2068 jasper/output_topology.py
    2064 jasper/multiroom/reconcile.py
    1921 jasper/wake_corpus/recording_backend.py
```

**Process facts worth keeping:** the Codex sandbox can never commit (git's index lock), so the conductor commits every build; the companion's app-server broker is one shared process — killing it kills every in-flight job; thirteen concurrent sandbox test lanes drove the laptop to load 500, after which CI was the only trustworthy gate. One finding (I4-01) was stopped by the builder itself because ADR-0235 R7 documents the behaviour as deliberate.


## 6. What only hardware or runtime can prove

- ALSA devices open as each unit's real `User=` with the declared formats.
- `PREEMPT airplay` releases a live shairport session before Spotify starts.
- Ring observations match live writer/reader behaviour through restart, stall
  and recovery; header/ABI moves are byte-identical on the Pi.
- Turntable discovery, hot-plug STOP, port lock and parking after any path move.
- AEC reference delivery, chip timing and wake response across unplug,
  restart and commissioning; whether coordinated fan-in restarts still buy
  an audible benefit after the C reader's pacing fix.
- Which retired files and env keys remain on each deployed Pi (retirements,
  legacy seeds), and whether mixed daemon generations still need the old
  response fallbacks.
- RSS and startup cost on the 415 MB target after import changes.
