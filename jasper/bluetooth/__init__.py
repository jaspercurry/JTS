"""Generic Bluetooth control for JTS — adapter ops, scan, pair, connect.

Mirrors the iPhone Settings → Bluetooth model: live device list, pair
anything, no per-device-class wizards. Per-device behaviour comes from
pluggable handlers under `handlers/` — the engine itself knows nothing
about HID vs A2DP vs GATT, just drives the bluez state machine.

Module layout:
  - models.py       BluetoothDevice dataclass; one row in the scan list
  - adapter.py      adapter-level ops (Powered, Discoverable, scan)
  - agent.py        DBus org.bluez.Agent1 with DisplayYesNo capability;
                    surfaces passkey-confirmation prompts via futures
                    so the web layer can prompt the user
  - engine.py       pair state machine; async generator of status events
  - scan.py         live ObjectManager observer + event stream
  - roles.py        /var/lib/jasper/bt_roles.json — minimal persistence
                    of {mac: handler_id} so a re-connecting device gets
                    routed back to the right place
  - handlers/       per-class behaviour (HID / A2DP-sink / GATT / default)

The web layer (jasper.web.bluetooth_setup) is a thin HTTP+SSE shell on
top of this — all bluez talk happens here.
"""
