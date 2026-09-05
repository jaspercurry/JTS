# Audit report: JTS microphone / AEC input path

Scope read in full: `jasper/cli/aec_bridge.py` (1294), `aec_bridge_capture.py` (253), `aec_bridge_config.py` (459), `aec_bridge_corpus_lanes.py` (454), `aec_bridge_engines.py` (329), `aec_bridge_reference.py` (234), `aec_bridge_telemetry.py` (458), `jasper/wake_legs.py` (140), `jasper/wake_fusion.py` (74), `jasper/wake_conditions.py` (86), `jasper/wake_condition_context.py` (85), `jasper/wake_events.py` (920), `jasper/openwakeword_guard.py` (122), `jasper/wake.py`, `jasper/audio_io.py` (relevant sections), `jasper/voice_daemon.py` (relevant sections), `jasper/chip_aec/policy.py` (417), `jasper/mics/xvf3800.py` / `jasper/usb_mic.py` (relevant sections), `deploy/systemd/jasper-aec-bridge.service`, `jasper-voice.service`, `jts-mic.slice`/`jts-audio.slice`, `deploy/bin/jasper-aec-reconcile` (relevant sections), `deploy/udev/99-jasper-aec-reconcile.rules`, ADR-0104, ADR-0139, ADR-0170, README.md AEC section.

---

### 1. Process/boundary design

**Two Python interpreters run continuously on the mic path**: `jasper-aec-bridge` and `jasper-voice`. Everything else on the path is a non-Python daemon (`jasper-outputd`, `jasper-fanin`, `camilladsp` — Rust/C++). `jasper-aec-init`/`jasper-aec-commission`/`jasper-aec-reconcile` are one-shot/event-driven, not resident.

The split is deliberately justified in-repo, not an accident:
- `BridgeStalled`'s docstring (`jasper/cli/aec_bridge.py:199-211`) states the actual failure mode the boundary defends against: PortAudio's `InputStream` callback can simply stop being invoked after a USB underrun, with **no in-process recovery path** — "only a new process gets a working stream." A callback wedge holding the GIL in-process would freeze the same interpreter running the live LLM WebSocket session (`jasper-voice`), which is strictly worse than losing wake detection alone. In-process threads cannot provide this isolation: SIGKILL-and-restart of one OS process without touching the other is exactly what threads inside one interpreter can't do against a wedged C extension holding the GIL.
- `jasper-aec-bridge.service:66-77` documents the same reasoning at the systemd layer (`WatchdogSec=30s` + `Restart=on-failure`, `TimeoutStopSec=5s` short so a wedge reaches SIGKILL+restart fast rather than corrupting kernel state).
- `jasper-aec-bridge.service:108-117` explicitly rejects `CPUSchedulingPolicy=fifo` because it crash-loops the bridge's DTLN/AEC3 model-load startup against `LimitRTTIME` — evidence this boundary was tuned against a real production incident, not designed in the abstract.

**Chip-AEC mode is not "just a forwarder" in the sense that would make a process unjustified.** With `production_chip_aec_enabled=True`, `engine=None` (`aec_bridge.py:1178`) so no WebRTC AEC3 runs, but the process still: holds the live PortAudio `InputStream` (the same wedge risk `BridgeStalled` exists for), demuxes 6 mic channels into `raw0`/`chip_aec_150`/`chip_aec_210` queues (`aec_bridge_capture.py:122-132`), applies the post-AEC gain/soft-clip (`_apply_mic_output_gain`, `aec_bridge.py:228-240`), runs the stall/starvation watchdogs, and writes the stats/RMS telemetry. So the process boundary's actual justification (capture-thread isolation) survives chip-AEC mode intact; what disappears is only the AEC3 CPU cost (`~3-8%` of one Pi 5 core, `aec_bridge.py:12-13`), not the reason for the boundary.

**Memory.** No `MemoryMax`/`MemoryHigh` is set on `jasper-aec-bridge.service` (grepped; absent). This is not an oversight: ADR-0104 (`docs/adr/0104-per-daemon-memory-caps-stay-deferred.md:14,19-25,29-38`) records the one hard data point — `jasper-aec-bridge` measured at **42 MB in zram** during a stress test — and explicitly defers per-daemon caps everywhere except the audio-path swap protection (`jts-mic.slice`/`jts-audio.slice`, `MemorySwapMax=0`) until a named trigger fires (leak, xruns correlated with pressure, a known-leaky new dep). `jasper-voice.service:306-323` by contrast documents "~150 MB Pss" and sizes `MemoryHigh=256M` at ~2.5x. Net picture on a 1 GB Pi: ~150 MB (voice) + ~42-80 MB (bridge, more if DTLN's onnxruntime is enabled) plus the Rust/C++ output chain — a bounded, previously-measured footprint, not a runaway one. Recommendation: none — the deferral is evidence-gated and documented; forcing a cap "while auditing" would violate ADR-0104's own stated discipline.

**Per-leg cost lands in `jasper-voice`, not in extra processes.** Each configured wake leg gets its **own** `openwakeword.model.Model` / `onnxruntime.InferenceSession` inside the single voice-daemon process (`jasper/voice/daemon_main.py:1108-1118`: "each leg gets its own detector — same model file + threshold, only the input stream differs"). ADR-0170 (`docs/adr/0170-...md:8-14,32`) is the exact right-sizing decision here: selecting `xvf_chip_aec` resets every `JASPER_WAKE_LEG_*` boolean to `0` so **the default chip-AEC install runs exactly one detector**; the software-AEC3 profile defaults `JASPER_WAKE_LEG_RAW=1` (`deploy/bin/jasper-aec-reconcile:338,2129`) so it runs **two** by default (`on` + `off`) — a deliberate recall-maximizing tradeoff (ADR-0132), not an oversight.

---

### 2. Bridge internals — `_aec_loop`

`_aec_loop` spans `aec_bridge.py:351-1059` (708 lines including its docstring), decorated `# noqa: PLR0915` (`:351`) — the codebase's own lint tooling already flags it as too-many-statements and the author suppressed the check rather than split it further.

**Map of responsibilities** (in execution order):
1. **Setup** (`377-597`, ~220 lines): resolve config, compute gain/stall thresholds, build every leg's `LegEmitter` (`add_emitter` closure, `416-467`), call into `aec_bridge_corpus_lanes.build_corpus_lanes` (`470-494`) for every corpus-only lane, assemble the startup log line, and publish the initial `_bridge_stats.set_active_capture_plan(...)` snapshot (`543-577`).
2. **Debug-WAV setup** (`598-628`): optional `JASPER_AEC_DEBUG_RECORD_DIR` WAV writers.
3. **Per-frame loop body** (`640-1038`, ~400 lines): stall/watchdog check → drain `mic_q` (with `BridgeStalled` escalation) → carry-forward `ref_q` → emit chip-direct raw leg → drain+emit `raw0` (+ its two optional XVF-raw0 engines) → drain+emit chip-AEC beam queues → resolve production-chip-AEC primary frame *or* run the primary AEC3 `engine.process()` → resolve the USB-mic-export source (leg fallback logic, `782-841`) → run the optional DTLN leg (`842-886`) → run the AEC3-sweep dispatcher → drain+process the optional USB corpus lanes (`890-928`) → write debug WAVs → apply output gain/soft-clip and emit the primary `on` leg → emit the USB-host-mic export leg → update RMS accumulators → every 5s, log the RMS/attenuation line and reset counters (`984-1038`).
4. **`finally`** (`1039-1059`): close every emitter/engine/WAV writer.

**Sizes/queues**: `FRAME_SAMPLES=320` (20 ms @ 16 kHz, `aec_bridge_engines.py:30-31`); `OUT_FRAME_SAMPLES=1280` (80 ms, 4 AEC frames batched per UDP packet, `aec_bridge_telemetry.py:36-41`); `QUEUE_MAXSIZE=32` for every queue (`aec_bridge.py:179`, ~640 ms of buffering at 20 ms/frame). Stall watchdog: continuous-empty counter (`consecutive_empty_sec`, default 5 s via `JASPER_AEC_STALL_RESTART_SEC`) plus `_MicStarvationWatchdog` (`243-306`) for the slow-drip case the continuous check structurally misses (a trickle of 1 frame/several-seconds keeps resetting the continuous counter to 0 forever) — 10 s rolling windows, 3 consecutive starved windows (~30 s) before restart. Both are well-reasoned, not defensive-for-hypotheticals: the slow-drip watchdog's docstring (`244-260`) names the exact gap the simpler check misses.

**Emitters per leg**: up to 13 possible (`on`, `usb_host_mic`, `off`/raw, `raw0`, `chip_aec_150`, `chip_aec_210`, `xvf_raw0_webrtc_aec3`, `xvf_raw0_dtln`, `ref`, `usb_raw`, `usb_webrtc`, `usb_dtln`, `dtln`, + N AEC3-sweep variants), but production defaults are 2-3 active (`on` + `raw0` in chip-AEC mode; `on` + `off` + `raw0` in software-AEC3 mode); the rest are corpus-only and off by default (`aec_bridge_corpus_lanes.py:5-12`).

**Telemetry writer cadence**: `_bridge_stats_writer` (`aec_bridge.py:193-196`) writes the JSON snapshot every 0.5 s via atomic tmp+rename (`aec_bridge_telemetry.py:325-332`).

**CPU per frame**: AEC3 documented at "~3-8% of one Pi 5 core" (`aec_bridge.py:12-13`); DTLN adds "~1.5 ms of DTLN inference per frame" against the 20 ms budget (`aec_bridge.py:842-845`, i.e. ~7.5% of a core), run only when explicitly enabled.

**Decomposition**: the module was *already* decomposed once — capture, config, corpus lanes, engines, reference, telemetry each split out with their own test files (`tests/test_aec_bridge_{capture,config,corpus_lanes,engines,reference,telemetry,stall}.py`). `_aec_loop` is the remaining orchestrator, and its length is driven by a real product requirement (up to 13 independently-togglable legs for the wake-corpus recorder), not by disorganization — it's also well covered by `tests/test_aec_bridge_stall.py`'s scripted-queue harness (`_ScriptedMicQ`), which lets one iteration through then raises `Empty`. Concrete, low-risk cut: extract the periodic RMS/logging block (`984-1038`, ~55 lines) into `_log_periodic_rms(...)`, and the USB-mic-source-selection block (`782-841`, ~60 lines) into `_resolve_usb_mic_frame(...)` — both are self-contained given their inputs and would shrink the loop body by ~110-150 lines with no behavior change. I would not chase further: the remaining body is one sequential per-frame pipeline, and splitting it further trades one long function for several tightly-coupled short ones passing the same dozen locals around.

---

### 3. Legs vocabulary

`jasper/wake_legs.py` is genuinely the single source of truth for the *port/token/kind* triple (`REGISTRY`, `10-124`) — its own docstring (`17-23`) documents that it replaced two independently-drifting definitions, and `by_token()`/`leg_default_port()` are consumed by both `aec_bridge_config.py:64-65` and `voice_daemon.py` (`by_token`, `wake_input_legs`, imported `voice_daemon.py:56`). `_LEG_DB` in `voice_daemon.py:787-812` is a *different, deliberate* axis — the wake-events column mapping per leg — and its own comment (`959-968`) enforces at `WakeLoop.__init__` time that every configured leg has an entry, raising at startup rather than at fire time. This is correct layering, not duplication: one registry owns wire identity, the other owns telemetry-column identity, and a startup assertion keeps them from silently diverging.

Two minor, low-severity soft-duplications found:
- `jasper/audio_profile_state.py:443-468` hardcodes the literal `":9876"` three times in human-readable display strings, rather than deriving it from `wake_legs.by_token("on").udp_port`. Display-only (no functional consequence), but would go silently stale if the "on" port ever changed.
- `deploy/bin/jasper-aec-reconcile:2069-2073` (bash) redeclares the five port defaults as its own literals (`AEC_UDP_PORT="${JASPER_AEC_UDP_PORT:-9876}"`, etc.) — unavoidable since bash can't import `wake_legs.py`, but it is a second hardcoded copy of the same numbers. This is meaningfully de-risked already: `tests/test_aec_reconcile.py:2300-2438` asserts the reconciler's actual env-file output against `wake_legs.by_token(...).udp_port`, so a drift would be caught by CI, not just by inspection.

**Concurrency on defaults**: chip-AEC profile (the auto-resolved default on qualifying XVF3800 + supported-DAC hardware, README.md:215-224) runs **1** wake leg/openWakeWord model by design (ADR-0170); software-AEC3 profile (the fallback/non-XVF path) runs **2** by default (`on` + `off`, `JASPER_WAKE_LEG_RAW=1` default, `jasper-aec-reconcile:338`). The two fixed chip beams (`chip_aec_150`/`chip_aec_210`) are opt-in custom toggles, off by default (`jasper-aec-reconcile:315-319`), each costing one more resident model if turned on.

---

### 4. Speculative scaffolding

**`WakeFuser` is provably a no-op today and matches the audit's own example exactly.** Its only construction site in production is `voice_daemon.py:1000`: `self._fuser: WakeFuser = WakeFuser()` — always empty offsets. `grep -rn "WakeFuser("` across the repo shows the *only* other constructions are in tests (`tests/test_wake_fusion.py`, `tests/test_voice_daemon_wake_{dual,triple}_stream.py`), and there is no config/env/corpus loader anywhere (`grep` for `fuser_offsets`/`threshold_offsets`/etc. returns nothing) that could ever populate a non-empty `offsets` dict in production. Concretely, at the two live call sites (`voice_daemon.py:3394-3396,3423-3425`), `effective_threshold(leg, condition, base) == base + {}.get(...) == base` always — so `firing_threshold` is provably always `detector.threshold`. And `verify()` (`wake_fusion.py:53-74`) unconditionally `return True`, making the `if not self._fuser.verify(...)` branch at `voice_daemon.py:3445-3453` (the `wake.suppressed` log event) dead code today. This is exactly the class + always-True `verify()` the audit brief named. It is not sloppy — the module docstring (`wake_fusion.py:5-22`) explicitly stages it for "Phase 1.3"/"Phase 1.4" — but per AGENTS.md's own rubric ("placeholders should be deleted until needed"), it is a candidate for deletion now and reintroduction when a real offset/verification source lands (git history preserves the design). Net effect of deleting: `wake_fusion.py` (-74 lines), `tests/test_wake_fusion.py` (-~60 lines), the dead `verify()` branch and its log event in `voice_daemon.py` (-~15 lines), plus trivial simplification of the two threshold call sites to direct attribute reads.

**`classify_condition`/`ConditionContext` (`wake_condition_context.py`) are NOT dead** — despite feeding the same currently-inert fuser, `classify_condition` is called on the real wake-fire hot path (`voice_daemon.py:3598-3601`) and its result is recorded into `wake_events.condition_class` (`3613`, schema at `wake_events.py:155`), which real offline tooling consumes (`scripts/_analyze_three_leg.py`, `scripts/_audit_wake_events.py`). It has independent value today as an observability label even though the threshold-offset consumer is inert. Do not delete.

**The wake-corpus lanes are not speculative** — `jasper/wake_corpus/` (5065 lines across `capture_plan.py`, `bridge_session.py`, `recording_backend.py`) is an actively used, well-tested tool with its own web wizard (`jasper/web/wake_corpus_setup.py`) and a dozen dedicated test files (`tests/test_wake_corpus_*.py`, `tests/test_aec_bridge_corpus_lanes.py`, etc.). The complexity it adds to `_aec_loop`/`aec_bridge_corpus_lanes.py` is real product cost for a real, exercised feature (the owner's wake-model training pipeline), not dead scaffolding — it just happens to also be the biggest single driver of `_aec_loop`'s length (item 2 above).

---

### 5. `wake_events` telemetry

`begin_event()` **is** awaited synchronously on the wake-fire path (`voice_daemon.py:3604`, inside a `try/except` that logs-and-continues on failure, `3618-3623`) — but it's a single SQLite WAL-mode `INSERT`, documented and designed to be ~1 ms (`wake_events.py:9-14,411-413`). The heavier work — `attach_audio()`'s up-to-five WAV writes — is deliberately **not** on the hot path: it runs as a fire-and-forget background task (`_finalize_event_audio`, `voice_daemon.py:3626-3630`, sleeping `CAPTURE_POST_SEC` first) with its file I/O and the retention-sweep directory scan both pushed to `asyncio.to_thread` (`wake_events.py:39-45,508-510,820-822`) specifically because a busy SD card could otherwise stall the same event loop the mic path shares. This is a correctly-designed hot-path/cold-path split, not a hazard.

**Bounded**: `DEFAULT_MAX_AUDIO_BYTES = 128 MiB` (`wake_events.py:82`), enforced by an oldest-first retention sweep (`_retention_sweep`, `796-834`) that keeps an incremental byte estimate so the common case is one comparison, not a directory stat-walk (only re-scans when the estimate crosses the cap). DB rows are kept forever by design (ADR-0133) with only the audio path columns rolled to a sentinel — a considered, documented tradeoff (structured rows are cheap; audio is not).

**Right-sized for a one-owner device?** Given what actually consumes it — `jasper/cli/wake_enroll.py`, `jasper/cli/wake_score.py`, `jasper/cli/doctor/aec.py` and `memory.py`, and five `scripts/*wake*` offline tools, plus the `flag_recent_issue` voice tool (`record_flag`, `wake_events.py:663-760`) — this is more than "a simple structured log line" would provide: it supports a *query surface* (get_event, per-leg score comparison, offline corpus extraction) a flat log can't. Given how much of this repo's wake-quality work (ADR-0129/0130/0132) is corpus-driven, the SQLite store is justified machinery, not gold-plating.

---

### 6. Resilience

- **Bridge death → recovery**: `Restart=on-failure`/`RestartSec=2` (`jasper-aec-bridge.service:98-103`) recovers a crashed or `BridgeStalled`-exited bridge in ~2 s typically, well under `jasper-voice`'s own `WatchdogSec=30s` (`jasper-voice.service:94`) — so a normal bridge blip never even triggers voice's watchdog. Escalation: 4 restarts in 300 s → `StartLimitAction=reboot` (`jasper-aec-bridge.service:49-55`), citing the reason (`MemorySwapMax=0` slice + clean unmount need).
- **No application-level "no mic frames" detection independent of the OS watchdog.** `UdpMicCapture` (`audio_io.py:264-333`) has no receive-timeout of its own — `frames()` just `await`s the queue forever. The only liveness signal for a persistently-dead bridge is `WakeLoop.run()`'s `Heartbeat.bump()` on each received frame (`voice_daemon.py:2436-2437`), which only manifests as `jasper-voice`'s own 30 s systemd watchdog killing and restarting the *whole voice daemon* — a restart that does not fix a dead bridge and plays no cue (a hard watchdog-triggered process kill gives the daemon no chance to run cue-playing code). If the bridge stays down for reasons that don't hit its own `StartLimitAction=reboot` quickly (e.g. its readiness marker being withheld), this could cycle silently for a while before any operator-visible signal beyond the journal/doctor. This doesn't fully match AGENTS.md's "no silent deafness" clause (which is scoped to *new* code paths), but it's the one gap in this subsystem closest to that concern, and there's no proactive cue for "wake detection has been dark for N seconds" today — only after-the-fact `jasper-doctor` (`jasper/cli/doctor/aec.py:1223-1328`, reading `/run/jasper/aec_bridge_stats.json`).
- **UDP packet loss**: deliberately not handled — documented as effectively zero-loss on loopback at ~256 kbps (`audio_io.py:277-282`, `aec_bridge.py:42-45`), consistent with the design's own stated tradeoffs (no seq/reorder buffer, `audio_io.py:340-341`).
- **XVF USB re-enumeration**: handled generically via `deploy/udev/99-jasper-aec-reconcile.rules` — any ALSA sound-card add/remove triggers `jasper-aec-reconcile.service`, which is documented as cheap/idempotent and only acts on JTS-owned mic configs.
- **`jasper-aec-init`/reconcile ordering**: `jasper-aec-bridge.service:25-39` uses `Wants=`+soft `After=` (not `Requires=`) for `jasper-aec-init`, with an explicit comment (`44-46`) that an `After=jasper-aec-reconcile` edge would deadlock both units since the reconciler blocking-restarts this unit from inside its own `ExecStart`. The bridge additionally gates on `ConditionPathExists=/run/jasper-aec-reconcile/aec-bridge-ready` (`47`) — a single reconciler-owned readiness marker (ADR-0224 per the comment), so the bridge fails closed rather than racing.
- **Attached-but-broken stays loud, not silently deaf**: ADR-0139 (`docs/adr/0139-...md:54-57`) — only an explicit reconciler-published `0` (mic definitively absent) drops a wake leg; a present-but-broken mic still raises and parks rather than quietly downgrading to push-to-talk.
- **`SAVE_CONFIGURATION`**: grepped across `jasper/`, `deploy/`, `rust/`, `c/` — zero hits. Non-negotiable #2 is respected throughout this subsystem.

---

### 7. Prose audit

Script (tokenize-based: comment tokens + leading docstring-statement lines counted as "prose," everything else as "code," blank lines separate) over the 33 files in scope:

```
TOTAL: 22147 lines, prose=6705 code=13190 ratio=0.51 (≈1 prose line per 2 code lines)
```

Top 10 by prose:code ratio:

| file | total | prose | code | ratio |
|---|--:|--:|--:|--:|
| `jasper/wake_conditions.py` | 86 | 68 | 4 | 17.00 |
| `jasper/openwakeword_guard.py` | 122 | 92 | 10 | 9.20 |
| `jasper/wake_fusion.py` | 74 | 54 | 10 | 5.40 |
| `jasper/wake_legs.py` | 140 | 83 | 29 | 2.86 |
| `jasper/aec_engines/dtln_models.py` | 135 | 73 | 38 | 1.92 |
| `jasper/wake_condition_context.py` | 85 | 46 | 24 | 1.92 |
| `jasper/wake_events.py` | 920 | 433 | 415 | 1.04 |
| `jasper/chip_aec/shipped.py` | 132 | 54 | 52 | 1.04 |
| `jasper/aec_engines/dtln.py` | 218 | 81 | 100 | 0.81 |
| `jasper/wake_models.py` | 351 | 130 | 175 | 0.74 |

**Caveat that matters**: a high ratio here is *not* a reliable badness signal in this codebase. The worst offenders are deliberately-thin "vocabulary"/contract modules (`wake_legs.py`, `wake_conditions.py`, `wake_condition_context.py`) whose own doctrine says to "keep this file as small as the contract" while carrying dense stability-contract rationale (frozen tokens, why-pointers to the historical corpus) — exactly what AGENTS.md's comment rule asks for. `openwakeword_guard.py` (ratio 9.2, read in full at `jasper/openwakeword_guard.py`) is a model example of *good* documentation: a measured 78 MB RSS justification (`14-28`), a precise "what breaks/doesn't break" contract (`34-43`), and a call-discipline rule enforced by `tests/test_lazy_imports.py` (`55-58`) — none of that should be cut.

**Actual rubric violations found** (dated/history narration the rubric prohibits) — small in number, worth a pass but not urgent:
- `jasper/audio_io.py:712` — "observed on JTS3 2026-06-11"
- `jasper/wake_events.py:199,215,392` — "(2026-05-23)"/"(2026-05-24)" phase-narration prefixes
- `jasper/mics/xvf3800.py:442` — "(2026-05-15)"
- `jasper/chip_aec/shipped.py:70` — "Measured jts.local 2026-09-02..." (borderline: justifies a non-derivable empirical constant, which the rubric allows, but the date-stamp itself is narration)

None of these are load-bearing; each could drop the date and keep the constraint.

---

### 8. What is genuinely good

- **`jasper/wake_legs.py`** is exactly the kind of "single source of truth" module AGENTS.md's duplication rule asks for, with a documented history of what it replaced and a frozen-token contract that protects historical telemetry.
- **`_MicStarvationWatchdog`** (`aec_bridge.py:243-306`) is a precise, non-hypothetical guard: it exists because the simpler continuous-empty check has a proven structural blind spot (a slow trickle resets it forever), and it's bounded/conservative by construction.
- **`openwakeword_guard.py`** — measured-cost justification + enforced call discipline via a dedicated test (`tests/test_lazy_imports.py`) is a strong pattern other subsystems could copy.
- **ADR-0170** is a clean example of "cost is derivable from the name" design discipline: switching profile resets the *whole* leg set rather than leaving orthogonal booleans to silently accumulate RAM cost.
- **`wake_events.py`'s hot-path/cold-path split** (fast WAL insert on the hot path, WAV writes and retention scans pushed to worker threads) is correct under real SD-card-latency constraints, not just "SQLite is fine because it's small."
- **The bridge module's own decomposition** (`capture`/`config`/`corpus_lanes`/`engines`/`reference`/`telemetry`, each independently tested) shows the team already applies the "extract and test" discipline AGENTS.md asks for — `_aec_loop` is what's left after that pass, not evidence it wasn't attempted.
- **Cross-checking test for the bash/Python port duplication** (`tests/test_aec_reconcile.py:2300-2438` asserting reconciler output against `wake_legs.by_token(...)`) turns an otherwise-fragile duplication into a guarded one.

---

### Findings table

| ID | Severity | file:line | Finding | Recommended change | Est. line delta |
|---|---|---|---|---|---|
| F1 | Medium | `jasper/wake_fusion.py:1-74`; `jasper/voice_daemon.py:1000,3394-3396,3423-3425,3445-3453` | `WakeFuser` is provably a no-op in production today (empty offsets always; `verify()` always `True`, making the `wake.suppressed` branch dead code); no config/corpus loader exists to populate offsets. | Delete `wake_fusion.py` and its wiring now; inline `detector.threshold` at the two call sites; drop the dead `verify()`/suppression branch. Reintroduce when Phase 1.3/1.4 has a real offset source (git history preserves the design). | −74 (module) −~75 (test) −~25 (voice_daemon.py wiring) ≈ **−170** |
| F2 | Low | `jasper/audio_profile_state.py:443,445,468` | Port `9876` hardcoded 3x in display strings instead of `wake_legs.by_token("on").udp_port`. Display-only, no functional risk, but can silently drift from the registry. | Derive from `wake_legs.by_token("on").udp_port` at call sites. | ~0 (refactor) |
| F3 | Low | `deploy/bin/jasper-aec-reconcile:2069-2073` | Bash reconciler re-literalizes the 5 UDP port defaults instead of reading them from `wake_legs.py` (unavoidable cross-language, but a second hardcoded copy). Already guarded by `tests/test_aec_reconcile.py:2300-2438`. | No urgent action; optionally add a one-line comment pointing at `wake_legs.py` as the canonical source for future editors. | 0 |
| F4 | Low | `jasper/audio_io.py:712`; `jasper/wake_events.py:199,215,392`; `jasper/mics/xvf3800.py:442`; `jasper/chip_aec/shipped.py:70` | 6 comments carry dated history narration ("observed on 2026-06-11", "(2026-05-23)"), which AGENTS.md's comment rule prohibits (no dates/history). | Drop the date stamps, keep the underlying constraint/rationale. | −1 line each (trivial) |
| F5 | Medium | `jasper/audio_io.py` (`UdpMicCapture`, `264-333`); `jasper/voice_daemon.py:2436-2437` | No application-level "no mic frames for N seconds" detection independent of the 30 s systemd watchdog; a persistently-dead bridge produces repeated silent process restarts with no cue and no proactive operator signal beyond `jasper-doctor`. | If this has been observed in practice, add a bounded (e.g. 10-15 s) no-frame log/`event=` line inside `WakeLoop.run()`'s frame loop; consider a cue only if it recurs — per AGENTS.md, don't add machinery against a hypothetical alone. | +~10-15 (log only) |
| F6 | None (verify only) | `jasper/cli/aec_bridge.py:351` (`# noqa: PLR0915`) | `_aec_loop` is ~708 lines; already flagged by the project's own lint config and already partially decomposed (6 sibling modules extracted). | Optional: extract `_log_periodic_rms` (`984-1038`) and `_resolve_usb_mic_frame` (`782-841`) as named helpers for readability; not urgent given existing test coverage (`tests/test_aec_bridge_stall.py`). | ~0 net (moves ~110-150 lines, doesn't remove them) |

Nothing found that touches the non-negotiables (`SAVE_CONFIGURATION` never called anywhere in scope; `devices.volume_limit`/`set_volume_db` clamps are untouched by this subsystem, as expected — the input path doesn't write output DSP config).