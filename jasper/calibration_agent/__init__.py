# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deterministic substrate for the calibration/tuning agent, plus its
opt-in LLM surface.

The package started without an LLM dependency: deterministic bundle/corpus
tools, prompt packaging, and response validation/action-running gave the
eventual agent a small, testable, auditable surface to call. It now also
ships that agent's P6 tuning surface (`model_client.call_advisor`,
`correction_advisor.interpret`/`propose`) — a live, opt-in OpenAI adapter
wired into the `/correction/` web flow's `POST /interpret`, `POST /propose`,
and `POST /propose/apply` endpoints. See `docs/HANDOFF-calibration-agent.md`
"The P6 tuning surface" for the shipped design.
"""
