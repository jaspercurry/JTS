# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Compare emitted linearization filters with their offline CamillaDSP output.

The treated and control renders share the same speaker baseline and stimulus.
Their difference is compared with ``complex_correction_response`` to detect
filter math that differs between the fitter and the DSP. The verdict
vocabulary belongs to ``active_speaker.delta_probe``.

``derivation`` preserves the emitted graph while changing its file devices;
``render`` owns the bounded binary invocation and repeatability check;
``compare`` owns analysis; ``loop`` runs the paired experiment.
"""

from __future__ import annotations
