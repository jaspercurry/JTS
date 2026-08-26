# Wake-word telemetry — operational spine

Every wake fire on this speaker writes a row to a local SQLite DB plus one
six-second WAV per active wake leg. That corpus is the feedback loop for wake
tuning: it is where "did the wake rate get better?" and "is this leg pulling
its weight?" are answered, and it is the source the gold corpus is extracted
from.

Owner of the store: `jasper/wake_events.py`. Hook call sites and leg/column
mapping: `jasper/voice_daemon.py`. Config: `jasper/config.py`
(`wake_events_dir`, `wake_events_max_audio_bytes`).

Decisions live in ADRs, not here: **ADR-0132** (the OR-gate fires now; the
false-positive cost is measured), **ADR-0133** (rows are permanent, audio is a
small ring), **ADR-0134** (labelling is post-hoc), **ADR-0130** (nothing gets
veto power upstream of the wake OR), **ADR-0129** (models are trained per leg).
The 2026-05 build narrative — the four-PR sequence, the two-leg starting
architecture, the shapes that were designed and never shipped — is in
[historical/wake-telemetry-build-2026-05.md](historical/wake-telemetry-build-2026-05.md).

---

## What gets recorded

An event is one **real wake fire**. There is no near-miss capture: a score
that never crosses a leg's threshold leaves no trace beyond the log line.
The first leg to cross wins and sets `trigger_kind`; other legs above their
own threshold with a *fresh* score at that instant are listed in `fired_legs`.

Per event:

- one row in `wake-events.sqlite3`, inserted on the wake hot path with
  `outcome='in_progress'`, then updated in place as the turn progresses;
- one WAV per captured leg — 4 s pre + 2 s post = 6 s at 16 kHz mono int16
  (`CAPTURE_PRE_SEC` / `CAPTURE_POST_SEC`), ~192 KB each, attached ~2 s later
  off the wake path.

Telemetry is fail-soft by design: a failed write logs at `WARNING` and
surfaces in doctor. Per AGENTS.md's no-silent-deafness rule this subsystem is
**not** a wake-blocking path, so a telemetry failure never plays a cue.

### Funnel stages

`update_stage(event_id, stage)` maps a stage name to its `ts_*` column
(`_STAGE_TO_COLUMN` in `jasper/wake_events.py` is the source of truth).

| Stage | Set when | Terminal? |
|---|---|---|
| `ts_wake` (`ts_utc`) | a leg's score crosses its threshold | — |
| `late_cancel` | mic muted or a correction measurement started before the turn opened | **terminal** |
| `peer_lost` | multi-Pi arbitration handed the wake to another speaker | **terminal** |
| `gate_blocked` | spend cap reached or the live connection is paused | **terminal** (plays a cue) |
| `turn_opened` | `_begin_turn()` succeeded — live session running | — |
| `speech_detected` | Silero's sustained-speech threshold crossed | — |
| `response_started` | first non-empty assistant PCM chunk reaches the shared playout drain | — |
| `tool_called` | first *registered* tool invoked in the turn | — |
| `tool_completed` | first registered-tool completion (success, error, or timeout) | — |
| `turn_complete` | turn ended naturally | **terminal** |

`speech_detected` is the false-positive proxy: a wake that opens a session and
never sees sustained speech is the strongest available signal that the fire was
spurious (music transient, TTS bleed, ambient noise).

The response and tool hooks are **provider-neutral** — the shared playout
drain owns `response_started` and shared tool dispatch owns
`tool_called`/`tool_completed`, so every provider reports the same milestones
with no adapter branches. Unknown tool names are not recorded as registered
calls. Both observer boundaries are capped at 100 ms; a wedged telemetry
callback is cancelled and logged rather than delaying speech or tool results.
The schema is a bounded **turn-level summary**: on a multi-call turn it keeps
the first registered tool's name and call timestamp and the first completion
timestamp. Those are funnel milestones, not a per-call duration when tools
overlap — correlating overlapping calls would need a stored call id, and
detailed multi-tool traces belong to the conversation/eval surfaces, not to new
unbounded wake-event columns.

`outcome` takes one of `in_progress`, `completed`, `late_cancel`, `peer_lost`,
`gate_blocked`, `no_speech`, `session_failed`, `tool_failed` (`_VALID_OUTCOMES`).

---

## Schema

`/var/lib/jasper/wake-events/wake-events.sqlite3`, WAL mode
(`synchronous=NORMAL`), autocommit, one connection per store instance with an
`asyncio.Lock` serialising writes. The directory is installed `0755 root:root`
by `stage_wake_models` in `deploy/lib/install/model-staging.sh`; jasper-voice
runs as root and creates files `0644`.

Read the DDL in `jasper/wake_events.py` (`_SCHEMA_SQL`) rather than a copy
here. The column families:

- **identity** — `event_id` (`20260522T143011Z-001`), `ts_utc`;
- **trigger** — `trigger_kind` (`fire_aec_on` | `fire_aec_off` | `fire_dtln` |
  `fire_chip_aec_150` | `fire_chip_aec_210`), `fired_legs` (sorted CSV of every
  leg above threshold at fire time), `threshold`;
- **per leg** — `peak_score_*`, `peak_offset_ms_*`, `mic_rms_dbfs_*`,
  `audio_*_path`, one set per leg (`on`, `off`, `dtln`, `chip_aec_150`,
  `chip_aec_210`);
- **funnel** — the nine `ts_*` columns above, plus `outcome`,
  `outcome_detail`, `tool_name`;
- **context at fire time** — `wake_model`, `mic_muted`, `music_active`,
  `music_renderer`, `music_volume_db`, `condition_class` (quiet/ambient/music,
  from `jasper.wake_conditions`), `voice_provider`, `bridge_config_json`;
- **session-time shadow VAD** — `max_silero_aec`, `max_silero_raw`,
  `silero_*_armed_at_ms`, `endpointer`, `transcript_nonempty`,
  `music_playing_at_turn`, `music_db_at_turn`;
- **labels** — `label`, `label_notes` (see ADR-0134).

Indexes on `ts_utc`, `outcome`, `trigger_kind`, `label`.

**Adding a column.** There is no `schema_version` table. Migration is additive
and idempotent: `open()` reads `PRAGMA table_info` and ALTERs in anything
missing. Put every new column in **both** `_SCHEMA_SQL` (fresh DBs) and
`_MIGRATION_COLUMNS` (already-deployed Pis) — a column in only the first
silently breaks INSERTs on upgraded DBs, and the fail-soft handler will hide it.

**Adding a leg** additionally needs an entry in `_LEG_DB` in
`jasper/voice_daemon.py`. The `on`/`off`/`dtln` column names are irregular for
back-compat with the historical corpus, which is why that table lists columns
explicitly instead of deriving them from the leg token.

---

## Files, retention, and disk

```
/var/lib/jasper/wake-events/
  wake-events.sqlite3        ← grows forever (~9 MB/year at 50 events/day)
  wake-events.sqlite3-wal
  wake-events.sqlite3-shm
  20260522T143011Z-001.aec-on.wav
  20260522T143011Z-001.aec-off.wav
  20260522T143011Z-001.aec-dtln.wav
  20260522T143011Z-001.aec-chip-aec-150.wav
  20260522T143011Z-001.aec-chip-aec-210.wav
  ...
```

- **Audio** — oldest-first ring, `DEFAULT_MAX_AUDIO_BYTES` = **128 MiB**,
  override `JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES`. At ~575 KB per three-leg
  event that is roughly 230 events, days rather than weeks. Pull anything
  worth keeping into the gold corpus promptly.
- **Rows** — never deleted (~500 B each).
- **Sweep** — `_retention_sweep` runs after every `attach_audio`; there is no
  timer. A running `_audio_bytes_estimate` makes the under-cap case a single
  comparison; the full stat-scan-and-prune only runs at startup (to seed the
  estimate) and when the estimate crosses the cap, on a worker thread via
  `asyncio.to_thread`. WAV writes go through the same thread hop — on a busy SD
  card, blocking file I/O on the event loop glitches the mic loop.
- **Rolled-off audio** — a deleted WAV rewrites its `audio_*_path` to
  `'rolled_off'` (`ROLLED_OFF_SENTINEL`). NULL still means "never captured", so
  filter with `audio_on_path IS NOT NULL AND audio_on_path != 'rolled_off'`.

**Doctor.** `check_wake_events_storage` (`jasper/cli/doctor/memory.py`) is a
read-only size warning on the directory. Its threshold is **derived**: the
*configured* audio cap (env override respected) plus a fixed DB/overshoot
allowance, overridable via `JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES`. A Pi that
deliberately raises the cap therefore does not warn, and a healthy ring never
warns; a warning means the reaper is wedged or the cap was raised and
forgotten. Do not replace this with a literal byte figure.

**Privacy.** Capture is entirely local — the WAVs are household audio and
nothing is uploaded anywhere. Getting them off the Pi is a deliberate operator
action.

---

## Working with the corpus

```sh
bash scripts/fetch-wake-events.sh          # snapshot DB + rsync WAVs + TSV index
open wake-events/latest/index.tsv
```

`fetch-wake-events.sh` is the canonical fetcher: it snapshots the DB via
Python's `sqlite3.backup` (a consistent read that takes no write lock against
the live jasper-voice), pulls every leg's WAVs, and writes a TSV index. Each
run lands under `./wake-events/<UTC-timestamp>/` with a `latest` symlink; the
tree is gitignored — regenerate, never commit captured audio.

Downstream, all laptop-side: `scripts/audit-wake-events.sh` (corpus health),
`scripts/analyze-three-leg.sh` (per-leg fusion breakdown),
`scripts/_extract_wake_corpus.py` (promote captures into the gold corpus),
`scripts/reset-wake-events.sh` (clear the Pi's ring). Corpus QA methodology is
[HANDOFF-wake-corpus-quality.md](HANDOFF-wake-corpus-quality.md); training is
[HANDOFF-wake-training-experiment.md](HANDOFF-wake-training-experiment.md).

### Queries

Daily funnel:

```sql
SELECT date(ts_utc) day,
       COUNT(*)                              wakes,
       SUM(ts_turn_opened      IS NOT NULL)  opened,
       SUM(ts_speech_detected  IS NOT NULL)  had_speech,
       SUM(ts_response_started IS NOT NULL)  got_response,
       SUM(ts_turn_complete    IS NOT NULL)  completed
FROM wake_events
GROUP BY day ORDER BY day DESC;
```

Which leg combinations fire — the fusion question. `fired_legs = 'dtln'` rows
are DTLN's solo-saves, i.e. wakes that exist only because that leg was added:

```sql
SELECT fired_legs, COUNT(*) fires
FROM wake_events
WHERE fired_legs IS NOT NULL
GROUP BY fired_legs ORDER BY fires DESC;
```

Suspected false accepts — the standing metric behind ADR-0132. If this trends
badly, that is the evidence that reopens the OR-gate decision:

```sql
SELECT fired_legs, COUNT(*) suspected_fp
FROM wake_events
WHERE ts_turn_opened IS NOT NULL AND ts_speech_detected IS NULL
GROUP BY fired_legs ORDER BY suspected_fp DESC;
```

Before trusting a `ts_response_started` / `ts_tool_called` query, confirm the
hooks are live for the provider in question — those columns read 0 rows on any
history recorded before the shared drain and dispatch hooks landed.

Last verified: 2026-08-26 (retention cap, capture window, funnel stages,
outcomes, trigger kinds, migration/leg-registration rules and the derived
doctor threshold rechecked against `jasper/wake_events.py`,
`jasper/voice_daemon.py`, `jasper/config.py`,
`jasper/cli/doctor/memory.py` and `deploy/lib/install/model-staging.sh`.
Near-miss capture and the `/wake-review/` wizard were removed as
never-implemented; directory ownership corrected from `pi:pi` to `root:root`.)
