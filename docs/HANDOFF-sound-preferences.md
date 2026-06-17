# Handoff: Sound Preferences

Current operational truth for the `/sound/` preference-EQ layer.

## Status

The sound-preference wizard is the independent preference-tuning layer
for users who want to shape the speaker without running room
correction. It lets users apply stock sound curves, a five-band Simple
EQ (Sub-bass / Bass / Mid / Presence / Treble), bounded advanced PEQ
bands, and named custom profiles copied from any stock or edited draft.
It is deliberately separate from `/correction/`:

- `/correction/` measures the room and emits room PEQs.
- `/sound/` applies user preference shaping after those room PEQs.
- The combined CamillaDSP config preserves ordering:
  room-correction room-chain filters first, preference EQ second, final
  `flat` terminator last. As of 2026-06-13, the room-chain segment may
  also include gainless per-channel `Delay` filters for leader-owned
  stereo-pair time-of-arrival calibration; that is correction/sync
  calibration state, not a preference-EQ control. When a room measurement
  applies *boosts* (the assertive strategy only — the cuts-only default has
  none), a single `room_headroom` attenuator rides between the room filters
  and the preference tail so the boosted bands cannot clip (see "Gain
  staging — boosts boost" below).

While a speaker is an active bonded follower, `/sound/` is a delegated
surface: the page shows a leader-owned notice and does not load the
preference-EQ editor. Content-DSP mutation routes reject with HTTP 409
instead of changing local sound state (`/sound/apply`, `/sound/audition`,
`/sound/live-draft`, `/sound/settings`, and the profile-library writes).
This is intentional: the leader renders content EQ, room correction, and
volume shaping for the paired image. Local active-speaker/driver-DSP
routes remain separate because crossover and driver protection belong to
the box that owns the DAC path.

The advanced parametric editor is intentionally touch-first: users
adjust filter type, frequency, gain, and Q/width with controls while the
graph keeps the single total-response curve visually dominant. Each
enabled band is one dot anchored **on** that summed curve (the curve's
own value at the band's frequency), so the dot sits on the line for every
filter type — including shelves and cuts, whose response at the corner
frequency is not the band's nominal gain. The graph draws exactly **one
line** — the summed response — in every state. The expanded band is
marked by its filled on-curve dot, a frequency guide line, and (for
peaking filters) a translucent width region; it does **not** draw a
second per-band response curve. Collapsed/non-selected bands are just the
on-curve dot. Dragging points on the graph is deferred; the graph is a
display surface, not the state authority.

Available band types (all CamillaDSP biquads): **Lowshelf**, **Peaking**,
**Highshelf**, **Highpass**, **Lowpass**, **Notch**. The three cut/notch
types carry no gain — the UI hides the Gain control, the model pins gain
to 0, and the emitted YAML omits the gain term. Shelves hide the Width
control (their slope is fixed at 6 dB/oct in the emit). High/low-pass Q is
capped at `CUT_MAX_Q` (1.4) in both the model and the slider, because a
high-Q cut is a large resonant *boost* at the corner (a Q=8 highpass peaks
~+18 dB) — surprising on a "pass" filter and a needless clipping source;
Notch keeps the full Q range since it is meant to be surgical. Switching a
band into high/low-pass snaps Q to Butterworth (0.707). The graph draws
real RBJ-cookbook biquad magnitude evaluated at CamillaDSP's 48 kHz rate
(`jasper/sound/profile.py` `_biquad_coeffs`, mirrored by
`deploy/assets/sound-profile/js/eq-math.js`), so the line matches the
speaker's actual output.

The Draft editor has two exclusive modes:

- **Simple** — five fixed-frequency gain sliders (Sub-bass 60 Hz,
  Bass 150 Hz, Mid 1 kHz, Presence 4 kHz, Treble 10 kHz). Only gain is
  editable per band; the slot table is `SIMPLE_BANDS`
  (`jasper/sound/profile.py`) and is exposed to the UI via
  `/state.limits.simple_bands`, so the page renders the columns from
  data rather than hardcoding band labels.
- **PEQ** — bounded parametric bands (a collapsible accordion) with
  exact Hz entry plus a log-frequency slider for fast touch adjustment.

Switching modes converts the draft rather than warning: PEQ → Simple
snaps each Simple slot to the nearest enabled PEQ band by log-frequency
(within ~1.2 octaves); Simple → PEQ turns each non-zero Simple band into
a parametric band. This replaces the older Basic/Advanced "mixed
profile" compatibility shim. Older 3-band profiles still load — the two
new Simple bands (Sub-bass, Presence) default to 0 dB — but note the
redesign moved the Bass centre (105 → 150 Hz) and the Treble shelf
(4 k → 10 k), so a migrated profile's bass/treble values shape slightly
different frequencies.

The **Saved** tab lists profiles in two sections:

- **Presets** are generated from built-in curves: Flat, Harman-style,
  and B&K-style. They are not persisted or editable, but can be opened
  in Draft (pencil) and saved as a new custom profile.
- **Your profiles** are custom profiles in
  `/var/lib/jasper/sound_profiles.json`. Each row has edit (pencil) and
  delete actions; the Draft footer adds Overwrite / Save as new /
  Rename. Custom profile library edits do not touch CamillaDSP; ordinary
  draft editing may separately update the live Draft through
  `/sound/live-draft`.

`SoundProfile` includes optional `profile_id` / `profile_name` metadata
so the UI can distinguish "applied Flat" from "draft edited from Flat"
without making the metadata part of the DSP math. Stock identities are
`stock:<curve_id>`; custom identities are `custom_<12 hex chars>`.
Deleting a custom library entry does not delete the currently applied
DSP profile; it only removes that profile as a future draft template.

The top control is **Off / Saved / Draft**, and it is the live source:

- **Off** — preference EQ disabled (bypass) while preserving room
  correction. Clicking Off durably applies a bypassed profile.
- **Saved** — the saved-profile listening lane. Entering Saved
  durably applies the currently selected saved profile; if there is no
  remembered selection, it falls back to the Flat preset. Tapping a row
  changes that remembered Saved selection and applies it immediately.
- **Draft** — the editor. Dragging a control schedules a live Draft
  update: the browser updates the graph immediately, coalesces audio
  updates, and asks `/sound/live-draft` to upload a generated active
  CamillaDSP config without changing the config file path or persisting
  profile state. Each live request carries the current durable DSP write
  epoch from `/var/lib/jasper/dsp_apply_state.json`; if a save or
  room-correction apply wins the writer lock first, the stale live
  request is skipped.

Off and Saved are **durable** (they go through `/sound/apply`); Draft is
a live, non-persistent preview until the footer Save commits it. The top
tabs are therefore A/B/C listening controls, not passive navigation:
Off bypasses, Saved resumes the saved reference profile, and Draft resumes
the unsaved working profile. The browser treats those live-source changes
as last-intent-wins: if a durable apply is already in flight, the newest
Off/Saved/Draft intent is replayed after the apply returns, so quick A/B
taps cannot leave the graph on one source while the speaker keeps playing
another. The
Draft footer adapts to origin: a new draft offers Save profile /
Discard; editing a custom profile offers Overwrite / Save as new /
Rename / Discard; editing a preset offers Save as new / Discard. Saving
emits `sound_current.yml` and persists only after the CamillaDSP reload
is confirmed.

### Gain staging — boosts boost

A preference boost applies at unity: a +N dB band raises only that band
and leaves the rest of the spectrum untouched, the way a consumer EQ
behaves. The generated config inserts **no** automatic preamp for
preference boosts. The only global attenuation for the preference layer
is an opt-in **output trim**, which is 0 by default, so the default is
"boosts boost". The `devices.volume_limit: 0.0` master ceiling remains
the hard clip guard, so removing the old preamp cannot raise the output
ceiling — at high volume a large boost clips at 0 dBFS rather than
ducking the whole mix.

**Room-correction boosts are the exception — they are headroom-compensated
automatically.** The assertive correction strategy (`cuts_only=False`) can
emit room PEQs with up to +3 dB total boost. Unlike preference boosts,
those bands cannot be left to clip: `_emit_filter_definitions` derives a
`room_headroom` preamp from the worst-case additive room boost (the sum of
positive room-PEQ gains, an upper bound on the combined peak) and
attenuates the whole signal by it, so a corrected room boost can never
exceed unity. Cuts-only correction (the safe/balanced default) has zero
boost and emits no `room_headroom`, keeping the config byte-identical.
This is deliberately separate from `output_trim_db`: that trim compensates
only the preference layer and is skipped on a flat sound profile, so it
would not protect room boosts on a household that has set no preference EQ.

Two opt-in, default-off global settings (distinct from per-profile EQ)
feed that trim. They are owned by `/sound/` at
`/var/lib/jasper/sound_settings.json` (`jasper/sound/settings.py`),
fail-soft to the do-nothing defaults — a missing or corrupt file never
alters the sound:

- **Match loudness** — turns each profile down by its loudness-weighted
  gain (`loudness_compensation_db`: a pink/K-weighted power average of the
  EQ response, **not** peak, so a narrow +8 dB band compensates ~1 dB, not
  8) so switching profiles compares tone, not volume. Anchored to
  attenuation (≥ 0), so it can never cause clipping.
- **Extra headroom** — a manual 0–12 dB digital attenuation
  (`headroom_trim_db`, clamped) for listeners running JTS at full digital
  volume into their own amplifier.

The emitter applies one `output_trim_db = headroom_trim + (loudness
compensation when match-loudness is on)` as the `sound_preamp` gain, and
only when the active profile actually has filters (a flat profile can't
clip from EQ). `estimate_headroom_db` remains as the peak-boost *metric*
surfaced by doctor / `control` `/state` / the calibration advisor; it no
longer drives an auto-preamp.

The `/sound/audition` endpoint and `sound_audition.yml` remain in the
backend as a validated, non-persisting apply: it writes a separate
`sound_audition.yml` through the same validation/rollback substrate as
`/sound/apply`, without persisting the profile. The redesigned UI no
longer drives it — editing previews through the faster `/sound/live-draft`
path and commits through durable apply — but it is intentionally retained
as the validated-preview surface for "propose, preview, then approve"
AI helper flows. As of 2026-05-31,
`jasper.calibration_agent.sound_actions` can opt into that exact backend
for validated advisor `propose_preference_eq_audition` actions. The CLI
flag is `jasper-calibration-agent --call-advisor --audition-sound` (or
`--run-advisor-actions <path> --audition-sound` for offline response
fixtures). This remains reversible: no profile persistence, no volume
authority, and the action result reports the generated config basename
rather than exposing raw filesystem paths.

### Advanced speaker setup entry point

As of 2026-06-02, `/sound/` also shows a collapsed **Advanced speaker
setup** entry point for active crossover commissioning. Opening it shows one
primary **Active crossover setup** walkthrough, not a separate environment
card. The walkthrough keeps one task card open at a time: choose speaker
layout, add crossover settings, confirm outputs, test each driver, validate
the summed crossover, then save the active profile. Layout and
crossover-settings steps do not play sound, load CamillaDSP, or touch live
audio; detected hardware is supporting context and the hardware refresh control
is a small utility inside the layout step.

The **Test each driver** card owns the guarded driver-check controls.
The primary UI no longer refreshes the old backend checklist/grid or asks the
user to understand environment, path-safety, staging, startup-load, or
safe-session probes as separate steps. Instead it presents one user action at a
time: choose the driver to test, let JTS prepare the route internally, start a
bounded quiet tone, and say whether that named driver was heard. Driver-choice
buttons appear after outputs are confirmed; clicking one records the normal
software guard for any missing high-frequency outputs in the saved active graph
and refreshes a stale no-audio crossover preview when the saved crossover
settings can produce one. Coherent single-DAC topologies can then use a
bounded direct-DAC diagnostic route: outputd is paused for the short test,
the generated multi-channel WAV has only the selected output populated, and
outputd is restarted afterwards. Outputd-owned active-lane topologies keep the
protected staging/load/arm path. If setup fails, the card reports one
product-level issue and confirms no sound played. The UI never asks users to
click separate arm/stage/path controls. Stop remains a normal-sized,
idempotent control.

A prepared driver exposes **Start tone** rather than a manual volume slider.
For outputd-owned active-lane commissioning, the browser starts one continuous
quiet tone and asks the operator to press **I hear the tone** as soon as the
selected driver is audible; **Wrong driver** and **Stop tone** remain visible
beside it. Internally, `/sound/active-speaker/commission-ramp-step` still raises
the per-driver active graph only in bounded, guarded steps over about 30 seconds,
while the same cancellable `correction_substream` tone keeps playing. The tone
frequency is not role-hardcoded: it is planned from the same compiled
active-speaker preset/crossover edges and tweeter-protection policy as the graph,
and a missing/narrow safe band blocks playback before fan-in is selected. If the
safe limit is reached with no audible driver, the UI stops/re-mutes and tells the
operator to check amp gain, wiring, and DAC output mapping. The level state is
separate from normal listening volume; the older direct-DAC diagnostic route for
coherent single-DAC passive/full-range topologies still uses short bounded tests.
The route still accepts mic observations when available, but a correct-driver
operator result can prove physical routing identity without pretending the
acoustic response is fully measured.

When the operator records a correct-driver result for that same target,
`/sound/active-speaker/driver-measurement` persists target-specific
driver-check evidence in
`/var/lib/jasper/active_speaker_measurements.json`. That evidence is bound to
the current saved physical target fingerprint, including DAC output assignment
and identity confirmation, so changing the speaker layout makes old records
informational rather than ready-state proof. Driver-check evidence is not
normal playback permission; it is the durable proof needed before the active
baseline compiler can proceed.

Physical DAC lane assignment, speaker grouping, left/right
swaps, active driver roles, passive speakers, and subwoofer outputs now have a
no-audio backend contract:
`/sound/output-topology` reads/saves the complete
`jasper.output_topology` JSON model at
`/var/lib/jasper/output_topology.json`. That model evaluates identity and
tweeter-protection evidence but never rewrites ALSA, reloads CamillaDSP, emits
tones, or authorizes playback; the audible safe-session path remains separate.
The same `/sound/` card renders a lightweight **Active crossover setup**
surface over that endpoint as collapsible task cards: **Choose speaker
layout**, **Add driver and crossover info**, **Confirm outputs**, **Test each
driver**, and **Validate and apply**.
The layout card opens by default on page load. Explicit Next/manual-open
actions use transient browser intent only; no persisted wizard-progress state
exists. The UI keeps one card open at a time, prevents opening future
prerequisite-gated cards, and lets users reopen earlier cards to edit them. The
layout card starts with
no-audio setup templates for mono/stereo passive, mono/stereo active 2-way, and
mono/stereo active 3-way wiring, plus the subwoofer add-on; detected hardware is
shown as supporting context rather than the primary call to action. Subwoofer is not a
duplicated template family: the UI offers it as an optional add-on that composes
with the current mono/stereo draft when an unused physical output is available,
adds a `subwoofer` group, and records it in `routing.subwoofer_group_ids`.
The route capability in `/sound/output-topology` deliberately separates
physical DAC outputs from the active-speaker diagnostic/apply route. A DAC8x
topology can describe eight physical outputs, and a short direct-DAC diagnostic
test may use that physical width for one selected driver. Durable active-profile
apply is narrower: it is enabled only for a DAC profile that declares an
outputd-owned active lane. Today that product apply handoff is the measured
dual-Apple USB-C 4-channel profile, not HiFiBerry DAC8x or a single Apple USB-C
dongle. Subwoofer add-ons count as real assigned DAC outputs; a layout that
uses the next free output needs one more lane, and a sparse assignment needs
lanes up to the highest assigned output.
Saving a speaker-layout draft is a complete topology JSON
replacement and only runs backend validation; it does not play sound or change
the live DSP graph. The same payload carries a clock-domain report that records
the current single final-output device assumption; aggregating multiple USB DACs
is explicitly not enabled for product active-crossover playback yet. The
confirm-outputs card shows a top-down speaker sketch plus flat **DAC output
assignments**: each assigned physical output names the speaker/driver role it
feeds and whether the operator has confirmed the wire. Users can mark or clear
an assigned output as physically verified only after external wiring
inspection, dummy-load/DMM checks, or a future low-level channel test confirms
the driver. Identity evidence is stored in the topology contract, but it is
not playback permission and it does not satisfy tweeter protection or
path-safety blockers by itself.
The crossover-settings card stores the product-visible starting values for
active-crossover planning: driver names, sensitivity, safe low test limits,
per-driver level trim, crossover point, filter family, slope, and build notes.
It also offers an optional **Use AI to fill these settings** helper. That
helper derives the expected driver-role fields from the current output map,
generates a precise prompt for an external assistant, and accepts a bounded
JSON response with kind `jts_active_crossover_driver_research`. Importing
that JSON fills the visible fields for operator review; saving persists the
visible manual settings as first-class draft input and may also preserve the
bounded research JSON as evidence. Hidden imported values never override
user-edited visible settings, and the draft still does not apply filters,
reload CamillaDSP, or authorize sound.
Choosing a confirmed driver in **Test each driver** calls the product-level
`/sound/active-speaker/prepare-driver-test` endpoint. That endpoint first saves
any needed software quiet guard and prepares a fresh crossover preview. If the
diagnostic route resolves to a coherent single DAC, the selected test can use
the direct-DAC backend: outputd is paused, one bounded test WAV is sent to the
physical DAC with only the selected output populated, and outputd is restarted.
If the topology resolves to a registered outputd active-speaker lane, the
endpoint uses the protected staging/load/arm path instead. The browser does not
expose those implementation steps as separate buttons or status grids.

The audible boundary is still `/sound/active-speaker/play-tone`. It recomputes
the driver-protection policy, requires the selected saved topology target,
confirmed DAC output identity, calibration-level bounds, and an audible backend.
For protected-outputd routes it also requires the safe-session/startup-load
evidence. For direct-DAC diagnostics it still records a short safe session and
never treats a failed stop/restart as a confirmable driver result. The saved
topology does not make `outputd_active_content_playback` an audible test
writer: that PCM is a daemon-owned CamillaDSP/outputd lane. Tweeter/high-
frequency targets are not horn-specific: the backend auto-records a
software-guarded bring-up request when no physical protection evidence is
present, and the tone plan still enforces role-specific caps and high-pass
guards.

Lower-level diagnostics still exist for tests and operator debugging:
`/sound/active-speaker/commissioning-rehearsal` derives a no-audio
sequence from durable evidence, `/sound/active-speaker/stage-config` stages the
protected startup candidate, `/sound/active-speaker/check-path-safety` writes
the path-safety evidence, and `/sound/active-speaker/load-startup-config` loads
the protected graph. The normal product UI does not require a user to understand
or click those controls.
The same walkthrough then opens **Validate and apply**. That card first runs a
short combined-speaker test through `/sound/active-speaker/summed-test`; the
summed crossover validation POST at
`/sound/active-speaker/summed-validation` must reference the latest audible
combined-test record for that group. Artifact-only or stale summed-test records
cannot unlock the active profile. After summed validation,
`/sound/active-speaker/baseline-profile` compiles the saved topology, visible
crossover settings, fresh crossover preview, driver-check evidence, and summed
validation into
`/var/lib/camilladsp/configs/active_speaker_baseline.yml` plus
`/var/lib/jasper/active_speaker_baseline_profile.json`. Compile is still
no-audio and does not load CamillaDSP. Applying the profile is a separate,
explicit `/sound/active-speaker/baseline-profile/apply` action that uses the
shared DSP apply transaction. It is currently enabled only for the outputd-owned
active output lane; other hardware paths can save the profile for review, but
the UI keeps Apply disabled until that handoff is supported. After apply
succeeds the UI can truthfully say this is now the active speaker profile.
The guarded startup substrate still persists readable evidence at
`/var/lib/jasper/active_speaker_staged_config.json` and
`active_speaker_path_safety.json`. The loader treats saved path-safety evidence
as stale if the topology, staged candidate, or rollback config path/hash changes
after the check. A normal bounded JTS stereo profile can be the first-run
rollback target because the staged muted/protected candidate owns driver
protection before any tone can play; missing rollback files, unreadable rollback
files, unknown/custom DSP, or rollback configs with unsafe positive gain still
fail closed.
The active-speaker runtime substrate starts in
`jasper.active_speaker`, the physical topology substrate starts in
`jasper.output_topology`, and the canonical safety/design plan lives in
[`HANDOFF-active-speaker-dsp.md`](HANDOFF-active-speaker-dsp.md).
The next `/sound/` slice should exercise the lab-gated quiet woofer/mid path on
hardware, then use the same driver-aware microphone loop for a protected
high-frequency target.

## Files

- `jasper/active_speaker/` — import-cheap active-speaker preset,
  channel-map, safety-envelope, baseline-profile schemas, and
muted/protected startup-template YAML emission plus read-only
environment reporting, safe-playback session state, preset-derived no-audio
tone-plan preparation, topology-target tone plans, bounded artifact rendering,
an explicitly lab-gated `aplay` backend for non-tweeter targets, and a
read-only playback-readiness gate plus protected startup-config staging for the
Epique/F110M mono cabinet. Current scope is validation/template generation and
status/session/plan/artifact/readiness/staging bookkeeping plus optional lab
channel tests and a guarded startup-config load/rollback boundary. The staging
writer is still no-load; only the startup-load route may reload the protected
graph, and it still does not emit audio or authorize playback.
- `jasper/output_topology.py` — import-cheap physical-output topology
  contract for DAC lanes, speaker groups, passive/active modes, subwoofers,
  identity verification, and tweeter-protection evidence. Current scope is
  JSON load/save/evaluate plus the `/sound/` Active crossover setup UI; it has no audio
  side effects, hardware loading, or sound-emitting playback.
- `jasper/sound/profile.py` — import-cheap persisted contract:
  `SoundProfile`, stock curves, simple EQ, bounded parametric bands,
  preview response, expanded-band overlays, the peak-boost `estimate_headroom_db`
  metric, and `loudness_compensation_db` (the loudness-weighted gain the
  opt-in match-loudness setting applies).
- `jasper/sound/settings.py` — import-cheap global output settings
  (`SoundSettings`: `headroom_trim_db`, `match_loudness`) persisted to
  `/var/lib/jasper/sound_settings.json`, fail-soft to the do-nothing defaults.
- `jasper/sound/camilla_yaml.py` — CamillaDSP YAML emitter and
  generated-config inspector. It must stay import-cheap; do not import
  NumPy/SciPy here.
- `jasper/dsp_apply.py` — import-cheap shared DSP apply substrate:
  typed CamillaDSP validation, config reload, rollback, file locking,
  compact last-result persistence, and the durable DSP write epoch used
  to fence stale live updates.
- `jasper/web/sound_setup.py` — `/sound/` page, `/state`, `/preview`,
  `/live-draft`, `/audition`, `/apply`, and `/settings`.
- `jasper/camilla.py` — lazy pyCamillaDSP wrapper. Besides the durable
  config-file loader, it owns the active-config upload/patch escape
  hatches used by live audition surfaces so raw Camilla command names do
  not leak into product code.
- `jasper/camilla_config_contract.py` — shared import-cheap CamillaDSP
  defaults and `PeqFilter` type used by generated config emitters.
- `jasper/correction/session.py` — correction apply now emits through
  the combined config path so a saved sound profile survives new room
  correction applies.
- `jasper/cli/doctor.py` and `jasper/control/server.py` — observability
  surfaces for profile/config drift and current sound state.

## Config Ownership

CamillaDSP has one active config path, so composition is load-bearing.
Do not add another writer that emits directly to CamillaDSP without
going through the same room-plus-preference ordering. `/sound/live-draft`
is the narrow exception: it emits the same combined room-plus-preference
config shape as the durable path, but uploads it as an active config
only. It must not persist profile state, write `sound_current.yml`,
change the config file path, or bypass the same known-config guard. It
still enters the shared DSP writer lock and checks the durable write
epoch before touching audio.

The generated saved sound filename is stable:

```text
/var/lib/camilladsp/configs/sound_current.yml
```

The generated unsaved audition filename is stable:

```text
/var/lib/camilladsp/configs/sound_audition.yml
```

`/sound/apply`, `/sound/audition`, and `/sound/live-draft` only preserve
room PEQs from configs they know how to inspect:

- `/etc/camilladsp/outputd-cutover.yml` → no room PEQs.
- `/var/lib/camilladsp/configs/correction_<session>_<ts>.yml` → extract
  room PEQs.
- `/var/lib/camilladsp/configs/sound_current.yml` → extract room PEQs.
- `/var/lib/camilladsp/configs/sound_audition.yml` → extract room PEQs.

Anything else is treated as a custom config and rejected rather than
silently overwritten. This is intentional fail-closed behavior.

The applied profile and named profile library are intentionally separate
files:

```text
/var/lib/jasper/sound_profile.json
/var/lib/jasper/sound_profiles.json
```

`sound_profile.json` answers "what preference profile is currently
applied?" `sound_profiles.json` answers "which named custom profiles can
the user load as a draft?" This separation keeps Bypass / Applied /
Draft and future AI proposals simple.

The primary user flow is: pick Off / a Saved profile, or open Draft to
tune Simple or PEQ controls and Save. Library operations (Save as new,
Overwrite, Rename, Delete) live on the Saved rows and in the Draft
footer rather than a separate menu.

The page is built on the **canonical design system**: shared tokens,
fonts, and component primitives live in `deploy/assets/app.css` (served
static by nginx, linked via `jasper.web._common.canonical_page`), with
only sound-specific component CSS inline. `/sound/` is the first wizard
on this system; see AGENTS.md "Canonical design system" for the
convention other wizards follow.

## Apply Semantics

`/sound/preview`:

1. Parses and clamps the posted `SoundProfile`.
2. Returns total and per-component response previews — real RBJ biquad
   magnitude at 48 kHz, matching CamillaDSP's output (shelves are drawn
   as a fixed Butterworth shelf to mirror the 6 dB/oct slope emit).
3. Does not touch CamillaDSP or disk.

`/sound/profiles/save`, `/sound/profiles/rename`, and
`/sound/profiles/delete`:

1. Require the shared JSON CSRF header.
2. Mutate only `/var/lib/jasper/sound_profiles.json`.
3. Never load a CamillaDSP config and never change
   `/var/lib/jasper/sound_profile.json`.
4. Stamp custom profiles with their library identity metadata.
5. Return the refreshed profile-library payload for the UI picker.

`/sound/live-draft`:

1. Parses and clamps the posted Draft `SoundProfile`.
2. Computes the draft's output trim from the global sound settings (manual
   headroom + loudness compensation when match-loudness is on; 0 by default,
   so boosts apply at unity).
3. Requires the posted `dsp_write_epoch` from the latest `/sound/state`.
4. Enters the shared DSP writer lock.
5. Skips the request as stale if the durable DSP write epoch changed.
6. Reads the active CamillaDSP config path with `best_effort=False`.
7. Rejects unknown/custom active configs.
8. Extracts room PEQs from known JTS-generated configs.
9. Emits a generated combined config in memory.
10. Uploads that YAML through `CamillaController.set_active_config_raw`.
11. Does **not** write `sound_audition.yml`, change the Camilla config
   file path, mutate `/var/lib/jasper/dsp_apply_state.json`, or persist
   `/var/lib/jasper/sound_profile.json`.
12. Returns `live_status=unavailable` without reloading a config when the
    controller does not expose active-config upload or CamillaDSP rejects
    the live upload; explicit compare buttons remain the safe reload path.

`/sound/audition`:

1. Parses and clamps the posted draft/bypass `SoundProfile`.
2. Computes the output trim from the global sound settings (same opt-in
   headroom + match-loudness as the live-draft path; 0 by default).
3. Reads the active CamillaDSP config path with `best_effort=False`.
4. Rejects unknown/custom active configs.
5. Emits `sound_audition.yml` atomically inside the DSP apply lock.
6. Runs CamillaDSP validation when available.
7. Loads the config through the CamillaDSP websocket.
8. Confirms the active config path when CamillaDSP is reachable.
9. Rolls back to the prior config path if reload/confirm fails.
10. Does **not** persist `/var/lib/jasper/sound_profile.json`.

`/sound/settings`:

1. Requires the shared JSON CSRF header (route-checked before CSRF).
2. Clamps and saves the global `SoundSettings`
   (`headroom_trim_db`, `match_loudness`) to
   `/var/lib/jasper/sound_settings.json`.
3. Re-applies the active profile through the `/sound/apply` path so the new
   output trim takes effect immediately and persists in `sound_current.yml`.
4. Saves the settings **before** the re-apply, so a failed re-apply still
   sticks (returned as an error; the saved setting takes effect on the next
   apply).

`/sound/apply` (`Save to Speaker` in the UI):

1. Reads the active CamillaDSP config path with `best_effort=False`.
2. Rejects unknown/custom active configs.
3. Enters the shared DSP apply path in `jasper/dsp_apply.py`.
4. Emits `sound_current.yml` atomically inside the DSP apply lock.
5. Runs `camilladsp --check <config>` when the binary is available.
   The config file is positional; `--check` is the validation flag.
6. Loads the config through the CamillaDSP websocket.
7. Confirms the active config path when CamillaDSP is reachable.
8. Rolls back to the prior config path if reload/confirm/persist fails.
9. Persists `/var/lib/jasper/sound_profile.json` only after a successful
   reload and confirmation.

`/correction/apply`:

1. Designs room PEQs from the measurement session.
2. Loads the saved `SoundProfile`.
3. Uses the same shared DSP apply path as `/sound/apply`, including
   locked YAML emission, validation, reload, rollback, and last-result
   persistence.
4. Emits a combined config to the correction filename so current
   correction status still works.

## Observability

`jasper-doctor` includes:

- `current correction` — recognizes correction configs plus
  `sound_current.yml` / `sound_audition.yml` when room PEQs are present.
- `sound profile` — reports saved profile, filter count, estimated
  headroom, the global match-loudness / headroom settings, the effective
  output trim, and warns when a saved active profile is not reflected in a
  generated active config.
- `DSP apply state` — reports the most recent DSP config apply result
  from `/var/lib/jasper/dsp_apply_state.json`; rollback failure is a
  doctor failure.

Live Draft updates intentionally do not write DSP apply state, so they
are observed through `event=sound.live_draft` logs and the `/sound/`
status line rather than doctor. Durable Save to Speaker remains the
stateful operation doctor audits. Repeated live-unavailable warnings are
rate-limited so a broken live-upload environment does not spam the
journal while the user drags a slider.

`/state` and `/sound/state` expose the saved sound profile plus the
profile-library picker payload and latest DSP apply record. `/sound/state`
additionally carries the global `sound_settings` and the effective
`output_trim_db` for the current profile, so the page renders the
match-loudness switch and headroom slider from server truth. The central
`jasper-control` `/state.audio.sound` carries the same `match_loudness`,
`headroom_trim_db`, and `output_trim_db` (and `jasper-doctor` prints them),
so an operator can see why a profile sounds quieter or level-matched without
opening the page. `/state` also includes the runtime Camilla config truth so
dashboards do not confuse "profile desired" with "profile actually loaded":

```json
{
  "audio": {
    "sound": {
      "enabled": true,
      "curve_id": "flat",
      "profile_id": "stock:flat",
      "profile_name": "Flat",
      "simple_eq": {"sub_bass_db": 0.0, "bass_db": 0.0, "mid_db": 0.0, "presence_db": 0.0, "treble_db": 0.0},
      "parametric_band_count": 0,
      "filter_count": 0,
      "headroom_db": 0.0,
      "updated_at": null,
      "last_dsp_apply": {
        "source": "sound",
        "result": "success",
        "active_config_path": "/var/lib/camilladsp/configs/sound_current.yml",
        "candidate_config_path": "/var/lib/camilladsp/configs/sound_current.yml"
      },
      "runtime": {
        "active_config_path": "/etc/camilladsp/outputd-cutover.yml",
        "last_apply_config_path": "/var/lib/camilladsp/configs/sound_current.yml",
        "matches_last_apply": false,
        "state": "base",
        "active": false,
        "warning": "Desired sound profile is not the active CamillaDSP config."
      },
      "dsp_write_epoch": "<latest dsp apply op_id or none>"
    }
  }
}
```

For `/state.audio.sound`, `enabled` remains the persisted preference.
Use `runtime.active` / `runtime.state` to answer whether CamillaDSP is
currently running the saved profile, the flat outputd base config, a
custom config, or an unexpected mismatch.

`/correction/status` also includes `last_dsp_apply` so a failed apply
can be diagnosed without scraping journal logs.

## Guardrails

- Keep `/sound/` cheap to load. The combined `jasper-web` process must
  not import NumPy/SciPy on cold start.
- Keep preference EQ bounded. The Simple EQ range is ±12 dB (matching
  the slider UI; shared with the calibration advisor via
  `SIMPLE_EQ_LIMIT_DB`) and advanced bands are capped. Boosts apply at
  unity by default (no auto-preamp); only the opt-in output trim
  (`headroom_trim_db` + match-loudness compensation) attenuates, and the
  `devices.volume_limit: 0.0` ceiling stays the hard clip guard.
- Do not merge room-correction target selection and preference EQ into
  one opaque layer. They can share UI affordances later, but the DSP
  contract must keep them distinct.
- A/B bypass should toggle only preference EQ, not erase room
  correction.
- The graph is visualization only. The canonical editable state is the
  bounded `SoundProfile` JSON model.
- Simple and PEQ are exclusive modes; switching converts the draft
  (PEQ → Simple snaps to the nearest slot; Simple → PEQ expands non-zero
  bands). Older 3-band profiles still load, with Sub-bass / Presence
  defaulting to 0 dB.
- Named custom profiles are draft templates. Loading, renaming, saving,
  or deleting one must not persist profile state unless the user saves to
  the speaker. Editing or loading a draft may change live audio through
  `/sound/live-draft`; that live draft remains intentionally non-durable.
- Unsaved auditions must never persist profile state. They may leave
  `sound_audition.yml` active until the user switches Bypass / Applied /
  Draft or applies; that is expected and observable via the DSP apply
  record.
- Live Draft must only touch the preference EQ layer of a known JTS
  config. It must never change room PEQs, source routing, limiter,
  crossover, or the `devices.volume_limit: 0.0` safety ceiling.
- Live Draft requests must be coalesced client-side. Do not fire one
  CamillaDSP upload per touch pixel.
- Live Draft requests must include and verify the durable DSP write
  epoch. A stale live request must be a no-op, not an older active
  config upload after `Save to Speaker` or `/correction/apply`.

## Future Work

- Browser/voice AI helper UI around the existing advisor harness: propose
  bounded `SoundProfile` edits, audition them through `/sound/audition`,
  compare against the listener's chosen baseline, then ask the user before
  saving anything.
- Optional profile export/import once we know what users want to share.
- A clipping indicator (live on `/sound/`, backed by a `/state` field from
  CamillaDSP's clipped-sample counter, with doctor carrying the cumulative
  count) so the opt-in headroom trim is guided rather than guessed.
- Optional desktop-only draggable graph handles. Keep mobile/touch
  controls as the primary path.
- Optional voice-feedback loop using the existing Pi microphone path.

Last verified: 2026-06-17 (`/sound/` active-speaker UI rechecked after the
continuous commission ramp tone, automatic quiet-ramp controls, direct-DAC
diagnostic route, and removal of unused legacy public test routes.)
