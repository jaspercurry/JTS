# ADR-0153: Failure cues are pre-rendered and content-addressed on disk, never streamed at play time

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-audible-feedback.md was trimmed
  to its operational spine; implements non-negotiable #6, "no silent deafness")

## Context

A wake-blocking failure must produce a sound. Silence in a living room, on a
device with no screen and no admin access, is unfixable from the user's side —
they cannot tell a broken speaker from a speaker that did not hear them.

The obvious implementation is to synthesize the message when it is needed: one
HTTP call to the TTS provider, play the result. That implementation fails
precisely when it matters most.

## Decision

**Every cue is rendered ahead of time to a WAV under
`/var/lib/jasper/sounds/`, named by a content hash of everything that could
change its audio. Nothing is synthesized at play time.**

The filename is `<slug>-<8charhash>.wav`, where the hash covers
`GENERATOR_VERSION`, the backend's actual synthesis model, the voice, the WAV
format, and the rendered text (post-`{hostname}` substitution) — `cue_hash()` in
`jasper/cues/generator.py` owns the exact input ordering.

Cues are baked by the same TTS provider family the box uses for conversation,
selected by `jasper.cues.factory.build_cue_tts_backend`, so the cue voice and
the assistant voice do not audibly switch mid-interaction.

## Consequences

- **The most important cue works when the network does not.** "I can't connect
  to the voice backend" is exactly the cue whose provider is unreachable at play
  time. Pre-rendering is what makes that sentence playable.
- **Cues are instant.** A one-shot TTS call costs 1–3 seconds; a cue that
  arrives that late reads as a broken speaker rather than a quick "I can't help
  right now."
- **Invalidation is unambiguous, and it is the filename.** Change a template,
  the management URL, the voice, the model, or `GENERATOR_VERSION`, and the
  expected name changes — the manager looks for it, does not find it, and
  regenerates. No mtime tracking, which gets this wrong on timezone changes,
  clock drift, and manual file copies.
- **Stale beats silent, and both beat lying.** A cache miss falls back to any
  existing `<slug>-*.wav` rather than playing nothing. If even that is missing,
  `play()` returns False and logs a warning — visibly, in
  `journalctl -u jasper-voice`, not silently.
- **The daemon never requires cues to start.** A working speaker without cues
  beats a dead speaker with them, so a failed regen (no network at boot, bad
  key, quota) logs a warning and the daemon comes up. The affected paths degrade
  to the silent failure this subsystem exists to prevent — which is why install,
  daemon startup, and the manual CLI all trigger regeneration.
- **The cost is a bake step and stale-file pruning.** Regeneration is
  synchronous (the TTS call is blocking; the daemon wraps it in
  `asyncio.to_thread`), and stale permutations are pruned at write time so the
  `<slug>-*` listing stays readable on a Pi's disk.
