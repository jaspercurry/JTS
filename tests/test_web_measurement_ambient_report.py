# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Retained ambient evidence remains readable."""

from jasper.audio_measurement import snr_policy


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
