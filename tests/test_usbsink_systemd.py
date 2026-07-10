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


def _exec_condition_lines(unit_text: str) -> list[str]:
    return [
        s.partition("=")[2].strip()
        for ln in unit_text.splitlines()
        if not (s := ln.strip()).startswith("#") and s.startswith("ExecCondition=")
    ]


def test_unit_file_exists():
    assert UNIT_PATH.exists(), f"jasper-usbsink.service missing at {UNIT_PATH}"


def test_parks_cleanly_without_a_composed_uac2_function():
    """review core-4: the bridge must ExecCondition-gate on the composed uac2
    function so a pre-reboot enable (no UDC → gadget composed nothing) parks
    cleanly instead of restart-looping the 30 s wait-card forever.

    The path is a hardcoded literal (systemd ExecCondition can't expand the
    gadget scripts' JASPER_CONFIGFS_ROOT); it must match the gadget dir name
    jts-usb-audio + functions/uac2.usb0, and must be ordered BEFORE the
    wait-card ExecStartPre so the condition skip happens before any 30 s poll.
    """
    body = UNIT_PATH.read_text()
    conditions = _exec_condition_lines(body)
    uac2_gate = [
        c for c in conditions
        if "/sys/kernel/config/usb_gadget/jts-usb-audio/functions/uac2.usb0" in c
    ]
    assert uac2_gate, (
        "jasper-usbsink.service must ExecCondition on the composed uac2.usb0 "
        "function dir so a start without a composed audio function parks "
        "cleanly instead of looping through wait-card + Restart=on-failure."
    )
    assert uac2_gate[0].split()[0] == "/bin/test", (
        f"expected the uac2 gate to use /bin/test -d; got {uac2_gate[0]!r}"
    )
    # The existing local-source ExecCondition must remain (belt: both gates).
    assert any("jasper-local-source-allowed" in c for c in conditions), (
        "the local-source ExecCondition must remain alongside the uac2 gate."
    )
    # Ordering: the condition DIRECTIVE (which skips) must precede the 30 s
    # wait-card ExecStartPre directive so the skip short-circuits before any
    # poll. Match on the directive lines, not the surrounding comments.
    def _directive_index(prefix: str, needle: str) -> int:
        for idx, ln in enumerate(body.splitlines()):
            s = ln.strip()
            if s.startswith(prefix) and needle in s:
                return idx
        raise AssertionError(f"no {prefix!r} directive containing {needle!r}")

    assert _directive_index("ExecCondition=", "functions/uac2.usb0") < (
        _directive_index("ExecStartPre=", "jasper-usbsink-wait-card")
    ), "the uac2 ExecCondition must be listed before the wait-card ExecStartPre"


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
    assert 'name="Capture Pitch 1000000"' in line, (
        f"ExecStopPost pitch reset must target the 'Capture Pitch 1000000' "
        f"control by name; got: {line!r}"
    )

    # Resets to the neutral value (1000000 = unity, no pitch bias). The line is
    # an `sh -c '... 1000000'` wrapper (review F2 gate below), so the neutral
    # value is the last token before the closing single quote.
    assert "1000000'" in line or line.rstrip().endswith("1000000"), (
        f"ExecStopPost pitch reset must write the neutral value 1000000, "
        f"not a stale/non-neutral bias; got line: {line!r}"
    )

    # Card is selected by expanding $JASPER_USBSINK_MIXER_CARD (shell expansion
    # inside the sh -c wrapper) rather than a hardcoded literal, so an operator
    # overriding that card in an EnvironmentFile also redirects this
    # belt-and-braces neutralize (review N3 — no hardcoded card drift between the
    # daemon and the stop line).
    assert '"$JASPER_USBSINK_MIXER_CARD"' in line, (
        f"ExecStopPost pitch reset must target -c $JASPER_USBSINK_MIXER_CARD "
        f"(shell-expanded), not a hardcoded card literal; got: {line!r}"
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


def test_execstoppost_pitch_reset_gated_on_not_standby():
    # Review F2: the belt must NOT be gated on the host-clock feature flag (a
    # value could only be non-neutral if the feature was enabled and the daemon
    # died uncleanly, so gating on JASPER_USBSINK_HOST_CLOCK would skip the reset
    # exactly when it is needed) — but it MUST be gated on NOT-standby.
    #
    # In combo mode this bridge runs in standby (JASPER_USBSINK_AUDIO_STANDBY=1)
    # and does not own the pitch ctl — fan-in does, and commands non-neutral
    # pitch. An unconditional neutralize here would fire on every stop of this
    # unit (deploy try-restart, operator restart) and stomp fan-in's live L0
    # command, desyncing fan-in's >10 ppm write-suppression epsilon → the host
    # free-runs un-slaved for minutes. So the belt fires only when this bridge is
    # NOT in standby (i.e. when it is the daemon that actually owns the ctl).
    body = UNIT_PATH.read_text()
    matches = [
        v for v in _exec_stop_post_lines(body)
        if "Capture Pitch 1000000" in v
    ]
    assert matches, "no ExecStopPost pitch-reset line found"
    line = matches[0]
    assert "JASPER_USBSINK_HOST_CLOCK" not in line, (
        "the ExecStopPost pitch-neutrality reset must not be gated on the "
        "host-clock feature flag — gating there would skip the reset exactly "
        "when a stale non-neutral value could exist (feature on, unclean death)"
    )
    assert '"$JASPER_USBSINK_AUDIO_STANDBY" != "1"' in line, (
        "the ExecStopPost pitch reset MUST gate on NOT-standby "
        '([ "$JASPER_USBSINK_AUDIO_STANDBY" != "1" ]) — in combo mode this '
        "bridge runs in standby and fan-in owns the ctl; an unconditional "
        "neutralize would stomp fan-in's live L0 command and desync its epsilon "
        f"gate (review F2). Got: {line!r}"
    )


def test_start_limit_widened_and_never_reboots():
    """Hardening rider (defect 2026-07-10): Restart=on-failure/RestartSec=2s with
    NO StartLimit* would let a fast ENODEV unplug/replug flap exhaust systemd's
    default 5-in-10s window and park the unit `failed` forever. The tolerance is
    RAISED VIA BURST COUNT, not a stricter interval: StartLimitIntervalSec=300 +
    StartLimitBurst=20 (a stricter interval alone, e.g. keeping burst at systemd's
    default-adjacent 5, would still park the unit on a slow flap — a cable jiggled
    every 30-60 s — well inside a 300 s window). With RestartSec=2s, burst=20 rides
    through that slow-flap case entirely, while a persistently-crashing bridge still
    burns ~20 bounded attempts (~40 s) before parking `failed` rather than
    restart-looping forever — the parked state is what jasper-doctor's
    check_usb_combo_fallback surfaces. And — unlike the core graph (jasper-fanin) —
    NEVER StartLimitAction=reboot: a repeatedly-failing USB bridge must not reboot
    the whole speaker."""
    body = UNIT_PATH.read_text()
    assert _value_for(body, "StartLimitIntervalSec") == "300", (
        "jasper-usbsink.service must set StartLimitIntervalSec=300 as the window "
        "over which StartLimitBurst is counted."
    )
    assert _value_for(body, "StartLimitBurst") == "20", (
        "jasper-usbsink.service must set StartLimitBurst=20 — tolerance is raised "
        "via burst count (rides out a slow replug flap) rather than a stricter "
        "interval, which would still park the unit on a slow flap."
    )
    # The load-bearing safety invariant: a bridge failure must never reboot the box.
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        assert not s.startswith("StartLimitAction="), (
            "jasper-usbsink.service must NOT set StartLimitAction (esp. =reboot): a "
            f"failing USB bridge must never reboot the speaker. Got: {s!r}"
        )
    # Restart policy that the StartLimit governs must still be present.
    assert _value_for(body, "Restart") == "on-failure"
