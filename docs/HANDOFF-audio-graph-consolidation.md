# Handoff: audio-graph consolidation campaign

> **Status: active campaign plan (2026-07-03).** This is the canonical
> architecture + sequencing document for consolidating JTS's audio graph
> onto one transport primitive (SHM slot rings + the `jts_ring` ioplug)
> and one clock discipline (DAC-paced graph, each foreign clock
> reconciled exactly once at its ingress), then deleting every duplicate
> and legacy path — including snd-aloop itself. Grounded in a file-level
> audit of `main` at commit `c287ee13` (2026-07-03). Companion docs:
> [HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md) (measured
> ring evidence, USB DIRECT, host clock),
> [HANDOFF-audio-latency-foundation.md](HANDOFF-audio-latency-foundation.md)
> (clock-domain archaeology), [audio-paths.md](audio-paths.md) (today's
> lane map — rewritten at campaign end).

## End state

- **One transport primitive.** SHM slot rings (`rust/jasper-ring` +
  `c/jts-ring-ioplug`) carry every intra-graph hop: renderer → fan-in,
  fan-in → CamillaDSP (Ring A), CamillaDSP → outputd (Ring B).
- **One clock discipline.** outputd's blocking DAC write paces the graph.
  Each foreign clock is reconciled exactly once at its ingress: USB gadget =
  host-clock servo (`rust/jasper-host-clock`) + `LaneResampler`; each network
  renderer = its per-lane `LaneResampler` at fan-in ring-read; TTS = clockless
  socket (`/run/jasper-fanin/tts.sock` — already the end state, no change);
  bonded followers = snapclient sample-stuffing (stays).
- **snd-aloop GONE**: module load, `deploy/modprobe.d/snd-aloop.conf`,
  `deploy/modules-load.d/snd-aloop.conf`, all `/etc/asound.conf` loopback
  lanes, aloop-specific catch-up/park workarounds, cable-wedge lore.
- **Deleted**: Python usbsink audio pump, usbsink solo aloop lane,
  fan-in→Camilla loopback coupling, outputd aloop content bridge,
  `rate_match`, `transport_pipe`, lean-FIFO lane, adaptive-buffer shrink,
  legacy cushion recipes. Docs coherent, no orphan env keys,
  `.env.example` honest.

## No-dupes audit (main @ c287ee13, 2026-07-03)

Every duplicate/legacy path, what replaces it, which phase deletes it, and
what must move first. "Lane N" = snd-aloop substream pair N per
[`deploy/alsa/asoundrc.jasper`](../deploy/alsa/asoundrc.jasper).

### A. USB ingress — three generations coexist

| Path | Lives in | Replaced by | Deleted in |
|---|---|---|---|
| **A1. Python/PortAudio pump** (lab-gated) — **DELETED** | was `jasper/usbsink/audio_bridge.py`, `daemon.py`, `preempt_listener.py`, `state_publisher.py`, `jasper/cli/usbsink_main.py` (`jasper-usbsink-python-lab`) | Rust `jasper-usbsink-audio` (production since the low-latency train) | **DONE — USB dead-pipeline sweep** |
| **A2. Rust solo/aloop mode** — **DELETED** | was `rust/jasper-usbsink-audio/src/main.rs` bridging `hw:UAC2Gadget` → `usbsink_substream` (lane 3), incl. the aloop catch-up (`CATCHUP_HIGH_WATER_PERIODS` sawtooth) and the solo `Fill`-mode host clock | fan-in **USB DIRECT** combo (`JASPER_FANIN_USB_DIRECT=enabled` + `JASPER_USBSINK_AUDIO_STANDBY=1` always; usbsink keeps intent/state + gadget scripts, no more `:8781` preempt/HTTP) | **DONE (2026-07-10)** — audio loop deleted; standby daemon stays |
| **A3. Lean-FIFO lane** (default-off) — **DELETED** | was `Mux._enter_lean`/`_leave_lean` in `jasper/mux.py`, `jasper/usbsink/output_mode_reconcile.py`, `stage_lean_capture_config`/`apply_lean_capture_config`/`restore_buffered_config` in `jasper/sound/runtime.py`, `DEFAULT_LEAN_CAPTURE_FIFO` in `jasper/camilla_config_contract.py`, `JASPER_LEAN_LANE` / the `fifo` value of `JASPER_USBSINK_OUTPUT_MODE` env. The 2026-07-13 residue sweep also removed the producerless `capture_pipe_path` RawFile/resampler surface from both Camilla emitters and active graph recomposition. `playback_pipe_path` remains live for Snapcast. `jasper-camilla-pipe-guard` survives solely for its live Snapcast PLAYBACK-pipe protection; its dead RawFile CAPTURE-pipe branch was deleted after transport_pipe's 2026-07-11 removal left it with no consumer. | USB DIRECT + rings (shared path, protection kept) | **DONE — USB dead-pipeline sweep + emitter residue sweep** |

**Load-bearing finding (2026-07-03), now resolved: the lean lane was
unarmable on a production box.** `output_mode="fifo"` was implemented ONLY in
the Python lab bridge; the production Rust daemon had **no** FIFO mode (zero
`OUTPUT_MODE` reads in `rust/jasper-usbsink-audio`). The finding warned that a
naive cleanup deleting "the Python pump" but keeping the lean consumers would
preserve a silent-audio trap, so **A1 and A3 had to be deleted together** — the
USB dead-pipeline sweep did exactly that. `JASPER_USBSINK_OUTPUT_MODE` now only
ever takes the `aloop` value (recorded for route identity, unread by the
daemon).

### B. fan-in → CamillaDSP — two couplings (`JASPER_FANIN_CAMILLA_COUPLING`)

| Coupling | Lives in | Status |
|---|---|---|
| `loopback` | fan-in writes lane 7; CamillaDSP dsnoop-captures `plug:jasper_capture` | Legacy / fail-safe fallback for ring-ineligible or operator-frozen boxes; replaced by Ring A on eligible product-default boxes; deleted P7/P9 |
| ~~`transport_pipe`~~ | *(removed 2026-07-11 — fifo.rs + local_content_pipe deleted; fails safe to loopback)* | |
| `shm_ring` (Ring A, product default on eligible stereo boxes) | `RingOutput` transport in [`rust/jasper-fanin/src/mixer.rs`](../rust/jasper-fanin/src/mixer.rs), [`jasper/fanin_coupling.py`](../jasper/fanin_coupling.py) (`RING_CAPTURE_DEVICE = "jts_ring_capture"`), `JASPER_FANIN_RING_PATH`/`_SLOTS` | Shipped default via `jasper-fanin-coupling-reconcile --auto`; becomes the only coupling after the remaining snd-aloop removals |

**Load-bearing finding: fan-in's `RingOutput` keeps a lossy aloop MIRROR
on lane 7** (`mixer.rs` — the ring writer plus `mirror: Option<PCM>` on
`hw:Loopback,0,7`, non-blocking, never the pacer). Under ring coupling
the `jasper_capture`/`jasper_ref` dsnoop consumers therefore keep
working — which is why dsnoop re-pointing (P7) can safely come *after*
rings-default (P4) but MUST come before snd-aloop removal (P9). The
mirror itself is a hidden aloop dependency that dies in P7.

### C. CamillaDSP → outputd — three content bridges (`JASPER_OUTPUTD_CONTENT_BRIDGE`)

| Mode | Lives in | Status |
|---|---|---|
| `direct` | outputd reads `outputd_content_capture` (lane 6) / `outputd_active_content_capture` (lane 5, N-ch) via `alsa_backend.rs`/`dac_content.rs` | Legacy / fail-safe stereo fallback and the active N-ch bridge; stereo path replaced by Ring B on eligible product-default boxes; active N-ch path is the P8 problem (below) |
| `rate_match` | [`rust/jasper-outputd/src/content_bridge.rs`](../rust/jasper-outputd/src/content_bridge.rs) + `JASPER_OUTPUTD_CONTENT_BRIDGE_{RING,TARGET,MAX_ADJUST}_*` env | Rejected in tuning (content xruns/EAGAIN); a second rate matcher inside an already DAC-paced domain — exactly the duplicate-clock class the end state forbids. Deleted P5c |
| `shm_ring` (Ring B, product default on eligible stereo boxes) | `shm_ring_source.rs`, `JASPER_OUTPUTD_SHM_RING_PATH`/`_SLOTS`; CamillaDSP writes via the `jts_ring_playback` ioplug device | Shipped default paired with Ring A by the coupling reconciler; becomes the only bridge for stereo topologies after fallback deletion |

`config.rs` already hard-fails `usb_low_latency_48k` claims combined with
~~`transport_pipe`~~/`rate_match` *(transport_pipe removed 2026-07-11)*, and
requires **full-range stereo** for `shm_ring` — see the P8 constraint.

### D. Renderer ingress — five aloop playback lanes (Tier 2)

| Lane | Writer | Config surface |
|---|---|---|
| 0 | librespot | `--device librespot_substream` in [`deploy/systemd/librespot.service`](../deploy/systemd/librespot.service) |
| 1 | shairport-sync | `output_device = "__RENDERER_DEVICE__"` in [`deploy/shairport-sync.conf.template`](../deploy/shairport-sync.conf.template), rendered by `deploy/lib/install/renderers.sh` (which also renders `__AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__` from the active CamillaDSP config) |
| 2 | bluealsa-aplay | `--pcm=bluealsa_substream` in [`deploy/systemd/bluealsa-aplay.service.d/jts-output.conf`](../deploy/systemd/bluealsa-aplay.service.d/jts-output.conf) |
| 3 | ~~jasper-usbsink (solo mode)~~ | was `JASPER_USBSINK_PLAYBACK_DEVICE=usbsink_substream` in `jasper-usbsink.service` — **DIED with A2 (P5a, 2026-07-10)**; fan-in DIRECT-captures the gadget now. Never part of Tier 2. |
| 4 | correction/test sweeps | `correction_substream` ([`jasper/correction/playback.py`](../jasper/correction/playback.py)) |

All five are `plug:` wrappers (44.1→48 via libsamplerate — the
`defaults.pcm.rate_converter` AEC HF-loss history). Replacement: per-lane
`jts_ring` ioplug ingress (design section below). Migrated one at a time
in P6, shairport LAST.

### E. snd-aloop platform + its workaround ecosystem

- Module: `deploy/modprobe.d/snd-aloop.conf` (`pcm_substreams=8`,
  `pcm_notify=0`, index=6 pinning) + `deploy/modules-load.d/snd-aloop.conf`.
- `/etc/asound.conf` lanes: rendered from `deploy/alsa/asoundrc.jasper` —
  all `*_substream` PCMs, `outputd_content_*`, `outputd_active_content_*`,
  `jasper_capture` dsnoop, `jasper_ref`, plus the legacy `jasper_out` dmix
  rollback block (`__DONGLE_CARD__`).
- Cable-wedge handling: the LoopbackAEC card was already deleted
  (2026-05-11, UDP replacement) — surviving artifacts are lore/comments in
  [`jasper/audio_io.py`](../jasper/audio_io.py) `UdpMicCapture`,
  `jasper/cli/doctor/aec.py`, and the modprobe comment block.
- Restart choreography: `park_audio_clients_for_core_graph_restart` /
  `reset_failed_core_graph_restart_targets` in
  [`deploy/lib/install/systemd-units.sh`](../deploy/lib/install/systemd-units.sh)
  exist because aloop pairs param-lock to their first opener across the
  core-graph restart; rings (self-describing header, attach-any-order,
  stale-ring guard shipped in the #1137–#1142 train) shrink this.
- Doctor pins: `check_loopback` (aplay -L CARD=Loopback),
  `check_fanin_asound_wiring` (lane-7 dsnoop shape),
  `check_shairport_sync_loopback_plughw`, `check_renderer_device_resolvable`
  — all in `jasper/cli/doctor/{audio,renderers}.py`; each goes stale at its
  migration phase and is rewritten, not deleted.

### F. dsnoop lane-7 consumers (re-point before snd-aloop removal)

1. **aec_tune**: `arecord -D jasper_capture` in
   [`jasper/cli/aec_tune.py`](../jasper/cli/aec_tune.py) → re-point at
   outputd's `:9891` reference (already the production AEC reference) or a
   fan-in ring tap.
2. **jasper_ref rollback**: `REF_DEVICE = "jasper_ref"` in
   [`jasper/cli/aec_bridge.py`](../jasper/cli/aec_bridge.py) — the
   explicit ALSA-fallback reference for the AEC bridge. Retire or re-point;
   its loss under `transport_pipe` was already accepted as
   diagnostic-only.
3. **Doctor topology checks**: `check_fanin_asound_wiring` +
   `check_aec_asound` (`jasper/cli/doctor/aec.py`).
4. `jasper/correction/playback.py` docstring topology.

### G. Clock/rate-matcher inventory (target: one per foreign clock)

Already consolidated (keep): `jasper_clock::Dll` (one impl, used by
fan-in lane resamplers, outputd reference/AEC clocks, host-clock crate);
`jasper-resampler` (`RateController`, S32→S16, `minimum_safe_fill_frames`
— single source, pinned by cross-crate vectors); `rust/jasper-host-clock`
(one servo core, `Fill`/`Correction` modes). Duplicates to delete:
`rate_match` (C), CamillaDSP `enable_rate_adjust`+AsyncSinc in lean
configs (A3), the usbsink aloop catch-up sawtooth (A2). On the ring graph,
CamillaDSP `enable_rate_adjust` is off; the blocking one-clock chain bounds
latency through the Ring A/B capacities instead. Stays: snapclient
sample-stuffing on bonded chains.

### H. Legacy cushion recipes

The lab resampler geometry `TARGET_FRAMES=256 + WARMUP_CUSHION_FRAMES=256`
(held 512) is **below** the fail-loud floor shipped in #1145
(`STATIC_CUSHION_JITTER_MARGIN_FRAMES`: held ≥ 562 at period 256 / ±500 ppm)
and now **reboot-loops** a box that still carries it (fanin
`StartLimitAction=reboot`). jts.local's live env is the known carrier —
its cushion must be ≥ 306 before any deploy. P5c deletes the stale recipe
prose from docs/env comments; the shipped production defaults (held 2560)
are immune.

### H.1 jts.local lab→product ring migration (REQUIRED before the first P2 deploy)

**Only jts.local needs this. jts3 / jts5 / jts4 are NOT lab-armed and need
nothing.** jts.local is currently ring-armed via the *lab* `ring-proto` tooling,
which collides with the P1/P2 product ring assets:

- The lab `arm-ring-a.sh` / `arm-ring-proto.sh` wrote **marked env blocks** into
  the SAME `/var/lib/jasper/fanin.env` and `/var/lib/jasper/outputd.env` the
  coupling reconciler now owns (`JASPER_FANIN_CAMILLA_COUPLING=shm_ring`,
  `JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`).
- They dropped hand `98-jts-ring-a-proto.conf` / `98-jts-ring-proto.conf`
  conf.d files defining the **same PCM names** (`pcm.jts_ring_capture` /
  `pcm.jts_ring_playback`) that P1's shipped `60-jts-ring.conf` now defines —
  duplicate ALSA definitions.
- The lab used **16 slots**; the reconciler-canonical Ring B is **2**. And a
  hand CamillaDSP YAML, not the product emitter's config.

On the first P2 deploy, `ensure_outputd_camilla_statefile` would read the
lab-written `shm_ring` and seed `outputd-cutover-ring.yml` against that mixed
(lab-conf.d + lab-env + hand-YAML) state.

**Migration — run on jts.local BEFORE deploying this branch:**

```sh
# 1) Tear down BOTH lab rings (removes the 98-*-proto.conf drop-ins, strips the
#    marked env blocks, restores the pre-lab CamillaDSP config):
bash scripts/ring-proto/disarm.sh          # Ring B proto
bash scripts/ring-proto/disarm.sh --ring-a # Ring A proto  (see the script's flags)

# 2) Deploy this branch normally (installs 60-jts-ring.conf + the product path):
bash scripts/deploy-to-pi.sh

# 3) Re-arm via the PRODUCT reconciler (coherent BOTH-ring flip, 2 slots):
ssh pi@jts.local 'sudo /opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring'
sudo /opt/jasper/.venv/bin/jasper-doctor | grep -E "ring platform|fan-in coupling"
```

Verify the migration landed: no `98-jts-ring*-proto.conf` remains under
`/etc/alsa/conf.d/`, `fanin.env`/`outputd.env` carry only the reconciler-written
`shm_ring` keys (no lab marker comments), and the two doctor checks report `ok`.

### I. What the ring path still lacks for default status (the P1–P2 gap list)

Verified missing on main (2026-07-03):

1. **ioplug build/ship**: ~~`install.sh` builds the three Rust daemons but
   has zero `ioplug`/`jts_ring` references; the `.so` is built only by
   `scripts/ring-proto/build-on-pi.sh` into
   `/usr/lib/aarch64-linux-gnu/alsa-lib/`.~~ **CLOSED by P1**:
   `deploy/lib/install/ring-platform.sh` (`build_install_jts_ring_ioplug`)
   compiles `c/jts-ring-ioplug` via `make plugin` on-Pi (contained,
   sha-compared, degrade-to-warn on failure) and installs the `.so` to the
   arch plugin dir. Called from both install paths in `install.sh` main().
2. **conf.d shipping**: ~~`pcm.jts_ring_playback` / `pcm.jts_ring_capture`
   exist only as lab drop-ins (`/etc/alsa/conf.d/98-jts-ring*-proto.conf`,
   written by `scripts/ring-proto/arm*.sh`).~~ **CLOSED by P1**: shipped as
   the product file `deploy/alsa/conf.d/60-jts-ring.conf` (installed to
   `/etc/alsa/conf.d/`, 0644), plus the `/dev/shm/jts-ring` directory via
   `deploy/tmpfiles/jts-ring.conf` (mode 2775 `root:jasper`). Both INERT —
   nothing opens the PCMs / no ring file is created until P2 arms a coupling.

   Ring open/recovery is one cross-language transaction. Both the C ioplug and
   Rust ring crate take the persistent adjacent `<ring path>.open.lock` flock
   (0660; bounded 500 ms acquisition) before they classify, conditionally
   reclaim, create, or initialize a ring. The lock stays held through a final
   fd-versus-path inode check, so a stale torn-inode reclaimer cannot unlink a
   valid replacement and a creator cannot report success on an unlinked private
   mapping. Valid-magic geometry/version failures remain fatal and are never
   reclaimed. Lock contention fails with `EAGAIN` without touching the ring;
   there is no background repair loop. Structured open/reclaim events emitted
   by either implementation use the role-qualified shared vocabulary
   `event=jts_ring.reader.*` / `event=jts_ring.writer.*`; these events describe
   the ring endpoint role, not whichever daemon happens to invoke the Rust
   crate.
3. **Config emission**: ~~no product emitter can produce a ring CamillaDSP
   config.~~ **CLOSED by P2**: `capture_kwargs_for_coupling("shm_ring")`
   (`jasper/fanin_coupling.py`) now returns the FULL end-to-end ring topology —
   capture `jts_ring_capture` AND playback `jts_ring_playback`, both S16_LE — so
   `emit_sound_config` emits a coherent ring config through the product carrier;
   `emit_flat_ring_config` (`jasper/sound/camilla_yaml.py`) is the ring sibling of
   the flat cutover config. The statefile seeder (`safe_graph_for_current_topology`
   `coupling="shm_ring"`) re-seeds `outputd-cutover-ring.yml` on a ring-armed box,
   so a camilla restart / deploy keeps the ring instead of reverting to loopback
   (finding 5 dies here). install.sh renders BOTH flat configs and passes
   `--coupling`/`--ring-flat-config` to the seeder.
4. **Ordered-transition ownership**: ~~`shm_ring` has no ordered arm/disarm.~~
   **CLOSED by P2**: `jasper-fanin-coupling-reconcile shm_ring` is a first-class
   mode. `_arm_ring` PREFLIGHTs P1 ring assets (`ring_assets_ready`), topology
   eligibility (`ring_topology_ready`), and BOTH geometry axes — the period
   (`ring_geometry_ready`: conf.d period == outputd period) AND the Ring-A slot
   count (`ring_slot_geometry_ready`: `JASPER_FANIN_RING_SLOTS` == conf.d
   `jts_ring_capture` `n_slots`; the stale slot-value hole the period gate missed).
   It also self-heals a shear-prone stale slot value out of fanin.env
   (`_migrate_stale_fanin_ring_slots`) and deletes a geometry-mismatched on-disk
   ring file (`_delete_stale_ring_files`, tmpfs transport state) before bouncing the
   daemons. Then it flips BOTH ends coherently (`_outputd_actions` is the single
   writer of the `JASPER_FANIN_CAMILLA_COUPLING=shm_ring` +
   `JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring` pair), ordered outputd→fanin→camilla,
   and fail-safes to loopback+direct on any failure or a partial flip.
5. **Topology-contract citizenship**: ~~know nothing of ring mode.~~ **CLOSED by
   P2**: `topology_supports_shm_ring` (`jasper/active_speaker/runtime_contract.py`)
   is the ring-eligibility predicate — solo-stereo/unconfigured only, NOT roleful /
   composite / explicit-mono (P8's ring-v2 problem). Two consumers actually consult
   it: (a) the coupling reconciler's arm preflight (`ring_topology_ready` in
   `jasper/fanin/coupling_reconcile.py`) refuses `shm_ring` on a non-eligible
   topology with a crisp reason (fail-safe to loopback), before outputd's Rust
   full-range-stereo rejection would; (b) the statefile seeder's ring branch
   (`safe_graph_for_current_topology`) gates the ring flat config on the predicate,
   not just `not requires_roleful_graph`, so a composite/mono box carrying a stale
   `coupling=shm_ring` falls back to the loopback flat config instead of seeding a
   stereo-ring config it cannot play. The multiroom bond-formation precheck is a
   *coupling* gate (`read_persisted_coupling == shm_ring` refuses a bond), not a
   topology gate — it does not consult this predicate.
   `transport_topology_for_coupling` names the resolved ring topology.
6. **Doctor**: ~~no ring asset/drift checks;~~ **Ring-asset check CLOSED by P1**
   (`check_ring_platform_assets`), **made ARMED-AWARE + coherence checks added by
   P2**: armed boxes skip the open-probe (EBUSY is not a defect) and a missing
   asset is a hard `fail`; `check_fanin_coupling` now verifies the coherent ring
   pair (capture/playback devices + the outputd bridge) and warns on a partial flip
   or a finding-5 revert; `check_ring_geometry_coherence` verifies the Ring-A
   `n_slots` agrees across all three axes (env ↔ conf.d ↔ on-disk ring header) when
   armed and skips cleanly when not (the 2026-07-05 geometry-hole surface);
   `check_fanin_service` recognizes the `shm_ring` transport. The E-list
   loopback-check rewrites remain a later-phase (P7/P9) task.
7. **/state observability**: ~~`/state.audio_graph` needs the resolved transport.~~
   **CLOSED by P2**: `/state.audio_graph.coupling` surfaces the persisted coupling,
   the outputd content bridge, whether the pair is coherent, and the live fan-in
   transport (`_coupling_state` in `jasper/control/state_aggregate.py`).
8. **Certification**: ~~the route-latency artifact binder REFUSES `shm_ring`.~~
   **CLOSED by P2**: `_route_policy_errors` (`jasper/audio_runtime_plan.py`) accepts
   the COHERENT ring pair (coupling=shm_ring AND bridge=shm_ring) for
   `usb_low_latency_48k`, still rejecting a partial flip / ~~`transport_pipe`~~ /
   `rate_match` *(transport_pipe removed 2026-07-11)*. So a ring-armed box's shipped low-latency claim no longer goes red.
9. **Rollback env**: defaults still `loopback`/`direct`; the default flip
   needs one documented rollback key pair until burn-in ends. (P2 keeps the default
   loopback; the P4 flip owns the documented rollback pair.)

## Phase map

Each phase = one PR-able unit. Constraint order per the campaign
directive: rings default BEFORE loopback deletion; renderer migrations one
at a time, AirPlay LAST; dsnoop consumers re-pointed before snd-aloop
removal; USB default-on gated on the in-flight compliance-persistence PR +
user burn-in. Full profile = jts, jts3, jts5; streambox = jts4 (runs the
FULL renderer/DSP graph — fanin, camilla, outputd, mux, all renderers —
only voice/AEC parked, per `park_streambox_brain_units`; every phase below
touches it EXCEPT the USB phases, gadget-dependent).

| # | Phase (one PR each) | Scope (files) | Validation gate | Rollback lever | Boxes |
|---|---|---|---|---|---|
| P0 | **(in flight)** compliance persistence | fanin compliance state module | its own PR gates | env off | jts.local |
| P1 | Ring platform ship (inert) | `install.sh` + `deploy/lib/install/`: build `c/jts-ring-ioplug` (needs `libasound2-dev`), install `.so` + conf.d for both ring devices + `/dev/shm/jts-ring` tmpfiles/perms; doctor `ring assets` check | deploy jts3: assets present, default-off byte-identical (doctor clean, AirPlay pass) | none needed (inert) | all 4 |
| P2 | Ring citizenship | emitters (`sound/camilla_yaml.py` + carrier) emit ring capture/playback; `coupling_reconcile` learns `shm_ring` (ordered arm/disarm + activation gate); topology-contract + statefile seeding; artifact binder + `audio_runtime_plan` accept ring for `usb_low_latency_48k`; `/state` + doctor drift checks; multiroom prechecks extended to `shm_ring` (risk 6) | jts3 arm→disarm round-trip via reconciler + AirPlay clicks; jts.local quick route artifact under ring | ordered disarm → loopback | full first |
| P3 | **LANDED** — USB combo default-on where gadget present | `coupling_reconcile --auto` (single writer) writes `JASPER_FANIN_USB_DIRECT`/`_HOST_CLOCK`/`_RESAMPLER_CUSHION_DECAY=enabled` into fanin.env on a gadget box (`dtoverlay=dwc2,dr_mode=peripheral` present), clears them off one; config.rs decay-floor default → validated **576** so a combo-armed default constructs (`jasper.fanin.coupling_auto`) | user smoke-test approval; quick artifact; soak | operator marker + unset the 3 flags (see below) | jts.local |
| P4 | **LANDED** — Rings default (solo-stereo topologies) | `coupling_reconcile --auto` resolves the default coupling to `shm_ring` when ALL #1169 arm preflights pass (assets+topology+geometry), else loopback; runs on deploy (install.sh `resolve_fanin_coupling_default`) + boot (`jasper-fanin-coupling-auto.service`); operator-choice marker (`JASPER_FANIN_COUPLING_CHOICE`) freezes a revert; `/state.audio_graph.coupling.choice` surfaces operator-vs-auto | per-box deploy + AirPlay/Spotify/BT pass + 24 h burn-in each; doctor green | marker + `JASPER_FANIN_CAMILLA_COUPLING=loopback` + `JASPER_OUTPUTD_CONTENT_BRIDGE=direct` | jts, jts3, jts4; **jts5/active EXCLUDED** (P8) |
| P5a | Delete: Python pump + lean lane + solo aloop mode | **DONE** — A1+A3 (USB dead-pipeline sweep: Python bridge + lean-FIFO consumers + `JASPER_LEAN_LANE` / `fifo` output-mode) plus **A2 (2026-07-10)**: the Rust solo `usbsink_substream` bridging + catch-up + solo `Fill`-mode host clock + `:8781` preempt/tap are deleted; the daemon is now standby-only (`JASPER_USBSINK_AUDIO_STANDBY` always `1`) | fleet deploy; USB DIRECT regression on jts.local | revert PR | all (USB bits jts.local) |
| P5b | Delete: transport_pipe | **DONE (2026-07-11)** — `fifo.rs`, `local_content_pipe.rs`, coupling branch + reconciler branch + prechecks, and the `JASPER_FANIN_CAMILLA_PIPE`/`JASPER_OUTPUTD_LOCAL_CONTENT_PIPE` env keys deleted; fails safe to loopback | fleet deploy + doctor | revert PR | all |
| P5c | Delete: rate_match + adaptive-buffer + cushion recipes | `content_bridge.rs` rate-matcher, `fanin/buffer_reconcile.py`, mux `_settle_adaptive_buffer`, stale env/doc recipes | fleet deploy + doctor | revert PR | all |
| P6a | librespot → ring ingress | fanin per-lane ring-reader `Input` variant; librespot conf.d lane; unit `--device` | Spotify router start/transfer on jts.local + jts3 | per-lane env: lane back to aloop (until P9) | all |
| P6b | bluealsa → ring ingress | drop-in `--pcm` | BT pairing + playback via Mac | same | all |
| P6c | correction sweeps → ring ingress | `correction/playback.py` lane | one sweep capture on jts3 | same | full |
| P6d | **shairport LAST** → ring ingress | conf template device + offset re-derivation | Music.app local-track loop + resync-log watch + bonded A/V spot-check | same | all |
| P7 | Re-point dsnoop consumers; drop fan-in mirror | F-list: aec_tune → `:9891`, jasper_ref retire, doctor rewrites, `mixer.rs` mirror removal | AEC bridge regression (wake-rate spot check) on jts.local | revert PR | full |
| P8 | Ring v2: N-channel + bonded round-trip | ioplug channel negotiation (header already self-describing — `OFF_RATE`/`OFF_CHANNELS` in `layout.rs`; the C plugin pins 2ch), outputd active-content ring, snapclient follower round-trip ingress; extend or explicitly re-scope | jts5 dual-DAC regression + S0-sync bench on a bonded pair | active boxes stay on aloop lanes 5/6 | jts5 + bonded |
| P9 | snd-aloop removal | modprobe.d, modules-load.d, asound lanes, `check_loopback`, cable lore, park choreography simplification | full-fleet deploy + doctor + every-source pass; reboot test per box | revert PR + reboot | all |
| P10 | Polish sweep | dead code, env pruning, `audio-paths.md` rewrite, supersessions, atlas/doc-map, memory | docs gates + `test_env_vars_codified` | n/a | — |

Deletions are separate PRs by repo guardrail. P4 starts the burn-in clock
that P5+ waits on. P8 may land before or parallel to P6 — it gates only P9.

### P3/P4 landed — what shipped + finding G resolution

The default-flip landed as one PR (`audio/default-flip-p3-p4`), built on the
#1169 overnight fix batch (its geometry preflight, self-heal, storm fixes, and
zombie-handle reopen are prerequisites). What it does:

- **Default resolution owner.** `jasper.fanin.coupling_auto` holds the pure
  decision; `jasper.fanin.coupling_reconcile.reconcile_auto` (the `--auto` CLI
  mode) owns the env I/O (pattern 3 — the reconciler is the single env writer).
  It runs on deploy (`install.sh` → `resolve_fanin_coupling_default`) and boot
  (`jasper-fanin-coupling-auto.service`).
- **Coupling default (P4).** On a solo, ring-eligible box the default coupling is
  `shm_ring`, resolved by gating on the SAME #1169 arm preflights a manual arm
  uses (`ring_assets_ready` + topology + `ring_geometry_ready` +
  `ring_slot_geometry_ready`) PLUS two auto-only gates: a ROUTE-support gate
  (`ring_route_ready` — a grouped box resolves `loopback`, so the boot unit does
  not fail on a healthy leader/follower) and the fail-CLOSED topology variant
  (`ring_topology_ready_strict` — an unreadable topology resolves `loopback` instead
  of arm→rollback-churning every boot). Any gate failing → `loopback`. Before the
  gates run, the auto pass self-heals a shear-prone stale `JASPER_FANIN_RING_SLOTS`
  exactly as a manual arm does, so a stale old-default `=8` line does not DISARM a
  box a manual arm would migrate and keep. Ineligible boxes (jts3 roleful, jts5
  composite, jts4 fanin-less, any grouped box) are a NO-OP that resolves loopback
  and succeeds.
- **USB combo default (P3).** The combo arms only on a box that BOTH has the gadget
  stack available (`dtoverlay=dwc2,dr_mode=peripheral` — added fleet-wide for the
  always-on USB network, so NOT a sufficient gate alone) AND has USB Audio Input
  turned ON by the household (`jasper-usbsink.service` enabled — the `/sources/`
  intent signal). When armed, the auto pass is the SINGLE writer of BOTH halves: the
  three fan-in keys (`JASPER_FANIN_USB_DIRECT`/`_HOST_CLOCK`/`_RESAMPLER_CUSHION_DECAY`
  `=enabled`) in fanin.env AND `JASPER_USBSINK_AUDIO_STANDBY=1` in usbsink.env (then
  it restarts jasper-usbsink so the bridge stands down and releases `hw:UAC2Gadget`).
  Off a combo box both halves are written their EXPLICIT off values (`disabled` / `0`,
  not unset — a stale `enabled` in jasper.env loads first and would otherwise win).
  The two halves must arm together or fan-in and the still-live bridge fight over the
  gadget capture and USB audio goes silent / crash-loops.
- **Floor default (P3).** `config.rs`'s `DEFAULT_CUSHION_DECAY_FLOOR_FRAMES` is the
  hardware-validated **576** — the exact floor the jts.local combo product-path gate
  proved stable. (The derived minimum `max(target,min_safe)+32` = 544 at the default
  geometry was always in range, so the pre-P3 default constructed fine; shipping 576
  is about landing on the PROVEN floor, not fixing a construct failure. It is still
  clamped into `[derived_min, ceiling]` so a small-target geometry constructs.
  Regression: `combo_armed_default_config_constructs`.)
- **Revert lever.** `JASPER_FANIN_COUPLING_CHOICE=operator` (written by the
  explicit reconciler CLI path) freezes the box — the auto pass never overrides
  an operator choice. `/state.audio_graph.coupling.choice` reports operator-vs-auto.
- **Entry serialization.** Every reconcile entry verb (`--auto`, `--health`,
  explicit CLI) runs under one advisory flock
  (`/run/jasper-fanin-coupling.lock`, `_acquire_entry_lock` in
  `coupling_reconcile.py`) — the two oneshot units have no systemd ordering
  between them, and install.sh / the operator CLI run the same verbs, so
  without it two concurrent passes could interleave their ordered daemon
  transitions (worst case reproducing the camilla RTTIME-SIGKILL cascade #1233
  fixed). Bounded 10 s wait; nothing touches env or daemons on contention.
  Loudness is verb-specific: `--auto` / explicit (a requested *change*) abort at
  ERROR with exit 1 → the oneshot lands `failed`, which
  `check_service_runtime_state` now tracks; the periodic `--health` watcher
  stands down at WARNING with exit 0 (a reconcile in flight is when it has
  nothing to observe — failing its unit on every deploy-arm collision would be a
  false doctor positive).

**Finding G resolved: Ring-A slot default is 2.** The production default is now
`DEFAULT_FANIN_RING_SLOTS = 2` and the packaged `jts_ring_capture` conf.d block
pins `n_slots = 2`. With `period_frames = 128`, Ring A contributes ≈5.3 ms at
48 kHz (`2 * 128 / 48000`) instead of the old 8-slot placeholder's ≈21.3 ms.
The CamillaDSP ring emit moves in lockstep to chunk 128 / target 128 /
queuelimit 1 with `enable_rate_adjust: false`; chunk 256 would span the whole
2-slot Ring-A buffer. Ring B was already 2 slots and is unchanged.

Hardware evidence for the default flip: the 40 ms-descent PoC measured 35.4 ms
tap→ref on the 2-slot/chunk-128 geometry, and the 2026-07-06 primed product-path
run measured 54.3 ms tap→ref with chunk 128 / target 128 / queuelimit 1.
Reconstruction puts the old 8-slot/deep-queue default at ≈90-95 ms e2e and the
new geometry at ≈48.8 ms e2e. #1169's geometry preflight, stale-ring-file
delete, CONFIRM-path self-heal, and doctor three-way coherence check are the
migration guardrails for already-armed 8-slot boxes.

## Renderer ingress design (Tier 2 core)

**Delay honesty — the shairport question.** The ioplug playback direction
reports an honest, occupancy-derived position: `jts_ring_pointer` /
`jts_ring_pointer_report` (four adversarial rounds fixed the
dishonest-pointer and mod-buffer-alias classes), and the `.delay`
callback (`jts_ring_delay` in
[`c/jts-ring-ioplug/pcm_jts_ring.c`](../c/jts-ring-ioplug/pcm_jts_ring.c))
returns `published-unread slots × period + staged frames`. So a sync
engine reading `snd_pcm_delay` sees truth — the OPPOSITE failure mode of
the aloop history, where `snd_pcm_delay` returned loopback ring FILL, not
DAC latency, and caused the ~60 s resync glitch storm until
`resync_threshold_in_seconds = 0.2` (PR #83; comments in the conf
template). Two consequences: (1) shairport's compensation value
`audio_backend_latency_offset_in_seconds` (rendered by `renderers.sh`
from the configured downstream buffers) MUST be re-derived for the ring
graph — the ring holds frames the offset math previously attributed to
the aloop ring; (2) with an honest small delay, `resync_threshold=0.2`
may be revisitable, but only AFTER measurement — keep 0.2 through the
migration. This is why AirPlay migrates last.

**Per-lane clock reconciliation stays at fan-in ring-read.** Renderer
lanes keep their `LaneResampler` (fan-in side) exactly as on aloop — the
transport changes, the one-rate-matcher-per-foreign-clock placement does
not. The ioplug is a dumb frame carrier.

**ALSA conf shape per renderer.** The ioplug pins 48 kHz/S16/2ch
(`JTS_RING_RATE`/`JTS_RING_CHANNELS` in `pcm_jts_ring.c`), but renderers
emit native rates (AirPlay 44.1k, BT variable), so each lane keeps its
`plug:` wrapper layered over a ring device:
`pcm.shairport_substream { type plug; slave { pcm "jts_ring_lane_airplay"; rate 48000; ... } }`
— preserving `defaults.pcm.rate_converter` (libsamplerate; the AEC
HF-loss history) and keeping renderer configs name-stable (unit files
don't change at migration, only the conf.d definition behind the name —
each lane's flip is one conf edit + renderer restart). Definitions ship
system-wide 0644 (conf.d), and each migration PR MUST re-run the PR #214
probe: `sudo -u <runtime-user> aplay -D <device> ...`
(`check_renderer_device_resolvable` codifies it). **Permissions design
point**: the ioplug WRITER creates the ring file, and renderer users
(`shairport-sync`, `pi`) must create/write under `/dev/shm/jts-ring/` —
P1 ships the directory via tmpfiles.d with group-write (`root:audio`,
2775 or equivalent) and the header's owner/perm contract documented.

**Teardown/hotplug semantics.** aloop pairs persist param-locked across
renderer restarts (`pcm_notify=0`); rings are more forgiving: reader on
an empty/absent-writer ring emits silence (`JTS_RING_SLOT_EMPTY`
zero-fill), writer free-runs (drop-oldest) if the reader dies, heartbeat +
stale-ring guards shipped in the train. fan-in's per-lane reader mirrors
the USB DIRECT precedent: `Input.pcm: Option<PCM>` is already `None` on
the direct lane (`mixer.rs`), so Tier 2 adds a third lane source
(ring-reader) beside `lane`/`direct` with the same silent-idle +
bounded-retry presence model and `/state` `source:"ring"` labeling.

## Risk register

1. **AirPlay sync regression** (highest): honest ioplug delay changes the
   number shairport compensates; offset mis-derivation reintroduces the
   resync-storm class. Mitigate: migrate LAST (P6d), A/B Music.app
   local-track loop + resync-log watch, re-derive offset in the same PR,
   keep threshold 0.2, per-lane rollback to the aloop conf until P9.
   Bonded: the #919/#931 latency-fit observability may need re-fitting —
   re-run the bonded A/V spot-check.
2. **Active/composite + bonded aloop dependencies survive naive deletion**:
   ring is stereo-pinned; outputd requires full-range stereo for
   `shm_ring`; active N-ch content rides lane 5; bonded ACTIVE followers'
   snapclient writes `hw:Loopback,0,6` (`snapclient_argv` in
   [`jasper/multiroom/reconcile.py`](../jasper/multiroom/reconcile.py)).
   P9 is HARD-GATED on P8. jts5 and any bonded-active pair are excluded
   from P4's default flip by topology check, not by hostname.
3. **Half-armed multiroom graphs**: **CLOSED by P2/P4**:
   `precheck_active_leader`, `topology_supports_shm_ring`, and
   `reconcile_coupling` now gate `shm_ring` on the same active/composite
   topology contract before any default arm, so a bonded active graph cannot
   split camilla#1 across ring and driver-domain topologies.
4. **Fleet box wedged mid-migration**: coupling transitions are owned by
   the ordered reconciler with fail-safe-to-loopback; camilla ExecStartPre
   statefile re-seed means any camilla restart reverts to the contract
   config (fail-safe = silence, not noise); fanin `StartLimitAction=reboot`
   interacts with stale-ring races (fixed in-train — keep the crash-loop
   regression tests green). Every phase's rollback is env-only until P5.
5. **install.sh on-Pi C compile**: new build dep (`libasound2-dev`),
   ~seconds of cc. Follow `rust-daemons.sh` patterns: sha256-compare to
   skip rebuilds, low-RAM guard, and a build failure must degrade to
   "ring unavailable + doctor warn," never a failed install (loopback
   remains the fallback until P9 — after P9 the ioplug is load-bearing
   and its absence must fail the install instead).
6. **CamillaDSP config drift**: ring configs MUST come from the carrier/
   emitters (P2), never hand YML; `camilladsp --check` + `volume_limit
   0.0` ceiling stay enforced (the lab generator already models this);
   conventions tests pin one emitter.
7. **Certification regression**: flip P4 only after the artifact binder
   accepts ring topologies (P2), else `usb_low_latency_48k` claims
   permanently fail and doctor goes red fleet-wide.
8. **Streambox divergence**: jts4 runs the full renderer graph — include
   it in P1/P4/P6 validation explicitly (it is the box most likely to be
   forgotten; it has no mic/voice to notice breakage audibly).
9. **USB default-on containment gap**: combo standby publishes
   `playing:false` — mux arbitration + Source UI blind to USB. The honest
   arbitration signal is IN P3's scope, not a follow-up.

## Done criteria

- **Code**: no `snd-aloop`/`Loopback` references outside historical docs;
  A1/A3/`transport_pipe`/`rate_match`/adaptive-buffer/cushion-recipe code
  deleted; guard tests assert the production route refuses every deleted
  env knob (the Legacy Cleanup rule).
- **Config**: modprobe.d + modules-load.d gone; `/etc/asound.conf` carries
  only DAC + ring-lane definitions; `.env.example` documents exactly the
  surviving keys (rollback keys removed after burn-in).
- **Clock discipline**: one rate matcher per foreign ingress, each visible
  in `/state` (fill/target/lock/ppm/xruns).
- **Fleet**: jts, jts3, jts4, jts5 on rings ≥ 7-day burn-in each, zero
  sustained resampler unlocks / ring rails / xruns; jts.local re-certified
  (quick + promotion route artifacts under the ring identity); per-box
  AirPlay/Spotify/BT/USB passes; bonded pair S0-sync bench if bonded.
- **Docs**: `audio-paths.md` rewritten to the ring lane map;
  `HANDOFF-usbsink.md`, `HANDOFF-speaker-output-reference.md`,
  `HANDOFF-fan-in-daemon.md` updated; foundation doc supersession banner
  finalized; README atlas + doc-map routing current; memory updated.
- **Evidence**: each phase's gate artifact linked from this doc's
  changelog appendix as phases land.

---

## Appendix: audit provenance

Audited on `main` @ `c287ee13` (2026-07-03) by direct file inspection:
`deploy/alsa/asoundrc.jasper`, `deploy/modprobe.d/` + `modules-load.d/`,
renderer units + `renderers.sh`, `rust/jasper-{fanin,outputd,usbsink-audio,
ring,host-clock,resampler,clock}`, `c/jts-ring-ioplug/`,
`scripts/ring-proto/`, `jasper/{fanin_coupling,audio_runtime_plan,
output_topology,camilla_config_contract}.py`, `jasper/fanin/`,
`jasper/usbsink/`, `jasper/sound/`, `jasper/multiroom/{reconcile,
follower_config,active_leader_config}.py`, `jasper/cli/doctor/{audio,
renderers,aec}.py`, `jasper/cli/{aec_tune,aec_bridge}.py`,
`deploy/lib/install/systemd-units.sh`, `.env.example`. Ring evidence and
measured floors: [HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md)
"Final state — 2026-07-03".

Last verified: 2026-07-12 (cross-language ring open/reclaim transaction and
role-qualified event vocabulary checked;
A2/P5a marked DONE — the Rust solo/aloop USB capture
path was deleted 2026-07-10; the standby daemon stays and `JASPER_USBSINK_AUDIO_STANDBY`
is now always `1`. Only the USB-ingress rows were re-verified this pass; other
phase rows unchanged.)
