# ADR-0148: Every source-owned unit re-reads canonical intent at its own start boundary; derived enablement is never household preference

- **Date:** 2026-08-26
- **Status:** Superseded by
  [ADR-0221](0221-source-start-gates-are-marker-files-published-by-the-coordinator.md)
  (recorded when HANDOFF-source-lifecycle.md was trimmed to its operational
  spine)

## Context

The source coordinator is the normal lifecycle writer, but it is not the only
thing that can ask systemd to start a source unit. Boot, dependency pulls,
operator `systemctl` commands, room-correction cleanup, and the one-time
librespot OAuth claim can all issue a start or a restart. Unit enablement,
BlueZ `Powered`, RF-kill state and gadget shape are all *derived* from intent —
and every one of them can be stale, snapshotted, or hand-edited.

Treating any of those derived facts as the household's answer means a follower
can advertise AirPlay after an interrupted transition, or a maintenance restore
can resurrect a source the household turned off.

## Decision

**Every source-owned unit carries
`ExecCondition=jasper-local-source-allowed --source <id>`, which re-reads
canonical household intent and current grouping role immediately before
`ExecStart`. Derived enablement is a readiness mirror, never a preference.**

The `<id>` vocabulary is fixed by the local-source registry, and a contract
test derives every source resource (plus optional Bluetooth accessory adapter
services) from that registry, so a new declaration cannot ship without its
matching gate.

Corollaries the guard makes true:

- Malformed or unreadable intent **fails closed** and emits
  `event=local_sources.guard_intent_failed`. It never falls back to the shipped
  On default at this boundary.
- A maintenance snapshot (correction, the OAuth claim) only decides whether to
  *request* restoration; the gate decides whether it happens.
- Desired-On with a failed or stale disabled mirror **suppresses** the source
  rather than advertising a function with no ready consumer.
- Units do not order themselves after or require the coordinator — the
  coordinator starts them, so such an edge would deadlock. The gate reads the
  atomic intent file directly and is therefore safe at boot.

## Consequences

- **Off and follower parking dominate every other input.** Stale unit
  enablement, a hand-run `systemctl start`, or a dependency pull cleanly skips
  the start instead of overriding the household.
- **The gate is a privileged boundary, so it is written like one.** The intent
  file stays `root:jasper 0660` under a non-world-traversable state directory;
  renderer users are not in the writer group; each guard crosses with a fixed
  `/usr/bin/env -i` argv and fixed `PATH`, and its unit unsets native-loader
  injection variables (`LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`,
  `GLIBC_TUNABLES`) before `/usr/bin/env` starts. Python sees no
  renderer-controlled environment while root reads the fixed file.
- **Shared infrastructure keeps a weaker gate on purpose.** `jasper-mux.service`
  has no single source intent, so it keeps the generic role-only guard;
  `jasper-usbgadget.service` cannot carry a whole-unit source gate because its
  NCM management network must survive USB Audio Off and follower parking —
  its wanted/up helpers ask the same `--source usbsink` guard instead.
- **A capability can also veto.** The USB guard additionally requires
  `usb_data_role.gadget_available`, so a manual start cannot bypass output
  ownership of a Zero's shared OTG port.
- **The cost is a re-read per start.** Every source start pays a small file
  read and a role lookup. That is the price of never having to trust a derived
  fact, and it is paid on a path that runs seconds apart at most.
