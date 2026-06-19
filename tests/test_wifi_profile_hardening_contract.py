"""Drift guard for the NetworkManager profile-hardening contract.

The same resilience triple — autoconnect on, retry forever, power-save off —
is written from THREE places so a recovered profile is as resilient as a
freshly-connected one:

  1. the /wifi/ wizard          jasper.web.wifi_setup._harden_wifi_profile
  2. install-time              deploy/lib/install/renderers.sh  tune_wifi_for_airplay
  3. WiFi recovery             deploy/bin/jasper-wifi-guardian  harden_profile

They can't share code (one Python, two bash standalone root scripts), so this
test pins the key+value set across all three. If a future change adds a key or
changes a value in one writer and not the others, this fails — keeping the
contract single-source-of-truth in spirit even though it lives in three files.
See AGENTS.md "the same NetworkManager profile-hardening triple".
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

# The canonical contract. `connection.autoconnect-retries 0` is NM's
# retry-forever value (see NetworkManager docs: 0 = forever, -1 = global
# default of 4).
REQUIRED_PAIRS = [
    ("connection.autoconnect", "yes"),
    ("connection.autoconnect-retries", "0"),
    ("802-11-wireless.powersave", "2"),
]


def _python_harden_argv() -> list[str]:
    """The actual nmcli argv the wizard's hardening hook issues (mocked).

    Tests real behaviour, not source text — so a docstring that merely
    mentions the keys can't make this pass."""
    import jasper.web.wifi_setup as wifi_setup

    captured: dict[str, list[str]] = {}

    def fake(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(wifi_setup, "_run_nmcli", side_effect=fake):
        wifi_setup._harden_wifi_profile("SomeProfile")
    return captured["cmd"]


def _bash_func_body(path: Path, name: str) -> str:
    """Return a bash function's body with comment lines stripped, so prose
    that names the keys can't false-pass the value assertions."""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"{name}\(\) \{{(?P<body>.*?)\n\}}", text, flags=re.S)
    assert m is not None, f"could not find bash function {name}() in {path}"
    body = "\n".join(
        line for line in m.group("body").splitlines()
        if not line.lstrip().startswith("#")
    )
    # Collapse line-continuations + whitespace so `a \<nl>  b` reads as `a b`.
    return re.sub(r"\s+", " ", body.replace("\\\n", " "))


def test_python_wizard_hardening_argv_matches_contract():
    argv = _python_harden_argv()
    assert argv[:3] == ["nmcli", "connection", "modify"]
    for key, value in REQUIRED_PAIRS:
        assert key in argv, f"{key} missing from wizard hardening argv"
        assert argv[argv.index(key) + 1] == value, (
            f"wizard hardening sets {key} to {argv[argv.index(key) + 1]!r}, "
            f"contract is {value!r}"
        )


def test_guardian_harden_profile_matches_contract():
    body = _bash_func_body(ROOT / "deploy/bin/jasper-wifi-guardian", "harden_profile")
    for key, value in REQUIRED_PAIRS:
        assert f"{key} {value}" in body, (
            f"jasper-wifi-guardian harden_profile is missing `{key} {value}`"
        )


def test_install_tune_wifi_matches_contract():
    body = _bash_func_body(
        ROOT / "deploy/lib/install/renderers.sh", "tune_wifi_for_airplay"
    )
    for key, value in REQUIRED_PAIRS:
        assert f"{key} {value}" in body, (
            f"tune_wifi_for_airplay is missing `{key} {value}`"
        )
