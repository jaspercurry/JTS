# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the CamillaDSP readback double against real CamillaDSP output.

The double in :mod:`tests._camilla_readback_double` is only worth having if it
actually behaves like the DSP. These tests hold it against a captured pair —
one JTS-emitted config, and what CamillaDSP 4.1.3 returned from ``GetConfig``
while running that exact config (jts5, 2026-08-06).

The second test is the regression guard for the whole defect class: the raw
file and the readback do NOT compare equal, so any live-graph proof that
fingerprints a JTS-authored file straight against a CamillaDSP readback refuses
everything it is asked to prove. That is what silently blocked commissioning,
correction, bass extension and both grouping roles until the bonded pair went
quiet on 2026-08-06.
"""

from __future__ import annotations

from pathlib import Path

from jasper.active_speaker.runtime_contract import _normalized_graph_fingerprint
from tests._camilla_readback_double import camilla_default_filled

_FIXTURES = Path(__file__).parent / "fixtures" / "camilla_readback"
_EMITTED = _FIXTURES / "jts_emitted_active_baseline.yml"
_READBACK = _FIXTURES / "camilladsp_4.1.3_getconfig.yml"


def test_double_reproduces_real_camilladsp_readback() -> None:
    """The double default-fills exactly what CamillaDSP 4.1.3 filled in."""

    emitted = _EMITTED.read_text(encoding="utf-8")
    real_readback = _READBACK.read_text(encoding="utf-8")

    assert _normalized_graph_fingerprint(
        camilla_default_filled(emitted)
    ) == _normalized_graph_fingerprint(real_readback)


def test_raw_emitted_config_never_matches_the_readback() -> None:
    """The asymmetry the double exists to model is real, not theoretical.

    If this ever starts passing, CamillaDSP stopped default-filling and the
    canonicalize seam in ``classify_active_bass_extension_graph`` could be
    reconsidered. Until then, comparing raw file text against a readback is a
    gate that always refuses.
    """

    emitted = _EMITTED.read_text(encoding="utf-8")
    real_readback = _READBACK.read_text(encoding="utf-8")

    assert _normalized_graph_fingerprint(emitted) != _normalized_graph_fingerprint(
        real_readback
    )
