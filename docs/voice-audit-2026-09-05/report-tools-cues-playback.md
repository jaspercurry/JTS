# Audit: Output Side & Tool Layer (jasper/tools, jasper/cues, jasper/voice/{earcons,turn_playback,output_gate,input_policy,input_presence}, assistant_loudness, conversation_history, timers)

No files were modified — this is a read-only audit via `sed`/`grep`/full reads plus ADR-0153/0154/0155/0157/0204 and the two daemon consumers.

## 1. Tool registry/executor (`jasper/tools/__init__.py`, `packs.py`, `catalog.py`)

**Shape is right and matches ADR-0157/0155.** `ToolDefinition` (schema+metadata, `jasper/tools/__init__.py:205-269`) is cleanly separated from `PythonExecutor`/`Tool` (runtime, `:308-411`). Schema is either hand-derived from a Python signature (`_params_schema`/`_annotation_to_schema`, `:711-763`) for `@tool`-decorated functions, or built explicitly (`weather.py:142-156`, `travel_routes.py:134-150`, `time.py:77-88`) when a tool needs a hand-tuned param schema or a separate short `llm_description`. Single dispatch seam: every provider (Gemini `voice/gemini_session.py:1555`, OpenAI/Grok `voice/openai_session.py:2060`) funnels through `dispatch_tool` (`jasper/tools/__init__.py:766-837`) — verified there are only these two call sites.

- **Timeouts are bounded and per-tool.** `DEFAULT_TOOL_TIMEOUT_SEC=12.0` (`:190`) applied via `asyncio.wait_for(tool.executor.execute(args), timeout=tool.timeout)` (`:818`). Home Assistant overrides to `DEFAULT_READ_TIMEOUT_SEC+5≈95s` (`home_assistant.py:74-78`) because HA's own LLM-backed agents legitimately take 30-60s — the override is derived from the client's real timeout, not a magic number.
- **Error fencing** is real: any exception or timeout inside `dispatch_tool` becomes `{"error": ...}` (`:828-835`), so a raising tool can never crash a session. (Separate concept: `fence_untrusted`, `:101-121`, wraps *third-party text*, not exceptions.)
- **Consequential-action confirmation matches ADR-0157 exactly.** `classify_consequential` (`home_assistant.py:132-140`) + `_ConfirmationStore` (`:150-179`) structurally split "ask" from "act": `home_assistant()` stashes and returns `needs_confirmation` (`:278-300`) when tainted; only `home_assistant_confirm()` (`:323-350`) executes. `consequential=True`/`untrusted_output=True` are used exactly where the ADR says (`home_assistant.py:208,321`; `gmail.py:248,358`; `calendar.py:152,213`; `travel_routes.py:145`) — verified by grep, no orphan flags.
- **Per-tool contract is convention, not framework-enforced — and the codebase says so out loud.** `build_tool`'s docstring (`jasper/tools/__init__.py:642-646`): "This does NOT validate or coerce the tool's return shape... a documented convention enforced by each tool's docstring, not by a base class here." `tests/test_tool_failure_contract_doc.py` pins the *prose* (system-instruction wording + docstring text), not runtime behavior — a new tool really can return `{}` on failure and nothing but review catches it. Given AGENTS.md's least-machinery bias this is a defensible trade-off, not a bug, but it's worth naming.
- **Latency / how results get back to the model:** tool calls within one LLM "round" are dispatched **sequentially**, not concurrently — `for fc in tool_call.function_calls: ... await dispatch_tool(...)` (`gemini_session.py:1552`, dispatch at `:1555-1557`) and `for fc in function_calls: await self._dispatch_function_call(fc)` (`openai_session.py:2003-2004`, dispatch_tool at `:2060`), followed by one batched `send_tool_response`/`response.create`. If a turn ever calls two tools (e.g. weather + a timer), their latencies add serially instead of overlapping — worse, HA's 95s budget would fully serialize against a second tool in the same round. No test exercises multi-tool rounds, so I can't say how often this fires in practice, but it's a structural latency risk, not a hypothetical.
- **Duplication:** within `jasper/tools/` itself, HTTP-client setup, secret loading, and caching are **not** duplicated — they're correctly pushed down into backend client modules (`weather.py`, `home_assistant.py`, `citibike.py`, `subway.py`, `google_creds.py`) that tools receive pre-built via `ToolDeps` (`packs.py:64-99`); no tool module touches `os.environ`/an API key directly (grep confirmed zero hits). The one real duplicate: `gmail.py:196-203` and `calendar.py:107-117` each define a near-identical `_api_error(account_name, exc)` that differs only in the log label and speakable sentence — candidate for one `google_errors.api_error(service_label, account_name, exc)`.
- **Dead / unused surface (verified by grep, not assumed):**
  - `jasper/tools/timer.py:18,262` imports+re-exports `announcement_text` in `__all__`, but every real caller (`voice_daemon.py:71`, `voice/daemon_main.py:49`) imports it from `jasper.timers` directly — the re-export has no consumer.
  - `jasper/tools/packs.py:152-155` `ToolPack = CapabilityPack` back-compat alias is used only inside `tests/test_tool_packs_registry.py` (~10 call sites) — trivial to rename away rather than keep permanently.

## 2. Cues vs. earcons (`jasper/cues/*` vs `jasper/voice/earcons.py`)

**Two systems, deliberately separated, and it's the right call — do not converge them.** `earcons.py`'s own docstring says it outright (`jasper/voice/earcons.py:5-11`): these are short synthesized interaction tones (wake/end-of-turn/mute/unmute), pure DSP recipes rendered once to PCM at daemon `__init__` (`voice_daemon.py:1112-1155`) — never touching disk or a TTS API. `jasper/cues/*` owns spoken TTS phrases for failure/proactive announcements (ADR-0153: pre-rendered, content-addressed, never streamed) plus dynamic-text (timer/research announcements). Different inputs (recipe constants vs. arbitrary text), different backends (numpy math vs. network TTS), different caching needs (none vs. content-addressed WAV cache) — merging them would just add a branch to a single God-module for no shared logic.

- **Cue playback critical path:** `_play_cue_owned` (`voice_daemon.py:2237-2262`) does `await ducker.duck()`, then `AudioCueManager.play()` (`cues/manager.py:255-328`) reads a **cached** WAV and writes it through the TTS/IPC pipe, then drains. No network call and no synthesis happen on this path — synthesis is baked ahead of time by `regenerate()`/`prerender_text()` (`cues/manager.py:184-251, 330-359`), so this is exactly ADR-0153's contract. One minor nit: `_read_wav_pcm` (`cues/manager.py:464-473`) is a synchronous file read inside `async def play()`/`_speak_text()`, not wrapped in `asyncio.to_thread` like every other blocking call in the same module — harmless in practice (small local WAV) but inconsistent with the module's own discipline.
- **Cache/prerender is solid:** content-addressed by `(template text, hostname, voice, model, format)` (`cues/generator.py:105-118`), with a documented stale-beats-silent fallback (`cues/manager.py:268-293`) and per-cue synthesis isolation on regen failure (`:194-200,229-251`). Timer fire announcements pre-render ahead of `fire_at` via `TimerScheduler._kick_pre_render` (`timers.py:262-280`).
- **Prose:** cues files run 2-9% `#`-comment density (see table below) — light and mostly why-pointers. One dated incident reference worth trimming: `cues/registry.py:84` ("the 2026-06-19 incident") and `cues/generator.py:186` ("released 2026-04-15") — AGENTS.md's comment rule explicitly excludes bare dates; point at an ADR/issue instead.

## 3. `turn_playback` / `output_gate` / `input_policy` / `input_presence` / `assistant_loudness`

All five are genuinely clean, single-concern modules — not leaked fragments of `voice_daemon.py`. Each takes state as parameters/injected config rather than reaching into daemon globals:

- `output_gate.py` (`AssistantOutputGate`, 190 lines) is a self-contained epoch-based ownership primitive over `asyncio.Lock`/`Event` — no dependency on `voice_daemon` at all.
- `turn_playback.py`'s `_play_responses`/`_idle_watchdog`/`_flush_for_interrupt` take `(turn, tts, barge_in_enabled=...)` as plain arguments; `voice_daemon.py` owns `_barge_in_active`/`_input_ended` (`:1175, :1217`) exclusively and passes them in at the call site (`barge_in_enabled=self._barge_in_active`, `:5051`). Grep confirms neither flag is duplicated anywhere else — one source of truth, cleanly threaded through.
- `input_policy.py` is explicitly documented as side-effect-free (`:12-14`) and is: no hardware probes, pure functions of a `Config`-like object.
- `input_presence.py` (95 lines) is a single-writer/multi-reader marker-file contract with an unusually dense but *substantive* docstring (AND-of-two-facts semantics, cold-boot fail-open behavior, cross-referenced to `Config.local_mic_present`) — not narration.
- `assistant_loudness.py`: **no lookahead, no first-audio latency.** Measurement is fully retrospective — `AssistantSourceMeter.observe_pcm_24k` just appends bytes (`assistant_loudness.py:251-260`); the actual biquad/LUFS math runs *after* the segment ends, off the critical path via `asyncio.create_task` (`audio_io.py:1795-1813`), with an explicit incident-driven comment explaining why: inline `finish()` "blocked the loop for ~0.7s per second of reply, delaying the end-of-turn chirp" (`audio_io.py:1806-1810`). Per-write cost is a cached profile lookup keyed by `(provider, model, voice)` (`audio_io.py:1771-1784`) — cheap.

I found no overlapping/duplicated state between these modules and `voice_daemon.py` beyond the deliberate parameter-passing above.

## 4. `conversation_history.py` / `timers.py`

Both are fail-soft SQLite stores with clear ownership. `conversation_history.py`'s retention is bounded by default even when env vars are absent (`DEFAULT_RETENTION_DAYS=30`, `DEFAULT_RETENTION_MAX_ROWS=500`, `:26-33`) — a good example of a guard with a stated reason (pre-existing Pis with no retention vars) rather than a hypothetical defense. `timers.py` is one `asyncio.Task` per active timer with SQLite persistence and drop-if-expired-during-downtime (`:202-210`); no cap on concurrent timer count, but that's a non-issue for a household appliance.

Minor: `ConversationStore.get()`/`.delete()` (`conversation_history.py:174-186, 213-225`) are unit-tested directly but have **no production caller** — `web/chat_setup.py` only exercises `.add`, `.recent`, `.stats`, `.clear`. Not clearly dead (looks like scaffolding for a not-yet-built "delete one turn" UI feature), but flagged since AGENTS.md asks to verify callers before calling something alive.

## 5. Prose audit (scripted: AST docstring spans vs. `#` comments vs. code)

Important caveat before the numbers: ADR-0155 **mandates** that per-tool docstrings carry the model-facing contract (when-to-call, response shape, voice-answer style) — that's not narration, it's the tool's actual interface, sent to the LLM. So the raw doc+comment ratio is expected to run high for small tool files (`subway.py` 70%, `citibike.py` 64%, `home_assistant.py` 62%) and is *not* a violation of the comment rule, which targets `#` engineer-notes. I scored both separately:

| file | total | code | `#` cmt | docstring | prose | prose ratio |
|---|---:|---:|---:|---:|---:|---:|
| jasper/voice/input_presence.py | 95 | 3 | 7 | 68 | 75 | 0.79 |
| jasper/tools/subway.py | 100 | 13 | 3 | 67 | 70 | 0.70 |
| jasper/tools/citibike.py | 174 | 37 | 7 | 104 | 111 | 0.64 |
| jasper/tools/home_assistant.py | 352 | 89 | 52 | 165 | 217 | 0.62 |
| jasper/tools/bus.py | 119 | 33 | 7 | 61 | 68 | 0.57 |
| jasper/tools/diagnostic.py | 165 | 52 | 9 | 81 | 90 | 0.55 |
| jasper/tools/timer.py | 262 | 86 | 3 | 129 | 132 | 0.50 |
| jasper/tools/__init__.py | 870 | 378 | 150 | 225 | 375 | 0.43 |
| jasper/voice/earcons.py | 392 | 182 | 43 | 113 | 156 | 0.40 |
| jasper/tools/spotify.py | 831 | 443 | 119 | 188 | 307 | 0.37 |

(full 33-file table generated; the other 23 files run 0.05-0.30 and are unremarkable.)

**`#`-comment-only density** (the metric AGENTS.md's "no narration" rule actually targets) tops out at `jasper/tools/__init__.py` (17%), `turn_playback.py` (15%), `home_assistant.py` (15%), `spotify.py` (14%) — and reading all of it, it's near-entirely load-bearing: empirically-tuned fuzzy-match thresholds (`spotify.py:20-56`), the prompt-injection threat model (`tools/__init__.py:55-136`), the barge-in cleanup contract (`turn_playback.py:140-148`). Genuine violations found were narrow:

- `spotify.py:717-718` — "verified 2026-05-22 against..." (bare date; substance is fine, date isn't).
- `cues/generator.py:186` — "released 2026-04-15" (same pattern).
- `cues/registry.py:84` — "the 2026-06-19 incident" (should point at an ADR if one exists for it, per the pattern already used two cues later at `:127`).
- `tools/packs.py:152` — "Compatibility name for the Phase-1 registry" (minor historical narration, tied to the dead `ToolPack` alias above).

No text addressed to a reviewer, no PR numbers, found anywhere in scope.

## 6. What's genuinely good

- `packs.py:436-498` — pack registration is transactional: a broken pack's partial `registry.tools` mutations are rolled back on exception (`originals` dict + reversed restore), so one bad contributor can't corrupt sibling packs' state. This is meaningfully better than "just try/except and move on."
- `fence_untrusted` + `UntrustedContentMonitor` + `classify_consequential`/`_ConfirmationStore` (ADR-0157) is a coherent, minimal, well-tested two-layer defense — exactly the "least machinery that works" the project asks for, not gold-plated privilege separation it explicitly says is future work.
- `AssistantOutputGate`'s epoch counter (`output_gate.py:180-190`) elegantly solves the "a waiter woken by episode A's end must not act as if episode B (which started before it got scheduled) is also over" race without polling.
- The assistant-loudness critical-path fix (`audio_io.py:1795-1813`) is a textbook "fix forward" per AGENTS.md's guard doctrine: a real incident (chirp delay), a `to_thread`/background-task fix, and a comment naming the measured cost.
- Cue caching's stale-beats-silent fallback (`cues/manager.py:268-293`) is a thoughtful, tested failure mode for a subsystem whose entire job is "be audible when everything else broke."

## Findings table

| ID | severity | file:line | finding | recommended change | est. Δlines |
|---|---|---|---|---|---|
| F1 | med | `voice/gemini_session.py:1552-1557`, `voice/openai_session.py:2003-2004,2060` | Tool calls within one LLM round dispatch sequentially (`await` in a for-loop), so N tools in a round add latency serially — worst case HA's 95s budget fully blocks a second tool in the same round | `asyncio.gather` the round's `dispatch_tool` calls (keep per-call `_note_activity()`/logging ordered) | +10/-5 per adapter |
| F2 | low | `tools/gmail.py:196-203`, `tools/calendar.py:107-117` | Near-identical `_api_error(account_name, exc)` helper duplicated, differs only in log label + sentence | Extract `google_errors.api_error(service_label, account_name, exc)` | -8 net |
| F3 | low | `tools/timer.py:18,262` | `announcement_text` re-exported in `__all__`; no caller imports it via `jasper.tools.timer` (all use `jasper.timers` directly, verified) | Drop the re-export | -2 |
| F4 | low | `tools/packs.py:152-155` | `ToolPack = CapabilityPack` back-compat alias used only by one test file (~10 sites, trivially renamable) | Rename in `test_tool_packs_registry.py`, delete alias | -4 |
| F5 | low | `voice/input_policy.py:49,105,117,129,140` | `SpeechInputContract.gain_controlled` is set at every construction site but never read by any consumer or test | Wire into `_resolve_openai_noise_reduction` if intended, else delete the field | -5 |
| F6 | low | `cues/__init__.py:10-19` | Package re-exports (`GeminiTTSGenerator`, `GrokTTSGenerator`, `OpenAITTSGenerator`, `TTSBackend`, `cue_hash`, `render_template`, `write_cue`, `CueDef`) that no call site imports via `jasper.cues` root (all go through `.generator`/`.registry`) | Trim `__init__.py` to `CUES`, `CueDef`, `AudioCueManager`, `build_cue_tts_backend` | -10 |
| F7 | low | `cues/manager.py:464-473` (called at `:296,416`) | Synchronous WAV read inside `async def play()`/`_speak_text()`, unlike every other blocking call in the module (not `to_thread`-wrapped) | Wrap in `asyncio.to_thread` for consistency (files are small; low real-world impact) | +2 |
| F8 | low | `tools/__init__.py:642-646` | `{error: ...}` upstream-failure contract is convention only (documented, not enforced); a new tool can silently return `{}` | Observation only — matches project's least-machinery stance; no change recommended | 0 |
| F9 | low | `tools/audio.py:38-39` (+ `volume_persistence.py:79-80`, `control/volume_ops.py:35-36`) | `VOLUME_MIN_DB`/`VOLUME_MAX_DB` back-compat aliases independently redefined in 3 files, each pointing at the same underlying constants | Converge into one re-export from `jasper.volume_curve` (touches 2 files outside this audit's scope) | -4 |
| F10 | low | `cues/registry.py:84`, `cues/generator.py:186`, `tools/spotify.py:717-718` | Bare dates in comments ("the 2026-06-19 incident", "released 2026-04-15", "verified 2026-05-22") — AGENTS.md excludes dates from comments | Point at an ADR/issue instead, or drop the date | 0 |

**Relevant files** (all read in full): `/home/user/JTS/jasper/tools/__init__.py`, `packs.py`, `catalog.py`, `spotify.py`, `gmail.py`, `calendar.py`, `transport.py`, `home_assistant.py`, `timer.py`, `audio.py`, `research.py`, `citibike.py`, `diagnostic.py`, `weather.py`, `travel_routes.py`, `bus.py`, `subway.py`, `time.py`, `google_errors.py`; `/home/user/JTS/jasper/cues/__init__.py`, `registry.py`, `manager.py`, `factory.py`, `generator.py`, `cli.py`; `/home/user/JTS/jasper/voice/earcons.py`, `turn_playback.py`, `output_gate.py`, `input_policy.py`, `input_presence.py`, `gemini_session.py` (excerpts), `openai_session.py` (excerpts); `/home/user/JTS/jasper/assistant_loudness.py`, `conversation_history.py`, `timers.py`, `audio_io.py` (excerpts), `voice_daemon.py` (excerpts); ADRs 0153, 0154, 0155, 0157, 0204.