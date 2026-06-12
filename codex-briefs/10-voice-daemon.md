# Brief 10 — voice_daemon.py: defects first, then the seam extractions

Mission: review §5.5 items 9-10 + §5.1 — the repo's largest file (4,133
lines). Two PRs, strictly in this order. Line numbers below are from the
2026-06-12 review at `6772b81a`; ~20 PRs have merged since — **re-locate
every site by symbol, not line**. This file is hot; rebase before every push.

Branch: `codex/voice-daemon`. File fence: `jasper/voice_daemon.py`,
`jasper/voice/` (new modules + session.py Protocol additions only),
`tests/test_voice_daemon*.py` + new test files. Nothing else.

## PR 1 — confirmed defects (small, each with a test)

1. **Fire-and-forget tasks can be garbage-collected.** The
   wake-arbitrate-acquire-drain task (created near `_arbitrate_acquire_drain`)
   and ~5 sibling `asyncio.create_task` sites are never stored — asyncio holds
   only weak refs, so a collected task strands `_acquiring=True` and deafens
   the speaker until the watchdog restarts it. Track them in a strong-ref
   `self._fire_and_forget: set[asyncio.Task]` with `add` +
   `discard`-on-done callback (NOT `_bg_tasks` — that set has
   the documented must-outlive-the-turn semantics; see the memory/comment),
   and cancel+await them in `run()`'s finally. Test: create the task, force a
   gc, assert it still runs.
2. **No output-side cap when a provider wedges mid-response.**
   `_idle_watchdog` has no timeout branch once any chunk has arrived — a
   provider stalling with the socket open leaves SESSION stuck until mute.
   Add a generous last-resort cap (e.g. 120 s with no new chunk AND no
   turn_complete) that ends the turn cleanly (normal teardown path, telemetry
   outcome recorded, no cue spam). Env-tunable with a documented default in
   `.env.example` + config (drift test updates).
3. **Stale control-socket docstring**: MUTE is documented as "Runtime only
   (no persistence)" but mute persists via `write_mic_muted` (PR #119
   promise). Fix the docstring.
4. **`begin_event` records the primary leg's threshold even when another leg
   fired.** Record the firing leg's effective threshold (and leg token) so
   corpus analysis doesn't misstate the firing bar. Extend the wake-event
   test that pins row contents.
5. **Declare the server-VAD shadow interface.** voice_daemon reaches
   `getattr(turn, '_create_response_only')` / `_mark_server_vad` and probes
   methods that exist only on the OpenAI adapter. Add them to the
   LiveConnection/LiveTurn Protocols in `jasper/voice/session.py` as optional
   (documented) members with public names; update the OpenAI adapter and the
   call sites. Provider #4 implementers must be able to read the full
   contract from the Protocol.

## PR 2 — the split (behavior-neutral; no logic edits ride along)

Extract, preserving names and import paths via `jasper/voice/` modules:
- `voice/prompt.py` — SYSTEM_INSTRUCTION + `_build_system_instruction` (the
  ~500-line prompt block). Keep the rationale comment block with it.
- `voice/earcons.py` — the earcon DSP helpers (~100 lines).
- `voice/turn_playback.py` — `_play_responses` + playback helper cluster.
- `voice/daemon_main.py` — `run()` composition root, control socket, builder
  functions. `voice_daemon.py` keeps WakeLoop + FanInDucker and re-exports
  what tests/docs reference (`WAKE_REFRACTORY_SEC` etc. stay importable from
  the old path).
- **WakeLoop test-constructor:** add a `WakeLoop.for_tests(**overrides)`
  builder with defaulted collaborators, migrate the test files that build
  half-initialized instances via `WakeLoop.__new__`, then delete the
  `getattr(self, ...)` self-probes that existed only to tolerate them.

Acceptance: `pytest tests/test_voice_daemon*.py tests/test_wake*.py
tests/test_cue_registry_coverage.py -q` green; `ruff check .`; PR 2's diff is
move-only (reviewer will diff function bodies); docs-impact note for
HANDOFF-voice-providers / HANDOFF-prompting (the prompt moved files — update
the "before editing SYSTEM_INSTRUCTION" pointers in AGENTS.md + those docs).
