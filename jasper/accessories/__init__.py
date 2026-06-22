# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Third-party HID accessories (volume knobs, macro pads, etc.).

This package owns Pi-side integration of "dumb" accessories — devices
whose firmware we don't control, which talk to the Pi over USB-HID or
Bluetooth-HID. They surface to Linux as `/dev/input/event*` nodes; we
translate their key events into HTTP calls against jasper-control.

For "smart" accessories (the ESP32-based rotary dial and AMOLED
satellite, whose firmware lives in `firmware/`), the integration is
direct HTTP from the device to jasper-control — see those modules
instead. They share the user-facing concept "an accessory" but not
the internal transport.
"""
