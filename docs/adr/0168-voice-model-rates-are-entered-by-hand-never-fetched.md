# ADR-0168: Voice-model rates are entered by hand, never fetched

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-pricing-editor.md was trimmed to
  its operational spine; the survey behind it dates to 2026-05-30)

## Context

JTS estimates spend from a per-model rate card, and the speaker already knows
which models it can run — it discovers them from each provider's models
endpoint. The obvious next step is to discover the *prices* the same way and
never ask the household to type a number.

The 2026-05-30 survey of the three voice providers found that step does not
exist. Two of them expose no pricing on their models endpoints at all — one
has no pricing API of any kind, the other returns capability metadata only.
The third does return per-token prices, but only for its text and image
models: its voice models run on a separate realtime stack, bill per minute,
and are excluded from that response. Third-party price datasets (LiteLLM,
OpenRouter, Artificial Analysis) lag new launches by weeks and carry no
coverage of that provider's voice models at all.

So for exactly the models JTS runs, machine-readable prices do not exist.

## Decision

**JTS does not fetch prices. Rates ship as a dated, version-controlled
default file and are corrected by hand through the `/voice` editor.** The
only automation is a copyable research prompt, pre-filled with this speaker's
exact current model IDs and the official pricing-page URLs, whose JSON answer
the wizard imports and validates through the same sanitizer the override
loader uses.

A "suggest rates from a third-party dataset (best-effort, verify)" button is
out of scope unless the owner asks for it.

## Consequences

- **No integration to rot.** There is no price scraper to break when a
  pricing page is restyled, and no third-party dataset dependency that can go
  stale or unmaintained without anyone noticing.
- **Rates can be wrong, visibly.** A hand-entered card drifts when a provider
  changes prices. That is why the bundled file carries `as_of`, why the
  override may carry its own, and why both are shown in the editor: staleness
  is surfaced rather than prevented.
- **Refreshing is cheap enough to actually do.** The generated prompt names
  the speaker's real models, so refreshing rates is paste-in, paste-back,
  save — not a research project. Sparse overrides keep the resulting file
  small and readable.
- **Newly discovered models are handled by the same path.** A model the
  bundled file has never heard of is unpriced, costs zero, and says so loudly
  (ADR-0161); pricing it is one editor row, not a code change.
- **The rejected alternative stays rejected on evidence, not taste.** If a
  provider ever publishes voice-model prices on a stable endpoint, that is new
  evidence and warrants a superseding ADR — not a quiet exception.
