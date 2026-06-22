# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Smoke test: the chip-AEC experimental daemon module still imports.

This test exists to catch passive bit-rot. `jasper.chip_aec_experiment`
is dormant infrastructure (see [`docs/CHIP-AEC-EXPERIMENT.md`](../docs/CHIP-AEC-EXPERIMENT.md)),
not exercised in production or by any other test. If an upstream
change renames a class the daemon imports (e.g. `alsaaudio.PCM`)
or refactors a shared symbol, the module silently breaks — and
nobody finds out until someone tries to run the experiment 6
months from now and has to spend half a day diagnosing.

CI catches that here. If this test goes red, either:
  - Fix the experiment to match the upstream change, OR
  - Open a PR to retire the experiment if the topology change
    has obsoleted it (don't silently leave it broken).

See [`docs/CHIP-AEC-EXPERIMENT.md`](../docs/CHIP-AEC-EXPERIMENT.md)
for the policy carve-out context that makes this exploratory
infrastructure live on `main` instead of a feature branch.
"""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def fake_alsaaudio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this dormant Pi-only module importable in hardware-free CI."""

    fake = types.SimpleNamespace(
        ALSAAudioError=Exception,
        PCM=object,
        PCM_CAPTURE=0,
        PCM_NORMAL=0,
        PCM_PLAYBACK=1,
        PCM_FORMAT_S16_LE=2,
    )
    monkeypatch.setitem(sys.modules, "alsaaudio", fake)
    monkeypatch.delitem(sys.modules, "jasper.chip_aec_experiment", raising=False)


def test_chip_aec_experiment_module_imports() -> None:
    """The experiment module is syntactically valid + imports cleanly."""
    import importlib

    module = importlib.import_module("jasper.chip_aec_experiment")

    # Sanity-check the public surface the five scripts/chip-aec-*.sh
    # shell scripts depend on. If any of these are renamed or removed,
    # the experiment is broken even if the bare import succeeded.
    assert callable(getattr(module, "main", None)), (
        "main() entry point missing — scripts/chip-aec-setup.sh "
        "invokes the daemon via `python -m jasper.chip_aec_experiment`"
    )
    assert callable(getattr(module, "reference_feeder", None)), (
        "reference_feeder() missing — chip-aec-capture-comparison.sh "
        "depends on --ref-only mode keeping this thread alive"
    )
    assert callable(getattr(module, "udp_mic_pump", None)), (
        "udp_mic_pump() missing — UDP frame emitter that delivers "
        "the selected chip channel to jasper-voice on 127.0.0.1:9876"
    )


def test_chip_aec_experiment_module_constants_intact() -> None:
    """Daemon's hardcoded ALSA + UDP targets haven't drifted.

    These constants are referenced by the shell scripts and by
    [`docs/CHIP-AEC-EXPERIMENT.md`](../docs/CHIP-AEC-EXPERIMENT.md).
    If somebody changes them without updating the docs/scripts, the
    experiment runs against the wrong targets — usually silently.
    """
    from jasper import chip_aec_experiment as m

    # The chip USB-UAC2 device — XVF3800 hardware identity.
    assert m.CHIP_DEVICE == "hw:CARD=Array,DEV=0", (
        f"CHIP_DEVICE changed: {m.CHIP_DEVICE!r}. "
        "Update CHIP-AEC-EXPERIMENT.md topology diagram + scripts/chip-aec-*.sh."
    )

    # The music-chain dsnoop tap. PR #214 added a renderer-side
    # dmix above it but left this name unchanged.
    assert m.SOURCE_DEVICE == "plug:jasper_capture", (
        f"SOURCE_DEVICE changed: {m.SOURCE_DEVICE!r}. "
        "Verify the music-chain tap is still pre-CamillaDSP."
    )

    # 16 kHz is the only rate the chip's USB-IN endpoint advertises
    # on every shipped firmware variant (HANDOFF-xvf3800.md §1).
    # Drifting this would feed nothing into the chip's AEC reference.
    assert m.RATE == 16000, f"RATE changed: {m.RATE}. Chip USB-IN is 16 kHz only."

    # Same UDP port the production AEC bridge writes to — preserves
    # the "no jasper-voice changes" contract.
    assert m.UDP_TARGET == ("127.0.0.1", 9876), (
        f"UDP_TARGET changed: {m.UDP_TARGET}. "
        "Voice daemon's mic_device default expects 9876."
    )

    # Option D tests chip-side AEC, not the production software-AEC
    # bridge. On 2026-05-29 the live A/B showed channel 0 carrying the
    # useful chip-AEC attenuation in this topology; keep it explicit and
    # override with JASPER_CHIP_AEC_MIC_CHANNEL/MIC_CHANNEL when testing
    # other chip taps.
    assert m.DEFAULT_MIC_CHANNEL == 0, (
        f"DEFAULT_MIC_CHANNEL changed: {m.DEFAULT_MIC_CHANNEL}. "
        "ch0 is the Conference beam; ch1 is ASR; ch2-5 are raw mics "
        "on 6-ch firmware. Verify against HANDOFF-xvf3800.md and the "
        "latest CHIP-AEC-EXPERIMENT.md results."
    )
