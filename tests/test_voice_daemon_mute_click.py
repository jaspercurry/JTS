# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations


class _FakeTts:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, dict]] = []

    async def write_segment(self, pcm: bytes, **kwargs) -> None:
        self.calls.append((pcm, kwargs))

    async def wait_drained(self) -> None:
        return None


async def test_mute_click_uses_matched_cue_path():
    from jasper.assistant_loudness import AssistantLoudnessProfile
    from jasper.voice_daemon import WakeLoop

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
    # STATED, not inherited: the bake width comes from `tts_wire_is_wide()`,
    # which reads the box's own fanin.env — absent on a test runner, and an
    # undeclared box is WIDE since #3655. The flag asserted below is this
    # value, so the test must declare it rather than read the host's.
    wl = WakeLoop.for_tests(_earcon_wide=False)
    wl._tts = tts
    wl._mute_click_on_pcm = b"on"
    wl._mute_click_off_pcm = b"off"
    wl._mute_click_on_profile = profile
    wl._mute_click_off_profile = object()

    await wl._play_mute_click(going_on=True)

    assert tts.calls == [
        (
            b"on",
            {
                "segment_kind": "cue",
                "source_profile": profile,
                # The earcon bake's width travels with its bytes. This
                # WakeLoop is DECLARED narrow above, so the flag reads False.
                "pcm_wide": False,
            },
        )
    ]


async def test_listening_chirp_uses_matched_chirp_path():
    from jasper.assistant_loudness import AssistantLoudnessProfile
    from jasper.voice_daemon import WakeLoop

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
    # STATED, not inherited — see the mute-click test above.
    wl = WakeLoop.for_tests(_earcon_wide=False)
    wl._tts = tts
    wl._chirp_on_pcm = b"wake"
    wl._chirp_off_pcm = b"end"
    wl._chirp_on_profile = profile
    wl._chirp_off_profile = object()

    await wl._play_listening_chirp(going_on=True)

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
