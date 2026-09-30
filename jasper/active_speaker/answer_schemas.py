# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""The answer version of each tuning answer that no artifact row names (ADR-0387).

It imports nothing: ``jasper-round list|show`` read their rows, and must list a
Pi Zero's rounds without loading the view stack (ADR-0226).
"""

#: Keyed by command; a view's omits ``jasper-round-views``. A view that writes
#: an artifact answers under that artifact's row instead (ADR-0344 §4).
#: ``trial`` answers under ``run``'s rows, and ``run|trial --wait`` under ``wait``'s (ADR-0389).
ANSWER_SCHEMAS = {
    "catalog": "jts_tool_catalog/2",
    "speaker-fit": "jts_speaker_fit/1",
    "repeat --set": "jts_repeat/1",
    "jasper-crossover-prescriber judge": "jts_prescription_judgement/2",
    "jasper-crossover-prescriber judge --preview": "jts_prescription_preview/3",
    "jasper-crossover-prescriber judge --preview --vary": "jts_prescription_preview_grid/3",
    "jasper-crossover-prescriber compose": "jts_prescription_candidate/1",
    "jasper-crossover-prescriber contract": "jts_prescription_contract/2",
    "jasper-crossover-prescriber status": "jts_prescriber_status/3",
    "jasper-round run": "jts_round_run/1",
    "jasper-round run --dry-run": "jts_round_preflight/1",
    "jasper-round placed": "jts_round_placement/1",
    "jasper-round stop": "jts_round_stop/1",
    "jasper-round status": "jts_round_status/1",
    "jasper-round wait": "jts_round_wait/1",
    "jasper-round apply": "jts_round_apply/1",
    "jasper-round reset": "jts_round_reset/1",
    "jasper-round list": "jts_round_list/2",
    "jasper-round show": "jts_round_show/2",
    "jasper-round presets": "jts_round_presets/1",
}
