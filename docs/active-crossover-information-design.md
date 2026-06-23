# Active Crossover Information Design Notes

Status: working product-design note, created 2026-06-12.

This note captures the current information-design direction for the
active crossover / multi-amp speaker setup flow. It is intentionally
product-facing: the backend safety contracts remain canonical in
[`HANDOFF-active-speaker-dsp.md`](HANDOFF-active-speaker-dsp.md), but this
file keeps the user journey and language choices easy to revisit.

## User Goal

A user has a DAC or amplifier with multiple outputs and wants to wire one or
more speakers safely. They may be building a passive speaker, a bi-amped
active 2-way speaker, a tri-amped active 3-way speaker, and optionally a
subwoofer. They may know the driver data, or they may need help collecting it.

The page should help them answer six questions, in order:

1. What am I building?
2. What drivers am I using?
3. What DAC output goes to each driver?
4. Can I hear each driver on the intended channel?
5. Does the crossover blend when drivers are summed?
6. Is this ready to become my active speaker profile?

The page should not feel like a lab console. It should feel like a calm setup
walkthrough where the system is conservative, specific, and honest about what
is still unknown.

## Preferred Flow

### 1. Choose Speaker Layout

The first card should open by default. It should ask what the user is wiring:

- mono or stereo
- passive, active 2-way, or active 3-way
- optional subwoofer

Hardware detection belongs here as supporting context, not the main action.
Refresh hardware is useful when someone plugged in a DAC after opening the
page, but it should not be a dominant call to action.

Good language:

- "Choose speaker layout"
- "Find hardware"
- "Save and continue"
- "No sound will play"

Avoid:

- "Output map" as user-facing copy
- "Blocked" before the user has done anything wrong
- large hardware/status CTAs before the user understands the layout task

### 2. Add Crossover Settings

This card is where the user sees the actual values JTS will use for the
no-audio crossover preview. It should not hide the plan inside an AI import.
Users can type the values directly, or they can open the optional AI helper to
fill the same visible fields for review.

Product-wise, this card is one current **working setup**, not a document
manager. JTS may persist an internal design draft so the form survives crashes
or page reloads, but the UI should not present "saved drivers" or "saved
crossover settings" as user-facing records. Reserve "saved" for production
artifacts such as the active speaker profile that can later be applied.

Visible settings should include the practical starting values:

- driver name/model for each role
- sensitivity, when known
- safe low test limit for high-frequency drivers
- per-driver level trim for large sensitivity mismatches
- crossover point, filter family, and slope for each active split
- build notes that help interpret the setup

The important distinction:

- **AI research warnings** are review notes, not errors
- **visible manual settings** are the source of truth for preview/staging input
- **backend safety gates** can prevent staging or playback, but should not prevent a
  no-audio crossover preview from being shown

The preview is a thinking artifact. It may show a useful crossover candidate
even while the system still refuses to stage or play sound.
Preparing a preview can update/overwrite the internal working setup first; the
user should not have to learn a separate "save settings" step before they can
see the no-audio plan.

Good language:

- "Working setup"
- "Crossover settings"
- "Update working setup"
- "Use AI to fill these settings"
- "Review notes"
- "No filters are active yet"
- "Prepare no-audio preview"
- "Ready to preview"
- "Needs crossover info"

Avoid:

- painting LLM warnings red
- making the AI JSON the only way to set crossover values
- saving hidden imported values over user-edited visible fields
- making the user wonder whether imported JSON filled the visible settings
- "N saved drivers" / "N saved crossover settings"
- "Saved crossover settings" for the working form

### 3. Confirm Outputs

This card should connect the abstract layout to the physical world. The user
needs to know:

- which DAC output or amp channel is assigned to each role
- which physical driver that output feeds
- whether the user has confirmed the wire for each output
- which confirmed driver they are about to test first

The product has to work for users who do not have capacitors or lab hardware.
Physical protection can be represented when present, but the normal path is
software-backed: muted startup, protective high-pass for high-frequency
drivers, hard limits, quietest start level, Stop available, and explicit operator
confirmation. Do not make "protection" or "software guard" a normal-path user
choice. A user who knows how to add external protection will usually add it
without being taught by JTS; the primary product job is to make the
software-backed path conservative, understandable, and hard to accidentally
bypass.

Good language:

- "Confirm outputs"
- "Confirm output"
- "DAC channel"
- "Choose woofer · Output 1"
- "JTS will start this driver at the quietest level"

Avoid:

- implying physical capacitors are required for everyone
- letting "confirmed" sound like permission to play sound by itself
- showing "hardware protected" and "software guard" as equal novice choices
- showing "protection" as a button or separate task
- exposing "check readiness" as a user action; the user chooses a driver and
  JTS checks readiness internally
- forcing the user to raise a level one decibel at a time just to discover
  audibility; the UI should raise toward audible in meaningful bounded steps
  while one stable Play/Stop control and "I hear the woofer/tweeter" remain
  visible for the continuous tone session

Channel assignment belongs in this card. The user must be able to choose the
physical DAC output for each driver. If there are exactly two outputs and two
drivers, choosing one may auto-fill the other; with three or more outputs the
flow should require explicit unique assignments before save.

### 4. Test Drivers

This card should not read like a safety audit. It is where the backend enforces
the real gates, but the visible interface should explain the next action
instead of reporting machine states.

The backend still follows a deterministic sequence:

1. Check the saved setup.
2. Build the safe test DSP setup.
3. Check the safe test path.
4. Load the safe test setup.
5. Open a bounded test session.
6. Check a target.
7. Start at the quietest setting.
8. Confirm the selected driver was heard.
9. Raise toward audible in bounded steps.

Those backend steps should not be shown as a grid of product cards. The normal
UI should show one next action at a time: choose the first driver, let JTS get
the safe audio path ready behind the scenes, start at the quietest level, then
record what happened.
Detailed gate evidence belongs in diagnostics or error details, not the primary
setup walkthrough. The UI should name the exact driver being tested before any
sound can play, hold the primary control in a disabled "getting ready" state
while the backend prepares the path, then turn it into Stop only once the tone
session is actually active.

The user does not need to understand "quiet test mode" as a separate setup
task. Once outputs are confirmed, driver choices should be visible. Clicking
one should record the normal software guard for any missing high-frequency
outputs in the saved active graph, refresh any stale no-audio crossover preview
when the working setup can produce one, then run the backend setup sequence; if
JTS cannot get the safe audio path ready, the card should explain the
product-level problem and confirm that no sound played. A user should not be
asked to click "set up test mode," "prepare preview," or interpret backend
evidence labels before choosing the driver they want to hear.

Good language:

- "Test each driver"
- "Play woofer"
- "Tone playing at 250 Hz"
- "I hear the woofer"
- "Back to configuration"
- "Stop"

Avoid:

- dense diagnostic payloads as primary copy
- exposing machine status names without translating them
- presenting "playback allowed: no" before explaining what unlocks it
- showing backend checklists or grids in the primary flow
- extra outcome buttons such as "wrong driver" or "too loud"; Stop and Back to
  configuration are the recovery actions

### 5. Validate Crossover Blend

After each driver has been confirmed on its own, the user needs one summed check
per active speaker group. This is still a guided setup action, not a lab report:
the UI should say which drivers will be heard together, play the combined test,
then have the user confirm the blend. The core `/sound/` flow should not try to
troubleshoot every bad blend outcome. If the user is unsure, they go back to
the crossover configuration; deeper microphone-based level/delay work belongs
in the HTTPS measurement flow.

The combined test needs a clear bounded level control for low-sensitivity
drivers. The user controls the next play across the full commissioning envelope
and starts low; the backend still enforces absolute min/max safety bounds and
logs the emitted level.

Good language:

- "Check the crossover blend"
- "Play combined test"
- "Sounds right"
- "Back to adjust crossover"

Avoid:

- "in-phase sum" as the primary button label
- asking the user to choose a delay before they know what problem they heard
- in-flow phone-mic capture; link to the HTTPS measurement experience after the
  basics are complete
- negative troubleshooting CTAs such as "sounds hollow" or "needs adjustment"

### 6. Validate And Apply

The final card should make the handoff explicit: JTS has a checked setup,
checked the combined speaker, compiled the baseline speaker profile, and the
user is choosing whether to make it active. It should have one primary action:
**Save and apply**. The frontend sends that intent; the backend writes the
candidate profile and applies it through the normal DSP transaction when this
hardware path supports it.

If apply is not supported for the current hardware path, the card should say
that plainly: the profile can be saved for review, but JTS cannot switch normal
playback to it from this page yet.

Good language:

- "Validate and apply"
- "Save and apply"
- "This is now your active speaker profile"

Avoid:

- "baseline candidate" as primary product copy
- implying that a saved draft or preview is already live
- hiding which values will be compiled into the speaker profile

## Current Product Gaps

- The driver-info card has historically conflated LLM warnings with
  errors. Warnings should be visually amber/review-oriented; only real safety
  blockers should read as dangerous.
- The internal design draft can be blocked because safety evidence is incomplete,
  even when the driver research is accepted. The UI should still allow a
  no-audio crossover preview so the user can see what JTS is thinking.
- The copy should prefer human tasks over backend nouns:
  "speaker layout" over "output map"; "confirm outputs" over
  "verification evidence"; "measure drivers" over "stage/load/startup" or
  "quiet test mode" unless the detailed backend state is being inspected.
- Saved state should advance the walkthrough. If layout, driver research, or
  output identity evidence already exists from an earlier session, the cards
  should show that work as complete and open the first unfinished card. The
  user should not have to re-click saved choices to make the UI believe them.
- Backend `blocked` is a safety decision, not default product copy. In early
  cards, translate it to the relevant human task ("test this driver",
  "save crossover settings", "choose one driver"). Reserve stronger
  failure language for an explicit unsafe action attempt or a failed probe.
- Driver checks in the core `/sound/` flow are now explicit by-ear confirmations:
  "I hear the woofer/tweeter" advances the walkthrough, while output-channel
  changes clear the stale identity evidence. Mic/calibration evidence should
  remain tied to the exact saved output target when it is exposed in the separate
  HTTPS measurement experience.
- Phone-mic capture exists in backend endpoints and the shared measurement core,
  but it is no longer part of the core `/sound/` active-crossover path. The next
  product step is to mount that experience under the HTTPS measurement/correction
  framework so browser mic permissions, calibrated-mic guidance, and acoustic
  proof live together.
- "Raise toward audible" should be product language for a user-controlled level
  slider and a backend-bounded test. Running tones need a playback backend that
  owns the process and can stop immediately; do not fake that interaction with a
  synchronous one-shot tone.
- The profile compiler should surface the user-owned values it consumed:
  crossover settings, trims/attenuation, measured driver evidence, and summed
  validation. Hidden JSON import or backend safety evidence must not be the only
  explanation of why a profile was accepted or rejected.

## Design Principles

- One primary open card at a time.
- Earlier cards stay editable.
- Every "Next" button should either save the current card or explain exactly
  what is missing.
- Warnings help the user review; blockers prevent unsafe side effects.
- No-audio previews are allowed to be informative even when later staging or
  playback is blocked.
- No-audio preview cards should show preview readiness and review notes, not
  future safety blockers that can only be resolved in later cards.
- Safety gates should remain backend-owned and deterministic.
- The UI should never imply that an LLM response, a saved draft, or a
  crossover preview applies filters or authorizes sound.
- Measurement evidence, combined-driver test evidence, summed validation,
  compile, and apply are distinct product states. Do not collapse them into
  one "done" flag.
- Summed validation must be tied to the current combined-driver test for that
  speaker group; a free-floating "sounds good" click must never unlock the
  active profile.
- The final apply step should use product language, but the backend must still
  write through the shared DSP transaction, retain rollback visibility, and
  respect outputd ownership. Direct single-DAC hardware baselines can be
  compiled for inspection, but should not be applied until the outputd handoff
  exists.

Last verified: 2026-06-18
