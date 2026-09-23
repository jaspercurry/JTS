# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Grouping ring state projection and the doctor device probe (issue #2786)."""

from __future__ import annotations

import errno
import re
import subprocess
from pathlib import Path

import pytest

from jasper import ring_header
from jasper.cli.doctor import grouping as doctor_grouping
from jasper.cli.doctor.grouping import _GROUPING_PCM_PROBE
from jasper.multiroom import config as grouping_config, grouping_ring
from jasper.multiroom.grouping_ring import GROUPING_RING_FILE, GROUPING_RING_PCM
from jasper.multiroom.state import read_grouping_state
from jasper.ring_header import RING_FLOW_PRIMING, RING_FLOW_READER_STALLED

_REPO_ROOT = Path(__file__).resolve().parents[1]
_IOPLUG_C = _REPO_ROOT / "c" / "jts-ring-ioplug" / "pcm_jts_ring.c"

# --- C-2: the /state block -------------------------------------------------


#: A valid bonded follower — the role that actually reads the grouping ring.
_BONDED_FOLLOWER_ENV = (
    "JASPER_GROUPING=on\n"
    "JASPER_GROUPING_ROLE=follower\n"
    "JASPER_GROUPING_CHANNEL=left\n"
    "JASPER_GROUPING_BOND_ID=bond-1\n"
    "JASPER_GROUPING_LEADER_ADDR=10.0.0.9\n"
)


def _grouping_env(tmp_path: Path, body: str) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "grouping.env"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _bonded_snapshot(tmp_path, flow: ring_header.RingFlowState) -> dict:
    return read_grouping_state(
        _grouping_env(tmp_path, _BONDED_FOLLOWER_ENV),
        unit_state_reader=lambda units: {u: "active" for u in units},
        tap_path_reader=lambda: "/run/jasper-grouping/snapfifo",
        stream_clients_reader=lambda: [],
        endpoint_status_reader=lambda: {},
        ring_state_reader=lambda: flow,
    )


def test_a_bonded_snapshot_carries_the_ring_block(tmp_path):
    """Every key earns its place: ``state``/``detail`` are the answer, the two
    ages say which end and for how long, and the cursor pair is what a drop count
    would be derived from."""
    flow = ring_header.RingFlowState(
        state=RING_FLOW_READER_STALLED,
        detail="reader pid 202 stopped stamping its heartbeat",
        writer_age_ns=5_000_000,
        reader_age_ns=47_000_000_000,
        write_seq=900_000,
        read_seq=899_984,
        occupancy_slots=16,
        writer_epoch=3,
    )
    ring = _bonded_snapshot(tmp_path, flow)["ring"]

    assert ring == {
        "pcm": GROUPING_RING_PCM,
        "path": GROUPING_RING_FILE,
        "state": RING_FLOW_READER_STALLED,
        "detail": "reader pid 202 stopped stamping its heartbeat",
        "writer_age_ms": 5,
        "reader_age_ms": 47_000,
        "write_seq": 900_000,
        "read_seq": 899_984,
        "occupancy_slots": 16,
        "writer_epoch": 3,
    }


def test_a_never_stamped_heartbeat_stays_null_rather_than_zero(tmp_path):
    """``None`` and ``0 ms`` mean opposite things — never attached versus beating
    right now. The projection must not collapse them."""
    flow = ring_header.RingFlowState(state=RING_FLOW_PRIMING, reader_age_ns=None)
    ring = _bonded_snapshot(tmp_path, flow)["ring"]
    assert ring["reader_age_ms"] is None


def test_a_solo_snapshot_gains_no_ring_key_and_reads_no_ring(tmp_path):
    """The zero-cost-when-N=1 contract :mod:`jasper.multiroom.state` states for
    every other runtime block. A solo speaker is the overwhelming majority of the
    fleet and its snapshot must stay byte-for-byte what it was."""
    def _must_not_run() -> ring_header.RingFlowState:  # pragma: no cover
        raise AssertionError("a solo snapshot must not read the grouping ring")

    snapshot = read_grouping_state(
        _grouping_env(tmp_path, "JASPER_GROUPING=off\n"),
        ring_state_reader=_must_not_run,
    )
    assert "ring" not in snapshot
    assert snapshot["enabled"] is False


def test_a_failed_ring_read_nulls_only_its_own_block(tmp_path):
    """The aggregator's fail-soft rule, applied one level down: a dead source
    nulls its section and never errors the call, so a ring problem cannot take
    the grouping section — or the rest of ``/state`` — with it."""
    def _boom() -> ring_header.RingFlowState:
        raise OSError("shm gone")

    snapshot = read_grouping_state(
        _grouping_env(tmp_path, _BONDED_FOLLOWER_ENV),
        unit_state_reader=lambda units: {u: "active" for u in units},
        endpoint_status_reader=lambda: {},
        ring_state_reader=_boom,
    )
    assert snapshot["ring"] is None
    assert snapshot["runtime"]["health"] == "ok"


# --- C-3: the doctor's device probe ----------------------------------------


@pytest.fixture()
def _installed_confd(tmp_path, monkeypatch):
    """Point the check at a present conf.d file so the probe branch is reached."""
    conf = tmp_path / "62-jts-ring-grouping.conf"
    conf.write_text("pcm.jts_ring_grouping { type jts_ring }\n", encoding="utf-8")
    monkeypatch.setattr(grouping_ring, "GROUPING_RING_CONF_D", str(conf))
    return conf


def _check(monkeypatch, rc, reason="", *, bonded=False):
    from types import SimpleNamespace


    monkeypatch.setattr(
        doctor_grouping, "_probe_grouping_pcm", lambda pcm: (rc, reason)
    )
    # The check reads exactly one field off the config, so a stub carrying that
    # field keeps the test about severity rather than about config parsing.
    monkeypatch.setattr(
        grouping_config, "load_config", lambda *a, **k: SimpleNamespace(enabled=bonded)
    )
    return doctor_grouping.check_grouping_ring_device()


def test_a_resolving_pcm_is_ok(monkeypatch, _installed_confd):
    result = _check(monkeypatch, 0)
    assert result.status == "ok"


def test_a_name_that_does_not_resolve_fails_a_bonded_box(monkeypatch, _installed_confd):
    result = _check(monkeypatch, -errno.ENOENT, bonded=True)
    assert result.status == "fail"
    assert result.reason == doctor_grouping.REASON_RING_PCM_UNRESOLVED


def test_the_same_defect_is_a_warning_on_a_solo_box(monkeypatch, _installed_confd):
    """Weighed by what it costs THIS box, mirroring ``check_ring_platform_assets``:
    nothing opens the name on a solo speaker yet, so the defect is real but not
    load-bearing — and surfacing it here is the whole point, since it gets fixed
    before anyone tries to bond."""
    result = _check(monkeypatch, -errno.ENOENT, bonded=False)
    assert result.status == "warn"
    assert result.reason == doctor_grouping.REASON_RING_PCM_UNRESOLVED


def test_a_missing_confd_fails_before_probing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        grouping_ring, "GROUPING_RING_CONF_D", str(tmp_path / "absent.conf")
    )

    def _must_not_probe(pcm):  # pragma: no cover
        raise AssertionError("no point probing a name that is not declared")

    monkeypatch.setattr(doctor_grouping, "_probe_grouping_pcm", _must_not_probe)
    result = doctor_grouping.check_grouping_ring_device()
    assert result.status == "fail"
    assert result.reason == doctor_grouping.REASON_RING_CONFD_MISSING


def test_an_unrunnable_probe_warns_rather_than_accusing_the_pcm(
    monkeypatch, _installed_confd
):
    """No libasound on this host is a fact about the host, not a verdict about
    the device."""
    result = _check(monkeypatch, None, "libasound.so.2 unavailable", bonded=True)
    assert result.status == "warn"
    assert result.reason == doctor_grouping.REASON_RING_PROBE_UNAVAILABLE


def test_the_probe_child_opens_and_closes_and_does_nothing_else():
    """The snippet is the safety argument's other half: whatever the C source
    does at ``prepare``, this never calls it. Pinned so a later "let's just make
    it play a moment to be sure" cannot land quietly."""
    assert "snd_pcm_open" in _GROUPING_PCM_PROBE
    assert "snd_pcm_close" in _GROUPING_PCM_PROBE
    for forbidden in (
        "snd_pcm_prepare",
        "snd_pcm_hw_params",
        "snd_pcm_writei",
        "snd_pcm_readi",
        "snd_pcm_start",
    ):
        assert forbidden not in _GROUPING_PCM_PROBE, (
            f"the probe must not call {forbidden} — that is the path that "
            "attaches the SHM ring and would perturb a live bond"
        )


def test_the_ring_is_attached_only_from_the_prepare_callbacks():
    """WHY OPEN-AND-CLOSE IS SAFE, pinned against the C source it is a claim about.

    ``snd_pcm_open`` runs the plugin's define-func, which parses the conf.d block
    and calls ``snd_pcm_ioplug_create`` — it never touches the ring path as a
    file. The SHM is create-or-attached in the ``prepare`` callbacks, which an
    open-then-close never reaches. Move either attach call into the define-func
    (or into ``hw_params``) and the doctor's probe would start creating ring
    files, stamping a writer pid, and taking the writer flock on a box carrying
    live bonded audio — so this fails there instead.
    """
    source = _IOPLUG_C.read_text(encoding="utf-8")
    enclosing: dict[str, str] = {}
    current = ""
    for line in source.splitlines():
        function = re.match(r"^static\s+[\w\s\*]+?(\w+)\s*\(", line)
        if function:
            current = function.group(1)
        for call in ("jts_ring_writer_open(", "jts_ring_reader_open("):
            if call in line:
                enclosing[call] = current

    assert enclosing == {
        "jts_ring_writer_open(": "jts_ring_prepare",
        "jts_ring_reader_open(": "jts_ring_capture_prepare",
    }, f"the ring is attached from unexpected callbacks: {enclosing}"


def test_the_probe_is_isolated_and_bounded():
    """A child interpreter with a timeout, matching the audio_runtime ring
    probe's isolation posture: ``snd_pcm_open`` dlopens plugin code, and one
    faulting check must not cost the operator every other check's result."""
    calls: list[tuple[list[str], float]] = []

    def _fake_run(cmd, timeout=5.0):
        calls.append((cmd, timeout))
        return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")

    original = doctor_grouping._run
    doctor_grouping._run = _fake_run  # type: ignore[assignment]
    try:
        rc, reason = doctor_grouping._probe_grouping_pcm("jts_ring_grouping")
    finally:
        doctor_grouping._run = original  # type: ignore[assignment]

    assert (rc, reason) == (0, "")
    cmd, timeout = calls[0]
    assert cmd[-1] == "jts_ring_grouping"
    assert timeout == doctor_grouping._GROUPING_PCM_PROBE_TIMEOUT_SEC
    assert timeout > 0


def test_an_unparseable_probe_answer_is_not_read_as_success():
    def _fake_run(cmd, timeout=5.0):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Traceback ...")

    original = doctor_grouping._run
    doctor_grouping._run = _fake_run  # type: ignore[assignment]
    try:
        rc, reason = doctor_grouping._probe_grouping_pcm("jts_ring_grouping")
    finally:
        doctor_grouping._run = original  # type: ignore[assignment]

    assert rc is None
    assert "no result" in reason
