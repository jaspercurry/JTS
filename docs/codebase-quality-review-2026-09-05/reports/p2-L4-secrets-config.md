# L4 — secrets (non-negotiable 3) and config ownership

Repo `/home/user/JTS` @ `2d571e6b8`. Read-only. Scratch scripts + probe output:
`scratchpad/L4-secrets-config/{probe_redaction.py,proposed.py}`.

## A. Verdict

**Storage is production-grade; egress is not.** The compartments
(`/var/lib/jasper-secrets` 2770 `jasper-secrets`, `/var/lib/jasper-intsecrets` 2770
`jasper-intsecrets`, setgid, tmpfiles-recreated, one wizard writer each, doctor-audited by
`cli/doctor/secret_compartments.py`) are the best-built thing in this lens — clean two-sided
availability/confidentiality contract, no secret in argv except the documented nmcli PSK, no
secret in a `value=` HTML attribute, no secret named by any doctor check. The failure is on
the way **out**: **eight independent redactors** in two languages, **five of them untested**,
and the one general-purpose redactor (`jasper/secret_redaction.py`, zero tests) **misses 8 of
23 realistic secret shapes I fed it — including every JTS env-key spelling that carries an
underscore before the secret word.** It is the sole guard on `/state.voice.connection_error`,
which serves a provider's raw HTTP rejection body to the whole LAN on `0.0.0.0:8780`.
**I grade non-negotiable 3 AMBER: at rest PASS, in transit FAIL.**
Config ownership is better than p0-config reported for `outputd.env` / `wifi_guardian.env`
(over-counted writers) and **worse** where it matters: the Python and bash env writers of the
same file use *different concurrency protocols* (Python holds `.<name>.env.lock`; the bash lib
holds nothing), and `write_stash` emits a shell-unquoted PSK into a file `jasper-wifi-guardian`
**`source`s as root** — which I reproduced executing arbitrary commands.

---

## 1. Secret flow trace

### 1a. Inventory — every secret class

| secret | at rest | mode / owner | writer | readers |
|---|---|---|---|---|
| `GEMINI_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY` | `/var/lib/jasper-secrets/voice_keys.env` | 0640 root:`jasper-secrets` (setgid dir) | `web/voice_setup.py:187 _write_split` | jasper-voice, jasper-web (`EnvironmentFile=`), `Config.from_env` |
| `GOOGLE_CLIENT_ID/_SECRET` | `/var/lib/jasper-secrets/google_credentials.env` | 0640 same | `web/google_setup.py:126 _write_creds_file` | voice, web |
| Google **refresh tokens** | `/var/lib/jasper-secrets/google/tokens/*.json` | 0640, dir 2770 | `google_creds.py:220 save_token` | `google_creds.load_credentials` |
| `GOOGLE_ROUTES_API_KEY` | `/var/lib/jasper-secrets/google_routes.env` | 0640 same | `web/transit_setup.py:1569` | voice, web |
| Spotify **refresh tokens** | `/var/lib/jasper-intsecrets/spotify/caches/*.json` | 0640, dir 2770 | `accounts.py:87 build_cache_handler` (atomic_io) | voice, control, mux, web |
| `SPOTIFY_CLIENT_ID` (PKCE — **no client secret**) | `/var/lib/jasper-intsecrets/spotify_credentials.env` | 0640 root:`jasper-intsecrets` | `web/spotify_setup.py:213` | voice, control, mux, web |
| `JASPER_HA_TOKEN` | `/var/lib/jasper-intsecrets/home_assistant.env` | 0640 same | `web/home_assistant_setup.py` ×6 sites | voice; **control fresh-reads** (`home_assistant.py:742`) |
| `JASPER_MTA_BUSTIME_KEY` | `/var/lib/jasper/transit.env` | 0640 group `jasper` | `web/transit_setup.py:1569` | voice, web |
| **Wi-Fi PSK** (`JASPER_WIFI_PSK`) | `/var/lib/jasper/wifi_guardian.env` | 0600 | `wifi_guardian_persistence.py:156 write_stash` **+** `deploy/lib/install/env-migrations.sh:566` | `jasper-wifi-guardian` (**`source` as root**), `control/wifi_guardian_state.py`, doctor |
| control token (CSRF) | `/var/lib/jasper/control_token` | 0640 group `jasper` | `control/control_token.py ensure_token` | control (verify), web (`_common.py:1170` → `<meta>` tag) |
| household credential (M2M bearer) | `/var/lib/jasper/household_secret` | 0640 group `jasper` | `control/household_credential.py ensure/adopt` | control, `grouping_supervisor:554` (`X-JTS-Household` header) |
| weather | **none** — Open-Meteo is keyless (`jasper/weather.py:5`) | | | |
| `JASPER_GEMINI_API_KEY`, `JASPER_GROK_API_KEY` | **docs-only ghosts**, no code (confirms p0 §2a) | | | |

### 1b. Egress paths and their guard

| path | carrier | guard | verdict |
|---|---|---|---|
| journal ← provider HTTP body | `voice/_supervisor.py:107 _rejection_text` | `redact_secrets` | **leaky** — see §2 |
| **`/state` (LAN, unauthenticated 0.0.0.0:8780)** | `voice_daemon.py:4747 connection_error` → `state_aggregate.py:108` | `redact_secrets` only | **Blocker** — same regex, LAN-visible. Not rendered in any `deploy/assets/` page, JSON only |
| doctor JSON (`--out /run/jasper-control/doctor-result.json`, 0640 group `jasper`) | `cli/doctor/_shared.py:78 _exception_detail`, `doctor/voice.py:268` | `redact_secrets` | same regex; doctor checks themselves name keys, never values (verified: only hit is `integrations.py:118 "GOOGLE_ROUTES_API_KEY is missing"`) |
| journal ← BusTime URL | `transit/providers/nyc_bus.py:138,140,257,299`, `bus.py:307,316`, `web/transit_setup.py:390,770,994` | `transit.base.scrub_secrets` | **correct and complete** for `[?&]key=` |
| journal ← Google token refresh | `google_creds.py:306 logger.warning(..., e)` | **none** | gap — `RefreshError` renders the token endpoint's response body unredacted |
| journal ← peer HTTP body | `control/grouping_supervisor.py:546` `resp.body.decode()[:160]` | **none** | low risk (peer is another JTS), but unredacted |
| journal ← nmcli argv | `web/wifi_setup.py:181,214`, `deploy/bin/jasper-wifi-guardian:321` | `_scrub_argv` / `_scrub_psk` / `scrub_psk` | correct at these sites; **`_scrub_argv` only knows the literal `password` marker** — `nmcli connection modify … 802-11-wireless-security.psk <PSK>` would pass through (no such call site today) |
| **`ps` / `/proc/*/cmdline`** | PSK on nmcli argv | none possible | **accepted + documented** (`wifi_setup.py:44`) |
| laptop `./logs/` | `scripts/fetch-pi-logs.sh` (journal, env-file cat, `systemctl status`) | `scripts/_diagnostic_redaction.sh` sed | **correct for env-file lines**, blind to JSON/header/URL shapes — see §2 composition |
| web HTML | all 7 secret inputs are `type="password"` with **no `value=`** (`google_setup:258`, `transit_setup:745,952,1148,1162`, `voice_setup:900`, `wifi_setup:1263`); saved keys shown via `mask_secret` | ✔ | clean |
| web error banner | flash rides a **cookie**, not `?msg=` (`_common.py:1300`) — not in nginx access logs | ✔ | clean |
| `flight_recorder.py` | dumps the whole DEBUG ring to the journal on any WARNING | **none** | redaction-bypass **amplifier**: DEBUG lines that would never have reached the SD card get published |
| `systemctl show -p Environment` | no unit carries a secret in `Environment=` (grepped all of `deploy/systemd/`) | ✔ | clean |
| `_unit_runtime_environ` (`doctor/renderers.py:941`) reads `/proc/PID/environ` | only used to expand `${VAR}` in a renderer device string; the resolved string is what is reported | ✔ | clean |

### 1c. Guard test results (`probe_redaction.py`, 23 realistic strings)

| helper | mandate | leaks **in mandate** | leaks overall |
|---|---|---:|---:|
| `secret_redaction.redact_secrets` | **everything** (general purpose) | **8 / 23** | 8 |
| `transit.base.scrub_secrets` | `[?&]key=` only | 0 / 2 | 21 |
| `scripts/_diagnostic_redaction.sh` | env-file / `Environment=` lines | **0 / 9** | 14 |
| `wifi_setup._scrub_psk(text, psk)` | value-based, PSK known | 0 | 0 |
| `_common.mask_secret` | display masking | 0 | 0 |

`redact_secrets` leaks: `GOOGLE_CLIENT_SECRET=`, `SPOTIFY_CLIENT_SECRET=`, `JASPER_HA_TOKEN=`,
`JASPER_WIFI_PSK=`, `JASPER_MTA_BUSTIME_KEY=`, `?key=<bustime>`, `X-JTS-Household: <tok>`,
`nmcli … password <psk>`. **Root cause is one character**: the `\b` at
`secret_redaction.py:24` cannot match after an underscore, so *every* `*_API_KEY` /
`*_TOKEN` / `*_SECRET` / `*_PSK` env spelling — i.e. the project's own naming convention,
which the bash redactor's regex explicitly encodes — is invisible to it.
`OPENAI/GEMINI/XAI_API_KEY=` only survive because the **prefix** rule fires, and it *masks*
(`sk-p...6789`) rather than redacts — a partial live credential onto `/state`.

**Composition (the fetch-pi-logs path: `redact_secrets` in-process, then the sed off-box):**
3 of 23 survive **both** stages — `?key=<bustime>`, `nmcli … password <psk>`,
`X-JTS-Household:`. Those are covered at source by `scrub_secrets` / `_scrub_psk` today; they
are the shapes with **no defence in depth**.

**Grade, static evidence only: non-negotiable 3 = AMBER.** Rest PASS. Transit FAIL on the
general redactor. No confirmed live leak — I could not prove any provider *echoes* a key in a
body — but the guard that exists to make that irrelevant demonstrably does not cover the shapes
JTS itself writes, and it is untested.

---

## 2. Redaction helpers: eight, converge to two

| # | helper | LOC | consumers | tests |
|---|---|---:|---:|---:|
| 1 | `jasper/secret_redaction.py:36 redact_secrets` | 46 | 3 | **0** |
| 2 | `jasper/transit/base.py:231 scrub_secrets` | 9 | 9 | 2 files |
| 3 | `jasper/web/voice_setup.py:246 _redact_provider_error` | 11 | **1** | **0** |
| 4 | `jasper/web/_common.py:1355 mask_secret` | 11 | 5 | 3 files |
| 5 | `jasper/web/wifi_setup.py:192 _scrub_argv` | 12 | 2 | **0** |
| 6 | `jasper/web/wifi_setup.py:805 _scrub_psk` | 10 | 2 | **0** |
| 7 | `scripts/_diagnostic_redaction.sh:20` | 9 | 4 scripts | 1 file |
| 8 | `deploy/bin/jasper-wifi-guardian:103 scrub_psk` | 15 | 1 | **0** |

*(Not redaction, correctly separate: `tools/__init__.py:690 _redacted_mapping_preview` — a
per-tool log opt-out; `multiroom/config.py:272 _scrub_roster_field` — delimiter injection.)*

**Converge to two, not one.** Pattern-based and value-based are genuinely different: a
whitespace-bearing WPA passphrase can only be fully removed by a caller that *holds* it.
- **Keep #6** (value-based) and make **#3** call it; delete #8 (bash port of #6 — the guardian
  can shell out to the Python or keep 15 lines with a test).
- **Fold #1, #2, #5, #7 into one `redact_secrets`.** #7 stays as a thin bash wrapper that
  shells out to `python -m jasper.secret_redaction` if the venv is up and falls back to today's
  sed otherwise (the bundle must work when the venv is the broken thing).
- **Keep #4** — display masking is a third, deliberate concern.

**The fixed regex set** (validated: `proposed.py` → **0/23 leak**, and 5 of 6 non-secret
control strings survive untouched):

| # | rule | regex | catches |
|---|---|---|---|
| R1 | scheme header | `(?i)\b(Bearer\|Basic)\s+[A-Za-z0-9._~+/=-]{8,}` | `Authorization:` |
| R2 | **key=value, underscore-tolerant** | `(?i)(?<![A-Za-z0-9])((?:[A-Za-z0-9-]+_)*(?:api[_-]?key\|access[_-]?token\|refresh[_-]?token\|id[_-]?token\|client[_-]?secret\|secret\|password\|passwd\|psk\|token\|bustime[_-]?key))(['"]?\s*[=:]\s*['"]?)([^'"\s,;}\]]+)` | every env spelling, JSON, `x-goog-api-key:` |
| R3 | CLI arg | `(?i)\b(password\|psk)\s+(\S+)` | nmcli argv echo |
| R4 | query param | `(?i)([?&](?:key\|api[_-]?key\|token\|access[_-]?token)=)[^&\s'"<>]+` | absorbs #2 |
| R5 | JTS headers | `(?i)\b(X-JTS-(?:Token\|Household))\s*:\s*\S+` | control token / household |
| R6 | live prefixes | `\b(?:AIza\|sk-\|xai-\|GOCSPX-\|1//0)[A-Za-z0-9_./+-]{8,}` | bare key, no key name — **replace, don't mask** |

Known trade: R3 over-redacts prose (`"password reset link sent"`). Fail-safe direction; take it.

**The one behavior test** (replaces zero tests across #1/#3/#5/#6/#8) — structured, no prose:

```python
# tests/test_secret_redaction.py
CASES = [  # (id, input, expected-output)
  ("env_openai",   "OPENAI_API_KEY=sk-proj-AbCdEf0123456789",   "OPENAI_API_KEY=<redacted>"),
  ("env_ha",       "JASPER_HA_TOKEN=eyJhbGci.eyJpc3M.sIg",      "JASPER_HA_TOKEN=<redacted>"),
  ("env_psk",      "JASPER_WIFI_PSK=hunter2xy",                 "JASPER_WIFI_PSK=<redacted>"),
  ("env_gsecret",  "GOOGLE_CLIENT_SECRET=GOCSPX-Ab12Cd34Ef56",  "GOOGLE_CLIENT_SECRET=<redacted>"),
  ("env_bustime",  "JASPER_MTA_BUSTIME_KEY=0d4e-6c2a-9f1b",     "JASPER_MTA_BUSTIME_KEY=<redacted>"),
  ("json_refresh", '{"refresh_token": "1//0gABCDEFGH"}',        '{"refresh_token": "<redacted>"}'),
  ("bearer",       "Authorization: Bearer eyJhbGciOi",          "Authorization: Bearer <redacted>"),
  ("query_key",    "GET https://x/y.json?key=abc12345&lat=1",   "GET https://x/y.json?key=<redacted>&lat=1"),
  ("jts_header",   "X-JTS-Household: kR3n9QpZ7sT2vX8b",         "X-JTS-Household: <redacted>"),
  ("bare_prefix",  "Incorrect API key provided: sk-abcd1234.",  "Incorrect API key provided: <redacted>."),
  ("negative_1",   "the key is under the mat",                  "the key is under the mat"),
  ("negative_2",   "tokenizer=whisper rate=48000",              "tokenizer=whisper rate=48000"),
]
@pytest.mark.parametrize("cid,raw,want", CASES, ids=[c[0] for c in CASES])
def test_redaction(cid, raw, want): assert redact_secrets(raw) == want
```
Plus one derived pin: `for name in EVERY_SECRET_ENV_NAME: assert redact_secrets(f"{name}=S3CR3TV4LUE") == f"{name}=<redacted>"`,
where `EVERY_SECRET_ENV_NAME` is imported from one shared tuple that
`_diagnostic_redaction.sh`'s regex is also generated from — so a new secret env var cannot be
added without both redactors learning it.

---

## 3. Env-file single-writer audit — corrections to p0-config

| file | real writers (verified) | keys | lock protocol | p0 said | my grade |
|---|---|---|---|---|---|
| `outputd.env` | `deploy/bin/jasper-audio-hardware-reconcile:797,1361,1804-1994` (`set_env_file_var*`) **+** `jasper/fanin/coupling_reconcile.py:919,1187,1602` (`_write_env_text` → whole-file `atomic_write_text`) | bash: BACKEND/SINK/DAC_PCM/ACTIVE_LANE/… Python: CONTENT_BRIDGE + ring path/slots | **incoherent**: Python holds `/run/jasper-fanin-coupling.lock` (entry lock, self-only); bash holds **nothing**. Neither holds `.outputd.env.lock` | 3 writers | **2 writers, Should-fix.** `jasper-apply-airplay-mode` is a **reader only** (`:397` `env_file_has_key`); p0's `:105` is a path variable. Real bug is the lock mismatch, not the count |
| `aec_mode.env` | `deploy/install.sh:1536` (`>` seed) **+** `deploy/bin/jasper-aec-reconcile:307-327,338` (`>` seed / `>>` per-key append) **+** `jasper/control/aec_endpoints.py:148,187` (`locked_update_env_file`) | disjoint | Python holds `.aec_mode.env.lock` (created 0660 by `env-migrations.sh:670`); **bash `>>` holds nothing and is not atomic** — an append can land on the pre-`os.replace` inode and vanish | 3 writers, "duplicated seed" | **confirmed 3 + the seed is byte-identical.** install.sh's seed is **redundant**: it calls `jasper-aec-reconcile --reason install` 7 lines later, whose `ensure_mode_file` creates the same file |
| `wake_model.env` | `web/wake_setup.py:1022` **+** `control/aec_endpoints.py:224` — **both** `locked_update_env_file`, disjoint keys, race documented at `wake_setup.py:1017-1021` | MODEL / THRESHOLD | ✔ same lock | ✘ two writers | **Earns-its-keep.** Only gap is the missing owner header — re-grade to Nit |
| `wifi_guardian.env` | `wifi_guardian_persistence.py:156` **+** `env-migrations.sh:566` (seed-if-absent) | SSID/PSK/KEY_MGMT | seed is `[[ -f ]]`-guarded | 3 writers | **2 writers, fine.** `deploy/bin/jasper-wifi-guardian` **sources**, never writes — but see F1 below |

**Owner headers:** 5 writers emit one (`fanin/latency_mode.py:108`, `audio_quality.py:116`,
`audio_hardware/usb_port_role.py:789`, `deploy/bin/jasper-identity-reconcile:231`,
`jasper-accessory-reconcile`). `web/_common.py:469 write_env_file` emits bare `KEY=VALUE`, so
~14 wizard/control-owned files carry none. Confirms p0 §7 #4. Three-line fix:
`write_env_file(path, values, *, owner: str)` prepending `# Written by {owner} — do not edit.`

**Daemon caching of wizard-owned values:** 14 module-level env reads tree-wide (AST-verified),
**all fixed paths/sockets, none wizard-owned** — p0's "Earns-its-keep" confirmed. The real cache
is at startup, and it is correct-by-restart everywhere except one place:

| daemon | caches | refreshed by | verdict |
|---|---|---|---|
| jasper-voice | `Config.from_env()` once (`voice/daemon_main.py:649`) — all 3 API keys, HA token, wake model | every wizard calls `restart_voice_daemon()` (`_common.py:672`) | ✔ |
| jasper-mux | `os.environ` snapshot (`mux.py:1761` in-function) | `spotify_setup.py:228` restarts mux | ✔ |
| jasper-control | `volume_ops.py:98 SPOTIFY_CLIENT_ID` from process env; **HA token fresh-read from the file** (`home_assistant.py:742,756` — exemplary, cites the rule) | restart + fresh read | ✔ |
| **jasper-web / `/google`** | `google_setup.py:1060-1072 make_server()` snapshots `GOOGLE_CLIENT_ID` + **`GOOGLE_CLIENT_SECRET`** into `cfg` at construction | only its own `POST setup-credentials` (`:906`) mutates `cfg` in place | **confirms p1-T16-2.** Self-healing for its own save; **stale** after any other writer (install migration, hand edit, restore). Fix: `_read_creds_file()` per request, like `home_assistant.probe_status_from_env` |
| `/spotify` | `spotify_setup.py:1378 _build_cfg` snapshots `SPOTIFY_CLIENT_ID` | — | **not a secret** (PKCE, no client secret) — p1-T16-2 over-graded this half |

---

## 4. One home for "set KEY=VALUE in an env file"

**Today: 3 bash spellings + 4 Python spellings.**

| lang | impl | atomic? | quotes? | locks? | callers |
|---|---|---|---|---|---|
| bash | `deploy/lib/jasper-env-file.sh:75 jasper_env_file_set` / `:150 _unset` | ✔ mktemp+mv | ✔ (`%q`-bug-proof) | **✘** | 2 reconcilers (via `set_env_var`, `set_env_file_var[_if_changed]`) |
| bash | **`deploy/install.sh:1416 set_jasper_env_value`** | **✘** `sed -i.bak` delete **then** `>>` append — the key is **absent** between the two ops, and `sed -i.bak` briefly unlinks the target name | **✘** | 1 (`python-runtime.sh:491`) |
| bash | `deploy/bin/jasper-aec-reconcile:307-327` raw `printf >>` | ✘ | ✘ | ✘ | seed-only |
| py | `atomic_io.py:478 locked_update_env_file` / `:518 locked_transform_env_file` | ✔ | n/a | ✔ `.<name>.env.lock` | 6 |
| py | `env_file.py upsert/remove` (text transform, order-preserving) | caller's | n/a | caller's | `fanin/coupling_reconcile`, `librespot_state` |
| py | `web/_common.py:469 write_env_file` | ✔ | ✘ (rejects `\n`) | **✘** — whole-file replace, lost-update documented in its own docstring | 17 |
| py | 6 hand-rolled emitters (`wifi_guardian_persistence:156`, `conversation_history:387`, `model_downloads`, `latency_mode:106`, `audio_quality:116`, `speaker_name`) | mixed | mixed | ✘ | — |

**The one home, and the diff to get there** (4 PRs, each < 400 lines):

1. **Make the bash lib take the same lock the Python one does.** In `jasper_env_file_set`/
   `_unset`, wrap the read-modify-mv in `exec 9>"$dir/.$(basename "$file").lock"; flock 9`.
   *This is the change that makes the multi-writer files actually correct* — `outputd.env` and
   `aec_mode.env` are today protected on the Python side only. `env-migrations.sh:670` already
   provisions two of these lock files at 0660.
2. **Delete `set_jasper_env_value`** (`install.sh:1416`, 6 lines); `source
   deploy/lib/jasper-env-file.sh` in install.sh and call `jasper_env_file_set "$ENV_DIR/jasper.env"
   JASPER_INSTALL_PROFILE streambox 0640 0755` at `python-runtime.sh:491`. install.sh is
   non-negotiable tier and is currently the **only non-atomic, non-quoting** env writer in the tree.
3. **Delete the `aec_mode.env` seed at `install.sh:1535-1539`** — `jasper-aec-reconcile
   --reason install` runs 7 lines later and `ensure_mode_file` creates the identical file. Replace
   the `printf >>` appends in `ensure_mode_file` with `jasper_env_file_set` calls.
4. **Python: `write_env_file` → `env_load`, gain a lock + an owner header.** Move
   `web/_common.py:469` to `jasper/env_load.py` (fixes the `control/` and `wake_corpus/`
   importing-from-`web/` inversion), give it `owner: str` and route it through
   `locked_transform_env_file`. Then retire the 6 hand-rolled emitters onto it (~150 lines out).

---

## 5. Knob deletion list — verified, with corrections

| # | knob | p0 claim | **my verification** | LOC out | risk | test pins it? |
|---|---|---|---|---:|---|---|
| K1 | `JASPER_WAKE_LEG_{DTLN,CHIP_AEC,CHIP_AEC_150,CHIP_AEC_210}` | "hard-seeded 0, no path sets 1" | **REFUTED.** `audio_profile_state.py:209 profile_env_updates` sets `CHIP_AEC=1` for the commissioned XVF profile; `control/handlers/aec.py:44 POST /aec/leg` toggles `dtln`/`chip_aec_150`/`chip_aec_210` live via `aec_endpoints._TOGGLE_TO_ENV_KEY:89-94`. These are **operator-reachable expert controls**, not dead branches | 0 | — | yes — `tests/test_control_aec_state.py` asserts each `=1` |
| K2 | `JASPER_TTS_TRANSPORT` | one legal value | **CONFIRMED.** `config.py:200-208` raises on anything but `outputd`; unit pins `Environment=…=outputd` (`jasper-voice.service:184`). 3 consumers still branch | ~40 (+`tts_routing.py:20`, `doctor/audio.py:525-581`, `daemon_main.py:258-263` unreachable tail) | low | in flight **PR #4105** |
| K3 | `JASPER_DUCK_TRANSPORT=camilla` | delete branch + `Ducker` + tests | **CONFIRMED reachable-in-theory, unreachable-in-practice.** `config.py:212` only permits `camilla` when `JASPER_TTS_OUTPUTD_SOCKET != FANIN_TTS_SOCKET` (bonded followers, `multiroom/reconcile.py:628`), but the unit pins `=fanin` and **no writer emits `camilla`** | **87** (`camilla.py:1228-1314 Ducker`) **+ 387** (`tests/test_camilla_ducker.py`) + 5 (`daemon_main.py:278-282`) | medium — must confirm the bonded-follower socket path never wants Camilla ducking | yes, 387 lines of it |
| K4 | `JASPER_FANIN_CAMILLA_COUPLING` | one-value vocabulary | **CONFIRMED.** `fanin_coupling.py:38 VALID_COUPLINGS = frozenset({COUPLING_SHM_RING})`; `rust/jasper-fanin/src/config.rs:678-690` refuses anything else | 0 wholesale — the *choice axis* is a no-op but the module owns Ring B + transport shapes | high | many |
| K5 | `JASPER_OUTPUTD_CONTENT_BRIDGE=direct` | "no writer emits it" | **CONFIRMED** — `rust/jasper-outputd/src/config.rs:374` `"direct"\|"off"\|"disabled" => Direct`; `control/transport_park.py:341-343` says so in prose | ~6 rust + the park branch | low, but **has a stated expiry already** (`transport_park.py:348` "dies with outputd's own FIFO reader, ADR-0220 #3118") — wait for it |
| K6 | 14 `JASPER_RAMP_*` "set nowhere" | | **PARTIALLY REFUTED.** 13 are read at `audio_measurement/ramp.py:451-505` with `bounded_env_float` defaults and shipped as **empty** assignments in `.env.example:364-389` (safe — `env_load.py:119` treats `""` as absent). **2 are genuinely DEAD**: `JASPER_RAMP_DRIFT_UNIFORM_DB`, `_BAND_TOL_DB` (`.env.example:396-399`, zero readers) | 4 | none | no |
| K7 | 31 wizard `*_WEB_HOST/_PORT` | 8 dead `main()`s | **CONFIRMED and under-counted.** Only **4** console scripts are exec'd by systemd (`jasper-{correction,bluetooth,chat,system}-web`). `jasper-web` runs as `python -m jasper.web` (`deploy/jasper-web.service:89`), so `spotify_setup:main` is dead too; `jasper-sound-web` (pyproject:218) has **0** references outside pyproject. **10 dead `main()`s**: google 44, ha 27, rooms 20, transit 38, voice 45, wake 39, wifi 24, sound 46, spotify, wake-corpus | **~330** + 7 dead `*_WEB_HOST` | low | no |
| K8 | 30 AEC3 tuning knobs | "declare a lab pack, don't delete" | **CONFIRMED and already half-done.** `aec_sweep.py:105-135` is a typed `_KnobSpec` registry (int/float/bool/enum + ranges); `aec_bridge_engines.py:65 _cfg_value(name, default, overrides)` is the plugin self-parse shape from `docs/extensibility.md`. The only thing missing is the *declaration* that these are a lab pack | 0 | none | registry is the test |

**Net verified deletion: ~470 LOC + 387 test LOC + 9 tokens** (K2 in flight, K3, K6's two, K7's ten `main()`s + 7 HOST knobs). K1 and K8 should be **kept and labelled**, not deleted.

---

## 6. Replacing `tests/test_env_vars_codified.py`

Confirmed: 257 lines, 69-entry `_UNCODIFIED`, passes vacuously (a prose mention counts), and
its docstring cites `AGENTS.md ("Codify, don't memorise")` — `grep -c Codify AGENTS.md` = **0**.

**Proposed contract.** For every `JASPER_*` (and `{GEMINI,OPENAI,XAI,GOOGLE,SPOTIFY}_*`) name
**read** in `jasper/`, exactly one must hold:
1. a **writer** exists in `deploy/install.sh`, `deploy/lib/install/`, `deploy/bin/`,
   `deploy/systemd/*.service` (`Environment=`), `jasper/web/`, or `jasper/control/`; **or**
2. it matches a **declared pack pattern** — `_SEAM_SUFFIXES = (_PATH|_FILE|_DIR|_ROOT|_DB|_SOCKET)$`
   (test/diagnostic path overrides), `^JASPER_AEC_` (the `aec_sweep._KnobSpec` lab pack — assert
   membership in that registry, not just the prefix), `_WEB_(HOST|PORT)$` (wizard hosting); **or**
3. it is in an explicit `_CONSTANT_NOT_A_KNOB` allowlist with a one-line why.

**Allowlist size, measured from the p0 ledger:** 509 tokens are read in `jasper/`; **162** have a
real writer; **347** would need cover. Of those, the three patterns absorb **167**
(69 path seams + 67 AEC + 31 web host/port), leaving **~180 enumerated entries** —
2.6× today's 69, which is the honest cost of making the guard mean something. The 180 skew
`WAKE` 22, `ACTIVE_SPEAKER` 20, `FANIN` 16, `OUTPUTD` 15, `GROUPING` 8. Ratchet the same way
(two-sided: gaining a writer or losing its reader fails), so it can only shrink; landing K7 and
K6 takes ~9 off on day one.

---

## 7. Ranked findings

| # | sev | file:line | what | evidence | fix |
|---:|---|---|---|---|---|
| F1 | **Blocker** | `jasper/wifi_guardian_persistence.py:186-188` + `deploy/bin/jasper-wifi-guardian:131` | PSK written **shell-unquoted** into a file the guardian `source`s **as root**. | Reproduced: `write_stash(..., "correct horse battery staple", ...)` then `source` → bash runs `horse` as a command and `$JASPER_WIFI_PSK` is **empty**; the guardian then takes the open-network branch (`:296`) and reconnects with **no password** — the recovery silently cannot work for any household whose passphrase contains a space. A `$(…)`-bearing PSK executed `id > pwned.txt` **as uid 0**. `env-migrations.sh:571-573` writes unquoted too. | Quote on write (`shlex.quote` / the lib's `jasper_env_quote_value`) in **both** writers **and** stop sourcing: parse with `grep '^KEY='` like `read_stash` does. Pin with a space-bearing and a `$(…)`-bearing PSK. |
| F2 | **Blocker** | `jasper/secret_redaction.py:24-30`, 0 tests | The one general redactor misses every underscore-prefixed secret env name; sole guard on the LAN-readable `/state.voice.connection_error`. | §1c: 8/23 leak, incl. `JASPER_HA_TOKEN=`, `GOOGLE_CLIENT_SECRET=`, `JASPER_WIFI_PSK=`. Confirms **p1-T18 F1**; I re-grade it from "rewrite-smaller" to Blocker because of the `/state` reach (`voice_daemon.py:4747` → `state_aggregate.py:108`, unauthenticated `0.0.0.0:8780`). | §2 regex set + the parametrized pin. |
| F3 | Should-fix | `deploy/lib/jasper-env-file.sh:75` vs `jasper/atomic_io.py:478` | Two writers of one file, two lock domains: Python holds `.<name>.env.lock`, bash holds nothing. | `aec_mode.env`: `aec_endpoints.py:148` locked vs `jasper-aec-reconcile:307-327` bare `>>`. `outputd.env`: `coupling_reconcile.py:919` whole-file `atomic_write_text` under a *different* lock vs `jasper-audio-hardware-reconcile:1804-1994`. | §4 step 1 — `flock` in the bash lib on the same path. |
| F4 | Should-fix | `deploy/install.sh:1416` | Non-atomic, non-quoting `sed`-delete + `>>`-append; install.sh never sources the lib the reconcilers share. Non-negotiable tier 4. | §4 | Delete it; source the lib. |
| F5 | Should-fix | `jasper/web/_common.py:469` | No owner header on ~14 wizard-owned env files, so AGENTS.md's "writer is named in each file's header" is unenforceable. 5 writers comply. | §3 | `owner:` kwarg + header line. Confirms p0 §7 #4. |
| F6 | Should-fix | `.env.example:28,36,48` | `GEMINI_API_KEY=` / `OPENAI_API_KEY=` / `XAI_API_KEY=` ship as empty placeholders in the template that becomes `/etc/jasper/jasper.env` (0640 group `jasper` — readable by **every** daemon). An operator filling one in there defeats the whole `jasper-secrets` compartment. `GOOGLE_ROUTES_API_KEY` at `:265-270` carries the "canonical file is X; this placeholder just documents the name" note; the three LLM keys do not. | grep of `.env.example` + `deploy/systemd/jasper-voice.service:96,117` load order | Copy the GOOGLE_ROUTES note onto all three; add a doctor check that fails when a `*_API_KEY`/`*_SECRET`/`*_TOKEN` in `/etc/jasper/jasper.env` is non-empty. |
| F7 | Should-fix | `jasper/web/google_setup.py:1060-1072` | jasper-web snapshots `GOOGLE_CLIENT_SECRET` at `make_server()`; only its own save refreshes it. Contra AGENTS.md "never cache wizard-owned values from `os.environ`". Confirms **p1-T16-2** for Google; **refutes** it for Spotify (PKCE — `client_id` only, not a secret). | §3 | Per-request `_read_creds_file()`, the `home_assistant.probe_status_from_env` pattern. |
| F8 | Should-fix | `jasper/google_creds.py:306-310` | `logger.warning("token refresh failed (%s)", e)` — google-auth `RefreshError` renders the token-endpoint response body; no `redact_secrets`. Same at `grouping_supervisor.py:546` (peer body). | read | Route both through the unified redactor. |
| F9 | Should-fix | `jasper/flight_recorder.py:63-90` | On any WARNING the whole DEBUG ring is published to the journal — a redaction bypass **amplifier**, with no redaction of its own. | read | Apply `redact_secrets` in `RingFlushHandler.emit`'s format step (one call, bounded cost: the ring is already formatted strings). |
| F10 | Nit | `deploy/install.sh:1535-1539` vs `deploy/bin/jasper-aec-reconcile:338` | Byte-identical 8-key `printf` seed; install.sh runs the reconciler 7 lines later, whose `ensure_mode_file` creates the same file. | diff of the two `printf` strings | Delete the install.sh copy. Confirms p0 §7 #13. |

**Also confirmed from p0/p1, not re-reported:** `jasper-apply-airplay-mode` is **not** an
`outputd.env` writer (p0 §3a over-counted); `wake_model.env`'s two writers are both correctly
locked (re-grade ✘→✔); `jasper-wifi-guardian` is a **reader** of its stash (p0 counted 3
writers, there are 2); `google_creds.save_token:235` uses a predictable `path + ".tmp"` sibling
— the **p1-T18 F2** `atomic_io` ratchet hole, in the secrets compartment.

---

## D. What only hardware/runtime can prove

- **Whether any provider echoes the API key in a rejection body.** I proved the *guard* is
  broken for JTS's own key spellings; I could not prove a live leak. Needs a bad-key session
  against Gemini/OpenAI/xAI with `journalctl -u jasper-voice` + `curl :8780/state | jq .voice`.
- **The `outputd.env` / `aec_mode.env` lost-update race** — needs a boot where a udev
  reconcile and a `/wake` save land inside the same second.
- **F1's real-world blast radius**: whether the owner's actual passphrase contains a space or a
  shell metacharacter. The code defect is static; the impact is not.
- Whether `/proc/<nmcli>/cmdline` is readable by a non-root LAN-reachable process on the live box
  (`hidepid` is not set anywhere in `deploy/`).
- Whether `flock` in `jasper_env_file_set` deadlocks against a reconciler that already holds
  `/run/jasper-fanin-coupling.lock` — lock ordering must be checked on metal.

## E. Coverage

**Opened:** `jasper/secret_redaction.py`, `transit/base.py:220-240`, `transit/providers/nyc_bus.py:100-310`,
`voice/_supervisor.py:85-125`, `voice/model_discovery.py:60-340`, `voice/daemon_main.py:250-300,649`,
`voice_daemon.py:3120-3145,4740-4760`, `google_creds.py:110-330`, `accounts.py:60-120`,
`home_assistant.py:55-110,735-770`, `weather.py` (key grep), `wifi_guardian_persistence.py` (full),
`web/wifi_setup.py:125-230,795-940,1480-1500`, `web/voice_setup.py:145-260,890-910,1400-1800`,
`web/google_setup.py:95-135,245-265,685-730,1050-1120`, `web/spotify_setup.py:1365-1450`,
`web/_common.py:435-540,1280-1380,640-680`, `control/{control_token,household_credential}.py:1-115`,
`control/aec_endpoints.py:80-230`, `control/handlers/aec.py:20-130`, `control/grouping_supervisor.py:540-570`,
`control/state_aggregate.py:95-125`, `cli/doctor/{_shared.py:55-95,voice.py:245-290,secret_compartments.py:1-80,renderers.py:935-1040}`,
`atomic_io.py:395-540`, `env_file.py`, `env_load.py:105-145,215-260`, `flight_recorder.py:1-90`,
`http_security.py:1-60`, `audio_profile_state.py:180-250`, `fanin/coupling_reconcile.py:1-50,1735-1760,1895-1935`,
`fanin_coupling.py:25-45`, `config.py:195-230,655-670`, `aec_sweep.py:100-135`, `aec_bridge_engines.py:160-190`,
`audio_measurement/ramp.py:445-510`, `calibration_agent/model_client.py:78-110`, `tools/__init__.py:680-710`,
`multiroom/config.py:260-290`; `deploy/lib/{jasper-env-file.sh,jasper-sed-inplace.sh}` (full),
`deploy/lib/install/env-migrations.sh:490-600,660-680`, `deploy/install.sh:1390-1450,1495-1545`,
`deploy/bin/{jasper-aec-reconcile:295-380,860-870, jasper-audio-hardware-reconcile:555-660,790-800,1355-2000, jasper-apply-airplay-mode:55-115,385-415, jasper-wifi-guardian:25-145,280-325}`,
`deploy/tmpfiles/jts-{secrets,intsecrets}.conf`, `deploy/systemd/{jasper-voice,jasper-control,jasper-mux,jasper-doctor-json,jasper-wifi-guardian}.service`,
`deploy/jasper-web.service`, `scripts/{_diagnostic_redaction.sh,fetch-pi-logs.sh:30-50,85-175,285-340}`,
`.env.example:20-55,265-275,360-400`, `tests/test_env_vars_codified.py:1-90`, `pyproject.toml` scripts block.

**Executed:** `probe_redaction.py` (23 strings × 5 helpers + the 2-stage composition),
`proposed.py` (the fixed regex set, 23 + 6 negative), the `write_stash` → `bash source`
reproduction (F1), an AST census of module-level env reads across all of `jasper/`, and a
writer-coverage recount over `jasper_env_ledger.csv`.

**Skipped:** the ~900 test files except the 4 named; the DSP/measurement semantics behind the
AEC3 and RAMP knobs (I asserted only whether a value is supplied); `rust/` beyond
`jasper-outputd/src/config.rs:365-385` and `jasper-fanin/src/config.rs:678-690`;
`docs/adr/` (grep-targeted only: 0100, 0215, 0220, 0236).

**Grep-only (not opened):** the 15 `*_WEB_PORT` WizardSpec rows in `web/__main__.py:465-521`
were counted, not each read; the "5 owner headers" count is a grep over `jasper/`, `deploy/bin/`,
`deploy/lib/`; the ~180 residual allowlist estimate is computed from p0's ledger CSV, whose own
error bars (§0 of `p0-config.md`) apply.
