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
        stamp_boot_ns=123,
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
    assert volume_context_publisher_for_runtime({
        "JASPER_DUCK_TRANSPORT": "fanin",
        "JASPER_TTS_OUTPUTD_SOCKET": "/tmp/custom-tts.sock",
    }) is None
    assert volume_context_publisher_for_runtime({
        "JASPER_DUCK_TRANSPORT": "fanin",
        "JASPER_TTS_OUTPUTD_SOCKET": "/tmp/custom-tts.sock",
        "JASPER_TTS_MIX_STAGE": "pre_dsp",
    }) is not None


def test_runtime_publisher_fails_closed_for_legacy_socket_only_grouping(
    tmp_path,
):
    grouping_env = tmp_path / "grouping-voice.env"
    grouping_env.write_text(
        "JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-outputd/tts.sock\n"
    )

    assert volume_context_publisher_for_runtime(
        {"JASPER_DUCK_TRANSPORT": "fanin"},
        grouping_env_path=str(grouping_env),
    ) is None


def test_dynamic_runtime_publisher_tracks_grouping_file(
    monkeypatch, tmp_path,
):
    sent = []
    parse_calls = 0

    def fake_send(path, context, *, timeout=0.5):
        sent.append((path, context, timeout))

    monkeypatch.setattr("jasper.assistant_volume._send_volume_context", fake_send)
    from jasper import env_load

    real_parse = env_load.parse_env_file

    def counted_parse(path):
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(path)

    monkeypatch.setattr(env_load, "parse_env_file", counted_parse)
    grouping_env = tmp_path / "grouping-voice.env"
    grouping_env.write_text("JASPER_TTS_MIX_STAGE=post_dsp\n")
    publisher = volume_context_publisher_for_runtime(
        {"JASPER_DUCK_TRANSPORT": "fanin"},
        grouping_env_path=str(grouping_env),
        dynamic_topology=True,
    )
    assert publisher is not None
    context = EffectiveVolumeContext(-30.0, 0.0, -41.0, False, 123)

    asyncio.run(publisher(context))
    assert sent == []
    assert parse_calls == 1

    grouping_env.write_text(
        "JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-outputd/tts.sock\n"
    )
    asyncio.run(publisher(context))
    assert sent == []
    assert parse_calls == 2

    grouping_env.write_text("")
    asyncio.run(publisher(context))
    assert sent == [("/run/jasper-fanin/tts.sock", context, 0.5)]
    assert parse_calls == 3


def test_snapshot_stamp_survives_delayed_out_of_order_serialization():
    from jasper.assistant_volume import serialize_volume_context

    older = EffectiveVolumeContext(-30.0, 0.0, -41.0, False, 100)
    newer = EffectiveVolumeContext(-24.0, 0.0, -39.4, False, 200)

    # Model fan-in's monotonic acceptance after the newer snapshot publishes
    # first and the older publisher wakes later. Serialization must preserve
    # acquisition order rather than assigning a fresh send-time stamp.
    accepted = None
    accepted_stamp = 0
    for context in (newer, older):
        payload = serialize_volume_context(context)
        stamp = int(payload.split()[-1])
        if stamp >= accepted_stamp:
            accepted = context
            accepted_stamp = stamp

    assert accepted is newer
    assert accepted_stamp == 200
