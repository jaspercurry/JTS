"""WS1 Phase 3b-3 — pin the polkit rule that authorizes the non-root
`jasper-web` service user's NetworkManager access.

Once jasper-web drops to a non-root user, the /wifi/ wizard's nmcli operations
(scan / connect / forget / radio toggle / saved-PSK re-read) are mediated by
polkit instead of a uid-0 bypass. NM's implicit defaults DENY a sessionless
daemon for every one of those actions (verified on hardware: jts.local, NM 1.52,
polkit 126), so this rule is load-bearing — without it a non-root jasper-web
cannot manage Wi-Fi, the worst-case brick for a headless, often Ethernet-less
speaker. The rule lives at deploy/polkit/49-jasper-web.rules and is installed to
/etc/polkit-1/rules.d/ by install.sh's install_jasper_web_polkit.

These tests pin the invariants that, if broken, silently brick Wi-Fi management
or over-grant the most network-exposed daemon:

* it grants exactly the five NetworkManager actions wifi_setup.py drives;
* it keys on `subject.user` ONLY (a sessionless daemon has subject.active ==
  false, so gating on .active would never fire — the single most likely mistake);
* it does NOT grant systemctl/reboot (jasper-web restarts via the restart broker,
  not polkit) nor any unrelated action;
* fall-through is NOT_HANDLED (never NO, which would veto other grants);
* install.sh installs it in both install profiles.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "deploy/polkit/49-jasper-web.rules"
INSTALL_SH = ROOT / "deploy/install.sh"

# The NetworkManager polkit actions wifi_setup.py exercises. Source of truth for
# the grant set; keep in lockstep with deploy/polkit/49-jasper-web.rules and the
# rule's header comment that maps each to a wifi_setup.py operation.
NM_ACTIONS = (
    "org.freedesktop.NetworkManager.settings.modify.system",
    "org.freedesktop.NetworkManager.settings.modify.own",
    "org.freedesktop.NetworkManager.network-control",
    "org.freedesktop.NetworkManager.enable-disable-wifi",
    "org.freedesktop.NetworkManager.wifi.scan",
)


def _rule_text() -> str:
    assert RULES.is_file(), f"missing polkit rule at {RULES}"
    return RULES.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Strip `//` line comments so assertions look at the JS code, not the prose
    that intentionally names the anti-patterns (e.g. subject.active, netdev)."""
    out = []
    for line in text.splitlines():
        idx = line.find("//")
        out.append(line if idx == -1 else line[:idx])
    return "\n".join(out)


def test_rule_grants_the_five_nm_actions():
    """Each NM action the /wifi/ wizard needs must be granted in executable
    code — a missing one means that wizard operation silently fails under the
    dropped user (scan dead, can't connect, can't forget, radio stuck, or the
    guardian PSK stash goes empty)."""
    code = _code_only(_rule_text())
    missing = [a for a in NM_ACTIONS if a not in code]
    assert not missing, f"rule must grant NM actions: {missing}"
    assert "polkit.Result.YES" in code, "rule must return YES for the granted actions"


def test_rule_keys_on_subject_user_not_active():
    """A no-session system daemon has subject.active/.local == false; the rule
    MUST match subject.user and MUST NOT gate on subject.active/.local (the
    desktop idiom that would never fire here)."""
    code = _code_only(_rule_text())
    assert 'subject.user !== "jasper-web"' in code or \
        'subject.user == "jasper-web"' in code, (
        "rule must key on subject.user == 'jasper-web'"
    )
    assert "subject.active" not in code, (
        "rule must NOT gate on subject.active (false for a sessionless daemon)"
    )
    assert "subject.local" not in code, (
        "rule must NOT gate on subject.local (false for a sessionless daemon)"
    )


def test_rule_does_not_grant_systemctl_or_reboot():
    """jasper-web restarts units through jasper-control's restart broker (a
    UNIX socket + SO_PEERCRED), NOT polkit-mediated systemctl, and never reboots
    — so this rule must NOT grant manage-units / manage-unit-files / login1.
    Granting them would needlessly widen a compromise of the most network-exposed
    daemon into a system-control primitive."""
    code = _code_only(_rule_text())
    for forbidden in (
        "org.freedesktop.systemd1.manage-units",
        "org.freedesktop.systemd1.manage-unit-files",
        "org.freedesktop.login1.reboot",
        "org.freedesktop.login1.power-off",
    ):
        assert forbidden not in code, (
            f"jasper-web rule must NOT grant {forbidden} (it restarts via the "
            "broker, not polkit; see docs/HANDOFF-privilege-separation.md)."
        )


def test_rule_does_not_grant_unrelated_nm_actions():
    """Scope discipline: only the five wifi_setup.py actions are granted. NM's
    own sleep/wake/reload/checkpoint actions are never called by the wizard;
    granting them would expand the blast radius of a jasper-web compromise."""
    code = _code_only(_rule_text())
    for forbidden in (
        "org.freedesktop.NetworkManager.sleep-wake",
        "org.freedesktop.NetworkManager.enable-disable-network",
        "org.freedesktop.NetworkManager.checkpoint-rollback",
    ):
        assert forbidden not in code, f"rule must not grant {forbidden}"


def test_rule_fallthrough_is_not_handled_not_deny():
    """Fall-through must be NOT_HANDLED so this rule never blocks an unrelated
    action for the jasper-web user; a polkit.Result.NO fallthrough would veto
    future legitimate grants."""
    text = _rule_text()
    assert "polkit.Result.NOT_HANDLED" in text
    assert "polkit.Result.NO" not in text.replace("NOT_HANDLED", ""), (
        "rule must not return polkit.Result.NO (use NOT_HANDLED for fallthrough)"
    )


def test_install_sh_installs_the_rule():
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert "install_jasper_web_polkit" in sh, (
        "install.sh must define + call install_jasper_web_polkit"
    )
    assert "49-jasper-web.rules" in sh
    # Called in BOTH profiles (full + streambox), like the control rule, so a
    # future streambox web drop finds the grant already present. Def + 2 calls.
    assert sh.count("install_jasper_web_polkit") >= 3, (
        "install_jasper_web_polkit must be defined and called in both the full "
        "and streambox main() paths"
    )
