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

### Why D6 happened

The inlined copies were **real, and were removed.** At `27f13a4e4~1` there were
two — in `_retain_cloud_position` and in `_retain_entry_baseline` — and commit
`27f13a4e4` (#2753, 2026-08-20) introduced `_hand_to_retention` and collapsed
both into it. What survived is a **comment** whose wording still matches.

That is §8's *"re-derive the line numbers"* trap (`:1162-1164`) in its most
expensive form: the cited line still exists and still says the quoted words, so a
spot-check that reads the line without its enclosing block **confirms a false
premise**. The rule it argues for is §7.1.

**W1-c is three sites in, not four**, and §1 `:60-62`'s *"leaves a second writer
behind"* hazard **does not exist** — the lift is smaller and safer than scheduled.

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

### What `tests/engine_twin.py:199` (`FakeRecords`) pins

1. **Ids are opaque strings the store mints.** `_mint` `:181-186` returns
   `f"{prefix}-{len(into)}"`; `_find` `:189-196` resolves by regenerating it.
   **Nothing in the engine or `PriorBank` parses an id** — so a path, a
   fingerprint or a take id all qualify, provided no caller has to construct one.
2. **`read`/`read_state` are implemented, not stubbed** (`:205-211`) — `:200-208`
   says why: they make `analyze` an offline verb (ruling S3).
3. **`bank` is the only method with a failure switch** — `bank_raises` `:213`,
   raising `SeamFailure` `:216`. `persist` has none (see §2.5).
4. **`read` of an unknown id returns `None`** (`_find` falls off its loop
   `:196`), not a raise.
5. **`by_position` `:228` and `kinds` `:232` are twin-only** and say so. A
   production store owes neither — W1-d's index replaces them, not a method.

---

## 2. W1-a — the production `RecordStore`

### 2.1 Where it lives

**`jasper/active_speaker/crossover_v2/record_store.py`** — new module, beside
the protocol it implements. **Not** `web/correction_crossover_v2.py`: that is the
god file §6 dissolves, and a new 300-line concern landing there is the defect
this refactor exists to undo.

**Add the `crossover_v2/__init__.py` entry in the same PR.** That file is the
package's own index — every sibling has one (`:57` `position_cycle`, `:68`
`evidence_packet`, `:87` `session_seams`, `:96` `prior_bank`) and
`crossover-v2-engine-design.md:510-512` makes it the file map's delegate. A
module absent from it is invisible to the next reader.

`CommissioningEvidenceStore` lives **outside** the organ package.
`bind_position_retention` handles that edge with a function-local import
(`web/…:3519-3527`); follow the new module's own import block rather than
inventing a third convention.

### 2.2 The four signatures, async (D1)

```python
async def bank(self, record: Mapping[str, Any]) -> str: ...
async def read(self, record_id: str) -> Mapping[str, Any] | None: ...
async def persist(self, state: Mapping[str, Any]) -> str: ...
async def read_state(self, state_id: str) -> Mapping[str, Any] | None: ...
```

W4-a flips the colour first, so the protocol is already async when W1-a starts.
§4 `:711-713`: the store can `await asyncio.to_thread(...)` around its file I/O
*"rather than blocking the event loop on every `bank`"*. Every filesystem touch
goes through `to_thread` — both backends block by construction
(`publish_json_artifact` fsyncs, `commissioning_evidence_store.py:677`,
`:690-701`; `save_v2_state` optionally fsyncs, `web/…:488`).

**If W4-a has not landed, stop and say so** rather than building sync and
converting — §4 `:707-710` is explicit that cutting the colour before the
implementation is the point.

### 2.3 `bank` / `read` — the evidence-store half

**Be the writer that already exists, not a second one.** `retain_position`
(`web/correction_crossover_v2.py:3529`, in `bind_position_retention` `:3490`,
bound `:5747`) writes at `:3567-3574`:

```python
artifact = store.publish_json_artifact(
    f"crossover_v2/{relay_session_id}/positions/{take_id}.json",
    {"schema_version": 1, "kind": "jts_crossover_v2_position_evidence",
     "relay_session_id": relay_session_id, **record},
)
```

Four load-bearing facts:

- **The caller never spells the root.** `_artifact_path`
  (`commissioning_evidence_store.py:237-238`) prefixes
  `{EVIDENCE_ROOT}/artifacts/`; `EVIDENCE_ROOT = "evidence/v1"` `:71`, the
  tree's only definition.
- **`{cap-id}` is the RELAY session id, not the bundle id**
  (`evidence_packet.py:220-223`; `v2_session_identity` `web/…:3597` makes the
  bundle id canonical and the relay id an alias). **A store minting its
  directory from `record["session_id"]` files the record where
  `round_artifact_dir` does not look.**
- **Idempotent for identical bytes, `PATH_CONFLICT` for different ones** —
  `_write_once:666-683`, raise at `:671-675`. `retain_position:3532-3541`
  records that this is why `take_id` carries `attempt`.
- **`round_artifact_dir` fails closed on two round directories**
  (`evidence_packet.py:630-635`), globbing `_EVIDENCE_GLOB =
  "evidence/v1/artifacts/crossover_v2/*"` (`:224`). One bundle, one round dir.

**The id scheme: the store-relative record path**, take component minted by
`take_id_for` (§3.1) — `crossover_v2/{relay_session_id}/positions/{take_id}.json`.

**Path, not a counter,** because `read` resolves ids handed back from a
*previous session's* `record_ids` (`prior_bank.py:157`). The twin's `rec-{n}`
works only because it still holds `banked` in memory; a production store would
have to rebuild the ordering by rescanning — a second index, which #3064
forbids. A path is self-resolving and satisfies `session_seams.py:237`
(*"return the id that finds it again"*) exactly.

**Still opaque in the sense the twin means it:** the *engine* never parses an
id, and does not here — it travels `bank`'s return (`session.py:598`) →
`_banked` (`:423`) → `record_ids` (`:500`) → `store.read`
(`prior_bank.py:157`) untouched. Only the store reads it.

**`read` returns `None`, never raises**, for an absent or unparseable path —
`session_seams.py:241-245`: *"a missing record is a fact an offline `analyze`
discloses, not an exception that strands the run."* Catch
`CommissioningEvidenceStoreError` with code `MISSING`; the `_missing(error)`
helper already exists at `commissioning_isolated_producer.py:90-91` —
**reuse it, do not re-spell the comparison.**

**Close the drift site (§1 `:115-119`):** the writer spells `schema_version` and
the kind as bare literals (`web/…:3570-3571`) while the reader imports
`POSITION_EVIDENCE_KIND` (`position_cycle.py:111`, checked `:246`). The store
imports the constant.

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

**The id scheme for state: `state-{session_id}-{n}`**, `n` the persist counter
within the session. `read_state` must answer *"is the file still the one you
named?"* — the file is overwritten every persist, and `session_seams.py:255-260`
makes *"the prior round's state is gone"* an ordinary `None`, never a refusal
(ruling S10). So: load the file, return the document only when the handed id
matches the id the document on disk would mint now. `session_id` is already
present — `session.py:502` engine-side, `built.state["session_id"]`
(`web/…:2839`) host-side. No new field.

**Do not add durability.** `persist` runs once per consumed capture, and
`web/…:512-514` says an fsync per capture *"buys nothing that the next capture's
write does not already redo"*. `durable=True` stays with the three writes
`:496-517` enumerates; fsyncing every `persist` regresses a documented #2291
decision.

### 2.5 The error contract — the gap §1's bar does not cover (D4)

`SeamFailure` is `tests/engine_twin.py:81` and **nowhere in `jasper/`** — there
is no production type separating *"the world failed"* from *"the engine
refused"*, and the engine catches nothing around `bank` (`session.py:598` is a
bare call).

Backends raise: `publish_json_artifact` →
`CommissioningEvidenceStoreError` (a `RuntimeError` with a `.code`:
`PATH_CONFLICT`, `TOO_LARGE`, `TOTAL_TOO_LARGE`, `INSUFFICIENT_SPACE`,
`PERSIST_FAILED`, `PERSIST_OUTCOME_UNKNOWN`, `MISSING`); `save_v2_state` →
`OSError`.

**Ruling for W1-a: let both propagate, unwrapped.** (1) It is the shipped
decision — `bind_position_retention` *"does NOT swallow failures"* (`:3511`) so
that *"the strictness the store was built for is preserved for every OTHER
caller"* (`:3518-3519`). (2) The conductor's fail-soft is `_hand_to_retention`,
and **W1-c** moves it, not W1-a. (3) Inventing a production `SeamFailure` here
would put a new exception vocabulary in front of five seams on the strength of
one; W4-a owns seam-wide shape.

**Open question for the owner, not for the builder:** whether the engine grows a
fail-soft `bank` (incident on the record, walk continues) or keeps the raise.
`session.py:594-597` already has the soft vocabulary — `UNPROVEN_LEVEL` as
`incident`, `record_id` `""` — so it is one `except` away. It is a behaviour
change, so W1-a does not make it.

### 2.6 Two stale claims in the protocol docstring

- `session_seams.py:215-216` — *"an 854-line function that is a schema writer
  with no schema"*. **58 lines** at HEAD, schema in
  `durable_state.build_conductor_state` (D2). **Fix in this PR.**
- `session_seams.py:212-213` — the bank half as *"wave 4's five blocks —
  identity · place · stimulus-and-path · honesty · the curve"*. `_record()`
  returns thirteen flat fields and no curve. **Flag, do not rewrite** — the
  blocks may be the intended destination shape (W1-b's subject).

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

The last is the important one: it is §1's *"a useless id empties `record_ids`"*
failure, and P8 is the only pin catching it where it does damage.

**Reuse `FakeRecords`' contract, do not re-write it** — §1's bar (`:261-263`)
says *"the twin's own contract run against the real store"*. Parametrize the
round-trip pins over both implementations so they cannot drift; the fixture
supplies a bundle and the twin ignores it.

Suites and content-greps per PR: §6.2.

---

## 3. W1-b — the thirteen fields, at the destination

### 3.1 The three builders, re-derived

All in `jasper/active_speaker/crossover_v2/spatial.py`. `_take_identity`
(`def :796`, keyword-only via the bare `*` at `:797`) returns **six** keys —
`phase` `:825`, `index` `:826`, `attempt` `:827`, `take_id` `:828`,
`session_id` `:829`, `wav_sha256` `:830` — and deliberately excludes the id key
(`:813-816`), because cloud and entry say `position_id` while a pose says
`pose_id`. Exactly three call sites, zero in tests: `:914`, `:1044`, `:1107`.

**D11 — §1's record-gap table cites stale lines for two of three builders.**
Its cloud column is correct; lateral is uniformly **+1** and entry is off by
**−2**. Corrected at HEAD:

| Engine field (`session.py`) | cloud (`:834`) | lateral (`:987`) | entry (`:1061`) |
|---|---|---|---|
| `session_id` `:693` | `:829` via identity | `:829` via identity | `:829` via identity |
| `kind` `:694` | **collision** (§3.2) | collision | collision |
| `baseline_record_id` `:695` | — | — | — |
| `position_deg` `:698` | `:924` | **`:1050`** *(§1: `:1051`)* | — |
| `position_axis` `:699` | `:925` | — | — |
| `prompt` `:700` | `:918` | **`:1048`** *(§1: `:1049`)* | — |
| `candidate_id` `:701` | — | — | — |
| `regime` `:702` | — | **`:1053`** *(§1: `:1054`)*, hardcoded `LATERAL_POSE_REGIME = "per_driver"` `:947` | — |
| `polarity` `:703` | — | — | — |
| `graph_fingerprint` `:704` | — | **`:1055`** *(§1: `:1056`)* | **`:1119`** *(§1: `:1117`)* |
| `level_db` `:705` | — | — | — |
| `stimulus_dbfs` `:706` | — | — | — |
| `incident` `:707` | — | — | — |

§1's **conclusion is unchanged and correct**: lateral 5, cloud 4, entry 2, and
six fields banked nowhere — `baseline_record_id`, `candidate_id`, `polarity`,
`level_db`, `stimulus_dbfs`, `incident`. Only the citations moved.

Two more §1 lines to correct while here: the `lateral_consumer` cite in the
`kind`-collision note is **`:1054`** (§1 `:159` says `:1055`), and entry's curve
arrays are **`:1121-1123`** — `freqs_hz` `:1121`, `magnitude_db` `:1122`,
`excluded` `:1123` — where §1 `:204` says *"magnitude only (`spatial.py:1122-1124`)"*.

### 3.2 The `kind` collision, and how to map it without lying

**Three different things are called `kind` in reach of one record:** the
engine's `spec.kind` ∈ `MEASURE_KINDS` (`contracts.py:1433-1437`, validated
`measure_spec.py:236-239`); the builders' `phase` (`_take_identity:825`); and
the sidecar's fixed record-type discriminator `POSITION_EVIDENCE_KIND`
(`web/…:3571`).

§1 `:156-161` is right that a silent `phase → kind` map mislabels every record.
It is worse than that — the map is **not well-defined**: `PHASE_LATERAL` is a
per-driver walk (`LATERAL_POSE_REGIME` `:947`) that is a `baseline` **or** a
`candidate` check depending on which candidate was applied under it (#3130's
body says exactly this, and it is why that PR put `graph_fingerprint` on the
lateral take). Only `PHASE_ENTRY_BASELINE` maps cleanly; the cloud phases
(`GROUP_PHASES`) split by which side of the apply they sit on.

**The rule: derive `kind` from `graph_fingerprint`, never from `phase`.** Equal
to the pre-apply applied-profile fingerprint → `baseline`; taken under a
candidate → `candidate`; a post-apply verify phase → `verify`.
`_entry_graph_fingerprint()` (`crossover_v2_flow.py:6999`, called `:5063` for
lateral) is already the source on the record.

**Carry #3130's judgement call verbatim — a later reader will want to "fix" it.**
That `graph_fingerprint` is the **applied profile's `candidate_fingerprint`**,
deliberately *not* the running-config hash `provenance.graph.fingerprint`
carries. A pose plays through the transient routing graph that omits crossover,
delay and linearization, so the running hash is **identical before and after an
apply** and cannot separate two walks. The applied candidate can. Different
namespaces — `capture_provenance.py`'s module docstring says so.

Where `kind` cannot be resolved, **write the field empty** rather than guessing.
The precedent is `baseline_record_id`: `""` where the prior baselined no such
pose, *"an honest fact about the capture, never a refusal to bank it"*
(`session.py:685-691`).

### 3.3 Where the six never-banked fields come from

| Field | Source at capture time | Note |
|---|---|---|
| `baseline_record_id` | `_baseline_for(spec, bearing, stimulus_dbfs)` `session.py:629` | resolved **per capture, by pose** (`session.py:679-684`). A bank-wide answer stamps the prior's last pose onto every capture — do not "simplify" it to one lookup |
| `candidate_id` | `spec.candidate_id` (`measure_spec.py:233`, default `""`) | on the flow side, the applied candidate's fingerprint |
| `polarity` | `spec.polarity` (`measure_spec.py:231`) | vocabulary owner is `program_analysis.polarity_label` |
| `level_db` | `proven_level_db` — `_proven_level()` `session.py:607` | **the whole point.** `prove()` is re-checked against the declared level by `fader_matches` (`:625`), and `session.py:616-624` names the 8.712 dB incident as the reason |
| `stimulus_dbfs` | the ladder rung, `MeasureSpec.level_ladder_dbfs` `:232` | moves the **stimulus**, never the claim (`measure_spec.py:217-223`) |
| `incident` | `outcome.incident` from the play seam | `UNPROVEN_LEVEL` is set at `session.py:594-595` when the level did not prove |

**D9 as a working fact.** §1 says `level_db` and `stimulus_dbfs` *"live only in
the debug ring today"*. They live in **neither** — those names do not occur in
`capture_provenance.py` at all. The ring's nearest neighbours, in
`CaptureProvenance.to_dict` (`:117`, body `:119-134`):

| Ring field | Source | Why it is not the engine's field |
|---|---|---|
| `main_volume_db` | `await cam.get_volume_db(best_effort=True)` `:316-318` | a **live fader read at retention time**, not the level the stimulus was admitted against |
| `session_volume_db` | `volume_plan.measurement_volume_db` `:334-340` | the session's declared level — closest analogue of `level_db`, and deliberately *not* a probe |
| `stimulus.peak_dbfs` | `_stimulus_peak_dbfs` `:201` = `max(segment.gain_db …)` | the program's peak, session volume explicitly **not** folded in (`:213`) |

§1's conclusion is **stronger** than it states: even with the ring enabled, what
is banked is three adjacent quantities under different names, none of which is
the proven level. A builder who greps `stimulus_dbfs` in `capture_provenance.py`
finds nothing and may conclude §1 was already satisfied. It was not.

### 3.4 Two structural constraints on adding fields

1. **The record is additively safe; the INDEX is not.** `read_lateral_take`
   (`position_cycle.py:205`) narrows to `_TAKE_FIELDS` by comprehension
   (`:250`), so extra record keys are ignored. But `read_position_cycle`
   (`:351`) is **strict in both directions** — unknown keys `:365`, missing keys
   `:368`, and per-take `set(take) != set(_TAKE_FIELDS)` at `:384`. **Adding a
   name to `_TAKE_FIELDS` (`:118-119`) invalidates every previously written
   index.** Since `read_position_cycle` and `takes_by_position` have zero
   production callers (§4.4), the cost is test-only today — but say so in the PR
   rather than discovering it.
2. **`_take_identity`'s six keys are shared by all three builders.** A field
   that belongs on every take goes there (one edit, three records); a
   kind-specific field goes in the builder. `graph_fingerprint` is on two of
   three builders and *not* in the identity block — #3130 chose the builder,
   which is the precedent to follow.

### 3.5 Test plan

Extend `tests/test_crossover_v2_spatial.py` (which already pins these builders;
`:623` pins `record["take_id"] == "entry_baseline_09_a02"`).

One **parametrized** pin over all three builders — per AGENTS.md, one
parametrized test beats an example cluster — asserting all thirteen engine field
names are present. §1's bar (`:270-271`) says mutation-verify by dropping one
field; do it **per builder**, because #3130 records that its first mutation
anchor matched **two** sites (the same two lines exist in `entry_baseline_record`)
and was caught only by the harness's count assertion. **Assert the match count
before mutating.**

Second pin: `kind` resolves through `MEASURE_KINDS` and a take under an unknown
graph gets `""`, not a guess.

---

## 4. W1-c — the retention lift

**Three sites in, three phases added, the ring out** (D6: not four sites).

### 4.1 The boundary being lifted

`_hand_to_retention` — `crossover_v2_flow.py:5256`, a method of
`CrossoverV2Session` (`:2154`). Signature `:5256-5258`:

```python
def _hand_to_retention(
    self, take_id: str, phase: str, result: Any, metadata: Mapping[str, Any],
) -> bool:
```

Body `:5266-5278`: `retain_position is None → return False` (`:5266-5267`); the
call (`:5269`); `except (OSError, RuntimeError, TypeError, ValueError)` → WARN
`correction.crossover_v2_position_retain_failed` with `exc_info=True`
(`:5270-5277`) → `return False`; `return True` (`:5278`).

**The parameter is named `take_id` but two of three callers pass a position or
pose id** — it is logged as `position_id=take_id` (`:5275`). Only site C passes
a minted take id. See §4.5.

The seam it guards: `V2FlowSeams.retain_position` (`:1515`),
`Callable[[str, Any, Mapping[str, Any]], None] | None`, bound once in the tree
at `web/correction_crossover_v2.py:5747-5749` inside `bind_v2_stage_seams`
(`:5675`) to `bind_position_retention(evidence_store, relay_session_id, refs)`.

### 4.2 The three sites

| | Enclosing | Call | Return used? | Record builder | Called from |
|---|---|---|---|---|---|
| **A** | `_retain_lateral_pose` `:5047` | `:5056-5067` — **the entire body** | **no** | `lateral_pose_record` built **inline as the 4th argument** (`:5058-5066`); no local, no other consumer | `_consume_lateral_pose` `:4968`, at `:5032`, **outside** `self._close_lock` (opens `:5037`; comment `:5029-5031`) |
| **B** | `_retain_cloud_position` `:5190` | `:5254` — the method's last statement | **no** | `cloud_position_record` → local `metadata` `:5227-5250`. **`metadata` has a second consumer:** written into `self._group_position_meta[phase][position.position_id]` at `:5251-5253` *before* the hand-off — which is why the build sits above the seam's early return (`:5202-5207`) | `_cloud_position_verdict` `:5112`, at `:5183`, **inside** `with self._close_lock:` (`:5182`) |
| **C** | `_retain_entry_baseline` `:6970` | `:7020-7022` | **yes — the only one.** `stored` → `artifact_ref = take_id if stored else ""` (`:7023`) → `EntryBaseline.from_measurement(..., artifact_ref=artifact_ref)` (`:7026-7031`) | `entry_baseline_record` → local `metadata` `:7000-7018`; `fingerprint = self._entry_graph_fingerprint()` `:6999`; `take_id` read back off the record at `:7019` | `_consume_entry_baseline` `:6914`, at `:6936`, guarded by `if verdict.accepted and measured is not None:` `:6935` |

**Three facts that shape the edit:**

- **Only site C reads the `bool`.** The lifted seam must keep a way to say
  *"stored / not stored"*, or `EntryBaseline.artifact_ref` silently becomes a
  take id that points at nothing. This is the one place where fail-soft has an
  observable consequence, and it is the thing to pin.
- **Sites A and B are on opposite sides of `self._close_lock`.** A lift that
  moves work across that boundary changes concurrency, and `:5029-5031` records
  a deliberate decision to keep A outside it. **Do not "harmonise" them.**
- **Site B's record is dual-purpose.** It is banked *and* stashed in
  `_group_position_meta` for the group's own bookkeeping. Lifting the bank must
  not take the stash with it.

### 4.3 The exact edit shape

**What moves:** the fail-soft `try/except/WARN/return False` of `:5266-5278`,
into the destination seam.

**What dies:** `_hand_to_retention` (`:5256-5278`, 23 lines) and the
`V2FlowSeams.retain_position` field (`:1515`) once the last caller is gone.

**What each call site becomes:** `self._records.bank(record)` — awaited, since
the seam is async after W4-a — with A and B discarding the result and C keeping
it:

```python
record_id = await self._records.bank(metadata)      # site C
artifact_ref = record_id or ""                       # replaces :7023
```

`record_id` is `""`-on-failure only if the store fail-softs; per §2.5 it does
not. **So the destination needs the fail-soft that `_hand_to_retention` is
today** — the lift does not delete that boundary, it relocates it. Put it in
the *binding* (where `bind_position_retention`'s docstring `:3511-3519` says the
strictness belongs to the store and the fail-soft to the caller), not inside the
store, or every other evidence-store caller loses the strictness it was built
for.

**Keep the WARN event name.** `correction.crossover_v2_position_retain_failed`
(`:5272`) is a log-search vocabulary; renaming it while moving it makes the
before/after unsearchable, and §8's content-verify step greps for exactly this
kind of symbol.

### 4.4 The three missing retention paths

The structural fact, verified at the dispatch: **`result` — the capture object
carrying `result.wav` — is never passed into any of the three.** In
`consume_capture` (`:4183`, signature `:4183-4185`, ends `:4334`), the dispatch
at `:4210-4227`:

| Arm | Line | Gets `index`? | `attempt`? | `result`? |
|---|---|---|---|---|
| `_consume_check(analysis)` | `:4211` | no | no | **no** |
| `_consume_measure(analysis)` | `:4213` | no | no | **no** |
| `_consume_lateral_pose(index, attempt, analysis, result)` | `:4215` | yes | yes | yes |
| `_consume_cloud_position(phase, index, attempt, analysis, result)` | `:4217-4219` | yes | yes | yes |
| `_consume_entry_baseline(index, attempt, analysis, result)` | `:4225` | yes | yes | yes |
| `_consume_verify(analysis, attempt=attempt)` | `:4227` | no | **yes** | **no** |

The three banking arms thread `index` and `attempt` — the identity pair
`take_id_for` needs. **CHECK and MEASURE have neither; VERIFY has `attempt`
only.** That is the gap, and it is a *signature* gap before it is a retention
gap.

**Where each phase's data is at capture time:**

| Phase | `def` | Body | What exists | The natural retention point |
|---|---|---|---|---|
| CHECK | `_consume_check` `:4638` | `:4638-4641` — four lines: `_check_verdict(analysis)`, `_safe_log_diag`, `return verdict` | `analysis`, `verdict`. **No WAV, no result, no provenance, no take id** | `_check_verdict` `:4643`, accept tail `:4677-4688`. `self._seams.publish_check(gain_plan, analysis.ambient_report or {})` at `:4687` already crosses a seam — **with the gain plan and ambient report only, no bytes.** Locals there: `gain_plan` `:4653`, `analysis` |
| MEASURE | `_consume_measure` `:4690` | `:4690-4693`, same four-line shape | `analysis`, `verdict` | `_measure_verdict` `:4778`, deferring accept at `:4923-4925`. `self._measure_analysis = analysis` (`:4924`) is the only thing that survives the call — the reduced `ProgramAnalysis`, **not the WAV** |
| VERIFY | `_consume_verify` `:7305`, signature `:7305-7307` (`attempt` **keyword-only**) | `:7308-7329` | `analysis`, `attempt`, `verdict` | `:7313` — `self._verify_analysis = analysis`, commented `:7310-7312` as *retained before grading* because the Full tier grades later *"from a call that cannot see this capture."* **That comment is the requirement statement for this work item** |

**So banking these three requires, in order:**

1. **Thread `index`, `attempt` and `result` into all three arms** (`:4211`,
   `:4213`, `:4227`). Mechanical, and it is the whole reason this item is not
   a one-liner per phase.
2. **A take id for a phase with no position.** CHECK and MEASURE have no
   prompted spot. `entry_baseline_record` already solved this shape:
   `position_id=f"{PHASE_ENTRY_BASELINE}_{index:02d}"` (`:1108`) with a comment
   (`:1113-1114`) that its position id **is** its take id. Follow it —
   `f"{PHASE_CHECK}_{index:02d}"` — rather than inventing a second convention.
3. **A record builder per phase**, or one parameterized builder. Three new
   near-copies of `cloud_position_record` would be the third implementation of
   one concern, which AGENTS.md's duplication rule forbids. **Extend
   `_take_identity`'s users, do not clone them.**
4. **`provenance.take()` must be carried, not moved** (obligation 4, §4.6).

**A judgement to surface rather than settle here:** VERIFY's data at `:7313` is a
reduced `ProgramAnalysis`, and CHECK's at `:4687` is a gain plan — neither is a
capture record in `_record()`'s sense. Banking "a take" for these phases may mean
banking the *capture* (bytes + identity, once `result` is threaded) or the
*analysis* (what the phase actually produces). §1 says *"a CHECK, a MEASURE and a
VERIFY capture each produce a banked take"* (`:281-282`), which reads as the
former. **Confirm with the owner before building** — it is the difference between
a mechanical thread-through and a new record shape.

### 4.5 A defect the lift must resolve: the entry baseline's doubled take id

Found while deriving §4.2, unpinned by any test, and the lift is where it dies.

`take_id_for` (`spatial.py:779`, body `:793`) is minted **twice,
independently**: inside the record (`_take_identity:828` → `record["take_id"]`)
and inside the closure (`web/…:3546`). They agree only because `attempt`
round-trips through `_take_identity`'s `"attempt"` key (`:827`). **For the entry
baseline they do not.** `entry_baseline_record` sets `"position_id":
identity["take_id"]` (`:1115`, the one kind where the two coincide `:1113-1114`);
`_retain_entry_baseline` reads that take id back (`:7019`) and passes it as the
closure's `position_id`, which re-mints:

```
record["take_id"]  = "entry_baseline_09_a01"
closure take_id    = "entry_baseline_09_a01_a01"   # web/…:3546
```

The artifact lands at `positions/entry_baseline_09_a01_a01.json` while the record
inside says `entry_baseline_09_a01`, and `refs["position_artifacts"]` gets the
**doubled** id (`web/…:3588`). `take_id_for`'s own docstring (`:788-792`) states
the invariant broken: *"The seam and the record must name the same take or the
bundle's sidecar path and the session's own evidence disagree."*

**Blast radius, honestly:** readers locate takes by **glob**, not by
reconstructing filenames, so nothing is observed-broken today. What is broken is
the **join** — `position_artifacts[].take_id` disagrees with the artifact's own
— and W1-d's index is built on exactly that join. Tests pin the single suffix at
the builder (`test_crossover_v2_spatial.py:623`) and a hand-made single-suffix
filename at the reader (`test_crossover_v2_position_cycle.py:377`); **no test
exercises the closure with an entry-baseline record**, which is why it survived.

**The fix falls out of the lift:** the store mints its path from the record's own
`take_id` (§2.3), and the second mint dies with the closure. **Add a pin
anyway** — bank an entry-baseline record, assert the artifact path's stem equals
`record["take_id"]` — because "it fell out for free" is what gets un-fixed later.

### 4.6 The #3076 obligations — quoted, and how the lift discharges each

Quoted from §1 `:227-243`, which records them so they cannot be lost.

> **1.** Build the three missing retention paths (CHECK / MEASURE / VERIFY takes)
> **at the destination**, retaining into #3064's `take_id` convention.

**Discharged by §4.4.** "At the destination" is load-bearing: the paths are built
against the lifted seam, never added to `_hand_to_retention` first and migrated
after. The `take_id` convention is `take_id_for` (`spatial.py:779`) — and §4.5 is
the one place it is currently violated.

> **2.** Flip the sidecar's **seven** readers — four via
> `evidence_packet.RING_SIDECAR_GLOB` … and **three that glob flat `*.json`** …
> fixing the globbers onto the one index as you flip them.

**Carried, with the composition corrected (D7, D8).** Seven is right; it is
**3 + 4**, not 4 + 3:

| # | Reader | Enclosing | Keys it consumes |
|---|---|---|---|
| G1 | `evidence_packet.py:1246` | `_capture_snr_block` `:1191` | `jts_session_identity` (via `read_session_identity`, `attribution/session_identity.py:233`; sub-keys `scheme`/`session_id`/`aliases`), `diagnostic` — every key containing `"snr"` (`_DIAGNOSTIC_SNR_MARKER` `:350`), `wav_sha256`, `phase`. **Touches no WAV** |
| G2 | `harmonic_evidence.py:603` | `_bind_measure_captures` `:582` | `phase` (gated `== PHASE_MEASURE`), `wav_sha256`, `jts_session_identity.session_id` (read as a Mapping at `:625`, **not** via `read_session_identity`), `setup_calibration_id` `:772`, `diagnostic` `:854` vs `FIDELITY_FIELDS` `:199` (5 fields), `diagnostic.glitch_detected` |
| G3 | `feature_classifier.py:456` | `load_round_captures` `:414` | `phase`, `jts_session_identity.session_id`, **and the filename stem** — `sidecar.stem.split("_")[0]` `:487`, the microsecond stamp. Reads no `diagnostic` |
| F1 | `audio_measurement/capture_integrity.py:193` | `_iter_sidecar_files` `:190` → `check_sidecar` `:147` | `capture_integrity{frames, encoded_frames, block_gaps, block_gap_frames, zero_run_count, truncated, capture_chain}` (must `== "alsa_s32le"`) and `frame_ledger{received_frames, declared_frames, encoded_frames, render_gaps, render_gap_frames, lost_at}` (`_frame_ledger_findings` `:123`) |
| F2 | `scripts/harmonic-distortion-replay.py:260` | `bind_captures` `:252` | `wav_sha256` `:265`, `phase` `:274`, `setup_calibration_id` `:625`, `diagnostic` `:346` vs its **own** `FIDELITY_FIELDS` `:105` (**10** fields), `diagnostic.glitch_detected` `:305` |
| F3 | `scripts/severed-twin-replay.py:240` | `bind_measure_capture` `:214` | `phase`, `diagnostic.{epsilon_ppm, predicted_ripple_db, alignment_confidence}` `:236` (the binding keys — requires a **unique** hit or `SystemExit`), `setup_calibration_id` `:331`, `diagnostic` `:382`/`:608` vs its `FIDELITY_FIELDS` `:74` (**19** fields) |
| **F4** | **`scripts/derive-crossover-incident-fixture.py:138`** | `_measure_sidecar` `:121` | `diagnostic.{delay_us, alignment_confidence, predicted_ripple_db}` `:141-143`, then `epsilon_ppm`, `woofer_gate_floor_source`, `woofer_gate_window_ms`, `tweeter_snr_db`, `woofer_snr_db`, `woofer_validity_floor_hz` `:229-236`. **Returns the `diagnostic` sub-dict**, which is why `derive()` subscripts it flat |

**The union any replacement must supply:** `phase`, `wav_sha256`,
`jts_session_identity{scheme, session_id, aliases}`, `setup_calibration_id`,
`diagnostic{…}`, `capture_integrity{…}`, `frame_ledger{…}`.

**Three traps in that table:**

- **Three `FIDELITY_FIELDS` definitions exist and disagree** — 5
  (`harmonic_evidence.py:199`), 10 (`harmonic-distortion-replay.py:105`), 19
  (`severed-twin-replay.py:74`). A duplication finding in its own right;
  **do not silently pick one** while flipping readers.
- **G3 parses the filename** (`:487`), not just the JSON. A new naming scheme
  must preserve a leading stamp token or fix G3 in the same PR.
- **F4 is a pinned frozen bank**, not a live path
  (`bundle/dsp_state/capture_dump_20260810/*_measure_*.json`). It breaks on the
  **rename** as well as the deletion. Being a fixture deriver, the right answer
  may be to re-derive and re-freeze once — **an owner call, not a builder's.**

**WAV re-pairing is two places, not three (D10):** `harmonic_evidence.py:613`
and `feature_classifier.py:482`, both `sidecar.parent.parent / "wav" /
f"{sidecar.stem}.wav"`. `severed-twin-replay.py:249` uses
`with_suffix(".wav")` (flat sibling, the **un-split** ring) and
`harmonic-distortion-replay.py:266-271` binds by sha256 across a separate
`--captures` dir. **Three "replacements" for three different bindings would be
three new defects.**

> **3.** **The sidecar dies in that same PR**, per ruling S5 — it is a proof
> bracket, not a fallback.

**Carried.** What dies, enumerated so nothing is left behind:

- `_maybe_retain_capture` — `web/…:3223` (signature `:3223-3227`), its sole call
  site `:3162-3172`, and `_prune_capture_dump` `:3179`.
- The gate and its constants: `capture_dump_enabled` `:220` (body `:229`),
  `XOVER_CAPTURE_DUMP_DIR` `:203` (`/var/lib/jasper/xover-capture-dump`),
  `XOVER_CAPTURE_DUMP_ENABLED_MARKER` `:211` (`"ENABLED"`),
  `XOVER_CAPTURE_DUMP_MAX_FILES = 90` `:216`,
  `XOVER_CAPTURE_DUMP_MAX_BYTES = 300 MB` `:217`, applied at `:3351-3356`.
- `RING_SIDECAR_GLOB` `evidence_packet.py:336` (`"**/sidecar/*.json"`) and its
  `__all__` entry `:180`.
- **The shell split, which is not Python** — `scripts/bank-crossover-round.sh`
  **`:214-227`** (§1 `:246-250` says `:234-238`; corrected). It `tar`s the ring
  off the Pi and `find … -maxdepth 1 -exec mv` splits `*.wav` into `dumps/wav/`
  and `*.json` into `dumps/sidecar/`. That `-maxdepth 1` is **why
  `RING_SIDECAR_GLOB` needs its `**/`**. Four more places in the same script
  depend on the layout: the header doc `:46-49`, the exit-code contract
  `:65-67`, the `python -m jasper.audio_measurement.capture_integrity
  "$DEST/dumps/sidecar"` call `:235-237` whose `checker_rc` `:238` **becomes the
  script's exit code**, and the summary line `:253`.

**A stale reference to delete while here:** `harmonic_evidence.py:587` cites
`:func:`~.round_views._dump_ring_captures`` — **that function does not exist
anywhere in the tree.** Trivial, in scope, fix it inline.

> **4.** `provenance.take()`'s single shot at the analyze seam is **plumbing to
> carry, not a field move**: by retention time the routing graph is restored and
> the fader may have moved.

**Carried, and this is the part to review hardest** (§8 `:1116-1118`).
`take()` (`capture_provenance.py:163`) swaps `_pending` to `None` under
`self._lock` (`:156`) and returns it — **single-shot and consuming.** A second
call with no intervening `record()` (`:159`) returns `None`, **not the previous
capture's context** (`:148-154`: stale provenance on a forensic clip is worse
than absent).

**Production call sites: exactly one** — `web/…:3172`,
`provenance=provenance.take() if provenance is not None else None`, inside the
`_maybe_retain_capture` call obligation 3 deletes.

So the lift must **re-home the single shot, not copy the fields.** Two
consequences:

- **The shot happens at the analyze seam today** —
  `bind_production_analyze(meta=refs, provenance=provenance)` (`web/…:5742`).
  The values are live at analyze time and **not** at retention time, which is
  obligation 4's whole point.
- **Deleting `_maybe_retain_capture` deletes the only consumer.** If nothing new
  calls `take()`, the recorder becomes write-only and the provenance is silently
  lost — **passing every test**, because none asserts a household run banks
  provenance. **Pin the carry explicitly** or this obligation discharges itself
  into a hole.

> **5.** The `ENABLED` gate over unconditional ~300 MB ring writes is an **owner
> call**. Leave it as it is until his ruling lands.

**Carried, and now moot-if-3-lands:** obligation 3 deletes the gate with the
ring, so the ruling is only needed if the ring survives. **Surface that rather
than letting the deletion decide it.**

### 4.7 One more thing the lift removes for free

`consume_capture`'s `entry: Any = None` (`:4184`) is **never referenced** in the
body (`:4183-4334`) — zero uses. Two callers pass a real value
(`correction_crossover_v2_relay.py:585`, `…_wired.py:607`); provenance is
recorded in the analyze binding instead (`web/…:5742`). Dead parameter, three
sites, in a signature this PR already edits. **Verify no `getattr`/`**kwargs`
reach before cutting**, then delete it with both call-site arguments.

---

## 5. W1-d — the 4j SQLite index

**Blocked until W1-a and W1-c land** — #3130's own body says why, and #3130 is
where the last input gap closed:

> **4j itself stays blocked** and is not attempted here — there is no production
> `RecordStore` implementation … and `_hand_to_retention` still has exactly three
> call sites … so CHECK/MEASURE/VERIFY bank no take at all.

Six columns, off `_record()`: **session · kind · position · candidate ·
timestamp · path**. The last two are the store's to supply, *"since only it knows
where it put the record and when"* (`session.py:667-670`).

- **`path`** is `bank`'s return under §2.3's scheme — the id **is** the path, so
  the column costs nothing extra.
- **`timestamp`** is not on `_record()`, and **the three builders disagree about
  its type** — a trap worth the whole column:

  | Builder | Declared | Value | Source |
  |---|---|---|---|
  | cloud | `captured_at: float` `:844` (emitted `:927`) | **Unix epoch float** | `time.time()` `crossover_v2_flow.py:5169` → `_CloudPosition.captured_at` (`:1342`) → `:5236` |
  | lateral | `captured_at: str` `:994` (emitted `:1056`) | **ISO-8601 `%Y-%m-%dT%H:%M:%SZ`** | `crossover_v2_flow.py:5064` |
  | entry | `captured_at: str` `:1069` (emitted `:1120`) | ISO-8601, same format | `crossover_v2_flow.py:7007` |

  Normalize at the index, not at the builders — a builder change rewrites banked
  records the store already wrote once. And **reuse `captured_at`; do not stamp a
  second clock at bank time**, or a re-bank of identical bytes (§2.3, idempotent)
  changes the timestamp and the write stops being idempotent.
- **`kind`** is §3.2's mapping — the reason W1-d is downstream of **W1-b** as
  well as of W1-a and W1-c.

Model it on `jasper/wake_events.py` (925 lines) per §1 `:288-290`.
**Rebuildable by rescanning; the banked files stay the SSOT**
(`session.py:672-674`). Bar: delete the index, rebuild by rescan, assert the
same six columns.

**It inherits §4.5's defect.** The index joins `path` to the record's identity,
and the doubled entry-baseline take id makes that join wrong for one of three
kinds. If §4.5 is not fixed in W1-c, W1-d must not paper over it.

---

## 6. PR slicing, bars and tiers

### 6.1 The slices

**Six PRs for four work items** — W1-c splits three ways. §1 `:276-279`
anticipates the split and says how: split by *reader* (obligation 2), keeping
obligations 1+3 together, because *"a sidecar that dies before its replacement
writes is a data loss, and a sidecar that outlives it is the second-writer
defect."*

| PR | Item | Contents | Est. | Gated on |
|---|---|---|---|---|
| 1 | **W1-a** | `crossover_v2/record_store.py` + the `__init__.py` index entry + `tests/test_crossover_v2_record_store.py` + the two stale docstrings (§2.6) | ~300 | **W4-a** |
| 2 | **W1-b** | the six fields + the `kind` mapping across three builders; the parametrized pin | ~250 | PR 1 |
| 3 | **W1-c/1** | the three call sites lift + `_hand_to_retention` dies + §4.5's take-id fix + the dead `entry` param (§4.7) | ~250 | **W5-b**, PR 2 |
| 4 | **W1-c/2** | the three missing phases: thread `index`/`attempt`/`result` through `:4211`/`:4213`/`:4227`, the builders, the `provenance.take()` carry | ~350 | PR 3 |
| 5 | **W1-c/3** | the seven readers flipped + the ring and its shell split die, **in one PR** (S5) | ~350 | PR 4 |
| 6 | **W1-d** | the SQLite index | ~200 | PR 5 |

**Why 1 and 3 land in different PRs when §1 says keep them together:** PR 4 **is**
the replacement writing and PR 5 is the death, so the sidecar outlives its
replacement by exactly one merge — the rule's *data-loss* direction is satisfied,
and its other direction (*"a sidecar that outlives it is the second-writer
defect"*) names a **steady state**, not a one-merge window. **If the conductor
would rather not hold that window open, stack 4 and 5 and land them together.**
Tradeoff stated, not decided.

### 6.2 Verification bar per PR

Every "assert" below is a **mutation instruction**: break it, watch *only* the
named pin fail, restore, re-run green — and check the un-mutated run is green,
because a harness that fails silently in both directions reads as covered either
way (§8 `:1156-1161`).

| PR | Suites | Mutation targets | Content-grep for the post-merge check |
|---|---|---|---|
| 1 | `test_crossover_v2_record_store`, `test_engine_twin`, `test_crossover_v2_engine_skeleton`, `test_active_speaker_commissioning_evidence_store`, `test_crossover_v2_position_cycle` + every module importing `prior_bank` | §2.7's five | `git grep -c POSITION_EVIDENCE_KIND -- jasper/` rises by 1; `git grep -n "854-line" -- jasper/` = **0** |
| 2 | `test_crossover_v2_spatial`, `test_crossover_v2_blend_prescription`, `test_crossover_v2_position_cycle`, `test_crossover_v2_stage_bridge` | drop each of the thirteen, **per builder**, asserting the anchor match count first (#3130's lesson) | all thirteen `_record()` names present in `spatial.py`; `git grep -c '"kind"' -- jasper/active_speaker/crossover_v2/spatial.py` |
| 3 | every module referencing `crossover_v2_flow`, `lateral_pose_record`, `entry_baseline_record`, `read_lateral_take` | remove the fail-soft → site C's `artifact_ref` pin fails; re-mint the take id → §4.5's stem pin fails | **`git grep -c "_hand_to_retention\|retain_position" -- jasper/` = 0**; `entry` gone from `consume_capture`'s signature |
| 4 | the above + `test_crossover_v2_round_wiring`, `test_correction_crossover_v2_endpoints` | drop the `provenance.take()` carry → the provenance pin fails (**build this pin first — nothing covers it today**) | a CHECK, a MEASURE and a VERIFY take each present in a round fixture |
| 5 | all seven readers' suites + `test_docs_impact` | break each reader's key lookup one at a time | `git grep -c RING_SIDECAR_GLOB` = **0**; `git grep -c xover-capture-dump` = **0** across `jasper/`, `scripts/` **and** `deploy/`; `bank-crossover-round.sh` has no `dumps/sidecar` |
| 6 | `test_crossover_v2_record_store` + the new index suite | delete the index, rebuild by rescan, assert the same six columns | the six column names present |

**Every grep above must be wrap-safe** (§8 `:1147-1151`) — join line pairs before
matching, and **paste the count into the PR**.

### 6.3 Review tier — judged against AGENTS.md, not inherited

AGENTS.md's non-negotiable tier is a **closed list**: the hearing clamps, the
XVF3800 brick hazard, secrets, deploy integrity, renderer ALSA, silent deafness,
paid tests, `deploy/install.sh`.

| PR | Tier | Honest reasoning |
|---|---|---|
| 1 | **default** | new module, two backends, no clamp, no fader write, no secret. §1 agrees |
| 2 | **default** | data shape only |
| 3 | **default** | **it touches `CrossoverV2Session`, and that is not by itself an adversarial trigger.** What the lift moves is a *write of evidence JSON* and its fail-soft. It writes no fader, computes no DSP, touches no secret and no `install.sh`. The concurrency question (`self._close_lock`, §4.2) is real and is the thing to review hardest — but "hard to review" is not the tier test, and treating it as one is the ceremony AGENTS.md's tiered policy replaced |
| 4 | **default** | same, plus the `provenance.take()` carry — §8 `:1116-1118` already names it *"the part to review hardest"* |
| 5 | **default** | deletion + reader flips. **One caveat worth stating:** it removes an observability surface (the ring) that AEC- and distortion-forensics scripts consume. That is not on the closed list, so it is not adversarial — but it *is* worth an explicit owner ack, because obligation 5's ENABLED ruling becomes moot when the gate is deleted (§4.6) |
| 6 | **default** | mechanical, on a shipped pattern |

**Nothing in W1 is adversarial tier.** The adversarial items in this DAG are
W4-a, W4-c, W5-b and W5-c (§8 `:1108-1113`) — all fader-path — and W1 sits
*downstream* of them precisely so it does not have to be.

**One thing W1 must not quietly acquire:** if a slice ends up touching
`SessionVolumePlan` or `_session_volume_io._set` (the wave-5 named exception
that still writes the fader directly —
`crossover-v2-engine-design.md:103-110`), **the tier changes and this table is
void.** Say so in the PR rather than re-reading the table.

---

## 7. Traps this work will actually hit

**All of §8 `:1128-1164` applies unchanged — read it, this does not restate it.**
Three carry extra weight here: **squash-ancestry** (this is a six-PR stack, the
highest-exposure shape there is — `git cherry`, `git rebase --onto`); **the reap
trap** (`web/correction_setup.py:1311-1327` — W1-a's methods are `await`ed inside
that bridge per D1, and its unbounded wait is *observability, not permission to
abandon cleanup*); and **merge-tree validation** of any tree-scanning check.

Four are **specific to W1**, each with the evidence that it is real:

1. **Confirm a call site by grepping the symbol, never by reading the cited
   line.** D6 is the worked example: `crossover_v2_flow.py:6879` still exists,
   still sits in a `try/except/WARN`, and still says *"Mirrors
   `_retain_cloud_position`'s fail-soft boundary"* — and is a **comment about a
   different seam**. One grep (`self._seams.retain_position` → two hits, both
   inside `_hand_to_retention`) settles in a second what reading the line settles
   wrongly.
2. **A matching total is not a matching set.** §1's "seven readers" is right and
   both halves are wrong (D7 + D8). A count that reconciles is the easiest thing
   in the world to stop checking.
3. **Subject-sweep, follow to assertion.** Do not sweep for the *subject*
   ("retention", "sidecar") and stop at the first hit. The ring's death (PR 5)
   reaches a **shell script** and a **frozen fixture deriver** that no Python
   grep for `RING_SIDECAR_GLOB` finds (F4, and `bank-crossover-round.sh:214-227`).
   §1 itself missed F4.
4. **Re-derive every line number before you cut.** Every citation here was true
   at `c253c3cf1`; §1's were true at `4a9e9f631`, and **six had already moved**
   (D6–D11) — one of them changing a work item's title. This document will rot
   the same way.
