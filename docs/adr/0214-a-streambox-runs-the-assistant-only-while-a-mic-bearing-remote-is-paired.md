# ADR-0214: A streambox runs the assistant only while a mic-bearing remote is paired

- **Date:** 2026-09-02
- **Status:** Accepted

## Context

A streambox is a Pi Zero 2 W with no microphone of its own. Until now the
tier granted nothing on the capability axis and the installer parked the
voice brain outright, so "streambox" and "no assistant" were the same
sentence. A paired WiiM Remote 2 breaks that: its mic reaches the box as a
manual source and the button carries both turn boundaries, so the board can
hold a conversation without ever running always-on inference.

The two halves are separate facts, and only one of them is about the board.
`WakeLoop._handle_wake_frame` scores every frame synchronously on the asyncio
loop; where that inference eats most of a core the Tier-1 heartbeat starves
and `WatchdogSec=30s` kills the daemon. The Zero 2 W measured over that line.
Nothing about holding a conversation does.

## Decision

**1. The tier grants `ASSISTANT` and not `WAKE_DETECTION`.** The grant table
in `jasper/install_profile.py` is the single statement of that; the mic/AEC
stack rides with `WAKE_DETECTION` because always-on wake is its only
always-on consumer.

**2. On a profile with `ASSISTANT` and without `WAKE_DETECTION`,
`jasper-accessory-reconcile` owns `jasper-voice.service`'s runtime state.**
It starts the unit while at least one paired accessory declares a mic
(registry `mic.status == "adapter"`), stops it otherwise, and **never enables
or disables** — `converge_voice_unit` has three verbs and enablement is not
among them. Unit enablement stays the installer's, which on this tier drops
the boot symlink and does nothing further; re-arming `WantedBy` from the
reconciler would start a mic-less daemon on every boot. Where `WAKE_DETECTION`
is granted nothing changes: the installer enables the unit and
`jasper-aec-reconcile` remains the voice-input gate's single owner, with the
accessory reconciler handing its half back as before.

**3. The daemon plans wake legs only where `WAKE_DETECTION` is granted.**
`jasper/voice/daemon_main.py` reads the install marker once at startup and
passes the answer to `_configured_wake_legs`, which stays pure; `Config` stays
env-only. Push-to-talk-only remains a DERIVED runtime state — zero wake legs
plus at least one manual mic — never a declared one, so the profile route and
the older no-local-mic route reach it by the same derivation. A push-to-talk-only
daemon also builds no `SpeechVAD`: every reader of it is already off on a
button turn. jasper-control follows the same axis: `/session/start`,
`/session/end`, `/cue/play` and `/system/restart/voice` are allowed on any
profile granting `ASSISTANT`, while `/mic`, `/aec` and the rest of the
local-mic stack stay off a streambox's route table.

**4. The `[streambox]` pip extras carry the assistant's dependencies.**
Wake-word runtimes, AEC engines and XVF tooling stay `[full]`-only.

**5. `jasper-deploy-health` expects `jasper-voice.service` on a streambox only
while the reconciler publishes a mic source** in `accessory-mics.env`.

## Consequences

- A bare streambox, or one paired with a mic-less knob, runs exactly what it
  ran before: no voice daemon, no warning, no resident model.
- Unpair, re-pair, reboot and adapter failure all converge through one owner
  on the existing reconcile tick. There is no second writer to disagree with,
  and no state to persist between them.
- The memory claim is measured, not assumed. Measured on jts4:
  `<to be filled by the verification step>`.
- `[streambox]` does not yet list the provider SDKs decision 4 assigns it; a
  box that pairs a remote today installs them via `[full]` or not at all. That
  split is the remaining implementation of this decision, not a second one.
- A full speaker converted to a streambox keeps a disabled
  `jasper-aec-reconcile` on disk. Starting it would run
  `systemctl enable jasper-voice.service` and permanently re-arm the brain, so
  the accessory reconciler never starts a parked gate owner.
- A BlueZ discovery timeout in `reconcile_once` raises before the voice unit
  is converged, deliberately: the same tick leaves `accessory-mics.env`
  unwritten, so a timed-out probe changes neither.
- `jasper-doctor`'s `voice`/`wake` groups stay omitted on a streambox by
  role, not by live pairing state, so a streambox with a paired remote and a
  dead `jasper-voice.service` is reported only by `jasper-deploy-health`'s
  WARN (decision 5), never by doctor — a known gap left for a follow-up.
- **Rejected: keeping the installer's `disable --now` and letting the
  reconciler enable the unit on demand.** Enablement survives reboots and the
  reconciler cannot; a box that booted with nothing paired would come up
  running a daemon with no microphone, which exits `VOICE_MIC_UNAVAILABLE_EXIT`
  and parks. Start/stop expresses "for exactly as long as" and enable does not.
- **Rejected: a `LOCAL_MIC` capability.** Mic presence is dynamic and already
  owned; a full-tier Pi 5 with no mic fitted and a remote paired is a real
  configuration that a tier fact would answer wrongly.
