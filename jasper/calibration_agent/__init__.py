# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic substrate for the calibration/tuning agent, plus its opt-in
LLM surface.

Deterministic bundle/corpus tools, prompt packaging and response validation,
plus the P6 tuning surface (`model_client.call_advisor`,
`correction_advisor.interpret`/`propose`) the `/correction/` flow calls.
"""
