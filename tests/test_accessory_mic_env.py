# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The accessory-mic env file's format owner.

``jasper.accessories.mic_env`` renders the file that
``jasper-accessory-reconcile`` writes and parses it for the two readers that
appeared with issue #2205: the voice-input gate (via
``deploy/bin/jasper-aec-reconcile``) and ``jasper.mic_presence``.

The load-bearing property is that its parser agrees with
``jasper.config._env_mapping`` about which files are USABLE. They are different
functions with different jobs — one answers "which sources?", the other builds
``Config`` — but if the gate accepts a file the daemon rejects, the gate opens
for a daemon that then raises at startup, and ``RuntimeError`` is not one of
jasper-voice's clean-park exits: it crash-loops into StartLimitAction=reboot.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from jasper.accessories.mic_env import (
    MANUAL_MIC_SOURCES_KEY,
    parse_manual_mic_sources,
    read_accessory_mic_sources,
    render_manual_mic_env,
)


def test_render_and_parse_round_trip() -> None:
    body = render_manual_mic_env({"b_remote": "udp:9893", "a_remote": "udp:9892"})
    assert body == f"{MANUAL_MIC_SOURCES_KEY}=a_remote=udp:9892,b_remote=udp:9893\n"
    assert parse_manual_mic_sources(body) == ("a_remote", "b_remote")


def test_empty_sources_render_to_no_file_body() -> None:
    """Never a published key with no value — the writer unlinks instead."""
    assert render_manual_mic_env({}) == ""
    assert parse_manual_mic_sources("") == ()


@pytest.mark.parametrize(
    "body",
    [
        f"{MANUAL_MIC_SOURCES_KEY}=wiim_remote_2\n",          # no '='
        f"{MANUAL_MIC_SOURCES_KEY}==udp:9892\n",              # empty id
        f"{MANUAL_MIC_SOURCES_KEY}=wiim_remote_2=\n",         # empty device
        f"{MANUAL_MIC_SOURCES_KEY}=a b=udp:9892\n",           # whitespace in id
        f"{MANUAL_MIC_SOURCES_KEY}=a=udp:1,a=udp:2\n",        # duplicate id
        # The dangerous one: one usable entry beside one broken entry. Partial
        # acceptance would open the gate for a Config that then RAISES.
        f"{MANUAL_MIC_SOURCES_KEY}=good=udp:9892,bad\n",
        f"{MANUAL_MIC_SOURCES_KEY}=bad,good=udp:9892\n",
    ],
)
def test_unparsable_bodies_publish_nothing(body: str) -> None:
    assert parse_manual_mic_sources(body) == ()


@pytest.mark.parametrize(
    "body",
    [
        f"{MANUAL_MIC_SOURCES_KEY}=wiim_remote_2\n",
        f"{MANUAL_MIC_SOURCES_KEY}==udp:9892\n",
        f"{MANUAL_MIC_SOURCES_KEY}=wiim_remote_2=\n",
        f"{MANUAL_MIC_SOURCES_KEY}=a b=udp:9892\n",
        f"{MANUAL_MIC_SOURCES_KEY}=a=udp:1,a=udp:2\n",
        f"{MANUAL_MIC_SOURCES_KEY}=good=udp:9892,bad\n",
        f"{MANUAL_MIC_SOURCES_KEY}=bad,good=udp:9892\n",
    ],
)
def test_every_body_this_parser_rejects_is_one_config_also_rejects(
    body: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract in the module docstring, asserted rather than asserted-in-prose.

    A body this parser accepts must be one ``Config`` can load; the reverse
    direction (we reject something Config would accept) is safe, so this only
    pins the dangerous direction.
    """
    from jasper.config import _env_mapping

    assert parse_manual_mic_sources(body) == ()
    raw = body.split("=", 1)[1].strip()
    monkeypatch.setenv(MANUAL_MIC_SOURCES_KEY, raw)
    with pytest.raises(RuntimeError):
        _env_mapping(MANUAL_MIC_SOURCES_KEY, "")


def test_accepted_bodies_load_into_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from jasper.config import _env_mapping

    body = render_manual_mic_env({"wiim_remote_2": "udp:9892"})
    assert parse_manual_mic_sources(body) == ("wiim_remote_2",)
    monkeypatch.setenv(MANUAL_MIC_SOURCES_KEY, body.split("=", 1)[1].strip())
    assert dict(_env_mapping(MANUAL_MIC_SOURCES_KEY, "")) == {
        "wiim_remote_2": "udp:9892",
    }


def test_last_assignment_wins_like_systemd(tmp_path, monkeypatch) -> None:
    path = tmp_path / "accessory-mics.env"
    path.write_text(
        f"{MANUAL_MIC_SOURCES_KEY}=old=udp:1\n"
        f"{MANUAL_MIC_SOURCES_KEY}=new=udp:2\n",
    )
    monkeypatch.setenv("JASPER_ACCESSORY_MIC_ENV_FILE", str(path))
    assert read_accessory_mic_sources() == ("new",)


def test_missing_file_publishes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "JASPER_ACCESSORY_MIC_ENV_FILE", str(tmp_path / "absent.env"),
    )
    assert read_accessory_mic_sources() == ()


def test_cli_exit_status_is_the_shell_contract(tmp_path) -> None:
    """deploy/bin/jasper-aec-reconcile branches on this exit status, so pin it:
    0 + ids on stdout when published, non-zero otherwise."""
    path = tmp_path / "accessory-mics.env"
    env = {
        "JASPER_ACCESSORY_MIC_ENV_FILE": str(path),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[1]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    argv = [sys.executable, "-m", "jasper.accessories.mic_env"]

    absent = subprocess.run(argv, env=env, capture_output=True, text=True)
    assert absent.returncode != 0
    assert absent.stdout.strip() == ""

    path.write_text(f"{MANUAL_MIC_SOURCES_KEY}=wiim_remote_2=udp:9892\n")
    present = subprocess.run(argv, env=env, capture_output=True, text=True)
    assert present.returncode == 0
    assert present.stdout.strip() == "wiim_remote_2"
