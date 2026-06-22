# Conversation history (`/chat`) — build plan

> **Status: living plan.** Execution plan for the first deliberate JTS
> **Feature** (a cross-layer vertical, per the extensibility doctrine):
> a household-visible log of what was said to the speaker and what it
> said back, with local opt-in/clear controls. The store, capture seam,
> OpenAI user/assistant transcript path, Grok user transcript path,
> `jasper-chat-web`, `GET /data.json`, `/state.chat`, doctor check,
> nginx/install/landing wiring, static ES-module renderer, household
> capture toggle, clear-all action, and write-time retention pruning are
> implemented as of 2026-06-22; Gemini transcript capture remains
> deferred.
> Grounded in code reads against `main` on 2026-06-19 and refreshed
> against the current tree on 2026-06-22. **Last updated: 2026-06-22.**

**Part of the JTS extensibility model.** This Feature is the *proving
instance* of the Feature contract in [extensibility.md](extensibility.md):
it composes a per-turn capture hook + a store + a web surface, and the host
owns/injects the shared plumbing. Build it concretely; the reusable Feature
helpers get *extracted* from what this and [research](research-tool-plan.md)
duplicate, not designed up front.

---

## 1. The product

A page at `http://jts.local/chat` that shows recent interactions as paired
turns — **the perceived command in, the response back** — newest first, with
a date filter. "Voice is great, but sometimes reading is better": it's where
you re-read something the speaker told you (a fact, a reminder, a research
answer), and, later, where links and longer material that the speaker won't
read aloud live ("the full list is in your history" — the vision named in
[research-tool-plan.md](research-tool-plan.md) §7).

**v1 is not chat-by-text.** It renders a local history and privacy controls;
an interactive text assistant is a different surface — see Non-goals.

Calling it the "perceived command" is deliberate and a feature: the stored
user text is the speaker's *ASR of what it heard*, so the page doubles as a
mis-hear debugger ("turn on the bedroom lights" logged as "turn on the
*burger* lights").

---

## 2. The hard part is the data, and it's resolved native-first

Before this Feature, there was **no conversation text stored anywhere** — the
code deliberately threw it away (`openai_session.py` logged `chars=len(text)`
and dropped the string; [PRIVACY.md](../PRIVACY.md) promised `usage.db` stores
no transcripts). The Feature's real work is *capturing* text, and the strategy
(verified against `main`) is **native-first** — use the transcript the realtime
API already emits; do **not** add audio capture or a local/cloud STT pass (that
would burn the 1 GB RAM budget and reverse the privacy posture far harder).

Per provider, today:

| Provider | User text (ASR) | Assistant text | Work needed |
|---|---|---|---|
| **OpenAI** | captured (input transcription, `gpt-4o-mini-transcribe`, set in `_session_config`) | captured (`assistant_transcript()` from `response.*audio_transcript.delta`) | **stop discarding** — surface both |
| **Grok** | captured (inherits OpenAI input transcription) | **no native path** (the one real gap) | user text only in v1; assistant text deferred |
| **Gemini** *(default)* | not requested | not requested | **net-new**: add `input_audio_transcription` + `output_audio_transcription` to the `LiveConnectConfig` and parse them in `_on_response` |

Because Gemini is the default provider, lighting it up is **required for v1
to not be a dud** — and it is the only net-new *provider* work. It costs zero
resident RAM (text arrives in-band over the existing websocket) and pennies/
month; confirm the exact Gemini text-output rate and any latency on-device
before relying on it.

### The capture seam (the one genuinely-new low-level extension point)

`jasper/voice_daemon.py` → `WakeLoop._end_turn_inner` is the
**provider-neutral convergence point** for a turn: it already runs next to
`self._usage_store.close_session(self._session_id, ...)` with `session_id` in
scope, and is where `update_session_vad` / `set_outcome` already fire. That
is where conversation capture hooks — **one write path for all three
providers.**

What differs per provider is only *how the turn object exposes its text*.
Add a small optional transcript capability beside `LiveTurn`
(`ConversationTranscriptTurn` in `jasper/voice/session.py`), with WakeLoop
probing the methods via `getattr`:

```
user_transcript() -> str | None        # the perceived command (ASR)
assistant_transcript() -> str | None    # what the model said
```

- OpenAI already has `assistant_transcript()`; add a one-line `_user_transcript`
  field fed from the `conversation.item.input_audio_transcription.completed`
  handler.
- Grok inherits OpenAI; `user_transcript()` works, `assistant_transcript()`
  returns `None` (honestly shown as "no transcript for this turn").
- Gemini implements both once the config/parse lands (Phase 2).

The daemon reads whatever the accessors return at `_end_turn_inner` and writes
a row. **The host owns the write; the provider only declares its text** — the
indirection invariant from the doctrine, applied.

> Borrow the *vocabulary* of `jasper/voice/trace.py` (which already
> distinguishes model-emitted `text_out` from an STT transcription) but not
> its plumbing — it is test-only and carries single-turn machinery. Do not
> wire production capture through it.

---

## 3. Storage — a new dedicated store, cloned from research

A new `ConversationStore` at `/var/lib/jasper/conversation_history.db`,
**copy-shaped from `ResearchJobStore`** (`jasper/research/scheduler.py`):
fail-soft (`available` property; every method degrades to a logged no-op on
`sqlite3.Error`), autocommit, one connection.

Schema (one row per captured turn):

```
id           TEXT PRIMARY KEY     -- sortable, e.g. 20260619T201500Z-001
ts_utc       TEXT NOT NULL
provider     TEXT                 -- gemini / openai / grok (read fresh)
user_text    TEXT                 -- the perceived command (may be NULL)
assistant_text TEXT               -- the reply (NULL for Grok assistant gap)
tool_calls_json TEXT              -- nullable: which tools fired + args summary
data_json    TEXT                 -- nullable: future links/rich content (add NOW)
session_id   INTEGER              -- joins usage.db sessions by id
```

- **Do NOT** extend `wake_events.db` (ML corpus, different retention/privacy
  domain) or `usage.db` (its no-transcript promise is load-bearing public
  copy). A dedicated db is what keeps that promise true.
- Add the nullable `tool_calls_json` / `data_json` columns **now** so the
  future links/rich-content vision needs no migration (the doctrine's
  "reserved field" discipline).
- `tool_calls_json` is cheap to populate: the turn already knows which tools
  it dispatched. Capturing it makes the page far more useful ("asked for the
  subway → called `get_subway_arrivals` → replied …") for ~free.

---

## 4. The web surface

`/chat` — a **dedicated socket-activated wizard service** mirroring `/system/`
(`jasper/web/system_setup.py`): a small dashboard/control surface for this
Feature.

- `jasper/web/chat_setup.py`: `_render_page` returns a `canonical_page` shell
  with a mount point and a `type=module` script tag that loads `main.js` from
  the page's `deploy/assets/chat/js/` dir (nginx serves it under `assets`);
  a `GET /` route (guarded by `guard_read_request`) renders it; a
  `GET /data.json` route returns recent rows (newest-first, date-filterable).
  Mutating `/capture` and `/clear` routes use `guard_mutating_request` and
  CSRF headers from the shared static ES-module helpers.
- `deploy/assets/chat/js/*.js`: a small ES-module graph (copy the
  `system-status/js/` shape), reading the CSRF token from the meta tag,
  rendering rows with the shared `table()` primitive. **All DOM via text
  nodes** — user/assistant text is untrusted; never `innerHTML`.
  Prompt 5 implements this renderer; the PR still needs an on-device browser
  pass for render + date-filter behavior.
- New `deploy/jasper-chat-web.{socket,service}` (copy `jasper-system-web.*`),
  added to `WIZARD_UNITS` in `deploy/lib/install/systemd-units.sh` (the
  `restart`-not-`start` lesson) + a `location /chat/` block in
  `deploy/nginx-jasper.conf` + a `console_scripts` entry + a landing-page link.

**Route decision:** `/chat` is currently unclaimed in code. The calibration
agent ([HANDOFF-calibration-agent.md](HANDOFF-calibration-agent.md)) *designs*
interactive `/chat*` routes, but they live on the `jasper-correction-web`
server under the `/correction/` prefix, so there is no collision as long as
this history/control surface owns the top-level `/chat` and the calibration
agent keeps its routes `/correction/`-scoped. (If a future top-level interactive chat is
ever wanted, rename this to `/history`; for now `/chat` matches the ask.)

---

## 5. Privacy, retention, mic-mute — first-class, not an afterthought

This Feature **reverses a deliberate no-store posture**, so the controls are
part of v1, not a follow-up:

- **Opt-in, default-off.** A wizard toggle (its own `/var/lib/jasper/*.env`,
  read fresh) gates capture. Fresh installs capture nothing until the
  household turns it on. The write path checks the flag every turn.
- **Mic-mute gated.** No row is written while `self._mic_muted` is set
  (`jasper/voice_daemon.py`) — mirrors how `wake_events` capture stops under
  mute. A muted mic is a privacy promise; the log honors it.
- **Text only, never audio.** This is the mainstream privacy-respecting design
  (Google "My Activity" offers rich text recall with audio retention off).
- **Bounded retention.** A TTL pruner (default 30 days) + a hard row cap
  (default 500 rows), enforced after production writes. A "clear all" control
  is on the page; per-item delete remains deferred until there is a local UI
  shape for row-level actions.
- **Never logged, never leaves the Pi.** Household-local SQLite only; capture
  must not reintroduce transcript text into journald.
- **A scoped PRIVACY.md paragraph** documents exactly what is stored, the
  opt-in, retention, and clear surface.

---

## 6. This is the Feature contract's proving instance

Mapped to [extensibility.md](extensibility.md):

- **Declares:** a store, a `/chat` web surface, and a per-turn capture
  observer. (No tool pack — capture is daemon-side, not LLM-invoked.)
- **Host owns + injects:** the storage substrate, the web mount, and the
  turn-loop hook point. The Feature never reaches into `jasper-voice`
  internals beyond the declared accessor interface; it never spawns threads
  or loads models (1 GB).
- **Inherits the obligations:** a `/state.chat` section (capture on/off, row
  count, retention, last-write age), a `jasper-doctor` check (skip-if-not-
  configured; warn on store-unavailable), and mic-mute/privacy gating. Capture
  and retention failures must degrade fail-soft (logged no-op), never block a
  turn.
- **Extraction discipline:** build all of this concretely here. The reusable
  Feature helpers (a storage-substrate helper, a web-mount helper) get
  extracted only once a **second** Feature (this + research) confirms the
  shape — do **not** build a generic Feature framework in this work.

---

## 7. Phased roadmap

Each phase is independently shippable and hardware-free-testable (except the
on-device Gemini-transcript and end-to-end checks, called out explicitly).

| Phase | Goal | HW needed |
|---|---|---|
| **1 — Foundation** | `ConversationStore` (cloned, pytest-covered: CRUD, fail-soft, retention, mic-mute gate) + the optional turn transcript capability (`user_transcript()`/`assistant_transcript()`) + the `_end_turn_inner` capture hook, gated by a default-off wizard flag. OpenAI/Grok stop discarding (surface what they already capture). Registers no UI yet. | none |
| **2 — Gemini transcripts** | Add `input_audio_transcription` + `output_audio_transcription` to the Gemini `LiveConnectConfig` and parse in `_on_response`. Lights up the default provider. | on-device cost/latency check |
| **3 — The `/chat` page** | `jasper-chat-web` service + `GET /data.json` + the install/nginx/landing wiring + `/state.chat` + the doctor check + static ES-module paired-turn renderer, research badge, null-assistant note, and date filter. **Implemented.** | on-device browser pass |
| **4 — Retention + privacy controls** | TTL pruner + row cap + clear-all delete + the wizard opt-in polish. The scoped PRIVACY.md paragraph keeps public docs truthful now that capture can be enabled in-product. **Implemented; per-row delete deferred.** | none |
| **5 — Richness (deferred, triggered)** | Surface `tool_calls_json` and links/rich content on the page (the `data_json` column pays off). A narrow Grok assistant-text fallback **only** if Grok usage matters. Search/filter. | none |

**First PR (Phase 1):** `conversation-history: store + capture seam (HW-free)`
— the store, the accessor interface, the gated hook, OpenAI/Grok surfacing,
and the pytest units (the highest-value test: a turn writes a row with the
right text for a mocked OpenAI turn; a muted turn writes nothing; a store-
unavailable path is a logged no-op that never raises into the turn).

---

## 8. Test plan

- **Store:** CRUD, fail-soft (unavailable db → no-ops, never raises),
  retention pruner (TTL + cap), ordering (newest-first read).
- **Capture hook:** mocked `LiveTurn` with/without each accessor → correct row
  (or none); **mic-mute gate** → no write; **opt-in flag off** → no write;
  capture exception → logged, turn unaffected.
- **Per provider:** OpenAI turn yields user+assistant text; Grok yields user
  text + `None` assistant; Gemini (post Phase 2) yields both.
- **Web:** `GET /` renders + uses shared primitives; `GET /data.json` shape;
  route guards (`guard_read_request`); untrusted text is escaped (no
  `innerHTML`). The conventions tests (`test_web_wizard_conventions`,
  `test_web_json_island`, `test_web_design_system`) must pass.
- **Docs:** this doc is mapped in `doc-map.toml` + the README atlas + has a
  `Last verified` footer (orphan/linkcheck/freshness CI gates).
- Not a tool → **no `tests/voice_eval/regression/` scenario** (that rule is
  for LLM-callable tools; capture is daemon-side).

---

## 9. Non-goals (v1)

- **Not interactive chat-by-text.** This is a read-only log, not a second way
  to talk to the assistant. (A future interactive surface would be its own
  Feature; it is *not* the calibration agent's `/chat` either.)
- **Not a new tool.** The LLM does not call anything to make this work.
- **No audio retention**, no local/cloud STT, no resident model.
- **None of the Phase-3 untrusted-code machinery** (sandbox, permission
  enforcement, secret broker, signing, marketplace) — out of scope by the
  trust gradient.

---

## 10. Open decisions (need the owner before/while building)

1. **Default retention window** — 30 days and 500 rows. The pruner + cap
   exist regardless; env vars can disable or tune either bound.
2. **Capture default** — confirmed default-**off**, opt-in via the wizard
   (reverses the no-store posture only on explicit consent).
3. **Gemini transcription in v1** — recommended **yes** (it's the default
   provider; without it the page is empty on a stock install). Gate on the
   on-device cost/latency check.
4. **Per-member attribution** — single household view for v1 (no voice
   diarization, per household norms); revisit only if asked.
5. **Route name** — `/chat` (matches the ask; free in code today) vs `/history`
   (frees `/chat` for a possible future interactive surface). Defaulting to
   `/chat`.

---

Last verified: 2026-06-22
