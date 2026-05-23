# HANDOFF — Voice prompting playbook

> *"Don't tune prompts by intuition."* Each provider publishes a
> prompting guide whose structure mirrors how the model was
> RLHF-trained. Aligning with that structure makes instructions
> stick; fighting it produces partial compliance at best.

This is the **canonical reference** for writing or editing any
LLM-facing prompt surface in JTS — the `SYSTEM_INSTRUCTION` in
`jasper/voice_daemon.py`, tool descriptions in `jasper/tools/`,
and per-tool conditional rules.

**Last fetched against provider docs: 2026-05-23.** Re-check the
linked sources every ~3 months or when a model version bumps.

**Path B applied 2026-05-23.** `build_tool()` now sends the full
cleaned docstring to the LLM; per-tool conditional rules live in
each tool's docstring under `jasper/tools/`. `SYSTEM_INSTRUCTION`
trimmed from ~265 lines to ~100 lines (well below Gemini's ~500-
token soft ceiling). The "Recommended edits to current code"
section at the bottom records what landed.

## Scope

Covers: system instruction + tool descriptions + per-tool
conditional rules for the three real-time voice providers JTS
supports — OpenAI `gpt-realtime-2`, Google `gemini-3.1-flash-live-preview`,
xAI `grok-voice-think-fast-1.0`.

Out of scope: provider architecture
([HANDOFF-voice-providers.md](HANDOFF-voice-providers.md)),
session management ([HANDOFF-persistent-live-session.md](HANDOFF-persistent-live-session.md)),
audio path ([audio-paths.md](audio-paths.md)), idle-anchor /
tool-round watchdog contract
([audit-pending-followups.md](audit-pending-followups.md):173).

---

## TL;DR — read before every prompt edit

1. **Conditional over absolute.** OpenAI's docs explicitly say
   *"remove overlapping `always`, `never`, `only`, and `must`
   rules unless they are truly required."* Phrase rules as
   "When X, do Y" and **enumerate X** — the model doesn't
   generalize unstated scopes.
2. **Structure helps OpenAI/Grok. Brevity helps Gemini.** OpenAI
   publishes an opinionated 12-section template. Gemini 3.1
   *"may over-analyze verbose or complex prompt engineering
   techniques from older versions"* and prefers terse direct
   prompts. Our current SYSTEM_INSTRUCTION is OpenAI-shaped;
   the trade-off on Gemini is documented in "Provider deltas"
   below.
3. **Per-tool conditional rules belong in the tool description,
   not the system prompt.** Community-validated for OpenAI; the
   Gemini Live best-practices guide says *"Be sure to tell Gemini
   under what conditions a tool call should be invoked"* (inside
   the tool description). Path B (applied 2026-05-23):
   `build_tool()` sends the full cleaned docstring; per-tool
   conditional rules (when to call, voice-answer style,
   response-shape handling) live in the tool's docstring.
4. **Don't ban preambles. List when to skip.** Absolute "never
   preamble" rules get ~33% compliance on gpt-realtime per a
   public community thread. OpenAI's documented suppression
   pattern is conditional.
5. **POSITIVE framing for tool calls.** "Call X when Y," not
   "Don't forget X." Verified failure mode: the previous
   negative-heavy version of our prompt produced zero tool calls
   across five voice-eval scenarios (rationale block at
   [voice_daemon.py:74-104](../jasper/voice_daemon.py)).
6. **ALL-CAPS imperatives work for guardrails.** Google's own
   language-pinning template uses `RESPOND IN {LANG}. YOU MUST
   RESPOND UNMISTAKABLY IN {LANG}.` Use sparingly and only for
   non-negotiable rules.
7. **Voice-eval is paid.** Iterating prompts via repeated full
   scenario runs burns money fast (~$0.075/scenario on Gemini,
   ~$0.60 on OpenAI). Investigate transcripts, don't loop.
8. **Surface prompt edits aren't bulletproof — gate at the tool
   layer too.** Voice models lag their text siblings by ~5–8
   points on tool-selection benchmarks
   ([Daily.co voice-agent bench](https://www.daily.co/blog/benchmarking-llms-for-voice-agent-use-cases/);
   [Confetti / text-to-voice, arXiv:2605.15104](https://arxiv.org/html/2605.15104)).
   Prompt-only mitigations of tool-preference bias show "limited
   effectiveness" in published evaluations
   ([BiasBusters, arXiv:2510.00307](https://arxiv.org/abs/2510.00307)).
   When a misroute means "wrong action taken" vs "couldn't find
   that", add a server-side validation gate alongside the prompt
   rule. Worked example: `spotify_play(kind='track')` applies the
   same `fuzz.WRatio >= 75` threshold `kind='auto'` uses, so a
   misroute degrades to refusal instead of playing the wrong
   song. See "Architectural alternatives considered" below.

---

## Cross-provider principles

These hold for all three providers we use. Provider-specific
nuance lives in the next section.

### 1. Conditional rules over absolutes

OpenAI's prompting guide (canonical source):

> *"Use precise language. The model may prioritize the exact
> wording of an instruction over the broader behavior you intended."*
>
> *"Remove overlapping `always`, `never`, `only`, and `must`
> rules unless they are truly required. Define priority when
> rules compete."*

The recommended replacement is `When <trigger>, <action>.` —
and **the triggers should be enumerated**, not left to the
model to generalize:

> *"When a user provides an exact identifier, including
> confirmation codes, order IDs, ticket IDs, reset PINs, claim
> numbers, tracking numbers, or account numbers, repeat the
> captured value and wait for confirmation before using it in a
> tool call."*

Real-world signal: a community thread
([Realtime API Preamble Inconsistent](https://community.openai.com/t/realtime-api-preamble-inconsistent/1361953))
documents only **~33% compliance** on non-conditional preamble
rules with gpt-realtime. The community fix that worked was
moving the rule into a per-tool description.

**Gemini caveat.** A developer forum thread
([Gemini 3.1 Flash Live Preview not following system instructions](https://discuss.ai.google.dev/t/gemini-3-1-flash-live-preview-not-following-system-instructions/144659))
reports 3.1 audio-mode *ignoring* conditional system
instructions that 2.5 honored — *"only confirm seeing the
screen if you have received message 'SCREEN SHARING ACTIVATED'"*
gets violated. No Google response in the thread. Google's
public blog claims the opposite (*"Adherence to complex system
instructions has been boosted significantly"*). Take the
marketing claim with caution — A/B test before relying on
conditionals for Gemini-only behavior.

### 2. Length and structure are inversely valued

OpenAI publishes a 12-section labeled skeleton (Role,
Personality, Language, Reasoning, Message Channels, Preambles,
Verbosity, Tools, Unclear Audio, Entity Capture, Long Context,
Escalation). Sections are opt-in: *"Not every use case needs
every section."* Labels help.

Gemini's guidance is the opposite:

> *"Be concise in your input prompts. Gemini 3 responds best to
> direct, clear instructions."*
>
> *"The model may over-analyze verbose or complex prompt
> engineering techniques from older versions."*

And there's a measured ceiling: [PLAN.md](../PLAN.md) "Risks
worth re-flagging" tracks that *"Long Gemini system prompt
breaks session resumption on the 3.1 Flash Live preview. Keep
system instruction under ~500 tokens."* Our current
`SYSTEM_INSTRUCTION` is ~265 lines, well over that ceiling.

**Practical posture for JTS:** we optimize for OpenAI's style
(labeled sections, explicit per-tool rules) because OpenAI is
the most-used provider and produces the cleanest signal when
violated. Gemini-specific divergences land in the
[Recommended edits](#recommended-edits-to-current-code) section
as candidate work.

### 3. Where per-tool conditional rules live

Both OpenAI's documented patterns and the community thread
converge on:

- **System prompt:** cross-tool meta-rules (role, persona,
  preamble policy, verbosity).
- **Tool description:** per-tool conditionals ("when result has
  `confirm` field, speak it verbatim"; "if `is_stale=true`,
  preface with…").

Gemini Live best practices says it directly:

> *"Be specific in your tool definitions. Be sure to tell Gemini
> under what conditions a tool call should be invoked."*

Path B applied 2026-05-23: per-tool output rules now live in each
tool's docstring (sent by `build_tool()` to the model). See
[the cookbook](#tool-prompt-cookbook) for the convention.

### 4. Positive framing for tool calls

`Call X when Y`, not `Don't forget X` or `Never guess`.

The rationale block at [voice_daemon.py:74-104](../jasper/voice_daemon.py)
documents a confirmed failure mode: a prior version of the
prompt had ~15 "Do NOT" clauses and zero positive "Call the
tool when…" instructions, and produced **zero tool calls
across five read-only voice-eval scenarios** on gpt-realtime-2.
The fix was restructuring per OpenAI's positive-framing pattern.

This isn't an "intuition" rule — it's the documented
hallucination pattern OpenAI's docs explicitly call out:

> *"Tell the model when to act immediately, ask for missing
> information, confirm high-precision details, retry after
> failure."*

### 5. Preamble suppression is conditional

OpenAI's documented pattern is *"Do not use a preamble when…"*
with an enumerated skip-list, **never** *"Never preamble."*
The current `SYSTEM_INSTRUCTION` at
[voice_daemon.py:226-240](../jasper/voice_daemon.py) follows this exactly.

When you add a tool fast enough that a preamble takes longer
than the tool itself (basically every JTS tool), make sure it
falls under one of the documented skip-cases — or extend the
skip-list rather than adding a contradicting absolute rule
elsewhere.

---

## Provider deltas

| | OpenAI gpt-realtime-2 | Gemini 3.1 Flash Live | Grok think-fast-1.0 |
|---|---|---|---|
| Skeleton | Opinionated 12-section template | Four-element checklist; no fixed structure | Nothing published |
| Conditional rules | Explicit: "remove always/never/only/must" | Forum evidence: 3.1 audio ignores conditionals 2.5 honored | Silent |
| Preambles | First-class, conditional triggers documented | Not modeled | Not modeled |
| Default verbosity | Lengthy unless constrained | **Terse by default** — explicit ask needed for warmth | Unclear |
| Reasoning knob | `reasoning.effort` low–xhigh; start `low` | `thinkingLevel` default `minimal` on Live | None exposed (background reasoning) |
| Tool-schema shape | Flat: `{type, name, description, parameters}` | OpenAPI inside `Tool(function_declarations=…)` | **Identical to OpenAI Realtime** |
| Session cap | 60 min hard | 15 min audio + 2 h resumption window | Not documented |
| Context window | 128K | Long sessions need `contextWindowCompressionConfig` | Not documented |
| Migration from older model | — | `thinkingBudget` → `thinkingLevel`; async tools unsupported; `send_client_content` for init only | — |

**Tool-schema serializers** are at
[jasper/tools/__init__.py:67-102](../jasper/tools/__init__.py). Because
Grok is OpenAI-Realtime-compatible by design, `openai_tools()`
serves both. Gemini gets its own shape via
`function_declarations()`. If you add a fourth provider, the
schema shape is the first thing to verify.

**Asymmetries worth knowing:**

- Gemini Live does NOT support: async/non-blocking function
  calling (regression from 2.5), proactive audio, affective
  dialogue.
- OpenAI Realtime does NOT support: streaming responses (per
  the model card).
- Grok Voice: NO session-cap, structural prompting guidance, or
  preamble model is documented — assume OpenAI-compat defaults.

Citations: OpenAI's
[realtime-models-prompting](https://developers.openai.com/api/docs/guides/realtime-models-prompting),
Gemini's
[3.1 Flash Live Preview docs](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview)
and [Live API best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices),
xAI's [Voice Agent guide](https://docs.x.ai/docs/guides/voice/agent).

---

## The current JTS SYSTEM_INSTRUCTION — walk-through

Lives at [voice_daemon.py:110-213](../jasper/voice_daemon.py) with
a rationale block at lines 74-109 explaining the design.
**Read the rationale block before editing** — it cites the OpenAI
guide, documents the previous-version failure mode (zero tool
calls across five scenarios with the negative-heavy prompt), and
records the Path B migration.

Eight labeled sections in order:

1. **Role & Objective** — Identity, user name, scope. Minimal,
   on-pattern.
2. **Personality & Tone** — Terse and factual; no follow-ups, no
   restating, one ambiguity question max.
3. **Verbosity** — Per-task length rules (1-2 sentences for
   direct answers; tool-result style deferred to each tool's
   description).
4. **Tools — when to call them** — POSITIVE framing. Only cross-
   tool routing rules where two similar tools need disambiguation
   live here (bare 'play' → resume vs. spotify_play; recency
   words → spotify_play_latest_by_artist; etc.). Per-tool "call
   for X" guidance lives in each tool's docstring.
5. **Tools — preambles** — CONDITIONAL skip-list. Mirrors
   OpenAI's documented pattern.
6. **Unclear audio** — Single clarification request, no tools,
   no reasoning. Per OpenAI's documented pattern.
7. **After a tool returns** — Cross-tool meta-rules only:
   `error` → speak verbatim; `confirm` → speak verbatim, no
   substitution. Per-tool voice-answer style lives in each
   tool's description.
8. **Out of scope** — sports/news/web. Minimal.

Dynamic content via `_build_system_instruction` (location,
linked Google accounts, transit-not-configured nudge,
ha-not-configured nudge) is appended at session-open time —
those are conditional on speaker configuration and don't live
in the static constant.

What's working:
- Section labels mirror OpenAI's template (Role / Personality /
  Verbosity / Tools / Preambles / Unclear audio / Tool results
  / Escalation-equivalent).
- Positive framing for tool calls; conditional framing for
  preambles.
- Rationale block makes the design legible to the next
  maintainer.
- ~100-line constant is comfortably below Gemini's ~500-token
  soft ceiling.

Gaps relative to OpenAI's 12-section template (still
nice-to-have):
- Reasoning section — gpt-realtime-2 supports reasoning levels;
  no posture hint today.
- Language section — we're English-only by deployment so
  omitted; revisit if multi-language ever ships.

---

## Tool-prompt cookbook

### How `build_tool()` works

[jasper/tools/__init__.py:126-149](../jasper/tools/__init__.py)
sends the full cleaned docstring to the LLM as the tool
description:

```python
def build_tool(fn, *, name=None):
    declared = name or getattr(fn, "__jasper_tool_name__", None) or fn.__name__
    desc = (inspect.getdoc(fn) or "").strip() or declared
    ...
```

**The full docstring is the LLM-facing surface.** Engineer-only
notes (dev TODOs, implementation details) belong in `#` comments
or the module docstring, NOT in tool function docstrings.

### Writing a new tool

Recommended structure for a tool docstring (all sent to the
LLM):

```
"""<One-sentence purpose>.

<When to call: 1-2 sentences with example utterances.>

Args:
  <param>: <semantics; what to pass for which utterances.>

Response shape:
  <Compact schema of the dict the tool returns.>

Voice answer style:
  <How to phrase the spoken answer. Examples + conditional rules
  ('when X is true, say Y'). The model treats this as load-
  bearing.>

<Cross-tool routing or constraint reminders ("Do NOT call as a
chaser after Y" / "Call fresh every time — data is live").>

<Error contract: "On error returns {error: ...}; speak the
error verbatim.">
"""
```

[home_assistant.py](../jasper/tools/home_assistant.py) is the
cleanest model — it has explicit positive triggers, a "Do NOT
call for" conditional list, response shape, voice-answer style,
and a "skip the preamble" hint, all in one docstring.

### Naming conventions

- Tool names: descriptive verbs, no spaces / periods / dashes.
  `get_citibike_status`, not `citibike-status` or
  `get.citibike.status`. (Gemini's function-calling docs reject
  the punctuated forms.)
- Parameter names: snake_case or camelCase, no spaces or
  special characters.
- Avoid voice-confusable names where possible (`get_qr_code` is
  fine; two tools named `read_status` and `get_status` is asking
  for trouble).

### Worked example: citibike

After Path B (2026-05-23):
- LLM-facing description: the full
  [citibike.py docstring](../jasper/tools/citibike.py) — one
  sentence of purpose + when-to-call + Args + Response shape +
  ZERO-COUNT / EBIKE_ONLY / STATUS / DOCKS / STALENESS /
  NO-MATCH rules verbatim.
- `SYSTEM_INSTRUCTION` no longer mentions citibike rules at
  all; it only carries the cross-tool meta-rules (`confirm` /
  `error` field handling).
- The pre-Path-B conflict ("running low on docks at 9 Av" in
  the unseen docstring vs. prohibition in `SYSTEM_INSTRUCTION`)
  is gone — one source of truth.

---

## Pitfalls + symptom catalog

| Symptom | Likely cause | Fix |
|---|---|---|
| Model answers from memory without calling a tool ("Jarvis tells me train times without ever calling the subway tool") | Negative framing in tool-call section ("Do not guess") | POSITIVE framing — "Call X when Y" |
| Model preambles every tool call ("Checking that…") despite system prompt | Absolute "never preamble" rule | Conditional framing — enumerate skip-cases per OpenAI's documented pattern |
| Gemini 3.1 ignores rules that 2.5 honored | 3.1 audio-mode conditional-rule degradation ([forum thread](https://discuss.ai.google.dev/t/gemini-3-1-flash-live-preview-not-following-system-instructions/144659)) | Document and live with it for now; A/B test absolute language for Gemini-only before adding a per-provider shim |
| Long Gemini sessions break on resumption | System instruction over ~500 tokens (PLAN.md tracker) | Shorten — move per-tool rules out of system prompt (blocked today by build_tool truncation) |
| Tool docstring "Voice answer style" sections seem ignored by the LLM (pre-2026-05-23) | `build_tool()` truncated to first paragraph | Lifted 2026-05-23; full docstring now sent. See cookbook. |
| Conditional rule violated in spoken response (e.g. ZERO-COUNT, STATUS, STALENESS) | Conflicting rule between SYSTEM_INSTRUCTION and tool docstring | After Path B, per-tool rules live ONLY in the tool docstring; system prompt has cross-tool meta-rules only |
| Model says preamble + tool result *and* also says result verbatim with no preamble (inconsistent across turns) | Conditional preamble rule too vague; missing "the tool call is lightweight" clause | Tighten the skip-list trigger; reference an existing precise version (current preamble block at voice_daemon.py:226-240) |
| Mic mishear gets confidently answered as if user said something else | No Unclear Audio rule | Add one — OpenAI's documented pattern: *"If the user's audio is not clear, ask once: 'Sorry, could you repeat that?'"* |

---

## Architectural alternatives considered

When two tools overlap enough that the LLM mis-routes between
them, the default fix is *disambiguation via prompt*. The
alternative — *consolidate into one tool with a discriminator
parameter* — is what every production voice assistant uses, and
is what Anthropic's published tool-design guide recommends. The
choices made in JTS are documented here so the trade-off is
visible to the next maintainer.

### Spotify: two tools vs one tool with a sort-order slot

**Status quo (chosen, 2026-05-23):** two tools, prompt
disambiguation + downstream validation.
- `spotify_play(query, kind, shuffle)` — catalog text search.
- `spotify_play_latest_by_artist(artist)` — `artist_albums` +
  sort by release date.
- Cross-tool routing rule in
  [SYSTEM_INSTRUCTION](../jasper/voice_daemon.py) enumerates
  recency triggers ("new", "newest", "latest", "just dropped",
  "just released") that route to the specialized tool.
- `spotify_play(kind='track')` applies a `fuzz.WRatio >= 75`
  threshold so a mis-route (e.g. the LLM passing "new song by
  X" to the generic tool anyway) degrades to refusal, not a
  wrong-song outcome.

**Road not taken:** consolidate to one tool with a sort slot.

```python
spotify_play(
    query: str,
    kind: str = "auto",
    sort_order: Literal["relevance", "recency"] = "relevance",
    shuffle: bool = False,
)
```

When `sort_order="recency"` and the query names an artist,
route internally to the `artist_albums` path. Otherwise, catalog
search.

**Why the slot-based design is the production-standard pattern:**

- Apple SiriKit `INPlayMediaIntent` has
  `INMediaSortOrder.newest` ([WWDC19 Session 207](https://asciiwwdc.com/2019/sessions/207),
  exact example used: *"Play the new Stuff You Should Know
  podcast"* → `INMediaSortOrder newest`).
- Alexa Music Skill API: single `GetPlayableContent` with
  `SortType=RECENCY` ([docs](https://developer.amazon.com/en-US/docs/alexa/music-skills/api-components-reference.html)).
- Google App Actions: single
  `actions.intent.PLAY_MEDIA` BII; temporal disambiguation
  falls through to search.
- Sonos Voice Control: single Play intent + slots
  ([Sonos tech blog](https://tech-blog.sonos.com/posts/on-device-voice-control-on-sonos-speakers/)).
- Home Assistant Music Assistant (LLM-piped): single
  `play_music` tool with free-text query, no recency slot
  ([MA voice script blueprint](https://github.com/music-assistant/voice-support/blob/main/llm-script-blueprint/llm_voice_script.yaml)).
- Anthropic's [Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents):
  *"Consolidate related operations into fewer tools… Fewer,
  more capable tools reduce selection ambiguity."*
- Classical NLU literature (joint intent detection + slot
  filling): "play X by Y" with a temporal modifier is the
  canonical example, and the modifier is always a *slot*, not
  a separate intent
  ([Louvan & Magnini, arXiv:2011.00564](https://arxiv.org/pdf/2011.00564)).

**Why we didn't go this way (yet):**

- Effort: ~2-3 hours refactor + new + migrated voice-eval
  scenarios (paid runs) + co-deploying the consolidated handler.
- The current two-tool design has a clean specialization
  boundary in the implementation — `artist_albums` paginated +
  date-sorted is meaningfully different from catalog `search`,
  so collapsing them at the API layer would mostly move the
  branch from "which tool" to "which sort_order".
- Disambiguation works in OpenAI's documented Realtime guidance
  (cell 73 of the [Realtime Prompting Guide](https://cookbook.openai.com/examples/realtime_prompting_guide)
  uses a near-identical pattern). Pre-PR-#230 the
  [`build_tool()` truncation](../jasper/tools/__init__.py:139)
  was actively defeating the rule's text — once that was fixed
  (Path B), the bug class may stop recurring on its own.
- The threshold gate (above) makes the failure mode soft: a
  misroute returns `_NOT_UNDERSTOOD` rather than playing the
  wrong song.

**When to revisit:**

- If the voice-eval regression scenario
  [`test_play_new_artist_song_routes_to_latest_by_artist`](../tests/voice_eval/regression/test_spotify.py)
  starts flaking across providers (this is the canary —
  consolidation eliminates the routing decision entirely).
- If a third recency-flavored variant is requested ("most
  popular X by Y", "the slow X by Y"). At that point a third
  tool is worse than a `sort_order` slot.
- If a new provider lands whose tool-selection scores
  significantly worse on the multi-tool benchmark (e.g. an
  open-source local model running on-device).

The same trade-off applies to any future overlap. Document the
choice when you make it.

---

## Recommended edits to current code

History of the audit's punch list. Items 1-4 landed in the same
PR as this doc (2026-05-23). Items 5-6 remain open.

### 1. Decide on build_tool() behavior — ✅ DONE (2026-05-23)

**Path B applied.** `build_tool()` at
[jasper/tools/__init__.py:139](../jasper/tools/__init__.py) now
sends the full cleaned docstring to the LLM:

```python
desc = (inspect.getdoc(fn) or "").strip() or declared
```

Per-tool conditional rules (when to call, voice-answer style,
response-shape handling) live in each tool's docstring under
`jasper/tools/`. Engineer-only notes belong in `#` comments or
the module docstring, not in tool function docstrings — the
[module docstring at tools/__init__.py](../jasper/tools/__init__.py)
codifies the convention.

### 2. Resolve the citibike "running low" conflict — ✅ DONE (2026-05-23)

Removed during the citibike docstring rewrite. The tool
docstring at [citibike.py](../jasper/tools/citibike.py) is now
the single source of truth and forbids "running low" / "low on
docks" / "almost full" / "tight" via the literal DOCKS RULE.

### 3. Add a Verbosity section to SYSTEM_INSTRUCTION — ✅ DONE (2026-05-23)

Added at [voice_daemon.py:126](../jasper/voice_daemon.py) per
OpenAI's documented per-task-type pattern:

> *"Direct answers: Use 1-2 short sentences. Clarifying
> questions: Ask one question at a time. Tool results:
> Summarize the result first, then give only the next useful
> action."*

JTS variant: direct answers in 1-2 short sentences; clarifying
questions one at a time; tool results defer to the tool's own
voice-answer style (which lives in the tool's docstring).

### 4. Add an Unclear Audio section — ✅ DONE (2026-05-23)

Added at [voice_daemon.py:186](../jasper/voice_daemon.py) per
OpenAI's documented pattern:

> *"If the user's audio is not clear, ask for clarification
> using a short English phrase such as 'Sorry, could you
> repeat that clearly?'"*
>
> *"Do not reason when the audio is unclear. Do not provide a
> preamble or call tools in the commentary channel when the
> audio is unclear."*

JTS variant: single clarification request, no tools, no
reasoning, then wait.

### 5. Optional: investigate Gemini-specific system instruction

The forum evidence that Gemini 3.1 audio ignores conditional
rules deserves an A/B test before we paper over with caveats.
If 3.1 needs absolute rules where OpenAI needs conditionals,
that's a real provider divergence — and would justify a future
per-provider `system_instruction` shim. For now: status quo
(single shared instruction), document the risk.

Workflow if pursued: pick one well-understood conditional rule
(e.g., a preamble skip-case), write the absolute-rule variant,
run a small voice-eval scenario on both providers, compare
adherence rates. ~$0.30 of OpenAI + ~$0.075 of Gemini per pass.

### 6. Add prompt-adherence voice-eval scenarios

Today voice-eval checks tool output (was the tool called, did
it return correct data) — not whether the model followed the
prompt's voice-style rules (ZERO-COUNT, STATUS, STALENESS,
preamble suppression). The latter is what regresses silently
when a prompt is edited carelessly.

Cost-discipline reminder: per the
[voice_eval README](../tests/voice_eval/README.md), real-time
sessions are paid. Don't add more than a handful of
prompt-adherence scenarios, and don't run them on every commit.

---

## References

All fetched 2026-05-23. Re-check on the next material prompt
edit (~quarterly, or whenever a model version bumps).

**OpenAI Realtime (gpt-realtime-2):**
- [Realtime Prompting Guide (Cookbook)](https://cookbook.openai.com/examples/realtime_prompting_guide)
  — section skeleton, preamble pattern, verbosity pattern
- [Using realtime models](https://developers.openai.com/api/docs/guides/realtime-models-prompting)
  — most prescriptive page; conditional-rule guidance,
  long-context Context block template
- [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)
  — 60-min session cap, conversation.item.truncate, VAD defaults
- [gpt-realtime-2 model card](https://developers.openai.com/api/docs/models/gpt-realtime-2)
  — 128K context, reasoning levels, function calling supported,
  streaming not supported
- [Community thread: Realtime API Preamble Inconsistent](https://community.openai.com/t/realtime-api-preamble-inconsistent/1361953)
  — real-world ~33% compliance with non-conditional preamble rules

**Gemini Live (gemini-3.1-flash-live-preview):**
- [3.1 Flash Live Preview model docs](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-live-preview)
  — includes 2.5 → 3.1 migration section
- [Live API best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices)
  — most actionable; per-tool conditional guidance, language pinning template
- [Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)
  — 15-min audio cap, 2-h resumption, manual VAD config
- [Gemini 3 developer guide](https://ai.google.dev/gemini-api/docs/gemini-3)
  — "Prompting Best Practices" section, terseness shift from 2.5
- [Function calling](https://ai.google.dev/gemini-api/docs/function-calling)
  — tool/parameter naming rules, JSON Schema shape
- [Prompting strategies](https://ai.google.dev/gemini-api/docs/prompting-strategies)
- [Forum: 3.1 Flash Live not following system instructions](https://discuss.ai.google.dev/t/gemini-3-1-flash-live-preview-not-following-system-instructions/144659)
  — counter-evidence to Google's adherence claims

**xAI Grok Voice (grok-voice-think-fast-1.0):**
- [Voice Agent API](https://docs.x.ai/docs/guides/voice/agent)
  — primary source; thin compared to OpenAI/Google
- [Function calling](https://docs.x.ai/docs/guides/function-calling)
  — tool schema, 200-tool-per-request limit
- [Models](https://docs.x.ai/docs/models) — pricing

**Empirical tool-selection research** — the published evaluation
literature on what works (and what doesn't) for tool selection
in LLMs. Use these to justify defense-in-depth beyond prompt
edits; cite when arguing for / against architectural changes.

- [Berkeley Function Calling Leaderboard (BFCL) — Patil et al., PMLR 2025](https://proceedings.mlr.press/v267/patil25a.html)
  — canonical multi-tool benchmark. `multiple` category (one
  correct tool + distractors) is the shape JTS hits. Provides
  per-model accuracy numbers; Gemini empirically among the
  weaker tool-selectors on adversarial sets.
- [MetaTool — Huang et al., ICLR 2024, arXiv:2310.03128](https://arxiv.org/abs/2310.03128)
  — when LLMs pick the wrong tool, ~50% land on the *most
  similar* wrong tool. Names our exact failure mode
  ("`spotify_play` wrongly chosen when
  `spotify_play_latest_by_artist` was correct").
- [Tool Preferences in Agentic LLMs are Unreliable — arXiv:2505.18135](https://arxiv.org/abs/2505.18135)
  — names three biases: *position bias* (earlier tools
  preferred), *description bias* (wording dominates capability),
  *edit-vs-edit bias* (trivial rewording flips preference).
  Mitigation suggestions: detailed balanced descriptions,
  consistent formatting, reorder-and-repeat.
- [BiasBusters — arXiv:2510.00307, 2025](https://arxiv.org/abs/2510.00307)
  — tests bias-mitigation across Claude/GPT/Gemini/DeepSeek.
  Finds "surface-level prompt modifications have limited
  effectiveness"; LoRA fine-tuning was needed for robust
  mitigation. **This is the reason we don't rely on prompt
  edits alone.**
- [Confetti / text-to-voice gap — arXiv:2605.15104](https://arxiv.org/html/2605.15104)
  — quantifies the modality penalty. Documents a 4.8-point
  regression for GPT-Realtime-1.5 vs its text sibling on the
  same tool-calling benchmark. Categorizes failures: argument
  errors 54-57%, decision errors 26-37%, tool-selection errors
  6-12%.
- [Daily.co voice-agent benchmarks](https://www.daily.co/blog/benchmarking-llms-for-voice-agent-use-cases/)
  — GPT Realtime 86.7% vs GPT-4.1 94.9% pass-rate (8.2-point
  gap on the same task in voice vs text). Direct evidence we
  operate in a harder regime than the provider docs assume.
- [Anthropic — Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
  — directly relevant prior-art for tool design even though we
  don't run Claude in production for this. Specifically:
  consolidate related operations; namespace by service;
  "Detailed descriptions are by far the most important factor
  in tool performance."
- [OpenAI cookbook — Realtime Prompting Guide, cell 72-73](https://cookbook.openai.com/examples/realtime_prompting_guide)
  — the documented `Use when: / Do NOT use when: / route to X
  instead` pattern for tool docstrings. The pattern JTS now
  uses in [home_assistant.py](../jasper/tools/home_assistant.py)
  and [spotify.py](../jasper/tools/spotify.py) (the
  spotify_play / spotify_play_latest_by_artist pair).

**Voice-assistant prior art** — how the production assistants
solve "play X by Y" with a temporal modifier. The "single intent
+ recency slot" pattern is the architectural alternative
documented above ("Architectural alternatives considered").

- [Alexa Music Skill API Components Reference](https://developer.amazon.com/en-US/docs/alexa/music-skills/api-components-reference.html)
  — `SortType=RECENCY` slot on a single `GetPlayableContent`
  intent.
- [Apple WWDC19 Session 207 — SiriKit Media Intents](https://asciiwwdc.com/2019/sessions/207)
  — `INMediaSortOrder.newest` on a single `INPlayMediaIntent`.
- [Sonos tech blog — on-device voice control](https://tech-blog.sonos.com/posts/on-device-voice-control-on-sonos-speakers/)
  — joint intent + slot extraction.
- [Music Assistant voice script blueprint](https://github.com/music-assistant/voice-support/blob/main/llm-script-blueprint/llm_voice_script.yaml)
  — LLM-piped: one `play_music` tool, free-text query.
- [Louvan & Magnini — Slot Filling and Intent Classification Survey, arXiv:2011.00564](https://arxiv.org/pdf/2011.00564)
  — pre-LLM NLU literature; "play X by Y" is the canonical
  joint-intent-and-slot-filling example.

---

## See also

- [voice_daemon.py:74-104](../jasper/voice_daemon.py) — inline rationale
  block; the design decisions that motivated the current
  SYSTEM_INSTRUCTION shape. Read before editing the constant.
- [HANDOFF-voice-providers.md](HANDOFF-voice-providers.md) —
  multi-provider architecture (LiveConnection / LiveTurn,
  schema serializers, adding a fourth provider)
- [HANDOFF-persistent-live-session.md](HANDOFF-persistent-live-session.md)
  — Gemini-specific session management, manual VAD rationale
- [audit-pending-followups.md](audit-pending-followups.md):173
  — idle-anchor / tool-round watchdog contract
  (`_note_activity()` must fire on every tool-round server
  event; load-bearing for any new tool)
- [CLAUDE.md](../CLAUDE.md) "Voice system prompt" section —
  the short pointer that lives alongside daily-driver rules
- [PLAN.md](../PLAN.md) "Risks worth re-flagging" —
  ~500-token Gemini ceiling, sequential tool calls
