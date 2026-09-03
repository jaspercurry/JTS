# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``web_measurement._stored_ambient_report`` — the driver capture's
controlled-pre-sweep validation stub (tuning-master-plan ticket 2.13).

Before this ticket the returned dict also carried ``domain="controlled_pre_sweep"``
and ``source={"kind": "pending_signal_boundary", ...}`` — placeholder values
naming a "pending" analyzer that had, in fact, already been built independently:
``driver_acoustics.analyze_driver_capture`` computes its own real ambient block
tagged ``domain="deconvolved"`` / ``source.kind="signal_bounded_pre_sweep_quiet"``
(see ``test_active_speaker_driver_acoustics.py``). A repo-wide grep for the two
literal values (``pending_signal_boundary``, ``controlled_pre_sweep``) across
every ``.py``/``.js``/``.md``/``.yml``/``.json`` file found no reader anywhere
outside this function's own writer — they were write-only.

The same dishonesty class reached the remaining ``"method":
"paired_signal_window_deconvolution"`` field: it is read
only inside ``if noise_domain == "deconvolved":`` branches
(``driver_acoustics.py``'s two ``effective_noise_report.get("method")`` sites),
the stub's ``domain`` is absent so ``unwrap_noise_report`` yields ``"raw"``, and
the stub never even reaches ``unwrap_noise_report`` on the live path — both
call sites in ``web_measurement.py`` (the single-capture path and the
repeat-capture "winner" finalize path) force ``noise_band_report=None``
whenever the stub exists. So ``method`` was equally write-only and is removed
too. This module pins:

  - the trimmed return shape (only ``schema_version`` / ``ambient_duration_s``
    remain — no ``domain``, ``source``, or ``method``)
  - the validation behavior is unchanged (still raises / still returns
    ``None`` on a bad or undeclared duration)
  - a report persisted by the OLD code (with all three removed fields) still
    reads exactly the same as a NEW one through the one reader every real
    consumer actually goes through, ``snr_policy.unwrap_noise_report`` — it
    only ever compares ``domain`` against ``"deconvolved"`` and reads
    ``.get("bands")``, so dropping the fields is not a migration
  - ``method``'s presence or absence never changes ``unwrap_noise_report``'s
    output, independent of ``domain``/``source`` — it isn't even a key that
    function looks at
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.io import wavfile

from jasper.active_speaker import web_measurement
from jasper.audio_measurement import snr_policy

SR = 48000


def _write_wav(path, seconds: float, *, sr: int = SR, seed: int = 1):
    rng = np.random.default_rng(seed)
    samples = rng.normal(0.0, 0.001, int(round(seconds * sr))).astype(np.float32)
    wavfile.write(path, sr, samples)
    return path


def test_stored_ambient_report_omits_the_removed_placeholder_fields(tmp_path):
    path = _write_wav(tmp_path / "capture.wav", seconds=5.0)
    report = web_measurement._stored_ambient_report(
        path,
        {"n_samples": SR},  # 1 s reference sweep
        calibration=None,
        ambient_duration_s=3.0,
    )
    # Only what something actually reads survives: ambient_duration_s is read
    # back by the caller (raw["ambient_duration_s"]), schema_version mirrors
    # the sibling "deconvolved" shape. domain/source/method are all gone.
    assert report == {
        "schema_version": 2,
        "ambient_duration_s": 3.0,
    }
    assert "domain" not in report
    assert "source" not in report
    assert "method" not in report


def test_stored_ambient_report_still_raises_when_the_capture_is_too_short(tmp_path):
    # 1 s of audio can't hold a 3 s ambient window plus a 1 s reference sweep.
    path = _write_wav(tmp_path / "capture.wav", seconds=1.0)
    with pytest.raises(ValueError, match="controlled ambient interval"):
        web_measurement._stored_ambient_report(
            path,
            {"n_samples": SR},
            calibration=None,
            ambient_duration_s=3.0,
        )


def test_stored_ambient_report_returns_none_for_an_undeclared_duration(tmp_path):
    path = _write_wav(tmp_path / "capture.wav", seconds=5.0)
    for undeclared in (None, 0, -1.0, "not-a-number"):
        assert (
            web_measurement._stored_ambient_report(
                path,
                {"n_samples": SR},
                calibration=None,
                ambient_duration_s=undeclared,
            )
            is None
        )


def test_a_legacy_stored_report_with_the_removed_fields_still_reads_as_no_bands():
    """A measurement recorded by the pre-ticket-2.13 code still parses.

    Its persisted ``acoustic.ambient`` block can still carry
    ``domain="controlled_pre_sweep"``, ``method="paired_signal_window_deconvolution"``,
    and ``source.kind="pending_signal_boundary"`` on disk. Nothing requires
    migrating those records: the tolerant reader every consumer goes through
    degrades a legacy record and a current one (none of the three fields) to
    the identical outcome — no band evidence, "not deconvolved" — because it
    was already keyed only off ``.get("bands")`` and an equality check
    against ``"deconvolved"``, never off presence of ``domain``/``source`` or
    the specific placeholder values.
    """
    legacy = {
        "schema_version": 2,
        "domain": "controlled_pre_sweep",
        "method": "paired_signal_window_deconvolution",
        "ambient_duration_s": 14.0,
        "source": {
            "kind": "pending_signal_boundary",
            "protocol_paused_duration_s": 14.0,
        },
    }
    current = {
        "schema_version": 2,
        "ambient_duration_s": 14.0,
    }

    legacy_domain, legacy_bands = snr_policy.unwrap_noise_report(legacy)
    current_domain, current_bands = snr_policy.unwrap_noise_report(current)

    assert legacy_bands is None
    assert current_bands is None
    # Neither domain string is ever "deconvolved" — the only value any
    # caller (driver_acoustics.py, program_analysis.py) branches on — so the
    # two shapes are behaviorally identical to every real reader even though
    # their domain strings differ.
    assert legacy_domain != "deconvolved"
    assert current_domain != "deconvolved"

    # method's presence or absence is a no-op on its own: unwrap_noise_report
    # never looks at that key at all (only "domain" and "bands"), so a record
    # that still carries the old method value on disk reads identically to
    # one that never had it — independent of whatever domain/source say.
    with_method = {**current, "method": "paired_signal_window_deconvolution"}
    with_method_domain, with_method_bands = snr_policy.unwrap_noise_report(
        with_method
    )
    assert with_method_domain == current_domain
    assert with_method_bands == current_bands
