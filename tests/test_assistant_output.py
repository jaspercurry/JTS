# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""`AssistantOutput` driven directly, with no wake loop around it."""

from __future__ import annotations

from types import SimpleNamespace


class _FakeTts:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, dict]] = []

    def set_emission_admission(self, _admission) -> None:
        return None

    async def write_segment(self, pcm: bytes, **kwargs) -> None:
        self.calls.append((pcm, kwargs))

    async def wait_drained(self) -> None:
        return None

    def expected_drain_at(self) -> float:
        return 0.0


class _FakeDucker:
    async def duck(self) -> None:
        return None

    async def restore(self) -> None:
        return None


def _output(tts: _FakeTts, *, stamped: list[str] | None = None):
    from jasper.voice.assistant_output import AssistantOutput

    return AssistantOutput(
        SimpleNamespace(),  # type: ignore[arg-type]
        tts,  # type: ignore[arg-type]
        _FakeDucker(),  # type: ignore[arg-type]
        None,
        SimpleNamespace(),  # type: ignore[arg-type]
        stamp_stage=(stamped if stamped is None else stamped.append),
    )


async def test_mute_click_uses_matched_cue_path():
    from jasper.assistant_loudness import AssistantLoudnessProfile

    profile = AssistantLoudnessProfile(
        provider="jts",
        model="synthetic-mute-click",
        voice="unmute",
        source_lufs=-30.0,
        source_peak_dbfs=-12.0,
        confidence=1.0,
        updated_at="static",
        method="synthetic_generated",
    )
    tts = _FakeTts()
    output = _output(tts)
    # STATED, not inherited: the bake width comes from `tts_wire_is_wide()`,
    # which reads the box's own fanin.env — absent on a test runner, and an
    # undeclared box is WIDE since #3655. The flag asserted below is this
    # value, so the test must declare it rather than read the host's.
    output._earcon_wide = False
    output._mute_click_on_pcm = b"on"
    output._mute_click_off_pcm = b"off"
    output._mute_click_on_profile = profile
    output._mute_click_off_profile = object()

    await output.play_mute_click(going_on=True)

    assert tts.calls == [
        (
            b"on",
            {
                "segment_kind": "cue",
                "source_profile": profile,
                # The earcon bake's width travels with its bytes. This
                # output is DECLARED narrow above, so the flag reads False.
                "pcm_wide": False,
            },
        )
    ]


async def test_listening_chirp_uses_matched_chirp_path():
    from jasper.assistant_loudness import AssistantLoudnessProfile

    profile = AssistantLoudnessProfile(
        provider="jts",
        model="synthetic-listening-chirp",
        voice="wake_start",
        source_lufs=-15.0,
        source_peak_dbfs=-14.9,
        confidence=1.0,
        updated_at="static",
        method="synthetic_generated",
    )
    tts = _FakeTts()
    stamped: list[str] = []
    output = _output(tts, stamped=stamped)
    # STATED, not inherited — see the mute-click test above.
    output._earcon_wide = False
    output._chirp_on_pcm = b"wake"
    output._chirp_off_pcm = b"end"
    output._chirp_on_profile = profile
    output._chirp_off_profile = object()

    await output.listening_chirp(going_on=True)

    assert tts.calls == [
        (
            b"wake",
            {
                "segment_kind": "chirp",
                "source_profile": profile,
                "pcm_wide": False,
            },
        )
    ]
    # The wake-side chirp stamps the turn timeline before it writes.
    assert stamped == ["cue"]


async def test_admission_and_drain_are_open_while_the_gate_is_idle():
    output = _output(_FakeTts())

    assert output.admission_refusal() is None
    assert await output.drain_inflight(timeout_sec=0.0) is True
