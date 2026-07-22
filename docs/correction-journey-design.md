# Correction journey — the three-step calibration spine

> **Status: design record, not yet implemented (2026-07-19).** This doc
> specifies the guided three-step calibration journey (1 Crossover →
> 2 Room → 3 Bass) layered over the existing `/correction/` tabs. It was
> written against `origin/main` `748f8d7a8` — every file/function named
> here was verified to exist at that commit. The implementing PR should
> follow the checklist in §8 and update this banner to point at the
> shipped code. Related canon:
> [HANDOFF-crossover-measurement-v2.md](HANDOFF-crossover-measurement-v2.md)
> (step 1), [HANDOFF-correction.md](HANDOFF-correction.md) (step 2),
> [HANDOFF-bass-extension-plan.md](HANDOFF-bass-extension-plan.md) +
> [bass-extension-waves/](bass-extension-waves/README.md) (step 3).

## 1. Product intent

A household calibrates a JTS speaker in a fixed physical order:

1. **Crossover** — commission the active speaker itself (the v2
   conductor flow: CHECK → MEASURE → REVIEW/APPLY → VERIFY). Makes the
   drivers coherent. Passive speakers skip this step entirely.
2. **Room** — measure and apply room correction at the listening
   position. Already **gated on step 1** by the shipped Active-to-Room
   eligibility receipt.
3. **Bass** — commission the volume-scheduled low-frequency alignment
   (bass-extension Waves 4–6, not yet shipped). Will be gated on steps
   1–2 when it lands.

Today the three tabs exist (`room`, `crossover`, `bass` in
`SECTIONS` in [`jasper/web/correction_hub.py`](../jasper/web/correction_hub.py))
but nothing tells the user which one to do, in what order, or what's
already done. The journey adds exactly that: **one glanceable strip on
all three tab pages** showing per-step state and a single "next" pointer.

## 2. Non-goals (as binding as the goals)

- **No shared wizard/session framework.**
  [`room-correction-information-design.md`](room-correction-information-design.md)
  explicitly lists "a generic tab, session, envelope, graph, or wizard
  framework" as a non-goal. The room, crossover, and bass flows keep
  their own envelopes, sessions, and state machines. The journey is a
  **read-only aggregator** over facts those flows already own.
- **The journey never mutates anything.** No POST endpoint, no state
  file, no writes. It computes and displays.
- **No reordering or restyling of the existing tabs.** `section_tabs`
  and the `SECTIONS` tuple stay as they are; the strip is additive.
- **No per-flow behavior changes.** Do not touch the room session, the
  v2 conductor, the eligibility gate, or the bass classifier beyond
  *calling* them.
- **No bass Wave 4/5/6 work.** Step 3 renders "not yet available" until
  those waves ship. Do not stub `/bassext/*` endpoints.
- **No new "done" definitions.** Every step-state predicate below reuses
  a fact an existing subsystem already owns (§4). If a predicate seems
  to need a new fact, stop and re-read §4 — deriving a parallel version
  of an existing decision is the SSOT violation this repo's review gates
  reject most often.

## 3. Architecture

One new pure module, one new GET endpoint, one shared ES module:

```
jasper/web/correction_journey.py      # pure aggregation (new)
    build_correction_journey(steps: JourneyInputs) -> dict   # pure
    read_journey_inputs(...) -> JourneyInputs                # the only I/O

jasper/web/correction_setup.py        # + route "/journey" (GET, JSON)

deploy/assets/correction/js/shared/journey-strip.js   # renders the strip
```

- **Pure core.** `build_correction_journey` takes plain data (the three
  sub-read results, each already reduced to a small dict) and returns
  the JSON payload. All state-machine logic (§5) lives here so the test
  matrix (§7) needs no mocks of Camilla/session machinery.
- **One I/O shim.** `read_journey_inputs` performs the three sub-reads
  (§4), each individually fail-soft (§6). It is the only place the
  journey touches other subsystems.
- **Client-side strip.** Each tab page already fetches its own status
  JSON from its own module; the strip follows the same pattern: the
  server renders an empty `<div id="journey-strip" hidden></div>`
  immediately after the `section_tabs` nav on each of the three pages,
  and each page's ES module imports
  `/assets/correction/js/shared/journey-strip.js` and calls
  `mountJourneyStrip(el, { activeStep })`, which fetches
  `GET /correction/journey` and renders. A shared module is justified
  under the promotion rule (used by all three pages). Do **not** inline
  any JS; do not hand-roll HTML with untrusted strings (all strip copy
  is server/module-owned constants, but still build DOM via the shared
  `h()` from `/assets/shared/js/dom.js`).

Why client-fetch instead of server-rendering the strip into each page:
`correction_crossover_flow.render_page` and
`correction_bass_flow.render_page` are synchronous string builders,
while the sub-reads need the async Camilla socket. A fetch matches the
existing per-tab status pattern and avoids changing three render
signatures.

## 4. The three sub-reads — exact sources of truth

Each sub-read reduces to a small plain dict. **Reuse these functions;
do not re-derive their logic.**

### Step 1 — Crossover

- **Primary fact (gate-grade):** the same decision Room already
  consumes. Call `_room_readiness()` in
  [`jasper/web/correction_setup.py`](../jasper/web/correction_setup.py)
  (it wraps `_read_room_correction_readiness` →
  `jasper.active_speaker.setup_status.read_active_speaker_setup_status`
  over the live CamillaDSP graph, normalized by
  `_normalize_room_readiness`). The returned `_RoomReadiness` gives:
  - `allowed` (bool) — crossover/setup is complete enough for Room;
  - the authority vocabulary — in particular the passive shape
    (`active=False`, status `not_required`) vs the active shapes
    (manual applied profile / automatic commissioning receipt).
  Using this **exact** read keeps step 1's "done" bit-identical to the
  gate that unlocks step 2 — one decision, two consumers.
- **Display enrichment (not gate-grade):** `crossover_v2_status_block()`
  in [`jasper/web/correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py)
  — its `applied` bool and screen let the strip say "measured & applied"
  vs "applied from manual setup". If this read fails, step 1 still
  resolves from the primary fact alone.

### Step 2 — Room

- **Fact:** does the currently loaded CamillaDSP config carry a room
  correction? Reuse `_current_config_presentation(sess)` in
  `correction_setup.py` (wraps
  `jasper.correction.status.describe_current_config` +
  `current_correction_presentation`). The `current_config` dict's
  `current_correction` field is the SSOT the room tab itself displays:
  - a correction descriptor → step 2 **done**;
  - `None` **while Camilla answered** → no correction → **todo**;
  - Camilla unreachable (the read raises / path is None because the
    websocket call failed) → **unknown**, *not* todo. The implementer
    must distinguish "Camilla said there is no correction" from "we
    could not ask" — check how `describe_current_config` represents the
    two cases before coding, and thread a tri-state out of the shim.
- **Blocked:** if step 1's `allowed` is False (active speaker, setup
  incomplete), step 2 is **blocked** regardless of the above.

### Step 3 — Bass

- **Today (until bass Waves 4–6 ship):** state is always
  `unavailable`, with display enrichment from
  `_classify_live_bass_extension_graph(cam)` in `correction_setup.py`
  (Wave 3's read-only classifier): if it reports an applied/bypassed
  bass-extension graph or a retained profile summary
  (`bass_extension_profile_summary` in `graph.details`), the strip may
  show that detail line; otherwise "Not yet available."
- **Future (Wave 6):** switch the sub-read to `GET /bassext/state`'s
  `available_actions` per
  [`bass-extension-waves/wave-6-ui.md`](bass-extension-waves/wave-6-ui.md),
  and gate entry on an eligibility receipt shaped like the
  Active-to-Room `CommissioningEligibilityReceipt` (one receipt
  vocabulary, three consumers — see §9). **Do not build any of this
  now.**

## 5. The step state machine

Per-step states (closed vocabulary — the JSON `state` field):

| State | Meaning | Chip |
|---|---|---|
| `not_required` | Passive topology; this step doesn't apply | — (dimmed dash) |
| `done` | The step's owned fact says complete | ✓ |
| `todo` | Actionable now, not complete | ○ (→ when it's the pointer) |
| `blocked` | Prerequisite step unmet | 🔒 (copy names the upstream step) |
| `unavailable` | Feature not shipped (bass today) | — with "coming" copy |
| `unknown` | Sub-read failed (fail-soft) | ? |

Derivation, in order (pure logic in `build_correction_journey`):

1. **Step 1** from the readiness read: passive shape → `not_required`;
   `allowed=True` (active) → `done`; `allowed=False` → `todo`;
   read failed → `unknown`.
2. **Step 2**: step 1 `todo`/`unknown` (active topology) → `blocked`;
   else from the correction fact: descriptor → `done`; answered-none →
   `todo`; can't-ask → `unknown`.
3. **Step 3**: always `unavailable` in v1 (with optional detail line).
4. **The pointer** (`next` field): the first step in order
   (crossover, room, bass) whose state is `todo`. `blocked` and
   `unavailable` steps are never the pointer — the pointer lands on the
   *cause*, not the casualty. If no step is `todo`: `next = null` and
   the strip shows the all-done line (or, if anything is `unknown`, a
   neutral "state unavailable" line instead of a celebration).

Passive-speaker walk-through (sanity check for the implementer): step 1
`not_required`, step 2 `todo` → pointer = room. Active fresh install:
step 1 `todo` (pointer), step 2 `blocked`, step 3 `unavailable`. After
crossover + room: `done / done / unavailable`, pointer null, all-done
line reads "Calibrated — bass extension coming soon."

**Stale detection is deliberately out of v1.** The room flow already
re-validates its accepted crossover identity at the DSP-writer boundary
(`_assert_room_authority_current`), and the bass profile schema carries
its own staleness vocabulary. Surfacing "step 2 stale because step 1
changed" on the strip is the natural v2 of this feature — do it by
*reading* those existing checks, and only once a cheap read exists.
Do not invent a fingerprint comparison inside the journey.

## 6. Failure behavior (fail-soft, like `/state`)

Each of the three sub-reads is wrapped individually in
`read_journey_inputs`; a raise → that step's input is the sentinel
"unknown" value and a WARN log (`jasper.log_event`, one stable event
name per sub-read, e.g. `event=correction.journey_read_failed
step=crossover`). The endpoint itself must never 500 because a daemon
was restarting, and the strip must never block or delay the tab page —
`mountJourneyStrip` fetches after page render, keeps the `hidden`
attribute on any fetch/parse failure, and never retries in a loop
(one fetch per page load).

## 7. Endpoint + payload

`GET /correction/journey` (add `"/journey"` to the recognized-GET path
allowlist in `Handler.do_GET` in `correction_setup.py`, **before**
`guard_read_request()` per the web conventions — unknown paths 404
without revealing guard state). Response:

```json
{
  "schema_version": 1,
  "steps": [
    {"id": "crossover", "label": "Crossover", "state": "done",
     "detail": "Measured & applied", "href": "/correction/crossover/"},
    {"id": "room", "label": "Room", "state": "todo",
     "detail": null, "href": "/correction/room/"},
    {"id": "bass", "label": "Bass", "state": "unavailable",
     "detail": "Coming soon", "href": "/correction/bass/"}
  ],
  "next": "room"
}
```

`steps` is always length 3, always in journey order. `label`/`href`
come from the hub's `SECTIONS` tuple (import it — don't duplicate the
strings). Copy strings (the `detail` values and strip captions) live as
constants in `correction_journey.py`, one place.

Strip copy (initial set — keep this terse, user-centric voice):

| Situation | Strip line |
|---|---|
| pointer = crossover | "Step 1 of 3 — set up your speaker's crossover" |
| pointer = room | "Step 2 of 3 — correct for your room" |
| step blocked | "Finish Crossover first" (on the blocked chip's title) |
| all done | "Calibrated — bass extension coming soon" |
| any unknown | "Some status unavailable — showing what we know" |

## 8. Implementation checklist (in order)

1. `jasper/web/correction_journey.py`: constants, `JourneyInputs`
   (plain dataclass), pure `build_correction_journey`, async
   `read_journey_inputs` reusing the §4 functions. No new env keys, no
   new state files.
2. Unit tests `tests/test_correction_journey.py`: the full §5 matrix as
   table-driven cases over the pure function (passive, active-fresh,
   active-crossover-done-room-todo, all-done, each sub-read unknown,
   pointer-skips-blocked, pointer-skips-unavailable). Plus one endpoint
   test asserting route-before-guard ordering and the fail-soft 200.
3. Route `"/journey"` in `Handler.do_GET` + handler calling the shim +
   `send_json_response`.
4. `deploy/assets/correction/js/shared/journey-strip.js` using `h()`
   from `/assets/shared/js/dom.js`; import + mount call in each of the
   three page modules; the `<div id="journey-strip" hidden>` emitted
   next to `section_tabs` in the three render paths (room:
   the `__TABS__` replacement site in `correction_setup.py`; crossover:
   `correction_crossover_flow.render_page`; bass:
   `correction_bass_flow.render_page`).
5. Strip CSS: page-scoped is impossible (three pages) — follow the
   promotion rule and check where the hub's shared correction styling
   ships today; put the `.journey-strip` block alongside it, not in
   `app.css` unless that's where the correction chrome already lives.
   Match the canonical design system (no focus rings, `--tone` for
   status colour).
6. Conventions: `scripts/check-js-syntax.sh`, the wizard-conventions
   test (`tests/test_web_wizard_conventions.py`) must stay green — no
   inline JS, no hand-rolled JSON islands, no native dialogs.
7. `docs/doc-map.toml`: this doc is already listed in the
   room-correction mapping (added by the doc PR); extend the
   `requires_docs_when` line if the journey vocabulary changes.
8. Verify at the user's surface: `curl -s http://jts3.local/correction/journey`
   through nginx (not just unit tests), then load all three tabs in a
   browser and screenshot the strip in at least the active-fresh and
   all-done states.
9. `scripts/test-fast`, then PR through the standard adversarial-review
   gate (0 blockers / 0 should-fixes) before merge.

## 9. Future extensions (recorded, not scoped)

- **Bass entry receipt (with Wave 4).** When the bass commissioning
  backend lands, its entry gate should consume a receipt shaped like the
  Active-to-Room `CommissioningEligibilityReceipt` — requiring crossover
  applied *and* room settled — rather than a bespoke check. This also
  addresses the bass plan's open room-EQ-boost-stacking risk: bass
  commissions against the room-corrected state and re-checks when it
  changes. One receipt shape, three consumers.
- **Stale propagation (§5 note).** Strip-level "step N stale because
  step M changed," read from the existing authority-binding and
  profile-staleness machinery.
- **Journey pointer on the landing page.** Once the strip proves out,
  the same `/correction/journey` read could drive a one-line "next
  calibration step" hint on `jts.local`'s landing page. Read-only, same
  endpoint, zero new state.

Last verified: 2026-07-19
