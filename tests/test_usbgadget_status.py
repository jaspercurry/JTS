# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.usbgadget import network_wanted, udc_host_connected


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


@pytest.mark.parametrize(
    "value, wanted",
    [
        (None, True),
        ("enabled", True),
        ("", True),
        ("disabled", False),
        ("DISABLED", False),
        ("Disabled", False),
        ("disabled-typo", True),
        ("off", True),
        # Whitespace-decorated near-misses stay ENABLED: the shell truth table
        # compares the raw value, and a stray space must never silently drop
        # the fallback network.
        (" disabled", True),
        ("disabled ", True),
    ],
    ids=[
        "unset", "enabled", "empty", "disabled", "upper", "mixed-case",
        "typo", "off", "leading-space", "trailing-space",
    ],
)
def test_network_wanted_reads_the_exact_literal_kill_switch(
    monkeypatch, value, wanted
):
    monkeypatch.delenv("JASPER_USB_NETWORK", raising=False)
    if value is not None:
        monkeypatch.setenv("JASPER_USB_NETWORK", value)

    assert network_wanted() is wanted
