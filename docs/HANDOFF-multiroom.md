# Handoff: multi-room / multi-speaker audio (stereo pair, 2.1, wireless sub)

> **Status: proposed design — not yet implemented.** This is the
> canonical design home for grouped/synchronized playback across
> multiple JTS speakers (stereo pairs, 2.1 with a wireless sub,
> and multi-room). No code exists yet; the numbers that gate the
> whole feature (network sync error, FLAC RAM/CPU) are unmeasured.
> The first deliverable is a throwaway **P0 measurement spike**, not
> product code. Treat sections below as *intended* operational
> truth, to be promoted to live HANDOFF prose as each phase ships.
> The existing `jasper/peering/` subsystem
> ([HANDOFF-peering.md](HANDOFF-peering.md)) is **wake arbitration
> only** — picking which speaker *answers* "Hey Jarvis" — and is a
> different subsystem from this one, though this design reuses its
> discovery/identity substrate.
>
> Design dialogue + prior-art research: 2026-06-04.

---

## 0. Implementation status (2026-06-04)

Off-by-default plumbing has landed; **no audio crosses the network
yet** and the gating spike has not been run. What exists:

- **`jasper/multiroom/config.py`** — pure, off-by-default
  `GroupingConfig` + `load_config()` (SSOT `/var/lib/jasper/grouping.env`;
  fail-safe to off, fail-loud `error` when on-but-invalid). Fields:
  `enabled, role, channel, bond_id, leader_addr, buffer_ms, codec, error`.
- **`jasper/multiroom/reconcile.py`** — pure `plan(cfg)` (config →
  desired snapserver/snapclient unit states; invalid → start nothing)
  + pure `snapserver_argv`/`snapclient_argv`; a thin `main()`
  entrypoint does the only systemctl I/O (`--reason` logged, validated
  on hardware, not in pytest).
- **`jasper/multiroom/state.py`** — `read_grouping_state()`, fresh-read
  (never `os.environ`); wired into `jasper-control` `/state.grouping`
  (fail-soft).
- **systemd units** (`deploy/systemd/jasper-{snapserver,snapclient,
  grouping-reconcile}.service`) — disabled by default, in
  `jts-audio.slice` (`MemorySwapMax=0` inherited), no CPU caps,
  anti-storm `Restart`/`StartLimit`.
- **`deploy/install.sh`** — `migrate_grouping` (seed/strip env) + unit
  install (not enabled) + `--dry-run` line.
- **`jasper-doctor`** — `check_grouping` (ok off / ok on-valid / warn
  on-invalid).
- **spike harness** — `scripts/multiroom-spike.sh` +
  `multiroom-spike-measure.py` (§8 P0; run on hardware).

Not yet built (P1+, post-spike): the `BondedSet` entity, channel-split
+ leader-side LFE crossover, satellite calibration, the `/rooms/`
wizard, the `jasper-outputd` snapfifo reference consumer, and live
validation of the snapcast process lifecycle.

---

## 1. What we're building

A household runs several JTS speakers. We want them to play music
together, in sync, in useful arrangements:

- **Stereo pair** — two speakers, one left / one right, one room.
- **2.1** — a stereo pair plus a **wireless subwoofer**.
- **Multi-room** — several of the above, in different rooms.

A speaker comes in two tiers:

- **Brainy speaker** — the existing JTS unit (Raspberry Pi 5 +
  CamillaDSP + the full stack). Runs the assistant, holds the
  source connections, does DSP/room-correction.
- **Dumb endpoint** — a cheap **Raspberry Pi Zero 2 W + I2S DAC
  HAT** running nothing but a synchronized audio client. No
  CamillaDSP, no voice, no renderers. Exists because a second Pi 5
  is too expensive to be a right-channel speaker, and because a
  wireless sub has to be cheap.

**The non-negotiable UX rule: a room is one logical speaker to the
outside world.** To an iPhone/Mac, a 2.1 living room is a *single*
AirPlay target, a *single* Spotify Connect device, a *single*
(future) Bluetooth pairing. All channel splitting — left/right,
crossover to the sub — happens behind the scenes. The sender never
sees the followers.

### V1 scope (locked 2026-06-04)

V1 ships **all three** topologies above, with the dumb endpoint
supporting **both** roles:

- **wireless sub** (LFE channel) — leads, because it's trivial; and
- **full-range satellite** (e.g. a standalone right channel) — same
  V1, one extra work item (per-channel correction, §4).

Deferred past V1: transient "play these rooms together right now"
ad-hoc groups, automatic leader failover/election, and ESP32/Pico
endpoints. See §8.

---

## 2. The core decision: buy the sync engine

**Decision: adopt [Snapcast](https://github.com/badaix/snapcast) as
the clock / transport / dejitter engine. Do not build our own
network audio sync.**

Keeping N speakers playing in sample-lock across consumer WiFi is
the single hardest part of this feature — independent sound-card
crystals drift (ppm), WiFi injects 50–200 ms jitter spikes, and
clock domains hop on roaming. Snapcast already solves it with a
timestamp + latency-buffer model: a software clock-offset estimate
per client over the same unicast TCP connection, sample-stuffing as
the rate-tracker, and a fixed playout buffer (~300–500 ms target).

**WiFi is a hard requirement; Ethernet is never required** — no
consumer smart speaker requires it, so neither do we. Snapcast clients
are designed to run over WiFi, and **buffer depth is the
jitter-absorption lever**: a deeper buffer tolerates more WiFi jitter
at the cost of more latency-to-glass (fine for music). The open
question is not "does WiFi work" but "what buffer size + codec hold
L/R sync on this household's WiFi" — that is what the §8 spike
measures.

This mirrors what the mature open-source players landed on. Both
Music Assistant and (effectively) Home Assistant draw a hard line:
**grouping/control is the platform's job; audio sync is the engine's
job.** JTS adopts the same boundary — we own discovery, grouping,
and the control plane; Snapcast owns the bytes-in-sync problem.

**Pro:** we skip the most bug-prone problem in the space and inherit
years of hardening.
**Con:** a third-party dependency in the audio path, and Snapcast is
designed around a *central server* — which is in tension with JTS's
no-single-point-of-failure philosophy. We resolve that with the
fixed-leader model (§3), accepting bounded, *visible* degradation
instead of seamless failover.

> **Note — do not borrow the wrong precedent.** An early draft
> justified Snapcast's unicast TCP by citing "JTS's own lesson that
> consumer-WiFi multicast is lossy." That citation is wrong:
> `jasper/peering/transport.py` *is* multicast and works fine,
> because wake-peering is designed best-effort-lossy. The honest
> justification for unicast TCP is just that it's Snapcast's proven
> design (per-client retransmit) — not a JTS precedent. Lossy
> multicast is fine for gossip; it says nothing about whether 20 ms
> audio chunks survive the link.

### Where it taps the existing pipeline

The JTS output chain today is single-Pi: renderers → `snd-aloop`
fan-in → CamillaDSP → `jasper-outputd` → DAC → amp → speakers
(see [audio-paths.md](audio-paths.md),
[HANDOFF-fan-in-daemon.md](HANDOFF-fan-in-daemon.md)).

The leader streams to followers from a **new reference consumer on
`jasper-outputd`** — the `ReferenceFanout` already copies
post-mix / post-CamillaDSP / post-TTS / **post-safety-clamp**
samples to bounded lossy per-consumer ring queues. We add one more
consumer that writes 48k/S16/stereo into a bounded non-blocking
FIFO (`/run/jasper/snapfifo`); `snapserver` reads it as a `pipe`
input. Tapping *after* the clamp is what makes the streamed audio
inherit JTS's hardware-safety ceiling (§7).

**Five timing invariants (load-bearing):**

1. `jasper-outputd`'s DAC write loop stays the **sole timing
   owner**; the snapfifo consumer is a bounded lossy side-reader
   that never back-pressures it.
2. The leader runs its *own* `snapclient` against `127.0.0.1`,
   playing to a real outputd content lane — **never a Loopback
   PCM** (dodges the documented `snd_pcm_delay`-lies-on-snd-aloop
   trap).
3. Voice / wake / TTS stay **entirely off** the Snapcast path
   (§6).
4. AEC taps `pcm.jasper_ref` (a *separate* reference consumer) —
   never shares a sender with the snapfifo consumer.
5. **Exactly one rate-adjuster per chain.** snapclient's
   sample-stuffing is the rate-tracker, so each member's local
   CamillaDSP runs `rate_adjust=false` / no resampler. Enforced in
   the config generator, checked by `jasper-doctor`. (JTS already
   documented that `rate_adjust` + `AsyncSinc` together oscillate.)

---

## 3. Identity, grouping, and the leader

**Decision: one fixed, config-declared leader per room. No
election, no automatic failover in V1.**

Each room has one **leader** (a brainy speaker). The leader is the
only unit that advertises to senders (AirPlay/Spotify/BT), receives
the source audio, runs it through its pipeline, and fans it out to
followers via Snapcast — playing its own share on the same buffered
clock so everything is aligned. This is what makes a 2.1 room look
like one speaker (§1).

**Entity model (minimal):**

- **`Speaker`** = the existing peering `peer_id` (stable UUID4,
  reused verbatim) + name, local correction profile, calibrated
  latency, channel capability.
- **`BondedSet`** (persistent, e.g. `stereo_pair` / `2.1`):
  `{members: [{speaker_id, role}], leader_id}` where `role ∈
  {L, R, sub, mono, ...}` and **`leader_id` is declared in config,
  not elected.** Survives reboots; addressable as one room / one
  volume. This is Sonos's load-bearing *pairing ≠ grouping*
  insight, and the thing Music Assistant most got wrong — worth
  building as a real single entity now.

**Reuse vs. extend the peering substrate:** discovery and identity
ride the existing `jasper/peering/` machinery — `peer_id`, the
`room` label (widened to a grouping key), the `primary` flag
(reinterpreted as the fixed-leader hint), and the Avahi
advertisement. We add three TXT records — `bond_id`, `role`,
`leader` — so a returning member finds its declared leader. **No
new multicast message family** is needed with a fixed leader.

**Control between Pis:** a minimal, localhost-only Snapcast
JSON-RPC adapter in `jasper-control` aligns the live group
(`Client.SetLatency` for per-speaker path-delay, `Client.SetVolume`).
Cross-Pi commands (volume) use the existing `jasper-control` HTTP
API on `:8780` (already binds `0.0.0.0`). We do **not** build the
group-membership RPCs — membership is static config in V1.

**Why fixed, not elected:** auto-election across partition-prone
consumer mesh is exactly the hand-rolled distributed-consensus glue
(split-brain, no fencing token, no term/epoch) that buying Snapcast
was meant to avoid. V1 handles leader death by loud, observable
degradation (§7), not election.

**Pro:** dramatically simpler and more predictable.
**Con:** if the leader loses power the room stops until it returns
(auto-recovers on reboot). Bounded, visible degradation — revisit
election only if it actually bothers a household with ≥3 rooms.

---

## 4. Channel splitting & per-speaker correction

**Decision: split channels and apply correction as late as
possible, co-located with the physical speaker that plays them.**

- **Stereo L/R across two brainy speakers:** the leader streams
  plain stereo; each member's local CamillaDSP selects its own
  channel post-snapclient and runs its own measure→PEQ→correction
  loop for its own seat. Channel role stays co-located with
  correction (`output_topology.py`'s
  `SpeakerChannel`/`physical_output_index`).
  **Gotcha:** `_emit_pipeline` today duplicates one mono PEQ onto
  both channels; a split pair must generate its *own* per-side
  config — do not centralize one correction config across both
  halves. A `target_channels` param on `emit_correction_config`
  makes this clean. Every generated config keeps `volume_limit:
  0.0`.

- **Wireless sub (dumb endpoint):** the leader computes the
  crossover, level, and delay and bakes them into the **LFE
  channel before streaming**. The dumb box just plays it. No
  on-endpoint DSP. Sub sync tolerance is *loose* (bass localizes
  poorly to the ear), so a few ms of misalignment is inaudible —
  this is why the sub is the easiest dumb endpoint.

- **Full-range satellite (dumb endpoint):** needs per-channel
  room correction for *its* seat, which it cannot compute (no
  CamillaDSP). So the leader applies a **channel-specific filter
  before streaming** (the `target_channels` path). To obtain the
  filter we run a one-time calibration: the satellite plays a
  sweep, a mic at the listening position captures it (reusing the
  existing `/correction` measurement flow), the filter is computed
  centrally and baked into that channel's stream. This open-loop
  calibration is **the single genuinely-new piece V1 adds**, and it
  is leader-side. See [HANDOFF-correction.md](HANDOFF-correction.md).

- **Inter-speaker time alignment** is Snapcast's job
  (`Client.SetLatency`), not correction's. Correction flattens each
  side's magnitude at its seat.

---

## 5. Volume

**Decision: a single room/pair-level command, fanned to all members
via the existing `VolumeCoordinator`, clamped and rate-limited at
the leader and re-clamped at each receiver.**

A pair/room volume command is "set `listening_level` on every
member." The leader calls each member's existing `POST /volume/set
{percent}` on `:8780`. No per-speaker `trim` composition algebra in
V1 — a bonded set shares one level.

Hard constraints (from the volume subsystem; see
[HANDOFF-volume.md](HANDOFF-volume.md)):

- **Push sources (Spotify Connect / Bluetooth) only work through the
  leader** — one librespot identity / one BT transport per Pi.
  That's fine: the leader *is* the single endpoint. Pair volume is
  coherent for CAMILLA_MASTER sources (AirPlay / USB / synced
  content); push sources are leader-local by nature.
- **The Camilla path is negative-only** (`set_volume_db` clamps
  positive to 0 dB). A network command cannot exceed 0 dB at any
  brainy member.
- Pair volume mid-duck defers to the Ducker (persisted target via
  `get_camilla_target_db`, never races live `main_volume`).
- State lives in the file, not process memory; mind the 2 s
  `last_used_at` echo window on fan-out.

*(A negative L/R balance trim — attenuate the louder side a few dB
for an asymmetric room, Camilla-legal — is a likely fast-follow.
`VolumeRecord` stays extensible for it; not built in V1.)*

---

## 6. Voice / TTS stays off the synced path

**Synced playback is a music-only, CAMILLA_MASTER-source feature.**
The conversational path (wake → LLM → TTS) never traverses the
Snapcast transport — sync requires a ~300–500 ms buffer that is
inaudible for music but would make the assistant feel broken (cf.
AirPlay 2 ~2 s, Snapcast ~1 s default buffering).

**V1 rule: the assistant speaks only on the leader it was addressed
on, on the local low-latency path; the room's buffered music keeps
playing underneath.** A *whole-house, time-synced spoken
announcement* (e.g. a timer ringing in every room at once) is a
genuinely hard product call — it would require routing TTS through
the buffered path, defeating its purpose — and is **open question
#1** for the owner (§9).

---

## 7. Resilience & hardware safety

### Failure modes (fixed-leader)

| Failure | Behavior (V1) | Mechanism |
|---|---|---|
| **Leader crash / power loss** | Room stops syncing. A *brainy* follower degrades to standalone local playback if it has its own source; otherwise it goes silent **with a cue + `/state` flag + dashboard card** — never silent-deaf. A *dumb* follower goes silent (correct). | No election. Boot reconciler modeled on `jasper-wifi-guardian`, incl. the stash-stale "don't stomp a manual regroup" branch. |
| **Follower drop** (unplug, power-cycle) | That channel/sub goes silent; the leader keeps playing its own share. On return the follower **self-rejoins on boot** to its declared leader. | `snapclient` rebuffer + boot reconciler. Absence shown on `/state`, doctor, dashboard. |
| **WiFi blip** | Buffer rides short blips; sustained loss → follower degrades + surfaces the failure. | TCP retransmit + buffer depth (the jitter lever — §2). WiFi is the supported transport; no Ethernet requirement. |
| **Solo (N=1), grouping off** | **Zero cost, verified.** No `snapserver`, no `snapclient`, no FIFO consumer registered (outputd byte-identical to today), no advert, no thread. | Mirrors peering `mode=off`. |

A dumb endpoint going silent when the leader is off is **correct
behavior**, not a regression (a sub *should* be quiet when the system
is off; a satellite's room depends on the leader anyway). We make it
*visible*, not invisible.

### Networked loud-output safety (critical for the dumb tier)

A dumb endpoint has none of JTS's software safety floors, so safety
is enforced at the analog stage — **exactly the existing
dongle-pinned-at-100% pattern** (the DAC's analog output is a fixed
ceiling; all volume is done in software upstream):

1. **Pin the endpoint amp's analog gain at install** so digital
   full-scale (0 dBFS) = the loudest SPL you ever want. Now *no*
   stream — buggy or malicious — can exceed a safe level, because
   the ceiling is physical. A `jasper-doctor` check verifies it
   stays pinned.
2. **The streamed audio is already clamped at the source** — it
   left the leader after CamillaDSP `volume_limit: 0.0` and the
   negative-only `set_volume_db` clamp (we tap post-clamp, §2).
3. **The endpoint must output silence, not noise, on stream loss**
   (mute-on-underrun). Important for a sub — a dropout must not
   thump the driver.
4. **Volume fan-out is clamped + rate-limited at the leader and
   re-clamped at each receiver** (never trust a network value).
5. **Snapcast's LAN audio ports** (1704/1705) are part of the
   threat surface, not just the control plane. Bind them to the
   specific LAN interface (not `0.0.0.0`-all); the home-LAN trust
   boundary is explicit (JTS already assumes it for inter-Pi). On a
   *brainy* member an injected stream still hits the local
   `volume_limit: 0.0` ceiling; on a *dumb* endpoint the analog
   ceiling (1) is the floor.

### Bond-formation transient

Forming a bond moves the leader's own music from near-zero local
latency onto the buffered path — a one-time transient (gap or brief
repeat) structurally identical to the source-switch transient JTS
already handles. Reuse the existing mux/`VolumeCoordinator`
prepare→move→finalize guard; accept one bounded transient at
formation; log it.

### Retained invariants

Realtime units (`snapserver`/`snapclient` FLAC, FIFO consumer) in an
audio slice with `MemorySwapMax=0`; **no CPU caps** (surface FLAC
CPU on `/system/` per project rule); the snapfifo consumer is a
*separate* sender from the chip-AEC one; crash-only restartable
units; **fail loud** if a bond is configured but its SSOT env file
is missing (exit-78 + cue + dashboard).

---

## 8. Phased delivery

**P0 — feasibility spike with MEASURED gates (throwaway, not
product).** Stand up `snapserver` reading a hand-fed FIFO on one
brainy Pi + `snapclient` on a second + a **Pi Zero playing a sub
channel** (conveniently exercises the cheap-endpoint path and the
loose-sub-sync claim at once) + the leader's own localhost snapclient
on a real content lane (never Loopback). Measure, on the **actual
household network**:
  1. **Inter-speaker sync-error distribution** (p50/p95/p99) on
     **WiFi** — idle and under `tc netem` loss/jitter — sweeping
     **buffer depth + codec** to find the setting that holds the
     bound, plus an ear check for comb-filtering on a mono tone to
     the L/R pair. Working target: p99 < 5 ms for L/R. Ethernet is
     measured only as a best-case reference, **never as a fallback
     requirement** — if WiFi needs a deeper buffer, that's the
     answer, and the lever is buffer size, not the cable.
  2. **FLAC encode/decode RAM + CPU budget** on the 1 GB Pi with a
     bond active atop the existing daemon stack (Pss + per-core).
  *Gate:* both numbers exist and pass; voice/TTS/AEC verified
  unaffected; solo N=1 byte-identical with grouping off.

**P1 — the product: fixed-leader bonded room (stereo pair, 2.1 with
wireless sub, full-range satellite), music-only, manual config.**
`Speaker` + `BondedSet` entities; `bond_id`/`role`/`leader` TXT
records; minimal localhost JSON-RPC (`SetLatency`/`SetVolume`);
local-Camilla channel-select + leader-side LFE crossover +
`target_channels`; the **dumb-endpoint image/role** (snapclient +
pinned-amp safety config); the **satellite calibration flow**
(sweep → mic at seat → baked per-channel filter); clamped/
rate-limited volume fan-out; the `/rooms/` directory surface (§ web);
the bond-formation transient guard; the boot reconciler (self-rejoin,
stash-stale branch). *Gate:* form/dissolve a bond from config; pair
volume tracks within bounds; leader crash → visible degradation;
follower power-cycle → auto-rejoin without stomping a manual regroup;
`/state.resilience.multiroom` + doctor + dashboard card present;
hardware-free pytest for the testable seams (config parse,
fail-loud-on-missing-SSOT, volume clamp/rate-limit, reconciler
stash-stale branch, `target_channels` config-gen).

**P2 — deferred until real demand:** transient ad-hoc `Group` +
leader election (only when a 3rd room exists, and re-justify election
vs. fixed-leader-per-group first); ESP32/Pico endpoints (firmware
ownership, no payoff yet); negative L/R balance trim; native
PTP-for-AirPlay carve-out (only if sample-perfect AirPlay multi-room
becomes a hard requirement).

### Web UX (P1)

A `/rooms/` page rendered with `canonical_page()` (page CSS in
`/assets/rooms/`, never `app.css`), shared icon sprite, CSRF meta.
It is a **directory, not a config aggregator**: each sibling row
links to that peer's own `http://<address>/system/`, so you
configure each speaker on its own UI (sidesteps cross-Pi write/auth;
home-LAN trust). This Pi's own `room`/`role`/`leader` are editable
in place; sibling fields are read-only. Live refresh, if wanted,
ships as a static ES module polling `GET /rooms.json` (mirror
`system_setup.py`'s `/data.json`). Escape all untrusted strings
(`room`, mDNS names, `address`) — escaped `data-*` + delegated
handler, never inline `onclick`. New wizard socket port →
`install.sh` must `systemctl restart` (not `start`) the wizard
socket (PR #118 502 failure mode).

---

## 9. Open questions for the project owner

1. **Whole-house spoken announcements.** V1 = leader-local TTS only
   (the assistant answers where addressed; room music keeps playing).
   Time-synced whole-house announcements need TTS on the buffered
   path — a real product call.
2. **AirPlay 2 sync expectation.** Route AirPlay through the
   Snapcast FIFO like every source (uniform, simpler), giving up
   shairport/nqptp's free PTP sample-alignment? A native PTP
   carve-out only earns its keep if sample-perfect AirPlay
   multi-room is a hard requirement.
3. **When does multi-*room* (>1 room) actually arrive?** The whole
   `Group`/election deferral rests on "one pair/room for now." A
   third room being imminent reorders the roadmap.

---

## 10. References

**In-repo:**
- [HANDOFF-peering.md](HANDOFF-peering.md) — wake arbitration (the
  *other* multi-Pi subsystem; reused discovery/identity substrate).
- [audio-paths.md](audio-paths.md),
  [HANDOFF-fan-in-daemon.md](HANDOFF-fan-in-daemon.md) — the output
  pipeline the sync engine taps.
- [HANDOFF-correction.md](HANDOFF-correction.md),
  [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md) —
  per-speaker correction + the calibration flow reused for satellites.
- [HANDOFF-volume.md](HANDOFF-volume.md) — `VolumeCoordinator`,
  source `VolumeMode`, the negative-only clamp.
- [HANDOFF-resilience.md](HANDOFF-resilience.md) — the resilience
  ladder the failure modes plug into.

**External prior art (consulted 2026-06-04):**
- Snapcast (badaix/snapcast) — the adopted sync engine.
- Music Assistant — player/sync-group model; *pairing ≠ grouping*.
- Home Assistant `media_player` grouping + Squeezebox/LMS slimproto —
  the control-vs-sync boundary.
- AirPlay 2 (PTP/IEEE-1588), Roon RAAT, Sonos (TruePlay per-speaker,
  bonded stereo pair, Sub crossover) — commercial references.
- Pi Zero 2 W + stock `snapclient` — the chosen dumb-endpoint tier
  (ESP32/Pico judged not worth the firmware ownership for V1).

---

Last verified: 2026-06-04 (off-by-default plumbing landed + staff
review — see §0 for what shipped; the §8 sync/RAM numbers remain
unmeasured until the spike runs on hardware)
