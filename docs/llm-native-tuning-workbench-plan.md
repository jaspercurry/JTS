# LLM-native tuning workbench — design and execution plan

> **Status: proposed direction (2026-07-27).** This is the current planning
> authority for the agent-assisted tuning workbench. It supersedes the
> prescriptive schema/lexicon approach in
> [`tuning-bench-design.md`](tuning-bench-design.md) and
> [`tuning-bench-execution-plan.md`](tuning-bench-execution-plan.md).
> No implementation ships from this document.
>
> Amended 2026-07-27 with a verified-seam execution layer: every symbol,
> constant, and trap named in §5 and §9 was checked against the repository
> on that date by a dedicated four-pass code investigation. Seams can drift;
> an implementer re-verifies a seam before building on it, but starts from
> these names rather than re-deriving them.

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
  restoring an experimental DSP configuration;
- a session record that preserves the user's words and trial feedback
  verbatim, so a household's reactions become durable evidence for the next
  iteration.

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

#### The geometry vocabulary is new — map it, do not rename the owners

The kinds above are workbench vocabulary, not strings that exist in the
repository today. The current domain literals are:

| Existing literal | Owner | Maps to workbench kind |
|---|---|---|
| `capture_geometry == "near_field"` | `jasper/active_speaker/driver_acoustics.py` | `driver_near_field` |
| `capture_geometry == "reference_axis"` | same | `gated_far_field` (per-driver) or `summed_far_field` (summed capture) |
| `MicGeometry.LISTENING_POSITION` (`"listening_position"`) | `jasper/correction/level_match.py` | `listening_position` |
| `MicGeometry.NEAR_FIELD_DRIVER` (`"near_field_driver"`) | same | `driver_near_field` |
| position clouds (`combine_positions`, `_CloudPosition`) | `jasper/audio_measurement/spatial_combine.py`, `crossover_v2_flow.py` | `spatial_cloud` |

One workbench module (`jasper/tuning_workbench/geometry.py`) owns the kind
constants and this mapping; adapters translate at the boundary. Domain code
keeps its own literals — renaming them is out of scope and would break
recorded evidence.

`valid_frequency_hz` is derived, not read: the low edge comes from the
existing scalar validity floor
(`jasper.audio_measurement.gating.f_valid_floor_hz`, propagated as
`validity_floor_hz`); the high edge is the instrument/calibration limit
when one is declared, else `null`. No existing artifact stores a validity
range — the workbench computes it at record time and must not pretend the
domain payload carried it.

## 5. Architecture

The workbench adds four small pieces. Everything else is an adapter to an
existing owner.

### 5.0 Where the pieces run — decided

The superseded execution plan was explicit about process placement; this
plan keeps the same, repo-standard split. Treat this as decided:

- **The CLI runs on the Pi.** `jasper-tuning-workbench` is an installed
  console script (`pyproject.toml` `[project.scripts]` →
  `jasper.tuning_workbench.cli:main`), living in the same
  `/opt/jasper/.venv` runtime as `jasper-doctor`. Every fact it reports and
  every lock it takes is local to the speaker; running it elsewhere would
  recreate remote state guessing.
- **The laptop agent is an SSH client.** A Claude/Codex session drives the
  CLI with the existing transport convention — `PI_HOST`/`PI_USER` from
  `.env.local` (`scripts/_lib.sh`), e.g.
  `ssh pi@jts.local sudo /opt/jasper/.venv/bin/jasper-tuning-workbench
  context --json`. `tools`, `context`, and read-only calls must work
  without root; measurement and experiment verbs may require it because
  the owners they call already do (the DSP writer lock lives in the
  root-owned, `jasper`-group config dir).
- **Capture front-ends stay what they are.** Measurements ride the
  existing product capture machinery (browser/phone relay, calibrated
  UMIK through the browser path). The workbench does not add a second
  capture stack.
- **Analysis runs on the Pi**, exactly as the correction and crossover
  flows already do. Heavy imports stay lazy inside executors so
  `import jasper.tuning_workbench` and the socket-activated wizards stay
  light — the package ships in the Pi venv either way.
- **CLI conventions** mirror `jasper/cli/sound.py`: `argparse`
  subcommands, a per-subcommand `--json` flag, `main() -> int`, non-zero
  exit on failure. `call <tool-name> --input -` reads stdin (the `-`
  convention from `jasper/cli/route_latency_artifact.py`).

### 5.1 Canonical tool catalog and call surface

The workbench is a Feature that contributes a `CapabilityPack` through JTS's
existing Tools contract in
[`tool-platform-plan.md`](tool-platform-plan.md). It does **not** define a
second capability descriptor, registry, executor, or dispatcher.

Each operation is one canonical `ToolDefinition` plus `ToolExecutor`,
collected by `ToolRegistry` and invoked only through `dispatch_tool()`
(all in `jasper/tools/__init__.py`). The derived manifest
(`Tool.to_manifest_entry()`, `MANIFEST_SCHEMA_VERSION = 2`) already gives a
model the operation's name, description, input schema, labels, timeout,
compatibility, and risk flags without running the executor. The fields are
exact — but note that `ToolRegistry.to_manifest()` has no production caller
today (it is pinned only by `tests/test_tool_manifest.py`);
`jasper-tuning-workbench tools --json` becomes its first real consumer.

The host builds a workbench-scoped `ToolRegistry` from the pack with the
existing subset seam: construct a fresh registry and call
`register_packs(registry, deps, disabled=frozenset(),
disabled_packs=frozenset(), packs=(TUNING_WORKBENCH_PACK,))`
(`jasper/tools/packs.py`). The pack defines its **own** frozen deps
dataclass (`WorkbenchToolDeps`, owned by `jasper/tuning_workbench/`). It
must **not** extend the voice assistant's `ToolDeps` or touch
`jasper/voice/daemon_main.py` — editing those is the path by which
commissioning operations would leak into the embedded voice registry, the
exact outcome this design forbids. Follow the shape of
[`docs/examples/tool_pack_starter.py`](examples/tool_pack_starter.py),
which is contract-tested to stay out of the production voice packs.
Workbench adapters receive only host-injected, operation-scoped services;
they never hold a `CamillaController`, filesystem handle, daemon object, or
other powerful host object.

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

Sound-emitting and mutating operations are declared `consequential=True`.
That flag is **declarative only** — `dispatch_tool()` runs any registered
tool unconditionally, and `ToolDefinition`'s own docstring says the risk
flags are not wired to runtime behavior. Enforcement is therefore
workbench-owned: the CLI refuses to dispatch a `consequential` tool until
its own approval ledger records the required user approval and the relevant
measurement or experiment lifecycle is open. Failures follow the existing
tool convention — a hard failure returns `{"error": <actionable string>}`,
never a partial success payload. The CLI is a transport, not an alternate
dispatch or safety path.

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

#### Test convention — decided

Workbench tools are deliberately outside the voice registry, so the
AGENTS.md rule "every new LLM-callable tool ships a
`tests/voice_eval/regression/` scenario" does not apply to them — that
harness opens paid realtime voice sessions to test what the *voice
assistant* does with a tool, and its static guard
(`tests/test_tools_have_regression_scenarios.py`) only scans
`jasper/tools/*.py`, so a `jasper/tuning_workbench/` pack is invisible to
it either way. The workbench convention instead:

- every workbench tool ships a hardware-free pytest that exercises it
  through the real `ToolRegistry` + `dispatch_tool()` path with mocked
  collaborators, plus manifest coverage;
- PR-W1 adds a workbench-scoped static guard test with the same
  AST-scanning shape as the voice guard, asserting every tool in
  `jasper/tuning_workbench/` has such a test;
- PR-W1 adds one clarifying sentence to AGENTS.md scoping the voice-eval
  rule to voice-registry packs, so the exemption is a documented decision,
  not a silent gap.

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
  "kind": "derived_frequency_response",
  "sensitivity": "derived",
  "recomputable": true,
  "sha256": "...",
  "byte_size": 12345,
  "recorded_at": 1785196800.0,
  "generated_by": "jasper.tuning_workbench.tools.measure_response",
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

Field conventions follow the existing corpus: `kind` values are flat
strings (`derived_frequency_response`, `capture_wav`, `capture_analysis`
are the precedents — not dotted namespaces), `generated_by` is a
fully-qualified `jasper.` path, and `sensitivity` uses the literals already
in use — `private_raw_audio`, `private_metadata`, `config`, `derived`,
`debug_safe`. Raw microphone audio is `private_raw_audio`, exactly as
`jasper/active_speaker/bundles.py` and `jasper/correction/artifacts.py`
classify it today. A workbench session bundle writes its own `info.json`
with an integer `bundle_schema_version` at open (or passes
`bundle_schema_version=` explicitly) — `record_artifact()` requires one of
the two.

The exact domain payload remains the original:

- room bundles remain room bundles;
- crossover evidence remains `CommissioningEvidenceStore` evidence;
- active-speaker profiles remain active-speaker artifacts;
- measurement arrays remain in their existing bundle format.

An existing domain bundle is reopened through its own manifest, not
translated into a workbench schema. The cross-bundle reference type already
exists — reuse it: `ArtifactIdentity` in
`jasper/audio_measurement/evidence_identity.py` (bundle kind, bundle id,
normalized relative path, SHA-256, byte size, and a derived fingerprint,
with strict `to_dict()`/`from_mapping()` round-tripping). Active-speaker
evidence reopens through `CommissioningEvidenceStore.reopen_artifact()`,
which re-reads the bytes and hard-fails on any digest or size mismatch;
that store refuses non-Active bundle kinds by design, so the workbench
session index adds one small loader that resolves an `ArtifactIdentity`
against other bundle kinds' manifests with the same hard-fail semantics.
Schema version is recorded alongside the identity in the session entry (an
additive field, not a fork of `ArtifactIdentity`). A mutable relative path
by itself is never durable provenance.

If a capability produces genuinely new evidence, its owning subsystem defines
that payload. The workbench owns only session indexing and workbench-specific
provenance placed in the existing manifest metadata.

#### Fingerprint fields name their domain

There is no repo-wide `config_fingerprint()`; roughly thirty modules carry
private `_fingerprint` helpers over at least four non-interchangeable
payload domains. The workbench does not add a thirty-first. Its metadata
fields are defined as:

- `config_fingerprint` — the normalized identity of the **running**
  CamillaDSP graph at capture/apply time: `NormalizedActiveRawIdentity`
  from `jasper/audio_measurement/evidence_identity.py` (domain
  `camilladsp_active_raw`), recorded with its algorithm id and version;
- `topology_fingerprint` —
  `jasper.active_speaker.baseline_profile.topology_config_fingerprint`
  over the saved output topology;
- any new workbench payload hashing goes through
  `evidence_identity.json_fingerprint`.

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

#### The transport and the reference transaction — decided

The production seam this workspace builds on already exists and is
hardware-proven; do not rediscover it:

- **Two transports exist and only one is safe here.**
  `CamillaController.set_config_file_path()` (CamillaDSP
  `SetConfigFilePath` + reload) repoints the durable boot anchor — it is
  the *permanent-apply* transport. `CamillaController.set_active_config_raw()`
  (CamillaDSP `SetConfig`) loads a graph into the live pipeline and leaves
  the anchor untouched — proven on hardware (jts3) and documented in
  [`HANDOFF-active-speaker-dsp.md`](HANDOFF-active-speaker-dsp.md).
  Experiment `apply` and `bypass` use the raw transport, always.
- **The named seam triple** is `commission_seams()` in
  `jasper/active_speaker/commission_wiring.py` —
  `(load_config, read_running_config, get_current_config_path)`.
- **The reference transaction** is
  `jasper.active_speaker.startup_load.load_driver_commissioning_config`.
  It composes `jasper/dsp_apply.py`'s `apply_dsp_config` with the raw
  transport correctly: `get_current_config_path=None` (the path-confirm
  step compares the persisted path against the candidate and **always
  fails under the raw transport** — passing both is a guaranteed
  self-rollback), with the real confirmation in the `persist=` callback: a
  bounded convergence poll (`LIVE_CONFIRM_POLL_INTERVAL_S = 0.15`,
  `LIVE_CONFIRM_CONVERGENCE_BUDGET_S = 5.0` — the live readback lags the
  ack by ~22 ms on real hardware) plus an assertion that the statefile
  still points at the pre-experiment anchor (drift means another writer
  moved it; fail closed). "Never converged" is a *load* failure with its
  own error, distinct from a safety failure — the operator remedies
  differ.
- `jasper/dsp_apply.py` contributes exactly: the global writer lock
  (`dsp_writer_lock`, a 10 s admission bound on
  `/var/lib/camilladsp/configs/.dsp_apply.lock` — never pass a custom
  `lock_path` in production), file validation, the one-shot best-effort
  rollback, the durable last-apply record, and the `dsp_write_epoch()`
  stale-write fence. It owns none of: transport choice, statefile, health,
  volume, settle windows, or session lifetime.

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

What enforces each refusal is mostly already built: graph structure and
protection via `jasper/active_speaker/graph_safety.py` (`GraphView` — use
`view_from_emitted_text` for JTS-emitted YAML but `view_from_camilla_dict`
for anything read back from CamillaDSP, whose dialect the text parser
cannot read; `pipeline_reference_closure_errors` for dangling references;
the tweeter/sub protection predicates), topology/runtime conformance via
`jasper.active_speaker.runtime_contract.classify_camilla_graph` /
`safe_graph_for_current_topology`, excitation bounds via
`jasper.audio_measurement.excitation_admission.admit_excitation` and
program admission, and the ceiling via `jasper/dsp_apply.py`'s
`validate_camilla_config` — with two fail-open holes the workbench must
close: a missing `camilladsp` binary yields `ok_to_apply=True` (a dev-box
convenience; the workbench treats `MISSING` as refusal), and the
volume-limit reader deliberately does not fail on an unreadable file.
Genuinely new validators this workspace must build: bounded graph/resource
limits for the target Pi (nothing today counts filters, taps, or
convolution cost in a candidate), a candidate-vs-snapshot comparator for
devices/channels/roles/routing (existing validators classify one graph;
none diffs two), the human-readable graph diff itself, and the
restorable-baseline proof.

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
   exact-baseline restore. The gate composes existing surfaces —
   `CamillaController.get_clipped_samples()` (resets on config load, so a
   post-apply read is already candidate-scoped), outputd `STATUS` deltas
   (`dac`/`content` xruns, `mix.clipped_samples`, `mix.reference_sequence`
   advance, `watchdog.last_progress_age_ms`), and per-core load — but
   **xrun counters alone are insufficient**: outputd zero-fills short
   content reads and still writes the period, inserting silence that is
   invisible to `xrun_count`, so the gate must also read
   `content.empty_periods`/`partial_periods` and `shm_ring.empty_reads`
   (the `jasper/correction/runtime_integrity.py` lesson). The gate is a
   fail-closed workbench primitive, not a `jasper-doctor` check — the
   doctor's correction checks are WARN-never-FAIL by doctrine; they report
   state and must not be the enforcement.
2. **Temporary means restart-safe — scoped to `config_path`.** The durable
   boot anchor is `/var/lib/camilladsp/outputd-statefile.yml`
   (`jasper.active_speaker.environment.DEFAULT_CAMILLA_STATEFILE`; the
   legacy `statefile.yml` is intentionally preserved for pre-outputd
   rollback — never "repair" it). The raw transport structurally never
   repoints the anchor's `config_path`, so a restart mid-experiment comes
   up on the baseline. Two qualifications are part of the contract: the
   statefile **also persists `main_volume`/`mute`**, which CamillaDSP owns
   and mutates live — volume changes made during an experiment survive
   restart and are the volume guard's job to restore, not the config
   path's; and a restart must be *detected*, not silently absorbed — the
   workspace keeps a durable experiment ledger under
   `/var/lib/jasper/tuning-workbench/` so `status` can report "an
   experiment was open at restart; the live graph is the baseline" instead
   of forgetting the session existed. `jasper-camilla-pipe-guard`
   (`ExecStartPre`) and install-time repair may legitimately rewrite the
   anchor; that lands in the "canonical state changed while open" arm, not
   in silence.
3. **Listening level is not always Camilla master volume.** Spotify and
   Bluetooth are `VolumeMode.PUSH` (`jasper/music_sources.py`): the source
   carries the household level and Camilla is pinned at 0 dB. An experiment
   must inspect volume mode and may not pretend `/volume/set` always
   changes Camilla — and the reverse trap is worse: "restoring" a
   snapshotted Camilla `main_volume` while a push-mode source is active
   restores a number that never carried the level, and can un-mask a
   push-guard attenuation (`VolumeCoordinator` deliberately preserves a
   guarded attenuation from a failed push handoff). While
   `VolumeMode.PUSH` is active the workspace does not write Camilla
   `main_volume` at all; the snapshot records mode, source, and push-guard
   state so `verify-restore` can prove the right thing.

Prefer keeping comparison level matching in the stimulus and keeping the
household level unchanged — this is the crossover-v2 regime (derive-once,
hold-fixed, restore-once) and the default. When an experiment truly needs a
temporary volume resource, the composition is: pause the 1 Hz volume
reconcilers in **both** coordinator processes (jasper-voice and
jasper-control each run one; `note_measurement_active()` is process-local —
`measurement_window()`'s voice pause is the cross-process half), hold a
durable restore-once latch shaped like
`jasper.active_speaker.session_volume_plan.SessionVolumePlan` (intent
written before the first mutation, wall-clock ceiling, emergency
attenuation), and make every transition through
`jasper.active_speaker.volume_latch.set_and_confirm_volume` (independent
readback within tolerance, refusing to proceed on an unproven transition).
No such composed primitive exists today — it is PR-W3b work, and config and
volume restoration must be fault-injection tested together.

#### The experiment lease and drift detection

The DSP writer lock is a per-transaction admission bound, not a session
lease — nothing today says "an experiment owns DSP mutation until it
closes." The workspace adds a durable lease artifact (part of the
experiment ledger) that other writers voluntarily honor:

- `jasper-sound reconcile-current-dsp` gains an open-experiment skip beside
  its existing `active_audition` skip — load-bearing because `install.sh`
  runs the reconcile on **every deploy**, and it would otherwise re-emit
  the anchor's carrier over a live experiment;
- the wizard/correction/commissioning apply paths already funnel through
  `apply_dsp_config`, whose writer lock serializes each transaction; the
  lease makes the *session* visible so those owners can refuse or warn;
- `open` pre-checks the bass-extension pending fence
  (`/var/lib/jasper/bass_extension_apply_intent.json` makes the writer
  lock refuse **every** mutation, including a would-be revert); if the
  fence appears while an experiment is open, `status` surfaces it and
  `revert` names the recovery command instead of silently bypassing.

Drift is detected against four anchors together — the statefile
`config_path`, the canonical config file's SHA-256, the running graph's
`NormalizedActiveRawIdentity` fingerprint, and `dsp_write_epoch()` — never
mtimes (`install.sh` rewrites files without semantic change; the `(id=…)`
header strip in `jasper/sound/runtime.py` exists for exactly this reason).
"Canonical state changed legitimately while open" is a first-class
`status`/`verify-restore` outcome with its own remediation text, not a
generic failure.

#### Refusals and status honesty follow the PR-L4 pattern

The accountability work in `jasper/active_speaker/crossover_v2_flow.py`
sets the house pattern the workspace copies: assert *before* the candidate
is stashed or published, so a refusal leaves nothing applicable downstream;
route every refusal through one constructor that stamps a stable machine
code plus household copy from a single registry (an unstamped refusal
renders as some other failure — that bug shipped once already); and make
"applied but never graded" its own visible state, distinct from failing.
`status` and `verify-restore` label every block they report (`role`,
`matches_applied`-style comparisons) so snapshot, live, and candidate
blocks cannot be confused — the exact ambiguity the PR-L4 setup-status
work fixed in `/state.active_speaker_setup`.

## 6. Reuse and ownership

The workbench composes current owners through their public boundaries.

| Concern | Canonical owner the workbench consumes |
|---|---|
| Sweep/deconvolution/gating/calibration/analysis primitives | `jasper/audio_measurement/` |
| Layer ordering and measurement meaning | `docs/active-speaker-tuning-layers-design.md` plus each layer's operational doc |
| Active topology and driver safety | `jasper/active_speaker/` runtime contract and evidence |
| Preference and room graph recomposition | `jasper/sound/graph_carrier.py` and the owning emitters |
| LLM-facing declarations, manifests, and dispatch | `jasper/tools/` (`CapabilityPack`, `ToolDefinition`, `ToolExecutor`, `ToolRegistry`, `dispatch_tool`) |
| DSP writer lock, file validation, one-shot rollback, apply record | `jasper/dsp_apply.py` |
| Anchor-preserving live config transport | `CamillaController.set_active_config_raw` + `jasper/active_speaker/commission_wiring.py` |
| Reference live-confirm transaction | `jasper.active_speaker.startup_load.load_driver_commissioning_config` |
| Durable volume restore-once and confirmed transitions | `jasper/active_speaker/session_volume_plan.py`, `volume_latch.py` |
| Product measurement isolation | `jasper/correction/coordinator.py` `measurement_window()` (composing `jasper/mux.py`'s test lease and jasper-voice's measurement pause) |
| Evidence identity and fingerprints | `jasper/audio_measurement/evidence_identity.py` (`ArtifactIdentity`, `json_fingerprint`, `NormalizedActiveRawIdentity`) |
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

### Shared engineering rules — every rung is reviewed against these

Not a PR; the bar each rung meets in addition to COAH:

- **One module, one owner, stated.** Every new module opens with a
  docstring naming what it owns and what it explicitly does not own —
  mirror `jasper/audio_measurement/bundles.py`'s "owns only the byte-level
  manifest contract" style.
- **DRY means adapters call owners.** No copied DSP math, no re-derived
  measurement primitives, no second manifest/capture/dispatch stack. When
  an owner lacks a callable boundary, add the boundary to the owner in the
  same PR and adapt it (W2 does exactly this).
- **Single source of truth for every value.** Constants are imported from
  their owner (`DEFAULT_CAMILLA_STATEFILE`, lease timings, quality codes),
  never re-declared. New workbench constants live in exactly one module.
- **Observable by default.** Every state transition and refusal logs one
  stable `event=tuning_workbench.<area>.<action>` line with structured
  fields; failures carry stable machine codes from one registry
  (`jasper/tuning_workbench/errors.py`); `status` is always answerable,
  including after a crash or restart.
- **Fail closed, report honestly.** Unprovable states are refusals with
  remediation text, not warnings. No path continues from "probably."
- **Pin promises with tests in the same PR.** Every behavioral sentence a
  rung implements gets a hardware-free pytest; hardware claims name the
  box and the session evidence.
- **Frozen dataclasses at boundaries** — plain `Mapping` in, typed object
  out, matching the tools-contract and evidence-identity house style.
- **Bounded everything.** Every subprocess, poll, and wait has an explicit
  timeout; every ledger and bundle directory has a stated retention story.

### PR-W1 — read-only context and canonical tool pack

Ship:

- `jasper/tuning_workbench/` — `pack.py` (the `CapabilityPack` and
  `WorkbenchToolDeps`), `context.py` (the assembler), `geometry.py` (the
  §4.1 kind constants and domain mapping), `errors.py` (the stable
  refusal-code registry), `cli.py`;
- a `CapabilityPack` of initial read-only `ToolDefinition`/`ToolExecutor`
  adapters over existing owners;
- the canonical `ToolRegistry`/`dispatch_tool()` path with per-pack and
  per-tool fault isolation;
- `jasper-tuning-workbench tools --json`, `context --json`, and
  `call <tool-name> --input <json-file|->` (new `[project.scripts]` entry
  `jasper-tuning-workbench = "jasper.tuning_workbench.cli:main"`);
- current topology, active graph identity, layer-artifact inventory,
  source/volume mode, capability availability, and evidence index;
- stable `event=` logs for unavailable or failed capability reads.

Verified seams (checked 2026-07-27; re-verify before building):

- Registry construction: `ToolRegistry()` +
  `register_packs(registry, deps, disabled=frozenset(),
  disabled_packs=frozenset(), packs=(TUNING_WORKBENCH_PACK,))`.
- Context facts come from domain-owned readers, never re-parsed files when
  a reader exists: topology via
  `jasper.output_topology.load_output_topology_strict`; boot anchor via
  `jasper.active_speaker.environment.DEFAULT_CAMILLA_STATEFILE` +
  `parse_camilla_statefile_config_path`; live graph identity via
  `CamillaController.get_active_config_raw()` / `get_config_file_path()`
  (constructing a controller outside the daemons is proven —
  `tests/voice_eval/harness.py` already does it); volume via
  `jasper.volume_persistence.VolumePersistence` and mode via
  `jasper.music_sources.volume_mode`; renderer/source state via the
  daemons' own surfaces (`/state`), never `/etc` files.
- CLI shape mirrors `jasper/cli/sound.py`; stdin `-` mirrors
  `jasper/cli/route_latency_artifact.py`.

Traps:

- Do not add the pack to `TOOL_PACKS`, extend the voice `ToolDeps`, or
  touch `jasper/voice/daemon_main.py` — the isolation is the point.
- `tests/_tool_pack_contract.py` asserts voice-catalog fields
  (`catalog_pack`) and cannot be reused as-is; write workbench-local
  registry/dispatch/manifest tests modeled on
  `tests/test_tools_registry.py`, `test_tools_dispatch.py`,
  `test_tool_manifest.py`, and `test_tool_packs_registry.py`.
- Keep executor imports lazy: the package ships in the Pi venv and is
  imported everywhere tests run; heavy deps load inside `execute()`.

Acceptance:

- no sound and no mutation;
- pack/definition import is lightweight;
- one broken adapter appears as unavailable without losing the rest of the
  packet;
- every context fact is traced to a domain-owned reader;
- adding a second fixture tool requires no prompt, context-assembler branch,
  or second registry/dispatcher;
- the §5.1 test convention lands in the same PR: the workbench static
  guard test exists and the AGENTS.md voice-eval rule is scoped.

### PR-W2 — neutral evidence manifests and measurement adapters

Ship:

- workbench session bundles built on the neutral artifact manifest
  (`record_artifact`/`write_json_artifact`, own `info.json` with
  `bundle_schema_version`) plus the `ArtifactIdentity` session loader
  (§5.3);
- **the narrow capture boundary this rung first adds to its owner**: no
  callable "capture a calibrated response" function exists today — the
  product flow is woven through `jasper/web/correction_setup.py` handlers,
  session state, and the phone/browser capture relay. Per §6, the boundary
  is added beside the owning subsystem (callable from a `ToolExecutor`,
  driving the same relay/browser machinery and quality gates), and the
  workbench adapts it. Budget this as the bulk of the rung;
- adapters over that boundary and the existing analysis primitives;
- `jasper.correction.coordinator.measurement_window()` reuse for
  isolation;
- explicit geometry, stimulus, mic calibration, config fingerprint,
  validity, units, and sensitivity on every new measurement, per the §4.1
  and §5.3 definitions;
- lazy artifact inspection from the context packet.

Start with the capture transport the product already proves — the
browser/relay path (phone mic or calibrated UMIK through the browser) — to
reproduce the JTS3/reference comparison. A headless laptop-mic transport is
a later adapter only when a real session needs it.

Verified seams (checked 2026-07-27; re-verify before building):

- Isolation: call `measurement_window()` **as-is**. The mux lease's
  owner/label sets are closed frozensets in `jasper/mux.py`
  (`FANIN_TEST_LABELS = {"correction"}`, `FANIN_TEST_OWNERS =
  {"active-speaker-commissioning", "correction-measurement"}`) — a
  workbench-specific owner id is a deliberate `jasper/mux.py` change, not
  just a new string. Crash recovery is built in: the mux lease auto-expires
  within 60 s and the voice pause auto-clears within 120 s if the holder
  dies.
- Stimulus: `jasper.audio_measurement.sweep.synchronized_swept_sine` +
  `write_sweep_wav` (mono-enforced); the exact single-channel invariant
  for driver work is
  `jasper.active_speaker.driver_acoustics.write_driver_sweep_wav` (sweep
  on `target_channel`, digital zero elsewhere).
- Quality refusal surfaces the existing codes, never parallel ones:
  `jasper.audio_measurement.quality.assess_capture` hard-fails
  `sample_rate_mismatch` / `capture_too_short` / `capture_clipped`;
  driver verdicts `silent` / `unusable_capture` live in
  `driver_acoustics`; the splice/glitch class is
  `drift_baselines_disagree` in program analysis.
- Validity floor: `jasper.audio_measurement.gating.f_valid_floor_hz`.

Traps (these bit the 2026-07-27 session or its forensics):

- `sweep.read_wav_mono` **averages stereo to mono** — wrong for a
  one-mic-on-one-channel capture; select the channel explicitly.
- `magnitude_response(normalize=True)` is the default and destroys
  absolute level — comparative work passes `normalize=False`.
- Vendor UMIK calibration files are **response** curves; the sign
  convention is explicit since linearization-integrity PR-L1 — never
  re-infer it.
- A gate at the search ceiling is a bound, not a measured reflection —
  carry the saturation flag rather than reporting it as a room fact.
- `thd_curve` yields NaN-capable ratios — serialize as `null`.
- Band-normalization helpers carry bass-era default bands — always pass
  bands explicitly and record them in the artifact.

Acceptance:

- no copied DSP math;
- exact single-channel stimulus invariant where the rig requires it;
- contaminated/missing/silent capture refuses with actionable output;
- domain payloads remain domain-owned;
- a hardware-free fixture round-trips the neutral manifest, verifies its
  digest via `ArtifactIdentity`, and reopens its domain payload;
- one owner-scheduled JTS3 smoke is documented before claiming the path works.

### PR-W3a — experiment session, candidate, validation, and diff (no live audio)

Ship the session half of the §5.5 lifecycle — `open`, `candidate`, `diff`,
`validate`, `status`, and `close` of a never-applied session — plus the
machinery `apply` will stand on:

- the durable experiment ledger under `/var/lib/jasper/tuning-workbench/`
  (open experiments, snapshots, approvals, restart visibility);
- the experiment lease other writers honor, including the
  `reconcile-current-dsp` open-experiment skip (one guarded branch beside
  its existing `active_audition` skip, with a test — load-bearing because
  `install.sh` runs the reconcile on every deploy);
- the four-anchor drift detector (§5.5);
- candidate admission: `validate_camilla_config` with `MISSING` treated as
  refusal, the `graph_safety` structural/protection predicates with the
  correct `GraphView` parser per source, `runtime_contract`
  classification, the volume-limit gate, the bass-extension fence
  pre-check, and the new candidate-vs-snapshot routing comparator and Pi
  resource bounds;
- the graph diff renderer with best-effort layer annotations;
- a `jasper-doctor` *reporting* check for ledger state (WARN-style; the
  enforcement lives in the workspace).

Acceptance is fault injection with no audio emitted and the live graph
never touched: candidate invalid; candidate changes after validation
(`expected_candidate_sha256` → `candidate_changed` already exists in
`dsp_apply` — prove it end-to-end); bass-extension fence present at open;
drift while open; restart with an open-but-never-applied session; a second
writer's apply during an open session.

### PR-W3b — apply, bypass, health, and proven restore

Ship `apply`, `bypass`, `revert`, `verify-restore`, and full `close` — the
raw transport composed per §5.5 with the guarded volume transition, the
post-load health gate, and the volume contract.

Before implementation, re-verify the §5.5 seams against the then-current
graph-change seam, live CamillaDSP statefile, output health surfaces, and
`VolumeCoordinator`; do not carry this plan's snapshot forward blindly.

Acceptance includes fault injection:

- candidate invalid;
- candidate changes after validation;
- config load response lost after landing (the readback-convergence poll
  distinguishes "never converged" from "unsafe");
- attenuation/mute transition cannot be proved;
- candidate loads but fails settle/liveness, xrun/clipping,
  unexpected-silence-under-probe (fill-count deltas, not just xruns), or
  CPU/headroom checks;
- process dies after apply;
- restart while candidate is active (the ledger reports it; the anchor
  brings up the baseline);
- CamillaDSP's crash-recovery ladder parks the service (a parked graph is
  not "restored");
- Spotify/Bluetooth push-volume source active (no Camilla volume writes;
  the snapshot proves the mode);
- competing DSP writer;
- user volume change during apply/revert;
- restore response lost after landing;
- canonical config changed legitimately while the experiment was open
  (including a pipe-guard or install-time repair of the statefile).

Every terminal state must be either verified restored, explicitly held, or
blocked with a recoverable command and visible doctor/status evidence. No
failure path may continue from "probably restored."

### PR-W4 — agent protocol and end-to-end proof

Ship:

- `docs/HANDOFF-tuning-workbench.md`, the short canonical operating
  protocol (current-truth-first, under 400 lines, per the documentation
  paradigm);
- a thin agent launcher skill (`.claude/commands/tuning-workbench.md`)
  that loads the protocol and current context over the §5.0 transport;
- worked examples that demonstrate model-chosen—not hardcoded—diagnostics;
- README atlas and `docs/testing-tooling.md` rows ("capture a calibrated
  sweep", "run a reversible DSP experiment" — closing a known gap), and
  `docs/doc-map.toml` routing for `jasper/tuning_workbench/**`;
- an end-to-end JTS3/reference run:
  inspect → measure → reason → propose → validate → apply → listen/verify →
  restore, with the user's listening feedback recorded verbatim in the
  session manifest.

If a new test reads the HANDOFF by literal path, register it in
`scripts/ci-classify.py`'s `DOCS_TEST_FILES` (the AST guard catches literal
readers; glob/subprocess readers are hand-registered with a comment).

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
| Deploy-time reconcile overwrites an open experiment | `reconcile-current-dsp` open-experiment skip + experiment lease + four-anchor drift detection (§5.5) |
| Workbench tools escape any test convention | Workbench-scoped static guard test + AGENTS.md scoping note (§5.1) |

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
remain valuable input during implementation. They are not current
architecture. The load-bearing subset — the verified kernel traps, the
isolation-lease facts, and the transport split — has been inlined into §5
and §9's per-rung seams and traps; consult the historical docs for
narrative context, not for contracts.

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
  HANDOFF ships, then routes operational changes to the HANDOFF as well;
- PR-W1 adds a `jasper/tuning_workbench/**` glob to `docs/doc-map.toml`
  routing to this plan (then to the HANDOFF once it exists).

Last verified: 2026-07-27
