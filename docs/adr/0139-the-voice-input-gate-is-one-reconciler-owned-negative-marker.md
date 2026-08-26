# ADR-0139: The voice-input gate is one reconciler-owned negative marker

- **Date:** 2026-08-26
- **Status:** Accepted (ratified on the fix 2026-06-21; the accessory OR
  2026-08-06/07 for issue #2205; recorded here when
  HANDOFF-hotplug-resilience.md was trimmed to its operational spine)

## Context

`jasper-voice` on a box with no usable microphone used to start, fail to open
the mic, and be restarted by systemd until `StartLimitAction=reboot` fired —
a missing *optional* device rebooting the speaker, repeatedly. A start gate
was needed that PID 1 could evaluate before the unit's sandbox exists, on the
first instant of boot, without the daemon guessing at hardware.

Two independent owners can each supply usable voice input: `jasper-aec-reconcile`
resolves a **local** mic, `jasper-accessory-reconcile` publishes a paired
**accessory** mic (`JASPER_MANUAL_MIC_SOURCES` in `accessory-mics.env`).
`ConditionPathExists` cannot express an AND, and a gate that merely opens
still does not tell the daemon *which* half answered — which is the fact
`_configured_wake_legs` needs to decide whether to plan a wake leg at all.

## Decision

**One persistent negative marker, `/var/lib/jasper/voice-input-absent`, written
by exactly one owner, plus a separately published local verdict.**

- **Negative polarity**: no marker ⇒ condition true ⇒ voice runs. A fresh box,
  or one where the reconciler never ran, behaves as before. The gate can only
  *withhold* voice when the reconciler positively determined there is no input.
- **Persistent, not `/run`**: the marker survives reboot, so a no-mic box is
  gated at instant zero instead of re-running the start-before-reconcile race.
- **Single writer**: `jasper-aec-reconcile` computes the AND of both absences
  *before* the write, reading the accessory owner's published env file (never
  BlueZ) through `jasper.accessories.mic_env`. The accessory reconciler never
  writes the marker; when its half moves it calls `refresh_voice_input`, which
  starts the gate owner when that unit is `enabled`, and otherwise
  `try-restart`s voice (a no-op unless voice is already running).
- **`enabled`, not "the start returned 0", is the permission to start** the
  gate owner: a converted full→streambox keeps the unit file installed but
  disabled, and starting it there would re-arm the voice brain on a box whose
  profile exists to keep it off.
- **The tri-state `JASPER_LOCAL_MIC_PRESENT` (`1`/`0`/`unknown`)** carries the
  local half, written once per pass before the branch tree so no reconciling
  path can leave it stale. Zero wake legs are planned only on an explicit
  `False` *with* a manual source; `unknown` (a custom `JASPER_MIC_DEVICE`) and
  an absent key keep the pre-existing behaviour.

## Consequences

- A no-mic box is `inactive`, not `failed`, and never counts a start against
  `StartLimitBurst` — the reboot escalation is structurally unreachable for
  this cause.
- **Attached-but-broken stays loud.** Only an explicit `0` drops the wake leg,
  so a mic with wrong firmware, busy, or failing to open still plans its leg,
  still raises, and still parks — instead of quietly downgrading a mic-bearing
  speaker to push-to-talk.
- **Unplugged-and-remote-paired is deliberately quiet.** The probe behind
  `1`/`0` is enumeration, not health, so an unplugged mic publishes `0` exactly
  like a speaker that never had one, and the box becomes push-to-talk. That is
  the intended trade: a household that unplugs the mic and keeps using the
  remote should keep being answered. The visible signal is
  `/state.voice.push_to_talk_only` plus the doctor's softened mic warns.
- The accessory-mic parser is deliberately stricter than it needs to be: it
  rejects a file it cannot parse exactly, because `Config.from_env` raises on a
  malformed entry and `RuntimeError` is not one of jasper-voice's clean-park
  exits. Opening the gate for a file the daemon will reject would crash-loop it.
- `park_managed_xvf` is deliberately **not** accessory-aware: it parks a mic
  that is attached but unsafe to use while `JASPER_MIC_DEVICE` still names the
  stopped bridge's `udp:PORT`. Un-parking it needs the device normalised away
  from `udp:` first.
- Rejected: a direct `/proc/asound/$card` `ExecCondition` like the output
  owner's. "Is there a usable mic" depends on the AEC bridge's `udp:` indirection,
  firmware channel count, and the owned-vs-custom distinction — that resolution
  belongs to the reconciler, and the unit stays a dumb gate.
- Rejected: two markers, one per owner. `ConditionPathExists` has no AND, and
  two writers of one verdict is the drift this ADR exists to prevent.
