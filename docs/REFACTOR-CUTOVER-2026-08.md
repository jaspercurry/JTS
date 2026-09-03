# The cutover plan: the engine goes live and the god files dissolve

> **Chunk 2 of the tuning refactor.** Chunk 1 — waves 0–8 of the tuning-engine
> plan, retired with its rulings carried into
> [ADR-0228](adr/0228-rulings-carried-out-of-refactor-tuning-on-its-retirement.md)
> — built the engine beside the god files and deleted what nothing called.
> This document plans the cutover: the engine takes the calls, and the god
> files dissolve.
>
> **The engine's contract is not restated here.**
> [`crossover-v2-engine-design.md`](historical/crossover-v2-engine-design.md) owns the four
> verbs, the five seams, the session graph, the volume owner and the playback
> transaction; its *"What is wired today, and what is not"* section is the ground
> truth this plan builds on. Read a shape question there and a scheduling
> question here.

**STATUS — all eight sections VERIFIED-COMPLETE, re-derived at `4a9e9f631`.
§6's three open decisions are SETTLED and the citation question is answered,
re-derived at `c253c3cf1`. NOTHING WAITS ON THE OWNER — he ruled both remaining
items in chat on 2026-08-26: 6.2's seam brief FOLDs, and Appendix A's question is
DROP the attribution.**

| § | Section | Status |
|---|---|---|
| 0 | Premise re-derivation ledger | VERIFIED-COMPLETE |
| 1 | RecordStore + the retention lift | VERIFIED-COMPLETE |
| 2 | The analyze registry | VERIFIED-COMPLETE |
| 3 | Recommender binding | VERIFIED-COMPLETE |
| 4 | Seam colour reconciliation | VERIFIED-COMPLETE |
| 5 | Front-end wiring | VERIFIED-COMPLETE |
| 6 | God-file dissolution map | VERIFIED-COMPLETE · **6.1 RULED · 6.2 RULED (FOLD, owner 2026-08-26) · 6.3 RULED** |
| 7 | Merge-order DAG + floor accounting | VERIFIED-COMPLETE |
| 8 | Risks and tiers | VERIFIED-COMPLETE |
| A | The "no-silent-failure" citations | **RULED (owner 2026-08-26) — DROP the attribution** |

---

## 0. Premise re-derivation ledger

Every premise this plan was handed was re-derived at HEAD, first at `5da40b9e2`
(wave 6e) and again at `4a9e9f631` after five merges landed under the open PR.
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
| `Recommender` Callable seam at `session_seams.py:~270` | **`:271`** | **renumbered.** #3140 added a line to that module's `SessionGraph` docstring, shifting every declaration below `:100` by one. Every `session_seams.py` citation in this document is re-derived at the new HEAD. |
| Five seams, all sync; one production implementation (`MeasurementSessionGraph`) is async | **three** production implementations are async — graph, volume owner, and `play_program` | **widened.** See §4. |
| *(implied)* the front end is an `asyncio` web server | stdlib `ThreadingHTTPServer` + sync `BaseHTTPRequestHandler` (`web/correction_setup.py:61`), bridging into **one** background loop thread `jasper-correction-loop` (`_ensure_loop` `:1275`) via `_run_async` (`:1292`) | **corrected, and it changes §4's argument.** The seam colour decision turns on *which* thread each caller is on, and they are not all on one. |
| The wizard's routes live in `web/correction_crossover_v2.py` | **zero** routes, zero HTML, zero CSRF there. The `if path == …` dispatch chain is `web/correction_setup.py:7366` (`_dispatch_crossover`), reached from `:8014` | **corrected.** `correction_crossover_v2.py` is an orchestration host, not a page. See §5. |
| `owned_measurement_volume_db_nowait` is zero-caller post-6e; wave 8 may have deleted it | **deleted on `main` by #3140**, with its two stale docstrings and its three test assertions | **closed.** It was still present, zero-caller, at `5da40b9e2`; #3140 landed while this document was in review. §5 records it as closed rather than dropping it. |
| Sync call sites at `session.py ~:306/:534/:554` | ten `self.seams.*` call sites: `:307 :309 :478 :501 :532 :548 :552 :585 :598 :624` | **widened.** The handed three are #3106's N6 note, which counted `SessionGraph`'s three (`:307`, `:532`, `:552`) only. See §4. |
| The three `spatial.py` take builders share `_take_identity` | see §1 | re-derived there. |
| `position_cycle.json` is THE index (#3064) | see §1 | re-derived there. |
| *(asserted by this plan itself)* a **fourth**, inlined fail-soft retention boundary at `crossover_v2_flow.py:6879` inside `_run_cloud_pipeline`, making the lift four sites | **REFUTED — three sites.** `:6879` is a **comment**, not code: it sits in the `except` arm of a `publish_cloud` call (`:6873-6888`) — different seam, different arity (`(phase, group_result)` vs `(take_id, result, metadata)`), different event (`…_cloud_publish_failed` vs `…_position_retain_failed`). `self._seams.retain_position` occurs **exactly twice** in the 9,228-line file, `:5266` and `:5269`, both inside `_hand_to_retention`, which has exactly three call sites: `:5056`, `:5254`, `:7020` | **corrected — #3145 D6.** The real inlined copies existed and were collapsed by `27f13a4e4` (#2753); the file says so itself at `:6980`. §1's *"leaves a second writer behind"* hazard **does not exist**, and W1-c is smaller than scheduled. |

**The fourth-site premise was this plan's own, and it spread before it was
caught.** It is recorded here rather than quietly deleted, because that is what
this ledger is for. It had already propagated into the two tier-7 dissolution
maps, which #3145 corrected at the same time; the three assertions inside *this*
document (`§0`, `§1`, `§6`) were left standing then and are retired in this pass.

---

## 1. RecordStore + the retention lift

### What the protocol requires, and what pins it

`RecordStore` (`crossover_v2/session_seams.py:208`) is two pairs: `bank` `:236` /
`read` `:240` for one capture record, `persist` `:248` / `read_state` `:252` for
the session's own durable state. Both reads return `None` rather than raising —
`:244-245` for a missing record, `:256-260` for a state id that outlived its
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
own"* (`crossover_v2/position_cycle.py:37-38`), and `:53` calls it convenience
over a nested glob. It is written by `bank_position_cycle`
(`scripts/run-crossover-round.py:713`, write at `:735`) from one call site
(`:1001`) **only when `--angles` is passed**; `read_position_cycle` (`:351`) and
`takes_by_position` (`:392`) have **zero production callers** — tests only.

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
The coverage WAS uneven — lateral banked a curve with phase, entry banked
magnitude only, cloud and the unprompted phases banked no curve at all — which
made acceptance row 3's *"`DriverResponse` banked"* satisfied in spirit for one
kind and unsatisfied for the rest, and it was recorded as an open acceptance
question rather than as done.

**Closed.** `spatial.analysis_curve_records` now carries the same
`pose_curve_record` shape onto the other three builders — one `curves` key,
one shape for all four kinds, so the reader-flip below has one thing to parse
rather than four. CHECK banks an empty list because `_analyze_check` computes
no transfer function at all; every other kind banks the PRIMARY responses its
analysis produced (a MEASURE analysis's repeat occurrences stay diagnostic and
unbanked, exactly as the walk leaves them). The 12-point/octave sampling and
the absolute-phase caveat both stand, and nothing reads `curves` yet: this
closes the PERSISTENCE half of ruling S3, not the resolution question and not
the flip.

### The retention lift

`_hand_to_retention` (`crossover_v2_flow.py:5256`) is the one fail-soft boundary:
`retain_position is None` returns `False`, a raising seam costs a WARN and returns
`False` (`:5266-5278`). Three call sites — `:5056` (`_retain_lateral_pose`),
`:5254` (`_retain_cloud_position`, the method's last statement), `:7020`
(`_retain_entry_baseline`, **the only site that uses the return value**, feeding
`artifact_ref = take_id if stored else ""` at `:7023`). **Three, and only
three** — the "fourth, inlined copy" this plan once claimed at `:6879` is a
comment in a different seam's `except` arm (§0's ledger, #3145 D6).

**CHECK, MEASURE and VERIFY bank no take at all** — there is no fourth, fifth or
sixth call site, and `_consume_check` (`:4638`), `_consume_measure` (`:4690`) and
`_consume_verify` (`:7305`) hand nothing to retention. That is not an oversight
to preserve: it is the gap that makes `harmonic_evidence._bind_measure_captures`
and `round_views.verify_pose_curve` depend on the debug ring instead.

**The #3076 obligations, carried whole** (from that PR's body, recorded so they
cannot be lost):

1. Build the three missing retention paths (CHECK / MEASURE / VERIFY takes)
   **at the destination**, retaining into #3064's `take_id` convention.
2. Flip the sidecar's **seven** readers. Seven is right; the composition is
   **3 + 4**, not 4 + 3. **Three** call `.glob(RING_SIDECAR_GLOB)` —
   `evidence_packet.py:1246`, `harmonic_evidence.py:603`,
   `feature_classifier.py:456`; `round_views` globs nothing and reaches the ring
   through the first of those, by passing `dump_ring_dir=`. **Four** glob flat
   `*.json` — `audio_measurement/capture_integrity.py:193`,
   `scripts/harmonic-distortion-replay.py:260`,
   `scripts/severed-twin-replay.py:240`, and
   `scripts/derive-crossover-incident-fixture.py:138`, which globs
   `*_measure_*.json` over a pinned frozen bank and so breaks on the filename
   scheme as well as on the deletion. Fix the globbers onto the one index as you
   flip them.
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
**Scope settled by the 2026-08-26 FOLD ruling (§6.2), and the D12 gate on this
item lifts:** the five `V2FlowSeams` publishers — `publish_check`,
`publish_candidate`, `publish_cloud`, `publish_findings`, `publish_round_receipt`
— **fold into this store**, discriminated by `kind`; `EngineSeams` gains no sixth
field. **Fail-soft stays at the caller**, in a named wrapper on
`_hand_to_retention`'s shape (`crossover_v2_flow.py:5256`), never inside the
store and never as a flag — so `publish_candidate`'s must-not-fail-quietly
contract and `publish_findings`/`publish_cloud`'s deliberate fail-soft stay
visible in the code that calls them. `bank`'s docstring and the twin pin
(`tests/engine_twin.py:199-234`) widen from *one capture record* to *one record*
in the same PR.
*Size:* ~300 lines including pins, plus the publisher fold and the twin's growth. *Verification bar:* the twin's own contract run
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

**W1-c — the retention lift: three sites in, three phases added, the ring out.**
The #3076 obligations 1–4 in one PR, per S5. This is the largest single item in
the cutover and the one most likely to want splitting; if it splits, split by
*reader* (obligation 2) and keep obligations 1+3 together — a sidecar that dies
before its replacement writes is a data loss, and a sidecar that outlives it is
the second-writer defect.
*Size:* target < 400 lines; if it exceeds, split as above. *Verification bar:*
a CHECK, a MEASURE and a VERIFY capture each produce a banked take; the three
`RING_SIDECAR_GLOB` readers and the four flat globbers read the take instead;
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

### What `analyze` must produce

`AnalyzeOutcome` (`crossover_v2/session.py:194-215`): `results: Mapping[str, Any]`
keyed by **analysis name**, and `disclosures: tuple[CapabilityStub, ...]`.
`results={}` is hard-coded at `:461`. Two contract lines the registry inherits —
`:203-208` (*read an empty `results` as "nothing is wired to run yet", never as
"everything ran and found nothing"*) and `:210-212` (hand it a **copy**, not the
dict still being filled).

**There is no analysis-name vocabulary anywhere in the tree.** That vocabulary is
the registry's first deliverable, not a detail of its second.

### The layer, re-derived

`analyze_program_capture` (`jasper/audio_measurement/program_analysis.py:6054`,
returning `ProgramAnalysis` `:6063`) takes
`(program, samples, sample_rate, *, calibration, geometry, priors, capture_report)`.
Two corrections to the one-line summary the design doc carries: the WAV arrives
**already decoded** as `(np.ndarray, int)`, and there is a **fourth optional
input** — `capture_report`, the phone's own frame counters (`:6067-6073`).

Inside, it is a three-way phase dispatch — `_analyze_check` `:6141`,
`_analyze_measure` `:6310`, `_analyze_verify` `:7020`, `ValueError` on anything
else `:6124`. **That dispatch is the closest thing to a registry the layer
already has**, and the registry should read as its generalization rather than as
a new mechanism beside it.

`ProgramAnalysis` (`:1852`) is a frozen dataclass with **25 fields**, and field
presence is phase-conditional and documented in-line at `:1864-1980`. **That
per-field "which phase sets this" table is the de-facto input-kind gating that
already exists** — it is the answer, written down in the wrong shape.

**Purity, precisely:** file- and network-free (no `open`, `pathlib`, `requests`,
`json`, `subprocess` in the imports at `:61-98`) but **not side-effect-free** —
thirteen `log_event`/logger sites fire inside the call tree. A registry that runs
N analyses per bank multiplies journal volume by N; budget for it.

### PREMISE FAILED — the "92 analysis units" cannot be reproduced at HEAD

The figure appeared five times in the chunk-1 plan and was **never enumerated**.
Its cited evidence base — fragments `00`–`11` under
`captures/tuning-stack-inventory-2026-08/` — is gitignored (`.gitignore:40`) and
absent from the tree, so it cannot be checked. **W2-d restates all five**
against the committed method below.

Counted at HEAD by `ast.parse` over top-level nodes: **79** modules = 35
(`jasper/audio_measurement/*.py`) + 44 (`jasper/active_speaker/crossover_v2/*.py`),
which reproduces the plan's own *"62 of 79 in-product modules pure · 10 file
readers · 7 live transport"* (`:177`) exactly. No definition of "unit" reaches 92:
425 public top-level functions, 482 private, 253 classes, 87 functions in
`program_analysis.py` alone, 25 `ProgramAnalysis` fields. One numeric
coincidence, flagged as a coincidence: `jasper/active_speaker/*.py` has 92
top-level modules — but that set includes `crossover_v2_flow.py`, a god file
explicitly outside the truth layer.

There is also **no in-repo pin on the 79-module membership**: the zero-upward-
imports test `tests/test_correction_boundary_ssot.py:175` scopes to
`jasper/audio_measurement` only (`:187`).

**Disposition — settled by W2-d, and it took BOTH halves rather than either.**
The two numbers answer different questions, so neither substitutes for the
other: module membership says *which modules are in the truth layer*, and the
registry's coverage claim needs *how many named analyses the table holds*. W2-d
therefore ships the unit count with a committed method — the 20 produced
`ProgramAnalysis` fields that `tests/test_program_analysis_field_census.py`
counts from a fresh parse, grouped by gate into 15 units — **and** widens
`tests/test_correction_boundary_ssot.py` so both package roots have a
no-upward-import pin. 92 is not carried forward.

### Who calls analysis today

**From `crossover_v2_flow.py`: exactly one call, and it goes through a seam.**
`self._seams.analyze(program, result, priors, self._geometry, phase=phase)` at
`:4207`, inside `consume_capture` (`:4183`), routed by phase to six consumers
(dispatch `:4210-4227`). The flow then reads the result **by field** — 18 of the
25 are consumed — and makes only one direct call into the layer,
`polarity_label(...)` at `:8538`; everything else it imports at `:237-249` is a
type or a constant.

**From `web/correction_crossover_v2.py`: two, both in the binding.**
`_pa.analyze_program_capture(...)` at `:3144` inside `_analyze` (`:3074`), the
closure `bind_production_analyze` (`:3037`) returns — result goes to
`_maybe_retain_capture` (`:3163`) and back to the flow (`:3175`); and
`analysis_diagnostic_summary(analysis)` at `:3297`, for the debug sidecar only.

**The seam:** `V2FlowSeams.analyze` (`crossover_v2_flow.py:1504`), one of six
required fields, typed `AnalyzeCapture` — a **Protocol** (`:1437`), not a
`Callable` alias, with `__call__` at `:1463-1471`. Bound in production at
`web/…:5742`, in the only product `V2FlowSeams(...)` construction (`:5740`).

**The one property a registry must not break:** `phase` is required and
keyword-only *deliberately* (`:1447-1461`), so a refactor that drops it fails at
the call rather than silently falling back to `program.phase` — the #1855
mislabel, 32 of 45 retained sidecars. **Keep `phase` non-defaulted.**

### Where the gating vocabulary would come from

No analysis declares its inputs; every gate today is an ad-hoc `if x is None` at
a call site. Four near-vocabularies exist, and only one is about capture content:

- `MEASURE_KIND_*` (`contracts.py:1430-1437`) — capture *parameterization*
  (baseline · candidate · verify), not analysis inputs.
- `CapabilityStub` (`measure_spec.py:92`) — **the disclosure half, already
  built.** Four codes (`:78`, `:81`, `:84`, `:88`), a `_ROWS` dict-literal
  (`:159`), `STUB_CODES` derived not re-listed (`:184`), and
  `stubbed_capabilities(spec)` (`:315`) — four `if`s on spec fields, total and
  side-effect-free, called from `session.py:406`. **Two of the four codes say in
  as many words that the missing piece is an `analyze` consumer** (`:78-79`
  R-3's splice, `:84-86` R-4's distortion floor).
- `MeasureSpec` (`:188`) — the parameters, not the content.
- **`STIMULUS_KINDS`** (`audio_measurement/program.py:162` — pilot · sweep ·
  summed-sweep; `KIND_SILENCE` `:158` is deliberately **outside** the set) — the
  only existing vocabulary that describes **what a capture actually contains**,
  and therefore the natural seed for the predicate.

### Recommendation: model it on `tools/packs.py`, and keep it out of `EngineSeams`

**Shape.** `jasper/tools/packs.py` is the closest prior art in the repo and it
already carries the exact split this needs: `CapabilityPack` (`:131`) has
`gate: Callable[[Any], bool] = lambda _d: True` (`:147`); `PackOutcome` (`:159`)
has `status` — `"registered" | "skipped" | "failed"` (`:182`) — and its docstring
distinguishes **skipped** (gate returned `False` — *"Expected, not a fault"*)
from **failed** (raised). That is `AnalyzeOutcome`'s not-run-versus-error split,
already shipped. Registration is a module-level tuple literal (`TOOL_PACKS`
`:281`, order load-bearing per `:14` and pinned); iteration is `register_packs`
(`:380`) with per-unit `try/except` isolation and `if not pack.gate(deps):`
(`:437`); `outcomes_to_state` (`:187`) is the one wire shape.

Second-closest and in-package: `refusal_copy.REASON_REGISTRY` (`:790`), the
repo's canonical *table-as-data, derived sets never re-listed* idiom
(`:1307-1311`, `:1549-1551`) — the same one `STUB_CODES` uses. Minimal form:
`jasper/cues/registry.py` (`CueDef` `:29`, `CUES` `:42`, `find` `:164`).

**No decorator-based and no entry-point registration exists anywhere in
`jasper/`.** Every registry here is a module-level literal walked by a function.
A `@register_analysis` decorator would be a new idiom with no precedent, and it
would break the one-place-to-add-a-row argument `measure_spec.py:157-158` and
`refusal_copy.py:788` both make explicitly. **Use a literal table.**

**Home: an in-layer import, not a sixth seam.** `EngineSeams` has no `analyze`
field (`session_seams.py:299-303`) and `analyze()` is deliberately seam-free
(`session.py:459-463`). Adding a seam would make the registry injectable —
i.e. optional — which is the opposite of *wholesale*. The caller imports the
table. That is what *"do not decouple the analysis layer, replace its caller"*
(`session.py:440`) means operationally.

**One failure mode to design against, by name.**
`verification._crossover_region_null_registry` (`verification.py:2294`) is called
a registry and is a single query function; its docstring (`:2301-2313`) records
the defect the wholesale default exists to prevent — the detector *"did not
return 'unknown,' it was **never asked** — its band excludes the region."* A
gate that answers `False` for the wrong reason reproduces that exactly, which is
why the skipped outcome must carry **which input was missing**, not just that one
was.

### Work items

**W2-a — the name vocabulary and the table.** A new module under
`crossover_v2/` holding `AnalysisUnit(name, fields, gate)` and the literal tuple,
with the gate predicate seeded from `STIMULUS_KINDS` + `MEASURE_KINDS` + the
`ProgramAnalysis` per-field phase table (`:1864-1980`). **No caller yet** — this
PR adds a table and its pins only.
*Size:* ~280 lines. *Verification bar:* every unit's `name` unique; every `gate`
total (a property test over generated banks asserting no gate raises); the
derived name set is derived, not re-listed. *Tier:* default.
**Landed as #3165 (`762715e24`) with `fields` and deliberately no per-unit
`run`:** the analysis layer has one entry point, so fifteen `run` placeholders
would have been throwaway scaffolding — ruling S4. The reviewer accepted the
deviation as additively recoverable and recorded one forfeit, which is what
re-words W2-b's bar below.

**W2-b — the walker replaces `results={}`.** `TuningSession.analyze` iterates the
table, calls `analyze_program_capture` once, and projects each passing unit's
`fields` out of that one `ProgramAnalysis` into the results copy.
**Isolation is per-unit at the gate and per-walk at the produce half** — with no
per-unit `run`, one raise inside the single analysis entry
(`program_analysis.py:6054`, phase dispatch internal) fails **all fifteen
produce-halves together**. `packs.py:439-441`'s `try/except` shape therefore
applies to the gate half, which is the half that is per-unit; the produce half
gets one `try/except` around the walk. The forfeit is recorded at
`analysis_units.py:70-76`.
*Size:* ~150 lines. *Depends on:* W2-a, W1-a (the store's `read`, which is what
makes `analyze` offline), W4-a (colour). *Verification bar:* a bank with one
missing input kind produces N−1 results and one skip naming the input;
mutation-verified **at the gate**, which is where per-unit isolation is real —
make one unit's gate answer `""` for a bank that lacks its input and watch that
unit alone leave the disclosure set, with no other unit's outcome moving.
`failed` and `skipped` stay distinguishable, but at the **walk** level: a raise
from the analysis entry marks the walk failed, not one unit. *Tier:* default.

**W2-c — the per-analysis not-run disclosure.** Render each skipped unit as a
`CapabilityStub` in §1's wording — *"no distortion analysis: no
distortion-vs-level capture in this session"* — carrying **which** input was
missing. This is the item that closes the `_crossover_region_null_registry`
failure mode.
*Size:* ~120 lines. *Depends on:* W2-b. *Verification bar:* the disclosure names
the missing input kind, asserted as a structured field and never as prose.
*Tier:* default.

**W2-d — settle the 92.** **Both halves, not either** — see the disposition
above for why one does not substitute for the other. Enumerate the units with a
committed counting method, shipped as a CI pin rather than as prose, **and**
widen `test_correction_boundary_ssot.py:175` to cover `crossover_v2` so the
truth layer's membership has a direction pin at all. The five plan sites restate
against the method. *(The "acceptance row 3c" this item used to name is a
mis-citation: row 3c is Front-end sharing (MS-17), and the analysis-unit claim
is not an acceptance row — it lives in the plan's §0 non-goals and §1 diagram.)*
*Size:* ~60 lines, plus the census pin. *Independent — schedulable first.*
*Tier:* default.

---

## 3. Recommender binding

### What the seam is, and what is bound to it

`Recommender` is a bare type alias — `Callable[[Sequence[str]], Mapping[str,
Any]]` at `crossover_v2/session_seams.py:271`, exported at `:65`, and the
`EngineSeams.recommend` field's type at `:303`. Those three sites are its **only**
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
   `RecordStore.bank` (`session_seams.py:236`). The prescriber takes an
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
- It honours the seam docstring's standing order at `session_seams.py:266-268` —
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
| `SessionGraph` | `install` · `patch` · `restore` | `session_seams.py:105` · `:121` · `:132` |
| `VolumeClaim` | `acquire` · `prove` · `release` | `:162` · `:171` · `:198` |
| `RecordStore` | `bank` · `read` · `persist` · `read_state` | `:236` · `:240` · `:248` · `:252` |
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
`_run_async` (`:1292`), which is
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
(`session_seams.py:162/171/198`). So the production `VolumeClaim` is a small
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
| the loop bridge | `_ensure_loop` `:1275`, `_run_async` `:1292` |
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
  `_release_pause_best_effort` (`:1309`), and `_set_relay_capture(None)`. (The
  fourth item this bullet once named, `release_session_measurement_graph`, is
  gone — deleted by #3240.)
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
1,171 lines) splits cleanly, and the split is the section's decision.**

*What dissolves into `VolumeOwner`:* the fader writes. The class **owns no
CamillaDSP** (`:670`) — `open`, `close`, `abandon`, `enforce_ceiling` and
`recover_unresolved` all take `(set, get)` as *parameters*. Swapping the injected
door for a `VolumeClaim` over the owner therefore needs **no internal
restructure**: `acquire`/`prove`/`release` (`session_seams.py:162/171/198`) map
onto `acquire_level`/`prove`/`release` (`volume_owner.py:261/582/504`) through
the handle-holding adapter W4-c builds.

*What survives, and must not be dissolved:* **the durable restore latch.**
`VolumeOwner` is explicitly in-memory and per-process — *"Durable volume-safety
state belongs to the claim holders that own it"* (`volume_owner.py:65-68`). The
owner provides none of:

- the persisted `_State` (`:590-596`) at
  `/var/lib/jasper/active_speaker_crossover_session_volume.json` (`:109-110`),
  `SCHEMA_VERSION = 1` (`:80`);
- `original_main_volume_db`, snapshotted in `open` (`:906`) at `:938-944` and
  persisted **before the first fader mutation** — the code says so in a comment,
  `:945-946`;
- `_drain_restore`'s (`:1056`) exact→emergency ladder (candidates built at
  `:1076-1081`) and the `session_volume_restore_unconfirmed` latch (`:1100`);
- `needs_recovery`'s two branches (`:744-748`) — a latched `unresolved` state,
  **or** a durably `active` state this process did not open (crash/restart
  hydration);
- the 1,800 s wall-clock ceiling (`DEFAULT_WALL_CLOCK_CEILING_S` `:87`) under the
  1-hour hard cap (`MAX_WALL_CLOCK_CEILING_S` `:96`).

That set **is** design-doc contract 9, the walked-away guarantee, and it does not
move. Its four drain entries are `close` `:1109`, `abandon` `:1121`,
`enforce_ceiling` `:1133` and `recover_unresolved` `:1153`. One ordering
invariant rides with it and is easy to lose in a rewrite: `_clear_resolved`
(`:858`) drops the in-memory intent **before** persisting, and its docstring
(`:862-880`) records both halves — the measured **+47.5 dB** (−60.0 → −12.5)
hazard of the other order at `:868`, and the mirror-image persist guard at
`:875-880`.

### Two stale facts to true up while here

*A third — `owned_measurement_volume_db_nowait`, which this plan reported as a
live zero-caller orphan — was **deleted on `main` by #3140** while this document
was in review, together with the two docstrings that still claimed a consumer for
it and the three test assertions that pinned it. Recorded rather than removed:
the finding was correct when made, and it is closed.*

1. **`held_target_db` is gone from code but live in two docs.** Zero `.py` hits
   at HEAD (6e / #3137); `camilla.py:106-109` now states the exception is gone.
   But `docs/adr/0004-duck-release-algebra-and-reference.md:101-106` and
   `crossover-v2-engine-design.md:252-268` both still describe it as live
   plumbing, the latter in the future tense (*"until 6e lands"*). ADR-0004 is
   append-only, so the fix is a superseding note, not an edit.
2. **The ledger's enumerated-set check cannot see one writer.**
   The chunk-1 writer ledger grepped `set_volume_db(` with a trailing
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
(`session_seams.py:295-296`) lands with it — it now has a front end to point at.
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

**W5-d — true up the two stale docs.** Add the ADR-0004 superseding note and
re-tense the engine design doc's 6e paragraph (`:252-268`), so neither still
describes `held_target_db` as live plumbing; widen the ledger's writer grep to
catch a bare bound-method reference and re-run it. *(The orphan half of this item
was closed on `main` by #3140 — see above.)*
*Size:* ~60 lines. *Verification bar:* zero `held_target_db` occurrences in
`docs/` that describe it in the present tense; the widened grep's own output
pasted into the PR. *Tier:* docs — author judgment plus a sanity look.
Independent of every other item — **schedulable first.**

## 6. God-file dissolution map

Coarser than §§1–5 by design: a reliable map, not a line-by-line audit. Line
ranges are approximate and will drift as items land — re-derive before cutting.

### `jasper/active_speaker/crossover_v2_flow.py` — 9,228 lines

`CrossoverV2Session` is `:2154-8978` — **6,825 lines, 74% of the file**, 158
methods (43 of them `@property`), with a **777-line `__init__`** at `:2181-2957`.
Roughly 53% of the file is prose: ~2,381 docstring lines and 2,483 comment lines,
almost all *justification essays* attached to constants and to `__init__` field
declarations. Refusal copy and UI strings are **not** here — that vocabulary
already moved to `crossover_v2/refusal_copy.py` (see the note at `:524-539`).

| Concern | ≈ lines | Absorbed by |
|---|---|---|
| module charter | 1–107 | dies with the file (design doc owns it) |
| imports / TYPE_CHECKING | 109–253 | dissolves |
| **four re-export barrels** — phase vocabulary, fc-sweep, capture-plan/geometry/tiers, refusal copy | 255–630 | **delete.** Historical-name doors; #3076 already killed a fifth |
| tuning constants + their essays | 631–877 | **RULED — 6.1.** Seven constants are passengers of the rows that hold their readers; one goes to `contracts.py`; the essays become ADRs |
| pure helpers: alignment plausibility · capture integrity/SNR fields · VERIFY evidence/claims · SNR log fields | 878–1395 | §2 (they are analyses) |
| `AnalyzeCapture` · `RecordModelError` · `V2FlowSeams` · `V2ConductorSnapshot` | 1428–1698 | `V2FlowSeams` → §5 (`EngineSeams` replaces it); `V2ConductorSnapshot` → §1 |
| attempt-history serialization | 1699–1923 | §1 (`persist`/`read_state`) |
| cloud combine (emitting half) · candidate-build shims | 1879–2153 | §2 |
| **`__init__`** | 2181–2957 | dissolves into acceptance row 5's four destinations |
| program composition · priors per phase | 2959–3197 | `crossover_v2/programs.py`; the parameterization is `MeasureSpec`'s |
| journey delegation | 3198–3211 | already delegating; deletes |
| **43 read-only accessors** | 3212–3742 | **re-triage.** Row 4 keeps 402 lines of these as the host adapter — but §5 replaces the host, so the ones the status block reads become `MeasureOutcome`/`AnalyzeOutcome` fields and the rest die |
| UI prompt payload building | 3743–3865 | `MeasureSpec.pose_prompts` + the record's `prompt` |
| lifecycle + `snapshot`/`hydrate` | 3866–3969 | §1 |
| **relay callbacks** `authorize_begin` · `on_armed` · `consume_capture` | 3970–4626 | **§5** — this is what `measure()` replaces |
| CHECK · MEASURE · lateral · cloud verdicts and retention | 4627–5279 | §1 (W1-c) + §2 |
| cloud group close + speculative close | 5280–5766 | §2 + §5 |
| **candidate build · publish · commit** | 5839–6334 | **RULED — 6.2.** 54% is pure compute with homes already; the commit half never applies; the five publisher seams **fold into `RecordStore`** (owner, 2026-08-26) |
| findings/evidence publishing + refusal | 6335–6728 | refusal → the organ; findings → §1 |
| cloud pipeline runner (**no retention here** — `:6879` is a comment in a `publish_cloud` `except` arm, not an inlined copy; §0) | 6729–6913 | §2 + W1-c |
| entry baseline | 6914–7048 | §1 |
| the round, graded | 7049–7304 | §2 |
| VERIFY verdict + attempt grading | 7305–7924 | §2 (`measure(kind=verify)` then `analyze`) |
| delta probe | 7925–8355 | §2 |
| **diagnostic logging** (four big per-phase emitters, feed no verdict) | 8356–8728 | **RULED — 6.3.** Neither: they are ADR-0143 plane-1 lines with 20 live pins, and they ride their verdicts into §2 |
| gate/candidate/linearization helpers | 8729–8978 | organs |
| production playback seams | 8979–9094 | §5 (`PlaybackTransaction` binding) |
| session-volume lifecycle | 9095–9164 | §5 (W5-c) |
| `__all__` (63 names) | 9166–9228 | dies with the barrels |

### `jasper/web/correction_crossover_v2.py` — 8,088 lines

Not a page: zero routes, zero HTML, zero CSRF (§0, §5). ~47% prose. Sixteen
banner comments mark its own sections.

| Concern | ≈ lines | Absorbed by |
|---|---|---|
| capture-dump retention config (+ impl at 3179–3361) | 189–229 | dies with W1-c, gated on the owner's `ENABLED` ruling |
| refusal / error taxonomy | 238–446 | clean leaf → `crossover_v2/refusal_copy.py` |
| durable JSON state I/O | 449–576 | §1 |
| journey-state observers | 579–1070 | §5 + `crossover_v2/journey.py` |
| **process-global volume / pause / graph singletons** | 1073–1459 | §5 (W5-b, W5-c). **Four module-level mutables** (`:235`, `:1126`, `:1127`, `:1191`) — move them together or they race |
| **JSON status projection** (`crossover_v2_status_block` `:1995`) | 1462–2164 | **the cleanest large slice**: pure read-side, no I/O beyond `load_v2_state`. Lifts whole to its own module |
| post-apply grading (`_post_apply_grade`, 296 lines) | 2167–2543 | §2 |
| staged-prescription / angle-walk intake | 2583–2805 | §3 (W3-b) |
| conductor persistence (write side) | 2809–2927 | §1 |
| capture/analyze seam binding + calibration | 2935–3361 | §2 (W2-b) + W1-c |
| evidence store + publishers | 3364–3962 | §1 (W1-a) |
| `bind_production_play` (374 lines, 6 nested `async def`) | 3965–4338 | §5 |
| volume hooks + group close | 4341–4433, 5390–5457 | §5 — **one concern split 1,000 lines apart** |
| conductor context resolution | 4436–4989 | §5 |
| **`PositionGate`** (280 lines) | 4992–5351 | **cleanest extractable class**; own module. Its one URL string (`:5069`) duplicates `arm_walk.py:427` — converge them |
| `V2PreparedSession` | 5355–5387 | §5 (W5-a) |
| applied-graph / rollback-anchor introspection | 5476–5672 | **stays** (apply-adjacent) |
| stage-seam binding · capture-source resolution | 5675–5941 | §5 |
| **the two preparers** (916 lines, near-duplicate twins) | 5944–6901 | §5 (W5-a) — the biggest de-dup win in either file |
| **apply transaction** (`handle_v2_apply`, 399 lines) | 6904–7360 | **STAYS.** Row 6's kept 1,185 lines |
| delta-probe rollback · restore/Undo · apply-blocked tail | 7363–8088 | **stays** |

### 6.1 RULED — the constants are passengers; the essays are the decision

**Tier: CONDUCTOR.** Ruling S7
([ADR-0228](adr/0228-rulings-carried-out-of-refactor-tuning-on-its-retirement.md))
and R7 already supply the mechanism, and a caller census removes the placement
question. Nothing here is taste.

**What the block IS.** `:631-877`, 247 lines, under a banner that read
*"tuning constants (PROVISIONAL pending W6 bench validation)"* (`:632`) — RULED
deleted, §6.1's close below. Fifteen names, and they are three unrelated
things:

- **Six re-export doors** — `GAIN_CAP_BACKOFF_DB` `:637`, `PILOT_LEVEL_DELTA_DB`
  `:641`, `LOCATE_MIN_CONFIDENCE` `:644`, `VERIFY_TOLERANCE_DB` `:647`,
  `courtesy_prelude_for_phase` `:869`, `CrossoverV2FlowError` `:875`. Each is
  `X = _module.X` with a comment naming the owner. They are not constants; they
  are the same thing row 3's barrels are, under a different banner — but they sit
  in **this** range, not in `:255-630` (none of the fifteen names appears there),
  so **this** row deletes them, on row 3's argument rather than in row 3's PR.
  Two carry live warnings: `courtesy_prelude_for_phase`'s comment (`:864-868`)
  says **nothing in this module reads it any more** and names the test that pins
  the door — *"do not delete it as dead on an importer grep alone"* — and
  `CrossoverV2FlowError` is the widest-used name in the block, raised at 13 sites
  in this file and imported across `angle_capture.py`, `capture_plan.py`,
  `web/correction_crossover_v2.py` and 13 test files **from `contracts.py`, its
  real owner** (`contracts.py:121`).
- **One function**, `verify_absolute_tolerance_db` `:650` (34 lines incl. its
  docstring), which derives a tolerance from `flat_spec.SPEC_BANDS` `:677-683`.
  That is an analysis, not a constant. It rides the pure-helper row
  (`:878-1395` → §2), and its in-file readers `:1133`/`:1156` are in that row.
- **Eight real definitions.** Below.

**Every one of the eight has zero production readers outside this file** — one
exception, and it is a script. Counted wrap-safe over `jasper/`, `scripts/`,
`rust/`, `deploy/`, `tests/`:

| Constant | def | In-file readers | Outside `crossover_v2_flow.py` | §6 row owning the reader |
|---|---|---|---|---|
| `CLIP_RETRY_BACKOFF_DB` | `:639` | `:4820` | none (prose only: `program_analysis.py:5806`) | 4627–5279 |
| `MEASUREMENT_DISTANCE_M` | `:687` | `:2371` | none | **`__init__`** 2181–2957 |
| `ALIGNMENT_CONFIDENCE_TRUST_FLOOR` | `:698` | `:4767 :4775 :4854` | none (prose: `durable_state.py:822`, `capture_dispatch.py:282`) | 4627–5279 |
| `ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS` | `:711` | `:918 :942` (default args) | **`scripts/severed-twin-replay.py:302`** | 878–1395 |
| `MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB` | `:767` | `:4724 :4731 :4849` | none (prose: `program_analysis.py:1589 :6823`) | 4627–5279 |
| `VERIFY_PILOT_TRANSFER_STEP_CEILING_DB` | `:783` | `:7766 :7790 :7904 :7922` | none (prose: `refusal_copy.py:243`, `capture_dispatch.py:784 :810`) | 7305–7924 |
| `VERIFY_REPEAT_FLOOR_DB` | `:848` | `:2863 :7642 :7650 :7658 :7683` | none | 7305–7924 |
| `VERIFY_TERMINAL_OUTCOME_DETERMINISTIC` | `:857` | `:7845` | none | **`contracts.py`** |

**So "each constant to its consuming organ" was never a decision — the consuming
organ is this file.** Seven of the eight are **passengers**: they move in the PR
that moves the row holding their reader, and §6's row should say so rather than
reserve a choice nobody has to make. Six of the seven ride two rows (4627–5279
and 7305–7924, both → §2); `MEASUREMENT_DISTANCE_M` rides `__init__`;
`ALIGNMENT_DELAY_PLAUSIBILITY_MARGIN_MS` rides the pure helpers **and is the one
that needs an importer rider** — `scripts/severed-twin-replay.py:302` imports it
by name.

**The eighth is the only real placement call, and `contracts.py` wins it for the
reason it loses the other seven.** `VERIFY_TERMINAL_OUTCOME_DETERMINISTIC` is a
**wire-contract slot value**, not a threshold: the capture-session code
reads `terminal_outcome` as an opaque string and never branches on it, exactly as
`:855-856` claims. `contracts.py` is already where that class of name lives —
`MEASURE_KINDS :1433`, `POLARITIES :1451`, `POSITION_AXES :1456`, `ADOPTION_ROWS
:1135`, `ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED :1408`.

**And the route is already cut and named.** `contracts.py:1374-1376` carries a
banner reading literally *"constants the flow used to own"*, holding the three
that already made the trip — `DEFAULT_CLOUD_MEASURE_POSITIONS :1398`,
`VERIFY_TOLERANCE_DB :1402`, `ATTEMPT_METRIC_VERIFY_MAX_NOTCH_EXCLUDED :1408`.
**All three are shared vocabulary**: `VERIFY_TOLERANCE_DB` is imported directly by
`program_analysis.py` and `tests/test_active_speaker_delta_probe.py`, and a
position count is read by the plan and the record alike. That is the membership
test, and **the eight in this block fail it** — none has a reader outside this
file. **The "one `contracts` block" option is wrong for the other seven** for the
reason it is right for the eighth: parking a threshold read by exactly one
analysis in the shared-vocabulary module separates the number from the only code
that reads it — the god file's own defect, rebuilt at package scale.

**One coupling the executor must not trip.** Five of the eight are named in
`__all__` — `ALIGNMENT_CONFIDENCE_TRUST_FLOOR :9214`,
`MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB :9215`,
`VERIFY_PILOT_TRANSFER_STEP_CEILING_DB :9218`, `VERIFY_REPEAT_FLOOR_DB :9219`,
`VERIFY_TERMINAL_OUTCOME_DETERMINISTIC :9220` — and **§6's row 3 schedules
`__all__` for deletion first, in the deletion order's step 1.** Five passengers
are listed in a manifest that is cut before their carriages move. Cut the names
with the constants, not with the list.

**Two of the eight have no pin at all.** `CLIP_RETRY_BACKOFF_DB` and
`MEASUREMENT_DISTANCE_M` each have exactly one in-file reader and **no import
anywhere in the tree — not production, not tests, not scripts** (the only other
occurrences are prose). They move unobserved, so their move needs a pin written
first, on wave 0d's rule: *"any row that comes back with no name gets a pin
WRITTEN — before the old pin is deleted."*

**The decision §6 actually reserved is the prose, and S7 already settled it.**
Counted, not estimated: **185 comment lines + 26 docstring lines = 211 of the
247**, against **17 lines of code** and 19 blank. It is not narration. Four
classes:

| Class | Instance | Disposition |
|---|---|---|
| measurement provenance with a named primary source | `VERIFY_REPEAT_FLOOR_DB`'s derivation from `captures/repeat-floor-20260731/README.md` (`:789-797`) | **ADR** |
| a dated owner ruling | the ripple's *"converted from a refusal to a disclosure by owner ruling on 2026-08-03, issue #2087"* (`:713-714`); the trust floor's nanny-burn-down demotion (`:691-697`) | **ADR** |
| an SSOT claim with a recurrence | *"This composition is OWNED here; cite this comment rather than re-quoting a count"* (`:720-721`) | **ADR**, then cited — never re-quoted |
| a non-derivable maintainer trap | *"a maintainer must NOT 'tighten' this toward 0.17016 believing it moves safe-ward: it moves the other way"* (`:822-824`) | **stays a comment** |

S7: a ruling that still binds the new engine and lives only in a docstring
*"becomes an ADR in `docs/adr/`, and the docstring shrinks to `See ADR-NNNN`"* —
range **ADR-0002–0099**, of which **0020–0099 are free at HEAD** (0001–0019 and
0100+ are taken). R7 makes the extraction *"a prerequisite, not a nicety."* The
precedent is already on `main`: **ADR-0010 was cut from two docstrings in this
exact file** and says so (`0010-candidate-build-commits-nothing.md:19-23`).
**No ADR owns any of the fifteen names today** — `docs/adr/` returns zero for all
of them — so this is new record, not a duplicate one.

**Shrink does not mean strip, and the tree already shows the size.** The two
constants that made the trip to `contracts.py` **kept right-sized essays**:
`DEFAULT_CLOUD_MEASURE_POSITIONS` carries 20 lines (`:1378-1397`, citing a dated
adjudication and a *"treat it as a constant, never as a promise about accuracy"*
caution), `VERIFY_TOLERANCE_DB` carries 3 (`:1399-1401`). S7 governs the
**ruling-class** content — the dated decision, the corpus, the derivation — not
every line above a `=`. What survives beside the constant is the trap plus a
pointer; what leaves is the argument.

**One essay is load-bearing in a way a reviewer will not guess.**
`MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB`'s comment declares itself the single
source for the 13-capture corpus composition **and names the incident that made
it so** — *"issue #2015 traced a since-corrected 12-capture, two-chain
restatement elsewhere to a copy that dropped the phone measure"* (`:721-723`).
That is a ruling with a recorded recurrence. Deleting it into git history
restores precisely the drift #2015 recorded, and re-typing it anywhere creates
the second copy it exists to prevent.

**Work item W6-a — extract the rulings; the constants then ride for free.** One
**docs-only** PR: four ADRs (the trust floor's demotion · the ripple's #2087
conversion *and* its corpus composition · G3's pilot-transfer evidence ·
`VERIFY_REPEAT_FLOOR_DB`'s derivation with its `CLAIM_FLOOR_P95_MULTIPLE`
second-spelling note at `:799-809`). No code moves. Each later row then carries
its own constants and shrinks their comments to `See ADR-NNNN` plus the trap.
**Plus two pins written for the two unobserved constants**, on wave 0d's rule.
*Size:* 4 ADRs + 2 pins, **0 lines of production code touched.** *Independent —
schedulable at tier 0*, and it is a **hard prerequisite** for rows 4627–5279,
7305–7924 and `__init__`: none of them may cut prose before it lands. *Verification bar:* every
deleted essay paragraph is quoted verbatim in exactly one ADR, and the surviving
comment cites it — mutation-checked by grepping the tree for a second copy of the
13-capture composition. *Tier:* mechanical (docs).

**One stale-looking label — RULED (owner 2026-08-29): delete it.** The banner
(`:632`) and five of the eight essays said *"PROVISIONAL pending W6 bench
validation"*, while `historical/crossover-measurement-productization-design.md:25`
records **W1–W6 complete** (2026-07-19) and the trust floor's own text
(`:693-695`) already cited a live bench datum that *"undercut it."* Owner
ruling, verbatim: *"i dont care about the banner. delete it."* The banner and
its in-repo restatements are deleted; `docs/adr/0181` and `0182` keep their
quotes of it unedited, as the record of what it read when each ADR was
written.

### 6.2 RULED — this block never applies, 53% of it already has a home, and the five publishers FOLD

**The row conflated two different things, and the correction is checkable.** The
range is `:5839-6334` — 496 lines, seven methods, **all seven internal to
`CrossoverV2Session`**: zero callers in `web/correction_crossover_v2.py`, zero in
`scripts/`, and the only entries are three sibling methods outside the range
(`_measure_verdict` `:4948`, `_close_measure_cloud_candidate` `:5815`/`:5817`,
`run_speculative_group_close` `:5647`).

| Method | lines | What it is |
|---|---:|---|
| `_publish_measure_candidate` `:5839` | 45 | a **3-line body** (`:5880-5882`) under a 41-line docstring — the pre-rider compatibility door |
| `_build_measure_candidate` `:5884` | 82 | **pure.** *"Commits NOTHING"* (`:5890`); zero `self._x =`, zero seam fires; returns `_SpeculativeClose` |
| `_previous_graph_predicted_sum` `:5966` | 143 | **near-pure** model of the graph an apply would replace; one in-memory dedup flag `:6096` guarding one INFO line |
| `_commanded_delta_for` `:6110` | 19 | **pure** |
| `_declared_transfer_for` `:6130` | 19 | **pure**, and a `@staticmethod` — no `self` at all |
| `commit_intervention_proposal` `:6150` | 104 | **the commit.** Seven session-state writes, then `publish_candidate` `:6251` and `_publish_level_frame_finding` `:6252` |
| `_commit_measure_candidate` `:6254` | 81 | orchestration + one disclosure log; its only production caller route |

**RULED (conductor) — 263 of the 496 lines are compute and §2 already owns
them.** The four pure/near-pure methods are analyses by the same argument §6
applies to the pure-helper row (`:878-1395`) and the delta probe (`:7925-8355`).
They move with §2, not with a publish organ. `_publish_measure_candidate` is a
door whose body is three lines; it dies with the callers that still take it
(`:4948`, `:5817`). **That leaves 185 lines — `commit_intervention_proposal` plus
`_commit_measure_candidate` — as the only thing needing a home.**

**RULED (conductor) — "irreversible" here does not mean the apply transaction,
and the row's citation borrowed the wrong authority.** *"Not a target. Ever."*
is about `handle_v2_apply`, where the one place it is still written at HEAD is
`session_seams.py:44-45` (the retired chunk-1 plan said it twice: *"the one
irreversible act and the only path that writes a live DSP graph"*).
**None of the seven methods touches CamillaDSP, ALSA, or any apply path** —
`_publish_measure_candidate`'s own docstring says *"nothing it returns triggers
an apply"* (`:5848`), and ADR-0014 records the two separate human acts that stand
between (*"the confirmation that makes a candidate real, and the POST that
applies it"*). The two are structurally independent: `handle_v2_apply` never
calls into `CrossoverV2Session`, and reaches this block's output only by reading
`candidate.json` back out of the evidence store (`web/…:7027`, `:7040-7049`).
**The irreversible acts in scope are one evidence-artifact write and a fire-once
session guard** (`:6265` calls them *"the two irreversible acts"*), which is a
much smaller thing than the row implies — and the reason they can be re-homed at
all.

**So the surviving question is not "where does this code live" but "what seam
does it fire," and that one is real.** `V2FlowSeams` carries **five publishers**
— `publish_check` `:1505`, `publish_candidate` `:1506`, `publish_cloud`,
`publish_findings`, `publish_round_receipt` (`:1428-1698`). **`EngineSeams` has
none**: its five fields are `graph`, `volume`, `records`, `play`, `recommend`
(`session_seams.py:299-303`). The engine's `save` is not the answer and does not
claim to be — it writes *"the session's own state"*, exactly the five keys
`PriorBank.read` reads back (`session.py:481-511`). **A candidate publication has
no landing in the engine's contract today.**

**And §6 has an internal inconsistency here that the executor must not inherit.**
The web-file table routes *"evidence store + publishers | 3364–3962"* — which is
where all five publishers are actually bound (`bind_evidence_publishers:3392`,
`bind_round_receipt:3456`, `bind_findings_publisher:3803`,
`bind_cloud_publisher:3921`) — to **§1 (W1-a)**. But W1-a's own text (`:255-265`)
scopes it to `bank` / `read` / `persist` / `read_state` and says nothing about a
publisher. **One side of the same seam is assigned; the other is marked NO HOME.**
The ruling below closes that disagreement, and W1-a's scope line is edited to
match it.

---

**RULED (owner, 2026-08-26) — FOLD. `RecordStore` is THE durable-write seam.**

The five `V2FlowSeams` publishers fold into `RecordStore`; **fail-soft stays at
the caller**, on `_hand_to_retention`'s shape, not inside the store and not as a
seam flag. `EngineSeams` keeps its five fields — there is no sixth seam. The
brief's reasoning is kept below as the recorded *why*.

*Context, plainly.* The engine talks to the outside world through exactly five
injected slots. Writing a durable file is one of them (`records`). But the old
flow had five *separate* slots for writing durable files, one per artifact kind,
and two of the five are deliberately allowed to fail quietly while one of them —
the candidate — must never fail quietly. When the old seam set is replaced by the
new one, those five either fold into the one store or the store gains a sibling.
**The boundary is one-writer-with-a-label versus two-writers-with-two-contracts.**

*Why fold — the recommendation the owner adopted.*

- **Cost of folding.** `bank`'s docstring and its twin pin (`tests/engine_twin.py:199-234`)
  currently mean *one capture record*; carrying a candidate, a check, a cloud
  result, a findings record and a round receipt widens that meaning, and the twin
  and W1-a both grow. W1-a sits at tier 2 feeding the W5-b join, so the growth is
  on the critical path.
- **Cost of a sixth seam.** `EngineSeams` stops being five fields, which is the
  number the design doc, `session_seams.py:275-297` and **all of §4** are written
  against. §4's colour flip (W4-a) is the tier-1 gate every later item waits on;
  changing the dataclass's shape in the same window is a merge collision with the
  one item that cannot slip.
- **Why fold wins.** The store already exists and is *under*-used — §1 records
  that `read` and `read_state` have **no engine caller at all**. Five names for
  "write a JSON artifact into `evidence/v1`" is the duplication the charter's
  converge-or-open-an-issue default is about, and `kind` is already how this
  engine discriminates records. The one real objection — that `publish_candidate`
  is irreversible while `publish_findings`/`publish_cloud` are fail-soft by design
  (`web/…:3636`, `:3832`) — **does not need a store feature**, because this repo
  already puts fail-soft in a named wrapper rather than in the seam:
  `_hand_to_retention` (`:5256`) is exactly that shape around `retain_position`.
  Keep the store plain, keep the wrapper, and the distinction stays visible
  instead of becoming a flag.

*What the owner decided:* `RecordStore` is **the** engine's durable-write seam,
not **a** durable-write seam. W1-a's scope line is edited to match (§1).

### 6.3 RULED — the emitters are plane-1 observability; both offered options are wrong

**Tier: CONDUCTOR.** Both branches of the row's either/or are refuted by
something already written down; what is left is not a choice.

**What the block IS.** `:8356-8728`, 373 lines — **250 code, 96 comment, 21
docstring, 6 blank** (AST-classified, not eyeballed). Five defs, not four:

| Def | lines | Event | Fields | Fires from |
|---|---:|---|---:|---|
| `_safe_log_diag` `:8363` | 25 | `…crossover_v2_diag_log_failed` (WARNING, failure path only) | 4 | the three `_consume_*` sites |
| `_log_check_diag` `:8389` | 34 | `…crossover_v2_check_diag` | 18 | `_consume_check` `:4640` |
| `_log_measure_level_solve` `:8424` | 45 | `…crossover_v2_measure_level_solve` | 12 | **`_log_check_diag` `:8422`** |
| `_log_measure_diag` `:8470` | 154 | `…crossover_v2_measure_diag` | 41 | `_consume_measure` `:4692` |
| `_log_verify_diag` `:8625` | 103 | `…crossover_v2_verify_diag` | 32 | `_consume_verify` `:7309` |

The row's *"feed no verdict"* holds and is now pinned to the shape: all four are
`-> None`, contain no valued `return`, are invoked as bare statements, and run
**after** the verdict exists (`:4638-4641` is the pattern — compute, then log,
then return). All four are **unconditional**: no debug flag, no verbosity gate,
in the emitters or in the phase router (`:4207-4227`).

**Delete is refuted by the charter and by an ADR.** AGENTS.md's Guards default
makes observability the sanctioned response to a real incident — *"fix forward
and add observability (`event=` log, `/state`, doctor)"* — and
**ADR-0143** defines plane 1 as *"always-on, cheap, fixed-shape truth"* that
expressly includes *"structured `event=` journal lines."* These four are exactly
that, and ADR-0143 does not require a plane-1 line to also reach `/state` or
doctor; the line itself is the membership. **103 structured fields would be the
largest single observability deletion in either chunk**, and the 96 inline
comment lines are dense per-field provenance — i.e. several of these fields exist
*because* of an incident, which is the thing the default exists to keep.

**They also have twenty live pins.** Zero production consumers — `jasper/cli/doctor/`,
`scripts/`, `jasper/web/` and `/state` were each searched by event name and each
returned nothing — but **19 test functions in `tests/test_crossover_v2_conductor.py`
plus one in `tests/test_crossover_v2_verify_grading.py:544` assert on these
lines**, including negative pins that a retired field must not reappear
(`:8737-8741`) and a count pin that `measure_level_solve` fires exactly twice on
accept and never on reject (`:6390`, `:6413`, `:6424`). *"Zero production
consumers"* is a real finding; *"nothing consumes them"* would be false.

**"Become `analyze`'s journal" is refuted by the type system.**
`crossover_v2_flow.py` contains **zero references to `AnalyzeOutcome` or
`TuningSession`** — the conductor runs its own parallel vocabulary
(`ProgramAnalysis`, `PhaseVerdict`, `V2FlowSeams`). And `AnalyzeOutcome`
(`session.py:195-215`) has exactly two fields, `results` and `disclosures`,
neither a journal. So the option is not a re-home; it is *"invent a third
`AnalyzeOutcome` field **and** wire the conductor to the engine"* — and wiring
the conductor to the engine is **W5-b**, the tier-4 join node. A §6 row may not
quietly acquire the plan's hardest item.

**RULED: the emitters ride their verdicts, unchanged, as `log_event` calls.**
They are not one concern; they are field extraction attached to three phase
verdicts, and §6 already routes those verdicts — CHECK and MEASURE at
`:4627-5279`, VERIFY at `:7305-7924`, both → §2. The emitters are the journal
half of those same rows and move in those same PRs. **No new home, no deletion,
no third `AnalyzeOutcome` field *for the emitters*, and no row of its own in the
deletion order.**

**Narrowed when W2-c landed.** The ruling above refutes turning the emitters into `analyze`'s journal, on
the ground that doing so would mean inventing a third field **and** wiring the
conductor to the engine — a §6 row quietly acquiring W5-b. `AnalyzeOutcome.skipped`
is neither: it is W2-c's own named deliverable, it carries the not-run disclosure
and nothing else, and it wires no conductor. The emitters still get no field.

**One sub-question is deferred, deliberately, to the item that can answer it.**
Once §2's registry exists, the 103 hand-extracted fields and
`AnalyzeOutcome.results` are reading the same objects, and one generic emitter
walking `results` could replace four hand-rolled ones. That is a **W2-b** call
made against a registry that exists, not a §6 homing decision made against one
that does not. §6 should not pre-commit it, and W2-b's verification bar should
mention it.

**Two traps for whoever moves them.**

- **`_log_measure_level_solve` is misnamed.** It fires from `_log_check_diag`
  `:8422`, on the CHECK event — not from `_consume_measure`. Four emitters serve
  **three** phases. An executor splitting the block by phase name will put it in
  the wrong PR.
- **Twenty pins match rendered log text.** The field *names* are a contract, so
  these are defensible under the Tests default; the *mechanism* is substring
  matching over `caplog`, so every emitter move churns them. Price that into the
  §2 rows rather than discovering it mid-PR, and prefer converting a pin to the
  structured record over re-spelling its substring.

**And the 96 comment lines get 6.1's treatment.** The per-field provenance notes
are the same content class as the tuning-constant essays — a field that exists
because a specific run produced a specific wrong number is a ruling, not a
comment. Ruling S7 applies unchanged: the ruling-class ones become ADRs before
the row cuts prose; the unit-and-range ones stay.

### Deletion order

1. **The four barrels + `__all__`** (`crossover_v2_flow` 255–630, 9166–9228) —
   zero dependencies, and every later diff gets smaller once importers point at
   the organs.
2. **The status projection** (`web` 1462–2164) — lifts whole, no seam involved.
3. **The two preparers converge** (W5-a) — the precondition for one construction
   site.
4. **The walk lifts** (W5-b, then W1-c) — `consume_capture` and the four
   retention sites go together.
5. **Volume** (W5-c) — after the walk, because the claim's lifetime is the walk's.
6. **The shell** — the accessors that lost their reader, `__init__`'s residue,
   and the commit pair once 6.2's brief is answered. **The diagnostic emitters
   are not in this step** (6.3): they leave with their verdicts in the §2 rows,
   which land before the shell is cut.

---

## 7. Merge-order DAG + floor accounting

### The DAG

```
tier 0 (independent — start these now, in parallel)
  W5-d  orphans + stale docs        W2-d  settle the 92
  W3-a  prescriber status door      W2-a  analysis table (no caller)
  W5-a  preparers converge

tier 1   W4-a  seam colour flip  ──►  W4-b  twin follows

tier 2   W1-a  RecordStore  ◄── W4-a        W4-c  VolumeClaim  ◄── W4-a

tier 3   W1-b  the 13 fields  ◄── W1-a
         W2-b  the walker     ◄── W2-a, W1-a, W4-a
         W3-b  recommender adapter ◄── W1-a, W3-a, W4-a

tier 4   W5-b  TuningSession in production   ◄── W4-a W4-c W1-a W3-b W2-b W5-a
         ▲ THE JOIN NODE — everything above must land first

tier 5   W1-c  retention lift  ◄── W5-b, W1-b
         W5-c  volume plan sheds its doors ◄── W5-b, W4-c
         W2-c  per-analysis disclosures ◄── W2-b

tier 6   W1-d  the 4j SQLite index ◄── W1-a, W1-c

tier 7   §6's dissolution PRs ◄── W5-b, W1-c
```

**Two scheduling facts.** W5-b is a single-point join: five items must land
before it, and nothing after it can start. And tier 0 is five genuinely
independent items — that is the parallelism budget for the first round, and
`W5-a` is the long pole in it.

### Floor accounting

The chunk-1 floor is **−90,000**, against a deletion subtotal of −93,702 and
~+2,000 of adds, reserving 1,702.

**What is countable on the two god files, at HEAD:**

| File | Row-4/6 baseline | HEAD | Banked | Target | Remaining |
|---|---:|---:|---:|---:|---:|
| `crossover_v2_flow.py` | 13,459 | 9,228 | **−4,231** | ≈1,500 | **≈−7,730** |
| `web/correction_crossover_v2.py` | 9,563 | 8,088 | **−1,475** | *no explicit target* | see below |
| **both** | 23,022 | 17,316 | **−5,706** | | |

Row 6 has no target in the chunk-1 evidence base and this plan does not invent
one. What §6 *can* say is which concerns stay: the apply transaction (1,185
lines, `:6904-7360`), the delta-probe rollback, restore/Undo and the apply-blocked
tail (`:7363-8088`), plus the applied-graph introspection (`:5476-5672`). That is
**≈2,000–2,400 lines kept**, so the web file's remaining contribution is
**≈−5,700 to −6,100** — stated as a derived bracket, not a target.

**So this plan's own two files contribute ≈−13,400 to −13,800 of what remains.**
The balance of the floor is chunk-1's, not this document's: class C+D test
deletion (−29,300, file-grained), census-zone prose (−30,000) and the
test-docstring residual (−15,000), per the net-lines table at `:1356-1368`.

**Do not re-quote those three rows as though this plan verified them.** Two live
caveats travel with them (`:1388-1398`): the 29,300 is **file-grained** while
ruling S7 made deletion **function-grained**, so it moves in both directions; and
the count is to be re-taken per wave against the actual tree via
`scripts/right-size-report.sh`, not hand-counted. **This plan re-derived only the
two god-file rows above** — the ones its own items are responsible for.

---

## 8. Risks and tiers

### Review tier per work item

**Adversarial** (per [AGENTS.md](../AGENTS.md): the clamps, DSP math on the
output path, secrets, or `deploy/install.sh`):

| Item | Why |
|---|---|
| **W4-a** seam colour flip | rewrites the fader **release** path into an awaitable one; the `asyncio.shield` is load-bearing |
| **W4-c** `VolumeClaim` adapter | the first production implementation of a fader claim |
| **W5-b** `TuningSession` in production | the first production path through that claim |
| **W5-c** `SessionVolumePlan` sheds its doors | the walked-away guarantee and the exact→emergency restore ladder |

**Default single review pass:** W1-a, W1-b, W1-c, W1-d, W2-a, W2-b, W2-c, W3-a,
W3-b, W5-a. W1-c is the largest and the one to review hardest —
`provenance.take()`'s single shot (§1, obligation 4) is plumbing, not a field
move.

**Mechanical, author judgment plus a sanity look:** W4-b (the twin follows the
colour), W2-d (docs), and the tier-0 half of W5-d (docs + the widened grep).

**Nothing here touches `deploy/install.sh`, secrets, or the SPL stop.** The
`devices.volume_limit = 0.0` ceiling is untouched by every item: the owner sits
behind that door as its only caller, never as its exception
(`jasper/volume_owner.py:39-45`).

### Traps the executors must carry

- **Squash-ancestry.** `main` squash-merges, so `git merge-base --is-ancestor`
  reports a landed branch as un-landed. Use `git cherry`, and rebase with
  `git rebase --onto` rather than a plain rebase, or you will replay work that
  already merged. Wave 6d hit this twice in one walk (delete-of-modified, then
  add/add).
- **Content-verify a landed check; never trust the notification.** Every wave-6
  disposition says *"content-verified on main"* for a reason. Grep the merged
  tree for the symbol you claimed to add or delete. `#3137`'s disposition is the
  shape: *"held_target_db count on main in `jasper/` is ZERO."*
- **The reap trap.** `_run_async`'s timeout path (`web/correction_setup.py:1311-1327`)
  cancels the loop task, waits `_RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S` for it to
  drain, logs **CRITICAL** if it does not — and then **waits unbounded anyway**,
  because *"a terminal response must never release measurement ownership while
  its graph/volume finalizer can still mutate the speaker"* (`:1323-1326`). W4-a
  makes the four verbs awaitable and therefore cancellable; any new cancel path
  inherits that rule. **The alarm is observability, not permission to abandon
  cleanup.**
- **Wrap-safe sweeps.** This tree wraps source at ~79 columns, so a symbol and
  its qualifier routinely land on two lines. A single-line grep for
  `set_volume_db(` is what let `jasper/cli/seat_level.py:413` escape the wave-5
  ledger (§5). Join line pairs before matching, and when a check counts
  something, paste the count into the PR.
- **Validate a tree-scanning check against the merge result**, not your branch:
  `git merge-tree --write-tree origin/main HEAD` (AGENTS.md:144). Two-dot diffs
  render phantom removals in this shared repo — use the merge base for scope and
  merge-tree for the net.
- **Do not report a property you have not tried to break.** Every guard claim in
  this plan's verification bars is a *mutation* instruction: break the thing,
  watch the named pin fail, restore, re-run green. A pin that stays green under
  its own mutation is not covering what you think — and a mutation harness that
  fails silently in **both** directions reads as covered either way, so check
  that the un-mutated run is green too.
- **Re-derive the line numbers.** Every `file:line` in this document was true at
  `4a9e9f631`. §0 exists because two of the numbers handed *to* this plan had
  already moved by two lines within a single merge. Re-derive before you cut.

---

## Appendix A — the "no-silent-failure" citations

The chunk-1 plan enumerated **10 live sites** attributing a *"no-silent-failure
rule"* to the charter, found that no such rule exists, and
left them alone rather than repoint them — because *"repointing them would widen
a non-negotiable by prose edit, which is not a rider's authority."* That judgment
was right and this appendix does not overturn it. It supplies what the flag was
missing: **the sites are not one class, and the widening was only ever needed for
one of the two.**

### The premise, re-derived at `c253c3cf1`

AGENTS.md's only match for `silent` is non-negotiable 6 (`:42-43`):

> **No silent deafness:** a new code path that prevents wake response must play
> a cue (`jasper/cues/registry.py`).

**But the wording the citations use is not extinct — it moved.**
`docs/extensibility.md:87-89` carries it live: *"**no silent failure → audible
cue** for anything that blocks a response"*, restated at `:153-155` as a Feature
obligation — *"no-silent-failure → audible cue (a proactive Feature that fails
must speak)."* That file is on AGENTS.md's Map. **So the rule exists, under this
wording, in a live doc — for the cue half only.**

**The other half has never existed anywhere.** No charter text, at any revision,
says a user-visible dead end must emit a greppable log line. That is the claim
nine of the thirteen sites make. **Two different rules are wearing one name**,
and that — not the missing heading — is why repointing them all to
non-negotiable 6 would widen a closed list.

### Per-site classification

**(i) response-blocking, cue-shaped — repoint is honest.** These four are about
the speaker going quiet, and non-negotiable 6 plus `extensibility.md:87-89` are
their true owners.

| Site | What it guards | Cue? |
|---|---|---|
| `jasper/wake_fusion.py:72` | `verify()`'s fail-open contract: *"must never make the speaker go deaf because verification was unsure."* Literally the wake path | no — it fails **open**, so the path that would prevent wake response is never taken. The stronger form of the same rule |
| `tests/test_cue_registry_coverage.py:7` | the registry↔play-site correspondence pin — *"a phantom play … the speaker falls silent on exactly the failure the cue existed to announce"* | **yes — this file is non-negotiable 6's mechanism** |
| `tests/test_cue_registry_coverage.py:66-67` (**wraps**) | the orphan-cue assert message, same pin | yes |
| `tests/test_tools_diagnostic.py:153` | a voice-tool telemetry failure must not silently end the turn | response-blocking, not wake-blocking — `extensibility.md:88`'s *"anything that blocks a response"* is the exact fit |

**(ii) observability disclosure — the citation is decorative.** All nine emit a
structured `event=`/`/state` disclosure so an operator can grep why a control is
disabled or a budget expired. **None touches wake response. None calls a cue.**
Each sentence already states its own reason — the parenthetical adds an authority
stamp, and the stamp is false.

| # | Site | The dead end it discloses |
|---|---|---|
| 1 | `jasper/web/correction_crossover_v2.py:4962` ⚑ | Stage-2 preflight refused → a disabled Apply; WARNING line + `STAGE2_PREFLIGHT_KEY` |
| 2 | `jasper/active_speaker/crossover_v2/accountability.py:666` ⚑ | `grade_prediction` → `None`; `EVENT_PREDICTION_UNGRADEABLE` with `why` separating the two causes |
| 3 | `tests/test_correction_crossover_v2_endpoints.py:3457` ⚑ | pins #1 |
| 4 | `tests/test_correction_crossover_v2_endpoints.py:8882` ⚑ | pins D8 — **which** budget expired (issue #1807) |
| 5 | `tests/test_crossover_v2_conductor.py:8541` ⚑ (**wraps**) | pins #2 end-to-end |
| 6 | `jasper/correction/autolevel.py:309` | a failed final `set_main_volume_db` can strand the speaker at measurement level; bare `logger.warning`, not `log_event` |
| 7 | `tests/test_correction_autolevel.py:392` | pins #6 |
| 8 | `docs/historical/two-stage-commission-flow-plan.md:440` | decision **D4** — the design source for #2/#5 |
| 9 | `docs/historical/two-stage-commission-flow-plan.md:696` | decision **D8** — the design source for #4 |

⚑ = in this plan's zone (five, matching the chunk-1 count).

### The count moved, and the mechanism is the usual one

**13 live attributions across 9 files, not 10.** The chunk-1 sweep searched the
hyphenated `no-silent-failure`; three more sites spell it *"no silent failure
paths"* and one of those **wraps across a newline**
(`test_cue_registry_coverage.py:66-67`). That is the fifth consecutive
wrap-and-variant miss this campaign has recorded, and it is why §8's wrap-safe
sweep trap is stated as a standing rule rather than an anecdote. **Treat 13 as a
floor, not a total** — it is what one phrasing-variant sweep found, and a looser
shape would only raise it.

### RULED (owner, 2026-08-26) — drop the attribution

The question was: nine sites claim the charter requires a greppable disclosure
line for a user-visible dead end, and it never has, in any revision — write that
into the charter as a named default, or drop the attribution and keep the
sentence, which already says why?

**RULED: drop the attribution. The charter does not grow, and the closed list is
not touched.** Executed as a comments-and-prose-only sweep, separately from the
wave riders this appendix originally proposed — the ruling arrived before the
waves opened those files, so a single sweep is cheaper than nine riders.

**The reasoning, as recommended and adopted:** AGENTS.md is
176 lines against a ~220 cap, and its own Docs default (`:74-76`) says *"do not
restate here … what another file owns."* Every one of the nine sentences is
self-sufficient without the parenthetical — *"a disclosure nobody can grep for is
not a disclosure"* needs no external authority — and where a durable why-pointer
is wanted, the **issue number is the one that cannot rot**: D8 has #1807, D4 has
its own decision id. The Comments default already sanctions exactly that shape
(*"why-pointers (`See ADR-NNN`, an issue, a doc)"*), and a charter line restating
what thirteen sites each already state is thirteen fresh drift sites.

**What "drop" means per class:** the four class-(i) sites repoint to
`extensibility.md:87-89`, which carries the wording live and is closer than
non-negotiable 6 for the two non-wake ones; the nine class-(ii) sites lose the
parenthetical and keep the sentence, which already says why; class (iii) — the
frozen review doc — is untouched. **The closed list is not touched by any of it.**

**Executed as one standalone comments-only sweep**, which departs from this
appendix's original *"riders in the wave that opens each file — never a
standalone PR."* That instruction assumed the sites would be reached
incidentally; the ruling landed first, the
waves have not opened these files, and the sites span four programs' zones (only
five of the thirteen are in this plan's). A single zero-behaviour comment sweep
is cheaper to review and to verify than nine riders spread across four programs.
