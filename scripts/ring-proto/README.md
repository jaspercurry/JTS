# Ring B prototype — CamillaDSP → outputd via a SHM ping-pong ring

> **P2 UPDATE (audio-graph consolidation): the end-to-end ring is now a PRODUCT
> path.** The `shm_ring` coupling is a first-class reconciler mode — arm/disarm
> BOTH rings coherently with
> `sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring`
> (and `… loopback` to revert). It emits the ring CamillaDSP config through the
> product emitters (no hand YML), re-seeds a ring config on camilla restart (the
> built-in-revert is gone), ships a reconciler-owned ALSA conf.d device
> (`60-jts-ring.conf`, P1) instead of the hand-written `98-jts-ring-proto.conf`,
> and wires `/state.audio_graph.coupling` + the `ring platform` / `fan-in
> coupling` doctor checks. **The DEFAULT remains loopback** (explicit arming
> until P4). These `arm*.sh`/`disarm.sh` scripts are retained ONLY for the
> isolated single-ring lab experiments (Ring A OR Ring B alone), which the
> product reconciler intentionally does not do. Canonical operational truth:
> [`docs/HANDOFF-audio-graph-consolidation.md`](../../docs/HANDOFF-audio-graph-consolidation.md).
>
> **jts.local migration (REQUIRED):** a box already lab-armed via these scripts
> collides with the P1/P2 product ring assets (the `98-jts-ring*-proto.conf`
> drop-ins duplicate the shipped `60-jts-ring.conf` PCM names; the marked env
> blocks live in the same reconciler-owned `fanin.env`/`outputd.env`). Before the
> first P2 deploy, `disarm.sh` **both** rings (`disarm.sh` + `disarm.sh --ring-a`),
> then deploy and re-arm with the product reconciler. Full steps: section **H.1**
> of the canonical handoff above.

**Status: EXPERIMENTAL lab tooling** for the ISOLATED single-ring experiments
(shipped default-off with the ring consumers change — the `jts-ring-ioplug` C
plugin + the outputd SHM reader; canonical operational truth lives in
[`docs/HANDOFF-usb-low-latency.md`](../../docs/HANDOFF-usb-low-latency.md)).
For the end-to-end ring use the P2 reconciler above, not these scripts. Each
script replaces one hop with a bounded SHM ring on a **lab Pi only**
(`jts3.local`, `jts5.local`, or a spare `jts.local`-shaped box — never a
household's production speaker). Everything it touches is reversible with
`disarm.sh`.

The remaining ring productization work (futex wait instead of poll; snd-aloop
removal so the ring is the ONLY transport) rides later campaign phases (P4→P9),
not this lab tooling.

## What this proves

The night of 2026-07-01, the first real route-latency artifact on
`jts.local` measured USB-ingress → post-Camilla-emit p50/p95/p99 =
173.6/181.5/183.5 ms (240/240 impulses, ~10 ms spread — a stable queue
depth, not jitter). The judged hypothesis (Design B, "One-Clock Ring
Graph") is that replacing free-running snd-aloop hops with DAC-paced SHM
frame rings removes most of that queue depth. This prototype proves the
riskiest slice of that hypothesis end-to-end: **CamillaDSP playback →
outputd content**, via a custom ALSA ioplug writing into a shared-memory
ring that outputd reads one slot per DAC period.

**Falsifiable target (as first written, pre-descent):** content hop was an
observed ~1536-frame queue (~32 ms); the ring bounds it to
`n_slots × period_frames` (2×128 = 256 frames ≈ 5.3 ms at 48 kHz). The
first-cut expectation was p95 **~155–162 ms** — the isolated Ring-B win off the
173.6 ms aloop baseline.

> **Superseded — direction validated and far exceeded.** The full ring graph
> (both rings 2-slot + USB DIRECT bridge deletion) descended the whole USB route
> to p50/p95 ≈ 35/37 ms (≈48–50 ms end-to-end), not ~155–162 ms — this hop's
> ~27 ms saving was one contribution to a much larger stack. The current numbers
> and the measured-results ladder live in
> [`docs/HANDOFF-usb-low-latency.md`](../../docs/HANDOFF-usb-low-latency.md); the
> `~155–162 ms` figure below is the original single-hop projection, kept for the
> falsification logic, not a current target.

The original prototype was falsified by any of:
- measured p95 > 170 ms after arming,
- `empty_reads` growing faster than the predicted clock-drift slip rate
  (roughly one slot per 1–2 minutes at 50–100 ppm — the SAME average slip
  the free-running aloop hop has today, just now visible in an honest
  counter instead of invisible in a bigger buffer),
- any sustained xrun regression on the DAC lane.

## What lives where (file ownership)

This directory (`scripts/ring-proto/`) and this README are the
**tooling** layer. Two other pieces of this prototype are owned by a
parallel effort on the same branch and are NOT edited by anything here:

- `rust/jasper-ring/` — the pure SPSC reader crate + shared header/seq
  layout (no ALSA dependency; `cargo test -p jasper-ring` runs on macOS).
- `c/jts-ring-ioplug/` — the ALSA ioplug (`pcm_jts_ring.c`, Pi-only,
  links `libasound`) plus its host-compilable pure-logic core
  (`jts_ring_shm.c`, `test_ring_core.c`, `ring_writer_bench.c` — no ALSA
  headers, builds on any host via the Makefile's `test`/`bench` targets).
- `rust/jasper-outputd/` — the product daemon's `config.rs` (the
  `ContentBridgeMode::ShmRing` variant + `JASPER_OUTPUTD_CONTENT_BRIDGE=
  shm_ring` / `JASPER_OUTPUTD_SHM_RING_SLOTS` env parsing),
  `shm_ring_source.rs` (the reader wiring), and `alsa_backend.rs`/
  `main.rs` (skip-opening-the-content-PCM + startup wiring). Default is
  `direct`; nothing here runs unless the flag is set.

If any of the above hasn't landed yet when you run these scripts, the
scripts degrade gracefully — see "Degraded/partial states" below.

## The scripts

| Script | Runs on | Mutates anything? |
|---|---|---|
| `host-check.sh` | laptop (macOS/Linux) | No — pure build+test gate |
| `build-on-pi.sh` | laptop, drives the Pi over SSH | Yes — stages source, builds, installs one `.so` |
| `make-camilla-ring-config.sh` | laptop, drives the Pi over SSH | Yes — writes one new Camilla config file (does NOT load it). `--ring-a` swaps `devices.capture` instead of `devices.playback` |
| `arm.sh` | laptop, drives the Pi over SSH | Yes — the full Ring **B** wiring sequence below |
| `arm-ring-a.sh` | laptop, drives the Pi over SSH | Yes — the Ring **A** capture-mirror wiring (see "Ring A" below) |
| `disarm.sh` | laptop, drives the Pi over SSH | Yes — unconditional rollback, idempotent. `--ring-a` rolls back Ring A |
| `_guard.sh` | sourced by the five mutating scripts above | No — safety gate only |

**Safety gate on every mutating script:** each of `arm.sh`, `arm-ring-a.sh`,
`disarm.sh`, `build-on-pi.sh`, and `make-camilla-ring-config.sh` refuses to run
unless `PI_HOST` is explicitly set (by you, or already persisted in
`.env.local` — see AGENTS.md "Laptop-side state"). Running any of them
bare, with no `PI_HOST` and no `.env.local`, is a `No changes made`
refusal, not a silent fall-through to whatever `scripts/_lib.sh`'s
ordinary product default (`jts.local`) resolves to.

This was not a hypothetical concern: during this script family's own
development, running `build-on-pi.sh` with no `PI_HOST` set landed on
the real `jts.local` box (the ordinary, correct product default for
every OTHER script in this repo) and ran a real `rsync` + `make` there.
The build failed on unrelated C compile errors before anything was
installed, and the scratch directory was manually removed afterward —
but the near-miss is exactly why `_guard.sh` exists now. **Always pass
`PI_HOST` explicitly for every command in this README.**

## Lab procedure

### 0. Pick your box and set `PI_HOST`

```sh
export PI_HOST=jts3.local   # or jts5.local, or a spare jts.local-shaped box
```

**jts3 first, amp unplugged.** jts3 is a HiFiBerry lab box (per project
memory, one of the boxes that are "ALL lab boxes — test freely"), and
running with the amp physically unplugged means a misconfigured ring, a
xrun storm, or a bad statefile swap makes no sound at all rather than a
loud surprise. Once `arm.sh`'s step 5 (bench writer) and step 7 (final
verify) both look clean on jts3 with the amp unplugged, reconnect the
amp, re-verify at a quiet volume, and only then repeat on `jts.local` if
you have a second Pi you're willing to risk (do NOT do this on a
household's only speaker).

**jts3 topology check.** As of this writing jts3 may be on an
active-speaker staged startup topology (a saved multi-DAC/crossover
graph), not the plain full-range stereo `direct` topology Ring B
requires. `arm.sh`'s own preflight checks this (`JASPER_OUTPUTD_SINK`,
`JASPER_OUTPUTD_ACTIVE_CHANNELS`, `JASPER_OUTPUTD_ACTIVE_LANE`) by
reading the running daemon's true environment from
`/proc/<MainPID>/environ` — **not** `systemctl show -p Environment`,
which returns only the unit's `Environment=` directives and misses the
`EnvironmentFile=` layers where JTS actually keeps this state
(`/var/lib/jasper/outputd.env`, `grouping-outputd.env`). Verified
2026-07-02: on jts3 `ACTIVE_LANE=1` lives in `outputd.env` and is
invisible to `systemctl show`, so reading that surface would blind the
tweeter-safety refusal. The preflight refuses cleanly if the box is not
full-range stereo. If jts3 is currently staged into an active topology,
revert it to the plain stereo baseline first (see
`docs/HANDOFF-speaker-output-reference.md`); this prototype does not
attempt that reversion for you.

### 1. Host-side build+test gate (no SSH, no Pi touched)

```sh
bash scripts/ring-proto/host-check.sh
```

Checks `rust/jasper-ring` (`cargo fmt --check` + `cargo test`, pinned
1.85.0 toolchain) and `c/jts-ring-ioplug`'s host-safe Makefile targets
(`make test` builds+runs `test_ring_core`; `make bench` build-only for
`ring_writer_bench`). If either piece hasn't landed yet on this branch,
that section prints `SKIP`, not `FAIL` — the two tracks (this tooling,
and the Rust/C core) develop in parallel. Exit 0 means both pieces that
exist right now are clean; it does NOT mean the ioplug's ALSA-linked
half (`pcm_jts_ring.c`) builds — that's Pi-only and step 2 below is what
checks it.

### 2. Build the ioplug on the Pi

```sh
bash scripts/ring-proto/build-on-pi.sh
```

Stages `c/jts-ring-ioplug/` to `/home/pi/jts-ring-proto/` on the Pi (a
scratch tree under the SSH user's home, NOT `/opt/jasper` — this is a
leaf artifact, not something that goes through the product install
pipeline), runs `make plugin bench`, and installs the resulting
`libasound_module_pcm_jts_ring.so` to the box's ALSA plugin directory
(`/usr/lib/aarch64-linux-gnu/alsa-lib/`, mode 0644 — confirmed live on
both jts3 and jts.local as of 2026-07, where bluealsa/jack already
register plugins from that same directory). Refuses up front if
`libasound2-dev` is missing (it should already be installed by product
`install.sh` on any onboarded box — this script does not install base
packages).

### 3. Arm

```sh
bash scripts/ring-proto/arm.sh
# or, to test the degraded-widening slot count instead of the 2-slot prototype:
JASPER_RING_PROTO_SLOTS=3 bash scripts/ring-proto/arm.sh
```

Runs, in order, with an automatic `disarm.sh` rollback on ANY step
failure (see "Rollback" below):

1. **Preflight.** Reachability, ioplug `.so` present, outputd's
   resolved runtime env is a full-range stereo L/R sink and not already
   running a conflicting content source, records rollback state.
2. **Install `/etc/alsa/conf.d/98-jts-ring-proto.conf`** — registers
   `pcm.jts_ring_playback`. Deliberately NOT appended to
   `/etc/asound.conf`: that file is a symlink to
   `/var/lib/jasper-asound/asound.conf`, fully re-rendered by
   `jasper-render-asound-conf` on every deploy/audio-quality toggle — a
   marked block there would be silently wiped the next time anything
   touches that renderer. `/etc/alsa/conf.d/*.conf` is the standard
   alsa-lib confdir mechanism (`/usr/share/alsa/alsa.conf` includes it),
   confirmed live and already used by bluealsa/jack on this box, and
   `install.sh` already creates the directory. Rollback is one `rm`.
3. **Resolvability probe:** `sudo aplay -D jts_ring_playback -c 2 -r
   48000 -f S16_LE -d 1 /dev/zero`. Terminates because the writer
   free-runs (drops frames) when no reader is attached yet — this also
   catches the PR #214 resolvability-bug class per AGENTS.md's renderer
   device rule.
4. **Arm outputd:** appends a marked block to
   `/var/lib/jasper/outputd.env` (`JASPER_OUTPUTD_CONTENT_BRIDGE=
   shm_ring`, `JASPER_OUTPUTD_SHM_RING_SLOTS=<n>`) and restarts
   `jasper-outputd`. Verifies via journal
   (`event=outputd.shm_ring.enabled`), not just "unit is active" — a
   restart into some other silently-broken state would still show
   `active`.
5. **Bench writer smoke test:** plays a short tone directly into the
   ring via `ring_writer_bench` (built by `build-on-pi.sh`), proving the
   reader end-to-end WITHOUT CamillaDSP in the loop. Soft-skips (not a
   failure) if the bench binary isn't built yet. **Volume:** this tone
   enters POST-CamillaDSP (ring → outputd → DAC), so `master_gain` cannot
   attenuate it and there is no ramp. arm.sh pins a deliberately quiet
   `-40 dBFS` (the bench's own default) per the JTS safe-test-volume
   doctrine; raise it only via `JASPER_RING_PROTO_BENCH_DBFS=<dbfs>` (or the
   bench's `--amplitude-dbfs`) after confirming the volume is safe on the
   box, with an operator listening.
6. **Build + load the hand Camilla config:** calls
   `make-camilla-ring-config.sh` (below), then points the
   `outputd-statefile.yml` at the result via the SAME
   `write_camilla_statefile` helper the product's active-speaker runtime
   contract uses, and restarts `jasper-camilla`.
7. **Final verify.** Prints the exact commands to check occupancy/empty
   counters and re-run the route-latency harness.

### 4. Build the hand Camilla config (called automatically by arm.sh; can be run standalone)

```sh
bash scripts/ring-proto/make-camilla-ring-config.sh
```

Reads whichever config the live statefile currently points at, copies
it, swaps **only** `devices.playback.device` to `jts_ring_playback`
(everything else — samplerate, chunksize, target_level, capture device,
every filter/mixer/pipeline entry, `volume_limit` — is preserved
byte-for-byte via a real YAML parse/round-trip, not string surgery),
writes it to `/var/lib/camilladsp/ring_proto.yml`, and validates it with
the product's own `jasper.dsp_apply.validate_camilla_config` — the same
function that runs `camilladsp --check` AND enforces the JTS 0 dB
`volume_limit` safety ceiling (which `camilladsp --check` alone does
not). A failed validation exits 2 and removes the invalid file; nothing
downstream proceeds. **Never touches the product emitters**
(`jasper/camilla_emit.py`, `jasper/active_speaker/camilla_yaml.py`) or
any packaged config under `/etc/camilladsp/` — it only ever reads
whatever is currently live and writes one new file.

### Why the pipe-guard doesn't fight this

`jasper-camilla-pipe-guard` runs as `ExecStartPre=` on every
`jasper-camilla` restart and repairs the statefile to a safe base config
if the statefile-selected config's `playback` block names the configured
Snapcast FIFO and that FIFO is dead. The ring config's playback block is
`type: Alsa, device: jts_ring_playback` — an ALSA device, not the Snapcast
File sink, so it has no `filename:` key at all. The guard logs
`event=camilla_pipe_guard.ok reason=solo_config` and exits without touching
anything. This mirrors exactly how it treats the ordinary product config
(`outputd_content_playback` is also an ALSA device, not a File sink).

### Re-measure with the route-latency harness

```sh
ssh pi@${PI_HOST} '/opt/jasper/.venv/bin/jasper-route-latency-harness generate quick --out-dir /tmp/route-latency'
ssh pi@${PI_HOST} 'sudo /opt/jasper/.venv/bin/jasper-route-latency-harness run /tmp/route-latency/quick-schedule.json --out-dir /tmp/route-latency --invoke-artifact --confirm-route-health-ok'
```

This is the SAME click/capture harness that produced the 173.6/181.5/
183.5 ms baseline — it measures the full USB-ingress-to-speaker-output
route, of which the Camilla→outputd content hop is one segment. Arming
Ring B changes that one segment; re-running the harness on the same
route is what falsifies or confirms the ~155–162 ms prediction. See
`docs/testing-tooling.md` "Route-latency click/capture harness" and
`docs/HANDOFF-usb-low-latency.md` for the full harness architecture —
this prototype adds no new measurement tooling.

`generate quick`/`run` play a WAV on a host machine at a modest, quiet
volume while the harness listens on both the USB ingress tap and a mic
near the speaker — read `docs/HANDOFF-usb-low-latency.md`'s walkthrough
before running this the first time on unfamiliar hardware. Read the
harness's own printed route-health delta report before passing
`--confirm-route-health-ok`; it is never inferred automatically.

### `:9891` electrical capture note (jts3-specific)

outputd's final-speaker reference UDP monitor
(`JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891`) is normally bound
by the AEC bridge, which is gated on AEC being configured for that box.
On a lab box like jts3 where AEC may not be enabled, nothing may be
listening on `:9891` even though outputd is publishing to it — a
"nothing received" result there is a configuration state, not proof the
prototype is broken. `scripts/aec-probe-timing.py --ref-source
outputd_udp` is the existing diagnostic that can bind `:9891` directly
for a one-off electrical capture without touching the AEC bridge; see
`docs/AEC-DIAG-03-timing-probe.md` for the full `--ref-source` menu and
usage. This prototype does not add a new electrical-capture tool.

## Ring A — the capture mirror (fan-in → CamillaDSP)

Everything above is **Ring B** (CamillaDSP writes the ring, `jasper-outputd`
reads — the output hop). **Ring A** is the mirror on the *input* side:
**`jasper-fanin` writes the ring, CamillaDSP's capture reads it** — replacing
the fan-in → camilla `dsnoop` capture hop. Same SHM contract v1, byte-identical
header; separate ring instance (`program.ring`, vs Ring B's `content.ring`).

**Roles flip, code is shared.** The C ioplug (`pcm_jts_ring.c`) dispatches on
`io->stream`: playback = the Ring B writer path, capture = the Ring A reader
path. The reader core (`jts_ring_reader_*` in `jts_ring_shm.c`) mirrors the Rust
`RingReader` (attach-resync `read_seq = write_seq`, defensive resync on
`W − R > n_slots`, reader pid/heartbeat stamped every consume, consume-oldest
with a Release `read_seq` advance). The capture pointer core
(`jts_ring_capture_pointer_report`) is the exact mirror of the playback one — the
same round-4 mod-buffer alias clamp, roles flipped (`hw` advances on writer
PUBLISH; a writer burst of exactly `buffer_size` between two pointer reads must
never alias to a zero delta).

**Two Ring-A-only behaviours to know:**

- **`-EBUSY` second-reader guard.** The ring is strictly SPSC. Unlike Ring B
  (where `jasper-outputd` owns the reader by construction), Ring A's capture
  device is operator-openable — a stray `arecord -D jts_ring_capture` while
  CamillaDSP is attached would corrupt `read_seq`. `jts_ring_reader_open` refuses
  with `-EBUSY` if a *live foreign* reader pid is already stamped (a dead/stale
  one does not block a fresh attach). Verified on hardware: a second concurrent
  `arecord` gets `rc=-16`, the incumbent keeps the ring uninterrupted.
- **Writer-dead timer-paced silence.** When `jasper-fanin` is heartbeat-dead
  (a routine deploy restart) the reader fabricates **wall-clock-paced** silence
  (one 128-frame period per period of realtime) so CamillaDSP stays up and
  DAC-paced instead of flapping into capture-error/prepare. Real audio resumes
  seamlessly on writer reattach (epoch observed). This is what makes
  `arecord -D jts_ring_capture -d 1` terminate (the resolvability probe) even
  with no fan-in running. Verified on hardware: writer death mid-`arecord` →
  paced silence → writer return (880 Hz) all captured cleanly, `arecord` exit 0,
  no xruns.

### Arm / disarm Ring A

```sh
# Build the .so on the Pi first (shared with Ring B):
PI_HOST=jts.local bash scripts/ring-proto/build-on-pi.sh

# Product default is now 2-slot / chunk 128 / target 128 / queuelimit 1 /
# rate_adjust off (PR #1186); the 8-slot default below remains valid only for
# isolated Ring-A measurement.
# Arm Ring A (default 8 slots — the capture BufferManager negotiates a
# 1024-frame buffer = 8 * 128; overridable 2..16):
PI_HOST=jts.local bash scripts/ring-proto/arm-ring-a.sh
JASPER_RING_PROTO_SLOTS=8 PI_HOST=jts.local bash scripts/ring-proto/arm-ring-a.sh

# Roll back:
PI_HOST=jts.local bash scripts/ring-proto/disarm.sh --ring-a
```

`arm-ring-a.sh` runs, in order, with an automatic `disarm.sh --ring-a` rollback
on ANY step failure: preflight (`.so` installed, `jasper-fanin` running on the
`loopback` default, host **reachable — unreachable is a FAILURE, not a skip**) →
install `/etc/alsa/conf.d/98-jts-ring-a-proto.conf` (`pcm.jts_ring_capture`,
`period_frames 128`, `n_slots 8`) → `arecord` resolvability probe (terminates via
the writer-dead silence path) → flip `jasper-fanin` to
`JASPER_FANIN_CAMILLA_COUPLING=shm_ring` (marked block in `/var/lib/jasper/fanin.env`,
the last `EnvironmentFile`), ordered restart (`reset-failed` + a 90 s spacing
guard so the two daemons that share the ring do not race the create/attach
handshake) → **enforce ring perms** (`program.ring` `root:jasper` `0664`, dir
`0775` — the reader WRITES `read_seq`+heartbeat, so the CamillaDSP user needs rw;
this is the EACCES class the outputd fix round hit) → build+load the Ring A hand
Camilla config (`make-camilla-ring-config.sh --ring-a`: `devices.capture →
{type: Alsa, device: jts_ring_capture, format: S16_LE}`, `camilladsp --check`-gated)
→ ordered `jasper-camilla` restart.

The capture device (`jts_ring_capture`) and wire format (`S16_LE`) are the SSOT
shared with `jasper.fanin_coupling.capture_kwargs_for_coupling("shm_ring")`; the
`--ring-a` config generator asserts they match at build time and fails loud on a
drift. `disarm.sh --ring-a` removes **only** the `program.ring` file (never the
`/dev/shm/jts-ring` dir — an armed Ring B's `content.ring` may share it).

**Reader bench for on-Pi interop:** `ring_reader_bench` (the capture-direction
mirror of `ring_writer_bench`) attaches, drains at DAC pace, and counts
`frames_read / silence_periods / epoch_resets` — the interop proof of the Rust
`RingWriter` → C reader wire format without a full CamillaDSP capture path.

## Rollback

```sh
bash scripts/ring-proto/disarm.sh
bash scripts/ring-proto/disarm.sh --purge   # also removes the built .so + build scratch dir
```

**Idempotent and safe to run cold.** Every step checks whether its own
target exists before touching it — running this against a box that was
never armed, or was only partially armed, is a no-op for anything not
present, never an error. `arm.sh` calls this itself on any step failure
(see `fail_and_rollback` in `arm.sh`), so it must never assume every
marked block exists.

Reverse order of arming: restores the CamillaDSP statefile's
`config_path` from the state `arm.sh` recorded before it changed
anything, restarts `jasper-camilla`; strips the marked block from
`/var/lib/jasper/outputd.env`, restarts `jasper-outputd`; removes the
`/etc/alsa/conf.d/98-jts-ring-proto.conf` drop-in; removes
`/dev/shm/jts-ring/` (tmpfs — harmless, self-heals on next arm); removes
the recorded rollback state. `--purge` additionally removes the
installed `.so` and the `build-on-pi.sh` scratch directory.

> **Interaction with the P1 product install (post-2026-07 branches).**
> Audio-graph consolidation P1 promoted `libasound_module_pcm_jts_ring.so`
> and the `/dev/shm/jts-ring` directory to PRODUCT assets (shipped INERT by
> `deploy/lib/install/ring-platform.sh`; the doctor's `ring platform` check
> verifies them). On a box that has that P1 deploy, two disarm actions touch
> product-owned files: `--purge` removes the now-product `.so`, and the Ring-B
> `disarm.sh` removes the whole `/dev/shm/jts-ring` directory. Both are still
> safe and self-healing — the next `bash scripts/deploy-to-pi.sh` rebuilds the
> `.so` and re-applies the tmpfiles dir — but until that redeploy (or a reboot,
> which re-applies the tmpfiles entry) the doctor's `ring platform` check will
> report `warn` ("inert platform incomplete"). That warn is expected after a
> disarm on a P1 box, not a regression. (Ring-A `disarm.sh --ring-a` removes
> only `program.ring`, never the dir, so it leaves the product dir intact.)

## Reboot-while-armed

Unit ordering is already correct for this without any change:
`jasper-outputd.service` carries `Before=jasper-camilla.service`, so on
a reboot with the ring armed, outputd normally starts (and
creates/attaches the ring) before Camilla tries to open
`pcm.jts_ring_playback` as a writer.

`/dev/shm/jts-ring/content.ring` is tmpfs and is gone after a reboot,
**including the `/dev/shm/jts-ring/` directory itself.** Both sides
create the directory (`mkdir -p` of the parent) before the O_EXCL create
— the Rust reader's `ensure_parent_dir` and the C writer's matching
`ensure_parent_dir` in `jts_ring_shm.c` — so whichever side opens the
path first recreates both the dir and the file (per the SHM contract's
create-or-attach discipline). This matters for the case where outputd
does NOT win the race: if outputd parks at boot (e.g. its DAC gate is
not yet satisfied) while Camilla is up, Camilla is the first opener. The
C writer then creates the directory + ring and free-run-drops (no live
reader), so Camilla does not ENOENT-crash-loop waiting for outputd. No
manual step is needed to survive a reboot while armed — though a reboot
is still a good moment to consider whether you meant to `disarm.sh`
first.

## Known hazards while armed

**`/sound/` or `/correction/` wizard saves.** Both wizards generate a
fresh CamillaDSP config via the product emitters and load it through
`jasper.dsp_apply.apply_dsp_config`, which always uses
`jasper.camilla_config_contract.DEFAULT_PLAYBACK_DEVICE =
"outputd_content_playback"` — NOT `jts_ring_playback`. Saving through
either wizard while Ring B is armed will load a config that points
Camilla's playback back at the ordinary snd-aloop lane, while outputd
(still in `shm_ring` mode until you `disarm.sh`) has stopped opening
that lane's capture side at all (see `alsa_backend.rs`'s
`skip_content_pcm`). **The resulting failure mode on the Camilla→outputd
seam has not been verified on hardware in this development pass** —
treat it as a real audio disruption (at minimum a Camilla-side write
stall/xrun into an unread snd-aloop buffer; possibly worse), not a
confirmed silent no-op. Don't touch `/sound/`/`/correction/` on a box
while Ring B is armed; `disarm.sh` first if you need to use either
wizard.

**Any other `jasper-outputd`/`jasper-camilla` restart path** (a deploy,
a manual `systemctl restart`, the AEC reconciler, the audio-hardware
reconciler) will pick up whatever the persisted state says at that
moment — if you're mid-arm-sequence when one of those fires, you may
land in a partially-wired state. Run `arm.sh` to completion (or let its
own failure path roll back) rather than interrupting it.

## Degraded/partial states

Because this tooling and the parallel Rust/C core track develop on the
same branch independently, you may run these scripts before the core
has fully landed:

- `rust/jasper-ring/` or `c/jts-ring-ioplug/` absent → `host-check.sh`
  SKIPs the missing piece; `arm.sh` step 2's resolvability probe will
  fail cleanly (no `.so` to open) with a clear message pointing at
  `build-on-pi.sh`.
- `c/jts-ring-ioplug/` present but `pcm_jts_ring.c` has a compile error
  (this happened during this branch's own early development — the C
  ioplug is Pi/ALSA-linked and does not build on macOS, so
  `host-check.sh`'s host-safe `make test`/`make bench` targets can pass
  cleanly while `build-on-pi.sh`'s Pi-only `make plugin` still fails) →
  `build-on-pi.sh` surfaces the compiler output verbatim and exits
  non-zero; nothing is installed. This is a bug in the parallel C track,
  not something this tooling layer can or should fix — flag it back to
  whoever owns `c/jts-ring-ioplug/` rather than patching those files
  from here.
- `rust/jasper-outputd`'s `shm_ring` observability block in `/state`
  (`STATUS\n` on `/run/jasper-outputd/control.sock`) may not have landed
  yet even after `JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring` itself works —
  `arm.sh` does not block on this; it verifies via the journal
  (`event=outputd.shm_ring.enabled`) instead, and step 7's printed
  verify commands note this explicitly.

## The eight questions

Per the design brief's requirement, answered here rather than in a
separate design doc (the module docs in `rust/jasper-ring/src/lib.rs`
and `c/jts-ring-ioplug/jts_ring_shm.h` answer the same eight questions
for the core; this is the tooling-layer view):

1. **What breaks if the writer (Camilla/ioplug) dies?** The ring's
   `write_seq` stops advancing; outputd's reader sees
   `SlotRead::Empty` every period and emits silence, incrementing
   `empty_reads`. `writer_pid`/`writer_heartbeat_ns` go stale, so a
   future `/state.shm_ring` (once wired) reports `writer_alive:false`.
   No crash, no wedge on the outputd side.
2. **What breaks if the reader (outputd) dies?** The writer's
   space-check fails once the ring fills, and because
   `reader_heartbeat_ns` is stale, it free-runs and drops frames rather
   than blocking Camilla's write loop. `arm.sh`'s step 3 resolvability
   probe exercises exactly this path (no reader is attached yet at that
   point in the sequence).
3. **What's the steady-state latency?** `<= n_slots * period_frames`
   frames of buffering — 2×128 = 256 frames ≈ 5.3 ms at 48 kHz for the
   prototype default; `JASPER_RING_PROTO_SLOTS=3` widens this to
   ~8.0 ms as a degraded-mode fallback if 2-slot proves too tight on
   real jitter.
4. **How is it observable?** Journal lines
   (`event=outputd.shm_ring.enabled`, `event=outputd.alsa.opened
   content_source=shm_ring`) today; `/state.shm_ring` (occupancy,
   empty_reads split startup-vs-steady, epoch_resets, reader_resyncs,
   writer_alive) once that wiring lands on the parallel track.
5. **How does it fail closed?** A geometry/version/size mismatch on
   attach is a hard error mapped to outputd's config-class exit 78, so
   the unit parks (`RestartPreventExitStatus=78`) instead of
   reboot-looping against a mismatched writer. `arm.sh`'s own preflight
   refuses on a non-full-range-stereo sink, a bonded/grouped box, or a
   conflicting content source before ever restarting anything.
6. **Is it default-off?** Yes on every layer: outputd's
   `JASPER_OUTPUTD_CONTENT_BRIDGE` defaults to `direct`; the ALSA plugin
   registration only exists once `arm.sh` installs the conf.d drop-in;
   nothing in `install.sh` or any product path references any of this.
7. **What's the memory-ordering argument?** Documented in
   `rust/jasper-ring/src/lib.rs`'s module doc and mirrored by
   `_Static_assert`s in `c/jts-ring-ioplug/jts_ring_shm.h` — Acquire/
   Release on the two sequence counters, C11 `atomic_*_explicit` and
   Rust `AtomicU64` both lowering to the same aarch64 `ldar`/`stlr`.
8. **What's the productization delta?** A 32-bit `futex_word` is
   reserved (but unused) in the header now specifically so the
   productized build can replace the writer's clamped-nanosleep poll
   with a real `FUTEX_WAIT`/`FUTEX_WAKE` pair without a header change;
   the lab `/etc/alsa/conf.d/*.conf` drop-in becomes a reconciler-owned
   device registration; `/state.shm_ring` gets wired into the doctor.
   None of that is this branch's job.

## Test/verification matrix

| Layer | Command | Runs where |
|---|---|---|
| Rust core unit tests | `cargo test -p jasper-ring` (via `host-check.sh`) | macOS/Linux laptop |
| C core unit test | `make test` in `c/jts-ring-ioplug/` (via `host-check.sh`) | macOS/Linux laptop |
| C core bench build | `make bench` in `c/jts-ring-ioplug/` (via `host-check.sh`) | macOS/Linux laptop, build-only |
| outputd config-parse / default-off proofs | `cargo test -p jasper-outputd` | Linux only (needs `libasound2-dev`) — CI's `rust` job |
| Full ioplug build | `build-on-pi.sh` | Pi only |
| Resolvability | `arm.sh` step 3 | Pi only |
| End-to-end reader | `arm.sh` step 5 (bench writer) | Pi only |
| Real chain | `arm.sh` steps 6–7 + route-latency harness | Pi only |

Every Pi-side step has a rollback (`disarm.sh`) and an observable
(journal `event=` lines, and `/state.shm_ring` once landed).

---

Last verified: 2026-07-02.

## Measured results — 2026-07-02 all-nighter (jts.local, Apple dongle, electrical :9891 mode)

Full arc, 240 impulses per run, 100% match unless noted:

| Config | p50 / p95 / p99 (ms) |
|---|---|
| Baseline all-aloop route | 173.6 / 181.5 / 183.5 |
| Host-slaved + cushion (certified artifact) | 139.3 / 156.7 / 157.6 |
| Ring B only, target 768 | 134.5 / 137.5 / 138.6 |
| Ring B + cushion 256 | 104.1 / 107.4 / 108.6 |
| Ring A+B, rate_adjust ON (lesson) | 194.0 / 196.9 / 197.3 |
| **Ring A+B, chunk 128, 4-slot rings, rate_adjust OFF** | **125.0 / 128.1 / 128.3** |

The 3.3 ms p50→p99 spread on the final row is the tightest chain ever measured
on this hardware. Two laws the night established, which own the next steps:

1. **Conservation:** in a fully backpressured (blocking-writer) chain with
   rate-matched ends, end-to-end latency equals the SUM OF QUEUE CAPACITIES —
   every warmup head-start becomes permanent fill. `rate_adjust` makes it
   worse (packs every stage). Productization lever #1: a **target-bounded
   writer** (publish blocks at occupancy ≥ 2 slots, not at capacity) — the
   capacity stays large for negotiation/safety, the standing fill does not
   (est. −26 ms: Ring A 4→2 slots + the usbsink lane stops backing up).
2. **The last aloop pays the residual:** with Ring A railing, standing fill
   relocates into the usbsink→fan-in snd-aloop lane (invisible to /state).
   Productization lever #2: **direct gadget capture in fan-in** (delete the
   lane) — est. −40 ms. Modeled floor after both levers: **~55–60 ms**, with
   the 40s reachable via outputd/DAC floor work and CamillaDSP internals.

Operational lessons baked into the scripts (verify before productizing):
statefile seeding is owned by the unit ExecStartPre (live ws swap is the
apply lever); orderly transition = stop camilla → restart fanin → restart
outputd → start camilla → ws swap (violent simultaneous restarts crashed
camilla on a dual device-death and start-limited fanin); ring files need
root:jasper 0664 after reader-first creation; the arm probe must not run as
an unprivileged user against a root-owned ring dir (EACCES).
