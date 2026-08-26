# HANDOFF: active speaker DSP commissioning

> **This file is current operational truth.** Its original 2026-05-25 →
> 2026-07-16 narrative moved to
> [historical/active-speaker-dsp-investigation-history.md](historical/active-speaker-dsp-investigation-history.md),
> preserved verbatim as primary-source archaeology and deliberately not kept
> in sync with the code. Read this file for what the system does today; read
> the history for why it ended up this way.

This doc is the canonical handoff for JTS speakers where CamillaDSP
directly drives woofer/midrange/tweeter amplifier channels (an
"active" speaker) instead of a passive in-cabinet crossover. JTS3
(DAC8x + a real bi/tri-amp speaker) is the only hardware running this
path today; other production units run passive until commissioned. The
InnoMaker HiFi AMP Pro now declares a width-2 active lane, so the active
layout is selectable there too.

It owns DSP topology, layer boundaries, and hardware-safety contracts.
Product behavior, the manual-vs-measured parameter split, the guided
user journey, and delivery acceptance criteria are canonical in
[active-crossover-information-design.md](active-crossover-information-design.md).
The crossover *measurement* flow (how CHECK/MEASURE/APPLY/VERIFY
actually run today) is canonical in
[tuning-operator-runbook.md](tuning-operator-runbook.md).
The output *transport* (how a commissioned graph reaches the DAC) is
canonical in
[HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md).
This doc links to both rather than restating them.

## Current Operational Truth

### What this is

Active speaker DSP asks "what should this speaker be before the room
is considered?" — a separate question from room correction ("what
should be compensated at this listening position?") and preference
voicing ("what tonal tilt does this listener like?"). The
`/sound/room/` room-correction wizard must never rewrite crossover,
polarity, per-driver gain, driver delay, or limiter policy; those
belong here.

### The layer model (adopted 2026-07-23)

Active speaker tuning is five layers, composed in fixed order inside
one CamillaDSP graph. Full rationale, the correction-envelope math,
and the phased execution plan are in
[active-speaker-tuning-layers-design.md](active-speaker-tuning-layers-design.md)
— read that before touching linearization, trim-solve, or
verification code.

| # | Layer | Job | Re-runs when |
|---|---|---|---|
| 1a | Driver linearization | flatten each driver within its own band on the design axis (horn/CD compensation, baffle step, breakup) | hardware change (driver, horn, pad) |
| 1b | Crossover integration | drivers sum correctly: crossover filters, scalar trim per driver, relative delay, polarity | hardware/geometry change |
| 2 | Bass | sub/extension integration below the gated measurement floor | hardware/placement change |
| 3 | Room correction | modal peaks below the transition, at most a gentle broadband tilt above | placement/room change |
| 4 | Preference | declared taste on top of honest-flat | whenever |

Layers 1a+1b are **the speaker layer** — what earlier revisions of
this doc called "Layer A." Layer 1a is new: the fit engine landed
2026-07-23 (#1668 PR-A/B/C —
[`linearization_fit.py`](../jasper/active_speaker/linearization_fit.py) +
[`linearization_envelope.py`](../jasper/active_speaker/linearization_envelope.py)).
**Layer 1a now emits (#1668 PR-D): `baseline_profile` threads the
fitted per-role filters (`linearization_fit.linearization_filters_by_role`)
into the applied CamillaDSP graph — but the emitted curve is not yet
hardware-validated.** Do not assume linearization has been confirmed
audible; [tuning-operator-runbook.md](tuning-operator-runbook.md)
"Current status" owns the emission's validation state, and the design
doc's "Execution plan for the implementing session" owns phase status.
Layers 3 and 4 (room correction, preference) are unchanged from before
and already land on the active graph for a solo speaker — see "Layer
Boundary" implementation status in the investigation history, still accurate.

### The two commissioning surfaces

Two web surfaces, two different jobs, run in sequence:

1. **`/sound/setup/`** (Active crossover setup) — physical topology: detect
   DAC outputs, group them into speakers, assign driver roles up to
   3-way, mark subwoofers, confirm each driver's identity and level by
   ear (`commission-load` / `commission-ramp-*`), stage a
   muted/protected startup graph. This is prerequisite substrate for
   *both* measurement flows below and doesn't change with which one is
   active. Backed by `jasper.output_topology` and
   `jasper.active_speaker.{staging,startup_load,commission_ramp,
   calibration_level,safe_playback,bringup,playback}`. Its APIs deliberately
   remain under `/sound/active-speaker/*`, including Stop and abort controls,
   so an older open setup tab retains control of an audible commissioning
   session.
2. **`/sound/crossover/`** (labelled **Active speaker**) — measures and
   applies crossover level/delay/polarity and linearization. **v2 is the only
   flow** —
   see
   [crossover-v2-engine-design.md](crossover-v2-engine-design.md),
   canonical for this flow's file map and invariants. W5b (2026-07-24)
   deleted the legacy near-field/null-depth/gated-summed triad
   described at length in the investigation history, along with the
   `JASPER_CROSSOVER_FLOW` selector that used to choose between the
   two; a stale `JASPER_CROSSOVER_FLOW=legacy` carried on an old box
   now selects nothing — no code reads that variable. (A
   `build_crossover_envelope` shim in
   [`crossover_envelope.py`](../jasper/active_speaker/crossover_envelope.py)
   used to forward to v2; it has been deleted, and callers reach
   `build_crossover_envelope_v2` directly or through that module's
   `build_crossover_envelope_logged`.) Do not treat the investigation history's "Consumer
   Wizard Triad," "Delay, Phase, and Null Verification," or the Wave
   1-3 commissioning-receipt narrative as the current flow. Some of the
   machinery they describe outlived it — the `CrossoverLevelLease` and
   the capture geometry are shared with v2
   ([HANDOFF-correction.md](HANDOFF-correction.md) owns which parts
   survive) — but the flow that drove it is gone. The existing correction
   service still owns this flow; `/correction/crossover/` remains a direct
   compatibility alias.

### File map (entry points only)

`jasper/active_speaker/` is 75 modules. Per-file responsibility
tables for the measurement flow live in the canonical docs linked
above; this is only the entry points:

| File | Owns |
|---|---|
| [`jasper/output_topology.py`](../jasper/output_topology.py) | Physical DAC-lane / speaker-group / driver-role topology. No audio side effects. |
| [`jasper/active_speaker/camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py) | Every CamillaDSP YAML emitter — startup, commissioning-masked, and baseline. |
| [`jasper/active_speaker/baseline_profile.py`](../jasper/active_speaker/baseline_profile.py) | Compiles and applies the durable active-speaker baseline; owns the candidate/promote lifecycle below. |
| [`jasper/active_speaker/staging.py`](../jasper/active_speaker/staging.py), [`startup_load.py`](../jasper/active_speaker/startup_load.py), [`commission_ramp.py`](../jasper/active_speaker/commission_ramp.py) | `/sound/setup/` topology-setup substrate: stage a muted graph, load it, run the per-driver by-ear ramp. |
| [`jasper/active_speaker/runtime_contract.py`](../jasper/active_speaker/runtime_contract.py) | Classifies saved topology against the running/candidate CamillaDSP graph; the doctor's fail-closed authority. |
| [`jasper/active_speaker/crossover_v2_flow.py`](../jasper/active_speaker/crossover_v2_flow.py), [`linearization_fit.py`](../jasper/active_speaker/linearization_fit.py), [`linearization_envelope.py`](../jasper/active_speaker/linearization_envelope.py) | The v2 conductor and the Layer 1a fit/envelope. Full map: [crossover-v2-engine-design.md](crossover-v2-engine-design.md) "File map". |
| `jasper/active_speaker/commissioning_*.py`, [`driver_acoustics.py`](../jasper/active_speaker/driver_acoustics.py) | Substrate built for the deleted legacy Wave 1-3 nine-state receipt lifecycle. Not a selectable flow any more; parts still back v2 (`commissioning_run` → `correction_crossover_backend`, `driver_acoustics`'s sweep constants → `web_measurement.capture_sweep_meta`). |

### Key invariants

- **Two-invariant protection model.** Every audible path enforces
  exactly two things: never too loud (one derived per-driver ceiling
  from declared sensitivities) and never the wrong frequency range
  (declared band behind a proven high-pass before any full-range
  content reaches a driver). Canonical statement:
  [crossover-v2-engine-design.md](crossover-v2-engine-design.md)
  "Contracts & invariants". The investigation history's longer "Hard Safety Rules"
  and "Failure Modes To Keep Visible" lists are still true detail,
  just superseded as the primary framing.
- **One audio path.** Commissioning tests, measures, and applies
  through the same production outputd-owned active CamillaDSP graph
  that plays music — never a direct-DAC bypass. There is no separate
  validation path; a config that can't reach the production active
  lane can't be commissioned. (The historical direct-DAC path was
  deleted 2026-06-17; narrative in the investigation history's "Single audio path
  commissioning.")
- **Crash recovery always lands muted.** Per-driver unmute states
  during commissioning are transient; only the final validated freeze
  step persists a loadable config, and every staged boot candidate is
  asserted fully muted (`staged_candidate_fully_muted`).
- **Statefile seeding has three outcomes, not two** (issue #2135).
  `safe_graph_for_current_topology` selects the flat cutover graph for a
  passive topology, the staged all-muted startup graph for a
  roleful/protected one — and, when a roleful topology has staged **no**
  startup graph at all, returns `parked_muted`: a generated,
  topology-derived graph that is silent twice over (a `File` sink at
  `/dev/null`, so no DAC is attached, plus a wired hard mute on every
  physical output). `apply_safe_graph_decision_to_statefile` materialises
  it at
  `/var/lib/camilladsp/configs/active_speaker_parked.yml` and re-proves
  both properties before the bytes reach disk. The deploy then **succeeds**
  rather than failing closed, so a household that pauses between
  "topology declared" and "crossover preview staged" does not wedge every
  deploy. What did *not* change: the flat-graph-on-roleful-topology
  refusal, and a staged graph that exists but fails its safety proof
  (that still blocks, with its blockers — a commissioning bug is not a
  paused household). A topology-level blocker (a half-assigned mid-edit
  draft, a channel saved `protection_status="required_missing"`) does
  **not** block: issue #2145 exempted the parked verdict from
  `classify_camilla_graph`'s blanket "any issue refuses" tail, because the
  parked graph's safety is *structural* — a `File` sink and a wired hard
  mute on every output, both re-proved off its own bytes — and no fact
  about the saved layout can falsify either. Refusing on one only stopped
  the box parking; it never made it quieter. The one topology-dependent
  parked check, `parked_graph_width_too_narrow`, lives inside that
  structural proof and still applies. The blockers stay **visible**: the
  parked decision reports them, so `runtime-safe-graph` prints each one in
  the install transcript while exiting 0, and `jasper-doctor`'s
  `check_active_speaker_topology_blockers` warns with the blocker names and
  the wizard step. Only a graph's *own* defects (a bad
  `devices.volume_limit`) still refuse a parked graph.
  Recovery is automatic once valid speaker intent exists:
  the parked branch is last in the selector, so the moment commissioning
  stages a startup graph the `select_active_startup` branch wins on the
  next runtime convergence pass. `jasper-audio-hardware-reconcile` now runs
  that selection on every successful pass, including boot and hotplug. Both
  exits out of parked work: the parked graph is
  an accepted `path_safety.restore_classifications` rollback target so
  `/sound/setup/` can start commissioning on a parked box, and
  `jasper-output-topology-reset` (and the `/sound/setup/` reset endpoint)
  converges CamillaDSP in-process over the websocket. Reset itself writes an
  unconfigured zero-group topology and remains parked; audio becomes eligible
  only after the household explicitly saves a passive layout or completes the
  protected active-speaker setup.
  Surfaces: `jasper-doctor`'s `active speaker
  runtime graph` WARNs with the next action (choose and save a speaker layout,
  or finish crossover preview to stage a startup graph where supported; on a
  DAC that declares no active outputd lane, active commissioning cannot
  complete there),
  `/state.resilience.active_speaker_parked` carries the same pair, and
  `/state.audio_health` keeps reporting the parked shape
  (`audio_health.PARKED_HEADLINE`).
- **Re-commissioning a committed box: the reconcile must not restore the
  baseline over the staged anchor mid-load.** `load_protected_startup_config`
  writes the all-muted staged startup anchor to the durable statefile and then
  kicks `jasper-audio-hardware-reconcile`; on an already-commissioned box that
  reconcile's selection would otherwise pick `select_active_baseline` and repoint
  the statefile off the anchor. `commission-load` then refuses at its **pre-audio
  precondition gate** (`commission_active_graph_not_staged`,
  `startup_load.py`), whose blocker reads: per-driver commissioning
  "requires the all-muted staged config to be the persisted boot config first".
  (That gate runs before the load, so the later S3 durable-drift check inside the
  persist phase is never reached.) `load_protected_startup_config` therefore
  takes an **ephemeral `/run` hold marker**
  (`jasper.active_speaker.startup_hold`) before it applies anything; while it is
  present `safe_graph_for_current_topology` preserves the staged all-muted anchor
  above the baseline-restore rung. **One TAKE and three RELEASEs own that marker**, which is
  every writer in the tree: the load TAKES it; the load's own `finally` RELEASES
  it when the apply does not stick; `rollback_protected_startup_config` RELEASES
  it when the anchor is deliberately abandoned; and
  `baseline_profile.persist_applied_baseline_profile` — the apply seam every
  "a baseline is now applied" path funnels through — RELEASES it when a
  commission COMPLETES, because a baseline is what boots then. Without that third
  one the marker outlives the commission that took it (seen on jts3 after a
  successful save-and-apply); it is inert while it lingers, since the rung also
  requires the current graph to classify as all-muted-active-startup, but it
  surprises the next commission and makes the doctor's "marker present" state
  ambiguous. The marker is in
  `/run`, so a normal boot never sees it — a commissioned box still restores its
  baseline on reboot — and preserving an all-muted anchor keeps the box silent,
  never loud, so this is gated only on the hold, not on identity (#2814's identity
  gate stays on the approved-runtime rungs).
  **The unit owns the marker directory, and a load that cannot be held refuses.**
  **Three** units reach the writer: `jasper-web` (`User=jasper-web`,
  `ProtectSystem=strict`) through the `/sound/` commissioning flow and its
  `POST /active-speaker/load-startup-config` route; `jasper-correction-web`
  (root, `ProtectSystem=full`, `UMask=0077`) through `/correction/`'s
  driver-capture and level-match arms, via
  `web_commissioning._ensure_commission_startup_anchor`; and
  `jasper-web-streambox.service` (root, `ProtectSystem=full`), which is the same
  `python -m jasper.web` process installed AS `jasper-web.service` on a
  streambox. All three can CLEAR the hold — `jasper-web` from
  `/active-speaker/rollback-startup-config`, and any of the three through the
  completion release at the baseline apply seam, which `/correction/`'s
  crossover-v2 apply and restore reach as well. What is unique to `jasper-web`
  is that its sandbox blocked the WRITE: `ProtectSystem=strict` mounts the
  hierarchy read-only apart from `/dev`, `/proc`, and `/sys`, so it cannot
  create `/run/jasper-active-speaker` itself. `deploy/jasper-web.service` declares
  `RuntimeDirectory=jasper-active-speaker` (mode 0755,
  `RuntimeDirectoryPreserve=yes` because the unit is socket-activated and idles
  out while a hold is live), which systemd creates as `jasper-web:jasper` and
  excludes from `ProtectSystem=`. The two root writers and the root reconciler's
  read need nothing further. **A marker that already exists still holds**, even
  when this writer cannot rewrite it — the root writers leave it `root:root`
  0600, `touch()` raises there, and `hold_staged_startup` therefore answers from
  the marker's presence rather than from its own call; `unlink` needs write on
  the 0755 directory, not on the file, so release works from either identity.
  When the hold cannot be taken at all,
  `load_protected_startup_config` returns `status="blocked"` with the
  `staged_startup_hold_unavailable` blocker naming the directory — it applies
  nothing, because a load whose durable half the next reconcile would undo must
  not answer success.
- **The applied baseline candidate is always a source-fingerprinted
  sibling, never the canonical filename, until a promote step runs
  (issue #1666).** `baseline_profile.build_baseline_profile_candidate`
  writes every candidate to
  `active_speaker_baseline_candidate_<fingerprint>.yml` beside the
  canonical `active_speaker_baseline.yml` — never in place — so a
  candidate that fails validation or activation can never appear at
  the canonical name. (Before this fix, the target alternated
  canonical/sibling across successive applies, so roughly half the
  time unvalidated bytes landed on the canonical name before
  CamillaDSP confirmed them.) Only after `apply_dsp_config` has
  confirmed the candidate live does
  `baseline_profile.promote_applied_baseline_candidate` byte-copy it
  onto the canonical name — fail-soft: a copy failure never fails an
  otherwise-successful apply. Promote also prunes candidate siblings
  down to the newest 20 (`_MAX_BASELINE_CANDIDATE_FILES`). Every
  apply/restore transaction in `baseline_profile.py`, and
  `commissioning_apply`'s `finalize_retained_candidate_apply`, runs
  promote right after its own `apply_dsp_config` succeeds. The running
  CamillaDSP graph and the JSON SSOT (`config.path`) are correct the
  instant apply succeeds; promote only keeps the canonical *filename*
  current for readers who trust it by name — the multiroom follower's
  solo-restore fallback, `jasper-doctor`, or an operator reading the
  file directly.
- **The doctor is the divergence check, not a second source of
  truth.** `jasper-doctor`'s `active speaker baseline canonical` check
  (`check_active_speaker_baseline_canonical` in
  [`jasper/cli/doctor/audio.py`](../jasper/cli/doctor/audio.py))
  compares the live applied candidate's Layer-A fingerprint against
  the canonical file's and WARNs — never fails — on mismatch; the
  running graph is always the audible truth regardless. Its sibling
  `active speaker runtime graph` check
  (`check_active_speaker_runtime_graph`) FAILs closed if a saved
  roleful/protected topology (any tweeter or subwoofer role) is
  running a flat full-range graph: `classify_output_contract` +
  `classify_bass_extension_graph` in `runtime_contract.py` are the
  shared classifier install/deploy must go through
  (`jasper-active-speaker runtime-safe-graph`), never a hand-written
  outputd statefile. A fourth check,
  `check_active_speaker_output_hardware_match`, flags a saved topology
  that no longer matches observed hardware. A fifth,
  `check_active_speaker_topology_blockers`, WARNs when a **parked**
  speaker's saved layout still carries topology blockers, naming each and
  the wizard step that clears it — since #2145 those no longer fail the
  deploy, so this is where they stay visible.
- **Room correction is solo-active only.** A grouped active leader
  returns `active_grouped_room_correction_not_supported`
  (`jasper.active_speaker.setup_status`); grouped support needs a
  later identity spanning both the leader and follower CamillaDSP
  instances. See
  [HANDOFF-distributed-active.md](HANDOFF-distributed-active.md).
- **The DAC-agnostic active-output transport is design-of-record and
  mostly built**, not still-in-progress the way the investigation history's
  "Staged, hardware-verified build sequence" narrates it — that
  section is the historical build log, not an open TODO list.
  Dispatch is on clock-domain shape (single coherent DAC vs. paired
  composite), never a per-DAC branch, so a new DAC of an established
  shape is a `DacProfile` row. Current status:
  [HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md)
  "Current Operational Truth" and "DAC-agnostic active-output
  transport".

### Operational commands

CLI (`jasper-active-speaker`): `startup-template`, `path-audit`,
`path-probe`, `environment-probe`, `runtime-safe-graph`,
`baseline-reemit`, `commission-load`, `commission-rollback`,
`commission-ramp {step,ack,status,abort}`. Full flags for every verb
above, and the `/sound/active-speaker/*` web-route surface with its
GET/POST and safety semantics: [testing-tooling.md](testing-tooling.md)
(search "active-speaker") — canonical, kept current there rather than
duplicated here. That table summarizes the web surface rather than
enumerating it; the route set itself is owned by the GET and
mutating-route allowlists in
[`jasper/web/sound_setup.py`](../jasper/web/sound_setup.py) — it takes
both, since read-only routes such as `environment` and `safe-playback`
appear only in the GET one.

Recovery when a saved topology has drifted from physical reality (for
example a physically passive box still carrying a stale roleful
topology, which parks the speaker silent — and still blocks the deploy
outright if a staged graph exists but fails its safety proof):

```sh
sudo /opt/jasper/.venv/bin/jasper-output-topology-reset --dry-run
sudo /opt/jasper/.venv/bin/jasper-output-topology-reset --yes
```

### Debugging entry points

```sh
sudo /opt/jasper/.venv/bin/jasper-doctor | grep -i "active speaker\|bass extension"
curl -s http://jts.local:8780/state | jq .active_speaker_output_safety
curl -s http://jts.local/sound/crossover/status | jq   # v2 conductor status
```

For crossover-measurement-specific debugging (capture retention,
per-capture diagnostic events, failure taxonomy), see
[historical/crossover-measurement-v2-campaign-record.md](historical/crossover-measurement-v2-campaign-record.md)
"Failure taxonomy & debugging". For general log/journal fetching, see
[testing-tooling.md](testing-tooling.md) "Pi-side diagnostics" and
AGENTS.md's evidence-first rule.

---

This doc's original 2026-05-25 → 2026-07-16 narrative lives in
[docs/historical/active-speaker-dsp-investigation-history.md](historical/active-speaker-dsp-investigation-history.md),
preserved verbatim as primary-source archaeology and deliberately not
kept in sync with the code.

Verification scope (2026-08-04): current route-surface scope only:
`/sound/setup/`, `/sound/crossover/`, and the unchanged
`/sound/active-speaker/*` commissioning namespace—including stale Stop/abort
control—were rechecked against the route configuration and focused tests; 173
unchanged commissioning/topology tests passed. No hardware behavior was
revalidated, and the historical narrative was not re-read. Prior 2026-07-24 (restructured into the canonical current-state-first
HANDOFF shape per issue #1681; the "Current Operational Truth" section above
was freshly re-verified against code at commit f59d5a776 -- the five-layer
tuning model (active-speaker-tuning-layers-design.md), the v2/legacy
crossover-flow split (crossover_flow.py, JASPER_CROSSOVER_FLOW), the #1666
candidate-promote lifecycle (baseline_profile.build_baseline_profile_candidate
/ promote_applied_baseline_candidate), the doctor's baseline-canonical and
runtime-graph checks (jasper/cli/doctor/audio.py), the jasper-active-speaker
CLI and /sound/active-speaker/* route surface, and the cross-links into
tuning-operator-runbook.md, HANDOFF-speaker-output-reference.md, and
active-speaker-tuning-layers-design.md. The investigation history is preserved
verbatim as historical narrative and was not independently re-verified in
this pass beyond the corrections called out in its own status note; treat its
file paths, line numbers, and "what's shipped" claims as of their original
dates, not current.)

Correction (2026-07-27): one entry in the manifest above is superseded — the
"v2/legacy crossover-flow split (crossover_flow.py, JASPER_CROSSOVER_FLOW)"
was retired by W5b hours after f59d5a776 was checked, so that file is deleted
and the selector selects nothing (see "The two commissioning surfaces"). The
`Last verified` date is deliberately NOT bumped: this footer records what was
checked at f59d5a776, and correcting one entry is not a re-read of the doc.

Correction (2026-08-15, audio-graph consolidation #2285 P9-C): the Stage 2
"wide content lane" design section (around "Stage 2 — reconciler + wide
content lane, dry-run" and "2a landed") described a
`__OUTPUTD_ACTIVE_CONTENT_CHANNELS__` render token that was never actually
implemented in `asoundrc.jasper`, and its raw `type hw` aloop substream
(snd-aloop pair 5) had its PCM definitions deleted at P9-C once the ACTIVE
ring became the roleful transport — corrected inline with pointer notes,
same rationale as the 2026-07-27 entry above: correcting these entries is
not a re-read of the doc, so `Last verified` stays unbumped. See
[audio-paths.md](audio-paths.md).

Doc-shape fix (2026-08-25): the historical narrative this file used to carry as
an appendix moved whole to
[historical/active-speaker-dsp-investigation-history.md](historical/active-speaker-dsp-investigation-history.md),
so what remains here is current truth only. Scope of the verification recorded
below is unchanged — the spine was re-read on 2026-08-04 and the historical
narrative was not — so `Last verified` stays unbumped, same rationale as the
two corrections above.

Last verified: 2026-08-04
