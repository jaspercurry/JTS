# HANDOFF — per-model pricing rates and the `/voice` editor

Operational spine for the rate data behind spend estimates and the `/voice`
surface that edits it. How cost is computed from those rates, the `Pricing`
rate card, and the spend cap itself belong to
[HANDOFF-voice-providers.md](HANDOFF-voice-providers.md); this doc owns where
rates come from and how a household changes them.

Rates are entered by hand, never fetched from a provider API —
[ADR-0168](adr/0168-voice-model-rates-are-entered-by-hand-never-fetched.md).
An unpriced model costs zero and says so loudly (ADR-0161).

## Where a rate comes from

Pricing is keyed by **model ID, full stop**. There is no provider-level rate
and no provider fallback anywhere in the lookup — a single price for a whole
provider is not a real thing, so JTS never fabricates one.

| Layer | Path | Owner |
|---|---|---|
| Bundled defaults | `jasper/data/model_pricing.json` (package data, carries `as_of`) | the repo; ships with the deploy, no install step |
| Household overrides | `/var/lib/jasper/pricing.json` (sparse, optional `as_of`) | the `/voice` wizard |

`load_default_pricing()` reads and validates the bundled JSON once, returning
`(dict[model_id, Pricing], as_of)`. Unreadable or corrupt data logs at ERROR
and yields an empty map — every model then resolves unpriced and surfaces,
rather than crashing the daemon.

`pricing_for_model(model_id, *, overrides=None)` takes the bundled entry,
overlays the override for that model ID if present, and returns
`Pricing(label="unpriced:<id>")` with all-zero rates when neither has it.
`jasper/voice/daemon_main.py` resolves pricing this way at startup and logs
`event=pricing.unpriced` for an unpriced active model. `jasper-doctor`'s
`check_pricing` (`jasper/cli/doctor/voice.py`) warns on both failure shapes:
rate data that failed to load, and an active model with no rate.

A stale provider-keyed `pricing.json` from before model keying harmlessly
no-ops — provider strings aren't model IDs, so they are ignored and the
bundled defaults apply. There is no migration.

## The `/voice` surface

Two sections, each an independent form so a rate edit never touches keys,
model, or voice settings:

- **Pricing rates** (`_pricing_section_html`, `POST /pricing`) — one
  sub-group per model, the model rows being the provider's catalog models
  ∪ its discovered models. Each sub-group renders only the buckets that
  provider's cost model uses; both the bucket list and the provider's
  official pricing-page URL come from `ProviderCatalogEntry.pricing_buckets`
  / `.pricing_url` in `jasper/voice/catalog.py`, which is the single
  per-provider source for that metadata.
- **Refresh pricing rates** (`_pricing_refresh_html`, `POST /pricing-import`)
  — `_pricing_research_prompt` builds a copyable prompt pre-filled with this
  speaker's exact current model IDs, the per-model rate fields, the official
  pricing URLs, and the output schema; the paste box accepts the JSON that
  comes back. `_apply_pricing_paste` tolerates a fenced block and a bare
  `{model_id: {...}}` map, then validates through the same
  `usage.sanitize_pricing_models` the override loader uses.

Field names follow `price__<model_id>__<bucket_field>`; model IDs are
provider-supplied and therefore escaped as untrusted, per the web-wizard
escaping rule.

### Invariants worth not breaking

- **Sparse overrides.** `_apply_pricing_save` keeps only values that *differ*
  from the bundled default. A blank field is a reset: it is omitted, and the
  daemon falls back to the bundled rate. This is what keeps `pricing.json`
  small and self-explanatory, and it is why "reset to default" needs no route
  of its own.
- **Import merges.** `POST /pricing-import` merges into the existing
  overrides (`_sparsify_overrides` over old ∪ new) rather than replacing
  them, so a paste that covers three models does not silently drop the
  fourth, and a pasted `as_of` is preserved.
- **Effective value in, default beside it.** Each input is pre-filled with
  the rate JTS is actually using, with the bundled default shown as
  placeholder/helper text and a `custom` marker when the effective value came
  from the override file. The bundled `as_of` is displayed so stale rates are
  visible.
- **Writes are atomic and unprivileged.** `write_json_file` in
  `jasper/web/_common.py` does temp-file + rename at mode 0644 —
  `pricing.json` holds no secrets. Saving restarts `jasper-voice`, which
  re-reads overrides at startup.
- **Edits affect future sessions only.** Stored `cost_usd` rows keep the rate
  in force when they were computed; historical cost never changes
  retroactively. The flash text says so.
- **Mutating routes are guarded.** `/pricing`, `/pricing-import`, and
  `/spend-cap` all pass `guard_mutating_request` (host allowlist + CSRF)
  before any work.

The `/voice` spend-cap status and settings surface (`_spend_cap_section_html`,
`_apply_spend_cap`, `POST /spend-cap`) lives in the same module; its
accounting behavior stays in
[HANDOFF-voice-providers.md](HANDOFF-voice-providers.md).

## File touchpoints

| Path | Role |
|---|---|
| `jasper/data/model_pricing.json` | bundled dated default rates; the single source of default pricing |
| `jasper/usage.py` | `load_default_pricing`, `default_pricing_as_of`, `pricing_for_model`, `load_pricing_overrides`, `sanitize_pricing_models` |
| `jasper/voice/catalog.py` | per-provider `pricing_url` + `pricing_buckets` |
| `jasper/voice/model_discovery.py` | the discovered-model list the editor and research prompt enumerate |
| `jasper/voice/daemon_main.py` | resolves pricing at startup; `event=pricing.unpriced` |
| `jasper/web/voice_setup.py` | editor, refresh/import, spend-cap forms and routes |
| `jasper/web/_common.py` | `write_json_file` (atomic, 0644) |
| `jasper/cli/doctor/voice.py` | `check_pricing` |

Tests: `tests/test_usage.py` (lookup, override overlay, unpriced label),
`tests/test_voice_setup.py` (sparse save, import merge), `tests/test_doctor_voice.py`.

---

Last verified: 2026-08-26 (routes, section/builder names, the merge-on-import
and sparse-save invariants, `write_json_file`'s mode, the catalog's pricing
metadata, and `check_pricing`'s two warn paths rechecked against the tree.
The three-phase build narrative was removed — all of it shipped, and the plan
is in git history.)
