# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pluggable AEC engines for jasper-aec-bridge.

The bridge captures mic + ref once and dispatches each chunk to N
engines in parallel. Each engine implements the same minimal
interface (`process(mic_bytes, ref_bytes) -> bytes`) and emits on
its own UDP port to jasper-voice.

Today the bridge runs AEC3-v2 (BEST_A) as the primary engine via
the binding in `jasper_aec3`. DTLN-aec sits in this package as a
neural alternative + a candidate 3rd leg for the triple-stream
wake-word architecture documented in
`docs/HANDOFF-mic-quality-v2.md` "Triple-stream architecture plan".
"""
