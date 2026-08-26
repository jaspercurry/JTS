# ADR-0158: Per-provider prompt divergence is a shared base plus an additive delta, never separate prompts

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

JTS runs one voice loop against three realtime providers. The single
`SYSTEM_INSTRUCTION` is OpenAI-shaped — labeled sections, explicit
conditional rules — because OpenAI publishes the most prescriptive guidance
and produces the cleanest signal when a rule is violated.

That shape is a mismatch for Gemini in two documented ways: Gemini 3 prefers
terse direct prompts and "may over-analyze verbose or complex prompt
engineering techniques from older versions", and its audio mode has been
observed reading prompt structure aloud. Grok needs nothing, being
OpenAI-Realtime-compatible by design.

The obvious fix — one prompt file per provider — was rejected. Three
independently-edited prompts drift, and the divergence is small and
concentrated on one provider.

## Decision

**One shared base plus a thin, additive per-provider delta.**
`_build_system_instruction(…, provider=…)` appends the entry for the active
provider from `_PROVIDER_AUGMENTATION`; the daemon passes the configured
provider and the voice-eval harness mirrors it, so an eval exercises the real
delta.

Only `gemini` has a delta today. OpenAI, Grok, and any unset or unknown value
get nothing, so their effective prompt stays **byte-identical to the shared
base**.

A delta must be additive. A delta that removes base rules, touches tool-call
framing, or imposes a hard length cap is a behavioural change of a different
kind — it is the zero-tool-calls and truncation regression path.

## Consequences

- Tuning Gemini can never silently regress the live provider: the
  byte-identical property is pinned by a test, and OpenAI/Grok need no
  re-validation when a delta changes.
- Changing a delta's *content* is still a behavioural change and needs a
  per-provider voice-eval pass — cheap on Gemini, which is where the
  divergence lives.
- The mechanism makes the open Gemini experiment (A/B an absolute-phrasing
  variant of one well-understood conditional rule) a data-only edit to
  `_PROVIDER_AUGMENTATION`.
- Deliberately given up: the ability to give a provider a genuinely different
  prompt shape. If one ever needs that, it supersedes this ADR rather than
  growing the delta into a second prompt by accretion.
