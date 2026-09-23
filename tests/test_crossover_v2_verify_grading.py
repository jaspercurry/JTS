# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""What the conductor decides about a VERIFY capture, and how it says so.

**Re-housed, not rewritten.** Every pin here came verbatim out of
``tests/test_crossover_v2_conductor.py``, where the verify region sat as five
consecutive sections inside a 12,800-line suite. Ruling S1 (ADR-0228) renames
what they cover — VERIFY is ``measure`` with
:data:`~jasper.active_speaker.crossover_v2.contracts.MEASURE_KIND_VERIFY` plus
``analyze`` — and the strangler moves the region's production half after this.
These pins are what that move has to keep true, so they now live in a file named
for the question they ask rather than for the god object that currently answers
it.

Five sections, in the order the evidence has to survive, each with the incident
that produced it:

* **#1873 — a verify-fail that REPEATS is a finding, not a transient.** Two
  answers 0.16 dB apart, from an instrument whose measured consecutive-pair
  repeat floor is 0.052 dB median / 0.085 dB p95, are the SAME answer twice; the
  phone offered "Try again" anyway and the capture session's TTL expired mid-loop.
* **#1971 — the VERIFY capture-integrity gate.** Nothing on the VERIFY path
  checked whether the capture the tracking verdict grades was intact, because
  ``glitch_detected`` came from a splice filter over ``KIND_SWEEP`` while
  VERIFY's sweep is ``KIND_SUMMED_SWEEP``.
* **#1974 — the outcome and the verdict that produced it**, including the gate
  record that banks the two numbers its own sentence narrates.
* **PR-5 — the retired per-capture flatness relay.** One VERIFY capture graded
  on its own grid was a SECOND construction of "is the speaker flat". What stays
  pinned is the boundary that made removing it safe: the verdict never consulted
  flatness.
* **G3 — verify inter-attempt pilot consistency**, and #1924/#1927's copy rule:
  the microphone is named as a cause only by the code that holds the evidence
  for it.

**What stayed behind, and why.** Fragment ``15``'s instrument, re-run at HEAD,
counts **71 in-lane functions** in the conductor suite. **37 of them are here**,
inside these five sections, together with **8 more** that sit in the same
sections and answer the same question. The other 34 are scattered through
sections whose subject is something else — the model-error store, the diag
logger, the delta probe, the claims block — and lifting a function out of the
section whose prose explains it would cost more than the split buys. They move
when their own section does.

The harness is ``tests/crossover_v2_fixtures.py``, unchanged: this module builds
the same real conductor over the same fake seams the suite it came from does.
"""

from __future__ import annotations

import dataclasses

import pytest

from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_REGISTRY,
)
from jasper.audio_measurement import gating

from tests.crossover_v2_fixtures import (
    _gate_block,
)


#
# Before this, nothing on the VERIFY path checked whether the capture the
# tracking verdict grades was intact: ``glitch_detected`` came from
# ``_estimate_drift`` (MEASURE-only), and the two flow gates that DO catch a
# splice filter ``KIND_SWEEP`` while VERIFY's sweep is ``KIND_SUMMED_SWEEP``.


@pytest.mark.parametrize(
    ("block_kwargs", "moved_rms_db", "reflection_delay_ms", "reflection_measured"),
    [
        pytest.param({}, 2.59, pytest.approx(5.33), True, id="both-numbers-banked"),
        # Null, never 0.0: nothing was found, so there is nothing to time, and a
        # 0.0 would say the reflection arrived with the direct sound. The delta
        # survives — ``SMALL_DELTA_RMS_DB``'s two readings: a capped gate still
        # moved the spectrum, which means "nothing was proven about
        # reflections" rather than "clean".
        pytest.param(
            {"first_reflection_ms": None, "floor_source": gating.FLOOR_SEARCH_BOUND},
            2.59, None, False, id="ceiling-capped-banks-no-delay",
        ),
        # ``pre_post_gate_delta`` is ``None`` when no band could price the gate
        # (an ungateable capture, or a program that declared no radiated band —
        # the over-report ``evaluation_band_hz`` refuses to make). A 0.0 here
        # would claim the gate changed nothing, which is a measurement.
        pytest.param({"rms_db": None}, None, pytest.approx(5.33), True,
                     id="unpriceable-banks-no-movement"),
    ],
)
def test_the_gate_record_banks_each_number_its_sentence_narrates_or_a_null(
    block_kwargs, moved_rms_db, reflection_delay_ms, reflection_measured,
):
    """Ticket 1.5. The sentence was the only copy of both numbers, and prose is
    not a number: the evidence packet's ``not_evaluated`` block said in so many
    words that the reflection time "is narrated inside verify.gate.disclosure
    prose and is not banked as a number anywhere in a round's artifacts".

    Equality against ``build_gate_disclosure``'s own derivations, not a
    recomputation — same discipline as the sentence's own test above. A record
    that assembled these from the raw block would be a second derivation of a
    fact that has one owner, and the digits in the prose and the digits in the
    fields could then disagree.

    The delay is a DELAY, not the absolute time the gating block spells
    ``first_reflection_ms`` — whose origin is the deconvolution window's, and
    which ``GateDisclosure.reflection_delay_ms`` calls meaningless to a reader
    on its own. 15.73 - 10.40, never 15.73.
    """
    from jasper.active_speaker.crossover_v2.capture_dispatch import (  # lazy: avoid measurement-stack import cost on unused paths
        _gate_record,
    )
    from jasper.audio_measurement import gate_disclosure as gd
    from tests.crossover_v2_fixtures import _driver_response_diag

    block = _gate_block(**block_kwargs)
    response = dataclasses.replace(_driver_response_diag("summed"), gating=block)
    typed = gd.build_gate_disclosure(block)
    record = _gate_record(response)

    assert record is not None
    assert record["moved_rms_db"] == typed.delta_rms_db == moved_rms_db
    assert record["reflection_delay_ms"] == typed.reflection_delay_ms
    assert record["reflection_delay_ms"] == reflection_delay_ms
    # The screen's two facts, beside the numbers.
    assert record["reflection_measured"] is reflection_measured
    assert record["disclosure"] == gd.describe_gate(block)


def _declared(tmp_path, **over):
    """One declared rig, written where only this test can see it (#3502).

    Never :data:`~jasper.audio_measurement.measurement_geometry.DEFAULT_PATH`:
    the production file is the operator's and a test must not read or write it.
    """
    from jasper.audio_measurement.measurement_geometry import DeclaredGeometry

    path = tmp_path / "measurement_geometry.json"
    DeclaredGeometry(**{
        "speaker_height_m": 0.84, "mic_height_m": 0.5, "distance_m": 1.0, **over,
    }).save(path)
    return path


def test_a_gate_record_carries_the_declared_room_floor_and_says_it_is_declared(
    tmp_path,
):
    """#3502 — the whole point of declaring a rig: the floor stops being unknown.

    The 2026-07-30 rig class never fires the measured reflection finder, so
    without a declaration every capture publishes ``unknown`` forever. With one,
    the same capture publishes a floor AND the word that says the operator's
    tape measure produced it — never a word that would let it read as measured.
    """
    from jasper.active_speaker.crossover_v2.capture_dispatch import (  # lazy: avoid measurement-stack import cost on unused paths
        _gate_record,
    )
    from jasper.audio_measurement import gating
    from jasper.audio_measurement.measurement_geometry import declared_first_bounce_s
    from tests.crossover_v2_fixtures import _driver_response_diag

    block = _gate_block(floor_source=gating.FLOOR_SEARCH_BOUND)
    response = dataclasses.replace(_driver_response_diag("summed"), gating=block)
    bounce_s = declared_first_bounce_s(1.0, path=_declared(tmp_path))

    declared = _gate_record(response, declared_first_bounce_s=bounce_s)
    undeclared = _gate_record(response)

    assert declared is not None and undeclared is not None
    assert declared["entanglement_floor_hz"] == pytest.approx(
        gating.f_entanglement_floor_hz(bounce_s)
    )
    assert declared["entanglement_floor_source"] == gating.ENTANGLEMENT_SOURCE_DECLARED
    assert undeclared["entanglement_floor_hz"] is None
    assert undeclared["entanglement_floor_source"] == gating.ENTANGLEMENT_SOURCE_UNKNOWN
    # Declaring a rig changes the floor and NOTHING else about the record.
    assert {k: v for k, v in declared.items() if "entanglement" not in k} == {
        k: v for k, v in undeclared.items()
        if "entanglement" not in k and k != "disclosure"
    } | {"disclosure": declared["disclosure"]}


#
# The flat-linearization plan's PR-5 removed ``ProgramAnalysis.flatness_tracking``
# and the conductor's ``flatness_evidence`` stash: one VERIFY capture graded on
# its own grid against its own band mean was a SECOND construction of "is the
# speaker flat", disagreeing with the spatial cloud's spec evaluation by however
# much a single mic position differs from the cloud. The claim now has exactly
# one owner (``assemble_cloud_group_result``'s ``flatness`` key). What stays
# pinned here is the boundary that made the removal safe: the VERIFY verdict
# never consulted flatness, so removing it changed no accept/code.


# ``reference`` is the pilot state of the attempt that ESTABLISHES the G3
# comparator, or ``None`` for a row with no preceding attempt; ``attempt`` is the
# pilot state of the attempt under test. Both are ``_verify_analysis`` kwargs.


def test_verify_level_shift_copy_is_true_on_both_surfaces():
    """#1924's routing half. One string renders on the measurement page's
    in-session retry (which re-compares the same reference and CAN repeat)
    and on the wizard's fresh-session retry (which since #1927 settles it in
    one capture). So it must command neither and discredit neither: state the
    fact, contextualize the retry, name the escalation conditionally."""
    message = REASON_REGISTRY["verify_level_shift"].message
    assert message == (
        "The microphone's levels changed between measurements, so this check "
        "couldn't settle. Try again — if it repeats, re-measure."
    )
    # The retired routing: it commanded the retry the phone cannot win.
    assert "re-verify" not in message.lower()
    # The visible primary is named, not undermined — the sibling
    # ``verify_out_of_tolerance`` names its primary too.
    assert "Try again" in message
    # …and the escalation is conditional on the retry repeating, never
    # presented as the only way forward.
    assert "if it repeats, re-measure" in message
