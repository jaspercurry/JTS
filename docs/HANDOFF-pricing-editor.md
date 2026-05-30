# Handoff: Per-model pricing editor (`/voice`)

> **Status: Phases 1 & 2 implemented (2026-05-30). Phase 3 deferred.**
> Pricing is model-ID-keyed with dated defaults in
> `jasper/data/model_pricing.json`; the `/voice` page has a per-model
> "Pricing rates" editor writing `/var/lib/jasper/pricing.json`. The
> broader spend/usage accounting truth (how cost is computed, the
> `Pricing` rate card, the spend cap) lives in
> [HANDOFF-voice-providers.md](HANDOFF-voice-providers.md). Phase 3 (the
> copy-paste research-prompt generator) is sketched at the bottom and not
> built. Snapshot date: 2026-05-30.

## Goal

Let a household surface and edit the per-model cost rates JTS uses to
estimate spend, from a collapsible section on `http://jts.local/voice/`.
Two phases:

1. **Phase 1 — make pricing model-ID-keyed** (today it's keyed by
   provider). Foundation for everything else.
2. **Phase 2 — the `/voice` editor section** that reads/writes those
   per-model rates to `/var/lib/jasper/pricing.json`.

A later **Phase 3** (deferred) generates a copy-paste "research these
exact models' current prices and emit this JSON" prompt. Spec sketched
at the bottom; do not build yet.

## Why model-keyed, not provider-keyed

The override is currently keyed `gemini / openai / openai_mini / grok`
(see `_OVERRIDE_KEYS` in [`jasper/usage.py`](../jasper/usage.py)). That
collapses models that genuinely differ: **`gpt-realtime-2` and
`gpt-realtime-1.5` share one rate card today even though their text-output
price differs ($24 vs $16 / 1M).** As providers ship more tiers (and a
future "Realtime 3"), provider-keying drifts further from reality.
Model-ID keying fixes the `-2`/`-1.5` gap, future-proofs for
newly-released models, and lines each editable row up 1:1 with the model
list we already discover.

## What we build on (already shipped)

- **Live model discovery** — [`jasper/voice/model_discovery.py`](../jasper/voice/model_discovery.py)
  (`fetch_provider_model_ids`, `refresh_provider_cache`, `load_cache`)
  fetches the current voice/live/realtime model IDs per provider and
  caches to `/var/lib/jasper/voice_model_discovery.json`. Already wired
  into `/voice` via the "Refresh available models" button
  (`POST /refresh-models`). **This is the model list the editor and the
  Phase-3 prompt enumerate** — no new fetching needed.
- **Curated catalog** — [`jasper/voice/catalog.py`](../jasper/voice/catalog.py)
  (`PROVIDERS`, `ModelOption`) is the hand-maintained per-provider model
  list. The editor's model rows = `catalog models ∪ discovered models`.
- **Override file + loader** — `Pricing`, `load_pricing_overrides`,
  `pricing_for_provider`, `DEFAULT_PRICING_FILE` (`/var/lib/jasper/pricing.json`)
  in [`jasper/usage.py`](../jasper/usage.py). Fail-soft today
  (missing/malformed → built-in defaults; non-numeric/bool ignored).
- **The wizard** — [`jasper/web/voice_setup.py`](../jasper/web/voice_setup.py)
  renders per-provider `<details class="account">` cards and writes
  config on save + restarts `jasper-voice`. Uses the legacy `wrap_page`
  path with `_VOICE_PAGE_STYLE` (the `details.disclosure` collapsible CSS
  is already available).

## Why we are NOT auto-fetching prices from the APIs

Researched 2026-05-30 against official docs. Verdict for the three voice
providers:

- **OpenAI** `/v1/models`: no pricing field; no pricing API at all.
- **Gemini** `models.list`: capability metadata only, no pricing.
- **xAI** `/v1/models` & `/v1/language-models`: *do* return per-token
  pricing (`prompt_text_token_price`, … in USD cents/100M tokens) — but
  **only for text/image models; `grok-voice-*` is excluded** (separate
  realtime WS stack, per-minute billing).

So voice-model prices are not machine-fetchable from official sources.
Third-party datasets (LiteLLM `model_prices_and_context_window.json`,
OpenRouter, Artificial Analysis) lag new launches and have **no
`grok-voice-*` coverage**. → **Do not build API price-fetching.** Manual
entry (Phase 2) + the optional research-prompt convenience (Phase 3) is
the right shape. A "suggest from LiteLLM (best-effort, verify)" button is
explicitly out of scope unless asked.

---

## Phase 1 — model-ID-keyed pricing, defaults in a dated repo JSON

Default rates move OUT of Python constants and INTO a version-controlled,
date-stamped JSON shipped with the package. Pricing is **per model ID,
full stop** — there is no provider-level price anywhere ("a single price
for a whole provider" isn't a real thing, so we never fabricate one).
`pricing_for_provider`, the `*_PRICING` constants, and the idea of a
provider fallback are **removed, not shimmed** (no vestigial code).

### Bundled default pricing: `jasper/data/model_pricing.json` (NEW)

Version-controlled package data, sitting alongside
[`jasper/data/mta_stations.csv`](../jasper/data/mta_stations.csv).
`install.sh` already copies `jasper/` into `/opt/jasper`, so **no install
step**. Carries an `as_of` date so the UI can show how fresh the bundled
rates are; the headline data point is `-1.5` text-out **16** ≠ `-2`'s 24.

```json
{
  "as_of": "2026-05-30",
  "source": "provider public pricing pages",
  "models": {
    "gpt-realtime-2":    {"audio_input_per_million_usd": 32, "audio_output_per_million_usd": 64,
                          "text_input_per_million_usd": 4, "text_output_per_million_usd": 24,
                          "cached_input_per_million_usd": 0.40},
    "gpt-realtime-1.5":  {"audio_input_per_million_usd": 32, "audio_output_per_million_usd": 64,
                          "text_input_per_million_usd": 4, "text_output_per_million_usd": 16,
                          "cached_input_per_million_usd": 0.40},
    "gpt-realtime-mini": {"audio_input_per_million_usd": 10, "audio_output_per_million_usd": 20,
                          "text_input_per_million_usd": 0.60, "text_output_per_million_usd": 2.40,
                          "cached_input_per_million_usd": 0.30},
    "gemini-3.1-flash-live-preview":                 {"audio_input_per_million_usd": 3, "audio_output_per_million_usd": 12},
    "gemini-2.5-flash-native-audio-preview-12-2025": {"audio_input_per_million_usd": 3, "audio_output_per_million_usd": 12},
    "grok-voice-think-fast-1.0":                     {"flat_per_hour_usd": 3.0}
  }
}
```
Omitted buckets default to 0 via the `Pricing` dataclass (Gemini omits
text/cached — the Live API can't split them; Grok is flat-only). `mini`
cached-text is really $0.06 but the single `cached_input` bucket keeps the
conservative $0.30. The `Pricing` dataclass itself is unchanged.

### Lookup (replaces `pricing_for_provider`)

- `load_default_pricing()` reads + validates the bundled JSON once →
  `(dict[model_id, Pricing], as_of: str)`. Bundled data is absent only on
  a packaging bug; treat unreadable/corrupt as log-ERROR + empty map
  (every model then "unpriced" + surfaced — never crash the daemon).
- `pricing_for_model(model_id, *, overrides=None) -> Pricing`:
  1. `base = defaults.get(model_id)` — if absent → `Pricing(label="unpriced:"+model_id)` (all-zero rates).
  2. if `overrides` has `model_id` → `replace(base, **overrides[model_id])`.
  - **No provider argument and no provider fallback.** Model IDs are
    globally unique across providers, so the provider isn't needed to price.
- Delete `pricing_for_provider`, the `GEMINI_PRICING`/`OPENAI_*`/`GROK_*`
  constants, `_OVERRIDE_KEYS`, and the `"mini" in model` substring hack.

### Override loader (small change)

- `load_pricing_overrides` keeps reading `/var/lib/jasper/pricing.json`,
  now keyed by **model ID** (any string key; `_OVERRIDABLE_FIELDS`
  numeric/non-bool validation stays). May carry an optional top-level
  `as_of` (written when the user refreshes via Phase 3) → UI shows "your
  rates, entered <date>".
- `pricing.json` schema (sparse — overrides only):
  ```json
  {"as_of": "2026-08-01",
   "models": {"gpt-realtime-2": {"text_output_per_million_usd": 28.0}}}
  ```
- **No migration.** A stale provider-keyed file harmlessly no-ops
  (provider strings aren't model IDs → ignored → bundled defaults apply).

### Unknown / unpriced model — the one consequence of dropping the fallback

When the active model is in neither the bundled JSON nor the override (a
bleeding-edge discovered model, or a hand-set custom ID) we genuinely have
no price. Behaviour: estimate **$0** and **surface loudly** — a startup
`logger.warning("event=pricing.unpriced model=<id> ...")`, the `/voice`
editor renders the model with empty "needs pricing" fields, and `/system`
can flag it. We do **not** invent a rate.
- **RESOLVED (maintainer, 2026-05-30):** don't fabricate a value where
  there isn't one. Unpriced → cost is **null/zero, not estimated**, and we
  **scream** (loud warning + UI flag). We explicitly do NOT use a
  "most-expensive-known-model" ceiling or any other invented number —
  "why give a value if there isn't a value." Consequence accepted: the
  spend cap can't bound an unpriced model until it's priced (an edge case;
  the bundled JSON ships every current model, and the editor + refresh
  prompt make pricing a new one a ~30-second fix). The loud surfacing is
  what keeps this from being a silent $0.

### Daemon wiring

[`jasper/voice_daemon.py`](../jasper/voice_daemon.py) `run()`:
`pricing = pricing_for_model(_active_model(cfg), overrides=load_pricing_overrides())`.
Log the resolved label (incl. `unpriced:`/custom). `ConnectionUptimeMeter`
wiring is unchanged (keys off `pricing.flat_per_hour_usd > 0`).

### Tests (`tests/test_usage.py`)
- bundled JSON parses; every entry has valid buckets; `as_of` present.
- `pricing_for_model` per model; **`-1.5` text-out 16 ≠ `-2` 24** (the
  regression that motivated model keying).
- override keyed by model ID overlays the bundled default.
- unknown model ID → all-zero `Pricing(label="unpriced:…")` (+ warning path).
- `mini` resolves from its own JSON entry (no substring hack).
- existing `estimate_cost` modality math unchanged (`Pricing` didn't move).

---

## Phase 2 — the `/voice` editor section

All in [`jasper/web/voice_setup.py`](../jasper/web/voice_setup.py) +
small helper in [`jasper/web/_common.py`](../jasper/web/_common.py).

### Placement & layout
- Inside `_provider_card_html`, add a
  `<details class="disclosure"><summary>Pricing rates</summary>…</details>`
  **after** the provider extras, **before** the clear-credentials form.
- Inside it, one sub-group **per model** (`catalog ∪ discovered` for that
  provider). Each sub-group shows only the buckets that provider's cost
  model uses: Gemini → `audio_in`, `audio_out`; OpenAI → all 5; Grok →
  `flat_per_hour`. Inputs are `<input type="number" step="0.01" min="0">`.
- Field name convention: `price__<model_id>__<bucket_field>` (escape the
  model ID for the attribute; it's provider-supplied → untrusted, per the
  web-wizard escaping rule).

### Default-vs-custom tagging (what "reset / tagging" means)
Plainly: when you open the section, each field is pre-filled with the
rate JTS is *actually using* for that model — which is either **our
built-in default** or **a value you previously saved**. Without a marker
you can't tell which. So:
- Pre-fill each input with the **effective** value
  (`pricing_for_model(model_id, overrides=load_pricing_overrides())`).
- Render the bundled default as the input's **placeholder / helper text**
  ("default $32.00") and a small inline **`custom`** chip when the
  effective value came from `pricing.json` (i.e. differs from the default).
- **Reset = clear the field.** Save writes a **sparse override**: only
  values that *differ* from the bundled default are written to
  `pricing.json`; a blank/at-default field is omitted → daemon falls back
  to the bundled default. So "reset to default" is just blanking the box (a
  tiny "↺ default" link that blanks it is the only JS needed). This keeps
  `pricing.json` minimal and makes the file self-explanatory.
- **Show freshness.** Near the section header, surface the bundled
  `as_of` date from `model_pricing.json` ("Bundled rates as of 2026-05-30
  — refresh via the prompt below" once Phase 3 lands). If the override
  carries its own `as_of`, show that for the customized models. This is
  the "little text thing that says as of whatever" — staleness made visible.

This is the minimal version. Fancier per-field reset animations etc. are
not in scope.

### Save path
- New route **`POST /pricing`** (decoupled from `/save` so pricing edits
  don't require touching keys/model/voice). CSRF: `verify_csrf` before
  any work, `reject_csrf` on failure (per `_common`).
- Pure builder `_apply_pricing_save(form, overrides_defaults) -> dict`:
  parse `price__*` fields, coerce to float, clamp `>= 0`, **keep only
  values differing from the built-in default**, group into the model-ID
  schema. Reject/skip non-numeric (mirror `load_pricing_overrides`
  leniency). Returns the dict to write (possibly `{}` → write an empty
  object or delete the file).
- Add **`write_json_file(path, obj, *, mode=0o644)`** to `_common.py`
  (atomic temp-file + rename, mirroring `write_env_file`). `pricing.json`
  holds **no secrets** → 0644 is fine (only `jasper-voice` reads it).
- After write, `restart_voice_daemon()` (same as `/save`) so the daemon
  reloads overrides at startup. Redirect via `send_see_other("./",
  flash="Pricing saved. Voice daemon restarting.")`.

### Notes / gotchas
- **Edits affect future sessions only.** Stored `cost_usd` rows keep the
  rate active when they were computed — historical cost does not
  retroactively change (correct). Say so in the flash/help text.
- The section is read-only-safe to render even with no key set (pricing
  isn't a secret) — but gate it behind the same card the model picker
  uses for layout consistency.
- Keep `voice_setup.py` on its existing `wrap_page`/`_VOICE_PAGE_STYLE`
  path; do **not** half-migrate it to `canonical_page` here.

### Tests
- `tests/test_voice_setup.py`: `_apply_pricing_save` builds the right
  sparse model-ID dict; at-default fields are omitted; blanks reset;
  negatives clamped; round-trips through `load_pricing_overrides` +
  `pricing_for_model` to the expected effective rate.
- `tests/test_web_wizard_conventions.py` (static) must still pass — the
  new form uses `csrf_field_html`, escapes the untrusted model IDs, no
  inline JS with untrusted strings.
- No paid voice-eval needed (pure config surface).

---

## Phase 3 — research-prompt generator (DEFERRED, sketch only)

When revisited: a "Generate pricing-research prompt" button emits a
copyable block pre-filled with the **exact model IDs** (catalog ∪
discovery) + the `pricing.json` JSON schema, instructing the user's
chatbot to look up current official prices and emit JSON in that schema.
A textarea takes the result → same `_apply_pricing_save`/validate/write
path as Phase 2. It reuses everything Phase 1+2 builds; it's purely an
input-convenience layer. Justified because the APIs don't expose voice
prices (above). Not built now.

---

## File touchpoints (summary)
- `jasper/data/model_pricing.json` — **NEW** bundled, dated default rates
  (model-ID keyed). The single source of default pricing.
- `jasper/usage.py` — `load_default_pricing`, `pricing_for_model`,
  `load_pricing_overrides` (model-ID keys); **delete** `pricing_for_provider`,
  the `*_PRICING` constants, `_OVERRIDE_KEYS`, the `"mini"` hack.
- `jasper/voice_daemon.py` — call `pricing_for_model(_active_model(cfg), …)`.
- `jasper/web/voice_setup.py` — `_provider_card_html` section,
  `_apply_pricing_save`, `POST /pricing` route, `as_of` display.
- `jasper/web/_common.py` — `write_json_file`.
- `tests/test_usage.py`, `tests/test_voice_setup.py` — coverage.
- On ship: update [HANDOFF-voice-providers.md](HANDOFF-voice-providers.md)
  "Spend-cap pricing" bullet (provider→model keying, dated JSON), and
  re-check `docs/doc-map.toml` (`voice-runtime-and-providers` already
  covers `jasper/web/voice_setup.py` + `jasper/usage.py`; add
  `jasper/data/model_pricing.json` to that subsystem's code globs).

## Open decisions
- **Resolved:** model-ID keying (not provider); **`pricing_for_provider`
  and any provider-level/fallback price removed outright — no vestigial
  code, no fabricated single-provider rate**; default rates ship as the
  dated `jasper/data/model_pricing.json`; build Phases 1+2 now; defer
  Phase 3 (the refresh-prompt flow is how defaults/overrides get updated);
  include minimal default/custom tagging + `as_of` text + sparse-override
  reset.
- **Resolved:** unknown/unpriced active-model → **null/zero, never an
  invented estimate, + loud warning + editor flag** (no "most-expensive-
  known" ceiling). Don't give a value where there isn't one.
- **Minor, settle during build:** `/pricing` save with an all-default form
  → **delete** `pricing.json` (lean) vs write `{}`.

Last verified: 2026-05-30 (design written against current `jasper/usage.py`,
`jasper/voice/model_discovery.py`, `jasper/voice/catalog.py`, and
`jasper/web/voice_setup.py`; no code changed yet. Rev 2: defaults move to a
dated `jasper/data/model_pricing.json`; `pricing_for_provider` + all
provider-level/fallback pricing removed per maintainer direction.)
