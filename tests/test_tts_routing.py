# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pre-DSP vs post-DSP TTS-route classification (issue #1547).

Outputd interprets ``VOLUME_CONTEXT`` (a first-class post-DSP consumer), so
the producer must publish the same wire message to a CONFIRMED post-DSP member.
These tests pin the classifier logic that both the coordinator publisher and
the voice daemon's PREPARE_ASSISTANT gate share.
"""

import pytest

from jasper.tts_routing import (
    resolved_tts_socket_feeds_post_dsp_outputd,
    resolved_tts_socket_feeds_pre_dsp_fanin,
    tts_socket_feeds_post_dsp_outputd,
    tts_socket_feeds_pre_dsp_fanin,
)

_OUTPUTD_SOCKET = "/run/jasper-outputd/tts.sock"


@pytest.mark.parametrize(
    ("resolved", "pre_dsp", "post_dsp"),
    [
        ({"JASPER_TTS_MIX_STAGE": "post_dsp"}, False, True),
        ({"JASPER_TTS_MIX_STAGE": "pre_dsp"}, True, False),
        # Unknown stage fails closed both ways: pre-DSP compensation into an
        # uncertain mix stage is a large level error.
        ({"JASPER_TTS_MIX_STAGE": "sideways"}, False, False),
        # Solo default: no socket, no stage.
        ({}, True, False),
        ({"JASPER_TTS_OUTPUTD_SOCKET": _OUTPUTD_SOCKET}, False, False),
    ],
)
def test_stage_classification(resolved, pre_dsp, post_dsp):
    assert resolved_tts_socket_feeds_pre_dsp_fanin(resolved) is pre_dsp
    assert resolved_tts_socket_feeds_post_dsp_outputd(resolved) is post_dsp


@pytest.mark.parametrize(
    ("grouping_file", "pre_dsp", "post_dsp"),
    [
        (
            f"JASPER_TTS_MIX_STAGE=post_dsp\nJASPER_TTS_OUTPUTD_SOCKET={_OUTPUTD_SOCKET}\n",
            False,
            True,
        ),
        ("", True, False),
    ],
)
def test_env_reader_layers_the_grouping_file(
    tmp_path, grouping_file, pre_dsp, post_dsp,
):
    path = tmp_path / "grouping-voice.env"
    path.write_text(grouping_file)
    assert tts_socket_feeds_pre_dsp_fanin({}, grouping_env_path=str(path)) is pre_dsp
    assert (
        tts_socket_feeds_post_dsp_outputd({}, grouping_env_path=str(path)) is post_dsp
    )
