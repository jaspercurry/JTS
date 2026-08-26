# W5 brief: the seam colour, the front-end wiring, and W7's dissolution

> **This brief dies when its work items land.** Supersede-and-delete: when
> W4-a, W4-c, W5-a, W5-b, W5-c and W5-d have merged, this file is deleted in the
> same PR as the last of them, and its doc-map row goes with it. It is a
> scaffold for six PRs, not a doc anyone should still be scanning in October.

**Scope.** The §5 work items of
[`REFACTOR-CUTOVER-2026-08.md`](REFACTOR-CUTOVER-2026-08.md), plus the §4
seam-colour decision they sit on top of, taken from "recommended" to
"implementable". The plan owns the *schedule*; this brief owns the *diff shape*.
Where the two disagree, the disagreement is recorded in §6 rather than papered
over.

## 1. The seam-colour ADR, ready to land

§4's recommendation was re-verified against the code before this text was
written, not restated from the plan. What the re-derivation confirmed, what it
strengthened, and what it added is in §6; the ADR below is the settled version.

**Mint at landing time.** `docs/adr/` runs to **0178** at `c253c3cf1`, so
**0179** was free when this was written — but ADRs land from several branches
and the number is claimed by whoever merges first. `ls docs/adr/ | tail -1` at
rebase time, take the next free number, and fix the slug to match
(`NNNN-<title-slug>.md`). Do not create the file from this brief before then:
the directory is append-only and immutable, so a file that lands on a taken
number is a supersession problem, not a rename.

**Land it in the same PR as W4-a**, not before. An ADR describing a colour the
code does not have is a claim about the future, and this directory records
decisions that hold.

---

**The block below is the file's whole contents.** Fenced so its own `#`/`##`
headings do not become this brief's; copy the fence's interior verbatim, change
the number in the title, and save it under the matching slug.

```markdown
# ADR-0179: The tuning engine's seams are async, and a release completes before cancellation propagates

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

`TuningSession`'s five seams are declared synchronous —
`SessionGraph.install` / `patch` / `restore`
(`jasper/active_speaker/crossover_v2/session_seams.py:105`, `:121`, `:132`),
`VolumeClaim.acquire` / `prove` / `release` (`:162`, `:171`, `:198`),
`RecordStore.bank` / `read` / `persist` / `read_state` (`:236`, `:240`, `:248`,
`:252`), the `Recommender` callable (`:271`, no `Awaitable` in its return
type), and `PlaybackTransaction.run`
(`crossover_v2/playback_transaction.py:190`). Twelve declarations in all.

**Three of the five have production implementations, and all three are async.**
`MeasurementSessionGraph` (`crossover_v2/session_graph.py:135`, `:176`, `:190`),
`VolumeOwner` (`jasper/volume_owner.py:261`, `:582`, `:504`), and
`play_program` (`jasper/active_speaker/program_playback.py:118`). The other two
have none: `RecordStore`'s only `def bank(` in the tree are two test doubles,
and `Recommender` is unbound. The seam module already recorded the mismatch and
deferred it to *"the wave that wires `TuningSession` to a front end"*
(`session_seams.py:95-102`), adding that it is **one decision for all of them,
not one per seam** — the same sentence `docs/crossover-v2-engine-design.md:87`
carries.

**The threading model decides it, and it is not what "async web host" would
suggest.** The transport is a stdlib `ThreadingHTTPServer` with plain-`def`
`BaseHTTPRequestHandler` methods (`jasper/web/correction_setup.py:61`). There is
exactly one background event loop — thread `jasper-correction-loop`, started by
`_ensure_loop` (`:1275`) — and handler threads reach it through `_run_async`
(`:1292`), which is `asyncio.run_coroutine_threadsafe(...)` followed by
`fut.result(timeout=60.0)`.

That splits the seams' callers across two thread kinds:

- **The walk runs ON the loop.** `_run_relay_capture` (`:1037`) fires the whole
  session as one long-lived coroutine at `:1153` and returns the tap link
  immediately. `measure()`'s caller lives inside that coroutine.
- **The short endpoints run on handler threads** — status, apply, restore,
  position-ready, complete, retake — and already bridge through `_run_async`.

## Decision

**All five seams are async.** Twelve seam declarations become `async def` (the
`Recommender` alias becomes
`Callable[[Sequence[str]], Awaitable[Mapping[str, Any]]]`); the four verbs
`measure` / `analyze` / `recommend` / `save` become `async def`;
`__enter__` / `__exit__` (`crossover_v2/session.py:332-351`) become
`__aenter__` / `__aexit__`; `_attach_cleanup_failure` (`:73-92`) takes an
awaitable cleanup.

**Every release path completes before cancellation propagates.** The two
release sites — `_release_slots` (`:537-553`) and
`_release_both_after_failed_open` (`:515-535`) — use the shape this repo already
ships in twenty-five places, whose reference form is
`jasper/web/correction_crossover_v2_wired.py:821-840`: start the cleanup as a
task, `await asyncio.shield(task)`, and on `CancelledError` **await the task to
completion before re-raising**, tolerating a repeat cancel. A bare
`await asyncio.shield(coro)` is not this decision — it detaches the release and
lets the cancellation propagate past it, which reorders the two releases and
lets the caller's drain gate open early (see Consequences). Acquires are **not**
shielded: an acquire that a caller no longer wants should not finish.

## Consequences

**What this makes easier.**

- The bridge that already exists is the only cost the short endpoints pay: one
  `_run_async(...)` per call, at ten sites that already have one.
- `RecordStore` is cut async **before** it is built, which is the same argument
  `session_seams.py:5-12` makes for cutting all three seams before any exists.
  Concretely it lets the store `await asyncio.to_thread(...)` around its file
  I/O instead of blocking the one loop on every `bank`.
- The release paths stop being a special case. `camilla.py:814-818` already
  shields a duck release in a `finally` for the identical reason — *"an
  interrupted release leaves the speaker ducked"* — so the engine now matches
  the door it writes through rather than diverging from it.

**What this makes harder, and what it gives up.**

- `analyze` and `recommend` become async even though they are the **offline**
  verbs, deliberately not gated on an open session (`session.py:430-437`). A CLI
  caller pays one `asyncio.run(...)`; an HTTP caller pays a bridge it already
  has. That is the whole cost, and it buys the store the freedom above.
- `_attach_cleanup_failure`'s broad catch is `except Exception`
  (`session.py:90`), which does **not** catch `CancelledError`. Under async that
  is a new hole: a cancelled cleanup would escape and replace the very exception
  the function exists to preserve. The shield discipline above is what closes
  it, and it is therefore part of this decision rather than a follow-up.
- A shielded release means a cancelling caller waits for the fader before it
  gets its `CancelledError`. That is the intended trade:
  `correction_setup.py:1323-1326` already refuses to *"release measurement
  ownership while its graph/volume finalizer can still mutate the speaker."*

**Rejected: sync seams with a bridge inside each one.** Each seam method stays
`def` and calls `_run_async(...)` internally. Called from a handler thread this
works; called **from the loop thread**, which is where the walk lives, it
deadlocks — and not for 60 s. `_run_async` blocks the loop on `fut.result(...)`,
so the coroutine it just scheduled can never run; the timeout fires;
`fut.cancel()` cancels a task that never started, so `_tracked`'s
`finally: drained.set()` (`:1304-1305`) never executes; and the reap then calls
`drained.wait()` **unbounded** (`:1327`) on that same blocked loop. The
correction subsystem wedges permanently with the fader stranded at measurement
level. `_run_graph_mutation` (`:1331`) passes `timeout=None`, so a seam bridging
through it would not even get the alarm.

**Rejected: colour per seam, by evidence** — async where the implementation is
async, sync where it is not. It splits the verbs, so `measure` would be async
and `analyze` sync and a caller has to remember which; and it is unstable,
because the first `RecordStore` that wants to `await` an fsync flips the sync
half anyway. `session_seams.py:95-102` and `crossover-v2-engine-design.md:87`
both foreclose it in advance.
```

---

**One rider, not part of the ADR text.** `crossover-v2-engine-design.md:81-89`
describes the mismatch in the pre-decision tense and names **two** async
implementations where there are three (its count of *"four seams"* is correct
within its scope — the fifth is declared in `playback_transaction.py`). W4-a
should re-tense that paragraph and point it at the new ADR in the same PR, the
way W5-d re-tenses `:252-268`. See §6.

## 2. The wiring map

Every line below was re-derived at `c253c3cf1`. Where it differs from §5 of the
plan, the difference is flagged **[≠plan]** and explained in §6.

### 2.1 The chain, corrected

| Layer | Where |
|---|---|
| nginx | `deploy/nginx-jasper.conf:463` — `location /correction/` → `127.0.0.1:8770` |
| transport | `ThreadingHTTPServer` + `BaseHTTPRequestHandler`, `web/correction_setup.py:61` |
| handlers | `do_GET` `:7809`, `do_POST` `:7984` — **both plain `def`**, and the only two |
| funnel | `:8014` `if path.startswith("/crossover/")` → `:8015` `self._dispatch_crossover(path)` **[≠plan: the call is `:8015`; `:8014` is the guard]** |
| dispatch | `_dispatch_crossover` `:7366`, a flat chain of **twelve** `if path …: … return` arms over **thirteen** route strings, no `elif` |
| loop bridge | `_ensure_loop` `:1275` (thread `jasper-correction-loop`), `_run_async` `:1292`, `_run_graph_mutation` `:1331` |
| walk | `_run_relay_capture` `:1037`, fired fire-and-forget at `:1153` |
| host module | `web/correction_crossover_v2.py` — **zero** routes, zero HTML/CSS/JS, zero CSRF (all three greps return 0) |

**The twelve arms**, with line numbers, because the post-merge check in §5
counts them: `/crossover/v2/session`+`/crossover/v2/verify` `:7369` (**one arm,
two routes** — `if path in {…}`), `position-ready` `:7433`, `complete` `:7469`, `retake` `:7488`,
`apply` `:7506`, `republish` `:7565`, `restore` `:7582`, `decline` `:7605`, and
four nested arms — `recover-volume` `:7637`, `relay-cancel` `:7757`, `reset`
`:7761`, `level-match` `:7766`.

**The bridge lives in `correction_setup.py`, and the host module receives it as
an argument. [≠plan]** `_run_async(` has **34 call sites in
`correction_setup.py`** (plus its own def at `:1292`). `correction_crossover_v2.py`
has **zero** — what it has is the injected `run_async` parameter, called at
**nine** sites: `:1325`, `:1340`, `:1369`, `:1411`, `:1448`, `:4336`, `:7274`,
`:7883`, `:7938`. The plan's *"ten sites, e.g. `correction_crossover_v2.py:7274`,
`:7883`"* names the right two examples under the wrong symbol and one too many.
The injection point is a single line: `correction_setup.py:6345`
`run_async=_run_async`.

That matters to W5-b for one reason: **the bridge is a seam of the flow already**,
so the async flip does not have to invent a way to remove it — it removes an
argument that is already parameterised, at one call site.

### 2.2 The session registry, corrected

`correction_setup.py` holds the registry; the host module does not.
`_session_lock` `:133`, `_relay_position_gate` `:149`,
`_relay_complete_request` `:154`, `_relay_retake_request` `:160`.

**[≠plan]** The plan attributes both the write and the clear to
`_set_relay_capture`. There are two functions:

- **`_begin_relay_capture`** (`:706`) **writes** the four slots at `:719-731`,
  under the lock, refusing when a capture is already in flight (`:722-726`).
- **`_set_relay_capture`** (`:614`) **clears** them at `:615-623` when the value
  is `None` or the status left `_RELAY_IN_FLIGHT_STATUSES`.

The distinction is load-bearing for W5-b: the *refusal* that makes "one session
at a time" true lives in the writer, not the clearer, and a cutover that moved
only the clearer would drop the mutual exclusion.

### 2.3 One handler serves both stages — and it already carries the stage flag

This is the finding that resizes W5-a.

```
correction_setup.py:6308  def _handle_crossover_v2_relay(handler, *, verify_only: bool, idle_hold=no_hold)
                   :6314    """POST /crossover/v2/session | /crossover/v2/verify (Wave 5a)."""
                   :6341    prepare = v2host.prepare_v2_verify if verify_only else v2host.prepare_v2_session
                   :6342    prepared = prepare(raw, status=status, run_async=_run_async, camilla_factory=_camilla)
                   :6358    kind = RelayCaptureKind(label=…, open=…, run_and_consume=…, request_stop=…,
                                                    position_gate=…, local=wired,
                                                    request_complete=…, request_retake=…)
                   :6369    return {"relay": _run_relay_capture(kind, relay_base, return_url=…, idle_hold=…)}
```

**`:6341` is the whole of W5-a's "stage argument".** The plan describes W5-a as
*"fold the two into one preparer plus a stage argument"*; the caller already
computes that argument (`verify_only`) and already selects on it at one line.
W5-a is therefore **pushing an existing boolean one frame down**, not
introducing a discriminator — a materially smaller and lower-risk change than
the plan's ~380/+120 framing implies. Its real work is reconciling the two
bodies, and there is exactly one asymmetry in their seam binding to reconcile
(next paragraph).

**`V2PreparedSession` (`correction_crossover_v2.py:5355`) is the lifecycle
interface, and it has eight fields**: `label`, `open`, `run_and_consume`,
`request_stop`, `position_gate`, `capture_source`, `request_complete`,
`request_retake`. Seven of them are adapted straight into `RelayCaptureKind`
(`correction_setup.py:834`) at `:6358-6367`; `capture_source` is consumed at
`:6357` to decide whether a relay origin is required. **This dataclass, not
`CrossoverV2Session`, is what the dispatch layer actually sees** — so it is the
type W5-b must keep producing, byte-for-byte in field values, if the endpoint
suite is to pass unedited.

### 2.4 The two preparers, and where `TuningSession` goes

| | stage 1 | stage 2 |
|---|---|---|
| def | `prepare_v2_session` `:5944` | `prepare_v2_verify` `:6520` |
| body | `:5944-6477` (**534 lines**) | `:6520-6901` (**382 lines**) |
| lazy import of `CrossoverV2Session` | `:6002` | `:6557` |
| construction | `CrossoverV2Session.hydrate(…)` `:6357` | `CrossoverV2Session(…)` `:6785` |
| seam binding | `bind_v2_stage_seams(…)` `:6369-6379` | `:6801-6812` **[≠plan: `:6801`, not `:6800`]** |
| holder | `holder: dict[str, Any] = {}` `:6258` | `:6711` |
| filled | `holder["run"] = _build_source_run(…)` `:6432` | `:6856` |
| drained | `async def _run(client, pi_session)` `:6448-6449` | `:6872-6873` |
| returns | `V2PreparedSession(` `:6455` | `:6879` |

**The one asymmetry in the seam binding**, which W5-a must decide rather than
diff away: stage 1 passes `publish_check=publish_check` and stage 2 passes
`publish_check=_publish_check` — a different callable under an
underscore-prefixed name. Every other kwarg in the two eleven-line calls is
identical. **[≠plan]** The plan calls these *"the same `bind_v2_stage_seams`
call shape"*; they are the same shape with one substituted binding, and a fold
that misses it silently swaps stage 2's check publisher.

**Where `TuningSession` slots in.** `_open()` — the nested closure inside each
preparer that builds the conductor — is the construction point, and the
session's lifetime is the `_run()` coroutine's. The mapping:

| `TuningSession` | What it replaces today |
|---|---|
| `open()` | `register_session_measurement_graph(session_graph)` (`correction_crossover_v2.py:4106`, from inside `bind_production_play` `:3965`) **plus** `_volume_hooks`'s `_open` arm (`:5399`), which is `acquire_session_measurement_pause()` then `await plan.open(context.session_volume_db, _set, _get)` (`:5408`) |
| `measure(spec)` | the consume/retain walk driven through `holder["run"]` (`:6432` / `:6856`, awaited `:6449` / `:6873`) |
| `save()` | `persist_conductor_state(conductor, failure_code=None, evidence=refs)` `:6431` (def `:2809`) |
| `close()` / abandon | `_volume_hooks`'s `_close` `:5443` and `_abandon` `:5450`, each of which is `_put_the_graph_back()` (`:5414` → `release_session_measurement_graph()` `:1215`) then `plan.close/abandon(_set, _get)` (`:5446` / `:5453`) then `release_session_measurement_pause()` in a `finally` |
| `analyze()` | nothing yet — `bind_production_analyze` (`:3037`) is the flow's per-capture analyze seam, not the session-wide verb (§2 of the plan) |
| `recommend()` | nothing yet — unbound (§3 of the plan) |

**One deletion W5-b unlocks that the plan does not name.** The session graph is
held in a **module global** — `_session_graph`, written by
`register_session_measurement_graph` (`:1213`, from `bind_production_play` at
`:4106`) and cleared by `release_session_measurement_graph` (`:1215`). Once the
graph is `seams.graph` on a session object whose lifetime is the run coroutine's,
that global and its displacement warning (`:1204-1211`) have no reason to exist.
Its docstring already reads as a lifetime workaround: *"Two callers, and between
them they cover every drain."*

### 2.5 The five seam bindings at construction

What W5-b writes at the converged `_open()`. Each row says what exists, what is
owed, and by which item.

| Seam | Bind to | State at HEAD |
|---|---|---|
| `graph` | `MeasurementSessionGraph(emit=_emit_program_graph, cam_factory=camilla_factory, writer_lock=…, confirm_live=confirm_graph_is_live)` — constructed today at `correction_crossover_v2.py:4098` | **exists.** Already `async` (`session_graph.py:135/176/190`), so it satisfies the seam directly after W4-a with no adapter. Move the construction out of `bind_production_play` and stop registering the global. |
| `volume` | the handle-holding adapter over `VolumeOwner` — `acquire_level` `volume_owner.py:261`, `prove` `:582`, `release` `:504` | **owed: W4-c.** The owner's API is handle-carrying and the seam's is handle-free; §3.2 has the mapping. |
| `records` | the `RecordStore` implementation | **owed: W1-a.** No production `def bank(` exists — the only three in the tree are `session_seams.py:236` (the Protocol), `tests/engine_twin.py:214` and `tests/test_crossover_v2_engine_skeleton.py:133`. |
| `play` | `program_playback.play_program` (`jasper/active_speaker/program_playback.py:118`), awaited in production today at `correction_crossover_v2.py:4314` inside `bind_production_play` | **exists**, and already `async`. What is owed is the `PlaybackTransaction.run` shape around it: the seam takes `spec / position_deg / prompt / level_db / stimulus_dbfs` (`playback_transaction.py:190-198`) where `play_program` takes a program plus the `bind_program_playback_seams` bundle (`crossover_v2_flow.py:9024`). That adapter is W5-b's, not W4-c's. |
| `recommend` | the prescriber's `packet → propose → stage → status` path | **owed: W3-b.** §3 of the plan says do not re-extract it; bind to it. |

**The construction belongs beside `bind_v2_stage_seams` (`:5675`), not inside a
preparer.** That function is already the single owner of *"which callable
implements each seam"* for the flow, its docstring says so in as many words, and
it already logs the stage's capability declaration. `V2FlowSeams`
(`crossover_v2_flow.py:1500`) has **17 fields** (`:1503-1606`); `EngineSeams` has
five. Putting the second binder anywhere else re-creates the *"two call sites
free to disagree"* problem that binder exists to have solved — and the two
binders coexist for the whole cutover, because the apply/rollback half of
`V2FlowSeams` never moves (§3 of the plan: *"not a target. Ever."*).

## 3. W7's dissolution

Authorities: the wave-5 W-ledger's W7 row
(`REFACTOR-TUNING-2026-08.md:937`) and ADR-0004's constraint set. The ledger
calls W7 *"the single named exception … dies with wave 6's flow; deliberately
not routed into a seam that is about to be deleted."* This section says what it
dies into.

### 3.1 The write door, exactly

`_session_volume_io` (`web/correction_crossover_v2.py:1289`, body `:1289-1306`,
**18 lines**) returns `(_set, _get)`:

```
:1294  async def _set(db: float) -> bool:
:1296      return await camilla_factory().set_volume_db(db, best_effort=False)
:1297  except CamillaUnavailable as exc:
:1298      raise RuntimeError("CamillaDSP is unavailable") from exc
```

**[≠plan]** The callee is `camilla_factory().set_volume_db(...)`, not
`CamillaController.set_volume_db` named directly — the factory is injected. The
chain past it is as the plan says: `jasper/camilla.py:684` → clamp through
`_coerce_main_volume_db` (`:141-160`) → `c.volume.set_main_volume` (`:695`). No
coordinator, no owner. That is the whole exception.

**Five call sites, four of them writers:**

| Site | Caller | Thread | What it drives |
|---|---|---|---|
| `:1367` | `enforce_session_volume_ceiling_if_stale` (`:1348`) | **handler**, via `run_async(…, timeout=15.0)` at `:1369` | `plan.enforce_ceiling(set_v, get_v)` |
| `:1409` | the recover-volume path | **handler**, via `run_async` at `:1411` | `plan.recover_unresolved(set_v, get_v)` |
| `:1446` | `reconcile_session_volume_for_new_session` | **handler**, via `run_async` at `:1448` | `plan.abandon(set_v, get_v, reason="stale_session_reset")` |
| `:4187` | a capture-time hold | — | **read-only**: `_, get_v = …`, feeding `hold_measurement_volume(get_v, …)` at `:4188` (def `session_volume_plan.py:972`). `_set` is discarded. |
| `:5397` | `_volume_hooks` (`:5390`) | **loop** — awaited directly | `plan.open` `:5408`, `plan.close` `:5446`, `plan.abandon` `:5453` |

**The split across thread populations is the finding the plan does not draw, and
it changes W5-c's shape.** Three of the four writers are **handler-thread**
callers that bridge through `run_async`; the fourth is the **loop-thread**
session. That is the same two-population split §1's ADR turns on, and it means
W5-c is not one substitution repeated four times.

### 3.2 The owner mapping — three calls, one adapter

`VolumeOwner`'s API is **handle-carrying**; the seam's is **handle-free**. The
adapter W4-c builds holds the handle between calls:

| `VolumeClaim` (`session_seams.py`) | `VolumeOwner` (`volume_owner.py`) | Notes |
|---|---|---|
| `acquire(level_db)` `:162` | `acquire_level(ClaimKind.SESSION_MEASUREMENT, level_db) -> VolumeClaimHandle` `:261` | Fail-closed: an unconfirmable write raises `VolumeClaimRefused` and **leaves no claim held** (`:266-268`). The seam contracts that `release` is still safe after a raised acquire — satisfied, because there is nothing to give back. |
| `prove() -> float \| None` `:171` | `prove(handle) -> float \| None` `:582` | Already the seam's exact semantics, including preemption: `:617-618` sets `result = "preempted"` and `:627` returns `None`. `None` for released, preempted, ducked-over, unreadable, or disagreeing (`:591-594`). |
| `release()` `:198` | `release(handle, *, household_level_db=None)` `:504` | Idempotent and safe against nothing-held by construction: `:542-543` `if self._claims.get(handle.token) != handle: return`. The owner's docstring cites `session_seams.VolumeClaim.release` **by name** at `:514`. |

`VolumeClaimHandle` (`:143-155`) is a frozen dataclass of `kind` / `token` /
`level_db` / `depth_db`, opaque by intent — so the adapter's only state is one
`handle: VolumeClaimHandle | None`.

**One precision the plan's "four ranked claim kinds" glosses.** `ClaimKind`
(`:116-122`) has four members, but `_LEVEL_RANK` (`:128-132`) ranks **three** —
the transient duck is deliberately absent because *"it declares an attenuation,
not a level"*. `SESSION_MEASUREMENT` ranks 1, between `HOUSEHOLD` (0) and
`COMMISSIONING` (2). A session claim is therefore preemptible by commissioning,
which is exactly why `prove()` is called once per stimulus rather than once per
spec.

**The 0 dB ceiling is untouched, and provably so.** `volume_owner.py:39-45`
states the owner *"sits BEHIND that door as its only caller, never as its
exception"*, and the clamp itself is `camilla.py:154`
`clamped = max(MIN_MAIN_VOLUME_DB, min(MAX_MAIN_VOLUME_DB, value))`. Nothing in
W5-c goes near either.

### 3.3 What survives, what dissolves — and the plan's split is two-way where the code is three-way

`SessionVolumePlan` (`session_volume_plan.py:667`) **owns no CamillaDSP** — its
own docstring says so at `:670`, and every mutating method takes the
`(set, get)` callables as *parameters*:

```
:906   async def open(self, measurement_volume_db, set_main_volume_db, get_main_volume_db) -> SessionVolumeOpenResult
:1109  async def close(self, set_main_volume_db, get_main_volume_db, *, reason="session_closed")
:1121  async def abandon(self, set_main_volume_db, get_main_volume_db, *, reason="session_abandoned")
:1133  async def enforce_ceiling(self, set_main_volume_db, get_main_volume_db, now=None)
:1153  async def recover_unresolved(self, set_main_volume_db, get_main_volume_db)
```

All five are **already `async`**, which matters: after W4-a the `VolumeClaim`
substitution is colour-compatible with no bridge anywhere.

**DISSOLVES — the fader writes.** Swapping the injected door for a `VolumeClaim`
needs no internal restructure, exactly as the plan says.

**SURVIVES — the durable restore latch, and here is why the owner cannot take
it.** `VolumeOwner` is explicitly **in-memory and per-process**: *"Durable
volume-safety state belongs to the claim holders that own it"* (`:65-68`). Five
pieces, and the reason each stays:

1. **The persisted `_State`** (`:589-596`: `status` / `reason` / `opened_at` /
   `wall_clock_ceiling_s` / `measurement_volume_db` / `original_main_volume_db`)
   at `/var/lib/jasper/active_speaker_crossover_session_volume.json`
   (`:109-111`), `SCHEMA_VERSION = 1` (`:80`). **Cannot move:** the owner has no
   disk at all, and giving it one would make it the second durable
   volume-safety store.
2. **`original_main_volume_db`, persisted before the first fader mutation.**
   Snapshotted in `open` at `:932-935`, built into the state at `:937-944`, and
   written at `:946` under the comment `# Write BEFORE the first mutation.`
   (`:945`). **Cannot move:** the owner learns the household level from the
   coordinator, not from a snapshot taken at this session's open — and the whole
   point is surviving a crash between the snapshot and the write.
3. **`_drain_restore`'s exact→emergency ladder** (`:1056-1107`; candidates built
   at `:1076-1081`; `_mark_unresolved("session_volume_restore_unconfirmed")` at
   `:1100`; `SessionVolumeRestoreResult.FAILED` at `:1107`). **Cannot move:**
   `release()` restores the next-ranked level outright and has no second
   candidate. There is no owner call that means *"try the exact original, and if
   the hardware will not confirm it, take the emergency floor, and if neither
   confirms, latch."*
4. **`needs_recovery`'s two branches** (`:744-748`): a latched `unresolved`
   state, **or** a durably `active` state this process did not open. **Cannot
   move:** the second branch is crash/restart hydration by definition, and
   `volume_owner()` answers `None` in a process that never installed one
   (`:799-806`).
5. **The wall-clock ceiling** — `DEFAULT_WALL_CLOCK_CEILING_S = 1800.0` (`:87`)
   under `MAX_WALL_CLOCK_CEILING_S = 3600.0` (`:96`), enforced by timestamp
   rather than by process restart (`:1067-1068`). **Cannot move:** it is a
   property of the walked-away human, not of a claim.

**[≠plan] The plan's W5-c deletes `(set, get)` from all five methods and hands
the plan a `VolumeClaim` instead. Two of the five cannot take one.**
`enforce_ceiling` and `recover_unresolved` run precisely when **no session is
open and often no claim was ever taken in this process** — the ceiling fires on
envelope build against a stale session, and recovery fires after a restart. A
`VolumeClaim` is a handle over an in-memory per-process owner; after a crash
there is no handle to release through. So the honest split is three-way:

| Method | Door after W5-c |
|---|---|
| `open` / `close` | **`VolumeClaim`.** Session-scoped, claim in hand. `acquire(level_db)` at open, `release()` at close. Clean. |
| `abandon` | **both.** Session-scoped from `_volume_hooks._abandon` (`:5450`, claim held) *and* process-scoped from `reconcile_session_volume_for_new_session` (`:1446`, no session, no claim). |
| `enforce_ceiling` / `recover_unresolved` | **not a `VolumeClaim`.** Route them the way the ledger already ruled W11 (unresolved-volume recovery): `VolumeOwner.declare_household_level_db` (`volume_owner.py:297`) plus the plan's own exact→emergency ladder. That is the same answer for the same shape, and it keeps one story rather than two. |

Deciding this in the brief rather than in the PR is the point: an executor who
takes W5-c's sentence literally writes a `VolumeClaim` parameter onto
`recover_unresolved` and then discovers at the first crash-recovery test that
there is nothing to hand it.

**[≠plan] `SessionVolumePlan` has a second consumer, outside crossover-v2
entirely.** `jasper/active_speaker/seat_level_ramp.py` builds its **own** plan
with its own state path, sets its own ceiling
(`plan.set_wall_clock_ceiling_s(watchdog_s + 60.0)`, `:1571` — *"a killed
leveling pass should surface far sooner than a measurement session's 30-minute
walked-away window"*), and calls `plan.open(start_db, set_main_volume_db,
get_main_volume_db)` at `:1573` and `plan.close(…, reason="seat_level_complete")`
at `:1689`. The god file adds two more: `crossover_v2_flow.py:9150` (`open`) and
`:9163` (`abandon`, the session-death hook). **Five production call sites across
three files**, where the plan's W5-c reads as though `correction_crossover_v2.py`
were the only one. `seat_level_ramp` is ledger row W10 and already holds a
`SESSION_MEASUREMENT` claim of its own, so it can mint the `VolumeClaim` W5-c
asks for — but it has no `TuningSession`, and a W5-c scoped to the wizard breaks
it at import time.

### 3.4 The one ordering invariant that must not be lost

`_clear_resolved` (`:858`) drops the in-memory intent **before** persisting.
Its docstring (`:859-891`) records both halves:

- **The measured hazard of the other order, at `:868`: `−60.0 → −12.5` = +47.5
  dB.** Persist-first left a drain that had already confirmed its volume on the
  hardware, then met an `OSError` writing the marker, raising out with the plan
  still `active` — so `assert_ready` passed and the plan named the declared
  volume onto a speaker sitting at the emergency floor.
- **The mirror-image persist guard from `:875`**, which is why the persist is
  guarded exactly as `_mark_unresolved` guards its own: a surviving intent makes
  a restart offer recovery for a volume already restored, and the re-drain moves
  the fader **up** to the household snapshot, never to the measurement level.

**[≠plan]** The plan cites the docstring as `:862-880`; it opens at `:859` and
closes at `:891`, and the mirror-image paragraph runs past `:880`. The source
uses Unicode `−` and `→`, so a grep for `-60.0 ->` finds nothing — a wrap-safe
sweep is not enough here; the glyphs differ too.

## 4. PR slicing, verification bars, tiers

Six PRs. Sizes are re-estimated against what §2 and §3 found, not copied.

| PR | Scope | Size | Tier |
|---|---|---|---|
| **W4-a** colour flip | `session_seams.py` (12 declarations) · `playback_transaction.py:190` · `session.py` (10 seam sites, 4 verbs, `:332-351`, `_attach_cleanup_failure` `:73-92`, both release paths) · re-tense `crossover-v2-engine-design.md:81-89` · land the ADR | ~270 | **adversarial** |
| **W4-b** twin follows | `tests/engine_twin.py` (**418**, not 419) · `test_engine_twin.py` (401) · `test_crossover_v2_engine_skeleton.py` (1,267). `engine_declarations.py` (93) is untouched — it imports nothing and declares constants | ~200 | mechanical |
| **W4-c** `VolumeClaim` adapter | one handle-holding class over `acquire_level` / `prove` / `release` | ~90 | **default** (demoted — §6) |
| **W5-a** preparers converge | push `verify_only` one frame down; reconcile the `publish_check` asymmetry; re-point 26 comments in 4 further files (§5.1) | ~250 net, **6 files** | default |
| **W5-b** `TuningSession` in production | build `EngineSeams` beside `bind_v2_stage_seams`; delete the two `holder` dicts and the `_session_graph` global | ~350 | **adversarial** |
| **W5-c** plan sheds its doors | three-way split (§3.3), five call sites across three files | ~350 | **adversarial** |
| **W5-d** stale docs + grep | engine-design `:252-268` only; the widened grep | ~25 | docs |

**Verification bars, as mutations. Every one names the pin that must fail.**

- **W4-a.** mypy clean is the backstop the N6 disposition named — but mypy passes
  a bare shield too, so it is not the bar. The bar is: *a `close()` cancelled
  mid-`volume.release()` still releases **both** slots **in order**.* **Mutate:**
  replace the `except CancelledError: await task; raise` arm with a bare
  `await asyncio.shield(coro)` and watch that pin alone fail. A bare shield
  detaches the release, so the graph restore in the `finally` races it — the
  order `_release_slots`'s docstring promises (`:538`, *"in reverse order of
  taking"*) is what breaks, and a pin that only asserts "both were called"
  stays green under the mutation and is therefore not covering this.
- **W4-a, second bar.** *A cancelled cleanup does not replace the primary
  exception.* `_attach_cleanup_failure` catches `except Exception`
  (`session.py:90`), which does not catch `CancelledError`. **Mutate:** remove
  the shield from `_release_both_after_failed_open` and assert the `open()`
  failure still propagates as itself rather than as `CancelledError`.
- **W4-b.** The existing engine tests pass **with no assertion edited**. An
  assertion that had to change means the flip changed behaviour.
- **W4-c.** Three pins: acquire-raised-then-release (no claim held, release is a
  no-op — mutate by removing the `:542-543` guard's equivalent in the adapter);
  double-release; prove-after-preemption returns `None` (drive it by taking a
  `COMMISSIONING` claim, rank 2, over the session's rank 1).
- **W5-a.** `tests/test_correction_crossover_v2_endpoints.py` (**13,171 lines**)
  passes with no assertion edited, and both stages produce a `V2PreparedSession`
  with the same eight field values. **Mutate:** swap stage 2's
  `publish_check=_publish_check` for stage 1's `publish_check` and confirm a pin
  catches it — if none does, that asymmetry is unpinned and W5-a must add one
  before it folds the two bodies.
- **W5-b.** One end-to-end pin driving a whole session through the real
  converged preparer against twin seams. Plus the engine-internal enforcement
  pin (`session_seams.py:295-296`), which now has a front end to point at.
- **W5-c.** Four drain paths (`close` / `abandon` / `enforce_ceiling` /
  `recover_unresolved`) land the fader on `original_main_volume_db`; one for the
  emergency fall-through; one for the `_clear_resolved`-before-persist order —
  **mutate by swapping the two statements and watching that pin alone fail.**
  Plus one that `seat_level_ramp.py:1573`/`:1689` still open and close.
- **W5-d.** Paste the widened grep's own output into the PR (§5.2 has today's).

**On the tiers.** AGENTS.md's non-negotiable list is **closed** — clamps, DSP
math on the output path, secrets, `deploy/install.sh` — and **none of these six
items touches any of them.** W4-a, W5-b and W5-c are still adversarial, on a
stated reason rather than the list: each one's failure mode is a fader nobody
holds, the shape `correction_setup.py:1323-1326` already treats as fail-closed.
That is the standard to argue against if you want a tier lowered. W4-c is
demoted; §6, Ruling B.

## 5. Content-greps for the post-merge check

Run these **against the merge result** (`git merge-tree --write-tree origin/main
HEAD`), not the branch. Paste each count into the PR.

### 5.1 Per-item

| After | Command (from repo root) | Expect |
|---|---|---|
| W4-a | `grep -c "async def" jasper/active_speaker/crossover_v2/session_seams.py` | ≥ 10 (today: 0) |
| W4-a | `grep -n "asyncio.shield" jasper/active_speaker/crossover_v2/session.py` | 2 sites |
| W4-a | `grep -rn "held_target_db" docs/crossover-v2-engine-design.md` | the `:252-268` hits re-tensed |
| W4-c | `grep -rn "acquire_level(" jasper/ \| grep -v volume_owner.py` | **5** (today: **4** — see §6, the owner already has takers) |
| W5-a | `grep -rn "prepare_v2_verify" jasper/` | 0 (today: **28 across 6 files** — see below) |
| W5-b | `grep -rn "TuningSession(\|EngineSeams(" jasper/` | ≥ 1 (today: **0** — production constructs neither) |
| W5-b | `grep -n "_session_graph\b" jasper/web/correction_crossover_v2.py` | 0 (today: declared `:1191`, written `:1212`, cleared `:1179` / `:1235`, read `:1203` / `:1232`, plus `global` at `:1176` / `:1202` / `:1231`. Four further hits — `:1209`, `:1334`, `:4102`, `:5438` — are log-event names and a `source=` string, and stay) |
| W5-b | `grep -n "holder\[" jasper/web/correction_crossover_v2.py` | 0 (today: `:6432`, `:6449`, `:6856`, `:6873`) |
| W5-c | `grep -n "_session_volume_io" jasper/web/correction_crossover_v2.py` | def + the one read-only site (today: def + 5) |
| W5-c | `grep -rn "set_main_volume_db=\|get_main_volume_db=" jasper/active_speaker/session_volume_plan.py` | the latch's internals only |

**W5-a's real reach, which resizes it again.** `prepare_v2_verify` has **28
hits across six files**: `correction_crossover_v2.py` **11** (the def at `:6520`
plus prose), `crossover_v2/durable_state.py` **11**, `crossover_v2_flow.py`
**3**, `crossover_v2/journey.py` **1**, `crossover_v2/capture_plan.py` **1**,
and `correction_setup.py` **1** (the selector at `:6341`). Two are code; **26
are comments and docstrings naming the function by name.** They go stale the
moment it folds, and AGENTS.md's comment rule makes deleting or re-pointing them
part of the same PR — so W5-a touches six files, not two, and its "no behaviour
change" claim needs a grep to back it, not just a green suite.

**One grep that lies, so read it rather than counting it.** `grep -rn "def bank("`
returns four hits, not three: the Protocol (`session_seams.py:236`), two test
doubles (`tests/engine_twin.py:214`, `tests/test_crossover_v2_engine_skeleton.py:133`)
— and `tests/test_crossover_v2_candidate_republish.py:60`, which is a **pytest
fixture named `bank`** and not a `RecordStore` at all. A count of `def bank(`
therefore reads as "one production implementation exists" the moment W1-a lands
and the fixture is still there.

### 5.2 The enumerated-set check, widened and re-run

The ledger's grep (`REFACTOR-TUNING-2026-08.md:953-959`) uses a trailing paren
and structurally cannot see a bare bound-method reference. Widened:

```
grep -rn "set_volume_db" jasper/
```

**17 hits at `c253c3cf1`**, and the classification is the answer, not the count:

- **the clamped door and its own internal callers** — `camilla.py:684` (def),
  `:741` (`adjust_volume_db`), `:810` / `:827` (the graph-swap duck and its
  release);
- **the owner's bound doors** — `volume_coordinator.py:2068`, and `:2830`
  (`set_fader_db=lambda db: fader.set_volume_db(db, best_effort=True)`, inside
  the `install_volume_owner(VolumeOwner(...))` call);
- **W15, ruled stopped** — `commissioning_service.py:1336`;
- **W7, the named exception this brief dissolves** —
  `correction_crossover_v2.py:1296`;
- **the reference the paren-grep could not see** — `cli/seat_level.py:413`
  `set_main_volume_db=cam.set_volume_db,`. A separate CLI process with no
  `install_volume_owner`, so `volume_owner()` answers `None` there
  (`volume_owner.py:799-806`) and nothing is unsafe. **Say that, rather than
  resting the claim on a grep that cannot find it.**
- **seven prose mentions** — `camilla.py:457`, `:700` (a log format string),
  `volume_owner.py:205`, `seat_level_ramp.py:1336`, `session_seams.py:158`,
  `correction_setup.py:291`, `cli/aec_tune.py:308`,
  `audio_measurement/level_solver.py:32`.

**The enumerated set holds after widening.** W5-c removes the W7 row from it.

### 5.3 Traps

- **Squash-ancestry.** `main` squash-merges, so `git merge-base --is-ancestor`
  reports a landed branch as un-landed. Use `git cherry`; rebase with
  `git rebase --onto`.
- **The reap trap, and it is coupled to W4-a's shield.** `_run_async`'s timeout
  path (`correction_setup.py:1310-1328`) cancels the loop task, waits
  `_RUN_ASYNC_CANCEL_DRAIN_TIMEOUT_S` (**45.0 s**, `:197`, aliased from
  `_CROSSOVER_VOLUME_RECOVERY_TIMEOUT_S` `:196`), logs **CRITICAL**, then waits
  **unbounded** — *"a terminal response must never release measurement ownership
  while its graph/volume finalizer can still mutate the speaker"* (`:1323-1326`).
  The gate it opens is `drained.set()` in `_tracked`'s `finally` (`:1304-1305`).
  **A bare `await asyncio.shield(coro)` defeats this**: the outer coroutine
  raises immediately, `_tracked` unwinds, `drained` fires — while the detached
  release is still running. The fail-closed promise is only kept if the cleanup
  is awaited to completion before the `CancelledError` propagates. **The alarm
  is observability, not permission to abandon cleanup.**
- **Sweep the subject, then follow every hit to the assertion it makes.**
  `held_target_db` has **10 doc hits**; exactly **one** of them
  (`crossover-v2-engine-design.md:252-268`) asserts the parameter is live.
  `adr/0004:101-106` says the opposite in as many words, and five of the ten are
  the cutover plan's own record of the finding. A subject sweep that stops at
  the count reports a two-doc problem where there is a one-doc problem — which
  is exactly what happened (§6).
- **Wrap-safe *and* glyph-safe.** This tree wraps at ~79 columns, so join line
  pairs before matching. And `session_volume_plan.py:868` writes `−60.0 → −12.5`
  with U+2212 and U+2192 — an ASCII grep for `-60.0 ->` finds nothing.
- **Validate a tree-scanning check against the merge result**, never the branch:
  `git merge-tree --write-tree origin/main HEAD` (AGENTS.md). Two-dot diffs
  render phantom removals in this shared repo.
- **Re-derive every line number before you cut.** Every `file:line` here was
  true at `c253c3cf1`. Two of the plan's had already moved by one or two lines
  within a single merge, and three pointed at the wrong file.

## 6. Disagreements with the plan, recorded

Everything marked **[≠plan]** inline, plus two rulings. Nothing here is papered
over; each is a place an executor following §4/§5 literally would go wrong.

| # | Where | The plan says | At `c253c3cf1` |
|---|---|---|---|
| 1 | §2.1 | `_dispatch_crossover` reached from `:8014` | `:8014` is the guard; the call is `:8015` |
| 2 | §2.1 | the short endpoints bridge with `_run_async`, *"ten sites, e.g. `correction_crossover_v2.py:7274`, `:7883`"* | `_run_async` has **zero** hits in that file. The two examples are real, under the injected `run_async` parameter, and there are **nine** of them. `_run_async` itself lives in `correction_setup.py` with 34 call sites |
| 3 | §2.2 | the four relay slots are *"written by `_set_relay_capture` `:719-731` and cleared at `:615-623`"* | two functions: `_begin_relay_capture` (`:706`) writes at `:719-731` and owns the in-flight **refusal**; `_set_relay_capture` (`:614`) clears at `:615-623` |
| 4 | §2.4 | stage 2's `bind_v2_stage_seams` call is `:6800-6811`, *"the same call shape"* as stage 1's | `:6801-6812`, and it is **not** the same shape: stage 2 passes `publish_check=_publish_check` |
| 5 | §2.4 | teardown includes `_release_pause_best_effort` (`:1309`) and `release_session_measurement_graph` (`:1215`), listed among `correction_setup.py` symbols | both live in `correction_crossover_v2.py`; `correction_setup.py` has **zero** hits for either |
| 6 | §3.1 | `_set` calls `CamillaController.set_volume_db` | it calls `camilla_factory().set_volume_db(...)` — the controller is injected |
| 7 | §3.4 | `_clear_resolved`'s docstring is `:862-880` | `:859-891`; the mirror-image paragraph runs past `:880` |
| 8 | §4 | `tests/engine_twin.py` is 419 lines | 418 |

**Ruling A — W5-d's ADR half should be dropped, not executed.** The plan says
`adr/0004:101-106` *"still describes `held_target_db` as live plumbing"* and
that *"the fix is a superseding note."* Those lines say the opposite: *"Scope
note, so the next reader does not mistake this for still-live plumbing: the
specific `held_target_db` parameter goes dead on the measurement path once the
engine installs one session-scoped graph…"* The forecast came true; an ADR is a
dated record and reads as of its date, so minting a supersession because a
correct forecast was fulfilled is ceremony. **W5-d keeps only the engine-design
half (`crossover-v2-engine-design.md:252-268`, which really does say *"read this
parameter as live plumbing … until 6e lands"*) plus the widened grep — whose
output §5.2 already carries. ~25 lines, not 60.** *(Visible and deliberately not
acted on: ADR-0004's own `jasper/camilla.py:96-141` citations no longer match
HEAD — `_duck_release_target_db` is now at `:85-130`. The directory is
immutable, and a supersession to fix line numbers is worse than the drift.)*

**Ruling B — W4-c demotes from adversarial to default, and the premise under it
is wrong.** The plan's reason is *"the first production implementation of a
fader claim."* **It is the fifth consumer of one that already ships.**
`acquire_level(` has four production call sites outside the owner today —
`correction_setup.py:267` (level-match), `:3583` (autolevel),
`sound_setup.py:1474` (commissioning floor tone), `balance_volume_guard.py:279`
(the measurement-volume guard) — and **three of them already take
`ClaimKind.SESSION_MEASUREMENT`**, the very kind W4-c's adapter takes
(`correction_setup.py:268`, `:3584`, `balance_volume_guard.py:280`). The owner
already clamps behind `camilla.py`'s door (`volume_owner.py:39-45`), already
contracts idempotent release (`:542-543`) and preemption-safe `prove`
(`:617-627`), and cites `session_seams.VolumeClaim.release` **by name** at
`:514`. W4-c is a ~90-line box holding one `VolumeClaimHandle` between three
calls; it touches no clamp and no DSP math, and a second review pass buys
nothing its three pins do not. By the same evidence **W5-b is not *"the first
production path through the volume claim"*** either — it is the first through
*the seam*. W5-b nonetheless **stays adversarial**, as do W4-a and W5-c: each of
those three really can leave a fader nobody holds, which is the stated standard
in §4.

*(A consequence worth carrying into W5-b rather than discovering: the session's
claim shares rank 1 with three existing takers. Two `SESSION_MEASUREMENT`
holders can therefore coexist in one process, which is exactly why `prove()` is
contracted per stimulus and not per spec.)*

**Two places the plan is right and stronger than it argues.**

1. **Option B does not hang for 60 s — it wedges permanently.** §4's refutation
   stops at *"hangs until the 60 s timeout fires and cancels."* Traced through
   `_run_async`: a loop-thread caller blocks the loop on `fut.result(...)`, so
   `_tracked` never runs, so `drained` never sets, so the reap's unbounded
   `drained.wait()` (`:1327`) blocks the same loop forever. And
   `_run_graph_mutation` (`:1331`) passes `timeout=None`, so a seam bridging
   through it deadlocks with no alarm at all. §1's ADR states it this way.
2. **The shield has house prior art, and the plan's wording would miss it.**
   Twenty-five `asyncio.shield` sites ship in `jasper/`. The reference form is
   `correction_crossover_v2_wired.py:821-840` — create the task, shield it, and
   on `CancelledError` **loop** until it is done, tolerating a repeat cancel —
   with `bass_extension/bench/activation.py:291-298` as the short version
   (`ensure_future` · `shield` · `except CancelledError: await; raise`). *"Shield
   the two releases"* read literally produces a bare `await asyncio.shield(coro)`,
   which is a different and weaker thing (§5.3's reap trap). §1's ADR names the
   idiom rather than the function.

**One thing this brief did not settle.** W5-c's three-way split (§3.3) sends
`enforce_ceiling` and `recover_unresolved` through
`VolumeOwner.declare_household_level_db` (`volume_owner.py:297`), by analogy
with the ledger's W11 ruling. That analogy was checked against the ledger row,
**not** against a crash-recovery run on hardware. The executor should treat it
as the recommended route and the first thing to disprove.
