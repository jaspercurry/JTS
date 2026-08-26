# W1 cutover briefs: the RecordStore and the retention lift

> **Scope.** The implementation-grade briefs for the cutover DAG's entry
> items — `W1-a` (the production `RecordStore`), `W1-b` (the thirteen fields),
> `W1-c` (the four-site retention lift) and `W1-d` (the 4j index) — as scheduled
> by [`REFACTOR-CUTOVER-2026-08.md`](REFACTOR-CUTOVER-2026-08.md) §1 and §7.
>
> **This document does not re-plan.** §1 owns *what* and *why*; this owns
> *exactly where and in what order*, at a grain a builder executes without
> re-deriving. Where a derivation here disagrees with §1, the disagreement is
> recorded in the ledger below rather than papered over.

Every `file:line` here was re-derived at **`c253c3cf1`** (`main` at
`docs/REFACTOR-CUTOVER-2026-08.md`'s own merge, #3143). §1's numbers were taken
one merge earlier at `4a9e9f631`; where they still hold this document says so,
because "unchanged" is a finding too.

---

## 0. Where this brief disagrees with §1

Ten. Group A (D1–D5) are shape questions W1-a has to answer and §1 leaves
partly open. Group B (D6–D10) are **counts that are wrong at HEAD** — and D6 is
the one that changes a work item's title.

Every one was verified by reading the file, not by taking a prior document's
word. Where §1 is right, this brief says so instead of re-asserting it as new.

### Group A — the shapes

| # | §1 says | At HEAD | Disposition |
|---|---|---|---|
| D1 | W1-a implements `bank` / `read` / `persist` / `read_state` (`:254-264`), stated in the sync colour the protocol carries today | §4's ruling — *"`RecordStore` is cut async before it is built"* (`:707-713`) — makes all four `async def` | **read §1 and §4 together.** The DAG (`:1044`) already gates `W1-a ◄── W4-a`, so this is a scheduling fact §1's item text simply does not repeat. **Build the four methods async.** §2 below writes the signatures out. |
| D2 | `persist`/`read_state` land on `persist_conductor_state`'s shape (`web/…:2809`), *"wave 3's schema writer with no schema"* | The schema now exists. `build_conductor_state` (`durable_state.py:1025`) owns the whole document and *"touches no file"* (`:1038`); `ConductorState` (`:97`) carries `state` + `durable`. `persist_conductor_state` is `web/…:2809-2867` — **58 lines**, a write wrapper | **premise moved, in the executor's favour.** The pure builder W1-a needed is already extracted. `session_seams.py:215-216` still calls it *"an 854-line function"* — that docstring is stale; fix it in W1-a's PR (§2.6). |
| D3 | *"One new module … over the `evidence/v1` layout `commissioning_evidence_store.py` already owns"* (`:255-257`) | The two pairs cannot share one backend. `bank` is **write-once** — `_write_once` raises `PATH_CONFLICT` when a path takes different bytes (`commissioning_evidence_store.py:666-675`). `persist` runs **once per consumed capture** (`save_v2_state`'s own docstring, `web/…:507-510`) and overwrites one file | **one module, two backends.** `bank`/`read` → the evidence store; `persist`/`read_state` → `save_v2_state`/`load_v2_state`. This is not a departure from §1's intent — `session_seams.py:255-260` already describes the persist half as *"the store this replaces overwrites its one file every persist"* — but §1's item text reads as one layout, and a builder who took it literally would hit `PATH_CONFLICT` on the second capture. |
| D4 | W1-a's verification bar names bank/read/round-trip and the discriminator constant (`:261-263`) — no error contract | `SeamFailure` has **no production definition**: `tests/engine_twin.py:81` is the only one in the tree (`jasper/` has zero). The engine does not catch it — `session.py:598` calls `bank` bare | **gap, closed in §2.5.** The store's raise behaviour is unspecified by the protocol, and it is the one thing the twin *cannot* pin because its exception type is test-only. |
| D5 | *"`read` and `read_state` have no engine caller"* (`:76-77`) | Holds, and is sharper: `PriorBank.read` (`prior_bank.py:115`) calls `store.read_state` at `:127` and `_baselines_by_pose` (`:151`) calls `store.read` at `:157` | **holds.** Named here because `PriorBank` is the *only* consumer, so it is the acceptance test for the read halves. |

### Group B — the counts

| # | §1 says | At HEAD | Evidence |
|---|---|---|---|
| **D6** | **The retention lift is FOUR sites**: the three `_hand_to_retention` call sites plus an *"inlined copy"* of the same try/except/WARN/return-`False` shape at `crossover_v2_flow.py:6879` inside `_run_cloud_pipeline`, *"with a comment saying it mirrors `_retain_cloud_position`'s fail-soft boundary"*. §0 `:55-62` calls this *"one structural finding the handed brief did not carry"*; §1 `:216-217` repeats it; W1-c's title is *"four sites in"* | **THREE sites. There is no fourth, and no second writer.** `crossover_v2_flow.py:6879` is a **comment**, not code — it sits inside the `except` arm of a `publish_cloud` call (`:6873-6888`) and reads *"Mirrors `_retain_cloud_position`'s fail-soft boundary"*. Different seam, different arity (`(phase, group_result)` vs `(take_id, result, metadata)`), different event (`…_cloud_publish_failed` vs `…_position_retain_failed`), and it has an `else:` arm mutating `_group_cloud_published` (`:6887-6888`) instead of returning `bool` | **`self._seams.retain_position` appears exactly twice in the entire 9,228-line file** — `:5266` (the `None` check) and `:5269` (the call), both inside `_hand_to_retention`. `_hand_to_retention` has exactly three call sites: `:5056`, `:5254`, `:7020`. The file says so itself at `:6980`: *"`retain_position` is reused rather than duplicated"* |
| **D7** | *"four via `evidence_packet.RING_SIDECAR_GLOB` (`evidence_packet`, `harmonic_evidence`, `feature_classifier`, `round_views`)"* (`:231-233`) | **THREE.** `round_views` contains **no `glob` call at all**. It reaches the ring *indirectly*, by passing `dump_ring_dir=` (`round_views.py:243`) into `build_crossover_evidence_packet` — i.e. through reader #1. Its `:55` is a docstring line | the three real readers are `evidence_packet.py:1246` (`_capture_snr_block` `:1191`), `harmonic_evidence.py:603` (`_bind_measure_captures` `:582`), `feature_classifier.py:456` (`load_round_captures` `:414`) |
| **D8** | *"three that glob flat `*.json`"* (`:233-236`) | **FOUR.** The three named are right; a fourth is missed: `scripts/derive-crossover-incident-fixture.py:138` (`_measure_sidecar` `:121`), globbing `*_measure_*.json` over a **pinned frozen bank** of the flat ring | it depends on the `{stamp}_{phase}_{device}` filename scheme, so it breaks on the naming change as well as on the deletion |
| — | *"**seven** readers"* (`:230`) | **seven** — 3 + 4 | the total survives; **both halves of it are wrong.** Do not let the matching total read as confirmation |
| **D9** | *"`level_db` and `stimulus_dbfs` live only in the debug ring today, through `CaptureProvenance.to_dict` (`capture_provenance.py:117-134`)"* (`:162-164`) | **Neither name exists in that file** — zero occurrences. `to_dict` (`:117`, body `:119-134`) emits `main_volume_db`, `session_volume_db`, `graph{kind,config_path,fingerprint}`, `stimulus{program_id,phase,wav_sha256,peak_dbfs}` | §1's *conclusion* holds and is if anything understated (§4.3); its *field names* are the engine's, not the ring's, and a builder grepping for them finds nothing |
| **D10** | *"three readers re-pair the WAV by `parent.parent / "wav" / stem` written out verbatim in three places"* (`:248-250`) | **TWO:** `harmonic_evidence.py:613`, `feature_classifier.py:482`. The other two pairings are **different rules** — `severed-twin-replay.py:249` uses `sidecar_path.with_suffix(".wav")` (flat sibling, the un-split ring), and `harmonic-distortion-replay.py:266-271` binds by sha256 content across a separate `--captures` directory with no path derivation at all | reader #1 (`_capture_snr_block`) never touches a WAV |

**Two §1 numbers re-checked and unchanged at HEAD**, so the builder does not
re-derive them: `MEASURE_KINDS` is `contracts.py:1433-1437`; `_record()`'s
`def` is `session.py:655` and its dict is `:692-708`. **`_take_identity`'s
"exactly three call sites" also holds** — `spatial.py:914`, `:1044`, `:1107`,
zero in tests (§3.1).

### Why D6 happened, and the rule it argues for

The inlined copies were **real**, and were removed. At `27f13a4e4~1` there were
two — inside `_retain_cloud_position` and inside `_retain_entry_baseline` —
and commit `27f13a4e4` (*"a staged angle walk is taken by the next session…"*,
#2753, 2026-08-20) introduced `_hand_to_retention` and collapsed both into it.

So §1's structural finding was true of a tree, just not of this one; what
survived the collapse is a **comment** whose wording matches. This is §8's
*"re-derive the line numbers"* trap (`:1162-1164`) in its most expensive form:
the citation was to a line that still existed and still said the quoted words,
so a spot-check that read the line without reading its enclosing block would
confirm it. **Confirm a call site by grepping the symbol, never by reading the
cited line.** Here the symbol grep is decisive in one command and the line read
is decisive in neither direction.

**W1-c is therefore three sites in, not four**, and the *"a lift that migrates
only the three named call sites leaves a second writer behind"* hazard (§1
`:60-62`) **does not exist**. The lift is correspondingly smaller and safer than
§1 scheduled it.

---

## 1. What the four methods are called by, and what that fixes

`RecordStore` — `jasper/active_speaker/crossover_v2/session_seams.py:208`.
Method declarations: `bank` `:236`, `read` `:240`, `persist` `:248`,
`read_state` `:252`.

**Engine call sites — exactly two, both in `session.py`:**

| Call | Line | Enclosing | What it hands over |
|---|---|---|---|
| `self.seams.records.bank(self._record(...))` | `:598` | `_one_stimulus` `:576` | the thirteen-field mapping, only when `outcome.played` **and** `proven_level_db is not None` (`:594-597`) |
| `self.seams.records.persist({...})` | `:501` | `save` (state written `:501-510`) | `session_id`, `graph_fingerprint`, `measurement_level_db`, `record_ids`, `disclosures` — **five keys, not thirteen** |

The `bank` return is assigned to `record_id` (`:598`) and accumulated into
`self._banked` (`:423`), which `save` reads as `ids` (`:500`). So **a `bank`
that returns a useless id silently empties the persisted state's
`record_ids`** — which is `PriorBank`'s entire input. That is the failure mode
the id scheme has to not have.

**Consumer call sites — `PriorBank` only:**

- `PriorBank.read(store, state_id)` — `prior_bank.py:115`; `store.read_state(state_id)` at `:127`.
- `_baselines_by_pose` — `:151`; `store.read(record_id)` at `:157`, once per id in `record_ids`.

### What `tests/engine_twin.py` pins about semantics

`FakeRecords` — `tests/engine_twin.py:199`. Four things it fixes, and one it
explicitly does not:

1. **Ids are opaque strings the store mints, not paths.** `_mint` (`:181-186`)
   returns `f"{prefix}-{len(into)}"` — `rec-1`, `state-1`; `_find` (`:189-196`)
   resolves by regenerating the same string. **Nothing in the engine or in
   `PriorBank` parses an id.** A production store may therefore make the id a
   relative path, a fingerprint, or a take id — but it must not *require* a
   caller to construct one.
2. **`read` and `read_state` are implemented, not stubbed** (`:205-211`). The
   twin's own docstring (`:200-208`) says why: they are what make `analyze` an
   offline verb (ruling S3).
3. **`bank` is the only method with a failure switch** — `bank_raises: bool`
   (`:213`), raising `SeamFailure` at `:216`. `persist` has none. Read that as
   the protocol's shape, not an oversight: see §2.5.
4. **`read` of an unknown id returns `None`**, via `_find` falling off its loop
   (`:196`). Not a raise.
5. **`by_position` `:228` and `kinds` `:232` are twin-only conveniences**, and
   their docstrings say so. A production store owes neither — and W1-d's index
   is what replaces them, not a store method.

---

## 2. W1-a — the production `RecordStore`

### 2.1 Where it lives

**`jasper/active_speaker/crossover_v2/record_store.py`** — new module, inside the
organ package beside the protocol it implements.

Two things the package layout requires, both easy to miss:

- **`crossover_v2/__init__.py` is the package's own index** and every sibling has
  an entry there (`:57` `position_cycle`, `:68` `evidence_packet`, `:87`
  `session_seams`, `:96` `prior_bank`). `crossover-v2-engine-design.md:510-512`
  makes that index the file map's delegate. **Add the entry in the same PR** —
  a module absent from the index is invisible to the next reader.
- The module imports `CommissioningEvidenceStore` from
  `jasper/active_speaker/commissioning_evidence_store.py`, which is **outside**
  the organ package. `bind_position_retention` handles the same edge with a
  function-local import (`web/…:3519-3523`); follow whichever the module's own
  import block already does rather than inventing a third convention.

**Not** in `web/correction_crossover_v2.py`. That file is the god file §6
dissolves; a new 300-line concern landing there is the exact defect this refactor
exists to undo.

### 2.2 The four signatures, async (D1)

W4-a flips the seam colour first, so the protocol these implement is already
async when W1-a starts. §4 `:711-713` states the payoff in as many words: the
store can `await asyncio.to_thread(...)` around its file I/O *"rather than
blocking the event loop on every `bank`"*.

```python
async def bank(self, record: Mapping[str, Any]) -> str: ...
async def read(self, record_id: str) -> Mapping[str, Any] | None: ...
async def persist(self, state: Mapping[str, Any]) -> str: ...
async def read_state(self, state_id: str) -> Mapping[str, Any] | None: ...
```

Every filesystem touch inside them goes through `asyncio.to_thread`. Both
backends are blocking-by-construction: `publish_json_artifact` fsyncs
(`commissioning_evidence_store.py:677`, `:690-701` measure free space), and
`save_v2_state` optionally fsyncs (`web/…:488`).

**If W4-a has not landed when this is picked up, stop and say so** rather than
building sync and converting. §4 `:707-710` is explicit that cutting the colour
before the implementation is the whole point.

### 2.3 `bank` / `read` — the evidence-store half

**Follow the writer that already exists.** `retain_position`
(`web/correction_crossover_v2.py:3529`, inside `bind_position_retention` `:3490`,
bound at `:5747`) is the shipped answer to *"where does a crossover-v2 capture
record land"*, and W1-a's job is to be that writer at the seam, not a second one.

Its write is `web/…:3567-3574`:

```python
artifact = store.publish_json_artifact(
    f"crossover_v2/{relay_session_id}/positions/{take_id}.json",
    {
        "schema_version": 1,
        "kind": "jts_crossover_v2_position_evidence",
        "relay_session_id": relay_session_id,
        **record,
    },
)
```

Four facts that follow, each load-bearing:

- **The path is relative and the root is never spelled by the caller.**
  `_artifact_path` (`commissioning_evidence_store.py:237-238`) prefixes
  `{EVIDENCE_ROOT}/artifacts/`, and `EVIDENCE_ROOT = "evidence/v1"` is
  `:71` — the tree's only definition. The store passes
  `crossover_v2/{cap-id}/positions/{take_id}.json` and gets
  `evidence/v1/artifacts/crossover_v2/{cap-id}/positions/{take_id}.json`.
- **`{cap-id}` is the RELAY session id, not the bundle session id.**
  `evidence_packet.py:220-223` says so; `v2_session_identity`
  (`web/…:3597`) records the bundle id as canonical and the relay id as an
  alias. **A store that mints its directory from `record["session_id"]` files
  the record where `round_artifact_dir` does not look.**
- **The write is idempotent for identical bytes and a refusal for different
  ones.** `_write_once` `:666-683`: an existing path with equal payload
  re-fsyncs the directory and returns the same identity; unequal payload raises
  `PATH_CONFLICT` (`:671-675`). `retain_position`'s comment `:3532-3541` records
  that this is why `take_id` carries `attempt`.
- **`round_artifact_dir` fails closed on two round directories**
  (`evidence_packet.py:630-635`), globbing `_EVIDENCE_GLOB =
  "evidence/v1/artifacts/crossover_v2/*"` (`:224`). One bundle, one round dir.

**The id scheme.** Mint `take_id_for(position_id, attempt)`
(`spatial.py`, §3.3) as the take component and return the **store-relative
record path** as the opaque id:

```
crossover_v2/{relay_session_id}/positions/{take_id}.json
```

Why the path and not a counter: `read` has to resolve an id handed back from a
*previous session's* persisted `record_ids` (`prior_bank.py:157`), and a
1-based counter is only resolvable by a store that still holds the list that
produced it. The twin can do that because it holds `banked` in memory; a
production store cannot, and reconstructing the ordering from the directory to
re-derive `rec-4` would be inventing a second index — exactly what #3064
forbids. A path is self-resolving, and the engine's own contract
(`session_seams.py:237`, *"return the id that finds it again"*) is satisfied
exactly.

**This does not violate the twin's "ids are opaque".** Opaque means the *engine*
never parses one, and it does not — the id travels from `bank`'s return
(`session.py:598`) to `_banked` (`:423`) to `record_ids` (`:500`) to
`store.read` (`prior_bank.py:157`) untouched. Only the store reads it.

**`read`** resolves the id against the bundle and returns `None` — never raises —
when the path is absent or unparseable. `session_seams.py:241-245` is explicit:
*"a missing record is a fact an offline `analyze` discloses, not an exception
that strands the run."* This means catching `CommissioningEvidenceStoreError`
with code `MISSING` and returning `None`;
`commissioning_isolated_producer.py:90-91` already has the `_missing(error)`
helper for exactly this test — **reuse it rather than re-spelling the code
comparison.**

**Close the drift site while here (§1 `:115-119`).** The writer spells
`"schema_version": 1` and `"kind": "jts_crossover_v2_position_evidence"` as bare
literals at `web/…:3570-3571`, while the reader imports the same string as
`POSITION_EVIDENCE_KIND` (`position_cycle.py:111`) and checks it at `:246`. The
store imports the constant. §1's verification bar names this pin.

### 2.4 `persist` / `read_state` — the durable-state half (D2, D3)

Not the evidence store. The backend is the pair in `web/correction_crossover_v2.py`:

| | Symbol | Line | Behaviour that matters |
|---|---|---|---|
| write | `save_v2_state(state, *, durable=False)` | `:488` | atomic rename always; fsync only when `durable=True`. The rule (#2291) is at `:494-517`: durable where power loss would lose the rollback anchor or falsify a receipt, cheap everywhere else |
| read | `load_v2_state()` | `:465` | returns `None` for missing, malformed, wrong `kind`, or wrong `schema_version` (`:479-484`) — **never raises** |

Path: `DEFAULT_V2_STATE_PATH = /var/lib/jasper/active_speaker_crossover_v2_state.json`
(`:169-171`). Vocabulary: `STATE_SCHEMA_VERSION = 1` (`:167`),
`STATE_KIND = "jts_crossover_v2_flow_state"` (`:168`).

**The document builder is already pure (D2).** `build_conductor_state`
(`durable_state.py:1025`) takes the conductor plus the `prior` state and returns
`ConductorState(state, durable)` (`:97-113`), touching no file (`:1038`).
`persist_conductor_state` (`web/…:2809-2867`) is now only: load prior `:2832`,
build `:2833`, read the pre-write grade `:2842`, `save_v2_state` `:2846`,
journal one transition `:2851-2866`.

**The id scheme for state.** `read_state` must answer *"is the file still the
one you named?"* — because the file is overwritten every persist, and
`session_seams.py:255-260` makes *"the prior round's state is gone"* an ordinary
outcome that returns `None` rather than refusing the round (ruling S10).

Mint the id from the identity the state already carries, so no new field is
needed:

```
state-{session_id}-{n}
```

where `n` is the persist counter within this session. `read_state` loads the
file, and returns the document only when the id it was handed matches the id the
document on disk would mint now; otherwise `None`. `session_id` is already in
the engine's persisted mapping (`session.py:502`) and is
`built.state["session_id"]` on the host side (`web/…:2839`).

**Do not add durability.** `persist` runs once per consumed capture
(`web/…:512-514`), and the same passage says an fsync per capture *"buys nothing
that the next capture's write does not already redo"*. `durable=True` stays with
the three writes `:496-517` enumerates. A store that fsynced every `persist`
would regress a documented #2291 decision.

### 2.5 The error contract — the gap §1's bar does not cover (D4)

`SeamFailure` is `tests/engine_twin.py:81` and **nowhere in `jasper/`**. So there
is no production type for *"the world failed"* versus *"the engine refused"*, and
the engine catches nothing around `bank` (`session.py:598` is a bare call).

What each backend raises today:

- `publish_json_artifact` → `CommissioningEvidenceStoreError`, a `RuntimeError`
  subclass with a `.code` (`PATH_CONFLICT`, `TOO_LARGE`, `TOTAL_TOO_LARGE`,
  `INSUFFICIENT_SPACE`, `PERSIST_FAILED`, `PERSIST_OUTCOME_UNKNOWN`, `MISSING`).
  `bind_position_retention`'s docstring `:3511-3519` states the design: the store
  is *deliberately strict* and **the fail-soft boundary lives one level up**.
- `save_v2_state` → `OSError` from the atomic write.

**The ruling for W1-a: let both propagate, unwrapped.** Three reasons, all
already in the tree:

1. It is the shipped decision. `bind_position_retention` *"does NOT swallow
   failures"* (`:3511`) precisely so *"the strictness the store was built for is
   preserved for every OTHER caller"* (`:3518-3519`).
2. The fail-soft boundary the conductor owns is `_hand_to_retention` (§3), and
   W1-c is where it moves — not W1-a.
3. Inventing a production `SeamFailure` in W1-a would put a new exception
   vocabulary in front of five seams on the strength of one, and W4-a is the PR
   that owns seam-wide shape.

**Record it as an open question for the owner rather than deciding silently:**
whether the engine grows a fail-soft `bank` (an incident on the record, the walk
continues) or keeps the raise. Today `session.py:594-597` already has the
vocabulary for the soft answer — `UNPROVEN_LEVEL` becomes `incident` and
`record_id` stays `""` — so the soft path is one `except` away. It is a
behaviour change, so it is not W1-a's to make.

### 2.6 Two stale claims to true up in this PR

Both are in the protocol docstring the store implements, and a wrong comment
misleads the next agent more than a missing one:

- `session_seams.py:215-216` — *"wave 3's `persist_conductor_state`, today an
  854-line function that is a schema writer with no schema"*. At HEAD it is 58
  lines and its schema is `durable_state.build_conductor_state` (D2).
- `session_seams.py:212-213` — the bank half is described as *"wave 4's five
  blocks — identity · place · stimulus-and-path · honesty · **the curve**"*.
  `_record()` returns thirteen flat fields, not five blocks, and the curve is not
  among them (§4). Either the blocks are the *destination* shape and the
  docstring should say so, or it is describing a shape that does not exist.
  **Flag rather than rewrite** — the blocks are W1-b's subject and the wording
  may be deliberate.

### 2.7 Test plan

New file: `tests/test_crossover_v2_record_store.py`.

**Behaviour pins** (one altitude per behaviour, per AGENTS.md):

| # | Pin | Asserts |
|---|---|---|
| P1 | bank → read round-trips | `await read(await bank(rec))` equals `rec` field-for-field |
| P2 | `read` of an unknown id returns `None` | not a raise — `session_seams.py:241-245` |
| P3 | `read_state` of an outlived id returns `None` | persist twice, read the first id back |
| P4 | persist → read_state round-trips | the five keys `session.py:502-509` writes |
| P5 | the discriminator is imported, not spelled | the banked JSON's `kind` `is` `position_cycle.POSITION_EVIDENCE_KIND` |
| P6 | a re-bank of identical bytes is idempotent | same id, no raise — `_write_once:666-683` |
| P7 | the record lands under the RELAY id | the path contains the relay session id, not `record["session_id"]` |
| P8 | `PriorBank.read` rebuilds over a real store | the acceptance test — bank a walk, `persist`, drop the session, `PriorBank.read(store, state_id)` returns the baselines (D5) |

**Watched-fail list** — break it, watch *only* the named pin fail, restore,
re-run green. AGENTS.md's mutation rule, and §8 `:1156-1161`'s warning that a
harness failing silently in both directions reads as covered either way, so
**assert the un-mutated run is green too**:

| Mutation | Must fail | Must stay green |
|---|---|---|
| `read` returns `{}` instead of `None` for a missing id | P2 | P1, P4 |
| `read_state` ignores the id and returns the current file | P3 | P4 |
| the `kind` literal is re-spelled instead of imported | P5 | P1 |
| the path is minted from `record["session_id"]` | P7 | P1 |
| `bank` returns `""` | P1, P8 | P2 |

The last one is the important one: it is the *"a useless id empties
`record_ids`"* failure from §1, and P8 is the only pin that catches it at the
altitude where it does damage.

**Reuse `FakeRecords`' own contract rather than re-writing it.** §1's bar
(`:261-263`) says *"the twin's own contract run against the real store"*.
Parametrize the round-trip pins over both implementations so the two cannot
drift; where the real store needs a bundle, the twin needs nothing, so the
fixture supplies one and the twin ignores it.

**Suites to run:** `tests/test_crossover_v2_record_store.py`,
`tests/test_engine_twin.py`, `tests/test_crossover_v2_engine_skeleton.py`,
`tests/test_crossover_v2_prior_bank.py` (if present — else every module
importing `prior_bank`), `tests/test_active_speaker_commissioning_evidence_store.py`,
`tests/test_crossover_v2_position_cycle.py`. Plus `mypy` on the new module and
`ruff` on every touched file.

**Content-grep for the conductor's post-merge check** (§8 `:1135-1138` — grep the
merged tree, never trust the notification):

```
git grep -n "class .*RecordStore" -- jasper/          # expect the new impl
git grep -c "POSITION_EVIDENCE_KIND" -- jasper/       # expect the count to RISE by 1
git grep -n "854-line" -- jasper/                     # expect ZERO (2.6)
```

*(sections W1-b … W1-d, PR slicing and traps land in the following commits)*
