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
4. Can I measure each driver safely?
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
  while Stop and "I hear the tone" remain live for the continuous tone session

### 4. Measure Drivers

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
sound can play, and should tell the user to press Stop if the wrong driver,
silence, or too much level appears.

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
- "Start quiet woofer test"
- "Tone playing at 250 Hz"
- "What did you hear?"
- "Too loud" as an always-available operator answer

Avoid:

- dense diagnostic payloads as primary copy
- exposing machine status names without translating them
- presenting "playback allowed: no" before explaining what unlocks it
- showing backend checklists or grids in the primary flow

### 5. Validate Crossover Blend

After each driver has a saved per-driver check, the user needs one summed check
per active speaker group. The per-driver check can be an operator-only guarded
tone confirmation; it is not the later acoustic measurement flow. The UI should
use that durable driver-check state, not volatile ramp ordering memory, when it
decides whether "Test each driver" is complete. The summed check is still a
guided setup action, not a lab report: the UI should say which drivers will be
heard together and ask whether the blend sounds coherent at the crossover
region. Polarity and delay are technical implementation details, but they can
be captured as simple outcomes when the user hears a problem. The combined
test also needs a bounded level control for low-sensitivity drivers: the user
can raise the next play from the quiet floor, while the backend still limits
upward motion and logs the emitted level. For the current product path, the
user's explicit listening result can unlock the first baseline after a current
audible combined test; phone-mic summed capture remains richer acoustic
validation rather than a prerequisite for the household flow.

Good language:

- "Check the crossover blend"
- "Blend sounds right"
- "Sounds hollow or weak"
- "Needs level or delay adjustment"

Avoid:

- "in-phase sum" as the primary button label
- asking the user to choose a delay before they know what problem they heard
- treating this as a replacement for future acoustic measurement automation

### 6. Validate And Apply

The final card should make the handoff explicit: JTS has a measured setup,
checked the combined speaker, compiled the baseline speaker profile, and the
user is choosing whether to make it active. The card owns three separate user
actions:

- **Play combined test** runs a short, quiet combined-driver test from the
  prepared crossover setup at the selected bounded test level.
- **Blend sounds right** records the user's combined crossover validation; it
  must be tied to the latest audible combined test and is stored as an explicit
  operator listening check when no microphone reading is present.
- **Save active profile** writes the candidate CamillaDSP YAML and durable
  profile state, but does not load it.
- **Apply active profile** loads that profile through the normal DSP apply
  transaction when this hardware path supports it.

If apply is not supported for the current hardware path, the card should say
that plainly: the profile can be saved for review, but JTS cannot switch normal
playback to it from this page yet.

Good language:

- "Validate and apply"
- "Play combined test"
- "Save active profile"
- "Apply active profile"
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
- Driver measurement should stay explicit: recording a correct-driver result
  with mic/calibration evidence is what lets the flow move from individual
  driver setup to summed crossover validation. That evidence must be tied to
  the exact saved output target that was tested; if the user changes the speaker
  layout or DAC assignment, the old measurement can remain visible as history
  but should no longer satisfy the current step.
- "Raise toward audible" should be product language for one backend-bounded
  step. A true 5-15 second rising tone needs a playback backend that owns the
  running process and can stop immediately; do not fake that interaction with
  the current synchronous one-shot tone backend.
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
  speaker group; a free-floating "sounds good" click should never unlock the
  active profile.
- The final apply step should use product language, but the backend must still
  write through the shared DSP transaction, retain rollback visibility, and
  respect outputd ownership. Direct single-DAC hardware baselines can be
  compiled for inspection, but should not be applied until the outputd handoff
  exists.

Last verified: 2026-06-18
