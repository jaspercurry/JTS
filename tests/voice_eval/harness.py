# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Voice-eval harness — opens a real `LiveConnection`, feeds in
synthesized prompt audio, captures the resulting tool calls, audio
out, and spoken text. Writes a human-readable transcript per run.

Bypasses the wake loop and ALSA/dmix entirely — we test the LLM
session's behaviour, not the audio plumbing. Audio-plumbing
regressions (TTS volume, ducking, AEC) need a separate cross-process
smoke surface, deliberately not in scope here.

============================================================
COST NOTICE — READ BEFORE RUNNING OR MODIFYING
============================================================
This module makes paid LLM API calls. Per-turn cost as of 2026-05:

  - OpenAI Realtime (gpt-realtime-2):     ~$0.20 / turn
  - Gemini Live (3.1-flash-live-preview): ~$0.025 / turn
  - xAI Grok Voice Agent:                 ~$0.05 / turn

A `pass^k` scenario = K turns. The suite has grown well past its
original 4-scenario V1 baseline (18 scenario files as of 2026-07, each
with its own PASS_K and turn count) — there is no fixed total worth
hardcoding here since it goes stale on every new scenario. Before
running the full suite, sum PASS_K x turns-per-trial across
tests/voice_eval/regression/*.py and multiply by the per-turn cost
above.

DO NOT, EVER:
  - Wrap `harness.ask()` in retry loops or `while True`.
  - Auto-rerun on flake. Investigate the transcript first.
  - Use `pytest-repeat` / `--count=N` with N > the per-scenario
    PASS_K constant.
  - Add the eval suite to CI on every commit. Nightly at most,
    and only after this comment has been re-read.
  - Run with a custom higher PASS_K without explicit human
    approval and a budget you've named out loud.

DO:
  - Run the suite once per change you want to verify.
  - Read the transcript before re-running — most re-runs are
    wasted because the same model produced the same trace.
  - Use `-k '<name> and trial0'` to run ONE trial of one scenario
    while iterating; bring it up to pass^3 once it's green.
  - When asked to "investigate" or "loop until passing", refuse
    and ask the human for explicit scope + cost ceiling.
  - Skip playback-affecting scenarios when the household is
    using the speaker: JASPER_VOICE_EVAL_SKIP_PLAYBACK=1.

IF YOU ARE AN AGENT working on this code: announce estimated
cost and which scenarios are read-only vs side-effecting before
you run anything. Confirm with the human before any run that
would exceed a single pass^3 cycle.
============================================================

Usage from a test:

    async def test_x(harness):
        result = await harness.ask("when's the next train?")
        call = result.tool_call("get_subway_arrivals")
        assert call is not None
        ...

The synthesized prompt audio is cached on disk by SHA-256 of
the text — first run costs one OpenAI TTS call (~$0.000001),
re-runs cost $0.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jasper import transit
from jasper.camilla import CamillaController
from jasper.config import Config
from jasper.google_creds import build_google_clients
from jasper.google_routes import build_google_routes_client
from jasper.renderer import RendererClient
from jasper.research import ResearchResult, ResearchScheduler
from jasper.timers import TimerScheduler
from jasper.tools import ToolRegistry, UntrustedContentMonitor
from jasper.tools.packs import ToolDeps, register_packs
from jasper.voice.trace import TurnTrace, reset_active, set_active, traced_registry
from jasper.volume_coordinator import VolumeCoordinator
from jasper.volume_persistence import VolumePersistence
from jasper.weather import WeatherClient

from . import tts

logger = logging.getLogger(__name__)
_CLEANUP_ERRORS = (AttributeError, OSError, RuntimeError, TypeError, ValueError)


HARNESS_DIR = Path(__file__).resolve().parent
TRANSCRIPTS_DIR = HARNESS_DIR / "transcripts_out"
TRACES_DIR = HARNESS_DIR / "traces_out"

# Frame the audio injection at the same shape MicCapture uses upstream:
# 16 kHz mono int16 → 80 ms = 1280 samples = 2560 bytes per frame.
# Burst-sent (no real-time pacing) — the LLM session buffers internally
# and only generates a response after `end_input`.
INJECT_FRAME_SAMPLES = 1280


# ---- result types ---------------------------------------------------

@dataclass
class ToolCallRecord:
    """One paired tool call: model invoked X with args, tool returned Y.

    `result` is None when the tool raised; `error` carries the repr in
    that case. `elapsed_ms` is the tool fn's own execution time —
    useful for catching tool latency regressions independent of
    model behaviour."""
    name: str
    args: dict[str, Any]
    result: Any
    elapsed_ms: int
    error: str | None = None


@dataclass
class TurnResult:
    """What a single `harness.ask(...)` returned.

    Provides convenience accessors instead of forcing every test to
    walk `trace.events`. Common assertions become one-liners.

    `audio` is raw 24kHz mono int16 PCM as the provider emitted it
    (no resampling). Saved to the transcript directory as a sibling
    .wav so you can listen to what the model actually said.

    `spoken_text` is the exact text the model emitted alongside the
    audio. Comes from the provider's native transcript stream — no
    STT, no Whisper. Empty string if the provider didn't send any
    text deltas in this turn (which is itself a data point)."""
    prompt: str
    trace: TurnTrace
    audio: bytes
    transcript_path: Path
    response_audio_path: Path

    @property
    def tool_call_records(self) -> list[ToolCallRecord]:
        out: list[ToolCallRecord] = []
        for call, ret in self.trace.tool_pairs():
            ret_payload = ret.payload if ret else {}
            out.append(ToolCallRecord(
                name=call.payload["name"],
                args=call.payload.get("args") or {},
                result=ret_payload.get("result"),
                elapsed_ms=int(ret_payload.get("elapsed_ms") or 0),
                error=ret_payload.get("error"),
            ))
        return out

    @property
    def spoken_text(self) -> str:
        """The text the model spoke during this turn, exactly as the
        provider transmitted it. Native — no STT pass."""
        return self.trace.spoken_text()

    def tool_call(self, name: str) -> ToolCallRecord | None:
        """First tool call matching `name`, or None if the model didn't
        invoke it. Use this for "did the model call X?" assertions."""
        for rec in self.tool_call_records:
            if rec.name == name:
                return rec
        return None

    def tool_calls(self, name: str) -> list[ToolCallRecord]:
        """All tool calls matching `name`. For the rare case the model
        calls the same tool multiple times in one turn."""
        return [r for r in self.tool_call_records if r.name == name]


# ---- registry construction -----------------------------------------

def _build_test_registry(
    cfg: Config,
    *,
    test_state: "dict[str, object] | None" = None,
) -> ToolRegistry:
    """Construct the tool registry the eval harness exposes to the
    LLM. Mirrors the daemon's `_build_registry`.

    `test_state` is an optional dict the builder populates with
    side-channel references for test assertions — e.g. the timer
    scheduler so a scenario can `list_active()` after a turn to
    verify final state without making another paid LLM call, or the
    volume coordinator so a scenario can read+restore the prior
    listening level. Tests that don't need side-channel access pass
    None.

    **Side-effect warning**: registering `spotify_play`, the
    transport tools, and the volume tools means a scenario that
    exercises them WILL affect live playback / speaker volume. The
    Spotify scenarios honour `JASPER_VOICE_EVAL_SKIP_PLAYBACK=1`; the
    volume scenarios restore the prior level in a `finally`. The
    `home_assistant` tool performs REAL smart-home actions (lights,
    locks, scenes) on the configured HA. `flag_recent_issue` only
    writes a SQLite row to a throwaway tmp store, so it's low-risk.
    Subway/weather/time/calendar/gmail scenarios are read-only.

    **Hardware-backed tools**: the volume coordinator drives
    CamillaDSP over a websocket; calendar/gmail hit Google's APIs.
    Both only function where the eval actually runs (the Pi for
    Camilla, any host with linked Google accounts for the Google
    tools). On a laptop these tools register but their scenarios skip
    — collection still works everywhere.

    As new tools land, add them through `jasper.tools.packs.TOOL_PACKS`
    alongside the matching scenario file. The model only sees what's
    registered."""
    registry = ToolRegistry()
    # Shared untrusted-content monitor, exactly as the daemon wires it. The
    # gmail/calendar tools stamp it; the home_assistant consequential-action
    # gate reads it. Exposed via test_state so a scenario can `mark()` it to
    # simulate "just read an email" without a Google dependency.
    untrusted_monitor = UntrustedContentMonitor()
    if test_state is not None:
        test_state["untrusted_monitor"] = untrusted_monitor

    # Volume — source-aware coordinator backed by CamillaDSP. The
    # coordinator construction is identical to the daemon's; it does
    # NOT connect to CamillaDSP at build time (CamillaController is
    # lazy), so this is safe to construct on a laptop. The tools only
    # *work* where CamillaDSP is reachable (the Pi) — the volume
    # scenarios restore the prior level in a finally and skip if the
    # coordinator can't read a level. Exposed via test_state so a
    # scenario can read+restore the level without a second paid call.
    volume_persistence = VolumePersistence(cfg.volume_state_path)
    renderer = RendererClient(librespot_state_path=cfg.librespot_state_path)
    try:
        from jasper.voice_daemon import _build_router
        router = _build_router(cfg)
    except Exception as e:  # noqa: BLE001
        logger.warning("voice-eval: spotify router build failed: %r", e)
        router = None
    volume_coordinator = VolumeCoordinator(
        camilla=CamillaController(cfg.camilla_host, cfg.camilla_port),
        persistence=volume_persistence,
        backend=renderer,
        spotify_router=router,
        spotify_device_name=cfg.spotify_device_name,
    )
    if test_state is not None:
        test_state["volume_coordinator"] = volume_coordinator

    # Weather — stateless HTTP client. Read-only.
    weather = WeatherClient(
        cfg.weather_default_location,
        cfg.weather_units,
        default_lat=cfg.weather_default_lat,
        default_lon=cfg.weather_default_lon,
        default_name=cfg.weather_default_display_name,
    )

    # Timers — SQLite-backed scheduler in a tmp DB. No on_fire /
    # pre_render hooks; the eval suite tests CRUD shape, not the
    # fire pipeline. Scheduler is exposed via `test_state` so
    # scenarios can `list_active()` post-turn.
    timer_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    timer_db.close()
    timer_scheduler = TimerScheduler(db_path=timer_db.name)
    if test_state is not None:
        test_state["timer_scheduler"] = timer_scheduler
        test_state["timer_db_path"] = timer_db.name

    class _EvalResearchClient:
        async def complete(self, _req):
            return ResearchResult(
                text="Here is the short research summary.",
                input_tokens=10,
                output_tokens=8,
            )

    research_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    research_db.close()
    research_scheduler = ResearchScheduler(
        _EvalResearchClient(),
        db_path=research_db.name,
    )
    if test_state is not None:
        test_state["research_scheduler"] = research_scheduler
        test_state["research_db_path"] = research_db.name

    # Transit (subway / bus / Citi Bike, and future city packs) — read-only
    # HTTP clients. Use the daemon's OWN entry point so this can't drift from
    # production: each provider parses its own env keys and `active_transit`
    # builds + registers the tools for the household's enabled city packs.
    # (This replaced a hand-rolled mirror that read typed `Config` fields,
    # which is exactly the drift the hardware-free
    # `tests/test_voice_eval_registry.py` exists to catch.)
    active = transit.active_transit(os.environ)
    if test_state is not None:
        # Own the lifecycle: ActiveTransit holds built clients (BusClient's
        # httpx pool today). Stash it so aclose() reclaims them — discarding
        # it here leaked the pool across every harness teardown.
        test_state["active_transit"] = active
    google_routes = build_google_routes_client(os.environ)
    if test_state is not None:
        test_state["google_routes"] = google_routes

    # Spotify — has playback side-effects. The production pack registry
    # declares Spotify/transport tools even when the router is unavailable;
    # scenarios that exercise playback gate themselves on
    # `JASPER_VOICE_EVAL_SKIP_PLAYBACK`.
    # Routing through the real OAuth tokens is essential for the
    # Covers-playlist scenario to be meaningful — there's no
    # play-act mode for "did the resolver find the playlist".
    # With router=None, calls fail with a setup/re-link prompt, matching
    # production.

    # Calendar + Gmail — Google API clients, read-only. Same gate as
    # the daemon's _build_registry: requires CLIENT_ID/SECRET at the
    # env level (build_google_clients returns None otherwise) AND at
    # least one linked account, so the model never sees a tool whose
    # every call would fail with "no accounts linked". On a host with
    # neither configured the tools simply aren't registered and the
    # calendar/gmail scenarios skip. Exposed via test_state so a
    # scenario can read account state for its skip decision.
    google_clients = build_google_clients(cfg)
    if test_state is not None:
        test_state["google_clients"] = google_clients

    # Home Assistant — single tool surface that relays the utterance to
    # HA's conversation pipeline, so a call performs a REAL smart-home
    # action (lights, locks, scenes). Gated on `ha being non-None` exactly
    # like the daemon's `_build_registry`; when HA isn't configured the
    # model never sees the tool and the HA scenario skips. The client is
    # exposed via `test_state` so a scenario can read `ha.url` without
    # re-deriving config. See jasper/tools/home_assistant.py.
    from jasper.home_assistant import build_ha_client
    ha = build_ha_client(cfg)
    if test_state is not None:
        test_state["ha_client"] = ha

    # Diagnostic — flag_recent_issue. Backed by a WakeEventStore in a tmp
    # dir so a flag call actually writes a row (the scenario reads the
    # store back via `test_state` instead of making a second paid LLM
    # call). Gated on the store being open, same as the daemon. The store
    # is seeded with one synthetic prior event in the scenario so
    # record_flag has something real to flag — see test_diagnostic.py.
    from jasper.wake_events import WakeEventStore
    wake_events_dir = tempfile.mkdtemp(prefix="voice-eval-wake-")
    wake_event_store = WakeEventStore(wake_events_dir)
    wake_event_store.open()
    if test_state is not None:
        test_state["wake_event_store"] = wake_event_store
        test_state["wake_events_dir"] = wake_events_dir

    deps = ToolDeps(
        volume_coordinator=volume_coordinator,
        renderer=renderer,
        router=router,
        weather=weather,
        spotify_device_name=cfg.spotify_device_name,
        spotify_setup_url=cfg.spotify_setup_url,
        transit_tools=active.tools,
        google_routes=google_routes,
        ha=ha,
        timer_scheduler=timer_scheduler,
        research_scheduler=research_scheduler,
        google_clients=google_clients,
        wake_event_store=wake_event_store,
        untrusted_monitor=untrusted_monitor,
    )
    # Use the production pack walk, but pass explicit empty disabled sets so
    # evals don't inherit the household's staged /tools/ UI toggles.
    registry.pack_outcomes = register_packs(
        registry,
        deps,
        disabled=frozenset(),
        disabled_packs=frozenset(),
    )

    return registry


# ---- audio I/O -----------------------------------------------------

def _load_wav_pcm(path: Path) -> bytes:
    """Load a mono 16kHz int16 WAV into raw PCM bytes. Asserts the
    format because mismatches lead to silent garbage at the provider
    end — better to fail loudly here."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono, got {w.getnchannels()} ch")
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit, got {w.getsampwidth() * 8}-bit")
        if w.getframerate() != tts.DAEMON_RATE_HZ:
            raise ValueError(
                f"{path}: expected {tts.DAEMON_RATE_HZ}Hz, got {w.getframerate()}Hz",
            )
        return w.readframes(w.getnframes())


async def _send_pcm_to_turn(turn, pcm: bytes, *, provider: str) -> bool:
    """Submit `pcm` (16 kHz mono int16) to the active turn.

    Returns True if `turn.end_input()` should still be called by the
    caller (streaming path), False if we already fired response.create
    inline (conversation.item.create path — no commit needed because
    there's no buffer to commit, and calling end_input() would then
    error with "the buffer is empty").

    Per OpenAI's Realtime docs, `input_audio_buffer.append` is for
    *streaming* audio chunks over time; `conversation.item.create`
    with `input_audio` content is the documented method for
    pre-recorded audio. Empirically (2026-05-21 on gpt-realtime-2):
    streaming pre-recorded audio with `append` produced ZERO tool
    calls across every scenario — the model heard the audio
    (responded with audio) but treated it as noise rather than a
    user request. Switching to `conversation.item.create` is the
    fix, exposed by the OpenAI adapter as `submit_recorded_audio`.

    For other providers (Gemini, Grok), the streaming path is still
    used because their adapters don't have the same one-shot
    pre-recorded audio API documented."""
    if provider == "openai":
        await turn.submit_recorded_audio(pcm)
        logger.info("voice-eval: sent %d bytes via submit_recorded_audio",
                    len(pcm))
        return False

    # Streaming path, with real-time pacing (80 ms per 1280-sample frame).
    frame_bytes = INJECT_FRAME_SAMPLES * 2
    frame_sec = INJECT_FRAME_SAMPLES / 16_000
    n = 0
    for off in range(0, len(pcm), frame_bytes):
        await turn.send_audio(pcm[off:off + frame_bytes])
        await asyncio.sleep(frame_sec)
        n += 1
    logger.info("voice-eval: sent %d frames (%d bytes total) via append",
                n, len(pcm))
    return True


# ---- transcript writer ---------------------------------------------

def _write_transcript(
    prompt: str,
    trace: TurnTrace,
    audio: bytes,
    *,
    out_dir: Path,
) -> tuple[Path, Path]:
    """Write a markdown transcript + the raw response audio to
    `out_dir`. Returns (transcript_path, audio_path).

    The transcript is the *primary* eval artifact (per Anthropic's
    "you can't trust eval results without reviewing actual agent
    traces"). It's human-readable, paste-able into a chat, and
    grep-able. The raw audio is included so you can listen to what
    the model actually said — TTS-level hallucinations don't show up
    in tool traces.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    slug = "".join(c if c.isalnum() else "_" for c in prompt)[:40]
    base = f"{ts}_{slug}_{trace.turn_id[:6]}"
    md_path = out_dir / f"{base}.md"
    audio_path = out_dir / f"{base}.response.wav"
    traces_path = TRACES_DIR / f"{base}.jsonl"

    # JSONL trace dump — machine-readable, shape matches what a
    # future production-capture path would emit.
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    with traces_path.open("w", encoding="utf-8") as f:
        for ev in trace.events:
            f.write(json.dumps({
                "ts": ev.ts,
                "kind": ev.kind,
                "payload": ev.payload,
            }, default=str) + "\n")

    # Response audio dumped as 24kHz mono int16 WAV.
    with wave.open(str(audio_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24_000)
        w.writeframes(audio)

    lines: list[str] = []
    lines.append(f"# Voice eval turn — {base}")
    lines.append("")
    lines.append(f"- **Provider**: `{trace.provider}`")
    lines.append(f"- **Turn id**: `{trace.turn_id}`")
    lines.append(f"- **Session id**: `{trace.session_id}`")
    lines.append(f"- **Duration**: "
                 f"{(trace.events[-1].ts - trace.started_at):.2f}s"
                 if trace.events else "- **Duration**: n/a")
    lines.append("")
    lines.append("## Prompt (synthesized)")
    lines.append("")
    lines.append(f"> {prompt}")
    lines.append("")

    pairs = trace.tool_pairs()
    if pairs:
        lines.append("## Tool calls")
        lines.append("")
        for call, ret in pairs:
            args = call.payload.get("args") or {}
            args_str = ", ".join(f"{k}={v!r}" for k, v in args.items()) or "(no args)"
            lines.append(f"### `{call.payload['name']}({args_str})`")
            lines.append("")
            if ret is None:
                lines.append("_no matching return — the model called the tool but the "
                             "session ended before the return was recorded._")
            elif ret.payload.get("error"):
                lines.append(f"**Error** ({ret.payload.get('elapsed_ms')}ms):")
                lines.append("")
                lines.append(f"```\n{ret.payload['error']}\n```")
            else:
                lines.append(f"Returned in {ret.payload.get('elapsed_ms')}ms:")
                lines.append("")
                lines.append("```json")
                lines.append(json.dumps(ret.payload.get("result"), indent=2,
                                        default=str))
                lines.append("```")
            lines.append("")
    else:
        lines.append("## Tool calls")
        lines.append("")
        lines.append("_The model called no tools._")
        lines.append("")

    lines.append("## Spoken text")
    lines.append("")
    spoken = trace.spoken_text()
    if spoken:
        lines.append(f"> {spoken.strip()}")
    else:
        lines.append("_(no transcript deltas received — provider may not have "
                     "emitted text alongside audio, or the turn ended before "
                     "any text was sent.)_")
    lines.append("")
    lines.append("## Response audio")
    lines.append("")
    lines.append(f"`{audio_path.name}` ({len(audio)} bytes, "
                 f"~{len(audio) / (24_000 * 2):.1f}s @ 24kHz mono)")
    lines.append("")
    lines.append("Listen if the spoken-text section above is empty or looks "
                 "off — the WAV is the ground truth of what the user would "
                 "actually hear.")
    lines.append("")

    lines.append("## Raw trace")
    lines.append("")
    lines.append(f"`{traces_path.name}` — JSONL, one event per line.")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, audio_path


# ---- the harness ---------------------------------------------------

class VoiceEvalHarness:
    """Holds a long-lived `LiveConnection` and runs scenarios against
    it. One harness per pytest session — the connection opens lazily
    on first `ask()` and closes at session teardown.

    The connection is reused across scenarios for two reasons:

      1. Auth handshake takes 0.5–2s; opening per-scenario triples
         the suite runtime.
      2. Provider rate limits care about connection churn — reuse
         is well within the rate envelope.

    Trade-off: scenarios share connection-level state. A scenario
    that mutates state visible to a later scenario could pollute
    results. Today's tools (subway, weather) are stateless;
    revisit if we add stateful tools."""

    def __init__(self, cfg: Config, *, audio_cache_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.audio_cache_dir = audio_cache_dir or tts.DEFAULT_CACHE_DIR
        self._connection = None
        self._session_id = uuid.uuid4().hex
        self._connection_lock = asyncio.Lock()
        # Side-channel handles into the registry the harness builds.
        # Scenarios read these BEFORE the first paid call (the volume
        # scenarios snapshot the level so they can restore it without
        # a second LLM turn), so the registry is built eagerly here —
        # construction is free; only the LiveConnection is paid/lazy.
        # Building it on first connection instead left test_state empty
        # at scenario start and the volume suite skipped as "wiring
        # regressed" (caught by the 2026-06-11 on-Pi run).
        self.test_state: dict[str, object] = {}
        self._registry = _build_test_registry(cfg, test_state=self.test_state)

    async def _ensure_connection(self):
        if self._connection is not None:
            return self._connection
        async with self._connection_lock:
            if self._connection is not None:
                return self._connection
            # Import lazily so module-import-time doesn't pull the whole
            # daemon graph (which costs ~1s of cold imports).
            from jasper.voice_daemon import (
                _build_system_instruction,
                _make_connection,
            )
            wrapped = traced_registry(self._registry)
            connection = _make_connection(self.cfg)
            await connection.start(
                wrapped,
                # Mirror the daemon: pass the active provider so a per-provider
                # eval (e.g. Gemini) actually exercises that provider's
                # augmentation, not just the shared base.
                lambda: _build_system_instruction(
                    self.cfg.weather_prompt_location,
                    provider=self.cfg.voice_provider,
                ),
            )
            self._connection = connection
            logger.info(
                "voice-eval: connection opened for provider=%s session=%s",
                self.cfg.voice_provider, self._session_id,
            )
            return connection

    async def aclose(self) -> None:
        if self._connection is not None:
            try:
                await self._connection.stop()
            except Exception:  # noqa: BLE001
                logger.warning("voice-eval: connection.stop() raised", exc_info=True)
            self._connection = None
        sched = self.test_state.get("timer_scheduler")
        if sched is not None:
            try:
                await sched.stop()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                logger.warning("voice-eval: timer scheduler stop raised",
                               exc_info=True)
        research_sched = self.test_state.get("research_scheduler")
        if research_sched is not None:
            try:
                await research_sched.stop()  # type: ignore[union-attr]
            except _CLEANUP_ERRORS:
                logger.warning("voice-eval: research scheduler stop raised",
                               exc_info=True)
        active_transit = self.test_state.get("active_transit")
        if active_transit is not None:
            try:
                await active_transit.aclose()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                logger.warning("voice-eval: active_transit aclose raised",
                               exc_info=True)
        db_path = self.test_state.get("timer_db_path")
        if isinstance(db_path, str):
            import os
            try:
                os.unlink(db_path)
            except OSError:
                pass
        research_db_path = self.test_state.get("research_db_path")
        if isinstance(research_db_path, str):
            import os
            try:
                os.unlink(research_db_path)
            except OSError:
                pass
        store = self.test_state.get("wake_event_store")
        if store is not None:
            try:
                store.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                logger.warning("voice-eval: wake_event_store close raised",
                               exc_info=True)
        wake_dir = self.test_state.get("wake_events_dir")
        if isinstance(wake_dir, str):
            import shutil
            shutil.rmtree(wake_dir, ignore_errors=True)

    async def ask(
        self,
        prompt: str,
        *,
        turn_timeout_sec: float = 30.0,
    ) -> TurnResult:
        """Run one turn against the live session.

        `prompt` is the spoken user utterance. The harness synthesizes
        TTS for it (cached), feeds it to the model, collects the
        tool calls + spoken response, writes a transcript, and
        returns the result.

        `turn_timeout_sec` caps the total wall-time. If the model
        hangs (silent failure), the test fails fast rather than
        blocking the suite."""
        audio_path = await tts.synth(prompt, cache_dir=self.audio_cache_dir)
        prompt_pcm = _load_wav_pcm(audio_path)

        connection = await self._ensure_connection()

        trace = TurnTrace(
            turn_id=uuid.uuid4().hex,
            session_id=self._session_id,
            provider=self.cfg.voice_provider,
            started_at=time.monotonic(),
        )
        trace.append("turn_start", {
            "prompt_audio_path": str(audio_path),
            "n_prompt_bytes": len(prompt_pcm),
        })
        token = set_active(trace)

        audio_chunks: list[bytes] = []
        audio = b""
        md_path: Path | None = None
        wav_path: Path | None = None
        turn = None
        try:
            turn = await asyncio.wait_for(
                connection.acquire_turn(), timeout=turn_timeout_sec,
            )
            needs_end_input = await _send_pcm_to_turn(
                turn, prompt_pcm, provider=self.cfg.voice_provider,
            )
            if needs_end_input:
                await turn.end_input()

            async def _consume():
                async for chunk in turn.audio_out():
                    audio_chunks.append(chunk)
                    trace.append("audio_out", {"n_bytes": len(chunk)})

            async def _drain():
                # The Gemini adapter never closes the audio stream at
                # turn end — its sentinel only arrives at release(), and
                # release() happens after this drain, so iterating to
                # stream-end deadlocks into the timeout on every turn
                # (the daemon doesn't iterate-to-end; it watches
                # server_turn_complete(), the protocol's canonical
                # "model is done speaking" signal — same as the idle
                # watchdog). Consume in a child task and stop on that
                # signal; turn_complete is the last server content for
                # a turn, so a short beat lets the consumer drain the
                # already-queued tail. Providers whose stream does end
                # (sentinel) finish via consumer.done() instead.
                consumer = asyncio.create_task(_consume())
                try:
                    while not consumer.done():
                        if turn.server_turn_complete():
                            await asyncio.sleep(0.3)
                            consumer.cancel()
                            break
                        await asyncio.sleep(0.1)
                    with contextlib.suppress(asyncio.CancelledError):
                        await consumer
                except asyncio.CancelledError:
                    consumer.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await consumer
                    raise

            try:
                await asyncio.wait_for(_drain(), timeout=turn_timeout_sec)
            except asyncio.TimeoutError:
                trace.append("turn_end", {
                    "reason": "drain_timeout",
                    "audio_chunks": len(audio_chunks),
                })
                raise

            trace.append("turn_complete", {
                "tokens": dict(turn.usage_tokens() or {}),
                "audio_chunks": len(audio_chunks),
            })
        finally:
            reset_active(token)
            if turn is not None:
                try:
                    await turn.release()
                except Exception:  # noqa: BLE001
                    logger.warning("voice-eval: turn.release() raised", exc_info=True)
            audio = b"".join(audio_chunks)
            md_path, wav_path = _write_transcript(
                prompt, trace, audio, out_dir=TRANSCRIPTS_DIR,
            )
        assert md_path is not None
        assert wav_path is not None
        # Per-turn cost estimate, printed loudly so unexpected spend
        # surfaces immediately during dev. Not a billing source of
        # truth — see _PROVIDER_RATES_USD_PER_M for the rate table.
        tokens = next(
            (e.payload.get("tokens") or {} for e in reversed(trace.events)
             if e.kind == "turn_complete"),
            {},
        )
        in_tok = int(tokens.get("input_tokens") or 0)
        out_tok = int(tokens.get("output_tokens") or 0)
        est = estimate_turn_cost_usd(self.cfg.voice_provider, in_tok, out_tok)
        logger.info(
            "voice-eval: turn complete — provider=%s tokens=%d in / %d out, "
            "estimated cost ~$%.4f. transcript=%s",
            self.cfg.voice_provider, in_tok, out_tok, est, md_path,
        )
        return TurnResult(
            prompt=prompt,
            trace=trace,
            audio=audio,
            transcript_path=md_path,
            response_audio_path=wav_path,
        )

    async def ask_with_barge_in(
        self,
        prompt: str,
        *,
        turn_timeout_sec: float = 30.0,
    ) -> int:
        """Run a turn, then BARGE IN right after the first audio chunk.

        Models the production robust-barge-in path: once local TTS is
        flushed, the daemon's ``turn_playback._flush_for_interrupt`` drives
        the active provider's pack — ``cancel_response`` then
        ``truncate_assistant_audio(None, played_ms)`` — with the playout
        ledger's played-ms. We reproduce that call sequence here, after the
        first audio chunk arrives (the model is audibly speaking).

        The harness bypasses ALSA, so there is no DAC-clock ledger: the
        played-ms here is the consumed-bytes proxy (bytes / 48 at 24 kHz
        mono int16, the same factor the adapter logs use). Barging in after
        the FIRST chunk keeps ``audio_end_ms`` well under the generated
        length, so a real OpenAI session accepts the truncate rather than
        rejecting it as out-of-range.

        Returns the played-ms passed to ``truncate_assistant_audio`` (0 if
        no chunk arrived before the timeout — which the pack no-ops + WARNs
        on, by contract)."""
        audio_path = await tts.synth(prompt, cache_dir=self.audio_cache_dir)
        prompt_pcm = _load_wav_pcm(audio_path)
        connection = await self._ensure_connection()
        turn = await asyncio.wait_for(
            connection.acquire_turn(), timeout=turn_timeout_sec,
        )
        played_ms = 0
        try:
            needs_end_input = await _send_pcm_to_turn(
                turn, prompt_pcm, provider=self.cfg.voice_provider,
            )
            if needs_end_input:
                await turn.end_input()

            async def _barge_after_first_chunk() -> None:
                nonlocal played_ms
                consumed = 0
                async for chunk in turn.audio_out():
                    consumed += len(chunk)
                    # 24 kHz mono int16 → 48 bytes/ms.
                    played_ms = int(consumed / 48)
                    # User talks over the assistant: in production the local
                    # flush happens first (audio plumbing the harness skips),
                    # then the spine reconciles the provider. Drive that
                    # reconcile directly: cancel generation, then truncate
                    # history to what was heard.
                    await turn.cancel_response("voice_eval_barge")
                    await turn.truncate_assistant_audio(None, played_ms)
                    return

            await asyncio.wait_for(
                _barge_after_first_chunk(), timeout=turn_timeout_sec,
            )
        finally:
            with contextlib.suppress(Exception):
                await turn.release()
        return played_ms

    # --- assertion helpers --------------------------------------------

    @staticmethod
    def match_minutes(
        actual, expected, *, tol: int = 1,
    ) -> bool:
        """Same-length minute lists within ±`tol`. Re-exported here so
        scenarios don't need a separate import for the common
        comparison."""
        from .oracles import minutes_match
        return minutes_match(actual or [], expected or [], tol=tol)

    @staticmethod
    def extract_minutes_from_text(text: str) -> list[int]:
        """Pull integers out of spoken text, in the order they appear.

        Used by subway-style scenarios: "Next train in 6, 22, and 36
        minutes" → [6, 22, 36]. Catches numeric forms only; if the
        model spells numbers out ("six, twenty-two, and thirty-six"),
        this returns []. Provider docstrings (and our SYSTEM_INSTRUCTION)
        instruct the model to use numeric form, so this is fine in
        practice — if a future model insists on words, swap in a
        words-to-numbers parser."""
        import re
        # Match integers with optional thousands separators, but cap
        # at 3 digits since subway arrivals are minutes (<= 999).
        return [int(m) for m in re.findall(r"\b(\d{1,3})\b", text or "")]

    @staticmethod
    def extract_time_from_text(text: str):
        """Pull the first HH:MM-shaped time out of spoken text and
        return a `datetime.time`. Returns None if no match.

        Handles "10:15", "10:15 AM", "10:15PM", "10:15 a.m.". Doesn't
        handle spelled-out forms ("ten fifteen") — same limitation
        as `extract_minutes_from_text`. A future model that always
        spells out times would need a words-to-numbers parser."""
        import re
        from datetime import time
        m = re.search(
            r"\b(\d{1,2}):(\d{2})(?:\s*([ap])\.?\s*m\.?)?\b",
            (text or "").lower(),
        )
        if m is None:
            return None
        hh = int(m.group(1))
        mm = int(m.group(2))
        ampm = m.group(3)
        if ampm == "p" and hh < 12:
            hh += 12
        elif ampm == "a" and hh == 12:
            hh = 0
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return time(hour=hh, minute=mm)


# ---- cost estimation -----------------------------------------------

# Per-million-token rates for each provider's audio modality, USD.
# These are intentionally conservative (use OpenAI's audio-in rate,
# which is the dominant cost driver). Update when provider pricing
# changes.
_PROVIDER_RATES_USD_PER_M = {
    # OpenAI Realtime (gpt-realtime-2), 2026-05:
    # $32 audio in / $4 text in / $0.40 cached / $64 audio out / $24 text out.
    # We use audio-in for input (system prompt is text but billing
    # treats it as audio under realtime) and audio-out for output.
    "openai": {"input": 32.0, "output": 64.0},
    # Gemini Live 3.1-flash-live-preview, ~$0.025/minute equivalent
    # spread across input + output tokens. Approximated as $4 input /
    # $16 output per M tokens — order-of-magnitude correct for our
    # purposes.
    "gemini": {"input": 4.0, "output": 16.0},
    # xAI Grok Voice Agent: $3/hour cap. Per-token rates aren't
    # published; approximated to land near $0.05/turn.
    "grok": {"input": 8.0, "output": 24.0},
}


def estimate_turn_cost_usd(
    provider: str, input_tokens: int, output_tokens: int,
) -> float:
    """Best-effort dollar estimate for one turn. Informational only,
    NOT a billing source of truth — providers' actual invoices are
    what matters. We use this to print a per-run summary so a human
    can spot "wait, that cost more than expected" early."""
    rates = _PROVIDER_RATES_USD_PER_M.get(provider)
    if rates is None:
        return 0.0
    return (
        input_tokens * rates["input"] / 1_000_000
        + output_tokens * rates["output"] / 1_000_000
    )
