# ADR-0340: Microphone absence is silent

- **Date:** 2026-09-22
- **Status:** Accepted; supersedes ADR-0239's audible microphone-loss policy.
- **Context:** During the jts3 unplug test, the owner rejected both the spoken
  microphone-loss message and its replacement tone. Unplugging is a visible
  physical action and does not need an audio alert. Refs #5510, #5581.
- **Decision:** Missing microphones produce no speech or tone at shutdown,
  startup, or a refused room-microphone request. Keep the structured refusal,
  logs, status, clean stop and automatic recovery when input returns.
- **Consequences:** Remove the dedicated cue, playback hooks and cue-only
  helpers. The normal retired-cue cleanup removes cached speech and tones.
  Configuration-fault cues remain. This is an explicit exception to the
  audible-failure rule; it adds no setting or opt-out flag.
