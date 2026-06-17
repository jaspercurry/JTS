"""WS1 Phase 3b-2 — pin the polkit rule that authorizes the non-root
`jasper-control` service user.

Once jasper-control drops to a non-root user, its in-process restart broker and
its supervisors (system_supervisor reboot, shairport_supervisor restart, the
/system buttons) can only `systemctl`/reboot through polkit. The rule lives at
deploy/polkit/49-jasper-control.rules and is installed to
/etc/polkit-1/rules.d/ by install.sh's install_jasper_control_polkit.

These tests pin the invariants that, if broken, silently disable a
resilience-critical path (a unit the broker accepts gets polkit-denied, or the
recovery reboot stops firing):

* the rule's MANAGED_UNITS and START_ONLY_UNITS allowlists are **set-equal** to
  the matching jasper.control.restart_broker constants (the broker docstring
  promises these never drift — restart_broker.py "derives the polkit rule ...
  from the same constants");
* it grants manage-units, manage-unit-files, and the login1 reboot/power-off
  actions (incl. the -multiple-sessions variants that fire when an operator is
  SSH'd in — verified on hardware);
* it keys on `subject.user` ONLY (a sessionless daemon has subject.active ==
  false, so gating on .active would never fire — the single most likely
  implementation mistake);
* install.sh installs it.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.control.restart_broker import MANAGED_UNITS, POLKIT_MANAGE_UNITS, START_ONLY_UNITS

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "deploy/polkit/49-jasper-control.rules"
INSTALL_SH = ROOT / "deploy/install.sh"


def _rule_text() -> str:
    assert RULES.is_file(), f"missing polkit rule at {RULES}"
    return RULES.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Strip `//` line comments so assertions look at the JS code, not the
    prose that intentionally names the anti-patterns (e.g. subject.active)."""
    out = []
    for line in text.splitlines():
        idx = line.find("//")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


def _units_in_rule_array(text: str, name: str) -> set[str]:
    """Extract the units from a JS `var NAME = [ ... ];` array."""
    m = re.search(rf"var {name} = \[(.*?)\];", text, re.DOTALL)
    assert m, f"rule must declare a `var {name} = [...]` array"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def test_rule_unit_allowlists_equal_broker_constants():
    """The polkit allowlists and the broker allowlists are one source of truth
    — drift means a unit the broker accepts could be polkit-denied (silent
    restart failure) or vice-versa."""
    text = _rule_text()
    managed_in_rule = _units_in_rule_array(text, "MANAGED_UNITS")
    start_only_in_rule = _units_in_rule_array(text, "START_ONLY_UNITS")

    assert managed_in_rule == set(MANAGED_UNITS), (
        "deploy/polkit/49-jasper-control.rules MANAGED_UNITS drifted from "
        "jasper.control.restart_broker.MANAGED_UNITS.\n"
        f"  only in rule:   {sorted(managed_in_rule - set(MANAGED_UNITS))}\n"
        f"  only in broker: {sorted(set(MANAGED_UNITS) - managed_in_rule)}"
    )
    assert start_only_in_rule == set(START_ONLY_UNITS), (
        "deploy/polkit/49-jasper-control.rules START_ONLY_UNITS drifted from "
        "jasper.control.restart_broker.START_ONLY_UNITS.\n"
        f"  only in rule:   {sorted(start_only_in_rule - set(START_ONLY_UNITS))}\n"
        f"  only in broker: {sorted(set(START_ONLY_UNITS) - start_only_in_rule)}"
    )
    assert managed_in_rule | start_only_in_rule == set(POLKIT_MANAGE_UNITS), (
        "deploy/polkit/49-jasper-control.rules union drifted from "
        "jasper.control.restart_broker.POLKIT_MANAGE_UNITS"
    )


def test_rule_keys_on_subject_user_not_active():
    """A no-session system daemon has subject.active/.local == false; the rule
    MUST match subject.user and MUST NOT gate on subject.active/.local (the
    desktop idiom that would never fire here)."""
    code = _code_only(_rule_text())
    assert 'subject.user !== "jasper-control"' in code or \
        'subject.user == "jasper-control"' in code, (
        "rule must key on subject.user == 'jasper-control'"
    )
    assert "subject.active" not in code, (
        "rule must NOT gate on subject.active (false for a sessionless daemon)"
    )
    assert "subject.local" not in code, (
        "rule must NOT gate on subject.local (false for a sessionless daemon)"
    )


def test_rule_grants_manage_units():
    code = _code_only(_rule_text())
    assert "org.freedesktop.systemd1.manage-units" in code, (
        "rule must grant manage-units (start/stop/restart/try-restart/reset-failed)"
    )


def test_rule_does_not_grant_manage_unit_files():
    """manage-unit-files (enable/disable) is DELIBERATELY not granted: it can't
    be unit-scoped (systemd passes NULL details) and `systemctl restart`
    consults it, so granting it re-opens restart-of-any-unit — defeating the
    manage-units allowlist (hardware-verified on systemd 257). Pin the decision
    so a future edit can't silently re-add the YES branch. (The action id may be
    named in an explanatory comment, but must not appear in the executable code.)"""
    code = _code_only(_rule_text())
    assert "manage-unit-files" not in code, (
        "rule must NOT reference manage-unit-files in executable code — granting "
        "it re-opens restart-of-any-unit (see the rule's header comment)."
    )


def test_rule_grants_reboot_and_poweroff_with_all_variants():
    """logind picks the base action when idle, -multiple-sessions when an
    operator is SSH'd in (verified on hardware), and -ignore-inhibit when a
    block inhibitor is held — so the recovery reboot must be granted all three
    pairs to stay bulletproof."""
    text = _rule_text()
    for action in (
        "org.freedesktop.login1.reboot",
        "org.freedesktop.login1.reboot-multiple-sessions",
        "org.freedesktop.login1.reboot-ignore-inhibit",
        "org.freedesktop.login1.power-off",
        "org.freedesktop.login1.power-off-multiple-sessions",
        "org.freedesktop.login1.power-off-ignore-inhibit",
    ):
        assert action in text, f"rule must grant {action}"


def test_rule_fallthrough_is_not_handled_not_deny():
    """Fall-through must be NOT_HANDLED so this rule never blocks an unrelated
    action for the jasper-control user; a `polkit.Result.NO` fallthrough would
    veto future legitimate grants."""
    text = _rule_text()
    assert "polkit.Result.NOT_HANDLED" in text
    # The only definitive verdict the rule returns is YES (grants). It must not
    # return NO anywhere.
    assert "polkit.Result.NO" not in text.replace("NOT_HANDLED", ""), (
        "rule must not return polkit.Result.NO (use NOT_HANDLED for fallthrough)"
    )


def test_install_sh_installs_the_rule():
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert "install_jasper_control_polkit" in sh, (
        "install.sh must define + call install_jasper_control_polkit"
    )
    assert "/etc/polkit-1/rules.d" in sh, (
        "install.sh must install the rule into /etc/polkit-1/rules.d"
    )
    assert "49-jasper-control.rules" in sh
    # Called in BOTH profiles (full + streambox both run jasper-control).
    assert sh.count("install_jasper_control_polkit") >= 3, (
        "install_jasper_control_polkit must be defined and called in both the "
        "full and streambox main() paths"
    )
