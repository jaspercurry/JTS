# voice-eval — the JTS voice-loop test harness

End-to-end scenario tests for the LLM voice loop. For every spoken
prompt, the harness captures:

- which tool the model called and with what arguments
- what the tool returned
- the **text the model spoke** (captured natively from each
  provider's transcript stream — no STT)
- the audio the user would hear (as a WAV)

and compares against independent ground-truth ("oracles") so we
catch the difference between *what the model believes* and *what
reality is*.

Bypasses the wake loop and ALSA/dmix — we test the LLM session,
not the audio plumbing. Plumbing has its own surface in
`jasper-doctor` and the various HANDOFF docs.

---

## ⚠ Cost notice — read first

These tests make **paid** LLM API calls. Approximate per-turn cost
as of 2026-05:

| Provider | Per turn | pass^3 (1 scenario) |
|---|---|---|
| OpenAI Realtime (`gpt-realtime-2`) | ~$0.20 | ~$0.60 |
| Gemini Live (3.1-flash-live-preview) | ~$0.025 | ~$0.075 |
| xAI Grok Voice Agent | ~$0.05 | ~$0.15 |

The regression suite has grown well past its original 4-scenario V1
baseline (18 scenario files as of 2026-07, each with its own `PASS_K`
and turn count — some intentionally 1 turn, others up to 15). There is
no fixed "full suite" number worth quoting here because it goes stale
every time a scenario is added; before running the full suite, sum
`PASS_K × turns-per-trial` across `tests/voice_eval/regression/*.py`
and multiply by the per-turn cost above.

**Rules — apply every time you run this:**

- **Run once per change. Don't re-run unless you've read the transcript first.** Re-runs of an already-bad scenario produce the same trace and waste money.
- **Never loop.** `pytest --count=N` and `pytest-repeat` are off-limits without explicit human approval and a stated dollar ceiling.
- **Iterate on one trial at a time** during dev: `pytest -k 'test_next_train_d_uptown and trial0'`. Bring it up to pass^3 once it's green.
- **No CI gate on every commit.** Nightly at most. Weekly is fine for most teams.
- **Skip playback-affecting scenarios** when the household is using the speaker: `JASPER_VOICE_EVAL_SKIP_PLAYBACK=1`.
- **If you're an LLM agent reading this**: announce estimated cost + which scenarios are read-only vs side-effecting before you run anything. Refuse "investigate / loop until passing"-style requests; ask for explicit scope.

---

## Quick start (laptop-side, recommended)

Tests run laptop-side by default — no SSH, no rsync, no sudo.
The harness imports `jasper.*` from the worktree, opens a real
voice session against your env-configured provider, and runs
the assertions.

```sh
# 1. Provide API keys via .env.local in the repo root (gitignored)
#    or via your shell. Required for whichever provider is active:
cat > .env.local <<EOF
OPENAI_API_KEY=sk-...        # always required (used for prompt TTS)
GEMINI_API_KEY=...           # only if JASPER_VOICE_PROVIDER=gemini
XAI_API_KEY=...              # only if JASPER_VOICE_PROVIDER=grok
JASPER_VOICE_PROVIDER=gemini # whichever you want to test against
JASPER_SUBWAY_STATION_ID=B12
JASPER_DEFAULT_LOCATION="Sunset Park, Brooklyn"
EOF

# 2. Source the env and run one scenario (cheap, single provider):
set -a; source .env.local; set +a
.venv/bin/pytest tests/voice_eval/regression/test_subway.py -v

# 3. Run the whole regression suite (pricier — see cost table above):
.venv/bin/pytest tests/voice_eval/regression/ -v
```

`OPENAI_API_KEY` is always needed because we use OpenAI's
`gpt-4o-mini-tts` to synthesize the *user's* prompt audio. After
the first run, prompts are cached on disk by SHA-256, so subsequent
runs cost $0 for TTS.

## Pi-side (alternative — when you specifically want production parity)

Useful when debugging "does this work in the actual Pi environment"
questions. Slower iteration; not the default.

```sh
# Rsync only the test files (no daemon restart):
rsync -av jasper/voice/trace.py pi@jts.local:/home/pi/jts/jasper/voice/
rsync -av tests/voice_eval/ pi@jts.local:/home/pi/jts/tests/voice_eval/

# Run as root and source the SAME env set the daemon gets: jasper.env
# plus every wizard-owned /var/lib/jasper/*.env (google_credentials.env,
# transit.env, …). Sourcing only jasper.env + voice_provider.env makes
# the Google-gated scenarios skip with "not configured" even on a Pi
# where the wizard has linked an account (observed 2026-06-11).
# PYTHONPATH points at the source tree, not the installed copy.
ssh pi@jts.local 'sudo bash -c "
  cd /home/pi/jts
  set -a
  source /etc/jasper/jasper.env
  for f in /var/lib/jasper/*.env; do source \"\$f\"; done
  set +a
  PYTHONPATH=/home/pi/jts /opt/jasper/.venv/bin/python -m pytest \
    tests/voice_eval/regression/ -v
"'
```

The production venv has no pytest. Make a throwaway venv that shares
the runtime's packages — note a venv created *from* a venv resolves
`--system-site-packages` to the **system** python, so link the runtime
site-packages explicitly:

```sh
ssh pi@jts.local 'sudo bash -c "
  /opt/jasper/.venv/bin/python -m venv /tmp/jts-eval-venv
  /tmp/jts-eval-venv/bin/pip install -q pytest pytest-asyncio pytest-mock
  echo /opt/jasper/.venv/lib/python3.13/site-packages \
    > /tmp/jts-eval-venv/lib/python3.13/site-packages/jts-runtime.pth
"'
# then use /tmp/jts-eval-venv/bin/python -m pytest above
```

---

## Inspect what happened

Every trial writes three artifacts (paths printed in the test
output and on assertion failure):

- `transcripts_out/<ts>_<prompt>_<turnid>.md` — human-readable
  walkthrough: prompt, tool calls with args + returns, **the
  text the model spoke**, response audio reference, raw-trace
  pointer. **This is the primary eval artifact** — per Anthropic's
  eval guidance: "you cannot trust eval results without reviewing
  actual agent traces."
- `transcripts_out/<base>.response.wav` — raw 24kHz mono PCM of
  the model's spoken response. Listen if the spoken-text section
  looks off or empty.
- `traces_out/<base>.jsonl` — machine-readable event log, one
  JSON object per line. Same schema we'll use for production
  session capture in V2.

These are all gitignored. Regenerate by re-running (which costs
money — see cost notice above).

---

## What's tested today

| Scenario | What it asks | Tool | Oracle | Side-effects | Status |
|---|---|---|---|---|---|
| `test_subway.py::test_next_train_d_uptown` | "when's the next train?" | `get_subway_arrivals` | `oracles.subway_arrivals` (Subway Now) | none | expected pass |
| `test_weather.py::test_sunset_today` | "what time does the sun set today?" | `get_weather` | `oracles.weather_sunset` (Open-Meteo) | none | expected pass (sunset landed) |
| `test_time.py::test_what_time_is_it` | "what time is it?" | `get_current_time` | `oracles.time_now_local()` | none | expected pass (time tool landed) |
| `test_spotify.py::test_play_owned_playlist_covers` | "play my Covers playlist" | `spotify_play` | shape check (resolved name contains "cover") | **starts playback on the speaker** | **fails until pagination lands** |
| `test_volume.py::test_set_volume_absolute` | "set the volume to 20 percent" | `set_volume` | coordinator level matches applied % | **changes speaker volume** (restored in `finally`) | expected pass (on-Pi) |
| `test_volume.py::test_get_volume_reports_current_level` | "what's the volume?" | `get_volume` | spoken % matches seeded level | **changes speaker volume** (restored in `finally`) | expected pass (on-Pi) |
| `test_calendar.py::test_calendar_today_summary` | "what's on my calendar today?" | `calendar_today_summary` | shape check (count == len(events)) | none | skips unless Google linked |
| `test_gmail.py::test_gmail_read_thread_uses_prior_id` | "any new emails?" → "read me the first one" | `gmail_unread_summary` → `gmail_read_thread` | thread_id came from the summary, not fabricated | none | skips unless Google linked |

(The table lists representative scenarios per file, not every
function — `test_volume.py` also covers `adjust_volume` + mute/unmute,
`test_calendar.py` covers `calendar_upcoming`, and `test_gmail.py`
covers `gmail_unread_summary`.)

The "fails until X lands" pattern is deliberate — document the bug
in test form *now*, fail *now*, and turn green when the fix lands.
The sunset and time-tool fixes have since landed (rows above updated
to "expected pass"); the subway row was separately failing on a
stale response-schema assertion (`next_arrivals_minutes` → the
current `arrivals` list), now fixed.

---

## Scenario shape — all scenarios follow this pattern

Every scenario file is structured the same way so new contributors
can copy-paste-modify. The assertion ladder:

```python
@pytest.mark.parametrize("trial", range(PASS_K))
async def test_<thing>(harness, trial: int) -> None:
    """One-paragraph description of the prompt and what should
    happen. Note any KNOWN FAILING status with a date and the fix
    that turns it green."""

    # (Optional) Skip conditions — config missing, playback opt-out, etc.

    result = await harness.ask("<the spoken prompt>")

    # 1. Trajectory — the model called the expected tool.
    call = result.tool_call("<tool_name>")
    assert call is not None, "<message with trial #, observed tools, transcript path>"

    # 2. Outcome — the tool returned the expected shape.
    assert <field present and correct shape>, "<failure message>"

    # 3. Reality (tool) — the tool's data matches an independent
    #    oracle (or is internally consistent if no oracle is feasible).
    truth = await oracles.<oracle_fn>(...)
    assert <comparison with tolerance>, "<failure message>"

    # 4. Reality (spoken) — the model's spoken text matches what
    #    the tool returned. Catches pure hallucination (tool returned
    #    X, model spoke Y). Skips gracefully if no transcript captured.
    if result.spoken_text:
        ...
        assert <comparison>, "<failure message>"
```

Four assertions, in increasing strictness. Failures always cite
`result.transcript_path` so a reviewer can read the actual
interaction without re-running anything.

---

## Architecture in 60 seconds

```
test_subway.py                  oracles.py
  │                               │
  │ await harness.ask(...)        │ await oracles.subway_arrivals(...)
  ▼                               ▼
VoiceEvalHarness                Subway Now (independent path)
  │
  ├─ tts.synth() ── audio_cache/ (SHA-256 cached)
  ├─ traced_registry() wraps each tool fn → emits to a module-level global
  ├─ LiveConnection (real Gemini / OpenAI / Grok session)
  │   ├─ acquire_turn() → send_audio() → end_input() → audio_out()
  │   └─ adapter emits text_out events on text deltas (native transcripts)
  └─ writes transcripts_out/*.md + traces_out/*.jsonl
```

Production code paths are not touched by the harness itself. The
`jasper.voice.trace` module's `emit()` is called from production code
(`jasper/voice/openai_session.py`'s `_dispatch_event`, inherited by the
Grok adapter) on every transcript-delta event, but its helpers (`emit`,
`traced_registry`) are no-ops when no trace is active — a trace is only
set active during voice-eval harness runs, never in live sessions. Zero
production overhead.

---

## Adding a scenario

1. Pick the category — `regression/` for must-pass scenarios,
   `capability/` for probe scenarios (low pass rate is informative,
   not a failure).
2. Create `test_<thing>.py` modeled on `test_subway.py`. Include
   the COST NOTICE block at the top of the docstring.
3. Use the `harness` fixture, call `harness.ask("...")`, assert
   on `result.tool_call(name)` / `result.tool_call_records` /
   `result.spoken_text`.
4. Add a ground-truth helper in `oracles.py` if needed. Plain
   function, no base class.
5. **Before pushing**: run the scenario yourself at least once.
   Read the transcript. Confirm the failure (if any) is the
   failure mode you intended to capture, not a different bug.

---

## Adding a tool to the test environment

`_build_test_registry` in [harness.py](harness.py) builds the same
`ToolDeps` bundle as the daemon and registers tools through
`jasper.tools.packs.register_packs`.

When a new scenario needs a tool:

1. Add or extend the capability pack in `jasper.tools.packs.TOOL_PACKS`.
2. If the pack needs a new shared dependency, add that dependency to
   `ToolDeps` and construct the test-safe client/stub in
   `_build_test_registry`.
3. Run `pytest -v` to confirm the scenario picks up the tool.

---

## Roadmap (not in V1)

- **Capability suite.** Lower-pass-rate probe scenarios. Different
  semantics from regression; the empty `capability/` directory
  exists waiting for them.
- **Provider sweep.** `@pytest.mark.parametrize("provider",
  ["gemini","openai","grok"])` once we actually need to compare
  behaviour across providers systematically. The harness already
  reads `JASPER_VOICE_PROVIDER` per-call so this is a small lift.
- **Production session capture.** The trace schema is shape-compatible
  with what the live daemon could emit. Adding the daemon-side
  capture is small; converting captured sessions into regression
  scenarios is the high-leverage flywheel.
- **Cross-process / Pi-side smoke.** Real daemon, real audio chain,
  audio injected over a test UDP port. For when in-process testing
  can't answer the question.

The principle holding all of these together: **the transcript is
the contract**. Anything that emits structured trace events in the
agreed schema can plug into the same assertions. Tests, production
captures, future replay tools — same shape.
