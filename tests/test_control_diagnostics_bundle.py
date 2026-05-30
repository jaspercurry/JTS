"""Tests for the /diagnostics-bundle helper (Tier D): run pi-bundle.sh,
return the tarball bytes + filename, and clean up the /tmp tarball."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from jasper.control import server


def test_run_diagnostics_bundle_returns_bytes_and_cleans_up(tmp_path, monkeypatch):
    tarball = tmp_path / "jasper-bundle-20260530T120000Z.tar.gz"
    tarball.write_bytes(b"\x1f\x8bFAKE-GZIP")

    def fake_run(cmd, **kw):
        assert cmd[0] == "bash"
        return SimpleNamespace(returncode=0, stdout=str(tarball) + "\n", stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    data, name = server._run_diagnostics_bundle(script="x")
    assert data == b"\x1f\x8bFAKE-GZIP"
    assert name == "jasper-bundle-20260530T120000Z.tar.gz"
    assert not tarball.exists()  # removed after reading — don't fill /tmp


def test_run_diagnostics_bundle_raises_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(RuntimeError):
        server._run_diagnostics_bundle(script="x")


def test_run_diagnostics_bundle_raises_when_file_missing(monkeypatch):
    monkeypatch.setattr(
        server.subprocess, "run",
        lambda cmd, **kw: SimpleNamespace(
            returncode=0, stdout="/tmp/nope-not-a-real-bundle.tar.gz\n", stderr="",
        ),
    )
    with pytest.raises(RuntimeError):
        server._run_diagnostics_bundle(script="x")
