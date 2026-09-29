# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

import asyncio

import pytest

from tests._async_wait import wait_signalled
from tests.volume_coordinator_fixtures import (
    _coord,
    _FakeBackend,
    _FakeCamilla,
    _real_coord,
    pushes as pushes,
)

from jasper.assistant_volume import (
    EffectiveVolumeContext,
    volume_context_publisher_for_runtime,
)
from jasper.playback_state.music_sources import Source
from jasper.volume_coordinator import VolumeCoordinator
from jasper.volume_curve import percent_to_db
from jasper.volume_persistence import VolumePersistence


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


# ---------- VolumeContextPublication, driven through the coordinator -------


async def test_dispatch_publishes_absolute_canonical_and_downstream_facts(tmp_path):
    published = []

    async def publish(context):
        published.append(context)

    coord, _, _ = _coord(
        tmp_path,
        active={"spotactive": True},
        volume_context_publisher=publish,
    )

    await coord.set_listening_level(46)

    assert len(published) == 2
    assert all(
        context.canonical_db == pytest.approx(percent_to_db(46))
        for context in published
    )
    assert all(
        context.downstream_db == pytest.approx(0.0) for context in published
    )
    assert all(context.muted is False for context in published)


async def test_nonzero_intent_publishes_before_slow_spotify_dispatch(
    tmp_path, pushes,
):
    published = []
    cloud_started = asyncio.Event()
    release_cloud = asyncio.Event()

    async def publish(context):
        published.append(context)

    async def blocked_cloud(_source: Source, _level: int) -> None:
        cloud_started.set()
        await release_cloud.wait()

    coord, _, _ = _real_coord(
        tmp_path,
        active={"spotactive": True},
        volume_context_publisher=publish,
    )
    pushes.hook = blocked_cloud

    operation = asyncio.create_task(coord.set_listening_level(67))
    await wait_signalled(cloud_started, "spotify dispatch started", producer=operation)

    assert len(published) == 1
    assert published[0].canonical_db == pytest.approx(percent_to_db(67))
    assert published[0].muted is False

    release_cloud.set()
    assert await operation == 67
    assert len(published) == 2
    assert published[-1].canonical_db == pytest.approx(percent_to_db(67))
    assert published[-1].muted is False


@pytest.mark.parametrize("blocker", ["source_push", "camilla_mute"])
async def test_mute_intent_is_local_and_published_before_the_slow_write(
    tmp_path, pushes, blocker,
):
    """The mute is local intent. Whichever downstream write is slow — the
    Spotify cloud round trip or Camilla's own main_mute — the muted context
    is already published before it returns."""
    published = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def publish(context):
        published.append(context)

    coord, cam, _ = _real_coord(
        tmp_path,
        active={"spotactive": True} if blocker == "source_push" else {},
        db=percent_to_db(59),
        level=59,
        volume_context_publisher=publish,
    )
    if blocker == "source_push":
        async def blocked_cloud(_source: Source, _level: int) -> None:
            started.set()
            await release.wait()

        pushes.hook = blocked_cloud
    else:
        first_call = True

        async def blocked_set_mute(_target: bool) -> None:
            nonlocal first_call
            if first_call:
                first_call = False
                started.set()
                await release.wait()

        cam.mute_hook = blocked_set_mute

    operation = asyncio.create_task(coord.mute())
    await wait_signalled(started, "slow downstream write started", producer=operation)

    if blocker == "source_push":
        # Camilla's mute has already landed; only the source push is slow.
        assert cam.muted is True
    assert len(published) == 1
    assert published[0].muted is True

    release.set()
    assert await operation == 59
    assert len(published) == 2
    assert published[-1].muted is True


async def test_overlapping_push_writes_keep_source_persistence_and_context_aligned(
    tmp_path, pushes,
):
    persistence = VolumePersistence(str(tmp_path / "speaker_volume.json"))
    persistence.save_listening_level(50)
    cam = _FakeCamilla(db=0.0)
    backend = _FakeBackend(active={"spotactive": True})
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    applied = []
    published = []

    async def push(_source: Source, level: int) -> None:
        if level == 20:
            first_started.set()
            await release_first.wait()
        applied.append(level)

    async def publish(context):
        published.append(context)

    first = VolumeCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=backend,
        volume_context_publisher=publish,
    )
    second = VolumeCoordinator(
        camilla=cam,
        persistence=persistence,
        backend=backend,
        volume_context_publisher=publish,
    )
    pushes.hook = push

    older = asyncio.create_task(first.set_listening_level(20))
    await wait_signalled(first_started, "older push write started", producer=older)
    newer = asyncio.create_task(second.set_listening_level(80))
    await asyncio.sleep(0)
    assert newer.done() is False
    release_first.set()
    assert await older == 20
    assert await newer == 80

    record = persistence.load()
    assert record is not None
    newest_context = max(published, key=lambda context: context.stamp_boot_ns)
    assert applied[-1] == 80
    assert record.listening_level == 80
    assert newest_context.canonical_db == pytest.approx(percent_to_db(80))


@pytest.mark.parametrize("camilla_readable", [True, False])
async def test_persisted_mute_intent_outranks_what_camilla_reports(
    tmp_path, camilla_readable,
):
    """A stale unmuted readback — or no readback at all — cannot resurrect
    audio the owner muted."""
    coord, cam, persistence = _real_coord(
        tmp_path, active={}, db=percent_to_db(59), level=59,
    )
    persistence.save_mute_state(59, "remote-mute")
    cam.muted = False
    cam.unavailable = not camilla_readable

    context = await coord.effective_volume_context()

    assert context.muted is True
    if camilla_readable:
        assert context.canonical_db == pytest.approx(percent_to_db(59))


async def test_unmute_and_push_mode_nonzero_publish_unmuted_context(tmp_path):
    published = []

    async def publish(context):
        published.append(context)

    coord, cam, persistence = _real_coord(
        tmp_path,
        active={},
        db=percent_to_db(59),
        level=59,
        volume_context_publisher=publish,
    )
    persistence.save_mute_state(59, "remote-mute")
    cam.muted = True

    await coord.unmute()
    assert published[-1].muted is False

    push = VolumeCoordinator(
        camilla=_FakeCamilla(db=0.0),
        persistence=persistence,
        backend=_FakeBackend(active={"spotactive": True}),
    )
    assert (await push.effective_volume_context()).muted is False


async def test_publisher_failure_never_breaks_volume_operation(tmp_path):
    async def fail(_context):
        raise OSError("fanin unavailable")

    coord, _, _ = _coord(tmp_path, volume_context_publisher=fail)
    assert await coord.set_listening_level(47) == 47


async def test_context_snapshot_retries_after_concurrent_volume_change(tmp_path):
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=percent_to_db(30), level=30,
    )
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    first = True

    async def blocked_read():
        nonlocal first
        if first:
            first = False
            read_started.set()
            await release_read.wait()
        return None

    cam.read_hook = blocked_read
    snapshot = asyncio.create_task(coord.effective_volume_context())
    await wait_signalled(
        read_started, "camilla volume/mute read started", producer=snapshot,
    )
    await coord.set_listening_level(80)
    release_read.set()
    context = await snapshot

    assert context.canonical_db == pytest.approx(percent_to_db(80))
    assert context.downstream_db == pytest.approx(percent_to_db(80))


async def test_context_snapshot_stamp_is_bound_before_slow_probe(
    tmp_path, monkeypatch,
):
    coord, cam, _ = _real_coord(
        tmp_path, active={}, db=percent_to_db(30), level=30,
    )
    stamp_bound = False

    def bind_stamp():
        nonlocal stamp_bound
        stamp_bound = True
        return 123

    async def verify_stamp_precedes_probe():
        assert stamp_bound is True
        return None

    monkeypatch.setattr(
        "jasper.assistant_volume.volume_context_stamp_boot_ns", bind_stamp,
    )
    cam.read_hook = verify_stamp_precedes_probe

    context = await coord.effective_volume_context()

    assert context.stamp_boot_ns == 123
