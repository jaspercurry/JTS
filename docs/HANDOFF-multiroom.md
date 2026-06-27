# Handoff: multi-room / multi-speaker audio (stereo pair, 2.1, wireless sub)

> **Status: core bonded music dataplane shipped, still off by default.** This
> is the canonical design home for grouped/synchronized playback across
> multiple JTS speakers (stereo pairs, 2.1 with a wireless sub, and
> multi-room). SHIPPED: the control/observability scaffolding
> (config/state/reconcile, the `/rooms` bond-forming UI, the channel-split
> weave, inv-5), **Increment 1** (fanin's music-only output — the voice/music
> split), **Increment 2** (the per-channel correction axis — one CamillaDSP
> bakes L-for-leader-seat / R-for-follower-seat), **Increment 5 PR-1** (leader
> CamillaDSP → snapserver pipe → snapclient FIFO → member outputd
> `dac_content` lane), **Increment 5 PR-2** (member-local TTS + grouping
> supervisor), and the 2026-06-11 cleanup that removed the retired
> outputd-as-producer machinery. The **P0 spike RAN on hardware** (2026-06-10,
> jts3↔jts): resource gate passed (snapcast ≈ ~15 MB Pss / ~0.2 % CPU) and the
> software sync proxy was clean. Still to build: Increment 6
> (per-follower calibration) and any future auto-unwind policy. **§0
> "Implementation status" is the live single source of truth** for what exists;
> treat older design sections below as intended truth only after checking them
> against §0. The existing
> `jasper/peering/` subsystem ([HANDOFF-peering.md](HANDOFF-peering.md)) is
> **wake arbitration only** — picking which speaker *answers* "Hey Jarvis" — a
> different subsystem, though this design reuses its discovery/identity
> substrate.
>
> Design dialogue + prior-art research: 2026-06-04. Status last reconciled with
> code: 2026-06-24 (see §0 + the footer changelog).

---

## 0. Implementation status (2026-06-24)

**"Endpoint behaviour" is the runtime FOLLOWER role, not a separate install
tier.** There are exactly two install profiles — `full` and `streambox`. The
former third tier (`endpoint` / `satellite`) was removed; those tokens are
still accepted and map to `streambox` so a field box auto-migrates on its
next deploy. A box that just plays a bonded channel — the old "endpoint" — is
now any full/streambox box acting as a multiroom **follower**: the grouping
reconciler parks its local source resource groups (bridge daemons plus any
advertise-side resources such as the USB Audio Input gadget, via
`jasper/local_sources/registry.py`) and each parked local-source unit also
has the `jasper-local-source-allowed` `ExecCondition` start gate, so boot and
manual starts skip while the role is a valid bonded follower. On a full
speaker, the reconciler parks its voice/AEC brain via the derived park flag;
the landing page suppresses Source/Sound and
relabels Volume for followers. Every member, either role, uses
the single `snapclient -> FIFO -> outputd` member lane; there is no longer a
direct-ALSA endpoint variant. The Zero 2 W lab runbook lives in
[`dumb-endpoint-bringup.md`](dumb-endpoint-bringup.md).

**Increment 5 PR-1 (the bonded MUSIC dataplane) is BUILT (2026-06-11).**
A bond now moves audio end-to-end: the leader's CamillaDSP bakes the shared
program and writes the snapserver pipe; every member's snapclient writes the
round-trip FIFO (`--player file:`), which outputd's `dac_content` lane plays
with the member's channel pick. The grouping reconciler is the single
applier (camilla config swap + outputd lane env + member FIFO + units, in a
load-bearing order — see `reconcile.main`'s docstring).
**Increment 5 PR-2 (member-local TTS + the grouping supervisor) is BUILT
(2026-06-11).** While bonded, non-sub passive/dumb members route assistant
TTS to their OWN outputd (`rust/jasper-outputd/src/tts.rs` — the fanin
wire-protocol twin feeding `OutputCore`), so voice answers are instant and
member-local (inv-3) instead of riding the sync buffer to every speaker;
`PROGRAM_DUCK` follows the same socket, ducking content on the speaking
member only, and the barge-in `FLUSH_SYNC` ack carries DAC-true
`audio_played_ms` from the playout ledger. Active endpoints stay on fan-in
upstream of the active graph; wireless sub followers park voice and keep
outputd TTS unarmed. The reconciler derives that route matrix for both
ends (grouping-outputd.env + grouping-voice.env; drift check:
`grouping: TTS lane`, which replaced the PR-1 standing `TTS interim`
warn). Runtime liveness is owned by
`jasper.control.grouping_supervisor` (starvation watch → rate-limited
reconciler kick; continuous leader binding read-repair; rostered-follower
reassert using the household credential; off via
`JASPER_GROUPING_SUPERVISOR=disabled`). Auto-unwind to solo is deliberately
NOT built — disband stays one tap on /rooms until a real non-converging
failure shape is observed.
After-the-fact observability for restart cascades is owned by
`jasper.multiroom.cascade_timeline` — a bounded `/state` ring that scans the
existing `multiroom.reconcile.*` / `restart_broker.*` /
`grouping_supervisor.*` journal lines so an operator can answer "what kicked
what?" without a raw log bundle. It is **solo-gated** (a speaker with no bond
configured produces no bond cascade, so `_tick` skips the per-unit
`journalctl` scan and only re-reads the cheap grouping env; the scan resumes
the moment a bond is configured) and, like the sibling supervisors, has an
off-switch — `JASPER_MULTIROOM_CASCADE_TIMELINE=disabled` (exact match,
case-insensitive; any other value warns and stays enabled). Surface:
`/state.resilience.multiroom_cascade` (`{"enabled": false}` when disabled or
not yet running).
A leader whose bond apply did not land reads `degraded` with "active
CamillaDSP config does not write the snapserver pipe".
**The retired outputd-as-producer machinery was REMOVED on 2026-06-11** (the
"Stranded by this design" cleanup, §2): `SnapfifoSink` (`snapfifo.rs`)
deleted, the `SNAPFIFO_PRODUCER_WIRED` mirror flag + `effective_leader_tap_path`
removed, the reconciler's outputd tap-env write + try-restart limb removed,
and the unit's optional tap `EnvironmentFile=` dropped. The reconciler still
writes the live member outputd lane/TTS env and restarts outputd as the lane
applier; what is gone is outputd as the snapfifo producer. The doctor's
`check_grouping_tts_separation`
folded into `check_grouping`'s runtime detail. The canonical producer is the
leader's *CamillaDSP* feeding the pipe (Increments 3–5); when it lands, its
liveness signal comes from the producing daemon's OWN status surface (daemon
truth, never a Python mirror of env intent — the removed flag's lesson). The
**target architecture is settled** — see
**"Canonical signal flow"** (§2, decided 2026-06-10, research-grounded): the leader
bakes per-channel content correction in its ONE CamillaDSP, streams a single stereo
stream, and transport receivers channel-drop; voice stays leader-local. Driver DSP
is separate: a future active satellite may run local CamillaDSP for
woofer/tweeter crossover and protection on the box that drives those DACs/amps, but
that local graph is not room/content DSP and does not make the endpoint a brain.
The gating §8 spike **RAN on hardware (2026-06-10,
jts3↔jts) and passed the resource gate** — snapserver+snapclient ≈ ~15 MB Pss,
~0.2 % CPU, FLAC ≈ PCM; the software sync proxy was clean across every buffer/codec
(acoustic L/R confirmation pends — it folds into Increment 2a). Still to build:
Increment 6 (per-follower calibration). What exists:

- **`jasper/multiroom/config.py`** — pure, off-by-default
  `GroupingConfig` + `load_config()` (SSOT `/var/lib/jasper/grouping.env`;
  fail-safe to off, fail-loud `error` when on-but-invalid). Fields:
  `enabled, role, channel, bond_id, leader_addr, buffer_ms, codec, error`.
- **`jasper/multiroom/reconcile.py`** — pure `plan(cfg)` (config →
  desired snapserver/snapclient unit states; invalid → start nothing)
  + pure `snapserver_argv`/`snapclient_argv`; a thin `main()`
  entrypoint does the only systemctl I/O (`--reason` logged, validated
  on hardware, not in pytest). `snapclient_argv` passes `cfg.leader_addr`
  **verbatim** to `--host`; the bond wizard now mints that as a **stable
  mDNS `.local` handle** (the leader's `JASPER_HOSTNAME`, e.g. `jts3.local`)
  rather than a raw DHCP IP, so a follower's snapclient **survives the leader
  changing IP** — it re-resolves the name at connect time. A literal IPv4 is
  still accepted (`config.GroupingConfig.leader_addr` documents both); no
  reconcile change was needed because snapclient resolves either. (P0
  staff-review fix.)
- **`jasper/multiroom/state.py`** — `read_grouping_state()`, fresh-read
  (never `os.environ`); wired into `jasper-control` `/state.grouping`
  (fail-soft). Now also carries a **`runtime` health block** when grouping
  is enabled: the pure `derive_grouping_runtime(...)` compares the
  reconciler plan's expected units against their live `systemctl is-active`
  state and reports `off` / `invalid` / `ok` / `degraded` — a follower whose
  snapclient can't reach its leader shows `degraded` with the leader addr,
  not a green-looking config. The `systemctl` probe is the thin injectable
  I/O edge; on a solo speaker there is NO probe and NO `runtime` key (zero
  added cost). The same pure derive feeds `jasper-doctor`'s `check_grouping`
  (warn on degraded). §7 "make it visible, not invisible". **A leader that is
  configured but whose active CamillaDSP config does not write the snapserver
  pipe reads `degraded` too** — the pure derive takes the leader's current
  tap path as an injected arg (`leader_tap_path`); `read_grouping_state` /
  `check_grouping` read it via `leader_config.active_leader_pipe_path()` only
  for a valid leader. The runtime block also carries `pair_lock`, the
  composite "pair locked + healthy" verdict shared with `jasper-doctor`'s
  `grouping: pair lock` check. Today it distinguishes three truths:
  unit/binding health, local FIFO byte flow (`outputd.dac_content.serving_fifo`
  means bytes flow, not clock lock), and follower clock-lock. Snapcast's
  documented JSON-RPC (`Server.GetStatus`) exposes connection, binding,
  latency, stream status, and volume, but **not** follower buffer fill, drift,
  or time-lock, so `pair_lock.signals.follower_clock_lock.status` honestly
  reads `unobservable` and the overall verdict is `unknown` rather than
  pretending bytes flow proves lock. This is intentional: P2's rejoin gate can
  consume the shape now and tighten the one signal when a real lock source
  exists.
- **`jasper/atomic_io.py`** — the single home for atomic text-file writes
  (`atomic_write_text(path, text, *, mode=0o644)`: same-dir tempfile →
  `chmod`-before-`os.replace`, parent created, RAISES on failure + cleans up
  the temp; fail-soft is a caller policy). Extracted from the ~39 hand-rolled
  `tempfile`+`os.replace` sites the staff review flagged; the multi-room
  reconciler's two env writers (`_write_args_file`,
  `_write_outputd_snapfifo_env`) now delegate to it (keeping their fail-soft
  log+`return False` wrappers). The other sites migrate incrementally
  (separate PRs) — `atomic_io` is purely additive.
- **`jasper/camilla_emit.py`** — shared CamillaDSP YAML *emission*
  primitives (`fmt`, `emit_gain_filter`, `emit_peaking_biquad`,
  `emit_linkwitz_riley`, `emit_mixer`, `emit_channel_select_mixer`): the
  single home for *how* a gain/biquad/crossover/mixer is spelled in YAML.
  Extracted from the correction / sound / active-speaker / multi-room
  generators, which had each hand-rolled (and re-derived) these — 3 copies
  of `_fmt`, 4 mixer emitters. All four now consume it; high-level config
  *assembly* stays per-subsystem. The inter-speaker channel-select recipe
  (the `channel_select` mixer name + the clip-safe −6.02 dB mono sum) also
  lives here (`emit_channel_select_mixer` / `channel_select_sources`,
  promoted from `channel_split.py` in distributed-active Slice 2) so the
  multi-room member-config path and the active-speaker follower's
  driver-domain graph spell the pick one canonical way. The shipped generators are byte-identical post-migration
  (golden-diffed); multi-room's sub crossover upgraded to CamillaDSP's
  native `BiquadCombo LinkwitzRileyLowpass`.
- **`jasper/multiroom/channel_split.py`** — pure channel-split DSP
  fragment generator (P1.2). `build_channel_split(channel)` emits the
  CamillaDSP `channel_select` Mixer (left/right route; mono/sub L+R sum
  at a clip-safe −6.02 dB so identical L==R hits exactly 0 dBFS) and,
  for `sub`, a native LR4 `BiquadCombo` 80 Hz lowpass crossover — all via
  the shared `camilla_emit` primitives. Host-agnostic recipe: the same
  fragment runs *locally* on a brainy stereo-pair member or *on the
  leader* to pre-bake a dumb endpoint's stream (§4). Never names
  `master_gain` (preserves the Ducker's identity-mixer contract) and
  emits no positive gain — every generated mixer holds the signal ≤ 0
  dBFS under `volume_limit: 0.0`. The `channel` axis is inter-speaker and
  composes with `output_topology.SpeakerChannel`'s intra-speaker driver
  axis because channel-select is interface-preserving 2→2 (§4). Pure /
  hardware-free; live weaving into the active config is P1.3.
  **Two distinct sub low-pass mechanisms now exist — don't conflate them.**
  This CamillaDSP `BiquadCombo` fragment is *not* on the live dumb-follower
  round-trip path (members drop their channel in `jasper-outputd`'s
  `ChannelPick`, not a local CamillaDSP weave). The **shipped** dumb wireless
  sub (2026-06-23) low-passes **receiver-side in `jasper-outputd`** —
  `ChannelPick::Sub(corner)` runs its own Rust LR4 (mono-sum → 4th-order
  Linkwitz-Riley at `JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ`, default 80 Hz) before
  the DAC, fail-closed (never full-range on FIFO / inv-B fallback / missing
  filter). Passive/dumb mains in the same bond now also high-pass
  receiver-side in `jasper-outputd`: the reconciler writes
  `JASPER_OUTPUTD_DAC_CONTENT_HP_HZ` at the same bond `crossover_hz` when a sub
  is present and the default-on mains-HP toggle is enabled; the shared
  Snapcast stream stays full-range. Active endpoints are different: their
  outputd `dac_content` lane is disabled, so Layer-A CamillaDSP owns the HP/LP
  protection path. This `channel_split.py` LR4 fragment stays the recipe for
  the *brainy/CamillaDSP* sub and the leader pre-bake (gap 5 alternatives).
  Both reuse the same `emit_linkwitz_riley` corner math. See
  [HANDOFF-distributed-active.md](HANDOFF-distributed-active.md) "Subwoofer —
  two different subs" for the full gap-5 picture.
- **`jasper-outputd` snapfifo producer — REMOVED (2026-06-11 cleanup).**
  History: `SnapfifoSink` (`snapfifo.rs`) shipped as the outputd-as-producer
  tap, commit 9102e13 unwired it when TTS ingress moved into `jasper-fanin`
  (re-wiring would have leaked the leader's TTS to followers, inv-3), and the
  canonical design then moved the producer to the leader's *CamillaDSP*
  feeding the pipe — so the struct, its tests, the reconciler's inert tap-env
  write + `outputd_tap_action` change-gate + try-restart limb, the unit's
  optional tap `EnvironmentFile=`, the `SNAPFIFO_PRODUCER_WIRED` mirror flag,
  and `jasper-doctor check_grouping_tts_separation` were all deleted together
  ("Stranded by this design", §2). Two design properties to CARRY FORWARD into
  the Increment 3–5 producer: **off = zero cost** (no consumer / thread / open
  on a solo speaker) and **never back-pressure the DAC loop** (bounded,
  drop-on-full). The reconciler now manages ONLY the snap units — it never
  touches outputd (which has `StartLimitAction=reboot`; not restarting it on
  bond changes is a resilience win the cleanup banked). A bonded leader's
  runtime health honestly reads `degraded` ("no music producer feeds the
  snapfifo") until the real producer lands. **SHIPPED:** inv. 5
  (`rate_adjust=false`) and the channel-split live weave (§2/§4).
- **`jasper-fanin` music-only output (Increment 1 — the inv-2 producer half +
  the standalone inv-3 leak fix)** — `JASPER_FANIN_MUSIC_OUTPUT_PCM` (off by
  default). When set, `mixer.rs` `step()` writes a SECOND output per period: the
  program post-duck, **pre-TTS** (`write_music_only` — a lossy, non-blocking,
  period-aligned drop-on-full side-tap, so it can NEVER back-pressure the primary
  output, inv-1). Best-effort open (a bad/unopenable PCM logs
  `event=fanin.music_output.open_failed` and degrades to solo, primary path
  untouched); STATUS gains a `music_output` block
  (`enabled`/`pcm`/`frames_written`/`drops`). This is the corrected inv-2
  design's separation point (keep TTS in fanin, tap music pre-TTS) AND the
  standalone inv-3 fix. **Not yet consumed** — wiring it to snapserver (the
  leader round-trip) is Increment 2, and `SNAPFIFO_PRODUCER_WIRED` stays `False`
  until then. Verified on-device: 57 fanin unit tests green + clean warning-free
  build on ARM/ALSA (jts3); the second-output AUDIO is exercised in Increment 2
  on the pair.
- **systemd units** (`deploy/systemd/jasper-{snapserver,snapclient,
  grouping-reconcile}.service`) — disabled by default, in
  `jts-audio.slice` (`MemorySwapMax=0` inherited), no CPU caps,
  anti-storm `Restart`/`StartLimit`. `jasper-snapclient.service` also carries
  a narrow `LogRateLimitBurst=30` per 60 s so a follower whose leader is
  powered off remains visible as degraded without filling the persistent
  journal with one refused-connection line per second.
- **Reconciler `reset-failed`s before every deliberate restart
  (config-apply ≠ crash).** `_restart_unit` runs `systemctl reset-failed
  <unit>` before each restart it issues (outputd / `jasper-aec-reconcile`→voice
  / shairport / snap units), so a rapid burst of `/grouping/set` applies — e.g.
  an active-crossover calibration/trim/delay sweep on the leader re-fanned to a
  follower — can never spend a reboot-budget unit's `StartLimitBurst` and
  escalate to `StartLimitAction=reboot`. Genuine crash loops still escalate (the
  daemon's own `Restart=` path does not `reset-failed`, so only deliberate
  reconciler restarts are exempted). Generalizes the outputd-only guard and
  mirrors `grouping_supervisor.kick_reconciler` +
  `shairport_supervisor.restart_shairport`. **Root incident:** 2026-06-24
  jts.local (bonded follower) took six `/grouping/set` POSTs from the leader in
  44 s — each restarting `jasper-outputd` — and rebooted on outputd
  start-limit-hit. Pinned by `test_restart_unit_resets_failed_before_restart`
  (+ fail-soft siblings) in `tests/test_multiroom_reconcile.py`. The matching
  audio-thrash fix now lives at the `/grouping/set` kick site in
  `jasper.control.server`: the first write still kicks promptly, later writes
  inside the 60 s window write the remaining delay to `/run/jasper-control/`
  and start one on-demand `jasper-grouping-reconcile-trailing.service`, which
  sleeps for that delay and then starts the existing oneshot reconciler. Because
  the reconciler re-reads `grouping.env`, a delay/crossover sweep applies the
  final value exactly once after the burst even if `jasper-control` exits before
  the trailing kick. Pair-balance trim is the exception: trim-only
  `/grouping/set` writes persist `grouping.env` but bypass the 60 s reconciler
  cooldown through `jasper.multiroom.runtime_balance` (CamillaDSP
  `pair_balance_trim` patch on active endpoints; `jasper-outputd`
  `SET_DAC_CONTENT_TRIM_DB` on passive endpoints). If the live apply fails,
  `/grouping/set` falls back to the trailing reconciler path and reports the
  scheduled state in its response.
  Hardware-free coverage:
  `test_grouping_set_delay_burst_coalesces_kicks_and_applies_last_env`,
  `test_grouping_set_trim_only_live_applies_without_reconciler`, and
  `tests/test_multiroom_runtime_balance.py` (+ trailing-service scheduler tests)
  in `tests/test_control_server.py`.
- **`deploy/install.sh`** — `migrate_grouping` (seed/strip env) + unit
  install (not enabled) + `--dry-run` line.
- **`jasper-doctor`** — `check_grouping` (ok off / ok on-valid / warn
  on-invalid).
- **spike harness** — `scripts/multiroom-spike.sh` +
  `multiroom-spike-measure.py` (§8 P0; run on hardware).
- **`/rooms/` — the combined "Speakers" surface** —
  `jasper/web/rooms_setup.py` + `deploy/assets/rooms/` (port 8785,
  `JASPER_ROOMS_WEB_PORT`, route `/rooms/`; no legacy `/peers/` route).
  Directory + wake-response toggle on one page ("my other speakers" is one
  household concern). Lists every JTS speaker on the LAN via the always-on
  `_jasper-control._tcp` mDNS service (NOT the wake-peering-gated
  `_jasper-peer._udp`, so it works regardless of peering state), each a
  click-through to that speaker's own hostname-derived
  `http://<hostname>.local/system/` URL, plus this speaker's grouping status
  (off/solo, or role/channel/bond/buffer/codec,
  fail-loud `error` when on-but-invalid). `GET /` is a static
  `canonical_page()` shell + ES module; `GET /rooms.json` carries the data
  (self block now includes a `peering: {enabled, primary}` wake-response
  block, read fresh via `jasper.peering.config`); self is
  excluded from `peers`. **Six POSTs.** (1) `/peering`, the wake-response
  toggle (CSRF via `X-CSRF-Token`; read-modify-writes `peering.env` through
  `jasper.peering.config.PEERING_ENV_FILE`, preserving `JASPER_PEER_ROOM`;
  restarts voice + control). (2) `/bond`, **the Sonos-style one-flow
  stereo-pair setup**: the primary browser flow sends only `peer_addr`; the
  server owns the member plan, mints a `bond_id`, and fans the grouping config
  out SERVER-side to each member's `jasper-control /grouping/set` (this speaker
  → leader/left, the picked one → follower/right). Existing advanced callers
  may still send the explicit member list for same-bond edits. The follower's
  `leader_addr` is set to the leader's
  **stable mDNS `.local` handle** (survives the leader's DHCP IP churn — see
  the reconcile bullet above). (3) `/unbond`, **dissolve the bond**: the
  server reads each member's current grouping via `GET /grouping` to
  discover bond membership, then fans `{enabled:false, trim_db:0.0}` to the
  matches plus self. (4) `/swap`, **exchange the pair's left/right
  channels** (the
  speakers stay put; each plays the other side): same discovery as
  `/unbond`, then requires EXACTLY one reachable same-bond peer and a
  {left,right} channel set — roles/bond_id/leader_addr are preserved,
  only `channel` flips, and each member's reconciler re-points its
  outputd ChannelPick. A mono or >2-member bond 400s (no well-defined
  swap). (5) `/trim`, pair balance: the primary page sends `target=pair`
  + signed `balance_db`, and the backend rewrites both member trims
  attenuate-only. (6) `/mains-highpass`, the advanced wireless-sub bass
  management toggle for an existing same-bond member list. The rooms-page
  button rides the bonded card next to Dissolve.
  Configuration is automatic — no per-speaker tinkering. The
  bond/unbond fan-out runs **concurrently** across members (one slow/absent
  peer doesn't serialize the rest). An SSRF guard limits cross-speaker
  POST/GET targets to private/loopback IPv4 and rejects bare hostnames (see
  §7 "Grouping control plane — threat model"); audio flows end-to-end since Increment 5 PR-1 (leader CamillaDSP → snapserver pipe → member snapclients → outputd dac_content), passive-member local TTS works since PR-2, and active endpoints keep TTS on fan-in upstream of their crossover/protection graph (see HANDOFF-distributed-active); runtime health reads the live truth (active camilla config + snapcast client bindings). Untrusted mDNS
  fields never enter the server HTML (the shell is data-free; data ships as
  `application/json` and the module renders it via DOM/text APIs).
  On `jasper-control` itself the grouping HTTP surface is `POST
  /grouping/set` (validates via the shared `validate_grouping` before
  persisting — same rule the config loader applies on read) and the new
  CSRF-free **`GET /grouping`** read (the same no-auth LAN surface as
  `/state`; fail-soft to `null`, never 500), which the unbond flow uses for
  membership discovery. **Friendly names + identity:** each speaker
  advertises its `/speaker` display name as a `name=` TXT on
  `_jasper-control._tcp`, rendered by `jasper/control_advert.py` from
  `deploy/avahi/jasper-control.service.template` (purely additive vs. the
  static service; XML-escaped; fail-soft). The advert also carries a
  `peer_id=` TXT (the stable `/var/lib/jasper/peer_id` identity, via
  `read_identity()`) — the handle the bond-forming UI should PIN leaders
  by when it lands, resolving peer_id → current address at use time
  instead of storing a hostname that an Avahi collision rename can
  silently repoint at a different speaker (mDNS is unauthenticated:
  treat peer_id as a stable handle, confirm trust-sensitive operations
  over HTTP — see docs/HANDOFF-identity.md). The directory renders
  peers and self by the friendly name. The self block now resolves name/room/hostname through
  the single identity reader `jasper/identity.py` (`read_identity()`); the
  shared one-shot browse is `jasper/mdns.py` (`browse_once`) and the one
  Avahi `*.service` renderer is `jasper/avahi_service.py` (`render_service`,
  used by both control_advert and peering). **The room label now lives in the
  speaker-identity home** (`jasper/speaker_name.py`, `JASPER_SPEAKER_ROOM`;
  `/speaker` writes it; `install.sh migrate_speaker_room` seeds it from the
  legacy peering room) — `JASPER_PEER_ROOM` remains only as a compatibility
  fallback for older peering env files. See §8 "Friendly names + identity".

Not yet built (P1+, post-spike): the `BondedSet` entity, satellite
calibration, **arbitrary >2-member multi-room bonds on `/rooms/`** (the
stereo-pair one-flow landed — `/bond` fans config out to all members —
and 2026-06-23 added an "add a subwoofer to a pair" flow [a 2.1 system:
left + right + sub] backed by the N-member `JASPER_GROUPING_ROSTER`; what
remains is a general multi-member channel/leader picker for groups beyond
pair-plus-sub), the
**leader's own snapclient → outputd content lane** (§2 inv. 2 — so the
leader plays the *buffered* stream in sync with followers, not its direct
unsynced output), and the on-device end-to-end + acoustic sync validation.
*(The producer `SnapfifoSink` + the reconciler tap env are WRITTEN but
**unwired** — 9102e13 removed outputd's reader, so enabling a leader does
**not** stream to followers yet (it reads `degraded`); re-wiring is blocked on
TTS separation, and sample-lock additionally needs inv. 2. See the §0 intro +
§2 "inv-2 realization.")* **SHIPPED since:** inv. 5
(`rate_adjust=false`, §2) AND the **live weave of the channel-split
fragment into the active config** — `weave_channel_split()`
(`channel_split.py`) splices the `channel_select` mixer + sub crossover
into the generated config (validated YAML; `stereo` is byte-for-byte
passthrough), and `emit_sound_config(channel_split=…)` weaves it on the
live `/sound` apply path for an active member.

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
- **Transport endpoint** — a cheap **Raspberry Pi Zero 2 W + DAC**
  running the JTS control plane and a synchronized audio client. No
  voice, no renderers, no room/content DSP. Exists because a second Pi 5
  is too expensive to be a right-channel speaker, and because a
  wireless sub has to be cheap.
- **Driver-DSP endpoint** — planned variant for an active satellite that
  drives local woofer/tweeter amps. It may run local CamillaDSP for
  crossover/protection because that is hardware safety at the DAC, but
  the leader still owns sources, room/content DSP, grouping, and voice.

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

> **Note (2026-06-27):** until the Stage-0 latency fix, the configured
> `buffer_ms` was **inert** — it was passed as a `pipe://…&buffer_ms=`
> *source-URL query param*, which snapcast silently ignores, so every bond
> actually ran snapcast's **1000 ms** global default regardless of the
> configured value. It is now routed through the global `--stream.buffer
> <ms>` flag (`reconcile.py:snapserver_argv`), so the configured value
> finally takes effect. Any on-device buffer-sizing observation recorded
> *before* this fix was made against the 1000 ms default, not the value in
> the config — re-measure before trusting earlier `buffer_ms` conclusions
> (including the §8 spike numbers).

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
FIFO; `snapserver` reads it as a `pipe` input. Tapping *after* the
clamp is what makes the streamed audio inherit JTS's hardware-safety
ceiling (§7). **Built but UNWIRED (`SnapfifoSink` — see §0):** the consumer
exists, but its `main.rs` writer thread + `JASPER_OUTPUTD_SNAPFIFO_PATH` gate
were removed by 9102e13 (TTS moved to fanin), so it moves no audio today
(`SNAPFIFO_PRODUCER_WIRED = False`); when re-wired it sits off-by-default behind
that gate. The FIFO lives at
`/run/jasper-snapserver/snapfifo` (snapserver's own `RuntimeDirectory`,
the reconciler's canonical `SNAPFIFO` — *not* the bare `/run/jasper/`
of an earlier draft, which would collide with `jasper-voice`'s sockets;
see `reconcile.py`).

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
   CamillaDSP runs `rate_adjust=false` / no resampler. **SHIPPED:**
   the rule is one of two transforms in the grouping **member-config
   policy** (`jasper/multiroom/member_config.py`
   `member_camilla_kwargs` — `is_active_member` decides; the other
   transform is the channel-split), applied identically on EVERY
   config path (`/sound`, `/correction`, and — when it lands — the
   inv-2 reconciler), never threaded per call site. `jasper-doctor`'s
   `check_grouping_rate_adjust` is the universal backstop — it reads
   the ACTIVE config, so it catches every generator and a config
   generated *before* the bond formed (stale → warns to regenerate).
   (JTS already documented that `rate_adjust` + `AsyncSinc` together
   oscillate.)

### Canonical signal flow — THE target architecture (DECIDED 2026-06-10)

This is the **authoritative** target. It was settled after a prior-art research
pass (Roon, Sonos, Music Assistant, Snapcast, Squeezelite/LMS, PipeWire) plus an
owner decision: **the brainy leader bakes ALL per-channel content correction; the
other speakers are transport receivers** — channel-droppers, no room/content DSP.
Driver DSP is the local-hardware exception: an active satellite that physically
drives woofer/tweeter amps needs its own crossover/protection graph on that box.
The "inv-2 realization" subsection below is kept as design archaeology; **where it
disagrees, THIS section wins.** RAM target for the transport path: a **1 GB Pi
leader with headroom — no second content-DSP CamillaDSP.**

**The shape, in one breath:** the leader's *one* CamillaDSP bakes a stereo program
where the **left channel is corrected for the leader's seat and the right for the
follower's seat**, writes it to a **pipe**, and `snapserver` streams that *single*
stereo stream to everyone. Each passive speaker — including the leader's own
localhost snapclient in the dumb stereo-pair shape — **drops the channel it
doesn't play** with a 3-line ALSA `route` (`ttable`) plug. Passive members mix
their own **voice/TTS** back in **low-latency at the final output stage**
(`jasper-outputd`), after the synced round-trip. Active endpoints are the safety
exception: their TTS stays upstream of the local crossover/protection graph; the
ratified active-speaker shape lives in HANDOFF-distributed-active.

```
SOLO (today, unchanged):
  renderers → fanin (music + TTS) → CamillaDSP (correct) → outputd → DAC

LEADER (stereo pair):
  renderers → fanin (MUSIC ONLY — JASPER_FANIN_MUSIC_OUTPUT_PCM, Increment 1)
            → CamillaDSP   (bake per-channel: L=leader-seat, R=follower-seat;
                            volume_limit:0.0 clamp; ONE instance)
            → pipe (FIFO)  → snapserver  (ONE stereo stream; Snapcast owns rate)
                ├─ leader localhost snapclient (-h 127.0.0.1) → ALSA ttable drop→L
                │     → outputd  (passive pair: mix leader TTS here) → DAC
                └─ follower snapclient → ALSA ttable drop→R → DAC   (no TTS)

FOLLOWER (transport): snapclient → ttable drop→its channel → DAC.
FOLLOWER (driver-DSP, planned): snapclient → local driver crossover/protection
                                → DAC(s)/amps. Still no voice/source brain.
```

**Three load-bearing decisions, each from prior art:**

1. **One stream + client-side channel-drop — NEVER separate L/R streams.** Snapcast
   sample-locks clients only *within one group on one stream*; separate
   streams/groups drift independently (maintainer-confirmed, snapcast#747). A
   phase-coherent L/R pair therefore *requires* a single stereo stream, each client
   dropping its unwanted channel via ALSA `route`/`ttable` (`ttable.0.0 1` = play
   left; `ttable.1.0 1` = play right). This is exactly what Music Assistant ships as
   a per-player "Left/Right/Mono" toggle. The receiver is a channel-picker — **the
   entire "dumb endpoint."**

2. **One content-DSP CamillaDSP on the leader bakes per-channel correction;
   transport receivers have none.**
   CamillaDSP applies a *different* filter to L vs R natively in one config (a
   `Filter` pipeline step per `channels: [0]` / `[1]`), ~1 % of a core, a few MB.
   This is the Roon model (DSP on the Core, per-zone; dumb RAAT endpoints). It
   **deletes the second content-DSP / per-follower room-DSP RAM cost entirely.**
   CamillaDSP writes to a **pipe**, not an ALSA device — which makes decision 3
   free. This does not prohibit the separate driver-DSP endpoint profile, whose
   local CamillaDSP exists only to protect and route the drivers attached to that
   endpoint's DACs.

3. **Snapcast owns output rate; CamillaDSP can't fight it — by construction.**
   `enable_rate_adjust` is unsupported on CamillaDSP's `File`/pipe backend (no output
   clock), so the only rate-tracking on the streamed path is Snapcast's own
   sample-stuffing. CamillaDSP's one rate job is its **capture** side — tuning the
   snd-aloop *input* loopback clock (HEnquist's bit-perfect method, no resampler).
   Three non-overlapping clock domains → the documented "`rate_adjust` + `AsyncSinc`
   oscillate" trap cannot occur here.

**Voice stays local (inv-3; confirmed by Music Assistant).** Conversational TTS is
low-latency and must not ride the ~buffer-delayed synced stream. For a passive
leader/member, TTS routes to **`jasper-outputd`** (post-round-trip) rather than
into fanin's pre-stream music. This intentionally re-introduces an outputd TTS
mix for passive bonded roles — 9102e13 retired it for the *solo* case
(fanin-mix is simplest there); a sample-locked passive member needs a
post-buffer mix point, and outputd is the final output owner. For an active
endpoint, outputd's 2-channel post-crossover TTS mixer is unsafe, so TTS stays
on fan-in upstream of the local crossover/protection graph
(HANDOFF-distributed-active "active-leader TTS band-limiting"). Group-wide
*announcements* (a timer ringing everywhere at once) MAY later ride the
buffered stream ducked (MA's model) — a separate feature, not conversational
TTS. Followers never receive TTS unless they are local voice-capable passive
members responding for themselves.

**Two invariants the build MUST hold (added 2026-06-10 after adversarial design
review — these are the design, not details):**

- **inv-A — the AEC reference must stay `== final DAC content`, TTS-inclusive,
  post-round-trip.** JTS hears "Hey Jarvis" *over* music by subtracting its own
  output from the mic, using the exact bytes outputd hands the DAC as the AEC
  reference (`ref_outputs.publish(&content_buf)` in `run_alsa`, fed to BOTH the
  software AEC3 UDP monitor AND the **chip-AEC** XVF USB-IN — the recommended
  profile). The leader design delays the music (round-trip) and splits voice onto a
  separate path, so the reference is correct only if, on a leader, the tap stays at
  outputd's **final post-mix** buffer containing **(i)** the round-tripped,
  snapclient-paced music AND **(ii)** the leader's TTS, summed **before** the
  `publish()` tap. If TTS mixes *after* the tap, the speaker's own voice bleeds into
  the mic and false-fires wake / breaks barge-in (the `tts_bleed` class).
  **Consequence:** routing leader TTS to outputd is a **REBUILD** of the outputd TTS
  mixer + the barge-in `audio_played_ms` ledger that 9102e13 retired (consolidated
  into fanin) — net-new Rust in a reboot-on-fail daemon, not a flag flip.
  **Prior (recalibrated 2026-06-11 after two external research passes + a re-read of
  [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md)): the gate SHOULD pass.** Per that
  doc's three-clock framework, the round-trip never enters the reference path: the
  tap is **downstream** of snapclient's sample-stuffing (an edit appears identically
  in reference and emitted audio, so it self-cancels), outputd's push to the chip
  stays paced by its own DAC-blocking loop, and mic + chip USB-IN + the sync-mode USB
  DAC all ride the one USB-SOF domain (measured ~1 ppm / converged on hardware).
  Stuff events are rare — order once per tens of seconds; the rate is set by the
  system-vs-DAC clock pair, so verify it empirically, but each event is a single
  smoothed 20.8 µs edit. We keep the gate anyway: the chip doc's own twice-corrected
  history is the standing warning against trusting architecture over measurement.
  **HARDWARE GATE (blocks the self-loop increment) — DELTA + PRODUCT criteria, NOT an
  absolute dB bar:** (a) bonded-leader chip-AEC ERLE ≈ the solo baseline within
  ~1–2 dB, measured over several minutes (long enough to capture ≥10 stuff events and
  any re-convergence dips); (b) wake FRR + barge-in success during loud music on the
  bonded leader — the real pass/fail, with ERLE only the proxy. **Do NOT gate on an
  absolute threshold** (an earlier draft nearly banked "ERLE > 20–25 dB"): our
  working production rig measures ~14.5 dB *linear-AEC residual*, which is normal
  (Amazon patent US 10,586,534 puts steady-state ERLE at ~15–25 dB; the 30–40 dB
  literature figures are far-field/post-beamforming and don't transfer) and total
  system suppression is higher after the chip's beamformer + post-filter. A gate that
  fails the working solo speaker is a miscalibrated gate. **Fallback ladder if the
  gate fails** (ranked; build on measured need only): (1) partial music ducking on a
  wake *candidate* (cheap, lifts SER/NER directly — noted, not built; today's
  duck-on-session-open already ships); (2) the software beamformed-reference (ARA)
  fallback — design recorded in
  [CHIP-AEC-EXPERIMENT.md](CHIP-AEC-EXPERIMENT.md) "Beamformed-reference (ARA)
  fallback" (on-chip is impossible; spatial nulling is indifferent to the driver
  nonlinearity that caps linear ERLE); (3) conservative residual suppression only —
  aggressive suppressors distort the near-end speech the speech-to-speech LLM
  consumes; (4) inv-B's direct-playback bypass for the leader's own channel (zero AEC
  risk, loses sample-lock) or accept "no wake-during-music on a bonded leader." No
  free lunch at rung 4 — the build picks one and owns it.

- **inv-B — the leader self-loop must NOT be a single point of failure for the
  leader's OWN music.** The leader plays its own channel via a localhost snapclient;
  if that client / snapserver / the pipe stalls (routine ALSA underruns on a Pi) the
  brainy speaker would go **silent on its own music** — even "bonded but alone."
  That breaks the no-silent-failure rule for the *leader itself*. Required: when the
  self-loop is unhealthy for N periods, outputd **falls back to the direct fanin
  music path** (un-synced) rather than silence — a momentarily-unsynced pair beats a
  silent leader. This **inverts inv-1** for the leader's own playback: a starved
  FIFO reading as silence is correct for a *follower*, NOT for the brainy leader
  hearing itself. Surface as a cue + `/state` flag + dashboard card; §7's failure
  table gains a "leader self-loop degraded" row.

**Per-speaker correction of a dumb follower — PEQ, open-loop, fast-follow.** To
correct the follower's seat, the leader holds the follower's measured room profile
and bakes it into the right channel (Roon's per-zone model). Measurement is
open-loop (play a sweep through the follower's stream, capture at the seat with a
mic); because mic and speaker are on independent clocks, absolute delay isn't
trustworthy, so the correction is stored as **parametric EQ (biquads), not FIR
convolution** (cheap, magnitude-only — all an open-loop capture earns; also what
Sonos Trueplay ships). V1 may launch with BOTH channels on the leader's own
correction; independent per-follower calibration is the fast-follow. (No product
does "measure remote + apply on transmitter" as one flow — it's our integration of
Roon's apply-half + Trueplay's measure-half.)

**RAM budget (the whole point).** Added on the leader vs solo: `snapserver`
(~low-tens MB) + one localhost `snapclient` (~5 MB) ≈ **~20–35 MB, no second DSP.**
Followers add ~5–10 MB on their own Pis. Measure on-device before trusting — the
component numbers are from research, not a measured stack.

**Build mechanics the research nailed down (bank these):**
- The leader's localhost client **must** use `-h 127.0.0.1` (dodges an mDNS/IPv6
  boot race, snapcast#715) and a per-client `--latency` trim (nulls fixed DAC-path
  offset between speakers — cheap insurance even with identical hardware).
- Prefer the **pipe** source over an `alsa://` capture source: avoids a second
  snd-aloop *and* the snapserver+loopback idle-delay bug (snapcast#1014). Mind
  `fs.protected_fifos` if the FIFO lives in a world-writable dir.
- Codec/chunk pairing: snapserver's default `codec=flac, chunk_ms=20` is documented
  non-optimal — FLAC wants ~26 ms chunks (and adds ~26 ms codec latency); plain PCM
  has zero codec latency at ~1.1 Mbps stereo (trivial on home WiFi). Our spike
  measured FLAC ≈ PCM on RAM/CPU, so pick by WiFi headroom: PCM first, Opus if
  bandwidth-constrained, FLAC only with chunk_ms raised.
- Three timing concepts, two calibration knobs: Snapcast's sync loop owns the
  distributed clock/transport problem; snapclient `--latency` / Snapcast
  `Client.SetLatency` nulls fixed whole-client *PCM/DAC/backend/output-path*
  latency; a **per-channel `Delay` baked in the leader's CamillaDSP** aligns
  *acoustic arrival* at the seat (L and R can be different distances from the
  listener). Measure first: colocated/electrical endpoint baseline belongs in
  Snapcast client latency; listening-seat arrival delta belongs in leader-side
  CamillaDSP channel delay. See
  [`research/balance-sync-calibration.md`](research/balance-sync-calibration.md).
- Pin **48 kHz / S16** end-to-end (snd-aloop locks format/rate on first open).

**Solo-impact contract (hard requirement for EVERY increment — owner-stated
2026-06-11):** a speaker that is NOT in a bond must be unaffected by this feature
existing. Concretely: **zero added latency** (solo keeps the direct
fanin→CamillaDSP→outputd→DAC path — the playout buffer is paid ONLY by bonded
members); **zero added resource use** (no snapserver / snapclient / FIFO / tap /
thread / PCM open on a solo speaker — off-by-default knobs do no per-period work
beyond a none-check); **zero behavior change** (byte-identical generated CamillaDSP
configs and byte-identical daemon I/O when the feature is off). **Enforcement, not
intention:** every increment ships its solo-path proof as a regression test —
golden-YAML tests for config generators (default args reproduce today's output
byte-for-byte), default-config tests for daemons (feature env unset → no
construction) — and §7's "Solo (N=1), grouping off — zero cost" row is the
acceptance criterion at deploy time. Increment 1 already follows this shape
(`JASPER_FANIN_MUSIC_OUTPUT_PCM` unset → no second PCM, no work) — every later
increment holds the same bar.

**Increment plan (RE-SCOPED 2026-06-10 after review — hardware-free / non-silencing
slices FIRST; nothing that can silence the leader ships before inv-A's hardware
gate). The earlier "2a = followers play" was NOT an honest slice: repointing the
leader's CamillaDSP to a pipe leaves its DAC with nothing to read → a silent leader
until the round-trip exists, so 2a secretly dragged in the outputd rework.**
- **Increment 1 — DONE** (`JASPER_FANIN_MUSIC_OUTPUT_PCM`): the music/voice split,
  the foundation. *Repurposed* — its music-only signal feeds the leader's pre-stream
  CamillaDSP, not snapserver directly.
- **Increment 2 — per-channel correction axis — ✅ BUILT (2026-06-11; pure Python,
  zero audio-path activation).** `emit_sound_config(room_peqs_right=…)`: ONE
  config, channel 0 corrected for the
  leader's seat, channel 1 for the follower's (only the ROOM segment is per-channel;
  preference EQ stays shared taste). Solo contract held by construction: `None`
  (default) is **byte-identical** to the pre-axis output (regression-locked,
  including an exact pipeline-bytes test); `[]` is distinct and bakes a FLAT right
  segment (uncalibrated followers ship flat — Increment 6's rule). The config
  extractor stays deliberately blind to `*_r*` filters (the leader apply path
  composes from STORED profiles — Increment 5). No callers pass the new params yet;
  the reconciler wires them in Increment 5.
- **Increment 3 — outputd second content-input (FIFO reader) — ✅ BUILT
  (2026-06-11; off by default, zero solo cost).**
  `rust/jasper-outputd/src/dac_content.rs` (`DacContentSource`), gated on
  `JASPER_OUTPUTD_DAC_CONTENT_FIFO` (+ `…_CHANNEL`, the channel-split vocabulary —
  needed because snapclient's `--player file` has no ALSA hop for the member's
  `ttable` drop; left/right duplicate, mono = clip-safe average, unknown values
  fail loud). What shipped: lazy non-blocking FIFO reader (inv-1 — the DAC write
  stays the sole pacer; a missing FIFO is one cheap retry per period), the
  **inv-B fallback mechanics** (starts in fallback; the FIFO must demonstrate
  health for ~210 ms before serving; starvation falls back the SAME period —
  zero silence; damped recovery so the DAC never flaps between two time-offset
  copies), bounded staging (~170 ms cap, oldest-period overflow drops), a
  bounded drain of the direct lane while the FIFO serves (an upstream loopback
  writer can never stall on a full ring), and the **self-reported STATUS
  `dac_content` block** (enabled/fifo/channel/serving_fifo/periods/transitions/
  recoveries/staged/overflow/failures — daemon truth per the
  SNAPFIFO_PRODUCER_WIRED lesson). Fail-loud config guards: rejects combination
  with the rate-match content bridge and with the dual-Apple sink. No reconciler
  wiring yet — the lane activates in Increment 5.
- **Increment 4 — acoustic-sync confirmation — FOLDED into the Increment 5
  bring-up (2026-06-11).** The owner exercised the product bond flow directly
  (the /rooms UI), so the throwaway bench rig is moot: the acoustic L/R
  validation happens on the REAL path (PR-1's dataplane) during the
  supervised bring-up session, alongside the inv-A ERLE delta + inv-B
  fallback checks.
- **Increment 5 — the big slice, staged as two PRs. PR-1 (music dataplane) ✅
  BUILT 2026-06-11; PR-2 (TTS rebuild + supervisor) ✅ BUILT 2026-06-11.**
  **PR-1 (built):** leader CamillaDSP→pipe (the `playback_pipe_path` emitter
  axis + the bonded config swap in `jasper/multiroom/leader_config.py`,
  reusing the wizards' shared `apply_dsp_config` engine + the ONE member
  policy `member_camilla_kwargs` — a /sound save while bonded REGENERATES
  the pipe config instead of silently un-bonding camilla); the round-trip
  FIFO + every member's snapclient on the `file` player (never an ALSA
  sink — the raw-DAC fight was the observed pre-Inc-5 failure); the
  reconciler role wiring (outputd lane env + restart-on-change, member
  FIFO, load-bearing apply order, solo-restore unwind ladder with a
  persistent VALIDATED-SOLO prior-config stash (a pipe-shaped wizard
  config — e.g. sound_current.yml regenerated while bonded — is never
  stashed NOR restored; restore falls through to re-emit-solo)); runtime health reads producer liveness
  from the ACTIVE camilla config (daemon-adjacent truth). **Snapcast
  registry (current truth, post-#619/#620):** snapcast PERSISTS
  group→stream assignments in server.json, and snapserver ALSO registers
  the packaged `snapserver.conf` "default" pipe source — so a stale
  binding can point at a stream that EXISTS (idle, producer-less) and
  the client plays zeros behind green health. The reconciler therefore
  pins bindings by an **ownership rule** on every leader reconcile:
  a group survives iff bound to a JTS-owned stream
  (`{SNAP_STREAM_ID} | allowed_streams` — extend the allowlist when a
  second JTS stream, e.g. group announcements, lands); anything else is
  re-bound, connected or not. Runtime health independently verifies the
  LIVE picture (every connected client on our stream + audible + the
  leader's own client present; snapserver RPC unreachable ⇒ explicit
  degraded). /state reads the probe through a 5 s TTL cache (failures
  cached — a hung snapserver costs one 1 s timeout per window, not one
  per dashboard poll); the doctor deliberately probes fresh.
  **Design note:**
  the leader's camilla keeps capturing lane 7 (`jasper_capture`) — all 8
  loopback substreams are allocated, and PR-2's TTS socket flip makes lane
  7 music-only BY CONSTRUCTION while bonded, so Increment 1's fanin music
  tap is NOT used by this design (it stays available for future group
  announcements). **Reboot-loop chain-breaker (post-merge review,
  MEASURED):** camilladsp 4.1.3 exits CLEAN (0) when its File sink path is
  absent, and blocks un-SIGTERM-ably in open(2) when the FIFO has no
  reader — with jasper-camilla's `Restart=always` + `StartLimitBurst=5/60s`,
  a snapserver hard-death while bonded would exhaust Camilla's recovery
  budget in under a minute. `jasper-camilla-pipe-guard`
  (`ExecStartPre=-`, pure bash, fail-open) repairs the statefile to the
  base config BEFORE camilla launches when the bonded pipe is dead
  (absent FIFO, or no reader via a write-open probe) — prevention, not
  reaction, because an `OnFailure=` rescue would RACE the reboot. Camilla
  then runs solo-safe (identity EQ, volume_limit intact); grouping.env
  stays bonded so the next reconcile re-applies the bond when snapserver
  is healthy; `event=camilla_pipe_guard.*` + the `leader pipe` doctor
  check surface the degraded state.
  **PR-2 (built 2026-06-11):** outputd grew a TTS server
  (`rust/jasper-outputd/src/tts.rs`) speaking fanin's exact newline-framed
  wire protocol (GAIN / PREPARE_ASSISTANT / SEGMENT_* / AUDIO /
  PROGRAM_DUCK_* / CONTENT_METER_* / FLUSH / FLUSH_SYNC / CLOSE — the twin
  is deliberate: Python keeps ONE playout implementation), feeding the
  surviving `OutputCore` engine (assistant segments, loudness profiles,
  saturating mix, `PlayoutLedger`); the `FLUSH_SYNC` barge-in ack now
  carries DAC-true `audio_played_ms` + per-segment events from
  `commit_prepared_period_with_dac_delay`. fanin's solo ack now carries its
  own per-segment playout ledger too (`rust/jasper-fanin/src/playout.rs`),
  but a mix-commit estimate that over-reads by the downstream pipeline
  depth; outputd's port is the DAC-true one. PASSIVE bonded non-sub members
  flip voice's TTS socket to their own outputd (inv-3: the leader's TTS never
  enters the shared stream — each speaker's OWN replies mix locally,
  post-round-trip, pre-reference, which is exactly inv-A's tap requirement;
  `PROGRAM_DUCK` rides the same socket, so ducking is member-local too).
  Active endpoints deliberately do not arm that socket; they keep TTS on
  fan-in upstream of CamillaDSP, where it is split/protected by the active
  graph. Wireless sub followers are parked and keep outputd TTS unarmed so
  full-range speech never reaches the sub. The reconciler derives the route
  matrix in `jasper.multiroom.tts_route.expected_grouping_tts_route`, then
  writes `JASPER_OUTPUTD_TTS_SOCKET` in grouping-outputd.env and
  `JASPER_TTS_OUTPUTD_SOCKET` / `JASPER_GROUPING_VOICE_PARK` in
  grouping-voice.env as that matrix requires (solo/active endpoint OMITS the
  socket key — present-but-empty would break voice's fanin default; a fresh
  solo reconcile skips creating the empty file so first boot doesn't restart
  voice) — and `grouping: TTS lane` (doctor) catches drift between them,
  including the worst shape: voice targeting a socket outputd never armed
  (silent assistant). Solo speakers are byte-identical to pre-PR-2 (no
  socket env → outputd runs the exact prior period loop).
  **The supervisor (jasper.control.grouping_supervisor, built with PR-2):**
  every bonded **dumb** member polls outputd's `dac_content.serving_fifo`
  every 30 s (cold start 60 s); 3 consecutive starved polls → `reset-failed` +
  `restart --no-block jasper-grouping-reconcile` (rate-limited 1/10 min). An
  ACTIVE endpoint (active follower or active-speaker leader) feeds the DAC via
  the camilla#2 active-content lane, not the `dac_content` round-trip, so the
  reconciler disables `dac_content` there and the supervisor **skips** the
  starvation watch for it (keyed on `is_active_speaker_box()` — the same
  predicate the reconciler uses; its absence is correct, not starvation —
  otherwise it self-kicked the reconciler every window).
  The leader additionally re-runs the `ensure_groups_on_stream` ownership
  pin every poll, making binding read-repair continuous (a runtime rebind
  from any snapcast app self-heals in ≤30 s). Phase D of the control-plane
  auth work adds the matching grouping-plane self-heal: a rostered leader
  first reads the follower's `/grouping`; if that peer is absent or drifted,
  the supervisor POSTs the intended follower role back to `/grouping/set`
  with `X-JTS-Household` when this leader has a household secret. If no
  secret exists, the POST carries no fake header and the member's fail-safe
  bootstrap behavior decides the outcome; already-converged peers are skipped
  so their reconciler is not restarted on every poll. Surfaced at
  `/state.resilience.grouping_supervisor`; off-switch
  `JASPER_GROUPING_SUPERVISOR=disabled` (exact match, mirrors the
  shairport/system supervisors). Auto-unwind to solo is deliberately NOT
  built: tearing down a user-created bond is not a 30 s-poll decision;
  disband stays one tap on /rooms until a real non-converging failure
  shape is observed (the kick converges every silence class seen to date).
  **Spec note (Increment 6's backstop, updated):** when per-follower
  profiles exist, a doctor check asserts a bonded leader's active config
  carries right-channel correction (`room_peq_r*`) when the follower has a
  stored profile — the silent-wrong-config class now covered for PR-1's
  surfaces by `check_grouping_leader_pipe` (un-piped leader = silent
  stream) and `check_grouping_channel_pick` (outputd lane env drift =
  wrong channel).
- **Increment 6 — per-follower calibration (fast-follow), SAME-ROOM pairs only.**
  Open-loop sweep → PEQ → bake into the follower's channel. A multi-room satellite in
  a *different* room needs its own correction for **correctness** (the leader's room
  curve is wrong there) — ship those *flat* + a "calibrate me" nudge until this
  lands; do NOT apply the wrong-room curve.

**Sequencing + owner gates (2026-06-11 — where the remaining work runs and what
each step needs from the owner):**
- **Status:** Increments 1–2 SHIPPED (PRs #575, #587/#588 + the #591 cleanup);
  the P0 spike's resource + software-sync gates PASSED on hardware (2026-06-10,
  jts3↔jts). Remaining: 3 → 4 → 5 → 6.
- **Increment 3 (next; no owner time):** pure Rust, built + `cargo test`ed on the
  lab Pi over SSH without touching live audio; off-by-default, byte-identical solo
  (the solo-impact contract). Does not depend on Increment 4.
- **Increment 4 (~20 min WITH the owner at the speakers; before Increment 5's
  ship decision):** the acoustic half of the P0 gate — chirp at moderate volume,
  one mic between the pair, cross-correlate (`multiroom-spike-measure.py
  acoustic`). Working target p99 < 1 ms L/R, but the OWNER's ear in the OWNER's
  room is the real acceptance — the number is a working figure, not gospel.
- **Increment 5 (the integration; owner validation sessions):** every contract
  above gets cashed in on the jts3↔jts pair. Two hardware gates — inv-A
  (bonded-leader ERLE delta + wake-FRR/barge-in DURING music; needs the owner
  saying "Hey Jarvis" over playback) and inv-B (kill the loopback mid-song →
  leader falls back to direct, never silent). Two OWNER decision points: the
  acoustic-quality call from Increment 4, and the buffer-size trade-off (deeper
  = more WiFi resilience, more pause/resume lag on the bonded pair — choose with
  measured numbers). Deliverable: bond on `/rooms` → sample-locked music on both
  speakers, leader still answers instantly.
- **Increment 6 (fast-follow):** needs Increment 5 + one owner measurement
  session at the follower's seat.

**Stranded by this design — cleanup EXECUTED 2026-06-11 for the dead half:**
`SnapfifoSink` (deleted), `SNAPFIFO_PRODUCER_WIRED` + `effective_leader_tap_path`
(removed), the outputd-tap reconciler limb + the unit's optional tap
`EnvironmentFile=` (removed — outputd no longer produces the snapfifo),
and the doctor's `check_grouping_tts_separation` (folded into `check_grouping`'s
runtime detail) all assumed **outputd** feeds the pipe; the canonical design has
**CamillaDSP** feed it, so that machinery was dead **by design** and is now gone.
**Deliberately KEPT (live until Increment 5):** `channel_split.py` /
`member_config.py` / their doctor checks — they serve the currently-SHIPPED
member model (the live `/sound` apply path weaves the split for an active
member); Increment 5 replaces that wiring with the leader-bake axis (the
`room_peqs_right` × `channel_split` mutual-exclusion guard polices the
migration). Also kept: `desired_snapfifo_path` (the pure "this role needs a
producer" predicate driving the runtime-health derive) and snapserver's pipe
source (the consumer side is unchanged; only the producer moves). snd-aloop
substreams are **already exhausted (8/8)**, so the round-trip uses a raw FIFO
(which is why Increment 3 adds the outputd FIFO reader), not a new loopback.

**2.1 / sub — the channel-count call (resolve before 2.1, which §1 scopes into V1):**
a single *stereo* stream cannot carry L+R+sub. The sync-correct answer is a **single
3-channel stream** (L/R/LFE, each endpoint `ttable`-drops its channel) — which means
the "pin 48 k/S16 **stereo**" line above generalises to "one rate, channels = bond
channel count," and outputd's stereo AEC-reference contract must handle 3-ch. A
**second stream for the sub is rejected**: separate Snapcast streams drift
independently (hundreds of ms under WiFi jitter), and "sub sync is loose" covers
~10 ms phase, not inter-stream drift. So the **stereo pair (2-ch) is the first
deliverable; 2.1 is the 3-ch generalisation**, not a parallel stream.

> **Superseded for the SHIPPED dumb sub (2026-06-23).** The 3-ch L/R/LFE plan
> above assumed the sub needs a *dedicated* LFE channel on the wire. The shipped
> wireless sub doesn't: it rides the **existing 2-ch stereo stream**, picks a
> clip-safe **mono sum** of L+R, and low-passes it **receiver-side** in
> `jasper-outputd` (`ChannelPick::Sub`, LR4 at `JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ`).
> Because the sub derives its lows from the full-range L+R already on the wire,
> no LFE channel — and so no stream-format change, no second stream — is needed.
> Bass management stays symmetric: each passive main high-passes locally in
> outputd at the same bond `crossover_hz` (`JASPER_OUTPUTD_DAC_CONTENT_HP_HZ`,
> default-on `/rooms/` toggle), while the shared stream remains full-range for
> the sub. `outputd`'s stereo AEC-reference contract is untouched. The 3-ch stream
> stays the (still-unbuilt) answer **only** if a household ever needs a
> *sender-side pre-baked* sub (a cheap endpoint that can't run a local low-pass).
> Canonical: [HANDOFF-distributed-active.md](HANDOFF-distributed-active.md)
> "Subwoofer — two different subs" (gap 5, receiver-side default).

**Sources:** snapcast#747 (channel-drop is the way; separate streams don't sync) ·
Music Assistant Snapcast provider (Left/Right/Mono toggle) · CamillaDSP docs
(per-channel `Filter`; `File`/pipe has no rate-adjust; capture-loopback clock
tuning) · Roon KB (per-zone DSP on the Core; dumb RAAT endpoints) · Sonos Trueplay
tech blog (open-loop mic measurement → on-device PEQ) · snapcast#715 / #1014
(localhost-client boot race; loopback idle-delay).

### inv-2 realization — the leader content lane (SUPERSEDED — see "Canonical signal flow" above)

> **Status: SUPERSEDED by the "Canonical signal flow" section above (decided
> 2026-06-10, research-grounded). Kept as design archaeology only.** Two specific
> reversals: **(a)** the canonical leader plays its channel from the BUFFERED
> round-trip (sample-locked), not the direct fanin output this subsection assumed;
> **(b)** the leader's TTS re-joins at `jasper-outputd` (post-round-trip,
> low-latency) — this subsection's "avoid re-adding the outputd TTS path" is
> *reversed*, because a sample-locked leader needs a post-buffer mix point. The
> BLOCKER analysis below remains accurate about WHY the naive dual-read was wrong;
> read it for the reasoning, not the prescription.

> #### ⚠ BLOCKER — TTS is pre-mixed into the streamed program
>
> `jasper-outputd/src/lib.rs`: *"Assistant/TTS ingress is owned by
> `jasper-fanin`."* So on the live ALSA path (`run_alsa`), TTS is mixed into
> the music by fanin **upstream of outputd** — it is INSIDE the `content_buf`
> that outputd reads, writes to the DAC (`write_dac_period(&content_buf)`), and
> taps (`ref_outputs.publish(&content_buf)`). That breaks the dual-read in two
> ways:
>
> 1. **The DAC swap would drop the leader's assistant audio.** If the leader's
>    DAC reads the music-only buffered round-trip (`dac_content`) instead of
>    `content_buf`, the TTS fanin mixed into `content_buf` never reaches the
>    speaker — the leader goes *silent on the assistant* mid-session. A serious
>    regression for a voice speaker.
 2. **`SnapfifoSink` would leak TTS to followers IF re-wired — and it is the
>    realistic failure mode.** Investigation (2026-06-09) corrected an earlier
>    overstatement: `SnapfifoSink` is currently **unwired DEAD CODE** (`grep
>    SnapfifoSink rust/` hits only `snapfifo.rs`). Commit 050d334 wired it as a
>    live `ReferenceFanout` consumer; commit 9102e13 (which moved TTS ingress
>    into fanin) removed the `snapfifo_path` config field and the `main.rs`
>    wiring. So the leak is **doubly latent**: the producer is unwired in
>    outputd AND there is no follower playback to receive it (the reconciler's
>    `JASPER_OUTPUTD_SNAPFIFO_PATH` write is inert — `Config::from_env` never
>    reads it). The real risk is a future dev re-applying the known-good 050d334
>    wiring against today's `main.rs` and shipping the leak. Guarded against:
>    a WARNING doc-comment on `SnapfifoSink` + `lib.rs`, and a `jasper-doctor`
>    `check_grouping_tts_separation` that warns a leader its streaming is behind
>    this blocker. (Consequence to fix when this lands: the reconciler-written
>    tap env + the `derive_grouping_runtime` leader-tap signal are now
>    intent-only — a bonded leader can read green-tapping while outputd consumes
>    nothing.)
>
> **What inv-2 actually requires (the corrected contract).** TTS must be
> SEPARATED from the streamed music — and the cleanest realization **keeps TTS
> in fanin (its current home) and diverts only MUSIC through the round-trip**,
> rather than resurrecting an outputd TTS path:
> - **fanin emits a second, MUSIC-ONLY output** (the separation point) —
>   ✅ **BUILT (Increment 1, `JASPER_FANIN_MUSIC_OUTPUT_PCM`, off by default).**
>   In `mixer.rs` `step()`, the program is summed and program-ducked
>   (`apply_gain_to_sum`, ~mixer.rs:286-288) BEFORE TTS is mixed in
>   (`tts.mix_period`, ~mixer.rs:289-291). Split there: clamp the pre-TTS,
>   post-duck sum to a music-only buffer + write it to a 2nd output PCM, THEN
>   mix TTS and write the existing full output. Off-by-default behind a new env
>   (unset = today's single output). One extra clamp + ALSA write per period.
>   This single change is BOTH the inv-3 leak fix (the tap reads music-only) AND
>   the music half of inv-2. **What landed:** the lossy non-blocking side-tap
>   (`write_music_only` — period-aligned via `avail_update`, drop-on-full, so it
>   can't perturb the primary output's pacing, inv-1), best-effort open (a bad
>   PCM degrades to solo, primary path untouched), and a STATUS `music_output`
>   block (`enabled`/`pcm`/`frames_written`/`drops`). **Not yet consumed** —
>   wiring it to snapserver (the leader round-trip) is Increment 2.
> - **the leader's DAC keeps the EXISTING full music+TTS fanin output** for its
>   own low-latency assistant playback; only the **synced MUSIC** takes the
>   round-trip (tap → snapserver → leader's snapclient → CamillaDSP-B → DAC).
>   So "post-round-trip TTS re-mix" reframes to "assistant stays leader-local,
>   music is what's synced" — which is exactly §6's V1 policy (assistant is off
>   the synced path). This avoids re-adding the outputd TTS IPC that 9102e13
>   deliberately retired.
>
> **Open questions (owner decisions, several hardware-only):** (1) the leader's
> local assistant being ~300-500 ms out of sync with the buffered music beneath
> it — §6 says music-only sync is the V1 promise, so likely fine, but confirm as
> a product call; (2) should followers hear the music **ducked** under the
> leader's local TTS (the music-only stream is post-duck) — a UX call; (3) the
> 2nd fanin ALSA output must not perturb the mixer work-loop cadence (xruns) —
> on-device; (4) two CamillaDSP instances on the leader (~+85 MB each) + the
> extra fanin output staying in the 1 GB envelope. The `DacContentSource` FIFO
> reader (a clean mirror of `snapfifo.rs`) is still the music-half outputd
> component; it lands with this TTS-aware integration, validated on ≥2 Pis.

> **⚠ Superseded on the tap source + leader-TTS — read the corrected contract
> above first.** The subsections that follow ("The tension to resolve" →
> "On-device validation plan") are the EARLIER realization. They still hold
> EXCEPT on the two points the BLOCKER above corrects — apply these as you read:
>
> 1. **Tap source.** Below, the SHARED program is tapped from `jasper-outputd`'s
>    post-CamillaDSP/post-clamp `ReferenceFanout` (`SnapfifoSink` → `SNAPFIFO`),
>    shown in the flow diagram's top line and assumed by "The outputd contract
>    change" (a)/(b). That source is **post-TTS**, so it would leak the leader's
>    assistant (inv-3). The corrected contract relocates the tap to a **fanin
>    MUSIC-ONLY output** (pre-TTS, post-duck). Wherever you read "outputd
>    `ReferenceFanout` → `SnapfifoSink` → `SNAPFIFO`," substitute "fanin
>    music-only → FIFO → `SNAPFIFO`."
> 2. **Leader-local assistant.** The flow diagram routes ONLY the buffered
>    round-trip music to the leader's DAC and omits the leader's own TTS. The
>    corrected contract keeps the assistant on the leader's existing local
>    low-latency fanin→DAC path (§6: the assistant is off the synced path); only
>    MUSIC takes the round-trip.
>
> UNCHANGED below: the tension itself (shared-on-wire vs per-member
> post-snapclient DSP, keeping tap-source ≠ DAC-source to dodge the audio loop),
> the per-member bottom-half chain (snapclient → FIFO → CamillaDSP-B → DAC) and
> its "the leader is a follower of itself" symmetry, the optional outputd
> DAC-content lane, the reconciler/env scaffolding (shipped), the invariant
> compliance, and the on-device validation plan.

**The tension to resolve.** Two shipped/written facts pull in opposite
directions:
- §2 (shipped `SnapfifoSink`): outputd taps **post-CamillaDSP, post-clamp** →
  SNAPFIFO. The streamed bytes are already processed + safety-clamped.
- §4: each member's channel-select + per-seat correction runs
  **post-snapclient** (so every member picks its OWN channel and corrects its
  OWN seat; the stream itself must stay the *shared, un-split, un-seat-corrected*
  stereo program, or followers inherit the leader's channel/seat — wrong).

Reconciling them: the program on the wire must be **shared** (clamped stereo,
no channel-split, no per-seat PEQ), and the per-member DSP runs **after**
snapclient. So for a leader the **tap source** (shared) and the **DAC source**
(this leader's own post-snapclient, channel-selected stream) are DIFFERENT
streams. A naïve `snapclient → outputd content → tap → snapserver → snapclient`
is an **audio loop** — the resolution must keep those two streams distinct.

**Resolved signal flow (leader = shared streamer + its own follower):**

```
renderers → snd-aloop fan-in
  → CamillaDSP-A  (SHARED only: master_gain/Ducker, volume_limit:0.0;
                   NO channel-split, NO per-seat correction)
  → jasper-outputd  ── ReferenceFanout → SnapfifoSink → SNAPFIFO  (shipped tap;
  │                                                       the shared clamped program)
  │                 → snapserver
  │                     → each FOLLOWER's snapclient → (its own follower chain)
  │                     → this leader's OWN snapclient (--host 127.0.0.1)
  │                            → FIFO  (snapclient --player file:<member content FIFO>;
  │                                     raw PCM, NOT snd-aloop — inv-2)
  │                            → CamillaDSP-B  (POST-snapclient, per-member:
  │                                             channel_select + per-seat PEQ,
  │                                             enable_rate_adjust:false — inv-5)
  │                            → outputd DAC-content lane → DAC loop → DAC
  └── (the DAC loop reads the DAC-content lane, NOT CamillaDSP-A; CamillaDSP-A
      flows only to the tap. inv-1: the DAC loop stays the sole timing owner;
      the FIFO/snapclient side never back-pressures it — a starved FIFO reads
      as silence, exactly like a starved ALSA capture today.)
```

A **follower** is the bottom half only: its snapclient → FIFO → CamillaDSP-B →
outputd → DAC. So **the leader literally is a follower of itself plus a
streamer** — the same post-snapclient member chain runs on every member; only
the leader additionally streams. This symmetry is the design's main payoff: one
member-playback path, validated once.

**The outputd contract change (the Rust work).** outputd gains an OPTIONAL
**DAC-content source** decoupled from the tapped content:
- Solo / today: DAC-content == tapped content (one stream; byte-for-byte the
  current daemon — zero change when `JASPER_OUTPUTD_DAC_CONTENT_*` is unset).
- Member: the DAC loop reads the DAC-content lane (fed by CamillaDSP-B from the
  snapclient round-trip); the ReferenceFanout still taps the (separate) shared
  content for a leader, or is idle for a pure follower.

The open implementation choice to settle on-device (both honor the flow above;
pick by measured RAM/latency): **(a)** outputd grows a second content input
(a FIFO/PCM `dac_content`) read by the DAC loop while the existing content read
drives only the tap; **(b)** keep outputd single-input but feed it CamillaDSP-B's
output as its content, and move the SNAPFIFO tap OFF outputd's ReferenceFanout
onto CamillaDSP-A's output (a small dedicated FIFO writer), retiring
`SnapfifoSink` for the leader. (a) reuses the shipped `SnapfifoSink` and keeps
one outputd; (b) is simpler in outputd but rebuilds the tap. Lean (a) unless
the dual-read loop proves too costly on the Pi 5.

**Two CamillaDSP instances on the leader.** CamillaDSP-A (shared, pre-stream)
and CamillaDSP-B (per-member, post-snapclient) are distinct configs and likely
distinct instances (~+85 MB each on the 1 GB Pi — a leader is a brainy Pi 5, so
affordable, but measure). A follower runs only CamillaDSP-B. The
already-shipped channel-split weave (`weave_channel_split`) + inv-5
`rate_adjust=false` produce CamillaDSP-B's config exactly — that work is done;
inv-2 is what *positions* CamillaDSP-B post-snapclient and feeds its output to
the DAC.

**Reconciler / env contract (the scaffolding to build first, INERT until the
outputd reader lands + the flag is on):**
- `JASPER_GROUPING_LEADER_CONTENT_LANE` (off by default) — the master gate; the
  whole reroute is staged behind it so a deploy can't activate an unvalidated
  audio path.
- snapclient gains a `--player file:filename=<FIFO>,...` output for an ACTIVE
  member (NOT added until the FIFO has a reader — a FIFO with no reader blocks
  snapclient).
- `JASPER_OUTPUTD_DAC_CONTENT_FIFO` (reconciler-owned, like the snapfifo tap
  env) — the lane outputd's DAC loop reads in member mode; unset = today's
  single-input behavior.

**Invariant compliance:** inv-1 (DAC loop sole timing owner — the snapclient
FIFO is a side-feed; underrun = silence, never back-pressure) ✓; inv-2
(snapclient writes raw PCM to a FIFO via `--player file`, never snd-aloop — no
`snd_pcm_delay`-lies trap) ✓; inv-4 (AEC's `pcm.jasper_ref` is untouched —
still a separate reference consumer) ✓; inv-5 (CamillaDSP-B is the post-snapclient
member DSP, `rate_adjust=false` — shipped) ✓; `volume_limit:0.0` is applied by
CamillaDSP-A *before* the tap (followers inherit the clamp) AND by CamillaDSP-B
before the DAC (the leader's own playback is clamped) ✓.

**On-device validation plan:** form a 2-Pi pair; confirm (1) followers receive
the SHARED stereo (not the leader's channel); (2) the leader plays its assigned
channel from the buffered round-trip, not its direct output; (3) measured L/R
sample alignment within the buffer target; (4) leader RAM with two CamillaDSP
instances stays within the 1 GB envelope; (5) a snapclient/FIFO underrun
degrades to silence + the existing `degraded` health, never a wedge.

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

> **SUPERSEDED on where correction runs (2026-06-10) — see "Canonical signal
> flow" (§2).** The canonical design bakes ALL per-channel correction on the
> LEADER (one CamillaDSP) and keeps every receiver DUMB. The "dumb endpoint"
> model in the wireless-sub bullet below now applies to the L/R mains too — *not*
> the "each member self-corrects post-snapclient" model originally written here.
> The measurement / `target_channels` mechanics stay useful (they're how the
> leader bakes a per-channel-corrected stream); only the *placement* (member-side
> → leader-side) changed.

**Decision (canonical): the leader bakes per-channel correction into the stream;
receivers are dumb channel-droppers.**

- **Stereo L/R:** the leader's one CamillaDSP corrects the left channel for its
  own seat and the right for the follower's seat, then streams the result; each
  speaker drops the channel it doesn't play (ALSA `ttable`). No post-snapclient
  DSP on any receiver. **The per-side axis is BUILT** (Increment 2, PR #587):
  `emit_sound_config(room_peqs_right=…)`
  bake a different room correction per channel in ONE config (`None` = solo
  byte-identical mono-duplicate; `[]` = FLAT right channel — an uncalibrated
  follower never gets the wrong-room curve). The earlier "`target_channels`"
  phrasing referred to this axis. `room_peqs_right` and the (superseded
  member-model) `channel_split` weave are **mutually exclusive** — the emitter
  raises on the combination. Every generated config keeps `volume_limit: 0.0`.

- **Wireless sub (transport endpoint):** the leader computes the
  crossover, level, and delay and bakes them into the **LFE
  channel before streaming**. The endpoint just plays it. No
  on-endpoint content DSP. Sub sync tolerance is *loose* (bass localizes
  poorly to the ear), so a few ms of misalignment is inaudible —
  this is why the sub is the easiest transport endpoint.

- **Full-range satellite (transport endpoint):** needs per-channel
  room correction for *its* seat, which it cannot compute (no
  local content DSP). So the leader applies a **channel-specific filter
  before streaming** (the `target_channels` path). To obtain the
  filter we run a one-time calibration: the satellite plays a
  sweep, a mic at the listening position captures it (reusing the
  existing `/correction` measurement flow), the filter is computed
  centrally and baked into that channel's stream. This open-loop
  calibration is **the single genuinely-new piece V1 adds**, and it
  is leader-side. See [HANDOFF-correction.md](HANDOFF-correction.md).

- **Inter-speaker time alignment has three owners, not one.** Snapcast
  owns dynamic distributed sync: timestamped chunks, client clock
  tracking, and drift correction. Snapcast client latency
  (`Client.SetLatency` / `snapclient --latency`) is a static
  whole-client PCM/output-path offset for fixed endpoint/DAC/backend
  latency. Leader-side CamillaDSP `Delay` is the static rendered-channel
  acoustic offset used to align arrival at the listening seat. Do not
  describe CamillaDSP as the sync engine, and do not use Snapcast client
  latency as a room-geometry catch-all. Measure first: colocated or
  electrical endpoint baseline -> Snapcast latency; listening-seat
  arrival delta -> leader-side CamillaDSP channel delay. See
  [`research/balance-sync-calibration.md`](research/balance-sync-calibration.md).

**P1.2 (2026-06-08):** the channel-split DSP itself is now codified,
pure and tested, in
[`jasper/multiroom/channel_split.py`](../jasper/multiroom/channel_split.py)
— the `channel_select` Mixer (an L/R route, or a clip-safe −6.02 dB L+R
sum for mono/sub) plus the sub's LR4 80 Hz lowpass. It is the *same*
recipe whether a brainy stereo-pair member applies it locally or the
leader applies it to pre-bake a transport endpoint's stream (the transport box
runs no content-DSP CamillaDSP, §1). It keeps `master_gain` identity (the Ducker
contract) and `volume_limit: 0.0`, and emits no positive gain. The
crossover and channel-select mixer are emitted through the shared
[`jasper/camilla_emit.py`](../jasper/camilla_emit.py) primitives (the
single home for CamillaDSP YAML emission, also used by the correction /
sound / active-speaker generators), so the sub crossover is CamillaDSP's
native `BiquadCombo LinkwitzRileyLowpass` — the same primitive the
active-speaker driver crossovers use. Deferred to P1.3: weaving it into
the live config (the `target_channels` / per-side-config path noted
above) and validating the sound on hardware.

**Two "channel" vocabularies — don't conflate them.** This module's
`channel` (left/right/sub/mono/stereo) is the **inter-speaker** axis —
which channel of the stereo *program* a whole speaker plays in a bond.
`output_topology.SpeakerChannel.role` (woofer/tweeter/…) is the
**intra-speaker** axis — which *driver* a DAC output feeds. They compose
rather than compete: on a multi-way active speaker that is also a bond
member, channel-select runs **first** (pick the L/R/mono program), then
the active-speaker crossover splits that program across the drivers.
They never need to know about each other because channel-select is
**interface-preserving** — a 2→2 transform that changes only *what* is
on the two channels, so per-channel correction and the active-speaker
2→N driver split both still receive two channels. The live weave of the
channel-select fragment into an active-speaker config is P1.3.

---

## 5. Volume

**Decision: a single room/pair-level command, fanned to all members
via the existing `VolumeCoordinator`, clamped and rate-limited at
the leader and re-clamped at each receiver.**

A pair/room volume command is "set `listening_level` on every
member." The leader calls each member's existing `POST /volume/set
{percent}` on `:8780`. No per-speaker `trim` composition algebra in
V1 — a bonded set shares one level.

> **REVISED 2026-06-10 after review — the canonical design moves where volume
> lives.** §5 above assumed every member is brainy (each sets its own CamillaDSP
> `master_gain`). Under the canonical design that breaks two ways: a **transport
> follower has no local content-DSP CamillaDSP** to set, and on the leader
> `master_gain` is now **pre-buffer**
> (CamillaDSP-A bakes the stream), so a "louder" lags ~1 buffer AND is baked
> identically into the shared stream. Resolution: **(1)** the shared room/pair level
> stays `master_gain` on the leader (pre-stream — every member inherits it; accept
> the ~buffer lag, it's music, not voice); **(2)** any **per-member / per-room trim**
> (and *all* follower volume) moves to **Snapcast per-client volume**
> (`Client.SetVolume`, post-buffer, works for a DSP-less endpoint — §3 already wires
> the RPC), NOT `master_gain`; **(3)** a transport follower never receives a
> `POST /volume/set` (no master_gain to set) — its level is Snapcast client volume
> only. The asymmetric-room balance trim flagged below therefore lives in Snapcast
> client volume, not Camilla. The dial's live gauge will lead the audible change by
> up to a buffer on the shared level — acceptable for a knob driving music, but note
> it.

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
| **Leader self-loop degraded** (its localhost snapclient / snapserver / pipe stalls — routine Pi ALSA underrun) | The leader **falls back to the DIRECT fanin music path** (un-synced) rather than going silent on its OWN music — a momentarily-unsynced pair beats a silent brainy speaker. Cue + `/state` flag + dashboard card. | **inv-B** (§2 canonical): outputd detects an unhealthy round-trip for N periods and switches its DAC source to the direct fanin music lane, recovering to the synced lane when the self-loop is healthy again. The inverse of inv-1 — silence is OK for a follower, NOT for the leader hearing itself. |
| **WiFi blip** | Buffer rides short blips; sustained loss → follower degrades + surfaces the failure. | TCP retransmit + buffer depth (the jitter lever — §2). WiFi is the supported transport; no Ethernet requirement. |
| **Solo (N=1), grouping off** | **Zero cost, verified.** No `snapserver`, no `snapclient`, no FIFO consumer registered (outputd byte-identical to today), no advert, no thread. | Mirrors peering `mode=off`. |

A dumb endpoint going silent when the leader is off is **correct
behavior**, not a regression (a sub *should* be quiet when the system
is off; a satellite's room depends on the leader anyway). We make it
*visible*, not invisible.

**Visible-failure surface (built 2026-06-08).** The "/state flag +
doctor" half of "make it visible" is live: `read_grouping_state` carries
a `runtime` health block and `jasper-doctor`'s `check_grouping` warns
when a configured-valid bond's units aren't actually up — both derived by
the one pure `derive_grouping_runtime(cfg, unit_states)`, which reuses
`reconcile.plan` for "what should be running." The **`/rooms` page renders
that block** too (an amber **Degraded** badge + the reason, vs green
**Grouped**), completing the `/state` + doctor + dashboard triad. Until
the P1.3 producer ships, an enabled leader correctly reads as `degraded`
(snapserver has no FIFO to read yet) — the honest state, not a false
green.

**Blast radius scales with bond size — leader-failover priority rises with
N.** The fixed-leader model's leader-crash cost grows with the group: losing
the leader of a stereo pair drops half the image (obvious, recovered by
re-forming the pair), but losing the leader of a six-speaker synced group
silences all six at once. The boot-reconciler election (modeled on
`jasper-wifi-guardian`, the leader-crash row above) is correctly deferred
while bonds are pair-shaped — building election for a two-device pair is
astronaut engineering — but its priority climbs with N. **A >2-member group
is the trigger to build it**, not a stereo pair. The related resilience floor
is already in: the concurrent dissolve discovery (a single hung/offline peer
can no longer wedge a bond teardown for the whole group — §8 "Known scaling
boundaries") and best-effort dissolve (an offline member self-corrects via the
`degraded` health, never strands the local leave).

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

### Grouping control plane — threat model (authenticated; see HANDOFF-control-plane-auth.md)

The grouping control plane — `POST /grouping/set`, `GET /grouping`, and
the bond/unbond fan-out that POSTs to those on *other* speakers — is now
**authenticated**. WS1 Phase 2
([HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md)) made
the per-device `control_token` **mandatory** on `/grouping/set` (and five
other destructive routes), so a tokenless caller gets
`403 control_token_required`. That per-device token gates **browser → its
own speaker**; it is a CSRF token and carries no caller identity, so it
cannot do the **device-to-device** (leader → follower) leg. That leg
authenticates with a separate **household credential** — a shared secret
minted at the human pairing moment (the `/rooms/` wizard's `POST /bond`),
distributed over the
trusted LAN, persisted `0640` group `jasper` per member (mirroring
`control_token`), and presented on the cross-device grouping path in an
`X-JTS-Household` header.
The full threat model, prior art, and design live in
[HANDOFF-control-plane-auth.md](HANDOFF-control-plane-auth.md), which is the
single source of truth for cross-device control auth; this subsection is the
multiroom-side summary.
The autonomous supervisor path uses the same credential when reasserting a
rostered follower without a browser in the loop; two-Pi reboot smoke remains
pending.

What the fan-out adds — and how each piece is now covered:

- **The SSRF guard is a target restriction, not an auth layer — still true
  and complementary.** `rooms_setup._post_grouping_to_member` (and the
  unbond fan-out) constrain cross-speaker POST/GET targets to **private or
  loopback IPv4** and reject bare hostnames — so the cross-speaker surface
  can never be aimed at an internet host (no internet-proxy / no
  DNS-rebinding pivot). It bounds *where* the server will talk; the
  household credential is what authenticates the *caller*. The two are
  orthogonal and both apply.
- **"LAN = trusted" is load-bearing ACROSS devices — now mitigated, not
  merely accepted.** Bond/unbond makes one speaker reconfigure *another* on
  the household's behalf, stretching the home-LAN trust boundary across
  devices. The household credential closes the steady-state hole: once a
  household is bonded, every subsequent `/grouping/set` requires the shared
  secret, so a casual / cross-site / curl actor can no longer flip the
  group. **Residual:** a malicious LAN device could still *initiate* its own
  `/rooms/` wizard `POST /bond` to mint a secret — the same residual the whole
  LAN-trust posture accepts, closed only by a future pairing code at bond time
  (HANDOFF-control-plane-auth.md §5 Option 2 / §9 decision 2). State it honestly;
  do not over-claim.

This subsection covers the *grouping control plane*; the *audio* threat
surface (Snapcast's 1704/1705 ports, the post-clamp tap, the dumb-endpoint
analog ceiling) is items 1–5 above.

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
is missing (exit-78 + cue + dashboard); **WiFi power-save stays
disabled** — `install.sh` already does this on wlan0 (for AirPlay;
see `deploy/lib/install/renderers.sh`), and it is equally
load-bearing for snapclient endpoints: brcmfmac power-save is a
documented cause of streaming dropouts, so a follower image must
keep that step.

---

## 7.5 The dumb-follower role-state contract

One table answers "what runs where, and who owns the transition" for
every unit a bond touches. **Role is runtime** (grouping.env, wizard/
bond-owned); **tier is install-time** (what exists on disk —
[dumb-endpoint-bringup.md](dumb-endpoint-bringup.md)); profiles derive
from both. A future "smart follower" is a profile variant, not a
rearchitecture.

| Unit | Solo | Leader | Follower | Transition owner |
|---|---|---|---|---|
| jasper-control | runs | runs | runs (volume/transport forward to leader) | — always on |
| jasper-outputd | runs | runs (dac_content L) | runs (dac_content R) | grouping reconciler (env + restart-on-change) |
| jasper-camilla / jasper-fanin | run | run (camilla bakes the pipe) | run (inv-B fallback lane only) | grouping reconciler (config swap) |
| jasper-snapserver | stopped | runs | stopped | grouping reconciler (plan) |
| jasper-snapclient | stopped | runs | runs | grouping reconciler (plan) |
| Local source resource groups (`jasper/local_sources/registry.py`: AirPlay+nqptp, Spotify, Bluetooth audio/agent, USB bridge+gadget, shared mux arbiter) | per /sources/ wizard | per /sources/ wizard | **parked** (stop resource groups; restore intent units on exit) | grouping reconciler (plan; STOP never disable — /sources/ owns enable/disable) |
| jasper-voice + jasper-aec-bridge (+aec-init) | per provider/mic gates | per provider/mic gates | **parked** (disable --now) | **jasper-aec-reconcile only** — grouping derives `JASPER_GROUPING_VOICE_PARK=1` into grouping-voice.env and kicks it; bond-validity logic is never re-derived in shell |

Interface contract while a follower (every surface tells the same
story; "parked-by-role" is surfaced state, NEVER a silent failure):

- Landing page: pair banner; source selector hidden; slider = "Pair
  volume" (server-side forward); mic card says the leader listens.
- /sources/: toggles disabled + pair note; POST /set 409s (an
  `enable --now` would reopen the advertise/leak hole).
- /voice/, /wake/, /sound/, /correction/: shared pair banner
  (`_common.pair_banner_html`); saves persist but
  `restart_voice_daemon` (the ONE helper all nine wizards use) skips
  the restart while parked — config applies on unbond via the un-park
  restart. Correction additionally warns: a follower's measurement
  sweep plays into outputd's DRAINED direct lane (inaudible).
- /system/: restart-voice 409s with the pair story; restart-audio
  touches only the alive subset (camilla).
- Doctor: 8 liveness checks read "parked (bonded follower)" via
  `_parked_as_bonded_follower` (doctor/_shared); `pair channels` does
  the cross-member coherence probe.
- Dial: volume + play/pause forward to the leader; hold-to-talk is
  dead while parked (accepted).

**DSP ownership — the two-kinds split (decided 2026-06-12):**

- **Content DSP** (room correction, sound preferences, EQ) is
  LEADER-side, baked per-channel into the synced stream. This is what
  lets a dumb member stay dumb, and it scales to the Zero endpoint
  tier unchanged.
- **Driver DSP** (active crossover, driver protection, per-driver
  gain/delay) must live ON THE BOX DRIVING THE DAC — it is per-driver
  signal routing and hardware-safety-critical (full-range program into
  a tweeter amp). A *dumb* (passive) follower has no driver-DSP path (its
  camilla is bypassed; the round-trip feeds outputd's `dac_content`
  ChannelPick), and outputd's round-trip lane FAIL-CLOSES on a non-single
  sink (`dac_content_lane_rejects_non_single_alsa_sink`). An **active**
  (multi-driver) follower realizes the driver-DSP-on-the-box path —
  snapclient → loopback → CamillaDSP [crossover/protection only] → outputd
  active sink, with snapcast's per-client `--latency` compensating camilla's
  fixed latency — which **landed in distributed-active Slice 3** (code; the
  active follower routes around the `dac_content` lane via CamillaDSP
  re-entry, so the fence is kept for the dumb lane but never fires on the
  active path). Applies to brainy followers and, with the prebuilt camilladsp
  binary, to a Zero 2 W "crossover endpoint" tier variant. **This boundary is
  owned by [HANDOFF-distributed-active.md](HANDOFF-distributed-active.md)** —
  see it for the clock contract, fail-closed/self-recovery, and slice status.

## 8. Phased delivery

**P0 — feasibility spike with MEASURED gates (throwaway, not
product).** For the practical Zero 2 W streambox setup, see
[`dumb-endpoint-bringup.md`](dumb-endpoint-bringup.md); this section owns
the measurement contract. ("Endpoint" here means the runtime follower
role on a streambox box, not a separate install tier — that tier was
removed.) Stand up `snapserver` reading a hand-fed FIFO on one
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

### Web UX

`/rooms/` is the combined **"Speakers"** surface — to the household,
"my other speakers" is *one* concern, so the read-only multi-room
directory and the wake-arbitration (peering) toggle live on the same
page. It is rendered with `canonical_page()` (page title "Speakers";
page CSS in `/assets/rooms/`, never `app.css`), shared icon sprite,
CSRF meta. For the **directory** part it is a **directory, not a config
aggregator**: each sibling row links to that peer's own
hostname-derived `http://<hostname>.local/system/` URL, so you configure
each speaker on its own UI (sidesteps cross-Pi write/auth; home-LAN trust).
The peer's raw LAN `address` remains in `/rooms.json` for server-side bond /
swap / trim control calls, where `_lan_target` applies the SSRF guard; browser
click-through URLs never fall back to raw IPs. Live refresh ships as a
static ES module polling `GET /rooms.json` (mirror `system_setup.py`'s
`/data.json`). Escape all untrusted strings (`room`, mDNS names,
`address`, hostname-derived URLs): on the server they never enter the HTML at
all (the shell is data-free), and the module renders every value via DOM/text
APIs — never `innerHTML`, never inline `onclick` with interpolated strings.
New wizard
socket port → `install.sh` must `systemctl restart` (not `start`) the
wizard socket (PR #118 502 failure mode).

**`/rooms` is the only peering UI.** The old `/peers/` page, redirect,
socket port 8776, and page CSS have been retired. `rooms_setup` now imports
the non-web peering helpers from `jasper.peering.config` so peering env
ownership lives with the peering package, not in a dead wizard module.

**Room stays in the identity home — NOT edited here.** Room lives at
`/speaker/` (the identity home; `JASPER_SPEAKER_ROOM`). The self card
*shows* the room (read via `identity.read_identity()`, already in
`/rooms.json`) with a small "Change in Speaker settings" link to
`/speaker/`. There is deliberately no room editor on `/rooms/` — adding
one would reopen the two-homes drift this increment closed. (The
peering → identity room consolidation remains a flagged follow-up; see
below.)

**Shipped: directory + wake-response toggle + stereo-pair bond-forming.**
The directory — discovery + click-through + this speaker's grouping
status — and the **wake-response card** (a toggle: "when multiple speakers
hear 'Hey Jarvis', only one answers", plus a "Primary" checkbox to prefer
this speaker in ties) landed behind port 8785 / `JASPER_ROOMS_WEB_PORT`,
sourcing siblings from the always-on `_jasper-control._tcp` service so it
works whether or not wake-peering is on (see §0). The page now carries
**two write cards**. The **wake-response** card: `POST /peering`
(CSRF-verified via the `X-CSRF-Token` header) read-modify-writes
`/var/lib/jasper/peering.env` through `jasper.peering.config.PEERING_ENV_FILE` —
flipping `JASPER_PEERING` on/off and setting/clearing `JASPER_PEER_PRIMARY`
while **preserving** `JASPER_PEER_ROOM` (owned by `/speaker/`) and operator
tuning knobs — then restarts voice + `jasper-control` and returns `{ok,
peering:{enabled, primary}}`. **Bond/unbond now ships (stereo pair):** the
primary bond card lets the household pick one sibling; the browser posts only
`peer_addr`, and the `/rooms/` backend owns the topology (this speaker →
leader/left, the picked one → follower/right). `POST /bond` mints a `bond_id`
and fans the grouping config out SERVER-side to each member's
`jasper-control /grouping/set`, with the follower's `leader_addr` set to the
leader's **stable mDNS `.local` handle** so the bond survives the leader's
DHCP IP churn (see §0 reconcile bullet). `POST /unbond` **dissolves** the
bond: the server discovers membership by reading each member's `GET
/grouping` (the new CSRF-free read on `jasper-control`) and fans
`{enabled:false, trim_db:0.0}` to the matches plus self. Both fan-outs run
**concurrently** across members (a slow/absent peer never serializes the
rest) over the LAN to each member's `jasper-control`, SSRF-guarded to
private/loopback IPv4 (bare hostnames rejected) — see §7 "Grouping control
plane — threat model" for the auth posture (token-gated browser→own-speaker;
the household credential authenticates the device-to-device fan-out).
Configuration is fully automatic (no per-speaker tinkering). The primary card
intentionally stays simple: create/dissolve/swap and pair balance. Subwoofer /
2.1 member-list edits remain an advanced/API path, not a default household
control. What's still honest preview scope: perfect sample-lock / follower
clock-lock remains partly unobservable from Snapcast RPC, so the card keeps the
hardware-validation note rather than implying a stronger guarantee than the
system can measure. Sibling rows in the directory stay read-only by design
beyond the pair-forming flow (configure each speaker's own knobs on its own UI).

#### Friendly names + identity on the directory (shared primitives)

The directory shows each speaker by its **friendly display name**, not a
bare hostname or the verbose mDNS instance string. The name reuses the
speaker's existing user-facing display name (the `/speaker` identity; the
same name shown on Spotify Connect / AirPlay / Bluetooth / USB).

**The room label now lives in the speaker-identity home.** The earlier
increment shipped with *no* room concept; the identity refactor adds one
without inventing a separate subsystem. [`jasper/speaker_name.py`](../jasper/speaker_name.py)
— already the canonical home for the display name — gained a `room` field
(`JASPER_SPEAKER_ROOM` in `/var/lib/jasper/speaker_name.env`; empty = unset,
no non-empty default). `validate_room` reuses the name's
printable-ASCII/normalize rules; `runtime_room` mirrors `runtime_name`'s
env→state→"" precedence; `write_state(name, room)` persists both atomically
and `write_state(name)` preserves the stored room (back-compat). The
`/speaker` wizard now renders a Room text input and writes both. `install.sh`'s
`migrate_speaker_room` seeds `JASPER_SPEAKER_ROOM` once from peering's legacy
`JASPER_PEER_ROOM` so existing rooms carry into the identity home.

**Three shared primitives back this (extracted, not re-grown per caller):**

- [`jasper/identity.py`](../jasper/identity.py) — **the single
  speaker-identity reader.** `read_identity()` composes
  `name + room + hostname + peer_id` and is TOTAL (never raises). Room
  precedence is the point: the **identity home wins**
  (`speaker_name.runtime_room()`), then a legacy fallback to peering's
  `JASPER_PEER_ROOM`, then `peering.config.default_room()` — so older
  peering env files still surface a room while the identity home becomes
  the source of truth. `/rooms/`'s self block (`_self_name` /
  `_self_hostname` / `_self_room`) now resolves through this reader, so the
  directory agrees with `control_advert` and the rest of the speaker on "who
  is this speaker." (control_advert and future bond/grouping code are meant
  to read identity too — see the consolidation follow-up below.)
- [`jasper/mdns.py`](../jasper/mdns.py) — **the one one-shot mDNS-SD browse
  primitive.** `browse_once(service_type)` is the fail-soft
  AsyncZeroconf browse+resolve+TXT/address parse moved verbatim out of
  `rooms_setup._discover_speakers`; it returns raw, parsed
  `DiscoveredService` facts (full instance name, SRV host, addresses, port,
  decoded TXT) and lets each consumer apply its own policy. Any failure (no
  zeroconf, a resolve error, a total browse failure) degrades to dropping
  that entry / returning `[]` — never raises. `rooms_setup` keeps only the
  rooms-display policy on top (`_peer_label`, port defaulting, the TTL cache).
- [`jasper/avahi_service.py`](../jasper/avahi_service.py) — **the one Avahi
  `*.service` renderer.** `render_service(template, out, substitutions, *,
  escape, reload)` is the shared render+stray-placeholder-guard+idempotent
  atomic-write+`reload_avahi` body that both `control_advert.py`
  (`__SPEAKER_NAME__`, free-form → `escape=True`) and `peering/avahi.py`
  (`__PEER_ID__`/`__ROOM__`/`__PRIMARY__`, mDNS-safe → `escape=True`,
  byte-identical) now call. It is fail-soft (missing template / stray token /
  write failure → `False`, never raises) and idempotent (a byte-stable render
  skips the write+reload, so a long-lived advert never tears down its
  service-group).

How a peer's name reaches the directory: each speaker advertises its
display name as a `name=` TXT record on the always-on
`_jasper-control._tcp` service.
[`rooms_setup._peer_label`](../jasper/web/rooms_setup.py) already prefers a
`name=` TXT over the SRV hostname, so once advertised, peers render by
their friendly name automatically — no client change. The **self** card
reads the same identity directly (`_self_name()` →
`jasper/speaker_name.runtime_name`, with hostname/room via
`identity.read_identity()`), so it shows the same name peers see;
`/rooms.json`'s `self` block carries `name` / `hostname` / `room`.

> **Compatibility note.** Peering still accepts its legacy
> `JASPER_PEER_ROOM` data key, and `identity.read_identity()` keeps that
> fallback so `/rooms/` stays consistent for older installs. There is no
> separate peering room editor anymore.

Coverage for the shared primitives is hardware-free:
`tests/test_avahi_service.py` (render/escape/stray-guard/idempotence/
fail-soft), `tests/test_mdns.py` (fail-soft when zeroconf is absent +
`DiscoveredService` parse mapping against a fake `AsyncServiceInfo`),
`tests/test_identity.py` (field composition + room precedence + totality),
and the room half of `tests/test_speaker_name.py`.

The advert is rendered, not static. `deploy/install.sh` installs
[`deploy/avahi/jasper-control.service.template`](../deploy/avahi/jasper-control.service.template)
to `/etc/jasper/avahi-templates/` and renders the live
`/etc/avahi/services/jasper-control.service` via
[`render_control_advert`](../jasper/control_advert.py); the `/speaker`
save (`speaker_setup._apply_name`) re-renders + reloads Avahi on every
name change. Safety contract (the dial depends on this service
resolving):

- **Purely additive vs. the historical static service** — same
  `<service>`/`<type>`/`<port>` byte-for-byte, only the one
  `<txt-record>name=…</txt-record>` added. The dial keys off service type
  + address; a TXT record cannot affect discovery. (Pinned by
  `tests/test_control_advert.py`'s byte-equivalence check.)
- **XML-escaped before substitution** — a free-form name with `&`, `<`,
  or `>` would otherwise make Avahi reject the whole `<service-group>` and
  drop `_jasper-control._tcp`, breaking the dial. A hostile-name test
  asserts the rendered file is still valid XML and round-trips.
- **Fail-soft, never raises** — a missing template, unreadable name, or
  failed Avahi reload logs `event=control_advert.*` and returns; the
  `/speaker` save and `install.sh` never break on a render failure
  (backstop: the next `jasper-control` restart re-renders). An unset/empty
  name falls back to the hostname so the TXT is never empty and the
  service always advertises.

### Known scaling boundaries & future extraction points

The grouping control plane is deliberately scoped to a stereo PAIR today.
These are the seams that will stretch as the feature grows — each paired with
the TRIGGER that says "extract/generalise now, not before," so we neither
front-run the complexity nor forget where it belongs.

- **Cross-speaker peer-control client.** The HTTP-to-a-peer's-control-API
  pattern lives as two helpers in `jasper/web/rooms_setup.py`
  (`_post_grouping_to_member`, `_get_member_grouping`) sharing the
  `_lan_target` SSRF guard and the `_map_peers` bounded-concurrency primitive
  — the right size for two call sites. (Concurrency is already DRY: `_map_peers`
  is the one pool used by the POST fan-out AND the discovery GETs, so adding a
  client wouldn't re-derive it.) **Trigger to extract a `PeerControlClient`:**
  the THIRD cross-speaker call (e.g. bond-wide volume sync, status
  aggregation). Then lift the guard + the GET/POST + the `:8780` base + the
  `known`-set threading into one client so the SSRF policy, timeouts, and
  never-raise contract have a single home — not three copies.

- **The GET /grouping wire contract has ONE home — keep it that way.**
  `grouping_response` / `parse_grouping_response` (+ `GROUPING_RESPONSE_KEY`)
  in `jasper/multiroom/state.py` are the producer/consumer pair, locked by a
  round-trip test. The C4 regression (2026-06-09) was exactly this envelope
  drifting across daemons (producer nested under `grouping`, consumer read the
  top level). **Rule:** any NEW cross-daemon grouping payload follows the same
  builder + parser + round-trip-test shape, never hand-rolled JSON on each
  side.

- **Bond topology is pair-shaped.** `role ∈ {leader, follower}`,
  `channel ∈ {stereo, left, right, sub, mono}`, and a single `leader_addr`
  model a stereo pair. `/unbond`'s discover-by-`bond_id` already scales to N
  members (it disables every match), but the CREATE flow does not. **Trigger
  for 2.1 / multi-member:** when >2-member bonds land, (a) the `channel` /
  `role` vocabulary grows (and `validate_grouping` with it), and (b)
  `makeBondCard()` in `deploy/assets/rooms/js/main.js` — today a two-faced
  create/dissolve card — splits into a create-view and a manage-view
  sub-component rather than growing a third face.

- **Dissolve is best-effort by liveness (accepted property, not a TODO).**
  `/unbond` only disables peers it can discover AND reach at dissolve time; a
  follower offline at that moment comes back still configured (stranded),
  which the `degraded` runtime health surfaces and the next bond/leave
  self-corrects. Self is always disabled, so the local leave never depends on
  a peer being up. If guaranteed teardown of an offline member is ever
  required, THAT is where a persisted roster + retry would be added —
  deliberately not built now (it would trade the stateless, drift-free
  discovery design for roster bookkeeping that can itself go stale).

---

## 9. Open questions for the project owner

1. **Whole-house spoken announcements — DECIDED 2026-06-09: leader-local
   only.** The owner confirmed the assistant replies on the speaker it was
   addressed from, NOT house-wide (the Sonos/Alexa default; room music keeps
   playing elsewhere). This is now a *requirement*, not a maybe — it is the
   entire reason inv-2 needs the `jasper-fanin` music-only output (followers
   get music with the leader's TTS held back; see §2 "inv-2 realization"). Had
   whole-house announcements been wanted instead, the leader would just stream
   its full mix and the TTS-separation work would vanish — so this decision is
   load-bearing for the inv-2 build. Time-synced whole-house announcements
   (TTS on the buffered path) remain a future, separate feature if ever wanted.
2. **AirPlay 2 sync expectation — ANALYZED 2026-06-21; build pending.**
   A receiver cannot grow the AP2 latency budget — AP2 latency is
   sender-authored (full mechanism in
   [HANDOFF-airplay.md](HANDOFF-airplay.md) "AirPlay 2 latency is
   sender-authored — the bonded-leader consequence"). So the plan is
   NEITHER to route AirPlay through the Snapcast FIFO NOR a PTP
   carve-out: the bonded leader keeps shairport/nqptp's native AP2 path
   and compensates the Snapcast round-trip locally with a **bond-aware**
   `audio_backend_latency_offset_in_seconds` (the offset shifts the AP2
   PTP anchor identically for realtime and buffered streams). INVARIANT:
   the Snapcast term is added ONLY while this speaker is an active bonded
   leader and torn down on unbond — solo/follower speakers are
   byte-for-byte unaffected. The remaining sub-question — which only
   decides whether bonded lip-sync is fully recovered or degrades to
   bounded residual lag — is the sender's negotiated budget vs. the
   ~150 ms + `buffer_ms` hidden delay *plus shairport's own 0.5 s backend
   buffer* (shairport drops the offset, not just trims it, once
   budget < need + 0.5 s — see HANDOFF-airplay.md "JTS-side observability");
   measure it per-app on-device with
   `scripts/airplay-latency-probe.sh` before sizing `buffer_ms`. The
   build hook is `reconcile.py` step 5 (the leader's bonded CamillaDSP
   apply — `passive_leader` re-emits the pipe bake, `active_speaker_leader`
   runs the camilla#1 program bake + arms camilla#2), where the bonded
   CamillaDSP config is already applied.
   **Stage D (observability) — BUILT 2026-06-21.** The tight regime is now
   visible JTS-side without reading the journal by hand:
   `/state.grouping.airplay_latency_fit` (computed budget-vs-need, fail-soft,
   `{"applicable": false}` off the bonded-leader path), the same field on
   `/rooms.json` rendered as an "AirPlay lip-sync" row in the `/rooms` bond
   card, a grouping doctor check (`check_grouping_airplay_latency`), and the
   AirPlay-health event ring classifying shairport's "too short to accommodate
   an offset" warning. See `jasper/multiroom/airplay_latency.py` and
   HANDOFF-airplay.md "JTS-side observability". The Step-0 measurement (jts.local: 9/9 observed sessions
   on the default ~2.0 s budget) showed the free regime dominates, so the
   surface is scoped down (quiet when comfortable, journal read only when
   actually a bonded leader). Still owed: the per-app **video** budget sweep
   (human/hardware) to confirm video stays in the free regime.
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

2026-06-12 session notes (BOND ROSTER — the leader now RECORDS who its
pair sibling is instead of inferring membership from "who on the LAN
claims my bond_id". Live failure that forced it: a third, foreign Pi
(the endpoint-tier test device) transiently claiming the live bond made
swap, trim, AND balance all fail "found 2" — and unbond would have
DISABLED the foreign device's grouping. New leader-only grouping.env
keys, written by the /rooms bond flow for 2-member bonds:
JASPER_GROUPING_PEER_ADDR (the follower's LAN IPv4 — cross-speaker
control calls are IP-only by SSRF design) + JASPER_GROUPING_PEER_NAME
(its directory display name, the DHCP re-resolution key). Both ride
/grouping/set with the trim_db contract (settable, validated,
PRESERVED when omitted; explicit empty clears — non-leader bond bodies
clear so a role flip can't keep a stale roster).
rooms_setup._resolve_bond_peer is the ONE resolver (swap, trim,
balance, unbond): roster IP probe → if dead, re-find PEER_NAME in the
live directory and accept the same-bond match at its new IP (logged
event=rooms.peer_addr_drift) → else a HARD error naming the speaker —
never a fall-back to inference a foreign claimer could satisfy.
Roster-less (pre-roster) bonds keep the legacy inference, whose
ambiguity error now suggests re-pairing to record the roster. Unbond
with a roster disables self + the recorded sibling only (best-effort
at its last known address when offline).
**Extended 2026-06-23 (N-member roster — "add a sub to a pair"):** the
leader now also records EVERY follower (not just the L/R sibling) in a
new leader-only key `JASPER_GROUPING_ROSTER` (`addr|name|channel`
entries, comma-joined; serialized name sanitized; each addr SSRF-checked
as private/loopback IPv4 in `validate_grouping`; same preserve/clear
contract). `_save_bond` records it for any N while keeping
`PEER_ADDR/_NAME` = the primary L/R sibling (so swap/trim stay on the
stereo pair). `_unbond` takes a full-roster path — disable self + EVERY
recorded member — so a 2.1's sub is never orphaned (the legacy single-
sibling/discovery path remains for pre-roster bonds). The full member-list
`POST /bond` path still supports adding a `channel=sub` follower to the SAME
bond_id, and the pure `addSubPlan` in grouping-view.js remains the policy
helper for that advanced shape; the primary `/rooms/` card no longer exposes
subwoofer/crossover controls by default. Regression tests:
tests/test_web_rooms_setup.py (foreign-claimer matrix, DHCP
rediscovery, named unreachable error, unbond containment, bond-body
roster), test_web_balance_flow.py (start survives a foreign claimer),
test_multiroom_config.py + test_control_server.py (parse/validate/
preserve/clear). The same 2026-06-23 pass made the wireless-sub crossover
symmetric for passive/dumb endpoints: `jasper-outputd` low-passes the sub
locally, high-passes each passive main locally at the same `crossover_hz`
when the default-on `/rooms/` toggle is enabled, and the reconciler clears the
HP env on no-sub/toggle-off/sub/active-endpoint paths. Earlier same day: PAIR
BALANCE P2, equal-loudness
walkthrough
— the v1 fixed-level A/B/A burst design was REPLACED the same day
after first live use: a badly mismatched pair (the exact case the tool
exists for) put the quiet speaker's bursts under the noise floor and
the whole take died with an opaque rejection, and the rapid identical
L/R/L bursts read as "both speakers at once" to the household. The
shipped design measures ONE SPEAKER AT A TIME at matched RECEIVED
loudness: jasper/multiroom/balance.py renders a per-channel stereo
ramp WAV (silence-lead-in, then 500 Hz–2 kHz seeded noise rising
-42 → -12 dBFS at 1.5 dB/s with a 4 s ceiling hold; the other channel
silent, so each bonded member's outputd channel pick emits it from
exactly one physical speaker through the FULL normal chain including
its current trim during normal playback). The balance session now
normalizes the owned measurement path before playing: inside
`measurement_window`, `jasper.measurement.volume_guard` snapshots
CamillaDSP `main_volume` plus both Snapcast clients' volume/mute/group
mute state, sets Camilla to the bounded calibration level and the pair's
Snapcast clients to 100% unmuted, then restores the snapshot in
`finally` before renderers resume. If Snapcast/Camilla cannot be
resolved or normalized, `/balance/start` fails visibly rather than
measuring through hidden attenuation. The phone at the listening
position meters its own in-band level (biquad band-pass in the
AudioWorklet — no audio is uploaded) and streams dBFS frames to
`/balance/meter`; the server computes recent floor, target (floor + 15
dB, min −55 dBFS), frame liveness, bounded floor-wait failure, and
lock. `/balance/lock` remains as a compatibility/manual seam, but the
shipped page no longer decides locks client-side. The server derives the
DRIVE level from monotonic-since-playback via the pure
`ramp_emission_dbfs` function.
Spawn/buffer/LAN/detection latencies are identical across the two passes
and cancel in `drive_delta_db`; locks inside the lead-in get
`keep_listening` (noise transient); a ramp that ends unheard marks that
channel `not_heard` — the actionable per-speaker error, retryable
per-speaker. `recommend_trims` is unchanged from v1 (attenuate-only,
quieter side renormalized to 0 dB, −24 floor surfaced as clamped).
The wizard (https://<host>/balance/, linked from the /rooms bonded
card; jasper/web/balance_flow.py riding INSIDE the correction
service's process + TLS origin, nginx 443 location /balance/ → :8770
prefix kept) holds ONE measurement_window across the whole walkthrough
(music doesn't blare back between speakers), released on completion /
the big Stop button / an INACTIVITY timeout (IDLE_TIMEOUT_S=90 s,
bumped by each ramp/lock/unheard so an active session is never yanked
mid-use however slow, while an abandoned phone tab releases the
renderers + wake loop within one idle window). Mutual exclusion with
correction is in-process: _reserve_start_slot consults
balance_flow.active_phase(), /balance/start sits behind correction's
idle check. /balance/apply writes one absolute /grouping/set per
member (peer first; partial failure reported per-member; idempotent
retry). Tests: tests/test_multiroom_balance.py (emission-function
contract incl. WAV-envelope relative-law tracking, determinism,
channel exclusivity, drive-delta sign, trim matrix),
tests/test_web_balance_flow.py (real background loop, terminable fake
playback, backend mic floor, backend meter-frame locks, exact lock
offsets via t0 rewind: gates, single held window, keep_listening,
not_heard + retry, full walkthrough → trims, stop, apply order/bodies,
correction exclusion), tests/test_measurement_volume_guard.py
(Camilla/Snapcast snapshot-normalize-restore), and
tests/test_multiroom_snapcast_rpc.py (Snapcast volume/mute RPC seams).
Updated 2026-06-24: `/rooms/`
now exposes pair balance as one centered slider (the UI clamps the ordinary
household adjustment to ±6 dB; the backend/grouping safety envelope remains the
validated attenuate-only -24..0 dB contract). `POST /trim` still
supports the legacy `target=self|peer` ±0.5 dB nudge, but the page uses
`target=pair` + signed `balance_db`, which rewrites BOTH member trims
absolutely and re-normalizes wasted attenuation so one side is always
0 dB. `JASPER_GROUPING_TRIM_DB` remains wizard/bond-owned intent — the LOUDER
speaker trims down, never a boost; outputd re-validates fail-closed. For dumb endpoints the
reconciler derives `JASPER_OUTPUTD_DAC_CONTENT_TRIM_DB` into
grouping-outputd.env (empty on solo = unset to env_f32) and outputd
applies one precomputed linear gain to the whole dac_content-armed path
(FIFO AND inv-B fallback periods — no level jump on starvation
transitions; applied before duck/mix/publish so the AEC reference carries
the trimmed program, inv-A) and reports `trim_db` in the dac_content
STATUS block. Active endpoints clear the dac_content lane, so their
driver-domain graph carries a dedicated non-positive `pair_balance_trim`
Gain after `channel_select` and before the driver split. An active leader
disables camilla#2 before the bake, re-seeds the crossover statefile, proves
the active-content PCM has been released, then starts camilla#2 from that
statefile; trim-only graph rewrites are picked up by process start, never by
trusting an idempotent `systemctl enable --now` to reload a running process.
Settable via /grouping/set (optional trim_db; PRESERVED when omitted, so
same-bond structural edits such as add-sub and swap never clobber a calibrated
balance). Fresh pair creation and unbond explicitly send `trim_db=0.0` so stale
balance from a prior bond cannot leak into the next topology. Earlier
same day (DUMB-FOLLOWER PR-C — the role-state contract
+ mode-aware interfaces. NEW §7.5: the unit×role table with transition
ownership, the interface contract, and the DSP two-kinds split (content
DSP = leader-side baked into the stream; driver DSP = local to the DAC
owner; the dual-Apple fail-closed note + the follower local driver-DSP
increment). Interface gates shipped with it: restart_voice_daemon — the
ONE helper all nine wizards use — skips while parked (a /voice//wake/
/transit/etc save no longer boots 240 MB of parked models; config
applies on unbond); /sources/ POST 409s while follower + toggles render
disabled with a pair note (enable --now would reopen the advertise/leak
hole); /system restart-voice 409s and restart-audio touches only the
alive subset; voice/wake/sound/correction pages carry the shared
pair_banner_html.) Earlier same day (DUMB-FOLLOWER PR-B — voice + the AEC stack
park while bonded, freeing ~310 MB (voice 238 + bridge 74) on a 1 GB
follower. Ownership stays single-writer: the GROUPING reconciler only
derives a Python-validated flag (JASPER_GROUPING_VOICE_PARK=1, written
into grouping-voice.env for an ACTIVE follower, omitted otherwise) and
KICKS jasper-aec-reconcile on change — that script remains the one owner
of the voice/bridge units and gains a single new park condition
(grep -Fxq on the exact flag line; bond-validity logic is never
re-derived in shell). Park is `disable --now` (mirroring the
provider-unset park) so a reboot can't boot-start 240 MB of models for
  seconds before re-parking; un-park is automatic (flag disappears →
  restart_voice re-enables; the grouping route matrix rewrites the right
  TTS socket state for the new role/channel). Role park wins over every
mic/profile shape including the custom-mic early exit. Doctor: the
bridge/mic liveness family (AEC bridge ×3, mic card, mic capture) reads
"parked (bonded follower)" via the shared skip idiom; the landing page's
mic card says "Paired — the assistant listens on the pair leader".
Accepted costs (owner sign-off): follower timers die on bond-form; dial
hold-to-talk is dead on a follower (volume/transport still forward).)
Earlier same day (DUMB-FOLLOWER PR-A — local source resource groups park
while bonded. `plan()` role=follower now stops the registry-owned park
units from `jasper/local_sources/registry.py`, including advertise-side
resources such as `jasper-usbsink-init.service`): a follower's local sources are structurally
unplayable — and a phantom AirPlay/Spotify session into the direct lane
AUDIBLY LEAKS during outputd's inv-B starvation-fallback periods, so
parking is correctness, not just UX. STOP, never disable — /sources/
keeps systemd enable/disable as the household's intent; solo/leader/
invalid plans carry NEW "restore" intents (start-only-if-enabled, applied
at the I/O layer) so unbond puts sources back exactly per the wizard.
_apply treats absent units as clean no-ops (the endpoint install tier's
dependency: stop/restore against never-installed units must not flip
rc). ShairportSupervisor gains a parked_by_role gate (no WARN buildup
against a deliberately-stopped shairport; snapshot says parked). The
dial's play/pause on a follower forwards to the leader (/transport/*
joins /volume* on the pair forward — with mux parked the local toggle
would be a dead button). Doctor: renderer liveness checks
(librespot/shairport/nqptp/mux/bluealsa) read "parked (bonded follower)"
via the NEW _parked_as_bonded_follower() shared skip idiom — the same
idiom PR-B's voice/AEC parking and the endpoint tier's doctor reuse.
check_grouping's runtime rows now include the parked units
(expected=stop), and only desired=start units can degrade health.
jasper-voice/jasper-aec-bridge are deliberately NOT in the parked set —
those units belong to jasper-aec-reconcile (single-writer rule), PR-B.)
Earlier same day (SESSION-REVIEW FIX BATCH — failure legibility +
cross-member coherence. postJSON now carries the server's JSON verdict on
the thrown error, so /rooms renders per-member swap/bond/unbond detail and
the rollback outcome instead of "HTTP 502"; the landing pair-volume slider
reverts to the last server-confirmed level and shows an em dash when
writes/polls fail (no more optimistic lie on a dead leader); the volume
forward relays a leader's own 4xx/5xx verdict verbatim (only transport
failures read "unreachable"). NEW doctor check `pair channels` (follower
GETs its leader's /grouping): a same-channel pair — interrupted swap whose
rollback also failed — was green on every member-local surface; /rooms
Swap now also REPAIRS that state (peer takes the opposite channel) instead
of rejecting it. leader_addr is shape-validated at validate_grouping
(hostname/IPv4 — it feeds snapclient argv, the forward's URL, the leader
link). Voice tools now compose jasper.control.client (the transport's one
owner) and the shared follower_leader_addr predicate; the grouping
supervisor's journal noise is latched (full WARN buildup once per
starvation streak, one rate-limited WARN per kick window, DEBUG
otherwise — /state counters unaffected); first-write-empty is pinned by
test.) Earlier same day (PAIR UX — follower landing-page banner +
pair-volume proxy + channel swap. jasper-control's /volume,
/volume/{set,adjust,mute} now FORWARD to the leader's control API when
this speaker is an active bonded follower (every follower volume surface
was INERT — bonded content bypasses the local CamillaDSP — so slider,
paired dial, and curl now control the PAIR volume from any member; loop
breaker via X-JTS-Pair-Forwarded; tiny load_config read per call, never
the runtime derive; responses additively tagged pair_leader). The
landing page polls GET /grouping (new exact-match nginx proxy) and on a
bonded member shows a stereo-pair banner; a FOLLOWER additionally hides
the source selector and relabels the slider "Pair volume" (leader link
gated on a hostname-shaped leader_addr; all text via textContent).
/rooms gained POST /swap — left↔right channel exchange for a 2-speaker
pair (roles/bond untouched, only channel flips through the same
/grouping/set fan-out; mono or >2-member bonds 400) — with a "Swap left
↔ right" button on the bonded card. Also fixed in passing: the /rooms
doc paragraph still claimed audio doesn't flow to followers, a pre-PR-1
leftover.) Earlier same day (INCREMENT 5 PR-2 BUILT — member-local TTS + the
grouping supervisor. outputd grew `tts.rs`: a server speaking fanin's exact
newline-framed TTS wire protocol (Python keeps ONE playout implementation;
the wire layer itself — command vocabulary + parser — was extracted to the
shared `rust/jasper-tts-protocol` crate right after merge, so the two
servers structurally cannot drift),
feeding the surviving OutputCore engine; the FLUSH_SYNC barge-in ack now
carries DAC-true audio_played_ms + segment events from the playout ledger
(fanin's ack hardcodes 0). The reconciler arms both ends while bonded —
JASPER_OUTPUTD_TTS_SOCKET in grouping-outputd.env, JASPER_TTS_OUTPUTD_SOCKET
in grouping-voice.env (solo OMITS the key; a fresh solo reconcile skips
creating an empty file so first boot doesn't restart voice) — and restarts
voice on change. Bonded TTS is therefore member-local + instant (inv-3;
PROGRAM_DUCK rides the same socket, so ducking is member-local too; inv-A
holds because TTS mixes pre-reference-publish). Solo is byte-identical to
pre-PR-2. Doctor: `TTS lane` (replaces the PR-1 standing `TTS interim`
warn) verifies the two env files agree, catching the silent-assistant
drift (voice targeting a socket outputd never armed) and the stale-solo
override. NEW jasper/control/grouping_supervisor.py (mirrors the shairport
supervisor shape): every bonded member polls outputd's
dac_content.serving_fifo each 30 s; 3 consecutive starved polls →
reset-failed + restart --no-block jasper-grouping-reconcile, rate-limited
1/10 min; the leader re-runs the ensure_groups_on_stream ownership pin
every poll (continuous binding read-repair, ≤30 s self-heal for runtime
rebinds); /state.resilience.grouping_supervisor;
JASPER_GROUPING_SUPERVISOR=disabled off-switch. Auto-unwind to solo
deliberately NOT built — disband stays one tap on /rooms until a real
non-converging failure is observed.) Earlier same day (SNAPCAST BINDING
PIN + CLIENT-TRUTH HEALTH — the
durable fixes for the silent-bond class found at bring-up. snapcast PERSISTS
group→stream assignments (server.json); both speakers' groups predated our
stream (bound to the distro era's "default") and played ZEROS behind fully
green health. NEW jasper/multiroom/snapcast_rpc.py (stdlib JSON-RPC against
the local snapserver, injectable transport): the reconciler now runs
ensure_groups_on_stream(SNAP_STREAM_ID) on every leader reconcile — every
persisted group (disconnected clients' groups too) is re-bound to OUR stream,
with bounded retries for the just-started server; unreachable/failed flips
the oneshot rc. SNAP_STREAM_ID is now ONE constant (argv builder + pin +
health). Runtime health (/state + doctor, the same derive) gained the
stream-client truth for leaders: a CONNECTED client bound elsewhere, a
CONNECTED client muted/volume-0 (snapclient's software mixer plays zeros),
or the leader's OWN client absent ⇒ degraded with the specific client named;
snapserver RPC unreachable ⇒ an explicit degraded verdict, never a silent
skip. Disconnected stale bindings deliberately do NOT degrade (the pin's
job, not health's). Earlier same day (INCIDENT: the jts3 boot loop + the
writer/validator coherence fix. At ~15:51 EDT jts3 boot-looped 3× in under a
minute: the grouping reconciler armed the round-trip lane
(grouping-outputd.env: FIFO+channel) while the lab's content-bridge soak
(/var/lib/jasper/outputd.env: CONTENT_BRIDGE=rate_match) sat in a LOWER env
layer — systemd composed the exact combination outputd's fail-closed guard
(eed2e5cf) rejects, outputd crash-looped, StartLimitAction=reboot fired, and
the T5.1 boot-loop guard (PR #573) contained it on boot 3 — its first
production save, the same day it was hardware-validated. The reconciler's
rc=1 at 15:50:36 was by-design surfacing (outputd_restart_failed), not a
separate bug. FIXES: (1) writer/validator coherence — outputd_grouping_env
pins CONTENT_BRIDGE=direct while bonded (grouping-outputd.env is the LAST
env layer, so the lane's hard requirement wins over lab retunes) and OMITS
the key when solo (never present-but-empty — outputd bails on an empty
bridge mode — and solo falls back to the lower layers, so the rate_match
soak and bonding now COEXIST); pinned by a writer-side parity test, and the
doctor's channel-pick check warns on a stale un-pinned file. (2)
Park-not-reboot — outputd exits EX_CONFIG (78) on config-validation failure
and the unit sets RestartPreventExitStatus=78: a fail-closed config
rejection now PARKS outputd failed (visible) instead of crash-looping into
reboot, killing the escalation class regardless of which combination goes
bad next; pinned by a unit↔code contract test. (3) install.sh enables +
runs jasper-grouping-reconcile (boot/install no-op when solo) so a bonded
speaker survives reboots — the gap that left jts3's snapclient down after
its recovery reboot. Also from the same bring-up: snapcast's persisted
group→stream registry bound both clients to a stale "default" stream
(silent bond behind green health — fixed live via Group.SetStream RPC;
the durable registry pin + client-connected runtime-health truthing are the
NEXT fixes). Earlier same day (CAMILLA PIPE-GUARD — the post-merge review of PR-1
measured camilladsp 4.1.3's dead-File-sink behavior on jts3 (absent path →
clean exit 0; readerless FIFO → blocked open(2) that ignores SIGTERM) and
found the reboot-loop chain: clean-exit loop × Restart=always ×
StartLimitBurst=5/60s × StartLimitAction=reboot ⇒ a snapserver hard-death
while bonded reboots the Pi in <60 s, and reboot-loops if snapserver stays
broken. Chain broken by deploy/bin/jasper-camilla-pipe-guard — ExecStartPre=-
on jasper-camilla, pure bash, FAIL-OPEN (missing statefile/base/probe-tool ⇒
no-op; a repair needs positive evidence of a dead pipe): bonded statefile +
dead pipe ⇒ statefile repaired to the base config before launch, camilla
starts solo-safe, grouping.env stays bonded for self-healing re-apply, loud
event=camilla_pipe_guard.repaired. The same review also shipped the
validated-solo stash guard (#612: a /sound save while bonded regenerates the
wizard's file pipe-shaped; the stash must never write nor restore one — both
ends guarded, pinned against real emitted configs). Earlier same day
(INCREMENT 5 PR-1 BUILT — the bonded MUSIC dataplane,
end-to-end. The emitter gained the `playback_pipe_path` axis (File→snapfifo
sink; pipe requires rate_adjust=false; never combines with the member weave);
`member_camilla_kwargs` moved to the canonical leader-bake policy (leader →
pipe; follower → solo defaults — its camilla feeds only the inv-B fallback
lane), so a /sound save while bonded regenerates the pipe config instead of
silently un-bonding camilla; NEW `jasper/multiroom/leader_config.py` owns the
bonded apply + solo-restore unwind ladder (persistent VALIDATED-SOLO prior-config
stash — never written from nor restored to a pipe-shaped config → re-emit-solo
fallback → no-op on the common solo reconcile), reusing the
shared validated `apply_dsp_config` engine + camilla's glitch-free config
swap; the reconciler orchestrates everything in a load-bearing, test-pinned
order (derived files+FIFO → solo-restore → outputd restart only on lane-env
change → units → bonded apply LAST so the pipe's reader exists before
camilla's write-open); every member's snapclient is on the `file` player
(the raw-DAC fight was the observed pre-Inc-5 failure: snapclient
`Device or resource busy` × 10/s against outputd); outputd boots
bonded-ready via the persistent `/var/lib/jasper/grouping-outputd.env`
(registered in env_load's ENV_FILES + the voice-sourcing registry — both
drift guards fired in test and were satisfied); runtime health (/state +
doctor) reads producer liveness from the ACTIVE camilla config
(`active_leader_pipe_path`, daemon-adjacent truth) with the happy path
pinned end-to-end against a REAL emitted config; doctor checks reworked to
the canonical model (`rate_adjust` leader-scoped; `channel_split` →
`channel_pick` env-drift; NEW `leader_pipe`; NEW STANDING `TTS interim`
warn while bonded); install.sh disables the DISTRO snapserver/snapclient
units (Trixie's package ships an enabled-by-default rogue server on :1704 —
observed live on jts3, where a bare snapclient auto-discovered the rogue
instead of the leader; killed + codified). Design note: the leader's camilla
keeps capturing lane 7 — all 8 loopback substreams are allocated, and PR-2's
TTS socket flip makes lane 7 music-only by construction, so Increment 1's
fanin music tap is NOT used by this design. KNOWN GAP (standing doctor
warn): TTS rides the synced stream until PR-2 (outputd TTS mixer + per-member
socket flip + the inv-B auto-unwind supervisor). Increment 4 folded into the
Increment 5 bring-up: acoustic L/R validation happens on the real path.
Earlier 2026-06-11 (INCREMENT 3 BUILT — the outputd `dac_content` FIFO
reader, `rust/jasper-outputd/src/dac_content.rs`. The round-trip lane's receiving
end: lazy non-blocking FIFO source gated on `JASPER_OUTPUTD_DAC_CONTENT_FIFO` +
`…_CHANNEL` (channel-split vocabulary; snapclient's `--player file` has no ALSA
hop for the member's ttable drop, so the source does the pick — left/right
duplicate, mono clip-safe average, fail-loud on unknown). inv-B mechanics:
starts-in-fallback until the FIFO demonstrates ~210 ms of health, same-period
fallback on starvation (zero silence), damped recovery (no flapping between
time-offset copies); bounded staging (~170 ms, oldest-drop overflow); the direct
lane is drained (bounded, discard) while the FIFO serves so an upstream loopback
writer can never stall. STATUS gains the self-reported `dac_content` block
(daemon truth). Config fail-louds: rejects rate-match-bridge and dual-Apple
combinations. The now-Rust-read `JASPER_OUTPUTD_DAC_CONTENT_FIFO` wire-contract
exception was dropped per the bidirectional guard. Pure split inside (assembler +
policy unit-tested; 4 real-temp-FIFO end-to-end tests incl. a clocked
never-blocks assertion). Zero solo cost (None ⇒ the loop is byte-identical); no
reconciler wiring — the lane activates in Increment 5. Earlier 2026-06-11:
the always-on `_jasper-control._tcp` advert gained a `peer_id=` TXT record
(#603 — the stable `/var/lib/jasper/peer_id` identity, rendered next to the
friendly `name=`). Recorded in the §8-adjacent "Friendly names + identity"
prose as the handle the bond-forming UI should pin leaders by when it lands:
store peer_id, resolve to an address at use time, treat mDNS as
unauthenticated (confirm trust-sensitive operations over HTTP) — see
docs/HANDOFF-identity.md. Earlier 2026-06-11
(status + roadmap reconciliation after the #591 cleanup
merge. The TOP BANNER was stale on three counts and is rewritten to current
truth: Increments 1–2 are SHIPPED (it predated them), the P0 spike RAN on
hardware and passed its resource/software-sync gates (it said "not run"), and
the removed `SNAPFIFO_PRODUCER_WIRED` flag is no longer cited (it was deleted in
#591). §0's heading date bumped. NEW "Sequencing + owner gates" note at the end
of the §2 increment plan records the build order (3 → 4 → 5 → 6), what each step
needs from the owner (Inc 3: nothing; Inc 4: ~20 min at the speakers, before Inc
5's ship decision; Inc 5: the inv-A wake-during-music + inv-B kill-the-loopback
validation sessions, plus two owner decision points — the acoustic-quality call
and the buffer-size trade-off; Inc 6: one measurement session after Inc 5), and
the deliverable (bond on /rooms → sample-locked music on both speakers, leader
still answers instantly). Doc-only; no code changed. Earlier 2026-06-11
(STRANDED-MACHINERY CLEANUP executed + the master_gain
docstring precision fix. Removed the retired outputd-as-producer machinery the
canonical design left dead: `SnapfifoSink` (`snapfifo.rs` deleted),
`SNAPFIFO_PRODUCER_WIRED` + `effective_leader_tap_path`, the reconciler's
outputd tap-env write / change-gate / try-restart limb (the reconciler now
manages ONLY the snap units — it never touches the reboot-on-fail outputd), the
unit's optional tap `EnvironmentFile=`, the now-dead
`JASPER_OUTPUTD_SNAPFIFO_PATH` wire-contract exception, and
`check_grouping_tts_separation` (its operator story folded into
`check_grouping`'s runtime detail: a bonded leader reads degraded with "leader
streaming is not built yet — no music producer feeds the snapfifo"; the async
check keeps order=79 — the registry contract is async-sorts-LAST, not
contiguous integers (resilience's fractional 78.5 insert proves it), so the
gap at 78 is intentional). Deliberately KEPT:
`channel_split.py` / `member_config.py` / their doctor checks (live in the
shipped member model until Increment 5 — the mutual-exclusion guard polices
that migration), `desired_snapfifo_path` (the pure needs-a-producer predicate),
and snapserver's pipe source (the consumer side is unchanged). §0 + §2 updated
to past-tense; tests updated (tap-machinery tests removed, leader-degraded +
production-default coverage added). Earlier 2026-06-11 (post-merge cross-agent
review fixes on PR #587 — a Codex
staff review found four real issues, all fixed: (1) the new shared
`emit_master_gain_pipeline` docstring mis-stated the Ducker mechanism (it
attenuates CamillaDSP's `main_volume`, NOT the `master_gain` mixer — master_gain
is the preserved identity anchor; corrected in `jasper/camilla_emit.py`); (2)
`room_peqs_right` × `channel_split` are different topology models (leader-bake vs
the superseded member weave) and now raise `ValueError` when combined — fail-loud
at the API boundary before Increment 5 wires callers; (3) §4's "gotcha (still
live)" / `target_channels` phrasing contradicted the built Increment 2 axis —
reconciled; (4) the right-channel extraction warning is now a stable
`event=sound.extract_room_peqs result=right_channel_ignored` line per house
observability style. Earlier 2026-06-11 (research banked from two external
deep-research passes,
cross-checked against CHIP-AEC-EXPERIMENT.md's measured ground truth. inv-A
RECALIBRATED: the prior is now "gate should pass" — the round-trip never enters the
reference path (tap is downstream of stuffing; outputd re-paces; mic + chip USB-IN +
sync-USB DAC share one USB-SOF domain, measured ~1 ppm) — and the gate criteria were
corrected to DELTA (bonded ≈ solo ±1–2 dB over ≥10 stuff events) + PRODUCT (wake FRR
/ barge-in during music), explicitly NOT an absolute dB bar: the first research pass
suggested "ERLE > 20–25 dB," which our working solo rig (~14.5 dB linear residual —
normal per Amazon US 10,586,534's 15–25 dB steady-state range) would have failed; a
gate that fails a working system is miscalibrated. inv-A also gained the ranked
fallback ladder (duck-on-candidate → software ARA per CHIP-AEC-EXPERIMENT.md's new
"Beamformed-reference (ARA) fallback" section → conservative-only suppression →
inv-B bypass). Build mechanics gained FLAC chunk_ms ≈ 26 ms / PCM-first guidance and
the two-knob alignment split (snapclient --latency = electrical path; CamillaDSP
per-channel Delay = acoustic arrival). §7 records WiFi power-save-off as
load-bearing for snapclient endpoints (install.sh already ships it). Rejected from
the research, with reasons recorded earlier: the XVF-as-I²S-master prescription (our
documented last resort; our USB topology is measured-coherent) and abandoning the
localhost self-loop (rested on a post-buffer inconsistency; SPOF is inv-B's job).
Doc-only; no code changed. Earlier 2026-06-10 (CANONICAL ARCHITECTURE made
BUILD-READY after a two-agent
adversarial design review (codebase-fit + design). The review confirmed the
high-level architecture but surfaced real issues the "DECIDED" doc had not
confronted; all are now recorded. Added **inv-A** (the AEC reference must stay
`== final DAC content`, TTS-inclusive, post-round-trip — else wake-during-music
breaks; routing leader TTS to outputd is a REBUILD of the retired outputd TTS mixer +
barge-in ledger, and chip-AEC ERLE on a bonded leader is a HARDWARE GATE) and
**inv-B** (the leader self-loop must fall back to the direct fanin path, never go
silent on its own music — added a §7 failure-table row). Re-scoped the increments:
the old "2a = followers play" was dishonest (it silences the leader); the new order
puts hardware-free / non-silencing slices first (`target_channels` per-channel
correction → outputd FIFO reader → acoustic-sync spike) before the leader
CamillaDSP→pipe + round-trip + TTS-rebuild slice, which is gated on inv-A/inv-B.
Recorded that the shipped `SnapfifoSink`/`SNAPFIFO_PRODUCER_WIRED`/outputd-tap limb
and `channel_split.py`/`member_config.py` are superseded BY DESIGN (CamillaDSP feeds
the pipe, dumb receivers) — retire/repurpose, not "pending wiring" (§0 + §2 updated).
§5 volume: per-member/follower volume moves to Snapcast per-client volume (a dumb
follower has no master_gain; the leader's is now pre-buffer). §2 records the 2.1 call:
a single 3-channel stream, NOT a second stream (which drifts). Doc-only; no code
changed. Earlier 2026-06-10 (CANONICAL ARCHITECTURE decided + recorded, after a
second prior-art research pass on implementation mechanics — Snapcast stereo-pair
channel routing, CamillaDSP↔Snapcast chaining, open-loop satellite calibration —
and the P0 sync spike running on hardware (jts3↔jts: ~15 MB Pss, ~0.2 % CPU, sync
proxy clean across all buffers/codecs). Owner decision: the brainy LEADER bakes ALL
per-channel correction in its ONE CamillaDSP; receivers are DUMB channel-droppers
(ALSA `ttable`), no second DSP — RAM target a 1 GB Pi. New authoritative "Canonical
signal flow" section in §2 records the chain (fanin music-only → CamillaDSP
per-channel correct → pipe → ONE stereo snapserver stream → each speaker
channel-drops; leader self-loops via `-h 127.0.0.1` snapclient for tight sync; TTS
re-joins low-latency at outputd for the leader role; per-follower correction is PEQ,
open-loop, fast-follow). The older "inv-2 realization", §4's member-self-correct
model, and §0 were marked superseded / updated to point at it; two reversals from
the prior "corrected contract" — the leader plays the BUFFERED round-trip (not the
direct fanin output) and TTS re-joins at outputd (re-adding an outputd TTS mix for
the leader role only, which 9102e13 retired for solo). Increment 1
(`JASPER_FANIN_MUSIC_OUTPUT_PCM`) is repurposed as the foundation; next is Increment
2a (followers play). Doc-only; no code changed. Earlier 2026-06-10
(single-source-of-truth for the snapfifo producer +
doc honesty pass. Replaced the `/state` "leader is streaming" band-aid with ONE
flag — `reconcile.SNAPFIFO_PRODUCER_WIRED` (`False` today, because 9102e13 left
the outputd producer unwired). `effective_leader_tap_path()` returns "" while
the flag is `False`, so `/state`'s `derive_grouping_runtime`, `jasper-doctor`'s
`check_grouping`, AND `check_grouping_tts_separation` all read the SAME truth: a
formed leader reads `degraded` everywhere (no false-green), and the
TTS-separation warning AUTO-RESOLVES the moment inv-2 flips the flag. Reconciled
the doc with that flag — §0, the §2 "Where it taps" note, and the §8 "Shipped"
bullet no longer claim "audio FLOWs to followers" (it does not — the producer is
unwired), and §2 "inv-2 realization" now marks the naive dual-read design (the
outputd-`ReferenceFanout` tap + the missing leader-local TTS) **superseded** by
the corrected fanin-music-only contract above it, per the repo's "current state
first, single source of truth, history below" doc rule. 397 affected tests
green, ruff clean; no audio path touched. Earlier 2026-06-09 (TTS-separation
scoping + inv-3 GUARD, via a 4-agent
investigation workflow. The investigation CORRECTED an earlier overstatement:
the `SnapfifoSink` is not "already leaking" — it is **unwired DEAD CODE**
(`grep SnapfifoSink rust/` hits only `snapfifo.rs`; commit 9102e13 removed the
`config.rs` field + `main.rs` wiring that 050d334 added, when it moved TTS into
fanin). So the leak is DOUBLY latent: the producer is unwired AND there is no
follower playback. The proportionate inv-3 fix is therefore a GUARD against
accidental re-wiring, not a blind audio change: a WARNING doc-comment on
`SnapfifoSink` (`snapfifo.rs`) + `lib.rs`, and a hardware-free `jasper-doctor`
`check_grouping_tts_separation` (order 78) that warns an active LEADER its
streaming is behind the blocker (so a bonded leader isn't mistaken for a working
stream — the reconciler tap env + the runtime-health leader-tap signal are now
intent-only). Scoping deliverable: the corrected TTS-aware inv-2 design now at
the top of §2 "inv-2 realization" — the elegant framing is to KEEP TTS in fanin
and emit a second MUSIC-ONLY output (one change = the inv-3 fix AND inv-2's
music half), divert only MUSIC through the round-trip, and leave the assistant
leader-local (§6) — avoiding the outputd TTS IPC that 9102e13 retired. The real
music-only-tap + round-trip code lands with inv-2, validated on ≥2 Pis (never as
a blind gated reroute). 209 affected tests green, ruff clean; the guard touches
no audio path. Earlier 2026-06-09 (PR-3b attempt — found inv-2 is BLOCKED on TTS
separation, design corrected instead of building a known-wrong reroute. Reading
the real `jasper-outputd` `run_alsa` loop to wire the leader DAC-content reader
surfaced that `lib.rs` states "Assistant/TTS ingress is owned by jasper-fanin"
— so TTS is mixed into the music UPSTREAM of outputd, inside the `content_buf`
that the loop writes to the DAC and taps to the snapfifo. Two consequences: (1)
swapping the leader's DAC to the music-only buffered round-trip (`dac_content`)
would DROP the leader's assistant audio mid-session; (2) the already-shipped
`SnapfifoSink` taps `content_buf` = music+TTS, so it LEAKS the leader's TTS to
followers (a latent inv-3 violation). The correct inv-2 needs TTS SEPARATED — a
fanin music-only stream for the leader's tap + a post-round-trip TTS re-mix onto
`dac_content` — which is materially larger than "a FIFO reader" and re-opens
where TTS mixes. Recorded the BLOCKER + corrected contract at the top of §2
"inv-2 realization"; §0 updated. The `DacContentSource` FIFO reader (a clean
mirror of `snapfifo.rs`) is the right music-half component but is NOT wired —
wiring it blind would ship the regression. No code shipped this round on
purpose: a known-wrong gated reroute is a landmine, not progress. Earlier
2026-06-09 (review nit — DRY'd the doctor's config-field scanners.
`check_camilla_volume_limit`, `check_grouping_rate_adjust`, and
`check_grouping_channel_split` each hand-rolled the same "scan a CamillaDSP
config field from a top-level block" line-scan (3×, a fragile parser
proliferating). Collapsed onto ONE shared `_camilla_block_field(text, block,
key)` in `jasper/cli/doctor/_shared.py` — the doctor's deliberately fail-soft
(never-raises) alternative to `yaml.safe_load`. Also tightened the channel-split
weave's 2-channel guard to reject a config that OMITS `channels` (not just one
that sets it != 2). Behavior-preserving (270 existing scanner/doctor/weave tests
green) + direct coverage for the shared scanner and the guard; 377 affected
green, ruff clean. Earlier 2026-06-09 (staff-review fixes on the sample-lock
work — the member-config LAYERING. The inv-5 + channel-split transforms were
threaded into
the `/sound` apply call sites only, leaving the `/correction` apply path
uncovered (a bonded member correcting its OWN seat — the §4 path — got neither),
with no observability for a missing channel-split. Fixed by collapsing the
decision into ONE grouping-owned policy `jasper/multiroom/member_config.py`
`member_camilla_kwargs(cfg)` (inv-5 rate_adjust off + the channel-split),
applied identically on BOTH wizard paths via `**member_camilla_kwargs()` — the
SAME policy the inv-2 reconciler will reuse for CamillaDSP-B (so it is the
scalable chokepoint, not throwaway). Added `jasper-doctor`
`check_grouping_channel_split` (order 75) — a missing channel-split is SILENT
(plays full stereo, wrong channel) so it needs its own backstop. Hardened the
weave validator: rejects non-2-channel configs (active-speaker weave is future
work) and asserts `channel_select` runs IMMEDIATELY after `master_gain`
(position, not just presence). Collapsed the redundant
`disables_local_rate_adjust` alias into `is_active_member`. **Deliberately NOT
done here:** auto-apply on bond-form — that belongs to the inv-2 reconciler
(building it now would bake the channel-split into the pre-snapclient position,
the wrong topology); until then `check_grouping_channel_split` keeps the gap
visible. 375 affected tests green, ruff clean. Earlier 2026-06-09 (inv-2 leader
content lane — DESIGN + inert
scaffolding. Resolved the topology §4 deferred (the "P1.3 integration
decision"): the leader = a shared streamer + its own follower — CamillaDSP-A
(shared, clamped, NO channel-split) feeds the `SnapfifoSink` tap → SNAPFIFO →
snapserver; the leader's own snapclient (`--host 127.0.0.1`) writes the
buffered round-trip to a raw-PCM FIFO via `--player file` (NEVER snd-aloop —
inv-2's `snd_pcm_delay`-lies dodge) → CamillaDSP-B (post-snapclient
channel-select + per-seat correction, `rate_adjust=false`) → outputd
DAC-content lane → DAC. The tap source (shared) and DAC source (this leader's
post-snapclient stream) are deliberately DISTINCT — that's the loop avoidance.
Full flow + the outputd DAC-content contract + the rejected alternatives + inv
compliance + the on-device validation plan are in §2 "inv-2 realization (DESIGN;
not yet built)". Inert scaffolding: `LEADER_CONTENT_LANE_GATE` (off-by-default
master gate) + `MEMBER_CONTENT_FIFO` / `OUTPUTD_DAC_CONTENT_FIFO_ENV` env
contract + a gated `snapclient_argv(player_fifo=…)` param whose default is
BYTE-FOR-BYTE the pre-inv-2 command (90 reconcile/state tests green, ruff
clean). The outputd Rust DAC-content reader is the next PR, built against this
design + validated on ≥2 Pis. Earlier 2026-06-09 (channel-split LIVE WEAVE
SHIPPED — a bonded member
that plays a single channel now actually filters it. `weave_channel_split(yaml,
split)` (`jasper/multiroom/channel_split.py`) splices the P1.2 `ChannelSplit`
fragment into a generated CamillaDSP config: the `channel_select` mixer under
`mixers:`, the sub crossover under `filters:`, the `channel_select` Mixer step
right after `master_gain` in the pipeline, and the crossover appended LAST to
each per-channel `Filter`. `stereo` is byte-for-byte passthrough; the woven
result is parsed + structurally validated (channel_select in mixers AND
pipeline), failing LOUD on a config missing the anchors rather than emitting a
broken DSP config. `emit_sound_config(channel_split=…)` weaves inside (before
the out_path write); the live `/sound` apply path builds the split from the
member's `cfg.channel` for an active member only. `master_gain` /
`volume_limit: 0.0` untouched (Ducker + safety contracts hold). 127 weave/sound
tests green, ruff clean. **Still on-device:** §2 inv. 2 (the leader content
lane — the Rust outputd change) is now the ONLY remaining sample-lock piece,
then end-to-end + acoustic validation. Earlier 2026-06-09 (inv-5 SHIPPED —
exactly one rate-adjuster per
chain: a grouped member's local CamillaDSP now runs `enable_rate_adjust:
false` so it doesn't fight snapclient's sample-stuffing (the documented
`rate_adjust`+`AsyncSinc` oscillation). `disables_local_rate_adjust(cfg)` /
`is_active_member(cfg)` predicates (`jasper/multiroom/config.py`) drive an
`enable_rate_adjust` param on the `correction` + `sound` generators (the live
`/sound` apply path passes it); `jasper-doctor`'s `check_grouping_rate_adjust`
(order 74) is the universal backstop — it reads the ACTIVE config, so it
catches every generator and a stale config generated before the bond formed,
warning to regenerate. 260 affected tests green, ruff clean. **Still
on-device:** §2 inv. 2 (the leader content lane — the Rust outputd change) and
the channel-split live weave. Earlier 2026-06-09 (second staff-review pass —
three follow-ups on the
hardening below: (1) the GET /grouping wire contract now has ONE home —
`grouping_response` / `parse_grouping_response` (+ `GROUPING_RESPONSE_KEY`) in
`jasper/multiroom/state.py`, used by both the `jasper-control` producer and the
`/rooms` `_get_member_grouping` consumer, locked by a round-trip test + a
cross-boundary assertion in the control-server test, so the two daemons can't
drift again (the C4 root cause). (2) per-member fan-out failures now log
`event=rooms.bond.member_failed` / `rooms.unbond.member_failed` (addr + reason,
failures only) so a half-formed/half-dissolved bond names the culprit in the
journal, not just an aggregate. (3) new §8 "Known scaling boundaries & future
extraction points" documents the `PeerControlClient` extraction trigger (3rd
cross-speaker call), the pair-shaped topology + `makeBondCard` split point for
multi-member, and best-effort-dissolve-by-liveness as an accepted property.
Coverage added in `test_multiroom_state.py` (round-trip + unknown-cases),
`test_control_server.py` (cross-boundary parse), `test_web_rooms_setup.py`
(per-member-failure caplog); 196 affected tests green, ruff clean. **Unchanged:**
§2 inv. 2 sample-lock is still a "preview.") Earlier 2026-06-09 (grouping
hardening — staff-review fixes on the
bond control plane: (1) `leader_addr` is now a **stable mDNS `.local`
handle** minted by the bond wizard (the leader's `JASPER_HOSTNAME`), not a
raw DHCP IP, so a follower survives the leader changing IP —
`reconcile.snapclient_argv` passes it verbatim and snapclient re-resolves
at connect time (no reconcile change needed; literal IPv4 still accepted).
(2) New operational surface: `POST /unbond` on `/rooms` dissolves a bond by
discovering membership via the new CSRF-free `GET /grouping` read on
`jasper-control` and fanning `{enabled:false, trim_db:0.0}` to matches + self; the
bond/unbond fan-out now runs concurrently across members. (3) Documented
the grouping control plane's threat model (§7): `POST /grouping/set` / `GET
/grouping` / the fan-out are UNAUTHENTICATED by design — the same home-LAN
trust model as the dial `/volume`; the SSRF guard bounds cross-speaker
targets to private/loopback IPv4 (bare hostnames rejected) and grants no
capability a LAN client lacked, but bond/unbond makes "LAN = trusted"
load-bearing ACROSS devices — an explicit, accepted home-appliance
trade-off. (4) `config.py` now documents the leader_addr IPv4-or-mDNS
acceptance and the intentional codec asymmetry (the wizard never sets
codec, so `/grouping/set` validates against `DEFAULT_CODEC`; the operator's
codec is preserved by `_write_grouping`'s read-modify-write and
re-validated fail-loud by `load_config` on read). Coverage: reconcile
regression that a follower `leader_addr="jts3.local"` yields `--host`
immediately followed by `jts3.local` (56 reconcile tests, ruff clean).
**Unchanged by this work:** the §2 inv. 2 sample-lock "preview" status —
the audio half is still not fully validated. Earlier 2026-06-09
(bond-forming UI — the Sonos-style one-flow
stereo-pair setup landed on `/rooms`: a bond card (pick a sibling for the
right channel, Save) + the wizard's `POST /bond` that mints a `bond_id` and
fans the grouping config out SERVER-side to each member's `jasper-control
/grouping/set` (leader/left = this speaker, follower/right = the picked
one), SSRF-guarded to private/loopback IPv4. Builds on the prior `POST
/grouping/set` control endpoint + shared `validate_grouping` (read/write
share one validator). Configuration is now fully automatic — no
per-speaker tinkering; audio FLOWs once the leader forms (producer + tap),
with sample-lock (§2 inv. 2) the honest "preview" gap. Coverage:
`_post_grouping_to_member` SSRF/self-routing + `/bond`
fan-out/partial-failure-502/bad-CSRF/empty-400 in
`tests/test_web_rooms_setup.py`; §0/§6 + this footer updated. Earlier
2026-06-09 (staff-review fixes): extracted shared
`jasper/atomic_io.py` (atomic_write_text) and migrated the reconciler's two
env writers onto it — the canonical home for the ~39 hand-rolled
tempfile+os.replace sites, migrated incrementally; and surfaced the leader
outputd-TAP status on `/state` + doctor (a configured leader not actually
feeding the snapfifo now reads `degraded`, via an injected `leader_tap_path`
into the pure derive). Built via a 6-agent workflow + adversarial verify;
786 affected-suite tests green, ruff clean. Earlier 2026-06-09 (P1.3 producer
activation): `reconcile.py` sets
`JASPER_OUTPUTD_SNAPFIFO_PATH` for a leader (pure `desired_snapfifo_path` +
`outputd_tap_action` change-gate) via a reconciler-owned env file that
`jasper-outputd.service` layers in optionally; `systemctl try-restart`s
outputd ONLY on an actual leader transition — never a steady-state reconcile
(load-bearing, outputd reboots on StartLimit). 54 reconcile tests inc. the
no-spurious-touch gate. Remaining: §2 inv. 2/5 + acoustic, on-device.
Earlier 2026-06-08 (P1.3 snapfifo producer): `jasper-outputd` gained
`SnapfifoSink` (`rust/jasper-outputd/src/snapfifo.rs`) + a writer thread —
a grouping leader taps post-clamp stereo to a bounded drop-on-full channel,
a dedicated thread does the blocking FIFO write (DAC loop never
back-pressured, §2 inv. 1; separate consumer from AEC, inv. 4); lazy
non-blocking FIFO open, reopen-on-broken-pipe; off-by-default behind
`JASPER_OUTPUTD_SNAPFIFO_PATH`. Hardware-free temp-FIFO `cargo test`;
reconciler-sets-env + end-to-end are the on-device follow-ons. §0/§2
updated. Earlier 2026-06-08 — grouping runtime observability: `state.py`
gained a pure `derive_grouping_runtime` + injectable `systemctl is-active`
probe, so `/state.grouping` carries a live `runtime` health block
(off/invalid/ok/degraded) and `jasper-doctor`'s `check_grouping` warns on
a configured-but-degraded bond — §7 "make it visible, not invisible";
zero probe + no runtime key when solo. The `/rooms` ES module renders the
block too (amber **Degraded** badge + reason), completing the /state +
doctor + dashboard triad. Earlier 2026-06-08 — shared CamillaDSP emission
layer +
channel boundary: extracted `jasper/camilla_emit.py` (`fmt`,
`emit_gain_filter`, `emit_peaking_biquad`, `emit_linkwitz_riley`,
`emit_mixer`) and migrated all four DSP generators (correction / sound /
active-speaker / multi-room) onto it — shipped subsystems golden-diffed
byte-identical, multi-room crossover upgraded to native `BiquadCombo`;
documented + tested the inter-speaker `channel` vs intra-speaker
`SpeakerChannel` boundary (channel-select is interface-preserving 2→2).
746 hardware-free tests green. Earlier 2026-06-08 (P1.2 channel-split):
`jasper/multiroom/channel_split.py` emits the pure, host-agnostic
`channel_select` Mixer + sub crossover fragment — clip-safe
−6.02 dB L+R sum, `master_gain` left identity for the Ducker, no
positive gain; hardware-free tests incl. a weave into the real
`outputd-cutover.yml`; live weaving deferred to P1.3.
Earlier 2026-06-08 / updated 2026-06-14: combined `/rooms` "Speakers"
surface: the wake-response (peering) toggle + Primary checkbox lives on
`/rooms`, which is canonical and has no legacy `/peers` redirect. `rooms_setup`
uses `jasper.peering.config`'s readers/`PEERING_ENV_FILE` — `POST /peering`
(CSRF via `X-CSRF-Token`) read-modify-writes `peering.env`
preserving `JASPER_PEER_ROOM`, and the self block in `/rooms.json` gains a
`peering: {enabled, primary}` block read fresh from the SSOT. Room is NOT
edited on `/rooms` — it stays at `/speaker/` (the self card links there).
Bond-forming controls remain deferred (§8 P0). Coverage added in
`tests/test_web_rooms_setup.py` (POST happy path / bad-CSRF reject /
unknown-path-404-before-CSRF / `peering`-block shape / no legacy `/peers`
route string-assert). Earlier 2026-06-07
(identity/discovery refactor): extracted three
shared primitives — the one mDNS-SD browse `jasper/mdns.py` (`browse_once`),
the one Avahi `*.service` renderer `jasper/avahi_service.py`
(`render_service`, now used by both `control_advert` and `peering/avahi`),
and the single speaker-identity reader `jasper/identity.py`
(`read_identity()`). The **room label moved into the speaker-identity home**
(`jasper/speaker_name.py`, `JASPER_SPEAKER_ROOM`; `/speaker` writes it;
`install.sh migrate_speaker_room` seeds it from the legacy peering room);
`/rooms/`'s self block reads name/room/hostname through `read_identity()`.
Peering still accepts the legacy `JASPER_PEER_ROOM` fallback for older env
files; the user-facing room editor now lives only in `/speaker/` — see §8
"Friendly names + identity". Hardware-free coverage: `tests/test_mdns.py`,
`tests/test_avahi_service.py`, `tests/test_identity.py`, the room half of
`tests/test_speaker_name.py`, and `tests/test_web_rooms_setup.py`. Earlier
2026-06-07: friendly-name advertising (`name=` TXT on `_jasper-control._tcp`).
Off-by-default plumbing + the read-only `/rooms/` directory previously landed
2026-06-04; bond-forming controls and the §8 sync/RAM numbers remain
deferred/unmeasured until the spike runs on hardware.)

---

Last verified: 2026-06-26 (`/rooms/` backend-owned stereo-pair intent,
primary subwoofer-control hiding, ±6 dB UI balance range, and fresh
pair/unbond trim reset rechecked against `jasper/web/rooms_setup.py`,
`deploy/assets/rooms/js/main.js`, `deploy/assets/rooms/js/grouping-view.js`,
`tests/test_web_rooms_setup.py`, and live jts4/jts5 browser + SSH trim
verification; pair-balance backend-meter flow and
Camilla/Snapcast volume guard rechecked against `jasper/web/balance_flow.py`,
`jasper/measurement/volume_guard.py`, and `deploy/assets/balance/js/main.js`;
Camilla pipe-guard/recovery-budget wording
rechecked against `deploy/systemd/jasper-camilla.service` and
`deploy/bin/jasper-camilla-recover`; 2026-06-24 pair-lock runtime surface
rechecked against
`state.py`, `snapcast_rpc.py`, and doctor wiring; control-side grouping kick
coalescing and the durable trailing service rechecked against
`jasper.control.server`,
`deploy/systemd/jasper-grouping-reconcile-trailing.service`, and the grouped
outputd env/reconcile path; live pair-balance trim rechecked against
`jasper.multiroom.runtime_balance`, `rust/jasper-outputd/src/state.rs`, and the
active-speaker `pair_balance_trim` graph; active-endpoint and wireless-sub TTS route
exceptions rechecked against
`jasper.multiroom.tts_route.expected_grouping_tts_route`,
`jasper.multiroom.reconcile`, and `jasper.cli.doctor.grouping`;
local-source follower parking rechecked against `jasper/local_sources/registry.py`
and `jasper.multiroom.reconcile`; wireless-sub 2.1 path from 2026-06-23
unchanged; snapclient journal rate limit rechecked against
`deploy/systemd/jasper-snapclient.service`)

Stage-0 update 2026-06-27: `buffer_ms` was inert (passed as a snapcast
`pipe://…&buffer_ms=` source-URL param it silently ignores, so bonds ran the
1000 ms default); now routed via `--stream.buffer` in
`reconcile.py:snapserver_argv` so the configured value takes effect. Pre-fix
buffer-sizing observations predate any real buffer change.
