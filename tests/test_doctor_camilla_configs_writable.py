# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor guard on the CamillaDSP config dir's group-write posture.

Pins the fix for the jts3 2026-07-06 incident: a deploy left
``/var/lib/camilladsp/configs`` root-only (setgid kept, group-write stripped —
mode 2755), so the non-root ``jasper-web`` user could not atomically write the
active-speaker staged config and staging failed with ``PermissionError`` —
surfacing to the household as "could not load the silent active-speaker setup".
This check catches that state at boot/probe time instead of at the wizard.
"""

from __future__ import annotations

import grp
import os

from jasper.cli.doctor import audio


def _own_group() -> str:
    return grp.getgrgid(os.getgid()).gr_name


def test_ok_when_group_writable_and_expected_group(tmp_path):
    d = tmp_path / "configs"
    d.mkdir()
    os.chmod(d, 0o2775)
    res = audio._camilla_configs_writable_result(d, expected_group=_own_group())
    assert res.status == "ok"


def test_fail_when_not_group_writable(tmp_path):
    # The exact regression shape: group-write bit stripped (2755).
    d = tmp_path / "configs"
    d.mkdir()
    os.chmod(d, 0o2755)
    res = audio._camilla_configs_writable_result(d, expected_group=_own_group())
    assert res.status == "fail"
    assert "PermissionError" in res.detail


def test_fail_when_wrong_group(tmp_path):
    d = tmp_path / "configs"
    d.mkdir()
    os.chmod(d, 0o2775)
    res = audio._camilla_configs_writable_result(
        d, expected_group="jts-no-such-group-xyz"
    )
    assert res.status == "fail"


def test_warn_when_missing(tmp_path):
    res = audio._camilla_configs_writable_result(tmp_path / "nope")
    assert res.status == "warn"


def test_wrapper_targets_the_constant_dir(monkeypatch, tmp_path):
    # The decorated check reads CAMILLA_CONFIGS_DIR (the real /var/lib path in
    # production); pinning that wiring keeps the guard pointed at the dir the
    # deploy actually permissions.
    missing = tmp_path / "nope"
    monkeypatch.setattr(audio, "CAMILLA_CONFIGS_DIR", missing)
    res = audio.check_camilla_configs_writable()
    assert res.status == "warn"
    assert str(missing) in res.detail
