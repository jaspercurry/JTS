# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The answer version of each tuning answer that no artifact row names (ADR-0387).

It imports nothing: ``jasper-round list|show`` read their rows, and must list a
Pi Zero's rounds without loading the view stack (ADR-0226).
"""

#: Keyed by command; a view's omits ``jasper-round-views``. A view that writes
#: an artifact answers under that artifact's row instead (ADR-0344 §4).
ANSWER_SCHEMAS = {
    "speaker-fit": "jts_speaker_fit/1",
    "repeat --set": "jts_repeat/1",
    "jasper-crossover-prescriber judge": "jts_prescription_judgement/1",
    "jasper-crossover-prescriber judge --preview": "jts_prescription_preview/1",
    "jasper-crossover-prescriber judge --preview --vary": "jts_prescription_preview_grid/1",
    "jasper-crossover-prescriber compose": "jts_prescription_candidate/1",
    "jasper-crossover-prescriber contract": "jts_prescription_contract/1",
    "jasper-crossover-prescriber status": "jts_prescriber_status/1",
    "jasper-round list": "jts_round_list/1",
    "jasper-round show": "jts_round_show/1",
}
