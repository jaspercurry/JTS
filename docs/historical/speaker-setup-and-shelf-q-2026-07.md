# Speaker setup walkthrough and the shelf-Q correction (2026-07 … 2026-08)

> **Archived detail, not current guidance.** The operational reference is
> [HANDOFF-sound-preferences.md](../HANDOFF-sound-preferences.md), which owns
> the `/eq/` and `/sound/setup/` contracts, config ownership, apply semantics,
> and guardrails. The preference-gain policy those rest on is
> [ADR-0121](../adr/0121-preference-boosts-boost-room-boosts-are-compensated.md).
>
> This file keeps two things the spine no longer carries: the 2026-07-27
> shelf-slope defect and exactly what it moved on an already-applied profile,
> and the step-by-step narrative of the `/sound/setup/` **Active crossover
> setup** walkthrough. The walkthrough describes UI and coordinator behaviour
> that changes with the measurement program — read
> [HANDOFF-active-speaker-dsp.md](../HANDOFF-active-speaker-dsp.md) and
> [measurement-loop-doctrine.md](../measurement-loop-doctrine.md) as the live
> owners before relying on any step here.

## The shelf-slope defect (fixed 2026-07-27)

**The defect this replaced (fixed 2026-07-27, linearization-integrity
PR-L2).** The emitter previously wrote `slope: 6.0`, on the documented-here
belief that 6 dB/octave *was* Butterworth. It is not. CamillaDSP's advanced
shelf takes `S = slope/12` and derives

```
Q = 1 / sqrt((A + 1/A) * (1/S - 1) + 2),   A = 10**(gain/40)
```

so Butterworth is `S = 1`, i.e. **`slope: 12`** — pinned by CamillaDSP's own
`lowshelf_slope_vs_q` test (`slope: 12.0` ≡ `q: FRAC_1_SQRT_2`). At
`slope: 6` the realized Q depends on the shelf's *gain* and collapses:
0.476 at −11 dB, where the realized curve missed the drawn/designed one by
up to 1.7 dB. Because the fit's realization gate, residual, and VERIFY
prediction all evaluated the Butterworth shelf, nothing in the loop could
observe the error — it was found by measuring the speaker against a
reference monitor. `q` is now emitted rather than `slope: 12` because the
emitted number is then literally the number the evaluators use, and unlike
`slope` its meaning does not depend on the band's gain.

**What happens to an already-applied profile.** The fix is in the
*emission*, not in any stored design: filter frequencies, gains and Qs
persist unchanged in the sound profile and in the active speaker's
immutable recomposition snapshot. So nothing changes until the next
re-emission — and then the *same stored design* is realized at the Q it was
always modelled at. Concretely:

- **`/eq/` taste EQ — changes on the next DEPLOY, with no user action.**
  `install.sh` runs `jasper-sound reconcile-current-dsp` on every deploy;
  `reconcile_sound_dsp_state` re-renders the graph and compares it to the
  on-disk YAML with only the id header stripped, so `slope:` versus `q:`
  makes them unequal → `status=reconciled` → the new graph is written *and*
  loaded. (An earlier `/eq/` save/apply gets there first if one happens.)
  Shelf bands move toward the curve the graph has always drawn, by an
  amount that grows with the band's gain (peak deviation over 20 Hz–20 kHz):
  the Harman-style and B&K-style tilt shelves 0.52 / 1.17 dB (they were the
  only shelves carrying `slope: 3.0`), their bass shelves 0.60 / 0.45 dB,
  and a Simple or Advanced shelf driven to the ±12 dB limit up to 1.87 dB.
  Like every shelf change this is a *tilt* about the corner, not an offset:
  the band above moves one way and the band below the other. A household
  that voiced a profile by ear on an old build may want to re-touch it; the
  graph it was voiced against did not move.
- **Active speaker (the load-bearing case).** The next write of the
  baseline graph — an Apply, or a recomposition triggered by saving room /
  preference EQ — realizes the linearization shelves at their designed Q.
  The 2026-07-27 JTS3 profile carried a **−11 dB Lowshelf at 4419 Hz**
  (`as_tweeter_linearization_shelf`; the CD-horn backbone is a Lowshelf, not
  a Highshelf). Its correction is antisymmetric about that corner:
  **±1.70 dB, 3.40 dB peak-to-peak** — up to **+1.70 dB** above the corner
  (+0.63…+1.70 dB across 5–12 kHz, +1.65 dB at 6912 Hz where `peak_2` sits)
  and up to **−1.70 dB** below it (minimum near 2.5 kHz). So the tweeter
  band gains up to 1.7 dB of its deficit back while the shoulder below the
  corner comes down by as much. **This is the fix working**: the speaker
  moves toward what the fit designed, not away from it. It is also not the
  whole 7–11 dB deficit — the rest is tracked in
  [linearization-integrity-plan.md](../linearization-integrity-plan.md).
- **The pre-existing drift guard fires, by design.** Until that re-emission
  happens, a speaker still running an old-build graph will not match the
  recomposed expectation, so
  `setup_status._applied_layer_a_binding` reports `mismatch` and Room
  correction is blocked with "The sound pipeline loaded on this speaker does
  not match the applied manual profile. Apply that crossover again before
  Room correction." That is the honest state — the loaded graph really is
  not the one the applied profile now describes — and re-applying clears it.
  The copy is deliberately cause-neutral: this drift comes from the emitter
  changing under an untouched profile, not from anyone editing a crossover.
  **Superseded 2026-08-26 (#3006):** the guard still detects the mismatch,
  but a topology change since the applied baseline now *discloses* rather
  than blocks the box. Read the consequence above as the 2026-07 behaviour;
  `jasper/active_speaker/setup_status.py` is the live owner.
- **Observable.** The write-time journal line carries the transition:
  `journalctl -u jasper-control | grep event=active_speaker_baseline_config_written`
  → `linearization_shelves=<n> shelf_q=0.7071068`. A graph written by an
  older build has neither field. For the preference-EQ side the signal is the
  deploy transcript itself: `event=sound.reconcile_current_dsp
  result=reconciled` on a deploy that changed no profile means the render
  moved, which on the first deploy after this change is the shelf-Q
  re-emission. The live YAML is self-describing in both cases: a fixed
  shelf reads `q: 0.7071068`, an old one `slope: 6.0000`.

## The EQ editor's modes, presets, and library

The Draft editor has two exclusive modes. **Simple** is five fixed-frequency
gain sliders (Sub-bass 60 Hz, Bass 150 Hz, Mid 1 kHz, Presence 4 kHz, Treble
10 kHz); the slot table is `SIMPLE_BANDS`, reaching the UI through
`/state.limits.simple_bands` so the page renders columns from data rather than
hardcoded labels. **PEQ** is bounded parametric bands with exact Hz entry plus a
log-frequency slider. Switching modes converts the draft rather than warning
(PEQ → Simple snaps each slot to the nearest enabled band within ~1.2 octaves;
Simple → PEQ expands each non-zero band). Older 3-band profiles still load with
Sub-bass and Presence at 0 dB — but the redesign moved the Bass centre
(105 → 150 Hz) and the Treble shelf (4 k → 10 k), so a migrated profile's values
shape slightly different frequencies.

Presets (Flat, Harman-style, B&K-style) are generated from built-in curves — not
persisted, not editable, but openable in Draft and saveable as new. Custom
profiles live in `sound_profiles.json`. `SoundProfile` carries optional
`profile_id`/`profile_name` metadata so the UI can distinguish "applied Flat"
from "draft edited from Flat" without making metadata part of the DSP math
(`stock:<curve_id>` / `custom_<12 hex>`). **Deleting a library entry does not
delete the applied DSP profile** — it only removes a draft template.

## The `/sound/setup/` speaker-setup walkthrough, step by step

As of 2026-08-04, `/sound/setup/` is the **Speaker setup** entry
point for active crossover commissioning. Opening it shows one primary
**Active crossover setup** walkthrough, not a separate environment card. The
walkthrough keeps one task card open at a time: choose speaker layout, add your
components, confirm outputs and each driver, test the combined
crossover, then save/apply the active profile. Editing layout and
crossover-settings drafts does not play sound or touch live audio. Saving a
layout uses the shared park -> commit -> runtime-converge transaction: explicit
passive intent can load a safe recomposed graph, while unconfigured or
incomplete active intent remains parked. Detected hardware is supporting
context and the hardware refresh control is a small utility inside the layout
step.

The **Confirm outputs** card owns the guarded driver-check controls.
The primary UI no longer refreshes the old backend checklist/grid or asks the
user to understand environment, path-safety, staging, startup-load, or
safe-session probes as separate steps. Active 2/3-way groups present one
commission action at a time: **Play** prepares a continuous quiet tone, moves
through a disabled preparing state while the backend opens the protected path,
then becomes a red **Stop** button once JTS believes audio is active. The only
positive result is driver-specific (**I hear woofer/tweeter/midrange**) and
also promotes the output identity when the output had not yet been confirmed.
The same card keeps DAC-channel assignment controls visible so the operator can
fix a wrong output mapping in place. Internally,
`/sound/active-speaker/commission-load` repairs missing software guards and
loads the protected active graph, then
`/sound/active-speaker/commission-ramp-step` raises only the selected driver in
larger bounded guarded steps while the same cancellable tone keeps playing.
Transient "raising" progress copy is not shown, so the card does not flap while
audio is playing. The following **Test combined drivers** step is only the
summed crossover check; it no longer duplicates individual driver
commissioning. Passive/full-range layouts use the normal listening path rather
than a separate direct-DAC driver-test card. The tone frequency is role-native
where that improves operator
recognition (woofer/subwoofer use normal low test tones), then bounded by the
compiled active-speaker preset/crossover edges and tweeter-protection policy. If
the safe limit is reached with no audible driver, the UI stops/re-mutes and tells the
operator to check amp gain, wiring, and DAC output mapping. Driver-choice buttons
also refresh a stale no-audio crossover preview when the current working setup
can produce one. The level state is separate from normal listening volume.

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
A save is refused outright when the submitted layout needs a roleful graph and
the topology's own DAC profile declares no active outputd lane
(`supports_active_outputd_lane=False`): that pairing can never reach the
drivers, so the wizard returns a blocker naming the DAC instead of persisting
a layout the box would go silent under.
The same `/sound/setup/` page renders a lightweight **Active crossover setup**
surface over that endpoint as collapsible task cards: **Choose speaker
layout**, **Add your components**, **Confirm outputs**, **Test combined
drivers**, and **Validate and apply**.
A full-range passive layout with no subwoofer reaches only the first three: it
has no inter-driver crossover and no bass-management split, so the coordinator
(`build_commissioning_view`) reports the last two steps `not_required` and the
flow finishes at **Confirm outputs**. Those two cards then explain that they do
not apply instead of offering a combined test with no target. The shape
predicate is `output_topology.topology_is_subless_passive_mains`; its sibling —
passive mains PLUS a sub — still compiles a (degenerate 1-way) active profile
and keeps the full ladder.
**Exactly one step is ever `active`** — the first unfinished step this speaker
can still reach. Each step tells `build_commissioning_view` only whether it is
finished and whether this shape will ever run it;
`_derive_step_statuses` decides which one holds the baton, and every step
message is chosen from that derived status so a `todo` step can never carry
done-state copy. A waiting step names the rung that actually holds the baton
(`_waiting_message`) rather than a fixed prerequisite — on a box with no saved
layout the values rung reports "not needed", so copy hard-coded to it demanded
work the card above had just called unnecessary. Steps after the live one are
`todo`, and the combined-driver test's own action is disabled until both the
values and the outputs are done — `jasper-web`'s staging refusal stays as the
backstop behind it. Before 2026-08-06 each step decided "am I active?" alone, so
a redrawn active 2-way that still carried confirmed outputs lit up the values
step AND the combined test three rungs below it; the household pressed the test
and the graph fail-closed on a stale staged config with no way forward.

Combined-test failure copy is owned by `summed_test_failure_message` and grouped
by the ACTION available to a household, not by the backend's cause: one code
(`commission_startup_anchor_not_staged`) covers every staging blocker, and the
sub-cause is not forwarded to the coordinator, so the copy routes ("go back to
Add your components") without diagnosing ("the preview is stale") — issue #2184
tracks plumbing the sub-code through. An unmapped blocker's own prose is
sanitised (`_household_safe_reason`: absolute paths and exception classes
stripped, backend vocabulary rejected outright) before it is shown, because that
prose carries all three and the no-jargon guard in `tests/test_sound_setup.py`
reads only the JS. The raw code still rides `combined_groups[].failure_code`, a
structured field, so a new failure mode is loud without leaking onto the card.
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
physical DAC outputs from the active-speaker runtime route. A DAC8x topology can
describe eight physical outputs, but active commissioning/apply is enabled only
for a DAC profile that declares an outputd-owned active lane wide enough for the
assigned outputs. Subwoofer add-ons count as real assigned DAC outputs; a layout
that uses the next free output needs one more lane, and a sparse assignment needs
lanes up to the highest assigned output.
Saving a speaker-layout draft is a complete topology JSON
replacement and only runs backend validation; it does not play sound or apply a
DSP graph. It does start `jasper-audio-hardware-reconcile` (fire-and-forget,
best-effort, never failing the save) so the runtime converges toward the newly
declared intent instead of drifting until the next boot. That convergence is
real: on a live box running a commissioned active graph, an edit that
invalidates that graph flips the reconciler's gate from active to passive and
bounces outputd, so output parks until the layout is re-commissioned or
reverted. The household sees that on the Status dashboard, not here: the parked
sentence has one writer (`_parked_signal` in
[`jasper/control/audio_health.py`](../../jasper/control/audio_health.py)) and the
dashboard renders it as the Audio card at the top of `/system/` and in the
current-stream card on `/system/audio/`. `/sound/setup/` itself carries no
parked banner. A brief parked flash during the post-save reconcile window is
possible and self-clears.
The same payload carries a clock-domain report that records
the current single final-output device assumption; aggregating multiple USB DACs
is explicitly not enabled for product active-crossover playback yet. The
confirm-outputs card shows a top-down speaker stack visual plus flat **DAC
output assignments**. Each driver row has a channel selector; if there are
exactly two physical outputs and two drivers in the group, choosing one output
auto-fills the peer driver with the remaining output. Larger output sets
require explicit unique choices. Saving the draft reruns backend validation,
and the backend still rejects duplicate or missing physical-channel
assignments. Each assigned driver row can also run a guarded quiet **Play**
audition through the normal commissioning ramp in identity-audition mode. That
mode lets the operator hear the physical driver before channel identity is
confirmed, but it still requires the saved topology, staged protected config,
software guards, path-safety evidence, calibration floor, Stop/session control,
and CamillaDSP rollback gates. Marking or clearing identity evidence stores
operator-confirmed physical channel identity in the topology contract; it is
not playback permission and it does not satisfy tweeter protection or later
path-safety blockers by itself.
The bottom **Reset speaker setup** action is a recovery control. It stops any
active-speaker tone/session, resets `/var/lib/jasper/output_topology.json` to an
unconfigured zero-group draft, kicks audio-hardware reconcile, and clears the
active-speaker setup/evidence JSON artifacts so stale staged configs,
measurements, or baseline candidates cannot masquerade as current after the
topology reset. Detected hardware is retained only as metadata. Reset does not
infer passive intent, emit sound, or delete generated CamillaDSP YAML; audio
stays parked until the household explicitly saves a layout.
The component card stores a versioned working draft per physical output. Its
default flow starts with one compact card for every independently amplified
driver. Each card asks for make/model plus the physical choice the research
assistant cannot infer: low-frequency drivers expose enclosure/acoustic loading,
tweeters expose their topology-owned exact driver/loading type, and every driver
can declare an optional in-line L-pad or resistor. One optional **Build notes**
field follows the complete component list and captures any shared enclosure,
passive-radiator, amplifier, mounting, or other whole-speaker context. There are
no per-driver free-text configuration boxes. These values use the existing
`manual_settings.drivers` owners except tweeter `driver_style`, which uses the
existing `SpeakerChannel` owner and auto-saves through the output-topology
writer. The page does not maintain a second component store. A passive
one-driver layout receives a research-only physical target through
`driver_research_targets`; measurement and commissioning remain restricted to
independently amplified active drivers.

After the component cards and Build notes, **Copy prompt** asks the server to generate one
exact, fingerprinted request from the current topology, per-output models,
visible safety context, cabinet facts, and build notes. The copied text embeds a
compact *projection* of that request — target identities, models, driver style,
and operator-declared context — not the whole object; the hardware inventory and
physical output labels are server bookkeeping an assistant cannot use. It asks
for exactly one fenced `json` block back, and asks only for fields something
downstream reads (the parser still accepts the wider v2 schema, so a more
verbose reply is not rejected). The operator pastes the assistant's reply —
fence markers and surrounding prose are tolerated — and selects
**Load information**. A compact proposed-crossover
summary appears before a single collapsed **Advanced** editor. Advanced groups
driver specifications, hard never-test-beyond frequency edges, the narrower
measurement band, required high/low-pass cutoff and
slope, cabinet geometry, level/duration limits, trim, crossover candidates,
provenance, and unknowns. All editable values and research evidence are rendered
as labeled sections directly inside Advanced; it contains no second-level
disclosures.

**Saving the declaration is declaring it — there is no confirm step.** Every
save of the driver details stamps the safety profile over the values it just
wrote, so an ordinary edit can no longer leave the speaker unmeasurable waiting
on a second human click. That ceremony was retired because the crossover-accept
seam could not perform it: accepting a measured crossover writes the Sound
declaration itself, which rotated the fingerprint, cleared the confirmation, and
then refused the very measurement loop that produced the value.

What still refuses every crossover measurement is a declaration JTS cannot use —
`incomplete` (blocking issues in the declared values), `stale` (the outputs
moved underneath it), or `malformed` — and each needs a different edit before a
save can succeed. Whenever
`driver_safety_profile_evaluation.confirmed_and_current` is not `true`, the
component card hoists a **Review the safety limits** callout to top level, above
the component list, naming which state it is in and what to change (issue
#1820). It carries no action button: the remedy is an edit plus the card's
ordinary Save. Its DOM id, `confirm-safety-limits`, is the deep-link target the
measurement wizard's `program_profile_not_confirmed` hard stop points at
(`/sound/setup/#confirm-safety-limits`); the page opens the owning setup step
and scrolls the callout into view rather than relying on fragment behaviour.

The research prompt treats operator-declared physical installation choices as
authoritative. The browser import boundary also refuses to replace enclosure
kind, the topology-owned tweeter type, an explicit product-technology
`driver_class`, or an operator-declared resistor pad; research can still fill
missing product and safety values. Legacy per-driver `notes` values remain
readable in the design/safety record but are not sent as invisible prompt
context; the visible Build notes field is the only free-text build context.
On load, an older v2 request/result fingerprint that included that hidden text
is demoted while its normalized manual values and safety profile are preserved,
so the next save cannot fail against an obsolete prompt contract. The bounded
v2 response must echo that request and every physical target; additions,
removals, or edits to the current request context make old research stale.
Imported research remains
advisory: it prefills visible fields and preserves bounded provenance/unknowns
for review, while operator-edited targets stop displaying stale provenance. A
routed local subwoofer remains owned by the subwoofer card's bass-management
corner and is not required research for the active-main preview. The optional
whole-speaker Build notes field is capped at 1000 characters. Research-produced
per-driver safety summaries remain capped at 2048 characters; full reports
belong outside the draft.
Nothing in this flow applies filters, reloads CamillaDSP, or authorizes sound.
Every save records the declaration time on the profile it just froze; a visible
safety edit or topology change rotates the fingerprint and the next save
re-freezes over it, and none of it grants playback authority. Saves use an
optimistic revision. If another tab wins, the browser keeps local unsaved
edits, adopts the fresh server revision, and asks the operator to review and
save again instead of silently replacing their work.
Choosing an active driver in **Confirm outputs** calls the commission
route family: `commission-load`, `commission-ramp-step`,
`commission-ramp-ack`, and `commission-ramp-abort`. The browser does not expose
the protected staging/load/arm implementation steps as separate buttons or
status grids. The saved topology does not make
`outputd_active_content_playback` an audible test writer: that name is a
daemon-owned CamillaDSP/outputd lane (its aloop PCM definition was deleted
at P9-C — an armed roleful box now uses the ACTIVE ring instead — but the
ban holds regardless, since the name still resolves in code and a
rolled-back box still names it). Tweeter/high-frequency targets are not
horn-specific: the backend auto-records a
software-guarded bring-up request when no physical protection evidence is
present, and the shared driver-test signal policy still enforces
role-specific frequency caps and high-pass guards before the protected
commission ramp can make a driver audible.

Lower-level diagnostics still exist for tests and operator debugging:
`/sound/active-speaker/commissioning-view` composes a no-audio setup
view model from durable evidence, `/sound/active-speaker/stage-config` stages the
protected startup candidate, `/sound/active-speaker/check-path-safety` writes
the path-safety evidence, and `/sound/active-speaker/load-startup-config` loads
the protected graph. The normal product UI does not require a user to understand
or click those controls.
The same walkthrough then opens **Validate and apply**. That card first runs a
spoken combined-speaker test through
`/sound/active-speaker/summed-test`; while that request is preparing/playing,
the same CTA moves through **Preparing** into a red **Stop** state backed by
`/sound/active-speaker/summed-test/stop`. The combined-test card has its own
bounded test-level slider so low-sensitivity drivers can be raised from the safe
floor without changing normal listening volume; while the loop is playing, the
slider posts live level changes to `/sound/active-speaker/summed-test/level`.
The backend reloads the protected all-drivers-live graph at the requested level
only after CamillaDSP accepts the new graph, clamps absolute min/max bounds, and
leaves the active loop metadata unchanged if the reload fails. **`duration_ms`
is how long the test plays, and the request returns when it has.** The
`start_combined_test` action the commissioning coordinator publishes carries
`duration_ms: 12000`, so a client that replays that action — the browser, or an
LLM driving the box over raw HTTP — gets a play that ends itself and a
**completed** test whose record is `captured`. One connection is enough: the
same client can POST the validation next without racing
`active_summed_test_running`. Omitting `duration_ms` keeps the open-ended loop,
which then continues until the operator stops it, presses **Sounds right**, or
the backend watchdog expires it. Stopping early is still an incomplete test
either way — a bounded play only removes the need for a second connection, it
does not decide whether the crossover blends. The summed crossover validation
POST at
`/sound/active-speaker/summed-validation` must reference the latest audible
combined-test record for that group: artifact-only, stale, stopped-before-audio,
or watchdog-expired records cannot unlock the active profile. For the current
product flow, an explicit operator listening result (`operator_listening_check`)
can validate **Sounds right** when no browser microphone reading is captured.
The `/sound/setup/` core flow no longer offers a phone-mic capture button;
microphone-based level and delay work belongs in the HTTPS
measurement/correction experience. After summed validation,
`/sound/active-speaker/baseline-profile` compiles the saved topology, visible
crossover settings, fresh crossover preview, driver-check evidence, and summed
validation into
`/var/lib/camilladsp/configs/active_speaker_baseline.yml` plus
`/var/lib/jasper/active_speaker_baseline_profile.json`. Compile is still
no-audio and does not load CamillaDSP. The UI exposes one final intent,
**Save and apply**, through
`/sound/active-speaker/baseline-profile/save-and-apply`. That backend-owned
operation compiles, validates hardware apply support, applies through the shared
DSP handoff, and reports one result. The product handoff is currently enabled
only for the outputd-owned active output lane; unsupported hardware paths show
clear apply-blocked copy and do not expose a fake save CTA that cannot make the
profile active. After apply succeeds the UI can truthfully say this is now the
active speaker profile.
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
[`HANDOFF-active-speaker-dsp.md`](../HANDOFF-active-speaker-dsp.md).
The next `/sound/setup/` slice should exercise the lab-gated quiet woofer/mid path on
hardware, then use the same driver-aware microphone loop for a protected
high-frequency target.

---

Archived 2026-08-26 from `docs/HANDOFF-sound-preferences.md`. Route names,
issue numbers, and step behaviour are as-written at the time of each pass.
