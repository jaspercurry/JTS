# ADR-0239: The voice daemon plays the mic-loss cue at shutdown; jasper-control has no player

- **Date:** 2026-09-05
- **Status:** Accepted
- Refs: #4027. Supersedes the mechanism sentence of R6 in
  [ADR-0235](0235-attached-hardware-one-owner-per-fact-and-no-facts-in-shell.md).
- **Context:** R6 assigned the mic-loss cue to jasper-control because that
  daemon "owns cue playback". It does not own a player. `_post_cue_play`
  (`jasper/control/handlers/voice.py:114`) proxies `CUE_PLAY <slug>` down the
  voice daemon's control socket (`jasper/control/uds.py`); the audio, the TTS
  gain and the cue cache all live in jasper-voice. And every reconciler path
  that writes the voice-input absence marker stops jasper-voice in the same
  breath (`deploy/bin/jasper-aec-reconcile:1718,1906,2036`), so a
  jasper-control poller on that marker would proxy into a socket that is
  already gone. The mechanism R6 named cannot make a sound.
- **Decision:** jasper-voice announces its own park. When
  `wake_loop.run()` returns on `SIGTERM` and `voice_parked_no_mic()`
  (`jasper/voice/input_presence.py`) is true, the daemon plays
  `no_room_microphone` through the player it already owns, then finishes
  shutting down. That reading IS the transition: the marker is written before
  the stop, and `ConditionPathExists=!` refuses every start while it stands,
  so a *running* daemon can only ever see it once, on the way down. A plain
  restart carries no marker and says nothing. The cue plays to its natural length
  through the daemon's owned ducked-output path — cutting it short truncates queued
  PCM — so `jasper-voice.service` raises `TimeoutStopSec` to 14 s, floored by
  `MIC_LOSS_CUE_STOP_FLOOR_SEC`; a player that cannot play logs one
  `event=voice.mic_loss_cue result=<code>` at WARNING and shutdown continues.
  The reconciler spawns nothing, and its unconditional top-of-pass
  bridge-ready revoke (`:204`,
  [ADR-0224](0224-the-aec-bridge-starts-on-a-reconciler-published-ready-marker.md))
  already gives R6 the ordering it asked for, so it is pinned, not re-added.
- **Consequences:** Only the mechanism sentence of R6 is superseded; the rest
  of it stands — the AEC bridge is still not taught to detect card loss, and
  G12 still closes. jasper-control keeps no watcher, no poll and no second
  cue path. The chip-AEC validation bounce (`:1906`, `activate_managed_chip_aec`)
  marks its park `transient=1`; the shutdown hook reads that
  (`voice_park_is_transient()`) and skips the cue, logging
  `result=transient_park` instead — the two real parks (`:1718`, `:2036`)
  carry no such line and still announce. G13's residual window (the bridge
  restarting between the microphone vanishing and the reconciler's next
  pass) is measured by H1, not guarded here. A daemon killed by the watchdog
  (`WatchdogSec=30s`, SIGABRT) or by SIGKILL never sees SIGTERM and plays no cue; that chain is P9's (#4208).
