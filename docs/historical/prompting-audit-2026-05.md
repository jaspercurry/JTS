# Voice prompting audit (2026-05 → 2026-06) — historical

> **Status: historical.** The record of the prompt audit that produced the
> current prompting posture: the punch list and what landed from it, the
> evidence behind the ~500-token investigation, and the worked example that
> demonstrated the docstring split. Every measurement is from its stated
> date. Current operational truth is
> [HANDOFF-prompting.md](../HANDOFF-prompting.md); the decisions are
> [ADR-0155](../adr/0155-per-tool-conditional-rules-live-in-the-tool-description.md)
> ·
> [ADR-0156](../adr/0156-the-gemini-system-instruction-token-ceiling-is-folklore.md)
> ·
> [ADR-0157](../adr/0157-untrusted-tool-text-is-fenced-and-consequential-actions-are-confirmed.md)
> ·
> [ADR-0158](../adr/0158-per-provider-prompt-divergence-is-a-shared-base-plus-an-additive-delta.md).

## The punch list and what landed

The audit's six recommendations, in the order they were written.

**1. Decide on `build_tool()` behavior — done 2026-05-23 ("Path B").**
`build_tool()` began capturing the full cleaned decorated-function docstring
as the rich `ToolDefinition.description`, instead of truncating it to the
first paragraph. The provider-visible default became `llm_description` when
present, else that rich description. Per-tool conditional rules moved out of
`SYSTEM_INSTRUCTION` and into whichever text the model actually sees.

**2. Resolve the citibike "running low" conflict — done 2026-05-23.**
Removed during the docstring rewrite. Before Path B the unseen docstring
suggested "running low on docks at 9 Av" while `SYSTEM_INSTRUCTION`
prohibited exactly that phrasing; the tool now owns the rule via a literal
DOCKS RULE forbidding "running low" / "low on docks" / "almost full" /
"tight".

**3. Add a Verbosity section — done 2026-05-23.** Per OpenAI's documented
per-task-type pattern. JTS variant: direct answers in 1-2 short sentences;
clarifying questions one at a time; tool results defer to the tool's own
voice-answer style.

**4. Add an Unclear Audio section — done 2026-05-23.** Single clarification
request, no tools, no reasoning, then wait. The fragment and
empty-string-argument triggers were added 2026-05-24 after the VAD test
matrix surfaced the failure mode they exist for: on empty or one-word
transcripts ("What?", "That's…", "") the model still confidently called
tools, including one `home_assistant` call that actually turned on the
bedroom lights while the user was asking about weather.

**5. Per-provider system-instruction shim — mechanism landed 2026-06-15.**
Shared base plus per-provider delta (ADR-0158). The remaining eval-gated
work: tune the Gemini delta's *content* — pick one well-understood
conditional rule, write the absolute-rule Gemini variant, run a small Gemini
voice-eval pass, compare adherence.

**6. Prompt-adherence voice-eval scenarios — still open.** Carried forward as
the open follow-up in the live doc.

## The zero-tool-calls regression (2026-05-21)

The prompt version before the audit had ~15 "Do NOT" clauses and zero
positive "Call the tool when…" instructions. Verified by voice-eval: it
produced **zero tool calls across five consecutive read-only scenarios** on
gpt-realtime-2. Restructuring to OpenAI's positive-framing pattern fixed it.
This is the regression that any aggressive trim of `SYSTEM_INSTRUCTION` risks
re-introducing, and the reason a trim is a paid-eval-gated change.

## The ~500-token figure: evidence trail

Re-checked 2026-06-15 including a web sweep. Verdict and rule in ADR-0156;
the sources are kept here.

*Where the number came from.* The likely seed is a single
[Google AI dev-forum post (2026-03-28)](https://discuss.ai.google.dev/t/session-resumption-for-gemini-3-1-flash-live-preview-does-not-seem-to-work-with-long-systeminstructions/136654)
claiming resumption "stops working with a systemInstruction of about **200
tokens**" on this exact model — no repro, no error code, no corroborating
report, and a moderator still asking for a repro months later. Note it says
~200, not ~500; the "500" most likely got conflated with the documented
~300–500-token *per-turn overhead*
([python-genai #1917](https://github.com/googleapis/python-genai/issues/1917):
`promptTokenCount` ≈ 334 for "hello"), a billing artifact.

*What weighs against a size→resumption link.*

- No documentary basis. Neither
  [Live API capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities)
  nor the
  [session-management guide](https://ai.google.dev/gemini-api/docs/live-session)
  documents any system-instruction token limit. The only documented
  resumption fact is the 2-hour handle validity; the context window is 128k
  (native-audio) / 32k (others), so a ~1,000-token instruction is ~1% of
  budget.
- The documented resumption breakers are not size: prior **audio+video**
  session state ([python-genai #2290](https://github.com/googleapis/python-genai/issues/2290)
  — JTS is audio-only), tool-call races (1008), generic 1011 reported as
  *uncorrelated* with prompts, and handle expiry or non-emission.
- Production ran well over 2× the figure with session resumption working.

*What the sweep did establish,* both real and neither about resumption:
instruction-following dilution (Gemini 3 "may over-analyze verbose or complex
prompt engineering techniques from older versions" —
[Gemini 3 guide](https://ai.google.dev/gemini-api/docs/gemini-3) — while
real-world Live system instructions are short: the official cookbook
quickstart sets none, LiveKit's example is one line, Pipecat's is ~3
sentences), and cost (the Live API re-bills the whole context window every
turn —
[Live API best practices](https://ai.google.dev/gemini-api/docs/live-api/best-practices)).
Latency is a weaker third signal: cold-start time "scales with system
instruction size"
([cookbook #1197](https://github.com/google-gemini/cookbook/issues/1197)),
though that report is at 14K–33K chars, far above the JTS prompt.

*Gemini conditional-rule caveat.* A developer forum thread
([3.1 Flash Live Preview not following system instructions](https://discuss.ai.google.dev/t/gemini-3-1-flash-live-preview-not-following-system-instructions/144659))
reports 3.1 audio mode ignoring conditional system instructions that 2.5
honored, with no Google response. Google's public blog claims the opposite
("adherence to complex system instructions has been boosted significantly").
The counter-evidence is why the Gemini delta exists as an A/B lever rather
than an assumption.

*Preamble compliance.* A community thread
([Realtime API Preamble Inconsistent](https://community.openai.com/t/realtime-api-preamble-inconsistent/1361953))
documents ~33% compliance for non-conditional preamble rules on
gpt-realtime; the fix that worked was moving the rule into a per-tool
description.

## Worked example: citibike after the split (2026-06-18)

The Phase 1.6 `llm_description` pass is the reference for how a tool splits
human and model-facing text.

- Human source of truth: the full `citibike.py` docstring — purpose,
  when-to-call guidance, Args, response shape, and the detailed ZERO-COUNT /
  EBIKE_ONLY / STATUS / DOCKS / STALENESS / NO-MATCH maintainer notes.
- LLM-facing: the shorter `GET_CITIBIKE_STATUS_LLM_DESCRIPTION`, which
  preserves only the load-bearing model rules — call fresh, pass
  `station_label`, answer "zero" not "no", respect `ebike_only`, surface
  `offline` / `missing_bike_data` / `no_match` / `error`, mention docks only
  when asked.
- `SYSTEM_INSTRUCTION` no longer mentions citibike at all.

Token budget at the time of the pass: the 29 shipped descriptions measured
~8.5k tokens before the first trimming pass and ~3.9k after, against
OpenAI Realtime's 16,384-token instructions+tools cap.

## Background

Prior OSS reviews flagged tool-result injection as an unexamined "Phase 2"
gap — no fencing, no confirmation, no test. The two-layer defense landed
2026-06-15.
