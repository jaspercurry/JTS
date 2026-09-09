# ADR-0264: The Camilla recovery is evidence and one bounded restart

- **Date:** 2026-09-09
- **Status:** Accepted. Supersedes (partial)
  [ADR-0175](0175-a-failed-camilla-recovery-parks-the-core-graph-once.md):
  its recovery ladder, its 300 s cooldown, and its `outputd_restart_failed`
  leg. ADR-0175's park contract stands unchanged — same record path, same
  ADR-0141 field vocabulary, same disarm — and is still what three live
  citations point at.
- **Context:** ADR-0175's handler defended the snd-aloop-era failure class
  (ALSA `Device or resource busy` during deploy and renderer churn): on an
  exhausted `jasper-camilla.service` burst it stopped eleven units, restarted
  fan-in, outputd and every renderer, gated itself with a 300 s cooldown and
  restored the graph from an EXIT trap, with timeouts it called provisional.
  Since ADR-0100 CamillaDSP opens only ring ioplugs, so no renderer or
  daemon can hold a device it needs, and the contention the ladder cleared
  cannot occur.
- **Decision:** `jasper-camilla-recover` captures evidence (`/dev/snd`
  holders, `/proc/asound/*/status`, the failing unit's status), makes ONE
  `reset-failed` + `start jasper-camilla.service`, waits 3 s for liveness,
  and on failure writes the ADR-0175 park record (same path, same fields,
  reason `camilla_start_failed`) that the doctor and `/state` read. No
  unit stops, no restarts of other daemons, no cooldown, no run lock (a
  `Type=oneshot` unit cannot be started twice concurrently). Deadlines are
  derived from that body and from every unit the camilla start pulls in:
  `TimeoutStartSec=180` (three 5 s captures, the 50 s
  `jasper-audio-hardware-reconcile` the start `Requires=`, one 90 s systemd
  default covering the `Wants=` pair `jasper-outputd`/`jasper-fanin` — both
  `Type=notify` with no `TimeoutStartSec`, unordered against each other — and
  the 3 s liveness wait) and `TimeoutStopSec=5` (nothing runs after SIGTERM).
  The 90 s term is the cold case; a SIGKILL at the deadline would cost the
  park record, which the handler writes last. `jasper-camilla.service`
  keeps `StartLimitAction=none` because CamillaDSP 4.1.3 exits clean on a
  rejected config and cannot fence the config class from a reboot burst;
  the park is its fence. `jasper-outputd.service` keeps `reboot`, fenced by
  `RestartPreventExitStatus=78`, because only a reboot un-wedges the DAC it
  owns.
- **Consequences:** A CamillaDSP that dies WITHIN the 3 s liveness wait
  parks on the first pass with the same record it always did; a transient
  start failure gets one more try. One that outlives the wait and exits
  after it does not: it reads `NRestarts=0`, is declared `recovered`, flaps
  through a fresh burst and re-enters `OnFailure=`, repeating the
  `fuser`/`lsof`/`/proc/asound` captures into the persistent journal roughly
  every 30 s for as long as it lasts. ADR-0175's `COOLDOWN_SEC=300` was the
  bound on that rate and is deliberately not replaced: no unit is stopped and
  nothing goes deaf, so the cost is journal churn only. Removal condition for
  that choice — re-add a rate bound if a journal shows this loop crowding out
  previous-boot forensics (`LogRateLimit*` on the recover unit is the cheap
  form). The wide ladder's tests, the park-units roster installed under
  `/usr/local/lib/jasper`, and the `outputd_restart_failed` reason are gone.
  Rejected: keeping the ladder behind a removal condition (nothing observable
  could re-arm it), and a reboot policy for camilla (a bad graph would
  reboot-loop the box).
