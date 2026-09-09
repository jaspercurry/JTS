# ADR-0271: jasper-heal is a control supervisor that observes before it acts

- **Date:** 2026-09-09
- **Status:** Accepted
- **Context:** The box already repairs itself wherever systemd can see the
  fault: `Restart=`/`StartLimitAction=`, the bootloop guard,
  jasper-camilla-recover, the outputd failure reconciler
  ([ADR-0269](0269-the-outputd-failure-reconciler-parks-on-exit-78-and-rate-limits-one-pass-per-window.md)),
  and jasper-control's supervisors and restart broker. Two household-visible
  failures sit outside all of it because every unit reads `active` in both:
  the speaker emits nothing with the audio path up, and the assistant hears no
  wake word with a reachable voice daemon and an unmuted mic. The owner
  notices those and clicks the /system dashboard's restart button. A first
  attempt shipped as a timer forking `jasper-doctor --core --json` — two
  interpreters per tick against
  [ADR-0226](0226-constrained-hardware-doctrine-push-dont-pull-no-spawns-one-interpreter.md),
  and that child re-ran the renderer `aplay` probe, serialized only by an
  in-process lock, so a tick racing the deploy gate's doctor run produced a
  false `renderer_device_unresolvable` failure.
- **Decision:** heal is a fourth supervisor inside jasper-control
  (`jasper/control/heal_supervisor.py`), on the one shared control loop, with
  no unit, no timer, no subprocess and no `JASPER_*` knob. Every fact is read
  in-process: the resident `AudioHealthSampler`'s `signal_path` and warmup
  flag, one `systemctl show` over the audio path's units, and jasper-voice's
  STATUS socket. Two cases map to actions the dashboard already offers — a
  signal-path code the household register pairs with "Restart audio" →
  `restart-audio`; a stale `last_wake_at` on a reachable, unmuted, wake-capable
  box → `restart-voice`. A guard unit that is not active means the fault is
  systemd's and heal stands down. It only OBSERVES: the module holds no
  actuator and calls no broker. A case names its action in
  `/state.resilience.heal.would_act` and logs `event=heal.would_act` once per
  episode per 30 min.
- **Consequences:** No new interpreter, no unit file, no race with the doctor;
  `/state` moves to schema 5. The bootloop guard stays the backstop above heal,
  and the `heal recency` doctor row exists because a fortnight of evidence is
  void if the observer dies quietly. ARMING CONDITION: wire one case to the
  restart broker only after a fortnight of `heal.would_act` lines on two boxes
  agrees with the operator's own diagnosis — a code change with a reviewable
  diff, which is what a first actuator on the audio path should cost. REMOVAL
  CONDITION: delete this module if that fortnight never puts a would_act line
  before a real incident. Honest note: this lands on convenience, not a
  recurrence — neither case has bitten yet, which is why it ships observing.
  `deaf` carries two open objections (24 h is a warn threshold a quiet weekend
  trips; `last_wake_at` resets on a jasper-voice restart, so the case fires at
  most once per daemon lifetime) and those lines exist to answer them.
  Rejected: a runtime knob to arm cases (a self-restarting speaker is not a
  setting), and acting on a failed unit (systemd's job; racing it makes
  restart storms).
