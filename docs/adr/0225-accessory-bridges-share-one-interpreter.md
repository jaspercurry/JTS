# ADR-0225: Accessory bridges share one interpreter

- **Date:** 2026-09-03
- **Status:** Accepted
- **Context:** `jasper-input` (evdev HID buttons → jasper-control) and
  `jasper-wiim-remote-mic` (BLE GATT voice report → UDP PCM) were two resident
  Python daemons with an identical lifecycle: same `User=`/`Group=`, no slice,
  `Type=simple`, no RT or nice, same `OOMScoreAdjust=`, neither holding an ALSA
  device, both asyncio, both started and stopped by the same reconciler for the
  same reason (an accessory is paired). On the smallest supported target — a Pi
  Zero 2 W with 415 MB — the mic bridge measured ~22 MB / ~19 MB resident, of
  which the CPython base is pure duplicate cost. Residents audit #14, report M1.
- **Decision:** One process runs both. `jasper-input` starts the HID bridge
  unconditionally and, alongside it, the accessory mic adapter task for each
  source `jasper-accessory-reconcile` publishes in
  `/var/lib/jasper/accessory-mics.env`. `jasper.accessories.supervisor` gives
  each bridge its own restart loop, so **a fault in one bridge never stops the
  other and never exits the process** — the HID path carries volume and
  push-to-talk, and a BLE-stack fault must not silence them. The reconciler
  keeps writing the same env file, and its only systemd action for the mic half
  is `try-restart` on the host when the published set changes: never `enable`,
  `disable` or `stop`, which would take the HID bridge down with the adapter.
  `jasper-wiim-remote-ce.service` stays a separate root oneshot — different
  user and `CAP_NET_RAW`, so it cannot fold in.
- **Consequences:** One interpreter base instead of two, and a box with no
  accessory mic paired never imports `dbus-next` or the ADPCM decoder at all.
  The merged unit takes the mic unit's loader-env scrub, its
  `ProtectClock`/`ProtectHostname`/`RestrictRealtime`/`RemoveIPC`/`UMask=0077`,
  and its `IPAddressDeny=any` + `IPAddressAllow=localhost` egress lockdown. It
  cannot take `PrivateDevices=true` (that would hide `/dev/input/event*`), and
  `ProtectProc=invisible`/`ProcSubset=pid`/`MemoryDenyWriteExecute=true` are
  deliberately left off until they are verified against the evdev/pyudev
  (ctypes) path on hardware — tightening them blind risks the button path. The
  `bluetooth` group the adapter needs is likewise NOT a `SupplementaryGroups=`
  directive: systemd fails a unit `216/GROUP` on a group name it cannot
  resolve, and bluez is installed after the service users are created, so the
  grant rides on the service user's own membership instead. The mic unit's
  `ConditionPathExists=` gate on the Bluetooth source-intent marker is enforced
  upstream now, by the reconciler declining to publish a source while Bluetooth
  intent is off; the published file is the single accessory verdict and the
  host deliberately does not re-derive it. That file is durable where the
  `/run` marker it replaces was volatile, so at boot the adapter acts on the
  previous boot's verdict until the reconciler's first pass lands. Costs: pairing or forgetting a
  mic-bearing remote restarts the HID bridge for ~1 s, where before it
  restarted only the mic daemon; a bridge that can never start now retries
  behind an exponential backoff with the unit still `active`, instead of the
  unit going `failed`, so the journal's `event=accessory.bridge_failed
  attempt=` line is the only surface for that state; and the teardown ordering
  that stopped the producer before withdrawing its source is gone, because the
  published file is now the only handle on the adapter. What reverses this: a
  second accessory bridge that genuinely needs a different `User=`, capability
  set, slice or scheduling class — that one gets its own unit rather than being
  forced under this roof.
