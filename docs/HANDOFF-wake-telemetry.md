# Wake-word telemetry — design, schema, and PR plan

> **Update 2026-05-22 night:** the 2-leg design described in this
> doc shipped in PR #191. The **next extension is 3-leg + planned
> 4-leg (with custom wake model)** per
> [HANDOFF-mic-quality-v2.md](HANDOFF-mic-quality-v2.md) "Triple-stream
> architecture plan". Schema additions for that extension (see
> "Planned schema extensions for triple-stream" below) are designed
> to slot into the existing ALTER-migration pattern without
> breaking changes.

This document is the canonical reference for the wake-event
telemetry subsystem: multi-stream wake-word detection (today: AEC ON
+ AEC OFF, OR-gated; next: + DTLN-aec leg; future: + custom-trained
wake model) plus per-event persistence to SQLite with audio capture
and full funnel tracking through to LLM response / tool call.

**Why this exists.** As of 2026-05-21, the 2026-05-20 wake-rate
sweep showed 14 of 20 Jarvis utterances stayed at 0.001 confidence
across ALL 23 AEC configurations — i.e. AEC tuning has reached its
useful range against the current `jarvis_v2` openWakeWord model.
Synthetic phone-track testing (`scripts/wake-rate-test.sh`) is a
poor proxy for real user-attempt distribution and is brutal to
iterate on. This subsystem replaces the synthetic-test feedback
loop with **production telemetry on real attempts**:

- Two streams scored on every frame → fires on either → roughly
  +15 percentage points over the better single leg, per test-1
  aligned A/B data (HANDOFF-aec.md "Open work streams — option C").
- Every wake event persists to SQLite with both per-leg peak
  scores, the funnel timestamps through to tool completion, the
  music/provider context, and pointers to the captured WAVs.
- Audio captures retained in a 500 MB ring buffer; DB rows kept
  forever for long-baseline funnel stats.

Companion docs:
- [HANDOFF-aec.md](HANDOFF-aec.md) — full AEC investigation +
  current tuning. Read its "Open work streams — option C" section
  before working on this subsystem.
- `NEXT_SESSION_PROMPT.md` at repo root — earlier scoping of this
  same problem. Superseded by this doc; delete that file when PR 3
  lands (the telemetry that makes the synthetic test obsolete).

---

## Architecture

### Two streams, one consumer

```
                    chip ch 1 (ASR beam, BF+NS+AGC+HPF,
                    chip AEC disabled via SHF_BYPASS=1)
                                   │
                                   ▼
                          jasper-aec-bridge
                          ┌────────────────────┐
                          │   chip-direct mic  │  ──── udp:127.0.0.1:9877 ─┐
                          │   (pre-AEC)        │                            │
                          │         │          │                            │
                          │         ▼          │                            │
                          │   WebRTC AEC3      │                            │
                          │   (existing)       │                            │
                          │         │          │                            │
                          │         ▼          │                            │
                          │   AEC ON output    │  ──── udp:127.0.0.1:9876 ─┤
                          └────────────────────┘                            │
                                                                            ▼
                                                          jasper-voice WakeLoop
                                                          ┌──────────────────────┐
                                                          │ Two UdpMicCapture    │
                                                          │ instances, one model │
                                                          │ per leg, OR-gated    │
                                                          └──────────────────────┘
```

Both streams carry **16 kHz mono int16, 1280-sample (80 ms)
packets** — the same contract jasper-voice already uses for the
AEC ON stream today. The chip-direct stream is what AEC3 receives
as its near-end input, before any AEC3 processing.

### Funnel — the stages a wake event passes through

Identified from `jasper/voice_daemon.py:_handle_wake_frame` and
`_arbitrate_acquire_drain`:

| Stage | Set when | Terminal? |
|---|---|---|
| `ts_wake` | Wake-word score crosses threshold on either leg | — |
| `ts_late_cancel` | Mic muted or correction measurement active before turn opens | **terminal** |
| `ts_peer_lost` | Multi-Pi arbitration handed it to another speaker | **terminal** |
| `ts_gate_blocked` | Spend cap reached OR live connection paused | **terminal** (plays cue) |
| `ts_turn_opened` | `_begin_turn()` succeeded → live session running | — |
| `ts_speech_detected` | Silero VAD's sustained-speech threshold crossed | — |
| `ts_response_started` | First audio/text chunk back from the LLM | — |
| `ts_tool_called` | Model invoked a registered tool | — |
| `ts_tool_completed` | Tool returned (success or error) | — |
| `ts_turn_complete` | Turn ended naturally | **terminal** |

`ts_speech_detected` is the natural false-positive proxy: a wake
that opens a session but never sees sustained speech is the
strongest signal that the wake was spurious (music transient, TTS
bleed-through, ambient noise). PR 3's query layer surfaces this
per leg.

### Capture trigger

An "event" is recorded to disk when:
- Either leg's score crosses the production wake threshold (a real
  fire), OR
- Either leg's score crosses **0.10** in any 80 ms frame within
  any 6 s window (a near-miss worth keeping)
- 5-second refractory between captures so one user attempt = one
  event, even if scores oscillate

For each event:
- `<event_id>.aec-on.wav` — 4 s pre + 2 s post = 6 s of the AEC ON stream
- `<event_id>.aec-off.wav` — same wall-clock window, chip-direct stream
- DB row in `wake-events.sqlite3` populated (see schema below)

---

## Schema

`/var/lib/jasper/wake-events/wake-events.sqlite3`, WAL mode, one
row per wake event. Mode 0644, owned by `pi` so the operator can
read it without sudo.

```sql
CREATE TABLE wake_events (
  event_id            TEXT PRIMARY KEY,    -- '20260522T143011Z-001'
  ts_utc              TEXT NOT NULL,       -- ISO8601 wake-detect moment

  -- Trigger info — which leg(s) fired
  trigger_kind        TEXT NOT NULL,       -- 'fire_both' | 'fire_aec_on' | 'fire_aec_off' | 'near_miss'
  peak_score_aec_on   REAL,                -- NULL if leg's UDP stream wasn't producing frames
  peak_score_aec_off  REAL,
  peak_offset_ms_on   INTEGER,             -- where in the captured window the peak landed
  peak_offset_ms_off  INTEGER,
  threshold           REAL NOT NULL,

  -- Funnel timestamps (ISO8601, NULL = didn't reach)
  ts_late_cancel      TEXT,
  ts_peer_lost        TEXT,
  ts_gate_blocked     TEXT,
  ts_turn_opened      TEXT,
  ts_speech_detected  TEXT,
  ts_response_started TEXT,
  ts_tool_called      TEXT,
  ts_tool_completed   TEXT,
  ts_turn_complete    TEXT,

  -- Terminal outcome
  outcome             TEXT NOT NULL,       -- 'completed' | 'late_cancel' | 'peer_lost' |
                                           -- 'gate_blocked' | 'no_speech' | 'session_failed' |
                                           -- 'tool_failed' | 'in_progress'
  outcome_detail      TEXT,                -- free text or reason code
  tool_name           TEXT,                -- if tool called, which one

  -- Context at capture time
  wake_model          TEXT NOT NULL,
  music_active        INTEGER NOT NULL,    -- 0 | 1
  music_renderer      TEXT,                -- 'spotify' | 'airplay' | 'bt' | NULL
  music_volume_db     REAL,
  voice_provider      TEXT,                -- 'gemini' | 'openai' | 'grok'
  bridge_config_json  TEXT,                -- {"ns": "low", "agc1": true, "mic_gain_db": 6, ...}

  -- Audio
  audio_on_path       TEXT,                -- relative to /var/lib/jasper/wake-events/
  audio_off_path      TEXT,

  -- Human-supplied (PR 4)
  --   manual triage via sqlite3 CLI: 'real_attempt' | 'music' | 'tv' |
  --   'ambient' | 'unclear' | 'mute_or_correction' | ...
  --   voice-tool-written (jasper/tools/diagnostic.py):
  --     'voice_flagged'  — user said "flag that"; complaint in label_notes
  --     'flag_action'    — the wake of the "flag that" utterance itself,
  --                        filter out of real-interaction rollups
  label               TEXT,
  label_notes         TEXT
);

CREATE INDEX idx_wake_events_ts        ON wake_events(ts_utc);
CREATE INDEX idx_wake_events_outcome   ON wake_events(outcome);
CREATE INDEX idx_wake_events_trigger   ON wake_events(trigger_kind);
CREATE INDEX idx_wake_events_label     ON wake_events(label);
```

**Write pattern:**
- `INSERT` on wake-detect, populating trigger fields + context +
  `outcome='in_progress'` + audio paths.
- `UPDATE` one timestamp column at each funnel transition.
- `UPDATE` `outcome` + `outcome_detail` at terminal state.
- All writes via prepared statements; SQLite handles per-row
  atomicity in WAL mode without explicit transactions.

### Planned schema extensions for triple-stream (2026-05-22 night)

The triple-stream architecture in
[HANDOFF-mic-quality-v2.md](HANDOFF-mic-quality-v2.md) extends the
current 2-leg AEC ON/OFF setup to a 3-leg system (raw + BEST_A AEC3
+ DTLN-aec). Adds these columns via the existing `_MIGRATION_COLUMNS`
ALTER-on-`open()` pattern:

```sql
-- Per-leg peak score (joins peak_score_aec_on / _aec_off)
ALTER TABLE wake_events ADD COLUMN peak_score_dtln_aec REAL;

-- Per-leg WAV path (joins audio_aec_on_path / _aec_off_path)
-- Stored as absolute path; literal "rolled_off" once aged out of the ring.
ALTER TABLE wake_events ADD COLUMN audio_dtln_path TEXT;

-- Which leg(s) actually crossed threshold and triggered the event.
-- CSV format, sorted alphabetically. Examples:
--   "aec_off"             — raw mic only
--   "aec_on,dtln"         — BEST_A + DTLN both fired
--   "aec_off,aec_on,dtln" — all three fired (consensus)
-- Critical column for the weekly review: answers "is engine X
-- ever the lone trigger?"
ALTER TABLE wake_events ADD COLUMN fired_legs TEXT;
```

Future extension for the custom-trained wake-word model (when that
track ships):

```sql
-- Per-leg peak score for the custom model against each AEC stream.
-- Naming: peak_score_<wake_model>_<aec_leg>. So if we add a
-- custom-trained "jasper_v1" model with AEC OFF + AEC ON + DTLN
-- legs, that's 3 more columns.
ALTER TABLE wake_events ADD COLUMN peak_score_jasper_v1_aec_off REAL;
ALTER TABLE wake_events ADD COLUMN peak_score_jasper_v1_aec_on  REAL;
ALTER TABLE wake_events ADD COLUMN peak_score_jasper_v1_dtln    REAL;
```

The `fired_legs` column generalizes to include wake-model
identification too — `"jasper_v1@aec_on,jarvis_v2@dtln"` etc. The
shape isn't fixed yet; finalize when implementing.

**Audio ring sizing**: today's 500 MB cap assumes 2 streams per event.
With 3 streams per event we'd retain ~2-4 weeks instead of 3-6.
**Bump to 1 GB** via `JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES=1073741824`
when the 3rd leg ships. Pi 5 has plenty of disk (39 GB free per
CLAUDE.md debug dump).

---

## File layout

```
/var/lib/jasper/wake-events/
  wake-events.sqlite3        ← grows forever (~9 MB/year at 50 events/day)
  wake-events.sqlite3-wal    ← WAL journal
  wake-events.sqlite3-shm    ← shared-memory index
  20260522T143011Z-001.aec-on.wav    ← 6 s, 16 kHz mono = 192 KB
  20260522T143011Z-001.aec-off.wav   ← 6 s, 16 kHz mono = 192 KB
  20260522T143011Z-002.aec-on.wav
  20260522T143011Z-002.aec-off.wav
  ...
```

**Retention split:**
- **Audio WAVs** — 500 MB ring buffer, oldest-first deletion. At
  ~400 KB per event (two 192 KB WAVs + JSON overhead), holds
  ~1250 events ≈ 3-6 weeks at typical use. Cleanup runs once per
  hour or when total exceeds the cap.
- **DB rows** — kept indefinitely. Per-row footprint is small
  (~500 B), so even 10 years at 50 events/day stays under 100 MB.
  The row is useful for funnel stats long after the audio has
  rolled off.

When the audio is rolled off, the DB row keeps `audio_on_path` /
`audio_off_path` but a sentinel value (NULL or `'rolled_off'`) is
written; queries can filter by `audio_on_path IS NOT NULL` to
restrict to events that still have audio on disk.

### Pulling the corpus to a laptop

`scripts/fetch-wake-events.sh` is the canonical fetcher. It
snapshots the DB via Python's `sqlite3.backup` (consistent read
without taking a write lock against the live jasper-voice), rsyncs
both legs' WAVs back, and generates a TSV index for at-a-glance
browsing:

```sh
bash scripts/fetch-wake-events.sh
open wake-events/latest/index.tsv
```

Each run lands under `./wake-events/<UTC-timestamp>/`; the
`wake-events/latest` symlink points at the most recent fetch. The
`./wake-events/` tree is gitignored — regenerate on demand, don't
commit captured audio.

### Capture-ring fill — primary vs secondary loop

Both legs need to feed their respective capture rings or the
audio at fire time will be empty / single-leg.

- Primary loop (`run()`): appends each frame to
  `_capture_ring_on` after the pre-roll append, gated past
  `_mic_muted` / `_measurement_active` so privacy promises hold.
- Secondary loop (`_wake_secondary_loop`): appends each frame to
  `_capture_ring_off` in the same gating position. **Easy to
  forget** — the secondary loop's job is wake-detection scoring;
  the capture-ring append is a separate concern that has to be
  remembered separately. Shipped without it in the integration
  branch; the result was `audio_off_path` NULL on every event
  even though dual-stream was firing. Fix: explicit append before
  the `_acquiring` / `_state` checks (so a wake fire's window
  still has pre-fire context even if the utterance overlaps the
  wake-to-turn-open buffer window).

---

## PR plan

Four PRs, each independently shippable. Sequenced because PR 2
depends on PR 1's UDP stream, PR 3 depends on PR 2's per-frame
scores, PR 4 (optional) consumes PR 3's DB.

### PR 1 — Bridge emits second UDP stream

**Scope:** `jasper-aec-bridge` adds a second non-blocking UDP
socket on `127.0.0.1:JASPER_AEC_UDP_PORT_RAW` (default 9877). It
emits the same chip-direct mic stream that AEC3 receives as
near-end input — pre-AEC3, post the chip's own BF + NS + AGC +
HPF (chip AEC stays disabled via `SHF_BYPASS=1`). Same 16 kHz
mono int16, 1280-sample packet format as the existing AEC ON
stream on 9876.

**No behavior changes elsewhere.** Pure plumbing. jasper-voice
continues to consume only 9876.

**Files touched:**
- `jasper/cli/aec_bridge.py` — new constants, second socket in
  `_aec_loop`, mic-bytes also sent to the raw port
- `tests/test_aec_bridge_stall.py` (or new file) — hardware-free
  pytest asserting both sockets emit at the right rate

**Done when:** existing pytest passes, new test green, manual
sanity check on the Pi via `nc -ul 9877 | head -c 2560 | wc -c`
shows frames arriving.

### PR 2 — Wake loop ingests both streams + OR-gate fires

**Scope:** jasper-voice's wake loop opens UDP on `:9876` and
`:9877`. Two `WakeWordDetector` instances (same model file, same
threshold). Every frame scored on both. **Fires on either leg
crossing threshold.** Shared 0.7 s refractory across legs — one
wake event regardless of which leg fired (or both).

The wake-detect log line and the data passed to
`_arbitrate_acquire_drain` carries both per-leg peak scores so PR 3
can persist them.

**Risk:** OR-gating without per-config FP measurement is the
explicit user-accepted trade. The existing spend cap (voice_daemon.py:1503)
+ Silero VAD sustained-speech gate (the natural FP filter)
protect against runaway cost. PR 3's `ts_speech_detected`-null
query is the post-deploy signal for whether FPs are exploding.

**Files touched:**
- `jasper/audio_io.py` — maybe parameterize `UdpMicCapture` port
  (it already is — just spin up two)
- `jasper/voice_daemon.py` (`WakeLoop`) — two detectors, OR-gate,
  per-leg scores in the event payload
- `jasper/wake.py` — possibly small changes if scoring needs to be
  exposed for batching
- `tests/test_voice_daemon_wake.py` or similar — assert OR-gate
  semantics + shared refractory + per-leg scores reach the funnel

**Done when:** in production, `event=wake.detected` log line
includes `score_on=X.XX score_off=Y.YY` and either crossing
threshold fires the wake.

### PR 3 — SQLite + capture + funnel hooks

**Scope:**
- New module `jasper/wake_events.py` — SQLite writer, capture
  trigger logic, ring buffer for retention.
- Schema migration on first run (idempotent `CREATE TABLE IF NOT
  EXISTS`).
- Funnel hook calls added at each stage in `_handle_wake_frame` /
  `_arbitrate_acquire_drain` / session callbacks.
- Audio capture: when an event triggers, 4 s of mic ring buffer
  is dumped (both legs) + 2 s forward captured + WAV files written
  to `/var/lib/jasper/wake-events/`.
- Retention loop: once per hour OR on event-write, sum directory
  size; if over 500 MB, delete oldest WAVs (NOT the DB rows) until
  under cap, mark deleted rows in DB.

**Files touched:**
- `jasper/wake_events.py` — new module
- `jasper/voice_daemon.py` — funnel hooks at each stage
- `deploy/install.sh` — `mkdir -p /var/lib/jasper/wake-events
  && chown pi:pi /var/lib/jasper/wake-events`
- `tests/test_wake_events.py` — schema migration, insert/update
  shape, retention pruning, sentinel for rolled-off audio

**Done when:** after a deploy, hitting the speaker with a few
"Hey Jarvis" gives DB rows visible via:
```sh
sqlite3 /var/lib/jasper/wake-events/wake-events.sqlite3 \
  "SELECT event_id, trigger_kind, peak_score_aec_on, peak_score_aec_off, outcome FROM wake_events ORDER BY ts_utc DESC LIMIT 5"
```

### PR 4 (optional, separable) — `/wake-review/` web UI

**Scope:** new web wizard at `http://jts.local/wake-review/`,
socket-activated like the other wizards. Sortable manifest table
(timestamp, peak scores, trigger kind, music state, outcome),
audio players per leg, score-timeline sparklines, label
dropdown that writes back to DB.

Browse and label post-hoc. NO real-time labeling, NO "I just said
Jarvis" button (deferred — see "Decisions" below).

**Files touched:**
- `jasper/web/wake_review.py` — new module
- `deploy/systemd/jasper-web.socket` — add port
- `deploy/nginx-jasper.conf` — `location /wake-review/`
- `jasper/web/templates/` (or inline HTML, per existing wizard
  patterns)

---

## Useful queries (post-PR 3)

**Daily funnel:**
```sql
SELECT date(ts_utc) day,
       COUNT(*)                                       wakes,
       SUM(ts_turn_opened     IS NOT NULL)            opened,
       SUM(ts_speech_detected IS NOT NULL)            had_speech,
       SUM(ts_response_started IS NOT NULL)           got_response,
       SUM(ts_tool_called     IS NOT NULL)            called_tool,
       SUM(ts_turn_complete   IS NOT NULL)            completed_naturally
FROM wake_events
WHERE trigger_kind LIKE 'fire%'
GROUP BY day
ORDER BY day DESC;
```

**Which leg is doing most of the triggering** (triple-stream):
```sql
SELECT trigger_kind,
       COUNT(*) fires,
       AVG(peak_score_aec_on)   avg_on,
       AVG(peak_score_aec_off)  avg_off,
       AVG(peak_score_dtln_aec) avg_dtln
FROM wake_events
WHERE trigger_kind LIKE 'fire%'
GROUP BY trigger_kind
ORDER BY fires DESC;
```

Possible `trigger_kind` values: `'fire_aec_on'`, `'fire_aec_off'`,
`'fire_dtln'`. The triple-stream OR-gate awards exactly one leg per
event (the one that crossed threshold first); other legs that were
also above threshold at fire time are recorded in `fired_legs`.

**Which combinations actually fired** (fired_legs Venn breakdown):
```sql
SELECT fired_legs,
       COUNT(*) fires,
       AVG(peak_score_aec_on)   avg_on,
       AVG(peak_score_aec_off)  avg_off,
       AVG(peak_score_dtln_aec) avg_dtln
FROM wake_events
WHERE fired_legs IS NOT NULL
GROUP BY fired_legs
ORDER BY fires DESC;
```

The headline question of the triple-stream experiment lives here:
events with `fired_legs = 'dtln'` are DTLN's solo-saves — wakes
caught only because the third leg was added. Run after a week and
check the count.

**AEC OFF saved a wake the OTHER two legs would have missed:**
```sql
SELECT event_id, ts_utc, fired_legs,
       peak_score_aec_on, peak_score_aec_off, peak_score_dtln_aec,
       audio_off_path
FROM wake_events
WHERE fired_legs = 'off'
  AND peak_score_aec_on < 0.10
  AND (peak_score_dtln_aec IS NULL OR peak_score_dtln_aec < 0.10)
ORDER BY ts_utc DESC;
```

**DTLN saved a wake the AEC legs would have missed:**
```sql
SELECT event_id, ts_utc, fired_legs,
       peak_score_aec_on, peak_score_aec_off, peak_score_dtln_aec,
       audio_dtln_path
FROM wake_events
WHERE fired_legs = 'dtln'
  AND peak_score_aec_on  < 0.10
  AND peak_score_aec_off < 0.10
ORDER BY ts_utc DESC;
```

**Suspected false positives per leg** (the FP cost of OR-gating —
events that opened a turn but never saw sustained speech):
```sql
SELECT trigger_kind, COUNT(*) suspected_fp
FROM wake_events
WHERE ts_turn_opened     IS NOT NULL
  AND ts_speech_detected IS NULL
GROUP BY trigger_kind
ORDER BY suspected_fp DESC;
```

**Suspected false positives broken down by which legs fired
together** (catches "OR-gate FPs concentrate when DTLN agrees with
nothing" patterns):
```sql
SELECT fired_legs, COUNT(*) suspected_fp
FROM wake_events
WHERE ts_turn_opened     IS NOT NULL
  AND ts_speech_detected IS NULL
  AND fired_legs IS NOT NULL
GROUP BY fired_legs
ORDER BY suspected_fp DESC;
```

**Tool-call completion rate per provider:**
```sql
SELECT voice_provider, tool_name, COUNT(*) called,
       SUM(ts_tool_completed IS NOT NULL) completed
FROM wake_events
WHERE ts_tool_called IS NOT NULL
GROUP BY voice_provider, tool_name
ORDER BY voice_provider, called DESC;
```

---

## Decisions made

These were resolved in the 2026-05-21 design conversation. Listed
here so future sessions don't relitigate them without new evidence.

1. **OR-gate fires immediately**, not after FP measurement. User
   call: "right now it's just triggering too little, too
   infrequently." FP cost monitored via the
   `ts_speech_detected`-null query above. Revisit only if that
   metric goes bad.
2. **Near-miss capture floor = 0.10** on either leg. Lower captures
   too much pure-music noise. The 14/20 silent-at-0.001 utterances
   from the 2026-05-20 sweep are inherently invisible to capture
   regardless of floor — the model produces no signal at all on
   them. Catching those needs option G (retraining), not lower
   floors.
3. **DB kept forever; audio in a 500 MB ring buffer.** Long-term
   funnel stats survive even after audio rolls off. Funnel queries
   degrade gracefully when audio is gone.
4. **No "I just said Jarvis" button / no rolling mic buffer for
   user-reported misses.** Phone-grab latency is too high to make
   the button useful; the in-memory ring buffer would already have
   rolled past the actual miss event. False-negative capture
   remains the domain of `scripts/wake-rate-test.sh` for now.
5. **No real-time FP flagging in the web UI.** Post-hoc labeling
   on the day's captures is sufficient. Adds UX complexity for the
   same data.

---

## What's NOT in scope

- **Reference-coherence gate** between mic and reference at the
  detection moment (HANDOFF-aec.md option C's "coherence" part).
  Defer; only add if `ts_speech_detected`-null shows AEC OFF
  generating intolerable FPs from music.
- **Custom wake-word retraining** (HANDOFF-aec.md option G). The
  labeled corpus produced by PR 3 + PR 4 is the prerequisite — if
  later we want to train, we'll have it ready. Not part of THIS
  subsystem.
- **Web UI real-time labeling** (per decision 5).
- **User-reported miss button** (per decision 4).
- **Audio compression** (FLAC). Could 2× the retention horizon but
  adds a CPU cost per write and a dependency. Trivial to add later
  if 500 MB / 3-6 weeks isn't enough.

---

## Operational notes

**Disk safety.** PR 3's retention loop is the only safeguard
against the speaker filling its SD card with WAVs. The cap is
hard (500 MB), runs on every write OR hourly, deletes oldest-first.
`jasper-doctor` adds a check: directory size + count of audio
files, warning if either exceeds expected bounds. Loss-of-write is
also surfaced (would indicate the WAL or the directory itself is
unwritable, e.g. SD card going read-only).

**Privacy.** All capture is local; no telemetry leaves the Pi.
The WAVs contain household audio, so directory permissions are
`0644` files, `0755` directory, owner `pi:pi`. Operator can rsync
or scp out for analysis. No automatic upload anywhere.

**Cue policy.** Per CLAUDE.md "no silent failure," this subsystem
is telemetry — not a wake-blocking code path. A telemetry write
failure does NOT trigger an audio cue. It logs to journal at
`WARNING` and surfaces in jasper-doctor.

**Backward compatibility.** PR 1 alone is safe to deploy without
PRs 2-4; the second UDP stream just goes unconsumed. Nothing
listens on 9877 until PR 2 ships. PR 2 alone (without PR 3) gives
dual-stream wake triggering with no persistence — still useful
but loses the funnel data. The full value lands with PR 3.
