# JTS Research Tool — Vision, Design & Roadmap

**Part of the JTS extensibility model** — the first *Feature* (cross-layer
vertical), plus the pluggable text-LLM *provider* layer it rides on. The
cross-cutting lens (the host-mediated-indirection invariant, the five
extension contracts, the decision tree) lives in
[extensibility.md](extensibility.md).

> **Status: living plan.** Forward-looking roadmap for the async
> "research this and tell me later" capability and the modular
> text-LLM-provider layer it rides on. Phase 1 foundation, Phase 2
> voice wiring, and Phase 3 etiquette/failure hardening are implemented,
> including privacy-safe `/state.research` and `jasper-doctor`
> observability; Phase 4 remains roadmap. Grounded in a
> 6-agent research pass (web-verified API capabilities + codebase reads)
> on 2026-06-19. **Last updated: 2026-06-22.**

---

## 1. The vision

Today every JTS tool is request/response inside one voice turn: the model
calls a tool, the tool answers in <12 s, the model speaks, the turn ends.
The **Research** tool breaks that mold on purpose. The user says:

> "<wake word>, research <X> and let me know."

The live model calls `research(query)`, which hands the question to a
**separate text LLM** running in the **background**. The voice turn ends
immediately with a spoken ack ("On it — I'll let you know."). Seconds to
minutes later the answer is ready, and the speaker **proactively perks
up** and delivers it — no wake word needed.

This is the first JTS tool whose result arrives **out of band**, and the
first to use a **text** LLM alongside the real-time **voice** LLM. It is
a clean, first-party **Phase-2** feature on the tool-platform trust
gradient (see [tool-platform-plan.md](tool-platform-plan.md)): you write
it and you run it, so it needs none of the Phase-3 "untrusted code"
machinery (sandbox, secret vault, marketplace).

**Why it's cheap:** ~80% already exists. The "fire-and-forget, perk up
later" shape is the **timer subsystem** ([`jasper/timers.py`](../jasper/timers.py)),
and the "speaker spontaneously speaks" path is the **timer-fire
announcement** (`announce_timer` → `_play_dynamic_text` → `cues.speak_text`,
all in [`jasper/voice_daemon.py`](../jasper/voice_daemon.py)). Research
reuses both. The genuinely new code is small.

---

## 2. v1 product shape — "Research" that offers to read a 30-second answer

The v1 flow, end to end:

1. **Ask.** "Research the best induction ranges under $2,000 and let me
   know." → the live model calls `research(query="...")`.
2. **Ack + end turn.** The tool starts the background job and returns
   `{ok, confirm, job_id}` instantly; the model speaks `confirm`
   ("On it — I'll let you know.") and the turn ends.
3. **Background.** A text LLM (v1: OpenAI) answers the query. **The
   prompt we send asks for a spoken-friendly answer of 30 seconds or
   less** (~75 words / ~450 characters), so the result is already
   consolidated. A hard character cap on the stored result is the
   backstop.
4. **Perk up + ask.** When the answer is ready (and the speaker is
   idle — not mid-conversation, see §6 etiquette), JTS announces:
   "Your research is ready — want me to read it now?"
5. **Open a short confirmation window.** After the announcement TTS
   drains and the speaker is silent, JTS opens a no-wake-word
   `WakeLoop._begin_turn` window with no pre-roll. Silence for the
   normal no-speech timeout dismisses without committing audio to the
   model. A yes calls `read_research_result(..., decision="yes")` and
   reads the ≤30 s answer. A no calls
   `read_research_result(..., decision="no")` and speaks the dismiss
   line. If mic mute, measurement, spend cap, or provider pause would
   block the confirmation window, JTS reads immediately so the result is
   never silently lost.

This is **not barge-in**: the confirmation window opens only after the
announcement has fully drained. A real wake during the window cancels
the confirmation and wins, mirroring the normal wake/acquire shape.

The full result text is **stored** (not just spoken) — the seed of the
future interaction history log (§7), and the recovery path if the
announcement can't play.

---

## 3. Architecture — reuse the timer pattern, add a text-provider layer

### 3.1 The tool is a normal fast tool (no `dispatch_tool` change)

`research(query)` is an ordinary `@tool` whose `execute()` starts a
background job and returns an ack inside the 12 s dispatch budget — a
structural clone of `set_timer` + `TimerScheduler`. The minutes-long
work lives in a detached `asyncio.Task` owned by a new
`ResearchScheduler`, **not** in the dispatch coroutine, so
[`dispatch_tool`](../jasper/tools/__init__.py) needs **zero** changes.
The scheduler is a shared dep on `ToolDeps`
([`jasper/tools/packs.py`](../jasper/tools/packs.py)), mirroring
`timer_scheduler`, constructed in `daemon_main._build_registry`.

### 3.2 Execution model — start-then-poll, no webhook

For the background call, **start the job and poll it** — do **not** open
an inbound webhook (that needs public ingress into a LAN device,
violating the home-LAN threat model) and do **not** hold one HTTP
request open for minutes (a home-WiFi blip kills it with no clean
resume). OpenAI's **Responses API background mode** (`background: true`)
runs the job **server-side**; JTS polls `GET /v1/responses/{id}` until
terminal. That survives brief WiFi blips while the daemon stays up. A
daemon restart marks the local job failed and announces that failure
rather than re-dispatching and risking a duplicate paid request; JTS
does not persist the provider-side response id yet. Poll cadence reuses
`reconnect_backoff_delay()` from
[`jasper/voice/_supervisor.py`](../jasper/voice/_supervisor.py)
(saturated exponential + jitter). Fetch + persist the result promptly on
completion (background output is retained ~10 min). Anthropic (v2) has
no clean server-side equivalent — see §4.

### 3.3 Proactive announcement — reuse timer-fire

The "your research is ready" speech reuses the timer-fire path:
a new `WakeLoop.announce_research_ready(job)` thin-wraps
`_play_dynamic_text` (cue-bake → duck → `cues.speak_text`), modeled on
`announce_timer`. The new bits are small (§6): the announcement is
triggered by a **job completing** instead of a timer deadline, and it
only opens the confirmation window after dynamic TTS reports successful playback.
Phase 3 upgrades the timer's "skip after 5 s in an active session" gate
to hold-and-read-when-idle: results that finish during a voice session
are kept in a small bounded wake-loop queue and drained only after the
state returns to WAKE.

### 3.4 Persistence — a small SQLite job store

`ResearchJobStore` (copy-shaped from `TimerStore`) at
`/var/lib/jasper/research_jobs.db`, fail-soft like every JTS store.
Columns: `id, query, status, result, error, created_at, finished_at,
announced, read`. Survives restart so a finished-but-unannounced report
is not lost (the one place this is **not** a timer copy — see §6).

---

## 4. The text-provider layer — clean, modular, pluggable

The user wants adding a text provider to be as trivial as the
live-provider layer makes adding a voice provider. It is — and far
**simpler**, because a text provider is request→text (no turns, no
audio, no resumption). It is **not** a `LiveConnection` mirror.

A self-contained **Pattern-2 registry** under `jasper/research/`,
modeled on [`jasper/transit/`](../jasper/transit/__init__.py) (the
repo's canonical "open-ended set of self-similar plugins" pattern — see
AGENTS.md "Config ownership"):

```
jasper/research/
  base.py        ResearchRequest / ResearchResult / ResearchError;
                 TextLLMProvider + TextLLMClient Protocols —
                 ONE method: async complete(req) -> ResearchResult
  catalog.py     TextProviderEntry(id, label, key_env, model_env,
                 default_model, provider); PROVIDERS tuple
  __init__.py    active_research_provider(env) — per-provider try/except,
                 returns None when the key is unset (fault isolation)
  providers/
    openai_research.py    v1 — lazy AsyncOpenAI, reads OPENAI_API_KEY
                          + JASPER_RESEARCH_OPENAI_MODEL from the env Mapping
```

**Adding Anthropic (v2) is one module + one registry entry + one dep +
one key line — zero core edits.** It reuses the existing **jasper-secrets**
key compartment (the OpenAI key is already there; the Anthropic key goes
in the same file). This is Pattern 2, **not** typed `Config` — a `Config`
field per provider is the N-core-edits cost AGENTS.md warns against.

**The v1/v2 asymmetry to design for:** OpenAI gives a clean server-side
background job handle (start → poll → fetch). Anthropic does **not** — its
options are 24 h Batches (wrong latency) or a held-open stream (fragile).
So the provider interface abstracts a **durable async job**
(`start → poll/await → result`), and each provider implements it
differently. Do **not** bake "there is always a server-side poll id" into
the shared interface — that's an OpenAI-ism.

---

## 5. Phased roadmap

Each phase is independently shippable and hardware-free-testable.

| Phase | Goal | Effort |
|---|---|---|
| **1 — Foundation** | `jasper/research/` provider registry + `ResearchScheduler` + store as pure pytest-covered units. No daemon wiring, no tool, no audio. Mergeable, registers nothing. **Implemented.** | ~1 day |
| **2 — Wire + demo** | `research(query)` tool, `ToolDeps.research_scheduler`, one `CapabilityPack`, `announce_research_ready`, OpenAI call (≤30 s prompt + char cap), spend integration, regression scenario. **Implemented; earliest the headline UX works.** | ~1–1.5 days |
| **3 — Etiquette + dedicated failure cues** | Hold-and-read-when-idle (don't drop mid-conversation like a timer); rate-limited `research_failed` cue; not-configured kickoff decline; privacy-safe `/state.research` + doctor check; tests pinning retry/deferral semantics. **Implemented; production-safe after this.** | ~1 day |
| **4 — Anthropic (v2, deferred)** | One module + one registry entry + `anthropic` dep + key line. Build only when you add an Anthropic key. | ~½ day |

### First PR (Phase 1) — `research: text-provider registry + background-job scheduler (HW-free)`

- `jasper/research/` (base / catalog / `__init__` / `providers/openai_research.py`) — Pattern-2 registry, one `complete()` method, fault-isolated `active_research_provider(env)`.
- `jasper/research/scheduler.py` — `ResearchScheduler` + `ResearchJobStore` cloned from `jasper/timers.py`: one `asyncio.Task` per job, `on_done` callback, `asyncio.Semaphore` concurrency cap (~2), `asyncio.wait_for(JASPER_RESEARCH_MAX_RUNTIME_SEC ≈ 300 s)` per job, `stop()` cancels in-flight.
- `jasper/config.py` — `JASPER_RESEARCH_DB`, `JASPER_RESEARCH_MAX_RUNTIME_SEC`, concurrency cap defaults.
- Tests: provider resolves with key / `None` without / fault-isolated; store CRUD fail-soft; concurrency cap; runtime-ceiling marks failed; **restart-restore (done-unannounced survives, running marked failed)** — the highest-value test.

---

## 6. Open decisions — chosen v1 defaults

| Decision | v1 default | Why |
|---|---|---|
| Execution mode | **OpenAI background mode + poll** | Survives WiFi blips/restart; no inbound exposure; answers the "polling vs webhook" question — polling, server-side job. (v1 answers are short, so latency is modest either way; the interface is identical to an awaited call, so it's a within-adapter choice.) |
| Restart while a job is mid-flight | **Mark failed + say so** ("Sorry, I couldn't finish that research. Please ask me again.") | Re-dispatch risks double-charging; silent drop breaks the promise. **Companion rule:** a job that *finished* before the restart **survives and is offered for reading** — do NOT copy the timer's drop-if-expired logic for finished reports. This asymmetry has a scheduler test and a wake-loop announce test. |
| Answer length | **≤30 s spoken (~75 words / ~450 chars)** via the prompt to the text model, plus a hard char cap on the stored result | The user asked for a consolidated read-out; instruct the model and guard with a cap. |
| Spend | Reuse `SpendCap.allowed()` kickoff gate + record cost in `UsageStore` + **add the model's price row to `jasper/data/model_pricing.json`** (load-bearing — without it cost prices as $0 and the cap silently under-counts) | Global daily cap suffices for one paid tool; no per-job budgets, no "quote me a price" dialog (defeats fire-and-forget). |
| Wizard / `/system/` display | **Defer** | The OpenAI key is present whenever OpenAI voice is. v2's Anthropic key is the natural trigger. If `/system/` ever shows the active text provider, use a fresh-file reader (like `provider_state.py`), never `os.environ`. |
| Failure speech | Phase 3 plays the provider-agnostic `research_failed` cue for runtime failures and restart-interrupted jobs, rate-limits proactive failed-job audio to once per hour, and adds a not-configured prompt redirect to `/voice/`. | No-silent-failure: an out-of-band job has no wake event to hang a reactive cue on, so failures must speak. The cooldown prevents a burst of failures from nagging the household. |
| Ready-result confirmation | **Ask after TTS drains, then open a ~5 s no-wake-word yes/no window.** | Avoids unsolicited read-outs while keeping the result available. Silence dismisses without a model commit; real wake cancels the window and wins. |

---

## 7. Future vision (build when the trigger fires)

- **Full barge-in.** The current confirmation is deliberately
  post-announcement, speaker-silent, and one-shot. A future version can
  let the user interrupt the announcement itself once the robust
  barge-in contract is ready for proactive TTS.
- **Interaction history log.** A household-visible log of all speaker
  interactions — the natural home for research reports, **links, and
  longer material the speaker won't read aloud** ("the full list is in
  your history"). The `ResearchJobStore` is the seed; the log generalizes
  it across tools. This is its own feature with its own UI; the research
  store should be shaped so it can feed it later.
- **Anthropic + more text providers.** §4's registry makes each new
  provider a one-module add. v2 = Anthropic; beyond that, whatever the
  household wants.
- **Deeper research.** v1 is a ≤30 s summary from a single text-model
  call. A future "deep research" mode (multi-step, longer, possibly a
  provider's agent/research endpoint) rides the same async shape — the
  job just takes longer and the result goes to the history log rather
  than being fully read aloud.

---

## 8. One-line summary

**`research(query)` is a fast tool that hands the question to a
pluggable text LLM running in a bounded background task (OpenAI
background-mode + poll), then announces readiness through the existing
timer-fire announcement path and opens a short no-wake-word confirmation
window before reading the ≤30 s answer — reusing ~80% of what already
exists, adding a small `jasper/research/` provider registry, a scheduler,
a store, usage accounting, an announce method, and privacy-safe state/doctor
observability. Etiquette hardening, the `research_failed` cue, and the
not-configured prompt redirect are implemented; Anthropic, full barge-in, and
richer interaction history are deferred, each behind its own trigger.**

---

Last verified: 2026-06-22
