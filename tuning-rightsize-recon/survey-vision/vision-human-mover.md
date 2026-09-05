# vision-human-mover — the HUMAN-MOVER loop, traced at HEAD

HEAD = `f4ff89731`. Read-only. Every claim below is `path:line` at that HEAD.
Behaviour marked **[executed]** was confirmed by running the seam under
`<scratchpad>/venv` (no hardware); everything else is static reading.

## Verdict in one line

The human-mover walk is **real, complete, and browser-only**. Nothing about it
is wanting for the *human*. Everything about it is wanting for the *LLM*: the
LLM can DECLARE the walk (`stage`) and OPEN the session (`jasper-round open`),
and then it is blind and mute for the entire walk. There is no toolbox verb
that reads what the human is being asked, no verb that releases a hold, no verb
that closes the set, no verb that re-asks a spot, and no verb that even reports
whether a walk is staged.

---

## 1. What `stage --mover human` writes, and who consumes it

### On disk

One file, last-wins, single-use:

`jasper/active_speaker/angle_capture_spool.py:73-75`
```python
DEFAULT_ANGLE_REQUEST_SPOOL_PATH = Path(
    "/var/lib/jasper/active_speaker_angle_capture_request.json"
)
```

Written atomically at `0o640` with the parent's group
(`angle_capture_spool.py:220-226`). Payload keys
(`angle_capture_spool.py:186-215`): `artifact_schema_version`, `kind`
(`jts_active_speaker_angle_capture_request_staged`), `mover`, `polarity`,
`inverted_role`, `delayed_role`, `delay_us`, `level_matched`, `program`,
`stops[]` (`angle_deg`, `regime`, `elevation_deg`, `candidate_id`, ordered,
position-major), `staged_at`. Ceiling `MAX_STOPS = 24`
(`angle_capture_spool.py:93`); refused while a session is live
(`SESSION_ALREADY_LIVE`, `:104`).

A taken document is moved to `…request.json.consumed` (`CONSUMED_SUFFIX`,
`:79`) **before** validation, so single-use is a property of the take
(`:250-268`).

### Who consumes it — the complete list (grep-verified)

| reader | file:line | what it does |
|---|---|---|
| **the web wizard's session open** | `jasper/web/correction_crossover_v2.py:1996` (`take_staged_angle_request()`), called from `:5830` inside `prepare_v2_session` | THE take. Turns the walk into `lateral_prompts` + `MeasureSpec`s |
| the tier chooser's price line | `jasper/active_speaker/crossover_envelope_v2.py:1865` (`peek_staged_angle_request()`) | browser-only; adds "Plus a staged walk (baseline/express): 5 more spots, 8 more measurements" to the Start button's description (`:1898-1911`) |
| `serve`'s pre-flight | `jasper/active_speaker/arm_walk.py:902-904` | a bare `.is_file()` — does **not** read `mover` |

### Which process runs the walk

**The commissioning web wizard, driven from a browser.** `stage` writes; the
walk is adopted by `POST /correction/crossover/v2/session`
(`jasper/active_speaker/wizard_client.py:33`), which is `prepare_v2_session`
(`jasper/web/correction_crossover_v2.py:5472`).

**`serve` does not accept `--mover human`** — the choice list has exactly one
member:

`jasper/cli/angle_capture.py:797-803`
```python
    parser.add_argument(
        "--mover",
        default=MOVER_TURNTABLE,
        choices=(MOVER_TURNTABLE,),
```
and `_cmd_serve` hard-constructs `arm_walk.TurntableMover(...)`
(`jasper/cli/angle_capture.py:599-606`). There is no `HumanMover` anywhere in
the tree.

The help text the brief flagged is **TRUE and load-bearing**:

`jasper/cli/angle_capture.py:882-892`
```
"  - plan/stage to RUN a walk -- they only DECLARE one; serve\n"
"    (lab arm) or the guided web flow (human mover) is what\n"
"    actually moves the microphone\n"
…
"  - serve, when a human is moving the mic by hand this session\n"
"    -- that is stage --mover human\n"
```
`docs/tuning-operator-runbook.md:350` says it plainly: **"A hand-walked round is
driven from the browser, not a CLI."**

`jasper-round open` does *not* run the walk either — it only POSTs the open
(`jasper/cli/round.py:161-163`) and then has nothing to say until `wait`.

---

## 2. How the human is told where to put the microphone

**Surface: one browser page, `http://<speaker>/sound/crossover/`**
(`jasper/identity.py:143`), and nothing else. No terminal, no voice.

The markup is a three-slot walkthrough panel:
`jasper/web/correction_crossover_flow.py:93-102`
```html
<div id="crossover-walk" class="capture-walk" hidden>
  <p id="crossover-walk-progress" class="eyebrow"></p>
  <p id="crossover-walk-headline" class="section__title"></p>
  <p id="crossover-walk-detail" class="form-hint"></p>
  <div id="crossover-walk-action" class="measurement-row__actions"></div>
```
filled by `renderWalk` (`deploy/assets/correction/js/crossover/main.js:676-716`)
from `capture.position_pending.prompt`, which the position gate lifts verbatim
off the plan entry (`jasper/web/correction_crossover_v2.py:4356-4362`).

The words themselves are generated, never hand-written:
`jasper/active_speaker/crossover_v2/capture_plan.py:471-503` (`remote_position_prompt`)
```python
        bearing = (
            f"Turn the microphone to {degrees_:+d}° "
            f"({abs(degrees_)}° {side} of the design axis)"
        )
        detail = (
            f"Keep it {MARK_DISTANCE_M:g} m from the speaker and pointed at it."
        )
```

**[executed]** the exact eleven screens a `stage --program baseline --size
express --mover human` round shows, built through
`build_v2_capture_plan(plan_shape=express + hand_released_positions=True)`:

| # | kind | `position_deg` | title (what the human reads) | body |
|---|---|---|---|---|
| 1 | check | `0` | Stand the microphone about 1 m in front of the speaker, at tweeter height. | JTS listens to the room exactly as it is first… |
| 2 | measure | `0` | Keep the microphone still — this spot is the mark. | This one is longer, and can be the loudest… |
| 3-6 | lateral | `0` | Leave the microphone on the design axis (0°). | On the mark, 1 m out, pointed at the speaker. |
| 7 | lateral | `-20` | Turn the microphone to -20° (20° LEFT of the design axis). | Keep it 1 m from the speaker and pointed at it. |
| 8 | lateral | `20` | Turn the microphone to +20° (20° RIGHT of the design axis). | Keep it 1 m from the speaker and pointed at it. |
| 9 | lateral | `0` | Keep the microphone on the design axis (0°), and 10° BELOW mark height. | On the mark, 1 m out, pointed at the speaker. |
| 10 | lateral | `0` | Keep the microphone on the design axis (0°), and 10° ABOVE mark height. | On the mark, 1 m out, pointed at the speaker. |
| 11 | entry_baseline | `0` | Back to the design axis (0°) — one last measurement before tuning. | This records how the speaker sounds right now… |

Progress reads `Measurement N of 11` (`capture_plan.py:1292-1298`).

**So: angle — yes, in degrees. Distance — yes, "1 m". Height — only as an
ANGLE ("10° ABOVE mark height"), never as a distance.** That is a regression in
usability relative to the tape-measure copy this replaced, which said
`"Move the microphone 5 in (12 cm) ABOVE mark height."`
(`capture_plan.py:264`, `format_position_distance` at `:126-133`). The angle
copy is selected by `_positioned_prompt` (`capture_plan.py:1301-1313`) whenever
`positions_gated`, and **every** hand-walked round is now gated, because
`prepare_v2_session` unconditionally rebinds the shape:

`jasper/web/correction_crossover_v2.py:5164` / `:5791`
```python
    return dataclasses.replace(plan_shape, hand_released_positions=True)
…
    plan_shape = _hand_released_plan_shape(plan_shape)
```
`positions_gated = externally_positioned or hand_released_positions`
(`capture_plan.py:885-897`). The `--mover` help still promises "string and
protractor" (`jasper/cli/angle_capture.py:708-717`) — accurate, but the
protractor is now mandatory even for the vertical poses, where the human must
convert 10° at 1 m to 17.6 cm themselves.

**No voice cue.** `jasper/cues/registry.py` registers ten slugs (`:48`-`:175`)
and not one concerns measurement or mic placement; `jasper-cues play` takes a
registered slug only (`jasper/cues/cli.py:224-231`) — there is no ad-hoc-text
verb. The only audible signal in the whole walk is the courtesy prelude, and
`COURTESY_PRELUDE_PHASES` is `{check, verify, entry_baseline}`
(`jasper/active_speaker/crossover_v2/programs.py:68-70`) — i.e. captures 1 and
11 beep, and the eight walk stops in between are silent. A human who looks away
from the screen has no channel at all.

---

## 3. How the human signals "mic placed, ready"

**One button on that page, posting one endpoint.**

`jasper/web/correction_crossover_v2.py:4181-4183`
```python
POSITION_READY_ENDPOINT = "/correction/crossover/v2/position-ready"
```

The gate publishes the control with the hold
(`jasper/web/correction_crossover_v2.py:4375-4389`):
```python
                    "action": {
                        "id": "crossover_v2_position_ready",
                        "label": (
                            "Microphone is on the design axis (0°)"
                            if target == 0
                            else f"Microphone is at {target:+d}°"
                        ),
                        "endpoint": POSITION_READY_ENDPOINT,
                        "body": {"index": int(index), "degrees": target},
                    },
```
rendered as a `<button>` at `main.js:698-706` and posted through `runAction`.
The handler is `_handle_crossover_v2_position_ready`
(`jasper/web/correction_setup.py:2969-3007`); route registered at `:354`.
`index` must be a real JSON integer and must match what is pending
(`:2987-3001`) — a crossed retry is refused, not misapplied.

Whether the button appears at all is decided by one server-stated flag, not by
transport (`jasper/web/correction_crossover_v2.py:4370-4374`):
```python
                    "hand_released": (
                        str(screen.get("auto_advance") or "") == AUTO_ADVANCE_TAP
                    ),
```
consumed at `main.js:679`. **[executed]** every entry of a staged human walk
carries `auto_advance: tap` (`angle_capture.py:504-505`), so `hand_released` is
true for all eleven.

While the tone plays, the panel swaps the button for a sentence rather than
leaving a dead control (`main.js:711-716`): *"Recording this spot — keep still
until the tone stops."*

There is **no** keypress, no voice, no HTTP verb in the toolbox — see §7.

---

## 4. What triggers each stimulus, and which door plays it

The release admits a begin that the capture worker had parked. The hold is the
shipped soft-hold, so no page contract changes
(`jasper/web/correction_crossover_v2.py:4205-4213`): the Pi answers
`capture_deferred`, the caller re-posts the identical begin every 1.5 s, and the
attempt budget is not spent.

**The door is the wizard's own in-process capture kernel, not `jasper-measure`.**
`build_v2_wired_run_and_consume`
(`jasper/web/correction_crossover_v2_wired.py:501-590`) runs the walk on a
worker thread; per capture it does *authorize (gate first, then
`conductor.authorize_begin`) → capture-while-play → consume*
(`:537-556`). Two legs play:

* the **flow leg** — the recorder rolls, confirms audio is flowing, then
  `conductor.on_armed` plays the program synchronously through the real DSP
  chain, recorder keeps rolling `WIRED_POST_ROLL_S` after (`:543-550`);
* the **engine leg** — `_bind_engine_measure_leg`
  (`jasper/web/correction_crossover_v2.py:5192-5215`) claims every index in
  `specs_by_index` and plays it through `TuningSession.measure()`. A staged
  walk's candidate stops are exactly those indices
  (`correction_crossover_v2.py:2088-2101`).

Every walk stop replays the MEASURE per-driver object verbatim
(`angle_capture.py:536-559` `program_for_stop`; `capture_plan.py:1521-1522`),
which is what makes cross-angle comparison meaningful.

`jasper-measure` is a **different door** — one placement per run, prompting
nobody (`jasper/cli/measure.py:10-14`). See §7.

---

## 5. A bad take

### The human

Non-terminal rejection auto-retries the **same index at attempt+1**, and a
gated session re-gates rather than settling
(`correction_crossover_v2_wired.py:939-949`; the contract is stated at
`:554-559`). So the human sees the **same prompt again, with a fresh button**,
plus a nudge whose copy comes from the reason registry
(`jasper/active_speaker/crossover_v2/refusal_copy.py:499-600`):

| condition | code | template | what the human reads |
|---|---|---|---|
| clipped | `clipped` (`refusal_copy.py:559-568`) | `silent_auto_retry` | banner only: *"That was a touch loud — measuring again a bit quieter."* screen does not change (`crossover_envelope_v2.py:2375-2383`) |
| SNR floor | `snr_floor` (`:533-539`) | `fix_and_retry` | *"The room is too loud right now, or the microphone is too far away. Quiet the room or move the microphone closer, then try again."* |
| pilot collapse | `pilot_level_collapse` (`:523-532`) | `fix_and_retry` | *"The test tones didn't rise clearly above the room…"* |
| glitch | `drift_baselines_disagree` (`:569-578`) | `silent_auto_retry` | *"The capture glitched — measuring again."* |
| wiring | `channel_map_mismatch` (`:540-548`) | `hard_stop` | session ends, "Back to speaker setup" |

The `fix_and_retry` screen's own primary is suppressed while a capture is in
flight (`main.js:790-792`), so mid-walk the human keeps the walkthrough and the
nudge and simply presses the release again. That is correct behaviour.

**"Wrong position" is not detectable.** Nothing verifies the microphone is where
it was asked to be; the release is an attestation, exactly like the arm's
(`correction_crossover_v2.py:4192-4196` — "A prompted pose is a promise that the
microphone has arrived").

**Two gate deaths kill the session** (`correction_crossover_v2.py:4297-4335`):
per-hold `REMOTE_POSITION_HOLD_BUDGET_S = 600.0` (`:4154`) → *"Nothing reported
the microphone in place, so the measurement stopped waiting."*; and the
session's own wall-clock ceiling → *"The measurement ran out of time before the
microphone reached every position."* **[executed]** for this 11-entry walk the
ceiling is `1800 + 8×120 = 2760 s = 46 min` (`capture_plan.py:1847-1862`,
`session_volume_plan.py:88`), which `walk_price` prints as `ceiling_min: 46`.

### The LLM

Nothing, except one **false signal**. A rejected capture persists a `failure`
block (`correction_crossover_v2_wired.py:797-802` →
`durable_state.py:1268-1304`, `"failure": {...} if failure_code else None`),
which the status envelope republishes (`correction_crossover_v2_status.py:152`)
and which `jasper-round wait` reads as a dead session:

`jasper/active_speaker/wizard_client.py:322-327`
```python
        failure = block.get("failure")
        if failure:
            return {"status": "failed", "reason": REASON_SESSION_FAILED,
```
The key is cleared on the next *accepted* capture (`failure_code=None`), so this
is a genuine poll race: **`jasper-round wait` reports `session_failed` and exits
1 on any retriable mid-walk rejection it happens to poll during**, while the
walk carries on and finishes fine. Default poll is 5 s
(`jasper/cli/round.py:66`).

Via `jasper-measure` the story is different and cleaner but shallower: the door
"does not grade" (`jasper/cli/measure.py:11-12`) and its per-stimulus
`incident` field carries only mechanical codes — `program_admission_refused`,
`program_play_failed`, `stimulus_emission_failed`, `stimulus_not_captured`,
`session_level_not_ready`
(`jasper/active_speaker/crossover_v2/program_transaction.py:54-82`). **A clipped
or SNR-starved take through `jasper-measure` is banked with an empty
`incident`.** No acoustic verdict is offered at that door at all.

---

## 6. Artifacts and what the LLM actually gets on stdout

### Per position

One record per accepted take, banked fail-soft into the session bundle under
`/var/lib/jasper/active_speaker/sessions/<bundle>/…/artifacts/<record_id>`
(`jasper/active_speaker/bundles.py:71`,
`correction_crossover_v2.py:2758-2781`). Built by
`spatial.lateral_pose_record` (`jasper/active_speaker/crossover_v2/spatial.py:1052-1114`):
`pose_id`, `take_id`, `index`, `attempt`, `session_id`, `wav_sha256`,
`graph_fingerprint`, **`prompt`** (the sentence the human read), `role`,
`position_deg`, `position_axis`, **`vertical_deg`**, `offset_cm`, `at_mark`,
`regime`, `lateral_consumer`, `curves[]`. Elevation IS preserved here
(`crossover_v2_flow.py:2803-2806`) even though the gate flattened it to 0.

Journal lines the LLM could grep but no verb surfaces:
`correction.crossover_v2_position_pending` (`correction_crossover_v2.py:4392-4399`),
`correction.crossover_v2_lateral_pose` (`crossover_v2_flow.py:2772-2779`),
`correction.crossover_v2_angle_walk_taken` (`correction_crossover_v2.py:2121-2141`).

### After the walk

Nothing lands until the human presses **"Save this measurement"**
(`crossover_envelope_v2.py:1002-1008`, POSTing `/correction/crossover/v2/complete`,
handler at `correction_setup.py:3009-3030`), which closes the held group and
runs the fit. Then `jasper-round bank <session_dir>` files the round
(`jasper/cli/round.py:238-273`) and `position_cycle.json` is derived from the
banked takes (`jasper/active_speaker/crossover_v2/position_cycle.py:32-36`).

### stdout/stderr sizes and keys

| verb | stdout | keys |
|---|---|---|
| `jasper-angle-capture stage --json` | ~1 stop-block per stop; 8 stops ≈ 3-4 KB | `ok`, `staged_at_path`, `program`, `candidates`, `price{mic_moves,captures,ceiling_min}`, `level{...}`, `handoff_url`, `mover`, `externally_positioned`, polarity/delay/level_matched, `stops[]{index,angle_deg,elevation_deg,regime,program_phase,prompt,screen}`, `announced_indexes` (`jasper/cli/angle_capture.py:325-372`) |
| `jasper-round open --json` | ~10 lines | `verb,status,stage,tier,path,http,session_id,phase,reason,detail` (`round.py:163-186`) |
| `jasper-round wait --json` | ~9 lines | `verb,status,reason,phase,session_id,candidate_fingerprint,failure,waited_s` (`round.py:189-206`) |
| `jasper-measure` | small; one entry per spec | `status,session_id,bundle_dir,record_ids[],specs[]{candidate_id,kind,graph_fingerprint,record_ids,stimuli[{position_deg,stimulus_dbfs,level_db,record_id,incident}],stubs[]}` (`jasper/cli/measure.py:860-882`, `:823-857`) |
| `jasper-crossover-prescriber status --json` | a report | `banked.walk{available,n_takes,angles_deg,elevations_deg}` (`jasper/cli/crossover_prescriber.py:749-770`) — post-hoc, needs a session dir |

Note what is **absent from every row**: `position_pending`. No verb prints it.

---

## 7. THE KEY QUESTION — can the LLM drive this loop over SSH?

**No. Verdict: FALSE, and by explicit design.**
`docs/tuning-operator-runbook.md:350-358`: *"A hand-walked round is driven from
the browser, not a CLI… Nothing else is needed: no `jasper-angle-capture serve`,
no CSRF dance, no second device."*

### The actual round-trip today

```
LLM  ssh> jasper-angle-capture stage --program baseline --size express --mover human
LLM  ssh> jasper-round open --tier express --hostname jts3.local
LLM  chat> "open http://jts3.local/sound/crossover/ and follow it"
     ── 46 minutes of blindness; 11 human taps the LLM neither sees nor causes ──
LLM  ssh> jasper-round wait --timeout-s 3000     # may spuriously return "failed" (§5)
LLM  ssh> jasper-round apply --expected-fingerprint …
LLM  ssh> jasper-round bank <session_dir>
```

The LLM cannot say "go to position 3", cannot know position 3 is the live one,
cannot wait for ready, cannot trigger, cannot see the take.

### Where exactly it is wanting

1. **No read verb for the live hold.** `capture.position_pending` — which
   already carries everything the LLM would need (`index`, `attempt`,
   `degrees`, `role`, `prompt{progress,title,body}`, `hand_released`, `action`)
   — has exactly one on-box reader, and it is the arm loop:
   `jasper/active_speaker/arm_walk.py:853-869` (`pending_from_capture`). No CLI
   in `pyproject.toml:[project.scripts]` GETs `/correction/crossover/status`.
2. **No release verb.** `POST /crossover/v2/position-ready` is reachable only
   from a browser or from `arm_walk.LoopbackSession`
   (`arm_walk.py:389`, `:285-292`). `jasper-round` has four verbs — `open`,
   `wait`, `apply`, `bank` (`jasper/cli/round.py:353-431`) — and none of them
   is it. Nor `/complete`, nor `/retake`, so the LLM cannot close the held set
   or re-ask a bad spot either.
3. **No "what is staged" verb.** `jasper-angle-capture`'s own help sends the
   reader to the wrong tool:
   `jasper/cli/angle_capture.py:886-888` — *"stage, when a walk is already
   staged -- check first with jasper-crossover-prescriber status"* — but that
   command never touches the angle spool; its `staged` section reads the
   **prescription** spool (`jasper/cli/crossover_prescriber.py:775-785`,
   `path = str(prescription_spool_path())`). `peek_staged_angle_request` is
   reachable only from the browser's tier chooser
   (`crossover_envelope_v2.py:1865`). **STALE POINTER / real gap.**
4. **The escape hatch confirms the diagnosis.** `jasper-measure` exists for
   exactly this shape — one placement per run — and it has a flag whose only
   purpose is to record what the LLM said out-of-band:
   `jasper/cli/measure.py:1058-1063`
   ```python
       parser.add_argument(
           "--prompt",
           action="append",
           help="what the mover was told, for the bearing this run measures",
       )
   ```
   The toolbox already assumes the LLM talks to the human in chat and then
   transcribes it. That is the "talk out-of-band" the vision names as wanting.
   (Its own epilog is stale: `jasper/cli/measure.py:1023-1024` claims
   *"scripts/run-crossover-round.py already calls this per pose"* — that script
   never invokes `jasper-measure`; it drives `jasper-angle-capture stage/serve`
   plus wizard endpoints.)
5. **The laptop runner is arm-only.** `scripts/run-crossover-round.py:511`
   hard-codes `stage --mover arm`, and `:1411-1418` refuses `--angles` without
   `--attest-rig-clear` because *"the walk it would stage is an ARM walk that
   nothing would serve."*
6. **The transport exists; only the door is missing.**
   `jasper.active_speaker.wizard_client.WizardClient` already owns the Host
   header + double-submit CSRF (`wizard_client.py:81-93`, `:145-155`) and has
   generic `get_json`/`post_json` (`:157-165`). Anything below is a thin
   argparse wrapper over methods that already ship — no new machinery.

### Contrast: the arm mover is complete

`stage --mover arm` → `serve --mover turntable` → `ArmWalk.run()` polls,
preflights power, moves, settles, POSTs release, parks on every exit
(`arm_walk.py:16-41`), with a machine-readable exit-code table
(`arm_walk.py:117-140`), a `summary()` line on stderr (`:844-850`) and an
optional `--trail` JSONL of one object per event
(`jasper/cli/angle_capture.py:868-871`). The human mover has no analogue of any
of those four.

**One latent hazard from the asymmetry:** `serve` checks only that *a* walk is
staged (`arm_walk.py:902-904`) and `pending_from_capture` ignores
`hand_released` (`:853-869`). A `serve` process left running beside a
hand-walked round would move the turntable and release holds meant for a
person. Lab-only, opt-in, foreground — but the reciprocal check the browser
makes (`main.js:679`) does not exist on the arm side.

---

## 8. What could NOT be determined statically (needs a hardware run)

1. Whether the walkthrough panel actually renders and the release button
   actually posts on a live wired hand-released round. Every seam checks out and
   `[executed]` confirms the plan entries, but no test in-tree exercises
   browser → `position-ready` → admitted begin end to end.
2. Whether an 11-stop human walk finishes inside the 46-minute session ceiling
   at real household pace (≈4 min/spot including the 25 s tone). The margin is
   thin and un-instrumented.
3. Whether the four consecutive identical `Leave the microphone on the design
   axis (0°).` screens (the `ANCHOR_REPEATS = 4` at
   `jasper/active_speaker/measurement_programs.py:35`) read to a human as a bug.
4. Whether the degree-only vertical copy ("10° ABOVE mark height") is
   actionable without a stated cm.
5. How often a mid-walk rejection actually fires — i.e. how badly the
   `jasper-round wait` false-`failed` race bites in practice.
6. Whether `position-ready` survives the deployed nginx front end as the browser
   sends it (I read the Python host/CSRF guards, not `deploy/nginx/*`).

---

## Gaps and the smallest honest change for each

| # | gap | file:line | smallest honest change | size |
|---|---|---|---|---|
| 1 | LLM cannot read the live hold | `arm_walk.py:853-869` is the only reader | add `jasper-round pending [--json]` — one `client.get_json(STATUS_PATH)`, print `capture.position_pending` verbatim | **tiny** |
| 2 | LLM cannot release a hold | `round.py:353-431` has no such verb | add `jasper-round position-ready --index N`, one `client.post_json(POSITION_READY_PATH, {"index": n})` | **tiny** |
| 3 | LLM cannot close the set or re-ask a spot | `correction_setup.py:3009`, `:3033` unreachable from any CLI | add `jasper-round complete` and `jasper-round retake` (empty bodies) beside #2 | **tiny** |
| 4 | `jasper-angle-capture` points at a tool that cannot answer "is a walk staged" | `jasper/cli/angle_capture.py:886-888` vs `crossover_prescriber.py:775-785` | add `jasper-angle-capture show [--json]` over the existing `peek_staged_angle_request`, and fix the two epilog lines to name it | **small** |
| 5 | `jasper-round wait` reports `session_failed` on a retriable mid-walk rejection | `wizard_client.py:322-327` | require the phase to also be terminal (or `failure.code` to be a non-retriable row of `REASON_REGISTRY`) before returning `failed` | **small** |
| 6 | Vertical poses are stated to the human only in degrees | `capture_plan.py:495-503` | append the cm to the elevation clause via the existing `format_position_distance` (`capture_plan.py:126-133`) | **tiny** |
| 7 | Release button and gate `degrees` flatten every vertical pose to `0` | `correction_crossover_v2.py:4384-4386`, `_entry_policy` at `capture_plan.py:1344-1366` | carry `position_vertical_deg` in `screen`/`pending` and suffix the label; the record already banks it (`crossover_v2_flow.py:2805`) | **small** |
| 8 | `jasper-measure` epilog names a caller that does not exist | `jasper/cli/measure.py:1023-1024` | delete the two lines | **tiny** |
| 9 | `serve` would drive the arm into a hand-walked round | `arm_walk.py:902-904`, `:853-869` | make `pending_from_capture` return `None` when `hand_released` is true | **tiny** |
| 10 | The human's only channel is a screen they must watch; the 8 walk stops are silent | `programs.py:68-70`, `cues/registry.py` | none honest without a product decision — a per-stop cue is new audible behaviour the owner has not asked for. Record as a question, not a change. | n/a |

Gaps 1-3 are one PR: four thin argparse verbs over `WizardClient` methods that
already ship. That single PR is what turns "the LLM must talk out-of-band" into
"the LLM drives the loop and the human just walks."
