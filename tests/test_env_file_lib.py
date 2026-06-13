"""Tests for the shared env-file quoting/writer lib
(deploy/lib/jasper-env-file.sh) and the drift guard that keeps both
reconcilers on it.

The `printf %q` bug class this lib exists to kill: bash 5.2 escapes
commas (`hw:CARD=A\\,DEV=0`), which systemd's EnvironmentFile= parser
keeps literally — corrupting ALSA device specs and breaking the
reconcilers' read-back idempotence (restart churn on every pass).
PR #534 fixed it in jasper-audio-hardware-reconcile; the shared lib
makes the fix the single implementation for every env-file writer.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.install_surface import installer_text


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy" / "lib" / "jasper-env-file.sh"
RECONCILERS = [
    ROOT / "deploy" / "bin" / "jasper-aec-reconcile",
    ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile",
]


def _bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}"\n{script}'],
        check=False,
        text=True,
        capture_output=True,
    )


def _quote(value: str) -> str:
    result = _bash(f"jasper_env_quote_value {value!r}")
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize(
    "value,expected",
    [
        # The bug-class values: commas must pass through unescaped.
        ("hw:CARD=A,DEV=0", "hw:CARD=A,DEV=0"),
        ("plughw:CARD=Array,DEV=0", "plughw:CARD=Array,DEV=0"),
        ("stereo:5,6", "stereo:5,6"),
        # Safe charset passes through verbatim.
        ("udp:9876", "udp:9876"),
        ("127.0.0.1:9891", "127.0.0.1:9891"),
        # Empty becomes an explicit empty literal.
        ("", "''"),
        # Unsafe values get single-quote wrapped.
        ("has space", "'has space'"),
        ("semi;colon", "'semi;colon'"),
        # Embedded single quotes use the '\'' idiom.
        ("it's", "'it'\\''s'"),
    ],
)
def test_quote_env_value(value: str, expected: str) -> None:
    assert _quote(value) == expected


def test_round_trip_through_source(tmp_path: Path) -> None:
    """Whatever the lib writes, `source` must read back the original."""
    values = ["hw:CARD=A,DEV=0", "has space", "it's", "a,b;c d'e"]
    env_file = tmp_path / "round.env"
    for value in values:
        result = _bash(
            f'jasper_env_file_set "{env_file}" KEY {value!r}\n'
            f'source "{env_file}"\n'
            'printf "%s" "$KEY"\n'
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == value


def test_env_file_set_upserts_and_dedupes(tmp_path: Path) -> None:
    env_file = tmp_path / "jasper.env"
    env_file.write_text(
        "JASPER_A=1\nJASPER_B=old\nJASPER_B=older\nJASPER_C=3\n"
    )
    result = _bash(f'jasper_env_file_set "{env_file}" JASPER_B new-value')
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == (
        "JASPER_A=1\nJASPER_B=new-value\nJASPER_C=3\n"
    )


def test_env_file_set_creates_file_with_requested_mode(tmp_path: Path) -> None:
    env_file = tmp_path / "sub" / "new.env"
    result = _bash(
        f'jasper_env_file_set "{env_file}" KEY "v,w" 0640 0750'
    )
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "KEY=v,w\n"
    assert (env_file.stat().st_mode & 0o777) == 0o640
    assert (env_file.parent.stat().st_mode & 0o777) == 0o750


def test_reconcilers_source_shared_lib_and_never_printf_q() -> None:
    """Drift guard: both reconcilers must load jasper-env-file.sh and
    must not regrow a local `printf %q` (the bash-5.2 comma bug) or a
    forked local quoting loop."""
    for script in RECONCILERS:
        text = script.read_text()
        assert "jasper-env-file.sh" in text, script.name
        assert "printf '%q'" not in text, script.name
        assert "printf %q" not in text, script.name
        # The quoting loop lives only in the lib — a reconciler that
        # re-grows its own `'\''` rewrite loop has forked the helper.
        assert "${rest%%\\'*}" not in text, script.name


def test_reconcilers_prefer_script_dir_sibling_lib() -> None:
    """Version-skew guard: install.sh runs the REPO copy of a reconciler
    mid-install (install_alsa's --print-env) before install_systemd_units
    refreshes /usr/local/lib, so the loader must prefer the readable
    SCRIPT_DIR-relative sibling over the installed copy — otherwise one
    mid-install call can pair a new script with a stale lib."""
    sibling = '"${SCRIPT_DIR}/../lib/jasper-env-file.sh"'
    installed = "/usr/local/lib/jasper/jasper-env-file.sh"
    for script in RECONCILERS:
        text = script.read_text()
        body = text[text.index("load_env_file_lib() {"):]
        assert body.index(sibling) < body.index(installed), script.name


def test_install_sh_installs_env_file_lib() -> None:
    install_sh = installer_text()
    assert "deploy/lib/jasper-env-file.sh" in install_sh
    assert "/usr/local/lib/jasper/jasper-env-file.sh" in install_sh
