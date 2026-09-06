# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared boot-config fixture paths and text for the USB port-role tests."""

from __future__ import annotations

from pathlib import Path

PI5 = "Raspberry Pi 5 Model B Rev 1.0"
PERIPHERAL = "[all]\ndtoverlay=dwc2,dr_mode=peripheral\n"
HAND_WRITTEN_BASE = "[all]\ndtoverlay=hifiberry-dac8x\ndtparam=audio=on\n"


def boot_paths(
    tmp_path: Path, *, model_text: str = PI5, boot_config: str = PERIPHERAL
):
    model, config, intent, hat, udc = (
        tmp_path / name
        for name in ("model", "config.txt", "i2s_hat.env", "hat", "udc")
    )
    model.write_text(model_text, encoding="utf-8")
    config.write_text(boot_config, encoding="utf-8")
    udc.mkdir()
    return model, config, intent, hat, udc
