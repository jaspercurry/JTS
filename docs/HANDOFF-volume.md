# Handoff: volume coordination — one canonical level, many attenuators

Canonical for `jasper/volume_coordinator.py`, `volume_observers.py`,
`volume_persistence.py`, `volume_curve.py`, `volume_diagnostics.py`, the
`/volume` surface in `jasper/control/`, and `jasper/tools/audio.py`.

Neighbouring owners — do not restate their content here:
[audio-paths.md](audio-paths.md) (the signal path and the complete
[assistant-loudness contract](audio-paths.md#assistant-loudness-matching)) ·
[HANDOFF-multiroom.md](HANDOFF-multiroom.md) (bonded pairs; §2 for the bypass) ·
[HANDOFF-sound-preferences.md](HANDOFF-sound-preferences.md) ·
[HANDOFF-usbsink.md](HANDOFF-usbsink.md) (the host slider) ·
[HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md) ·
[historical/volume-control-redesign-2026-05.md](historical/volume-control-redesign-2026-05.md)
(the disproven AirPlay push-mode brief).

Decisions live in ADRs, not here:
[0151](adr/0151-a-new-source-is-camilla-master-until-it-proves-an-observable-volume-surface.md)
(push is earned) ·
[0176](adr/0176-the-airplay-sender-slider-is-not-a-control-surface.md) (AirPlay
is Camilla-master both directions) ·
[0177](adr/0177-duck-ownership-is-asked-of-the-owner-never-inferred-from-a-db-gap.md)
(the duck probe) ·
[0009](adr/0009-measurement-volume-hold-is-not-gated-on-observation.md) (the
measurement hold) ·
[0004](adr/0004-duck-release-algebra-and-reference.md) (duck release
algebra — **tuning-program owned**).

## The canonical state

Several attenuators sit on the chain in series — the sender app's slider, the
source's protocol volume, CamillaDSP's `main_volume` — and most are *upstream*
of CamillaDSP. There is nonetheless **one canonical state**, in
`/var/lib/jasper/speaker_volume.json`, interpreted by `VolumeState`:

- `listening_level` (0-100) is the user's remembered level and the level
  restored after a temporary mute.
- `pre_mute_level` being present is the temporary mute latch.
- `mute_token` identifies that exact transition, so the long-lived source
  observer can tell a stale pre-push reading from a later user change.
- `effective_percent` is derived exactly once: 0 while the latch is present,
  otherwise `listening_level`.

Every input writes through `VolumeCoordinator`, and every user-facing read uses
that same `VolumeState` projection — no HTTP handler, voice tool, accessory, or
web client infers mute independently. The distinction is load-bearing: muting at
60% must render and actuate as 0% while retaining 60% as the unmute target,
while an explicit 0% has no restore target yet derives the same silence and
final-output mute.

**Serialization.** The persistence-owned operation lock spans the physical
renderer/Camilla actuation, so the voice observer, control requests, and voice
tools cannot interleave two half-applied source changes; mux holds the same lock
across a whole handoff (`source_handoff_operation`), so a queued old-source
observation cannot become authoritative after the lane has moved. Independent
persistence field updates take a separate short read-modify-write lock and
reload under it, so a stale `VolumePersistence` cannot erase a newer mute latch
or token. Read surfaces never take the long lock.

## Which attenuator carries the level

`jasper/music_sources.py` declares each source's `VolumeMode`, consumed by
`_camilla_carries_level(source)`. **Push-mode** means JTS can drive the source's
own slider, so Camilla is pinned at 0 dB; **camilla-as-master** means Camilla
carries `listening_level` on the calibrated curve.

| Source | Mode | Carrier |
|---|---|---|
| Spotify | `PUSH` | Web API `PUT /me/player/volume` via the multi-account `spotify_router`; librespot 0.8.0 has no local control HTTP, so the write goes cloud → spirc → librespot (~200-800 ms) and visibly moves the app slider on every client |
| Bluetooth | `PUSH` | `org.bluez.MediaTransport1.Volume` on the active a2dpsnk path (uint16 0..127) |
| AirPlay | `CAMILLA_MASTER` | `main_volume` — always, both directions ([ADR-0176](adr/0176-the-airplay-sender-slider-is-not-a-control-surface.md)) |
| USB sink | `CAMILLA_MASTER` | `main_volume`; the host slider is observed one-way by `jasper-usbsink`, never written |
| Idle | `CAMILLA_MASTER` | `main_volume` (the coordinator's internal fallback, not a mux-selectable lane) |

Outbound dispatch resolves the source first: `backend.selected_source()` for
mux's effective audible source (manual selection, else the latest-source-wins
`winner`); if mux is unreachable or has no winner yet, raw
`backend.active_renderers()` at `airplay > spotify > bluetooth > usbsink > idle`.

`listening_level=0` is the explicit exception in both modes: the source slider
is still pushed to zero, and Camilla additionally asserts `main_mute` and stores
the calibrated floor, so "0%" is really muted rather than the renderer's lowest
slider value. (Assistant speech follows the voice/mic policy and the outputd
path, never source volume.)

**The audible curve.** `percent_to_db` maps 1% to `volume_floor_db` and 100% to
0 dB. The floor is calibrated on `/sound/setup/` against a live 1% tone, clamped
to −60..−10 dB and defaulting to −50 dB (`jasper/volume_curve.py`); only
`/sound/settings` persists it. **JTS never maps the user slider above 0 dB** —
raising the floor only compresses the quiet end for low-sensitivity speakers.
The settings file is `0640` with the parent `jasper` group so `jasper-web` and
`jasper-control` read the same floor; otherwise voice and control commands
silently fall back to the default curve.

**Bonded followers proxy everything.** An ACTIVE bond follower's local knobs are
inert (bonded content bypasses the local CamillaDSP), so jasper-control forwards
every `/volume` verb verbatim to the leader, tagged `pair_leader`, with
`X-JTS-Pair-Forwarded` breaking loops and the follower check kept to one
env-file read per call through the shared effective-role reader — never the
runtime derive with its systemctl/RPC probes. Voice takes the SAME forward via
`_pair_volume` (`jasper/tools/audio.py`), so a leader-unreachable failure is a
spoken error rather than a silently inaudible local write. Solo speakers and
leaders never enter this path.

## Inbound observation

`VolumeObserver` polls at 1 Hz (`POLL_INTERVAL_SEC`) and feeds detected changes
into `coordinator.observe_source_volume(...)`; its module docstring says why
polling and not DBus `PropertiesChanged`.

- **Spotify** — `/run/librespot/state.json`, written atomically at 0644 by
  librespot's `--onevent` hook so non-root readers can see it; raw 0-65535.
- **Bluetooth** — `bluealsa-cli list-pcms` for the transport, then
  `busctl get-property` for `Volume`.
- **AirPlay** — `busctl get-property` for `AirplayVolume`, read for diagnostics
  and **unconditionally ignored** downstream
  ([ADR-0176](adr/0176-the-airplay-sender-slider-is-not-a-control-surface.md)).
  `-144` is AirPlay's mute sentinel, clamped up to `AIRPLAY_DB_MIN`.
- **USB sink** — not polled here. `jasper-usbsink` watches the gadget mixer at
  4 Hz in its own daemon and POSTs `source="usbsink"` observations.

**Echo prevention.** Every outbound write timestamps itself per source
(`_OutboundStamp`); an observation of our own value inside `ECHO_WINDOW_SEC`
(500 ms) is ignored — enough for a DBus round trip on a busy Pi, short enough
that a real slider movement just after our write is not swallowed.

**Clearing a degraded guard.** A confirmed Spotify/Bluetooth source volume
clears a Camilla fallback guard (for any `VolumeMode.PUSH` source) and pins
Camilla back to 0 dB; "confirmed" means either a real user-side slider change or
an observation that the active source already sits at the canonical
`listening_level`. **The Ducker is the exception:** while a voice/TTS duck holds
Camilla, a push confirmation is not a real clear — the coordinator records a
failed/deferred clear and leaves the guard persisted for the next tick, so
persistence never claims Camilla is pinned at 0 dB while the graph is
attenuated.

**USB is observed, not authoritative.** A host-side observation updates
`listening_level` and then converges Camilla (`main_volume` plus the 0%
`main_mute` flag) — the macOS mute/unmute path. The legacy Camilla-ducker lock
defers the dB write; the mute flag always reflects current intent. USB carries
`observation_initial=true` while retrying the startup mixer snapshot: that may
synchronize an ordinary unmuted session, but it yields to a mute already
asserted elsewhere.

**A live measurement hold declines, it does not defer.**
[`control/measurement_hold.py`](../jasper/control/measurement_hold.py), taken by
[`measurement_window()`](../jasper/correction/coordinator.py) and shown at
`/state.measurement`, makes `_post_volume_set`
(`jasper/control/handlers/volume.py`) short-circuit every `source=`-bearing
request BEFORE a coordinator is built: nothing is updated, Camilla is untouched,
and the caller gets the established `observation_applied: false` on an HTTP 200.
Nothing replays it — the USB bridge re-presents the host value once the hold
lapses. **Authoritative** writes (no `source`) are unaffected; this is
isolation, not a lockout.

**Mute is token-barriered.** A temporary mute preserves `listening_level` and
atomically records `pre_mute_level` plus a fresh `mute_token`; dispatch still
receives the derived effective 0%. Push writes can be slow, so a nonzero
observation is ignored until the observer has seen renderer 0% for that same
token — that 0% confirms the mute rather than being a new canonical 0% edit, and
cannot erase the restore level. After the barrier a nonzero source-side change
is unambiguous fresh intent and clears the mute normally. Observer dedup
includes the token revision as well as the renderer value, so two rapid mutes
that both expose 0% stay distinct.

**Boot.** `VolumeCoordinator.initialize()` runs once at voice_daemon startup:
load `VolumeRecord` (v1→v2 migration is internal), compute the boot target via
`regress_listening_level_if_stale` (its defaults are the ladder), apply through
the normal dispatch, and persist with `mark_user_change=False`. Boot writes do
NOT bump `last_used_at`, or every reboot would reset the staleness clock and
yesterday's bedtime 90% would never get clamped. Staleness is anchored on
`last_used_at` (last user-initiated change), not `updated_at` (last write of any
kind) — that field is the authority if you change staleness semantics, and it is
written ONLY on set/adjust/observe, never on boot restore.

## The two consumers

**`jasper.tools.audio.make_audio_tools(coordinator)`** — `get_volume`,
`set_volume`, `adjust_volume`, `mute`, `unmute`, each a thin wrapper on the
coordinator's public API. Voice mute/unmute send an explicit `{"muted": bool}`;
`/volume/mute` accepts that additively (absent body = the legacy HID toggle).

**`jasper.control`** — HTTP for management clients, accessories, and LAN
automation, building a fresh `VolumeCoordinator` per request via
`_with_coordinator`. Relative adjustments use `delta_percent`; absolute setters
take `percent`, with the established `db` form retained for automation. Only
mutating requests construct the actuators — `GET /volume` is deliberately
persistence-only, so the landing page's 500 ms visible-only single-flight
refresh never rebuilds Spotify clients or renderer-control machinery. Both
daemons converge through the persistence file; only voice_daemon's coordinator
runs the inbound observers. Every successful `/volume` response shares one
additive payload contract — `{"db", "percent", "muted", "restore_percent"}`,
where `percent` and `db` are what a client should render, `muted` is the
effective silence assertion (temporary mute or explicit 0%), and
`restore_percent` is non-null only for a temporary mute. Existing clients stay
compatible by reading only `percent`.

## Cross-daemon Camilla ownership

Voice-session state and Camilla ownership are two facts, not one
([ADR-0177](adr/0177-duck-ownership-is-asked-of-the-owner-never-inferred-from-a-db-gap.md)
has the rule and why the alternative failed). `WakeLoop` publishes both through
`note_voice_session(active, camilla_volume_locked=...)`: `_voice_session_active`
suppresses source handoffs and the 1 Hz reconciler (both can race session
topology even when fan-in owns the duck), while `_camilla_volume_locked` gates
`_set_camilla` and is set only by the legacy Camilla `Ducker`. Per-request
coordinators in jasper-control cannot read a process-local flag, so
`_duck_active_probe` asks jasper-voice `STATUS` for that boolean, falls back to
the older `duck_active` field during rolling upgrades, and fails open on `None`.
Both appear in `/state.voice`, where
`duck_active=true, camilla_volume_locked=false` is normal fan-in speech and
volume writes proceed. Under the legacy lock, `listening_level` still persists
and `Ducker.restore()` converges Camilla to `get_camilla_target_db()` at session
end. **What this surface publishes:** one absolute `VOLUME_CONTEXT` of five —
canonical user dB, downstream Camilla dB, the quiet-room TTS envelope target,
mute, and a `CLOCK_BOOTTIME` stamp taken at snapshot acquisition and carried
immutably, so an older snapshot stays older even if its write is delayed. The
same five ride `PREPARE_ASSISTANT` at turn start, making identity and safety one
atomic command. The mute bit is fail-safe: pre-mute intent, canonical 0%, or
observed Camilla mute can raise it; a stale or unreadable observation can never
lower user intent. Slow actuators are ordered differently — for
Spotify/Bluetooth the coordinator publishes known user intent BEFORE awaiting
the source round trip and a converged snapshot after, and mute publishes
`muted=true` before even the best-effort Camilla backstop, so neither cloud
latency nor a wedged Camilla can delay the TTS stop. The message carries no
source name or gain policy: source dispatch stays here, and everything
downstream — consumption, mix-stage selection (`JASPER_TTS_MIX_STAGE`,
single-written by the grouping reconciler; callers use
`tts_socket_feeds_pre_dsp_fanin()` / `tts_socket_feeds_post_dsp_outputd()`
rather than inferring stage from a socket path), and the gain math — belongs to
[audio-paths.md](audio-paths.md#assistant-loudness-matching).

## Source handoff guard

`jasper-mux` owns source policy; `VolumeCoordinator` owns the handoff safety
invariant: **a fan-in lane must not become audible until the correct volume
carrier is safe for the current `listening_level`.** Under the lease above, mux
calls `prepare_source_handoff(prev, current, reason=...)` before
`SELECT <label>` and `finalize_source_handoff(...)` after the gate moves.

- **Push-mode target** (`spotify`, `bluetooth`): push `listening_level` to the
  source FIRST; after fan-in selects the lane, hold the prior Camilla guard for
  a propagation window (`JASPER_SOURCE_PUSH_SETTLE_SEC`, 0.75 s) before
  returning Camilla to 0 dB. A failed push still allows the switch, in a
  `degraded_safe` state at the canonical guard level — quieter than ideal, never
  louder. That guarded `main_volume_db` is persisted and
  `get_camilla_target_db()` preserves it through `Ducker.restore()` rather than
  unmasking a source whose volume could not be set.
- **Camilla-master target** (`airplay`, `usbsink`): lower Camilla to the guard
  level FIRST and wait past Camilla's 400 ms ramp
  (`JASPER_SOURCE_HANDOFF_SETTLE_SEC`, 0.45 s) before mux exposes the lane. This
  is the real Spotify → AirPlay failure: Spotify leaves Camilla at 0 dB, but
  AirPlay depends on Camilla for its volume. During a voice duck, prepare
  succeeds only if the ducked level is already at or below the guard; otherwise
  mux leaves fan-in closed on the prior source and retries.

Final handoffs log `event=source.handoff` with `id`, `from`, `to`, `reason`,
`level`, `guard_db`, `camilla_before`, `prev_mode`, `target_mode`, `push_ok`,
`settled_ms`, `result`, `elapsed_ms`; early prepare/fan-in failures log the
compact `id/from/to/reason/result/detail` shape at WARNING. Mux status exposes
`last_handoff` with the richer fields, so `/source/state` and
`/state.source_selection` carry it on failure paths too.
`apply_active_source_transition(...)` remains an observer backstop for raw
renderer-state changes, boot convergence, and paths outside mux's control; it
follows the same carrier rules and calls `_refresh_from_disk()` first, because
the control daemon writes `listening_level` on every change and a stale cache
would dispatch the wrong level.

## Self-healing reconciler

`maybe_reconcile_camilla()` runs inside `VolumeObserver._tick` at 1 Hz on
jasper-voice: a no-op when healthy, a write-back when `main_volume_db` has
drifted from `percent_to_db(listening_level)`. It is **not** the primary defense
against desync — the ownership signal above is — but it catches drift from other
writers whatever the cause. **Gates**, all required:

1. No voice session or correction measurement active. Voice sessions pause this
   background repair; foreground user writes stay live unless
   `_camilla_volume_locked` is separately true. `MEASURE_PAUSE` sets
   `_measurement_active`, which narrowly disables the reconciler so it cannot
   replace a measurement ramp's requested volume with persisted
   `listening_level` — not a cross-daemon Camilla lock, and never a block on
   emergency user mute or attenuation.
2. Active source is camilla-as-master (push-mode pins Camilla at 0 dB by design,
   the 0% mute floor excepted).
3. `|drift| > RECONCILE_DRIFT_DB` (1 dB) — a dead band above Camilla's normal
   jitter. Mute-state drift is repaired too: at −50 dB with `main_mute=false`,
   0% is not converged. A persisted `pre_mute_level` is active mute intent, so
   the reconciler expects the floor and `main_mute=true` while preserving the
   restore level rather than "repairing" back to the prior audible level.
4. Deep QUIET drift is skipped (`expected - current >= RECONCILE_DUCK_SKIP_DB`,
   10 dB) — `CueDuck` plays proactive cues without setting
   `_voice_session_active`, and the graph-mutation bracket rides the same
   carve-out. Deep LOUD drift is **not** skipped: a writer that left Camilla far
   above canonical is unsafe, not a duck. (An operator who sets `JASPER_DUCK_DB`
   shallower than 10 dB may therefore see cues briefly un-duck; the production
   default, −25 dB, is well clear.)
5. The unlocked preflight is only a hint. Before writing, the reconciler takes
   the shared operation lease and re-reads source, canonical level, and live
   Camilla state, so a newer control-daemon command wins instead of being
   overwritten by a stale observer snapshot.

Every write emits `event=volume.reconciled` with `source`, `level`,
`current_db`, `expected_db`, `drift_db`, `current_mute`, `expected_mute` — the
drift forensics in `journalctl -u jasper-voice`.

## Hearing-safety belt

The coordinator pushes commands; it doesn't enforce safety on its own.
Multiple guardrails sit on top:

- `regress_listening_level_if_stale` clamps stale + extreme values
  into `[20%, 70%]` by default.
- Fan-in (pre-DSP) and outputd (post-DSP) assistant/TTS loudness have no fixed
  source-gain ceiling. Both match assistant loudness to measured content or the
  quiet-room envelope and cap the result with the dynamic peak-aware limit
  (`max_peak_dbfs - source_peak_dbfs`) so quiet voices are not pinned below
  music by a stale global clamp. See
  [HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md).
- `volume_limit: 0.0` in every JTS CamillaDSP YAML — base,
  room-correction, sound-preference, and active-speaker baseline configs
  all cap the main fader at full scale.
- `CamillaController.set_volume_db` validates every Python write and
  clamps positive gain to 0 dB as runtime defense in depth.
- `VolumeCoordinator` treats 0% as Camilla `main_mute=true` plus the
  calibrated floor (default −50 dB); nonzero unmute writes the safe dB
  target before clearing `main_mute`.
- `jasper-doctor` checks the active Camilla config for
  `devices.volume_limit <= 0` and fails if it is missing or positive.
- `dsp_apply` refuses at apply time any config that omits `volume_limit` or
  sets it above 0 dB, so a bad graph never reaches CamillaDSP.
- `/state.audio` exposes Camilla playback RMS, playback peak, and
  clipped-sample count for lightweight diagnostics.

Don't bypass any of these. The user is volume-sensitive ("don't blow
my eardrums out"); defense in depth is the design. `devices.volume_limit`
staying `0.0` and the `set_volume_db` clamp are **AGENTS.md non-negotiable 1** —
never weaken either.

## Diagnosing at the user's surface

`/state.audio.volume_policy` makes the quiet-Spotify-at-100% class of bug
readable without SSH, at the cost of no Spotify, DBus, network, or Camilla call
(`volume_diagnostics.build_volume_policy_snapshot` is the field list). Healthy
after a push or a confirmed observation: `volume_mode="push"`,
`carrier="source"`, `push_guard_active=false`, `main_volume_db` near `0.0`. A
safe degraded failure reads `carrier="camilla_guard"`, `push_guard_active=true`,
and a `guard_reason` — quieter than intended, protecting against a loud
transient.

```sh
curl -s http://jts.local:8780/state | jq .audio.volume_policy
```

## Reaching Camilla

`CamillaController` is the tree's one fader door: every daemon path,
`jasper-doctor`, and `jasper-aec-tune` go through it, and it bounds the
synchronous pycamilladsp client. Two of its constraints reach this surface:

- `set_config_file_path()` is a sequential `SetConfigFilePath` then `Reload`,
  not an atomic protocol. A DSP transaction needing rollback must retain and
  restore its prior path; a transport timeout is not proof a mutation did or did
  not land.
- Every websocket graph mutation runs inside a deep main-fader duck
  (`_graph_mutation`). Its release algebra, `held_target_db` included, is
  tuning-program owned:
  **[ADR-0004](adr/0004-duck-release-algebra-and-reference.md) is authoritative
  and this doc states nothing further about it.** The one fact this surface owns
  is reconciler gate 4 — the duck rides `main_volume`, not `main_mute`, because
  `maybe_reconcile_camilla` treats a mute as drift to correct while a deep drop
  is left alone.

The canonical release target is per process: jasper-voice hands over its
long-lived coordinator's `get_camilla_target_db`, and every other graph-swapping
process calls `install_env_canonical_target_provider()` at startup. That set is
a maintained list, not a derived one — read `_ENTRY_POINTS` in
[`tests/test_canonical_target_registration.py`](../tests/test_canonical_target_registration.py),
because a lost registration line compiles fine and would silently put that
daemon's swaps back on snapshot releases.

## Adding a source

After [`audio-paths.md`](audio-paths.md#adding-a-new-music-source): declare
`MusicSourceSpec` with its fan-in label and `volume_mode` (`CAMILLA_MASTER`
unless push is earned — ADR-0151); extend the `_active_source()` priority chain;
add one `_set_<source>` dispatcher carrying `_stamp_outbound(Source.NEW, level)`
and one `_read_<source>_*` observer reader — or a source-local bridge like
`jasper-usbsink` where `VolumeObserver` would be the wrong ownership boundary;
pin both a push-mode and a camilla-master handoff.

---

Last verified: 2026-08-26 (triage pass — every surviving claim rechecked against
the volume and camilla modules, `control/handlers/volume.py`, `mux.py`,
`tools/audio.py`, `cli/doctor/audio.py`, `dsp_apply.py`,
`usbsink/volume_bridge.py`, `deploy/index.html`. Corrected: `jasper-aec-tune` no
longer constructs pycamilladsp directly. AirPlay → ADR-0176; duck probe →
ADR-0177; duck release algebra → ADR-0004.)
