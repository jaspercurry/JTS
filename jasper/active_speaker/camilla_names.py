# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CamillaDSP filter names for active-speaker graphs."""

import re

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


def name_token(value: str) -> str:
    token = _SAFE_NAME_RE.sub("_", value).strip("_").lower()
    return token or "unnamed"


def driver_delay_name(role: str) -> str:
    return f"as_{name_token(role)}_delay"


def driver_limiter_name(role: str) -> str:
    return f"as_{name_token(role)}_startup_limiter"


def driver_baseline_gain_name(role: str) -> str:
    return f"as_{name_token(role)}_baseline_gain"


def driver_baseline_limiter_name(role: str) -> str:
    return f"as_{name_token(role)}_baseline_limiter"


def protective_tweeter_hp_name(role: str) -> str:
    return f"as_{name_token(role)}_protective_hp"


def baseline_protection_name(role: str, index: int, highpass: bool) -> str:
    return f"as_{name_token(role)}_declared_protection_{index}_{'hp' if highpass else 'lp'}"


def sub_lowpass_name() -> str:
    return "as_sub_lowpass"


def sub_baseline_gain_name() -> str:
    return "as_sub_baseline_gain"


def sub_baseline_limiter_name() -> str:
    return "as_sub_baseline_limiter"


def sub_startup_limiter_name() -> str:
    return "as_sub_startup_limiter"


def bass_management_hp_name(role: str) -> str:
    """The complementary mains bass-management high-pass on the lowest driver."""
    return f"as_{name_token(role)}_bass_mgmt_hp"


def driver_linearization_shelf_name(role: str) -> str:
    return f"as_{name_token(role)}_linearization_shelf"


def driver_linearization_peak_name(role: str, index: int) -> str:
    return f"as_{name_token(role)}_linearization_peak_{index}"


def driver_linearization_taper_name(role: str) -> str:
    # The CD-horn stage's optional TRAILING Highshelf taper. A distinct name
    # from the leading shelf so a Lowshelf-led backbone and its taper can
    # coexist in one chain without a duplicate filter name.
    return f"as_{name_token(role)}_linearization_taper"


def output_commission_mute_name(index: int) -> str:
    """The per-physical-output commission-mute filter name for ``index``.

    Public because the protected-staging software guard references these by
    index to prove a driver's output is muted: the emitter owns the spelling.
    """
    return f"as_out{index}_commission_mute"
