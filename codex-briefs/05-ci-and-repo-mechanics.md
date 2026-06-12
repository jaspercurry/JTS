# Brief 05 — CI hardening + repo mechanics

Mission: review §5.4 + Phase 0.7 — table-stakes signals an OSS launch gets
scored on (OpenSSF-visible), each under an hour.

Branch: `codex/ci-mechanics`. File fence: `.github/workflows/tests.yml`,
`.github/CODEOWNERS` (new), `CHANGELOG.md` (new), `docs/doc-map.toml` (only
if registering CHANGELOG is needed — check the orphan test's globs first).

⚠️ **Merge constraint:** PR 1 touches `.github/workflows/*` — a `gh` OAuth
token without `workflow` scope cannot merge it. Open it normally and tell the
maintainer to merge from the web UI (AGENTS.md PR-workflow rule 6). Don't burn
time retrying `gh pr merge`.

## PR 1 — tests.yml hardening (workflow-scope merge)

Preserve the existing why-comments (one is itself pinned by
`tests/test_doc_staleness_sweep_20260604.py` — do not reword the conftest
fixture credit at lines ~93-97). Add:

1. Top-level `permissions: contents: read` (the three sibling doc workflows
   already do this; tests.yml is the only one running with default grants).
2. `concurrency: group: ${{ github.workflow }}-${{ github.ref }}` with
   `cancel-in-progress: ${{ github.event_name == 'pull_request' }}` — main
   gets multiple force-pushes/hour and superseded runs currently pile up.
3. `timeout-minutes` per job: pytest ~20, rust ~15, shell ~5. The suite has
   documented hang-prone subprocess tests; the default 360-min hold on a
   required check is the failure mode.
4. Rust job: change `cargo build --release --locked` + `cargo test --locked`
   (which cold-compiles two profiles) to `cargo test --release --locked`, and
   add SHA-pinned `Swatinem/rust-cache` before the crate steps. Keep the
   three-crate structure and the lab-crate rot-gate comment.
5. New fast job `js`: `node --check` every `deploy/assets/**/js/*.js` and
   `tests/js/*.mjs`, then run the two node harnesses
   (`tests/js/sound_profile_harness.mjs`, and the dialog harness if present —
   check `tests/test_dialog_helper.py` for how it invokes node) plus
   `scripts/check-peq-parity.mjs`. 13k LOC of shipped JS currently has zero
   syntax gate.
6. Shell job: derive the file list from `git ls-files` + shebang filter
   instead of the hardcoded globs (which already miss
   `firmware/*/build.sh`), or at minimum add the two firmware build scripts
   and assert a minimum file count so a renamed dir can't silently empty a glob.
7. Optional, flag for maintainer decision in the PR body rather than deciding:
   a `python: [3.11, 3.13]` matrix on the pytest job (floor is 3.11,
   only 3.13 is exercised) — adds ~5 min CI; the alternative is raising
   `requires-python` to 3.13 to match the Pi.

## PR 2 — CODEOWNERS + CHANGELOG (normal merge)

1. `.github/CODEOWNERS`: one line, `* @jaspercurry`. Review routing for
   incoming contributors; trivially extensible later.
2. `CHANGELOG.md` at repo root: Keep-a-Changelog skeleton with an
   `## [Unreleased]` section seeded from the last week's headline merges (read
   `git log --oneline --since=2026-06-05` and summarize the 5-8 biggest:
   multiroom dataplane + grouping supervisor, outputd TTS lane via
   jasper-tts-protocol, transit registry rework, the 2026-06-12 review doc).
   Note in the header that v0.1.0 will be tagged at OSS launch (tagging is a
   maintainer action — do not create tags).
3. Root `*.md` files must be registered:
   `tests/test_docs_impact.py::test_root_and_top_level_docs_are_intentionally_mapped`
   globs `ROOT/*.md`. Add `CHANGELOG.md` to `docs/doc-map.toml` (the
   `security-oss-and-maintainer-backlog` subsystem's docs array, or a
   `classified_docs` entry if the map supports a session-artifact class —
   match how other root docs like QUICKSTART.md are mapped). Run the test
   before pushing.

## Acceptance

- `actionlint` clean if available, else careful YAML review; all jobs still
  green on the PR itself (the workflow change runs in its own PR).
- `pytest tests/test_docs_impact.py tests/test_doc_staleness_sweep_20260604.py -q`
  green (doc-map registration + the pinned CI comment untouched).
