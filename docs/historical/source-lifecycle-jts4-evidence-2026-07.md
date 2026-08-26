# Source-lifecycle hardware evidence — JTS4, 2026-07 — historical

> **Status: historical.** Frozen record of the two dated JTS4 validation
> passes that certified the local-source lifecycle: the pre-role-reboot
> Bluetooth pass (2026-07-14) and the post-role-reboot USB/output pass
> (2026-07-15). Kept because both passes cost real hardware time and because
> the gaps they left open (UAC2 positive gadget mode, physical pairing) are
> still the gaps. **Nothing here is current operational truth** — the live
> spine is [HANDOFF-source-lifecycle.md](../HANDOFF-source-lifecycle.md), and
> the decisions are ADR-0147, ADR-0148 and ADR-0149.

## Bluetooth pass — JTS4, 2026-07-14 (pre-role reboot)

The streambox-profile JTS4 passed the Bluetooth portion of the validation
checklist through the real CSRF-protected `/sources/set`, `/bluetooth/power`,
and `/bluetooth/scan` HTTP surfaces. Off produced matching desired/effective
state on both pages, left `bluetooth.service` active, disabled and stopped all
three resource units, set BlueZ `Powered: no`, and soft-blocked `hci0`. Those
states survived reboot. The first boot pass hit the prior AirPlay client
timeout; an explicit supported reconcile retry then converged all four sources
without resurrecting Bluetooth. The timeout contract has since been widened and
pinned to AirPlay's service plus required NQPTP transaction, but that exact boot
path still needs a final JTS4 replay.

On restored RF-kill, `Powered: yes`, all resource units, and a successful scan
start/stop. The shared intent and three lock files landed `root:jasper 0660`,
and both web service owners wrote successfully. Supported deploys in both On and
Off states preserved the saved intent; the low-memory probe certified the
Bluetooth radio and units in both states.

At that pre-role-reboot snapshot JTS4 had no observable configured output DAC,
so its unrelated outputd fake-backend advisory remained. No physical pairing
target or USB UAC2 host was available; pairing and USB re-enumeration remained
explicit gaps at that point.

## USB/output pass — JTS4, 2026-07-15 (post-role reboot)

After the role reboot, JTS4 (Zero 2 W) resolved an active `host` data role and
detected its Apple USB-C output DAC. The output-hardware artifact reported the
registered Apple profile ready, outputd used the ALSA backend, Bluetooth was
enabled, USB Audio Input was intentionally unavailable, and strict deploy health
completed with 0 failures / 0 warnings.

This validates the Zero USB-output negative gadget path and output recovery; it
does **not** validate UAC2 or positive gadget mode, which still requires a
registered I²S-output Zero or a board with separate host ports.
