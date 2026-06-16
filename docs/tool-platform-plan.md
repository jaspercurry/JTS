# JTS Tool Platform — Vision, Research & Plan

> **Status: living plan.** Forward-looking roadmap, not operational
> truth about shipped code. Captures the vision for turning JTS's
> integrations into an extensible foundation, the research behind it,
> what we decided, and how we'll get there.
> **Last updated: 2026-06-15.**

---

## 1. The vision

Today JTS ships a fixed set of ~28 built-in tools (weather, transit,
smart-home, music, calendar, email, timers, …). The vision is to turn
that into an **extensible foundation other people can build on** — so
that adding a new capability to the speaker is a clean, local act, and
eventually a *store / marketplace* where households discover and add
the tools they want, and creators publish their own.

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

### 3.2 The one real structural bottleneck
Tool assembly is **hardcoded** in `jasper/voice/daemon_main.py`
(`_build_registry`): a fixed import block plus one register loop per
subsystem. Transit is the only carve-out that escaped it. **Adding a
tool family means editing core.** Generalizing this one function —
applying the transit pattern to all tools — is the actual "modularize"
deliverable, and it's nearly free.

### 3.3 One problem is live *today*, at 28 tools
Our tool **descriptions alone** total **~8,200 tokens** — already about
half of OpenAI Realtime's hard **16,384-token** instructions+tools
ceiling (a real builder hit that wall with just 9 verbose tools). Tool
descriptions are the verbatim docstrings, which we keep deliberately
rich. Splitting a short model-facing description from the full human
docstring roughly **halves** that footprint and buys years of runway.
This is the one near-term fix that isn't optional.

### 3.4 "Sessions" make the re-declare wall a non-issue for us
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

### 3.5 Tool scaling has a clean ladder (when we eventually need it)
- **A — declare everything** (today). Simplest; correct until token
  pressure or mis-selection appears. The description split (3.3)
  extends its runway dramatically.
- **B — static scoping** via the existing `_visible_to` seam: declare a
  context-relevant subset at connection-open (room / recently-used /
  enabled), reconnect-to-broaden on a miss (cheap, per 3.4).
- **B+ — daemon-side embedding pre-filter** feeding B: a tiny local
  model (e.g. Model2Vec `potion-base-8M`, ~8 MB, numpy-only) picks the
  top-k relevant tools to declare — *full schemas kept, no model
  round-trip*. This is RAG-MCP applied to our own registry, in our code.
- **C — in-band router meta-tool: never.** Hides schemas, adds latency,
  solves a problem (long-session state) we don't have.

Note: industry tool-search/deferred-loading features are **text-API
only** — not available on the realtime voice path as of mid-2026 — so we
can't outsource this to the provider. We own the scoping layer.

### 3.6 Sandboxing, secrets, distribution — real, but Phase-3 problems
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

### 3.7 Free wins that help at any scale
- **Tool-use examples** raise parameter accuracy materially (Anthropic
  measured 72% → 90%) — attach 1–2 example calls to confusable tools.
- **Clean, non-overlapping descriptions + deterministic ordering** beat
  token-count as the real scaling risk past ~100 tools. "If a human
  can't tell which tool to use, neither can the model."

### 3.8 Verified codebase corrections (don't build on wrong facts)
It's **28 tools, not 27**. `build_tool` **warns** on sync functions
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

2. **The trust gradient defers the hard work honestly.** In Phase 1 you
   write the tools. In Phase 2 you *review and run* each contributed
   tool, so it's trusted the same way your own code is — you still need
   no sandbox or permission enforcement. Only Phase 3 (running code you
   can't personally vet, at a scale where review doesn't keep up)
   requires the heavy machinery.

3. **Do the cheap thing in a forward-compatible shape — but don't build
   the future.** Generalize the registry *the transit way* (so a "pack"
   could one day be fed by a manifest) and split descriptions (so a
   manifest field already exists). That costs nothing extra and means
   later phases bolt on without rework. It is *not* a license to build
   the later phases now.

4. **It honors JTS's own bar (COAH).** Smallest durable shape that fits;
   no speculative abstraction; bounded RAM on the 1 GB core; the 1 GB
   cap applies to the *core* only — heavier or more numerous tools are
   an opt-in concern of later phases and bigger hardware.

---

## 5. The initial plan (Phase 1 — "just me")

**Goal: adding a tool is a clean, local, one-place change, and the model
doesn't choke as the catalog grows.** That's the whole first version.

**Must-have**
1. **Generalize `_build_registry` into a data-driven registry walk**,
   mirroring `jasper/transit/active_transit`: each tool family becomes a
   registry entry with its own `build(deps)` and a `gate(deps)`
   predicate (lifting the inline gating out of the daemon), iterated
   behind the same per-entry try/except fault isolation transit uses.
   Verify with a **before/after registry-equality assertion** (the tool
   set must be byte-identical) — a free, hardware-free gate; *not* a
   paid voice-eval loop.
2. **Split the model-facing description from the human docstring.** Add
   a short `llm_description` (name + one-line purpose + one
   disambiguating cue) compiled into the schema sent to the model; keep
   the full docstring for humans. Add a CI token-budget check so the
   convention holds as tools are added.

**Cheap, optional, forward-compatible**
3. **Derive a manifest** — `Tool.to_manifest()` + a round-trip no-loss
   test over all 28 tools. ~40 lines, no behavior change. Worth it only
   because it's the seam a future catalog/store and a Phase-2 PR-review
   flow bolt onto.
4. **Free accuracy wins** — add tool-use examples to the most confusable
   tools; tighten any overlapping descriptions.
5. **Tool labels** (shipped as a Phase-1.5 follow-on) — a
   declaration-only `labels` facet on the tool/manifest contract
   (`@tool(labels=...)`), the catalog's future sort/filter/search
   primitive, with the transit tools tagged. Resolves the transit
   "city as a label, not a `CityPack` toggle" question — see §6.

Everything is built with **Opus**. Every new tool still ships its
regression scenario under `tests/voice_eval/regression/` (existing hard
rule). Nothing here adds a daemon, a dependency, or RAM.

**Explicitly NOT in the first version:** sandbox, safe-boot-for-tools,
capability enforcement, secret broker, encryption, the curated index,
CI trust-gating, `/tool-store/` wizard, grants, anti-rug-pull,
kill-lists, MCP, the embedding pre-filter, and even static `_visible_to`
scoping (the description split buys enough headroom that scoping waits
until install counts actually grow).

---

## 6. How we continue to make progress

We build each later piece **only when its specific trigger fires** —
never on a schedule. The deferred work is catalogued so it isn't lost,
but it stays catalogued until a real need pulls it forward.

### Phase 2 — "trusted PRs" (build when contributors want in)
The goal is to let other people contribute tools that *you review and
run yourself*. Because they're maintainer-vetted and run in your repo,
they stay trusted — no sandbox, no enforcement.
- **Publish a `jts-tool.json` manifest format + `parse_manifest`** so a
  contributed tool is reviewable as data (the Phase-1 derived manifest
  is the first half of this).
- **Write contribution guidelines** (the "clean, non-overlapping,
  cheaply-described tool" principles; the regression-scenario
  requirement; the manifest fields).
- **A light CI check** — manifest validity, no obfuscation, descriptions
  present. This is "review a manifest + CI, not every line," but with a
  human (you) still reading and running the code.
- Tools still register in-process, in-repo, via the Phase-1 registry.

### Tool labels — the catalog's facet, and retiring the transit city toggle

A `labels` facet now rides the tool/manifest contract (`@tool(labels=...)`
→ `Tool.labels` → the derived manifest), declaration-only and **not sent
to the model** (it never enters the provider serializers — zero token
cost). It is the catalog's sort/filter/search primitive — how a household
will find tools in the future `/tool-store/`, and how third-party tools
categorize themselves. The transit tools are tagged today
(`("transit","nyc","subway")`, etc.).

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
**Build the foundation (modular tools + a derivable manifest), fix the
one live problem (the description-token ceiling), and stop. Add trusted
contributor PRs when people want in. Build the boundaries only if the
champagne problem ever actually arrives.**
