# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Publish the resolved paths the interpreter-free reconcile condition hashes."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from jasper import env_load, output_hardware, usbgadget
from jasper.atomic_io import atomic_write_text
from jasper.install_profile import BUILD_MANIFEST_FILE

from . import config_txt, usb_port_role

if TYPE_CHECKING:
    from .reconcile import Pass


def publish_reconcile_inputs(run: Pass) -> None:
    # Observed paths are never written by a pass. The shim rechecks them before
    # stamping because udev coalesces a mid-pass hotplug into the running unit.
    entries = (
        ("observed", "JASPER_PROC_ASOUND", Path(run.proc_asound) / "cards"),
        ("observed", "JASPER_PROC_ASOUND", Path(run.proc_asound) / "pcm"),
        ("observed", "JASPER_PI_MODEL_FILE", run.model_path),
        ("observed", "JASPER_UDC_CLASS_DIR", run.udc_class_dir),
        ("input", "JASPER_ENV_FILE", run.env_file),
        ("input", "JASPER_OUTPUTD_ENV_FILE", run.outputd_env_file),
        ("input", "JASPER_FANIN_ENV_FILE", run.fanin_env_file),
        ("input", "JASPER_ASOUND_SOURCE_TEMPLATE", run.asound_source_template),
        ("input", "JASPER_ASOUND_TEMPLATE", run.asound_template),
        ("input", "JASPER_I2S_HAT_INTENT_FILE", run.i2s_hat_intent_file),
        ("input", "JASPER_I2S_HAT_REBOOT_REQUIRED_PATH", run.i2s_hat_reboot_required_path),
        ("input", "-", run.management_transport_marker),
        ("input", "JASPER_INSTALL_PROFILE_FILE", run.install_profile_file),
        ("input", "JASPER_OUTPUT_TOPOLOGY_PATH", run.output_topology_path),
        ("input", "JASPER_OUTPUT_HARDWARE_STATE_PATH", run.state_path),
        ("input", "JASPER_CAMILLA_STATEFILE", run.camilla_statefile),
        ("input", "JASPER_CAMILLA2_STATEFILE", run.camilla2_statefile),
        ("input", "JASPER_CAMILLA_CONF_DIR", run.camilla_conf_dir),
        ("input", "JTS_BOOT_CONFIG_FILE", run.boot_config_path),
        # build.txt is committed last by install. These loaded source files also
        # invalidate a stale list during an install or an uncommitted checkout.
        ("input", "-", BUILD_MANIFEST_FILE),
        ("input", "-", __file__),
        ("input", "-", Path(__file__).with_name("reconcile.py")),
        ("input", "-", output_hardware.__file__),
        ("input", "-", usb_port_role.__file__),
        ("input", "-", config_txt.__file__),
        ("input", "-", usbgadget.__file__),
        ("input", "-", env_load.__file__),
    )
    lines = ["JTS_RECONCILE_INPUTS_V1"]
    for kind, key, path in entries:
        resolved = str(Path(path).absolute())
        if any(char in resolved for char in "\t\r\n"):
            raise ValueError("reconcile input path contains a line separator")
        lines.append(f"{kind}\t{key}\t{resolved}")
    atomic_write_text(
        Path(run.state_path).parent / "reconcile.inputs",
        "\n".join(lines) + "\n",
        mode=0o644,
    )
