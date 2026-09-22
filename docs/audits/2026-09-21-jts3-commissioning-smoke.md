# JTS3 speaker commissioning smoke test

Frozen report: 2026-09-21, America/New_York. Tested the live browser flow at
`http://jts3.local/sound/speaker/`, on deployed build
`88551523d41cd3722e6576a8bc2f5b4b9acbf657`. No product code was changed or deployed.

## Result

**The browser can save and apply a base speaker configuration, but the main
next action sends the user into measurement before that save.** The base apply
control works when the user opens the later, collapsed profile step.

Completed: backup, browser reset, empty driver form, mono/cardioid layout,
driver research, fenced JSON import, save, base apply, live-state check, tuning
prompt inspection, and restoration. Stopped at the linearization entry page.
No sweep, seat-level ramp, linearization, or listening test was run. This proves
configuration and runtime loading, not measured sound quality.

## Original state and restoration

- HiFiBerry DAC8x; miniDSP UMIK-2 detected by the page.
- Mono active two-way plus rear woofer: output 1 front woofer, output 2
  B&C DE250-8, output 3 rear woofer. Both woofers were declared Epique E150HE-44,
  sealed, with no pad. The compression driver also had no pad.
- Original crossover: 2.5 kHz Linkwitz-Riley, 24 dB/octave. Front woofer trim
  0 dB with 0.186 ms delay; tweeter trim −21.3 dB. Existing driver correction,
  rear processing, and bass extension were preserved in the backup.
- Volume: 70%, reported by `/state` as −15.15 dB. Flat preference EQ.
- Original loaded graph: `active_speaker_baseline_candidate_48676134ca08.yml`.

Local backup and test evidence: `logs/jts3-commissioning-smoke-20260921/`.
The backup archive contains the speaker JSON settings and CamillaDSP files;
it excludes credential compartments and unrelated account data.

Backup SHA-256:
`592a33170694712ba912470ae9728310ca4c3304243761540d3c819266abeb32`.

Restoration was checked in both the browser and `/state`. Seven file hashes
matched the backup: topology, design draft, applied profile, sound settings,
sound profile, speaker volume, and the original loaded graph. The loaded path
and volume also matched. CamillaDSP, outputd, and fan-in were active; outputd
reported no deaf state, and clipping remained zero. The bounded doctor run
exited 0: 161 passed checks and one non-critical Google Routes setup warning.

The test-only candidate and campaign were moved out of the active directories
to `/var/tmp/jts3-commissioning-smoke-20260921/` on JTS3. Their local evidence is
in `after-base/`. Original tune history remains in place.

## What worked

1. Reset cleared the saved layout, driver fields, and applied profile. The
   speaker parked with audio muted. Logs reported `cleared=3 missing=5 errors=0`.
2. Mono → Active 2-way → Cardioid gave the correct three outputs. The channel
   selectors remained available. No-pad defaults matched the build.
3. The research prompt named all three targets. A newly researched reply in
   the requested JSON code fence imported successfully on the first attempt.
4. Save values accepted the researched limits and crossover. Save and apply
   then loaded a new graph and reported success.
5. The graph had a 2 kHz Linkwitz-Riley fourth-order split, 0 dB woofer trim,
   −25.2 dB tweeter trim, zero timing offsets, and a muted rear output. It had
   no linearization or rear calibration. Volume limit remained 0.0 dB and
   driver limiters remained present.

The test used manufacturer data from the
[Dayton E150HE-44 datasheet](https://www.daytonaudio.com/images/resources/295-102--epique-e150he-44-spec-sheet.pdf)
and [B&C DE250 datasheet](https://www.bcspeakers.com/en/products/hf-driver/1-0/8/de250.pdf).
The 2 kHz split was an explicitly estimated starting point. The −25.2 dB trim
matches the published sensitivity difference, 83.3 versus 108.5 dB. Installed
coil wiring, amplifier gain, and horn model remained unknown; no claim of
measured level matching was made. Sealed loading came from the saved build.

## Findings

| Priority | Observed behavior | User impact |
| --- | --- | --- |
| High | After Save values, the primary action is **Run speaker experiment**. **Save and apply** is in the later collapsed profile step. | The working base-save path is hidden behind the step the user is told to do first. |
| High | Applying the unmeasured base marks **First speaker experiment** complete. The measurement page says **Automatic crossover applied**. | The UI claims progress and automation that did not occur. |
| High | **Other programs** opens to an empty body immediately after apply. Reloading exposes rear, bass, and room prompts. | The next tuning choices appear broken at the exact handoff point. |
| Medium | Saving basic model/loading/no-pad details succeeds as `needs_research`, but shows ten missing measurement/safety-field messages. The top action remains **Save values**. | A normal intermediate save looks like a failed setup and does not guide the user to research. |
| Medium | The generated research targets contain model, role, and tweeter style, but omit the selected enclosure and pad fields. | Physical facts entered in the form do not fully reach the research prompt. This run repeated them in build notes. |
| Medium | Datasheet-derived starting trims are labelled **Manual** and **manually pinned**. The page also uses **What JTS is running with** beside **No filters are active yet**. | Draft, estimated, measured, and currently active values are hard to tell apart. |
| Medium | A three-channel cardioid build requires **Active 2-way** plus a checkbox. **Active 3-way** means woofer/mid/tweeter. | Channel count and acoustic-way count need distinct labels. |
| Medium | The next stage shows console commands, seat-level leveling, and a pose-set selector with 23 choices across several programs. | The default path exposes tool details before the user needs them. |
| Low | Driver-level guidance hard-codes `jts.local/sound/speaker/crossover` on JTS3. | The text points at the wrong speaker host. |

The generated tuning prompt specifies speaker → rear → bass → room. The
requested order was speaker → rear → room → bass. This is a flow decision to
settle in the next plan, not a change made by this test.

Clipboard limit: the page reported successful copies and contained the complete
prompt text, but the browser tool's virtual clipboard remained empty. Its paste
action failed with “virtual clipboard has no data to paste.” Prompt generation
was verified; native clipboard success was not. The prompt was read from the
page to continue the requested JSON test. Do not treat this alone as a confirmed
product clipboard defect.

## Evidence and code pointers

- `03-save-before-research.txt`: basic save followed by ten later-field messages.
- `05-saved-driver-values.txt`: experiment is the next action after research save.
- `06-base-apply-step.txt`: working Save and apply button in the later step.
- `07-base-apply-result.txt` and `08-active-base-profile.txt`: successful base
  apply, experiment checkmark, and trim labels.
- `09-tuning-handoff.txt` and `10-other-programs-after-reload.txt`: empty menu
  after apply versus populated menu after reload.
- `11-linearization-entry.txt`: “Automatic crossover applied” and pose choices.
- `commissioning-events.log`: `needs_first_experiment` at 20:13:29, followed by
  successful base apply at 20:14:10, with no measurement between them.
- `restore-verification.txt`, `state-restored.json`, and
  `doctor-restored.txt`: restoration checks.

Source inspection explains the key transitions:

- `jasper/active_speaker/commissioning_coordinator.py`,
  `build_commissioning_view`: chooses the experiment before the base save and
  treats an applied profile as a completed experiment.
- `deploy/assets/sound-profile/js/main.js`, `renderOutputTopologyBody` and
  `renderBaselineProfileCard`: the later profile section can apply the base.
- The same file's `saveAndApplyBaselineProfile` replaces the profile from the
  apply response. `renderTuningHandoffCard` reads `tuning_programs` there, while
  `jasper/web/sound_active_speaker.py` adds that list only to the profile GET
  response. This explains why reload repairs the empty menu.
- `jasper/active_speaker/driver_safety.py`, `build_driver_research_context`:
  research context contains models, styles, roles, and notes, without the form's
  enclosure and pad settings.
- `deploy/assets/sound-profile/js/active-speaker-ui.js`: manual-level guidance
  contains the fixed `jts.local` hostname.

The standard log-fetch command collected service journals and the combined log,
then stalled before the reboot summary. It was stopped. A focused setup-event
read supplied the evidence above. No GitHub issues were published in this run.

## Direction for the next plan

Make base setup its own short flow: layout → driver details → research prompt
→ paste result → review a small base summary → save to speaker. Then show
separate tuning programs. Keep full values and manual editing under Details.
The base apply already works; the main gap is how the UI orders and labels it.
