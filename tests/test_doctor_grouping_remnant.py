# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The grouping-remnant guard (#2285 P9-C).

Pins the contract that snd-aloop is retained ONLY for the bonded grouping
round-trip on pair 6, and that anything holding a substream with no registered
purpose in this phase — pair 5, whose PCM definitions P9-C deleted — is a FAIL
that names the offender.

Two of these tests are the load-bearing ones:

* ``test_positive_control_*`` — the design's required positive control: a
  deliberately-opened foreign substream must trip the check. A guard with no
  positive control guards nothing.
* ``test_registered_open_pair_is_ok_jts4_shape`` — the false-positive
  regression. jts4 (a HEALTHY box) holds pcm1c/sub3 RUNNING as fan-in's
  documented usbsink idle-read fallback; a guard that failed there would train
  operators to ignore the doctor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper.cli.doctor import grouping

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MODPROBE_CONF = _REPO_ROOT / "deploy" / "modprobe.d" / "snd-aloop.conf"
_RECONCILE_PY = _REPO_ROOT / "jasper" / "multiroom" / "reconcile.py"

_OPEN_STATUS = (
    "state: RUNNING\n"
    "owner_pid   : 4242\n"
    "trigger_time: 10741.879940354\n"
    "tstamp      : 0.000000000\n"
    "delay       : 320\n"
)


def _make_card(tmp_path: Path, open_subs: dict[str, list[int]] | None = None) -> Path:
    """Build a synthetic /proc/asound tree with an snd-aloop card.

    ``open_subs`` maps a pcm dir (``pcm0p``/``pcm1c``/…) to the substream
    indices that should read as open; every other substream reads ``closed``,
    exactly as the kernel prints it.
    """
    open_subs = open_subs or {}
    root = tmp_path / "asound"
    card = root / grouping._ALOOP_CARD_ID
    for pcm_dir in grouping._ALOOP_PCM_DIRS:
        for pair in range(grouping._ALOOP_SUBSTREAMS):
            sub = card / pcm_dir / f"sub{pair}"
            sub.mkdir(parents=True)
            is_open = pair in open_subs.get(pcm_dir, [])
            (sub / "status").write_text(
                _OPEN_STATUS if is_open else "closed\n", encoding="utf-8"
            )
    return root


@pytest.fixture()
def proc_root(monkeypatch, tmp_path):
    def _set(root: Path) -> None:
        monkeypatch.setenv(grouping._ALOOP_PROC_ROOT_ENV, str(root))

    return _set


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------

def test_no_aloop_card_is_ok(proc_root, tmp_path):
    """A box without snd-aloop has no remnant to police."""
    empty = tmp_path / "asound"
    empty.mkdir()
    proc_root(empty)
    result = grouping.check_grouping_aloop_remnant()
    assert result.status == "ok"
    assert "not loaded" in result.detail


def test_all_closed_is_ok(proc_root, tmp_path):
    proc_root(_make_card(tmp_path))
    result = grouping.check_grouping_aloop_remnant()
    assert result.status == "ok"
    assert "no non-grouping pair currently open" in result.detail
    # The remnant's size is REPORTED, not merely asserted — risk 5.1 in the
    # design is "the remnant becomes permanent by silence".
    assert "grouping remnant on pair 6" in result.detail
    assert "#2508" in result.detail


def test_registered_open_pair_is_ok_jts4_shape(proc_root, tmp_path):
    """THE FALSE-POSITIVE REGRESSION GUARD.

    Observed on jts4, 2026-08-14: /proc/asound/Loopback/pcm1c/sub3 in
    `state: RUNNING`, owner cgroup jasper-fanin.service — the usbsink lane's
    idle-read fallback documented in deploy/modprobe.d/snd-aloop.conf. That
    box is HEALTHY. If this check ever fails it, the check is wrong.
    """
    proc_root(_make_card(tmp_path, {"pcm1c": [3]}))
    result = grouping.check_grouping_aloop_remnant()
    assert result.status == "ok"
    assert "[3]" in result.detail


def test_grouping_pair_open_is_ok(proc_root, tmp_path):
    """A bonded box holding pair 6 is the remnant working as designed."""
    proc_root(_make_card(tmp_path, {"pcm0p": [6], "pcm1c": [6]}))
    result = grouping.check_grouping_aloop_remnant()
    assert result.status == "ok"
    # Pair 6 is the grouping pair itself, so it is not listed as a
    # "non-grouping pair".
    assert "no non-grouping pair currently open" in result.detail


@pytest.mark.parametrize("pcm_dir", ["pcm0p", "pcm0c", "pcm1p", "pcm1c"])
def test_positive_control_foreign_substream_fails(proc_root, tmp_path, pcm_dir):
    """POSITIVE CONTROL — a deliberately-opened foreign substream trips it.

    Pair 5 is the foreign one: P9-C deleted its PCM definitions, so a holder
    there resurrected a deleted lane. Parametrised across all four PCM
    directions so a walker that only scanned the playback side would fail this.
    """
    proc_root(_make_card(tmp_path, {pcm_dir: [5]}))
    result = grouping.check_grouping_aloop_remnant()
    assert result.status == "fail"
    assert f"{pcm_dir}/sub5" in result.detail
    assert "no registered purpose" in result.detail


def test_positive_control_names_the_offender(proc_root, tmp_path):
    """The FAIL names the offender — the design asks for pid/process."""
    proc_root(_make_card(tmp_path, {"pcm0p": [5]}))
    result = grouping.check_grouping_aloop_remnant()
    assert result.status == "fail"
    assert "pid=4242" in result.detail
    # And it tells the operator what to do about it.
    assert "deploy-to-pi.sh" in result.detail


def test_offender_detail_is_bounded(proc_root, tmp_path, monkeypatch):
    """A pathological box cannot produce an unbounded doctor line.

    The registered set is shrunk to the grouping pair alone — which is also
    the reduce's END STATE — so every other pair reads as an offender and the
    offender count (28) genuinely exceeds the cap. An earlier version of this
    test opened only pair 5 across the four PCM dirs, giving exactly 4
    offenders against a cap of 4; `[:cap]` and `[:]` were then
    indistinguishable and the bound was asserted but never proven.
    """
    monkeypatch.setattr(
        grouping, "_derive_registered_pairs", lambda: ({6: "grouping"}, 6)
    )
    proc_root(
        _make_card(
            tmp_path,
            {
                pcm: [p for p in range(grouping._ALOOP_SUBSTREAMS) if p != 6]
                for pcm in grouping._ALOOP_PCM_DIRS
            },
        )
    )
    result = grouping.check_grouping_aloop_remnant()
    assert result.status == "fail"
    cap = grouping._ALOOP_OFFENDER_DETAIL_CAP
    shown = result.detail.count("/sub")
    assert shown <= cap, f"detail listed {shown} offenders, cap is {cap}"
    # The truncation is DISCLOSED, not silent — an operator must not think
    # four offenders is the whole story.
    assert "more)" in result.detail


def test_unreadable_proc_is_warn_not_fail(proc_root, tmp_path):
    """FAIL-SOFT: 'I could not look' must never become 'something is wrong'.

    Every status path is made a DIRECTORY, so read_text raises IsADirectoryError
    (an OSError) without needing permission games that behave differently under
    root in CI.
    """
    root = tmp_path / "asound"
    card = root / grouping._ALOOP_CARD_ID
    for pcm_dir in grouping._ALOOP_PCM_DIRS:
        for pair in range(grouping._ALOOP_SUBSTREAMS):
            (card / pcm_dir / f"sub{pair}" / "status").mkdir(parents=True)
    proc_root(root)
    result = grouping.check_grouping_aloop_remnant()
    assert result.status == "warn"
    assert "could not be verified" in result.detail


def test_missing_substreams_are_not_evidence(proc_root, tmp_path):
    """A narrower snd-aloop is not a fault — absence is not an offender."""
    root = tmp_path / "asound"
    card = root / grouping._ALOOP_CARD_ID
    sub = card / "pcm0p" / "sub0"
    sub.mkdir(parents=True)
    (sub / "status").write_text("closed\n", encoding="utf-8")
    proc_root(root)
    result = grouping.check_grouping_aloop_remnant()
    assert result.status == "ok"


def test_check_never_raises_on_hostile_status(proc_root, tmp_path):
    """A garbage/empty status file must not crash the doctor."""
    root = _make_card(tmp_path)
    card = root / grouping._ALOOP_CARD_ID
    (card / "pcm0p" / "sub0" / "status").write_text("", encoding="utf-8")
    (card / "pcm0p" / "sub1" / "status").write_text(
        "\x00\xff garbage", encoding="utf-8", errors="replace"
    )
    proc_root(root)
    result = grouping.check_grouping_aloop_remnant()
    assert result.status in {"ok", "warn", "fail"}


# --------------------------------------------------------------------------
# Contract pins (design :498 — "GROUPING_LOOPBACK_PLAYBACK/_CAPTURE and the
# modprobe pcm_substreams value pinned together, so a reduction to 1 pair
# cannot leave the constants naming pair 6")
# --------------------------------------------------------------------------

def _modprobe_substreams() -> int:
    text = _MODPROBE_CONF.read_text(encoding="utf-8")
    options = [
        line for line in text.splitlines()
        if line.strip().startswith("options snd-aloop")
    ]
    assert len(options) == 1, f"expected one options line, got {options!r}"
    m = re.search(r"pcm_substreams=(\d+)", options[0])
    assert m, f"no pcm_substreams= in {options[0]!r}"
    return int(m.group(1))


def test_walker_range_matches_modprobe_pcm_substreams():
    """The walker must scan exactly the substreams the module creates."""
    assert grouping._ALOOP_SUBSTREAMS == _modprobe_substreams()


def test_grouping_constants_name_a_pair_that_exists():
    """THE DESIGN'S PIN: a reduction to 1 pair cannot leave the constants
    naming pair 6.

    Both halves of the grouping round-trip are parsed and checked against the
    module's substream count, so shrinking `pcm_substreams` without moving
    `GROUPING_LOOPBACK_*` fails here instead of on a bonded speaker.
    """
    from jasper.multiroom.reconcile import (
        GROUPING_LOOPBACK_CAPTURE,
        GROUPING_LOOPBACK_PLAYBACK,
    )

    substreams = _modprobe_substreams()
    pairs = set()
    for const in (GROUPING_LOOPBACK_PLAYBACK, GROUPING_LOOPBACK_CAPTURE):
        m = re.fullmatch(r"hw:([^,]+),(\d+),(\d+)", const.strip())
        assert m, f"{const!r} is not an hw:<card>,<device>,<sub> triple"
        assert m.group(1) == grouping._ALOOP_CARD_ID
        pairs.add(int(m.group(3)))
        assert int(m.group(3)) < substreams, (
            f"{const!r} names substream {m.group(3)} but snd-aloop is loaded "
            f"with pcm_substreams={substreams}"
        )
    assert len(pairs) == 1, (
        f"the grouping round-trip must ride ONE pair, got {sorted(pairs)}"
    )


def test_check_reads_the_grouping_pair_from_reconcile():
    """The remnant's identity has ONE owner — jasper/multiroom/reconcile.py."""
    from jasper.multiroom.reconcile import GROUPING_LOOPBACK_PLAYBACK

    expected = int(GROUPING_LOOPBACK_PLAYBACK.rsplit(",", 1)[1])
    assert grouping._grouping_pair_index() == expected


def test_grouping_pair_index_is_none_on_unparseable(monkeypatch):
    """An unparseable constant degrades to None, so the check can warn rather
    than guess an index."""
    import jasper.multiroom.reconcile as reconcile

    monkeypatch.setattr(reconcile, "GROUPING_LOOPBACK_PLAYBACK", "not-a-pcm")
    assert grouping._grouping_pair_index() is None


def test_unparseable_grouping_constant_is_warn(proc_root, tmp_path, monkeypatch):
    import jasper.multiroom.reconcile as reconcile

    monkeypatch.setattr(reconcile, "GROUPING_LOOPBACK_PLAYBACK", "not-a-pcm")
    proc_root(_make_card(tmp_path))
    result = grouping.check_grouping_aloop_remnant()
    assert result.status == "warn"
    assert "could not derive" in result.detail


# --------------------------------------------------------------------------
# Derivation — the registered set is READ from its owners, never restated
# --------------------------------------------------------------------------

#: The pairs that must be registered at this head, written as a LITERAL on
#: purpose. Deriving this expectation from the same constants the production
#: code derives from would make it move in lockstep with a regression — drop
#: pair 0 from `_FANIN_EXPECTED_ALOOP_INPUTS` and a derived expectation drops
#: it too, so nothing fails. A literal is what makes a source-constant
#: deletion detectable.
_EXPECTED_REGISTERED_PAIRS = (0, 1, 2, 3, 4, 6, 7)


def test_derived_set_matches_the_expected_allocation():
    """Dropping a pair from any owning constant changes this set."""
    derived, grouping_pair = grouping._derive_registered_pairs()
    assert tuple(sorted(derived)) == _EXPECTED_REGISTERED_PAIRS
    assert grouping_pair == 6


@pytest.mark.parametrize("pair", _EXPECTED_REGISTERED_PAIRS)
def test_every_registered_pair_open_is_ok(proc_root, tmp_path, pair):
    """THE PER-ROW GUARD.

    One case per registered pair, each opening that pair and requiring `ok`.
    Dropping a pair from its owning constant makes that pair unregistered, and
    this test then FAILs it — which is the whole point: an unregistered pair
    that a real box holds open is a red doctor on healthy hardware.
    """
    proc_root(_make_card(tmp_path, {"pcm0p": [pair], "pcm1c": [pair]}))
    result = grouping.check_grouping_aloop_remnant()
    assert result.status == "ok", f"pair {pair} open read as {result.detail}"


def test_grouping_pair_is_always_registered():
    """Replaces the old 'grouping pair missing from the registry' warn branch.

    That branch existed because a hand-maintained table could drift from
    GROUPING_LOOPBACK_PLAYBACK. The derived set inserts the grouping pair from
    that same constant, so the drift is now structurally impossible and the
    branch was deleted as unreachable. This is the invariant that replaced it.
    """
    derived, grouping_pair = grouping._derive_registered_pairs()
    assert grouping_pair in derived


def test_derivation_is_all_or_nothing_on_a_bad_input(monkeypatch):
    """A partial derivation would SHRINK the set and red-doctor a healthy box,
    so an unparseable source must return None (-> warn), never a subset."""
    from jasper.cli.doctor import audio_runtime

    monkeypatch.setattr(
        audio_runtime,
        "_FANIN_EXPECTED_ALOOP_INPUTS",
        [("spotify", "hw:Loopback,1,0"), ("airplay", "not-a-pcm")],
    )
    assert grouping._derive_registered_pairs() is None


def test_derivation_rejects_a_non_loopback_card(monkeypatch):
    from jasper.cli.doctor import audio_runtime

    monkeypatch.setattr(
        audio_runtime, "_FANIN_EXPECTED_OUTPUT_PCM", "hw:SomeOtherCard,0,7"
    )
    assert grouping._derive_registered_pairs() is None


def test_ring_armed_lanes_still_reserve_their_aloop_pair():
    """The roster is read in its ALOOP form, not through the armed-aware
    `_fanin_expected_inputs()`: a ring-armed lane still reserves its pair, so
    the registered set must not flap with arming state.

    Scoped to the derivation function's own source, not the whole module —
    the module comment legitimately names the armed-aware helper to explain
    why it is NOT used, and a file-wide substring check flagged that prose.
    """
    import inspect

    source = inspect.getsource(grouping._derive_registered_pairs)
    assert "_FANIN_EXPECTED_ALOOP_INPUTS" in source
    assert "_fanin_expected_inputs" not in source


def test_pair_five_is_not_registered():
    """P9-C DELETED pair 5, so no owner names it any more. If a future edit
    re-registers it, this fails — the deletion is what the guard protects."""
    derived, _ = grouping._derive_registered_pairs()
    assert 5 not in derived
    text = _RECONCILE_PY.read_text(encoding="utf-8")
    assert "unallocated" in text.lower(), (
        "reconcile.py should still describe pair 5 as unallocated"
    )


def test_registered_pairs_are_within_the_module_range():
    substreams = _modprobe_substreams()
    derived, _ = grouping._derive_registered_pairs()
    for pair in derived:
        assert 0 <= pair < substreams, (
            f"registered pair {pair} is outside pcm_substreams={substreams}"
        )


def test_no_phase_labels_in_the_registry():
    """The retirement pointer is gone: retirement is mechanical (a pair leaves
    when its owning constant stops naming it), so there is no phase label here
    to drift against the campaign's own vocabulary."""
    source = Path(grouping.__file__).read_text(encoding="utf-8")
    assert "retires_with" not in source
    assert "P9-E (" not in source


def test_check_text_carries_the_eol_issue():
    """Design risk 5.1's mitigation: the EOL issue is referenced from the
    doctor check's own text, so an operator reading it learns where the
    remnant is scheduled to die."""
    source = Path(grouping.__file__).read_text(encoding="utf-8")
    assert "2508" in source
