# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""R10 — the live cross-check: the one rule that binds a render to reality.

Compares each owner channel's rendered post-limiter peak against the LIVE
``get_playback_peak_all()`` reading for that channel, fail-closed. This module
is pure numeric/comparison logic; the live I/O (polling
``CamillaController.get_playback_peak_all`` / ``get_clipped_samples`` across a
role's playback) is the executor's concern.

**The fader-metering citation — the most safety-load-bearing finding in this
implementation.** R10(a)/(b) make the comparison offset depend on a recorded
obligation: does the render carry the recorded main-fader gain, or not (the
same R4(c) branch)? Verified against the pinned CamillaDSP v4.1.3 source:

* ``src/pipeline.rs`` ``Pipeline::process_chunk``: ``self.volume.process_chunk
  (&mut chunk)`` (the process-wide main-fader stage, built in
  ``Pipeline::from_config`` from ``processing_params.current_volume(0)``) runs
  BEFORE the ``for step in &mut self.steps`` loop — i.e. before EVERY
  configured pipeline step, unconditionally. The main fader therefore always
  precedes the owner limiter for this build; R4(c)'s "precedes" branch is the
  only reachable one (not "currently unreachable" as the amendment's R4(c)
  prose states — that prose describes a different, PIPELINE-embedded
  ``Volume``-type FILTER step, which R7 separately refuses on the owner path;
  it is not this always-present process-wide fader).
* ``src/socketserver.rs``: ``WsCommand::SetVolume``/``GetVolume`` read/write
  ``processing_params.target_volume(0)`` — the exact index ``Pipeline::
  from_config`` reads for ``self.volume`` — confirming pycamilladsp's
  ``main_volume`` (what ``CamillaController.get_volume_db`` wraps) IS this
  fader.
* ``rust/…/alsa_backend/device.rs`` playback loop: ``chunk.update_stats(&mut
  chunk_stats)`` runs on the chunk received from the processing thread — i.e.
  AFTER the full ``pipeline.process_chunk`` (fader + every step) already ran —
  so ``get_playback_peak_all()`` always reads downstream of the fader too.

Given R4(c) therefore ALWAYS resolves to "precedes the limiter" for this
build, the render ALWAYS reproduces the recorded fader gain (see
``render.py``'s ``render_config``, which threads ``--gain=<fader_db>`` — ONE
``=``-joined argv token, never two separate elements — into every render's
argv; this is the mechanism, not merely a documented intent), and the live
meter always reads AFTER the fader (the third citation above). Both sides
therefore carry the SAME fader attenuation — :func:`comparison_offset_db`
resolves to 0 dB, not the written ``-recorded_main_volume_db`` term. This
finding was independently confirmed by gate-2 review (including the
``--gain``/``initial_volumes[0]``/no-startup-ramp mechanism, ``src/bin.rs``),
so the amendment's "VOID until this citation is recorded" marker is retired
— see the revision-7 (errata) changelog entry and the corrected R4(b)/(c)
and R10(a)/(b) rule text in
``docs/bass-extension-waves/limiter-tap-realization.md``, not the prior
"meter reads before the main fader" phrasing this module's own docstring
used to quote (that phrasing was itself corrected to "after" in the same
errata — quoting it verbatim here would now be a second, driftable copy of
doc prose). This module still keeps BOTH branches as an explicit, general,
parameterized function rather than hardcoding the conclusion, so a reviewer
can audit the reasoning without trusting a hardcoded constant.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .analysis import sample_peak_dbfs


class CrossCheckError(ValueError):
    """R10(e): disagreement, an unavailable observation, or a bad tolerance
    refuses the pass — this is a campaign-level refusal, not a channel
    verdict."""


class OwnerChannelsInadmissible(ValueError):
    """R10(a): the campaign manifest names a bass owner with an invalid
    channel index — refused at AUTHORING time, never at bench time."""


def validate_owner_channels_admissible(
    owner_channels: Sequence[int], *, devices_playback_channels: int
) -> None:
    """R10(a): every ``owner_channels`` entry must satisfy
    ``0 <= index < devices.playback.channels``.

    Admissibility is a per-entry index check, not a topology restriction —
    multi-entry ``owner_channels`` are admissible; owner KIND is never the
    criterion. Raises :class:`OwnerChannelsInadmissible`, naming the
    offending index, so a campaign manifest can be validated before any
    bench pass runs.
    """

    if devices_playback_channels <= 0:
        raise OwnerChannelsInadmissible(
            f"devices.playback.channels must be positive, got {devices_playback_channels}"
        )
    for index in owner_channels:
        if not (0 <= index < devices_playback_channels):
            raise OwnerChannelsInadmissible(
                f"owner channel index {index} is outside "
                f"[0, {devices_playback_channels}) — condition (2) failed"
            )


def cross_check_available(
    live_peak_all: Sequence[float] | None, *, owner_channels: Sequence[int]
) -> bool:
    """R10(a): ``get_playback_peak_all()`` returning ``[]`` or shorter than
    ``max(owner_channels)+1`` makes the cross-check unavailable."""

    if not owner_channels:
        return False
    if live_peak_all is None or len(live_peak_all) == 0:
        return False
    return len(live_peak_all) >= max(owner_channels) + 1


def comparison_offset_db(
    *, render_carries_fader_gain: bool, recorded_main_volume_db: float
) -> float:
    """R10(b): the dB added to the live meter reading before comparison.

    When the render reproduces the recorded fader gain (R4(c)'s "precedes the
    limiter" branch — see the module docstring: this is the ONLY branch this
    CamillaDSP build ever takes), both sides already carry identical
    attenuation, so no correction is needed: offset 0. When the render
    carries NO fader gain (R4(c)'s "follows the limiter" branch), the live
    meter's fader-inclusive reading must be corrected back to the same
    un-faded reference the render sits at: subtract the fader's dB.
    """

    if render_carries_fader_gain:
        return 0.0
    return -recorded_main_volume_db


def compute_permissive_tolerance_bound_db(
    rendered_body_samples: np.ndarray,
    *,
    sample_rate_hz: int,
    poll_interval_s: float,
) -> float:
    """R10(c): the maximum RISE in the rendered artifact's level over any
    window of one poll interval, computed over the R3 analysis window
    (stimulus body only — lead-in/lead-out excluded by the caller), NEVER
    from the stimulus.

    Bounds how far a poll of ``get_playback_peak_all()`` — a once-per-interval
    sample of the true peak — can under-read the true maximum. Using the
    envelope's RATE of change (not its level) keeps the bound non-circular: a
    constant-offset derivation bug (e.g. R2(ii)'s -6.02 dB split-mixer hazard)
    leaves the rate unchanged, so the bound stays tight and the bug is still
    caught.
    """

    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    if poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be positive")
    samples = np.asarray(rendered_body_samples, dtype=np.float64)
    if samples.size == 0:
        return 0.0
    window = max(1, round(poll_interval_s * sample_rate_hz))
    window_peaks = [
        sample_peak_dbfs(samples[start : start + window])
        for start in range(0, samples.size, window)
        if samples[start : start + window].size > 0
    ]
    if len(window_peaks) < 2:
        return 0.0
    rises = [after - before for before, after in zip(window_peaks, window_peaks[1:])]
    return max(0.0, max(rises))


@dataclass(frozen=True, slots=True)
class ChannelCrossCheckResult:
    """One owner channel's R10 verdict and the numbers behind it, recorded
    verbatim per R10(b)."""

    channel: int
    verdict: str  # "pass" | "fail"
    rendered_peak_dbfs: float
    live_peak_dbfs: float
    offset_db: float
    corrected_live_dbfs: float
    tolerance_db: float
    computed_bound_db: float

    def to_receipt(self) -> dict[str, object]:
        return {
            "channel": self.channel,
            "verdict": self.verdict,
            "rendered_peak_dbfs": self.rendered_peak_dbfs,
            "live_peak_dbfs": self.live_peak_dbfs,
            "offset_db": self.offset_db,
            "corrected_live_dbfs": self.corrected_live_dbfs,
            "tolerance_db": self.tolerance_db,
            "computed_bound_db": self.computed_bound_db,
        }


def cross_check_channel(
    *,
    channel: int,
    rendered_peak_dbfs: float,
    live_peak_all: Sequence[float],
    recorded_main_volume_db: float,
    render_carries_fader_gain: bool,
    tolerance_db: float,
    computed_bound_db: float,
) -> ChannelCrossCheckResult:
    """R10(b)/(c): one owner channel's fail-closed comparison.

    Raises :class:`CrossCheckError` (a campaign-level refusal, not a channel
    verdict) if ``tolerance_db`` exceeds ``computed_bound_db`` — the
    permissive (exceeds) side is bounded, not merely recorded. The under
    (falls-below) side always uses the plain manifest ``tolerance_db``.
    """

    if tolerance_db <= 0:
        raise CrossCheckError("R10 tolerance must be a positive recorded value")
    if tolerance_db > computed_bound_db:
        raise CrossCheckError(
            f"manifest tolerance {tolerance_db} dB exceeds the computed "
            f"permissive bound {computed_bound_db} dB — campaign refuses"
        )
    if channel < 0 or channel >= len(live_peak_all):
        raise CrossCheckError(
            f"owner channel {channel} is outside the live_peak_all reading "
            f"(length {len(live_peak_all)})"
        )

    live_peak = live_peak_all[channel]
    offset = comparison_offset_db(
        render_carries_fader_gain=render_carries_fader_gain,
        recorded_main_volume_db=recorded_main_volume_db,
    )
    corrected_live = live_peak + offset
    diff = rendered_peak_dbfs - corrected_live
    if diff > 0:
        verdict = "pass" if diff <= tolerance_db else "fail"
    else:
        verdict = "pass" if -diff <= tolerance_db else "fail"
    return ChannelCrossCheckResult(
        channel=channel,
        verdict=verdict,
        rendered_peak_dbfs=rendered_peak_dbfs,
        live_peak_dbfs=live_peak,
        offset_db=offset,
        corrected_live_dbfs=corrected_live,
        tolerance_db=tolerance_db,
        computed_bound_db=computed_bound_db,
    )


def clipped_samples_verdict(*, before: int, after: int) -> str:
    """R10(d): a non-zero clipped-samples increase across the role's playback
    refuses the pass — fail-closed evidence of digital clipping upstream."""

    return "pass" if after <= before else "fail"


def cross_check_owner_channels(
    *,
    owner_channels: Sequence[int],
    rendered_peaks_dbfs: dict[int, float],
    live_peak_all: Sequence[float] | None,
    recorded_main_volume_db: float,
    render_carries_fader_gain: bool,
    tolerance_db: float,
    computed_bounds_db: dict[int, float],
    clipped_samples_before: int,
    clipped_samples_after: int,
) -> list[ChannelCrossCheckResult]:
    """R10 end to end for one stimulus role's playback, EVERY owner channel.

    ``computed_bounds_db`` is PER CHANNEL — R10(c)'s bound is computed from
    each channel's OWN rendered artifact (its own envelope-rise rate), never
    max-collapsed across channels: two owner channels can legitimately have
    different envelopes (different acoustic paths) and therefore different
    permissive bounds.

    A render is never admitted uncross-checked: an unavailable observation, a
    clipped-samples increase, or any single channel failing refuses the
    WHOLE pass — one failing channel refuses the whole pass, per R10(a).
    """

    if not cross_check_available(live_peak_all, owner_channels=owner_channels):
        raise CrossCheckError(
            "cross_check: unavailable — get_playback_peak_all() returned "
            f"{'[]' if not live_peak_all else f'{len(live_peak_all)} channels'}, "
            f"short of owner_channels {list(owner_channels)}"
        )
    if clipped_samples_verdict(
        before=clipped_samples_before, after=clipped_samples_after
    ) == "fail":
        raise CrossCheckError(
            "get_clipped_samples increased during this role's playback "
            f"({clipped_samples_before} -> {clipped_samples_after})"
        )
    assert live_peak_all is not None
    results: list[ChannelCrossCheckResult] = []
    for channel in owner_channels:
        if channel not in rendered_peaks_dbfs:
            raise CrossCheckError(f"no rendered peak recorded for owner channel {channel}")
        if channel not in computed_bounds_db:
            raise CrossCheckError(f"no computed tolerance bound for owner channel {channel}")
        result = cross_check_channel(
            channel=channel,
            rendered_peak_dbfs=rendered_peaks_dbfs[channel],
            live_peak_all=live_peak_all,
            recorded_main_volume_db=recorded_main_volume_db,
            render_carries_fader_gain=render_carries_fader_gain,
            tolerance_db=tolerance_db,
            computed_bound_db=computed_bounds_db[channel],
        )
        results.append(result)
    if any(result.verdict == "fail" for result in results):
        raise CrossCheckError(
            "R10 cross-check disagreement on channel(s) "
            f"{[r.channel for r in results if r.verdict == 'fail']} — refuses the pass"
        )
    return results


def min_across_channels(peaks: Sequence[float]) -> float:
    """R3's conservative collapse rule for a FUTURE single-number revision.

    This campaign never collapses owner channels (each yields its own source
    observation and candidate) — this helper is exercised only as a pure unit
    test of the collapse invariant, per R3's "the minimum across owner
    channels — the conservative choice — never an unstated one."
    """

    if not peaks:
        raise ValueError("peaks must be non-empty")
    return min(peaks)


__all__ = [
    "ChannelCrossCheckResult",
    "CrossCheckError",
    "OwnerChannelsInadmissible",
    "clipped_samples_verdict",
    "comparison_offset_db",
    "compute_permissive_tolerance_bound_db",
    "cross_check_available",
    "cross_check_channel",
    "cross_check_owner_channels",
    "min_across_channels",
    "validate_owner_channels_admissible",
]
