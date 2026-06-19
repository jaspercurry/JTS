# JTS Review — Small Wins (Bugs, Quick Fixes, Doc Staleness, Hygiene)

> **Status: superseded (2026-06-18).** Point-in-time audit snapshot — several
> items here have since shipped (notably the daemon privilege separation and the
> OSS governance docs). **Do not drive work from this doc.** The current,
> verified launch-readiness backlog is
> [LAUNCH-READINESS.md](LAUNCH-READINESS.md). Preserved for archaeology (the
> audit reasoning), not current operational truth.

> Companion to [REVIEW-2026-06-04-deep-dive.md](REVIEW-2026-06-04-deep-dive.md).
> These are the contained, high-value-per-effort items: confirmed bugs,
> security/governance quick wins, hygiene infrastructure, and the
> documentation-staleness cluster. Almost all are `quick` effort. Each has
> file:line evidence from the multi-agent review.

---

## Confirmed bugs

All three high-severity bug claims were re-checked by an adversarial
verifier reading the actual code. **All confirmed.** Severities below are
the verifier's post-review assessment.

### 1. `_end_turn` re-entrancy race (Medium, was High)
`jasper/voice_daemon.py:3146,3232,2719,1966`. `_end_turn` runs many awaits
(telemetry, peering notify, bg-task join, `end_input` with 2 s timeout,
release, chirp, duck-restore) and only flips `self._state = State.WAKE` at
its **last** line, clearing `_session_id` at `:3301-3302`. The
control-socket `mute_mic` handler and the main mic loop's
`_handle_session_frame` both call `_end_turn()` guarded only by
`_state is State.SESSION` / a done bg-task — with no re-check. Because state
stays `SESSION` across the whole (potentially multi-second) teardown, a mute
arriving in that window re-enters `_end_turn` and hits
`assert _session_id is not None` (now cleared). `mute_mic` swallows the
AssertionError; the main-loop path does **not** (`:1738` is unwrapped), so
that ordering crashes the daemon (systemd recovers, but the session drops +
a corrupted usage row). No `_ending` flag / lock exists.
**Fix:** flip state to an intermediate/`WAKE` at the *top* of `_end_turn`
before the first await, or early-return idempotency
(`if _state is not SESSION or _turn is None: return`). Add a regression
test driving `mute_mic` concurrently with a bg-task-done session frame.

### 2. `jasper-cues play <slug>` → `NameError` (Medium, was High)
`jasper/cues/cli.py:166-167`. `_cmd_play` calls `_env("JASPER_CONTROL_HOST"…)`
and `_env("JASPER_CONTROL_PORT"…)`; `_env` is never defined or imported
(the only `os` is a `import os as _os` local to a *different* function). Any
valid slug hits `NameError` right after the `find_cue()` guard. Latent since
2026-05-07; the sole test exits early on the unknown-slug path so the happy
path is uncovered. It's an operator/install diagnostic CLI (not the live
daemon cue path), but it violates the project's own "no silent failure"
bar. **Fix:** add module-level `import os`, replace `_env(...)` with
`os.environ.get(...)`, add a play-path regression test against a mocked
control endpoint.

### 3. `JASPER_WAKE_THRESHOLD` disagrees three ways (Low, was High)
`config.py:451` & `wake_setup.py:112` & AGENTS.md say **0.50**;
`.env.example:115` ships **0.30** with a comment calling it the
"empirically-tuned production value." `install.sh:1587` copies
`.env.example` into `jasper.env`, chained as the *first* `EnvironmentFile`,
so the 0.30 literal wins and the 0.50 code default is unreachable in
production. Behavior is the *intended* 0.30 (and the `/wake/` UI shows the
true value) — only engineers reading code/docs and dev/test envs (which
fall back to 0.50) are misled. **Fix:** reconcile to one canonical value
(most likely bump `config.py` + `wake_setup.py` + AGENTS.md to 0.30 since
the comment marks the code default as the stale one); add a test asserting
`.env.example` matches the `config.py` default for load-bearing knobs.

### Other real bugs (not in the high-verify set)
- **Gemini GoAway tears down the in-flight turn** ignoring `time_left`
  (`gemini_session.py:1343`) — see big-rocks §3.
- **Grok spend cap inoperative** — `ConnectionUptimeMeter` is never wired
  into the Grok construction path, so the token-based daily cap under-counts
  Grok's flat $3/hr billing; AGENTS.md claims it's metered by uptime but the
  meter isn't attached (`grok_session.py:25`, `voice_daemon.py:754`).
- **Tool schema generator collapses complex types** — `Literal`/`list`/
  `dict` all return bare `{"type":"string"}` with no `enum`/`items`, now
  load-bearing for `spotify_play(kind=...)` whose 5 values live only in
  prose (`tools/__init__.py:190`). Add a `Literal`→`enum` and `list`→`array`
  branch (backward-compatible).

---

## Resilience & safety quick wins

- **Spotify Web API has no request timeout (High · quick).**
  `spotipy.Spotify(auth_manager=auth)` is built with no `requests_timeout`
  (default = none) (`spotify_router.py:197`), and the two pause calls in
  `_spotify_pause_via_web_api` (`mux.py:961,974`) are *not* wrapped in
  `wait_for` — unlike every other external call in `mux.py`. A hung Spotify
  socket suspends the mux tick coroutine, freezing all source transitions
  and the fan-in gate until it returns; multiple stale accounts compound
  serially. **Fix:** `spotipy.Spotify(auth_manager=auth, requests_timeout=4)`
  + `asyncio.wait_for` around the two `to_thread` calls. Matches the timeout
  discipline already used everywhere else in the file.
- **Resolved 2026-06-17: `active_speaker` lab `aplay` path now has the
  forbidden-lane guard.** The old concern was that the lab tone path could run
  `aplay -D <test PCM>` against `jasper_out` or an outputd/CamillaDSP lane.
  `jasper.active_speaker.playback` now rejects daemon-owned test writers in
  both `tone_backend_status` and `AplayTonePlaybackBackend.__init__`, via the
  shared `_forbidden_test_pcm_token` helper. No action remains for this item.
- **`require_outputd_ready` aborts the whole install on a transient
  failure (Medium · quick).** Under `set -euo pipefail`, a bare call at
  `install.sh:2672` with only a 3 s probe deadline lets a momentary DAC
  "device busy" abort `main()` *before* nginx, TLS, cues, and the doctor
  summary run. **Fix:** move it to the end of `main()`, or give it a bounded
  retry, or downgrade to a loud WARN that still parks voice.

---

## Security & governance quick wins

These are mostly settings/config changes, high signal for a public repo.

- **Wizard tier doesn't validate `Host` — DNS-rebinding gap (High ·
  moderate).** The control daemon blocks DNS-rebinding via
  `http_security.py`, but none of the ~18 nginx-proxied wizards validate
  `Host`/`Origin` (grep finds no `is_allowed_management_host`/`Sec-Fetch` in
  `jasper/web/`), and nginx forwards `Host $host` under `server_name _`. A
  victim on a malicious page that DNS-rebinds to the Pi's LAN IP becomes
  same-origin and can read wizard pages + issue authenticated POSTs (forget
  WiFi, repoint HA, exfiltrate/overwrite API keys, reboot) — the
  double-submit CSRF doesn't help once same-origin. **Fix:** wire the
  *existing, tested* guard into `web/_common.py`'s shared request flow so
  every wizard inherits it (defense-in-depth: also pin nginx `server_name`).
  This also makes SECURITY.md's DNS-rebinding claim true for the wizard tier.
- **Public repo, no branch protection on `main` (High · quick).**
  `gh api …/branches/main/protection` → 404. CI runs but nothing requires
  it; any collaborator or compromised token can push straight to `main`,
  bypassing the test + provenance gate. **Fix:** require the `pytest` check
  + one review, block force-push/deletion. Settings change, no code.
- **GitHub Actions pinned to mutable tags (Medium · quick).** All steps use
  `@v4`/`@v5`; a repointed tag runs arbitrary code with a `pull-requests:
  write` token. Inconsistent with the repo's own immutable-pin discipline
  for downloads. **Fix:** pin to commit SHAs with a `# v4` comment + add
  Dependabot's `github-actions` ecosystem.
- **No Dependabot/Renovate (Medium · quick).** Wide security-relevant dep
  surface (openai, google-genai, httpx, spotipy, onnxruntime, pyusb,
  esptool, dbus-next, zeroconf) on a months-unattended appliance, with no
  CVE automation. **Fix:** minimal `.github/dependabot.yml` for `pip`,
  `github-actions`, `cargo` (weekly).
- **Vendored firmware assets lack license attribution (Medium · quick).**
  `firmware/dial/src/CST816D.{h,cpp}` (Elecrow) and the SquareLine-generated
  `ui_img_*.c` blobs ship with no LICENSE/SPDX/NOTICE. **Fix:** add upstream
  license text + a `firmware/THIRD_PARTY` (or SPDX headers); confirm
  SquareLine free-tier redistribution terms.
- **Destructive endpoints unauthenticated (Medium).** See big-rocks §6 —
  document the tradeoff and consider a token for poweroff/reboot/wifi-off.

---

## Hygiene infrastructure (the linter that isn't real)

- **`ruff` declared but unconfigured and unenforced (Medium).** `pyproject`
  lists `ruff>=0.6` in dev extras but has no `[tool.ruff]` block and no
  mypy config; CI explicitly defers lint with a stale "~185 violations"
  comment (default `ruff check .` now reports **161**, 102 auto-fixable,
  dominated by trivial F401/F541). **Fix:** add `[tool.ruff]` (E,F,W,I,UP at
  least), `ruff check --fix`, per-file-ignore `tests/voice_eval`, then wire
  `ruff check` into CI non-blocking → blocking once green.
- **610 `# noqa`, 462 for a rule that no longer runs (Medium).** 462 are
  `BLE001` (blind-except) and 500+ total suppress rules not in ruff's
  default set with no `[tool.ruff]` enabling them — dead decoration that
  obscures the ~148 live-rule suppressions. **Fix:** either re-enable the
  `BLE001`/`ANN`/`ARG` ruleset so the markers are load-bearing, or strip the
  vestigial codes mechanically. (The codebase is healthier than this
  implies: ~75% return-typed, **zero** bare `except:`.)
- **`.env.example` frozen-seed shadows code defaults (High · moderate).**
  `install.sh:1563` seeds `jasper.env` from `.env.example` only on fresh
  install and never re-syncs; every literal there then permanently overrides
  the `config.py` default (it already hid a `HEADROOM=16` volume fix and is
  hiding the wake-threshold divergence above). **Fix:** reduce `.env.example`
  to only keys with no safe code default (secrets, site-specific), or add an
  `install.sh` migration that prunes `jasper.env` keys still equal to the
  template literal (like the existing `migrate_*` helpers). At minimum
  document which keys are frozen-on-first-install vs live-from-code.
- **No committed Python lock (Medium).** No `uv.lock`/`requirements*.txt`;
  pyproject mixes exact pins with open ranges, and both CI and the Pi
  resolve transitive deps live, so two installs days apart can differ.
  **Fix (when main slows):** commit one lock, have CI + install.sh consume
  it, extend `check-provenance` to assert coverage — mirroring the existing
  `Cargo.lock` enforcement.
- **Five inconsistent boolean-parsing conventions in `config.py` (Low ·
  quick).** A `_env_bool` helper exists but four flags hand-roll different
  truth sets, so `enabled` turns on peering but not `citibike_ebike_only`,
  etc. (`config.py:584,796,808,858`). Route positive-default flags through
  `_env_bool`; add an `_env_bool_default_true` for the `verify_ssl`/
  `server_vad` cases.

---

## Test-discipline gaps

- **~14 of 30 tools have no voice-eval regression scenario (Medium).** The
  audio (get/set/adjust volume, mute/unmute), transport (next/prev/pause/
  resume/now-playing), calendar, gmail, home_assistant, and diagnostic tools
  have none, and nothing enforces the rule AGENTS.md states. **Fix (no paid
  LLM cost):** a hardware-free meta-test that builds the registry with mock
  deps, enumerates tool names, and asserts each has a scenario file/marker —
  failing the build on a gap. Then backfill the missing scenarios over time.
- The CI `--ignore=tests/voice_eval` is now justified purely by cost; the
  env-leak it cites was already fixed by the autouse `_isolate_environ`
  fixture (see stale-doc below).

---

## Documentation staleness cluster

The corpus is genuinely well-maintained (0 stale at the 90-day threshold,
honest footers). These are specific drifts worth a single sweep:

| Doc | Drift | Fix |
|---|---|---|
| `HANDOFF-wake-telemetry.md:33,285,311,440,607` | Cites **500 MB** ring + an "hourly cleanup" timer that doesn't exist; production is **1 GB**, swept only on wake-fire attach. | Bump to 1 GB; replace "once per hour" with "swept after each wake-fire's audio attach"; update `wake_events.py` docstring + `DEFAULT_MAX_AUDIO_BYTES`. |
| `AGENTS.md:230-280` (design system) | Describes the canonical-UI migration as *in progress* with a "these pages hand-roll their own shell" list; it's **complete** — every wizard uses `canonical_page` + a static ES module, and `wrap_page`/`PAGE_STYLE`/`TOGGLE_CSS`/`NAV_BACK` are now **dead code** (~250 lines in `_common.py`, zero production callers). | Rewrite the section to "migration complete"; delete the dead legacy stack from `_common.py`. |
| `AGENTS.md:582-600` (renderer arch) | Omits `jasper-outputd`, a deployed final-output daemon that's a hard `After=/Wants=` dep of voice + camilla (referenced 18× in README). | Add a short subsection pointing to `HANDOFF-speaker-output-reference.md` as canonical. |
| `SECURITY.md:34-44` | "Management endpoints reject DNS-rebinding" is only true for `:8780`, not the wizards. | Fix the gap (preferred) and keep the claim, or scope the wording. |
| `correction/__init__.py:16` + `HANDOFF-correction.md:841` | `emit_correction_config` advertised as the public apply path with a wrong signature; it's production-dead (live path is `emit_sound_config`). | Delete or mark legacy; fix the `__init__` signature. |
| `docs/audio-paths.md:133` | "Add a music source" checklist points to `JASPER_FANIN_INPUT_PCMS`/`_RENDERERS` env vars that aren't in the service file; real defaults are compiled in `rust/jasper-fanin/src/config.rs:80`. | One sentence: canonical lane list lives in `config.rs`; the env vars are an optional override. |
| `HANDOFF-canonical-ui-migration.md` | Orphan — tagged historical but not in README's atlas (rule #8). | Add an atlas line or move to `docs/historical/`. |
| `.github/workflows/tests.yml:44-48` + `conftest.py:16` | CI comment says the `test_doctor` env-leak is unfixed; it was fixed by the autouse `_isolate_environ` fixture in **#256** (conftest credits #254/#255). | Update both comments. |
| `aec_bridge.py:210` vs `AGENTS.md:1471`/`README.md:852` | DTLN leg figures disagree (~95 MB/12% vs ~75 MB/25%). | Re-measure once, reconcile to one number. |
| `system_supervisor.py:38` | "Why not session-gated" rationale reasons from "all 3 probes failing" but the code reboots on **any** one probe failing for 3 cycles. | Fix the rationale to match (and confirm any-fail is intended — a transient sshd hiccup rebooting mid-music is a real consequence). |
| `tools/gmail.py:5` | Docstring says `gmail_unread_summary(max=5)`; real param is `limit`. | One-line fix. |
| `wake_corpus_setup.py:40` | Usage example points at `jts.local:8782` but default bind is loopback (proxied at `/wake-corpus/`). | Fix the example URL. |
| `install.sh` `--dry-run` plan | Hand-maintained static heredoc, structurally disconnected from `main()`'s real steps. | Add a test asserting every `main()` function name appears in the plan text. |
