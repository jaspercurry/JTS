# ADR-0155: Per-tool conditional rules live in the tool's model-facing description, never in the system prompt

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

JTS's realtime voice loop has two places a behavioural rule can be written:
the single `SYSTEM_INSTRUCTION` shared by every turn, and each tool's own
description, which the provider serializers ship alongside the schema.

Before May 2026 both were used. `SYSTEM_INSTRUCTION` carried per-tool rules
(how to phrase a Citi Bike dock count, when not to chase a Spotify play with
a now-playing call) while the tool docstrings carried their own versions of
the same rules. Two consequences followed. The rules drifted into direct
contradiction — the docstring said "running low on docks" was a fine phrasing
while the system prompt forbade it — and the docstring half was not even
reaching the model, because `build_tool()` truncated a decorated function's
docstring to its first paragraph.

Both provider vendors document the split explicitly: OpenAI's realtime
prompting guidance puts cross-tool meta-rules in the system prompt and
per-tool conditionals in the tool, and Gemini's Live best-practices page says
to tell the model *inside the tool definition* under what conditions the tool
should be invoked. A public OpenAI community thread measured only ~33%
compliance for a non-conditional rule stated in the system prompt; moving it
into the tool description was the fix that worked.

## Decision

**A rule that concerns one tool lives in that tool's model-facing
description. `SYSTEM_INSTRUCTION` carries only cross-tool meta-rules** —
role, persona, verbosity, preamble policy, unclear-audio handling, the
`error` / `confirm` / `needs_confirmation` result contracts, the
untrusted-fence rule, and the small set of routing rules that disambiguate
two similar tools.

The model-facing description resolves in one order —
user override → `llm_description` → `ToolDefinition.description` — and
`build_tool()` sends the full cleaned docstring, not the first paragraph.

## Consequences

- One source of truth per rule. A tool's when-to-call guidance, response
  shape, and voice-answer style are read and edited in one file.
- The system prompt stops growing with the tool count. Adding a tool costs
  nothing in the shared constant.
- Compliance improves where it was measurably bad: a per-tool
  "skip the preamble" sentence fixed the talks-twice defect on the Spotify
  tools first try, where escalating the system-prompt rule had not.
- The cost is a token budget spent per tool rather than once. Providers cap
  the combined instructions+tools payload, which is why a tool may split a
  rich maintainer docstring from a shorter `llm_description`; the docstring
  stays the human source of truth.
- Deliberately given up: the ability to state a per-tool rule in one central
  place a reader can skim. The compensating rule is that engineer-only notes
  never go in model-facing text — they belong in `#` comments or the module
  docstring.
- Rejected: keeping per-tool rules in the system prompt and fixing only the
  truncation bug. That preserves the drift hazard, since the same rule would
  still have two homes.
