# Old audio graph (snd-aloop) — ACTUAL-USAGE map

**Scope.** Where the snd-aloop graph is *actually opened at runtime*, by
whom, under which box state — as distinct from where it is *defined* or
*named*. Companion to `captures/LOOPBACK-CENSUS-2026-08-17.md`, which
remains the file/symbol SSOT and is not restated here. The value added
is the **runtime lens** (which process would call `snd_pcm_open` on
which device under state X) and **fleet ground truth** (what is open
right now).

No design proposals.

**The one distinction this document is built on.** A claim about *code*
("this consumer survives Phase 2") and a claim about *runtime* ("this
device is open") are different claims, and they can both be true at
once. Most of the campaign's apparent contradictions dissolve into that
gap. Where a prior document is right about code and wrong only about
the fleet, the ledger says so rather than calling it an error.

---

## 1. METHOD

**Pinned SHA.** All code citations are against
`200d54578cbf63373d1cc8849e1e7205334da41e` (`origin/main` at
2026-08-18T12:54Z), exported with `git archive` into an owned,
`.git`-free directory. No git command was run in any worktree.

**Fleet snapshot.** Two granted boxes, read-only probes only (no
`systemctl start/stop/restart`, no writes, no config change):

| Box | Target | Probe window | Installed build |
|---|---|---|---|
| jts.local | `pi@192.168.1.74` | 2026-08-18T12:54:06Z → 13:04:17Z | `620065dff`, `status=ok`, 2026-08-17T13:58:15-04:00 |
| jts4 | `jts4` | 2026-08-18T12:54:29Z → 12:59:29Z | `620065dff`, `status=ok`, 2026-08-17T14:03:56-04:00 |

Both run the **same** installed build (`620065dff`), which is *behind*
the pinned SHA. jts3 was not touched (peer session owns it); jts5 was
not touched (unplugged). Every fleet claim is scoped to two boxes and
says so.

**How the sweep was done.** Runtime: all 32
`/proc/asound/Loopback/pcm{0,1}{p,c}/sub*/status` per box,
`fuser -v /dev/snd/*` over the full glob, `ps`, `systemctl is-active`/
`is-enabled` per renderer unit, `journalctl -u jasper-fanin` startup
blocks, live `/etc/asound.conf`, the live CamillaDSP statefile and the
config it names, `/etc/shairport-sync.conf`, the bluealsa-aplay
drop-in, `/var/lib/jasper/{renderer_lanes,fanin,outputd,aec_mode,
grouping}.env`, `output_topology.json`, `build.txt` (all `sudo -n`), and
the presence/content of the operator probe scripts on the Pi. Code:
ALSA/modprobe confs and units read directly; Rust, Python,
unit/shell/installer, and prose surfaces swept exhaustively against the
pinned export.

**Failed probes (recorded, not retried).**
- `lsof` is not installed on jts.local. The same question was answered
  by `fuser -v /dev/snd/*` over the full glob.
- `sudo -n tr '\0' '\n' < /proc/<pid>/environ` failed `Permission
  denied` on both boxes (the shell performs the redirect before `sudo`
  applies). The `/var/lib/jasper/*.env` files were read instead, so
  effective-environment statements are **[I]** from env files plus
  systemd load order, not **[M]** from the running process.

**Three verbs, kept rigorously apart.** Table cells are **OPENS** only.

- **DEFINES** — an alias or constant exists.
- **REFERENCES** — code, a unit, an env file, a log line, or prose
  *names* the device. **Naming is not opening.**
- **OPENS** — a live process would call `snd_pcm_open` and hold an fd on
  `/dev/snd/pcmC<card>D<dev><dir>` under the stated state.

A fourth verb earns its own column below because it is where most
config-generation code sits: **GENERATES-CONFIG** — Python emits a
CamillaDSP YAML naming an aloop device, so *CamillaDSP* opens it. The
emitter never touches ALSA.

**Honesty legend.** **[M]** measured on a live box · **[I]** inferred
from code at the pinned SHA, not observed running · **[N]** absent after
search (a first-class answer) · **UNKNOWN** — legal cell value.

**Disambiguation for future sweepers:** `deploy/systemd/camillagui.socket`
matches a grep for "loopback" but means **network** loopback
(`127.0.0.1:5005`). Not counted anywhere below.

---

## 2. THE TRUTH TABLE

### 2.0 The state axes the code actually discriminates on

| Axis | Values | Owner | **Code default** | **Fleet value** |
|---|---|---|---|---|
| **Camilla coupling** | `loopback` / `shm_ring` | `JASPER_FANIN_CAMILLA_COUPLING` (`fanin.env`) | **`loopback`** — `Coupling::from_env_value` fail-safes to it; Python `resolve_coupling(None)` likewise | **`shm_ring`** both **[M]** |
| **Per-lane renderer arming** | lane ∈ / ∉ armed list | `JASPER_FANIN_RENDERER_RING_LANES` (`renderer_lanes.env`) | **empty** — no `Environment=` in the unit; nothing writes the file at install | **all four armed** **[M]** |
| **USB direct** | `enabled` / else | `JASPER_FANIN_USB_DIRECT` | not `enabled` → aloop fallback | jts.local `enabled`; jts4 `disabled` **[M]** |
| **outputd content bridge** | `direct` / `shm_ring` | `JASPER_OUTPUTD_CONTENT_BRIDGE` (`outputd.env`) | `direct` (unit `Environment=`) | **`shm_ring`** both **[M]** |
| **Camilla playback sink** | ring device / non-ring | emitted per graph | non-ring | **ring** both **[M]** |
| **Grouping role** | solo / passive leader / active leader / active follower / dumb member | `grouping.env` | `off` | both **solo**; snapclient `inactive`+`disabled` **[M]** |
| **Correction sweep** | running / not | spawn lifetime | — | not running at probe time **[M]** |

**Two structural facts that reorganize the whole table.**

1. **The capture device follows the SINK, not the coupling.**
   `jasper/active_speaker/camilla_yaml.py` `capture_device_for_playback`
   is the single owner: `if playback_device not in RING_PCM_DEVICES:
   return DEFAULT_CAPTURE_DEVICE` (`plug:jasper_capture`), else
   `jts_ring_capture`. Both boxes emit a ring sink, so both capture the
   ring — which is what the fleet measurement shows. **[I+M]**

2. **A bonded box is FORCED onto the aloop coupling.**
   `jasper/audio_runtime_plan.py` `coupling_supported_for_route` refuses
   `shm_ring` for every grouping-enabled route mode
   (`active_leader`, `active_follower`, `invalid_grouping`), and
   `jasper/multiroom/active_leader_config.py` raises with the remediation
   "Run `jasper-fanin-coupling-reconcile loopback` before bonding."
   **[I]** The loopback coupling is therefore not dormant legacy — it is
   the *required* transport for any bonded box at this SHA. Untested on
   this fleet: both boxes are solo.

### 2.1 Per-pair truth table

Allocation owned by `deploy/modprobe.d/snd-aloop.conf`
(`options snd-aloop enable=1 index=6 id=Loopback pcm_substreams=8 pcm_notify=0`).
Cells are **OPENS**.

| Pair | Alias / device | WHO OPENS IT + condition | Evidence site | Fleet today |
|---|---|---|---|---|
| **0** | `librespot_substream` → `hw:Loopback,0,0` (W)<br>`hw:Loopback,1,0` (R) | **W:** librespot `--device ${JASPER_LIBRESPOT_DEVICE}`, in-unit default `librespot_substream` — only while `spotify` UNARMED.<br>**R:** fan-in `spotify` input.<br>**+ doctor probe** (§2.4). | `deploy/systemd/librespot.service`; `rust/jasper-fanin/src/config.rs` `input_pcms[0]`; open in `mixer.rs` `open_input` | **NOBODY [M]** — cmdline shows `--device librespot_ring_lane`; fan-in `source=ring` |
| **1** | `shairport_substream` → `hw:Loopback,0,1` (W)<br>`hw:Loopback,1,1` (R) | **W:** shairport-sync; `output_device` substituted per start by `jasper-apply-airplay-mode`, fallback `shairport_substream` — only while `airplay` UNARMED.<br>**R:** fan-in `airplay` input.<br>**+ doctor probe.** | `deploy/bin/jasper-apply-airplay-mode`; `deploy/shairport-sync.conf.template`; `config.rs` `input_pcms[1]` | **NOBODY [M]** — live conf has `output_device = "shairport_ring_lane"` |
| **2** | `bluealsa_substream` → `hw:Loopback,0,2` (W)<br>`hw:Loopback,1,2` (R) | **W:** bluealsa-aplay `--pcm=${JASPER_BLUEALSA_DEVICE}` — only while `bluealsa` UNARMED.<br>**R:** fan-in `bluealsa` input.<br>**+ doctor probe.** | `deploy/systemd/bluealsa-aplay.service.d/jts-output.conf`; `config.rs` `input_pcms[2]` | **NOBODY [M]** — runs `--pcm=bluealsa_ring_lane` |
| **3** | *no write alias*<br>`hw:Loopback,1,3` (R only) | **W: NOBODY IN ANY STATE [N]** — the usbsink solo bridge that wrote `hw:Loopback,0,3` was removed 2026-07-10; no alias, no unit, no code writes it.<br>**R:** fan-in's `usbsink` lane **idle fallback**, whenever USB direct is not the live source. | `config.rs` `input_pcms[3] = "hw:Loopback,1,3"`, aligned with label `usbsink`; branch `is_direct = usb_direct_enabled && label == input_resampler_lane_label` in `mixer.rs` | **jts4: OPEN [M]** (fan-in, capture, `source=lane`, `pcm1c/sub3 RUNNING`)<br>**jts.local: NOT open [M]** (`source=direct`) |
| **4** | `correction_substream` → `hw:Loopback,0,4` (W)<br>`hw:Loopback,1,4` (R) | **W-a:** the product's correction/measurement spawn — only while `correction` UNARMED.<br>**W-b: six operator diagnostic sites that hardcode the aloop name and ignore arming entirely** (§2.3).<br>**R:** fan-in `correction` input. | `jasper/audio_measurement/correction_lane.py` `correction_play_device()`; `config.rs` `input_pcms[4]` | **NOBODY [M]** — armed; fan-in `source=ring`; live writer lock is `lane-correction.ring.writer.lock` (owner `jasper-web`) |
| **5** | **none — PCM defs deleted (P9-C)** | **NOBODY IN ANY STATE [N]** — no alias to open; the endpoint vocabulary has no `aloop` value that could name it (`jasper/cli/active_speaker.py`: "there is no `aloop` rollback arm to name"). Index reserved to avoid renumbering. | `snd-aloop.conf`; `jasper/active_speaker/runtime_contract.py` (constant retained, "Production readers: ZERO") | **NOBODY [M]** |
| **6** | `outputd_content_playback` → `hw:Loopback,0,6`<br>`outputd_content_capture` → `hw:Loopback,1,6`<br>*plus* raw `0,6`/`1,6` | **Use A — passive content hop.** CamillaDSP writes `outputd_content_playback`; jasper-outputd reads `outputd_content_capture`. Gate: `CONTENT_BRIDGE=direct` **and** `SINK=single_alsa`.<br>**Use B — bonded round-trip.** snapclient writes raw `hw:Loopback,0,6`; the **active follower's** *and* the **active leader's camilla#2** capture raw `hw:Loopback,1,6` at S16_LE. Gate: `active_endpoint` true. | A: `deploy/camilladsp/outputd-cutover.yml`; `rust/jasper-outputd/src/alsa_backend.rs` (skipped when bridge is `ShmRing`). B: `jasper/multiroom/reconcile.py` `GROUPING_LOOPBACK_PLAYBACK`/`_CAPTURE`; `follower_config.precheck_active_follower`; `active_leader_config.precheck_active_leader`; `deploy/systemd/jasper-snapclient.service` | **NOBODY [M]** — both `CONTENT_BRIDGE=shm_ring`; both solo, snapclient `inactive`/`disabled`; `pcm0p/sub6` + `pcm1c/sub6` `closed` |
| **7** | `pcm.jasper_capture` (dsnoop on `hw:Loopback,1,7`)<br>`pcm.jasper_ref` (plug over it)<br>`hw:Loopback,0,7` (W) | **W:** fan-in's summed output, **Playback, blocking** — `loopback` coupling only.<br>**R-a:** CamillaDSP via `plug:jasper_capture` on any **non-ring sink**.<br>**R-b: the bonded ACTIVE leader's camilla#1 program bake**, which defaults to `plug:jasper_capture` and is structurally locked there (§2.5). | W: `config.rs` `JASPER_FANIN_OUTPUT_PCM`, opened in `mixer.rs` `open_output`. R: `jasper/camilla_config_contract.py` `DEFAULT_CAPTURE_DEVICE`; `active_leader_config.precheck_active_leader` | **NOBODY [M]** — `pcm0p/sub7` + `pcm1c/sub7` `closed` on both; fan-in opens `program.ring` |

### 2.2 Per-alias roll-call — "defined but never opened" made loud

| Alias / device | Opened by, in ANY state | Fleet |
|---|---|---|
| `librespot_substream` / `shairport_substream` / `bluealsa_substream` | their renderer — unarmed lane only; **+ the doctor's install probe** **[I]** | not open **[M]** |
| `correction_substream` | product spawn (unarmed only) **+ 6 operator sites unconditionally** **[I/M]** | not open **[M]** |
| `outputd_content_playback` | CamillaDSP — `direct` bridge only **[I]** | not open **[M]** |
| `outputd_content_capture` | jasper-outputd — `direct` bridge + `single_alsa` **[I]** | not open **[M]** |
| `pcm.jasper_capture` | CamillaDSP — any non-ring sink, incl. the bonded active leader's bake **[I]** | not open **[M]** |
| **`pcm.jasper_ref`** | **NOBODY, ANY STATE [N]** — both readers retired (AEC bridge fallback U4/P7-1; timing probe U4/P7-3). Its **only** remaining consumer is a doctor check that asserts its *presence* as text (§2.4). | not open **[M]** |
| **`hw:Loopback,0,3`** | **NOBODY, ANY STATE [N]** | not open **[M]** |
| **pair 5 (any name)** | **NOBODY, ANY STATE [N]** — defs deleted; `ACTIVE_OUTPUTD_PLAYBACK_DEVICE` / `_CAPTURE_DEVICE` constants survive in Python with zero production readers | not open **[M]** |
| **`hw:Loopback,0,7`** under `shm_ring` | **NOBODY [N]** — yet still printed by fan-in every boot *and asserted by a doctor check* (L-01) | not open **[M]** |
| `ctl.jasper_capture`, `ctl.outputd_content_capture` | **no `Ctl` is opened on the Loopback card anywhere in `rust/` [N]** | not open **[M]** |
| `usbsink_substream`, `jasper_renderer_in`, `jasper_renderer_mix`, `LoopbackAEC` | **names in Python/prose whose ALSA PCM no longer exists [N]** | n/a |
| `jts_ring_*`, `outputd_dac` | **not aloop** — listed to rule them out | open **[M]** |

**Dead in every box state:** pair 5, `hw:Loopback,0,3`, `pcm.jasper_ref`
(as a *device*), the Loopback `ctl.*` aliases, and four vestigial name
constants.

**Alive only in states this fleet does not occupy:** pairs 0/1/2/4
(unarmed lane, or an operator probe), pair 6 (`direct` bridge **or** a
bonded active endpoint), pair 7 (`loopback` coupling **or** a bonded
active leader's bake).

### 2.3 An opener class a file/symbol lens under-weights: operator probes

**Six shipped diagnostic sites open `correction_substream`
(`hw:Loopback,0,4`) unconditionally** — none consults
`renderer_lanes.env`, none has a ring branch:

| Site | Call |
|---|---|
| `scripts/aec-probe-pinknoise.sh` | `sudo aplay -D correction_substream …` |
| `scripts/aec-probe-latency.sh` | `subprocess.run(["aplay","-D","correction_substream", …])` |
| `scripts/aec-probe-xvf-ref-level.sh` | `["aplay","-D","correction_substream", …]` |
| `scripts/aec-probe-timing.py` | `run_cmd(["aplay","-q","-D","correction_substream", …])` |
| `scripts/s0-sync-bench.sh` | `hw:Loopback,{0,1},${ALOOP_SUB}` (default substream **0**) |
| `jasper/cli/doctor/__init__.py` | `--probe-aec` help text still says "play a brief sine into correction_substream" (the code itself correctly uses `correction_play_device()`) |

**Measured:** all five scripts are present on jts.local under
`/home/pi/jts/scripts/` (mtime 2026-08-17 13:54) and
`correction_substream` appears 5 times across the shell/Python probes.
**[M]**

**Consequence, measured.** The `correction` lane is ring-armed on this
fleet, so nothing reads `hw:Loopback,1,4`. An operator running any of
these probes today writes into a cable with no reader: `aplay` succeeds
and the audio goes nowhere. **These probes are already silently broken
on both granted boxes**, before any deletion. The product's own path is
fine — `correction_play_device()` resolves per spawn from
`read_armed_labels()` and returns `correction_ring_lane` — so this is
purely a diagnostics-vs-product drift. **[M/I]**

### 2.4 The doctor: both an opener and an aloop-asserting authority

Four checks matter to the deletion walk. All **[I]** — deliberately not
executed, because the probes open renderer devices and the fleet bar for
this task was "nothing that could interrupt audio."

| Check | What it does | Gate |
|---|---|---|
| `doctor/audio.check_loopback` | runs `aplay -L`, **`fail`** if `CARD=Loopback` is absent | **none — unconditional on both couplings** |
| `doctor/audio_runtime.check_fanin_asound_wiring` | text-matches the deployed `/etc/asound.conf`; **`fail`** if `pcm.jasper_ref` is *missing* or no longer plug-wraps `jasper_capture`, and if the four `*_substream` aliases or the `1,7` dsnoop drift | **none — unconditional** |
| `doctor/audio_runtime.check_fanin_service` | compares live fan-in STATUS `output.pcm` against `_FANIN_EXPECTED_OUTPUT_PCM = "hw:Loopback,0,7"`; mismatch → fail | **none** |
| `doctor/renderers.check_renderer_device_resolvable` | **OPENS**: `sudo -u <user> aplay -q -D <device> … /dev/zero`; disambiguates `EBUSY` via `/proc/asound/Loopback/pcm0p/sub<N>/status` using `_FANIN_PRIVATE_RENDERER_DEVICES` (`librespot_substream`→0, `shairport_substream`→1, `bluealsa_substream`→2) | probes the **configured** device — ring lanes on this fleet |

Two of these are deletion-coupled hazards worth naming now:
`check_fanin_asound_wiring` **fails when `pcm.jasper_ref` is removed**,
so P9-E must move the check and the conf together; and
`check_fanin_service` **asserts the aloop output name** that fan-in only
prints and never opens (L-01), so it is downstream of the misleading
field rather than a check on reality.

A fifth, `doctor/grouping.check_grouping_aloop_remnant`, walks 4 PCM
dirs × 8 substreams of `/proc/asound/Loopback` and fails on any open
substream outside a derived registered set `{0,1,2,3,4,6,7}`. On this
fleet it returns `ok` both ways: jts4's open sub 3 is registered, and
jts.local has nothing open. **[I]**

### 2.5 The bonded-leader bake — the one aloop capture with a structural lock

`jasper/multiroom/active_leader_config.py` `precheck_active_leader`
emits camilla#1's program bake via
`emit_active_speaker_program_bake_config(...)` **with no
`capture_device` argument**, so it falls to
`DEFAULT_CAPTURE_DEVICE = "plug:jasper_capture"` — aloop pair 7. Two
independent mechanisms keep it there: **[I]**

1. `jasper/audio_runtime_plan.py` `apply_capture_precedence` returns the
   caller's kwargs unchanged when
   `member_kwargs_are_pipe_sink(...)` is true (a bonded leader's sink is
   the SNAPFIFO pipe), so the coupling's ring kwargs never apply.
2. `jasper/sound/graph_carrier.py` `_ProgramBakeCarrier.reemit` does
   `del fanin_coupling_capture_kwargs` outright — the coupling is an
   explicit no-op on this carrier.

Same precheck emits camilla#2's crossover with an explicit
`capture_device=GROUPING_LOOPBACK_CAPTURE` (`hw:Loopback,1,6`). So a
bonded active leader would open **two different aloop pairs at once** —
7 for the bake's capture and 6 for the crossover's — while
`coupling_supported_for_route` independently forces the box onto
`loopback` coupling, which is what puts a writer on pair 7 in the first
place. Coherent, and entirely unexercised on this fleet. **[I]**

### 2.6 What a fully-default box would hold, for contrast

With no `renderer_lanes.env`, `USB_DIRECT` unset, and coupling unset
(every code default), jasper-fanin holds **six** aloop handles — five
captures (`hw:Loopback,1,0..4`) plus one **blocking playback**
(`hw:Loopback,0,7`, which paces its whole work loop) — and
jasper-outputd holds a seventh via `outputd_content_capture`. **[I]**

The granted fleet holds **one** (jts4) and **zero** (jts.local). That
gap is the whole story of this document.

---

## 3. FLEET-EMPIRICAL SNAPSHOT (read-only)

### 3.1 What is actually open

**jts.local** — 2026-08-18T12:54:06Z–13:04:17Z.

- **All 32 snd-aloop substreams `closed`.** **[M]**
- `fuser -v /dev/snd/*` over the **full** glob returns exactly four
  holders, **none on the Loopback card (card 6)**: **[M]**
  - `pcmC3D0p`, `pcmC4D0p` → `jasper-outputd` (two Apple dongles)
  - `pcmC5D0c`, `controlC5` → `jasper-fanin` (`hw:UAC2Gadget` capture)
- **snd-aloop is completely unused on this box** — two independent
  measurements agree (substream status + fd holders).

**jts4** — 2026-08-18T12:54:29Z–12:59:29Z.

- **Exactly one substream open:** `pcm1c/sub3`, `state: RUNNING`,
  `owner_pid 3829` = `jasper-fanin`; corroborated by `fuser`
  (`/dev/snd/pcmC6D1c: root 3829 F.... jasper-fanin`). **[M]**
- Only other `/dev/snd` holder: `jasper-outputd` on `pcmC1D0p`
  (InnoMaker HiFi AMP Pro). **[M]**
- That open is **`hw:Loopback,1,3`, the usbsink idle read fallback, with
  no writer** (`pcm0p/sub3` `closed`) — a capture reading silence
  forever. Cause: no UAC2 gadget on this box; fan-in logged
  `event=fanin.usb_direct.absent device=hw:UAC2Gadget errno=19`. **[M]**

### 3.2 The arming state that explains it

Both boxes: **[M]**

```
renderer_lanes.env:  JASPER_FANIN_RENDERER_RING_LANES=spotify,bluealsa,correction,airplay
                     JASPER_LIBRESPOT_DEVICE=librespot_ring_lane
                     JASPER_BLUEALSA_DEVICE=bluealsa_ring_lane
                     JASPER_CORRECTION_DEVICE=correction_ring_lane
                     JASPER_SHAIRPORT_DEVICE=shairport_ring_lane
fanin.env:           JASPER_FANIN_CAMILLA_COUPLING=shm_ring
outputd.env:         JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring
grouping.env:        JASPER_GROUPING=off
```

| | jts.local | jts4 |
|---|---|---|
| `JASPER_FANIN_USB_DIRECT` | `enabled` | `disabled` |
| `JASPER_FANIN_COUPLING_CHOICE` | `operator` | **`auto`** |
| `JASPER_OUTPUTD_SINK` | `dual_apple` | `single_alsa` |
| `JASPER_OUTPUTD_CONTENT_PCM` | `''` | **`outputd_content_capture`** (aloop alias — configured, never opened) |
| `JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT` | `1` | `''` |
| Ring file | `active-content.ring` (roleful) | `content.ring` (flat) |
| CamillaDSP capture → playback | `jts_ring_capture` → `jts_ring_active_playback`, 4 ch | `jts_ring_capture` → `jts_ring_playback`, 2 ch |
| `aec_mode.env` | `JASPER_AUDIO_INPUT_PROFILE=xvf_chip_aec` | file absent (no mic) |

**jts4's `COUPLING_CHOICE=auto` resolving to `shm_ring` is
load-bearing.** The fail-safe default in both Rust and Python is
`loopback`; the box nevertheless runs `shm_ring`, so the unattended
reconciler wrote it. Ring coupling on this fleet is not solely an
operator's explicit act. **[M]**

### 3.3 Corroborating runtime evidence

- **Renderers write rings, not aloop.** `output_device =
  "shairport_ring_lane"`; librespot `--device librespot_ring_lane`;
  bluealsa-aplay `--pcm=bluealsa_ring_lane`. Ring writer locks exist for
  `lane-airplay` (owner `shairport-sync`), `lane-bluealsa`, and
  `lane-correction` (owner `jasper-web`). **[M]**
- **`lane-spotify.ring` has no `.writer.lock` on either box** — librespot
  opens its lane lazily, on first playback. Absence of that lock is
  **not** evidence of an unarmed lane. **[M]**
- **fan-in startup block** (both boxes): four lanes `source=ring
  pcm=/dev/shm/jts-ring/lane-*.ring`, then `event=fanin.ring.opened
  path=/dev/shm/jts-ring/program.ring`. **[M]**
- **`jasper-usbsink.service` is process-free:** `Type=oneshot`,
  `RemainAfterExit=yes`, `MainPID=0`, `ExecStart=/bin/true`, no process
  in `ps`, and `PrivateDevices=true` would block ALSA anyway. **[M]**
- **snapclient / snapserver `inactive` + `disabled`** on both boxes. **[M]**
- **jasper-aec-bridge** `inactive`/`disabled` (jts.local),
  `not-found` (jts4); `ExecStart` carries no device argument and its
  reference is outputd's UDP monitor. **[M/I]**

### 3.4 The one true discriminator in the logs

`event=fanin.input.opened` carries **both** `pcm=` and `source=`, and
only `source=` tells the truth:

| `source=` | Real source | Is the printed `pcm=` device opened? |
|---|---|---|
| `ring` | SHM lane ring | n/a — `pcm=` shows the ring path, consistent |
| `lane` | the aloop device | **YES** |
| `direct` | `hw:UAC2Gadget` | **NO** — `pcm=` still prints `hw:Loopback,1,3` |

Measured proof: jts.local logs `label=usbsink pcm=hw:Loopback,1,3
direct=true source=direct` while holding **zero** fds on the Loopback
card. Emit site `rust/jasper-fanin/src/mixer.rs`; the same parity is
pinned in the `/state` fixture in `rust/jasper-fanin/src/state.rs`,
whose own comment says the audio comes from `hw:UAC2Gadget` and the
aloop name is kept "for parity with the label." Ledger **L-02**.

---

## 4. CONFUSIONS LEDGER

**17 entries: 12 corrections/ambiguities (L-01…L-12) and 5
confirmations (C-01…C-05).** Claims that fail to reproduce are recorded,
not silently corrected. Confirmations are included so the ledger
separates "checked and wrong" from "checked and right."

### The shape of the confusion, stated once

Three independent layers each **name** an aloop device that is **not
opened** on this fleet: a startup log (L-01), a per-lane log + `/state`
field (L-02), and a persisted env file (L-03). All three are *runtime*
artifacts, which normally outrank prose — so an investigator who
correctly prefers measurement over documentation is misled by all
three. A fourth layer, the doctor, then *asserts* one of them (§2.4).
That, more than any single stale sentence, is why "is aloop still live?"
keeps re-opening.

---

### L-01 — fan-in's startup log names an aloop output it never opens — and a doctor check asserts it

**Claim (a log line).** Both ring-coupled boxes, every start:

> `event=fanin.config_loaded inputs=5 output=hw:Loopback,0,7 sample_rate=48000 …`

**Where.** `rust/jasper-fanin/src/main.rs`, the
`event=fanin.config_loaded … output={}` format string.

**Measured truth.** Under `shm_ring` fan-in writes
`/dev/shm/jts-ring/program.ring` and **never opens `hw:Loopback,0,7`**.
`pcm0p/sub7` is `closed` on both boxes; fan-in holds no Loopback fd at
all on jts.local. The field prints `config.output_pcm`, which under
`shm_ring` is parsed and echoed but never used to open anything. **[M]**

**The aggravating factor.** `doctor/audio_runtime.check_fanin_service`
compares live fan-in STATUS `output.pcm` against
`_FANIN_EXPECTED_OUTPUT_PCM = "hw:Loopback,0,7"` and fails on mismatch —
so the health check is downstream of the misleading field rather than a
check on reality. **[I]**

### L-02 — `fanin.input.opened pcm=` and the `/state` field print a device that was not opened

**Claim.** jts.local: `event=fanin.input.opened label=usbsink
pcm=hw:Loopback,1,3 … direct=true source=direct`.

**Where.** `rust/jasper-fanin/src/mixer.rs`; `/state` parity pinned in
`rust/jasper-fanin/src/state.rs`.

**Measured truth.** That process holds fds on `pcmC5D0c` and nothing on
card 6; all 32 substreams `closed`. On jts4 the same line reads
`source=lane` and the device genuinely **is** open. **[M]**

**Why it matters.** Two boxes emit near-identical lines for opposite
realities, separated only by a trailing field, and `/state` reproduces
the trap for anyone reading the API instead of the journal.

### L-03 — `outputd.env` persists an aloop alias on a ring-coupled box

**Claim.** jts4's `/var/lib/jasper/outputd.env`:
`JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture`.

**Measured truth.** The same file carries
`JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`, and outputd's
`content_pcm_skipped` path (`rust/jasper-outputd/src/alsa_backend.rs`)
substitutes a synthetic negotiation and opens **no** ALSA handle for
content under `ShmRing`. Measured: `pcm1c/sub6` `closed`; outputd holds
only `pcmC1D0p`. **[M]**

**Why it matters.** The instance most likely to mislead a *config audit*
rather than a log audit: the box's own persisted state names the old
lane.

**Adjacent finding (recorded, not a separate entry).** In
`deploy/systemd/jasper-outputd.service`,
`EnvironmentFile=/etc/jasper/jasper.env` loads **before** the
`Environment="JASPER_OUTPUTD_CONTENT_PCM=…"` block, so an operator
setting that key in `jasper.env` is silently overridden by the packaged
default — the opposite ordering from librespot/bluealsa/fan-in, which
all put their override file last. Known and compensated for in
`jasper-apply-airplay-mode`, but real.

### L-04 — "the fleet default is unarmed" — six sites; true of code, false of both granted boxes

**Claim.** Seven load-bearing sites across four documents. The two most
consequential:

- `captures/LOOPBACK-CENSUS-2026-08-17.md` §2.3 heading and body:
  > "AXIS-2 — renderer ingress (fleet default unarmed)" … "**FLEET
  > DEFAULT = UNARMED, established six ways** (each verified)"
- `docs/audio-paths.md`, "The two paths" and again in "Operational
  notes":
  > "Arming is per box and operator-explicit; the fleet default is
  > unarmed, and an unarmed box runs the aloop path above."

Also: census §6.2 ("Today the armed set is empty because *nothing
writes* `/var/lib/jasper/renderer_lanes.env` at install or boot"),
census §6.3, the sealed design §3.2 N6, `PLAN-loopback-retirement`'s
session-open entry, and the next-session prompt's axis-2 description.

**Measured truth.** Both granted boxes run **all four** renderer lanes
armed and `shm_ring` coupling on both fan-in and outputd. Zero aloop
renderer hops are live. jts4 reached `shm_ring` from
`COUPLING_CHOICE=auto`. **[M]**

**Precisely what is wrong — and what is right.** The *code-default*
reading is **TRUE and now independently confirmed**:
`JASPER_FANIN_RENDERER_RING_LANES` has no `Environment=` default,
`env_csv_labels` defaults to empty, and the census's claim that nothing
writes the file at install is correct — the only writer is
`jasper-audio-config renderer-lanes`. The *deployment* reading — that
boxes in the field run unarmed — is **false for 2 of 2 granted boxes**.

**Fairness note, which the ledger owes the census.** Census §1.4
disclaims runtime truth up front — "Nothing was deployed, run, or
probed. Every claim is a claim about source at this SHA" — and §6.2
explicitly lists **"Whether any fleet box currently has a lane armed"**
as one of two UNKNOWNs gating the decision, naming
`cat /var/lib/jasper/renderer_lanes.env` as the settling evidence. The
census did not claim to have measured this; it asked for exactly the
measurement this document supplies. The defect is confined to the
*phrasing* — a section heading and a bolded "established six ways (each
verified)" that read as fleet fact — not to the census's method. The
census even quotes shipped code anticipating the answer:
`_fanin_lane_busy_owner_matches`'s "Fleet arming state is not that
trigger."

**Scope honesty.** 2 of 2 *granted* boxes. jts3 and jts5 were not
probed, so this is not a whole-fleet claim — but it falsifies the
sentence as a description of how boxes actually run.

### L-05 — the sealed design carries an `[M]` tag on an unmeasured claim

**Claim.** `captures/DESIGN-PROPOSAL-grouping-ring-2026-08-17.md` §3.2,
note N6:

> "`61-jts-renderer-lanes.conf:8-14` declares itself **INERT UNTIL
> ARMED** and **the fleet default is unarmed** `[M]`, so 256×16 is
> **shipped and legal, not exercised**"

**Measured truth.** The 256×16 renderer-lane geometry **is** exercised —
eight ring-lane PCMs across two boxes, with fan-in logging
`slot_frames=256 n_slots=16` on every attach. **[M]**

**Why this is its own entry rather than part of L-04.** The `[M]`
annotation asserts *measured*, on a claim the census had explicitly
flagged as unmeasured one document earlier. This is an honesty-bar
failure independent of whether the underlying claim is right: a
downstream reader has no way to know the tag was inherited from prose
rather than from a probe. It is also load-bearing — "not exercised" is
the justification for treating the fallback geometry as untested.

### L-06 — "armed" means two different things, and both boxes are armed in both senses

**Claim.** The word is used for two subjects and never disambiguated in
a single sentence:
- **coupling**: `NEXT-SESSION-PROMPT-2026-08-18` — "jts.local … **armed**
  composite roleful box"; "jts4 … **ring-armed**, `choice=auto`".
- **renderer lanes**: census §2.3 — "FLEET DEFAULT = **UNARMED**".

**Measured truth.** Both boxes are armed in **both** senses
simultaneously — `CAMILLA_COUPLING=shm_ring` *and* all four renderer
lanes in `RENDERER_RING_LANES`. **No document states this.** **[M]**

**Why it matters.** A reader who sees "jts4 is ring-armed" in the prompt
and "fleet default unarmed" in the census can hold both without noticing
a conflict, because the words silently refer to different axes. This
collision is a plausible mechanism for the campaign's recurring
confusion, independent of L-04's factual error.

### L-07 — `HANDOFF-fan-in-daemon.md`'s TL;DR describes the aloop path as live and primary, with no caveat

**Claim.** § "TL;DR":

> "Each renderer gets its own snd-aloop substream (`hw:Loopback,0,0..3`),
> and correction/test playback gets `correction_substream`
> (`hw:Loopback,0,4`) … writes to a single dedicated 'summed music'
> substream (`hw:Loopback,0,7`). CamillaDSP dsnoops on the capture side
> … (`hw:Loopback,1,7`)"

**Measured truth.** Every named device in that sentence is `closed` on
both boxes. **[M]**

**The file's own correct text, ~150 lines later.** § "Lane sources"
scopes it exactly right — "`lane` | snd-aloop capture substream | the
default; **every lane on every unarmed box**" — and the topology block's
summed-substream note is the single most accurate pair-7 statement in
the repo ("On a shm_ring box it has neither a writer nor a reader"). The
correction was simply never retrofitted into the TL;DR.

**Two internal inconsistencies in the same file.** (a) The topology
ASCII brackets the *write* arrow with `[coupling=loopback]` /
`[coupling=shm_ring]` but leaves the **read** arrow
(`hw:Loopback,1,0..4`) unbracketed. (b) It lists
"jasper-usbsink → hw:Loopback,0,3" under "Renderers (each on its own
snd-aloop substream pair)", which the same file contradicts three
sections later ("`jasper-usbsink.service` is a process-free readiness
marker").

### L-08 — `HANDOFF-multiroom.md` names lane 7 for the bonded leader, and asserts all 8 pairs are allocated

**Claim.** § "Increment 5 → Design note", duplicated verbatim in the
revision log:

> "the leader's camilla keeps capturing lane 7 (`jasper_capture`) — all
> 8 loopback substreams are allocated, and PR-2's TTS socket flip makes
> lane 7 music-only BY CONSTRUCTION while bonded"

**Truth, on three counts.** (1) **Right for the wrong reason, and only
for the active leader.** The *active* leader's camilla#1 bake really
does default to `plug:jasper_capture` (§2.5) — so the device is
correct — but the doc states it as a property of "the leader" generally,
and a *passive* bonded leader reaches the same device by a different
route (`member_camilla_kwargs` sets no capture device). **[I]**
(2) **"all 8 loopback substreams are allocated" is false** — pair 5 is
UNALLOCATED since P9-C, as this same file's revision log records.
(3) The "BY CONSTRUCTION" safety argument rests on a lane that is
`closed` on both measured boxes. **[M]**

**Related, same file.** § 7.5 describes the active follower as
"snapclient → **loopback** → CamillaDSP", naming an unnumbered loopback
hop that conflicts with the file's own inv-2 rule ("raw PCM, **NOT**
snd-aloop"); and § "Where it taps the existing pipeline" states in the
present tense that "The JTS output chain **today** is single-Pi:
renderers → `snd-aloop` fan-in → CamillaDSP → `jasper-outputd` → DAC".
Both are stale against the measured chain. **[M]**

### L-09 — AGENTS.md's single renderer-ingress sentence is unconditional and wrong for this fleet

**Claim.** § "Renderer architecture — file map":

> "All music/content sources enter the fan-in topology through a private
> snd-aloop lane."

**Measured truth.** On jts.local **zero** sources enter over an aloop
lane (four over SHM rings, one over direct UAC2 capture). On jts4, one
of five does, and only as an idle fallback. **[M]**

**Why it matters more than its length suggests.** This is the *only*
sentence in AGENTS.md describing renderer ingress, and AGENTS.md is the
file every fresh agent session inherits. The file never names the ring
transport or `JASPER_FANIN_RENDERER_RING_LANES` anywhere. Its
neighbouring bullet on `${JASPER_<RENDERER>_DEVICE}` indirection is
accurate and does mention `renderer_lanes.env` — so the correction is
already half-present in the same document.

### L-10 — `HANDOFF-speaker-output-reference.md` caveats the output half and not the ingress half, under "Current Operational Truth"

**Claim.** § "Current Operational Truth", ASCII block:

> "AirPlay / Spotify / Bluetooth / USB / correction -> private snd-aloop
> lanes -> jasper-fanin -> pcm.jasper_capture -> jasper-camilla"

**Measured truth.** Ring ingress and `jts_ring_capture` on both boxes.
**[M]**

**Why this one is the worst of the three unconditional chains** (with
L-08's and L-09's): the *output* side of the very same code block **is**
coupling-caveated ("Ring B (low-latency route), or a paired ALSA
playback/capture lane"), so the block demonstrates that its author knew
the distinction and applied it to one half only — under a heading that
claims current operational truth.

**Same file, § "Rollout Plan" step 4:** "nothing opens `pcm.jasper_ref`
at all; the underlying `pcm.jasper_capture` tap **survives for
CamillaDSP alone**." The `jasper_ref` half is right (C-01); the
`jasper_capture` half is phrased as a *verified survival* claim, which
makes it the strongest surviving assertion that pair 7 has a live
reader. It is closed on both boxes.

### L-11 — `docs/audio-paths.md`'s allocation list still calls pair 3 "USB sink", and its renderer rule reads as the present default

**Claim.** § "Adding a new music source", items 1 and 3:

> "Current allocation … `3` USB sink, `4` correction/test, `5` UNALLOCATED"

> "Its systemd unit should write to the alias, not to `jasper_capture`,
> `outputd_content_*`, or raw `hw:Loopback,*` names."

**Measured truth.** Pair 3 has had **no writer since 2026-07-10** and no
write alias; both allocation owners say so explicitly. And every
migrated renderer on this fleet writes a `*_ring_lane` device. **[M/I]**

**Why it matters.** This is the list and the rule a contributor reads
when adding a source. Item 1 is the one place in the tree where pair 3
reads as occupied-by-a-source; item 3 is not wrong (the aloop alias is
still the in-unit default) but a reader following it literally never
learns the ring lane is what the fleet runs.

### L-12 — several campaign conclusions are premise-narrowed by this measurement, but NOT falsified

**Claims.** The family of statements built on "AXIS-2 and AXIS-3
survive Phase 2":

- census §6.3: "the campaign's phase set is sufficient to delete the
  loopback COUPLING, and insufficient to unload the module."
- census §5(a): "If AXIS-1 dies but AXIS-2 lives: the module stays,
  `pcm_substreams` stays ≥5."
- census §5(c): `check_loopback` "survives Phase 2 unchanged and
  correctly (AXIS-2/AXIS-3 still need the card)."
- design §9 / plan digest #7: "'snd-aloop no longer loads on any box' is
  **UNREACHABLE** by Phase 1+2 … eleven STAYS consumers."

**Assessment — deliberately not "contradicted."** These are claims about
**code**, and as code claims they hold: the AXIS-2/AXIS-3 consumers do
survive Phase 2, the module config is still installed unconditionally by
`install.sh` (`rmmod snd_aloop; modprobe snd-aloop` on every deploy),
and a disarmed lane or a bonded box would need the card immediately.
Nothing measured refutes any of that.

**What the measurement does change is the *calibration*.** Those
surviving consumers are **dormant on this fleet**: on jts.local not one
of them holds an fd, and on jts4 exactly one does. So "the module is
still needed" is true as a statement about reachable code paths and
misleading as a statement about current utilization.

**One sub-claim is genuinely weakened.** census §5(c)'s "and correctly"
— `check_loopback` returns **`fail`** unconditionally when
`CARD=Loopback` is absent, on both couplings. On jts.local that check
would fail the box over a missing card that **nothing on it uses**. The
check survives Phase 2; whether it survives *correctly* is exactly the
kind of question this measurement reopens.

---

### C-01 — `pcm.jasper_ref` is reader-less — **reproduces**

`docs/audio-paths.md`, `asoundrc.jasper`'s header, the census §2.4/§5(b),
and `HANDOFF-fan-in-daemon.md` all agree it has no reader. **Confirmed:**
no Loopback fd exists on either box and `hw:Loopback,1,7` is `closed`.
**[M]** Independently confirmed in code: the exhaustive Python sweep
found **no** code that opens it or names it as a CamillaDSP capture
device — its only remaining consumer is
`check_fanin_asound_wiring`, which asserts its *presence as text*. **[I]**

### C-02 — pair 7 is writer-less and reader-less under `shm_ring` — **reproduces**

`asoundrc.jasper` and `snd-aloop.conf` both say so. **Confirmed:**
`pcm0p/sub7` and `pcm1c/sub7` `closed` on both boxes. **[M]**

**Worth flagging:** this is the inverse of the usual failure direction —
here the **prose is right and the log line is wrong** (L-01). An agent
applying the normal heuristic "trust runtime over documentation" gets
this one backwards.

### C-03 — `jasper-usbsink.service` is a process-free readiness marker — **reproduces**

`Type=oneshot`, `RemainAfterExit=yes`, `MainPID=0`,
`ExecStart=/bin/true`, `PrivateDevices=true`, no process in `ps`.
Consistently and correctly described in AGENTS.md,
`HANDOFF-usbsink.md`, and `HANDOFF-fan-in-daemon.md`. **[M]**

### C-04 — the AEC bridge no longer reads any aloop device — **reproduces**

`ExecStart` carries no device argument; the reconciler publishes
`ref_source="outputd_udp"` as its single value; the Python sweep found
only retirement comments. Inactive on both boxes. **[M/I]**

### C-05 — pair 3's behaviour was predicted exactly, twice — **reproduces**

`HANDOFF-usbsink.md` ("When USB Audio Input is off, fan-in opens
`hw:Loopback,1,3` as that lane's idle fallback (nobody writes it)"),
`HANDOFF-fan-in-daemon.md`'s asoundrc note, and census §6.1/Q9 ("a
capture with no writer, kept because a missing required input is
**fatal** to fan-in") all predict jts4's single open substream
precisely. **[M]**

**One refinement.** The docs state the trigger as "USB Audio Input is
off." The measured trigger is `JASPER_FANIN_USB_DIRECT=disabled`, and on
jts4 the underlying cause is that no UAC2 gadget exists at all
(`errno=19`). Same outcome, but the doc's phrasing suggests a user
setting where the fleet's cause is hardware absence.

---

## 5. IMPLICATIONS (observations only)

**For Phase 2's deletion walk.**

1. **Five surfaces are dead in every box state**, not merely on this
   fleet: pair 5, `hw:Loopback,0,3`, `pcm.jasper_ref` as a device, the
   Loopback `ctl.*` aliases, and four vestigial name constants
   (`usbsink_substream`, `jasper_renderer_in`, `jasper_renderer_mix`,
   `outputd_active_content_*`). No configuration reaches them.

2. **Exactly one aloop device is open anywhere on the granted fleet:**
   `hw:Loopback,1,3` on jts4, with no writer. jts.local — the more
   complex box, dual-DAC and roleful — opens **nothing**. Removing pairs
   0, 1, 2, 4, 5, 6, or 7 would not change a single fd on either box
   today.

3. **Two deletion-coupled doctor hazards, both unconditional.**
   `check_fanin_asound_wiring` **fails when `pcm.jasper_ref` is
   removed**, so P9-E must move the check and the conf in one change;
   and `check_loopback` **fails when the card is absent on any box**,
   including one where nothing opens it. Neither is gated by coupling or
   arming.

4. **The observability surface will outlive the graph.** L-01, L-02, and
   L-03 mean that after the devices stop being opened, fan-in's log,
   fan-in's `/state`, and outputd's env file will keep naming them — and
   `check_fanin_service` will keep asserting the name. Any "is it gone?"
   verification that greps the journal, reads `/state`, or audits
   `outputd.env` will produce false positives.

5. **Two opener classes live outside the daemon set** and a
   daemon-scoped walk would miss both: six operator diagnostic sites
   that hardcode `correction_substream` (§2.3) — already silently broken
   on the armed fleet — and the doctor's install-time `aplay` probe
   (§2.4).

6. **Pair 3 is the one needing care, and it is the least "graph-like."**
   It is not a hop between components; it is a placeholder fan-in opens
   so a configured input always exists when USB direct is not the live
   source, because a missing required input is fatal to fan-in. The two
   boxes differ for a *hardware* reason, not a policy one. Anything
   touching pair 3 changes fan-in's behaviour on a gadget-less box — the
   jts4 and Zero-class shape.

**For the pending owner decision on axes 2 and 3.**

7. **The fleet has already taken both axes.** All four renderer-ingress
   lanes — including `correction` — are armed on both granted boxes, and
   the correction lane's live writer lock (owner `jasper-web`) shows the
   product's own measurement spawn used the ring path. The question is
   not "should we move the fleet onto the ring" but "should we keep an
   aloop fallback the fleet has already stopped using." **[M]**

8. **Risk profile, stated precisely.** On these two boxes, retiring axes
   2/3 removes paths with **zero** current openers. Residual risk lives
   entirely in states the granted fleet does not occupy: an unarmed box,
   a `loopback`-coupled box, a `direct` content-bridge box, and a bonded
   active endpoint. Whether such a box exists is **UNKNOWN** from here —
   jts3 and jts5 were not probed, and two boxes are not a fleet.

9. **The premise correction is a narrowing, not a reversal.** Arguments
   reasoning from "the fleet default is unarmed" (L-04) are calibrated to
   a state 2 of 2 granted boxes do not occupy, so they will
   *overestimate* the blast radius of deleting the renderer-ingress and
   correction hops and *underestimate* how much of the surface is
   already unreachable. But the claim is true of code defaults and false
   only of deployed boxes — the fix is to say which is meant (L-06's
   vocabulary collision is the same disease), not to invert the
   conclusion. The census asked for exactly this measurement in its own
   UNKNOWN list.

10. **`auto` resolves to the ring.** jts4 reaching `shm_ring` from
    `COUPLING_CHOICE=auto`, against a fail-safe default of `loopback` in
    both Rust and Python, means new eligible boxes converge onto the ring
    without operator action. The unarmed/`loopback` population is
    shrinking by construction. **[M]**

11. **The finding most likely to change the campaign's shape: the aloop
    coupling and multiroom are currently welded together, in both
    directions.** Verified by direct read, not inherited:
    `jasper/audio_runtime_plan.py` `coupling_supported_for_route` refuses
    `shm_ring` for `_GROUPING_ENABLED_ROUTE_MODES = frozenset({"active_leader",
    "active_follower", "invalid_grouping"})`, and its refusal string names the
    mechanism outright — arming the ring on a bonded box "would strand the
    leader's local output (outputd reads Ring B while **camilla#1 still bakes
    the aloop/loopback grouped program**)", with the remediation "Disarm the
    ring … or ungroup this speaker; keeping the coupling on loopback."
    That matches §2.5 exactly: the bake takes no `capture_device`
    argument and is pinned to `plug:jasper_capture` by two independent
    mechanisms, so a bonded active leader would open **two** aloop pairs
    at once (7 for camilla#1's capture, 6 for camilla#2's crossover).
    **[I]**

    The docstring also names the converse — "the symmetric half of the
    multiroom reconciler's **'ring-armed box cannot bond'** gate" — which
    has a live consequence: **both granted boxes, being ring-armed,
    currently cannot enter a bond at all** without first being disarmed
    back to `loopback`. **[I, from code; not exercised — neither box was
    asked to bond.]**

    So the loopback coupling is not dormant legacy awaiting deletion. At
    this SHA it is the **required transport for any bonded box**, and it
    is the one major aloop consumer this fleet structurally cannot
    exercise. Any deletion sequencing that treats AXIS-1 as "the dead
    one" should reconcile with this first.

---

*Prepared 2026-08-18. Code pinned at
`200d54578cbf63373d1cc8849e1e7205334da41e`; fleet measured on jts.local
and jts4, both running installed build `620065dff`. jts3 and jts5 not
touched. Every unqualified statement in §2–§3 carries [M], [I], [N], or
UNKNOWN.*
