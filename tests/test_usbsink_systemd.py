# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-usbsink.service contract: it MUST ride the zram-shielded audio tier.

usbsink is a real-time music SOURCE — its production Rust bridge runs
capture/playback on fixed ALSA periods. Diagnosed on 2026-06-28: usbsink was
the only music-path daemon left OUTSIDE ``jts-audio.slice``, so its pages could
swap to zram and zram-decompression jitter made the audio period miss deadlines
in bursts -> snd-aloop xruns -> drops. Slice membership (the
``MemorySwapMax=0`` swap shield) is a MEMORY policy, not a CPU cap, so it
respects the no-CPU-caps rule.

This pins the membership + the OOM band so a future unit edit can't silently drop
usbsink off the audio tier (the regression that caused the drops).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UNIT_PATH = REPO / "deploy" / "systemd" / "jasper-usbsink.service"
PYPROJECT_PATH = REPO / "pyproject.toml"


def _value_for(unit_text: str, key: str) -> str | None:
    """Last value for ``key=`` in the unit's [Service] section (last wins, as
    systemd resolves it). Ignores comment lines."""
    val: str | None = None
    for ln in unit_text.splitlines():
        s = ln.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip() == key:
            val = v.strip()
    return val


def _line_index(unit_text: str, needle: str) -> int:
    for idx, line in enumerate(unit_text.splitlines()):
        if needle in line:
            return idx
    raise AssertionError(f"{needle!r} not found in unit")


def test_unit_file_exists():
    assert UNIT_PATH.exists(), f"jasper-usbsink.service missing at {UNIT_PATH}"


def test_rides_zram_shielded_audio_slice():
    # The swap shield is the load-bearing fix for the bursty USB-drop tail.
    body = UNIT_PATH.read_text()
    assert _value_for(body, "Slice") == "jts-audio.slice", (
        "jasper-usbsink.service must set Slice=jts-audio.slice (MemorySwapMax=0 "
        "swap shield) — off it, zram jitter makes the RT callback miss deadlines "
        "and the queue overflows. See the 2026-06-28 USB-drop diagnosis."
    )


def test_oom_band_below_the_output_chain():
    # A killed source restarts and only USB stops; a killed output (outputd -950
    # / camilla -900 / fanin -800) stops all audio — so usbsink sits ABOVE them
    # (less negative = less protected), but still protected vs the default 0.
    body = UNIT_PATH.read_text()
    raw = _value_for(body, "OOMScoreAdjust")
    assert raw is not None, "jasper-usbsink.service must set OOMScoreAdjust"
    val = int(raw)
    assert -800 < val < 0, (
        f"usbsink OOMScoreAdjust={val} must be negative (protected) but above the "
        f"output chain's -800 (fanin) — a music source is less critical than output."
    )


def test_no_unit_level_rt_scheduling():
    # Unit-level CPUSchedulingPolicy=fifo is what SIGKILL-crash-looped the AEC
    # bridge on 2026-06-27; RT priority, if ever needed, is elected in-thread
    # after start(). Guard against it regressing into this unit.
    body = UNIT_PATH.read_text()
    assert _value_for(body, "CPUSchedulingPolicy") is None, (
        "Do NOT set CPUSchedulingPolicy at the unit level (AEC-bridge crash-loop "
        "2026-06-27). Elect RT in-thread after start() if ever required."
    )


def test_rust_audio_bridge_is_the_service_execstart():
    body = UNIT_PATH.read_text()
    assert _value_for(body, "ExecStart") == "/opt/jasper/bin/jasper-usbsink-audio"
    assert "jasper-usbsink-volume.service" in (_value_for(body, "Wants") or "")


def test_python_usb_bridge_has_only_explicit_lab_entrypoint():
    data = tomllib.loads(PYPROJECT_PATH.read_text())
    scripts = data["project"]["scripts"]

    assert scripts.get("jasper-usbsink-python-lab") == "jasper.cli.usbsink_main:main"
    assert "jasper-usbsink" not in scripts


def test_packaged_defaults_do_not_override_operator_or_generated_env():
    body = UNIT_PATH.read_text()

    default_idx = _line_index(body, "JASPER_USBSINK_CAPTURE_DEVICE=hw:UAC2Gadget")
    base_env_idx = _line_index(body, "EnvironmentFile=-/etc/jasper/jasper.env")
    generated_env_idx = _line_index(
        body,
        "EnvironmentFile=-/var/lib/jasper/usbsink.env",
    )

    assert default_idx < base_env_idx < generated_env_idx


# ----------------------------------------------------------------------
# Stage 1 host-slaved USB clock (default-OFF): the pitch-neutrality safety
# invariant needs a belt-and-braces ExecStopPost= reset, because the in-
# process reset (on clean exit / SIGTERM / demotion / disable) cannot run
# when the daemon is SIGKILLed, OOM-killed, or watchdog-aborted. A host must
# never stay slaved to a pitch command from a daemon that is no longer
# running. See docs/HANDOFF-usb-low-latency.md "Host-slaved USB clock".
# ----------------------------------------------------------------------


def _exec_stop_post_lines(unit_text: str) -> list[str]:
    """Every ``ExecStopPost=`` line's value (multiple are valid systemd
    syntax — unlike Environment=/ExecStart=, they do NOT "last wins"; all
    of them run in sequence on stop). Ignores comments."""
    values: list[str] = []
    for ln in unit_text.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        if s.startswith("ExecStopPost="):
            values.append(s[len("ExecStopPost="):])
    return values


def test_execstoppost_resets_pitch_to_neutral():
    body = UNIT_PATH.read_text()
    matches = [
        v for v in _exec_stop_post_lines(body)
        if "Capture Pitch 1000000" in v
    ]
    assert matches, (
        "jasper-usbsink.service must carry an ExecStopPost= that resets the "
        "gadget's 'Capture Pitch 1000000' ctl to neutral — belt-and-braces "
        "for the pitch-neutrality safety invariant when the daemon is "
        "SIGKILLed/OOM-killed and cannot run its own in-process reset."
    )
    line = matches[0]

    # `-` prefix: ignore failure when the card is absent (feature disabled,
    # gadget torn down, or a different capture device) — a missing card
    # must not fail the stop/restart of the unit itself.
    assert line.startswith("-"), (
        f"ExecStopPost pitch reset must be prefixed with '-' to ignore "
        f"failure when the UAC2Gadget card is absent; got: {line!r}"
    )

    # Targets the exact ctl this Stage 1 module writes: iface=PCM,
    # name='Capture Pitch 1000000' (verified live on jts.local kernel
    # 6.12.75: iface=PCM numid=1, range 750000..1005000).
    assert "iface=PCM" in line, (
        f"ExecStopPost pitch reset must target iface=PCM; got: {line!r}"
    )
    assert "name='Capture Pitch 1000000'" in line, (
        f"ExecStopPost pitch reset must target the 'Capture Pitch 1000000' "
        f"control by name; got: {line!r}"
    )

    # Resets to the neutral value (1000000 = unity, no pitch bias) — the
    # trailing numeric argument to `amixer cset`, not the "1000000" that is
    # part of the control's own NAME.
    trailing_value = line.rsplit(None, 1)[-1]
    assert trailing_value == "1000000", (
        f"ExecStopPost pitch reset must write the neutral value 1000000, "
        f"not a stale/non-neutral bias; got trailing value {trailing_value!r} "
        f"in line: {line!r}"
    )

    # Card is selected by expanding ${JASPER_USBSINK_MIXER_CARD} rather than a
    # hardcoded literal, so an operator overriding that card in an
    # EnvironmentFile also redirects this belt-and-braces neutralize (review
    # N3 — no hardcoded card drift between the daemon and the stop line).
    assert "-c ${JASPER_USBSINK_MIXER_CARD}" in line, (
        f"ExecStopPost pitch reset must target -c ${{JASPER_USBSINK_MIXER_CARD}} "
        f"(systemd-expanded), not a hardcoded card literal; got: {line!r}"
    )
    # And the unit must declare a packaged default for that variable, or the
    # expansion would resolve to empty and amixer would fail (harmless with the
    # `-` prefix, but then the neutralize silently never runs). Default is
    # UAC2Gadget (NO underscore — see the ALSA two-names note in .env.example).
    assert 'Environment="JASPER_USBSINK_MIXER_CARD=UAC2Gadget"' in body, (
        "jasper-usbsink.service must declare a packaged "
        'Environment="JASPER_USBSINK_MIXER_CARD=UAC2Gadget" default so the '
        "ExecStopPost ${JASPER_USBSINK_MIXER_CARD} expansion resolves to the "
        "correct card when no operator override is present."
    )

    # amixer invoked by absolute path — ExecStopPost= runs outside a login
    # shell, so a bare command name would not resolve via $PATH.
    assert "/usr/bin/amixer" in line, (
        f"ExecStopPost pitch reset must invoke amixer by absolute path; "
        f"got: {line!r}"
    )


def test_execstoppost_pitch_reset_runs_unconditionally():
    # The invariant must hold even when the feature was never enabled: a
    # value could only be non-neutral if the feature was enabled and the
    # daemon then died uncleanly, so the reset runs on every stop rather
    # than being gated on JASPER_USBSINK_HOST_CLOCK.
    body = UNIT_PATH.read_text()
    idx = _line_index(body, "Capture Pitch 1000000")
    line = body.splitlines()[idx].strip()
    assert "JASPER_USBSINK_HOST_CLOCK" not in line, (
        "the ExecStopPost pitch-neutrality reset must not be gated on the "
        "host-clock feature flag — it is a belt-and-braces safety net that "
        "must run unconditionally"
    )
