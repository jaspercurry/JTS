# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from jasper.cli import usbsink_main


def test_legacy_python_bridge_refuses_without_lab_flag(monkeypatch, capsys):
    monkeypatch.delenv(usbsink_main.LAB_ALLOW_ENV, raising=False)

    assert usbsink_main.main([]) == 64

    captured = capsys.readouterr()
    assert "legacy Python bridge" in captured.err
    assert "jasper-usbsink-audio" in captured.err
