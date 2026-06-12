# Brief 03 — Fix the contributor front door

Mission: `docs/REVIEW-2026-06-12-oss-due-diligence.md` §4.3 — the documented
quick start fails on first contact for every new contributor. All items
adversarially verified.

Branch: `codex/front-door`. File fence: `CONTRIBUTING.md`, `pyproject.toml`,
`uv.lock` (new), `jasper/cli/spotify_auth.py`, new tests under `tests/`.
Do NOT touch AGENTS.md/README (brief 06 owns those).

## PR 1 — make `uv sync` actually produce a test-capable venv

- Today pytest/ruff live only in `[project.optional-dependencies].dev`
  (pyproject.toml ~101-106); `uv sync` installs no extras, so
  CONTRIBUTING's `uv sync && .venv/bin/pytest` fails.
- Fix: add a PEP 735 `[dependency-groups]` `dev` group (uv installs the `dev`
  group by default on `uv sync`). Keep the existing `dev` extra for pip users
  (`pip install -e '.[dev]'` — CI uses this). Add a drift-guard test in the
  repo's house style pinning the group and the extra to the same list, so they
  can't diverge.
- Commit `uv.lock` (run `uv lock`). Mention in CONTRIBUTING that the lockfile
  is canonical for dev envs.

## PR 2 — Linux-only C deps must not break macOS installs

- `pyproject.toml:58` `pyalsaaudio>=0.11` and `:74` `evdev>=1.7` are
  Linux-only C extensions with no environment markers; the macOS path
  CONTRIBUTING explicitly courts cannot build them.
- Add `; sys_platform == 'linux'` markers to both. Then grep all import sites
  (`import alsaaudio`, `import evdev`/`from evdev`) and confirm each is
  lazy/guarded so the suite passes without them — they should already be
  (verify, don't assume; fix any module-top import you find with the repo's
  lazy-import idiom and a comment naming the platform constraint).
- Update `uv.lock`. Note in the PR that Linux CI (`pip install -e '.[dev]'` on
  ubuntu) still installs both, so coverage of the real imports is unchanged.

## PR 3 — fix the shipped-broken `jasper-spotify-auth` CLI + smoke test

- `jasper/cli/spotify_auth.py:25` does
  `from ..tools.spotify import SPOTIFY_SCOPE`, but that symbol moved to
  `jasper/spotify_router.py:43` on 2026-05-05 — every invocation of the
  console script ImportErrors. Fix the import (`from ..spotify_router import
  SPOTIFY_SCOPE`) and run the module to confirm it at least reaches argparse.
- Add `tests/test_console_scripts_import.py`: parse `[project.scripts]` from
  pyproject.toml and `importlib.import_module()` every entry-point module,
  asserting the named attribute exists. This pins the whole bug class (a
  console script that can't import) hardware-free.

## PR 4 — CONTRIBUTING.md accuracy pass (this brief owns the file)

- Quick start: `uv sync` (now correct), note the Linux `apt` system deps CI
  installs (`portaudio19-dev`, the `openwakeword --no-deps` dance) for
  contributors who want the audio extras, per .github/workflows/tests.yml.
- Replace the stale "~1000 tests across 93 files in under a minute" (appears
  twice, lines ~41 and ~91) with non-rotting phrasing ("the full
  hardware-free suite — thousands of tests") or pin with a guard test.
- Branch protection: required checks are `pytest` AND `rust` (tests.yml:109);
  fix the prose and the emergency-override JSON snippet (its `contexts` array
  currently drops `rust`). Add two bullets to the test matrix: cargo
  build/test gates `rust/` crates; `bash -n` + shellcheck gate shell entry
  points. Ask the maintainer in the PR description to confirm the live
  branch-protection rule matches.
- Fix the two dangling prose pointers: PLAN.md "What comes after v1" (heading
  gone) and AGENTS.md "AEC bridge — reconciler toggle" (now "AEC bridge —
  input profile and reconciler"). Convert both to real markdown links so the
  docs-links CI guards them. Either delete the `ruff format` instruction or
  leave a TODO referencing the deferred tree-wide format decision — do not
  run a 600-file reformat.

## Acceptance

- Fresh-clone simulation: `uv sync && .venv/bin/pytest tests/test_config.py -q`
  works on macOS (no pyalsaaudio/evdev build attempts).
- New smoke test fails if any console-script import breaks again.
- `ruff check .` clean; `pytest tests/test_console_scripts_import.py
  tests/test_env_example_matches_config_defaults.py -q` green.
