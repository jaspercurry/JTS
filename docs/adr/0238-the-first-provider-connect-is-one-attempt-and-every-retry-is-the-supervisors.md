# ADR-0238: The first provider connect is one attempt, and every retry is the supervisor's

- **Date:** 2026-09-05
- **Status:** Accepted
- **Context:** The voice daemon's first connect used to run its own retry
  loop beside the reconnect supervisor's: one adapter retried only on HTTP
  409 and re-raised everything else, the other spent a 600 s wall-clock
  budget and then exited non-zero so systemd would restart it and grant
  another. Both shapes exist for a process that could neither hear nor
  speak while it dialled — exiting was how a deaf process got a second
  chance, and with `RestartSec=5` under `StartLimitBurst=20`/300 s a fast
  exit could walk a box into `StartLimitAction=reboot` for a WAN outage.
  [#4206](https://github.com/jaspercurry/JTS/pull/4206) removed that
  premise: mics, TTS, the cue manager and `READY=1` now come up before the
  connect, which runs in the background, so a dialling speaker already
  hears its wake word and plays cues. What the exit still cost was an
  announcement per budget: the outage cue plays, the budget expires, and a
  restarted process with a fresh `OutageTracker` says the same unchanged
  sentence every ten minutes for as long as the ISP is down — the failure
  [ADR-0215](0215-a-broken-cloud-connection-is-announced-once-and-only-when-a-human-must-act.md)
  exists to prevent.
- **Decision:** **The first connect is a single attempt.** Any failure the
  adapter can catch leaves the connection `FAILED` and hands the retry to
  the reconnect supervisor, which already owns backoff, transient/terminal
  classification, the nudge-interruptible wait, network-down detection and
  the edge-triggered cue. Nothing classifies a first connect specially,
  there is no initial-connect budget and no knob for one, and **no connect
  failure exits the daemon** — `Restart=` in `jasper-voice.service`
  participates in crashes only, never in provider recovery.
- **Consequences:** One retry loop instead of three, and one outage is one
  announcement: ADR-0215's edge trigger holds because the tracker now lives
  as long as the process does. A wake word during the wait shortens it
  through the reconnect nudge, which the budgeted loop could not do — it
  slept on the stop event. The daemon keeps hearing, answering local tools
  and reporting the outage on `/state` for an outage of any length. The
  cost: a first connect that fails for a locally-fixable reason no longer
  surfaces as a non-zero exit in `systemctl status`; it surfaces as the cue,
  `/state.voice.connection_error`, doctor and the journal, which is where
  ADR-0215 already put it. Deliberately given up: "a fresh process gets a
  fresh budget" as a recovery for wedged client-side state — the watchdog
  still restarts a daemon that stops heartbeating. Rejected: keeping the
  budget with the cue suppressed while it runs, which leaves two retry
  policies to keep in step and re-introduces the exit that ends the boot in
  a reboot.
