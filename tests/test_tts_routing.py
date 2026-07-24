# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pre-DSP vs post-DSP TTS-route classification (issue #1547).

Outputd now interprets ``VOLUME_CONTEXT`` (a first-class post-DSP consumer), so
the producer must publish the same wire message to a CONFIRMED post-DSP member.
These tests pin the classifier logic that both the coordinator publisher and
the voice daemon's PREPARE_ASSISTANT gate share.
"""

from jasper.tts_routing import (
    resolved_tts_socket_feeds_post_dsp_outputd,
    resolved_tts_socket_feeds_pre_dsp_fanin,
    tts_socket_feeds_post_dsp_outputd,
    tts_socket_feeds_pre_dsp_fanin,
)


def test_explicit_post_dsp_stage_is_post_dsp_only():
    resolved = {"JASPER_TTS_MIX_STAGE": "post_dsp"}
    assert resolved_tts_socket_feeds_post_dsp_outputd(
        resolved, grouping_socket_override=False
    )
    assert not resolved_tts_socket_feeds_pre_dsp_fanin(
        resolved, grouping_socket_override=False
    )
    # The override flag does not change a stage-explicit classification.
    assert resolved_tts_socket_feeds_post_dsp_outputd(
        resolved, grouping_socket_override=True
    )


def test_explicit_pre_dsp_stage_is_pre_dsp_only():
    resolved = {"JASPER_TTS_MIX_STAGE": "pre_dsp"}
    assert resolved_tts_socket_feeds_pre_dsp_fanin(
        resolved, grouping_socket_override=True
    )
    assert not resolved_tts_socket_feeds_post_dsp_outputd(
        resolved, grouping_socket_override=True
    )


def test_missing_stage_is_never_post_dsp():
    # Solo default (no socket, no stage) is pre-DSP fan-in, not post-DSP.
    resolved: dict[str, str] = {}
    assert resolved_tts_socket_feeds_pre_dsp_fanin(
        resolved, grouping_socket_override=False
    )
    assert not resolved_tts_socket_feeds_post_dsp_outputd(
        resolved, grouping_socket_override=False
    )


def test_legacy_socket_only_override_fails_closed_both_ways():
    # A grouping file that carries only the socket (no stage) is ambiguous:
    # neither classifier claims it, so no pre-DSP compensation is published to a
    # possibly-post-DSP mixer during an upgrade window.
    resolved = {"JASPER_TTS_OUTPUTD_SOCKET": "/run/jasper-outputd/tts.sock"}
    assert not resolved_tts_socket_feeds_pre_dsp_fanin(
        resolved, grouping_socket_override=True
    )
    assert not resolved_tts_socket_feeds_post_dsp_outputd(
        resolved, grouping_socket_override=True
    )


def test_env_reader_classifies_post_dsp_and_legacy(tmp_path):
    confirmed = tmp_path / "grouping-voice.env"
    confirmed.write_text(
        "JASPER_TTS_MIX_STAGE=post_dsp\n"
        "JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-outputd/tts.sock\n"
    )
    assert tts_socket_feeds_post_dsp_outputd({}, grouping_env_path=str(confirmed))
    assert not tts_socket_feeds_pre_dsp_fanin({}, grouping_env_path=str(confirmed))

    legacy = tmp_path / "legacy.env"
    legacy.write_text("JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-outputd/tts.sock\n")
    assert not tts_socket_feeds_post_dsp_outputd({}, grouping_env_path=str(legacy))
    assert not tts_socket_feeds_pre_dsp_fanin({}, grouping_env_path=str(legacy))

    solo = tmp_path / "solo.env"
    solo.write_text("")
    assert not tts_socket_feeds_post_dsp_outputd({}, grouping_env_path=str(solo))
    assert tts_socket_feeds_pre_dsp_fanin({}, grouping_env_path=str(solo))
