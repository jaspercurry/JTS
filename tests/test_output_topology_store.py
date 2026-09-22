# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from jasper import output_topology_store as output_topology_mod
from jasper.camilla_emit import BASS_MANAGEMENT_CORNER_HZ_DEFAULT
from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology, OutputTopologyError, new_topology_draft
from jasper.output_topology_store import (
    bass_management_corner_hz,
    clear_topology_fingerprint_stamp,
    load_output_topology,
    load_output_topology_snapshot,
    load_output_topology_strict,
    read_topology_fingerprint_stamp,
    save_output_topology,
    topology_fingerprint_stamp,
    write_topology_fingerprint_stamp,
)
from tests._log_events import event_fields, event_records
from tests.output_topology_fixtures import (
    _base_hardware,
    _fingerprint_topology,
    _passive_main,
    _passive_sub_topology_raw,
    _topology,
)


@pytest.mark.parametrize("stored", [
    "present", "absent", "required_missing", "software_guard_requested",
    "not_required", "unknown",
])
def test_stored_tweeter_protection_status_loads_and_is_dropped(tmp_path, stored):
    raw = _topology(groups=[{
        "id": "mono", "label": "Mono", "kind": "mono", "mode": "active_2_way",
        "channels": [
            {"role": "woofer", "physical_output_index": 0},
            {"role": "tweeter", "physical_output_index": 1},
        ],
    }]).to_dict()
    raw["speaker_groups"][0]["channels"][1]["protection_status"] = stored
    path = tmp_path / "topology.json"
    path.write_text(json.dumps(raw))
    before = path.read_bytes()

    loaded = load_output_topology_strict(path)

    assert loaded.status == "valid"
    assert loaded.schema_version == 1
    assert "protection_status" not in (
        loaded.to_dict()["speaker_groups"][0]["channels"][1]
    )
    assert path.read_bytes() == before


def test_save_and_load_output_topology_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "output_topology.json"
    topology = _topology(groups=[
        {
            "id": "mono",
            "label": "Mono speaker",
            "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 2}],
        }
    ])

    save_output_topology(topology, path)
    loaded = load_output_topology(path)

    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == (
        OUTPUT_TOPOLOGY_KIND
    )
    assert loaded.topology_id == "living_room"
    assert loaded.to_dict()["speaker_groups"][0]["channels"][0][
        "human_output_label"
    ] == "DAC output 3"


def test_save_output_topology_cleans_temp_file_on_replace_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "output_topology.json"
    topology = _topology(groups=[
        {
            "id": "mono",
            "label": "Mono speaker",
            "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 2}],
        }
    ])

    def fail_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("jasper.atomic_io.os.replace", fail_replace)

    with pytest.raises(OSError):
        save_output_topology(topology, path)

    assert not path.exists()
    assert list(tmp_path.glob(".output_topology.json.*.tmp")) == []


def test_save_output_topology_publishes_group_readable_0640(
    tmp_path: Path,
) -> None:
    # /var/lib/jasper is group jasper but not setgid, so the non-root
    # jasper-group management daemons read this file by its group bits.
    path = tmp_path / "output_topology.json"
    topology = _topology(groups=[
        {
            "id": "mono",
            "label": "Mono speaker",
            "kind": "mono",
            "mode": "full_range_passive",
            "channels": [{"role": "full_range", "physical_output_index": 2}],
        }
    ])

    save_output_topology(topology, path)

    assert path.stat().st_mode & 0o777 == 0o640
    assert json.loads(path.read_text(encoding="utf-8"))["kind"] == OUTPUT_TOPOLOGY_KIND


def test_load_output_topology_fails_soft_to_detected_draft(tmp_path: Path) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    loaded = load_output_topology(path)

    assert loaded.status == "draft"
    assert loaded.speaker_groups == ()


# --------------------------------------------------------------------------- #
# Fail-soft load WARN is rate-limited (#2140).
#
# `load_output_topology` is called every 60 s by jasper-control's audio_health
# route sampler, so an unguarded WARN is ~1,440 identical journal lines/day in
# an already-degraded state. What is pinned here is the promise: transitions
# and due reminders are logged, steady repeats are not.
#
# Each test uses its own `tmp_path`, which is also the rate-limiter's key, so
# these need no reset of the module-level state.
# --------------------------------------------------------------------------- #


def test_load_output_topology_warns_once_for_a_repeated_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="jasper.output_topology_store"):
        for _ in range(5):
            assert load_output_topology(path).status == "draft"

    fields = event_fields(caplog, "output_topology.load_failed")
    assert fields["repeat_suppression_sec"] == "3600"


def test_load_output_topology_rewarns_when_the_failure_changes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="jasper.output_topology_store"):
        load_output_topology(path)
        load_output_topology(path)
        # A different corruption is a different failure: it must not inherit
        # the suppression window of the one before it.
        path.write_text(json.dumps({"kind": "not_a_topology"}), encoding="utf-8")
        load_output_topology(path)

    assert len(event_records(caplog, "output_topology.load_failed")) == 2


def test_load_output_topology_rewarns_after_the_reminder_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")
    clock = [1_000.0]
    monkeypatch.setattr(output_topology_mod, "_now", lambda: clock[0])

    with caplog.at_level(logging.WARNING, logger="jasper.output_topology_store"):
        load_output_topology(path)
        clock[0] += output_topology_mod.LOAD_FAILURE_REMINDER_SEC - 1.0
        load_output_topology(path)
        assert len(event_records(caplog, "output_topology.load_failed")) == 1
        clock[0] += 1.0
        load_output_topology(path)

    assert len(event_records(caplog, "output_topology.load_failed")) == 2


def test_load_output_topology_logs_recovery_once_after_a_logged_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="jasper.output_topology_store"):
        load_output_topology(path)
        save_output_topology(new_topology_draft(), path)
        load_output_topology(path)
        load_output_topology(path)

    assert len(event_records(caplog, "output_topology.load_recovered")) == 1


def test_load_output_topology_is_silent_when_it_never_failed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "output_topology.json"
    save_output_topology(new_topology_draft(), path)

    with caplog.at_level(logging.INFO, logger="jasper.output_topology_store"):
        load_output_topology(path)
        load_output_topology(path)

    assert not event_records(caplog, "output_topology.load_failed")
    assert not event_records(caplog, "output_topology.load_recovered")


def test_load_failure_state_is_bounded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A caller passing many paths cannot grow the limiter without bound."""
    for index in range(output_topology_mod._LOAD_FAILURE_STATE_MAX + 4):
        path = tmp_path / f"topology-{index}.json"
        path.write_text("{not json", encoding="utf-8")
        load_output_topology(path)

    assert (
        output_topology_mod._load_failures.tracked()
        <= output_topology_mod._LOAD_FAILURE_STATE_MAX
    )


@pytest.fixture(params=[load_output_topology_strict, load_output_topology_snapshot])
def loader(request: pytest.FixtureRequest):
    return request.param


def test_load_output_topology_strict_rejects_corrupt_state(
    tmp_path: Path, loader
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(OutputTopologyError):
        loader(path)


def test_load_output_topology_strict_rejects_non_utf8_bytes(
    tmp_path: Path, loader
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_bytes(b'{"kind": "\xff\xfe not utf-8"}')

    with pytest.raises(OutputTopologyError):
        loader(path)


def test_strict_loaders_reject_unreadable_file(tmp_path: Path, loader) -> None:
    path = tmp_path / "output_topology.json"
    save_output_topology(new_topology_draft(), path)
    with patch.object(Path, "open", side_effect=PermissionError):
        with pytest.raises(OutputTopologyError):
            loader(path)


@pytest.mark.parametrize("device_id", [5, True, 1.5, ["hifiberry_dac8x"], {"id": 1}])
def test_load_output_topology_strict_rejects_a_non_string_device_id(
    tmp_path: Path, device_id: object, loader
) -> None:
    path = tmp_path / "output_topology.json"
    path.write_text(
        json.dumps({
            "artifact_schema_version": 1,
            "kind": OUTPUT_TOPOLOGY_KIND,
            "topology_id": "living_room",
            "name": "Living room",
            "status": "draft",
            "hardware": {**_base_hardware(), "device_id": device_id},
            "speaker_groups": [],
            "routing": {},
        }),
        encoding="utf-8",
    )

    with pytest.raises(OutputTopologyError):
        loader(path)


def test_load_output_topology_strict_allows_missing_as_unconfigured(
    tmp_path: Path, loader
) -> None:
    loaded = loader(tmp_path / "missing.json")
    if isinstance(loaded, output_topology_mod.OutputTopologySnapshot):
        assert loaded.revision == "missing"
        loaded = loaded.topology
    assert loaded.status == "draft"
    assert loaded.speaker_groups == ()


def test_topology_snapshot_revision_hashes_the_exact_loaded_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "output_topology.json"
    topology = new_topology_draft(name="Exact bytes")
    save_output_topology(topology, path)
    data = path.read_bytes()

    snapshot = load_output_topology_snapshot(path)

    assert snapshot.topology == topology
    assert snapshot.revision == "sha256:" + hashlib.sha256(data).hexdigest()


def test_mutation_save_returns_revision_without_post_write_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "output_topology.json"
    topology = new_topology_draft(name="Published once")

    with output_topology_mod.output_topology_mutation(path) as mutation:
        monkeypatch.setattr(
            output_topology_mod,
            "load_output_topology_snapshot",
            lambda *_args, **_kwargs: pytest.fail(
                "save must not read after publication"
            ),
        )
        revision = mutation.save(topology)

    data = path.read_bytes()
    assert revision == "sha256:" + hashlib.sha256(data).hexdigest()
    assert json.loads(data)["name"] == "Published once"


@pytest.mark.parametrize("legacy_verified", [True, False])
def test_stored_topology_drops_legacy_channel_identity(
    tmp_path: Path, legacy_verified: bool,
) -> None:
    raw = _topology(groups=[_passive_main("mono", "mono", 0)]).to_dict()
    raw["speaker_groups"][0]["channels"][0]["identity_verified"] = legacy_verified
    path = tmp_path / "output_topology.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = load_output_topology_strict(path)

    del raw["speaker_groups"][0]["channels"][0]["identity_verified"]
    assert loaded.to_dict() == OutputTopology.from_mapping(raw).to_dict()


def test_topology_publication_uses_durable_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture(path, data, **kwargs):
        captured.update(path=path, data=data, kwargs=kwargs)

    monkeypatch.setattr(output_topology_mod, "atomic_write_text", capture)

    save_output_topology(new_topology_draft(), tmp_path / "topology.json")

    assert captured["kwargs"]["durable"] is True


def _subless_topology_raw() -> dict:
    raw = _passive_sub_topology_raw(120.0)
    raw["speaker_groups"] = raw["speaker_groups"][:2]
    raw["routing"].pop("subwoofer_group_ids")
    return raw


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (json.dumps(_passive_sub_topology_raw(120.0)), 120.0),
        (
            json.dumps(_passive_sub_topology_raw(None)),
            BASS_MANAGEMENT_CORNER_HZ_DEFAULT,
        ),
        (json.dumps(_subless_topology_raw()), None),
        ('{"kind": "jts_output_', None),
    ],
    ids=["declared corner", "default corner", "no subwoofer", "unreadable"],
)
def test_bass_management_corner_resolves_from_the_persisted_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str,
    expected: float | None,
) -> None:
    """Fail-soft: an unreadable topology resolves to "no subwoofer", never a
    raise — a room correction and a display both depend on that."""
    target = tmp_path / "topology.json"
    target.write_text(text)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(target))

    assert bass_management_corner_hz() == expected


def test_the_stamp_version_moves_with_the_fingerprint_projection() -> None:
    """The canary the whole bump discipline rests on: jasper-camilla's gate
    compares stamps only within one version, so a projection change that kept
    the old version would compare two hashes of different things and read a code
    change as a wiring change. This literal moving means the projection moved —
    bump `TOPOLOGY_STAMP_VERSION` with it, do not re-pin alone."""
    assert topology_fingerprint_stamp(_fingerprint_topology()) == (
        "v1:d19c8e21ed816bd01714eec33475c7d435ed97bbcbd4392eb144bfebc0badcc9"
    )


def test_a_stamp_nobody_could_write_or_retire_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Both halves are best-effort on the boot path, and both end in "unknown",
    which the gate ALLOWS — so the journal line is the only place a box that has
    gone blind is visible. It must not be an exception either way."""
    caplog.set_level(logging.INFO)
    unwritable = tmp_path / "readonly"
    unwritable.mkdir()
    stamp = unwritable / "s.topology"
    stamp.write_text("a" * 64 + "\n", encoding="utf-8")
    unwritable.chmod(0o500)

    try:
        assert write_topology_fingerprint_stamp(stamp, "b" * 64) is False
        assert clear_topology_fingerprint_stamp(stamp) is False
    finally:
        unwritable.chmod(0o700)

    # Absent is retired, not a failure: the steady state after a clean apply.
    assert clear_topology_fingerprint_stamp(tmp_path / "never-existed") is True

    assert event_records(caplog, "camilla_topology_stamp.write_failed")
    assert event_records(caplog, "camilla_topology_stamp.clear_failed")


def test_an_unreadable_or_empty_stamp_reads_as_unknown(tmp_path: Path) -> None:
    """Unknown, never a wrong answer: the gate allows on None and refuses only
    on two known, different values."""
    assert read_topology_fingerprint_stamp(tmp_path / "absent") is None
    (tmp_path / "empty").write_text("\n", encoding="utf-8")
    assert read_topology_fingerprint_stamp(tmp_path / "empty") is None
    assert read_topology_fingerprint_stamp(tmp_path) is None
