# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from jasper.audio_hardware.config_txt import MANAGED_BLOCK_BEGIN, render_boot_config
from jasper.audio_hardware.i2s_hat import configured_i2s_overlays


def test_i2s_overlay_parser_ignores_comments_and_non_applicable_sections() -> None:
    content = """\
# dtoverlay=hifiberry-dac8x
[cm5]
dtoverlay=hifiberry-dac8x
[all]
   dtoverlay = hifiberry-dac8x   # configured output
"""

    assert configured_i2s_overlays(content) == ("hifiberry-dac8x",)


def test_studio_dac8x_overlay_is_recognized_as_a_registered_i2s_hat() -> None:
    """A Studio-configured box must not read as "no I2S HAT present" (#2250).

    This parser intersects config.txt against the `dtoverlay` each registered
    profile declares, and USB port-role resolution consumes the result. While
    the Studio profile declared the BASE board's `hifiberry-dac8x`, a box
    correctly running the Studio's own overlay matched nothing here and looked
    like a speaker with no audio HAT at all.
    """
    content = "[all]\ndtoverlay=hifiberry-studio-dac8x\n"

    assert configured_i2s_overlays(content) == ("hifiberry-studio-dac8x",)
    # The two boards' overlays are distinct entries, not one shared string.
    assert configured_i2s_overlays(
        "[all]\ndtoverlay=hifiberry-dac8x\ndtoverlay=hifiberry-studio-dac8x\n"
    ) == ("hifiberry-dac8x", "hifiberry-studio-dac8x")
    # The PRO's overlay is deliberately NOT registered: no Pro profile exists,
    # and inventing one for hardware nobody owns is what #2250 warns against.
    assert configured_i2s_overlays(
        "[all]\ndtoverlay=hifiberry-studio-dac8x-pro\n"
    ) == ()


def _has_adjacent_empty_all_sections(text: str) -> bool:
    """True if two ``[all]`` headers appear with only blank lines between."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() != "[all]":
            continue
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor < len(lines) and lines[cursor].strip().lower() == "[all]":
            return True
    return False


@pytest.mark.parametrize(
    "boot_config",
    [
        pytest.param("[cm4]\notg_mode=1\n\n[all]\nfoo=1\n", id="clean"),
        pytest.param(
            "[cm4]\notg_mode=1\n\n" + ("[all]\n\n" * 7) + "[all]\nfoo=1\n",
            id="stray_all_sections",
        ),
    ],
)
def test_render_boot_config_heals_stray_all_sections_and_is_idempotent(
    boot_config: str,
) -> None:
    once = render_boot_config(boot_config, "host")
    twice = render_boot_config(once, "host")

    assert twice == once
    assert once.count(MANAGED_BLOCK_BEGIN) == 1
    assert not _has_adjacent_empty_all_sections(once)


def test_render_boot_config_never_drops_a_commented_all_header() -> None:
    boot_config = "[cm4]\notg_mode=1\n\n[all]  # keep me\n\n[all]\nfoo=1\n"

    rendered = render_boot_config(boot_config, "host")

    assert "[all]  # keep me" in rendered


def test_render_boot_config_migrates_legacy_role_and_is_idempotent() -> None:
    legacy = """\
arm_64bit=1

# JTS install — required for the composite USB gadget (management network +
# optional audio). Old installer prose.
[all]
dtoverlay=dwc2,dr_mode=peripheral
"""

    rendered = render_boot_config(legacy, "host")

    assert "arm_64bit=1" in rendered
    assert rendered.count("dtoverlay=dwc2,dr_mode=host") == 1
    assert "dtoverlay=dwc2,dr_mode=peripheral" not in rendered
    assert rendered.count(MANAGED_BLOCK_BEGIN) == 1
    assert render_boot_config(rendered, "host") == rendered


def test_legacy_migration_never_consumes_intervening_hardware_directives() -> None:
    legacy = """\
# JTS install — required for the composite USB gadget (management network +
# optional audio). Old installer prose.
[all]
dtparam=i2c_arm=on
dtoverlay=hifiberry-dac8x
dtoverlay=dwc2,dr_mode=peripheral
"""

    rendered = render_boot_config(legacy, "host")

    assert "dtparam=i2c_arm=on" in rendered
    assert "dtoverlay=hifiberry-dac8x" in rendered
    assert rendered.count("dtoverlay=dwc2,dr_mode=host") == 1


def test_bare_dwc2_is_migrated_but_unknown_parameters_fail_loudly() -> None:
    rendered = render_boot_config("[all]\ndtoverlay=dwc2\nfoo=1\n", "host")
    assert rendered.count("dtoverlay=dwc2,dr_mode=host") == 1
    assert "\ndtoverlay=dwc2\n" not in rendered
    assert "foo=1" in rendered

    with pytest.raises(ValueError, match="ambiguous.*dwc2"):
        render_boot_config(
            "[all]\ndtoverlay=dwc2,dr_mode=peripheral,g-rx-fifo-size=512\n",
            "host",
        )
