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

<!-- BEGIN READY-TO-LAND ADR — copy verbatim into docs/adr/NNNN-….md -->

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
ships in twenty-six places, whose reference form is
`jasper/web/correction_crossover_v2_wired.py:822-838`: start the cleanup as a
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

<!-- END READY-TO-LAND ADR -->

---

**One rider, not part of the ADR text.** `crossover-v2-engine-design.md:81-89`
describes the mismatch in the pre-decision tense and names **two** async
implementations where there are three (its count of *"four seams"* is correct
within its scope — the fifth is declared in `playback_transaction.py`). W4-a
should re-tense that paragraph and point it at the new ADR in the same PR, the
way W5-d re-tenses `:252-268`. See §6.

## 2. The wiring map

## 3. W7's dissolution

## 4. PR slicing, verification bars, tiers

## 5. Content-greps for the post-merge check

## 6. Disagreements with the plan, recorded
