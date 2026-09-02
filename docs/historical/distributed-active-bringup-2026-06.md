# Distributed-active bring-up record (2026-06) — historical

> **Status: historical.** Frozen record of the June 2026 bring-up of the
> distributed active crossover: the S0-sync de-risk bench, the on-device
> Stage A/B runs, the three fail-closed hardenings each real incident forced,
> and the Q2 TTS-latency measurements. Kept because the numbers cost hardware
> time and the rejected shapes are worth not re-litigating. Every number is
> from its stated date and describes the topology of that date — several of
> those topologies no longer exist (see the staleness notes inline).

## S0-sync de-risk gate — bench result (2026-06-20)

**Stale seam.** This bench characterises the **snd-aloop** seam. The bonded
ingress has since moved to the grouping ring, which exposes no `PCM Rate
Shift`, so neither the bench nor its clock-lock evidence transfers; a
ring-seam de-risk is owed.

A throwaway BENCH run **before** Slice 3 wired the reconciler, to prove (or
disprove) that a wireless **active** follower stays sample-locked through the
one seam the active path adds and the dumb path deliberately avoids:
`snapclient → snd-aloop → crossover-only CamillaDSP → real DAC`. (The dumb
follower path uses `--player file` → raw FIFO precisely to dodge snd-aloop;
the multiroom spike already validated *its* p99 budget. S0 isolated the
**new** risk: the snd-aloop re-entry + the `rate_adjust`/no-resampler
capture-from-loopback clock seam against the DAC.) Harness (throwaway, no
product code): [`scripts/s0-sync-bench.sh`](../../scripts/s0-sync-bench.sh) +
[`scripts/s0-sync-measure.py`](../../scripts/s0-sync-measure.py). Topology:
snapserver + follower#1 on `jts3` (HifiBerry DAC8x), follower#2 on `jts4`
(Pi Zero 2 W, USB dongle — the cheap-follower tier, so a stricter soak); each
`snapclient → hw:Loopback → camilla [crossover-only, volume_limit 0,
enable_rate_adjust, no resampler, chunksize 1024, fixed target_level] → DAC`,
with `snapclient --latency`.

**Acceptance as written:** p99 inter-speaker offset < 5 ms over a 2-hour run,
no audible resync, plus a ≥24 h `snd-aloop` xrun soak. The seam's clock-lock
was measured **directly** from camilla's websocket (state + `buffer_level` vs
target + `rate_adjust` + raw capture rate via `pycamilladsp`) alongside the
xrun soak and CPU/temp/Pss.

**Result (telemetry basis the owner accepted; ~0.65 h xrun-clean — the full
≥24 h durability soak was never run, boxes reclaimed early):**

- **Clock-lock: PASS (LOCKED, both followers, on every pair exercised —
  jts3+jts4 and jts3+jts.local).** Over the ~0.65 h run, `state=RUNNING`
  throughout; `buffer_level` held target (jts3 999–1055, mean 1025/1024; jts4
  964–1109, mean 1032/1024); `rate_adjust` tight and stable
  (~0.99980–1.00007, i.e. < ±0.03 %). camilla logged `Capture device supports
  rate adjust` — the bit-perfect loopback method engaged, no resampler.
  **0 xruns.** Notably the weak Zero 2 W (`jts4`) locked as cleanly as the
  Pi 5s.
- **snd-aloop xrun soak: clean over ~0.65 h**, then the lab boxes were
  reclaimed. Steady-state cost: camilla ≈ 5.5 MB Pss, snapclient ≈ 5 MB;
  temps jts3 ~40 °C, jts4 52–55 °C (Zero 2 W), load < 1.1, no throttling.
- **Inter-client sync:** snapclient `diff to server` ≈ 0 ms steady-state
  (sub-ms) — necessary-not-sufficient (it does not see camilla's
  contribution; the clock-lock telemetry above does).
- **Acoustic p99: DEFERRED.** The onboard mics (jts3 XVF; jts.local's XVF +
  USB-PnP) **cannot** measure the inter-speaker offset — each is dominated by
  its own close speaker, so the autocorrelation cannot resolve the faint far
  speaker (it returns "no clean peak"; an earlier constant ~0.29 ms read was
  an analyzer artifact = the search-window floor, since fixed). The acoustic
  p99 needs a single mic placed **between** two co-located speakers. Owner
  accepted the telemetry de-risk (2026-06-20).

**Operational findings, hardware-learned:**

- **Borrowing the DAC reboots a live JTS box.** The essential audio units
  (`jasper-fanin`/`outputd`/`voice`/`aec-bridge`) carry
  `StartLimitAction=reboot`; stopping them lets a re-trigger fail-loop into a
  reboot (hit 3× on `jts3`). Camilla is the exception by design —
  `StartLimitAction=none` routed to the non-rebooting
  `jasper-camilla-recover` oneshot. The bench disarms first via the same
  `/run` drop-in (`StartLimitAction=none`) that `jasper-bootloop-guard` uses,
  verifies it, then stops. The reconciler does **not** have this problem — it
  swaps the chain *in place* (no DAC contention). Worth knowing for any
  future DAC-borrowing bench.
- **snapserver does not reliably hold a pipe's read end** (`mode=read` AND
  `mode=create` both ENXIO'd a writer); the bench fed via a `process://`
  source. Production feeds the snapfifo from CamillaDSP's `File` output,
  which sidesteps this.

**Verdict.** On the telemetry basis the owner accepted, the clock seam held
and Slice 3 was GO, with two confirmations outstanding: the ≥24 h durability
xrun soak and the acoustic end-to-end p99 < 5 ms.

## Stage A — active follower on-device (2026-06-21): PASSED

Rig: bond `jts.local` (passive leader) → `jts3` (active follower).

- `/state.grouping.endpoint = active_crossover`; the **live** re-proof
  (`classify_camilla_graph` on the running follower config) returned the
  driver-domain baseline `allowed=True` — no full-range path to the tweeter.
- **Clock-lock under real music:** camilla `:1234` `rate_adjust` spread
  **8–23 ppm**, `buffer_level` steady ~**2040–2085 / 2048**, **0 xruns**,
  `state = RUNNING`.
- On-seat listen confirmed good; **self-recovery** verified — unbond → both
  boxes return to clean solo, `jts3` re-proves its active baseline.
- **Outstanding (telemetry accepted in lieu):** acoustic inter-speaker
  p99 < 5 ms needs a mic placed *between* the two speakers.

## Stage B Step 0 — the three hardenings, each forced by a real incident

**B1 (2026-06-21) — dormant camilla#2 infrastructure.** The second CamillaDSP
instance (`:1235`) shipped INERT: `jasper-camilla-crossover.service` +
`jasper-camilla-crossover-guard` installed, `JASPER_CAMILLA2_*` config fields
+ `crossover_controller()` present, install.sh seeding
`crossover-statefile.yml` through the active-speaker runtime contract; not
boot-enabled and not wired into any reconciler. Two B1 safety invariants were
pinned by tests and still hold: camilla#2 carries **no
`StartLimitAction=reboot`** (it fails closed to silence, never reboots the
household speaker), and its crossover guard repairs ONLY to the re-proven
driver-domain (Layer-A-intact) baseline, **never flat** (a flat crossover
would send full-range to the tweeter).

**Step 0 (2026-06-22) — the HW-free reconciler arm, music-only seam.**
`jasper.multiroom.active_leader_config` + the grouping reconciler's
active-leader branch arm the two-instance bring-up on bond: camilla#1 runs
the program bake (File→`SNAPFIFO`), `crossover-statefile.yml` is seeded with
the re-proven driver-domain graph, the audio-hardware reconciler proves the
paired statefiles and writes outputd's active-lane env, and only then is
camilla#2 armed. This closed the B1 seam (the install seed is flat; the guard
repairs a dead pipe, not a flat statefile). camilla#2 ran `rate_adjust` **ON**
— the already-validated active-follower seam, no `outputd-summer`, no leader
TTS.

**Hardening 1 (2026-06-23) — fail-closed DAC handoff (the JTS5 reboot loop).**
The first on-device exercise (JTS5, an active-speaker bench box with **no
Snapcast installed**) surfaced a reboot loop: the reconciler armed camilla#2
onto the DAC **even though camilla#1's bake had failed** (no FIFO reader
without snapserver → camilla#1 stayed on its solo-active DAC baseline), so
both CamillaDSP fought for the DAC and camilla#1 exhausted its recovery
budget. Three gates closed it: (1) the precheck refuses the bond if
`snapserver`/`snapclient` are not installed (`snapcast_unavailable`); (2) the
reconciler does not bake unless `snapserver` is actually active; (3)
camilla#2 is armed only if the bake succeeded — camilla#1 has provably moved
off the DAC to the wire. **The generalisable lesson: a second CamillaDSP must
never take the DAC until the always-on camilla#1 has provably released it.**

**Hardening 2 (2026-06-24) — the positive handle barrier (the jts3 EBUSY
reboot loop).** A real `jts3` active-leader run found a tighter race:
camilla#1's websocket bake reload can return successfully *before* the
exclusive active-content PCM is actually closed. If camilla#2 armed inside
that close-lag its open got `EBUSY`, and camilla#1 — the recovery-budget unit
— repeatedly tried to recover the core graph. The reconciler gained a bounded
positive probe between seeding the crossover statefile and arming camilla#2.
Timeout/still-open blocks the bond, restores camilla#1 to the re-proven
solo-active baseline, records `active_content_pcm_busy`, and exits nonzero so
the next reconcile retries; a probe that cannot ANSWER (`unknown`) fails
closed the same way under `active_content_pcm_unverified`.

> **Stale signal (superseded 2026-08-15, audio-graph consolidation P9-C).**
> The 2026-06-24 barrier polled the per-substream ALSA status path
> `/proc/asound/Loopback/pcm0p/sub5/status` for exact `closed`. That PCM
> (`outputd_active_content_playback`, `hw:Loopback,0,5` — pair 5) has since
> been **deleted** from `asoundrc.jasper`; there is no procfs path left to
> fall back to. The release signal is now a non-blocking exclusive `flock` on
> the ACTIVE ring's writer lock. Insertion point, bounded poll, fail-closed
> direction and both block reasons are unchanged.

Its **on-device validation (jts3, 2026-06-24) exercised the earlier procfs
signal** and has not been re-run against the flock barrier: with grouping
off, the procfs path reported non-`closed` while camilla#1 owned the PCM; a
forced busy probe logged `active_leader_blocked`, restored camilla#1 to
`active_speaker_baseline.yml`, left camilla#2 un-armed, and surfaced
`/state.grouping.endpoint.blocked_reason=active_content_pcm_busy`; the next
real reconcile retried from that blocked state, read exact `closed` on
attempt 1 (`timeout_sec=0.8`), armed camilla#2, and completed `rc=0`. The box
was then restored to solo (camilla#2/snapcast inactive, no failed units, no
reboot).

**Hardening 3 (2026-06-24) — outputd follows the paired graph contract.** The
same `jts3` deployment exposed a recovery-only gap:
`jasper-outputd-failure-reconcile` re-entered the single audio-hardware env
writer, but that writer inspected only camilla#1's `outputd-statefile.yml`.
In active-leader mode camilla#1 is the safe `program_bake_pipe` (no DAC),
while the endpoint graph that proves the active playback lane lives in
camilla#2's `crossover-statefile.yml`. The runtime contract now owns that
matrix: `program_bake_pipe` is never accepted as an outputd endpoint by
itself; it is a sentinel that makes the reconciler prove camilla#2 is a legal
`driver_domain_baseline` on the active lane, so a deploy/restart no longer
downgrades `outputd.env` while grouped active lanes are already owned.

## Q2 spike — TTS band-limiting latency, measured on `jts3` (2026-06-20)

Solo active 2-way @ 48 kHz. The decision this evidence produced is ADR-0125.

| Path | TTS injected at | Incremental vs today's outputd mix | Tweeter-safe? |
|---|---|---|---|
| (a) outputd mix [then-current; **unsafe** on active] | outputd `OutputCore`, post-crossover | 0 — reference (TTS-to-glass ≈ DAC playout **63.7 ms**) | ✗ |
| (c) Option 2 — protective filter on the TTS lane at outputd | outputd, + one biquad | **< 1 ms** (biquad group delay; no added buffering) | ✓ but muffled, or re-implement the crossover |
| (b) Option 3 — TTS into the crossover instance's input | camilla#2's input (post-snapclient) | **+85–125 ms** (camilla chunk 21 ms + playback buffer 43 ms + content-bridge handoff ~63 ms; = the solo-active path) | ✓ |
| Option 1 — upstream of the bake [**rejected**] | fanin, pre-stream | **+~400 ms** (snapcast `buffer_ms` round-trip) | ✓ but laggy, and streams TTS to the follower (inv-A) |

Measured anchors (live `/state`, solo active, buffering of that date):
`dac.snd_pcm_delay_ms = 63.7`; `content_bridge.fill_frames ≈ 3026` (~63 ms —
measured on the since-deleted `rate_match` bridge; `/state` no longer
publishes `content_bridge.fill_frames`); camilla `chunksize 1024` (21.3 ms) +
`target_level 2048` (42.7 ms).

**Solo-active TTS was traced and confirmed a non-gap.** There is one TTS
transport (`JASPER_TTS_TRANSPORT=outputd` — the wire protocol); the *socket*
decides where it mixes. Solo defaults route TTS into fanin, upstream of
CamillaDSP, so on a solo active speaker TTS rides `fanin (music+TTS) →
CamillaDSP (Layer A) → outputd → DAC`: split by the per-driver crossover (on
`jts3`, the tweeter `LinkwitzRileyHighpass @ 2 kHz`), therefore tweeter-safe,
and in the AEC reference. The outputd 2-channel `single_alsa` TTS mixer is
the *bonded-member* mixer, armed only by the reconciler, and does not
silently drop solo TTS.

**A latent guard hazard found and closed here.** The outputd TTS-mixer guard
rejected `content_channels != 2`, but an active 2-way speaker is *also*
2-channel (woofer/tweeter, like `jts3`), so the guard would have *permitted*
the outputd mixer on a 2-ch active sink — where mixing post-crossover is
full-range to the tweeter. The structural fix was `JASPER_OUTPUTD_ACTIVE_LANE`
(set on a 2-ch active sink), teaching outputd that the invariant is
"full-range stereo L/R sink," not "exactly 2 channels."

## External design review (2026-06-20)

A source-cited external review pressure-tested the load-bearing claims and
**confirmed** the engine decision (CamillaDSP bit-perfect loopback
rate-tracking; the `rate_adjust`+resampler oscillation trap, CamillaDSP #207)
and every safety building block (LR4 sums flat; sub+mains = one crossover;
clock drift ~1 ms/min, audible within ~1 min). It sharpened two seams: (1)
the snapclient→loopback→downstream-CamillaDSP sync seam is the #1 risk and
must pass an S0-sync de-risk gate first (builders report failing exactly this
shape); (2) the 1 GB-RAM question is really **CPU + thermal** (active
cooling). It also pinned the follower crossover config for the aloop seam it
read: clock-master direction, `chunksize ≥ 1024`, no SIGHUP during playback.
