# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

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

import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests.install_surface import installer_text


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy" / "lib" / "jasper-env-file.sh"
RECONCILERS = [
    ROOT / "deploy" / "bin" / "jasper-aec-reconcile",
    ROOT / "deploy" / "bin" / "jasper-audio-hardware-reconcile",
    ROOT / "deploy" / "bin" / "jasper-wifi-guardian",
]


def _bash(
    script: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "{LIB}"\n{script}'],
        check=False,
        text=True,
        capture_output=True,
        env=run_env,
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
    values = ["hw:CARD=A,DEV=0", "has space", "it's", "a,b;c d'e", "x=1,y=2 z"]
    env_file = tmp_path / "round.env"
    for value in values:
        result = _bash(
            f'jasper_env_file_set "{env_file}" KEY {value!r}\n'
            f'source "{env_file}"\n'
            'printf "%s" "$KEY"\n'
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == value


def test_env_file_set_waits_out_a_concurrent_holder(tmp_path: Path) -> None:
    """A second writer must wait, not clobber.

    The holder here does what jasper/atomic_io.py's locked_update_env_file
    does — take the advisory lock at `<dir>/.<basename>.lock`, read, then
    write the whole file back — so an unlocked bash upsert landing inside
    that window would be lost. Removal condition: a single Python owner
    writes every env file.
    """
    env_file = tmp_path / "jasper.env"
    env_file.write_text("SEED=1\n", encoding="utf-8")
    lock = tmp_path / f".{env_file.name}.lock"
    holding = tmp_path / "holding"
    hold_seconds = 0.5
    holder = subprocess.Popen(
        [
            "bash",
            "-c",
            f'exec 9>>"{lock}"\n'
            "flock 9\n"
            f'snapshot="$(cat "{env_file}")"\n'
            f': > "{holding}"\n'
            f"sleep {hold_seconds}\n"
            f'printf "%s\\nHOLDER=1\\n" "$snapshot" > "{env_file}"\n',
        ],
    )
    try:
        deadline = time.monotonic() + 10
        while not holding.exists():
            assert time.monotonic() < deadline, "holder never took the lock"
            time.sleep(0.01)
        result = _bash(f'jasper_env_file_set "{env_file}" WRITER 2')
    finally:
        holder.wait(timeout=30)

    assert result.returncode == 0, result.stderr
    # An unlocked upsert lands inside the hold and the write-back drops it.
    assert env_file.read_text(encoding="utf-8") == "SEED=1\nHOLDER=1\nWRITER=2\n"


@pytest.mark.parametrize(
    "plant", ["live_symlink", "dangling_symlink", "device_symlink", "fifo"]
)
def test_env_file_set_refuses_a_planted_lock(tmp_path: Path, plant: str) -> None:
    """/var/lib/jasper is group-writable, so a jasper-group process can plant
    anything at the lock path, and bash has no O_NOFOLLOW. Nothing may be
    created or re-moded BY NAME: a live symlink must not carry the lock's 0660
    onto its target (transit.env, control_token live in that directory), a
    dangling one must not make root create the target, and a device must not be
    re-moded — bash adds O_EXCL under noclobber only when its pre-open stat
    FAILS, so a device is opened and followed where a regular file is refused.
    Removal condition: only jasper/atomic_io.py, which opens O_NOFOLLOW, writes
    these files.
    """
    env_file = tmp_path / "jasper.env"
    env_file.write_text("A=1\n", encoding="utf-8")
    victim = tmp_path / "victim"
    device = Path("/dev/null")
    device_mode = stat.S_IMODE(device.stat().st_mode)
    lock = tmp_path / f".{env_file.name}.lock"
    if plant == "fifo":
        os.mkfifo(lock)
    elif plant == "device_symlink":
        lock.symlink_to(device)
    else:
        if plant == "live_symlink":
            victim.write_text("secret\n", encoding="utf-8")
            victim.chmod(0o600)
        lock.symlink_to(victim)

    result = _bash(f'jasper_env_file_set "{env_file}" A 2')

    assert result.returncode == 1
    assert "reason=not_regular" in result.stderr
    assert env_file.read_text(encoding="utf-8") == "A=1\n"
    if plant == "live_symlink":
        assert victim.read_text(encoding="utf-8") == "secret\n"
        assert stat.S_IMODE(victim.stat().st_mode) == 0o600
    elif plant == "device_symlink":
        assert stat.S_IMODE(device.stat().st_mode) == device_mode
    elif plant == "dangling_symlink":
        assert not victim.exists(), "root created the symlink's dangling target"


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


def test_env_file_set_assigns_parent_group_before_publish(tmp_path: Path) -> None:
    env_file = tmp_path / "sub" / "new.env"
    env_file.parent.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    chgrp_log = tmp_path / "chgrp.log"
    fake_chgrp = fake_bin / "chgrp"
    fake_chgrp.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" >> \"$JTS_CHGRP_LOG\"\n"
    )
    fake_chgrp.chmod(0o755)

    result = _bash(
        f'PATH="{fake_bin}:$PATH" '
        f'JTS_CHGRP_LOG="{chgrp_log}" '
        f'jasper_env_file_set "{env_file}" KEY value 0640 0750'
    )

    assert result.returncode == 0, result.stderr
    # Two publishes, both taking the parent group: the advisory lock, then the
    # tempfile that becomes the env file. The lock is addressed by DESCRIPTOR
    # — a rename over its name after the create cannot redirect the chgrp.
    args = chgrp_log.read_text().splitlines()
    assert args[0] == f"--reference={env_file.parent}"
    assert args[1].startswith("/dev/fd/")
    assert args[2] == f"--reference={env_file.parent}"
    assert args[3].startswith(str(env_file.parent / ".KEY."))


def test_env_file_set_preserves_existing_ownership_before_rename(
    tmp_path: Path,
) -> None:
    """Atomic rewrites must not drop a pre-existing group-readable owner/group.

    On the Pi, /etc/jasper/jasper.env is root:jasper 0640 so non-root
    jasper-control can fresh-read status after reconcilers mutate it.
    A root-created tempfile renamed over it would become root:root unless the
    writer copies ownership from the existing file first.
    """
    env_file = tmp_path / "jasper.env"
    env_file.write_text("A=1\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    chown_log = tmp_path / "chown.log"
    fake_chown = fake_bin / "chown"
    fake_chown.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" >> \"$JTS_CHOWN_LOG\"\n"
    )
    fake_chown.chmod(0o755)
    chgrp_log = tmp_path / "chgrp.log"
    fake_chgrp = fake_bin / "chgrp"
    fake_chgrp.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" >> \"$JTS_CHGRP_LOG\"\n"
    )
    fake_chgrp.chmod(0o755)

    result = _bash(
        f'PATH="{fake_bin}:$PATH" '
        f'JTS_CHOWN_LOG="{chown_log}" '
        f'JTS_CHGRP_LOG="{chgrp_log}" '
        f'jasper_env_file_set "{env_file}" A 2 0640 0755'
    )

    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "A=2\n"
    args = chown_log.read_text().splitlines()
    assert args[0] == f"--reference={env_file}"
    assert args[1].startswith(str(env_file.parent / ".A."))
    assert args[1] != str(env_file)
    chgrp_args = chgrp_log.read_text().splitlines()
    assert chgrp_args[0] == f"--reference={env_file.parent}"
    assert chgrp_args[1].startswith("/dev/fd/"), chgrp_args
    assert len(chgrp_args) == 2, "the rewrite must keep the file's own group"


def test_env_file_repair_permissions_uses_parent_group(tmp_path: Path) -> None:
    env_file = tmp_path / "outputd.env"
    env_file.write_text("A=1\n", encoding="utf-8")
    env_file.chmod(0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    chgrp_log = tmp_path / "chgrp.log"
    fake_chgrp = fake_bin / "chgrp"
    fake_chgrp.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$@\" >> \"$JTS_CHGRP_LOG\"\n"
    )
    fake_chgrp.chmod(0o755)

    result = _bash(
        f'PATH="{fake_bin}:$PATH" '
        f'JTS_CHGRP_LOG="{chgrp_log}" '
        f'jasper_env_file_repair_permissions "{env_file}" 0640'
    )

    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == "A=1\n"
    assert (env_file.stat().st_mode & 0o777) == 0o640
    assert chgrp_log.read_text().splitlines() == [
        f"--reference={env_file.parent}",
        str(env_file),
    ]


def test_env_file_unset_removes_key(tmp_path: Path) -> None:
    """jasper_env_file_unset must REMOVE the key (not write it empty).

    The #27 HIGH fix relies on this: when an operator sets a floor key in an
    earlier-loaded EnvironmentFile, the reconciler-owned later file must DROP
    the key so an empty `KEY=` assignment can't override the operator value.
    """
    env_file = tmp_path / "outputd.env"
    env_file.write_text("KEEP=1\nDROP=stale\nKEEP2=2\n")
    result = _bash(f'jasper_env_file_unset "{env_file}" DROP')
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "KEEP=1\nKEEP2=2\n"
    # Crucially NOT left as an empty assignment.
    assert "DROP=" not in env_file.read_text()


def test_env_file_unset_removes_all_duplicate_lines(tmp_path: Path) -> None:
    env_file = tmp_path / "outputd.env"
    env_file.write_text("DROP=a\nKEEP=1\nDROP=b\n")
    result = _bash(f'jasper_env_file_unset "{env_file}" DROP')
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "KEEP=1\n"


def test_env_file_unset_noop_when_key_absent(tmp_path: Path) -> None:
    env_file = tmp_path / "outputd.env"
    env_file.write_text("KEEP=1\n")
    result = _bash(f'jasper_env_file_unset "{env_file}" MISSING')
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "KEEP=1\n"


def test_env_file_unset_noop_when_file_absent(tmp_path: Path) -> None:
    env_file = tmp_path / "absent.env"
    result = _bash(f'jasper_env_file_unset "{env_file}" KEY')
    assert result.returncode == 0, result.stderr
    assert not env_file.exists()


@pytest.mark.parametrize(
    "body,expected",
    [
        ("WANT=plain\n", "plain"),
        # Last assignment wins, matching read_stash's line loop.
        ("WANT=first\nOTHER=x\nWANT=last\n", "last"),
        ("WANT='pass word'\n", "pass word"),
        ('WANT="pass word"\n', "pass word"),
        # `#` is a legal PSK character, not a comment introducer mid-line.
        ("WANT=hash#tag\n", "hash#tag"),
        # Nothing is evaluated: shell metacharacters come back as bytes.
        ("WANT=$(id)`x`;rm -rf /\n", "$(id)`x`;rm -rf /"),
        ("# WANT=commented\nOTHER=x\n", None),
        ("OTHER=x\n", None),
    ],
)
def test_env_file_get(tmp_path: Path, body: str, expected: str | None) -> None:
    """The reader half of the lib: verbatim value of the last `KEY=` line,
    one matched quote pair stripped, rc 1 with no output when absent."""
    env_file = tmp_path / "stash.env"
    env_file.write_text(body)
    result = _bash(f'jasper_env_file_get "{env_file}" WANT')
    if expected is None:
        assert result.returncode == 1
        assert result.stdout == ""
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected + "\n"


def test_env_file_get_missing_file_returns_one(tmp_path: Path) -> None:
    result = _bash(f'jasper_env_file_get "{tmp_path / "absent.env"}" WANT')
    assert result.returncode == 1
    assert result.stdout == ""


def test_env_file_get_round_trips_env_file_set(tmp_path: Path) -> None:
    r"""The lib's two halves must agree: the writer wraps an apostrophe-
    bearing value in single quotes and splices the apostrophe as `'\''`,
    so the reader has to undo that splice or `set` becomes unreadable."""
    env_file = tmp_path / "round.env"
    result = _bash(
        f'jasper_env_file_set "{env_file}" WANT "it\'s"\n'
        f'jasper_env_file_get "{env_file}" WANT'
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "it's\n"


def test_reconcilers_source_shared_lib_and_never_printf_q() -> None:
    """Drift guard: every script in RECONCILERS (the two reconcilers and
    the wifi guardian) must load jasper-env-file.sh and must not regrow a
    local `printf %q` (the bash-5.2 comma bug) or a forked local quoting
    loop."""
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
