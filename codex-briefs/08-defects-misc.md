# Brief 08 — Misc confirmed Python/bash defects

Mission: review §5.5 items 8, 11, 12 + appendix nits — small, isolated,
high-confidence fixes. Diagnose each against current main first; cite the
located cause in the PR. Group into ~3 PRs as below.

Branch: `codex/misc-defects`. File fence: `jasper/config.py`,
`jasper/control/server.py` (ONLY `_write_wake_threshold`/env-write helpers),
`jasper/web/wake_setup.py`, `deploy/bin/jasper-bootloop-guard` + the four
sibling `deploy/bin/` scripts sharing its arg-parse idiom,
`tests/voice_eval/harness.py`, `tests/voice_eval/tts.py`, plus tests.

## PR 1 — config parsing defects (jasper/config.py)

1. `server_vad_enabled` uses a negative-list parse that makes empty string,
   "off", and "disabled" all truthy → the knob FAILS OPEN on a stale-empty
   env value, unlike every other boolean. Route it through `_env_bool`
   (empty → default) and add the regression test for empty-string
   `JASPER_SERVER_VAD_ENABLED`. Check the negative-list values it previously
   accepted ("0"/"false"/"no"?) keep their meaning — behavior change only for
   junk/empty input; say so in the PR.
2. `google_setup_url` defaults to the literal `"http://jts.local/google"`
   while `spotify_setup_url` derives `f"http://{hostname}/spotify"` — renamed
   speakers get told the wrong URL by the doctor. Derive from hostname the
   same way (and reuse the hostname local in `google_redirect_uri` if that's
   what the sibling does). Update `tests/test_config.py` +
   `tests/test_env_example_matches_config_defaults.py` if the default is
   pinned there.
3. `_env_float`/`_env_int` raise bare ValueError without the env-var name
   (escapes to a raw traceback at daemon start); wrap as
   `RuntimeError(f"{name} must be a number") from e`, matching
   `_env_optional_float`. Test one case.

## PR 2 — wake_model.env dual-writer race + path literal

`jasper/web/wake_setup.py` (model save) and `jasper/control/server.py`
`_write_wake_threshold` both read-modify-write `/var/lib/jasper/wake_model.env`
with atomic rename but NO cross-process lock — concurrent saves lose one
writer's key. The repo already solved this class in `jasper/dsp_apply.py`
(fcntl.flock). Add a small shared helper (flock + read-modify-write + atomic
replace — `jasper/atomic_io.py` is the documented home) and use it from both
writers. Also: server.py duplicates the file-path literal that
`jasper/wake_models.py` exports — import the constant instead. Tests: two
interleaved writers preserve both keys (the dsp_apply flock test is the
template).

## PR 3 — bootloop-guard CLI bugs (+ siblings) and voice_eval artifact fixes

1. `deploy/bin/jasper-bootloop-guard` `--reason` with no value: `shift 2`
   fails without shifting and the while-loop spins forever on a pegged core
   (empirically confirmed). Guard: `[[ $# -ge 2 ]] || { usage >&2; exit 2; }`
   before every `shift 2`. The review says the same idiom exists in 4 sibling
   deploy/bin scripts — grep `shift 2` across `deploy/bin/` and fix every
   instance.
2. Same script uses `${var@Q}` (bash 4.4+) on its fail-open error paths —
   aborts with "bad substitution" on macOS bash 3.2, which the tests force via
   PATH, violating the exit-0 promise. Replace with `printf %q` or simple
   quoting. Run `pytest tests/test_bootloop_guard_script.py -q` on macOS —
   the previously-failing garbage-tuning-env test should now pass.
3. Marker hygiene from the same dive (cheap, same file): write the marker via
   the existing mktemp+mv pattern (it's the only non-atomic write in the
   script), and record daemon-reload failure in the marker (e.g.
   `reload_ok`) so /state and doctor can't report "tripped" when systemd
   never loaded the drop-ins. Extend the 15-test suite for both.
4. `tests/voice_eval/harness.py`: the drain-timeout path re-raises before
   `_write_transcript`, discarding the artifact its own docs tell you to read
   instead of re-running paid turns. Move transcript writing into a
   `finally`. And `tests/voice_eval/tts.py` writes the TTS cache
   non-atomically (a crash poisons the cache forever — any existing file is
   treated as a hit); write to a tempfile + `os.replace`, matching repo
   idiom. Both are testable without API keys (mock the connection /
   synth path); do NOT run any live eval.

## Acceptance

- `pytest tests/test_config.py tests/test_bootloop_guard_script.py
  tests/test_web_common.py tests/test_voice_eval_registry.py -q` green plus
  the new tests; `ruff check .`; `bash -n` + shellcheck (severity=warning) on
  every touched deploy/bin script.
- Each fix's PR section names the located cause (file:line, current main).
