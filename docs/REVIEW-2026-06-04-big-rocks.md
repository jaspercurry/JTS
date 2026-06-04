# JTS Review — Big Rocks (Architecture & Re-architecture)

> Companion to [REVIEW-2026-06-04-deep-dive.md](REVIEW-2026-06-04-deep-dive.md).
> These are the larger structural items — file decompositions, shared-base
> extractions, and extensibility seams. None are correctness emergencies;
> they are the difference between "impressive" and "delightful to extend."
> Each has concrete file:line evidence from the multi-agent review.

The unifying theme: **a handful of god-files are the parts a new
contributor will fear.** The project's own stated bar is "add a transit
provider / new hardware without touching the core," and these files are
the counter-examples. Sequencing and effort are in the
[deep-dive roadmap](REVIEW-2026-06-04-deep-dive.md#recommended-sequencing).

---

## 1. `doctor.py` — 5,632-line monolith, hand-ordered check list (Medium · Large)

**Where:** `jasper/cli/doctor.py:1-5632`, the 80-entry list at `:4699-4846`.

The single largest file in the repo. 73 `check_`/`probe_` functions + ~30
helpers in one module; `run_async()` holds a hand-ordered 80-entry list
mixing bare callables and `(label, lambda)` tuples. Adding a check means
editing the middle of that list *and* defining the function 1,000+ lines
away. No per-domain grouping, no registry, no way for a subsystem to
register its own checks.

**Direction:** Split into per-domain modules (`doctor/audio.py`,
`doctor/network.py`, `doctor/renderers.py`, `doctor/memory.py`, …) behind a
`@doctor_check(label, group=)` decorator that appends to a registry; each
domain module imported for side effects. `run_async()` iterates the
registry. The `CheckResult` dataclass + `_run_doctor_check` crash-isolation
harness already exist — this is re-homing, not redesign. **This is the
highest-leverage decomposition: it's the file most likely to scare a
contributor, and the registry pattern directly serves the "register your
own checks" extensibility goal.**

Related (Medium · Performance): doctor runs ~31 subprocesses *sequentially*
(`:4841`) inside a blocking jasper-control HTTP worker with a 30 s timeout
(`server.py:1869`); summed nominal timeouts ≈ 83 s, so under memory
pressure `/system/diagnostics` can approach the ceiling. Parallelize the
subprocess-bound checks with `asyncio.to_thread` (the run is already async)
and batch sibling `systemctl` queries.

---

## 2. `voice_daemon.py` — 3,923 lines, uncut seams (Medium · Large)

**Where:** `WakeLoop.__init__` `:1291-1539` (~250 lines, ~40 attrs);
`run()` `:3410-3898` (~480-line dependency-wiring fn); `SYSTEM_INSTRUCTION`
`:167-330` (163-line literal); `_handle_wake_frame` `:2037-2335` (~300).

The file is sophisticated and well-instrumented (the multi-leg OR-gate, the
acquire-buffer-across-context-reset pattern, the telemetry funnel are all
genuinely good — preserve them). But the class docstring itself enumerates
five distinct responsibilities, and the seams are already visible in the
code.

**Direction (each independently testable, no behavior change):**
- Move `SYSTEM_INSTRUCTION` + `_build_system_instruction` →
  `jasper/voice/system_instruction.py`.
- Extract the end-of-utterance VAD state machine (the
  `_user_speech_seen`/`_silence_started_at`/`_speech_run_*` cluster + the
  logic in `_handle_session_frame`) into an `EndpointDetector` helper.
- Factor `run()`'s collaborator construction into a `build_dependencies()` /
  `VoiceDaemonContext` dataclass so `__init__` takes a config object instead
  of wiring 20+ collaborators inline.

> Pairs with the **`_end_turn` re-entrancy bug** (see small-wins) — fixing
> that race naturally introduces the state-machine discipline that makes
> the extraction safer.

---

## 3. Voice adapters — ~36 duplicated methods across two ~2,000-line files (Medium · Large)

**Where:** `openai_session.py` (2,050) and `gemini_session.py` (1,979);
`_set_state` (`gemini:548`/`openai:757`), the `ConnectionState` enum
(`gemini:91`/`openai:130`), `_supervisor_loop`, `_reconnect_with_backoff`,
`_maybe_fire_escalation_cue`, `_on_turn_released`, `is_paused`, the
`_noisy_transitions` frozenset, the fingerprint-ring init, and the entire
turn-side `audio_out`/`release`/`last_*` cluster — all near-identical copies.

The seam itself is good: a clean `LiveConnection`/`LiveTurn` Protocol, a
single switch point, and Grok proves it works at **112 lines** by
subclassing OpenAI. The problem is that a *fourth genuinely-distinct*
provider rewrites ~2,000 lines that already exist twice.

**Direction:** Extract a `_BaseLiveConnection` (mixin/ABC) holding the state
machine, the supervisor-loop skeleton, escalation plumbing, and the
connected-event/turn-lock lifecycle. Providers implement only
`_open_session` / `_teardown_session` / `_build_config` and the
wire-format receive loop. The existing HANDOFF correctly argues against
sharing the loop *body* — this lifts only the scaffolding *around* it. Add
a `runtime_checkable` Protocol-conformance test.

Two adapter bugs ride alongside (fix during the extraction):
- Gemini GoAway with `time_left` reconnects immediately, tearing down the
  in-flight turn; OpenAI added a proactive pre-cap watchdog for exactly
  this — Gemini ignores the advance signal (`gemini_session.py:1343`).
- Two divergent initial-connect retry strategies (OpenAI's 10-min
  time-budget that survived the 2026-05-23 boot race vs Gemini's weaker
  fixed 15 s schedule); promote the time-budget helper to shared.

---

## 4. `wake_corpus_setup.py` — 3,862-line audio subsystem mis-filed under `web/` (Medium · Large)

**Where:** `jasper/web/wake_corpus_setup.py`. `RecordingBackend` (the
asyncio multi-leg UDP capture engine, `:1737-2756`, ~1,000 lines), bridge/
leg orchestration (`:313-1550`, ~1,200), ALSA mixer probing, AEC3 sweep
config — with the actual HTTP layer being a single `_Handler` whose
`do_POST` is a 354-line inline `if/elif` dispatcher (`:3050-3404`). For
comparison, the next-largest `web/` file is 1,672 lines.

**Direction:** Extract a `jasper/wake_corpus/` (or `jasper/enrollment/`)
package — `bridge_session.py` (bridge env + outputs + restart),
`recording_backend.py` (the backend + task + clip metadata) — leaving
`wake_corpus_setup.py` as a thin HTTP adapter with a `{path: handler}`
route table. This is an operator tool, so it's lower-risk to refactor than
the live wake path.

Related (Medium · Resilience): entering corpus test-mode runs
`systemctl stop jasper-voice` with no failsafe (`:2807`). If the operator
opens the recorder and closes the tab, the speaker stays deaf — the
socket-activated corpus server won't even restart to recover unless a new
request arrives. Add a bounded auto-recovery (a `RuntimeMaxSec`/marker
check that re-runs `exit_corpus_test_mode` + restarts voice if the session
marker is stale).

---

## 5. `_aec_loop` — ~960 lines, production hot path tangled with 4 corpus leg families (Medium · Large)

**Where:** `jasper/cli/aec_bridge.py:1237-2199`. The production path (drain
mic, drain ref, emit `off` leg, run engine, emit `on` leg) is interleaved
with the DTLN leg, chip-AEC legs, `xvf_raw0` corpus legs, `usb_raw`/
`usb_webrtc`/`usb_dtln` corpus legs, and AEC3 sweep variants — ~14 distinct
sockets/batches/engines set up in the first ~180 lines, with the
emit-batch-`sendto` pattern copy-pasted per leg.

The ref-starvation fix is intact and the queues are correctly bounded —
this is purely about finding the production path inside experiment
scaffolding.

**Direction:** Route every leg through the existing `emit_packet` helper
(the DTLN/USB/ref/sweep blocks predate or ignore it), and lift the corpus/
experiment leg setup + per-frame emission into a small list of leg-handler
objects iterated in the loop. `_aec_loop` then reads as production
mic→ref→engine→emit with the experiment legs as pluggable handlers.

---

## 6. `control/server.py` `do_POST` — 654-line route dispatcher (Medium · Large)

**Where:** `jasper/control/server.py:1903` (~654 lines, ~16 inlined
`if self.path == "/...":` branches). Whole control server is 2,734 lines.

Adding a control endpoint means editing the middle of this method — the
exact friction the "extend without touching core" goal warns against.

**Direction:** Extract a `{path: handler}` route table (or small per-domain
handler classes); the dispatcher becomes a lookup. The same shape is
reusable by the web wizards (their per-wizard `_make_handler`/`make_server`
boilerplate is a milder version of the same pattern). Decompose
incrementally under the existing `test_control_server.py`.

Two area-level issues sit with this server (Medium):
- Unbounded `ThreadingHTTPServer` + per-request `asyncio.run()` +
  subprocess fan-out on a 1 GB Pi (`:2566`, `:1781`): an aggressive poller
  or several open dashboards can spawn unbounded threads + concurrent
  forks. Bound concurrency (request-queue size + worker cap/semaphore) and
  add single-flight/short-TTL caching for `/state` and `/system/diagnostics`.
- Destructive endpoints (`/system/poweroff|reboot|restart`) have no auth —
  `_guard_mutating_request` only constrains *browser* shapes (it returns OK
  when `Origin` is absent), so any non-browser LAN client passes (`:2489`,
  `http_security.py:190`). Defensible "trusted LAN" tradeoff, but document
  it prominently and consider a shared-secret token for the genuinely
  destructive subset.

---

## 7. Extensibility seams that are leakier than advertised

**Transit "3 places" is really ~9–12 (Medium · Extensibility).** The
discovery layer (`transit/base.py` Protocol, data-driven `REGISTRY`,
`covering()`/`all_env_keys()`) is genuinely clean — a keyless CSV provider,
a credentialed REST provider, and a keyless GBFS provider coexist with zero
special-casing. But adding a Berlin/SF city actually touches: the provider
module, the `REGISTRY` line, a `transit_setup.py` `_index_html` elif
(`:1059`), a bespoke `_xxx_card_html` renderer, a `make_xxx_tools` factory,
a runtime client class, **three separate edits in core `voice_daemon.py`**
(client construction `:3457`, tool import+registration, the
`transit_configured` boolean), and a duplicated bash array in `install.sh`
with no drift-guard test. The transit memory and some prose claim "3
places."

- *Cheapest:* make the docs honest — update the `__init__.py` checklist to
  call out the three `voice_daemon.py` edits, and fix the "3 places" prose.
- *Better:* a small per-provider runtime descriptor (client factory + tool
  factory + an `enabled` predicate) so `voice_daemon.py` iterates providers
  instead of hardcoding each. Also: derive the wizard's hardcoded
  "NYC subway and bus" copy from the covering providers' labels; and
  document/structure the `Stop.lines` field that citibike overloads as a
  display string while subway/bus use it as a route-id list.

**Single-source the constants the bash reconcilers re-hardcode (Medium ·
Architecture).** `jasper/wake_legs.py` + `jasper/wake_ports.py` are the
SSOT for leg→UDP ports (9876/9877/9878/9887/9888); the bash reconciler
re-hardcodes them as env fallbacks (`jasper-aec-reconcile:503-512`), and
`xvf3800.py`'s mixer channel string is independently rebuilt in bash
(`:362-366`). Have `install.sh` render these into a small conf file from the
Python registry (mirroring how `voice_provider_ids` is already generated)
and have the reconcilers source it; at minimum add a pytest that greps the
reconciler against the Python constants so drift fails CI.

---

## 8. Roadmap-only (deliberate scope calls, not defects)

- **No `/metrics` / time-series export (Low · Extensibility).**
  `/state`, `/healthz`, `jasper-doctor --json` are all point-in-time. Many
  adopters will run JTS next to Home Assistant; a tiny read-only `/metrics`
  on jasper-control that re-expresses *existing* `/state` fields as
  Prometheus gauges (no new collection, no new RAM) is the cohort-standard
  low-cost answer. The HANDOFF deliberately excludes soak-history from the
  production plane, so this is a roadmap call.
- **No ESP32 OTA on either device (Medium · Resilience).** USB re-flash is
  the only update path; the dial is designed to run off USB-power
  standalone, so every firmware fix means physically retrieving the
  accessory. The device already does mDNS + HTTP to the Pi, so
  `esp_https_ota` pulling a Pi-hosted `.bin` is the natural fit. At minimum,
  document the absence + recall cost in `docs/satellites.md`.
- **Correction bundle / config retention (Medium · Resilience).** Bundles
  (multi-MB WAVs per position) and `correction_*.yml` configs have no prune
  logic — deletion is manual-only. On a months-unattended Pi with finite SD
  storage, re-measurements grow unbounded. Add a ring-buffer cap
  oldest-first (mirror `JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES`) + a doctor
  disk check.
