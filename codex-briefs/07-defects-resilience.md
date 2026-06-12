# Brief 07 — Resilience defects (audio + supervisors)

Mission: review §5.5 items 2-6. Five confirmed availability defects. Repo rule
applies hard here: **diagnose before solving** — reproduce each claim against
*current* main first; the fanin/outputd TTS layer was refactored on 2026-06-12
(new `rust/jasper-tts-protocol` crate, new `rust/jasper-outputd/src/tts.rs`),
so the rust findings' line anchors have certainly moved and one may be
obsolete.

Branch: `codex/resilience-defects`. File fence: `jasper/control/server.py`
(supervisor wiring only), `jasper/control/system_supervisor.py`,
`jasper/control/shairport_supervisor.py`, `jasper/control/mpris.py`,
`rust/jasper-fanin/**`, `rust/jasper-outputd/**`,
`deploy/systemd/jasper-camilla.service`, `deploy/bin/**` (only if the camilla
gate needs a helper script), `jasper/voice_daemon.py` is OUT of fence
(wave 2 owns it) — if a fix seems to need it, document the voice-side half in
the PR for wave 2 instead. One PR per defect.

## PR 1 — stuck music duck when voice dies mid-TTS

Finding (against 6772b81a): fanin's TTS client handler returned on EOF without
releasing `program_duck`, and a freshly restarted voice daemon's
`FanInDucker.restore()` no-ops (`_ducked=False`), so music stays ducked until
reboot. Re-diagnose on current code: the handler now lives in/under the
jasper-tts-protocol refactor — find where a client disconnect is handled and
whether duck state is cleared. Fix on the Rust side (duck release on
disconnect of the duck-holding client) — that heals regardless of how voice
died. Also expose current duck state in the fanin `/state` JSON so the
dashboard can show it. Add a Rust test: connect, DUCK_ON, drop the socket,
assert mixer duck released + a structured `event=fanin.tts_duck_release
reason=disconnect` log. Check `tests/test_wire_contracts.py` if you add a
state key.

## PR 2 — shairport supervisor can't recover a dead shairport

`jasper/control/shairport_supervisor.py` `_tick`: `is_session_active`
fail-safes to True when `mpris.shairport_playing` returns None — and busctl
returns non-zero precisely when the process is dead, so the restart path is
suppressed in exactly the state it exists for. Fix: before fail-safing to
"active", check `systemctl is-active shairport-sync` (inject the systemctl
runner the way the reconciler tests do); a dead/failed unit bypasses the MPRIS
gate and counts failures normally. Also apply the review's counter note: keep
counting consecutive probe failures while a session suppresses the *restart
action*, so recovery after a long playback session isn't delayed by a reset
counter. Extend `tests/test_control_server.py`'s supervisor tests (or the
dedicated supervisor test file) for: dead unit → restart proceeds; live
session → restart still suppressed.

## PR 3 — yanked DAC costs up to two full system reboots

`deploy/systemd/jasper-camilla.service` has StartLimitBurst + reboot
escalation, and the Apple dongle drops its UAC interface without an analog
jack load — so a yanked 3.5mm plug walks the unit into reboot escalation
(~2 reboots) before jasper-bootloop-guard parks it. Fix with the pattern the
repo already ships in outputd: a device-presence gate that parks *cleanly*
instead of crash-looping — either `ExecStartPre=` presence check exiting a
code listed in `RestartPreventExitStatus=`, or an `ExecCondition=` (mirror how
jasper-aec-bridge gates on `--check-aec-ready`). The udev dongle-recover path
must still restart it when the DAC returns — read
`deploy/udev/99-jasper-apple-dongle.rules` + `jasper-dongle-recover.service`
and keep that chain working. Pin with the deploy-wiring guard style
(`tests/test_outputd_wiring.py` / `tests/test_deploy_wiring_guards.py`).
**Flag the PR needs-on-device-validation** (real unplug/replug test is
hardware).

## PR 4 — SystemSupervisor reboots healthy sshd-less speakers daily

`jasper/control/system_supervisor.py` hardcodes the sshd banner probe
(port 22); on an install with sshd disabled/masked, that probe alone trips the
3-failure threshold → clean reboot every 24h. Fix: at supervisor start (and
cheaply re-checked, e.g. hourly), skip the sshd probe when
`systemctl is-enabled ssh`/`sshd` reports disabled/masked/not-found, logging
one `event=system_supervisor.sshd_probe_skipped reason=...` line; optionally
honor `JASPER_SYSTEM_SUPERVISOR_SSHD_PORT=0` as an explicit off-switch.
Document in the module docstring. Tests: disabled sshd → probe skipped, other
probes still trip; enabled sshd → unchanged behavior.

## PR 5 — fanin epoch gate drops in-flight control commands

Finding: the flush epoch gate silently dropped any stale-epoch command, with
only `ProgramDuckOff` exempt — a flush racing `CONTENT_METER_RESUME` left the
content meter paused (same one-way-trap shape as the already-fixed duck-off
bug, which has regression tests to mirror). Re-diagnose post-refactor: find
the epoch gate in the current tts code (fanin or the shared
jasper-tts-protocol crate). Fix: exempt state-restoring control commands (or
all non-Audio commands) from the epoch drop, and log/count stale drops so the
next one is visible. Mirror the existing
`stale_program_duck_on_does_not_relatch_after_flush`-style tests for
ContentMeterResume.

## Acceptance

- `cargo test --locked` green in the touched crates (CI builds them; run
  locally if cargo is available, else say so);
  `pytest tests/test_control_server.py tests/test_outputd_wiring.py
  tests/test_wire_contracts.py -q` green; `ruff check .` clean.
- Each PR body: the diagnosis (file:line on current main), the failure mode in
  one sentence, and the test that now pins it.
