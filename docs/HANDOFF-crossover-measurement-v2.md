# Handoff: crossover measurement v2 — the commission session

> **This file is the live spine**: what v2 is, how to run it, the shape it has
> today, the contracts that must survive a refactor, and where to look when it
> breaks. The campaign narrative — dated bench results and the decision
> archaeology, including the failure taxonomy and the W6 bug-class catalog the
> Debugging section delegates its deeper catalogs to — moved to
> [historical/crossover-measurement-v2-campaign-record.md](historical/crossover-measurement-v2-campaign-record.md).
> Its specific facts (env defaults, thresholds, "what's working" lists, class
> names that no longer exist) are deliberately not kept in sync with the code.
> Read this file for "what does this do"; read the campaign record for "why is
> it like this."

## What it is

v2 measures a fully-active 2-way crossover's **level, delay, and polarity**
from a **guided spatial cloud** — the household walks a phone microphone
through a handful of prompted positions around one mark — then proposes a
correction, applies it on an explicit tap, and measures again to grade what
changed — repeating that apply-and-re-measure round, up to three times, while
the result is still getting flatter (#2602).

- **Two tiers, chosen every session** on the `/correction/` wizard. Both run
  the same stage 1 and differ in **stage 2 only** — Full takes the longer
  stage-2 cloud, Express the shorter one. The capture counts are not written
  here: `tier_display_info()` derives them from the plans themselves and is
  what the household-facing chooser reads (`TIER_FULL` / `TIER_EXPRESS` /
  `DEFAULT_TIER` / `tier_display_info`, in
  [`crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py)).
  This doc describes Full unless it says otherwise.
- **A third tier, `TIER_REMOTE`, is API-only and experimental.** It is Full's
  own shape and counts — Full's walk driven by an external mic
  positioner instead of by hand, stating every pose as an ANGLE. It drops
  nothing: the post-apply pose set has been vertical-free by construction
  since the 2026-08-24 geometry ruling, so there is no pose a positioner
  cannot reach left in it. The chooser never offers it — consenting to it
  means owning a
  positioner the wizard cannot see — so it is reached only by
  `POST /correction/crossover/v2/session {"tier": "remote"}`. See
  "The remote tier" below.
- **It is the only flow.** The legacy per-driver near-field procedure and its
  `JASPER_CROSSOVER_FLOW` selector are gone, and so is the
  `build_crossover_envelope` shim that used to forward to v2: callers reach
  `build_crossover_envelope_v2` directly, or through
  `crossover_envelope.build_crossover_envelope_logged` (that call plus a serve
  log).
- **Nothing applies inside a capture session.** A session produces a proposal;
  the household applies it from the `review` screen.

This is the canonical operational doc for v2. The design record — why it
exists, what was rejected, the wave plan — is
[`crossover-measurement-productization-design.md`](crossover-measurement-productization-design.md).

## How to run it

**Household surface:** `http://jts.local/correction/` → the crossover step.
Screens are `speaker_setup → microphone_check → measure → apply → verify`.
Place the mic ~1 m in front of the speaker at tweeter height, pick a tier on
`microphone_check`, tap Start, follow the phone. When measurement ends, return
to jts.local and choose Apply explicitly.

**Three independent releases ship in a fixed order.** The phone page and the
relay Worker both go out **before** the Pi, because each must be able to
accept what the Pi will emit:

1. **Phone capture page** — [`capture-page/`](../capture-page/README.md), a
   Cloudflare Pages app at `capture.jasper.tech`. Deploy from the repo root:
   `npx wrangler pages deploy capture-page/dist --project-name jts-capture-page --branch=main`.
   `--branch=main` is load-bearing — without it wrangler publishes a preview
   alias and the production domain keeps serving the stale page. The custom
   domain lags the deploy by ~5 min; verify it before moving on.
2. **Relay Worker** — [`relay/`](../relay/README.md) at `relay.jasper.tech`.
   `cd relay && npx wrangler deploy`, then confirm the public artifact:
   `curl -fsS https://relay.jasper.tech/capabilities` and check
   `max_capture_plan_attempts` is at least what the Pi build will emit. The
   Worker's blob-index space *is* the capture-plan attempt ceiling.
3. **The Pi** — `bash scripts/deploy-to-pi.sh`.

The Pi reads `/capabilities` at session setup and refuses before registering
rather than dying on the ninth capture
(`event=capture_relay.plan_capacity_refused`). Both READMEs own the full
ordering rule, including the removal direction (page last).

**Commissioning is memory-privileged.** A MEASURE-accept analysis peaks around
400–430 MB and its co-residency headroom on a 1 GB Pi has never been budgeted
— see "Boundaries" below before assuming it fits.

## Architecture — four parties, one direction of authority

```
phone (dumb recorder)  →  relay  →  Pi session owner  →  pure decision organs
                                          ↓
                                   pure analysis
```

**Phone = dumb recorder.** Per phase it records a known-length window and
uploads one encrypted WAV. No live phone↔Pi feedback mid-capture and no
per-repeat gestures: it reads the next capture's plan entry (duration +
prompt) from the relay session and posts a WAV back.

**Pi = the session owner.** `CrossoverV2Session` in
[`crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py) holds
one session's mutable state, the injected seams, the locks, and the acts that
cannot be undone or repeated (play, publish, apply, commit, journal). It is
also the **adapter** for its one caller, the web host — which is why a
one-line `return self._x` accessor there is a contract rather than
scaffolding. Hand `authorize_begin` / `on_armed` / `consume_capture` to
`run_capture_plan` ([`capture_relay/session.py`](../jasper/capture_relay/session.py))
to drive a session; `snapshot` / `hydrate` carry phase persistence.

**The decisions are not there.** Every verdict rule, admission policy, prior,
program composition, fit, sweep, spatial close and grade lives in
[`jasper/active_speaker/crossover_v2/`](../jasper/active_speaker/crossover_v2/__init__.py)
— one module per organ, each pure and separately testable. The session reads
its state, calls an organ, and records what came back.

**The direction is the invariant: the session imports the package; the package
never imports the session or the web host.** `test_no_domain_module_imports_
the_host_or_the_legacy_flow` in
[`test_crossover_v2_journey.py`](../tests/test_crossover_v2_journey.py) holds
that line. When a decision starts being made in the session file it belongs in
an organ; when session state or a seam starts being read in an organ it
belongs in the session file.

**Analysis = pure functions.** `analyze_program_capture` in
[`program_analysis.py`](../jasper/audio_measurement/program_analysis.py) maps
`(ExcitationProgram, WAV, cal, geometry, priors) → ProgramAnalysis` with no
hidden state, so every verdict is reproducible offline from the stored
artifacts.

**All side effects cross one boundary.** `V2FlowSeams` carries six required
seams (`play`, `analyze`, `publish_check`, `publish_candidate`,
`apply_complete`, `apply_failed`) plus optional ones a session can run
without. The web host
([`correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py))
binds the real ones; tests inject fakes.

Two names still say *conductor* on purpose: `V2ConductorSnapshot` (the
session's persisted form) and `V2ConductorContext` (the host's resolved
construction context), with `resolve_conductor_context` and
`persist_conductor_state` beside them. They are persistence- and
host-adjacent, and renaming them would rewrite a durable shape for cosmetics.
The class they were named after — `CrossoverV2Conductor` — was dissolved in
#2291 Phase 5c-iv; what survived was a session owner, so it is named one.

## The capture flow

The journey is **two relay sessions** with an untimed household decision
between them. Both use `crossover_v2:session` / `crossover_v2:verify`.

**Stage 1 — `POST /correction/crossover/v2/session`, the same captures on both tiers.**

| index | phase | what it is |
|---|---|---|
| 1 | `check` | microphone check |
| 2 | `measure` | design-axis anchor, per-driver |
| 3 | `entry_baseline` | summed sweep at the mark — the round's measured "before" |

Which phases stage 1 walks is stated in the flow file, not a guess:
`STAGE1_INCLUDES_ENTRY_BASELINE` is `True` and `STAGE1_INCLUDES_CLOUD_MEASURE`
is `False`, and no stage-1 plan builds a `lateral` group at all — an operator's
staged angle walk is the one way `lateral` indexes reach a plan (#2732, and its
close publishes nothing). The entry baseline is
**last** on purpose: the less the room, the mic and the household have moved
between it and the graph change, the more of the before→after difference is
the graph.

**The 6-pose `lateral` walk is no longer a stage-1 group (2026-08-18 pause,
retired 2026-08-22).** It ran as indexes 3–8 from R17 until the pause. The
owner-ratified evidence: over the 8 banked rounds it was 59.4% of all session
audio, never changed an outcome, and the scalar it fed ranked below its own
3.54 dB repeat noise. **The R17 Fc candidate sweep went with it** — plan ruling
R1, `docs/tuning-master-plan.md` ticket 2.3 — so a round commits the corner the
household declared or an operator pinned, which is the verdict all 8 banked
rounds reached anyway.

The pose measurements themselves are sound, so every piece of the walk stays in
place — prompts, screens, ladder, curve builder, relay arithmetic — and an
operator's staged angle walk runs all of it as evidence for the offline P2
forward model. What is gone is the stage-1 arming and the adjudicating close.
The relay capacity guard counts those six poses **unconditionally**, because a
staged walk can add them to any session.

The set is held open past its capture target until the phone posts
`complete_capture_set` — the household's "Continue". That signal closes the
group and publishes the candidate; until it arrives the final position is
still retakeable. **Nothing is applied inside this session.**

**Stage 2 — `POST /correction/crossover/v2/verify` with
`{"stage": "post_apply"}`, 6 captures at Full (1 on Express, 6 on Remote —
this stage is the whole difference between the tiers).**

| index | phase | what it is |
|---|---|---|
| 1 | `verify` | design-axis anchor, summed |
| 2–6 | `cloud_verify` | 5 prompted post-apply positions — `CLOUD_VERIFY_POSE_PROMPTS` |

The same endpoint with no `stage` is the 1-entry recovery re-verify.

### The remote tier — an external driver, not a household

`TIER_REMOTE` is **Full's measurement with a different operator**: a program on
another machine drives a mic positioner and advances the session over HTTP,
while the capture browser runs unattended. It is experimental and API-only.
Product code stays positioner-agnostic — nothing under `jasper/` names any
particular hardware, and the vocabulary is *remote* / *external positioner* /
*angle*.

**What differs from Full, and nothing else does.**

| | Full | Remote |
|---|---|---|
| stage 1 | the shared opening captures | the same |
| stage 2 | the post-apply cloud | the same |
| per-entry advance | `AUTO_ADVANCE_TAP` | `AUTO_ADVANCE_COUNTDOWN` + `countdown_s` |
| pose copy | "12 cm to the LEFT of the mark" | "Turn the microphone to −7°" |
| stage-2 anchor | carries `confirm_title` (a tap) | omits it — the gate makes that promise |

`remote_cloud_verify_positions()` **derives** the stage-2 count rather than
stating it: it
is the longest prefix of `CLOUD_VERIFY_POSE_PROMPTS` containing no
`POSITION_ROLE_XOVR` pose, so editing that table moves the walk instead of
stranding it. Since the 2026-08-24 geometry ruling that table is vertical-free
by construction, so the subtraction is a no-op and stage 2 does not differ
between Full and Remote. The derivation stays because it is what keeps them
from diverging silently if a vertical pose is ever added;
`REMOTE_VERTICAL_DISCLOSURE` stays for the same reason it always existed — a
consumer reading this group's roles finds no `xovr` member and must read that
as *unsampled*, not *flat*. `position_angle_deg()` likewise derives each
bearing from the pose's own `offset_cm` at `MARK_DISTANCE_M`, signed by the
row's LEFT/RIGHT word — there is no second table of angles to drift. The walk
is **0°, −7°, +7°, −22°, +22°**, and since that ruling the 0° is a prompted
`cloud_verify` POSE rather than only the anchor in front of the group: the
anchor's summed sweep is consumed by the tracking verdict and never joins the
group, so without it the post-apply combine carried no design-axis curve at
all. Roles still come from the existing `WIDE_OFFSET_MIN_CM` rule, so a remote
group's durable evidence stays comparable with a hand-walked one's.

**The pose set is a PARAMETER, not a fixed table.**
`build_v2_verify_capture_plan` / `build_v2_verify_session_spec` /
`CrossoverV2Session` all take `verify_prompts`, resolved once through
`verify_pose_table()`; `None` is `CLOUD_VERIFY_POSE_PROMPTS`, the ratified
default above. The shape's `M` and the table's length must agree — a shape
asking for more prompted poses than the set supplies is refused rather than
walked short.

**Every retained cloud position records WHERE it was taken**, as fields rather
than as prose: `position_deg` (signed whole degrees, negative LEFT — the same
word a lateral pose record uses), `position_axis` (`horizontal` / `vertical`),
and `mark_distance_m`. Derived by `position_geometry()` off the pose the
operator was actually given; nothing parses the `prompt` string. `position_deg`
is `None`, never `0`, where no bearing was commanded — a vertical pose, or a
geometry-locked retake, whose record declares no side.

**The position gate replaces the tap.** Because entries auto-begin, something
has to guarantee the tone never plays into an arm still moving. Every begin —
including the 0° ones — is held until the driver says the microphone has
arrived. The hold is the **shipped** `CaptureBeginDeferred` soft-hold, so **no
capture-page change is involved**: the Pi answers `capture_deferred`, the page
parks with no affordance and re-posts the identical begin every 1.5 s, the
attempt budget is not spent, and the session does not end. Gating is per
`(index, attempt)`, so a retake re-gates.

**Two shapes are gated, and the arm is only one of them** (#2879). What the
gate needs is a pose stated as a BEARING — the number it publishes and waits
for — and that is a separate fact from who advances the walk. `V2PlanShape`
says them separately: `externally_positioned` is the ADVANCE axis (a machine
moves, so every entry auto-begins behind the countdown) and `positions_gated`
is the POSE-STATEMENT one (poses read as angles, entries carry
`position_deg`/`position_role`, and every begin is held). The arm holds both.
A **hand-walked round on the wired source** holds only the second: it is
`hand_released_positions`, set by the host when a hand-walked shape opens on
the wired capture source, and it keeps the tap because a person is there to
give one. That combination — degrees plus a tap — is the string-and-protractor
technique, and no tier can express it, which is why it is a shape fact rather
than a fourth tier.

**Session start takes THREE human gestures at the capture device, not one.**
The tier automates the WALK, not the opening of a session. Someone has to open
`relay.tap_link` in a browser and then:

1. grant the microphone (a `getUserMedia` permission prompt — a browser gesture
   requirement, not something the plan can waive);
2. tick the placement acknowledgement the spec binds
   (`acknowledgement_binding`); and
3. tap **Start**, which posts the first `begin_capture`.

Only after that does the tier run hands-off. Plan for a person at the capture
device for the first ~30 seconds of every remote session.

**The driver contract**, in the order a run uses it:

1. `POST /correction/crossover/v2/session` with `{"tier": "remote"}` (CSRF as
   usual). A human performs the three gestures above at the capture device.
2. Poll `GET /correction/crossover/envelope` and POST the `next_action` specs
   as the wizard would.
3. When `relay.position_pending` is present, it names the target:
   `{index, attempt, degrees, role, action}`. Move the positioner to
   `degrees` (negative = left of the design axis), wait your own settle time.
4. POST `position_pending.action` — `/correction/crossover/v2/position-ready`
   with `{"index": …}`. `index` must be a JSON integer (a malformed body is a
   400) and is checked against what is actually pending, so a retry that
   crossed a capture starting is refused (409) rather than releasing the *next*
   position. The held begin is then admitted.
5. Repeat. Analysis, apply, verify and restore are unchanged.

**Steps 3–5 have a shipped implementation for the lab turntable arm:**
`jasper-arm-walk` ([`jasper/active_speaker/arm_walk.py`](../jasper/active_speaker/arm_walk.py),
CLI in [`jasper/cli/arm_walk.py`](../jasper/cli/arm_walk.py)). It runs on the
speaker, polls this envelope over loopback, drives the turntable adapter as a
subprocess, and posts `/position-ready` — with the power preflight, the ±45°
envelope clamp, the measured settle, and the park-and-verify held in code rather
than in an operator's attention. Every campaign before it wrote the loop again
from scratch. It is opt-in and foreground: nothing starts it. Steps 1–2 are
still the operator's (or the wired source's, below); see
[`testing-tooling.md`](testing-tooling.md#lab-arm-walk-harness).

**The WIRED capture source changes steps 1–2 of this contract** (#2662 W2b,
`jasper/web/correction_crossover_v2_wired.py`). When a measurement-class USB
mic is plugged into the Pi (usbid matched against the calibration registry —
a UMIK-2; never a voice array) the session opens on the wired source by
default (`JASPER_CAPTURE_SOURCE` overrides in either direction, documented in
`.env.example`): the Pi plays and records on one host, so there is **no phone,
no relay dependency, and none of the three capture-device gestures** — the
walk begins on its own. The position gate is unchanged (step 3–4 exactly as
above: every begin still holds until `/position-ready`) — and on the wired
source a HAND-WALKED round is gated too (#2879), because there is no capture
page to tap: without the hold the local runner would fire every capture back
to back while the household was still walking to the next spot. Whoever is
holding the tape is the one who POSTs `/position-ready`, and the envelope
publishes the same `position_pending` payload the arm's driver reads.

> **Until [#2881](https://github.com/jaspercurry/JTS/issues/2881) lands, that
> POST is MANUAL, once per capture.** Nothing shipped renders
> `relay.position_pending` or posts the release except `jasper-arm-walk`, which
> drives a turntable. So a Full/Express round on the wired source — the source
> auto-selects whenever a measurement-class mic is attached — holds at **every**
> capture and waits for a human to run the curl below. Leave it unattended and
> each hold expires after `REMOTE_POSITION_HOLD_BUDGET_S` (600 s) as
> `position_hold_expired`: loud, named, and self-recovering, but a wasted
> session. **The tuning loop is unaffected** — `scripts/run-crossover-round.py`
> defaults to `--tier remote`, which `jasper-arm-walk` already releases.
>
> The release needs the CSRF dance (no bypass exists, by design): GET a wizard
> page with a cookie jar to mint `jts_csrf` + the `<meta name="jts-csrf">`
> token, then POST with the jar and the `X-CSRF-Token` header. `index` is the
> `position_pending.index` the envelope just published — a stale one is a 409,
> which is the guard working.
>
> ```sh
> JAR=$(mktemp)
> TOKEN=$(curl -fsS -c "$JAR" http://jts.local/correction/crossover/ \
>   | sed -n 's/.*name="jts-csrf" content="\([^"]*\)".*/\1/p')
> # ...read position_pending.index off the envelope, then per capture:
> curl -fsS -b "$JAR" -H "X-CSRF-Token: $TOKEN" \
>   -H 'Content-Type: application/json' -d '{"index": 1}' \
>   http://jts.local/correction/crossover/v2/position-ready
> ```

Two steps are new on this source:

* when stage 1's pre-apply group is walked, the held set closes on
  `POST /correction/crossover/v2/complete` (empty body) — the wired stand-in
  for the phone's all-spots-measured confirmation; the wait is bounded by the
  session's wall-clock ceiling and expires as `session_ceiling_expired`; and
* `POST /correction/crossover/v2/retake` (empty body) re-opens the take that
  just completed — the wired stand-in for the phone's
  `begin_capture {retake: true}`. **Its terms are the relay's own §2.6, stated
  once** in `run_capture_plan`'s docstring
  ([`jasper/capture_relay/session.py`](../jasper/capture_relay/session.py)) and
  implemented against that statement in `build_v2_wired_run_and_consume`
  ([`jasper/web/correction_crossover_v2_wired.py`](../jasper/web/correction_crossover_v2_wired.py));
  read either, not a third copy here. The two facts that are LOCAL rather than
  the relay's: the request names no index (WHICH slot is the walk's own fact),
  and a retake the walk cannot serve is journalled as
  `event=correction.crossover_v2_wired_retake_refused`, leaving the household
  the take they already had rather than ending the session.

A
REJECTED wired capture auto-retries the same position on the next attempt
(bounded by the plan's own admission budget), so the rejected-capture stall
below is a relay-session shape. Wired captures carry real frame/gap counters
plus the ≥128-zero dropout scan in `capture_integrity`, so the analyzer's
frame-accounting checks always evaluate them.

**A REJECTED capture ends an unattended run — watch for it.** When a capture is
rejected (clipped, too quiet, locate failed, …) the capture page renders a
human **"Try again"** affordance and — with the one exception below — nothing
auto-fires it: `auto_advance` governs the transition after an *accepted*
capture only. A remote session with
nobody at the device therefore stops there and eventually dies on the runner's
inactivity budget. **The driver should detect the stall from the envelope
rather than wait it out** — a `relay` block whose own `status` is still one of
the in-flight ones while `position_pending` is absent and no new capture is
accepted is the signature — and report it for a human. **That status is
load-bearing, not decoration:** the finished session's block STAYS in the slot
(it is what the status page renders the outcome from), so a driver that read
the block's mere presence as "in flight" would call a completed round a stall.
`jasper-arm-walk` did exactly that until 2026-08-21 and reported a fully
measured jts3 round as `rc 6` — see
[`testing-tooling.md`](testing-tooling.md#lab-arm-walk-harness) "A walk ends
when its session does". The gap #2506 describes is still open
([issue #2506](https://github.com/jaspercurry/JTS/issues/2506)), and it is
about a CLASS, not a run count: a genuinely transient rejection — the
`silent_auto_retry` vocabulary, `clipped` and `drift_baselines_disagree` — is
one the same spot would clear on the next take, and it routes to a button no
unattended session can press.

**The exception, since capture-page build `20260815.5`
([#2557](https://github.com/jaspercurry/JTS/issues/2557) phase B).** One
rejection retakes itself: a take whose OWN pre-upload scan found a
block-aligned render quantum of digital zeros inside the recording — the
browser capture-FIFO splice, present in the phone's `Float32Array` before it
uploads. The page then presses its own **Try again** once for that measurement,
inside the extra-try budget the host already minted, and declares the automatic
round with `capture_integrity.auto_retake`. It is not a class filter and it
does not close #2506: it fires only on the page's own measured evidence, so
every other rejection — including a `drift_baselines_disagree` with no such
evidence — still waits for a thumb.

**Do not size that gap from the 2026-08-15 remote deaths.** Those sessions died
on rejected lateral captures, but the cause was located and it was not
transient: a deterministic ~128-sample playback insertion in the fan-in render
thread, which broke *every* lateral capture and which retries measurably did not
clear ([issue #2533](https://github.com/jaspercurry/JTS/issues/2533)). Against a
deterministic Pi-side fault an auto-retake is actively harmful — it would spend
the tier's attempt budget re-measuring a defect and hide it behind a
budget-exhausted death instead of a named rejection. That fault class has since
been fixed ([#2536](https://github.com/jaspercurry/JTS/pull/2536),
[#2542](https://github.com/jaspercurry/JTS/pull/2542)), which is exactly why the
case left for auto-retry is the transient one. Whatever lands must stay bounded
by the attempt budget already minted, and must keep the honest rejection
visible when the budget runs out.

What #2506 is blocked on is where the fire can live. **A retake cannot be fired
from the Pi.** The relay protocol has the phone
initiate every capture (`begin_capture {index, attempt}`), and the host→phone
vocabulary — `capture_authorized` / `capture_deferred` / `capture_refused` /
`capture_result` / `capture_set_complete` / `capture_set_exhausted` — carries no
"record now". The page's begins that need no thumb are
`advanceAfterAccepted` (an *accepted* verdict), the `capture_deferred`
re-post loop (which re-posts the identical begin, and only inside the
begin→authorize exchange, before anything is recorded), and — since #2557
phase B — the witness-triggered auto-retake above. Only that third one is
reachable from a rejected verdict, and only on the page's own capture-time
evidence. So the fire is the page's; what the Pi already publishes is the
**decision** — `PhaseVerdict.to_relay_dict()` puts `template` and `auto_retry`
(`spec.template == TEMPLATE_SILENT_AUTO_RETRY`, today `clipped` and
`drift_baselines_disagree`) on the wire beside `attempts`, and the page reads
neither field.

**Where the class filter belongs, revised by what #2557 phase B measured.** The
earlier ruling here was "keep the class filter on the Pi when that lands —
`auto_retry` is already the machine-readable *safe to retry from the same
spot*". That still holds for the #2506 CLASS case, and the geometry ask
(`cloud_geometry_locked`, or any gated session's `geometry_retake_unreachable`)
is
deliberately not in `auto_retry`, which is what makes it usable as that filter.
It is the wrong filter for the glitch trigger that actually shipped, and the
re-derivation is worth recording because the two look interchangeable:

* A glitched capture's reason code IS in `auto_retry`. `program_analysis`'s
  glitch inputs (`epsilon_out_of_bound` / `residual_desync` /
  `repeat_level_disagree` / `timeline_slip`) are telemetry disambiguators, not
  codes; the verdict is one code, and `spatial`'s `SCREEN_CAPTURE_GLITCH` maps
  to `drift_baselines_disagree`, whose template is `silent_auto_retry`. So a
  Pi-side `auto_retry` filter would have fired on every one of the 13 events of
  the 2026-08-15 campaign — **and equally on every other glitch-class
  rejection, whatever caused it**.

  > **`timeline_slip`, and the exposure it does NOT close — read this before
  > trusting per-driver phase.** The fourth input gates a discrete
  > sub-sample timeline step, the silent-USB-slip class: the Stage-0 bank
  > measured these at ~0.5 % of captures with **no ALSA error at all**, and a
  > slip landing between the interleaved per-driver segments biases exactly
  > the woofer-vs-tweeter timing that per-driver phase and the `M*C/P`
  > composition depend on. It moved the rejected step from about 4 samples to
  > 2 (83 µs → 41 µs). **A ~1-sample slip (20.8 µs) still passes**, which is
  > at the 20 µs relative-phase bar — 15° at 2 kHz, which is the entire
  > 10–15° summation budget. That floor is a
  > structural property of the analysis, not a tuning choice, and it is pinned
  > by test: six locate positions and a best-of-five cut search cannot resolve
  > below it, and tightening the gate to try costs 15 % false rejection one
  > stress row below worst case. Closing the last 2× requires adding signal to
  > the capture — a program-level timing pilot — which is a future program
  > change, not an analysis one. Owner of the measured numbers and the limit:
  > `SLIP_GATE_SAMPLES` in
  > [`jasper/audio_measurement/timeline_slip.py`](../jasper/audio_measurement/timeline_slip.py).
* That is the wrong side of the line this section already draws two paragraphs
  up. The 2026-08-15 remote deaths were rejected as glitches by a
  *deterministic* Pi-side playback insertion (#2533), against which an
  auto-retake is actively harmful — and an `auto_retry` filter cannot tell
  those from a browser hiccup, because both carry the same code.
* The page's witness can. A Pi-side insertion into the played stimulus leaves
  the phone recording a live room; the zeros #2557 detects are in the phone's
  own buffer and are produced by the browser's capture FIFO. Filtering on
  *evidence measured in this take* rather than on *the class of the rejection*
  is what makes the automatic retake safe to spend an attempt on.

So the trigger is page-side; the geometry exclusion is page-side too, and the
`prompt` field it reads is safe as a proxy only while every geometry rejection
is prompted or terminal (pinned in
`tests/test_capture_page_js.py::test_capture_page_auto_retake_never_answers_a_geometry_ask`).

**A transient relay failure is NOT that stall — the page recovers on its own.**
A network blip on the begin exchange used to land in the same
tap-to-continue state, which cost the first remote night a whole stage
([issue #2517](https://github.com/jaspercurry/JTS/issues/2517)): one blip at the
admission moment became a 120 s `awaiting_arm` expiry. The capture page now
re-sends that exchange automatically on a backoff ladder — bounded by both rung
count *and* wall clock, so it can never spend the `awaiting_arm` budget the
household's tap still needs — before it ever asks for a tap
(`withRelayReconnect` in
[`capture-page/js/main.js`](../capture-page/js/main.js); the rungs, the
wall-clock arithmetic, and the safety argument for re-posting an identical
begin are all stated there, not restated here). The
distinction matters to a driver author: a **rejected** capture still needs a
human and is #2506's problem, while a **transport** blip no longer does. Both
still end the run if they outlast their budgets, so the envelope-stall
detection above stays the driver's backstop.

**A geometry-locked group refuses rather than prompting — on EITHER gated
shape** (#2879). If a group's echo estimates cluster, a screen-paced walk asks
for a wider retake: 75 cm out, and on the second rung 75 cm out *and above*
mark height. A gated walk cannot serve that, for two reasons. An external
positioner cannot **reach** either rung. And any gated retry re-authorizes the
same plan entry, so the position gate goes on publishing that entry's original
bearing while the screen names the wider spot — two answers to where the
microphone should be, which a person could walk to but could not be told which
of to believe (and rung 2 is a HEIGHT, which a bearing cannot spell at all).
So a gated session ends with `geometry_retake_unreachable` and recommends the
screen-paced instrument that can ask for those spots. The predicate is
`positions_gated`, not the tier: before #2879 stage 2 was constructed with no
tier at all, so its groups prompted for the 75 cm rung even on the arm.

**Two clocks end a hold, and they name different failures.** An unanswered hold
does not wait forever: `REMOTE_POSITION_HOLD_BUDGET_S` (600 s) refuses it as
`position_hold_expired`, because a mover that stopped — a crashed driver, or a
person who walked away — would otherwise pin the measurement volume, the paused
voice, and the relay slot indefinitely. That is
a **per-hold** bound and not the operative total — the session's own wall-clock
ceiling (`session_wall_clock_ceiling_s`, 1800 s for stage 1 and 2160 s for
stage 2 at the shipped shape) covers the whole walk, so a mover that answers
every position but answers slowly ends on that ceiling with no single hold ever
expiring. That death has its own name too, since
[issue #2506](https://github.com/jaspercurry/JTS/issues/2506):
`session_ceiling_expired`, refused by the gate on the next held begin after the
lazy ceiling enforcement (the same envelope poll the releasing side already
makes)
reports the walk stale. It is checked **after** the per-hold budget, so a
genuine stall keeps the more actionable sentence when both bounds are past. No
new budget is introduced by that name: past the ceiling the session volume
fails closed (`SessionVolumePlan.assert_ready` refuses a stale-active plan), so
every capture after it was already doomed — what changed is that the session
says so instead of limping on to the relay link's own expiry and reporting
`relay_timeout`, a claim about a transport that never failed. The hold cannot
outlive its session either: the relay slot drops the gate as soon as it leaves
an in-flight status, and the gate clears its own pending state on both exits.
Observability: `event=correction.crossover_v2_remote_session_open` — emitted
for either gated shape, with `hand_released=` naming which mover releases the
holds; the event keeps the arm's name because drivers grep it —
`…_position_pending` (with `degrees`), `…_position_released`,
`…_position_hold_abandoned` (a held begin the walk left to serve a retake, so
the envelope stops advertising a position nothing is measuring),
`…_position_hold_expired`, `…_session_ceiling_expired`,
`…_geometry_retake_unreachable`, and on the wired source
`…_wired_retake` / `…_wired_retake_refused`.

**The link is minted to outlive the stage.** A relay link is an absolute clock
(`TIME_BUDGET_LINK` — minted once, refreshed by nothing), and the shared
default is shorter than either remote ceiling above, so a remote stage sizes
its own: `relay_link_ttl_s` asks for that stage's ceiling plus
`REMOTE_RELAY_TTL_MARGIN_S`, clamped at what the relay Worker grants
(`capture_relay.session.MAX_TTL_S`, mirrored from `relay/src/worker.js`, which
clamps rather than refuses). Each stage mints its own link, so stage 2 gets a
fresh one across the apply boundary. Hand-walked tiers keep the default. Read
the numbers off those constants, not from here. Symptom when this is wrong: the
capture page is alive and re-posting, and the Pi's own status poll takes a
`404 not_found` mid-walk — recorded as
[issue #2509](https://github.com/jaspercurry/JTS/issues/2509), which killed the
first remote run at ~890 s of a 2520 s stage.

**What it cannot say.** A remote walk samples one axis, so its post-apply group
carries no `xovr` role at all. The done screen discloses that once
(`crossover_v2_remote_horizontal_only`, severity `info`) and recommends a Full
measurement — it never blocks. Read an absent vertical as *unsampled*, never as
flat.

**The SESSION phase vocabulary is data, in one place.** `PHASE_*`,
`CAPTURE_PHASES` and `GROUP_PHASES` live in
[`crossover_v2/journey.py`](../jasper/active_speaker/crossover_v2/journey.py),
with `JourneyPlan` (index map → ordered walk → group index spans →
`post_apply_verifies`) and `CommissionJourney` (the `accept` / `mark_applied`
transitions). `GROUP_PHASES` are the three whose accepted-capture bookkeeping
is per *index* rather than per phase, because one phase spans many prompted
positions: `cloud_measure`, `cloud_verify`, `lateral`.

"Session" is load-bearing in that heading. A **second** phase vocabulary — the
STIMULUS one — lives in
[`audio_measurement/program.py`](../jasper/audio_measurement/program.py) and
answers a different question: which composer built the excitation being played
(`PROGRAM_PHASE_CHECK` / `_MEASURE` / `_VERIFY`, and `PROGRAM_PHASES`). The two
are not interchangeable, and a spatial cloud is where they come apart most
visibly: all of its positions sit under the ONE session phase named just above
(`cloud_measure`, or `cloud_verify` after the apply — that is what a
`GROUP_PHASES` member spanning many indexes means), while every one of those
captures plays the VERIFY-shaped summed sweep, so each carries
`program.phase == "verify"`. One session phase, many positions, a stimulus
phase that matches neither's name. They used
to share all three NAMES as well as their string values, which let an import
site take the wrong family silently; the `PROGRAM_` prefix is what separates
them now. The values still coincide, deliberately and permanently: both sets
are banked (the stimulus phase is hashed into `program_id`; the session phases
are persisted in the flow state), so neither may be renamed on the wire.

**The fit is the last thing before the apply.** Building the candidate at the
group's close rather than at MEASURE's accept is what lets it consume the
cloud evidence the household was just asked to produce. MEASURE keeps every
trust gate it owned — they read the analysis, not the candidate — so a session
doomed at sweep two still fails at sweep two.

## The round, graded

A round is *capture → plan → apply → verify → adopt*. Its **tail** — grade,
act, restore if the table says restore, bank the receipt — is
[`crossover_v2/coordinator.py`](../jasper/active_speaker/crossover_v2/coordinator.py):
`run_round(evidence, ports)` over a frozen `RoundEvidence` and a narrowed
`RoundPorts` (five seams), returning a `RoundDecision` whose refusal is a
typed `RoundRefusal` kind. It is the one module in the package that calls
seams and journals; it still holds no session state and reaches no host
object.

**Four independent verdicts, never one overloaded pass/fail.** Each is its own
function in
[`crossover_v2/verification.py`](../jasper/active_speaker/crossover_v2/verification.py):

| question | function |
|---|---|
| is this capture evidence at all? | `evaluate_capture_validity` |
| did the graph do what the model said? | `evaluate_realization` |
| is the after better than the before? | `evaluate_benefit` |
| does it meet the flat spec? | `evaluate_spec` |

`evaluate_capture_validity` grades `ProgramAnalysis.capture_integrity`, whose
first two checks are frame accounting (#2094): the browser's own report of
render-graph continuity, and whether every frame the capture page says it
recorded reached this host. Both are `not_evaluated` when the page reported no
counts, which the shipped comparability rule still treats as usable — the record
is `not_evaluated`, never a pass. See
[`jasper/audio_measurement/frame_ledger.py`](../jasper/audio_measurement/frame_ledger.py)
for the per-hop exactness argument, including the one hop no counter can close.

`verification_result` bundles them. Since #2537 they are then composed into
**adoption axes** — `evaluate_evidence_trust`, `evaluate_applied_safety`,
`evaluate_round_quality`, and since #2602 `evaluate_iteration_headroom` — and
`decide_adoption` selects one of seven rows from those. The four-verdict split
exists because a realization answer once stood in for an acoustic one and a
failing round read as passed; the #2537 axis rebuild exists because the table
those four fed keyed on whether a round could *prove* it helped, and reverted a
measured, safe, improving candidate that could not. #2602 added the fourth axis
and split the one row that used to be terminal: a round that passes keeps
iterating while a flatter, more level result is still reachable, up to
`ROUND_SERIES_CAP` rounds — *in-tolerance is not done*. #2656 split the MISSING
row the same way: the budget ends that series too (row 7), because until then a
round that kept missing kept being offered another one with none left to spend,
and the only bound was a button a headless driver never presses.

**Only three answers from that axis end a series**, per the ethos's
"least-bad measured, honed in bites": the round cap, the plateau, and
"already inside the plateau". `HEADROOM_NO_OBJECTIVES` is not one of them —
an ungradable objective is missing evidence, not a plateau, so it names the
ending in the reason and leaves the status `REACHABLE`. The practical effect
is that an Express round, which walks no post-apply cloud and therefore grades
no objectives, now offers another bite instead of stopping; the cap still
bounds it and the review screen's decline closes it on request.
See "The round, graded" below for the table itself; the campaign record's copy predates
#2602 and still shows the five-row shape, so read `decide_adoption` for the
current rows.

The two measurements a round compares, reduced to comparands and carrying the
margin below which a difference is not a change, are
[`crossover_v2/round_evidence.py`](../jasper/active_speaker/crossover_v2/round_evidence.py).

### The blend region — a second owner, and a second reported claim (#2600)

Per-driver linearization is deliberately blind across the crossover blend:
neither branch's own sweep can say what the SUM does there. Decision 10 of
[`active-speaker-tuning-layers-design.md`](active-speaker-tuning-layers-design.md)
gives that region a second owner — the summed at-the-mark measurement — and
one bounded tool. The solve is
[`crossover_v2/blend_correction.py`](../jasper/active_speaker/crossover_v2/blend_correction.py);
read its module docstring for the argument, not a second copy here.

Three things about it are worth knowing before touching the round:

- **It is emitted PRE-SPLIT**, with the room PEQs and above
  `active_baseline_headroom`. That placement is the safety argument, not a
  convenience: one summed fact gets one filter, the correction is common-mode
  by construction (the sum scales, the inter-driver ratio does not), and it
  sits upstream of the crossover high-pass that IS the tweeter's protection in
  the durable baseline. Moving it per-role would make it alignment work wearing
  a shape-correction hat.
- **The loop is incumbent-accounted, and it holds rather than reverts.**
  Per-branch MEASURE sweeps ride the protected-neutral graph, so the trim is
  re-derived absolutely each round and that is correct. The summed VERIFY
  capture rides the APPLIED graph, so its deviation already contains the
  incumbent's own correction — re-deriving absolutely there oscillates rather
  than converges. Every refusal arm re-prescribes the adopted incumbent
  unchanged: a round whose evidence failed has no standing to remove a
  correction adopted on measured evidence. The one exception is an incumbent
  that cannot be ESTABLISHED, which prescribes none — #2653's condition applied
  to this quantity, and the one state where there is nothing to hold.
- **A round that does not KEEP its graph issues no instruction.** A
  prescription describes a speaker measured through a specific incumbent, so a
  restored round's prescription describes a speaker that no longer exists. The
  next candidate then derives its correction from the applied (restored)
  profile instead. What the round commanded is still banked — that is history,
  and history survives a restore. A household **Undo** is the other door to
  that same state and takes the same withdrawal (#2698): `observe_restore`
  clears the receipt's `blend` sub-object — the instruction and the residual it
  was decided against — while keeping `round_ordinal`, so the series does not
  lose its place against the round cap.
- **It stops re-prescribing once the region stops improving.** A defect
  narrower than the correction can represent (`Q > 2`) cannot be matched, so
  the fit over-corrects its shoulders and the loop limit-cycles; the stop
  bounds that. It bounds the wander rather than guaranteeing improvement — the
  overshoot that triggers it has already been applied. See the module's own
  docstring for the measured series.
- **`benefit` now reports twice.** The pooled verdict is unchanged and is still
  the only adoption input. Beside it, `evaluate_region_benefit` runs the same
  estimator with only the band narrowed, because a win confined to two octaves
  cannot show itself in a residual pooled over six — which is why every
  series-1 round banked `residual_within_margin` about a speaker that had in
  fact not moved. The region claim discloses; it does not gate.

The region itself is **not** re-derived: it is read off the VERIFY absolute
claim's `band_hz`, which is
`program_analysis.crossover_region_band_hz`'s output. That function is
deliberately not `overlap_band_hz` — see its docstring — and the difference is
load-bearing rather than academic: on jts3 the per-branch band's floor is
1600 Hz and the series-1 dip sat at 1938 Hz, so both bands contain the dip and
only the summed one contains the octave below it.

Receipts bank the region's commanded-vs-realized pair under
`round_measurements.blend`, with the reason code beside the numbers so a round
that prescribed nothing says which arm fired — "the region was already clean"
and "the instrument refused" are different facts.

## File map

One line per file. Design prose lives in each module's own docstring — read
the module, not a second copy here.

| File | What it owns |
|---|---|
| [`crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py) | `CrossoverV2Session` — session state, seams, irreversible acts, and the host adapter; plus the capture-plan builders, tier/plan shape, cloud prompts, and `confirm_graph_is_live`. |
| [`crossover_v2/__init__.py`](../jasper/active_speaker/crossover_v2/__init__.py) | The organ package's index — what each sibling owns, and the rule that they are no longer numbered. |
| [`crossover_v2/contracts.py`](../jasper/active_speaker/crossover_v2/contracts.py) | The immutable domain values and their construction-time invariants and fingerprints. |
| [`crossover_v2/refusal_copy.py`](../jasper/active_speaker/crossover_v2/refusal_copy.py) | What the household is told when a round refuses: `REASON_*` codes, `TEMPLATE_*` shapes, `REASON_REGISTRY`, and `PhaseVerdict`. |
| [`crossover_v2/journey.py`](../jasper/active_speaker/crossover_v2/journey.py) | Where a round is and what its stage can do: the phase vocabulary, `JourneyPlan`, `CommissionJourney`, `open_stage`. |
| [`crossover_v2/programs.py`](../jasper/active_speaker/crossover_v2/programs.py) | What a session plays and how loud: `back_off_gain`, `SessionExcitation`, `program_for_phase`. |
| [`crossover_v2/priors.py`](../jasper/active_speaker/crossover_v2/priors.py) | What the analyzer is TOLD about each capture — every function a decision about what to withhold. |
| [`crossover_v2/spatial.py`](../jasper/active_speaker/crossover_v2/spatial.py) | What a capture-consuming phase decides about one take: the three screen ladders, the geometry-retake rule, the retained records. |
| [`crossover_v2/candidates.py`](../jasper/active_speaker/crossover_v2/candidates.py) | What one candidate build produced, as values that travel without `self`. |
| [`crossover_v2/fc_sweep.py`](../jasper/active_speaker/crossover_v2/fc_sweep.py) | Whether a crossover corner is admissible for this speaker's declarations — `_fc_rejection`'s two hard-excitation bounds — and the single owner of re-cornering a preset, `recornered_preset`. |
| [`crossover_v2/topology_prescription.py`](../jasper/active_speaker/crossover_v2/topology_prescription.py) | ONE crossover corner and order, pinned for ONE round: the request gate, its admissibility bounds, and the durable read-back. |
| [`crossover_v2/planning.py`](../jasper/active_speaker/crossover_v2/planning.py) | One candidate assembled: the eligibility gate, the planner request, and the emitted candidate. |
| [`crossover_v2/admission.py`](../jasper/active_speaker/crossover_v2/admission.py) | Who may start one more capture and what it costs — the bounded-retry meter, `MAX_EXTRA_ATTEMPTS_PER_POSITION`. |
| [`crossover_v2/capture_dispatch.py`](../jasper/active_speaker/crossover_v2/capture_dispatch.py) | Which screens an anchor capture must clear, and in what order, for the three sit-still phases (CHECK, MEASURE, VERIFY). |
| [`crossover_v2/intervention.py`](../jasper/active_speaker/crossover_v2/intervention.py) | The deterministic prescription planner as pure functions — assembly around existing DSP primitives, never a second fitter. |
| [`crossover_v2/accountability.py`](../jasper/active_speaker/crossover_v2/accountability.py) | Whether a built candidate may be PROPOSED at all — three assertions, most-specific first. |
| [`crossover_v2/proposal.py`](../jasper/active_speaker/crossover_v2/proposal.py) | One committed candidate gathered into the fingerprinted `InterventionProposal` the round receipt names. Computes nothing; refuses rather than raising. |
| [`crossover_v2/verification.py`](../jasper/active_speaker/crossover_v2/verification.py) | The four verification verdicts, the four adoption axes they compose into, and the seven-row table. |
| [`crossover_v2/round_evidence.py`](../jasper/active_speaker/crossover_v2/round_evidence.py) | The two measurements one round compares, the margin that makes a difference a change, and the series policy the headroom axis is handed (round cap, plateau margin). |
| [`crossover_v2/blend_correction.py`](../jasper/active_speaker/crossover_v2/blend_correction.py) | Decision 10's blend-region shape correction: the bounded cuts-first solve over the summed response, its four ceilings, the damped incumbent-accounted iteration, and the four refusals that make it a no-op instead of a boost. |
| [`crossover_v2/round_anchor.py`](../jasper/active_speaker/crossover_v2/round_anchor.py) | What an apply displaced, what it put live, whether the running graph is still that, and whether a restore is aimed at what the round displaced. |
| [`crossover_v2/coordinator.py`](../jasper/active_speaker/crossover_v2/coordinator.py) | The round's tail: grade, act on the adoption table, restore, bank the receipt. |
| [`crossover_v2/attempt_grading.py`](../jasper/active_speaker/crossover_v2/attempt_grading.py) | Whether a VERIFY capture is a new tuning attempt, and how it grades against the cross-session ledger. |
| [`session_volume_plan.py`](../jasper/active_speaker/session_volume_plan.py) | One fixed measurement volume per session: the `min(−20, max(caps))` SSOT plus open/close/abandon and the restore-once latch. |
| [`measured_crossover_candidate.py`](../jasper/active_speaker/measured_crossover_candidate.py) | `MeasuredCrossoverCandidate` — the fingerprinted apply artifact. |
| [`candidate_bank.py`](../jasper/active_speaker/candidate_bank.py) | Where banked candidates live on disk, and finding one by its own fingerprint — bounded scan, integrity through the candidate model, minting lineage resolved. The one owner of that shape (`applied_speaker_evidence` reads it from here). |
| [`linearization_envelope.py`](../jasper/active_speaker/linearization_envelope.py) | The Layer-1a correction envelope: per-bin allowed depth and the terms it takes the `min` across. |
| [`linearization_fit.py`](../jasper/active_speaker/linearization_fit.py) | The Layer-1a fit engine: `fit_driver_linearization` and its budgets, bands, and give-back. |
| [`camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py) | The baseline emitter, and the independent re-validation of every linearization filter before it reaches CamillaDSP. |
| [`crossover_envelope_v2.py`](../jasper/active_speaker/crossover_envelope_v2.py) | The pure `status → envelope` renderer: step list, screen dispatch, registry copy. |
| [`web/correction_crossover_v2.py`](../jasper/web/correction_crossover_v2.py) | The web host: endpoint bindings, durable v2 state, the real seams, apply/restore, `resolve_conductor_context`, `persist_conductor_state`. |
| [`web/correction_crossover_v2_republish.py`](../jasper/web/correction_crossover_v2_republish.py) | The republish door (7a): re-publish a banked candidate by fingerprint so apply can reach it. A host sibling reaching `correction_crossover_v2` late-bound, like the relay/wired providers. |
| [`web/correction_crossover_v2_relay.py`](../jasper/web/correction_crossover_v2_relay.py) | The relay capture provider (#2662): the plan-walk hosting (`build_v2_run_and_consume`), the phone phase ladder, purge grace, and link-TTL policy. The host re-publishes its names. |
| [`web/correction_crossover_v2_wired.py`](../jasper/web/correction_crossover_v2_wired.py) | The WIRED capture provider (#2662 W2b): source resolution (`JASPER_CAPTURE_SOURCE` + registry usbid match), the local plan walk against the same conductor hooks, and the answer mint. |
| [`audio_measurement/wired_capture.py`](../jasper/audio_measurement/wired_capture.py) | The wired capture engine: registry-anchored device probe, parameterized S32_LE ALSA capture with exact gap accounting, the re-homed ≥128-zero dropout scan, 32-bit WAV encode. |
| [`crossover_v2/capture_source.py`](../jasper/active_speaker/crossover_v2/capture_source.py) | The capture-source seam's contract (decision 13): provider identities and the WAV+metadata answer any source owes the session. |
| [`audio_measurement/program.py`](../jasper/audio_measurement/program.py) | The excitation-program model and its composers. Pure data, no safety decisions. |
| [`audio_measurement/program_analysis.py`](../jasper/audio_measurement/program_analysis.py) | The pure analysis: locate/segment, drift, gated transfer functions, prediction, VERIFY tracking. |
| [`audio_measurement/spatial_combine.py`](../jasper/audio_measurement/spatial_combine.py) | The spatial-cloud combiner and the echo/geometry diagnostics. numpy only. |
| [`audio_measurement/interference_nulls.py`](../jasper/audio_measurement/interference_nulls.py) | The interference-null identification gate and the per-position variance classifier. |
| [`audio_measurement/frame_fit.py`](../jasper/audio_measurement/frame_fit.py) | The frame between two curves about to be differenced — the model and its disclosure record, no band and no verdict. |
| [`attribution/`](../jasper/attribution/__init__.py) | Mechanism attribution's schema and persistence half: findings, the declaration registry, promotion, bundle-lifetime storage. |
| [`capture_relay/session.py`](../jasper/capture_relay/session.py), [`spec.py`](../jasper/capture_relay/spec.py) | Session-spanning capture plans, the begin/deferred/refused vocabulary, hold and timeout budgets, `CAPTURE_PROTOCOL_VERSION`. |
| [`capture-page/`](../capture-page/README.md) | The static phone recorder and the capture protocol it advertises. |

## Contracts & invariants (preserve these)

1. **Two safety invariants, one owner each.** *Never too loud* — one derived
   ceiling per driver, from declared sensitivities
   (`derive_hf_measurement_ceiling_dbfs` in `driver_protection.py`). *Never
   the wrong frequency range* — declared band plus a proven high-pass before
   any full-range content; MEASURE's channel routing carries each driver's
   crossover filter by construction.
2. **Sensitivities live in exactly one place: the declaration.**
   `declared_effective_driver_sensitivities(draft)` in `design_draft.py` is
   the SSOT, folded through any declared in-line pad. The same mapping threads
   into program admission *and* play-time readmission, so composed levels and
   the admission gate can never disagree about a derived ceiling.
3. **Session volume is `min(reference, max(caps))`, not `min(caps)`.**
   `session_measurement_volume_db` lets the least-sensitive driver reach the
   reference level while more-sensitive drivers attenuate down digitally —
   attenuating downward is always satisfiable, so every driver's cap is
   enforceable at this volume. `min(caps)` starved multi-way systems. Latched
   once per session; refused below the −60 dB emergency floor
   (`EMERGENCY_MEASUREMENT_VOLUME_DB`). **Nothing moves it, including the
   apply boundary.** The `reference` half is the codified −20 dB
   (`MEASUREMENT_REFERENCE_VOLUME_DB`) until an operator runs the calibrated
   seat-SPL leveling step (`jasper-seat-level`), which banks the measured
   volume in `seat_level_reference.py` for this derivation to read. The caps
   half is not operator-derivable: whatever the reference says, `min` keeps
   every driver's excitation ceiling binding.
4. **Analysis is a pure function of `(program, WAV)`.** No side-channel state.
   The `program_id` is a content hash and fingerprints both the analysis and
   the candidate, so a re-run can never be mistaken for a resume.
5. **Clock drift is estimated in-capture.** Each MEASURE capture embeds a
   repeated sweep so ε is estimated from the longest available baseline;
   baseline disagreement ⇒ glitch ⇒ reject plus one retry. The repeated sweep
   is **mandatory**, and the primary gate is anchored to the WOOFER's
   first-vs-last located sweep specifically — a design invariant, not an
   artifact of there being only one repeat.
6. **Adaptive gating, never a false verdict.** The reflection gate width sets
   a validity floor `f_valid_hz = 1/window_s`. VERIFY requires its gate window
   ≥ MEASURE's; a forced shorter VERIFY gate yields `verify_inconclusive` —
   never a false pass or fail.
7. **Apply is read-only compose, then transactional apply.** `handle_v2_apply`
   reopens the published candidate (the tamper check), gates on
   `expected_candidate_fingerprint`, translates the measured fingerprint into
   the baseline candidate's own fingerprint at the host boundary, then rides
   the existing `apply_baseline_profile` transaction with rollback.
    **7a. A BANKED candidate can be made live again — `POST
    /correction/crossover/v2/republish` (`{"fingerprint": …}`).** The apply
    slot is single-valued and has no lookup: `persist_conductor_state` rebuilds
    `state["candidate"]` from a fresh literal on every persist, so each measure
    session overwrites it and a failed one leaves it `None` — with every
    candidate still sitting write-once in its bundle. `handle_v2_republish`
    ([`web/correction_crossover_v2_republish.py`](../jasper/web/correction_crossover_v2_republish.py),
    a host sibling reaching the host late-bound like the relay/wired providers)
    locates one by its own fingerprint
    ([`candidate_bank.py`](../jasper/active_speaker/candidate_bank.py),
    also the single owner of where banked candidates live — the neighbouring
    `applied_speaker_evidence` reader shares it), re-verifies it through
    `MeasuredCrossoverCandidate.from_mapping` (the same recompute-and-compare
    apply runs; there is no second hasher), and republishes it with its
    **minting** `session_id` + `evidence.bundle_session_id`, because those two
    are the path `_reopen_candidate_artifact` rebuilds. It publishes
    `accepted_phases: ["measure"]` and clears `applied` and the accepted-Sound
    pair, because `_update_current_review`'s compare-and-set gates on all four
    and a failed CAS would apply the graph while recording nothing — leaving
    Undo unreachable. It applies nothing: every admission gate (declared floor,
    `_assert_stage_2_can_open` → `resolve_conductor_context`, excitation
    ceilings, the seam's own fingerprint guard) reads live SSOT, so no state
    write can satisfy one. **Two things it will not do:** restore `verify_priors`
    — they belong to the stage-1 conductor that ran the fit, so they are
    cleared rather than inherited and a post-apply VERIFY grades INDETERMINATE,
    never a false pass; and republish a candidate whose crossover differs from
    what `/sound` declares — that apply rewrites the declaration and needs the
    minting session's `sound_design_revision`, which no bundle carries, so it
    refuses naming that fact instead of inventing a revision. Journal:
    `event=correction.crossover_v2_banked_candidate_found` (the scan),
    `…_candidate_republished` (success), and `…_republish_refused` carrying a
    machine `code=` — an incident-recovery door refuses out loud, since an
    operator reaches it only when something has already gone wrong.
8. **Undo survives everything.** `handle_v2_apply` stashes the
   `pre_apply_profile` and `persist_conductor_state` carries it
   *unconditionally* across every snapshot, so `handle_v2_restore` can pin a
   restore to the prior compiled config even after a VERIFY re-arm. The
   `/sound` declaration undo is written in the SAME state write, so neither
   half can describe a different apply from the other. The anchor is a
   `(path, digest)` pair, and **its integrity is `restore_applied_baseline_profile`'s
   to prove** (#2519): it hashes the retained file itself and refuses under
   `restore_target_unreadable` or `restore_target_changed`, then hands the
   digest it just computed to `apply_dsp_config`, whose own proof is a
   validate-to-load race check over the current call and nothing older.
9. **The walked-away guarantee.** `SessionVolumePlan` holds one measurement
   window with an abort target, a wall-clock ceiling, and a restore-once latch
   drained by close, session death, or the ceiling. **Each stage arms its own
   ceiling, sized from the plan it actually emits** — the number moves with the
   plan; the cap is what makes the guarantee. A household that walks away can
   never leave the speaker pinned at measurement volume. The voice-daemon
   measurement pause is held for the whole session so the idle reconciler
   cannot revert it.
10. **The CamillaDSP safety ceiling stays.** `devices.volume_limit` is `0.0`
    (`DEFAULT_VOLUME_LIMIT_DB` in `camilla_config_contract.py`) and positive
    writes clamp to 0 dB in `CamillaController.set_volume_db`. The program
    graph adds no headroom beyond the main volume.

    **10a. The apply boundary's level move is DECLARED, never compensated.**
    An applied graph absorbs its correction's boost as a pre-split common
    attenuation, so the same commanded volume drives the speaker measurably
    quieter. **That attenuation is the excitation-safety property, not a bug
    to cancel** — the graph is `−H` pre-split and `+L_r(f)` post-split with
    `L_r ≤ H`, so a boosted band lands at or under unity however deep the
    correction. Raising the commanded volume to "restore" the level would put
    the boosted band over the compression driver's cap on a sustained swept
    sine. VERIFY therefore measures the corrected speaker at the **unchanged**
    commanded level, and the move is instead *declared to the analysis*:
    `observe_apply_success` persists `expected_post_apply_offset_db` in the
    same state write as the `applied` flag, so the flag that releases VERIFY's
    hold can never become visible without the offset beside it. The VERIFY
    tracking gate needs no such treatment — it is already level-offset
    invariant. The delta probe deliberately is not, because a level shortfall
    is one of the things it classifies.
11. **Linearization emission is independently re-validated at every boundary,
    never trust-the-caller.** The emitter and the runtime-safety verifier each
    re-prove biquad type ∈ {Peaking, Highshelf, Lowshelf}, gain at or under
    `MAX_LINEARIZATION_BOOST_DB`, and the shelf-placement structure from
    scratch — the fit engine's own vocabulary and per-filter-cap invariants are
    not assumed to have survived a JSON round-trip. (This bullet said
    "non-positive gain" and "cut-only invariant" until #2603's sweep; PR-L5's
    boost ruling had already moved both re-proofs to the cap.) The
    safety-posture rationale is owned by
    [`active-speaker-tuning-layers-design.md`](active-speaker-tuning-layers-design.md).
12. **A submitted graph is proven live before anything plays.**
    `confirm_graph_is_live` normalizes the submitted YAML through CamillaDSP's
    own `ReadConfig` and compares fingerprints strictly. Text equality against
    `GetConfig` cannot work — the readback is a default-filled, value-
    normalizing superset — so both sides come back through the same
    deserialization path. Normalization failure and mismatch stay distinct
    refusals.
13. **Stage 1's graph names BOTH ends of the box's transport.** CHECK and
    MEASURE are the only phases that emit their own graph — the four
    `SUMMED_SWEEP_PHASES` play into the already-active production graph — and
    that emit derives its whole `devices:` block from the resolved playback
    endpoint in one call (`active_emit_devices` in
    [`camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py)), capture lane
    and wire format and latency geometry together. It matters because a
    ring-armed speaker's playback endpoint is not its snd-aloop one: naming only
    the sink would sweep into the ring while CamillaDSP captured a lane nobody
    feeds — silence with every daemon healthy (issue #2450). Which transports
    exist and how a box gets armed are **not** this document's to state; the
    authority is
    [`HANDOFF-audio-graph-consolidation.md`](HANDOFF-audio-graph-consolidation.md).
14. **Every band a per-driver decision is graded over is clamped to the band
    that driver's own sweep excited.** `overlap_band_hz` does it for the GCC
    alignment, trim solve, ripple and VERIFY tracking; `branch_snr_band_hz`
    does it per branch for the capture-SNR verdict (issue #2613 — an
    unclamped window let a row the tweeter sweep never entered refuse every
    round). The clamp's contract, its named residual, and why an EMPTY window
    still cannot enfranchise an unexcited row are **code-owned**: read
    `branch_snr_band_hz`'s docstring in
    [`program_analysis.py`](../jasper/audio_measurement/program_analysis.py),
    not a restatement here.
15. **A prescribed round is opened AT what it was prescribed, and never
    inherits one.** Four things can be prescribed — blend and driver stage
    through `jasper-crossover-prescriber`; alignment and topology arrive as
    request-body keys on session open and refuse the WHOLE session at the tap.
    The four classes, the two entry surfaces and the severity split are
    tabulated once, in
    [`testing-tooling.md`](testing-tooling.md#the-other-two-prescriptions-do-not-come-through-this-door-2773)
    (#2773); do not restate them here. Two consequences are this document's:
    a **topology** pin replaces the session's own corner *and* preset at both
    stages (via `fc_sweep.recornered_preset`), so the fit, the §4.2
    de-embedding, the emitted graph and VERIFY's design target are that
    topology's rather than the incumbent's — and stage 2 must rehydrate the pin
    or it would grade an applied graph for not being the crossover it replaced.
    And a pinned round **publishes no selector verdict** — no round does since
    the corner hunt was deleted (ticket 2.3) and its selector retired (ticket
    2.4), so `fc_selection` is ABSENT from the record rather than written null,
    because reporting a verdict for a comparison that never ran is the
    same dishonesty as `polarity_agrees_with_sum` reporting disagreement for
    one. None of the four is inherited from a lapsed session's durable state
    the way `tier` deliberately is (#2639).

## Debugging — where to look first

**Terminal verdicts are internal reason codes, not screens.** `REASON_REGISTRY`
in [`crossover_v2/refusal_copy.py`](../jasper/active_speaker/crossover_v2/refusal_copy.py)
is the single source of truth for the copy: it maps each `REASON_*` code to
one of four templates (`silent_auto_retry` / `fix_and_retry` / `hard_stop` /
`session_restart`) plus the two special screens, the household sentence, and
the retry budget (`retry_budget == 0` ⇒ non-retriable). **Read the registry,
not a table.** The session decides the code; the envelope renders the copy —
one copy source, no drift. The retry COUNT is per *position*, not per code.

**The registry does NOT carry an owning phase** — `ReasonSpec`'s fields are
`code` / `template` / `retry_budget` / `banner` / `message` / `next_action` /
`retry_copy`, and that is the whole record. Which phase a refusal came from
is on the journal line instead: `event=correction.crossover_v2_result` logs
`phase=` beside the code. The per-code phase column in the campaign record's reason
table is a historical reading aid written when that mapping was prose — treat
it as dated, and read `phase=` for the answer about a specific session.

```sh
# The phase walk (the /correction/ wizard runs under jasper-correction-web).
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(authorized|play|result|apply|apply_complete|restored|cloud_group_complete|cloud_geometry_retry|cloud_spec|cloud_publish_skipped)'

# Session volume lifecycle (fail-closed). persist_failed is CRITICAL — the
# durable intent could not be written; sweep for it, not just the happy three.
journalctl -u jasper-correction-web | grep -E 'event=correction\.session_volume_(opened|restored|restore_failed|persist_failed)'

# Apply boundary, and the volume hazard. volume_close_failed is CRITICAL and
# means the speaker may still be sitting at measurement volume — sweep for it
# by name, never infer safety from a quiet log.
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(applied|volume_close_failed|volume_abandon_failed|volume_open_failed|volume_recovery_timeout)'

# What accountability GRADED and banked (it refuses nothing since doctrine
# deviations (c)/(i)), why a session refused (the delta probe, post-apply),
# and what the speaker actually did with the correction.
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(level_estimator_finding|level_match_finding|prediction_gate|predicted_spec_failed|realized_level_match|delta_probe|round_restore|round_recovery_required|delta_probe_restore)'

# Calibration handoff / uncalibrated warnings.
journalctl -u jasper-correction-web | grep -E 'event=correction\.crossover_v2_(calibration_resolve_failed|uncalibrated_capture|default_calibration_hint_failed)'
```

Reading the results:

- **`cloud_group_complete` and `cloud_spec` fire on EVERY close of a cloud
  group**, including a retake's re-close. Seeing either twice for one phase is
  the retake contract working, not a bug. `cloud_publish_skipped` is the line
  that means "the durable artifact now lags the candidate".
- **Triage a `locate_failed` OR a `channel_map_mismatch` by reading
  `event=program_analysis.anchor` first.** A mis-anchored timeline fabricates
  both verdicts on pristine captures — `locate_failed` when the shifted
  windows hold silence (#2093), `channel_map_mismatch` when they hold the
  OTHER driver's pilot (#2644). `ambiguous=true` on that line means the
  analyzer said so itself.
- **`crossover_v2_level_estimator_finding` is the banked-and-proceeded arm** (it is now the ONLY arm — a disagreement never refuses) — grep
  it when a session COMPLETED but the two level estimators disagreed.
- **A failure screen has a lifetime.** The persisted `failure` record carries
  its own `at` stamp and the terminal screen renders only inside
  `crossover_envelope_v2.FAILURE_FRESH_WINDOW_S`; older than that the
  household gets the ordinary entry screen plus one dated nudge. A record with
  no `at` reads as aged.

Deeper catalogs — the full reason-code table with its history, per-capture
diagnostics, the anchor cross-check, operator capture retention, and the W6
bug-class list — are in the campaign record.

## Boundaries / non-goals

- **3-way is a v2 non-goal.** The program/WAV layer generalizes to N channels,
  but the candidate and prediction would need to reshape from one alignment
  triple to per-boundary entries — a schema change.
- **Subwoofer/main alignment belongs to the bass-extension program.** v2
  measures nothing below its gated validity floor.
- **Fc/slope re-derivation and driver EQ beyond trims are a v3 door.** v2
  deliberately measures *as-crossed* branches and cannot recover them.
- **Commissioning's headroom on a literal 1 GB Pi is unmeasured** (#2168). One
  production-shaped MEASURE-accept analysis peaks ~400–430 MB and cannot
  complete under a 384 MB cgroup — measured 2026-08-06 on jts3 with the
  bounded runner: indefinite reclaim-thrash at PSI 92%, 8421 `memory.high`
  breaches, stalls rather than OOMs. Co-residency headroom against the
  resident daemon set has never been measured or budgeted — say "unmeasured",
  not "fine". Commissioning is rare and owner-present, which is why this is
  disclosed rather than engineered around; the three candidate fixes stay open
  on #2168.

---

The v2 campaign's dated narrative — bench sessions, hardware results,
and the decision archaeology behind the spine above — lives in
[docs/historical/crossover-measurement-v2-campaign-record.md](historical/crossover-measurement-v2-campaign-record.md).
It is frozen: read it for *why is it like this*, never for current state.

---

**Scope of this verification.** #2291 Phase 5c-v rewrote the **live spine**
above and verified every
claim in it against the code at `main` — module and class names, the seam list,
the stage flags, the phase vocabulary, the four verdict functions, the file
map, and the numeric constants in "Contracts & invariants" — by reading the
named symbols, not by trusting the previous text. Hardware claims were not
re-measured: the memory figures under "Boundaries" are quoted from the
2026-08-06 jts3 measurement, not re-taken. **The campaign record was
NOT re-verified**; per the documentation paradigm, historical sections are
deliberately not kept in sync with code, and the date below is not a warranty
on any fact in them.

**Addendum, 2026-08-14 — the remote tier.** "The remote tier" section and the
`TIER_REMOTE` bullet in "What it is" were written against the code that shipped
them and verified by test: the behavioural claims (the derived walk, the
angles, the gate, the dropped
confirm tap, the disclosure, the geometry-retake refusal) are pinned by
`tests/test_crossover_v2_remote_tier.py`. No capture count is restated in this
doc, so none needs a pin. The three-gesture start and the
rejected-capture stall were re-derived during the adversarial review of
PR #2505 — the acknowledgement gate from `capture-page/js/render.js`
(`acceptedAcknowledgement`, and the `refs.acknowledgement` block that holds
every `begin_capture` control disabled until the box is ticked), the Start tap
from `main.js`'s `onPlanStart`, and the stall from `main.js`'s
`advanceAfterAccepted`, which routes on the UPCOMING entry's policy only after
a capture is ACCEPTED. **The date below is deliberately NOT
bumped**: that addendum re-verified only what it added, not the rest of the
spine, and moving the date would claim a sweep that did not happen.

**Addendum, 2026-08-15 — the delta probe's band, frame, and retained curve
(#2521 / #2522).** The delta-probe paragraphs under "The delta probe verifies
the apply", the `correction_model_error` and `correction_level_shortfall` rows
in the refusal table, the "VERIFY
discloses the FRAME it compared across" section's closing note, and the stage
bridge's key list were re-derived against `delta_probe.classify_delta_probe`,
`crossover_v2_flow._run_delta_probe`, `capture_dispatch._gate_trusted_band_hz`,
and `correction_crossover_v2.persist_conductor_state` as landed. The measured
figures quoted there (the keystone's 0.575 → 0.307 octaves under a flat 1.0 dB
floor, its 0.575 → 0 under a graded-bin frame fit, and the ~36 KB retention
cost) were computed on this branch and are pinned by
`tests/test_active_speaker_delta_probe.py` and
`tests/test_crossover_v2_stage_bridge.py`; the live-session numbers (357–20,000
vs 325–22,480, `max_error_db=23.4` at 21,266 Hz, the −7.8 dB / ≈2.02 pair) are
quoted from issue #2521's diagnosis and were NOT re-measured here. The stage
bridge's key list also gained `proposal_fingerprint`, which had been live since
#2392 and unlisted — a drift found while updating the count, not a change this
work made. The adversarial review of PR #2530 then moved the frame gate ahead of
BOTH rollback doors (it had guarded only the shape one, leaving the same class
walking through the scale door) and added the frame's fitted span to the two
surfaces that report its terms; the numbers quoted for both — 203 of 4,000 swept
draws, and p95 |tilt| 10.5 dB/octave over a 10-bin quiet span — are the gate's,
re-derived on this branch before being written here. **The date below is
deliberately NOT bumped**, for the same reason as the addendum above.

**Addendum, 2026-08-15 — the residual becomes a change, and its band claim is
bounded (#2533).** The three new paragraphs under "The delta probe verifies the
apply" and the durable-summary sentence a few paragraphs below it were written
against `delta_probe.classify_delta_probe` and
`crossover_v2_flow._entry_delta_db` as landed on this branch. The cycle-4 figures
quoted there — the reported −3.342 dB decomposing as −1.660 standing anchoring,
−1.457 real measured change confined to 12–20 kHz and −0.221 declared graph move;
the quiet set's 158-of-160 bins above 12 kHz with strays at 493 Hz and 1.9 kHz —
are an OFFLINE re-derivation of session `cap_M_7TWNJJenpHAa4olM7tEA`'s retained
`verify_priors`, not a fresh hardware run. Re-grading that record through the
patched classifier reproduces the live `residual_offset_db` (−3.338 against the
persisted −3.342, the difference being the 512-point decimation of a
163,574-bin grid), removes a −1.528 anchor, and scores `quiet_probe_coverage`
**0.239** against a graded band of 539.6–9,970.6 Hz. The synthetic pins are in
`tests/test_active_speaker_delta_probe.py`, the retention/re-grade contract in
`tests/test_crossover_v2_stage_bridge.py`.

Two claims in the first draft of this addendum were wrong and are corrected
here rather than quietly rewritten. (1) The coverage ratio divided by the graded
band's WHOLE span and was justified by a log-uniform derivation — but production
grids are linear, and that form scored 0.303 for a perfect uniform sampling of a
real 357 Hz–10 kHz band, i.e. it was unclearable on any real capture at any band
width. The shipped ratio divides by the band's own interquartile span, which is
grid-invariant (1.000 co-spanning, on both grid shapes), and the cycle-4 figure
above is the new metric's; the draft's 0.079 was the old one's. (2) The
repeat-round contaminant was called "bounded"; it is not, and the caveat above
now states the fabricate and mask cases the adversarial gate constructed. Both
corrections came from that gate's review of PR #2545, and every figure in them
was measured on this branch. **The date below is deliberately NOT bumped**, for
the same reason as the two addenda above.

Addendum 2026-08-18 (session trims): the courtesy-tone section, the stage-2
tables, the tier capture totals, and the remote-tier comparison were rewritten
against the code in the same diff — the prelude now announces a SESSION
(`courtesy_prelude_for_phase`) and `DEFAULT_CLOUD_VERIFY_POSITIONS` sits at its
floor of 5. **The date below is deliberately NOT bumped**: nothing outside
those sections was re-verified.

Addendum 2026-08-19 (Fc/slope apply path): "Recommending an Fc" was rewritten
against the code in the same diff. Three claims in it had become false: the
apply is no longer gated on `fc_selection` (it derives the change from the
candidate's own preset, so the dormancy of the sweep no longer implies the
dormancy of the route); the declaration writer carries slope as well as
frequency and is now `apply_measured_crossover_geometry`; and a crossover below
the tweeter's declared protection floor is refused at the apply boundary,
before anything is written. The Undo paragraph gained the geometry the record
now carries. **The date below is deliberately NOT bumped**: nothing outside
that section was re-verified.

Addendum 2026-08-21 (CHECK channel map — the CROSS test is a ratio): the
fixed additive cross-rise bound `CHANNEL_MAP_CROSS_RISE_DB` (6.0 dB) is
retired and replaced by `CHANNEL_MAP_MIN_ISOLATION_DB` (12.0 dB) applied to
`target_rise − cross_rise`. Three sections were re-verified against the code
in the same diff and edited: the `crossover_v2_check_diag` field list (it now
publishes `channel_map_isolation_db` per role plus the bound, beside the two
raw rises), the analysis-constants paragraph, and gotcha #6 — whose "cross-band
rise <6 dB" sentence had become false. New gotcha #25 carries the hardware
table, the baseline-graph discriminator that ruled out crosstalk, and the
derivation of the bound. Amended in the same PR's gate round: the ratio is only
JUDGED above `CHANNEL_MAP_ISOLATION_JUDGED_ABOVE_DB` (the ungated form raised
the effective target floor by `cross_rise` and newly hard-stopped a
quiet-but-correct capture), and the claim that a swap collapses isolation to ~0
was measured false and replaced — the TARGET floor is the mis-wire catcher; the
CROSS half guards abnormal cross-band energy. **The date below is deliberately
NOT bumped**: nothing outside those sections was re-verified.

Addendum 2026-08-21 (the timeline anchor's witness score): a jts3 per-driver
MEASURE round failed 3/3 on the G2 schedule gate after re-anchoring a full
pilot spacing, because `_locate_in_window` returned only the aligner's
PEAKEDNESS margin and that margin cannot tell an empty search window from an
occupied one. Candidates are now ranked on the aligner's other score, the
correlation SIMILARITY, and `ANCHOR_DISCRIMINATION_MARGIN` (a 0.05 difference)
becomes `ANCHOR_DISCRIMINATION_RATIO` (50x); its derivation, including what the
ratio does and does not buy, lives at that constant in `program_analysis.py`
and is summarised under "Timeline anchor". Three sections were re-verified
against the code in the same diff and edited: that discussion (its
margin-does-not-separate-populations paragraph described the retired quantity),
the `event=program_analysis.anchor` field list, and both the `anchor_ambiguous`
and `channel_map_mismatch` rows of the refusal table. **The date below
is deliberately NOT bumped**: nothing outside those sections was re-verified.

2026-08-24 — the geometry ruling (fixlist T1-5/T1-6). What was re-verified
against the code in the same diff and edited, and nothing else: in the LIVE
spine, the two tier-count bullets, the stage-2 heading and index table, the
Full-vs-Remote table's stage-2 row, the `remote_cloud_verify_positions()`
paragraph (which gained the pose-set-is-a-parameter and
pose-geometry-as-fields notes), and the stage-2 wall-clock ceiling; in the
CAMPAIGN RECORD, only the sections the 2026-08-18 pass already states it maintains —
the stage-2 table, the walk enumeration, the constants list, the prompt-table
ORDER claim, the artifacts bullet, the `position_evidence_block` field list,
and the courtesy-prelude saving. **The date below is deliberately NOT bumped**:
nothing outside those was re-read.

Last verified: 2026-08-24 (#2929 — the fader-hold block was re-read against the
shipped code and CORRECTED, because #2925 had recorded the wrong mechanism for
it: item 1's mechanism and its acceptance criterion (now two lines read
together, `result=held` plus zero `result=disagreed` lines — wave 5 removed the
hold's repair write, so what used to be a repair PAIR is now a single
disagreement line before a refusal), item 4's racing-writer bound
(neither shape is bounded; the second `min` operand is `current + |depth|`),
the `measurement_volume_drift` row's closing clause, and the
capture-provenance section's retention note. Every claim in them was written
against `volume_latch.hold_fader_at`,
`camilla._duck_release_target_db`/`_graph_mutation`, and
`SessionVolumePlan.owned_measurement_volume_db_nowait` in the same diff, and
against CamillaDSP v4.1.3 at tag `05e9cfc`. **Scope: only those paragraphs**;
nothing else in the live spine was re-verified this pass. The prior pass's
reading, carried forward unchanged: 2026-08-18 — the lateral pause — the stage-1 capture flow, both
capture tables, the tier capture/duration totals, the remote wall-clock
ceiling, the fit-timing rule, and the "Recommending an Fc" section were
re-verified against the shipped `STAGE1_INCLUDES_LATERAL = False` and the
values `tier_display_info()` / `session_wall_clock_ceiling_s` actually return.
**Scope: only those sections.** The prior pass's reading, carried forward
unchanged: 2026-08-17 — series-2 D1 — the safety-axis section was rewritten
against the code in the same diff: the anchored directional findings, the two
SAFE reasons and their five surfaces, the comparability rule, what the anchored
rule cannot see, and the `safety_only` block. Two paragraphs that D1's own fix
round falsified were re-read against code and corrected in it — the seam-fence
paragraph, which had said the fence "needed no edit" and that an unanchored map
defers, and the surface count. **The date is deliberately NOT bumped**: nothing
outside the safety axis and the delta-probe section was re-verified.
Carried forward: #2600 — the "round, graded" section gained the
blend-region subsection, whose every claim was written against the code it
describes in the same diff, and the file map gained
`crossover_v2/blend_correction.py`. Nothing else in the live spine was
re-verified that pass. Carried forward: #2609/#2641/#2639 — the paragraphs that round's
change falsified were re-read against code and corrected: the headroom axis's
endings against `evaluate_iteration_headroom`, the receipt paragraph against
`coordinator._write_round_receipt` and `evaluate_round_quality`'s probe
escalation, and the review screen's decline and re-measure tier against
`_review_envelope`, `handle_v2_decline`, `_phase_from_state`, and
`prepare_v2_session`. Carried forward: #2602 — the live
spine's adoption-axis count, row count, and file-map rows re-read against
`decide_adoption`; #2611 — the delta-probe section's commanded-axis and
chained-round paragraphs re-read against `crossover_v2.commanded` and
`classify_delta_probe`; #2662 — the `driver_levels_disagree` row and the two
level-estimator event paragraphs re-read against
`intervention.plan_linearization`'s anchor block,
`check_level_consistency`, `accountability`'s `EVENT_LEVEL_ESTIMATOR_FINDING`
payload, and the `…_linearization_giveback` emit. All three named a level-datum
owner the code does not have, through two symbols
(`summed_level_reference_db`, `trim_band_delta_db`/`core_level_delta_db`) that
do not exist repo-wide. Carried forward: #2698 — the blend-region section's
restored-graph bullet re-read against `_blend_prescription`,
`coordinator._write_round_receipt`, and `observe_restore`, and extended to name
the household-Undo door the same rule now closes. Carried forward: #2738 — the
spec-verdict consumer bullet and the cloud-`flatness` "gates?" cell were both
re-read against `_done_nudges` and the done-screen assembly, which had
falsified them (the terminal result code overrode badge and copy on every
post-R18 session); the bullet now names the cap and its one capped code, and
the table cell was found true again as written. Carried forward: the
realized-level demotion (`measurement-loop-doctrine.md` deviation (i)) — the
`driver_levels_disagree` row was re-read against `accountability`'s item 1 arm
and `refusal_copy`'s registry, found falsified in both (the code and its row
are deleted, the gate banks a finding and the round proceeds), and rewritten as
a struck RETIRED row on the `correction_not_an_improvement` pattern; the two
journalctl recipes naming `…_level_match_refused` were repointed at
`…_level_match_finding`. Only that row and those two recipes were re-derived
that pass. **Scope: only the paragraphs named above were re-verified this
pass**; the rest of the live spine carries
its 2026-08-16 reading, and the campaign record's dated narrative was NOT re-verified
and still shows the pre-#2602 five-row table, as its own status callout says
it will)
