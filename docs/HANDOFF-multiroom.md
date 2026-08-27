# Handoff: multi-room / multi-speaker audio (stereo pair, 2.1, wireless sub)

Canonical for grouped/synchronized playback across JTS speakers: stereo pairs,
2.1 with a wireless sub, and multi-room. The bonded music dataplane,
member-local TTS, and the `/rooms` bond-forming UI are shipped and off by
default; a speaker with no bond configured pays nothing for this feature
existing.

The decisions behind the shape live in ADRs, not here:
[ADR-0110](adr/0110-buy-the-sync-engine-one-stream-leader-bakes.md) (Snapcast is
bought; one stereo stream; the leader bakes per-channel correction; receivers
pick a channel) · [ADR-0111](adr/0111-one-fixed-leader-no-election.md) (one
fixed leader, no election) ·
[ADR-0112](adr/0112-assistant-audio-never-rides-the-synced-stream.md) (assistant
audio stays local) ·
[ADR-0113](adr/0113-the-leader-self-loop-is-never-a-single-point-of-failure.md)
(the leader's fallback lane).

Neighbouring owners — do not restate them here:
[HANDOFF-distributed-active.md](HANDOFF-distributed-active.md) (active
endpoints, the two kinds of subwoofer, the driver-DSP clock contract) ·
[HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md) (parking a
follower's sources) ·
[HANDOFF-control-plane-auth.md](HANDOFF-control-plane-auth.md) (cross-device
auth) · [HANDOFF-peering.md](HANDOFF-peering.md) (wake arbitration — a different
subsystem sharing this discovery/identity substrate) ·
[dumb-endpoint-bringup.md](dumb-endpoint-bringup.md) (the Zero 2 W streambox
runbook) · [HANDOFF-volume.md](HANDOFF-volume.md) ·
[HANDOFF-airplay.md](HANDOFF-airplay.md).

---

## 0. Implementation status

**Shipped:** the control/observability scaffolding (config/state/reconcile, the
`/rooms` UI); the per-channel correction axis; the bonded music dataplane
(leader CamillaDSP → snapserver pipe → snapclient FIFO → member outputd
`dac_content` lane); member-local TTS plus the grouping supervisor; the wireless
sub with symmetric receiver-side bass management; pair balance; the N-member
roster.

**Not built:** per-follower calibration (an uncalibrated follower ships FLAT,
never the leader's wrong-room curve); a general channel/leader picker for bonds
beyond pair-plus-sub; automatic unwind to solo.

**Deleted, and not a rollback target:** the second music-only fan-in output PCM
(`JASPER_FANIN_MUSIC_OUTPUT_PCM`). It shipped off by default as the music/voice
split's producer half and nothing ever wrote its env var or read the PCM,
because routing the leader's TTS to outputd already leaves fan-in's ONE output
music-only while bonded. A name-absence guard in
`tests/test_fanin_coupling_rust_contract.py` keeps it from creeping back: if
multi-room v2 wants a pre-TTS fan-in tap, design it against the then-current
topology and update that guard deliberately.

**Roles are runtime; tiers are install-time.** There are two install profiles,
`full` and `streambox`; the former `endpoint`/`satellite` tier was removed and
those tokens map to `streambox`. A box playing a bonded channel is any box in
the **follower** role. Every member of either role uses the single
`snapclient → FIFO → outputd` lane — there is no direct-ALSA endpoint variant.

**Requested and landed roles are deliberately separate.** The wizard-owned
`grouping.env` keeps a requested follower bond even when a safety gate refuses
it; the reconciler falls back to solo and preserves the block reason. The
root-owned `/var/lib/jasper-grouping/effective-role.json` carries a
fingerprinted local-source permission plus the boot ID that produced it, in a
persistent `StateDirectory`, so a deny survives an interrupted transition and a
reboot while boot freshness invalidates stale grants. Grants are deny-first: a
requested follower is denied before role mutation and granted solo-source
permission only after the solo DSP restore and unit plan land; a landed follower
moving to solo/leader reports `role_transition_in_progress` until every
landed-role predicate succeeds. Stale, prior-boot, malformed, or missing grants
park fail-safe. Details:
[HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md#role-parking).

**Runtime liveness** is `jasper.control.grouping_supervisor`: a starvation watch
that kicks the reconciler, continuous leader binding read-repair, and
rostered-follower reassert using the household credential
(`JASPER_GROUPING_SUPERVISOR=disabled` turns it off). Cascade observability is
`jasper.multiroom.cascade_timeline`, a bounded
`/state.resilience.multiroom_cascade` ring that scans existing
`multiroom.reconcile.*` / `restart_broker.*` / `grouping_supervisor.*` journal
lines so an operator can answer "what kicked what?" without a log bundle; it is
solo-gated (no bond ⇒ no journal scan) and switches off with
`JASPER_MULTIROOM_CASCADE_TIMELINE=disabled`.

The package is `jasper/multiroom/`; read the module docstrings for what each
file owns, and `reconcile.main`'s docstring for the load-bearing apply order.

---

## 1. What we're building

A household runs several JTS speakers and wants them to play together, in sync:
a **stereo pair**, **2.1** (a pair plus a wireless sub), and **multi-room**
(several of those, in different rooms). Speakers come in three tiers: the
**brainy speaker** (the full JTS unit — assistant, source connections,
room/content DSP); the **transport endpoint** (a cheap Pi Zero 2 W + DAC running
the control plane and a synchronized audio client, because a second Pi 5 is too
expensive to be a right channel and a wireless sub has to be cheap); and the
**driver-DSP endpoint** (an active satellite that runs local CamillaDSP for
crossover/protection, because that is hardware safety at the DAC — the leader
still owns sources, room/content DSP, grouping, and voice).

**The non-negotiable UX rule: a room is one logical speaker to the outside
world.** To an iPhone or Mac, a 2.1 living room is a single AirPlay target, a
single Spotify Connect device, a single future Bluetooth pairing. All channel
splitting happens behind the scenes; the sender never sees the followers.
Deferred past V1: transient ad-hoc groups, automatic leader failover, and
ESP32/Pico endpoints.

---

## 2. The dataplane

Snapcast is the clock/transport/dejitter engine, the leader bakes one stereo
program, and every receiver picks its channel —
[ADR-0110](adr/0110-buy-the-sync-engine-one-stream-leader-bakes.md) holds the
decision and the prior art.

```
SOLO (unchanged):
  renderers → fanin (music + TTS) → CamillaDSP (correct) → outputd → DAC

LEADER (stereo pair):
  renderers → fanin (MUSIC ONLY by construction — the leader's TTS is routed to
                     outputd while bonded, so fanin's ONE output has no assistant)
            → CamillaDSP   (bake per-channel: L=leader-seat, R=follower-seat;
                            volume_limit:0.0 clamp; ONE instance)
            → pipe (FIFO)  → snapserver  (ONE stereo stream; Snapcast owns rate)
                ├─ leader localhost snapclient (-h 127.0.0.1)
                │     → FIFO → outputd ChannelPick (mix leader TTS here) → DAC
                └─ follower snapclient → FIFO → outputd ChannelPick → DAC

FOLLOWER (transport): snapclient → FIFO → outputd ChannelPick → DAC.
FOLLOWER (driver-DSP): snapclient → grouping ring → CamillaDSP
                       [crossover/protection only] → outputd active sink.
```

**Five timing invariants (load-bearing):**

1. Each member's `jasper-outputd` DAC write loop stays the **sole local playback
   timing owner**. The leader's CamillaDSP is the Snapcast producer; member
   outputd consumes the explicit snapclient FIFO lane. **outputd is not, and
   must not become, the snapfifo producer** — the `SnapfifoSink` machinery that
   assumed it was is deleted, and producer liveness must come from the producing
   daemon's own status surface, never a Python mirror of env intent.
2. The leader runs its *own* `snapclient` against `127.0.0.1`. What a member's
   snapclient writes into must report time-to-DAC honestly — a raw-PCM FIFO or
   the grouping ring — and **never snd-aloop**, which lies about
   `snd_pcm_delay`.
3. Voice, wake, and TTS stay **entirely off** the Snapcast path (§6).
4. **The AEC reference stays `== final DAC content`, TTS-inclusive and
   post-round-trip** (inv-A). Software AEC consumes outputd's final-electrical
   localhost UDP monitor and the optional chip-AEC writer uses its dedicated
   XVF3800 reference PCM; neither shares the Snapcast producer transport. On a
   bonded member the tap must stay at outputd's final post-mix buffer, holding
   both the round-tripped snapclient-paced music AND that member's own TTS,
   summed **before** the publish — assistant audio mixed after the tap bleeds
   into the mic and false-fires wake or breaks barge-in.
5. **Exactly one rate adjuster per chain.** snapclient's sample-stuffing is the
   synced chain's rate tracker, and CamillaDSP structurally cannot fight it on
   the leader: `enable_rate_adjust` is unsupported on the `File`/pipe backend,
   which has no output clock. So no CamillaDSP *in a bonded chain* runs
   `rate_adjust=true`, on either role. A dumb follower is the deliberate
   exception: its CamillaDSP is out of the bonded path, feeding the fallback
   lane into Ring B, an ioplug sink with no clock to track. The leader-bake
   half is the member-config policy (`member_camilla_kwargs`), applied
   identically on every config path (`/sound`, `/correction`, the reconciler)
   rather than threaded per call site; `check_grouping_rate_adjust` is the
   backstop and owns the full rule.

**Buffer depth is the jitter lever, and it is now real.** `buffer_ms` rides the
global `--stream.buffer` flag in `reconcile.snapserver_argv`. It was previously
passed as a `pipe://…&buffer_ms=` source-URL param that snapcast silently
ignores, so every bond ran the 1000 ms default — **any on-device buffer-sizing
observation recorded before that fix was made against 1000 ms.**

**Snapcast persists group→stream bindings**, and snapserver also registers the
packaged "default" pipe source, so a stale binding can point at a stream that
exists, is idle, and plays zeros behind green health. Every leader reconcile
pins bindings by an **ownership rule**: a group survives iff bound to a
JTS-owned stream; anything else is re-bound, connected or not. Runtime health
independently verifies the live picture (every connected client on our stream,
audible, leader's own client present); an unreachable snapserver RPC is an
explicit `degraded`, never a silent skip.

**The dead-pipe chain-breaker.** camilladsp 4.1.3 exits cleanly (0) when its
`File` sink path is absent and blocks un-SIGTERM-ably in `open(2)` when the FIFO
has no reader; with `Restart=always` plus `StartLimitBurst=5/60s`, a snapserver
hard-death while bonded would exhaust Camilla's recovery budget in under a
minute. `deploy/bin/jasper-camilla-pipe-guard` (`ExecStartPre=-`, pure bash,
fail-open — a repair needs positive evidence of a dead pipe) repairs the
statefile to the base config before camilla launches, so camilla runs solo-safe
with `volume_limit` intact while `grouping.env` stays bonded for the next
reconcile. Surfaces: `event=camilla_pipe_guard.*` and the `leader pipe` check.

**Restart hygiene — a control-plane action is not a crash.** The reconciler
`reset-failed`s before each restart it issues, so a burst of `/grouping/set`
applies cannot inherit prior start-limit parking and escalate a reboot-budget
unit; genuine crash loops still escalate, because a daemon's own `Restart=` path
does not reset. Cross-owner kicks are queued (`--no-block` restart of
`jasper-aec-reconcile`, sole owner of voice and the AEC bridge); same-owner
ordered applies stay blocking. At the `/grouping/set` kick site the first write
kicks promptly and later writes inside a 60 s window schedule one trailing
oneshot, so a sweep applies the final value exactly once; trim-only writes
bypass that cooldown through `jasper.multiroom.runtime_balance`.

---

## 3. Identity, grouping, and the leader

One fixed, config-declared leader per bond; no election, no automatic failover —
[ADR-0111](adr/0111-one-fixed-leader-no-election.md). A `Speaker` is the peering
`peer_id` (stable UUID4, reused verbatim) plus name, correction profile,
calibrated latency, and channel capability; a bond is `role ∈ {leader,
follower}` + `channel ∈ {stereo, left, right, sub, mono}` + `bond_id` +
`leader_addr` in `grouping.env`, surviving reboots and addressable as one room
and one volume.

**The leader records its roster rather than inferring membership.** Inference
from "who on the LAN claims my bond_id" failed live when a foreign test Pi
transiently claimed a bond: swap, trim, and balance all failed, and unbond would
have disabled the foreign device. Leader-only keys carry it now —
`JASPER_GROUPING_PEER_ADDR` / `_PEER_NAME` (the primary L/R sibling) and
`JASPER_GROUPING_ROSTER` (`addr|name|channel` entries for every follower, each
SSRF-checked as private/loopback IPv4 in `validate_grouping`). All ride
`/grouping/set` with the preserve-when-omitted contract; explicit empty clears,
and non-leader bodies clear so a role flip cannot keep a stale roster.
`rooms_setup.resolve_bond_peer` is the ONE resolver for swap/trim/balance/unbond:
roster IP probe → on failure re-find the peer name in the live directory and
accept the same-bond match at its new address (`event=rooms.peer_addr_drift`) →
else a hard error naming the speaker, never a fall-back to inference a foreign
claimer could satisfy.

**Discovery and identity** ride the peering substrate. Every speaker advertises
on the always-on `_jasper-control._tcp` service with `name=` and `peer_id=` TXT
records (rendered through `jasper/avahi_service.py`: XML-escaped, fail-soft,
idempotent); `jasper/identity.py`'s `read_identity()` is the single identity
reader and `jasper/mdns.py`'s `browse_once` the single browse primitive. mDNS is
unauthenticated — treat `peer_id` as a stable handle and confirm
trust-sensitive operations over HTTP ([HANDOFF-identity.md](HANDOFF-identity.md)).
`leader_addr` is minted as the leader's stable `.local` handle so a follower
survives the leader's DHCP churn; a literal IPv4 is still accepted.

**Control between Pis** is `jasper-control`'s HTTP API on `:8780` plus a
localhost-only Snapcast JSON-RPC adapter for `Client.SetLatency` /
`Client.SetVolume`. Group-membership RPCs are deliberately not built —
membership is config.

---

## 4. Channel selection & per-speaker correction

**The leader bakes; receivers pick.** `emit_sound_config(room_peqs_right=…)`
bakes a different room correction per channel in ONE config: `None` (the
default) is byte-identical to the solo output, and `[]` bakes a FLAT right
channel, because an uncalibrated follower must never inherit the leader's
wrong-room curve. Only the ROOM segment is per-channel; preference EQ stays
shared taste, and every generated config keeps `volume_limit: 0.0`.

**A bonded member's channel pick is receiver-side** in `jasper-outputd`'s
`ChannelPick` (`rust/jasper-outputd/src/dac_content.rs`), driven by
`reconcile.outputd_grouping_env` from the member's `cfg.channel` — never a local
CamillaDSP weave. snapclient's `--player file` has no ALSA hop, so the source
does the pick: left/right duplicate, mono is a clip-safe average, unknown values
fail loud.

**Bass management is symmetric and receiver-side.** A `sub` member mono-sums the
program and applies a 4th-order Linkwitz-Riley low-pass at
`JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ` before the DAC — fail-closed, so a sub never
plays full-range on the FIFO path, the fallback lane, or a missing filter. A
passive main high-passes locally at the same corner via
`JASPER_OUTPUTD_DAC_CONTENT_HP_HZ`, which the reconciler emits only for a
non-`sub` member in a bond that has a sub; an absent or out-of-range corner
plays the mains full-range. The shared stream stays full-range either way. An
active main bonded to a *wireless* sub has its `…_HP_HZ` cleared, because an
active main with a *local* sub applies mains-HP once in its own Layer-A graph;
an active main with neither currently gets no mains-HP at all — the documented
gap, reported to `/correction/bass/` as
`mains_highpass_unwired_reason=active_endpoint_wireless_sub`. See
[HANDOFF-distributed-active.md](HANDOFF-distributed-active.md) "Subwoofer — two
different subs".

**The corner value has one home:** `jasper.camilla_emit.BASS_MANAGEMENT_CORNER_HZ_*`
(80 Hz default, 40–200 Hz bounds, LR4). `multiroom.config`,
`active_speaker.profile`, and `output_topology` bind to it rather than
re-declaring numbers that could drift, and the sub-LP guard ceiling references
the same constant. The shared `channel_select` mixer name and the clip-safe
−6.02 dB mono-sum primitive live in
[`jasper/camilla_emit.py`](../jasper/camilla_emit.py).

**Inter-speaker time alignment has three owners, not one.** Snapcast owns
dynamic distributed sync; Snapcast client latency (`--latency` /
`Client.SetLatency`) is a static whole-client PCM/output-path offset; leader-side
CamillaDSP `Delay` is the static per-channel *acoustic* offset aligning arrival
at the seat. Measure first: colocated or electrical endpoint baseline → Snapcast
latency; listening-seat arrival delta → leader-side channel delay. See
[`research/balance-sync-calibration.md`](research/balance-sync-calibration.md).

**Two "channel" vocabularies — don't conflate them.** This module's `channel`
(left/right/sub/mono/stereo) is the **inter-speaker** axis: which channel of the
program a whole speaker plays. `output_topology.SpeakerChannel.role`
(woofer/tweeter/…) is the **intra-speaker** axis: which driver a DAC output
feeds. They compose — channel-select runs first, then the active crossover
splits that program across drivers — and never need to know about each other,
because channel-select is interface-preserving (a 2→2 transform).

---

## 5. Volume

A pair/room level is one command fanned to all members, clamped and rate-limited
at the leader and re-clamped at each receiver. Where it lands depends on the
member:

1. The shared room level stays `master_gain` on the **leader** (pre-stream —
   every member inherits it; the acknowledgment can lead the audible change by
   up to a buffer, acceptable for music).
2. Per-member trim and *all* follower volume ride **Snapcast per-client volume**
   (`Client.SetVolume`, post-buffer — it works for a DSP-less endpoint).
3. A transport follower never receives `POST /volume/set` (no `master_gain` to
   set). A bonded follower's own volume surfaces forward to the leader, so
   slider, remote, and curl all control the pair.

**Pair balance** is a centered slider on `/rooms` that rewrites BOTH member
trims absolutely and re-normalizes wasted attenuation so one side is always
0 dB. `JASPER_GROUPING_TRIM_DB` is wizard/bond-owned intent — the **louder**
speaker trims down, never a boost, and outputd re-validates fail-closed. For
passive endpoints the reconciler derives `JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB`
and outputd applies one precomputed linear gain to the whole armed path (FIFO
and fallback periods alike, so a starvation transition produces no level jump)
before duck/mix/publish, so the AEC reference carries the trimmed program.
Active endpoints instead carry a non-positive `pair_balance_trim` gain after
`channel_select` in their driver-domain graph. Trim is preserved when omitted
from `/grouping/set`, so structural edits (add-sub, swap) never clobber a
calibrated balance; fresh pair creation and unbond explicitly send `trim_db=0.0`.

The guided walkthrough (`/balance/`, `jasper/web/balance_flow.py`) measures ONE
speaker at a time at matched *received* loudness: a per-channel ramp plays from
exactly one physical speaker through the full normal chain while the phone at
the listening position meters its own in-band level and streams dBFS frames to
the server, which owns floor/target/lock. It normalizes the measurement path
first — snapshotting CamillaDSP `main_volume` and both Snapcast clients'
volume/mute state, setting the bounded calibration level, restoring in `finally`
— and fails visibly rather than measuring through hidden attenuation.

Hard constraints from [HANDOFF-volume.md](HANDOFF-volume.md): push sources are
leader-local by nature; the Camilla path is negative-only; a pair volume
mid-duck defers to the Ducker.

---

## 6. Voice / TTS stays off the synced path

Synced playback is a music-only feature: the conversational path never traverses
the Snapcast transport, and a bonded passive member mixes its own assistant
audio at `jasper-outputd` after the round trip and before the reference is
published. Active endpoints keep TTS on fan-in upstream of their
crossover/protection graph; wireless sub followers park voice entirely. The
rule, the AEC-reference reason behind it, and the whole-house-announcement
decision are
[ADR-0112](adr/0112-assistant-audio-never-rides-the-synced-stream.md).

The reconciler derives the route matrix in
`jasper.multiroom.tts_route.expected_grouping_tts_route` and writes both ends —
`JASPER_OUTPUTD_TTS_SOCKET` in `grouping-outputd.env`, and
`JASPER_TTS_OUTPUTD_SOCKET` / `JASPER_TTS_MIX_STAGE=post_dsp` /
`JASPER_GROUPING_VOICE_PARK` in `grouping-voice.env`. It omits the socket key
entirely for solo and active endpoints (present-but-empty would break voice's
fan-in default) and skips the empty file on a fresh solo reconcile so first boot
does not restart voice. `check_grouping_tts_lane` catches drift between the two
files, including the worst shape: voice targeting a socket outputd never armed
(a silent assistant).

---

## 7. Resilience & hardware safety

### Failure modes (fixed-leader)

| Failure | Behavior | Mechanism |
|---|---|---|
| **Leader crash / power loss** | Room stops syncing. A brainy follower degrades to standalone local playback if it has its own source; otherwise it goes silent **with a cue + `/state` flag + dashboard card** — never silent-deaf. A dumb follower goes silent (correct). | No election. Boot reconciler with a stash-stale "don't stomp a manual regroup" branch. |
| **Follower drop** | That channel/sub goes silent; the leader keeps playing its own share. On return the follower self-rejoins on boot. | `snapclient` rebuffer + boot reconciler; absence shown on `/state`, doctor, dashboard. |
| **Leader self-loop degraded** | The leader falls back to the DIRECT fan-in lane rather than going silent on its own music. | [ADR-0113](adr/0113-the-leader-self-loop-is-never-a-single-point-of-failure.md). |
| **WiFi blip** | Buffer rides short blips; sustained loss degrades the follower visibly. | TCP retransmit + buffer depth. WiFi is the supported transport; no Ethernet requirement. |
| **Solo (N=1), grouping off** | **Zero cost.** No snapserver, no snapclient, no FIFO consumer, no advert, no thread. | The solo-impact contract, pinned per increment by golden-config and default-config regression tests. |

A dumb endpoint going silent when the leader is off is **correct behavior**, not
a regression — we make it *visible*: `read_grouping_state` carries a `runtime`
health block, `check_grouping` warns on degraded, and `/rooms` renders an amber
**Degraded** badge with the reason. That block also carries `pair_lock`, whose
follower-clock-lock signal honestly reads `unobservable`: Snapcast's JSON-RPC
exposes connection, binding, latency, stream status, and volume, but not
follower buffer fill, drift, or time-lock, so the verdict is `unknown` rather
than pretending byte flow proves lock. An enabled snapshot also carries a `ring`
block — one read-only 128-byte read of the grouping ring's shared header (never
an mmap, never an ALSA open) classifying ingress as `flowing` / `priming` /
`reader_stalled` / `idle` / `absent` / `unreadable`. The priming-versus-stalled
split is the point: the pacing governor holds a stalled reader near nominal, so
drop volume no longer separates a stall from a cold start. Both blocks are
solo-gated — no bond, no key, no probe.

### Networked loud-output safety (critical for the dumb tier)

A dumb endpoint has none of JTS's software safety floors, so safety is enforced
at the analog stage — the existing dongle-pinned-at-100% pattern:

1. **Pin the endpoint amp's analog gain at install** so digital full-scale is
   the loudest SPL you ever want; then no stream — buggy or malicious — can
   exceed it, because the ceiling is physical. A doctor check verifies it.
2. **The streamed audio is already clamped at the source** — it left the leader
   after CamillaDSP `volume_limit: 0.0` and the negative-only `set_volume_db`.
3. **The endpoint outputs silence, not noise, on stream loss.** A dropout must
   not thump a sub's driver.
4. **Volume fan-out is clamped and rate-limited at the leader and re-clamped at
   each receiver.** Never trust a network value.
5. **Snapcast's LAN audio ports (1704/1705) are part of the threat surface**,
   not just the control plane. Bind them to the LAN interface.

### Grouping control plane — threat model

`POST /grouping/set`, `GET /grouping`, and the bond/unbond fan-out that POSTs to
those on *other* speakers are authenticated, by two orthogonal mechanisms that
both apply. The per-device `control_token` is mandatory on `/grouping/set`, but
it is a CSRF token carrying no caller identity, so it gates **browser → its own
speaker** only; the device-to-device leg authenticates with a **household
credential** — a shared secret minted at the human pairing moment
(`POST /bond`), distributed over the trusted LAN, persisted `0640` group
`jasper` per member, and presented as `X-JTS-Household` (the supervisor uses it
too when reasserting a rostered follower with no browser in the loop). Beside
that, the **SSRF guard** constrains cross-speaker targets to private/loopback
IPv4 and rejects bare hostnames — it bounds *where* the server will talk, while
the credential authenticates the *caller*.
[HANDOFF-control-plane-auth.md](HANDOFF-control-plane-auth.md) is the single
source of truth. **Residual, stated honestly:** a malicious LAN device can still
initiate its own `POST /bond` to mint a secret — the residual the whole
LAN-trust posture accepts, closed only by a future pairing code at bond time.

### Retained invariants

Realtime units in an audio slice with `MemorySwapMax=0` and **no CPU caps**
(surface CPU on `/system/` instead); crash-only restartable units; **fail loud**
if a bond is configured but its env file is missing; and **WiFi power-save stays
disabled** — `install.sh` already does this on `wlan0` for AirPlay, and
brcmfmac power-save is a documented cause of streaming dropouts, so a follower
image must keep that step. `jasper-snapclient.service` carries a narrow
`LogRateLimitBurst=30` per 60 s so a follower whose leader is powered off stays
visibly degraded without filling the journal with one refused connection per
second.

---

## 7.5 The dumb-follower role-state contract

| Unit | Solo | Leader | Follower | Transition owner |
|---|---|---|---|---|
| jasper-control | runs | runs | runs (volume/transport forward to leader) | — always on |
| jasper-outputd | runs | runs (dac_content L) | runs (dac_content R) | grouping reconciler (env + restart-on-change) |
| jasper-camilla / jasper-fanin | run | run (camilla bakes the pipe) | run (fallback lane only) | grouping reconciler (config swap) |
| jasper-snapserver | stopped | runs | stopped | grouping reconciler (plan) |
| jasper-snapclient | stopped | runs | runs | grouping reconciler (plan) |
| Local source resource groups | per persisted source intent | per persisted source intent | **parked** (resource groups stopped; USB audio recomposed away) | [source lifecycle](HANDOFF-source-lifecycle.md) is the single owner; grouping supplies the role and waits for convergence |
| jasper-voice + jasper-aec-bridge (+aec-init) | per provider/mic gates | per provider/mic gates | **parked** (`disable --now`) | **jasper-aec-reconcile only** — grouping derives `JASPER_GROUPING_VOICE_PARK=1` and kicks it; bond-validity logic is never re-derived in shell |

Grouping invokes only `jasper-source-intent-reconcile.service` after the role
plan lands and waits for its terminal result; that coordinator alone knows the
accessory and USB-coupling ordering. An already-activating pass is joined before
the guaranteed-fresh pass runs, so a role snapshot cannot coalesce. AirPlay's
grouping latency file is refreshed with `try-restart`, so an intentional
household Off is never turned back on by a bond transition.

**Interface contract while a follower** — every surface tells the same story,
and "parked-by-role" is surfaced state, NEVER a silent failure. The landing page
shows a pair banner, hides the source selector, relabels the slider "Pair
volume", and says the leader listens. `/sources/` disables its toggles and 409s
`POST /set` (an `enable --now` would reopen the advertise/leak hole). `/voice/`
and `/wake/` save but skip the daemon restart while parked — config applies on
unbond. `/eq/` delegates to the leader. `/sound/setup/` stays local for the
output-topology and active-speaker commissioning work owned by the speaker
driving the DAC, while leader-owned EQ and volume-shaping controls are omitted
and their mutation routes blocked. Room is leader-owned and refuses mutations,
because a follower's sweep would play into a drained lane and be inaudible —
crossover measurement is the exemption, staying local to the DAC owner.
`/system/` 409s restart-voice and restarts only the alive audio subset. Doctor's
liveness checks read "parked (bonded follower)" via `_parked_as_bonded_follower`,
and `check_grouping_pair_channels` does the cross-member coherence probe that
catches a same-channel pair (an interrupted swap whose rollback also failed) —
green on every member-local surface otherwise. On the remote, volume and
play/pause forward to the leader; hold-to-talk is dead while parked (accepted).

**DSP ownership — the two-kinds split.** *Content DSP* (room correction, sound
preferences, EQ) is leader-side, baked per-channel into the synced stream: this
is what lets a dumb member stay dumb and scales to the Zero tier unchanged.
*Driver DSP* (active crossover, driver protection, per-driver gain/delay) must
live ON THE BOX DRIVING THE DAC — it is per-driver routing and
hardware-safety-critical. A passive follower has no driver-DSP path and
outputd's round-trip lane fail-closes on a non-single sink; an active follower
routes around the `dac_content` lane via CamillaDSP re-entry. That boundary is
owned by [HANDOFF-distributed-active.md](HANDOFF-distributed-active.md).

---

## 8. Surfaces and scaling boundaries

`/rooms/` is the combined **"Speakers"** page (port 8785,
`JASPER_ROOMS_WEB_PORT`): the speaker directory, the wake-response (peering)
toggle, and pair setup, because "my other speakers" is one household concern. It
lists every JTS speaker on the LAN via the always-on `_jasper-control._tcp` mDNS
service — not the peering-gated `_jasper-peer._udp`, so it works regardless of
peering state — each row a click-through to that speaker's own
`http://<hostname>.local/system/`. It is a directory, not a config aggregator:
you configure each speaker on its own UI. `GET /` is a data-free
`canonical_page()` shell plus an ES module; `GET /rooms.json` carries the data,
and the module renders every untrusted value (room, mDNS names, addresses) via
DOM/text APIs — never `innerHTML`.

Six POSTs: `/peering` (the wake-response toggle, read-modify-writing
`peering.env` while preserving keys owned by `/speaker/`), `/bond`, `/unbond`,
`/swap`, `/trim`, `/mains-highpass`. `/bond` is the one-flow stereo-pair setup:
the browser sends only `peer_addr`, and the server owns the member plan, mints a
`bond_id`, and fans config out SERVER-side to each member's `/grouping/set`.
Before writing any member it concurrently reads every member's `GET /grouping`
`readiness` verdict and fails closed if it is missing, malformed, or blocked —
the target derives that verdict through the same policy seam `/grouping/set`
rechecks immediately before mutation, so preflight and write authorization
cannot drift and a race still fails safely. `/unbond` discovers membership from
the roster (or, pre-roster, from each member's `GET /grouping`) and disables
self plus every recorded member, so a 2.1's sub is never orphaned. `/swap`
requires exactly one reachable same-bond peer and a {left,right} channel set,
preserves roles/bond/leader, flips only `channel`, and repairs a same-channel
pair. Fan-outs run concurrently, so one slow peer never serializes the rest.

**Room is not edited here.** The room label lives in the speaker-identity home
(`jasper/speaker_name.py`, `JASPER_SPEAKER_ROOM`, written by `/speaker/`); the
self card shows it with a link there. Peering's legacy `JASPER_PEER_ROOM`
survives only as a read fallback for older env files.

### Known scaling boundaries

Each is paired with the trigger that says "generalize now, not before":

- **Cross-speaker peer-control client.** The HTTP-to-a-peer pattern stays local
  to `jasper/web/rooms_setup.py`, where the grouping POST and the shared
  `GET /grouping` transport already share one SSRF guard and one
  bounded-concurrency primitive. **Trigger to extract a `PeerControlClient`:** a
  second distinct cross-speaker API operation.
- **The `GET /grouping` wire contract has ONE home** — `grouping_response` plus
  `parse_grouping_response` / `parse_grouping_readiness` in
  `jasper/multiroom/state.py`, locked by a round-trip test. The regression it
  prevents is that envelope drifting across daemons. **Rule:** any new
  cross-daemon grouping payload follows the same builder + parser +
  round-trip-test shape.
- **Bond topology is pair-shaped.** `/unbond` already scales to N members; the
  CREATE flow does not. **Trigger for >2-member bonds:** the `channel`/`role`
  vocabulary grows (and `validate_grouping` with it), and the two-faced
  create/dissolve card in `deploy/assets/rooms/js/main.js` splits into create
  and manage views rather than growing a third face.

---

## 9. Open questions for the owner

1. **AirPlay 2 sync on a bonded leader — analyzed, build pending.** AP2 latency
   is sender-authored, so a receiver cannot grow the budget. The plan is neither
   to route AirPlay through the Snapcast FIFO nor a PTP carve-out: the bonded
   leader keeps shairport/nqptp's native AP2 path and compensates the round trip
   locally with a **bond-aware** `audio_backend_latency_offset_in_seconds`.
   INVARIANT: that term is added only while this speaker is an active bonded
   leader and torn down on unbond — solo and follower speakers stay
   byte-for-byte unaffected. Measure per-app with
   `scripts/airplay-latency-probe.sh` before sizing `buffer_ms`; the mechanism,
   the budget-versus-need arithmetic, and the shipped observability
   (`/state.grouping.airplay_latency_fit`, the `/rooms` lip-sync row,
   `check_grouping_airplay_latency`) are owned by
   [HANDOFF-airplay.md](HANDOFF-airplay.md) and
   `jasper/multiroom/airplay_latency.py`.
2. **When does multi-*room* (>1 room) actually arrive?** The deferral of ad-hoc
   groups and election rests on "one pair/room for now". A third room being
   imminent reorders the roadmap — and is the trigger to revisit election
   ([ADR-0111](adr/0111-one-fixed-leader-no-election.md)).
