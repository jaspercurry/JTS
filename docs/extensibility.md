# JTS extensibility — the doctrine

> **Status: canonical doctrine.** This is the cross-cutting *lens* for how
> JTS is extended. It owns the shared invariant, the taxonomy of extension
> contracts, the decision tree, and the build-now-vs-defer line. It does
> **not** restate the per-contract details — those live in their own docs
> (linked below) and remain the single source of truth for their domain.
> When in doubt about *how* to add something, start here; for the specifics
> of a given contract, follow the link.

JTS already extends in several distinct ways — LLM tools, audio sources,
model providers, hardware profiles, and a newer kind of cross-cutting
"feature." They were built at different times and look different, but they
are the *same shape* underneath. This doc names that shape so every future
addition is reasoned about the same way, and so a future contributor can
follow the lead with a clear, understood contract.

It was distilled from JTS's own in-repo prior art (the `Config ownership`
decision tree in [`AGENTS.md`](../AGENTS.md#config-ownership--which-pattern-for-a-new-dac--mic--provider--city),
the transit registry, the reconcilers, the tool platform) plus an external
deep-research pass over how the best extension ecosystems draw these lines
(Home Assistant, VS Code, Figma, Django/Backstage, CLAP/LV2, Kubernetes, and
the cautionary tales — see the appendix).

> **On names:** the contract names below (Tool, Source, Provider, Profile,
> Feature) are working labels. The *lens* is the point, not the vocabulary —
> don't relitigate naming. Where an existing doc uses a different word for the
> same thing, the concept is what matters.

---

## 1. The one rule that matters: host-mediated indirection

**No extension ever holds a direct reference to a powerful host object.**
Everything an extension touches — the audio path, the DAC, secrets, storage,
the scheduler, another extension — it reaches only through host-owned
indirection: a registry the host walks, the single-writer reconciler, the
`dispatch_tool` seam, or a service handle the host injects. An extension
*declares* and *receives*; it never *grabs*.

This is the load-bearing decision, and it is **free today**. It is also the
single property that decides whether the trusted→untrusted future (§5) is
cheap or a rewrite:

- **Figma** swapped its entire plugin execution model (Realms → a QuickJS
  JS VM in WebAssembly) in **9 days, mid-incident**, because plugins only
  ever held *opaque handles* across a message-passing boundary — the host
  could replace everything behind it.
- **Salesforce** needed a multi-year, version-gated *breaking* migration to
  retrofit isolation, because its components had been handed *raw DOM and
  global references*.

JTS already does the right thing for two contracts: the reconciler is the
*single writer* of resolved hardware config (daemons only read), and every
tool call goes through one `dispatch_tool`. The job this doctrine sets is to
make that property **universal and explicit** across all five contracts —
not to build the enforcement machinery (that's deferred, §5).

Concretely, per contract:
- a **Source** can only feed audio into a private snd-aloop lane; it has no
  affordance to set output volume, bypass CamillaDSP, or reach the DAC. The
  loud-output ceiling is a *stage it cannot route around*, not a rule it is
  asked to honor.
- a **Tool** returns data through dispatch; it does not touch the audio path,
  the secret store, or another tool's internals directly.
- a **Feature** (§4) *declares* its contributions; it does not reach into
  `jasper-voice`, spawn its own threads, or open its own provider connection.

---

## 2. The shared meta-pattern

Every JTS extension contract is built from the same pieces. A new contract
that doesn't fit this shape is a smell; a new *instance* that does is routine.

- **Pure-data declaration the host reads without running author code** — a
  frozen dataclass registry entry (we do not need a JSON manifest format
  while every reader is in-process Python and every author is in-repo). This
  is what makes discovery, fault isolation, the token-budgeted tool catalogue,
  and a future capability inspection all possible.
- **One host owner walks the registry** with little per-entry knowledge —
  `dispatch_tool`, a reconciler, a coordinator, a scheduler.
- **Per-entry fault isolation** — one broken entry is skipped (logged,
  surfaced), never crashes startup.
- **Wizard-owned `/var/lib/jasper/*.env` (or a `/var/lib/jasper/*.db`) config
  SSOT, read fresh** — never `os.environ` cached in a long-lived daemon.
- **Observability is part of the contract** — a `/state` section, a flat
  `@doctor_check`, stable `event=` logs, and **no silent failure → audible
  cue** for anything that blocks a response.
- **Strict, loud validation at the boundary** — a malformed declaration fails
  fast and visibly. (No Postel-style liberality; "be liberal in what you
  accept" entrenches accidental behavior as a de-facto contract.)
- **Extract the abstraction only on the SECOND real instance** — the
  rule-of-three applied to subsystems. The first instance is built
  concretely; duplication is cheaper than the wrong abstraction.

---

## 3. The five extension contracts

These five are distinct *because the host must do genuinely different things
with them* (§5 decision tree), not because they were named differently. Each
keeps its own detailed contract doc; this table is the map.

| Contract | The unit | Why it's its own contract | Canonical doc |
|---|---|---|---|
| **Tools** | a tool (grouped into a pack) | an LLM-callable action; declared to the provider at connect time; spends the model's token budget; dispatched uniformly | [`tool-platform-plan.md`](tool-platform-plan.md) |
| **Sources** | a music/audio source | enters the real-time fan-in topology; touches the hot Rust path, mux arbitration, and the loud-output safety chain | [`HANDOFF-source-capabilities.md`](HANDOFF-source-capabilities.md), [`audio-paths.md`](audio-paths.md) |
| **Model providers** | a swappable LLM backend | interchangeable implementation behind one narrow interface (realtime `LiveConnection` for voice; a simpler request→text layer for background work) | [`HANDOFF-voice-providers.md`](HANDOFF-voice-providers.md), [`research-tool-plan.md`](research-tool-plan.md) |
| **Hardware profiles** | a pure-data profile | a hardware variant whose *presence is dynamic*; resolved by a single-writer reconciler on boot/hotplug | [`HANDOFF-audio-capability-platform.md`](HANDOFF-audio-capability-platform.md), [`AGENTS.md` Config ownership](../AGENTS.md#config-ownership--which-pattern-for-a-new-dac--mic--provider--city) |
| **Features** *(new — §4)* | a cross-layer vertical | composes several of the above *and* owns its own user surface (a web page, a store, background work, proactive speech) | [`research-tool-plan.md`](research-tool-plan.md), [`HANDOFF-calibration-agent.md`](HANDOFF-calibration-agent.md) *(its first two instances)* |

The first four are mature and shipped to varying degrees. The fifth — the
**Feature** — is the one with no agreed contract yet, and it is the gap this
doctrine most exists to close.

---

## 4. The Feature contract (the new one)

A **Feature** is a self-contained vertical that the host *composes*. Today
JTS has two forming (the async "research" feature and the calibration agent)
and a third arriving (the conversation-history page). Each currently
reinvents the same plumbing — its own store, its own LLM-provider layer, its
own web surface, its own background-work and proactive-speech hooks. That
duplication is the signal that the shape is real.

The answer is the **Django-app / Backstage-plugin / Rails-engine** model:
**the Feature *declares* its contributions; the host *owns and injects* the
shared facilities.** This is what structurally prevents both failure modes —
the god-object (a Feature can't accumulate plumbing it doesn't contain) and
the scatter (each Feature stops re-implementing storage/web/scheduling).

A Feature **declares**:
- a **tool pack** (reuse the Tools contract — the Feature just points at it),
- a **store** requirement (a schema/migrations the host runs),
- a **web surface** (handlers/templates the host mounts at a host-assigned route),
- **background work** (jobs + intervals the host schedules),
- **proactive-speech hooks** ("I may want to speak when X"; the host decides if/when).

The **host owns and injects** (a Feature never builds these itself):
- the **storage substrate** — one host-created, host-migrated SQLite db per
  Feature (the `ResearchJobStore` shape is the seed),
- the **web mount** — the existing wizard server + `canonical_page`,
- **one shared scheduler / worker pool** — Features do **not** spawn their
  own threads (on a 1 GB Pi that is a memory-safety requirement, not tidiness),
- **one shared text-LLM-provider facility** — Features do **not** each build
  their own (research and the calibration agent currently do; that is the
  duplication to collapse),
- **voice/audio arbitration and the safety/duck guards**,
- **secrets and account/credential lifecycles**.

A Feature **inherits, as obligations** (non-negotiable): no-silent-failure →
audible cue (a proactive Feature that fails must speak), a `/state` section, a
`doctor` check, and mic-mute / privacy gating.

**Build discipline (this matters):** do **not** build a generic Feature
framework speculatively. Build the next instance —
[`conversation-history`](research-tool-plan.md) is the chosen first deliberate
Feature — *concretely*, extracting each shared host facility (a storage
helper, a web-mount helper, a scheduler helper) only when a **second** caller
needs it. Let the *contract* crystallize last. Even Backstage got its
composition contract right only on the second iteration, by evolving it.

> Status: the Feature contract is **not built yet**. `conversation-history`
> is its first proving instance; the helpers get extracted from what
> `research` and the calibration agent already duplicate.

---

## 5. The decision tree

One front door, then the existing pattern selector. Default conservative.

**Step 1 — what *kind* is this?** (ask in order)
1. Does the host need genuinely **new dispatch / lifecycle / policy logic** to
   handle it? → it's a **new contract** (rare; the four+one above should cover
   almost everything).
2. Can it be a **new data entry in an existing registry**, read by existing
   host code? → it's a **sub-type / registry entry** (the default — this
   should win most arguments).
3. Does it **compose existing contracts and own its own surface**? → it's a
   **Feature** (§4).
4. Is there only **one** real instance so far? → **do not** create a contract
   or abstraction; build it concretely and duplicate if needed.
5. Would publishing this contract create a **stability burden you can't
   afford** while the only consumer is still you? → keep it internal and
   unpublished until a reviewed contributor actually needs it.

**Step 2 — once you know it's a config-bearing subsystem, which ownership
pattern?** → the three-pattern table in
[`AGENTS.md` "Config ownership"](../AGENTS.md#config-ownership--which-pattern-for-a-new-dac--mic--provider--city)
(central typed `Config` / self-contained module + registry / pure-data
registry + reconciler). That table is the single source of truth for the
selector; this doctrine does not duplicate it.

---

## 6. Trust gradient — build now vs defer

Near-term reality: **Phase 2 of the trust gradient — trusted contributors
send PRs you review and run yourself.** They stay trusted the same way your
own code is, so they need *review discipline and good fault isolation*, **not
a sandbox**. The full phasing (Phase 1 "just me" → Phase 2 "trusted PRs" →
Phase 3 "untrusted at scale") and the per-trigger deferral catalogue live in
[`tool-platform-plan.md`](tool-platform-plan.md) §§1, 6 — this doctrine does
not restate them.

**Adopt now** (foundational regardless of trust — each pays off for *your own*
code immediately):
- the **host-mediated indirection invariant** (§1);
- **declarative metadata separable from the executor** (host reads data, runs
  runtime lazily);
- **host-owned lifecycle + cleanup** (the author registers a resource through
  a host helper; the host tears it down — authors forget);
- **per-entry fault isolation**;
- the **observability obligations** (`/state`, `doctor`, cue);
- a **reserved-but-UNENFORCED `capabilities`/risk field** in each contract's
  metadata — documentation-only today; the cheap half of forward-compat (a
  reserved field, *not* a versioning framework). Tools already carry risk
  flags; the others should carry the field even though nothing reads it yet;
- **host-injected shared facilities** (§4) — the 1 GB RAM ceiling makes this a
  requirement, not a nicety: a Feature/tool that spawns its own worker pool or
  loads its own model is a self-inflicted OOM.

**Defer until untrusted, out-of-tree authors actually exist** (Phase 3 — may
never arrive; each gated by its own trigger in
[`tool-platform-plan.md`](tool-platform-plan.md) §6):
- process/VM **sandboxing**;
- permission **enforcement** / capability brokering;
- a **secret-broker daemon** (beyond simple host-stored credentials);
- **signing / anti-tamper** and a curated **install-from-store marketplace**.

Building any of these now is paying the untrusted-code tax before a single
untrusted author exists — a bank vault before the lemonade stand.

---

## 7. Anti-patterns this doctrine prevents

- **Leaky boundary** (an extension holding a direct host reference) — the
  expensive one to claw back (Salesforce). Enforce §1 everywhere.
- **Premature / over-generalized abstraction** — abstract on the *second*
  instance, never the first; duplication is cheaper than the wrong
  abstraction. This is the dominant risk for a single-author system.
- **God-object verticals** — prevented structurally by §4's declare-don't-
  contain: a Feature receives injected plumbing, it never owns it.
- **Hook soup / free-form extensibility** — keep contracts closed and
  purpose-built; do not add a single generic `register_anything()`.
- **The public-API tax** — every observable behavior becomes a dependency
  (Hyrum's Law); breaking changes become migrations you must finance (Chrome
  MV2→MV3, Obsidian). Defense: few, narrow contracts, and don't publish a
  contract before a contributor needs it.

---

## Appendix — prior art (the "why")

Distilled from a deep-research pass (2026-06-19) over how mature ecosystems
draw these boundaries. Kept here as rationale, not operational truth.

| System | The principle JTS borrows |
|---|---|
| **Figma** | Indirection makes the sandbox swappable; plugins hold opaque handles, never host object references (the §1 invariant, proven). |
| **VS Code** | One packaging envelope, many purpose-built, statically-declared contribution points; Workspace Trust retrofitted as a *declarative* opt-in. |
| **Home Assistant** | The host owns the lifecycle, the shared polling coordinator, and OAuth (Application Credentials); the author implements only the domain fetch. Setup vs reconfigure are distinct flows. |
| **Django apps / Rails engines / Backstage** | The Feature-vertical answer (§4): the vertical declares models/views/jobs; the framework owns and injects the ORM, migration runner, web mount, and scheduler. |
| **CLAP / LV2** | Real-time safety is an un-routable stage; the host owns the threads and pushes expensive/variable work off the audio path. Static metadata discoverable without running code. |
| **Kubernetes operators** | The reconciler generalizes to *all* dynamic-presence problems (hardware hotplug, an OAuth token expiring, a provider going offline): level-triggered, idempotent, desired-vs-actual, finalizers for cleanup. |
| **Cautionary** | **Salesforce Lightning Locker** (raw refs → breaking migration), **Chrome MV2→MV3** (the cost of a public API ∝ author count), **WordPress** (hook soup + backwards-compat ossification), **Obsidian** (an unstable API and the per-update breakage tax). |

---

Last verified: 2026-06-19
