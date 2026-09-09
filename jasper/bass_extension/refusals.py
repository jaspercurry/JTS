# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The bass layer's refusal vocabulary — one name per way it says no.

A leaf so a reader that only needs the NAMES pays nothing for the physics:
:mod:`jasper.bass_extension.profile` reaches scipy through the enclosure
adapters, and the candidate field is on the crossover candidate's own import
path.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["BassExtensionRefusal"]


class BassExtensionRefusal(StrEnum):
    BASELINE_NOT_APPLIED = "bass_extension_baseline_not_applied"
    TOPOLOGY_MISMATCH = "bass_extension_topology_mismatch"
    BASS_OWNER_AMBIGUOUS = "bass_extension_bass_owner_ambiguous"
    BONDED_BASS_OWNER_REMOTE = "bass_extension_bonded_bass_owner_remote"
    ENCLOSURE_UNKNOWN = "bass_extension_enclosure_unknown"
    ENCLOSURE_UNSUPPORTED = "bass_extension_enclosure_unsupported"
    MEDIAN_UNREADABLE = "bass_extension_median_unreadable"
    PLANT_UNRESOLVED = "bass_extension_plant_unresolved"
    TUNING_NOT_LOCATED = "bass_extension_tuning_not_located"
    PR_NOTCH_NOT_LOCATED = "bass_extension_pr_notch_not_located"
    FIT_QUALITY_INSUFFICIENT = "bass_extension_fit_quality_insufficient"
    CAPTURE_QUALITY_REFUSED = "bass_extension_capture_quality_refused"
    CAPTURE_SNR_INSUFFICIENT = "bass_extension_capture_snr_insufficient"
    MIC_MOVED_BETWEEN_RUNGS = "bass_extension_mic_moved_between_rungs"
    LADDER_INCOMPLETE = "bass_extension_ladder_incomplete"
    BOOST_LIMIT_EXCEEDED = "bass_extension_boost_limit_exceeded"
    PROFILE_STALE = "bass_extension_profile_stale"
    # The candidate field's own refusals (:mod:`.candidate_field`): ONE
    # vocabulary for the layer, so the prescription door and the persistence
    # boundary refuse under the same names.
    FIELD_MALFORMED = "bass_extension_field_malformed"
    OWNER_INVALID = "bass_extension_owner_invalid"
    ADAPTER_UNKNOWN = "bass_extension_adapter_unknown"
    PLANT_INVALID = "bass_extension_plant_invalid"
    TARGET_INVALID = "bass_extension_target_invalid"
    TARGETS_UNORDERED = "bass_extension_targets_unordered"
    TARGET_LEVEL_INVALID = "bass_extension_target_level_invalid"
    PROTECTION_INVALID = "bass_extension_protection_invalid"
    BASIS_INVALID = "bass_extension_basis_invalid"
