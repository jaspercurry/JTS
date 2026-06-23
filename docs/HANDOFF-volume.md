# HANDOFF — Volume coordination

Volume control on a Pi-based smart speaker has more moving parts than
"set the slider" suggests. The user's iPhone, the Spotify app, the
Bluetooth phone, the rotary dial, the voice tool, and the always-on
CamillaDSP all attenuate audio independently. This document explains
how `jasper.volume_coordinator` makes them feel like one knob.

If you're modifying anything in this subsystem, read this first.

## The problem

Several attenuators sit on the audio chain in series:

```
track_loudness × airplay_sender_vol × spotify_connect_vol
    × bt_avrcp_vol × usbsink_host_vol × camilla_main_volume → DAC
```

Most of these are **upstream of CamillaDSP**. If the iPhone slider is
at 30%, that's a ~20 dB pre-attenuation. Moving CamillaDSP's
`main_volume` between 0% and 100% only spans the remaining 70% of
perceived loudness.

The pre-coordinator behaviour: voice tool's `set_volume(percent)` only
adjusted CamillaDSP. So "Hey Jarvis, set volume to 80%" with the
iPhone slider at 30% sounded like 24% — a confusing disconnect.

## The model

There is **one canonical `listening_level` (0-100)** persisted in
`/var/lib/jasper/speaker_volume.json`. It's what every input writes
and what every read reports. The coordinator's job is to keep it in
sync with whatever attenuator is actually doing the work.

### Outbound dispatch

When the voice tool / dial / "louder" wants to change volume:

1. Coordinator asks `backend.selected_source()` for mux's effective
   audible source: manual `selected_source` when the user picked one,
   or auto `winner` when latest-source-wins owns the gate.
2. If mux is unavailable or has no winner yet, falls back to raw
   `backend.active_renderers()` with priority
   `airplay > spotify > bluetooth > usbsink > idle`.
3. Decides whether the source is **push-mode** (it has a slider we
   can drive — camilla pinned at 0 dB) or **camilla-as-master** (we
   can't drive its slider — Camilla carries `listening_level` on the
   calibrated 1..100% dB curve). The decision lives in the source registry at
   `jasper/music_sources.py` (`VolumeMode`), consumed by
   `_camilla_carries_level(source)`:
   - **IDLE** → camilla-as-master
   - **AIRPLAY** → camilla-as-master *always* (see "AirPlay is always
     camilla-as-master" below for the why)
   - **SPOTIFY** → push-mode (Web API)
   - **BLUETOOTH** → push-mode (AVRCP via bluez-alsa)
   - **USBSINK** → camilla-as-master (the host slider is observed
     one-way by `jasper-usbsink`, not driven by JTS)
4. Pushes the level to the right attenuator:
   - **AirPlay** → CamillaDSP `main_volume` (1% maps to the calibrated
     floor, default −50 dB; 100% maps to 0 dB)
   - **Spotify** → Spotify Web API `PUT /me/player/volume` via the multi-account `spotify_router` (librespot 0.8.0 has no local control HTTP, so we go through Spotify's cloud → spirc → librespot, ~200-800ms latency, also propagates to all Spotify clients so the app slider visibly moves)
   - **Bluetooth** → `org.bluez.MediaTransport1.Volume` property on the active a2dpsnk path (uint16 0..127)
   - **USB sink** → CamillaDSP `main_volume`
   - **Idle** → CamillaDSP `main_volume`
5. In push-mode, **CamillaDSP `main_volume` is normally pinned at
   0 dB** so there's no double-attenuation. If a source-side push
   fails, Camilla remains a degraded-safe fallback guard at the
   canonical level until a later handoff or recovery path can clear it.
   `listening_level=0` is the explicit exception: the source slider is
   still pushed to zero, but Camilla also asserts `main_mute` and stores
   the calibrated floor (default −50 dB) so content/music "0%" is actually
   muted, not just the renderer's lowest slider value. Assistant speech is
   governed by the voice/mic policy and outputd path, not by source volume.
   In camilla-as-master mode (idle, AirPlay, USB), `main_volume` IS the
   user-facing knob. The same 0% rule applies there: the normal
   calibrated 1-100% curve applies, while 0% additionally sets Camilla's
   `main_mute`.

The audible curve is configured by `/sound/` advanced settings:
`volume_floor_db` is the dB value for 1%, clamped to −60..−10 dB and
defaulting to −50 dB. The page can start a continuous 1% calibration tone,
update CamillaDSP `main_volume` as the floor slider moves, and stop the tone
explicitly or on page leave. The Reset floor button saves the default −50 dB
floor through the same path; only `/sound/settings` persists the chosen floor.
JTS never maps the user slider above 0 dB; raising the floor only compresses
the quiet end of the slider for low-sensitivity speakers. The settings file is
published `0640` with the parent `jasper` group so both `jasper-web` and
`jasper-control` read the same floor; otherwise voice/control volume commands
would silently fall back to the shipped default curve.

### Bonded-follower volume proxy (stereo pairs)

While a speaker is an ACTIVE multiroom bond follower, its local volume
knobs are inert — bonded content bypasses the local CamillaDSP entirely
(the leader's one Camilla bakes the program;
[HANDOFF-multiroom.md](HANDOFF-multiroom.md) §2). jasper-control
therefore forwards `GET /volume` and `POST /volume/{set,adjust,mute}`
verbatim to the leader's control API and relays the answer (tagged
`pair_leader`), so the landing-page slider, a paired dial, and any HTTP
client control the PAIR volume from whichever member they talk to. A
`X-JTS-Pair-Forwarded` header breaks forward loops; the follower check
is one grouping.env parse per call. Solo speakers and leaders never
enter this path. Voice volume commands on a follower route through the
SAME forward: the audio tools (`jasper/tools/audio.py`,
`_pair_volume`) drive the local control API via loopback when
bonded-as-follower, so "Jarvis, louder" moves the pair from either
speaker; a leader-unreachable failure becomes a spoken error, never a
silently inaudible local write. Voice mute/unmute send an explicit
`{"muted": bool}` — `/volume/mute` accepts that additively (absent
body = the legacy HID toggle, contract pinned by tests).

### `/state` volume policy visibility

`jasper-control` exposes `/state.audio.volume_policy` so the quiet
Spotify-at-100% class of bug is visible without SSH. The block includes:

- `active_source` from the top-level `/state` view and the resolved
  music `source` used for volume policy.
- `volume_mode` and `carrier` (`camilla`, `source`, or
  `camilla_guard`).
- `listening_level_percent`, current `main_volume_db`, and persisted
  `main_volume_db`.
- `push_guard_active`, `guard_db`, `guard_reason`, `guard_context`, and
  `previous_db` when a push-mode source is protected by a Camilla
  fallback guard.
- `last_source_push_result`, `last_clear_event`, and mux
  `last_handoff` when those facts are available.

The snapshot is cheap by design. `/state` builds it from values it
already collected (Camilla status, mux status, and
`/var/lib/jasper/speaker_volume.json`) plus a tiny volatile diagnostics
file at `/run/jasper/volume_policy.json` written by
`jasper.volume_diagnostics`. That file is updated only when a source
push, degraded guard, or guard clear happens. It performs no Spotify,
DBus, network, or Camilla calls, and it is fail-soft: if `/run` state is
missing, `/state` still derives the current guard from persisted/current
Camilla dB.

Hardware validation for the Spotify quiet-at-100% fix is still required
after deploy. During the AirPlay → Spotify Connect reproduction, check:

```sh
curl -s http://jts.local:8780/state | jq .audio.volume_policy
```

Healthy recovery after Spotify push or confirmed source observation:
`volume_mode="push"`, `carrier="source"`, `push_guard_active=false`,
and `main_volume_db` near `0.0`. A safe degraded failure shows
`carrier="camilla_guard"` and `push_guard_active=true`; that is quieter
than intended but protects against a loud transient.

### `/state` gain-chain ledger

`/state.audio.volume_policy` answers "which volume carrier owns the user
knob?" The adjacent `/state.audio.gain_chain` answers "what gain stages are
currently affecting the audible path, and what single number can we trust?"

The headline field is `common_static_gain_db`: the sum of scalar gain stages
that apply to common program audio right now. It intentionally excludes
per-driver calibration, dynamic duckers, limiters, and source-owned volume
whose dB value is not observable. Those stages still appear in `stages`, but
with `included_in_common_total=false`, `dynamic=true`, `nonlinear=true`, or a
warning when appropriate. This keeps the single number useful without turning
the ledger into another hidden volume knob.

The ledger is built in `jasper.control.gain_chain` from state that other
owners already expose:

- `volume_policy` / Camilla `main_volume` for the user-visible knob or push
  guard fallback.
- The active CamillaDSP config path for actual `Gain`, `Limiter`,
  `devices.volume_limit`, and active-speaker fold-down stages.
- Sound-profile state only as a fallback when the active config does not
  expose `sound_preamp`; the loaded DSP graph wins to avoid double-counting.
- `jasper-fanin` STATUS for active TTS program ducking and assistant-loudness
  normalization.
- `jasper-outputd` STATUS for bonded/multiroom DAC-content trims.

Important stage semantics:

- `active_baseline_headroom` is emitted by the active-speaker baseline config
  and is currently `0.0 dB` by default. Active preference boosts should not
  move this stage; only explicit output trim or match-loudness attenuation
  should. If it ever appears as attenuation, it is visible in this ledger and
  included in `common_static_gain_db`.
- `active_mono_fold_down` reports `gain_db=0.0` in the common total even though
  each L/R source feed is `-6 dB`; correlated mono sums back to unity, while
  hard-panned content is quieter. The per-source gains are listed in
  `details.source_gains_db`.
- Driver calibration such as `as_tweeter_baseline_gain` is visible but not
  part of the common program total because each output channel can differ.
- Limiters and TTS ducks are visible as dynamic/nonlinear stages. If a daemon
  does not expose the exact dB value, the ledger marks that stage as unknown
  instead of inventing a number.

The snapshot is read-only and is logged with
`event=audio.gain_chain.snapshot` only when its fingerprint changes, so
polling `/state` does not spam the journal. On a deployed speaker, inspect it
with:

```sh
curl -s http://jts.local:8780/state | jq .audio.gain_chain
journalctl -u jasper-control.service | grep 'event=audio.gain_chain.snapshot'
```

If you add, remove, or reinterpret an audio gain stage, update this section and
`tests/test_gain_chain.py` in the same change. If the gain stage lives outside
the volume coordinator, make sure `docs/doc-map.toml` routes that code path to
this handoff so future agents find the ledger contract.

### Inbound observation

A 1 Hz poller (`jasper.volume_observers.VolumeObserver`) reads each
source's current value and feeds detected changes into
`coordinator.observe_source_volume(...)`:

- AirPlay: `busctl get-property` for `AirplayVolume` (read but
  unconditionally ignored downstream — see exception below)
- Spotify: read `/run/librespot/state.json` (written atomically at 0644 by librespot's `--onevent` hook on every player event, so non-root mux/control readers can observe it; `volume` field is raw 0-65535, mapped to percent)
- Bluetooth: `bluealsa-cli list-pcms` to find the transport, then
  `busctl get-property` for `Volume`
- USB sink: observed in the `jasper-usbsink` daemon itself. The host
  slider is bridged into `VolumeCoordinator.observe_source_volume(...)`
  with `source="usbsink"`; the shared `VolumeObserver` does not poll it.

When the user moves the Spotify app slider or BT volume, the next
poll picks it up (sub-second latency) and the coordinator updates
`listening_level` accordingly.

If a push-mode source handoff had fallen back to a Camilla guard
(`degraded_safe`), a confirmed Spotify/Bluetooth source volume clears
that guard and pins Camilla back to 0 dB. "Confirmed" includes both
a real user-side source slider change and an observation that the
active source already sits at the canonical `listening_level`. At that
point the source slider has proven it is carrying the user's intent, so
leaving the downstream fallback attenuation in place would make
"Spotify 100%" still sound like the older guarded level.

USB is different from Spotify/Bluetooth: the Mac/PC host slider is an
observed input, but not the final speaker-volume carrier. When USB is
active, a host-side observation updates `listening_level` and then
converges Camilla (`main_volume` plus the 0% `main_mute` flag) to match.
That is the Wispr Flow/macOS mute-unmute path: host "0%" asserts content
mute; host unmute restores Camilla to the observed level unless the
voice-session duck gate is active, in which case the dB write is
deferred but the mute flag still reflects the user's current intent.

**Exception: AirPlay observations are unconditionally skipped.** The
sender's slider sits *upstream* of camilla in the audio chain —
honoring it as the user's master-volume intent would mean the
canonical level bounces around with whatever the phone/Mac is
showing, disconnected from what camilla (the actual master) is
doing. So we ignore the iPhone/Mac AirPlay slider and let the dial
and voice tools own the canonical JTS speaker level. The sender
slider remains upstream trim, not the JTS volume source of truth.

### Echo prevention

When the coordinator writes to a source, the source emits a property-
changed event that the observer also sees. We don't want this to look
like user input. So every outbound write timestamps itself per source
(`_OutboundStamp`), and on observation:

```
if observed.source.was_written_by_us within ECHO_WINDOW_SEC (500 ms):
    ignore
```

500 ms covers DBus round-trip + bus latency on a busy Pi 5 with
generous slack. It's short enough that a real user-touched slider
movement landing just after our write isn't swallowed.

### Why polling, not DBus subscriptions

`busctl get-property` is the proven pattern in this codebase
(`jasper.renderer`, `jasper.mux`). DBus PropertiesChanged
subscriptions would need a new dependency (dbus-next) and a more
complex error model (long-lived subscriptions to manage). For our
use case the ergonomic wins don't materialise: source-side volume
changes happen at finger-touch speed, and 1 Hz polling captures
everything with sub-second latency.

## The boot path

`VolumeCoordinator.initialize()` is called once at voice_daemon
startup:

1. Load `VolumeRecord` from disk (handles v1→v2 migration internally —
   v1 files derive `listening_level` from `main_volume_db` percent).
2. Compute the boot target via `regress_listening_level_if_stale`:
   - **No record** → `first_boot_default_pct` (50% by default).
   - **Fresh** (now − last_used_at < `stale_after_sec`) → use as-is.
   - **Stale + extreme** → clamp into `[safe_low_pct, safe_high_pct]`.
   - **Stale + safe** → use as-is.
3. Apply via dispatch (whichever source is active, or camilla if idle).
4. Persist with `mark_user_change=False` — boot writes do NOT bump
   `last_used_at`. Otherwise every reboot would reset the staleness
   clock and yesterday's bedtime 90% would never get clamped.

`stale_after_sec` is tied to `last_used_at` (last user-initiated
change), not `updated_at` (last write of any kind). This decouples
"how recently the user actually touched volume" from "how recently
the daemon wrote to disk".

## The two consumers

**`jasper.tools.audio.make_audio_tools(coordinator)`** — voice tool
surface. Five tools: `get_volume`, `set_volume`, `adjust_volume`,
`mute`, `unmute`. Each is a thin wrapper around the coordinator's
public API.

**`jasper.control.server`** — HTTP surface for the rotary dial and
LAN automation. Builds a fresh `VolumeCoordinator` per request via
`_with_coordinator` (matches the pre-existing `_toggle_transport`
pattern). Both legacy `delta_db`/`db` payloads and newer
`delta_percent`/`percent` payloads are accepted; the legacy ones
convert at the HTTP boundary.

Both daemons converge through the persistence file. voice_daemon's
coordinator runs the inbound observers; control_daemon's
coordinator does not (it doesn't need them — it's a write surface).

### Cross-daemon defer signal

The coordinator writes camilla via `_set_camilla(level)` on the
camilla-master paths (AirPlay + idle + USBSINK). A camilla write
mid-voice-session would clobber the Ducker's setting and make music
audibly louder mid-TTS, so two complementary gates short-circuit
the write:

1. **`_voice_session_active` flag** — set by `note_voice_session(True/
   False)` from jasper-voice's `WakeLoop`. Catches the voice-tool-
   driven path (LLM calls `set_volume` mid-session). Only meaningful
   on the long-lived coordinator owned by jasper-voice; per-request
   coordinators in jasper-control always read it as `False`.
2. **`_duck_active_probe` callback** — the authoritative cross-daemon
   signal. jasper-control's per-request coordinators are constructed
   with a probe that asks jasper-voice over UDS (`STATUS` →
   `duck_active`) whether the `Ducker` is currently engaged. Probe-
   true defers (same effect as the flag); probe-false writes camilla;
   probe-`None` (UDS unreachable, voice wedged, malformed) **fails
   open** — the coordinator writes camilla anyway. The dial must
   never silently stop working because of an inter-daemon problem;
   better to occasionally un-duck music for a moment than to leave
   the user with a dead knob. Built by `_make_duck_active_probe` in
   `jasper/control/server.py`.

Both gates persist `listening_level` (user intent recorded); only
the camilla write is skipped. When `Ducker.restore()` runs at
session end it reads disk → `get_camilla_target_db()` → camilla
lands at the user's intended level. The defer log lines distinguish
the two paths: `event=volume.deferred reason=voice_session_active`
(flag path) vs `event=volume.deferred reason=session_signaled`
(probe path). Both `jasper-control` and `jasper-mux` build this probe
when they construct per-request/per-handoff coordinators.

#### Why the probe replaced the prior dB-comparison heuristic

Until 2026-05-25, gate #2 was a heuristic: "if the requested target
is more than 5 dB above camilla's current `main_volume`, infer a
duck and defer." This conflated two situations that produced an
identical signal — *Ducker has lowered camilla by 25 dB* and *user
spun the dial 3 detents in one batch (+6 dB)*. The dial firmware
batches multi-detent spins into one POST (correct behavior — what
makes fast spins feel responsive), so any sufficiently fast spin
crossed the threshold.

The misfire wasn't merely a glitch. When the heuristic deferred,
`listening_level` was persisted (the caller does it in `_dispatch`'s
finally block) but `main_volume_db` was not. With no actual Ducker
running, nothing came along to converge them. Every subsequent dial
twist computed its target from the now-inflated `listening_level`,
the gap to current `main_volume` only widened, and the heuristic
fired again — a self-perpetuating cascade. Users saw the dial UI
and web slider both reading 100% while the speaker stayed quiet.

The probe replaces a structurally ambiguous signal with an
authoritative one: jasper-voice is the source of truth about whether
its own Ducker is engaged, so we ask it. The fail-open behavior
preserves the AGENTS.md "production speaker — must be resilient and
plug-and-play" contract: if jasper-voice is down or wedged, the dial
keeps working at the cost of *possibly* un-ducking music for a
moment (which wouldn't happen anyway because the wedged daemon
can't duck either). The previous fix's asymmetric "only raises
defer" rationale was correct in spirit but the wrong target — what
mattered was "is a duck *actually* active," not "would this write
*look like* it's fighting a duck."

What we **don't** do: TTS gain does NOT respond to dial / web-
slider input during TTS playback. The tracker stays paused. The
user can adjust between turns; mid-TTS the audible feedback is that
music doesn't get loud (good), and TTS itself plays at the gain set
at turn-start (no change). Building real-time TTS responsiveness to
user input requires a delta-based tracker refactor; not justified
for the use frequency observed in production.

### Source handoff guard

`jasper-mux` owns source policy, but `VolumeCoordinator` owns the
handoff safety invariant: **a fan-in lane must not become audible until
the correct volume carrier is safe for the current `listening_level`.**
The mux calls `prepare_source_handoff(prev, current, reason=...)`
before `SELECT <label>` for selectable music sources and
`finalize_source_handoff(...)` after the fan-in gate moves.

The two cases:

- **Push-mode target** (`spotify`, `bluetooth`): push
  `listening_level` to the source first. After fan-in selects that
  lane, keep the prior Camilla guard for a short propagation window
  (`JASPER_SOURCE_PUSH_SETTLE_SEC`, default 0.75 s), then return
  CamillaDSP to 0 dB. If the push fails, lower CamillaDSP to the
  canonical guard level and still allow the switch in a `degraded_safe`
  state; this is quieter than ideal, never louder. The guarded
  `main_volume_db` is persisted, and `get_camilla_target_db()`
  preserves it through `Ducker.restore()` instead of unmasking a source
  whose own volume could not be set. A later successful push dispatch
  or confirmed active-source observation clears the guard generically
  for all `VolumeMode.PUSH` sources.
- **Camilla-master target** (`airplay`, `usbsink`; `idle` is the
  coordinator's internal fallback, not a mux-selectable lane): lower
  CamillaDSP to the canonical guard level first and wait slightly past
  CamillaDSP's 400 ms default ramp before mux exposes the lane. This
  covers the real Spotify → AirPlay failure mode: Spotify leaves
  Camilla at 0 dB, but AirPlay depends on Camilla for receiver-side
  volume. During a voice duck, the prepare step succeeds only if the
  current ducked Camilla level is already at/below the guard; otherwise
  mux leaves fan-in closed/on the prior source and retries later.

The source registry in `jasper/music_sources.py` declares each source's
`VolumeMode`; source-specific push I/O remains in the coordinator
dispatchers. Successful/final handoff logs use `event=source.handoff`
with `prev_mode`, `target_mode`, `guard_db`, `camilla_before`,
`push_ok`, `settled_ms`, and `result`; early prepare/fan-in failures
log the compact `from/to/reason/result/detail` shape. Mux status also
exposes `last_handoff` with the richer fields, so `/source/state` and
`/state.source_selection` have the same recent diagnostic even for
failure paths.

## Self-healing reconciler (backstop)

`VolumeCoordinator.maybe_reconcile_camilla()` is the resilience
backstop that runs inside `VolumeObserver._tick` at 1 Hz on
jasper-voice. It's a no-op when state is healthy; when
`camilla.main_volume_db` has drifted from
`percent_to_db(listening_level)`, it writes the expected value back
to converge.

The reconciler is **not** the primary defense against the desync
class of bugs — the cross-daemon defer signal (above) is. The
reconciler protects against drift from other writers (room
correction, a future code path that bypasses the coordinator, a
camilla restart blip that swallows a write) and gives the system a
self-healing property no matter how the drift was introduced.

**Gates** (all must pass for a write to land):

1. `_voice_session_active=False` — the Ducker owns camilla during
   a session.
2. Active source uses camilla-as-master (idle / AirPlay / USBSINK).
   Push-mode sources pin camilla at 0 dB by design, except for the
   explicit 0% content-mute floor.
3. `|drift| > RECONCILE_DRIFT_DB` (1 dB) — dead band above camilla's
   normal jitter (~0.1 dB). Mute-state drift is also repaired: if dB is
   already at −50 but `main_mute=false`, 0% is not converged.
4. Deep quiet drift is skipped (`expected - current >=
   RECONCILE_DUCK_SKIP_DB`) — CueDuck plays proactive cues without
   setting `_voice_session_active`, so a 25 dB drop below expected can
   be intentional. Deep loud drift is **not** skipped. If Camilla is
   much louder than the canonical level, the reconciler pulls it back
   even when the drift is larger than 10 dB.

**Trade-off:** if an operator configures `JASPER_DUCK_DB` shallower
than 10 dB (e.g., -5 dB), the reconciler may briefly un-duck cues
during proactive cue playback (1 Hz of "raise camilla to expected"
inside a 5–10 s cue window). Production default of -25 dB is well
clear. If this needs to change, gate the reconciler on a "cue
active" flag set by the cue manager, mirroring `note_voice_session`.

**Emits** `event=volume.reconciled` on every write with
`source=`, `level=`, `current_db=`, `expected_db=`, `drift_db=`,
`current_mute=`, and `expected_mute=`. Visible in
`journalctl -u jasper-voice` for drift forensics.

The reconciler does NOT replace mux-owned source handoff. Mux
`prepare_source_handoff(...)` / `finalize_source_handoff(...)` is the
primary path for landing-page selection and latest-source-wins fan-in
changes. `apply_active_source_transition(...)` remains an observer
backstop for raw renderer-state changes, boot convergence, and older
paths that report activity outside mux's control.

## Hearing-safety belt

The coordinator pushes commands; it doesn't enforce safety on its own.
Multiple guardrails sit on top:

- `regress_listening_level_if_stale` clamps stale + extreme values
  into `[20%, 70%]` by default.
- `OutputdTtsPlayout.set_gain_db` and jasper-fanin enforce the
  `MAX_TTS_GAIN_DB = -6 dB` hardware ceiling on the TTS path
  independent of any volume math.
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
- `/state.audio` exposes Camilla playback RMS, playback peak, and
  clipped-sample count for lightweight diagnostics.

Don't bypass any of these. The user is volume-sensitive ("don't blow
my eardrums out"); defense in depth is the design.

## AirPlay is always camilla-as-master

shairport-sync exposes `SetAirplayVolume` as a method that should
forward volume changes back to the AirPlay sender via the legacy DACP
back-channel. In modern AirPlay 2 sessions, this is not a reliable
control surface. Real hardware validation on 2026-05-14 showed both
macOS and iOS sessions reporting:

```
RemoteControl.Available = false
SETUP AP2 no Active-Remote information
SETUP AP2 doesn't include DACP-ID string information
```

Calling `SetAirplayVolume` returned success at the DBus layer but left
`AirplayVolume` unchanged and did not move the sender UI or audible
level. This matches upstream shairport-sync issue #1822: iOS 17.4 /
macOS 14.4 stopped providing the DACP-ID / Active-Remote headers in
AirPlay 2 mode, so DBus/MPRIS receiver-originated commands are ignored
or impossible. shairport-sync's AirPlay 2 documentation also states
that modern remote-control facilities are not implemented.

So instead of trying to drive the AirPlay sender's slider, we
attenuate at camilla — `main_volume` sits *downstream* of
shairport's receiver in the audio chain (shairport → snd-aloop →
camilla → DAC), so it reduces what the speakers actually emit
regardless of what the sender chose to send. The dial behaves like
a master volume on every source.

**Trade:** the iPhone/Mac AirPlay slider on the sender does not
visibly move when the dial turns. The audio at the speaker does.
Voice volume control and the rotary dial share the same coordinator
path, so both remain reliable during AirPlay.

Mux-owned source handoff is the primary boundary path. The observer
backstop, `apply_active_source_transition`, still follows the same
rules for raw active-source transitions:

| Edge | What happens |
|---|---|
| camilla-as-master → push-mode (e.g. AirPlay → Spotify) | push level to new source first; confirm Camilla's push-mode carrier only after the push succeeds (`0 dB` for 1-100%, `main_mute=true` + calibrated floor for 0%) |
| push-mode → camilla-as-master (e.g. Spotify → AirPlay) | camilla → percent_to_db(level) and `main_mute=(level == 0)` (take over) |
| push → push (e.g. Spotify → BT) | push level to new source; keep/clear Camilla's 0% content mute as needed |
| camilla → camilla (idle ↔ AirPlay) | no change; camilla already carries level |

The first edge is the one users notice: without it, the residual
camilla attenuation from AirPlay mode would compound with the new
source's own slider when they switch (e.g. start Spotify Connect
and find the speaker mysteriously twice as quiet as expected).

**Cross-process staleness fix.** `apply_active_source_transition`
calls `_refresh_from_disk()` before dispatch. The control daemon
(dial / HTTP) writes listening_level to disk on every twist; without
the refresh, voice_daemon's in-memory cache lags and a transition
that fires between voice operations would dispatch a stale level.

If the sender slider is below 100%, it pre-attenuates upstream of
camilla and the dial position stops being a 1:1 read of perceived
loudness. JTS still responds audibly; the user may just turn the dial
further. Do not add hidden fallback behavior that sometimes treats
AirPlay as push-mode — that recreates two competing product contracts.

A previous iteration tried to make AirPlay push-mode by calling
`SetAirplayVolume` and treating `AirplayVolume` observations as
canonical. That worked in unit tests but failed on real iOS/macOS
AirPlay 2 sessions for the protocol reasons above. The code now leans
fully into the robust contract: AirPlay is camilla-as-master.

### Future research: Bose/HomePod-style reflection

Commercial AirPlay 2 speakers such as Bose can reflect receiver-side
hardware volume back into the iPhone/Mac slider. That does not appear
to use shairport-sync's legacy DACP/DBus path. Public reverse-
engineering points to the modern AirPlay 2 control plane: `/info`
capabilities such as `initialVolume` / `volumeControlType`,
`POST /command`, event/data channels, HomeKit/HAP-derived encryption,
and MRP-style protobuf messages.

If we revisit AirPlay slider reflection, keep it separate from the
production volume path and test it as protocol research:

1. Capture Bose or HomePod traffic while changing physical speaker
   volume and compare it with JTS/shairport.
2. Look specifically for `/info` volume capability fields,
   `POST /command`, event/data-channel traffic, and MRP volume messages.
3. Check whether shairport-sync receives, ignores, or never establishes
   the needed channel.
4. Prototype below the Python coordinator layer, likely as a
   shairport-sync patch or AirPlay 2 sidecar. Do not reintroduce
   `SetAirplayVolume` as the normal JTS AirPlay volume path unless
   hardware proves receiver-originated slider reflection works on
   current iOS/macOS.

Useful references:

- https://github.com/mikebrady/shairport-sync/issues/1822
- https://github.com/mikebrady/shairport-sync/blob/master/AIRPLAY2.md
- https://emanuelecozzi.net/docs/airplay2/rtsp/
- https://emanuelecozzi.net/docs/airplay2/protocols/
- https://pyatv.dev/documentation/protocols/
- https://openairplay.github.io/airplay-spec/audio/volume_control.html

## What's NOT here

- **No DBus subscription library**. Polling at 1 Hz is the model; if
  someone wants to switch to PropertiesChanged subscriptions later,
  `dbus-next` is the recommended async option.
- **No AirPlay mute via -144 dB sentinel**. AirPlay
  `AirplayVolume = -144` is a documented "muted" sentinel; we
  treat it as effective silence (clamped to AIRPLAY_DB_MIN → 0%).
  The source-volume 0% mute is owned by `VolumeCoordinator` through
  Camilla `main_mute`, not by source-specific sentinel values.
- **No iPhone-side slider visual update on Jarvis-initiated changes**.
  This is an AirPlay protocol limitation, not a JTS limitation.
  Audio attenuates correctly; the slider widget on the phone shows
  a stale position until the user touches it.

## Changes that need this doc

If you're adding another audio source, start with
[`audio-paths.md`](audio-paths.md#adding-a-new-music-source), then hook
into the volume-specific pieces here:

- `MusicSourceSpec` in `jasper/music_sources.py`, including
  `volume_mode` and fan-in label.
- `_active_source()` priority chain. Source selection is an audible
  fan-in gate owned by mux; volume follows mux's effective
  `selected_source`/`winner` when mux is reachable, then falls back to
  raw renderer probes.
- One `_set_<source>` dispatcher
- One `_read_<source>_*` observer reader, or a source-local bridge like
  `jasper-usbsink` if polling from `VolumeObserver` would be the wrong
  ownership boundary
- Echo-prevention: `_stamp_outbound(Source.NEW, level)` in the
  dispatcher
- Handoff tests for both push-mode and camilla-master transitions

If you're changing the staleness semantics (idle reset thresholds),
the field of authority is `last_used_at` in the persistence record,
written ONLY on user-initiated changes (set/adjust/observe), NOT
on boot restore.

---

Last verified: 2026-06-22 (volume floor calibration checked against `jasper.volume_curve`, `/sound/` settings, and the focused volume/sound pytest suite; prior 2026-06-21 pass covered gain-chain ledger against `jasper.control.gain_chain`, `jasper.control.state_aggregate`, and JTS3 `/state.audio.gain_chain`; prior 2026-06-17 pass covered librespot state-file reader mode against `jasper-librespot-event`, prior 2026-06-14 pass covered active-speaker baseline `volume_limit` guard against `camilla_yaml.py`, and prior 2026-06-08 pass covered 0% content mute, USB observed-carrier sync, push-source degraded guard recovery, /state volume-policy visibility, mux effective-source path, and fan-in TTS ceiling path)
