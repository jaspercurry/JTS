# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import asyncio

import pytest

from jasper.assistant_volume import (
    EffectiveVolumeContext,
    volume_context_publisher_for_runtime,
)


@pytest.mark.parametrize(
    ("env", "expected_socket"),
    [
        # Solo default: pre-DSP fan-in.
        ({}, "/run/jasper-fanin/tts.sock"),
        # Confirmed post-DSP member: the SAME wire message, outputd's socket.
        (
            {
                "JASPER_TTS_MIX_STAGE": "post_dsp",
                "JASPER_TTS_OUTPUTD_SOCKET": "/run/jasper-outputd/tts.sock",
            },
            "/run/jasper-outputd/tts.sock",
        ),
        ({"JASPER_TTS_MIX_STAGE": "pre_dsp"}, "/run/jasper-fanin/tts.sock"),
        # A socket that names no mix stage publishes nothing: pre-DSP
        # compensation into an uncertain stage is a large level error.
        ({"JASPER_TTS_OUTPUTD_SOCKET": "/tmp/custom-tts.sock"}, None),
        ({"JASPER_TTS_MIX_STAGE": "sideways"}, None),
    ],
)
def test_runtime_publisher_is_scoped_to_context_consuming_routes(
    monkeypatch, env, expected_socket,
):
    calls = []

    def fake_send(path, context, *, timeout=0.5):
        calls.append((path, context, timeout))

    monkeypatch.setattr("jasper.assistant_volume._send_volume_context", fake_send)
    context = EffectiveVolumeContext(
        canonical_db=-30.0,
        downstream_db=-30.0,
        tts_envelope_lufs=-41.0,
        muted=False,
        stamp_boot_ns=123,
    )

    asyncio.run(volume_context_publisher_for_runtime(env)(context))

    assert calls == (
        [] if expected_socket is None else [(expected_socket, context, 0.5)]
    )
    # downstream_db is NOT mutated to 0 in Python — the structural-zero fact
    # belongs to the post-DSP consumer.
    assert context.downstream_db == -30.0


def test_runtime_publisher_rereads_the_grouping_file_every_publish(
    monkeypatch, tmp_path,
):
    """The reconciler rewrites this file while the daemon runs, so a route
    frozen at construction would keep publishing to a stale mix stage."""
    sent = []
    parse_calls = 0

    def fake_send(path, context, *, timeout=0.5):
        sent.append((path, context, timeout))

    monkeypatch.setattr("jasper.assistant_volume._send_volume_context", fake_send)
    from jasper import tts_routing

    real_parse = tts_routing.parse_env_file

    def counted_parse(path):
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(path)

    monkeypatch.setattr(tts_routing, "parse_env_file", counted_parse)
    grouping_env = tmp_path / "grouping-voice.env"
    # Confirmed post-DSP member (stage + outputd socket): the same wire message
    # now flows to outputd.
    grouping_env.write_text(
        "JASPER_TTS_MIX_STAGE=post_dsp\n"
        "JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-outputd/tts.sock\n"
    )
    publisher = volume_context_publisher_for_runtime(
        {},
        grouping_env_path=str(grouping_env),
    )
    context = EffectiveVolumeContext(-30.0, 0.0, -41.0, False, 123)

    asyncio.run(publisher(context))
    assert sent == [("/run/jasper-outputd/tts.sock", context, 0.5)]
    assert parse_calls == 1

    # A socket with no stage names no mix stage → fail closed; no new send.
    grouping_env.write_text(
        "JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-outputd/tts.sock\n"
    )
    asyncio.run(publisher(context))
    assert sent == [("/run/jasper-outputd/tts.sock", context, 0.5)]
    assert parse_calls == 2

    # Back to solo (empty) → pre-DSP fan-in.
    grouping_env.write_text("")
    asyncio.run(publisher(context))
    assert sent == [
        ("/run/jasper-outputd/tts.sock", context, 0.5),
        ("/run/jasper-fanin/tts.sock", context, 0.5),
    ]
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
