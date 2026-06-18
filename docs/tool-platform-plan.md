# JTS Tool Platform — Vision, Research & Plan

> **Status: living plan.** Forward-looking roadmap, not operational
> truth about shipped code. Captures the vision for turning JTS's
> integrations into an extensible foundation, the research behind it,
> what we decided, and how we'll get there.
> **Last updated: 2026-06-18.**

---

## 1. The vision

Today JTS ships a fixed set of 29 built-in tools (weather, transit,
smart-home, music, calendar, email, timers, …). The vision is to turn
that into an **extensible foundation other people can build on** — so
that adding a new capability to the speaker is a clean, local act, and
eventually a *store / marketplace* where households discover and add
the tools they want, and creators publish their own.

The important refinement: this is not "first-party tools now,
user-defined tools later" as two runtime classes. The durable target is
one **source-neutral tool boundary** that every tool crosses:

- tools Jasper writes in this repo,
- trusted contributor tools reviewed as PRs,
- and future no-code / HTTP / MCP integrations created through an
  interface.

The runtime should not care which source produced a tool. Every source
compiles into the same provider-neutral definition, executor, registry,
catalog, dispatch, logging, state, and test expectations. First-party
tools should therefore be the reference implementation of the boundary,
not privileged exceptions to it. This is the "show by doing" path: make
today's working tools look like the tools we want others to copy.

We get there along a **trust gradient**, not in one leap. The three
phases below are the spine of this whole plan:

| Phase | Who writes tools | How they're trusted | Where we are |
|---|---|---|---|
| **1 — "Just me"** | Jasper | Fully trusted (first-party, in-repo) | **Now → first version** |
| **2 — "Trusted PRs"** | Contributors | Maintainer reviews them against guidelines and *runs them himself* | Intermediate future |
| **3 — "Champagne problem"** | Anyone, at scale | Can't personally review/run everything → real boundaries needed | Far future, maybe never |

The single most important framing in this document: **almost all of the
"platform" machinery (sandboxing, permission enforcement, encrypted
secrets, a curated index, fail-closed CI, anti-tamper, a marketplace UI)
exists to safely run code written by people you don't trust.** In Phase
1 there are none. In Phase 2 the maintainer reviews and runs each tool,
so they're still trusted. That machinery only becomes necessary in Phase
3 — and we are not there, won't be for a long time, and may never be.

So: **build the foundation now, build the vault when there's something
to put in it.**

---

## 2. The research we did

Four bodies of research fed this plan. Preserved here so we don't lose
the findings.

1. **Internal codebase analysis** — a multi-agent workflow (17 agents)
   mapped every extensibility-relevant JTS subsystem against a
   tool/plugin-library design report, then ran an *adversarial
   verification* pass (38 load-bearing claims checked against real
   code; 4 defects caught) and a *completeness critic* against JTS's
   own hard rules (COAH, no-silent-failure, bounded RAM, file
   ownership, the config-ownership decision tree).

2. **External pass 1 — platform primitives.** A prioritized briefing on
   the five primitives every plugin ecosystem needs (manifest,
   host↔plugin contract, capability model, trust/distribution, secret
   management), the four-tier distribution model, sandboxing options,
   and a capability-broker secret design. Prior art: Home Assistant
   (core integrations + HACS), VS Code/Open VSX, MCP, Obsidian,
   Raycast, browser/iOS permissions, WASM/Extism, Homebrew taps.

3. **External pass 2 — the "five wrinkles."** A decision briefing on the
   real-time re-declare wall, the sandbox spawn path vs daemon
   hardening, the docstring-vs-token-ceiling split, generic tool-failure
   cues, and panic-mode safe-boot.

4. **External pass 3 — tool-scaling on real-time voice.** How
   dynamic-tool-loading patterns (OpenAI `tool_search`/`defer_loading`,
   Anthropic Tool Search Tool, langgraph-bigtool, RAG-MCP) behave, and
   crucially how they map (or don't) onto a real-time speech-to-speech
   voice loop.

Key external sources worth keeping: RAG-MCP (arXiv:2505.03275);
Anthropic "Introducing advanced tool use" (Nov 2025) and "Effective
context engineering"; Gemini Live API tools docs (128 function-
declaration limit; tools fixed at `BidiGenerateContentSetup`); OpenAI
Realtime notes (16,384-token instructions+tools ceiling); the Postmark
MCP backdoor, npm Shai-Hulud worm, and Open VSX GlassWorm incidents (the
"review-once is dead" evidence for *untrusted* code).

---

## 3. What we found

### 3.1 JTS is ~70% of the way there already — this is not greenfield
The hard parts exist in embryo and should be reused, not reinvented:
- A real tool contract: `Tool` + `@tool` + `build_tool` + a single
  `dispatch_tool` execution seam (`jasper/tools/`).
- A **proven self-contained plugin registry** in `jasper/transit/`
  (`active_transit`, `CITY_PACKS` → derived registry, fail-loud
  duplicate-id guard, per-provider fault isolation) — the daemon
  iterates it with zero per-provider knowledge. Two more registry
  exemplars exist: `jasper/music_sources.py` and `jasper/voice/catalog.py`.
- A **dormant but fully-wired per-tool filter**: `Tool.providers` +
  `_visible_to`. It filters by provider today; it is the natural seam
  for tool *scoping* later. No shipped tool uses it yet.
- Supporting infrastructure to reuse: `model_downloads` (bounded,
  **hash-pinned** fetch), wizard-owned `/var/lib/jasper/*.env` SSOT +
  atomic writers, the single-writer reconciler pattern, the flat
  `@doctor_check` registry, the `cues` no-silent-failure machinery, and
  `jasper-bootloop-guard` (a ready-made safe-boot foundation).
- `numpy`, `scipy`, and `onnxruntime` already ship (for the audio /
  wake stack), so a future embedding-based tool retriever adds
  essentially **zero new dependencies**.

### 3.2 The capability pack is the real boundary
The copyable unit should be a **capability pack**, not a loose function
and not a special "third-party tool" lane. A pack may be tiny (`time`:
one function, no setup, no client) or deep (`transit`: wizard, saved
config, provider registry, runtime clients, several tools). The system
should see both through the same outer contract:

| Pack facet | What it owns |
|---|---|
| Metadata | stable id, title, category, summary, labels, setup URL |
| Setup | optional wizard/config state, setup gate, "needs setup" status |
| Runtime | optional clients, source adapters, provider registries, caches |
| Tools | provider-neutral `ToolDefinition` records |
| Execution | one `ToolExecutor` per tool (`python` now; `http`, `mcp`, or sandbox later) |
| Tests/observability | manifest/catalog/dispatch tests, regression scenario, doctor/state hooks |

The pack boundary is what makes complexity local. A simple pack should
stay a postcard example. A sophisticated pack should be allowed to own a
real configuration UI and runtime client graph. What must not vary is the
outer shape that `jasper-voice` consumes.

The conceptual stack:

1. `ToolDefinition` — provider-neutral schema and metadata: name,
   model-facing prompt, input schema, output/failure contract, labels,
   risk flags, provider visibility, timeout, examples, and pack identity.
   OpenAI/Gemini/Grok serializers read only this.
2. `ToolExecutor` — a small async execution protocol. Current coded tools
   use a `PythonExecutor`; future UI-built integrations use an
   `HttpExecutor`; later MCP/sandbox support is just another executor type.
3. `CapabilityPack` — the contributor-facing module boundary. It owns
   setup, runtime deps, definitions, executors, and tests.
4. `ToolRegistry` — aggregation only: walk packs, fault-isolate pack
   build, apply enabled/disabled state and prompt overrides, then expose
   provider schemas and dispatch handles.
5. `dispatch_tool()` — the single runtime gate for timeout, logging,
   redaction, scalar wrapping, errors, tracing, and eventually async job
   handoff.

`@tool(...)` should remain a good authoring convenience, but it should be
sugar for "make a `ToolDefinition` plus a `PythonExecutor`." The decorator
is not the platform boundary by itself.

Do not flatten rich packs into the shape of simple ones. Instead, make
rich packs prove that complex setup and runtime state can live behind the
same contract. `Transit` is the important reference case here, not an
exception to hide.

### 3.3 The former structural bottleneck
Tool assembly used to be **hardcoded** in `jasper/voice/daemon_main.py`
(`_build_registry`): a fixed import block plus one register loop per
subsystem. Transit was the only carve-out that escaped it. Phase 1.5
replaced that with `jasper.tools.packs.TOOL_PACKS`, and the next slice
promoted those records into the explicit `CapabilityPack` contract. The
remaining scaling rule is now local: adding a tool family should mean
adding or extending a capability pack, not editing daemon registration,
provider adapters, catalog internals, or central `Config`.

### 3.4 One problem is live *today*, at 29 tools
Our tool **descriptions alone** total **~8,200 tokens** — already about
half of OpenAI Realtime's hard **16,384-token** instructions+tools
ceiling (a real builder hit that wall with just 9 verbose tools). Tool
descriptions are the verbatim docstrings, which we keep deliberately
rich. Splitting a short model-facing description from the full human
docstring roughly **halves** that footprint and buys years of runway.
This is the one near-term fix that isn't optional.

### 3.5 "Sessions" make the re-declare wall a non-issue for us
JTS opens **one persistent live connection** at daemon startup; wake
events ride *turns* on it. Tools are declared when that connection
opens, so "changing tools mid-session" means a reconnect. But: our
interactions are short and one-shot, and `CONTEXT_RESET` is **off by
default** — we keep little conversation state and have explicitly
decided it isn't worth much. So a reconnect costs us ~sub-second of
latency and almost no lost state. **The re-declare wall is a latency
footnote for JTS, not an architecture driver** — which is why the
industry's "router / meta-tool" pattern (built to preserve long-session
state and prompt cache) is the wrong fit for us: it would hide
parameter schemas and add a model round-trip to buy benefits we don't
collect.

### 3.6 Tool scaling has a clean ladder (when we eventually need it)
- **A — declare everything** (today). Simplest; correct until token
  pressure or mis-selection appears. The description split (3.4)
  extends its runway dramatically.
- **B — static scoping** via the existing `_visible_to` seam: declare a
  context-relevant subset at connection-open (room / recently-used /
  enabled), reconnect-to-broaden on a miss (cheap, per 3.5).
- **B+ — daemon-side embedding pre-filter** feeding B: a tiny local
  model (e.g. Model2Vec `potion-base-8M`, ~8 MB, numpy-only) picks the
  top-k relevant tools to declare — *full schemas kept, no model
  round-trip*. This is RAG-MCP applied to our own registry, in our code.
- **C — in-band router meta-tool: never.** Hides schemas, adds latency,
  solves a problem (long-session state) we don't have.

Note: industry tool-search/deferred-loading features are **text-API
only** — not available on the realtime voice path as of mid-2026 — so we
can't outsource this to the provider. We own the scoping layer.

### 3.7 Sandboxing, secrets, distribution — real, but Phase-3 problems
- **Sandbox** (for untrusted code): `systemd-run` + a `jts-tools.slice`
  for resource caps, with **unprivileged-userns bubblewrap + seccomp**
  inside for isolation, is the primary path; WASM/Extism is a narrow
  pure-compute case (it can't run C-extension Python like numpy);
  Firecracker is out (the Pi is GICv2). None of this is needed until we
  run code we haven't vetted.
- **Secrets:** capability-brokerage (a tool asks for "a smart-home
  backend," not a raw key — prior art: HA Application Credentials).
  **Encryption-at-rest is deferred**: `peer_id` is world-readable
  (mode 0644) so it's a bad key anchor, and a device-bound key risks an
  unrecoverable brick after an SD re-image. If ever needed: a
  passphrase-derived key (Argon2id) with escrowed recovery codes, never
  device-bound. Plaintext-0600 matches the home-LAN threat model.
- **Distribution / review-once is dead — for untrusted code.** The
  Postmark MCP backdoor, the npm Shai-Hulud worm, and the Open VSX
  GlassWorm token leak all weaponized *post-approval* changes, which is
  why a future marketplace would need fail-closed CI, description
  hash-pinning, and anti-rug-pull re-consent. All of that is Phase 3.

### 3.8 Free wins that help at any scale
- **Tool-use examples** raise parameter accuracy materially (Anthropic
  measured 72% → 90%) — attach 1–2 example calls to confusable tools.
- **Clean, non-overlapping descriptions + deterministic ordering** beat
  token-count as the real scaling risk past ~100 tools. "If a human
  can't tell which tool to use, neither can the model."

### 3.9 Verified codebase corrections (don't build on wrong facts)
It's **29 tools today**. `build_tool` **warns** on sync functions
rather than rejecting them (so "everything is a coroutine" is a
convention, not an invariant). The live connection is **persistent**.
`peer_id` is **0644**.

---

## 4. Rationale — why the first version is small

1. **~90% of the full platform plan defends against untrusted
   third-party code that does not exist yet.** Sandbox, capability
   *enforcement*, encryption, the index, fail-closed CI, anti-rug-pull,
   kill-lists, signing keys, MCP — every one exists to safely run
   strangers' code. Building them now is building a bank vault before
   opening a lemonade stand.

2. **The trust gradient defers the hard work honestly, not the
   boundary.** In Phase 1 you write the tools. In Phase 2 you *review
   and run* each contributed pack, so it's trusted the same way your own
   code is — you still need no sandbox or permission enforcement. Only
   Phase 3 (running code you can't personally vet, at a scale where
   review doesn't keep up) requires the heavy machinery. But Phase 1
   tools should already cross the same definition/executor/pack boundary
   that Phase 2 contributors will cross.

3. **First-party tools are the conformance suite.** The current tools
   should not be privileged runtime exceptions. They should prove that a
   simple function tool, a source-backed music tool, a setup-backed API
   tool, a deep transit integration, and a consequential smart-home tool
   can all fit behind one outer pack contract.

4. **Do the cheap thing in a forward-compatible shape — but don't build
   the future.** Generalize the registry *the transit way*, make
   definitions/executors explicit, and split descriptions. That means
   later contributors and no-code builders compile into the same
   artifact, without pulling Phase-3 sandbox/store machinery forward. It
   is *not* a license to build the later phases now.

5. **It honors JTS's own bar (COAH).** Smallest durable shape that fits;
   no speculative abstraction; bounded RAM on the 1 GB core; the 1 GB
   cap applies to the *core* only — heavier or more numerous tools are
   an opt-in concern of later phases and bigger hardware.

---

## 5. The initial plan (Phase 1 — make current tools prove the boundary)

**Goal: adding or extending a capability pack is a clean, local act, and
every current tool works through the same layers a future contributor or
no-code builder would use.** The model also cannot choke as the catalog
grows.

**Already shipped foundation**
1. **Data-driven pack walk.** `_build_registry` now delegates to an
   ordered `TOOL_PACKS` registry of `CapabilityPack` records, mirroring
   `jasper/transit/active_transit`: each tool family has `build(deps)` and
   optional `gate(deps)`, and pack build is fault-isolated behind
   try/except. The before/after registry-equality assertion pins
   byte-identical tool names, schemas, descriptions, providers, timeouts,
   and order. `ToolPack` remains a compatibility alias for older in-repo
   references.
2. **Model-facing description seam.** `llm_description` lets a tool send a
   shorter model prompt while keeping the full human docstring. The token
   budget check keeps the convention honest as tools are added.
3. **Derived manifest.** `Tool.to_manifest()` and
   `ToolRegistry.to_manifest()` emit a provider-neutral record in
   registration order. Today it is derived from code; tomorrow it is the
   review/scaffold seam for contributor packs.
4. **Labels and risk flags.** `labels`, `untrusted_output`, and
   `consequential` are declarative metadata that catalog/store/policy
   layers can consume without sending extra text to the model.
5. **Built-in catalog UI.** The
   `/tools/` wizard ([`jasper/web/tools_setup.py`](../jasper/web/tools_setup.py)).
   The shipped surface is a browse + on/off manager over the *first-party*
   tools — explicitly **not** the install-from-store marketplace (no install
   path; that stays Phase-2/3). It is now **pack-first**: `/tools/` renders
   one top-level card per user-facing capability pack, grouped by category,
   with singleton packs for standalone tools. A household enables "Spotify"
   or "Weather" first, then optionally opens the generated pack detail page
   for child-tool controls, full prompt copy, schema/metadata, and advanced
   prompt override/reset. Architecture mirrors the other wizards: the
   socket-activated page only **reads** the catalog `jasper-voice` writes to
   `/run/jasper/tools.json`
   ([`jasper/tools/catalog.py`](../jasper/tools/catalog.py)) and never
   imports `jasper.tools` (the transit lazy-import lesson — keep the
   wizard light). The catalog payload now carries scan-friendly `summary`
   copy, full `details`, a `category`, and an optional display `pack`
   ([`CatalogPack`](../jasper/tools/packs.py)). Display packs are a UI
   affordance, not a runtime container: multiple internal registration
   packs can share one display pack (`calendar` + `gmail` → Google), and
   standalone tools receive generated singleton packs in the catalog view.
   The `/tools/` page groups by category and pack, and each visible pack gets
   a generated `/tools/pack/<id>/` detail page from the same catalog JSON
   (`/tools/tool/<name>/` remains a compatibility route for older links).
   **Toggle stages, Apply commits — two steps on purpose.**
   A toggle only writes staged state to the wizard-owned SSOTs:
   `/var/lib/jasper/tool_state.env`
   (`JASPER_DISABLED_TOOLS`, `JASPER_DISABLED_TOOL_PACKS`,
   [`jasper/tool_state.py`](../jasper/tool_state.py), mode 0644) and
   `/var/lib/jasper/tool_prompt_overrides.json`
   ([`jasper/tool_prompt_overrides.py`](../jasper/tool_prompt_overrides.py));
   it does **not** restart `jasper-voice`. Restarting the assistant drops
   any in-progress conversation and briefly deafens the speaker, so doing it
   silently on every checkbox or prompt edit is user-hostile — and an
   unthrottled per-change restart could feed `jasper-voice`'s
   `StartLimitAction=reboot` crash-loop ladder. The page re-derives each
   pack/tool's on/off state and prompt customization through an overlay
   ([`jasper/tool_catalog_view.py`](../jasper/tool_catalog_view.py) — also
   light: `json` + state readers only) so the UI **converges instantly**
   without waiting on, or being raced by, a restart. An explicit **Apply**
   (`POST /apply`) restarts `jasper-voice` **once** so staged changes go
   live; it reports honestly when no restart will happen (no provider /
   bonded follower) and is rate-limited (≥20 s between restarts) so a burst
   of Apply calls can't trip the reboot ladder. `jasper-voice` then
   re-filters the registry (`register_packs(..., disabled=...,
   disabled_packs=...)`), applies prompt overrides, and re-writes the catalog
   JSON. Observability: the catalog summary (present / count / disabled /
   disabled packs / prompt overrides / pending) is on `/state.tools` and
   `jasper-doctor`'s `check_tool_catalog`. The confirmation companion
   `home_assistant_confirm` is hidden from the catalog UI (an internal half
   of the HA consequential-action flow, not an independently toggleable
   capability). Fail-safe toward *more* functionality, mirroring
   `mic_mute_persistence`: missing/unreadable/malformed state resolves to
   "nothing disabled and no prompt overrides," so an FS-corruption incident
   cannot deafen the assistant. A disabled tool simply does not register —
   the model never sees it — so there is no audible cue (it's the user's
   explicit choice, not a failure).
6. **Explicit definition/executor/pack boundary.** `@tool(...)` now compiles
   to a provider-neutral `ToolDefinition` plus `PythonExecutor`, while
   `dispatch_tool()` calls the `ToolExecutor` protocol so explicit
   definition/executor pairs flow through the same timeout/log/error
   contract as decorated first-party functions. `CapabilityPack` is the
   copyable unit: it owns metadata, setup-required state, build/gate logic,
   catalog grouping, and may return either decorated functions or already
   built `Tool` objects. The voice-eval harness now registers through the
   same pack walk as the daemon. The shipped `time` pack is the simple
   explicit reference, and `weather` is the API-backed explicit reference.

**Next boundary slice**
1. **Keep migrating examples across the complexity ladder.** With `time` and
   `weather` now covering the simple and API-backed references, the next
   candidates are `spotify` / `playback` as source-backed references,
   `transit` as the deep wizard/config/provider-registry reference, and
   `home_assistant` as the high-risk consequential-action reference.
2. **Gate every refactor on byte-identical behavior.** Existing 29-tool
   provider schemas, manifest entries, catalog payloads, dispatch behavior,
   and registration order must stay identical unless a change is explicitly
   intentional and voice-eval/docs are updated.

Everything is built with **Opus**. Every new tool still ships its
regression scenario under `tests/voice_eval/regression/` (existing hard
rule). Nothing here adds a daemon, a dependency, or RAM.

**Explicitly NOT in the first version:** sandbox, safe-boot-for-tools,
capability enforcement, secret broker, encryption, the curated index,
CI trust-gating, the install-from-store `/tool-store/` marketplace
(distinct from the read-only built-in `/tools/` on/off catalog, which
*did* ship — item 5 above), grants, anti-rug-pull, kill-lists, MCP, the
embedding pre-filter, and even static `_visible_to` scoping (the
description split buys enough headroom that scoping waits until install
counts actually grow).

---

## 6. How we continue to make progress

We build each later piece **only when its specific trigger fires** —
never on a schedule. The deferred work is catalogued so it isn't lost,
but it stays catalogued until a real need pulls it forward.

### Phase 2 — "trusted PRs" (build when contributors want in)
The goal is to let other people contribute **capability packs** that
*you review and run yourself*. Because they're maintainer-vetted and run
in your repo, they stay trusted — no sandbox, no enforcement.

Contributor packs should use the same boundary first-party packs use:

- **A pack manifest** describing metadata, setup needs, tool definitions,
  risk flags, examples, and executor types. The Phase-1 derived manifest
  is the first half of this; Phase 2 makes it authorable/parseable for
  review.
- **A local runtime module** only when needed. A simple pack might be
  manifest + one Python function. A deep pack can own clients, setup
  readers, provider registries, and caches behind the same outer pack
  contract.
- **A light CI check** — manifest validity, no obfuscation, descriptions
  present, risk flags declared, examples parse, and every exposed tool has
  a regression-scenario mention.
- **Contribution guidelines** covering pack shape, when to extend an
  existing pack vs create a new one, prompt style, failure contracts,
  setup ownership, and the no-silent-failure rule.

The runtime remains the Phase-1 runtime. Contributor packs register
in-process and in-repo through the same `ToolRegistry`,
`ToolDefinition`, `ToolExecutor`, catalog, and dispatch paths as Jasper's
own tools. "Contributed" is a source/review status, not a runtime type.

### Tool labels — the catalog's facet, and retiring the transit city toggle

A `labels` facet now rides the tool/manifest contract (`@tool(labels=...)`
→ `Tool.labels` → the derived manifest), declaration-only and **not sent
to the model** (it never enters the provider serializers — zero token
cost). It is the catalog's sort/filter/search primitive — how a household
will find tools in the future `/tool-store/`, and how third-party tools
categorize themselves. The shipped first-party tools are tagged today
(`("transit","nyc","subway")`, `("music","spotify")`,
`("productivity","google","gmail")`, etc.).

This resolves a transit design question. Today a city is a first-class
`CityPack` + a `JASPER_TRANSIT_CITIES` toggle — but a city carries no
shared behavior (the NYC providers share no key or runtime, only the name
"NYC"), so it is a *taxonomy*, which wants a **label**, not a container
class. The forward model: city becomes a label; the tools UI filters by
it; per-tool enablement (default *configured ⇒ on*, preserving provider
self-gating) replaces the city toggle.

**Sequenced, not rushed.** The `labels` field ships now (cheap, through
the Phase-1 seam, useful immediately for the catalog). Retiring
`CityPack` / `JASPER_TRANSIT_CITIES` waits for **two forcing functions**:
the Phase-2 `/tool-store/` UI (to host the filter + per-tool enable) and a
**second city** (so the multi-city label scheme is designed against two
real examples, not one). Until then `CityPack` is the right-sized solution
and stays. Labels are organization; enablement is separate state — keep a
single source of truth for "is this tool on."

### Display packs and categories — organization, not a source/provider model

The built-in catalog now has two human-facing organization levels:
`category` (Music, Transit, Smart Home, Productivity, Utilities, System)
and `CatalogPack` display groups (Spotify, NYC Transit, Google, Timers,
Playback, Home Assistant, plus generated singleton packs for standalone
tools). This is intentionally the smallest useful shape for the current
problem: make the built-in catalog browsable, shorten card copy, and give
each capability a detail/configuration page.

This does **not** make "Spotify" a new runtime source abstraction, and it
does **not** make "OpenAI/Gemini/Grok" a generalized provider package.
Those may become larger opt-in modules later (sources, voice providers,
hardware profiles), but they should be pulled by concrete needs and use the
repo's existing Pattern-2 registry / reconciler decision tree. Runtime tool
definitions can still omit a display pack; the catalog view creates
singleton packs only for the UI so standalone tools remain natural in code.

### Pack-first catalog UI — shipped product shape

The `/tools/` catalog now treats the **pack/capability** as the top-level
object:

- `/tools/` renders one card per user-facing capability pack, not one card
  per tool. Spotify, Music Playback, NYC Transit, Home Assistant, Google,
  Timers, Weather, and Time are the right mental model. A capability with
  exactly one tool still gets one top-level card; its detail page simply
  contains one child tool.
- Categories remain a browsing/filtering layer over those cards. They do
  not make some packs full-width and some half-width as an accident of child
  tool count; the layout is stable and scan-friendly.
- Setup/configuration belongs to the pack when a pack exists. Child tools
  can inherit `needs_setup` state, but the user sees one clear setup action.
- Top-level toggles operate at the pack level. Individual tool toggles are
  advanced controls on the detail page. This matches the real choice:
  enable/disable "Spotify" first, then optionally disable a specific leaf
  like queueing if a household has a reason.
- Pack and tool state remain separate, so pack toggles do not erase per-tool
  preferences. The SSOT is a wizard-owned disabled pack set next to the
  disabled tool set: `JASPER_DISABLED_TOOL_PACKS` plus
  `JASPER_DISABLED_TOOLS`. Effective disabled state is "pack disabled OR
  tool disabled." A pack whose children are mixed renders as partially
  enabled.
- Applying stays two-step. Staging pack/tool/prompt changes avoids
  restarting `jasper-voice`; Apply restarts once, with the existing
  rate-limit and honest "could not restart" behavior.

Pack detail pages are the real management surface. A detail page shows:

- Pack title, description, status, setup/configuration action, and a link to
  the tool-authoring guide.
- The child tools as compact rows/sections, not nested cards inside cards.
  Each row shows status, optional advanced toggle, and an expand affordance.
- Expanded tool content with the exact model-facing prompt, the default
  prompt, JSON schema/parameters, labels, provider compatibility, timeout,
  and risk flags such as `untrusted_output` and `consequential`.
- A reset-to-default affordance wherever user-editable text exists.

Prompt editing is intentionally allowed, but framed as advanced and
at-risk. JTS is open source, so a user can edit the tool descriptions in
the repo anyway; the UI should make that power available without
pretending it is harmless. Do **not** add an intermediate "custom addendum"
layer. The plan is full prompt override:

- The immutable default comes from code
  (`Tool.default_model_facing_description()`), and the UI displays it
  read-only when no override exists.
- An **Edit** action opens the full model-facing prompt in a textarea/editor.
  Saving writes a user override; Reset deletes that override and returns to
  the code default.
- The editor warns that prompt overrides can change model behavior,
  break tool selection, weaken safety instructions, and invalidate eval
  expectations. The wording can be plain: "Advanced: edit at your own risk."
- Overrides are stored outside the codebase in wizard-owned state under
  `/var/lib/jasper/`, atomically written, and treated as non-secret prompt
  text. Missing or malformed override state fails safe to code defaults; a
  future hardening pass can add a dedicated doctor warning if prompt editing
  becomes common enough to merit operator-facing remediation.
- Runtime injection happens at the same seam as
  `model_facing_description()`, so provider serializers, the catalog, and
  manifest-like surfaces agree on what the model will actually see.
- Default and override stay separate in the catalog payload so the UI can
  show a diff, mark "customized," and reset without importing tool modules.
- Prompt edits follow the same staged/apply lifecycle as toggles:
  save the override, show pending, and restart `jasper-voice` once on Apply.
- Prompt length is visible. This is both UX and correctness: tool
  descriptions already press against realtime model token ceilings, so an
  override should show character/token-ish budget feedback before it ships
  to the model.

Tool creation should also start under a parent pack, not as a global
"random tool" button. In the near term this should be a contributor/developer
workflow: the detail page can link to a guide and eventually scaffold a pack
manifest or PR checklist, while executable tool code still lands through
reviewed repository changes. Full in-browser creation of executable tools is
a Phase-3/untrusted-code problem unless it emits a declarative
`ToolDefinition` for an already-existing safe executor such as HTTP.

### Tool-authoring guide for jts.local

The `/tools/guide/` page is a lightweight user/developer-facing guide linked
from `/tools/` and every pack detail page, opened in a new tab
(`target="_blank" rel="noopener"`). This is not the marketplace. It is the
house style for first-party and trusted-PR tools:

- What belongs in a tool vs a pack.
- When to create a new tool, extend an existing pack, or add a label.
- How the `ToolDefinition` / `ToolExecutor` / capability-pack boundary
  works, including why first-party and contributed tools use the same
  runtime path.
- How to write model-facing prompt copy: short purpose first, concrete call
  boundaries, "do not call when..." cases, response style, and failure
  contract.
- Prompt length guidance and why long descriptions matter for realtime
  providers.
- Required tests: static manifest/catalog coverage and a regression scenario
  under `tests/voice_eval/regression/`.
- Safety metadata: `untrusted_output`, `consequential`, logging redaction,
  setup ownership, and no-silent-failure expectations.
- Future polish: examples of good and bad tool prompt copy, including
  Spotify/music, Home Assistant, and transit examples.

### Phase 3 — "champagne problem" (build only if it ever arrives)
The trigger is one of: you want to run tools you *haven't* personally
vetted, OR there are so many tools/authors that review can't keep up.
Then, and only then, draw from this catalogue — each item gated by its
own trigger:

| Future capability | Trigger that pulls it forward |
|---|---|
| Static `_visible_to` scoping (ladder B) | Instructions+tools approach ~12–13K tokens, or mis-selection rises |
| Embedding pre-filter (ladder B+) | Households install tens/hundreds of tools |
| Disambiguation + deterministic ordering pass | Catalog passes ~100 semantically-overlapping tools |
| Curated index + fail-closed CI + tiers | Third-party authors at a scale review can't keep up with |
| Out-of-process sandbox (bubblewrap + slice) | You run code you haven't personally vetted |
| Safe-boot-for-tools (extend bootloop-guard) | Installable tools can crash the speaker |
| Capability *enforcement* + secret broker | Untrusted tools touch secrets / network / smart-home |
| Anti-rug-pull (hash-pin + re-consent) + kill-list | Tools update out-of-band after approval |
| Per-plugin spend metering | Tools make paid LLM / API calls |
| MCP client bridge | A power user wants external MCP servers (heavy-hardware tier) |
| Encryption-at-rest | Multi-user or off-LAN exposure (with the Argon2id + escrow design) |

### Cross-cutting rules that apply whenever we do build later phases
- No separate third-party runtime lane. First-party, trusted-contributor,
  and future UI-built tools all compile into the same
  `ToolDefinition`/`ToolExecutor`/registry/dispatch/catalog path.
- No silent failure → audible cue (generic slugs; cues can't name a
  plugin) for any tool-layer event that blocks a response.
- Everything is **Pattern 2** (self-contained registry), never typed
  `Config`.
- Pin safety invariants with tests in the same PR.
- Doctor checks stay flat; reserve an order-band for tool-platform
  checks.
- Gate refactors on the free registry-equality assertion, not paid
  voice-eval loops.
- The 1 GB cap binds the *core*; heavier tools are an opt-in,
  bigger-hardware concern.

### On-device facts to verify before relying on later phases
1. Unprivileged user namespaces enabled on the deployed Pi OS Trixie
   image (needed for the Phase-3 sandbox).
2. The ~30-tool accuracy "cliff" is a *text-model* number applied to
   voice by analogy — validate with a cheap on-device eval before
   trusting any specific N.
3. Whether the 16K instructions+tools ceiling rises on newer realtime
   models.

---

## 7. One-line summary
**Make current tools prove the shared boundary: capability packs compile
`ToolDefinition` + `ToolExecutor` into the existing registry, dispatch,
provider serializers, catalog, and tests. Add trusted contributor packs
through that same path when people want in; build the vault only if the
champagne problem ever actually arrives.**
