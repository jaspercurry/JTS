# ADR-0338: Tools share one boundary and defer untrusted distribution

- **Date:** 2026-09-22
- **Status:** Accepted

## Context

The tool-platform plan held both the shared tool boundary and unbuilt trust
and distribution choices. Retiring the roadmap must not turn future choices
into claims about shipped behavior. This record retains the decisions;
[extensibility.md](../extensibility.md#tools) and the on-device
`/assistant/tools/guide/` ([source](../../jasper/web/tools_setup.py)) own the
current extension and authoring contracts. A small pointer stays at the old
plan path so frozen records retain a valid link. No handoff tier is restored
([ADR-0199](0199-the-handoff-doc-corpus-is-deleted.md)).

## Decision

### One source-neutral boundary

First-party, reviewed contributor, and future generated tools must use the
same `ToolDefinition` + `ToolExecutor`, pack registry, provider serializers,
catalog, and `dispatch_tool()` path. A decorator is an authoring convenience,
not a second runtime contract. A capability pack is the copyable unit: it can
own setup, clients, provider registries, caches, and several tools. Do not
force a deep integration such as transit into a single-function shape.

The host injects shared services and owns dispatch, timeout, logging,
redaction, scalar wrapping, errors, and call/completion observation. Unknown
names are not registered calls. Future HTTP, MCP, sandbox, or async-job
execution must cross that same boundary. Adding a family belongs in a pack,
not daemon registration branches, provider adapters, or central `Config`.
First-party packs are the reference implementations; further migration across
simple, API-backed, source-backed, transit, and consequential-action examples
must preserve schemas, manifests, catalog payloads, dispatch, and order unless
a behavior change is explicit. Use local registry comparisons for refactors;
do not replace them with paid voice-eval loops.

Keep rich human descriptions separate from short `llm_description` prompts.
Labels and risk flags are metadata, not extra provider prompt text or a
permission boundary. Examples help distinguish confusable tools. The core
must fit its supported Pi memory budget; heavier optional tools require
appropriate hardware, not a larger resident core.

### Catalog and household choices

The built-in catalog is a pack-first manager, not an install marketplace.
Display packs and categories organize tools; they do not define runtime
sources or voice-provider packages. Several internal packs may share one
display pack, and a standalone tool gets a generated singleton display pack.
Pack setup is shared; child controls and full prompt/schema/metadata details
belong on the generated pack detail page. Cards must not change hierarchy or
width because one pack has more children.

Keep pack and tool disabled sets separate: effective disabled state is pack
disabled OR tool disabled, preserving child choices across pack toggles.
Mixed child state is shown as partial. A deliberate disable removes the tool
from declarations; it is not a response-blocking fault requiring a cue.

Stage toggles and full prompt overrides without restarting voice. Explicit
Apply performs one restart, rate-limited, and reports when it cannot restart.
Restarting on each edit would interrupt conversations and could feed the
service's reboot escalation. The wizard reads catalog JSON and a live settings
overlay; it never imports tool modules or builds the registry.

Prompt editing is advanced and at the user's risk. Keep the immutable code
default separate from the override; Edit replaces the full model prompt, and
Reset deletes the override. Do not add a custom-addendum layer. Atomically
store non-secret overrides outside the repo in wizard-owned state. Missing
or malformed settings fall back to no disables and code-default prompts.
Use the same effective prompt in serializers, manifests, and catalog; expose
default/customized state and prompt length so editing and reset stay clear.
A dedicated doctor warning for overrides remains conditional on actual use.

### Trust and distribution remain triggered work

The trust phases are choices about review, not separate runtime types:

1. **First-party:** trusted code in this repo proves the shared boundary.
2. **Trusted PRs:** when contributors need it, review and run their packs.
   Retain the same in-process runtime. Deferred contribution tooling includes
   an authorable/parseable pack manifest, local runtime modules where needed,
   light CI for manifest validity, clear descriptions, declared risks,
   parseable examples, no obfuscation, and regression-scenario coverage.
   Contribution guidance covers pack boundaries, prompt/failure contracts,
   setup ownership, and audible response-blocking failures.
3. **Untrusted distribution:** only when running code the maintainer has not
   vetted, or when author volume exceeds personal review, consider the
   machinery below. None is authorized merely by retiring the plan.

| Deferred capability | Trigger / retained choice |
|---|---|
| Static tool scoping | Instructions + tools near the original 12–13K-token planning threshold, or measured mis-selection; select at connection open and reconnect to broaden. Recheck provider limits before relying on the threshold. |
| Local embedding pre-filter | Tens/hundreds of installed tools; keep full schemas and avoid an extra model round trip. Model2Vec was a candidate, not an adopted dependency. |
| Description disambiguation and deterministic ordering pass | Around 100 overlapping tools; verify accuracy on voice rather than treating text-model tool counts as a law. |
| Curated index, distribution tiers, fail-closed CI | Third-party volume exceeds personal review. |
| Process sandbox | Unvetted code: `systemd-run` with a resource-capped `jts-tools.slice`, unprivileged-userns bubblewrap and seccomp is the selected direction; verify user namespaces on the deployed OS first. |
| Safe boot for tools | Installable tools can crash the speaker; extend the existing bootloop-guard path. |
| Capability enforcement and secret broker | Untrusted tools reach secrets, network, or smart-home actions; inject a backend capability, not a raw key. |
| Hash-pinning, re-consent, signing/anti-tamper, kill-list | Out-of-band changes after review; approval must not silently cover changed code/descriptions. |
| Per-plugin spend metering | Tools make paid API/LLM calls. |
| MCP bridge | A concrete power-user need for external MCP servers, on suitable hardware. |
| Encryption at rest | Multi-user or off-LAN exposure; use passphrase-derived Argon2id with escrowed recovery codes, never a device-bound key or world-readable `peer_id`. |

Declare all enabled/provider-compatible tools until measured pressure warrants
scoping. Reject an in-band router meta-tool: hiding parameter schemas and
adding a model round trip buys little for short voice interactions. The old
reconnect-cost estimates and provider token limits were planning assumptions,
not guarantees for every current adapter. WASM/Extism is only a possible
pure-compute sandbox (not C-extension Python); Firecracker was rejected for
the Pi's GICv2 platform. Plaintext credentials with restricted file permissions
fit the household-LAN model; SD re-image must not strand encrypted credentials.

City is taxonomy, not a new runtime container. Keep the current `CityPack` /
`JASPER_TRANSIT_CITIES` until both a tool-store UI with filtering/per-tool
enablement and a second city require the change. Then use city labels and
configured-implies-enabled provider gates, with one owner for enablement.
Tool creation should start within a pack through reviewed repository changes.
An in-browser executable builder needs the untrusted-code boundary; a future
declarative builder may instead target an already-supported safe executor.

Later work still uses registry-owned configuration, flat doctor checks (reserve
an order band when platform checks exist), generic audible cues for failures
that block responses, and [AGENTS.md](../../AGENTS.md) for acceptance and tests.
No sandbox, secret broker, marketplace, HTTP/MCP executor, scoping model, or
generic Feature framework is added by this decision.

## Consequences

At `04a2a9bc949223079dc8d39371e7c9cd16cc1744`,
[tool definitions and dispatch](../../jasper/tools/__init__.py),
[packs](../../jasper/tools/packs.py), and the
[catalog wizard and authoring guide](../../jasper/web/tools_setup.py) implement
the current boundary. Deferred trust/distribution work is not shipped behavior.
Future work rechecks code and its trigger rather than following a dated phase
checklist. Existing ADRs and frozen audit/history records remain unchanged.
