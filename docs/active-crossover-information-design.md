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

The page should help them answer five questions, in order:

1. What am I building?
2. What drivers am I using?
3. What DAC output goes to each driver?
4. Is JTS ready to start quietly?
5. Can I try one driver at the quietest possible level?

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

### 2. Add Driver Info

This card should be optional but valuable. Users can enter driver names, copy
a precise research prompt, paste JSON from an external assistant, and save a
design draft. The important distinction:

- **driver research warnings** are review notes, not errors
- **backend safety gates** can prevent staging or playback, but should not prevent a
  no-audio crossover preview from being shown

The preview is a thinking artifact. It may show a useful crossover candidate
even while the system still refuses to stage or play sound.

Good language:

- "Review notes"
- "No filters are applied"
- "Prepare no-audio preview"

Avoid:

- painting LLM warnings red
- saying only "blocked" when the real action is "confirm output" or "test
  this driver"
- making the user wonder whether their pasted JSON was accepted

### 3. Confirm Outputs

This card should connect the abstract layout to the physical world. The user
needs to know:

- which DAC output is assigned to each role
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
  audibility; the UI should raise toward audible in meaningful bounded steps,
  with a future continuous ramp only after the playback backend is cancellable
  mid-tone

### 4. First Quiet Test

This card should not read like a safety audit. It is where the backend enforces
the real gates, but the visible interface should explain the next action
instead of reporting machine states.

The backend still follows a deterministic sequence:

1. Check the saved setup.
2. Build the quiet test DSP setup.
3. Check the quiet test path.
4. Load the quiet test setup.
5. Open a bounded test session.
6. Check a target.
7. Start at the quietest setting.
8. Confirm the selected driver was heard.
9. Raise toward audible in bounded steps.

Those backend steps should not be shown as a grid of product cards. The normal
UI should show one next action at a time: set up quiet test mode, choose the
first driver, start at the quietest level, then record what happened.
Detailed gate evidence belongs in diagnostics or error details, not the primary
setup walkthrough. The UI should name the exact driver being tested before any
sound can play, and should tell the user to press Stop if the wrong driver,
silence, or too much level appears.

The user should understand that loading the test setup reloads DSP but does not
play sound, and that the first audible test starts at the quietest setting.

Good language:

- "Prepare first quiet test"
- "Set up quiet test mode"
- "What did you hear?"
- "Too loud" as an always-available operator answer

Avoid:

- dense diagnostic payloads as primary copy
- exposing machine status names without translating them
- presenting "playback allowed: no" before explaining what unlocks it
- showing backend checklists or grids in the primary flow

## Current Product Gaps

- The driver-info card has historically conflated LLM warnings with
  errors. Warnings should be visually amber/review-oriented; only real safety
  blockers should read as dangerous.
- A saved design draft can be blocked because safety evidence is incomplete,
  even when the driver research is accepted. The UI should still allow a
  no-audio crossover preview so the user can see what JTS is thinking.
- The copy should prefer human tasks over backend nouns:
  "speaker layout" over "output map"; "confirm outputs" over
  "verification evidence"; "first quiet test" over "stage/load/startup"
  unless the detailed backend state is being inspected.
- Saved state should advance the walkthrough. If layout, driver research, or
  output identity evidence already exists from an earlier session, the cards
  should show that work as complete and open the first unfinished card. The
  user should not have to re-click saved choices to make the UI believe them.
- Backend `blocked` is a safety decision, not default product copy. In early
  cards, translate it to the relevant human task ("test this driver",
  "save driver research", "choose one driver"). Reserve stronger
  failure language for an explicit unsafe action attempt or a failed probe.
- The microphone path should become more explicit before audible testing:
  "mic recommended for guided level" with a conservative manual path for users
  who are intentionally proceeding without mic feedback.
- "Raise toward audible" should be product language for one backend-bounded
  step. A true 5-15 second rising tone needs a playback backend that owns the
  running process and can stop immediately; do not fake that interaction with
  the current synchronous one-shot tone backend.

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

Last verified: 2026-06-12
