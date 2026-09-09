# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The wire vocabulary is byte-identical to the literals it replaced.

Every expected value below is the exact string the sender spelled before
``jasper.platform.wire`` owned it, so a formatter change that would move a byte
on the fan-in, mux or TTS socket fails here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.assistant_volume import EffectiveVolumeContext
from jasper.platform import wire

REPO = Path(__file__).resolve().parents[1]
FANIN_STATE_RS = REPO / "rust" / "jasper-fanin" / "src" / "state.rs"
TTS_PROTOCOL_RS = REPO / "rust" / "jasper-tts-protocol" / "src" / "lib.rs"
MUX_PY = REPO / "jasper" / "mux.py"
VOICE_CONTROL_SOCKET_PY = REPO / "jasper" / "voice" / "control_socket.py"

_CONTEXT = EffectiveVolumeContext(
    canonical_db=-18.25,
    downstream_db=-6.5,
    tts_envelope_lufs=-21.0,
    muted=True,
    stamp_boot_ns=1234567890123,
)


@pytest.mark.parametrize("command, expected", [
    # jasper-fanin control socket (mux's source gate).
    (wire.STATUS, "STATUS"),
    (wire.FANIN_NONE, "NONE"),
    (wire.fanin_select("spotify"), "SELECT spotify"),
    (wire.fanin_lane_mute("usbsink", muted=True), "MUTE usbsink"),
    (wire.fanin_lane_mute("usbsink", muted=False), "UNMUTE usbsink"),
    # jasper-mux control socket.
    (wire.MUX_AUTO, "AUTO"),
    (wire.mux_select("airplay"), "SELECT airplay"),
    (wire.mux_preempt("airplay"), "PREEMPT airplay"),
    (wire.mux_test_select("correction", "doctor"), "TEST_SELECT correction doctor"),
    (wire.mux_test_release("doctor"), "TEST_RELEASE doctor"),
    # TTS playout socket.
    (wire.TTS_CLOSE, "CLOSE"),
    (wire.TTS_FLUSH_SYNC, "FLUSH_SYNC"),
    (wire.TTS_SEGMENT_END, "SEGMENT_END"),
    (wire.TTS_CONTENT_METER_PAUSE, "CONTENT_METER_PAUSE"),
    (wire.TTS_CONTENT_METER_RESUME, "CONTENT_METER_RESUME"),
    (wire.tts_program_duck(True), "PROGRAM_DUCK_ON"),
    (wire.tts_program_duck(False), "PROGRAM_DUCK_OFF"),
    (wire.tts_gain(-12.0), "GAIN -12.000"),
    (wire.tts_gain(0.12345), "GAIN 0.123"),
    (wire.tts_audio(wire.TTS_AUDIO_NARROW, 960), "AUDIO 960"),
    (wire.tts_audio(wire.TTS_AUDIO_WIDE, 1920), "AUDIO32 1920"),
    (wire.tts_segment_start("speech", "item-7", None), "SEGMENT_START speech item-7"),
    (
        wire.tts_segment_start("speech", "item-7", ("openai", "gpt", "cedar")),
        "SEGMENT_START speech item-7 openai gpt cedar",
    ),
    (
        wire.tts_volume_context(_CONTEXT),
        "VOLUME_CONTEXT -18.250 -6.500 -21.000 1 1234567890123",
    ),
    (
        wire.tts_prepare_assistant(
            provider="openai", model="gpt", voice="cedar",
            tts_envelope_lufs=-21.0,
        ),
        "PREPARE_ASSISTANT openai gpt cedar -21.00",
    ),
    (
        wire.tts_prepare_assistant(
            provider="openai", model="gpt", voice="cedar",
            tts_envelope_lufs=-21.0, volume_context=_CONTEXT,
        ),
        "PREPARE_ASSISTANT openai gpt cedar -21.00 "
        "-18.250 -6.500 -21.000 1 1234567890123",
    ),
    # jasper-voice control socket (external IPC into the wake loop).
    (wire.STATUS, "STATUS"),
    (wire.voice_start(), "START"),
    (wire.voice_start("airplay"), "START airplay"),
    (wire.VOICE_END, "END"),
    (wire.voice_cue_play("doorbell"), "CUE_PLAY doorbell"),
    (wire.voice_mic_mute(True), "MUTE"),
    (wire.voice_mic_mute(False), "UNMUTE"),
    (wire.VOICE_MEASURE_PAUSE, "MEASURE_PAUSE"),
    (wire.VOICE_MEASURE_RESUME, "MEASURE_RESUME"),
])
def test_formatter_bytes_match_the_literal_they_replaced(command, expected):
    assert command == expected
    assert wire.encode(command) == (expected + "\n").encode("ascii")


def test_unmuted_volume_context_carries_the_zero_token():
    unmuted = EffectiveVolumeContext(
        canonical_db=0.0, downstream_db=0.0, tts_envelope_lufs=0.0,
        muted=False, stamp_boot_ns=0,
    )
    assert wire.tts_volume_context(unmuted) == "VOLUME_CONTEXT 0.000 0.000 0.000 0 0"


@pytest.mark.parametrize("command, reader", [
    (wire.STATUS, FANIN_STATE_RS),
    (wire.FANIN_NONE, FANIN_STATE_RS),
    (wire.fanin_select("x"), FANIN_STATE_RS),
    (wire.fanin_lane_mute("x", muted=True), FANIN_STATE_RS),
    (wire.fanin_lane_mute("x", muted=False), FANIN_STATE_RS),
    (wire.TTS_CLOSE, TTS_PROTOCOL_RS),
    (wire.TTS_FLUSH_SYNC, TTS_PROTOCOL_RS),
    (wire.TTS_SEGMENT_END, TTS_PROTOCOL_RS),
    (wire.TTS_CONTENT_METER_PAUSE, TTS_PROTOCOL_RS),
    (wire.TTS_CONTENT_METER_RESUME, TTS_PROTOCOL_RS),
    (wire.tts_program_duck(True), TTS_PROTOCOL_RS),
    (wire.tts_program_duck(False), TTS_PROTOCOL_RS),
    (wire.tts_gain(0.0), TTS_PROTOCOL_RS),
    (wire.tts_audio(wire.TTS_AUDIO_NARROW, 0), TTS_PROTOCOL_RS),
    (wire.tts_audio(wire.TTS_AUDIO_WIDE, 0), TTS_PROTOCOL_RS),
    (wire.tts_segment_start("k", "i", None), TTS_PROTOCOL_RS),
    (wire.tts_volume_context(_CONTEXT), TTS_PROTOCOL_RS),
    (
        wire.tts_prepare_assistant(
            provider="p", model="m", voice="v", tts_envelope_lufs=0.0,
        ),
        TTS_PROTOCOL_RS,
    ),
    (wire.STATUS, MUX_PY),
    (wire.MUX_AUTO, MUX_PY),
    (wire.mux_select("x"), MUX_PY),
    (wire.mux_preempt("x"), MUX_PY),
    (wire.mux_test_select("l", "o"), MUX_PY),
    (wire.mux_test_release("o"), MUX_PY),
    (wire.STATUS, VOICE_CONTROL_SOCKET_PY),
    (wire.voice_start(), VOICE_CONTROL_SOCKET_PY),
    (wire.voice_start("airplay"), VOICE_CONTROL_SOCKET_PY),
    (wire.VOICE_END, VOICE_CONTROL_SOCKET_PY),
    (wire.voice_cue_play("doorbell"), VOICE_CONTROL_SOCKET_PY),
    (wire.voice_mic_mute(True), VOICE_CONTROL_SOCKET_PY),
    (wire.voice_mic_mute(False), VOICE_CONTROL_SOCKET_PY),
    (wire.VOICE_MEASURE_PAUSE, VOICE_CONTROL_SOCKET_PY),
    (wire.VOICE_MEASURE_RESUME, VOICE_CONTROL_SOCKET_PY),
])
def test_every_verb_is_still_handled_by_its_reader(command, reader):
    """The reader for each socket still dispatches on the verb we emit.

    Verb-level, not argument-level: an argument-order change is pinned by the
    byte test above, but a verb the reader dropped is only visible here.
    """
    verb = command.split(" ", 1)[0]
    # The reader spells a bare verb `"VERB"` and an argument-taking one
    # `"VERB `; requiring that boundary keeps AUDIO from being satisfied by
    # AUDIO32's arm.
    handled = re.search(rf'"{re.escape(verb)}[" ]', reader.read_text())
    assert handled, f"{reader.name} no longer handles {verb}"
