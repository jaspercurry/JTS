# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The grouping snapcast-provisioning opt-in (jasper.multiroom.provision).

Pins: idempotent no-op when present; apt-install + distro-unit neutralise +
status on success; TOTAL fail-soft (apt nonzero / apt raises / installed-but-
still-missing all resolve to a recorded ``failed`` status, never an exception)."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

from jasper.multiroom import provision


def _which(present):
    """A shutil.which stub: resolves only the names in ``present`` (a set)."""
    return lambda name: f"/usr/bin/{name}" if name in present else None


class _Runner:
    """A subprocess.run stub recording argv. ``apt-get install`` returns
    ``apt_rc`` (or raises ``apt_raise``); when it succeeds it flips ``installed``
    so a post-install which() sees the binaries. ``apt-get update`` returns 0 (or
    raises ``update_raise``). ``snapclient --version`` returns
    ``snapclient_version_stdout`` (default a real-shaped version line).
    systemctl calls return ``systemctl_rc``."""

    def __init__(
        self, *, installed, apt_rc=0, apt_raise=None, update_raise=None,
        systemctl_rc=0, snapclient_version_stdout="snapclient v0.31.0\n",
    ):
        self.calls: list[list[str]] = []
        self._installed = installed
        self._apt_rc = apt_rc
        self._apt_raise = apt_raise
        self._update_raise = update_raise
        self._systemctl_rc = systemctl_rc
        self._snapclient_version_stdout = snapclient_version_stdout

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if list(argv[:2]) == ["apt-get", "update"]:
            if self._update_raise is not None:
                raise self._update_raise
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if list(argv[:2]) == ["apt-get", "install"]:
            if self._apt_raise is not None:
                raise self._apt_raise
            if self._apt_rc == 0:
                self._installed.add("snapserver")
                self._installed.add("snapclient")
            return SimpleNamespace(
                returncode=self._apt_rc,
                stdout="",
                stderr="E: boom" if self._apt_rc else "",
            )
        if list(argv[:2]) == ["snapclient", "--version"]:
            return SimpleNamespace(
                returncode=0, stdout=self._snapclient_version_stdout, stderr="",
            )
        return SimpleNamespace(
            returncode=self._systemctl_rc,
            stdout="",
            stderr="systemctl boom" if self._systemctl_rc else "",
        )


def test_snapcast_present() -> None:
    assert provision.snapcast_present(which=_which({"snapserver", "snapclient"})) is True
    assert provision.snapcast_present(which=_which({"snapserver"})) is False
    assert provision.snapcast_present(which=_which(set())) is False


def test_present_is_a_noop(tmp_path) -> None:
    runner = _Runner(installed=set())
    status = str(tmp_path / "s.json")
    r = provision.ensure_snapcast_installed(
        runner=runner, which=_which({"snapserver", "snapclient"}), status_path=status,
    )
    assert r["state"] == "present"
    assert runner.calls == []  # never shelled out to apt
    assert json.loads(Path(status).read_text())["state"] == "present"


def test_install_success_neutralises_distro_units(tmp_path) -> None:
    present: set[str] = set()
    runner = _Runner(installed=present)
    status = str(tmp_path / "s.json")
    r = provision.ensure_snapcast_installed(
        runner=runner, which=_which(present), status_path=status,
    )
    assert r["state"] == "installed"
    assert any(c[:2] == ["apt-get", "install"] for c in runner.calls)
    # The distro auto-enabled units are neutralised (mirror install.sh).
    assert any(
        c[:3] == ["systemctl", "disable", "--now"] for c in runner.calls
    )
    assert json.loads(Path(status).read_text())["state"] == "installed"


def test_apt_update_precedes_install(tmp_path) -> None:
    """A stale package index is the #1 spurious-failure cause, so a best-effort
    `apt-get update` runs BEFORE the install."""
    present: set[str] = set()
    runner = _Runner(installed=present)
    provision.ensure_snapcast_installed(
        runner=runner, which=_which(present), status_path=str(tmp_path / "s.json"),
    )
    updates = [i for i, c in enumerate(runner.calls) if c[:2] == ["apt-get", "update"]]
    installs = [i for i, c in enumerate(runner.calls) if c[:2] == ["apt-get", "install"]]
    assert updates and installs
    assert updates[0] < installs[0]  # update first


def test_install_waits_out_the_dpkg_lock(tmp_path) -> None:
    """unattended-upgrades holds the dpkg lock daily, so the install passes
    DPkg::Lock::Timeout to wait it out rather than failing instantly."""
    present: set[str] = set()
    runner = _Runner(installed=present)
    provision.ensure_snapcast_installed(
        runner=runner, which=_which(present), status_path=str(tmp_path / "s.json"),
        apt_lock_timeout=90,
    )
    install = next(c for c in runner.calls if c[:2] == ["apt-get", "install"])
    assert "DPkg::Lock::Timeout=90" in install


def test_apt_update_failure_is_nonfatal(tmp_path) -> None:
    """A failed/offline `apt-get update` must NOT block the install — the index
    may already be fresh, so its result is ignored."""
    present: set[str] = set()
    runner = _Runner(
        installed=present,
        update_raise=subprocess.TimeoutExpired(cmd="apt-get", timeout=120),
    )
    r = provision.ensure_snapcast_installed(
        runner=runner, which=_which(present), status_path=str(tmp_path / "s.json"),
    )
    assert r["state"] == "installed"  # update failed, install still proceeded + succeeded


def test_install_success_logs_nonzero_distro_unit_neutralise(
    tmp_path, caplog,
) -> None:
    """Apt can succeed while the distro unit cleanup fails. That must remain
    fail-soft, but the rogue-unit cleanup gap must be visible in the journal."""
    present: set[str] = set()
    runner = _Runner(installed=present, systemctl_rc=1)
    status = str(tmp_path / "s.json")

    with caplog.at_level(logging.WARNING):
        r = provision.ensure_snapcast_installed(
            runner=runner, which=_which(present), status_path=status,
        )

    assert r["state"] == "installed"
    assert json.loads(Path(status).read_text())["state"] == "installed"
    assert "multiroom.provision.distro_unit_neutralise_failed" in caplog.text
    assert "rc=1" in caplog.text
    assert "systemctl boom" in caplog.text


def test_apt_nonzero_fails_soft(tmp_path) -> None:
    present: set[str] = set()
    runner = _Runner(installed=present, apt_rc=100)
    status = str(tmp_path / "s.json")
    r = provision.ensure_snapcast_installed(
        runner=runner, which=_which(present), status_path=status,
    )
    assert r["state"] == "failed"
    assert "boom" in r["detail"] or r["detail"]
    # Did NOT proceed to neutralise distro units on a failed install.
    assert not any(c[:2] == ["systemctl", "disable"] for c in runner.calls)
    assert json.loads(Path(status).read_text())["state"] == "failed"


def test_apt_raises_fails_soft_never_raises(tmp_path) -> None:
    present: set[str] = set()
    runner = _Runner(
        installed=present,
        apt_raise=subprocess.TimeoutExpired(cmd="apt-get", timeout=300),
    )
    status = str(tmp_path / "s.json")
    # Must NOT propagate — provisioning can never crash the reconcile path.
    r = provision.ensure_snapcast_installed(
        runner=runner, which=_which(present), status_path=status,
    )
    assert r["state"] == "failed"
    assert json.loads(Path(status).read_text())["state"] == "failed"


def test_apt_ok_but_binaries_still_missing_fails(tmp_path) -> None:
    # apt exits 0 but never actually provides the binaries (held package / bad
    # index): which() stays empty, so the post-install verify catches it.
    present: set[str] = set()

    class _SilentRunner(_Runner):
        def __call__(self, argv, **kw):
            self.calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="", stderr="")  # no install

    runner = _SilentRunner(installed=present)
    status = str(tmp_path / "s.json")
    r = provision.ensure_snapcast_installed(
        runner=runner, which=_which(present), status_path=status,
    )
    assert r["state"] == "failed"
    assert "still not on PATH" in r["detail"]


def test_read_provision_status_round_trips(tmp_path) -> None:
    status = str(tmp_path / "s.json")
    assert provision.read_provision_status(status) == {}  # absent → {}
    runner = _Runner(installed=set())
    provision.ensure_snapcast_installed(
        runner=runner, which=_which({"snapserver", "snapclient"}), status_path=status,
    )
    got = provision.read_provision_status(status)
    assert got["state"] == "present"
    assert "detail" in got


# --- §8.1: version probe + record (T-9) ------------------------------------


def test_install_probes_and_records_snapclient_version(tmp_path) -> None:
    """After a successful install, `ensure_snapcast_installed` probes
    `snapclient --version`, parses it, and records it — surfaced both in the
    return value and in the status file `read_provision_status` re-reads."""
    present: set[str] = set()
    runner = _Runner(installed=present, snapclient_version_stdout="snapclient v0.31.0\n")
    status = str(tmp_path / "s.json")
    r = provision.ensure_snapcast_installed(
        runner=runner, which=_which(present), status_path=status,
    )
    assert r["state"] == "installed"
    assert r["version"] == "0.31.0"
    assert any(c == ["snapclient", "--version"] for c in runner.calls)
    assert provision.read_provision_status(status)["version"] == "0.31.0"


def test_version_probe_failure_is_fail_soft(tmp_path, caplog) -> None:
    """A probe that cannot run (timeout / missing binary) must never fail the
    install it only observes — the install still reports `installed`, just
    with an empty (unrecorded) version, and the miss is logged rather than
    silent."""
    present: set[str] = set()
    base_runner = _Runner(installed=present)

    def _raising_call(argv, **kw):
        if list(argv[:2]) == ["snapclient", "--version"]:
            raise subprocess.TimeoutExpired(cmd="snapclient", timeout=5)
        return base_runner(argv, **kw)

    status = str(tmp_path / "s.json")
    with caplog.at_level(logging.WARNING):
        r = provision.ensure_snapcast_installed(
            runner=_raising_call, which=_which(present), status_path=status,
        )
    assert r["state"] == "installed"
    assert r["version"] == ""
    assert "multiroom.provision.snapclient_version_probe_failed" in caplog.text


def test_version_probe_unparseable_output_is_empty(tmp_path) -> None:
    """Output with no version-shaped token degrades to an empty version
    rather than raising or recording garbage."""
    present: set[str] = set()
    runner = _Runner(installed=present, snapclient_version_stdout="not a version\n")
    status = str(tmp_path / "s.json")
    r = provision.ensure_snapcast_installed(
        runner=runner, which=_which(present), status_path=status,
    )
    assert r["state"] == "installed"
    assert r["version"] == ""


def test_present_path_carries_the_version_forward_without_reprobing(
    tmp_path,
) -> None:
    """The `present` fast path stays a true no-op subprocess-wise (it is
    called on every reconcile pass while grouping is active): it must NOT
    re-invoke `snapclient --version`, and must instead carry forward whatever
    a prior install already recorded."""
    present: set[str] = {"snapserver", "snapclient"}
    status = str(tmp_path / "s.json")
    # Seed a prior recorded version, as a real install would have left it.
    Path(status).write_text(
        json.dumps({"state": "installed", "detail": "", "version": "0.31.0"}),
    )
    runner = _Runner(installed=present)
    r = provision.ensure_snapcast_installed(
        runner=runner, which=_which(present), status_path=status,
    )
    assert r["state"] == "present"
    assert r["version"] == "0.31.0"
    assert runner.calls == []  # still a true no-op — no probe, no apt
