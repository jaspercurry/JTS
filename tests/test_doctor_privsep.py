# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free behaviour tests for the privsep readability check core.

The check runs as root on the Pi (so it can't use os.access — that would read
everything) and reasons about a daemon's identity vs each file's mode. These
tests exercise the pure, identity-parameterized core with tmp files and
synthetic identities — no systemctl, no real jasper users:

- the headline contract: a non-root daemon's input at 0600-root-style (owner
  bits only, daemon owns neither uid nor group) FAILS; the same file at 0640
  group-`jasper` (daemon shares the group) PASSES;
- glob expansion flags only the unreadable member;
- absent inputs are not flagged (absent != the present-but-unreadable bug);
- the household_secret verdict: present-but-unreadable = gate fail-safe-OPENED,
  present-and-readable = gate enforced, absent = not paired.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from jasper.cli.doctor import privsep
from jasper.cli.doctor.privsep import MANIFEST, OUT_OF_SCOPE_NONROOT_UNITS
from tests.systemd_unit_helpers import value_for, values_for


def _make(path: Path, mode: int) -> os.stat_result:
    path.write_text("x")
    os.chmod(path, mode)
    return path.stat()


# --------------------------------------------------------------------------- #
# _process_can_read — POSIX owner/group/other precedence
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mode, as_owner, shares_group, readable",
    [
        # Owner bits win even when the group lacks the read bit.
        (0o640, True, False, True),
        (0o640, False, True, True),
        (0o640, False, False, False),
        # 0600: a group member still cannot read.
        (0o600, False, True, False),
        # 0644: other-read lets a stranger read.
        (0o644, False, False, True),
    ],
    ids=["owner", "group", "stranger-0640", "group-0600", "stranger-0644"],
)
def test_process_can_read_follows_posix_precedence(
    tmp_path: Path, mode, as_owner, shares_group, readable
):
    st = _make(tmp_path / f"f{mode:o}{as_owner}{shares_group}", mode)
    uid = st.st_uid if as_owner else 999_999
    gids = frozenset({st.st_gid if shares_group else 777_777})

    assert privsep._process_can_read(st, uid, gids) is readable


# --------------------------------------------------------------------------- #
# _classify_readable_inputs — the headline 0600-fails / 0640-passes contract
# --------------------------------------------------------------------------- #
def test_input_at_0640_group_jasper_passes(tmp_path: Path):
    f = tmp_path / "voice_provider.env"
    st = _make(f, 0o640)
    # Daemon shares the file's group but does not own it (the real scenario:
    # jasper-control reading a jasper-web-written, group-`jasper` file).
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-control",
        (str(f),),
        uid=999_999,
        gids=frozenset({st.st_gid}),
        user="jasper-control",
    )
    assert result.status == "ok", result.detail
    assert result.reason == privsep.REASON_INPUTS_READABLE


def test_input_at_0600_root_fails_naming_file_and_mode(tmp_path: Path):
    f = tmp_path / "voice_provider.env"
    _make(f, 0o600)
    # 0600 owned by someone the daemon is not, group not shared -> unreadable.
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-control",
        (str(f),),
        uid=999_999,
        gids=frozenset({777_777}),
        user="jasper-control",
    )
    assert result.status == "warn", result.detail
    assert result.reason == privsep.REASON_INPUTS_UNREADABLE


def test_wrong_group_0640_fails(tmp_path: Path):
    """0640 but the daemon shares neither uid nor the file's group -> unreadable
    (the sound_settings.json 0640-but-wrong-group shape)."""
    f = tmp_path / "sound_settings.json"
    _make(f, 0o640)
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-web",
        (str(f),),
        uid=999_999,
        gids=frozenset({777_777}),
        user="jasper-web",
    )
    assert result.status == "warn"
    assert result.reason == privsep.REASON_INPUTS_UNREADABLE


def test_glob_flags_only_unreadable_member(tmp_path: Path):
    configs = tmp_path / "configs"
    configs.mkdir()
    ok_cfg = configs / "sound_current.yml"
    bad_cfg = configs / "grouping_leader.yml"
    ok_st = _make(ok_cfg, 0o640)
    _make(bad_cfg, 0o600)
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-control",
        (str(configs / "*.yml"),),
        uid=999_999,
        gids=frozenset({ok_st.st_gid}),
        user="jasper-control",
    )
    assert result.status == "warn"
    assert result.reason == privsep.REASON_INPUTS_UNREADABLE
    assert str(bad_cfg) in result.detail
    assert str(ok_cfg) not in result.detail


def test_absent_inputs_are_not_flagged(tmp_path: Path):
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-control",
        (str(tmp_path / "does-not-exist.env"), str(tmp_path / "none.yml")),
        uid=999_999,
        gids=frozenset({777_777}),
        user="jasper-control",
    )
    assert result.status == "skipped"
    assert result.reason == privsep.REASON_NO_INPUTS_PRESENT


def test_mixed_present_and_absent_only_checks_present(tmp_path: Path):
    present = tmp_path / "transit.env"
    st = _make(present, 0o640)
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-web",
        (str(present), str(tmp_path / "absent.env")),
        uid=999_999,
        gids=frozenset({st.st_gid}),
        user="jasper-web",
    )
    assert result.status == "ok"
    assert result.reason == privsep.REASON_INPUTS_READABLE


# --------------------------------------------------------------------------- #
# household_secret verdict — the fail-safe-open observability case
# --------------------------------------------------------------------------- #
def test_household_secret_present_unreadable_warns_gate_open(tmp_path: Path):
    secret = tmp_path / "household_secret"
    st = _make(secret, 0o600)
    result = privsep._household_secret_verdict(
        st, uid=999_999, gids=frozenset({777_777}), user="jasper-control"
    )
    assert result.status == "warn"
    assert result.reason == privsep.REASON_HOUSEHOLD_SECRET_UNREADABLE


def test_household_secret_present_readable_ok_gate_enforced(tmp_path: Path):
    secret = tmp_path / "household_secret"
    st = _make(secret, 0o640)
    result = privsep._household_secret_verdict(
        st, uid=999_999, gids=frozenset({st.st_gid}), user="jasper-control"
    )
    assert result.status == "ok"
    assert result.reason == privsep.REASON_HOUSEHOLD_SECRET_READABLE


def test_household_secret_absent_is_skipped(tmp_path, monkeypatch):
    """Absent secret = not paired, nothing to verify (skipped). This path
    returns before any systemctl call, so it is fully hardware-free."""
    from jasper.control import household_credential

    monkeypatch.setattr(
        household_credential, "SECRET_FILE", str(tmp_path / "nope"), raising=True
    )
    result = privsep.check_household_secret_readable()
    assert result.status == "skipped"
    assert result.reason == privsep.REASON_HOUSEHOLD_SECRET_ABSENT


# --------------------------------------------------------------------------- #
# _unit_runtime_identity — the evidence-cache seams
# --------------------------------------------------------------------------- #
def test_unit_runtime_identity_batches_user_group_across_daemons(monkeypatch):
    """User/Group/SupplementaryGroups are read once for the whole manifest,
    not once per daemon queried (ADR-0233 rule 4)."""
    from jasper.cli.doctor import _evidence

    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_property(prop, units):
        calls.append((prop, tuple(units)))
        return [f"{prop}-value" for _ in units]

    def fake_unit_states(units, *, timeout):
        return {u: {"unit": u, "load_state": "loaded"} for u in units}

    monkeypatch.setattr(_evidence, "_systemctl_show_property", fake_property)
    monkeypatch.setattr(_evidence, "read_unit_states", fake_unit_states)

    first = privsep._unit_runtime_identity("jasper-control")
    second = privsep._unit_runtime_identity("jasper-web")

    assert first["User"] == "User-value"
    assert second["User"] == "User-value"
    assert sorted(prop for prop, _ in calls) == ["Group", "SupplementaryGroups", "User"]


def test_unit_runtime_identity_is_none_when_a_property_is_unreadable(monkeypatch):
    """A LoadState success with a broken User/Group/SupplementaryGroups read
    must not misclassify as 'runs as root' or 'not installed' — it is
    unknown, like a full systemctl failure."""
    from jasper.cli.doctor import _evidence

    def fake_unit_states(units, *, timeout):
        return {u: {"unit": u, "load_state": "loaded"} for u in units}

    monkeypatch.setattr(_evidence, "read_unit_states", fake_unit_states)
    monkeypatch.setattr(
        _evidence, "_systemctl_show_property", lambda prop, units: None,
    )

    assert privsep._unit_runtime_identity("jasper-control") is None


# --------------------------------------------------------------------------- #
# Integration: the decorated checks must be total (never crash) off the Pi.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "check",
    [
        privsep.check_control_readable_inputs,
        privsep.check_web_readable_inputs,
        privsep.check_chat_web_readable_inputs,
        privsep.check_mux_readable_inputs,
        privsep.check_voice_readable_inputs,
        privsep.check_usbmic_readable_inputs,
    ],
    ids=lambda fn: fn.__name__,
)
def test_decorated_checks_are_total_without_systemctl(monkeypatch, check):
    """With systemctl unavailable every per-daemon check returns a skip,
    never raising — the doctor must stay total on a dev host."""
    monkeypatch.setattr(privsep, "_unit_runtime_identity", lambda unit: None)

    assert check().status == "skipped"


def test_not_installed_unit_skips(monkeypatch):
    monkeypatch.setattr(
        privsep,
        "_unit_runtime_identity",
        lambda unit: {"LoadState": "not-found", "User": ""},
    )
    result = privsep.check_voice_readable_inputs()
    assert result.status == "skipped"
    assert result.reason == privsep.REASON_UNIT_NOT_INSTALLED


def test_root_unit_skips(monkeypatch):
    """A unit running as root (e.g. streambox jasper-web) reads everything -> skip."""
    monkeypatch.setattr(
        privsep,
        "_unit_runtime_identity",
        lambda unit: {"LoadState": "loaded", "User": "root"},
    )
    result = privsep.check_web_readable_inputs()
    assert result.status == "skipped"
    assert result.reason == privsep.REASON_UNIT_RUNS_AS_ROOT


def test_classify_warn_overflow_truncates(tmp_path: Path):
    """Many unreadable files -> detail truncates with a (+N more) marker."""
    paths = []
    for i in range(9):
        f = tmp_path / f"f{i}.env"
        _make(f, 0o600)
        paths.append(str(f))
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-control",
        tuple(paths),
        uid=999_999,
        gids=frozenset({777_777}),
        user="jasper-control",
    )
    assert result.status == "warn"
    assert result.reason == privsep.REASON_INPUTS_UNREADABLE
    # Pure formatting behavior: the shown-list caps at 6 with an overflow marker.
    assert "(+3 more)" in result.detail


# --------------------------------------------------------------------------- #
# MANIFEST vs the committed systemd units
#
# MANIFEST hardcodes each non-root daemon's runtime identity and the files it
# reads. The check resolves identity from the LIVE unit, so a unit edit that
# desyncs the manifest would make it reason about the wrong identity.
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]

# The non-root daemons whose read sets this privsep guard covers. Removing one from the
# manifest should be a conscious edit, not a silent drop.
_EXPECTED_MANIFEST_UNITS = frozenset(
    {
        "jasper-control",
        "jasper-web",
        "jasper-chat-web",
        "jasper-correction-web",
        "jasper-bluetooth-web",
        "jasper-system-web",
        "jasper-mux",
        "jasper-voice",
        "jasper-input",
        "jasper-usbmic",
    }
)

# The check only reasons about the single `jasper` group dimension, so every
# declared read must live under a group-`jasper` state tree.
_ALLOWED_PATH_PREFIXES = ("/var/lib/jasper/", "/var/lib/camilladsp/")


def _unit_identity(unit_file: Path) -> tuple[str, str, frozenset[str]]:
    """(User, Group, {SupplementaryGroups}) from a unit file. User/Group take the
    last assignment (systemd's last-wins); SupplementaryGroups accumulate across
    every line (systemd unions them)."""
    unit_text = unit_file.read_text()
    return (
        value_for(unit_text, "User") or "",
        value_for(unit_text, "Group") or "",
        frozenset(values_for(unit_text, "SupplementaryGroups")),
    )


def test_manifest_covers_exactly_the_tier_a_daemons():
    assert {s.unit for s in MANIFEST} == _EXPECTED_MANIFEST_UNITS


def test_each_spec_identity_mirrors_its_unit_file():
    for spec in MANIFEST:
        unit_file = ROOT / spec.unit_file
        assert unit_file.is_file(), f"{spec.unit}: unit file {spec.unit_file} missing"
        user, group, supp = _unit_identity(unit_file)
        assert spec.user == user, (
            f"{spec.unit}: manifest User={spec.user!r} but {spec.unit_file} has "
            f"User={user!r} — update privsep.MANIFEST to match the unit."
        )
        assert spec.group == group, (
            f"{spec.unit}: manifest Group={spec.group!r} but unit has Group={group!r}"
        )
        assert frozenset(spec.supplementary_groups) == supp, (
            f"{spec.unit}: manifest SupplementaryGroups="
            f"{sorted(spec.supplementary_groups)} but unit has {sorted(supp)} — "
            "update privsep.MANIFEST to match the unit."
        )


def test_every_nonroot_jasper_unit_is_classified():
    """Enumerate every deploy unit declaring User=jasper-*; each must be in the
    manifest or the explicit out-of-scope set. Catches a new non-root daemon
    added without a scope decision."""
    manifest_units = {s.unit for s in MANIFEST}
    nonroot: dict[str, str] = {}
    for unit_file in sorted(ROOT.glob("deploy/**/*.service")):
        user, _, _ = _unit_identity(unit_file)
        if user.startswith("jasper-"):
            nonroot.setdefault(unit_file.stem, user)
    assert nonroot, "no User=jasper-* units found — parser regression?"
    unclassified = {
        unit
        for unit in nonroot
        if unit not in manifest_units and unit not in OUT_OF_SCOPE_NONROOT_UNITS
    }
    assert not unclassified, (
        f"non-root jasper unit(s) {sorted(unclassified)} are neither in "
        "privsep.MANIFEST nor OUT_OF_SCOPE_NONROOT_UNITS. A new non-root daemon "
        "must be classified: add its read-set to the manifest, or document it as "
        "out-of-scope (e.g. a reconciler) in OUT_OF_SCOPE_NONROOT_UNITS."
    )


def test_manifest_paths_stay_in_group_jasper_trees():
    for spec in MANIFEST:
        for path in spec.paths:
            assert path.startswith(_ALLOWED_PATH_PREFIXES), (
                f"{spec.unit}: path {path} is outside the group-`jasper` trees "
                f"{_ALLOWED_PATH_PREFIXES} this check is scoped to (secret "
                "compartments are deliberately excluded)."
            )


def test_out_of_scope_units_are_real_and_nonroot():
    """The out-of-scope allowlist must not rot: each entry must be a real deploy
    unit that actually runs as a non-root jasper user."""
    for unit in OUT_OF_SCOPE_NONROOT_UNITS:
        matches = list(ROOT.glob(f"deploy/**/{unit}.service"))
        assert matches, f"out-of-scope unit {unit} has no deploy unit file"
        user, _, _ = _unit_identity(matches[0])
        assert user.startswith("jasper-"), (
            f"out-of-scope unit {unit} is not non-root (User={user!r}); remove it "
            "from OUT_OF_SCOPE_NONROOT_UNITS."
        )
