# Phase 0 cartography — configuration, flags, and state files

Repo `/home/user/JTS` @ `2d571e6b8`. Read-only pass. Artifacts written beside this file:

| file | contents |
|---|---|
| `jasper_env_ledger.csv` | 829-row ledger: token → class → prod reads/read files → prod sets/set files → test reads/sets → documented? |
| `config_fields.csv` | 94 `Config` fields → line, env token, non-test consumer count, consumer files |
| `knobs_noval.json` | the 229 never-assigned knobs with the `file:line` of their reads |
| `ledger3.json`, `rows4.json`, `statepaths.json` | raw indexes (occurrence-level) |
| `ledger.py`, `pyscan.py`, `merge2.py`, `classify3.py`, `final_csv.py`, `statefiles.py`, `cfgfields.py` | the scripts that built them |

## 0. Method and its error bars (read this before trusting a number)

Extraction is a Python-AST pass over every tracked `*.py` (call args, subscripts,
`Compare` nodes, module-level `NAME = "JASPER_X"` constants resolved to their use
sites), plus regex passes over `*.rs`, bash, systemd units, udev, nginx, ALSA conf
and Markdown. Reads counted through: `os.environ.get`/`getenv`/`environ[...]`,
`env.get(...)` on an env mapping, a constant flowing into any call or comparison,
Rust `std::env::var` + the `jasper-env` crate helpers (`env_str`/`env_f32`/`env_parse`)
+ per-crate `env_list`/`env_u32_*`/`env_enabled`, bash `${VAR}`, `grep '^KEY='` /
`sed -n 's/^KEY=//p'` / `awk ENVIRON[...]`, systemd `EnvironmentFile=`/`Environment=`,
udev `ENV{}`.

**Known residual error, both directions.** (a) A handful of Python constants named
`JASPER_*` are ordinary module constants, not env names (`JASPER_SERVICE_GROUPS` in
`jasper/cli/system_soak.py:30`) and are wrongly in the ledger. (b) Dynamic key
composition (`f"JASPER_DEBUG_{sub.upper()}"` in `jasper/debug_mode.py`,
`JASPER_{SOURCE}_SOURCE_INTENT` in `jasper/source_intent.py:75`,
`JASPER_WAKE_LEG_*`) means some listed tokens are only docstring spellings of a
generated key. (c) Test env is often injected as a plain dict literal
(`{"JASPER_X": str(p)}`) which reads as a bare string, so `knob-nobody-turns`
over-counts test seams. Spot-verified ~40 tokens by hand; every claim below with a
`file:line` was opened.

Token universe: **874 raw** `JASPER_*` matches; 41 are prefix fragments from glob
prose (`JASPER_AEC_`, `JASPER_WAKE_LEG_`, …) and 4 are placeholders
(`JASPER_A/B/C/X`). **829 real tokens** analysed. There is a *second* env namespace:
**45 `JTS_*`** env vars (deploy/laptop tooling, the C ioplug's test seams, onboarding)
— no rule anywhere says which prefix a new var takes; AGENTS.md names only
`JTS_ACCEPT_NEW_IDENTITY`.

---

## 1. The ledger — classification counts

| class | count | meaning |
|---|---:|---|
| `live` | **227** | read in prod code **and** set by some prod surface (install.sh, reconciler, wizard, unit, `.env.example`) |
| `knob-nobody-turns` | **289** | read in prod with a default; **no writer anywhere**, not even a test |
| `knob-test-set-only` | **190** | read in prod; only a test ever supplies a value |
| `test-harness-only` | **66** | appears only under `tests/` (fake-shim plumbing: `JASPER_NMCLI_LOG`, `JASPER_FAKE_APLAY_LISTING`, `JASPER_ARGV_CAPTURE`, …) |
| `DEAD-no-read` | **42** (2 are the false positives above → **40** real) | no consumer at all |
| `test-only-read` | **15** | read only by test code |
| **total** | **829** | |

**Only 27% of the `JASPER_*` surface (227/829) is a knob anything actually turns.**
`knob-nobody-turns` + `knob-test-set-only` = **479 tokens = 58%** are pure
read-with-a-default. Undocumented (absent from `.env.example` and from every `.md`):
**538 tokens**, incl. **69 `live` ones**.

Per-file counts, read files and set files for every token are in
`jasper_env_ledger.csv` (columns: `token, classification, prod_reads, read_files,
prod_sets, set_files, test_reads, test_sets, listed_only_in, documented`).

### 1a. There is already a guard, and it measures the wrong thing — Should-fix
`tests/test_env_vars_codified.py` (257 lines, 69-entry `_UNCODIFIED` allowlist)
asserts every env var read in `jasper/` has *some* "codification surface". It passes
today while 479 knobs have no writer, because "a prose mention in `.env.example`"
counts as codification. Its docstring also cites `AGENTS.md ("Codify, don't
memorise")` — that rule no longer exists (`grep -c Codify AGENTS.md` = 0; the
doctrine was replaced 2026-08-26, ADR-0001).
*Fix:* re-point the guard at the property that matters — "read in prod ⇒ written by
some prod surface **or** listed as an internal seam" — and refresh the citation.

---

## 2. Deletion candidates

### 2a. `DEAD-no-read` — 40 tokens with no consumer at all

| group | tokens | evidence |
|---|---|---|
| **Retirement-scrub list** (only appearance is a `sed '/^KEY=/d'` that strips them from `jasper.env` on every deploy, forever) | `JASPER_SPOTIFY_DEVICE_NAME`, `JASPER_AIRPLAY_DEVICE_NAME`, `JASPER_AEC_CHIP_AEC_DAC_AUTO`, `JASPER_AEC_CHIP_AEC_DAC_TRIAL`, `JASPER_CAPTURE_RELAY_BASE`, `JASPER_CAPTURE_ORIGIN`, `JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN`, `JASPER_CONTROL_PORT` | `deploy/lib/install/python-runtime.sh:372-384` and again `:498-501` (streambox); pinned by `tests/test_install_helpers.py:730` |
| **`.env.example` tombstone prose** (documented as removed, still greppable) | `JASPER_FANIN_ADAPTIVE_BUFFER`, `JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES`, `JASPER_FANIN_CAMILLA_PIPE`, `JASPER_FANIN_CAMILLA_PIPE_BYTES`, `JASPER_FANIN_OUTPUT_PCM`, `JASPER_FANIN_OUTPUT_BUFFER_FRAMES`, `JASPER_OUTPUTD_LOCAL_CONTENT_PIPE_BYTES` | `.env.example:293,295,312-313` |
| **Assigned in `.env.example`, read by nothing** — ships into every new `/etc/jasper/jasper.env` | `JASPER_RAMP_DRIFT_UNIFORM_DB`, `JASPER_RAMP_DRIFT_BAND_TOL_DB` | `.env.example:396-399` document a "range 0..24dB, default 3.0/2.0"; `git grep RAMP_DRIFT -- jasper tests` = 0 hits |
| **Docs-only ghosts** (named in a doc, never in code) | `JASPER_GEMINI_API_KEY`, `JASPER_GROK_API_KEY`, `JASPER_{OPENAI,GEMINI,GROK}_IDLE_TIMEOUT_SEC`, `JASPER_GROUPING_{SUBWOOFER_PRESENT,CROSSOVER_HZ,MAINS_HIGHPASS}`, `JASPER_OUTPUTD_DAC_CONTENT_{HP,SUB}_HZ`, `JASPER_GO_LIBRESPOT_URL`, `JASPER_RENDERER_BACKEND`, `JASPER_USBSINK_HOST_CLOCK`, `JASPER_CROSSOVER_FLOW`, `JASPER_HARNESS_LAB`, `JASPER_BASS_EXTENSION_SESSION_STATE`, `JASPER_OUTPUT_DAC_ROUTE` | subwoofer group is the ADR-0236 deletion (issue #4031, in flight); `docs/audit-pending-followups.md`, `docs/ux-audit-2026-09-03/*`, `docs/historical/*` |
| **Written but never parsed** | `JASPER_USB_GADGET_FORENSICS` | `jasper/control/usb_gadget_forensics.py:68` writes `{"JASPER_USB_GADGET_FORENSICS": "1"}`; the consumer (`deploy/usbsink/jasper-usbgadget-snapshot:25`, `deploy/systemd/jasper-usbgadget-forensics.path`) keys off **file existence** only. The key is decoration. |
| **Docstring-only spellings of generated keys** | `JASPER_DEBUG_AEC`, `JASPER_DEBUG_CONTROL`, `JASPER_DEBUG_FILE` | `jasper/debug_mode.py:17,18,53` — real keys are built as `f"JASPER_DEBUG_{id.upper()}"`. Harmless, but they inflate every grep. |
| Other | `JASPER_LOW_MEMORY_UNPARK_{FAILED,RESTORED}` (shell locals with a `_JASPER_` prefix, `deploy/lib/install/systemd-units.sh:988-989`), `JASPER_OPENWAKEWORD_MODELS_DIR` (`jasper/openwakeword_guard.py:42`, prose only) | |

**Blocker-adjacent:** the retirement-scrub list is permanent machinery with no expiry
condition, contra AGENTS.md ("any new guard ships with a removal condition or expiry
noted beside it"). Eight `sed` deletes run on every deploy of every Pi forever for
keys retired months ago, and the streambox branch carries a hand-copied subset.

### 2b. `knob-nobody-turns` with **no assignment anywhere** in the tree — 229 tokens

Ranked by how many prod sites branch on them (`knobs_noval.json` has all 229 with
`file:line`). By family:

| family | count | representative | note |
|---|---:|---|---|
| wizard `*_WEB_PORT` / `*_WEB_HOST` | **31** | `JASPER_WIFI_WEB_PORT` — `jasper/web/__main__.py:489` (WizardSpec) **and** `jasper/web/wifi_setup.py:1489` (argparse default) | same key parsed twice, two mechanisms; see §6a |
| AEC3 / WebRTC tuning | **30** | `JASPER_AEC_ERLE_MAX_L` — `jasper/cli/aec_bridge_engines.py:170` (`_cfg_value(name, "1.5", overrides)`) | whole `JASPER_AEC_{NEAREND_*,MASK_*,ERLE_*,DND_*,BOUNDED_ERL,CONSERVATIVE_HF,…}` block; registry in `jasper/aec_sweep.py:115` |
| path / state-file seams | **36** | `JASPER_MUX_MODE_STATE_PATH` — `jasper/mux.py:118` | legitimate test seams; belong in the guard's seam list, not the operator surface |
| bash-script seams | **26** | `JASPER_BOOTLOOP_STATE_FILE` — `deploy/bin/jasper-bootloop-guard:65` | injected by tests as subprocess env dicts (`tests/test_bootloop_guard_script.py:83`), invisible to my SET scan |
| AEC UDP ports | **10** | `JASPER_AEC_UDP_PORT_REF` — `jasper/cli/aec_bridge_config.py:198` | paired defaults on both ends; loopback wiring |
| genuinely unexplained | **96** | see below | |

Highest-value from the last group (each read at ≥2 prod sites, never set, never
documented, no test value):

| token | reads | verdict |
|---|---|---|
| `JASPER_ENV_TEST_U32` / `_STRING` / `_F32` | `rust/jasper-env/src/lib.rs:85-124` | crate's own unit-test fixture names leaking into the product namespace — rename to a non-`JASPER_` prefix |
| `JASPER_CONVERSATION_HISTORY_ENABLED` | `jasper/conversation_history.py:339,343,381` | 4 reads, 0 writers; the `/chat/` wizard writes `conversation_history.env` — verify it writes *this* key or delete the branch |
| `JASPER_LIVE_CONTEXT_RESET_SEC` | `jasper/config.py:763,767,771` | a `Config` field nothing supplies |
| `JASPER_VOLUME_REGRESS_{AFTER_SEC,SAFE_LOW_PCT,SAFE_HIGH_PCT}`, `JASPER_VOLUME_FIRST_BOOT_DEFAULT_PCT` | `jasper/config.py:913-927` | four knobs + four range validators (`jasper/config.py:217-224`) for values nobody sets |
| `JASPER_SPOTIFY_{SETUP_URL,MANUAL_REDIRECT_URI,BOUNCE_REDIRECT_URI}` | `jasper/config.py:835`, `jasper/web/spotify_setup.py:1435,1445` | derived-default overrides for "nonstandard reverse proxy" that no proxy config in the repo uses |
| `JASPER_OPENAI_TTS_MODEL` / `_VOICE` | `jasper/active_speaker/speech_stimulus.py:67-71` | lab stimulus knobs |

---

## 3. State and env FILES

### 3a. Every `*.env` under the managed dirs

Owner/writer determined by opening the write site; "single writer" = AGENTS.md rule.

| file | writer(s) | readers | owner header? | rule holds? |
|---|---|---|---|---|
| `/etc/jasper/jasper.env` | `deploy/install.sh:1416` (`set_jasper_env_value`), `deploy/lib/install/python-runtime.sh:364,372,387,484,498`, `deploy/lib/install/env-migrations.sh` | ~95 prod files; `EnvironmentFile=` in every daemon unit; `jasper/env_load.py:50` | no | operator-owned base; multiple install-time writers, all in `deploy/` |
| `voice_provider.env` | `jasper/web/voice_setup.py:183` | `config.py`, `voice/provider_state.py`, `cues/cli.py`, `usage.py`, 5 units, 3 scripts | **no** | ✔ |
| `wake_model.env` | **2 writers**: `/wake` model picker (`jasper/web/wake_setup.py:1022`) and jasper-control's sensitivity slider (`jasper/control/aec_endpoints.py:224`) — both via `locked_update_env_file`, race documented at `wake_setup.py:1018-1021` | `config.py`, `wake_models.py`, `model_downloads.py`, `control/aec_endpoints.py` | **no** | ✘ two writers, correctly locked, but the file names neither |
| `weather.env`, `transit.env`, `tool_state.env`, `conversation_history.env`, `speaker_name.env`, `peering.env`, `home_assistant.env`, `spotify_credentials.env`, `google_credentials.env`, `google_routes.env`, `voice_keys.env` | the matching `jasper/web/*_setup.py` via `write_env_file` | daemons via `EnvironmentFile=` + `jasper/env_load.py:52` | **no** | ✔ (one wizard each) |
| `aec_mode.env` | **3 writers**: `deploy/install.sh:1536` seed **and** `deploy/bin/jasper-aec-reconcile:338` seed — *byte-identical 8-key `printf`, duplicated* — plus per-key appends at `jasper-aec-reconcile:307-322` and `jasper/control/aec_endpoints.py:148,187` (`locked_update_env_file`) | `audio_profile_state.py`, `wake_corpus/bridge_session.py`, doctor, 3 scripts | no | ✘ duplicated seed literal |
| `fanin.env` | `deploy/bin/jasper-audio-hardware-reconcile`, `jasper/fanin/coupling_reconcile.py:1741` | `rust/jasper-fanin/src/config.rs`, `renderer_lanes.py`, doctor | no | key-scoped, prose-only |
| `outputd.env` | **3 writers**: `deploy/bin/jasper-audio-hardware-reconcile:1862`, `jasper/fanin/coupling_reconcile.py` (`_outputd_actions`), `deploy/bin/jasper-apply-airplay-mode:105` | `rust/jasper-outputd/src/config.rs`, `audio_runtime_plan.py`, `ring_health.py` | no | ✘ *three* modules each document themselves as "the single writer" of a different key subset (`jasper/fanin/coupling_reconcile.py:18,109`; `jasper/multiroom/reconcile.py:1408`; `jasper/fanin/converge.py:44`) |
| `grouping.env`, `grouping-outputd.env`, `grouping-voice.env`, `grouping-airplay.env` | `jasper/multiroom/reconcile.py` | outputd/voice/airplay units | no | ✔ (good example: separate files per consumer instead of shared keys) |
| `renderer_lanes.env` | `jasper/renderer_lanes.py` (`jasper-audio-config renderer-lanes`) | ALSA conf.d, shairport template, librespot/bluealsa units, fanin | no | ✔ |
| `identity.env` | `deploy/bin/jasper-identity-reconcile:231` | `identity.py`, `http_security.py`, doctor | **yes** ("Written by jasper-identity-reconcile — do not edit") | ✔ |
| `accessory-mics.env` | `jasper-accessory-reconcile` | `jasper/accessories/mic_env.py`, voice unit | **yes** (`deploy/bin/jasper-deploy-health:38`) | ✔ |
| `audio_quality.env` | `deploy/install.sh:1327` seed + `jasper/audio_quality.py:116` | `deploy/bin/jasper-render-asound-conf:21`, ALSA `asoundrc.jasper` | **yes** ("Written by JTS /system audio quality control") | 2 writers (seed + control) |
| `usb_latency.env` | `jasper/fanin/latency_mode.py:108` | same module only | **yes** | ✔ |
| `debug.env` | `jasper/control/debug_control.py:61` | `jasper/debug_mode.py` | no | ✔ |
| `mic_mute.env` | `jasper/mic_mute_persistence.py` | `config.py`, `wake_corpus/recording_backend.py` | no | ✔ |
| `source_intent.env` | `jasper/source_intent.py` | `fanin/coupling_auto.py`, `usb_mic.py`, deploy-health | no | ✔ |
| `wifi_guardian.env` | `deploy/bin/jasper-wifi-guardian`, `jasper/wifi_guardian_persistence.py`, `deploy/lib/install/env-migrations.sh` | `control/wifi_guardian_state.py`, doctor | no | ✘ 3 writers |
| `airplay_mode.env` | `jasper/web/airplay_setup.py:225` | `deploy/bin/jasper-apply-airplay-mode`, shairport unit | no | ✔ |
| `i2s_hat.env` | `deploy/bin/jasper-audio-hardware-reconcile` | `jasper/audio_hardware/usb_port_role.py` | **yes** (`usb_port_role.py:789` "Generated from board topology") | ✔ |
| `usb_mic.env`, `wake_corpus_bridge.env`, `usb_gadget_forensics.env` | `usb_mic.py` / `wake_corpus/bridge_session.py:1386` / `control/usb_gadget_forensics.py:68` | matching units | no | ✔ |

**Written by nobody / read by nobody:**
- `/var/lib/jasper/audio_topology.env` — **no writer at all**; sole reference is a
  doctor staleness check for the retired dmix/fanin switcher
  (`jasper/cli/doctor/audio_runtime_fanin.py:352-360`). Guard with no expiry.
- `/var/lib/jasper/tts.env`, `/var/lib/jasper/usbsink.env`,
  `/var/lib/jasper/{google_credentials,home_assistant,spotify_credentials}.env` —
  referenced only by *negative* assertions (`tests/test_outputd_systemd.py:188`,
  `tests/test_systemd_hardening.py:535,590,603`) or by `docs/historical/`. Retired
  paths kept alive as regression pins; none carries a removal condition.
- 51 further paths under `/var/lib/jasper*`, `/run/jasper*` appear only in tests or
  docs (`statepaths.json`) — mostly test fixtures, but includes
  `/run/jasper-usbsink/state.json`, `/run/jasper-grouping/snapfifo`,
  `/run/jasper-fanin/camilla.pipe`, `/run/jasper-outputd/content.pipe` — retired
  transports still named by doctor tests.

**Readers that cache at import time:** only **14** module-level env reads across all
of `jasper/` — `jasper/control/{client.py:70,control_token.py:65,household_credential.py:77,restart_broker.py:89}`,
`jasper/mux.py:111,117`, `jasper/wake_corpus/bridge_session.py:231-246`,
`jasper/web/wifi_setup.py:92,124,125`. All are fixed path/socket defaults, none is a
wizard-owned value, so the AGENTS.md "never cache wizard-owned values from
`os.environ`" rule holds. **Earns-its-keep.**

### 3b. The secrets compartments
`/var/lib/jasper-secrets/{voice_keys,google_credentials,google_routes}.env` and
`/var/lib/jasper-intsecrets/{home_assistant,spotify_credentials}.env`: single wizard
writer each (`jasper/web/{voice,google,transit,home_assistant,spotify}_setup.py` via
`write_env_file(..., mode=SECRET_ENV_MODE)`), read via `EnvironmentFile=` and
`jasper/env_load.py`, mode/ownership re-asserted every deploy
(`reassert_{secrets,intsecrets}_compartment_perms`), audited by
`jasper/cli/doctor/secret_compartments.py`. Clean. **Earns-its-keep.**

---

## 4. `jasper/config.py` (1020 LOC)

**94 fields**, all reachable: 89 have a direct attribute consumer outside
`config.py`; the other 5 are consumed by string dispatch and `getattr` —
`mic_device_raw`/`mic_device_dtln`/`mic_device_chip_aec_150`/`_210` through the leg
table at `jasper/voice_daemon.py:823-826`, and `aec_chip_aec_enabled` through
`getattr(cfg, "aec_chip_aec_enabled", False)` at `jasper/voice/input_policy.py:96`.
**No dead Config fields.** Full table in `config_fields.csv`.

`config.py` reads **80 distinct env tokens** via 36 `_env*()` calls in one
`from_env()`. Most-consumed fields: `hostname` (33 sites), `mic_device` (28),
`voice_provider` (21), `wake_model` (13).

### Fields duplicated by a second parse path — Should-fix
**38 tokens are read both by `Config.from_env()` and directly elsewhere in prod.**
The worst offenders (full list in the CSV, filter `read_files` containing
`jasper/config.py`):

| token | Config field | also parsed directly in |
|---|---|---|
| `JASPER_WAKE_MODEL` | `wake_model` | `cli/wake_enroll.py`, `control/aec_endpoints.py`, `model_downloads.py`, `web/wake_setup.py`, `scripts/_offline_wake_count.py` (12 sites) |
| `JASPER_VOICE_PROVIDER` | `voice_provider` | `voice/provider_state.py`, `web/voice_setup.py`, `deploy/bin/jasper-aec-reconcile` (7) |
| `JASPER_PEERING` | `peering` | `peering/config.py`, `cli/doctor/peering.py`, `web/rooms_setup.py` (6) |
| `JASPER_WAKE_THRESHOLD` | `wake_threshold` | `control/aec_endpoints.py`, `web/wake_setup.py` (6) |
| `JASPER_SPOTIFY_ACCOUNTS_PATH` | `spotify_accounts_path` | `mux.py`, `control/volume_ops.py`, `web/__main__.py`, `web/spotify_setup.py` (5) |
| `JASPER_AEC_CHIP_AEC_ENABLED` | `aec_chip_aec_enabled` | `mics/xvf3800.py`, `audio_profile_state.py`, `cli/aec_init.py` (4) |
| `JASPER_CAMILLA_HOST` / `_PORT` | `camilla_host/_port` | `camilla.py`, `control/server.py`, `cli/doctor/_cli.py` (3 each) |
| `JASPER_WEATHER_{LAT,LON,DISPLAY_NAME}`, `JASPER_TRANSIT_{LAT,LON,DISPLAY_NAME}` | 6 fields | all re-parsed in `jasper/location_state.py` |

Several of these are defensible (a wizard reads its own file before writing it, a
doctor reads without building a `Config`), but `location_state.py` re-implementing
six `Config` fields and `web/__main__.py` re-reading `JASPER_SPOTIFY_ACCOUNTS_PATH`
and `JASPER_GOOGLE_ACCOUNTS_PATH` are straight duplication.

---

## 5. Config-loading mechanisms — nine of them

| # | mechanism | call sites | scope |
|---:|---|---:|---|
| M1 | **Central typed `Config`** — `jasper/config.py`, one `from_env()` | 36 `_env*` calls / 80 tokens; 40 `Config.from_env()` callers | the voice daemon and everything that imports it |
| M2 | **`jasper/env_load.py`** — `load_env_files`, `parse_env_file`, `merged_env_files`, `read_env_file_state`, `bounded_env_{float,int}`, `outputd_reconciled_env` | 78 prod call sites | CLIs/doctor that must see the union of every unit's `EnvironmentFile=` (`ENV_FILES`, 24 paths, guarded by `tests/test_env_load_mirrors_unit.py`) |
| M3 | **Ad-hoc `os.environ.get` / `getenv`** in `jasper/` | **127** direct `JASPER_*` reads across 98 files | everything else |
| M3b | **`env.get(...)` on an injected mapping** (the plugin self-parse pattern) | 36 | transit plugins, wake-corpus bridge, AEC bridge |
| M4 | **Hand-rolled `*.env` line parsers/emitters** — each module re-implements strip/`#`/`partition("=")`/dequote on the read side and the `KEY=VALUE` + newline-guard loop on the write side | **19 modules** carry the parse loop; 6 parse a `/var/lib/jasper` file (`tool_state.py:76-82`, `mic_mute_persistence.py:52-60`, `fanin/latency_mode.py:88-96`, `audio_quality.py`, `speaker_name.py`, `wifi_guardian_persistence.py`); `conversation_history.py:387-392` additionally re-implements the *emitter* | duplicates `env_load.parse_env_file` and `_common.write_env_file` exactly |
| M5 | **Rust** — `jasper-env` crate (`env_str`/`env_f32`/`env_parse`) plus per-crate `env_list`/`env_u32_positive`/`env_enabled`/`env_csv_labels`/`env_optional_with_default` re-declared in `rust/jasper-fanin/src/config.rs:1192-1280` and `rust/jasper-outputd/src/config.rs:916-960` | 85 helper calls / 22 raw `std::env::var` | fanin, outputd |
| M6 | **Bash** — `deploy/lib/jasper-env-file.sh` (`jasper_env_file_set`/`_unset`/`_quote_value`, atomic) sourced by 2 reconcilers; **plus** `set_env_file_var`/`_if_changed` wrappers (`jasper-audio-hardware-reconcile:570-575`), **plus** `set_jasper_env_value` (`deploy/install.sh:1416` — a `sed -i` delete + `>>` append, **non-atomic and non-quoting**, and install.sh never sources the lib), **plus** raw `printf >> "$MODE_FILE"` in `jasper-aec-reconcile:307-322` | | |
| M7 | **systemd** — 100 `EnvironmentFile=` lines, 19 `Environment=` lines | | the real production writer of several "defaults" |
| M8 | **Python env-file writers** — `jasper/web/_common.py:469 write_env_file` (no lock, no header) and `jasper/atomic_io.py:478 locked_update_env_file` (locked) | 17 / 6 | `write_env_file` lives in `web/` but is imported by `control/debug_control.py`, `control/usb_gadget_forensics.py`, `wake_corpus/bridge_session.py` — a layering inversion |
| M9 | **udev / ALSA / nginx** — `ENV{JASPER_DONGLE_CARDNUM}` (`deploy/udev/99-jasper-apple-dongle.rules:40`), ALSA conf.d reading `renderer_lanes.env`, nginx reading `tool_state.env` path | 3 | |

### Convergence target (per `docs/extensibility.md` §Step 2 pattern-selector)
The doc's three sanctioned patterns are **central typed `Config`** / **self-contained
module + registry (`env_keys` + `build_client(env)`)** / **pure-data registry +
reconciler-as-single-writer**. M1, M3b and M6/M7 are those three. Everything else is
accidental:

1. **M4 → M2.** Delete the 6 hand-rolled `/var/lib/jasper/*.env` parsers (and
   `conversation_history.py`'s hand-rolled emitter); call
   `env_load.parse_env_file` / the shared writer. `debug_mode.py`, `identity_state.py`,
   `conversation_history.py` and `voice/provider_state.py` already do — the pattern
   is proven, four modules just didn't get the memo. (~120 lines out.)
2. **M8 → M2.** Move `write_env_file` out of `jasper/web/_common.py` into
   `jasper/env_load.py` (its natural home beside the reader), and make it emit the
   `# Written by <owner>` header AGENTS.md requires — see §7 #4.
3. **M6 → the lib.** `deploy/install.sh` should source
   `deploy/lib/jasper-env-file.sh` and delete `set_jasper_env_value`; the
   `jasper-aec-reconcile` `printf >>` sites should use `jasper_env_file_set`.
   `install.sh` is non-negotiable-tier per AGENTS.md, and its bespoke writer is the
   only non-atomic one in the tree.
4. **M5.** Move the five duplicated `env_*` helpers from
   `jasper-fanin/src/config.rs` and `jasper-outputd/src/config.rs` into the
   `jasper-env` crate that already exists for exactly this.
5. **M3.** Not convergeable wholesale, and shouldn't be — but the 38 tokens read both
   via `Config` and directly (§4) should pick one.

---

## 6. Feature flags and mode switches

| flag | branches | production value | evidence | verdict |
|---|---|---|---|---|
| `JASPER_TTS_TRANSPORT` | `outputd` \| `sounddevice` | **`outputd` only** | `jasper/config.py:200-209` **raises** on any other value, incl. `sounddevice`; `deploy/systemd/jasper-voice.service:184` `Environment="JASPER_TTS_TRANSPORT=outputd"` | **one legal value** — delete the knob, the two raises, and `TTS_TRANSPORT_ENV` (`jasper/tts_routing.py:20`) |
| `JASPER_DUCK_TRANSPORT` | `fanin` \| `camilla` | **`fanin`** | `.env.example:162`, `jasper-voice.service:186`; branch at `jasper/voice/daemon_main.py:278` | `camilla` selects `Ducker` (`jasper/camilla.py:1228-1314`, 87 lines) + `tests/test_camilla_ducker.py` (387 lines) for a path nothing ships. **Delete branch, class, tests.** |
| `JASPER_FANIN_CAMILLA_COUPLING` | `shm_ring` **only** (`VALID_COUPLINGS = frozenset({COUPLING_SHM_RING})`, `jasper/fanin_coupling.py:37`) | unset/`shm_ring` | `.env.example:302`; `deploy/alsa/conf.d/60-jts-ring.conf:5` | a **one-value vocabulary** with a 999-line module (`jasper/fanin_coupling.py`) + 2152-line reconciler around it and 14 prod read sites. Not deletable wholesale (the module also owns Ring B + transport shapes) but the *coupling-choice* axis is a no-op — rightsizing target |
| `JASPER_OUTPUTD_CONTENT_BRIDGE` | `shm_ring` \| `direct`/`off`/`disabled` | **unset ⇒ `shm_ring`** | `deploy/systemd/jasper-outputd.service:76` "deliberately UNSET"; `rust/jasper-outputd/src/config.rs:374` | `jasper/control/transport_park.py:342` states outright: `direct` is "which no writer emits". The `direct` branch exists only to produce a park message |
| `JASPER_OUTPUTD_BACKEND` | `alsa` \| `fake` | `alsa`; `fake` written by the reconciler when no DAC is present | `deploy/systemd/jasper-outputd.service:69`; `deploy/bin/jasper-audio-hardware-reconcile:1993` | **live both ways.** Earns its keep |
| `JASPER_OUTPUTD_SINK` | `single_alsa` \| `composite`(alias `dual_apple`) | `single_alsa`; `dual_apple` on dual-DAC hardware | `jasper-audio-hardware-reconcile:1863,1950`; `rust/jasper-outputd/src/config.rs:325-337` | live both ways |
| `JASPER_AUDIO_ROUTE_PROFILE` | `corrected_48k` \| `usb_low_latency_48k` \| `bitperfect_passthrough_declared` | `corrected_48k` | `.env.example:159`; `jasper/audio_runtime_plan.py:158` | three-way, all reachable |
| `JASPER_INSTALL_PROFILE` | `full` \| `streambox` | both ship | `jasper/install_profile.py:64`; `deploy/lib/install/python-runtime.sh:484` | earns its keep |
| `JASPER_AEC_MODE` | `auto` \| `disabled` | `auto` | `deploy/install.sh:1536`, `jasper-aec-reconcile:338` | live (wizard flips it) |
| `JASPER_WAKE_LEG_RAW` / `_DTLN` / `_CHIP_AEC` / `_CHIP_AEC_150` / `_CHIP_AEC_210` | 5 booleans | **`RAW=1`, other four hard-seeded `0` by both writers; no code path ever sets one to 1** | `deploy/bin/jasper-aec-reconcile:307-322,338`; `deploy/install.sh:1536` | the 4 off legs are lab-only (`jasper/cli/wake_enroll.py:19`, `jasper/cli/noise_capture.py:29` tell the operator to `export` them). ~240 occurrences across `jasper/wake_legs.py`, `audio_profile_state.py`, `audio_validation.py`, `cli/doctor/wake.py`, `voice_daemon.py`, `jasper-aec-reconcile` + 4 test files. Biggest single dormant-branch surface in the tree |
| `JASPER_AEC_CORPUS_*_ENABLED` (6) | booleans | stamped per wake-corpus session | `jasper/wake_corpus/bridge_session.py` | lab feature, self-contained |
| `JASPER_FANIN_RING_WIRE_FORMAT` | `S16_LE` \| `S32_LE` | `S32_LE` (default) | `.env.example:306`; `deploy/alsa/conf.d/60-jts-ring.conf:37` calls `S16_LE` "the rollback lever" | rollback lever with no expiry note |
| `JASPER_CONVERSATION_HISTORY_ENABLED` | on/off | never written | `jasper/conversation_history.py:339,343,381` | see §2b |
| `JASPER_USB_LATENCY_MODE` | `low`\|`medium`\|`high` | `low` | `jasper/fanin/latency_mode.py:14,108` | file-key (not env); live via `/system` |

### 6a. Keys advertised on a surface that does not feed them — Should-fix
Three keys look like `jasper.env` env vars but are only ever read out of a
`/var/lib/jasper/*.env` file by a bespoke parser, so setting them where they are
advertised **silently does nothing**:

- `JASPER_DISABLED_TOOLS` — commented in `.env.example:60`; read only at
  `jasper/tool_state.py:76` by comparing line keys inside
  `/var/lib/jasper/tool_state.env`.
- `JASPER_MIC_MUTED` (`jasper/mic_mute_persistence.py:40`),
  `JASPER_USB_LATENCY_MODE` (`jasper/fanin/latency_mode.py:14`) — same shape, not
  advertised, fine, but they share the bug class.

And the **wizard host/port double-parse**: `jasper/web/__main__.py:71-82` reads
`spec.env_var` for each of 12 wizards, while each wizard module *also* has its own
`main()` with `argparse` defaults on the same key
(`jasper/web/wifi_setup.py:1485,1489`). **Eight of those `main()`s are unreachable**
— no `[project.scripts]` entry and no `python -m jasper.web.X` anywhere:
`google_setup`, `home_assistant_setup`, `rooms_setup`, `transit_setup`,
`voice_setup`, `wake_setup`, `wifi_setup`, plus `sound_setup` whose entry point
`jasper-sound-web` (`pyproject.toml:218`) is referenced nowhere outside
`pyproject.toml`. That is 8 dead `main()` blocks and ~16 dead `*_WEB_HOST`/`_WEB_PORT`
knobs. *(Overlaps issue #4031's web-UI cleanup — flagging the config half.)*

### 6b. Patch seams shipping as production code — Nit
`jasper/control/server.py:628-636` keeps `_write_audio_input_profile` and
`_atomic_rewrite_env` as thin re-exports of `jasper/control/aec_endpoints.py`, the
latter self-described as a *"compatibility patch seam for grouping env persistence"*
— i.e. production indirection that exists so tests can monkeypatch one name. This is
the `control/handlers` seam already named in issue #4030; noting the config half only.

---

## 7. Ranked simplifications (top 15, by payoff)

| # | severity | change | payoff |
|---:|---|---|---|
| 1 | Should-fix | **Delete the 4 dormant wake legs** (`JASPER_WAKE_LEG_{DTLN,CHIP_AEC,CHIP_AEC_150,CHIP_AEC_210}`) or move them behind one `JASPER_WAKE_LAB_LEGS` list key. Prod hard-seeds all four to `0` at `deploy/bin/jasper-aec-reconcile:338` and `deploy/install.sh:1536`. | largest dormant branch surface: ~240 occurrences, `jasper/wake_legs.py` (140 L), leg fan-out in `audio_profile_state.py`, `audio_validation.py`, `cli/doctor/wake.py`, `voice_daemon.py` + 4 test files |
| 2 | Should-fix | **Delete `JASPER_TTS_TRANSPORT`** — one legal value, enforced by two raises (`jasper/config.py:200-209`) | removes a knob, 2 validators, `TTS_TRANSPORT_ENV`, and the doctor branch at `cli/doctor/audio.py:541` |
| 3 | Should-fix | **Delete `JASPER_DUCK_TRANSPORT=camilla`** and with it `jasper/camilla.py:1228-1314 Ducker` + `tests/test_camilla_ducker.py` | −474 lines, one fewer duck implementation |
| 4 | Should-fix | **Give every env-file writer an owner header.** `jasper/web/_common.py:469 write_env_file` emits bare `KEY=VALUE`, so ~14 wizard- and control-owned files carry no header — AGENTS.md requires "the writer is named in each file's header". Only 4 writers comply (`identity-reconcile`, `audio_quality`, `latency_mode`, `usb_port_role`). | makes the single-writer rule checkable instead of aspirational; one 3-line change |
| 5 | Should-fix | **Fold `set_jasper_env_value` (`deploy/install.sh:1416`) into `deploy/lib/jasper-env-file.sh`.** install.sh is the only non-atomic, non-quoting env writer and never sources the lib the reconcilers use. install.sh is non-negotiable tier. | correctness + one writer instead of three bash implementations |
| 6 | Should-fix | **Retire the retirement list.** The 8-key `sed` scrub (`deploy/lib/install/python-runtime.sh:372-384`, duplicated at `:498-501`) has no expiry and its keys have been unread for months. Replace with a one-shot migration marker or delete. | −20 lines, removes permanent per-deploy machinery contra AGENTS.md |
| 7 | Should-fix | **Delete the 8 unreachable wizard `main()`s** + `jasper-sound-web` entry point and their ~16 `*_WEB_HOST/_WEB_PORT` knobs (§6a). | −~350 lines, −16 knobs, one hosting mechanism instead of two |
| 8 | Should-fix | **Converge M4 → `env_load.parse_env_file`** in the 6 modules that hand-roll `/var/lib/jasper/*.env` parsing (`tool_state`, `mic_mute_persistence`, `fanin/latency_mode`, `audio_quality`, `speaker_name`, `wifi_guardian_persistence`). | −~120 lines, one parse semantic (quote/comment handling currently differs per module) |
| 9 | Should-fix | **Fix `tests/test_env_vars_codified.py`** to assert "read ⇒ *written* by a prod surface" and drop the dead `AGENTS.md ("Codify, don't memorise")` citation. | turns a passing-but-vacuous guard into the one that would have caught the other 479 |
| 10 | Should-fix | **Delete the 40 `DEAD-no-read` tokens** and the `.env.example` tombstone block (`:293-313`) + the two live-but-unread assignments (`:396-399`). | −~25 lines of seed template that every new Pi inherits; −40 grep hits |
| 11 | Nit | **Delete the `direct` content-bridge branch** (`rust/jasper-outputd/src/config.rs:374`) — `jasper/control/transport_park.py:342` already says no writer emits it. Keep only the "unrecognized ⇒ park loud" path. | one dead transport branch in the output daemon |
| 12 | Nit | **Move `write_env_file` from `jasper/web/_common.py` to `jasper/env_load.py`** — `control/` and `wake_corpus/` currently import from `web/`. | fixes a layering inversion; free |
| 13 | Nit | **De-duplicate the `aec_mode.env` seed**: the identical 8-key `printf` at `deploy/install.sh:1536` and `deploy/bin/jasper-aec-reconcile:338`. Have install.sh invoke the reconciler's `--auto` instead. | one writer for one file |
| 14 | Nit | **Move the 5 duplicated Rust `env_*` helpers** from `jasper-fanin/src/config.rs:1192-1280` and `jasper-outputd/src/config.rs:916-960` into the `jasper-env` crate. | that crate exists for exactly this |
| 15 | Nit | **Write down the `JASPER_*` vs `JTS_*` rule** (one line in AGENTS.md: `JASPER_*` = on-device runtime, `JTS_*` = build/deploy/test) and rename `JASPER_ENV_TEST_{U32,STRING,F32}` (`rust/jasper-env/src/lib.rs:85-124`) out of the product namespace. | 45 `JTS_*` + 829 `JASPER_*` with no stated boundary |

Also worth the owner's triage, below the top 15: the **30 AEC3/WebRTC tuning knobs**
and **10 AEC UDP ports** that nobody sets (§2b) — they are a real lab surface
(`jasper/aec_sweep.py:115` registry, `experiments/aec3-v2-deep-tune-spike/`), so the
right move is probably to declare them a *self-parsed lab pack* per
`docs/extensibility.md` rather than delete them; today they are indistinguishable
from operator knobs.

---

## Coverage

**Opened and read:** `AGENTS.md`; `docs/extensibility.md` (§1-2, §6, the Step-2
pattern-selector table at :180-200); `jasper/config.py` (header, validators
:195-225, `from_env` field block); `jasper/env_load.py` (full);
`jasper/web/_common.py:460-500`; `jasper/tool_state.py`; `jasper/mic_mute_persistence.py`;
`jasper/fanin/latency_mode.py` (full); `jasper/fanin_coupling.py` (:1-50, :204-330,
:650-700, :750-810); `jasper/fanin/coupling_reconcile.py` (:1-125, :1730-1745);
`jasper/audio_runtime_plan.py:95-175`; `jasper/debug_mode.py:1-45`;
`jasper/active_speaker/state_paths.py`; `jasper/web/__main__.py:60-100, 320-525`;
`jasper/web/wifi_setup.py:1480-1495`; `jasper/voice/daemon_main.py:270-300`;
`jasper/voice_daemon.py:3120-3145`; `jasper/camilla.py:1228-1250`;
`jasper/control/transport_park.py:342`; `rust/jasper-env/src/lib.rs`;
`rust/jasper-fanin/src/config.rs` (:600-650, :1185-1230);
`rust/jasper-outputd/src/config.rs` (:300-400, :1140-1200);
`deploy/lib/install/python-runtime.sh` (:1-60, :340-400, :480-505);
`deploy/install.sh:1320-1340, 1410-1440, 1500-1545`;
`deploy/lib/jasper-env-file.sh`; `deploy/bin/jasper-audio-hardware-reconcile:565-600,
780-800, 1855-2000`; `deploy/bin/jasper-aec-reconcile:300-345, 2060-2145`;
`deploy/udev/99-jasper-apple-dongle.rules`; `deploy/systemd/jasper-outputd.service`,
`jasper-voice.service`, `jasper-snapclient/-snapserver.service`; `.env.example`
(:55-70, :120-170, :290-320, :390-400); `tests/test_env_vars_codified.py` (full);
`tests/test_systemd_hardening.py:525-625`; `pyproject.toml` scripts block.

**Machine-scanned (not each site read):** every tracked file for `JASPER_*`/`JTS_*`
occurrences (874 raw tokens, ~11k occurrences); every tracked `*.py` by AST for env
calls, subscripts, comparisons and constants; every tracked file for
`/var/lib/jasper*`, `/run/jasper*`, `/etc/jasper` path literals (281 distinct paths).

**Skipped, and why.**
- The **900 test files** were classified but not read, except the ~8 quoted. Test
  env-injection via dict literals is the main source of `knob-nobody-turns`
  over-count; a per-token confirmation would need to read them.
- **Semantics** of the AEC3 tuning knobs, the DSP math, and the ring/coupling
  transport design — out of scope for a config census; I asserted only whether a
  value is ever supplied, never whether a default is correct.
- **`docs/adr/`** (157 files) was searched, not read; ADR-0100/0101/0199/0226/0235/0236
  were consulted only where a grep pointed at them.
- **`jasper/active_speaker/`, `audio_measurement/`, `correction/`** state files were
  enumerated from path literals but their own doctrine
  (`docs/measurement-loop-doctrine.md`) was not audited — that program has its own
  rules and is covered by the tuning-rightsize waves already in flight.
- I did **not** run `scripts/test-fast` or any test; nothing was edited.
