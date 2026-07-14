# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.usbgadget import udc_host_connected


def test_udc_host_connected_reads_any_configured_controller(tmp_path):
    first = tmp_path / "fe980000.usb"
    first.mkdir()
    (first / "state").write_text("not attached\n")
    second = tmp_path / "1000480000.usb"
    second.mkdir()
    (second / "state").write_text("configured\n")

    assert udc_host_connected(tmp_path) is True


def test_udc_host_connected_fails_soft_for_missing_or_unreadable_state(tmp_path):
    (tmp_path / "fe980000.usb").mkdir()

    assert udc_host_connected(tmp_path) is False
    assert udc_host_connected(tmp_path / "missing") is False
