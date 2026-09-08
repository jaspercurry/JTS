# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Paid voice scenarios using production input, capture, usage and playback.

Announce a fixed scenario count and estimated cost before running. Never
loop or auto-retry paid sessions. See README.md for setup and evidence limits.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
import wave
from dataclasses import asdict, dataclass
from datetime import datetime, time as datetime_time
from pathlib import Path
from typing import Literal

from jasper import transit
from jasper.audio_io import MicCapture
from jasper.camilla import CamillaController
from jasper.config import Config
from jasper.google_creds import build_google_clients
from jasper.google_routes import build_google_routes_client
from jasper.home_assistant import build_ha_client
from jasper.renderer import RendererClient
from jasper.research import ResearchResult, ResearchScheduler
from jasper.timers import TimerScheduler
from jasper.tools import ToolRegistry, UntrustedContentMonitor
from jasper.tools.packs import ToolDeps, register_packs
from jasper.usage import UsageStore, load_pricing_overrides, pricing_for_model
from jasper.voice.daemon_main import (
    _build_router,
    _build_system_instruction,
    _make_connection,
    _wire_billable_activity_meter,
)
from jasper.voice.session import LiveTurn, TurnCapture, TurnUsage
from jasper.voice.turn_playback import play_responses
from jasper.volume_coordinator import VolumeCoordinator
from jasper.volume_persistence import VolumePersistence
from jasper.wake_events import WakeEventStore
from jasper.weather import WeatherClient
from tests.voice_replay import RecordingPlayout

from . import tts
from .oracles import minutes_match
from .trace_registry import ToolCallRecord, traced_registry
from .turn_trace import TurnTrace, reset_active, set_active

logger = logging.getLogger(__name__)
_CLEANUP_ERRORS = (AttributeError, OSError, RuntimeError, TypeError, ValueError)


HARNESS_DIR = Path(__file__).resolve().parent
TRANSCRIPTS_DIR = HARNESS_DIR / "transcripts_out"
TRACES_DIR = HARNESS_DIR / "traces_out"

@dataclass
class TurnResult:
    """Provider evidence and simulated output; no acoustic playback claim."""

    prompt: str
    trace: TurnTrace
    audio: bytes
    transcript_path: Path
    response_audio_path: Path
    capture: TurnCapture | None
    usage: TurnUsage
    estimated_cost_usd: float | None
    cost_status: Literal["estimated", "incomplete", "unpriced"]
    tool_call_records: list[ToolCallRecord]

    @property
    def spoken_text(self) -> str:
        return (self.capture.assistant_text or "") if self.capture else ""

    def require_spoken_text(self) -> str:
        if not self.spoken_text:
            import pytest  # lazy: only scenario assertions need pytest
            pytest.xfail(f"Native transcript unavailable; inspect {self.response_audio_path}")
        return self.spoken_text

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
    ha = build_ha_client(cfg)
    if test_state is not None:
        test_state["ha_client"] = ha

    # Diagnostic — flag_recent_issue. Backed by a WakeEventStore in a tmp
    # dir so a flag call actually writes a row (the scenario reads the
    # store back via `test_state` instead of making a second paid LLM
    # call). Gated on the store being open, same as the daemon. The store
    # is seeded with one synthetic prior event in the scenario so
    # record_flag has something real to flag — see test_diagnostic.py.
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
        google_setup_url=cfg.google_setup_url,
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
    # evals don't inherit the household's staged /assistant/tools/ UI toggles.
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


async def _send_pcm_to_turn(turn: LiveTurn, pcm: bytes) -> None:
    """Pace 16 kHz mono int16 at the production delivery frame size."""
    frame_bytes = MicCapture.OUTPUT_FRAME_SAMPLES * 2
    for off in range(0, len(pcm), frame_bytes):
        chunk = pcm[off:off + frame_bytes]
        await turn.send_audio(chunk)
        await asyncio.sleep(len(chunk) / (tts.DAEMON_RATE_HZ * 2))
    await turn.end_input()


# ---- transcript writer ---------------------------------------------

def _write_transcript(
    prompt: str,
    trace: TurnTrace,
    audio: bytes,
    *,
    capture: TurnCapture | None,
    estimated_cost_usd: float | None,
    cost_status: str,
    records: list[ToolCallRecord],
    out_dir: Path,
) -> tuple[Path, Path]:
    """Write provider evidence and the PCM accepted by the simulated sink."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%SZ")
    slug = "".join(c if c.isalnum() else "_" for c in prompt)[:40]
    base = f"{ts}_{slug}_{trace.turn_id[:6]}"
    md_path = out_dir / f"{base}.md"
    audio_path = out_dir / f"{base}.response.wav"
    traces_path = TRACES_DIR / f"{base}.jsonl"

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
    lines.append(f"- **Estimated cost (USD)**: "
                 f"{estimated_cost_usd if estimated_cost_usd is not None else 'unavailable'} "
                 f"({cost_status})")
    lines.append(f"- **Duration**: "
                 f"{(trace.events[-1].ts - trace.started_at):.2f}s"
                 if trace.events else "- **Duration**: n/a")
    lines.append("")
    lines.append("## Prompt (synthesized)")
    lines.append("")
    lines.append(f"> {prompt}")
    lines.append("")

    lines.extend(["## Tool calls", ""])
    for record in records:
        lines.extend([
            f"### `{record.name}`", "", "```json",
            json.dumps(asdict(record), indent=2, default=str), "```", "",
        ])
    if not records:
        lines.extend(["_The model called no tools._", ""])

    lines.append("## Spoken text")
    lines.append("")
    spoken = capture.assistant_text if capture else None
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
    lines.append("This WAV records simulated output. It does not prove speaker playback "
                 "or what a person heard.")
    lines.append("")

    lines.append("## Raw trace")
    lines.append("")
    lines.append(f"`{traces_path.name}` — JSONL, one event per line.")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, audio_path


# ---- the harness ---------------------------------------------------

class VoiceEvalHarness:
    """One paid connection per session; scenarios share its history and tool state."""

    def __init__(self, cfg: Config, *, audio_cache_dir: Path | None = None) -> None:
        self.cfg = cfg
        self.audio_cache_dir = audio_cache_dir or tts.DEFAULT_CACHE_DIR
        self._connection = None
        self._pricing = pricing_for_model(cfg.active_voice_model, overrides=load_pricing_overrides())
        self._usage_store = UsageStore(":memory:", pricing=self._pricing)
        self._tool_records: list[ToolCallRecord] = []
        self._session_id = uuid.uuid4().hex
        self._connection_lock = asyncio.Lock()
        # Scenarios read tool handles before the first paid call.
        self.test_state: dict[str, object] = {}
        self._registry = _build_test_registry(cfg, test_state=self.test_state)

    async def _ensure_connection(self):
        if self._connection is not None:
            return self._connection
        async with self._connection_lock:
            if self._connection is not None:
                return self._connection
            wrapped = traced_registry(self._registry, records=lambda: self._tool_records)
            connection = _make_connection(self.cfg)
            _wire_billable_activity_meter(
                connection=connection, usage_store=self._usage_store,
                provider=self.cfg.voice_provider,
                flat_per_hour_usd=self._pricing.flat_per_hour_usd,
            )
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
            if connection.is_paused():
                # start() now survives a terminal connect so the daemon
                # stays up; an eval must fail fast on it instead, with
                # the provider's own reason.
                detail = connection.last_failure_detail()
                await connection.stop()
                raise RuntimeError(
                    f"voice-eval: provider={self.cfg.voice_provider} rejected "
                    f"the connection: {detail}"
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
            try:
                os.unlink(db_path)
            except OSError:
                pass
        research_db_path = self.test_state.get("research_db_path")
        if isinstance(research_db_path, str):
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
            shutil.rmtree(wake_dir, ignore_errors=True)

        self._usage_store._conn.close()

    async def ask(self, prompt: str, *, turn_timeout_sec: float = 30.0) -> TurnResult:
        result, _ = await self._run_turn(prompt, turn_timeout_sec, interrupt=False)
        return result

    async def ask_with_barge_in(self, prompt: str, *, turn_timeout_sec: float = 30.0) -> dict:
        """Interrupt at the simulated output boundary; retain the turn's evidence."""
        _, ack = await self._run_turn(prompt, turn_timeout_sec, interrupt=True)
        assert ack is not None, "No output acknowledgement before interruption"
        return ack

    async def _run_turn(
        self, prompt: str, turn_timeout_sec: float, *, interrupt: bool,
    ) -> tuple[TurnResult, dict | None]:
        """Bound acquisition, input and playback after prompt and connection setup."""
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

        self._tool_records = []
        sink = RecordingPlayout()
        capture = None
        usage = TurnUsage()
        turn = None
        prior_spend = self._usage_store.spend_last_24h_usd()
        outcome = "incomplete"
        try:
            async with asyncio.timeout(turn_timeout_sec):
                turn = await connection.acquire_turn()
                await _send_pcm_to_turn(turn, prompt_pcm)
                async def on_first_write() -> None:
                    turn.request_local_interrupt()

                await play_responses(
                    turn, sink, barge_in_enabled=interrupt,
                    on_first_write=on_first_write if interrupt else None,
                )
                if turn.turn_lost() or turn.audio_dropped_bytes():
                    raise AssertionError("Provider response was lost or truncated")
                if not sink.audio:
                    raise AssertionError("No response audio received")
                outcome = "interrupted" if interrupt else "complete"
        except BaseException as error:  # noqa: BLE001
            outcome = type(error).__name__
            raise
        finally:
            server_complete = turn is not None and turn.server_turn_complete()
            if turn is not None:
                capture, usage = turn.capture(), turn.usage()
                try:
                    await turn.release()
                except Exception:  # noqa: BLE001
                    logger.warning("voice-eval: turn.release() raised", exc_info=True)
            cost = None
            cost_status: Literal["estimated", "incomplete", "unpriced"] = "unpriced"
            if not self._pricing.label.startswith("unpriced:"):
                cost_status = "incomplete"
                if self._pricing.flat_per_hour_usd > 0:
                    cost = self._usage_store.spend_last_24h_usd() - prior_spend
                elif server_complete:
                    cost = self._pricing.estimate_cost(usage.breakdown or {
                        "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                    })
                if cost is not None:
                    cost_status = "estimated"
            trace.append("turn_end", {"capture": asdict(capture) if capture else None,
                                      "usage": asdict(usage), "audio_bytes": len(sink.audio),
                                      "outcome": outcome, "estimated_cost_usd": cost,
                                      "cost_status": cost_status,
                                      "simulated_flush": sink.flush_ack})
            reset_active(token)
            md_path, wav_path = _write_transcript(
                prompt, trace, sink.audio, capture=capture,
                estimated_cost_usd=cost, cost_status=cost_status, records=self._tool_records,
                out_dir=TRANSCRIPTS_DIR,
            )
            logger.info("voice-eval: model=%s outcome=%s estimated_cost_usd=%s transcript=%s",
                        self.cfg.active_voice_model, outcome, cost, md_path)
        return TurnResult(
            prompt=prompt, trace=trace, audio=sink.audio, capture=capture, usage=usage,
            estimated_cost_usd=cost, cost_status=cost_status, tool_call_records=self._tool_records,
            transcript_path=md_path, response_audio_path=wav_path,
        ), sink.flush_ack

    # --- assertion helpers --------------------------------------------

    @staticmethod
    def match_minutes(
        actual, expected, *, tol: int = 1,
    ) -> bool:
        """Same-length minute lists within ±`tol`. Re-exported here so
        scenarios don't need a separate import for the common
        comparison."""
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
        return datetime_time(hour=hh, minute=mm)
