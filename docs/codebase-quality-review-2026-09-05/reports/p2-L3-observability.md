# L3 — OBSERVABILITY (tree-wide lens) @ 2d571e6b8

## A. Verdict

The box has **excellent primitives and no contract**. `jasper/log_event.py` (97 LOC, one
escaper, one JSON sink), `jasper/doctor_contract.py` (one CheckResult schema, closed status
set, reason required on warn/fail), `jasper/flight_recorder.py`, `scripts/journal-review.sh`
(week-over-week `event=` deltas + never-seen-key detection), and `jasper/cues/registry.py`
are each the right shape. What is missing is one level up: **1,309 distinct event names with
no registry**, **six independent "state" builders**, **five publish mechanisms where one
would do**, and — the load-bearing gap — the three things that answer "is the speaker
broken" all fail on the *daemon-died* case. `check_outputd_service` fails without
`speaker_silent=True`; outputd's DAC vanishing emits no `event=`; `AudioCueManager.play` —
the non-negotiable-6 mechanism itself — has **zero** structured events, zero `/state` fields
and zero doctor checks, so "the speaker fell silent when it should have cued" is
unobservable by construction. The owner can tell *that* something is wrong from
`/system` and `jasper-doctor`; they mostly cannot tell *which daemon stopped and when*
without `journalctl | grep -i` on prose.

---

## 1. Publish topology

| # | producer | publishes | mechanism | read by | freshness / staleness |
|---|---|---|---|---|---|
| 1 | jasper-fanin (Rust) | STATUS JSON (~107 `push_kv`) | UDS `/run/jasper-fanin/control.sock`, `uds._local_status_json:165` | `/state.fanin` verbatim + `audio_graph.fanin` projection, doctor `evidence.fanin_status`, audio_health | live probe — fresh by construction; `null` on failure is indistinguishable from "socket busy" |
| 2 | jasper-outputd (Rust) | STATUS JSON (164 `push_kv`, incl. a **256-entry ring**) | same, `/run/jasper-outputd/control.sock` | `/state.outputd` verbatim + `audio_graph.outputd` projection, doctor, grouping_supervisor | live; same null ambiguity |
| 3 | jasper-voice | `STATUS` line protocol | UDS `/run/jasper/voice.sock`, `uds._voice_socket_command:47` | `/state.voice` | live |
| 4 | jasper-mux | `STATUS` line protocol | UDS `/run/jasper-mux/control.sock` | `/state.source_selection` | live |
| 5 | CamillaDSP | volume/meters/config path | websocket, `_camilla_status:788` | `/state.audio.*` | live |
| 6 | shairport-sync | playing bool | **`busctl` subprocess** per uncached `/state`, `mpris.shairport_playing` | `/state.renderers.airplay` | live, 2 s cap |
| 7 | jasper-control itself | supervisor counters, debug session, measurement hold | in-process singletons (`shairport/grouping/system_supervisor.snapshot()`) | `/state.resilience.*` | in-memory; **lost on restart, no marker** |
| 8 | oneshot reconcilers (audio-hardware, aec, source-intent, identity, usbgadget…) | env + JSON state files under `/var/lib/jasper/`, `/run/jasper-*/` | file write + re-read | `/state`, doctor, wizards | `output_hardware.json` carries `observed_at` (`output_hardware.py:221`) — **no reader computes its age** (grep: 0 consumers) |
| 9 | jasper-wifi-guardian (boot oneshot) | last action | **journal scraping**: 2×`nmcli` + 1×`journalctl -n 200` per uncached `/state` (`wifi_guardian_state.py:68,105,133`) | `/state.resilience.wifi_guardian`, doctor `check_wifi_guardian` (its own second copy) | timestamp from the journal line |
| 10 | multiroom cascade | restart timeline | journal scraping on a **background sampler thread** (`cascade_timeline._tick:245`) | `/state.resilience.multiroom_cascade` | `last_scan_at` published ✓ (the model) |
| 11 | jasper-voice | conversation history | **read-only SQLite** `_conversation_history_state:508` | `/state.chat` | `last_write_age_seconds` ✓ |
| 12 | systemd | unit states | `systemctl show` behind a **30 s sampler** (`system_metrics.SERVICE_STATE_INTERVAL_SEC=54`) ✓ | `/state`, `/system/snapshot`, doctor | sampler-fed |
| 13 | Home Assistant | connectivity | HTTP in a **child process** (`ha_status_cache` + `ha_probe_child`) | `/state.home_assistant`, `/system/snapshot` | `stale` flag ✓ |

**Nine mechanisms (p1-T08 C9 confirmed at HEAD).** 5 of 25 `/state` sections carry any
freshness marker; the other 20 cannot distinguish "read now" from "file last written at boot".

**Recommend one mechanism per producer class:**

| class | one mechanism | migration cost |
|---|---|---|
| long-lived daemon (fanin, outputd, voice, mux, camilla) | **UDS `STATUS` → JSON**, verbatim into `/state` | fanin/outputd already there; voice+mux are line-protocol → JSON = ~40 LOC each + `uds.py` loses two readers |
| oneshot reconciler | **one JSON state file with a mandatory `{observed_at, outcome, degraded[]}` header**, written atomically; `event=<domain>.reconciled outcome=…` on stderr | ~8 reconcilers × ~15 LOC; `reconcile.degraded` (`jasper-audio-hardware-reconcile:58,68,1855`, **write-only — no `/state`, no doctor**) becomes a field, not a marker file |
| Rust daemon | already correct (STATUS + `event=` on stderr) — just add the missing edges (§3) | ~10 `eprintln!` lines |
| **delete** | journal scraping as a *primary* (#9): wifi-guardian already writes a stash file; make the guardian write `{action, at}` into it and drop 3 subprocesses from `/state` | ~30 LOC removed, cascade_timeline keeps its sampler |

---

## 2. `event=` discipline

Measured by AST over `jasper/` (script: `scratchpad/L3-observability/count_events.py`).

| package | `log_event` | raw `event=` str | plain logger | of which WARN+ |
|---|---:|---:|---:|---:|
| jasper/ (top) | 255 | 4 | 340 | 207 |
| web | 338 | 0 | 165 | 101 |
| active_speaker | 278 | 32 | 6 | 18 |
| control | 116 | 0 | 76 | 43 |
| **voice** | **42** | 0 | **113** | 51 |
| cli | 66 | 0 | 51 | 32 |
| **correction** | **22** | 0 | **46** | 37 |
| peering | 20 | 0 | 35 | 21 |
| **tools** | **3** | 10 | 39 | 28 |
| **cues** | **3** | 0 | **31** | 18 |
| wake_corpus | 16 | 0 | 23 | 16 |
| multiroom / fanin / accessories / usbsink | 164 | 0 | 6 | 1 |
| **TOTAL Python** | **1453** | **47** | **976** | **605** |

Other languages: Rust **249** raw `event=` sites / **148** distinct names (all `eprintln!`);
bash+C **92** sites / **76** names, via **two incompatible `log_event()` helpers**
(`deploy/bin/jasper-audio-hardware-reconcile:279` takes `name, k=v…`;
`scripts/onboard.sh` takes `phase, status, k=v…`) plus 90 raw `echo`/`printf`.

**Registry: none.** 1,085 distinct Python names + 148 Rust + 76 bash ≈ **1,309**, across
**162** Python top-level prefixes, **45** of them flat (no `domain.` at all — all in
`correction/`: `ramp_start`, `duck`, `level_lock_stored`…). `correction` alone owns 254 names
in 5 spelling conventions (p1-T11 F14 confirmed).

**Guard that exists:** `tests/test_log_event_conventions.py` (255 LOC) is AST-based, CI-
enforced, and has a bounded, staleness-checked 14-file allowlist. **p0-duplicates #16
re-graded from Should-fix to Nit-in-flight**: all 57 raw sites are inside that allowlist with
a stated removal condition. **But the guard has a hole**: it only matches
`logger.<level>("event=…")`, so **8 `print("event=…")` sites bypass it entirely** —
`jasper/usb_network.py:441,456,462,468` and `jasper/audio_hardware/usb_port_role.py:919,933,943,950`
— emitting unescaped `reason={state.reason}` (free text) straight to stdout.

**State transitions with no `event=` (verified, file:line):**

| site | transition | what it logs today |
|---|---|---|
| `jasper/voice/openai_session.py:942` + `gemini_session.py:670` `_set_state` | the connection state machine that decides whether the speaker can answer | `logger.info("%s connection state: %s → %s")` — prose |
| `jasper/voice/daemon_main.py:979` `_shutdown` | jasper-voice stopping | `logger.info("shutdown requested")`; **no `voice.started`/`voice.ready` event either** |
| `jasper/mux.py:1987` `main` | jasper-mux starting/stopping | nothing at all |
| `jasper/control/server.py:2297` | jasper-control ready | prose (`control.shutdown` exists ✓) |
| `jasper/cues/manager.py:255-326` `play` | **every** cue failure incl. "user gets silence" | 5 `logger.warning` prose branches, 1 `log_event` |
| `jasper/camilla.py:158` | **NON-NEG-1 volume clamp fires** | `logger.warning("camilla main_volume clamped…")` (p1-T05 F2 confirmed) |
| `jasper/correction/autolevel.py:321,333,344,370,381` | live ramp START/LOCKED/CANCELLED/MAXED | printf; the *replaced* `level_match.py` has 6 `log_event` (p1-T11 F14 confirmed) |
| `crossover_v2/{record_store,session_seams,composition,measure_spec,program_transaction}.py` | the new tuning engine's durable-write seam | **0 `log_event` in 1,188 LOC** (p1-T13-1 F11 confirmed) |
| `jasper/output_hardware.py` (whole file) | classification / DAC vanish-return | **0** (p1-T06 F9 confirmed) |
| `rust/jasper-outputd/src/alsa_backend.rs:1638` | ENODEV/EIO — the DAC disappearing | bare `Error:` from `Termination` (p1-T20 F4 confirmed) |
| `jasper/voice_daemon.py:4643` | manual-start refused at spend cap | nothing (p1-T01 F4 confirmed) |
| `jasper/voice_daemon.py:3473` | wake dropped after research-window cancel timeout | `logger.warning` prose (p1-T01 F11 confirmed) |

**Should there be a registry? Yes — the cheapest possible one.** Not a framework: a
per-package `EVENTS: frozenset[str]` beside each daemon plus one AST test extending the
existing `test_log_event_conventions.py` to assert (a) every `log_event` name literal is in
its module's package set, (b) `domain.action` shape (kills the 45 flat names), (c) each
top-level prefix is declared in exactly one package. `scripts/journal-review.sh:157`'s
"never-seen-before key" detector is already the runtime half of this — it just runs
per-box, weekly, by hand, instead of in CI.

---

## 3. Failure-mode coverage matrix

Legend: ✓ present · ✗ absent · ~ partial. "cue" only applies where the failure is
wake-blocking or output-blocking.

| daemon | failure mode | `event=` | `/state` | doctor | cue |
|---|---|:--:|:--:|:--:|:--:|
| **jasper-outputd** | xrun storm | ✓ `outputd.xrun` | ✓ | ✓ `check_outputd_service` | n/a |
| | **DAC vanishes (ENODEV/EIO)** | ✗ bare `Error:` | ~ `outputd:null` | ✓ fail | ✗ |
| | unit dead → speaker silent | ✗ | ~ null | ✓ fail, **no `speaker_silent`** | ✗ |
| | FIFO lane orphaned (ADR-0220) | — | ✓ dead keys | ✗ | — |
| | protocol errors on a bonded member (tts.rs fork dropped `mark_protocol_error`) | ✗ | ✗ | ✗ | — |
| **jasper-fanin** | ring stall | ✓ `fanin.ring.stall_*` | ✓ | ✓ `check_fanin_ring_stall` | n/a |
| | TTS command drop | ✓ | ✓ | ✓ | n/a |
| | unit dead | ✓ `fanin.shutdown` | ~ null | ✓, **no `speaker_silent`** | ✗ |
| **CamillaDSP** | park after failed recovery | ✓ (bash) | ✓ `resilience.camilla_recover` | ✓ **`speaker_silent`** ✓ | ✗ |
| | unit dead | ✗ | ~ | ✓ `check_camilla_service`, **no `speaker_silent`** | ✗ |
| | **NN-1 clamp fires** | ✗ prose | ✗ | ~ config only (`check_camilla_volume_limit`) | n/a |
| | sustained outage | ~ DEBUG only (`camilla.operation_retry`) | ✗ | ~ | ✗ |
| **jasper-voice** | mic unavailable → park | ✓ `voice.mic_unavailable` | ✓ `voice.parked_no_mic` + `microphone` | ✓ | n/a |
| | provider unconfigured / VAD setup | ✓ | ✓ `provider_status` | ✓ | n/a |
| | connection paused / terminal outage | ~ prose `_set_state` | ✓ | ~ | ✓ |
| | **daemon start / stop** | ✗ | ~ `reachable` | ~ `check_service_runtime_state` | n/a |
| | spend cap hit (wake) | ✓ | ✓ | ✓ `check_spend_cap` | ✓ |
| | spend cap hit (**button**) | ✗ | ✗ | ✓ | **✗** |
| **jasper-aec-bridge** | ref path silent | ~ **prose regex** out of the journal | ✗ | ✓ `check_aec_bridge_output_health` | ✗ |
| | daemon start/stop | ✗ | ✗ | ✓ `check_aec_bridge_running` | n/a |
| **jasper-mux** | source handoff | ✓ (25 `log_event`) | ✓ | ✓ | n/a |
| | daemon start/stop | ✗ | ~ null | ✓ `check_jasper_mux` | n/a |
| **jasper-control** | overload / shutdown | ✓ | n/a | ✓ | n/a |
| | polkit denial on a unit mutation | ✗ (200-and-lie, p1-T08 C1) | ✗ | ✗ | n/a |
| **audio-hardware-reconcile** | classifier crash | ~ `state_written_failed`, **stderr `2>/dev/null`** | ✗ | ~ | n/a |
| | **`reconcile.degraded` set** | ✗ | ✗ | ✗ | n/a |
| | state file stale (`observed_at` old) | ✗ | ✓ field published, **0 readers** | ✗ | n/a |
| **cue subsystem** | **no baked WAV → user gets silence** | ✗ prose | ✗ | ✗ | — |
| | regenerate failed | ✓ `cue.regenerate_failed` | ✗ | ✗ | — |
| | TtsPlayout.write failed | ✗ prose | ✗ | ✗ | — |

**Non-negotiable 6 — wake-blocking paths and their cues:**

| path | file:line | cue? |
|---|---|---|
| spend cap (wake) | `voice_daemon.py:3838` | ✓ `spend_cap_reached` |
| connection paused (wake) | `:3856` | ✓ `wake_cue()` |
| turn-acquire exception | `:3898-3900` | ✓ |
| no room microphone (button) | `:4620` | ✓ |
| connection paused (button) | `:4658` | ✓ |
| mic muted / measurement open | `_wake_late_cancelled:3916` | ✗ **correct** — household chose the silence |
| peering LOSE | `:3822` | ✗ **correct** — a peer answers |
| **spend cap (button)** | **`:4643`** | **✗ GAP** (p1-T01 F4) |
| **research-window cancel timeout** | **`:3473`** | **✗ GAP** (p1-T01 F11) |
| **`MEASURE_PAUSE` opens during acquire → unbounded `await resumed.wait()`** | **`voice/output_gate.py:96-105`** | **✗ GAP, up to 120 s deaf** (p1-T01 F3) |
| **cue plays but no WAV exists** | `cues/manager.py:282-287` | **✗ GAP — the cue mechanism silently fails** |
| **outputd/camilla dead → cue cannot reach the DAC** | — | **✗ GAP — no cue path exists that survives it** |

`tests/test_cue_registry_coverage.py` proves registry↔play-site *symmetry*; nothing proves
*coverage* of wake-blocking paths, and nothing observes cue playout at runtime.

---

## 4. Journal spam and cadence

**There is no cadence policy.** No line in AGENTS.md, no ADR, no shared constant. Measured
per-emitter constants are ad hoc: `DEFERRED_EXIT_LOG_PERIOD_SEC=300`,
`LIVE_DRAFT_UNAVAILABLE_LOG_INTERVAL_SEC=30`, `_ACCOUNT_FAILURE_LOG_INTERVAL_SEC=600`,
`CONTROL_OVERLOAD_LOG_INTERVAL_SEC=5`, Rust `CATCHUP_LOG_EVERY=64`,
`DRAIN_STATS_LOG_EVERY=1<<15`.

**#4118 (in flight, 5 s→15 s RMS) is the symptom, not the disease.** Its own measurement:
`jasper-aec-bridge` writes ~1,750 lines/h, ~715 of them this one INFO line (24–32 % of all
journal lines); journald turns over every ~3 days at `SystemMaxUse=500M`. That implies
~165 MB/day, while `deploy/journald/50-jts-persistent-storage.conf:14` still claims
"~50 MB/day" — a **stale comment contradicted by the PR editing the emitter**.

The real defect underneath: the emitter is a **prose format string** (`aec_bridge.py:1007,1021`)
and `jasper/cli/doctor/aec.py:662,666` parses it back out with two regexes including a
Unicode `→`. That coupling is why #4118 needed a threshold-reachability essay to change a
constant. `log_event(logger, "aec_bridge.rms", ref=…, mic=…, attenuation_db=…)` makes the
doctor's parse a logfmt read and decouples cadence from thresholds. (AGENTS.md: "Never
assert on … log/error prose — assert types, codes, and structured fields." The doctor is
doing exactly that, at runtime.)

Other findings: `camilla._call:448` fixed a "~4 Hz journal flood" by **demoting to DEBUG**
rather than backing off (p1-T05 F3 confirmed) — the flood is still there, just invisible.
60 INFO+/`log_event` sites sit inside sleeping loops across 25 files; the concentrations
are `correction/autolevel.py` (7), `voice/openai_session.py` (6), `measurement_window.py` (5).
No `RateLimitIntervalSec`/`RateLimitBurst` is set, so systemd's 10 000/30 s default silently
drops messages with no doctor check for the suppression notice.

**Policy worth one paragraph in AGENTS.md:** *periodic telemetry is DEBUG unless a
machine reads it; a machine-read periodic line is `log_event` with a named
`*_LOG_INTERVAL_SEC` constant beside its reader's window; edges (state changes) are INFO,
outcomes WARNING+, and nothing repeats an unchanged fact at INFO.*

---

## 5. `/state`

**Measured** (`scratchpad/L3-observability/statesize.py`, all probes stubbed null):
25 top-level keys, **260 leaf fields, max depth 4, 6,956 bytes floor**, one 159-line dict
literal (`state_aggregate.py:1266-1425`), 34 function-local imports, 13 `_soft_read` sites.

**On a real chip-AEC box it is ~32 KB**, because `"outputd": probes.outputd` (`:1333`) is
verbatim and outputd's STATUS carries up to 256 `recent_writes` entries
(`rust/jasper-outputd/src/state.rs:78`). The repo's own test measures it:
`tests/test_control_uds.py:330` — *"the array alone is ~25.6 KB"*. Its **only** reader is
`jasper/cli/aec_init.py:111`, a commissioning one-shot.

**Build cost, uncached:** 1 websocket + 4 UDS round-trips + **1 `busctl` + 2 `nmcli` +
1 `journalctl -n 200`** (`wifi_guardian_state.py`), each ≤3 s, plus ~13 sync file reads on
the event-loop thread. `_STATE_AGGREGATE_BUDGET_SEC = 20.0`.

**Consumers — and this corrects p1-T08 C7.** No browser page polls `/state`: nginx has **no
`location /state`** block (`deploy/nginx-jasper.conf`, 50 locations checked). The landing page
and `/system` poll `/system/snapshot` via `web/system_setup.py:127`; `/sources` and `/wifi`
poll their *own* wizards' `./state`. The real readers are **five**: `doctor/_evidence.py:287`
(memoized once per run, used by `resilience.py:192`, `voice.py:332`, `wake.py:149`),
`jasper/audio_validation.py:582`, and a human's `curl`. So the 1 s `_SingleFlightTTLCache`
and the "hot path" framing are sized for a polling load that does not exist — **re-grade
p1-T08 C7 from "hot-path breach" to "ADR-0233 rule-2 breach + a needless 25 KB payload"**;
the fix is still right, the urgency is on the doctor's 600 s budget, not a 4 s poll.

**Proposed slim contract** (delivers ADR-0233 rule 2's three unshipped items —
`schema_version`, key-set pin, no per-request spawns):

```python
@dataclass(frozen=True)
class StateSection:            # one wrapper, 4 fields
    value: Any                 # the section payload, or None
    observed_at: float         # epoch of the read that produced it
    source: str                # "uds" | "file" | "memory" | "sampler"
    error: str | None = None   # why value is None, when it is

# GET /state -> {"schema_version": 1, "ts": …, "sections": {name: StateSection}}
```

Three concrete moves, each independently shippable: (a) add `schema_version: 1` + a
`test_state_top_level_keys` pin — ~15 LOC; (b) delete `audio_graph.fanin` /
`audio_graph.outputd` (`:296-327`), which re-project the same two probes already carried
verbatim — ~40 LOC removed; (c) drop `reference_outputs.chip_ref_writer.recent_writes` from
`/state.outputd` (keep it on the socket for `jasper-aec-init`) — one filter, −25 KB/response.

---

## 6. Same fact in N places

| fact | homes at HEAD | ADR-0233 target |
|---|---|---|
| "is the USB combo armed" | `doctor/usbsink.py:866` **≡ byte-identical ≡** `state_aggregate.py:501` | `fanin/coupling_auto.combo_armed(text)`; both consume |
| wifi stash matches active NM profile | `doctor/network.py:238-300` vs `control/wifi_guardian_state.py:70-230` (each forks `nmcli`) | one reader in `wifi_guardian_persistence.py` |
| conversation-history health | `doctor/web.py:271-291` vs `state_aggregate.py:508-551` (two `ConversationStore(read_only)` opens) | `conversation_history.health()` |
| fanin/outputd probe values | `/state.fanin` + `/state.audio_graph.fanin` + `audio_health.signal_path` (3 renderings, 1 response + 1 route) | verbatim only |
| transport park | `/state.resilience.transport_park`, `/system/snapshot.transport_park`, `doctor.check_ring_transport_park` | **already one reader** ✓ (the model) |
| memory-headroom thresholds | `doctor/memory.py` **≡ hand-mirrored ≡** `deploy/assets/system-status/js/format.js:101` (a test pins the mirror) | generate the JS constants |
| "did the install leave the box healthy" | `deploy/bin/jasper-deploy-health` (900 LOC, its own `OK/FAIL/WARN` printf shape, ~25 rows) vs 172 doctor checks | **rule 5: `--core` subset; `--core` does not exist anywhere in the repo (grep → 0)** |
| "what is the box's state" | **six builders**: `/state`, `/system/snapshot` (`handlers/system.py:106`, 14 keys, 7 overlapping), `/sources/state` (`sources_setup._gather_state:324`), `/wifi/state`, `/bluetooth/state`, `/sound/state` | two surfaces, per rule 2 |

**Doctor at HEAD (re-verified, script `doctorfail.py`): 172 registered checks, 88 with no
reachable `fail`, 3 that can only be `ok`/`skipped`.** p1-T10's 172/87 confirmed.
`speaker_silent=True` reachable from only **6 checks / 8 sites**
(`audio.check_camilla_ring_chunk_fits`, `audio.check_active_speaker_runtime_graph`, the
three `audio_runtime_ring` park checks, `audio_runtime_camilla.check_camilla_recover_park`).
**Not** `check_outputd_service`, **not** `check_fanin_service`, **not**
`check_camilla_service` — the three "the daemon that makes sound is dead" checks.

**Is `--core` the right cut for the deploy gate? Yes, with one amendment.** The gate should
be *"every check that can set `speaker_silent`, plus required-units-active"* — a
`--speaker-silent-or-fail` selection, not a hand-curated core list that will drift the way
`deploy-health` did. That makes the gate's meaning derivable from the contract rather than
from a second roster, and it forces (7) below: any check that can prove silence must say so.

---

## 7. Top 15 fixes, ranked by payoff / diff

| # | sev | fix | file:line | diff |
|---|---|---|---|---|
| 1 | **Blocker** | `speaker_silent=True` on the daemon-dead branches of `check_outputd_service`, `check_fanin_service`, `check_camilla_service` — today the dashboard headline "the speaker is silent" cannot fire when the DAC owner is dead | `doctor/audio_runtime_{outputd,fanin,camilla}.py` | 3 kwargs + 3 test pins |
| 2 | **Blocker** | Instrument `AudioCueManager.play`: `log_event` on every branch (`cue.played`, `cue.missing_asset`, `cue.write_failed`, `cue.no_playout`) + a `cues` block in `/state` (last slug, last outcome, missing-asset count) + `check_cue_assets` in the doctor. NN-6's mechanism is currently unobservable | `cues/manager.py:255-326` | ~30 LOC + 1 check |
| 3 | **Should-fix** | Emit `event=outputd.dac.write_failed pcm=… errno=… action=exit` on the non-xrun ALSA arm (confirms p1-T20 F4) | `rust/jasper-outputd/src/alsa_backend.rs:1638` | 1 `eprintln!` |
| 4 | **Should-fix** | Convert the AEC-bridge RMS line to `log_event(logger, "aec_bridge.rms", …)` and make `doctor/aec.py` read logfmt fields instead of 2 prose regexes — unblocks #4118's cadence change from threshold archaeology | `cli/aec_bridge.py:1007,1021`; `doctor/aec.py:662-694` | ~40 LOC, deletes 2 regexes |
| 5 | **Should-fix** | Cue + `log_event` on the three NN-6 gaps: button-CAP (`voice_daemon.py:4643`), research-cancel timeout (`:3473`), and a bounded `begin_turn()` (`output_gate.py:96`) | 3 files | ~15 LOC (p1-T01 F3/F4/F11) |
| 6 | **Should-fix** | Drop `recent_writes` from `/state.outputd` (keep on the socket) + delete the `audio_graph.fanin`/`audio_graph.outputd` re-projections | `state_aggregate.py:296-327,1333` | −40 LOC, −25 KB/response |
| 7 | **Should-fix** | Land ADR-0233 rule 5 as `--speaker-silent-or-fail` (not a curated core list); delete `deploy/bin/jasper-deploy-health` + its 2 call sites | `doctor/_cli.py`, `install.sh:2173`, `deploy-to-pi.sh:571` | −900 LOC |
| 8 | **Should-fix** | `log_event` the NN-1 clamp: `camilla.volume_clamped requested_db= clamped_db=` at WARNING, + a `/state.audio.clamp_count` | `camilla.py:158` | 5 LOC (p1-T05 F2) |
| 9 | **Should-fix** | `event=` on daemon lifecycle for the four Python daemons that lack it: `voice.started`/`voice.stopped`, `mux.started`/`mux.stopped`, `control.started` | `voice/daemon_main.py:979`, `mux.py:1987`, `control/server.py:2297` | ~10 LOC |
| 10 | **Should-fix** | Make `reconcile.degraded` a published fact: fold it into `output_hardware.json` as `degraded: [codes]`, surface at `/state.audio.output_hardware.degraded`, add a doctor `fail` | `jasper-audio-hardware-reconcile:58,68,1855`; `output_hardware.py` | ~25 LOC (p1-T06 G5) |
| 11 | **Should-fix** | Structure the connection state machine: `log_event(logger, "voice.connection", provider=…, from=…, to=…)` in both `_set_state`s | `voice/openai_session.py:942`, `gemini_session.py:670` | 2 sites |
| 12 | **Should-fix** | Close the conventions-guard hole: extend `test_log_event_conventions.py` to `print("event=…")`; migrate the 8 sites | `tests/test_log_event_conventions.py`, `usb_network.py`, `usb_port_role.py` | ~20 LOC |
| 13 | **Should-fix** | Per-package `EVENTS: frozenset[str]` + AST test for membership and `domain.action` shape; fixes the 45 flat `correction/` names and the 5 conventions | new test + 1 constant per package | ~60 LOC total |
| 14 | **Nit→Should-fix** | Replace the wifi-guardian journal scrape with a field in the stash the guardian already writes; drop 3 subprocesses per `/state` and the doctor's duplicate verdict | `wifi_guardian_state.py:105,133`, `doctor/network.py:238` | ~30 LOC removed |
| 15 | **Nit** | Add `schema_version: 1` + a top-level key-set test to `/state`; give `/system/snapshot` its overlapping keys from `_get_state`'s sections rather than re-reading | `state_aggregate.py:1266`, `handlers/system.py:106` | ~20 LOC |

Also worth 5 minutes each: wire `scripts/journal-review.sh` to a weekly timer (it is the only
tool that treats `event=` as a vocabulary and it is invoked by nothing);
`flight_recorder.install()` in `mux` / `accessories` / `usbsink-volume` (3 of 7 daemons have
it); fix the stale "~50 MB/day" in `deploy/journald/50-jts-persistent-storage.conf:14`
against #4118's ~165 MB/day; drop `2>/dev/null` on the classifier spawn
(`jasper-audio-hardware-reconcile:393`) so a traceback reaches the journal.

---

## D. What only hardware/runtime can prove

- The real `/state` byte size on a chip-AEC full speaker (I measured the 6,956 B null floor
  and cite the repo's own 25.6 KB ring measurement; the sum is inferred, not observed).
- Whether the wifi-guardian `nmcli`/`journalctl` triple actually costs ~3 s under memory
  pressure, and whether it ever trips `_STATE_AGGREGATE_BUDGET_SEC`.
- Current per-unit journal line rates beyond #4118's aec-bridge measurement — i.e. whether
  the 500 MB / ~3-day turnover is aec-bridge alone or systemic.
- Whether journald's default rate limiter is actually dropping messages today
  ("Suppressed N messages" in `journalctl`).
- The `p1-T01 F3` 120 s deafness window: reachable only by opening `MEASURE_PAUSE` inside the
  wake acquire window on metal.
- Whether a cue can be heard at all when outputd is dead (matrix row asserts the code path,
  not the acoustic outcome).

## E. Coverage

**Opened:** `jasper/log_event.py` (full), `jasper/flight_recorder.py` (head),
`jasper/control/state_aggregate.py` (`_get_state` 1085-1425, `_audio_graph_state`,
`_multiroom_cascade_snapshot`, `_read_output_hardware`, `_augment_source_payload`,
`_conversation_history_state`, `_read_audition_state`),
`jasper/control/{wifi_guardian_state,uds,client,server(main+cache),handlers/system}.py`,
`jasper/cli/doctor/{_cli,_evidence(control_state),resilience,audio(volume_limit,
output_hardware_state),audio_runtime_outputd,audio_runtime_camilla,web,usbsink,aec(regexes,
fallback),memory(journald)}.py`, `jasper/doctor_contract.py`,
`jasper/cues/{registry,manager}.py`, `jasper/voice_daemon.py` (3440-3960, 4600-4700),
`jasper/voice/{output_gate,daemon_main,openai_session._set_state}.py`,
`jasper/mux.py` (transitions + main), `jasper/camilla.py:150-170,440-478`,
`jasper/correction/{autolevel,session._set_state}.py`, `jasper/cli/aec_bridge.py:975-1035`,
`jasper/multiroom/cascade_timeline.py:230-300`, `jasper/output_hardware.py` (state schema),
`jasper/web/{sources_setup._gather_state,system_setup}.py`,
`rust/jasper-outputd/src/{state.rs(snapshot_json, ring rationale),alsa_backend.rs:1610-1645,
main.rs:140-160}`, `deploy/bin/{jasper-deploy-health(head+main),jasper-audio-hardware-reconcile
(log_event,degraded),jasper-bootloop-guard}`, `deploy/systemd/*.service` (Type/StartLimitAction
sweep + `jasper-doctor-json.service`), `deploy/journald/50-jts-persistent-storage.conf`,
`deploy/nginx-jasper.conf` (locations), `deploy/assets/system-status/js/actions.js`,
`scripts/journal-review.sh`, `tests/{test_log_event_conventions,_log_events,test_control_uds:270-350,
test_wire_contracts(head)}.py`, `docs/adr/0233`, PR #4118 (full body via API).

**Mechanical sweeps over 100 % of `jasper/`** (AST, scripts in
`scratchpad/L3-observability/`): `log_event` vs raw-`event=` vs plain-logger per package;
event-name vocabulary, prefix and shape distribution; `print("event=…")` detection; doctor
check registration + fail/warn reachability 4-deep; INFO+ logging inside sleeping loops;
`/state` key/leaf/byte measurement with stubbed probes.

**Skipped:** the 21 k-LOC doctor test suite; `audio_health.py`/`airplay_health.py` bodies
(sampler verdicts — read their `/state` and `/system/snapshot` seams only); `jasper/web/`
wizard internals beyond the four `/state` route handlers; Rust bodies outside
`state.rs`/`alsa_backend.rs`/`main.rs`.
