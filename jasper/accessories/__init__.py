# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Supported third-party HID accessories (volume knobs, remotes, etc.).

Devices connect over USB-HID or Bluetooth-HID and surface to Linux as
``/dev/input/event*`` nodes. The bridge translates their key events into
HTTP calls against jasper-control; profile-specific adapters may additionally
publish a manually selected microphone source.
"""
