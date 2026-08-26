# ADR-0156: The ~500-token Gemini system-instruction ceiling is folklore — trim for adherence and cost, never for resumption

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

A line entered the v1 master plan on 2026-05-03 and was carried forward
verbatim for months: *"Long Gemini system prompt breaks session resumption on
the 3.1 Flash Live preview. Keep system instruction under ~500 tokens."*
JTS's `SYSTEM_INSTRUCTION` measures ~997 words ≈ ~1,400–1,600 tokens, so the
claim, if true, meant the shipped prompt was more than 2× over a hard limit
and every prompt edit was fighting an invisible ceiling.

A 2026-06-15 re-check, including a sweep of community reports, found no basis
for it:

- The likely seed is a single Google AI dev-forum post claiming resumption
  stops working at "about **200** tokens" of system instruction — no repro,
  no error code, no corroboration, still in triage months later. The "500"
  appears to be a conflation with the documented ~300–500-token *per-turn
  overhead*, which is a billing artifact.
- Neither Google's Live API capabilities page nor its session-management
  guide documents any system-instruction size limit, or any link between
  instruction length and resumption.
- The resumption failures that *are* documented have other causes: prior
  audio+video session state (JTS is audio-only), tool-call races, generic
  1011 closes reported as uncorrelated with prompts, and handle expiry.
  JTS's own catalogue of observed resumption failures names the same modes
  and never size.
- Production contradicts the claim directly: the deployment runs at ~2× the
  figure with session resumption listed as a working capability.

The sweep did surface two real, sourced reasons the prompt is above norm —
neither of them resumption. Gemini 3 "may over-analyze verbose or complex
prompt engineering techniques from older versions", and real-world Live
system instructions are short (the official cookbook quickstart sets none;
LiveKit's example is one line). Separately, the Live API re-bills the whole
context window every turn, so a verbose instruction inflates per-turn cost as
a session grows.

## Decision

**The ~500-token figure is not a ceiling and is not a reason to trim.** Do
not shorten `SYSTEM_INSTRUCTION` to chase session resumption.

Trimming is justified by instruction-following adherence or per-turn cost.
Because trimming risks the documented zero-tool-calls regression, any trim is
a behavioural change that needs a paid voice-eval pass first.

For the resumption question specifically, voice-eval is the wrong
instrument: confirm from production reconnect logs on a Gemini-active
speaker, or A/B the full versus trimmed prompt across a reconnect and watch
whether the handle still restores context.

## Consequences

- Prompt edits are no longer sized against a number nobody can reproduce.
  The real budget conversation is adherence and per-turn cost, both of which
  have sources.
- A resumption failure gets diagnosed from logs rather than attributed to
  prompt length by reflex.
- Deliberately given up: the safety of an unverified conservative bound. If a
  real size-linked resumption failure ever appears, it supersedes this ADR
  with evidence rather than folklore.
- The general rule this instantiates: an inherited constraint with no repro,
  no vendor documentation, and production evidence against it is folklore.
  Name it in the doc that carries it rather than quietly honouring it.
