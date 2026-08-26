# The cutover plan: the engine goes live and the god files dissolve

> **Chunk 2 of the tuning refactor.** Chunk 1 — waves 0–8 of
> [`REFACTOR-TUNING-2026-08.md`](REFACTOR-TUNING-2026-08.md) §3 — built the engine
> beside the god files and deleted what nothing called. This document plans the
> cutover: the engine takes the calls, and the god files dissolve.
>
> **The engine's contract is not restated here.**
> [`crossover-v2-engine-design.md`](crossover-v2-engine-design.md) owns the four
> verbs, the five seams, the session graph, the volume owner and the playback
> transaction; its *"What is wired today, and what is not"* section is the ground
> truth this plan builds on. Read a shape question there and a scheduling
> question here.

**STATUS — updated every push.**

| § | Section | Status |
|---|---|---|
| 0 | Premise re-derivation ledger | VERIFIED-COMPLETE |
| 1 | RecordStore + the retention lift | VERIFIED-COMPLETE |
| 2 | The analyze registry | STUB |
| 3 | Recommender binding | VERIFIED-COMPLETE |
| 4 | Seam colour reconciliation | VERIFIED-COMPLETE |
| 5 | Front-end wiring | VERIFIED-COMPLETE |
| 6 | God-file dissolution map | STUB |
| 7 | Merge-order DAG + floor accounting | STUB |
| 8 | Risks and tiers | STUB |

---

## 0. Premise re-derivation ledger

Every premise this plan was handed was re-derived at HEAD (`5da40b9e2`, wave 6e).
The ones that moved are recorded here rather than silently corrected, because a
plan whose inputs drifted is a plan whose schedule drifted.

| Premise as handed | At HEAD | Disposition |
|---|---|---|
| `crossover_v2_flow.py` under `jasper/correction/` | `jasper/active_speaker/crossover_v2_flow.py` | **corrected path.** `jasper/correction/session.py` is a different, unrelated 127 KB module (`MeasurementSession`) — `crossover_v2/session.py:45-47` says so explicitly. |
| `crossover_v2_flow.py` ~9,228 lines | **9,228** | holds. |
| `web/correction_crossover_v2.py` ~8,106 lines | **8,088** | −18. Use 8,088. |
| `_hand_to_retention` call sites at `:5056 / :5252 / :7018` | **`:5056` / `:5254` / `:7020`** | **corrected.** The handed numbers are #3130's, written one merge earlier. Definition is `crossover_v2_flow.py:5256`. |
| `TuningSession._record()` at `session.py:~692` | `crossover_v2/session.py:655` (def), returns the record dict at **`:692-708`** | holds. |
| `analyze` hard-codes `results={}` at `session.py:~194/:428-461` | `AnalyzeOutcome` at `:194`; `analyze()` `:428-463`; **`results={}` at `:461`** | holds. |
| `Recommender` Callable seam at `session_seams.py:~270` | **`:270`** | holds. |
| Five seams, all sync; one production implementation (`MeasurementSessionGraph`) is async | **three** production implementations are async — graph, volume owner, and `play_program` | **widened.** See §4. |
| *(implied)* the front end is an `asyncio` web server | stdlib `ThreadingHTTPServer` + sync `BaseHTTPRequestHandler` (`web/correction_setup.py:61`), bridging into **one** background loop thread `jasper-correction-loop` (`_ensure_loop` `:1275`) via `_run_async` (`:1293`) | **corrected, and it changes §4's argument.** The seam colour decision turns on *which* thread each caller is on, and they are not all on one. |
| The wizard's routes live in `web/correction_crossover_v2.py` | **zero** routes, zero HTML, zero CSRF there. The `if path == …` dispatch chain is `web/correction_setup.py:7366` (`_dispatch_crossover`), reached from `:8014` | **corrected.** `correction_crossover_v2.py` is an orchestration host, not a page. See §5. |
| `owned_measurement_volume_db_nowait` is zero-caller post-6e; wave 8 may have deleted it | see §5 | re-derived there. |
| Sync call sites at `session.py ~:306/:534/:554` | ten `self.seams.*` call sites: `:307 :309 :478 :501 :532 :548 :552 :585 :598 :624` | **widened.** The handed three are #3106's N6 note, which counted `SessionGraph`'s three (`:307`, `:532`, `:552`) only. See §4. |
| The three `spatial.py` take builders share `_take_identity` | see §1 | re-derived there. |
| `position_cycle.json` is THE index (#3064) | see §1 | re-derived there. |

**One structural finding the handed brief did not carry:** `_hand_to_retention`
is not the only fail-soft retention boundary. `crossover_v2_flow.py:6879` inside
`_run_cloud_pipeline` carries an **inlined copy** of the same
try/except/WARN/return-False shape, with a comment saying it *"mirrors
`_retain_cloud_position`'s fail-soft boundary"*. The retention lift is therefore
**four** sites, not three, and a lift that migrates only the three named call
sites leaves a second writer behind — which is exactly the one-index rule's
failure mode in a different costume.

---

## 1. RecordStore + the retention lift

### What the protocol requires, and what pins it

`RecordStore` (`crossover_v2/session_seams.py:207`) is two pairs: `bank` `:235` /
`read` `:239` for one capture record, `persist` `:247` / `read_state` `:251` for
the session's own durable state. Both reads return `None` rather than raising —
`:243-244` for a missing record, `:255-259` for a state id that outlived its
state. The engine calls exactly two of the four: `records.bank` at
`crossover_v2/session.py:598` and `records.persist` at `:501`. **`read` and
`read_state` have no engine caller** — they exist for `PriorBank`
(`crossover_v2/prior_bank.py:52`) and for offline `analyze` (ruling S3).

`tests/engine_twin.py:199-234` is the pin. What it fixes:

- **Ids are opaque strings the store mints**, not paths. `_mint` (`:181-186`)
  returns `rec-{n}` / `state-{n}`; `_find` (`:189-196`) resolves them. Nothing in
  the engine parses an id.
- **`read` and `read_state` are implemented, not stubbed** (`:205-208`) — a
  store that could only be written to would make `analyze`'s offline promise
  unkeepable.
- `by_position` `:228` and `kinds` `:232` are **twin-only conveniences**, not
  protocol. A production store owes neither.

**There is no production implementation.** The only `def bank(` in the tree are
`tests/engine_twin.py:214` and `tests/test_crossover_v2_engine_skeleton.py:133`.

### Where records land today

`EVIDENCE_ROOT = "evidence/v1"` — `jasper/active_speaker/commissioning_evidence_store.py:71`,
the tree's only definition. `_artifact_path` (`:237-238`) prefixes every relative
path with `{EVIDENCE_ROOT}/artifacts/`, so a caller never spells the root itself;
the writers are `publish_raw_artifact` `:777` and `publish_json_artifact` `:786`.

One crossover-v2 round lives under `evidence/v1/artifacts/crossover_v2/{cap-id}/`
(`crossover_v2/evidence_packet.py:224` `_EVIDENCE_GLOB`, resolved by
`round_artifact_dir` `:617`). **`{cap-id}` is the RELAY session id, not the
bundle session id** (`:220-223`) — a trap for any id scheme the store invents.
Inside it: `positions/{take_id}.json`, `candidate.json` (`web/…:3432`),
`check.json` (`:3405`), `round_receipt.json` (`:3479`), `{phase}.json` (`:3956`),
`{phase}_program.wav` (`:4110`).

The position record's writer is the `retain_position` closure from
`bind_position_retention` (`web/correction_crossover_v2.py:3490`, closure `:3529`,
bound at `:5747`), writing at `:3567-3574`. Its WAV is **not** beside it — it goes
to `summed/summed_{take_id}_{uuid4hex}.wav` at the bundle root
(`bundles.py:205-220`, called `web/…:3553`). `take_id_for(position_id, attempt)`
= `f"{position_id}_a{attempt:02d}"` (`crossover_v2/spatial.py:779-794`).

**A drift site to close while lifting:** the writer spells `"schema_version": 1`
and `"kind": "jts_crossover_v2_position_evidence"` as **bare literals**
(`web/…:3570`, `:3571`) while the reader imports the same string as
`POSITION_EVIDENCE_KIND` (`crossover_v2/position_cycle.py:111`). Writer and
reader spell the discriminator independently.

### `_take_identity` — the claim holds, exactly three

Definition `crossover_v2/spatial.py:796`; six keys returned at `:824-831`
(`phase`, `index`, `attempt`, `take_id`, `session_id`, `wav_sha256`). Exactly
three call sites, all in `spatial.py`: `:914` (`cloud_position_record` `:834`),
`:1044` (`lateral_pose_record` `:987`), `:1107` (`entry_baseline_record` `:1061`).
The id **key** is deliberately excluded (`:817-820`) — cloud and entry say
`position_id`, a pose says `pose_id`.

### The record gap — this table is the section's deliverable

Against `TuningSession._record()`'s thirteen fields (`session.py:692-708`):

| Engine field | cloud | lateral | entry |
|---|---|---|---|
| `session_id` | yes | yes | yes |
| `kind` | **name collision** | collision | collision |
| `baseline_record_id` | — | — | — |
| `position_deg` | `:924` | `:1051` | — |
| `position_axis` | `:925` | — | — |
| `prompt` | `:918` | `:1049` | — |
| `candidate_id` | — | — | — |
| `regime` | — | `:1054` (hardcoded `per_driver`) | — |
| `polarity` | — | — | — |
| `graph_fingerprint` | — | `:1056` | `:1117` |
| `level_db` | — | — | — |
| `stimulus_dbfs` | — | — | — |
| `incident` | — | — | — |

**No builder carries more than five of the thirteen** (lateral 5, cloud 4, entry
2). Six are banked **nowhere today**: `baseline_record_id`, `candidate_id`,
`polarity`, `level_db`, `stimulus_dbfs`, `incident`.

Two consequences the executor must carry:

- **`kind` collides.** The engine's `kind` is `spec.kind` ∈ `MEASURE_KINDS`
  (`crossover_v2/contracts.py:1433-1437` — baseline · candidate · verify).
  Today's `kind` is a fixed record-type discriminator. The nearest today-analogue
  of the engine's meaning is the identity block's `phase` (`spatial.py:825`) plus,
  for lateral, `lateral_consumer` (`:1055`). A lift that maps `phase → kind`
  silently is a lift that mislabels every record.
- **`level_db` and `stimulus_dbfs` live only in the debug ring today**, through
  `CaptureProvenance.to_dict` (`capture_provenance.py:117-134`), which is gated
  off by default (below). So on an ordinary household run **the two fields the
  8.712 dB incident is about are banked nowhere at all** — which is exactly what
  `session.py:616-624` exists to stop.

### Three sidecar families, and only one is the record

| Family | Writer | Naming |
|---|---|---|
| (a) the bundle position record | `retain_position` `web/…:3567` | `positions/{take_id}.json` |
| (b) the WAV-adjacent capture sidecar | `_append_capture_entry` `bundles.py:679` | the `.wav` path with the suffix swapped (`:691`), plus an `info.json` entry (`:729`); reached via `register_capture` `:751` from `web/…:3563` |
| (c) the debug dump ring | `_maybe_retain_capture` `web/…:3223` | `{stamp}_{phase}_{device}.{wav,json}` (`:3279-3281`) under `/var/lib/jasper/xover-capture-dump` (`:203`), behind an `ENABLED` marker (`:211`, `:220-229`, `:3264`), ring-pruned at 90 files / 300 MB (`:216-217`) |

### The one-index rule (#3064), re-derived

`position_cycle.json` is **not** an authority and does not claim to be:
*"The index is DERIVED, never authored — this file writes down no fact of its
own"* (`crossover_v2/position_cycle.py:37-38`), and `:57-60` calls it convenience
over a nested glob. It is written by `bank_position_cycle`
(`scripts/run-crossover-round.py:713`, write at `:735`) from one call site
(`:1001`) **only when `--angles` is passed**; `read_position_cycle` (`:346`) and
`takes_by_position` (`:398`) have **zero production callers** — tests only.

So the rule the plan inherits is sharper than "do not mint a second index": the
banked files are the SSOT, any index is derived and rebuildable by rescanning
them, and **the one index that exists is already write-only**. A production
`RecordStore` must not become a second authority; and it should not resurrect
`position_cycle.json` as its read path either.

### The curve — banked, but not `DriverResponse`

Wave 4a banked a pose's curve **with phase**: `pose_curve_record`
(`spatial.py:956`, returning `:978-984` — `role`, `band_hz`, `freqs_hz`,
`magnitude_db`, `phase_deg`), reconstruction rule in its docstring `:961-963`.
The VERIFY re-derivation is gone (`round_views.py:37-40`).

**But the banked object is `LateralPoseCurve`, not
`program_analysis.DriverResponse`** (`program_analysis.py:1426`), which has no
serializer anywhere and is ~33.6 MB of ndarray for two occurrences
(`crossover_v2_flow.py:2713`). What is banked is a 12-point/octave *sampling*.
And the coverage is uneven: lateral banks a curve with phase, entry banks
magnitude only (`spatial.py:1122-1124`), cloud banks **no curve at all**. Acceptance
row 3's *"`DriverResponse` banked"* is therefore satisfied in spirit for one of
three kinds and unsatisfied for two — **record this as an open acceptance
question, not as done.**

### The retention lift

`_hand_to_retention` (`crossover_v2_flow.py:5256`) is the one fail-soft boundary:
`retain_position is None` returns `False`, a raising seam costs a WARN and returns
`False` (`:5266-5278`). Three call sites — `:5056` (`_retain_lateral_pose`),
`:5254` (`_retain_cloud_position`, the method's last statement), `:7020`
(`_retain_entry_baseline`, **the only site that uses the return value**, feeding
`artifact_ref = take_id if stored else ""` at `:7023`). Plus the **fourth,
inlined** copy at `:6879` inside `_run_cloud_pipeline` (§0).

**CHECK, MEASURE and VERIFY bank no take at all** — there is no fourth, fifth or
sixth call site, and `_consume_check` (`:4638`), `_consume_measure` (`:4690`) and
`_consume_verify` (`:7305`) hand nothing to retention. That is not an oversight
to preserve: it is the gap that makes `harmonic_evidence._bind_measure_captures`
and `round_views.verify_pose_curve` depend on the debug ring instead.

**The #3076 obligations, carried whole** (from that PR's body, recorded so they
cannot be lost):

1. Build the three missing retention paths (CHECK / MEASURE / VERIFY takes)
   **at the destination**, retaining into #3064's `take_id` convention.
2. Flip the sidecar's **seven** readers — four via
   `evidence_packet.RING_SIDECAR_GLOB` (`evidence_packet`, `harmonic_evidence`,
   `feature_classifier`, `round_views`) and **three that glob flat `*.json`**
   (`audio_measurement/capture_integrity.py:193`,
   `scripts/harmonic-distortion-replay.py:260`,
   `scripts/severed-twin-replay.py:240`) — fixing the globbers onto the one index
   as you flip them.
3. **The sidecar dies in that same PR**, per ruling S5 — it is a proof bracket,
   not a fallback.
4. `provenance.take()`'s single shot at the analyze seam is **plumbing to carry,
   not a field move**: by retention time the routing graph is restored and the
   fader may have moved.
5. The `ENABLED` gate over unconditional ~300 MB ring writes is an **owner call**.
   Leave it as it is until his ruling lands.

One more, discovered here: the `sidecar/` directory those readers glob is created
by a **shell script**, not by Python —
`scripts/bank-crossover-round.sh:234-238` splits the flat ring into `dumps/wav/`
and `dumps/sidecar/`, and three readers re-pair the WAV by
`parent.parent / "wav" / stem` written out verbatim in three places. Deleting the
ring means deleting that split, not only its Python readers.

### Work items

**W1-a — the production `RecordStore`.** One new module under
`crossover_v2/`, implementing `bank` / `read` / `persist` / `read_state` over the
`evidence/v1` layout `commissioning_evidence_store.py` already owns. Mints its own
opaque ids over `take_id_for`; **no new index**. `persist`/`read_state` land on
`persist_conductor_state`'s shape (`web/…:2809`), which is wave 3's *"schema
writer with no schema"*.
*Size:* ~300 lines including pins. *Verification bar:* the twin's own contract run
against the real store — bank, drop, read back, and a `read` of an unknown id
returning `None` rather than raising; plus a pin that the discriminator constant
is read from `position_cycle.POSITION_EVIDENCE_KIND` rather than re-spelled.
*Tier:* default single review pass.

**W1-b — the thirteen fields, at the destination.** Extend the three
`spatial.py` builders (or the store's record composition) to carry the six
never-banked fields, and map `phase → kind` **explicitly** through
`MEASURE_KINDS`, never implicitly.
*Size:* ~250 lines. *Verification bar:* one parametrized pin asserting all
thirteen present for all three kinds, mutation-verified by dropping one field.
*Tier:* default. **Depends on W1-a.**

**W1-c — the retention lift: four sites in, three phases added, the ring out.**
The #3076 obligations 1–4 in one PR, per S5. This is the largest single item in
the cutover and the one most likely to want splitting; if it splits, split by
*reader* (obligation 2) and keep obligations 1+3 together — a sidecar that dies
before its replacement writes is a data loss, and a sidecar that outlives it is
the second-writer defect.
*Size:* target < 400 lines; if it exceeds, split as above. *Verification bar:*
a CHECK, a MEASURE and a VERIFY capture each produce a banked take; the four
`RING_SIDECAR_GLOB` readers and the three flat globbers read the take instead;
watched-fail on each reader. *Tier:* default — it touches no clamp — but the
`provenance.take()` carry (obligation 4) is the part to review hardest.

**W1-d — the 4j SQLite index, afterward.** Six columns off `_record()` —
session · kind · position · candidate · timestamp · path — the last two supplied
by the store, since only it knows where it put the record and when
(`session.py:667-670`). Mechanical on the `jasper/wake_events.py` pattern
(925 lines). **Rebuildable by rescanning**; the banked files stay the SSOT
(`session.py:672-674`).
*Blocked until W1-a and W1-c land* — the 4j verdict on #3130 says exactly this
(*"no production `RecordStore` implementation … `_hand_to_retention` still has
exactly three call sites, so CHECK/MEASURE/VERIFY bank no take at all"*), and
#3130 closed one input gap by putting `captured_at` and `graph_fingerprint` on
the lateral take.
*Size:* ~200 lines. *Verification bar:* delete the index file, rebuild by
rescan, assert the same six columns. *Tier:* default.

## 2. The analyze registry

*STUB.*

---

## 3. Recommender binding

### What the seam is, and what is bound to it

`Recommender` is a bare type alias — `Callable[[Sequence[str]], Mapping[str,
Any]]` at `crossover_v2/session_seams.py:270`, exported at `:65`, and the
`EngineSeams.recommend` field's type at `:302`. Those three sites are its **only**
production references; a whole-tree grep finds nothing else outside
`tests/engine_twin.py` (`FakeRecommender`, `:298-313`) and
`tests/test_crossover_v2_engine_skeleton.py:181`. It is unbound in production and
has never been bound.

The engine's side is complete and thin: `TuningSession.recommend()`
(`crossover_v2/session.py:465-479`) passes `tuple(self._banked)` to the seam and
wraps the answer in `RecommendOutcome` (`:219-224`). The only test that exercises
it — `test_recommend_asks_the_prescriber_over_everything_banked`
(`tests/test_crossover_v2_engine_skeleton.py:892-904`) — asserts the verb
*reaches* the seam and nothing about what a real one returns. `engine_twin.py:299`
says so in as many words.

### What production prescriber logic exists

`jasper/cli/crossover_prescriber.py`, 1,140 lines, console script
`jasper-crossover-prescriber` (`pyproject.toml:203`). Four verbs:

| Verb | Entry | What it does |
|---|---|---|
| `packet` | `_cmd_packet` `:149` over `_load_packet` `:140` | emits the evidence document, via `build_crossover_evidence_packet(Path(args.session_dir), state_path=…, driver_draft_path=…, dump_ring_dir=…)` `:141-146` |
| `propose` | `_cmd_propose` `:292` | reads a model's answer back through `_gate` `:200` — dry run of `stage` |
| `stage` | `_cmd_stage` `:509` | `_gate`, then `stage_prescription(payload, prescription, for_round_ordinal=…, classifications=…)` `:547` |
| `status` | `_cmd_status` `:918` over `_status_sections` `:784` | the four sections — declared `:649` · banked `:677` · staged `:740` · applied `:760` — plus `_next_actions` `:820`. Writes nothing. |

`_gate` (`:200`) is shared whole by `propose` and `stage` and dispatches on the
document's own `kind` into `read_driver_prescription`
(`crossover_v2/driver_prescription.py:1686`) or `read_blend_prescription`
(`crossover_v2/blend_prescription.py:1374`). The spool
(`crossover_v2/prescription_spool.py`) is the mailbox: `stage_prescription` `:477`,
`staged_prescription_pending` `:598`, `take_staged_prescription` `:622` (consumed
by the web host at `web/correction_crossover_v2.py:2641`),
`withdraw_staged_prescription` `:961` (Undo, `web/…:870`).

The packet builder `build_crossover_evidence_packet`
(`crossover_v2/evidence_packet.py:2469`) has exactly two production callers:
`crossover_prescriber.py:141` and `crossover_v2/round_views.py:239` inside
`load_banked_round(round_dir: Path)` (`:223`). The second is the prior art the
binding consumes — the tree already maps *a bank on disk* to *a packet*.

### The two gaps, and the one they expose

1. **Arity.** The seam takes `Sequence[str]` — record ids minted by
   `RecordStore.bank` (`session_seams.py:235`). The prescriber takes an
   `argparse.Namespace` carrying four filesystem paths. **Nothing today maps
   banked record ids to a bundle directory.** This is a hard dependency on §1:
   the binding cannot be built before the store defines what an id resolves to.
2. **Return.** Every `_cmd_*` returns an exit `int` and *prints*; the payload
   dicts are locals (`:303`, `:554`, `:951`). The seam needs a value.

The third gap is the one worth naming out loud, because the seam's own signature
hides it: **`propose` and `stage` are not functions of the banked ids.** They
consume an external document — the model's answer — and the CLI has no model
client by design (*"Who calls the model is not this tool's business"*,
`crossover_prescriber.py:5-45`). A seam whose whole input is `Sequence[str]`
therefore **cannot reach `propose` or `stage`**, and a plan that promised it would
would be promising an impossible binding.

So the verb's own docstring — *"naming the next thing to try"* — overstates what
this signature can deliver, and the plan must pick a side rather than paper it.

### Recommendation: bind `recommend` to `packet + status`, and say what that is

**Bind the seam to the read-only half of the prescriber: build the packet over
the banked round, and compose the four status sections over it.** Rationale, in
order of weight:

- It is the half that **is** a function of the bank. Ids in, evidence and spool
  state out, no external document.
- `_status_sections` (`:784`) already composes all four sections from **one**
  packet — its own docstring's *"no second walk of the bundle"* — so the adapter
  is a call, not a re-implementation. `_staged_section` (`:740`) already answers
  *"is a prescription waiting"* from `staged_prescription_pending()` (`:748`)
  **without consuming the spool**, so no `peek` needs inventing beside `take`.
- It honours the seam docstring's standing order at `session_seams.py:265-267` —
  **do not re-extract the prescriber**. The logic stays in `crossover_prescriber.py`;
  what moves is the print/return boundary.
- When a prescription **has** been staged for the next round ordinal, the staged
  section IS "the next thing to try"; when it has not, the packet is the evidence
  for producing one. Both are honest answers to *"what should happen next"*, and
  neither invents a decision the engine does not own.

**Rejected: widening the seam to take an optional answer document.** It would
make `Recommender` a two-argument callable to serve a caller that does not exist
(the engine never holds the model's reply), and §1 cut this seam narrow on
purpose.

### Work items

**W3-a — `crossover_prescriber` gains a return-shaped status door.**
Split `_cmd_status` (`:918`) into a `status_document(...) -> dict[str, Any]` that
returns what `_print_status` (`:908`) prints, and keep `_cmd_status` as the
print-and-exit wrapper over it. Same for `_load_packet` if the packet is wanted
separately. Zero behaviour change; the CLI's stdout is byte-identical.
*Size:* ~60 lines. *Verification bar:* the existing prescriber CLI tests pass
unchanged, plus one pin asserting `status_document(...)` and the CLI's printed
JSON carry the same keys. *Tier:* default single review pass.

**W3-b — the `Recommender` adapter, bound in the web host.**
A function `record_ids -> Mapping` that resolves the ids to the round's bundle
directory (§1's store owns the resolution), calls
`build_crossover_evidence_packet`, and returns
`{"packet": …, **status_document(...)}`. Bound onto `EngineSeams.recommend` at
the same construction site §5 builds.
*Size:* ~120 lines including the pin. *Depends on:* §1 (id→path resolution) and
§4 (colour — the adapter's declared colour must match whatever §4 settles).
*Verification bar:* one pin driving the **real** adapter against a bundle
fixture, asserting the returned mapping carries the packet fingerprint and the
staged section, and that the spool is **not** consumed (assert
`staged_prescription_pending()` is unchanged across the call) — that last
assertion is the one that would catch a `take` slipping in for a `pending`.
*Tier:* default single review pass. Nothing here touches the output path.

---

## 4. Seam colour reconciliation

### The finding, re-derived

All five seams are declared **synchronous**:

| Seam | Methods | Declared at |
|---|---|---|
| `SessionGraph` | `install` · `patch` · `restore` | `session_seams.py:104` · `:120` · `:131` |
| `VolumeClaim` | `acquire` · `prove` · `release` | `:161` · `:170` · `:197` |
| `RecordStore` | `bank` · `read` · `persist` · `read_state` | `:235` · `:239` · `:247` · `:251` |
| `Recommender` | the callable | `:270` (no `Awaitable` in the return type) |
| `PlaybackTransaction` | `run` | `playback_transaction.py:190` |

**Three of the five have production implementations, and all three are async:**

- `MeasurementSessionGraph` — `async def install` `crossover_v2/session_graph.py:135`,
  `async def patch` `:176`, `async def restore` `:190`.
- `VolumeOwner` — `async def acquire_level` `jasper/volume_owner.py:261`,
  `async def release` `:504`, `async def prove` `:582`.
- `play_program` — `async def` `jasper/active_speaker/program_playback.py:118`,
  awaited in production at `web/correction_crossover_v2.py:4314`.

The remaining two have **no** production implementation: `RecordStore` is a
Protocol whose only `def bank(` in the tree are the two test doubles
(`tests/engine_twin.py:214`, `tests/test_crossover_v2_engine_skeleton.py:133`),
and `Recommender` is unbound (§3).

**The threading model is the thing the decision actually turns on, and it is not
what "async web host" would suggest.** The transport is stdlib
`ThreadingHTTPServer` + `BaseHTTPRequestHandler` (`web/correction_setup.py:61`)
and **every HTTP handler is a plain `def`**. There is exactly one background
event loop — thread `jasper-correction-loop`, started by `_ensure_loop`
(`web/correction_setup.py:1275`) — and handler threads reach it through
`_run_async` (`:1293`), which is
`asyncio.run_coroutine_threadsafe(...)` followed by `fut.result(timeout=60.0)`,
with a cancel-and-drain path on timeout (`:1311-1320`).

So there are **two caller populations, on two thread kinds**:

- **The walk runs ON the loop.** `_run_relay_capture` (`web/correction_setup.py:1037`)
  fires the whole session as one long-lived coroutine —
  `asyncio.run_coroutine_threadsafe(_run(), _ensure_loop())` at `:1153`,
  fire-and-forget — and the POST returns the tap link immediately. `measure()`'s
  caller is inside that coroutine.
- **The short endpoints run on handler threads** — status, apply, restore,
  position-ready, complete, retake — and already bridge with `_run_async`
  (ten sites, e.g. `web/correction_crossover_v2.py:7274`, `:7883`).

The mismatch is therefore not *"one implementation happens to be async"*. It is
that the whole production side is async, the whole declared side is sync, and the
seam's **primary** caller sits on the loop thread itself.

**The engine's ten seam call sites**, all in `crossover_v2/session.py`, all
currently plain calls: `:307` (`graph.install`), `:309` (`volume.acquire`), `:478`
(`recommend`), `:501` (`records.persist`), `:532` (the release pair in
`_release_both_after_failed_open`), `:548` (`volume.release`), `:552`
(`graph.restore`), `:585` (`play.run`), `:598` (`records.bank`), `:624`
(`volume.prove`). #3106's N6 disposition counted three — that note was scoped to
`SessionGraph` alone and is correct within its scope.

### The options

**A — the seams go async.** All twelve seam methods become `async def`; the four
verbs become `async def`; `__enter__`/`__exit__` (`session.py:332-351`) become
`__aenter__`/`__aexit__`; `Recommender` becomes
`Callable[[Sequence[str]], Awaitable[Mapping[str, Any]]]`;
`_attach_cleanup_failure` (`:73-92`) takes an awaitable cleanup.

**B — sync seams with a bridge inside each one.** Each seam method stays `def`
and calls `_run_async(...)` internally. **This deadlocks, and that is the whole
refutation.** `_run_async` is `run_coroutine_threadsafe(...)` then
`fut.result(timeout)`; called *from the loop thread*, it schedules work onto the
loop and then blocks the loop waiting for that work to run. The loop cannot run
it, and the call hangs until the 60 s timeout fires and cancels — mid-stimulus,
inside a session holding the fader. The walk is on the loop thread (above), so
this is the primary path, not an edge case. Ruling S4's *"no unused bridges"*
never has to be reached: these bridges would be used, and they would hang.

**C — colour per seam, by evidence.** Async where the implementation is async
(graph, volume, play), sync where it is not (records, recommend). Rejected: it
splits the verbs — `measure` would be async and `analyze` sync — so a caller has
to remember which; and it is unstable, because the first `RecordStore` that wants
to `await` an fsync or a remote write flips the sync half anyway. The design doc
already forecloses this at `crossover-v2-engine-design.md:87-89`: *"it is one
decision for all of them, not one per seam."*

### Recommendation: A, in one PR, all five seams at once

The evidence points one way and the ruling already says to decide once. The
argument in one line: **the walk's caller is on the loop, so a sync seam cannot
bridge without deadlocking; the short endpoints are off the loop, and for them
the bridge already ships and is already used ten times.** Async verbs cost those
callers one existing `_run_async(...)` each and nothing else.

Three consequences the executor must carry rather than discover:

1. **`RecordStore` is cut async before it is built.** That is a feature, not a
   cost: `session_seams.py:5-12` argues that cutting all three seams *before* any
   of them exists is what stops three waves each cutting a seam of its own shape.
   Cutting the colour before the implementation is the same argument one level
   down — and it means §1's store can `await asyncio.to_thread(...)` around its
   file I/O rather than blocking the event loop on every `bank`, which a sync
   seam would force it to do inside an `aiohttp` handler.
2. **The release path needs `asyncio.shield`.** `_release_slots` (`:537-553`)
   runs in a `finally`; awaiting there is cancellable, and a cancelled release
   strands the fader at measurement level — the exact failure ADR-0004's scope
   note warned about when it insisted the (now-deleted) `held_target_db` read
   stay synchronous and non-blocking. The parameter died in 6e (#3137); the
   hazard did not. **Shield the two releases, do not shield the acquires.**
3. **`analyze` and `recommend` become async, and they are the offline verbs.**
   `analyze` is deliberately not gated on an open session (`session.py:430-437`)
   so a bank can be re-analyzed forever. A CLI caller pays one
   `asyncio.run(...)`; an HTTP caller pays one `_run_async(...)` it already has.
   That is the whole cost, and it buys the store the freedom in (1).

### Work items

**W4-a — the colour flip.** `session_seams.py` + `playback_transaction.py` +
`crossover_v2/session.py`: twelve seam declarations, ten call sites, four verbs,
the context-manager pair, and the `_attach_cleanup_failure` signature. Add the
`asyncio.shield` on the two release paths in the same PR — it is not separable,
because the flip is what makes the release awaitable.
*Size:* ~250 lines across three files, almost all mechanical. *Verification bar:*
mypy clean (it is the backstop the N6 disposition named), plus a pin that a
**cancelled** `close()` still released both slots — mutation-verified by removing
the shield and watching exactly that pin fail. *Tier:* **adversarial.** It touches
the fader release path, which is a non-negotiable-tier surface per
[AGENTS.md](../AGENTS.md).

**W4-b — the twin follows.** `tests/engine_twin.py` (419 lines) and
`tests/test_crossover_v2_engine_skeleton.py` and `tests/test_engine_twin.py`: the
five fakes become async, `open_session` becomes an async context manager.
*Size:* ~200 lines, mechanical. *Verification bar:* the existing engine tests pass
with no assertion edited — an assertion that had to change would mean the flip
changed behaviour, which it must not. *Tier:* test-only; author judgment plus a
sanity look.

**W4-c — the `VolumeClaim` adapter.** Not a colour item, but it is discovered
here and it blocks §5. The owner's API is **handle-carrying**:
`acquire_level(kind, level_db) -> VolumeClaimHandle` (`volume_owner.py:261`),
`release(handle, *, household_level_db=None)` (`:504`), `prove(handle)` (`:582`).
The seam's is **handle-free**: `acquire(level_db)`, `prove()`, `release()`
(`session_seams.py:161/170/197`). So the production `VolumeClaim` is a small
stateful adapter that holds the handle between calls — and its `release` must be
idempotent and safe against nothing-held, which is exactly what the owner already
promises (`volume_owner.py:509-514` cites the seam's contract by name).
*Size:* ~90 lines including pins. *Verification bar:* pins for acquire-raised-
then-release, double-release, and prove-after-preemption returning `None`.
*Tier:* **adversarial** (fader path).

---

## 5. Front-end wiring

### What actually drives `CrossoverV2Session`

The brief's premise that the routes live in `web/correction_crossover_v2.py` is
false at HEAD (§0). That file has **zero** route decorators, zero HTML/CSS/JS
(a grep for `<div|<html|<script|<style|innerHTML|document.getElementById`
returns 0), and zero CSRF or static-asset code. Its own docstring says so at
`:7-9`. It is an **orchestration host**.

The chain, end to end:

| Layer | Where |
|---|---|
| nginx `location /correction/` | `deploy/nginx-jasper.conf:463` |
| one `ThreadingHTTPServer` | `web/correction_setup.py:61`, started per location by `web/__main__.py` |
| `/crossover/*` funnel | `correction_setup.py:8014` → `_dispatch_crossover` `:7366`, a linear `if path == …` chain |
| the loop bridge | `_ensure_loop` `:1275`, `_run_async` `:1293` |
| the walk | `_run_relay_capture` `:1037`, fired at `:1153` |
| the host module | `web/correction_crossover_v2.py` |

The session registry is **in the caller, not the host**:
`correction_setup.py` holds `_session_lock` `:133`, `_relay_position_gate`
`:149`, `_relay_complete_request` `:154`, `_relay_retake_request` `:160`, written
by `_set_relay_capture` `:719-731` and cleared at `:615-623`. The three signal
routes (`position-ready`, `complete`, `retake`) look up through that lock.

### The session lifecycle, and where `TuningSession` slots in

Two entry points into the host, both `def`, both near-duplicate twins:
`prepare_v2_session` (`:5944`, **534 lines**) and `prepare_v2_verify` (`:6520`,
**382 lines**). They share `_open` / `_run` / `holder` structure, the same
`bind_v2_stage_seams` call shape (`:6369-6379` vs `:6800-6811`) and the same
`_build_source_run` tail.

- **Construction** — lazy import at `:6002` / `:6557`; then
  `CrossoverV2Session.hydrate(...)` at `:6357` (stage 1) and
  `CrossoverV2Session(...)` at `:6785` (stage 2), both inside a nested `_open()`
  closure.
- **Storage** — nowhere durable. It is a plain local. The only handle that
  outlives `_open()` is a closure capture in a bare `holder: dict[str, Any]`
  (`:6258`, `:6711`), filled at `:6432` / `:6856` and drained by
  `async def _run(...)` at `:6448-6449` / `:6872-6873`.
- **Persist** — `persist_conductor_state(conductor, …)` at `:6431` (def `:2809`).
- **Teardown** — none for the conductor; it is collected when the runner closure
  returns. What is explicitly torn down is the ambient global state: the
  `_volume_hooks` close/abandon arms (`:5443`, `:5450`),
  `_release_pause_best_effort` (`:1309`), `release_session_measurement_graph`
  (`:1215`), and `_set_relay_capture(None)`.
- **The human's entry point** is `GET /correction/crossover` →
  `correction_setup.py:7847` → `correction_crossover_flow.render_page`
  (`correction_crossover_flow.py:24`); the page is fed by
  `crossover_v2_status_block` (`web/…:1995`), embedded as
  `payload["crossover_v2"]` at `correction_crossover_backend.py:2097-2104`.

**So `TuningSession` slots in at `_open()`, with its lifetime equal to the
`_run()` coroutine's.** `open()` replaces the graph-register + volume-open pair;
`measure()` replaces the consume/retain walk; `save()` replaces
`persist_conductor_state`; `close()` replaces the four-way ambient teardown. The
`holder` dict and the two `_open` closures are what the cutover deletes — the
session becomes the thing that is held, instead of a closure capture standing in
for one.

### W7's dissolution — what dies, and what must not

`_session_volume_io` (`web/correction_crossover_v2.py:1289`, 18 lines) is a
factory returning `(set, get)` closures. Only `_set` (`:1294-1298`) is the named
exception: it calls `CamillaController.set_volume_db(db, best_effort=False)`
(`jasper/camilla.py:684`), which clamps through `_coerce_main_volume_db`
(`:141-160`) and writes `c.volume.set_main_volume` (`:695`). No coordinator, no
owner. Five call sites — `:1367`, `:1409`, `:1446`, `:4187` (which **discards**
`_set`; read-only), `:5397` — so four consume the write door.

**`SessionVolumePlan` (`jasper/active_speaker/session_volume_plan.py:667`, module
1,208 lines) splits cleanly, and the split is the section's decision.**

*What dissolves into `VolumeOwner`:* the fader writes. The class **owns no
CamillaDSP** (`:670-671`) — `open`, `close`, `abandon`, `enforce_ceiling` and
`recover_unresolved` all take `(set, get)` as *parameters*. Swapping the injected
door for a `VolumeClaim` over the owner therefore needs **no internal
restructure**: `acquire`/`prove`/`release` (`session_seams.py:161/170/197`) map
onto `acquire_level`/`prove`/`release` (`volume_owner.py:261/582/504`) through
the handle-holding adapter W4-c builds.

*What survives, and must not be dissolved:* **the durable restore latch.**
`VolumeOwner` is explicitly in-memory and per-process — *"Durable volume-safety
state belongs to the claim holders that own it"* (`volume_owner.py:65-68`). The
owner provides none of:

- the persisted `_State` (`:589-596`) at
  `/var/lib/jasper/active_speaker_crossover_session_volume.json` (`:109-111`),
  `SCHEMA_VERSION = 1` (`:80`);
- `original_main_volume_db`, snapshotted in `open` at `:933-936` and persisted
  **before the first fader mutation** (`:946-948`);
- `_drain_restore`'s exact→emergency ladder (`:1093-1144`, candidates at
  `:1113-1136`) and the `session_volume_restore_unconfirmed` latch (`:1137`);
- `needs_recovery`'s two branches (`:744-762`), including the
  durably-active-but-not-opened-this-process one that survives a crash — crash
  hydration deliberately does **not** flip `active`→`unresolved` (`:652-654`);
- the 1,800 s wall-clock ceiling (`DEFAULT_WALL_CLOCK_CEILING_S` `:87`, hard cap
  `:96`).

That set **is** design-doc contract 9, the walked-away guarantee, and it does not
move. Two ordering invariants ride with it and are easy to lose in a rewrite:
`_clear_resolved` (`:858`) drops the in-memory intent **before** persisting
(`:893-896`) — its docstring at `:862-874` records the measured **+47.5 dB**
(−60.0 → −12.5) hazard of the other order — and the restore is **once**, drained
by close, session death, or the ceiling.

### Three stale facts to true up while here

1. **`owned_measurement_volume_db_nowait` still exists at HEAD** —
   `session_volume_plan.py:1060` — and wave 8 did **not** delete it. Whole-repo
   grep: the definition, four own-module docstring cross-references (`:39`,
   `:871`, `:990`, `:1039`), three test assertions
   (`tests/test_crossover_v2_measurement_volume_drift.py:962`, `:968`, `:976`),
   and two historical docs. **Zero production callers.** Its own docstrings at
   `:871` and `:990` still claim it *"feeds the swap's release reference"* — the
   consumer 6d deleted.
2. **`held_target_db` is gone from code but live in two docs.** Zero `.py` hits
   at HEAD (6e / #3137); `camilla.py:106-109` now states the exception is gone.
   But `docs/adr/0004-duck-release-algebra-and-reference.md:101-106` and
   `crossover-v2-engine-design.md:252-268` both still describe it as live
   plumbing, the latter in the future tense (*"until 6e lands"*). ADR-0004 is
   append-only, so the fix is a superseding note, not an edit.
3. **The ledger's enumerated-set check cannot see one writer.**
   `REFACTOR-TUNING-2026-08.md:919-925` greps `set_volume_db(` with a trailing
   paren; `jasper/cli/seat_level.py:413` binds `set_main_volume_db=cam.set_volume_db`
   — a bare bound-method reference with no paren. It is a separate CLI process
   with no `install_volume_owner`, so `volume_owner()` answers `None` there and
   nothing is unsafe — but the *"no unaccounted writer remains"* claim should say
   so rather than rest on a grep that structurally cannot find it.

### Work items

**W5-a — the two preparers converge.** Fold `prepare_v2_session` and
`prepare_v2_verify` into one preparer plus a stage argument, deleting the
duplicated `_open` / `_run` / `holder` scaffolding. Pure de-duplication, no
behaviour change, and it is the precondition for constructing one
`TuningSession` in one place.
*Size:* ~380 lines removed, ~120 added. *Verification bar:* the existing endpoint
suite passes with no assertion edited; both stages still produce a
`V2PreparedSession` with the same field values. *Tier:* default.

**W5-b — `TuningSession` is constructed in production.** Build `EngineSeams` at
the converged preparer, holding the session for the `_run()` coroutine's
lifetime. This is the PR that makes `crossover-v2-engine-design.md`'s *"is
constructed only in tests"* false, and wave 2's engine-internal enforcement pin
(`session_seams.py:294-295`) lands with it — it now has a front end to point at.
*Depends on:* W4-a (colour), W4-c (`VolumeClaim`), W1-a (`RecordStore`), W3-b
(recommender), §2 (analyze registry). **This is the DAG's join node.**
*Size:* ~350 lines. *Verification bar:* one end-to-end pin driving a whole
session through the real preparer against twin seams. *Tier:* **adversarial** —
it is the first production path through the volume claim.

**W5-c — `SessionVolumePlan` sheds its fader doors and keeps its latch.** Delete
the `(set, get)` parameters from `open`/`close`/`abandon`/`enforce_ceiling`/
`recover_unresolved`; the plan takes a `VolumeClaim` instead. Delete
`_session_volume_io._set` and its four consuming call sites. `_get` survives for
`:4187`'s read-only hold.
*Size:* ~300 lines. *Verification bar:* pins for all four drain paths
(close / abandon / ceiling / recover) landing the fader on
`original_main_volume_db`, plus one for the emergency fall-through, plus one
asserting the `_clear_resolved`-before-persist order — mutation-verified by
swapping the two statements and watching that pin alone fail. *Tier:*
**adversarial** (fader path, and the walked-away guarantee).

**W5-d — claim the two orphans and the two stale docs.** Delete
`owned_measurement_volume_db_nowait` with its three test assertions and correct
the two stale docstrings; add the ADR-0004 superseding note and re-tense the
engine design doc's 6e paragraph; widen the ledger's writer grep to catch a bare
bound-method reference and re-run it.
*Size:* ~120 lines net negative. *Verification bar:* zero `owned_measurement`
hits in `jasper/`; the widened grep's own output pasted into the PR.
*Tier:* default. Independent of every other item — **schedulable first.**

## 6. God-file dissolution map

*STUB.*

## 7. Merge-order DAG + floor accounting

*STUB.*

## 8. Risks and tiers

*STUB.*
