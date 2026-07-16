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
        muted=False,
    )

    asyncio.run(make_volume_context_publisher("/tmp/fanin.sock")(context))

    assert calls == [("/tmp/fanin.sock", context, 0.5)]


def test_runtime_publisher_is_scoped_to_fanin_topology():
    assert volume_context_publisher_for_runtime({"JASPER_DUCK_TRANSPORT": "camilla"}) is None
    assert volume_context_publisher_for_runtime({"JASPER_DUCK_TRANSPORT": "fanin"}) is not None
