# Hardware pass brief — loopback-retirement Phase 1 (bonded round-trip on the grouping ring)

For the operator-run Pi-side agent. The cloud session that built PR-2..PR-6 has
no Pi access; this brief is the complete handoff. Read alongside: the sealed
design §10.2 (captures/DESIGN-PROPOSAL-grouping-ring-2026-08-17.md — its exact
text governs the spikes' numeric bars; this brief adds the campaign's banked
findings, it does not replace §10.2), the evidence template
(captures/8.7-EVIDENCE-jts-local-2026-08-17.md), and the lifeline's 2026-08-19/20
tail entries (captures/PLAN-loopback-retirement-2026-08-18.md).

## Fleet + permissions (non-negotiable)

- **jts.local** = `pi@192.168.1.74` — ALWAYS the LAN IP for deploys. Dual-Apple
  composite roleful, ring-armed, dummy loads STAY (owner ruling OD-3; acoustic
  p99 re-scoped, owed against #889). **LEADER** (OD-1).
- **jts4** (`ssh jts4`) — Zero-2W streambox, InnoMaker, ring-armed,
  grouping_allowed=true. **FOLLOWER, dumb member** — it is flat; the
  active-follower instance is undemonstrated on this fleet and the evidence
  file must say so (panel S8).
- **jts3 FORBIDDEN** (peer session's box). **jts5 unplugged — leave it.**
- Deploys: `bash scripts/deploy-to-pi.sh` ONLY, dedicated detached worktree per
  target, from the laptop checkout.

## Step 0 — prerequisites, in order

0.1 **Heal jts4's dead reconcilers** (failed since the 08-17 14:16 boot — a
    jasper-usbgadget restart timeout cascaded):
    `sudo systemctl start jasper-grouping-reconcile.service jasper-source-intent-reconcile.service`,
    confirm `is-active` both, capture the journal into the evidence file. If
    the observed dead window offends household tolerance, that is a finding to
    fix in-session (design §10.2 step 0 item 1).
0.2 **Apply jts.local's durable baseline** (OD-2). It currently runs the
    staged-startup safety graph, so `grouping_allowed=false` blocks BOTH roles
    (PC-2). Verify before/after:
    `curl -s http://192.168.1.74:8780/state | jq .active_speaker_setup.grouping_allowed`
    → false before, true after. (Note: setup_status.py's sites moved post-seal
    — :719/:836/:1210 — re-read if anything surprises.)
0.3 **Deploy the sealed stack to BOTH boxes** (the campaign branch tip — SHA in
    FINALIZE below). `jasper-doctor` green on ring/coupling checks on both;
    known parked-mic / chip-AEC / calibration warnings unchanged. ALSO run
    `sudo /opt/jasper/.venv/bin/jasper-doctor | grep -i 'snapcast version'`
    on both boxes — the first real-binary verification of PR-4's live version
    probe (container-verified only by shape; any surprise degrades to an
    honest ok-skip naming the cause — record whatever it prints).
0.4 **Record jts4's classification**:
    `curl -s http://jts4:8780/state | jq '.active_speaker_setup | {active, active_group_count}'`
    — `active_group_count == 0` ⇒ passive ⇒ the grouping ring opens on
    jts.local alone (PC-6/S8).
0.5 **Confirm the emitted follower YAML on the bonded box**: `chunksize: 128`,
    `enable_rate_adjust: false` (cheap glance — code-established by
    resolve_output_layout's unconditional active-ring return; PR-3 panel).

## Spikes — run in this order; S0 gates everything after it

- **S5** — demoted: opportunistic pre-deploy baseline only, gates nothing.
- **S0** — does snapclient negotiate against the ioplug at all (the
  snapcast#1154 −77 shape is the known failure signature). Fallback geometry
  if falsified: 256×16 (§3.2) — with the L-05 truth in mind: that geometry
  already runs in production on eight renderer-lane PCMs on this fleet, but
  snapclient's OWN negotiation against it is exactly what S0 must demonstrate.
- **S1** — rate-adjust inertness on the ring capture.
- **S2** — delay honesty / ripple / occupancy; THE `--latency` number comes
  from here. **Panel addition — capture the `.delay` series across BOND-START
  specifically**: expect a ramp to ~42.7 ms during the readerless window
  (writer free-runs, drop-oldest), saturation there, then ONE step down at
  reader attach — a single hard sync, inside the 60 s settle. A measured max
  above 45.3 ms, a non-saturating delay, or repeated hard syncs = STOP and
  escalate (it falsifies the free-run-drop assumption; the `.delay` dead-mode
  discount becomes owed as a code change — pcm_jts_ring.c records the
  accepted reasoning at the callback).
- **S3** — THE gate: ≥2 h then ≥24 h electrical soak, zero hard syncs after
  the first 60 s settle. Load-bearing now, not a nice-to-have: snapclient's
  resync is the SOLE clock tracker post-flip.
- **S4** — the >2 s dead-reader cliff.
- **S6** (new in v2, B1's hardware signal) — bonded leader under shm_ring:
  camilla#1's log shows the ring capture attached, zero short reads, the
  snapfifo has a live reader. **Leader silence with healthy daemons =
  EXPLICIT FAIL** — the exact shape B1 exists to prevent.

## S6 / B1 probes (from the PR-5 panel — the whole hazard class is
green-everywhere-and-silent, so every "healthy" reading below must be
corroborated by audible flow)

1. **camilla#1's LIVE capture** read from the instance (GetConfigFilePath →
   the loaded YAML), NOT from LEADER_BAKE_CONFIG_PATH. Must be
   `jts_ring_capture`. On-disk right + live wrong is the re-emit hazard's
   signature.
2. **Ring A attached, zero short reads / capture xruns on camilla#1** — the
   bake's chunk is DAC-derived (256 on jts.local; 1024 fleet worst case)
   against a 2-slot × 128-frame Ring A; the design declined forwarding
   chunksize — this is the residual to MEASURE (expect latency/burstiness,
   not overrun; record numbers).
3. **Live SNAPFIFO reader** with non-zero bytes flowing + non-zero snapclient
   stream stats on EVERY member.
4. **THE EQ-SAVE PROBE** — with the bond up and ring armed, save an EQ change
   at jts.local/sound/, then immediately re-read (1). Repeat after a
   /correction/ run and after a correction reset. Run this EVEN with the
   re-emit fix landed — it is the fix's proof.
5. **Disarm/re-arm the coupling while bonded** — the coupling reconciler and
   the bond apply are separate writers of camilla#1's capture; confirm a
   flip in either direction leaves the two agreeing. Also: arm shm_ring
   explicitly on the bonded leader and play WITHOUT running
   jasper-grouping-reconcile — confirm whether the bond stays audible
   (the stale-bake window; check /state.grouping honesty while it lasts).
6. **check_grouping_rate_adjust on the ACTIVE follower** (newly in scope):
   expect ok, not the new "could not confirm" warn.
7. **The C-9 cell** if stageable: ring-armed + corrupt topology.json must
   refuse with topology_unreadable and leave the box solo-active, still
   playing its own content. Also: a bonded member with NO saved
   output_topology.json now admits shm_ring (lane cleared, nothing to
   strand) — one live check that such a box isn't silent for an unrelated
   pre-existing reason. And on a solo box with corrupt topology.json,
   check the journal WARN rate (post-fix: bounded, not 1,440/day).

## Resilience drills (from the PR-3 panel — run on metal)

- **SIGKILL snapclient mid-bond**: camilla emits paced silence (no wedge),
  snapclient respawns at 3 s and takes the ring FIRST TRY (no `-EBUSY` in the
  journal), audio resumes with no operator action.
- **Bounce camilla mid-bond**: bounded resync — `.delay` pins at ~42.7 ms
  then recovers at attach; no hard-sync storm.
- **Perms**: after the first bond AND after a snapclient restart cycle,
  `/dev/shm/jts-ring/grouping.ring` is mode 0660, group jts-ring.
- **Stale-geometry awareness** (the one bounded-but-not-self-recovering
  shape): the ring file survives deploys and the installer deliberately does
  NOT unlink it. If GROUPING_RING_* geometry ever changes, both ends fail
  closed and a REBOOT clears tmpfs. Symptom: snapclient `failed` after 6
  retries + a camilla capture-open failure — the bond simply won't form.

## Known pointers (don't rediscover these)

- `jasper-camilla.service` carries the campaign's three recorded latent gaps
  (StartLimitBurst=5 — the test exemption; RestartSec=2 == the liveness
  window; no UMask). If the soak ever produces a camilla EBUSY run, look
  there first.
- **No runtime escape hatch post-flip**: the GROUPING_LOOPBACK_* env
  overrides are deleted and the ring PCM name is a literal. A misbehaving
  ring path on metal means redeploy/revert, not an env flip.
- The unarmed/DAC bonded shape does NOT ship (resolve_output_layout returns
  the active ring unconditionally) — withdrawn as a test item by the panel.
- Expect the wifi-guardian subprocess tests to warn (not fail) under load if
  lanes are ever run on-box — known class, not a finding.

## Close-out

Evidence file at §8.7 grade (mirror captures/8.7-EVIDENCE-jts-local-2026-08-17.md's
structure) → close **#2581**, then **#2508**, then **#2481 with the OD-4
narrow-close comment**: grouping is on the ring; pair 6 has one consumer left
(outputd's passive content lane); the snd-aloop module still loads for axes
2/3; the zero-aloop successor is decided when Phase 2 completes; the only
live aloop use on the fleet is jts4 fan-in's usbsink idle-read fallback
(hardware-absent, errno=19).

## Deploy target — FINALIZED

- **Deploy SHA: `315589bd`** on `claude/loopback-retirement-phase1-survey-7bg0mu`
  (all six waves sealed 0/0/0: PR-2, PR-3, PR-4, PR-5, PR-6 + PR-0/PR-1 merged
  to main earlier). Deploy the branch tip, not main.
- **`captures/` is a TEMPORARY transport commit** carrying the sealed design
  set + this brief + the campaign record. It is gitignored upstream and MUST
  be dropped before any merge. It does not ship to the Pi in any harmful way
  (install.sh rsyncs the checkout), but do not treat it as product.
- §10.2's numeric bars govern the spikes verbatim — this brief adds the
  campaign's banked findings, it does not restate or replace them. Read the
  design's §10.2 alongside this file.

## S0-SYNC CAVEAT — read before running S2/S3

`scripts/s0-sync-bench.sh` and `HANDOFF-distributed-active.md`'s S0-sync
section characterise the **snd-aloop seam Slice 3 originally shipped on**, not
the shipped ring seam. Two independent reasons it does not transfer:

1. PR-3 moved the bonded ingress to the grouping ring, so the bench's
   transport is not the product's.
2. The bench's MECHANISM cannot exist on the shipped path: `s0-sync-bench.sh:32`
   has camilla nudge snd-aloop's `PCM Rate Shift` control to hold target_level;
   a ring PCM is an ioplug, so CamillaDSP finds no mixer element to steer
   (HANDOFF-distributed-active.md:349). Its PASS evidence ("camilla logs
   `Capture device supports rate adjust`") is evidence for a mechanism the
   shipped active-follower no longer has.

The section is now explicitly DATED rather than neutralised, and both of its
open forward-looking gates (the un-run ≥24 h soak; the clock-topology gate)
fall inside that dating note. **A ring-seam de-risk is OWED and is not what
this bench provides.** Do not let a green S0-sync bench stand in for S0's
actual question (does snapclient negotiate against the ioplug at all).

## Also owed, surfaced by the campaign — not blockers for the pass

- **The five `correction_substream` probe rigs are already silently broken on
  any ring-armed box** (`aec-probe-pinknoise.sh`, `aec-probe-latency.sh`,
  `aec-probe-xvf-ref-level.sh`, `aec-probe-timing.py`, `s0-sync-bench.sh`):
  they `aplay -D correction_substream` unconditionally, writing into a cable
  with no reader. The product's own path is fine (`correction_play_device()`
  resolves per spawn). Predates Phase 1; deliberately not fixed here (the
  resolver is unreachable behind their documented standalone-no-jasper-import
  exemption). **If you reach for one of these rigs during the pass, it will
  measure silence and tell you nothing.**
- `jasper-camilla.service` carries three recorded latent gaps:
  `StartLimitBurst=5` (the test exemption), `RestartSec=2` exactly ON the ring
  liveness window, and no `UMask` (it is also the grouping ring's reader end).
  Latent today — all ring participants run as root. First place to look if the
  soak produces a camilla EBUSY run.

Last verified: 2026-08-20
