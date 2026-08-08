# Handoff: runtime hardware hot-plug / unplug resilience

Treat the speaker like a computer: the microphone (XVF3800), the
output DAC/dongle, USB host, and HID accessories can be **attached or
detached while the speaker is running**, and the system must converge
to a correct state on its own — in **both** directions, with no
redeploy, no manual restart, and **no crash-loop**. On **unplug** the
dependent function parks cleanly and says so; on **plug-in** it comes
back automatically and promptly (event-driven via udev, not only on
boot/deploy/timer).

This doc is the canonical reference for that convergence model. It is
the home for Workstream C of
[`install-update-resilience-plan.md`](install-update-resilience-plan.md)
(problem #6). The cross-cutting resilience ladder lives in
[`HANDOFF-resilience.md`](HANDOFF-resilience.md); the mic/AEC reconciler
internals in [`HANDOFF-aec.md`](HANDOFF-aec.md); the output owner in
[`HANDOFF-speaker-output-reference.md`](HANDOFF-speaker-output-reference.md).

## The invariant (what "converges" means here)

For every hot-pluggable component, all four must hold:

1. **Unplug never crash-loops.** The dependent daemon must not
   start-fail-restart in a tight loop. On JTS a sustained restart
   spiral escalates to `StartLimitAction=reboot` (Tier 4.5/T5.1), so a
   crash-loop on a *missing optional device* is not merely ugly — it
   reboots the speaker, repeatedly.
2. **Unplug parks cleanly and observably.** The function goes
   `inactive` (not `failed`), and the state is reported as *expected
   idle* — distinct from *broken* — in `jasper-doctor`, `/state`, and
   the journal.
3. **Plug-in converges automatically and promptly.** A udev event
   re-runs the owning reconciler within seconds; no human action.
4. **No false health.** An update/deploy must not report the function
   healthy when it is only idle for missing hardware (and must not fail
   just because the hardware is absent).

## Status by component

| Component | Owner | Unplug | Plug-in | Notes |
|---|---|---|---|---|
| **Output DAC / Apple dongle** | `jasper-outputd` + `jasper-audio-hardware-reconcile` + `jasper-dongle-recover` | clean park / failure-triggered reconcile | udev → reconcile/recover restart | **Fixed 2026-06-22; tightened 2026-07-06.** ALSA control events plus Apple USB remove helper wake reconcile; outputd stages and validates buffer/period env before retry, and config exits get one bounded reconcile/retry before parking |
| **Microphone (XVF3800 / USB)** | `jasper-voice` + `jasper-aec-reconcile`; optional output reference owned by `jasper-outputd` | voice clean park; outputd keeps DAC playback running and retries the chip-reference sink in the background | udev → reconcile restart; outputd reconnects its reference writer without a playback restart | **Fixed 2026-06-21; output/reference isolation tightened 2026-07-10.** A missing mic may park voice and degrade chip AEC, but cannot silence speaker output |
| **HID accessories** | `jasper-input` | in-process udev | in-process udev | **Already resilient** — pyudev monitor, no per-device unit |
| **WiiM Remote 2 BLE mic** | `jasper-accessory-reconcile` + `jasper-wiim-remote-mic` + `jasper-wiim-remote-ce` + `jasper-voice` manual mic source | Bluetooth forget/boot reconcile removes the manual source and disables the adapter; voice keeps normal mic path | Bluetooth pair/connect reconcile writes `accessory-mics.env`, enables adapter, then refreshes voice — starting `jasper-aec-reconcile` so the gate's owner re-derives it when that owner is enabled, else `try-restart`ing a running voice; adapter re-requests the BLE connection-event reservation on every reconnect | **Fixed 2026-06-26; gate made accessory-aware 2026-08-06, daemon half 2026-08-07 (issue #2205).** Optional push-to-talk path; absent remote costs 0 resident RAM and is not a voice-daemon health failure. A paired remote satisfies the voice-input **gate** on its own, and a no-local-mic box now plans zero wake legs and serves the button instead of parking — driven by the reconciler's published `JASPER_LOCAL_MIC_PRESENT`, not by anything the daemon guesses. End-to-end "the speaker wakes via the remote" is **not yet hardware-verified**. **Realtime delivery additionally depends on the CE reservation landing** — see the degraded mode below |

The original Workstream C gap was the **microphone**. A later JTS5
dual-Apple unplug incident found one output-side edge too: when one Apple
DAC disappeared, the reconciler did not always run before `outputd`
restarted against stale dual-DAC env. The output side now has the same
two-direction convergence guarantee.

The output reference is deliberately a side branch, not a playback
prerequisite. `jasper-outputd` opens and primes the physical DAC first; an
unavailable XVF3800 USB-IN PCM changes only
`reference_outputs.chip_ref_writer` to `degraded` and starts bounded background
retries; a non-recoverable worker/configuration fault is `failed` and calls for
an outputd restart after correction. `jasper-doctor` warns about that AEC degradation while the outputd
playback check remains healthy. This separation prevents the 2026-07-10 JTS3
failure mode where an absent microphone caused outputd startup to fail and a
reconciler retry left CamillaDSP writing the active lane while outputd read the
passive lane.

## The mechanism (mic)

Three layers, defence-in-depth. Layers 1 and 2 mirror the
`voice-provider-unset` handling (reconciler park + daemon `EX_CONFIG`
exit) and the **output** `ExecCondition` gate; nothing here is a new
mechanism.

### Layer 1 — presence gate: `ConditionPathExists` on a reconciler-owned marker (primary)

`jasper-voice.service` carries:

```ini
ConditionPathExists=!/var/lib/jasper/voice-input-absent
```

A **negated** condition evaluated by PID 1 (as root, *before* the unit's
sandbox/`User=`/`StateDirectory=` are set up — no permission or ordering
traps). When the marker exists, the unit is **skipped cleanly**:
`ActiveState=inactive`, *not* counted as a start, *not* subject to
`Restart=`, *never* escalates to `StartLimitAction=reboot`. This is the
same clean-skip property the output owner relies on (see
[`jasper-outputd.service`](../deploy/systemd/jasper-outputd.service)
`ExecCondition`).

The marker is the **negative** ("voice input is known-absent") and lives
in **persistent** storage (`/var/lib/jasper`, not `/run`). Both choices
are load-bearing:

- **Negative + fail-open.** No marker ⇒ condition true ⇒ voice runs.
  A fresh box, or one where the reconciler never ran (bug, missing
  prereq), behaves exactly as today. The gate can only *withhold* voice
  when the reconciler positively determined there is no mic.
- **Persistent + boot-safe.** The marker survives reboot, so on a no-mic
  box PID 1 knows "no mic, skip voice" at the very first moment of boot —
  voice never even attempts to start. A `/run` (tmpfs) marker would be
  cleared on boot and re-introduce the start-before-reconcile race that
  caused the original incident.

`jasper-aec-reconcile` is the **single writer**. It already owns the
"is there usable voice input, and should voice run" decision (it resolves
`JASPER_MIC_DEVICE`, starts/stops the AEC bridge, and starts/parks
voice). It now also expresses that verdict as the marker:

- every path that **stops voice for no input** (`stop_voice`, reached only
  when no candidate local mic is present *and* no accessory mic is paired)
  → **creates** the marker;
- every path that **(re)starts voice because input is available**
  (`restart_voice`) → **removes** the marker;
- the **custom-`JASPER_MIC_DEVICE`** early-exit (nonstandard hardware /
  corpus rigs, which the reconciler deliberately does not manage) →
  **removes** the marker, so a custom-mic operator is never gated by us
  (their device's openability is enforced by Layer 2 instead).

#### The gate is an OR over two owners (issue #2205)

"Usable voice input" is not only a local microphone. A paired mic-bearing
accessory (today: the WiiM Remote 2) is a usable push-to-talk input on its
own, and the two facts have two legitimate owners:

| fact | owner | published as |
|---|---|---|
| a **local** mic is present | `jasper-aec-reconcile` | its own resolution |
| an **accessory** mic is paired | `jasper-accessory-reconcile` | `JASPER_MANUAL_MIC_SOURCES` in `/var/lib/jasper/accessory-mics.env` |

The marker is the **AND of their absences**. `ConditionPathExists` cannot
express an AND, so the AND is computed *before* the single write rather than
split across two markers: `stop_voice` consults the accessory half first and
parks only when both are absent. Before this, a box with no local mic and a
paired remote was condition-skipped before Python ran, and the remote's button
did nothing.

Three boundaries keep this from becoming two owners of one fact:

- The AEC reconciler reads the **published env file**, never BlueZ — the same
  posture it takes toward `grouping-voice.env`. It also does not parse the file
  in shell: [`jasper.accessories.mic_env`](../jasper/accessories/mic_env.py)
  owns the path and the entry format, and the reconciler shells out to it, so
  no second parser exists to drift. That parser is deliberately **stricter than
  it needs to be**: it rejects a file it cannot parse *exactly*, because
  `Config.from_env` raises on a malformed entry and `RuntimeError` is not one of
  jasper-voice's clean-park exits. Opening the gate for a file the daemon will
  reject would crash-loop it into `StartLimitAction=reboot`; parking is the safe
  answer.
- The accessory reconciler never writes the marker. When its half moves it
  calls `refresh_voice_input`, which asks systemd for the gate owner's
  `LoadState`/`UnitFileState` and then does exactly one of two things:
  **owner enabled** → `systemctl start jasper-aec-reconcile`, so the marker's
  single writer re-derives the AND and takes the matching action; **owner
  absent or disabled** → `try-restart jasper-voice`, which restarts it *only if
  it is already running* and can never start a stopped one.
  See "Which boxes reach `refresh_voice_input`" below for why the second branch
  reads unit state instead of starting and hoping.
- `stop_voice`'s accessory restart is **deferred** to the end of the pass. The
  restart is queued with `--no-block`, and the calling paths still rewrite a
  stale `JASPER_MIC_DEVICE=udp:PORT` to a real candidate; restarting at the
  decision point would race voice into reading the device being replaced.

**Which boxes reach `refresh_voice_input`.** Three shapes. The second is why
the second branch reads unit state instead of starting the owner and hoping.

- **Full speaker.** `install_systemd_units` installs *and* enables both
  reconcilers, so the gate owner is `LoadState=loaded UnitFileState=enabled` →
  start it. This is the path the fix exists for.
- **Fresh streambox.** `install_streambox_systemd_units` installs **neither**
  reconciler — both `install` lines live in `install_systemd_units` — so
  nothing calls `refresh_voice_input` there at all. (An earlier revision of
  this section claimed the call happened and answered "not installed". It does
  not happen; the case was unreachable as written.)
- **Converted full → streambox.** `park_streambox_brain_units` runs
  `systemctl disable --now` on `jasper-aec-reconcile` and names the accessory
  reconciler nowhere, and no install path removes either unit *file*. So both
  files survive the conversion: the accessory reconciler keeps running (and
  `jasper.source_intent` starts it on every Bluetooth transaction, gated only
  on `unit_available`) while the gate owner sits **installed and disabled**.
  `systemctl start` on a disabled-but-installed unit **succeeds** — `disable`
  only drops the `WantedBy` symlink — and the gate owner's `restart_voice`
  runs `systemctl enable jasper-voice.service`. Starting it here would
  persistently re-arm the voice brain on a Zero-class box whose whole profile
  exists to keep it off. Hence `enabled` — not "the start returned 0" — is the
  permission to start.

The two shapes that do not start the owner fall back to `try-restart
jasper-voice`, which is a no-op unless voice is already running, so a live
push-to-talk session picks up the new source and a deliberately-stopped voice
brain stays stopped. Verified on jts4 (2026-08-07): `try-restart` on a loaded
unit that is `inactive`, and on one that is `failed`, returns rc=0 and leaves
`ExecMainStartTimestamp` untouched, while a plain `start` on the same unit
does re-run it. The converted-box shape above is read from
`deploy/lib/install/systemd-units.sh`, not observed on a box — jts4 is a spike
box whose accessory unit was placed by hand, so it is evidence for the
*absent* branch (`LoadState=not-found`), not for the parked one.

`park_managed_xvf` is deliberately **not** accessory-aware. It parks a mic that
*is* attached but is not safely usable (wrong firmware, unapproved output DAC,
alignment not commissioned) and leaves `JASPER_MIC_DEVICE` on the AEC bridge's
`udp:PORT` while that bridge has just been stopped. Starting voice there would
bind an unfed UDP socket and watchdog-restart into `StartLimitAction=reboot`.
Un-parking that path needs the mic device normalised away from `udp:` first;
that is its own change.

#### Which half satisfied the gate — the published local verdict

Opening the gate is not the same as the daemon being able to *serve* an
accessory-only box, and for a while it was not: `_configured_wake_legs` built
the primary (`"on"`) leg unconditionally, `daemon_main` re-raised
`InputDeviceUnavailable` for it, and the daemon exited 66 before the
manual-mic loop below it ever ran. The gate opened and the remote's button
still did nothing.

The marker structurally cannot fix that, because it is the AND: its presence
means "neither half", and its **absence** does not say which half answered. So
the gate owner publishes its own half as a fact, and the daemon reads it:

```sh
JASPER_LOCAL_MIC_PRESENT=1|0|unknown   # /etc/jasper/jasper.env
```

- `write_local_mic_presence_env` in `jasper-aec-reconcile` writes it **once
  per pass, before the branch tree**, so no *reconciling* path can exit
  leaving a stale value, and every `restart_voice` below picks up the fresh
  one. Two exits precede it and both are deliberate no-op modes that mutate
  nothing: `--check-aec-ready` (the read-only `ExecCondition` probe) and the
  live-commissioning-marker hand-off.
- `1` / `0` are the same `PRESENT_MIC` (`first_present_candidate`) probe every
  mic-selecting branch already trusts. `unknown` is a custom
  `JASPER_MIC_DEVICE` — an operator device the reconciler deliberately does
  not manage, whose name need not appear in `MIC_CANDIDATES` at all, so a
  missing candidate card says nothing about it.
- `Config.local_mic_present` (`bool | None`) reads it. `_configured_wake_legs`
  plans **zero** wake legs when, and only when, it is an explicit `False`
  *and* `manual_mic_sources` is non-empty. An empty leg plan and "no primary
  mic" then agree by construction rather than by two derivations that can
  disagree.

The daemon must not re-derive this. `Config.mic_device` defaults to the
literal `"Array"` when unset, and the reconciler writes a real candidate name
on its no-mic paths to clear a stale `udp:`, so "empty or odd `mic_device`" is
not evidence of absence on any real box.

The tri-state is what keeps **"this speaker has no room mic"** separable from
**"the room mic should be here and isn't."** Only an explicit `0` drops the
leg; `unknown` and an absent key keep the pre-existing behaviour, so a mic that
is *attached but unusable* — wrong firmware, busy, failing to open — still
plans its leg, still raises, and still parks loudly (Layer 2) instead of
quietly downgrading a mic-bearing speaker to push-to-talk.

**Where that protection stops, stated plainly.** The probe behind `1`/`0` is
`first_present_candidate` — enumeration, not health. A mic that is simply
**unplugged** enumerates as nothing and publishes `0`, exactly like a speaker
that never had one; with a remote paired, that box silently becomes
push-to-talk instead of parking. That is the intended trade, not an oversight:
a household that unplugs the mic and keeps using the remote should keep being
answered, and the alternative — parking a speaker that has working input — is
worse. What the tri-state buys is the *attached-but-broken* case above, which
is the one where silence would be a lie. The visible signal for the unplugged
case is `/state.voice.push_to_talk_only`, plus the doctor's softened
`mic ALSA card` / `mic capture` warns (issue #2205) — not the `microphone`
line, which reads `ok` on this box, and not a park.

The daemon derives the resulting mode ONCE — `WakeLoop._push_to_talk_only`,
from what it actually opened (zero wake legs plus at least one manual source) —
and every site that acts on it reads that: the keepalive branch below, the
`NO_ROOM_MIC` refusal, and `session_status()` →
**`/state.voice.push_to_talk_only`**. The `/state` field is why the mode is
visible rather than inferred: an empty `wake_legs` list on its own reads
identically to a daemon whose legs all failed to open, which is the opposite
diagnosis. `jasper-doctor`'s `Wake legs` check reads the same two published
facts and reports `n/a` on this box instead of a permanent yellow.

On the zero-leg box the daemon then substitutes a keepalive tick
(`PTT_KEEPALIVE_INTERVAL_SEC`, 2 s) for the primary mic's frame stream, because
that stream was also the Tier-1 heartbeat's liveness proof. The tick is an
honestly weaker claim — a frame proves capture *and* the loop, a tick only the
loop — which is why `_require_usable_input` refuses to run a daemon that
opened neither a wake leg nor a manual mic: otherwise it would pat its
watchdog forever while deaf.

**Startup only.** Nothing watches the accessory's liveness at *runtime*:
`UdpMicCapture.frames()` has no timeout, so a dead remote blocks its task
rather than ending it, and the tick keeps patting `WatchdogSec` — a dead
remote reads as a healthy speaker to systemd, `/state`, and the doctor alike.
Tracked as issue #2243. The detector cannot be a frame timeout (silence is a
push-to-talk device's steady state); it is Bluetooth connection state, which
the accessory reconciler owns.

Runtime, not gate: `manual_session_start` with **no source** on such a speaker
is refused (`NO_ROOM_MIC`) with a WARN and the `no_room_microphone` cue.
Accepting it opened a turn nothing could feed — music ducked, chirp played,
zero bytes sent, killed by the idle watchdog ~20 s later with no warning at
all, because both end-of-turn warnings are keyed on `bytes_sent > 0`.

**Cold boot still fails closed where it matters.** A box with *no* input at all
carries the marker from its last reconcile and is gated at instant zero, exactly
as before. A box whose accessory satisfies the gate correctly boots with the
marker absent. A pairing change made while the box was powered off converges on
the first accessory reconcile of the next boot — `accessory-mics.env` is
persistent, so a rewrite means BlueZ genuinely disagreed with the last known
state, and that rewrite is what triggers `refresh_voice_input`. No reboot loop
in either direction.

`restart_voice` queues the `jasper-voice` restart with
`systemctl --no-block restart`. This is load-bearing: `jasper-voice` is
`Type=notify` and may itself be waiting on other startup dependencies, so
the AEC oneshot must apply its single-writer verdict and exit rather than
blocking inside PID 1's job graph. `jasper-aec-reconcile.service` also has
`TimeoutStartSec=60`; if a future edit reintroduces a blocking child, the
mistake becomes a visible failed unit instead of an indefinite
`activating` state that leaves voice looking permanently offline.

Why a reconciler-written marker instead of a direct `/proc/asound/$card`
check like the output owner's `ExecCondition`: the mic is reached via
`udp:PORT` (the AEC bridge) or a candidate list, and "is there a usable
mic" depends on firmware channel count and the owned-vs-custom
distinction. That resolution is the reconciler's job and lives in one
place; the unit stays a dumb gate. The richer "why" is in the journal
(`event=aec_reconcile…`) and the doctor.

### Layer 2 — clean exit: daemon parks instead of crashing (backstop)

Layer 1 cannot cover three residual cases: a custom mic the reconciler
won't gate; a mic that ALSA enumerates but PortAudio can't *open* (busy,
firmware glitch); and the very first boot of a fresh no-mic box before
any reconcile has written the marker. For those, the daemon itself must
not crash-loop.

In [`jasper/voice/daemon_main.py`](../jasper/voice/daemon_main.py) the
leg factory opens the **primary** ("on") wake leg's mic. On failure it
now raises `InputDeviceUnavailable`
([`jasper/audio_io.py`](../jasper/audio_io.py)); `main()` catches it,
logs, and exits **`66`** (`os.EX_NOINPUT`, `VOICE_MIC_UNAVAILABLE_EXIT`).
The unit lists `66` in both `SuccessExitStatus=` and
`RestartPreventExitStatus=` — exactly the treatment provider-unset gets
with `78` — so the daemon **parks clean** instead of looping toward a
reboot.

Trade-off (deliberate): a parked daemon does **not** auto-retry; it
waits for a reconcile/udev event (or a manual `systemctl restart`).
Standard mics recover on replug via the existing udev → reconcile →
`restart_voice` path. A custom mic, or a *transient* "device busy" with
no re-enumeration event, parks until the next reconcile/restart rather
than retrying. This is the price of "never reboot-loop," and it is the
right call: a persistent open failure is a real fault that should be
visible (doctor/journal), not retried into a reboot.

### Layer 3 — observability: idle vs broken (one source of truth)

The read side is unified behind one reader,
[`jasper.mic_presence.read_mic_presence()`](../jasper/mic_presence.py): every
status surface *displays* its verdict instead of independently re-probing
ALSA / `lsusb` / PortAudio (which is how "no mic" used to surface as a scatter
of contradicting lines). **It is mic-agnostic** — `present` is driven by the
generic gate marker (true for the XVF `Array`, the `L16K6Ch` variant, or a
custom non-XVF mic such as a UMIK-2), while the XVF runtime-profile JSON
(`/run/jasper-mic-profile/xvf3800.json`) is XVF-only *enrichment* layered on
top. Driving presence off the XVF profile would report a working non-XVF mic
as "absent"; the separation exists to prevent that, and generalises when a
second mic profile + `jasper/mics/base.py` land (see
[HANDOFF-mic-fusion-architecture.md](HANDOFF-mic-fusion-architecture.md)).

- **`jasper-doctor`.** One headline, `check_microphone`
  ([`jasper/cli/doctor/audio.py`](../jasper/cli/doctor/audio.py)), states
  present/absent + why in a single line — `warn` (one yellow flag) when
  absent, never `fail`. The per-device checks defer to it via
  `read_mic_presence().absent_confirmed`: `check_mic_card_matches_config` no
  longer re-runs `arecord -L` to emit a contradicting red ✗, and
  `check_mic_capture` reports the same expected idle. A genuine open failure
  with a mic *present* (custom/busy) still falls through to the probe + its
  **fail** — a real signal. `check_service_runtime_state`
  ([`jasper/cli/doctor/resilience.py`](../jasper/cli/doctor/resilience.py))
  treats `inactive` as ok and `failed`/`activating` as fail, so a Layer-1 park
  reads ok while a crash reads fail.
- **`/state`.** A top-level `microphone` block carries the full record
  (present, reason, card, variant, channels, a ready-made `summary`); the
  voice block's `parked_no_mic` is derived from the **same** read so the
  boolean and the record can't drift.
- **`/mic` / landing page.** The control endpoint returns first-class
  `status` values: `parked` for a bonded follower, `starting` while
  systemd reports `jasper-voice` as activating/reloading/deactivating, and
  `offline` for a real unreachable daemon. The landing page is a dumb
  renderer of that payload: `starting` reads as "Reloading", not permanent
  failure.
- **Open-failure log.** `_log_audio_open_failure`
  ([`jasper/audio_io.py`](../jasper/audio_io.py)) logs one line and skips its
  portaudio/`arecord`/`aplay`/`dmesg` dump when the mic is confirmed-absent —
  the dump is for *surprise* failures, not the expected no-mic state.
- **Journal.** PID 1 logs the condition skip; the reconciler logs the
  marker write with its reason.

## Output side repair (2026-06-22)

The output owner still has a start-time `ExecCondition`: if the resolved
final-output card in `JASPER_AUDIO_DAC_CARD` is gone, and the backend is
not `fake`, `jasper-outputd.service` parks cleanly before the Rust process
opens ALSA. That catches the simple "configured card vanished" case.

The JTS5 dual-Apple incident exposed a subtler case: with two Apple USB-C
DACs saved as one four-channel profile, unplugging one child can leave
`JASPER_AUDIO_DAC_CARD` naming the surviving child. The `ExecCondition`
passes, but outputd then fails while opening the stale second child PCM
(`JASPER_OUTPUTD_DUAL_DAC_B_PCM`, e.g. `hw:CARD=A_1,DEV=0`). If the
reconciler has not already rewritten `/var/lib/jasper/outputd.env`, the
normal `Restart=on-failure` attempt repeats the stale dual-DAC config and
can hit the restart burst.

The repaired output ladder is:

1. **udev add/change/remove on ALSA control nodes** still triggers
   `jasper-audio-hardware-reconcile.service` through
   `SYSTEMD_WANTS`. This remains the generic surface for current and
   future output hardware.
2. **Apple USB remove** additionally runs
   [`jasper-output-hardware-hotplug`](../deploy/bin/jasper-output-hardware-hotplug),
   which asks systemd to start the reconciler with `--no-block`. This
   covers remove paths where the disappearing `controlC*` device does not
   activate `SYSTEMD_WANTS`.
3. **outputd failure** runs
   [`jasper-outputd-failure-reconcile`](../deploy/bin/jasper-outputd-failure-reconcile)
   from `ExecStopPost`. It skips normal stops and `ExecCondition` parks.
   For ordinary retryable failures it invokes
   `jasper-audio-hardware-reconcile --reason outputd-failure --no-restart`;
   the next built-in `Restart=on-failure` attempt then reads fresh
   `outputd.env` (single-Apple `single_alsa` when one DAC remains, or
   `fake` when none remain). For `EX_CONFIG=78`, where
   `RestartPreventExitStatus=78` would normally park immediately, the
   helper gives the system one short-window reconcile plus explicit
   `systemctl --no-block restart jasper-outputd.service`. A second config
   exit inside that window skips retry and leaves the unit parked instead
   of restart-looping into `StartLimitAction=reboot`.

`/sound/` also keeps the saved speaker topology separate from the current
observed hardware. A saved dual-Apple active topology is not silently
deleted when one DAC is unplugged; the page shows the saved topology, the
currently attached hardware, and a mismatch blocker before active-speaker
commissioning actions. `jasper-doctor` uses the same split: "Output
hardware state" reports the current reconciler-owned hardware, while
"active speaker output hardware" owns saved-topology mismatch.

## HID and Bluetooth accessories

The HID accessory bridge
[`jasper-input`](../deploy/systemd/jasper-input.service) runs a pyudev
hot-plug monitor in-process and opens evdev fds as devices appear, so it
already converges both directions without per-device units. The WiiM
Remote 2 microphone is different from ordinary HID buttons because its
audio arrives over a BLE GATT notification stream; its adapter
[`jasper-wiim-remote-mic`](../deploy/systemd/jasper-wiim-remote-mic.service)
is profile-gated by
[`jasper-accessory-reconcile`](../deploy/systemd/jasper-accessory-reconcile.service).
When BlueZ has no paired WiiM Remote 2, the reconciler removes
`/var/lib/jasper/accessory-mics.env` and disables the adapter, so there
is no resident BLE decoder and no UDP listener in `jasper-voice`. When
the profile is paired, the reconciler writes `wiim_remote_2=udp:9892`,
enables the adapter, and restarts `jasper-voice` only when the published
manual-mic env changes and voice is already active. Install/env-change
reconciles restart the adapter so code/config updates take effect; no-change
boot/connect reconciles use `start`, which is a no-op for an already-running
adapter and avoids interrupting a remote mic session. Reconcile runs at
boot/deploy and after successful Bluetooth pair/connect/forget operations, so
the UI pairing flow converges without a second deploy. Adapter service changes
are queued with `systemctl --no-block` and the boot reconciler orders only
before `jasper-voice`, not before the adapter it may start, so optional
accessory state cannot wedge voice startup. The accessory reconciler also carries
`TimeoutStartSec=60`, matching the AEC oneshot: future blocking mistakes fail
visibly instead of holding voice startup forever. Grouping has a separate
finite 300-second bound because it may join one pre-existing accessory and
fan-in coupling activation before queuing fresh post-role passes, with bounded
manager-call overhead and terminal margin. A
paired-but-sleeping remote still self-heals:
missing GATT report logs `event=wiim_remote_mic.not_ready` (throttled
after the first visible event) and retries; `jasper-voice` keeps the
normal primary mic path alive and only routes the manual source when
`/session/start` names it.

### BLE connection-event reservation — a second convergence, per connection

Getting the adapter running is necessary but not sufficient for realtime
audio. The remote's mic needs 62.5 GATT notifications/second and each one
takes about 6 Link Layer PDUs, but BlueZ hardcodes the connection-event
length to 0. On the Pi Zero 2 W's BCM43436 that default admits roughly one
PDU per connection event, so the mic runs at about a quarter of realtime
(measured on jts4: 196/190 packets against 794/805 with the reservation).

So right after `char.call_start_notify()`, the adapter asks jasper-control's
restart broker to `start`
[`jasper-wiim-remote-ce`](../deploy/systemd/jasper-wiim-remote-ce.service),
a root oneshot that issues one `HCI_LE_Connection_Update` on the live link
(design + threat model in
[HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md)). **The
reservation lives on the connection**, so it is lost on every disconnect and
cannot be expressed as unit ordering — the adapter re-requests it on each
reconnect, and the helper is `StartLimitIntervalSec=0` so a flapping link
cannot rate-limit it into `failed`.

This adds one convergence dependency worth knowing about when reading a
slow-mic report: **realtime delivery now also needs `jasper-control` to be
up**, because the broker socket is the adapter's only route to the helper
(the adapter is non-root, so there is no direct-systemctl fallback).

The new mode is **degraded but working**, and every step fails soft:

- broker unreachable, unit missing, or the request rejected — the adapter
  logs `event=wiim_remote_mic.ce_request ok=0` and carries on. Audio flows
  at the starved rate; the mic is not dead.
- helper ran but changed nothing — `event=wiim_remote_ce.skipped` at WARNING
  with a reason (`no_le_links`, `no_match`, `ambiguous`, `handle_reused`,
  `bluez_unavailable`, `conn_list_failed`). In production this only runs
  after the adapter subscribed to a live name-matched link, so any of these
  is an anomaly.
- controller refused or never confirmed — `event=wiim_remote_ce.not_applied`
  at WARNING, and the unit exits 1 so it is visible in `systemctl --failed`.
- applied — `event=wiim_remote_ce.applied`. The `ce_*_requested` fields are
  named that way deliberately: `LE Connection Update Complete` carries no CE
  fields, so the interval/latency/timeout in that line are confirmed and the
  CE values are what we asked for.
- **applied, then silently reverted mid-connection.** This is the one mode
  with no log line at all, so read it before concluding from an `applied`
  event that the reservation is still in force. `hci_le_conn_update()` in
  `net/bluetooth/hci_conn.c` hardcodes `cp.min_ce_len = cp.max_ce_len = 0`,
  and that is the function BlueZ calls to service a **peripheral-initiated**
  L2CAP Connection Parameter Update Request. So a remote that asks for its
  own parameters — typically slower ones when it goes idle — overwrites our
  reservation, and nothing re-requests it until the next disconnect.

  **Measured not to fire, on jts4, 2026-08-06:** the reservation was applied
  at 18:38:49 and five push-to-talk holds spanning 18:40:31–18:42:43 all
  delivered at full rate (990 packets, `bad_packets=0`), including across a
  74 s idle gap — so it survived ~4 minutes and multiple idle→active cycles
  on this remote.

  **Re-arm machinery was deliberately declined**, not overlooked. The two
  implementations cost different things, and they are priced separately here
  so a future reader re-opens the decision on the real numbers:

  - *Watch the HCI event stream* for the parameter update. HCI has no read
    path for live connection parameters, so this means a btmon-style monitor
    subscription — **a new resident process** on a 415 MB Pi Zero 2 W, which
    is the expensive option.
  - *Watch the packet rate* in the adapter and re-request when it collapses.
    This one is **not** a residency cost: the adapter is already resident and
    already counts packets (`WiimVoicePacketStream.packets`), and the
    discriminator is clean — ~62.5/s holding, ~15/s starved, 0 idle. Its real
    cost is **complexity**: a rate window, starved-versus-idle logic, and
    debounce that has to interact correctly with the helper's start path.

    Only the **automatic re-request** is declined. The *measurement* half is
    cheap and now ships: `WiimVoicePacketStream.close_segment` logs each hold's
    rate as `event=wiim_remote_mic.segment` (see the triage recipe below). It
    needs no rate window or debounce because a hold boundary already exists —
    the >250 ms gap that resets the stream. So an operator can see a starved
    link; nothing acts on it automatically.

  Both were declined for the same reason — neither is worth carrying to defend
  against something this remote was measured not to do. If a slow-mic report
  ever survives all the checks above with an `applied` event in the journal,
  **this is the remaining explanation**: capture `btmon` during an idle→active
  cycle, look for a peripheral-initiated parameter update, and reopen with
  that evidence. The packet-rate watcher is the cheaper of the two to build if
  it comes to that.

**How to tell a starved link from a healthy one.** Read the delivery *rate*,
not a packet count:

```sh
journalctl -u jasper-wiim-remote-mic | grep 'event=wiim_remote_mic.segment'
# event=wiim_remote_mic.segment packets=625 duration_ms=9984 rate_hz=62.5
```

One line is emitted per stream segment — in practice one push-to-talk hold —
when the next hold begins or when the link drops mid-hold. **`rate_hz` near
62.5 is realtime; near 15 is the starved link this section is about**, and the
two are otherwise indistinguishable: the count alone has no denominator (the
same 10 s hold reads `packets=625` healthy and `packets=148` starved), the
packets still decode so `bad_packets` stays 0, and the starved 67.5 ms
inter-packet gap is well under the 250 ms `WIIM_STREAM_GAP_SEC`, so
`stream_reset` cannot fire either. The cumulative `packets` in
`event=wiim_remote_mic.disconnected` spans idle time as well as holds, so do
not try to derive a rate from it.

Two symptoms to keep apart: a low `rate_hz` with `bad_packets=0` is a
*delivery* problem and points here; distorted or wrong-sounding audio is
decode and does not.

## Verified vs needs-hardware

**Verified hardware-free (tests):**

- Reconciler creates the marker on the no-mic paths, removes it on the
  mic-present paths, and removes it for a custom mic
  ([`tests/test_aec_reconcile.py`](../tests/test_aec_reconcile.py)).
- The unit carries `ConditionPathExists=!<marker>` and `66` in both
  `SuccessExitStatus`/`RestartPreventExitStatus`, and the marker path
  agrees across the unit, the reconciler default, and the Python helper
  ([`tests/test_voice_input_gate.py`](../tests/test_voice_input_gate.py)).
- `main()` exits `66` on `InputDeviceUnavailable`; the doctor reports
  expected-idle when the marker is present.
- Output hardware hotplug and outputd-failure helpers request reconcile
  without blocking udev/systemd, skip non-retrying stops, and give outputd
  `EX_CONFIG=78` one bounded reconcile/retry before preserving the park
  ([`tests/test_output_recovery_scripts.py`](../tests/test_output_recovery_scripts.py),
  [`tests/test_outputd_wiring.py`](../tests/test_outputd_wiring.py),
  [`tests/test_outputd_systemd.py`](../tests/test_outputd_systemd.py)).
- `/sound/` and `jasper-doctor` keep current output hardware readiness
  separate from saved active-speaker topology mismatch
  ([`tests/test_sound_setup.py`](../tests/test_sound_setup.py),
  [`tests/test_doctor_audio.py`](../tests/test_doctor_audio.py)).

**Needs a real plug/unplug hardware pass (flag for the next on-Pi
session):**

1. **Cold boot with no mic** never starts `jasper-voice` (condition
   skip), `jasper-doctor` is all-green with "no microphone present
   (expected)", and **no reboot** occurs. Confirm
   `systemctl show jasper-voice -p NRestarts` stays `0` and
   `ActiveState=inactive`.
2. **Hot-unplug the XVF3800 while running**: udev fires
   `jasper-aec-reconcile`, voice stops cleanly, marker appears,
   `/state.voice.parked_no_mic=true`. Confirm no restart spiral in
   `journalctl -u jasper-voice`.
3. **Hot-plug the XVF3800 back**: udev → reconcile removes the marker and
   `restart_voice` brings voice up within seconds; "Hey Jarvis" works
   without any manual step.
4. **Deploy/update a box with no mic attached**: install succeeds, leaves
   voice cleanly parked (not failed), and the post-deploy verification
   does not report voice healthy (Workstream B owns broadening that
   verification; this PR ensures the parked state is *correct and
   labelled*).
5. **Output DAC unplug/replug** converges in both directions. For the
   dual-Apple case, unplug one child and confirm:
   `journalctl -u jasper-audio-hardware-reconcile` shows a hotplug or
   outputd-failure reconcile, `/run/jasper-output-hardware/output_hardware.json`
   reports the single remaining Apple DAC, `/var/lib/jasper/outputd.env`
   switches to `JASPER_OUTPUTD_SINK=single_alsa`, and `jasper-outputd`
   restarts without reaching the start limit. `/sound/` should show
   "Saved speaker topology" separately from "Currently attached hardware"
   and block active-speaker actions with a saved/attached mismatch.

## Files

- [`deploy/systemd/jasper-voice.service`](../deploy/systemd/jasper-voice.service) — `ConditionPathExists` gate, exit-66 park
- [`deploy/bin/jasper-aec-reconcile`](../deploy/bin/jasper-aec-reconcile) — single writer of the marker; publisher of `JASPER_LOCAL_MIC_PRESENT`
- [`deploy/systemd/jasper-aec-reconcile.service`](../deploy/systemd/jasper-aec-reconcile.service) — bounded oneshot wrapper for mic/AEC/voice convergence
- [`jasper/voice/input_presence.py`](../jasper/voice/input_presence.py) — marker path + `voice_parked_no_mic()`
- [`jasper/voice_daemon.py`](../jasper/voice_daemon.py) — `_configured_wake_legs` (reads the published local verdict), the push-to-talk keepalive, and the `NO_ROOM_MIC` refusal + cue
- [`jasper/accessories/mic_env.py`](../jasper/accessories/mic_env.py) — accessory-mic env path + entry format; the accessory half of the gate
- [`jasper/mic_presence.py`](../jasper/mic_presence.py) — the voice-input SSOT reader (mic-agnostic presence + accessory sources + XVF enrichment)
- [`jasper/audio_io.py`](../jasper/audio_io.py) — `InputDeviceUnavailable`; absent-aware open-failure log
- [`jasper/voice/daemon_main.py`](../jasper/voice/daemon_main.py) — raise on primary mic-open failure, exit 66
- [`jasper/cli/doctor/audio.py`](../jasper/cli/doctor/audio.py) — `check_microphone` headline + mic checks deferring to the reader
- [`jasper/control/server.py`](../jasper/control/server.py) — `/mic` parked/starting/offline state surface
- [`jasper/control/state_aggregate.py`](../jasper/control/state_aggregate.py) — `microphone` block + `voice.parked_no_mic`
- [`deploy/udev/99-jasper-audio-hardware-reconcile.rules`](../deploy/udev/99-jasper-audio-hardware-reconcile.rules) — output-DAC add/remove/change triggers
- [`deploy/bin/jasper-output-hardware-hotplug`](../deploy/bin/jasper-output-hardware-hotplug) — Apple USB remove reconciler request
- [`deploy/bin/jasper-outputd-failure-reconcile`](../deploy/bin/jasper-outputd-failure-reconcile) — outputd retry-time env refresh
- [`deploy/systemd/jasper-outputd.service`](../deploy/systemd/jasper-outputd.service) — output device gate + failure-time reconcile hook
- [`deploy/systemd/jasper-accessory-reconcile.service`](../deploy/systemd/jasper-accessory-reconcile.service) — optional accessory mic profile gate
- [`deploy/systemd/jasper-wiim-remote-mic.service`](../deploy/systemd/jasper-wiim-remote-mic.service) — optional BLE remote mic adapter
- [`deploy/systemd/jasper-wiim-remote-ce.service`](../deploy/systemd/jasper-wiim-remote-ce.service) + [`jasper/cli/wiim_remote_ce.py`](../jasper/cli/wiim_remote_ce.py) — per-connection BLE connection-event reservation, re-requested on every reconnect

Last verified: 2026-08-07 (Layer 1 re-derived against the reconcilers and units
for the voice-input-gate OR, issue #2205, including which install profiles
reach `refresh_voice_input` — read from `deploy/lib/install/systemd-units.sh`
— and `try-restart`'s no-op behaviour measured on jts4. The "which half
satisfied the gate" subsection is the daemon half of #2205 and is verified
against the code and its hardware-free tests only: no accessory-only speaker
has been observed answering on hardware, so treat end-to-end behaviour there as
inferred. Accessory/grouping timeout relationship retains its 2026-07-14
verification, and other subsystem claims their 2026-07-10 verification.)
