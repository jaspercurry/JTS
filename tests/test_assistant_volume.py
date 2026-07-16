# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import asyncio

from jasper.assistant_volume import (
    EffectiveVolumeContext,
    make_volume_context_publisher,
    volume_context_publisher_for_runtime,
)


def test_volume_context_publisher_sends_one_absolute_idempotent_message(monkeypatch):
    calls = []

    def fake_send(path, context, *, timeout=0.5):
        calls.append((path, context, timeout))

    monkeypatch.setattr("jasper.assistant_volume._send_volume_context", fake_send)
    context = EffectiveVolumeContext(
        canonical_db=-30.0,
        downstream_db=0.0,
        tts_envelope_lufs=-41.0,
        muted=False,
    )

    asyncio.run(make_volume_context_publisher("/tmp/fanin.sock")(context))

    assert calls == [("/tmp/fanin.sock", context, 0.5)]


def test_runtime_publisher_is_scoped_to_fanin_topology():
    assert volume_context_publisher_for_runtime({"JASPER_DUCK_TRANSPORT": "camilla"}) is None
    assert volume_context_publisher_for_runtime({"JASPER_DUCK_TRANSPORT": "fanin"}) is not None
    assert volume_context_publisher_for_runtime({
        "JASPER_DUCK_TRANSPORT": "fanin",
        "JASPER_TTS_MIX_STAGE": "post_dsp",
    }) is None


def test_dynamic_runtime_publisher_tracks_grouping_file(
    monkeypatch, tmp_path,
):
    sent = []

    def fake_send(path, context, *, timeout=0.5):
        sent.append((path, context, timeout))

    monkeypatch.setattr("jasper.assistant_volume._send_volume_context", fake_send)
    grouping_env = tmp_path / "grouping-voice.env"
    grouping_env.write_text("JASPER_TTS_MIX_STAGE=post_dsp\n")
    publisher = volume_context_publisher_for_runtime(
        {"JASPER_DUCK_TRANSPORT": "fanin"},
        grouping_env_path=str(grouping_env),
        dynamic_topology=True,
    )
    assert publisher is not None
    context = EffectiveVolumeContext(-30.0, 0.0, -41.0, False)

    asyncio.run(publisher(context))
    assert sent == []

    grouping_env.write_text("")
    asyncio.run(publisher(context))
    assert sent == [("/run/jasper-fanin/tts.sock", context, 0.5)]
