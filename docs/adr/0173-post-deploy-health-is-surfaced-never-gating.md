# ADR-0173: Post-deploy health is surfaced, never gating

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

A deploy answers two different questions, and conflating them was the original
bug: "did the install *process* complete?" (hardware-independent, owned by
`install.sh` and recorded in the build manifest) and "is the running system
healthy, or correctly idle?" (owned by the post-restart `jasper-doctor` run).
The second question has no honest boolean yet. `jasper-doctor` already tells a
crash-looped daemon from a cleanly parked one — `check_service_runtime_state`
treats `failed`/`activating` as a failure and `inactive` as fine — and the
no-mic case is fully fixed: the AEC reconciler writes
`/var/lib/jasper/voice-input-absent` and `jasper-voice.service`'s
`ConditionPathExists=!…` makes systemd skip the start cleanly. But the same
reclassification has not been done for every other missing-hardware daemon (an
absent XVF, a renderer whose hardware is not present).

## Decision

**`surface_system_health` prints the doctor report and never gates the
deploy.** The deploy fails on end-state facts only: the management-surface
probe (nginx → wizard → jasper-control), and `verify_manifest_advanced`
confirming the Pi's manifest records the deployed full SHA with
`JASPER_INSTALL_STATUS=ok`.

**Removal condition:** when every missing-hardware daemon reads as idle rather
than broken, the gate tightens to "any doctor core-fail blocks the deploy."
Until then, gating would fail deploys of speakers that are working exactly as
configured.

## Consequences

- A no-mic speaker reports "install completed, voice idle for missing
  hardware" instead of either lying ("all good") or falsely failing ("voice
  broken") — which is the whole reason the two claims are kept separate.
- A genuinely broken daemon can pass a deploy. The operator sees it in the
  printed report; nothing silently swallows it.
- Both post-restart steps read the Pi over ssh, so they are skipped under
  interactive sudo (where `ssh -tt` corrupts captured output), matching the
  identity and direction guards. Passwordless sudo is the posture that gets
  fully-verified deploys.
- The seam is deliberate and dated, not an oversight to be "fixed" by someone
  adding a gate without doing the reclassification first.
