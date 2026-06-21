# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Guard against drift between the bash AEC reconciler and its Python SSOT.

deploy/bin/jasper-aec-reconcile is bash and cannot import Python, so it
re-hardcodes two sets of constants that actually live in Python:

  * the leg-to-UDP-port map owned by jasper.wake_legs (re-exported via
    jasper.wake_ports), as ``${JASPER_AEC_UDP_PORT*:-NNNN}`` fallbacks; and
  * the XVF3800 capture-mixer control names + max volume + channel count
    owned by jasper.mics.xvf3800, in ``ensure_capture_mixer_open``.

Both source files say in prose "this reconciler is bash and can't import
Python; if the constants change in the profile, update them here too." If
Python changes a port and the bash does not, the reconciler points
jasper-voice at the wrong UDP port (or opens the wrong mixer control) and
wake silently fails while every other unit test stays green. This test is
that missing CI guard: it reads the Python values, parses the hardcoded
fallbacks out of the script text, and fails — naming the exact drifted
constant and BOTH values — when they diverge.

Guard only: it does not run the reconciler. The behavioural harness is
tests/test_aec_reconcile.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper import wake_legs
from jasper.mics import xvf3800

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-aec-reconcile"
SCRIPT_TEXT = SCRIPT.read_text()


def _hardcoded_port(env_var: str) -> int:
    """Parse the ``${env_var:-NNNN}`` default out of the reconciler text.

    Each port is assigned exactly once as ``KEY="${ENVVAR:-NNNN}"``; the
    env-var name is the unambiguous anchor (the assigned-to KEY differs
    from it). Asserts a unique match so a refactor that drops or
    duplicates the line fails loudly here rather than silently parsing
    the wrong number.
    """
    matches = re.findall(
        r"\$\{" + re.escape(env_var) + r":-(\d+)\}",
        SCRIPT_TEXT,
    )
    assert len(matches) == 1, (
        f"expected exactly one '${{{env_var}:-<port>}}' default in "
        f"{SCRIPT.name}, found {len(matches)}: {matches}"
    )
    return int(matches[0])


# bash env-var carrying the fallback -> jasper.wake_legs token that owns
# the canonical port. The names intentionally do NOT line up one-to-one:
# the chip-direct / AEC-OFF leg (token "off") is carried by the bash var
# JASPER_AEC_UDP_PORT_RAW, and the primary AEC3 leg (token "on") by the
# bare JASPER_AEC_UDP_PORT. Encoding the crossed mapping explicitly is
# the point of the guard.
_PORT_ENV_TO_TOKEN = {
    "JASPER_AEC_UDP_PORT": "on",
    "JASPER_AEC_UDP_PORT_RAW": "off",
    "JASPER_AEC_UDP_PORT_DTLN": "dtln",
    "JASPER_AEC_UDP_PORT_CHIP_AEC_150": "chip_aec_150",
    "JASPER_AEC_UDP_PORT_CHIP_AEC_210": "chip_aec_210",
}


@pytest.mark.parametrize(("env_var", "token"), sorted(_PORT_ENV_TO_TOKEN.items()))
def test_reconciler_udp_port_matches_wake_legs(env_var: str, token: str) -> None:
    python_port = wake_legs.by_token(token).udp_port
    bash_port = _hardcoded_port(env_var)
    assert bash_port == python_port, (
        f"UDP port drift: {SCRIPT.name} hardcodes ${{{env_var}:-{bash_port}}} "
        f"but jasper.wake_legs.by_token({token!r}).udp_port = {python_port}. "
        f"Update the reconciler fallback to {python_port}."
    )


def _amixer_cset_args(control_name: str) -> str:
    """Return the comma-list argument the reconciler passes to
    ``amixer ... cset name='<control_name>'`` (the line continues over a
    ``\\``-newline, so match across it)."""
    m = re.search(
        r"cset name='" + re.escape(control_name) + r"'\s*\\\s*\n\s*([0-9a-z,]+)",
        SCRIPT_TEXT,
    )
    assert m is not None, (
        f"could not find the amixer cset for '{control_name}' in {SCRIPT.name}"
    )
    return m.group(1)


def test_reconciler_mixer_control_names_match_profile() -> None:
    """The two capture-mixer control names the reconciler opens must be
    the exact strings jasper.mics.xvf3800 owns."""
    for control in (xvf3800.MIXER_CAPTURE_SWITCH, xvf3800.MIXER_CAPTURE_VOLUME):
        assert f"name='{control}'" in SCRIPT_TEXT, (
            f"mixer control drift: jasper.mics.xvf3800 names {control!r} but "
            f"{SCRIPT.name} has no `amixer ... cset name='{control}'`. "
            f"Update ensure_capture_mixer_open."
        )


def test_reconciler_mixer_volume_matches_profile() -> None:
    """The volume the reconciler writes to every capture channel must be
    MIXER_VOLUME_MAX (0 dB), once per recommended-firmware channel."""
    channels = xvf3800.RECOMMENDED_FIRMWARE.capture_channels
    expected = ",".join([str(xvf3800.MIXER_VOLUME_MAX)] * channels)
    actual = _amixer_cset_args(xvf3800.MIXER_CAPTURE_VOLUME)
    assert actual == expected, (
        f"mixer volume drift: jasper.mics.xvf3800 sets "
        f"MIXER_VOLUME_MAX={xvf3800.MIXER_VOLUME_MAX} across "
        f"capture_channels={channels} -> {expected!r}, but {SCRIPT.name} "
        f"writes {actual!r}. Update ensure_capture_mixer_open."
    )


def test_reconciler_mixer_switch_channel_count_matches_profile() -> None:
    """The capture switch is opened once per recommended-firmware channel
    (``on,on,...``); the count must track capture_channels so a firmware
    channel-count change can't leave channels muted."""
    channels = xvf3800.RECOMMENDED_FIRMWARE.capture_channels
    expected = ",".join(["on"] * channels)
    actual = _amixer_cset_args(xvf3800.MIXER_CAPTURE_SWITCH)
    assert actual == expected, (
        f"mixer switch channel-count drift: jasper.mics.xvf3800 "
        f"capture_channels={channels} -> {expected!r}, but {SCRIPT.name} "
        f"writes {actual!r}. Update ensure_capture_mixer_open."
    )
