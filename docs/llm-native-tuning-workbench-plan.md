# LLM-native tuning workbench — design and execution plan

> **Status: proposed direction (2026-07-27).** This is the current planning
> authority for the agent-assisted tuning workbench. It supersedes the
> prescriptive schema/lexicon approach in
> [`tuning-bench-design.md`](tuning-bench-design.md) and
> [`tuning-bench-execution-plan.md`](tuning-bench-execution-plan.md).
> No implementation ships from this document.

## 1. Outcome

Build a **workbench for an increasingly capable LLM**, not a second acoustic
expert system beside it.

The workbench makes JTS's existing measurement, evidence, and DSP machinery
easy for a laptop-side Claude/Codex session to discover and use. It gives the
model:

- a factual description of the speaker, active DSP graph, tuning layers, and
  current artifacts;
- a catalog of measurements and actions the repository can already perform;
- structured, provenance-rich evidence produced by those capabilities;
- a reversible workspace for proposing, validating, applying, measuring, and
  restoring an experimental DSP configuration.

The model decides what the evidence means, which test would distinguish its
hypotheses, and what experiment to propose. The host performs every powerful
operation and keeps the hardware-safe boundary.

The end-to-end user promise is:

> Tell the agent what you hear. Let it inspect the whole tuning stack, gather
> the evidence it needs, propose and explain a temporary cross-layer
> experiment, verify the result, and return the speaker exactly to its prior
> state.

## 2. Why this shape

The 2026-07-27 JTS3/iLoud session proved the loop:

1. preserve the user's words;
2. level-match and measure;
3. distinguish plausible causes with evidence;
4. apply a temporary experiment;
5. listen and re-measure;
6. restore and verify.

What made that session valuable was not a private dictionary saying that
"dull" means one frequency band. It was access to good instruments, knowledge
of the system's layer boundaries, and a disciplined experimental lifecycle.

LLMs already bring broad acoustic knowledge and reasoning, and that capability
will improve. Hardcoded interpretation rules depreciate as models improve.
Clean measurements, explicit provenance, safe mutation primitives, and
single-source-of-truth context become more useful.

Therefore:

- **Encode facts, not conclusions.**
- **Expose capabilities, not a prescribed diagnostic script.**
- **Link evidence, do not flatten every domain into a second schema.**
- **Let experiments cross tuning layers, but never cross the hardware-safety
  boundary.**
- **Keep the model behind host-mediated indirection.**

This is the cross-layer Feature shape from
[`extensibility.md`](extensibility.md): declarations are inspectable as data;
the host owns dispatch, lifecycle, cleanup, and safety. It is not permission
to build a generic plugin framework or a free-form `register_anything()` hook.

## 3. Responsibility boundary

### 3.1 Deterministic host responsibilities

JTS owns:

- capturing samples and generating stimuli;
- computing deterministic measurement products;
- reading current topology, configuration, and artifact state;
- identifying the physical geometry and conditions of a measurement;
- recording timestamps, units, calibration identity, validity limits, and
  configuration fingerprints;
- resolving and dispatching declared capabilities;
- creating candidate diffs and running structural validation;
- taking/releasing mux and voice measurement leases;
- applying, bypassing, restoring, and verifying experiments;
- enforcing non-negotiable hardware constraints;
- producing stable logs and recovery instructions.

### 3.2 LLM responsibilities

The model owns:

- interpreting the user's natural-language description;
- choosing which available capability to call;
- forming and revising hypotheses across layers;
- deciding whether existing evidence is sufficient;
- comparing measurements and identifying uncertainty;
- proposing a candidate experiment;
- explaining the proposed change and its tradeoffs;
- choosing the next useful test from the result;
- distinguishing measured facts from inference in its report.

### 3.3 What must not become executable policy

Do not build:

- a required mapping such as `muddy -> 100–300 Hz`;
- a fixed decision tree for which discriminator follows which adjective;
- a rule requiring every user word to resolve to one coded cause;
- an algorithmic confidence verdict derived from the model's prose;
- a layer-specific action taxonomy duplicated inside an agent package;
- a prompt-sized copy of every subsystem's implementation knowledge.

The user's exact words should be recorded verbatim. Optional acoustic glossary
material may be offered to a model as ordinary reference context, but it is not
a schema, validator, or source of truth.

## 4. The factual layer model

The workbench consumes—not redefines—the adopted layer architecture in
[`active-speaker-tuning-layers-design.md`](active-speaker-tuning-layers-design.md).

| Layer | Existing owner | Evidence that can inform it |
|---|---|---|
| Driver linearization | Active-speaker commissioned profile | Per-driver gated far-field response; valid woofer near-field supplement; repeatability, linearity, distortion |
| Crossover integration | Active-speaker crossover commissioning | Per-driver and summed gated response; relative level, delay, polarity, phase/summation evidence |
| Bass management | Bass-extension and sub-integration owners | Near-field LF evidence where valid; in-room low-frequency response; compression/excursion evidence |
| Room correction | Room-correction owner | Listening-position and spatial-cloud in-room measurements; placement and reflection evidence |
| Preference EQ | Sound-profile owner | User intent and listening comparison, optionally informed by any of the above |

This table is context for reasoning, not an ownership bypass. A permanent
change still goes through the layer that owns its durable source of truth.

### 4.1 Measurement geometry is a fact

The workbench records what was measured without claiming what the result means.
The minimum shared vocabulary is:

- `driver_near_field` — one driver's close-mic response within the geometry's
  declared validity range;
- `gated_far_field` — direct-sound response on a declared axis, with the gate
  and resulting low-frequency validity floor;
- `summed_far_field` — the integrated speaker response on a declared axis;
- `listening_position` — ungated/in-room evidence at one seat;
- `spatial_cloud` — a declared set of room or direct-sound positions and its
  combining method;
- `configuration` — graph, filter, topology, or commissioning evidence with no
  acoustic capture.

Every acoustic artifact carries structured geometry details appropriate to its
kind, such as distance, axis, driver/target, position label, gate, and
environment. The vocabulary can grow only when a real instrument needs a new
physical distinction.

The host may state a measurement's mathematical validity—such as a gated
response being invalid below `1 / gate_seconds`. It does not turn that fact
into a diagnosis.

## 5. Architecture

The workbench adds four small pieces. Everything else is an adapter to an
existing owner.

### 5.1 Canonical tool catalog and call surface

The workbench is a Feature that contributes a `CapabilityPack` through JTS's
existing Tools contract in
[`tool-platform-plan.md`](tool-platform-plan.md). It does **not** define a
second capability descriptor, registry, executor, or dispatcher.

Each operation is one canonical `ToolDefinition` plus `ToolExecutor`, collected
by `ToolRegistry` and invoked only through `dispatch_tool()`. The existing
derived manifest already gives a model the operation's name, description,
input schema, labels, timeout, compatibility, and risk flags without running
the executor. The host builds a workbench-scoped `ToolRegistry` instance from
the pack; reuse of the contract does not automatically expose commissioning
operations to the embedded voice assistant's registry. Workbench adapters
receive only host-injected, operation-scoped services; they never hold a
`CamillaController`, filesystem handle, daemon object, or other powerful host
object.

Current availability and precondition failures are live context facts joined
to the canonical tool name, not a parallel declaration table. If a real
workbench operation proves the Tools contract lacks generally useful metadata,
extend `ToolDefinition` once and let every catalog/serializer consume it.
Do not create a workbench-only copy.

The laptop-side model needs a stable transport as well as a catalog:

- `jasper-tuning-workbench tools --json` emits the workbench-scoped derived
  manifest;
- `jasper-tuning-workbench call <tool-name> --input <json-file|->` parses an
  input object, applies workbench approval/lifecycle policy, then routes
  through `dispatch_tool()`;
- `jasper-tuning-workbench context --json` emits the bounded live context
  packet described below.

Sound-emitting and mutating operations are consequential. Their executors
remain unreachable until the workbench records the required user approval and
opens the relevant measurement or experiment lifecycle. The CLI is a
transport, not an alternate dispatch or safety path.

The initial pack is built from concrete existing capabilities. Candidate first
entries include:

- inspect active topology/configuration/layer artifacts;
- inspect a correction or commissioning evidence bundle;
- capture a calibrated response through an existing measurement path;
- compare referenced responses on a declared grid;
- inspect/bypass an existing tuning layer where its owner supports that;
- operate the experiment workspace described below.

Adding an adapter should require one tool definition/executor beside the
subsystem that owns the operation and one pack contribution. It must not
require edits to a second handwritten prompt list, a universal analysis switch
statement, and a parallel documentation table.

### 5.2 Context assembler

One read-only command builds a bounded context packet for the model:

- speaker identity (redacted where appropriate);
- output topology and active graph identity;
- current layer/artifact inventory with fingerprints;
- current listening/source/volume mode facts relevant to an experiment;
- available canonical tool manifest entries;
- current workbench session state;
- an index of existing evidence artifacts;
- warnings or unavailable capabilities with reasons.

The packet carries summaries and references, not every capture array or entire
historical report. The model opens artifacts lazily when they become relevant.
This keeps context useful as the repository grows.

The assembler reads each subsystem's canonical state. It must not infer layer
state from filenames when a domain-owned reader exists, and it must not cache
mutable runtime truth in a long-lived process.

### 5.3 Neutral manifests and domain-owned payloads

Do not force crossover, linearization, bass, room, and preference data into one
giant canonical payload. They have legitimately different contracts and
owners.

There is no new workbench artifact envelope. A workbench session is an ordinary
neutral evidence bundle using `jasper.audio_measurement.bundles`. Every local
artifact is recorded through `record_artifact()` or `write_json_artifact()` and
therefore receives the canonical `ArtifactEntry` identity and integrity fields:

```json
{
  "path": "measurements/response.json",
  "kind": "measurement.frequency_response",
  "sensitivity": "derived",
  "recomputable": true,
  "sha256": "...",
  "byte_size": 12345,
  "recorded_at": 1785196800.0,
  "generated_by": "tuning_workbench.measure_response",
  "dependencies": ["stimulus.json", "mic-calibration.txt"],
  "schema_version": 1,
  "metadata": {
    "speaker_id_hash": "...",
    "config_fingerprint": "...",
    "topology_fingerprint": "...",
    "geometry": {"kind": "gated_far_field"},
    "valid_frequency_hz": [180.0, 16000.0]
  }
}
```

The exact domain payload remains the original:

- room bundles remain room bundles;
- crossover evidence remains `CommissioningEvidenceStore` evidence;
- active-speaker profiles remain active-speaker artifacts;
- measurement arrays remain in their existing bundle format.

An existing domain bundle is reopened through its own manifest, not translated
into a workbench schema. A cross-bundle session reference records the owning
bundle identity plus the artifact's normalized path, schema version, and
SHA-256; the loader rechecks the digest. A mutable relative path by itself is
never durable provenance.

If a capability produces genuinely new evidence, its owning subsystem defines
that payload. The workbench owns only session indexing and workbench-specific
provenance placed in the existing manifest metadata.

### 5.4 Session manifest

A workbench session is a graph of references, not a duplicate analysis
database. Its manifest records:

- session id and timestamps;
- the user's words verbatim and when they were captured;
- context snapshot references;
- capability calls and their inputs/results;
- artifact references;
- candidate and validation references;
- explicit user approvals;
- apply/bypass/restore transitions;
- final restoration proof;
- optional free-form model notes clearly labeled as model-generated inference.

The manifest is append-oriented and serialized through the existing neutral
bundle machinery. Raw microphone audio keeps the repository's private-audio
sensitivity classification.

### 5.5 Reversible experiment workspace

The experiment workspace is the one new mutation owner. It does not decide
what to tune.

It provides:

- `open` — snapshot the live configuration, volume/source mode, topology,
  layer artifacts, fingerprints, and persistence anchor;
- `candidate` — accept a full experimental CamillaDSP configuration or a
  candidate emitted by existing layer-owned tools, always at an
  experiment-only path;
- `diff` — show the full graph diff plus best-effort layer annotations;
- `validate` — run CamillaDSP validation and JTS structural/safety checks;
- `apply` — transition through the host-owned safe graph-change guard, load
  the candidate without changing canonical intent, prove the live path is
  healthy, then ramp back to the admitted listening level;
- `bypass` — use the same guarded transition to switch between candidate and
  baseline for listening comparison;
- `status` — report live path/raw-config identity, persistence anchor, volume
  mode, drift, and restoration state;
- `revert` — restore the exact baseline and remove temporary artifacts;
- `verify-restore` — prove configuration, volume, topology, and layer
  fingerprints match the snapshot;
- `close` — refuse unless restoration is verified or the user explicitly
  requests a documented hold.

#### Cross-layer candidates

A candidate may change several tuning layers at once. The LLM can therefore
test a complete explanation rather than being forced into a preference-EQ-only
box.

That flexibility is temporary by design:

- the candidate never overwrites a canonical config;
- it never mutates the durable source-of-truth artifacts for a layer;
- a user-approved permanent change is translated into calls to the relevant
  layer owners after the experiment, not committed by copying experimental
  YAML over generated state;
- the manifest preserves the candidate and diff so the permanent proposal can
  be reproduced and reviewed.

#### Non-negotiable host validation

User trust authorizes an experiment; it does not bypass hardware safety. The
host refuses a candidate that:

- changes output devices, channel roles, or hardware routing outside an
  explicit commissioning capability;
- removes or weakens required driver protection, limiters, or the configured
  output ceiling;
- creates dangling pipeline references or an invalid graph;
- violates the active topology/runtime contract;
- exceeds admitted excitation or headroom limits;
- exceeds bounded graph/resource limits for the target Pi;
- cannot name and prove a restorable baseline.

Layer-owned commissioning capabilities may deliberately change crossover,
delay, polarity, or protection parameters, but they bring their own stronger
admission contract. The generic experiment workspace does not turn a raw YAML
edit into commissioning authority.

#### Live transition, persistence, and volume are explicit resources

The first tuning-bench plan incorrectly treated these details as implementation
footnotes. They are acceptance contracts:

1. **A valid file is not yet healthy audio.** Apply and bypass use a
   host-owned attenuation/mute-and-ramp transition around the serialized graph
   change. After load, the host confirms raw config identity, waits a bounded
   settle window, and proves output-pipeline liveness plus admitted xrun,
   clipping, unexpected-silence-under-probe, and CPU/headroom conditions.
   Failed or ambiguous health keeps output attenuated and triggers automatic
   exact-baseline restore.
2. **Temporary means restart-safe.** Applying a candidate must not silently
   replace CamillaDSP's durable boot anchor. A restart either restores the
   snapshotted baseline or produces an explicit recoverable state; it never
   makes an experiment accidentally permanent.
3. **Listening level is not always Camilla master volume.** Spotify and
   Bluetooth carry household level at the source. An experiment must inspect
   volume mode and may not pretend `/volume/set` always changes Camilla.

Prefer keeping comparison level matching in the stimulus and keeping the
household level unchanged. If an experiment truly needs a temporary volume
resource, it must acquire a host-owned guard, record requested and realized
values, verify every transition before changing the graph, and restore through
the same coordinator. Config and volume restoration must be fault-injection
tested together.

## 6. Reuse and ownership

The workbench composes current owners through their public boundaries.

| Concern | Canonical owner the workbench consumes |
|---|---|
| Sweep/deconvolution/gating/calibration/analysis primitives | `jasper/audio_measurement/` |
| Layer ordering and measurement meaning | `docs/active-speaker-tuning-layers-design.md` plus each layer's operational doc |
| Active topology and driver safety | `jasper/active_speaker/` runtime contract and evidence |
| Preference and room graph recomposition | `jasper/sound/graph_carrier.py` and the owning emitters |
| LLM-facing declarations, manifests, and dispatch | `jasper/tools/` (`CapabilityPack`, `ToolDefinition`, `ToolExecutor`, `ToolRegistry`, `dispatch_tool`) |
| DSP mutation serialization/rollback | `jasper/dsp_apply.py` |
| Product measurement isolation | `jasper/correction/coordinator.py` |
| Neutral artifact manifests | `jasper/audio_measurement/bundles.py` |
| Commissioning evidence identity and reopening | `CommissioningEvidenceStore` |
| Runtime/household state | daemon `/state` and domain-owned state readers |

The workbench newly owns only:

- its concrete tool-pack adapters and laptop-side transport into the canonical
  dispatch boundary;
- the bounded context assembler;
- its session bundle index and workbench-specific provenance metadata;
- the temporary experiment ledger and lifecycle.

It does **not** own:

- new DSP math;
- a second tool registry, manifest, or dispatch path;
- a duplicate graph compiler;
- a universal copy of all domain schemas;
- permanent layer configuration;
- acoustic-language interpretation;
- a second tool implementation when an existing public boundary suffices.

When an existing subsystem lacks a narrow callable boundary, add that boundary
to the owner and adapt it. Do not import its private web handler or copy its
logic into the workbench.

## 7. Agent interaction model

The initial model is the laptop-side Claude/Codex session. There is no new
model client, response schema, spend ledger, or in-speaker agent in v1.

The agent receives:

1. a short protocol describing safety, user approval, and evidence honesty;
2. the current context packet;
3. capability schemas;
4. artifact references it can open on demand.

Prompt guidance should remain thin:

- capture the user's description before presenting measurements when practical;
- label statements as measured, inferred, or proposed;
- do not claim below/above an instrument's validity;
- level-match before comparative tonal claims;
- explain and obtain approval before sound or mutation;
- prefer one discriminating experiment over a large speculative change;
- close with verified restoration unless the user explicitly requests a hold.

Those are experimental-discipline rules. They do not tell the model what
"muddy" means or which layer must be blamed.

## 8. Example user journey

1. The user says, "JTS sounds muddy and the vocals feel farther away than the
   reference speaker."
2. The agent reads the context packet. It sees the active linearization,
   crossover, bass, room, and preference artifacts and the measurements/tools
   currently available.
3. It preserves the user's words and inspects relevant existing evidence.
4. It decides whether a new gated, near-field, or listening-position
   measurement would separate its hypotheses and explains why.
5. The host performs the approved measurement and returns an artifact with
   geometry, configuration fingerprint, calibration, and validity.
6. The agent interprets the evidence. No coded dictionary is consulted.
7. It proposes a temporary candidate that may span more than one layer,
   explains the graph diff, and states what result would support or weaken the
   hypothesis.
8. The host validates the candidate; the user approves; the workspace applies
   it without changing canonical intent.
9. The user listens and the agent optionally repeats the measurements.
10. The agent refines, recommends a permanent layer-owned change, or concludes
    that the evidence did not support the hypothesis.
11. The workspace restores and proves the original state.

## 9. Execution ladder

Each rung is independently useful and begins from current `origin/main`.
Behavior changes require tests in the same PR. Hardware claims require named
hardware evidence; Linux CI remains the full-suite authority.

### PR-W1 — read-only context and canonical tool pack

Ship:

- a small `jasper/tuning_workbench/` package;
- a `CapabilityPack` of initial read-only `ToolDefinition`/`ToolExecutor`
  adapters over existing owners;
- the canonical `ToolRegistry`/`dispatch_tool()` path with per-pack and
  per-tool fault isolation;
- `jasper-tuning-workbench tools --json`, `context --json`, and
  `call <tool-name> --input <json-file|->`;
- current topology, active graph, layer-artifact inventory, source/volume mode,
  capability availability, and evidence index;
- stable `event=` logs for unavailable or failed capability reads.

Acceptance:

- no sound and no mutation;
- pack/definition import is lightweight;
- one broken adapter appears as unavailable without losing the rest of the
  packet;
- every context fact is traced to a domain-owned reader;
- adding a second fixture tool requires no prompt, context-assembler branch,
  or second registry/dispatcher.

### PR-W2 — neutral evidence manifests and measurement adapters

Ship:

- workbench session bundles built on the neutral artifact manifest;
- adapters over existing calibrated capture/analysis paths;
- mux test-lease and voice measurement-window reuse;
- explicit geometry, stimulus, mic calibration, config fingerprint, validity,
  units, and sensitivity on every new measurement;
- lazy artifact inspection from the context packet.

Start with the minimum hardware-proven path needed to reproduce the
JTS3/reference comparison. Add other transports only when a real session needs
them.

Acceptance:

- no copied DSP math;
- exact single-channel stimulus invariant where the rig requires it;
- contaminated/missing/silent capture refuses with actionable output;
- domain payloads remain domain-owned;
- a hardware-free fixture round-trips the neutral manifest, verifies its
  digest, and reopens its domain payload;
- one owner-scheduled JTS3 smoke is documented before claiming the path works.

### PR-W3 — reversible cross-layer experiment workspace

Ship the `open/candidate/diff/validate/apply/bypass/status/revert/
verify-restore/close` lifecycle.

Before implementation, resolve the live-transition, persistence, and volume
contracts in §5.5 against the production graph-change seam, live CamillaDSP
statefile, output health surfaces, and `VolumeCoordinator`; do not carry
forward the earlier plan's assumptions.

Acceptance includes fault injection:

- candidate invalid;
- candidate changes after validation;
- config load response lost after landing;
- attenuation/mute transition cannot be proved;
- candidate loads but fails settle/liveness, xrun/clipping,
  unexpected-silence-under-probe, or CPU/headroom checks;
- process dies after apply;
- restart while candidate is active;
- Spotify/Bluetooth push-volume source active;
- competing DSP writer;
- user volume change during apply/revert;
- restore response lost after landing;
- canonical config changed legitimately while the experiment was open.

Every terminal state must be either verified restored, explicitly held, or
blocked with a recoverable command and visible doctor/status evidence. No
failure path may continue from "probably restored."

### PR-W4 — agent protocol and end-to-end proof

Ship:

- `docs/HANDOFF-tuning-workbench.md`, the short canonical operating protocol;
- a thin agent launcher/skill that loads the protocol and current context;
- worked examples that demonstrate model-chosen—not hardcoded—diagnostics;
- README/testing-tooling entries;
- an end-to-end JTS3/reference run:
  inspect → measure → reason → propose → validate → apply → listen/verify →
  restore.

The proof records the model and prompt version as provenance, but the
acceptance claim is model-agnostic: another capable session can discover the
same tools and complete the lifecycle without hand-written Python.

### Later adapters, not a separate platform rewrite

After W1–W4 prove the seam, add adapters when real work needs them:

- crossover commissioning evidence and safe actions;
- driver-linearization evidence and experiments;
- bass-extension evidence;
- room-correction bundles and spatial evidence;
- preference profiles;
- delta-probe or future instruments.

Each addition stays with its owner and becomes visible through the same
canonical tool/context/manifest boundaries.

## 10. Acceptance for the whole direction

The workbench is successful when:

1. A fresh capable LLM can discover what JTS can inspect, measure, and
   temporarily change without reading the whole repository.
2. The context packet describes the actual live speaker and links to canonical
   evidence without duplicating it.
3. The model—not executable lexicon code—chooses hypotheses and tests.
4. A candidate may span multiple tuning layers while canonical layer intent
   remains untouched.
5. The host shows a comprehensible diff, validates non-negotiable safety, and
   requires user approval.
6. Apply/bypass/revert survive transport ambiguity, unhealthy post-load audio,
   process death, source-mode differences, and restart.
7. Restoration is proven against the original snapshot.
8. A new real capability is registered once and appears automatically in model
   context.
9. No new DSP math or permanent configuration owner exists in the workbench.
10. Raw audio and identifying metadata retain explicit privacy handling.

## 11. Risks and countermeasures

| Risk | Countermeasure |
|---|---|
| Workbench tool pack becomes generic hook soup | Reuse the closed canonical Tools contract; add only concrete operations; no workbench-only descriptor or dispatcher |
| One universal schema duplicates every subsystem | Neutral `ArtifactEntry` manifests plus content-identified references to domain-owned payloads |
| Context grows beyond a useful model budget | Bounded summaries and lazy artifact reads |
| Model overstates acoustic conclusions | Instrument validity in data; measured/inferred/proposed prompt discipline; user-visible evidence references |
| Hardcoded prompt becomes another expert system | Keep prompts procedural and model-agnostic; no adjective or diagnosis rules |
| Cross-layer experiment bypasses durable owners | Experiment-only config; permanent changes translated back through layer capabilities |
| Trusted raw config harms hardware | Structural/protection/resource checks, guarded mute-and-ramp transition, post-load health proof, automatic restore, explicit commissioning authority for protected changes |
| Temporary config becomes permanent after restart | Persistence anchor is a tested lifecycle resource, not an assumption |
| Makeup gain changes Spotify/BT instead of Camilla | Inspect volume mode; prefer stimulus matching; guarded coordinator transaction when volume mutation is unavoidable |
| Session captures private household audio | Private sensitivity, local bundles, no raw audio in model context unless explicitly requested |
| Workbench grows a second implementation of existing DSP | Adapters call owner boundaries; review rejects copied math/emitters |

## 12. Explicitly superseded choices

The following choices from the 2026-07-27 tuning-bench design/execution
snapshot are not part of this direction:

- `lexicon.py` as executable adjective-to-band/cause mapping;
- a mandatory structured attribution verdict;
- one giant `jts_tuning_bench_analysis` payload spanning every domain;
- a fixed diagnostic/discriminator order;
- preference-EQ-shaped overlay limits as the definition of all experiments;
- widening the calibration agent's action taxonomy before the deterministic
  workbench seam is proven;
- a new implementation of any measurement math already owned by
  `jasper.audio_measurement`.

Useful evidence and operational traps recorded in those historical documents
remain valuable input during implementation. They are not current architecture.

## 13. Deferred

- An embedded, autonomous in-speaker tuning agent.
- Generic Feature-framework extraction beyond what a second real caller proves.
- Untrusted third-party capability sandboxing or permissions.
- Automatic promotion of an experiment into permanent layer state.
- A universal acoustic ontology.
- A marketplace or public workbench API.

The laptop-side agent and reviewed in-repo code are the present trust boundary.

## 14. Documentation ownership

This file owns the current architecture and execution direction.

When implementation begins:

- `docs/HANDOFF-tuning-workbench.md` becomes the concise operational truth;
- this plan remains the design/decision record;
- subsystem behavior continues to live in its existing canonical HANDOFF;
- `README.md` remains the atlas;
- `docs/doc-map.toml` routes affected measurement/DSP changes here until the
  HANDOFF ships, then routes operational changes to the HANDOFF as well.

Last verified: 2026-07-27
