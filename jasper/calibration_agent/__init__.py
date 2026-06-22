# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic substrate for the future calibration/tuning agent.

The package intentionally starts without an LLM dependency. It exposes
deterministic bundle/corpus tools, prompt packaging, and response
validation/action-running first so the eventual agent has a small,
testable, auditable surface to call.
"""
