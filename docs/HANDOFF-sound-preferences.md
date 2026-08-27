# HANDOFF — Sound preferences

Operational truth for the `/eq/` preference-EQ page, the `/sound/setup/`
global-output and speaker-setup page, and their shared `/sound/*` backend APIs.

Neighbouring owners — do not restate their content here:
[HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md) (crossover/driver
safety) · [HANDOFF-dsp-graph-carrier.md](HANDOFF-dsp-graph-carrier.md) (which
configs can host an EQ write) ·
[HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md)
(outputd state) · [HANDOFF-volume.md](HANDOFF-volume.md) ·
[the archive](historical/speaker-setup-and-shelf-q-2026-07.md) (the 2026-07
shelf-slope defect and the step-by-step speaker-setup walkthrough).

## Routes and ownership

| Route | Surface and owner |
|---|---|
| `/eq/` | Profiles, Simple EQ / PEQ, Match Loudness. Rendered by `jasper.web.sound_setup`. |
| `/sound/setup/` | I²S HAT boot enablement, extra headroom, volume-floor calibration, output topology, active-speaker commissioning. Same server, different page mode. |
| `/sound/crossover/`, `/sound/room/`, `/sound/bass/` | Active-speaker, room, and bass measurement. Backed by the correction service. |

`/sound/` is navigation only: a non-cacheable redirect to `/sound/setup/`, and
mutating requests are never redirected. The `/correction/*` measurement URLs stay
direct compatibility aliases. The split pages reuse the same handlers, renderers,
JSON state, static module, and ports, and **opening either does not start or
proxy through the heavier correction worker**. The full and streambox nginx
profiles spell out the same route map explicitly; there is no shared include
layer. The `/sound/active-speaker/*` namespace is the commissioning API, not a
measurement namespace; `jasper.web.active_speaker_flow`'s cooperative TTL-backed
exclusion is the coordination boundary, and the page split added no cross-process
lease or recovery state machine.

**While a speaker is an active bonded follower, `/eq/` is a delegated surface**:
the page shows a leader-owned notice and does not load the editor, and every
content-DSP mutation route rejects with HTTP 409 — the leader renders content EQ,
room correction, and volume shaping for the paired image. `/sound/setup/` keeps
local output topology and active-speaker commissioning available, because
crossover and driver protection belong to the box that owns the DAC path.

## The layering invariant

`/sound/room/` measures the room and emits room PEQs; `/eq/` applies preference
shaping **after** them. The combined CamillaDSP config preserves that order:
room-chain filters first, preference EQ second, the `flat` terminator last, with
a single `room_headroom` attenuator between them when a room measurement applied
boosts. The room-chain segment may also carry gainless per-channel `Delay`
filters for leader-owned stereo-pair time-of-arrival calibration — correction
state, not a preference control.

**Do not merge room-correction target selection and preference EQ into one opaque
layer.** They may share UI affordances; the DSP contract keeps them distinct, and
an A/B bypass toggles only preference EQ.

### Gain staging

A preference boost applies at unity —
[ADR-0121](adr/0121-preference-boosts-boost-room-boosts-are-compensated.md). No
automatic preamp is inserted for preference gain; the only global attenuation on
that layer is the opt-in output trim, 0 by default. `devices.volume_limit: 0.0`
stays the hard clip guard, so removing the old preamp cannot raise the ceiling —
a large boost at high volume clips at 0 dBFS rather than duck the mix.
Active-speaker baselines follow the same policy: preference bands ride at unity
ahead of the split, while output trim and match-loudness attenuation fold into
`active_baseline_headroom`.

Room-correction boosts are the exception and are compensated automatically:
`jasper.camilla_stereo_prefix.build_stereo_prefix` derives `room_headroom` from
the sum of positive room-PEQ gains (an upper bound on the combined peak) and
attenuates the whole signal by it; cuts-only correction emits none, keeping the
config byte-identical. This is deliberately separate from `output_trim_db`,
which compensates only the preference layer and is skipped on a flat profile —
it would not protect room boosts on a household with no preference EQ set.
`estimate_headroom_db` survives as the peak-boost **metric** doctor, `/state`,
and the calibration advisor surface; it drives nothing.

### Global output settings

Three settings, distinct from per-profile EQ, are owned by
`/var/lib/jasper/sound_settings.json` (`jasper/sound/settings.py`) and fail soft
to do-nothing defaults — a missing or corrupt file never alters the sound. It is
`0640` and inherits the parent `jasper` group, so non-root `jasper-control`
reads the same calibrated floor as the wizard.

- **Match loudness** — turns each profile down by `loudness_compensation_db`, a
  pink/K-weighted power average of the EQ response, **not** peak, so a narrow
  +8 dB band compensates ~1 dB rather than 8. Anchored to attenuation (≥ 0), so
  it can never cause clipping.
- **Extra headroom** — a manual 0–12 dB clamped attenuation (`headroom_trim_db`)
  for listeners running JTS at full digital volume into their own amplifier.
- **Volume floor** — the dB value for the user-visible 1% point
  (`volume_floor_db`, clamped −60..−10, default −50); 0% remains a hard
  CamillaDSP mute. `/sound/setup/` can start a continuous 1 kHz calibration
  tone, update it live as the slider moves, and stop it explicitly or on leave.

The emitter applies one `output_trim_db = headroom_trim + (loudness compensation
when match-loudness is on)` as the `sound_preamp` gain, and only when the active
profile has filters — a flat profile cannot clip from EQ.

## The editor contract

The graph is **visualization only**; the canonical editable state is the bounded
`SoundProfile` JSON model. It draws exactly **one line** — the summed response —
in every state, with each enabled band as one dot anchored *on* that curve, so
the dot sits on the line for every filter type including shelves and cuts, whose
response at the corner is not the band's nominal gain; it never draws a second
per-band curve. The line is real RBJ-cookbook biquad magnitude at CamillaDSP's
48 kHz rate (`jasper/sound/profile.py` `_biquad_coeffs`, mirrored by
`deploy/assets/sound-profile/js/eq-math.js`), so it matches the speaker.

Band types are CamillaDSP biquads: Lowshelf, Peaking, Highshelf, Highpass,
Lowpass, Notch. The three cut/notch types carry no gain — the UI hides Gain, the
model pins it to 0, and the YAML omits the term. Shelves hide Width because
every shelf is drawn *and* emitted at the fixed Butterworth Q. High/low-pass Q
is capped at `CUT_MAX_Q` (1.4) in both the model and the slider, because a
high-Q "pass" filter is a large resonant **boost** at the corner (a Q=8 highpass
peaks ~+18 dB) — surprising, and a needless clipping source; Notch keeps the
full range since it is meant to be surgical.

**Shelf steepness is one constant, drawn and emitted.** Every `Lowshelf` /
`Highshelf` JTS emits — curve presets, Simple bands, Advanced bands, and the
active speaker's linearization shelf, CD-horn backbone, and trailing taper — is
emitted at `jasper.camilla_config_contract.SHELF_Q` = 1/√2, spelled into
CamillaDSP's shelf `q` field by the single choke point
`jasper.camilla_stereo_prefix.emit_filter_spec`. That is the same Q every
evaluator draws a shelf at (`profile._biquad_coeffs`, `eq-math.js`,
`linearization_fit._HIGHSHELF_Q`), so the preview curve, the fit's residual, and
the speaker's output are one curve. There is deliberately **no** per-band shelf
steepness: no evaluator reads one, so a band-level value would describe a filter
the system cannot see. `q` is emitted rather than `slope` because the emitted
number is then literally the number the evaluators use, and unlike `slope` its
meaning does not depend on the band's gain — the
[defect that taught this](historical/speaker-setup-and-shelf-q-2026-07.md#the-shelf-slope-defect-fixed-2026-07-27).

**Off / Saved / Draft is the live source, not navigation.** Off durably applies a
bypassed profile (preserving room correction); Saved durably applies the
remembered saved profile, falling back to Flat; Draft is a live, non-persistent
preview through `/sound/live-draft` until Save commits it. The browser treats
source changes as last-intent-wins: with a durable apply in flight the newest
intent is replayed after it returns, so quick A/B taps cannot leave the graph on
one source while the speaker plays another.

The Draft editor has two exclusive modes — **Simple** (five fixed-frequency gain
sliders whose slot table is `SIMPLE_BANDS`, reaching the UI through
`/state.limits.simple_bands` so columns render from data) and **PEQ** (bounded
parametric bands) — and switching converts the draft rather than warning. Presets
are generated from built-in curves: not persisted, not editable, but openable in
Draft and saveable as new; custom profiles live in `sound_profiles.json`, and
**deleting a library entry does not delete the applied DSP profile**. Band
frequencies, the migration caveat, and the profile-identity metadata:
[archive](historical/speaker-setup-and-shelf-q-2026-07.md#the-eq-editors-modes-presets-and-library).

## Speaker setup

`/sound/setup/` is the **Speaker setup** entry point for active crossover
commissioning: one walkthrough of collapsible task cards (layout → components →
confirm outputs → combined test → validate and apply) with **exactly one step
ever `active`**, the first unfinished step this speaker can still reach.
`_derive_step_statuses` decides which step holds the baton and every message is
chosen from that derived status, so a `todo` step cannot carry done-state copy
and a waiting step names the rung that actually holds it. A full-range passive
layout with no subwoofer reaches only the first three; the last two report
`not_required` and say so rather than offer a test with no target.

Four rules hold across the whole flow.

- **Editing does not play sound or touch live audio.**
  `/sound/output-topology` reads and saves the whole `jasper.output_topology`
  JSON model at `/var/lib/jasper/output_topology.json` and never rewrites ALSA,
  reloads CamillaDSP, emits tones, or authorizes playback.
- **A layout that cannot reach the drivers is refused, not persisted.** If it
  needs a roleful graph but the topology's DAC profile declares no active outputd
  lane, the wizard returns a blocker naming the DAC. Saving does kick
  `jasper-audio-hardware-reconcile` (fire-and-forget, never failing the save),
  and on a live commissioned box an invalidating edit flips the gate to passive
  and parks output until re-commissioned — visible on the Status dashboard,
  whose parked sentence has one writer (`_parked_signal` in
  `jasper/control/audio_health.py`). `/sound/setup/` carries no parked banner.
- **Saving the driver declaration is declaring it — there is no confirm step.**
  Every save stamps the safety profile over the values it just wrote. That
  ceremony was retired because the crossover-accept seam could not perform it:
  accepting a measured crossover writes the declaration itself, which rotated
  the fingerprint, cleared the confirmation, and then refused the very
  measurement loop that produced the value. What still refuses a measurement is
  a declaration JTS cannot use — `incomplete`, `stale`, or `malformed`, each
  needing a different edit — surfaced as the `confirm-safety-limits` callout the
  wizard's `program_profile_not_confirmed` stop deep-links to.
- **Evidence is not permission.** Driver-check evidence is bound to the saved
  physical target fingerprint, so a layout change makes old records
  informational rather than ready-state proof; it is what the baseline compiler
  needs, not playback authority. Marking channel identity is likewise operator
  evidence, satisfying neither tweeter protection nor path-safety blockers.

Step-by-step card behaviour — commission ramp, combined test, research prompt,
reset — is in the
[archive](historical/speaker-setup-and-shelf-q-2026-07.md#the-soundsetup-speaker-setup-walkthrough-step-by-step).
Sound Setup also owns one power-style **Enable I²S audio HAT** control for the
supported InnoMaker HiFi AMP Pro; label and boot overlay come from its
`DacProfile`, and browser logic owns no driver metadata. Intent persists to
`/var/lib/jasper/i2s_hat.env` (absence = Auto/Off). `POST /sound/i2s-hat` is a
bounded, CSRF/Host-guarded JSON write that saves the one desired fact and starts
the allowlisted `jasper-audio-hardware-reconcile.service` through the action
broker — **that root oneshot is the only boot-file writer**, composing the
overlay with its existing USB-role block and atomically replacing `config.txt`.
There is no pre-overlay HAT probe, and
`/run/jasper-output-hardware/i2s-hat-reboot-required` exists only when that
reconcile changed the managed setting and runtime still disagrees. On a
Zero-class Streambox, enabling it moves output from the shared USB host port to
the HAT and reserves that port for gadget mode — see the canonical
[USB role matrix](HANDOFF-usb-gadget.md#usb-data-role-policy). The card warns to
remove all power before fitting or removing the HAT, never to power the Pi from
two inputs at once, never to hot-plug, and to start very low.

## Config ownership

CamillaDSP has one active config path, so composition is load-bearing. **Do not
add another writer that emits directly to CamillaDSP without going through the
same room-plus-preference ordering.** `/sound/live-draft` is the narrow
exception: it emits the same combined shape but uploads it as an active config
only, still entering the shared writer lock and checking the durable write epoch
first.

```text
/var/lib/jasper/sound_profile.json      # what preference profile is applied
/var/lib/jasper/sound_profiles.json     # named custom profiles, draft templates
/var/lib/jasper/sound_settings.json     # the three global output settings
/var/lib/camilladsp/configs/sound_current.yml    # generated saved render
/var/lib/camilladsp/configs/sound_audition.yml   # generated unsaved preview
```

**The generated YAML is a cache, not the source of truth** — saved intent is the
JSON, and `jasper-sound reconcile-current-dsp` re-renders from it at
deploy/startup (below). `sound_current.yml` is the canonical generated render but
**not necessarily the config CamillaDSP is running**: an active-speaker crossover
v2 apply does not update it (#1605), pointing CamillaDSP at a
source-fingerprinted `active_speaker_baseline_candidate_<fp>.yml` and owning its
own Layer-A SSOT, so the two legitimately diverge. **Runtime truth is always
whatever CamillaDSP's statefile reports, never a fixed filename.**

`/sound/apply`, `/sound/audition`, and `/sound/live-draft` route the loaded
config through the **graph carrier**
([HANDOFF-dsp-graph-carrier.md](HANDOFF-dsp-graph-carrier.md)), which preserves
room PEQs from the configs it can host. Anything it cannot host fails **closed**
with a typed reason rather than being silently overwritten: an active-speaker
crossover graph refuses with `eq_on_active_not_wired` unless it is a solo active
baseline recomposable through the active emitter; a program bake whose grouping
state no longer resolves to the pipe sink refuses with
`program_bake_pipe_unavailable`; anything else, `unknown_config`. The refusal
returns HTTP 200 `{status:"blocked", reason_code, message}` so the wizard renders
an honest hint instead of a 502.

## Apply semantics

Every route that reads the active config does so with `best_effort=False` and
rejects unknown/custom graphs. `/sound/preview` touches neither CamillaDSP nor
disk; `/sound/profiles/{save,rename,delete}` mutate only `sound_profiles.json`.

`/sound/live-draft` requires the posted `dsp_write_epoch` from the latest
`/sound/state`, enters the shared writer lock, **skips as stale if the durable
epoch changed**, and uploads a combined config built in memory via
`set_active_config_raw` — writing no file, changing no config path, mutating no
apply state, persisting no profile. If upload is unavailable or CamillaDSP
rejects it, it returns `live_status=unavailable` **without reloading anything**;
explicit compare buttons remain the safe reload path.

`/sound/audition` emits `sound_audition.yml` atomically inside the apply lock,
validates, loads, confirms, and rolls back on failure — without persisting
`sound_profile.json`. The UI no longer drives it, but it is retained deliberately
as the validated-preview surface for propose-preview-approve helper flows, which
`jasper.calibration_agent.sound_actions` opts into.

`/sound/apply` ("Save to Speaker") emits `sound_current.yml` atomically inside
the lock, runs `camilladsp --check` when available, loads through the websocket,
confirms the active path, rolls back on failure, and **persists
`sound_profile.json` only after a successful reload and confirmation**.
`/sound/settings` shares **one module-local Sound-state transaction** with
`/sound/apply`, so their durable writes and DSP emits cannot interleave and each
fresh-reads what it needs inside it. Settings Save holds the boundary through
source-aware volume reconciliation, then re-emits the active profile through the
same durable runtime and asks the volume coordinator to re-apply
`listening_level` when the active source is Camilla-master — so a saved floor
affects the current source without unguarding push-mode handoffs. **The settings
are saved before the re-apply**, so a failed reload still sticks.

`jasper-sound reconcile-current-dsp` runs the same lock and transaction so
emitter fixes take effect at deploy rather than waiting for someone to visit
`/eq/`. It skips unknown/custom graphs, non-hostable protected graphs, flat
no-op profiles, and `sound_audition.yml`, and three of its behaviours are worth
knowing. **When the daemon is unreachable it falls back once to CamillaDSP's
statefile** — the config the daemon opens next start — and carries the whole
pass over that disk transport, naming the reader in its result line. That is a
transport fallback, never a graph choice: the carrier still comes from the
config the statefile names, so a roleful box re-emits its own roleful graph. (A
`CamillaConfigRejected` is a daemon that answered and refused, not an absent one,
and still raises.) **"Unchanged" compares saved intent against the config
CamillaDSP is running**, whatever it is named — which lets a kept
active-crossover candidate stay the running config across a deploy, since
writing identical bytes under a second filename would move only the NAME and
cost a measurement round its entry-graph identity (#2572). **And on a roleful
box it re-emits through the playback endpoint the box is LIVE on**, not the one
its applied snapshot recorded, so a deploy cannot return a roleful speaker from
the ACTIVE ring to Ring B (endpoint derivation:
[audio-paths.md](audio-paths.md)).

**Read its "fail-open" narrowly.** install.sh runs it under an outer timeout and
a failure leaves the current graph in place — but the statefile pointer is proved
legal only for the saved TOPOLOGY (the classifier reads playback device,
channels, and volume limit, never the sample format), so a graph can be legal and
still un-openable. That is what killed jts4 on 2026-08-17. **A fail-open
reconcile is a degraded outcome to investigate, not a safe one.**
`/correction/apply` designs room PEQs, loads the saved profile, uses the same
shared apply path, and emits a combined config to the correction filename.

## Observability

`jasper-doctor` reports **current correction**, **sound profile** (saved profile,
filter count, estimated headroom, the global settings, the effective trim, and a
warning when a saved active profile is not reflected in a generated active
config), and **DSP apply state** — where a rollback failure is a doctor failure.
Live Draft deliberately writes no apply state, so it is observed through
`event=sound.live_draft` and the `/eq/` status line instead, with
live-unavailable warnings rate-limited so a broken environment cannot spam the
journal while a slider is dragged.

**Deploy/startup reconciliation is the first place to look when a deploy did or
did not refresh the audible profile.** It emits
`event=sound.reconcile_current_dsp` for every outcome — `reconciled`,
`unchanged reason=running_config_matches_intent`, or `skipped reason=<code>` —
each carrying `transport=websocket` or `transport=statefile`, where a
`statefile` line means CamillaDSP was down for that pass. The CLI's own
fail-open `result=failed` line carries no `transport`, because no reader ever
answered; `_print_reconcile` does not print the field either, so **read it in the
journal, not the install transcript**. On the `unchanged` line, `current=` and
`candidate=` differ exactly when a non-`sound_current.yml` graph was left in
place.

`/state.audio.sound` and `/sound/state` expose the saved profile, the library
payload, and the latest apply record; `/sound/state` adds `sound_settings` and
the effective `output_trim_db`, and `jasper-control` carries the same fields so
an operator can see why a profile sounds quieter without opening the page.
**`enabled` is the persisted preference; use `runtime.active` / `runtime.state`
to answer whether CamillaDSP is actually running the saved profile**, the flat
outputd base, a custom config, or a mismatch.

## Guardrails

- Keep `/eq/` and `/sound/setup/` cheap to load: the combined `jasper-web`
  process must not import NumPy/SciPy on cold start or activate the correction
  worker to render either page. `jasper/sound/{profile,settings,camilla_yaml}.py`,
  `jasper/dsp_apply.py`, `jasper/output_topology.py`, and
  `jasper/camilla_config_contract.py` are import-cheap and must stay so.
- Keep `SoundSettings` aggregate. Split pages post only the fields they own; the
  one shared Sound-state transaction is the lost-update guard, and there is no
  second settings file. Capture a coherent snapshot before release, then render
  previews and library data outside the boundary. Keep preference EQ bounded:
  Simple EQ is ±12 dB (`SIMPLE_EQ_LIMIT_DB`, shared with the calibration
  advisor) and advanced bands are capped.
- Named custom profiles are draft templates, and unsaved auditions are previews:
  neither may persist profile state unless the user saves to the speaker. An
  audition may leave `sound_audition.yml` active until the user switches source
  or applies — expected, observable in the apply record, and never promoted by
  deploy reconcile.
- Live Draft must touch only the preference layer of a known JTS config — never
  room PEQs, source routing, the limiter, the crossover, or the
  `volume_limit: 0.0` ceiling. It must be coalesced client-side (not one upload
  per touch pixel) and must include and verify the durable write epoch, so a
  stale request is a no-op rather than an older config landing after a save.

## Files

`jasper/sound/` — `profile.py` (the `SoundProfile` contract, stock curves, Simple
EQ, bounded bands, preview, `estimate_headroom_db`, `loudness_compensation_db`),
`settings.py`, `camilla_yaml.py`, `runtime.py`; `jasper/cli/sound.py` is the CLI.
`jasper/dsp_apply.py` is the shared apply substrate — typed validation, reload,
rollback, bounded cancellation-safe writer-lock admission with a required feature
identity, last-result persistence, and the durable write epoch.
`jasper/camilla_config_contract.py` and `jasper/camilla_stereo_prefix.py` own the
emitted vocabulary; `jasper/camilla.py` is the lazy pyCamillaDSP wrapper that
also owns the active-config upload/patch escape hatches, so raw Camilla command
names do not leak into product code. `jasper/web/sound_setup.py` serves both page
modes and every `/sound/*` route through the `sound-page-data` JSON island;
`jasper/output_topology.py` and `jasper/active_speaker/` own the topology
contract and active-speaker runtime; `jasper/correction/session.py` emits through
the combined config path so a saved profile survives a room-correction apply.
Page assets: `deploy/assets/sound-profile/`.

**Future work:** a browser/voice helper UI around the existing advisor harness ·
optional profile export/import · a clipping indicator backed by CamillaDSP's
clipped-sample counter, so the headroom trim is guided rather than guessed ·
optional desktop-only draggable graph handles, keeping touch primary.

Last verified: 2026-08-26 (triage pass — `SHELF_Q`, `CUT_MAX_Q`,
`SIMPLE_EQ_LIMIT_DB`, `SIMPLE_BANDS`, `emit_filter_spec`,
`build_stereo_prefix`'s `room_headroom` / `sound_preamp`, the `SoundSettings`
fields, `reconcile-current-dsp`, and every file-map path rechecked against their
owning modules. The shelf-slope defect record, the editor's mode/preset detail,
and the step-by-step speaker-setup walkthrough moved to
`docs/historical/speaker-setup-and-shelf-q-2026-07.md`; the preference-gain
policy became ADR-0121.)
