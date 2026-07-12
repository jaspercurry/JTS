# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Generic Bluetooth control for JTS — adapter ops, scan, pair, connect.

Mirrors the iPhone Settings → Bluetooth model: live device list, pair
anything, no per-device-class wizards. Per-device behaviour comes from
pluggable handlers under `handlers/` — the engine itself knows nothing
about HID vs A2DP vs GATT, just drives the bluez state machine.

Module layout:
  - models.py       BluetoothDevice dataclass; one row in the scan list
  - adapter.py      short-lived adapter ops (Powered, pairing window, removal)
  - agent.py        DBus org.bluez.Agent1 with NoInputNoOutput capability;
                    accepts no-code pairing and rejects code-based flows
  - no_code_agent.py
                    systemd entrypoint for the always-on default agent
  - engine.py       long-lived discovery owner, pair state machine, status events
  - scan.py         live ObjectManager observer + event stream
  - roles.py        /var/lib/jasper/bt_roles.json — minimal persistence
                    of {mac: handler_id} so a re-connecting device gets
                    routed back to the right place
  - avrcp.py        AVRCP transport controls (play/pause/next/etc.) for
                    connected A2DP-sink devices
  - handlers/       per-class behaviour (HID / A2DP-sink / default —
                    GATT-only peripherals fall through to default)

The web layer (jasper.web.bluetooth_setup) is a thin HTTP+SSE shell on
top of this — all bluez talk happens here.
"""
