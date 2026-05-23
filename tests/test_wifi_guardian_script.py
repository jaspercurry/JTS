"""Tests for deploy/bin/jasper-wifi-guardian.

Pure-bash policy script, tested via subprocess.run with a fake nmcli
that records its argv and returns canned stdout — same pattern as
tests/test_aec_reconcile.py.

The fake nmcli responds based on the first non-flag argument:

  - `connection show --active`      -> active connection list
  - `connection show`               -> all profile names
  - `connection show <NAME>`        -> profile details (SSID lookup)
  - `device wifi connect <SSID>`    -> connect attempt
  - `connection up <NAME>`          -> activate saved profile
  - `connection delete <NAME>`      -> profile cleanup

Each test sets up the JASPER_NMCLI_* env vars to control the fake's
canned responses for that scenario, then asserts on:
  1. The structured `event=wifi_guardian.<action>` line in stderr
  2. The expected sequence of nmcli invocations in the log
  3. The exit code

Tests cover all five cases from the design doc §3.5 plus PSK
redaction and the wpa-eap skip path.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-wifi-guardian"


def _fake_nmcli(tmp_path: Path) -> tuple[Path, Path]:
    """Create a fake nmcli at tmp_path/nmcli that:
      - logs its full argv to tmp_path/nmcli.log
      - reads canned responses from env vars
      - returns the canned exit code

    Each scenario passes the canned responses via environment so we
    don't need to overwrite the fake between calls.
    """
    log = tmp_path / "nmcli.log"
    fake = tmp_path / "nmcli"
    fake.write_text(r"""#!/usr/bin/env bash
# Record argv (with the password value scrubbed for PSK assertions).
scrub=""
prev=""
for a in "$@"; do
    if [[ "$prev" == "password" ]]; then
        scrub+=" ***"
    else
        scrub+=" $a"
    fi
    prev="$a"
done
printf '%s\n' "${scrub# }" >> "$JASPER_NMCLI_LOG"

# Also log the raw argv (with PSK) to a separate file so tests can
# assert on what nmcli ACTUALLY received vs what we log publicly.
printf '%s\n' "$*" >> "${JASPER_NMCLI_LOG}.raw"

# Walk argv recognizing:
#  - flag-with-arg: -t -s --terse --show-secrets (no consume next)
#  - flag-with-arg-consumes-next: -f --fields --wait
#  - field-name: 802-11-* (positional-looking but actually a field arg
#    that follows -f / --fields)
#  - positional: everything else
# This lets us recover the dispatch tokens (op, sub, target) cleanly.
positional=()
skip_next=0
for a in "$@"; do
    if [[ "$skip_next" == "1" ]]; then
        skip_next=0
        continue
    fi
    case "$a" in
        -t|-s|--terse|--show-secrets|--active) ;;
        -f|--fields|--wait)
            skip_next=1
            ;;
        802-11-*) ;;
        *)
            # only count actual operands after the flags
            if [[ "${a:0:1}" == "-" ]]; then
                continue
            fi
            positional+=("$a")
            ;;
    esac
done

# Whether --active appeared anywhere in argv (separate from positional
# walking because nmcli accepts it positionally too).
active_flag=0
for a in "$@"; do
    [[ "$a" == "--active" ]] && active_flag=1
done

# Print canned stdout based on what's being asked.
op="${positional[0]:-}"
sub="${positional[1]:-}"
case "$op $sub" in
    "connection show")
        third="${positional[2]:-}"
        if [[ "$active_flag" == "1" ]]; then
            printf '%s' "${JASPER_NMCLI_ACTIVE:-}"
        elif [[ -z "$third" ]]; then
            printf '%s' "${JASPER_NMCLI_ALL_PROFILES:-}"
        else
            # `connection show <NAME>` — return SSID lookup
            printf '%s' "${JASPER_NMCLI_PROFILE_DETAILS:-}"
        fi
        ;;
    "device wifi")
        # `device wifi connect <SSID> [password X]`
        if [[ -n "${JASPER_NMCLI_CONNECT_STDERR:-}" ]]; then
            printf '%s' "${JASPER_NMCLI_CONNECT_STDERR}" >&2
        fi
        exit "${JASPER_NMCLI_CONNECT_RC:-0}"
        ;;
    "connection up")
        exit "${JASPER_NMCLI_UP_RC:-0}"
        ;;
    "connection delete")
        exit 0
        ;;
esac
exit 0
""")
    fake.chmod(0o755)
    return fake, log


def _run_guardian(
    tmp_path: Path,
    stash_contents: str | None,
    *,
    nmcli_env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the guardian script with a fake nmcli + stash file."""
    fake_nmcli, nmcli_log = _fake_nmcli(tmp_path)
    stash_path = tmp_path / "wifi_guardian.env"
    if stash_contents is not None:
        stash_path.write_text(stash_contents)
    env = os.environ.copy()
    env.update({
        "JASPER_WIFI_STASH_FILE": str(stash_path),
        "JASPER_NMCLI": str(fake_nmcli),
        "JASPER_NMCLI_LOG": str(nmcli_log),
    })
    if nmcli_env:
        env.update(nmcli_env)
    proc = subprocess.run(
        ["bash", str(SCRIPT), *(args or [])],
        check=False,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    return proc, nmcli_log


def _stash(ssid: str = "Home", psk: str = "p", key_mgmt: str = "wpa-psk") -> str:
    return (
        f"JASPER_WIFI_SSID={ssid}\n"
        f"JASPER_WIFI_PSK={psk}\n"
        f"JASPER_WIFI_KEY_MGMT={key_mgmt}\n"
    )


def _nmcli_log(p: Path) -> str:
    return p.read_text() if p.exists() else ""


def _nmcli_raw_log(p: Path) -> str:
    raw = p.with_name(p.name + ".raw")
    return raw.read_text() if raw.exists() else ""


# ----- Case 0: no stash -----


def test_guardian_no_stash_is_noop(tmp_path):
    """Without a stash file at all the guardian emits `absent` and
    returns 0. The systemd unit's ConditionPathExists= already skips
    in this case; this test covers the manual-invocation path."""
    proc, _ = _run_guardian(tmp_path, stash_contents=None)
    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_guardian.absent" in proc.stderr


def test_guardian_empty_stash_is_noop(tmp_path):
    """Stash file exists but JASPER_WIFI_SSID is empty (operator
    cleared it by hand)."""
    proc, _ = _run_guardian(tmp_path, stash_contents="JASPER_WIFI_SSID=\n")
    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_guardian.absent" in proc.stderr
    assert "stash_empty" in proc.stderr


# ----- Case 1: steady state -----


def test_guardian_steady_state(tmp_path):
    """Active SSID matches the stash → no-op, log steady_state."""
    proc, log = _run_guardian(
        tmp_path,
        _stash(ssid="Home"),
        nmcli_env={
            "JASPER_NMCLI_ACTIVE": "Home:802-11-wireless\n",
            "JASPER_NMCLI_PROFILE_DETAILS": "802-11-wireless.ssid:Home\n",
            "JASPER_NMCLI_ALL_PROFILES": "Home\n",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_guardian.steady_state" in proc.stderr
    # No connect or activate attempts.
    nm = _nmcli_log(log)
    assert "device wifi connect" not in nm
    assert "connection up Home" not in nm


# ----- Case 2: stash stale (MOST IMPORTANT defensive test) -----


def test_guardian_stash_stale_does_not_stomp(tmp_path):
    """User is currently on `Cafe` (manually connected via SSH) but
    stash points at `Home`. Guardian MUST NOT disconnect Cafe to
    chase Home — log stash_stale and exit clean. This is the most
    important defensive behaviour: a wrong action here would
    disconnect a working network mid-operator-session."""
    proc, log = _run_guardian(
        tmp_path,
        _stash(ssid="Home"),
        nmcli_env={
            "JASPER_NMCLI_ACTIVE": "Cafe:802-11-wireless\n",
            "JASPER_NMCLI_PROFILE_DETAILS": "802-11-wireless.ssid:Cafe\n",
            "JASPER_NMCLI_ALL_PROFILES": "Cafe\n",  # Home isn't in the list either
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_guardian.stash_stale" in proc.stderr
    assert "active=Cafe" in proc.stderr
    assert "stash=Home" in proc.stderr
    # Critically: NO connect attempt to Home and NO disconnect of Cafe.
    nm = _nmcli_log(log)
    assert "device wifi connect Home" not in nm
    assert "connection down" not in nm


# ----- Case 3: profile exists, just bring it up -----


def test_guardian_activates_present_profile(tmp_path):
    """No active WiFi, but `Home` is in the saved-profiles list →
    `nmcli connection up Home` to activate it."""
    proc, log = _run_guardian(
        tmp_path,
        _stash(ssid="Home"),
        nmcli_env={
            "JASPER_NMCLI_ACTIVE": "",  # no active wifi
            "JASPER_NMCLI_ALL_PROFILES": "Home\nGuest\n",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_guardian.activate" in proc.stderr
    assert "event=wifi_guardian.activate_ok" in proc.stderr
    nm = _nmcli_log(log)
    assert "connection up Home" in nm
    # Critically: no `device wifi connect` (that would create a NEW
    # profile and potentially clobber the existing PSK).
    assert "device wifi connect" not in nm


def test_guardian_activate_failure_exits_nonzero(tmp_path):
    """Profile exists but `connection up` fails (PSK changed, AP gone).
    The guardian does NOT fall through to recreate — that would stomp
    the existing profile with the stashed PSK, possibly wrong now.
    Operator's job from here."""
    proc, log = _run_guardian(
        tmp_path,
        _stash(ssid="Home"),
        nmcli_env={
            "JASPER_NMCLI_ACTIVE": "",
            "JASPER_NMCLI_ALL_PROFILES": "Home\n",
            "JASPER_NMCLI_UP_RC": "4",
        },
    )
    assert proc.returncode == 1
    assert "event=wifi_guardian.activate_fail" in proc.stderr
    # Did NOT fall through to recreate.
    nm = _nmcli_log(log)
    assert "device wifi connect" not in nm


# ----- Case 4: THE INCIDENT — recreate missing profile -----


def test_guardian_recreates_missing_profile(tmp_path):
    """The 2026-05-23 incident path. Stash present, no active wifi,
    no profile in the saved list → `nmcli dev wifi connect SSID
    password PSK` to recreate."""
    proc, log = _run_guardian(
        tmp_path,
        _stash(ssid="Home", psk="myhomepsk", key_mgmt="wpa-psk"),
        nmcli_env={
            "JASPER_NMCLI_ACTIVE": "",
            "JASPER_NMCLI_ALL_PROFILES": "",  # nothing saved
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_guardian.recreate_attempt" in proc.stderr
    assert "event=wifi_guardian.recreate_ok" in proc.stderr
    # Raw log: did nmcli actually receive the PSK?
    raw = _nmcli_raw_log(log)
    assert "device wifi connect Home password myhomepsk" in raw


def test_guardian_recreates_open_network(tmp_path):
    """Open networks (key_mgmt=none, empty PSK): connect without
    `password ARG`. Passing an empty password to nmcli would itself
    fail."""
    proc, log = _run_guardian(
        tmp_path,
        _stash(ssid="GuestWifi", psk="", key_mgmt="none"),
        nmcli_env={
            "JASPER_NMCLI_ACTIVE": "",
            "JASPER_NMCLI_ALL_PROFILES": "",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_guardian.recreate_ok" in proc.stderr
    raw = _nmcli_raw_log(log)
    assert "device wifi connect GuestWifi" in raw
    assert "password" not in raw


def test_guardian_recreate_failure_cleans_up_broken_profile(tmp_path):
    """nmcli's `connect` creates the profile BEFORE attempting
    activation. If activation fails, the profile sits in saved as a
    broken entry. The guardian deletes it (mirrors `wifi_setup.connect_new`)
    so we don't accumulate garbage on every boot retry."""
    proc, log = _run_guardian(
        tmp_path,
        _stash(ssid="Home"),
        nmcli_env={
            "JASPER_NMCLI_ACTIVE": "",
            "JASPER_NMCLI_ALL_PROFILES": "",
            "JASPER_NMCLI_CONNECT_RC": "10",
            "JASPER_NMCLI_CONNECT_STDERR": (
                "Error: Connection activation failed: (7) Secrets were required, "
                "but not provided.\n"
            ),
        },
    )
    assert proc.returncode == 10
    assert "event=wifi_guardian.recreate_fail" in proc.stderr
    # Cleanup invoked.
    nm = _nmcli_log(log)
    assert "connection delete Home" in nm


# ----- WPA-Enterprise skip -----


def test_guardian_skips_enterprise(tmp_path):
    """Stash with key_mgmt=wpa-eap (hand-edited; wizard rejects at
    write-time too) → skip with `event=wifi_guardian.skip`. Do not
    invoke nmcli connect — we don't have the certificate identity."""
    proc, log = _run_guardian(
        tmp_path,
        _stash(ssid="EnterpriseNet", psk="ignored", key_mgmt="wpa-eap"),
        nmcli_env={
            "JASPER_NMCLI_ACTIVE": "",
            "JASPER_NMCLI_ALL_PROFILES": "",
        },
    )
    assert proc.returncode == 0
    assert "event=wifi_guardian.skip" in proc.stderr
    assert "reason=enterprise" in proc.stderr
    nm = _nmcli_log(log)
    assert "device wifi connect" not in nm


# ----- PSK redaction -----


def test_guardian_logs_redact_psk(tmp_path):
    """The PSK must never appear in the structured log output —
    operators tail journals, screenshot pages, paste into bug reports."""
    secret_psk = "highly-confidential-psk-do-not-leak"
    proc, log = _run_guardian(
        tmp_path,
        _stash(ssid="Home", psk=secret_psk, key_mgmt="wpa-psk"),
        nmcli_env={
            "JASPER_NMCLI_ACTIVE": "",
            "JASPER_NMCLI_ALL_PROFILES": "",
            "JASPER_NMCLI_CONNECT_RC": "4",
            "JASPER_NMCLI_CONNECT_STDERR": (
                f"Error: device wifi connect '{secret_psk}' failed: "
                f"--password {secret_psk} rejected\n"
            ),
        },
    )
    # Public-facing nmcli log has password scrubbed.
    nm = _nmcli_log(log)
    assert secret_psk not in nm
    assert "password ***" in nm
    # And critically: the guardian's own stderr (what lands in
    # journalctl) has no PSK either — even though we re-emit the
    # nmcli stderr on recreate_fail.
    assert secret_psk not in proc.stderr
    # The scrubbed `password ***` should show up in the err= field on
    # recreate_fail.
    assert "password ***" in proc.stderr


def test_guardian_log_emits_to_stderr_not_stdout(tmp_path):
    """structured `event=` lines need to land on stderr so
    journalctl captures them but `bash -x` style stdout consumers
    don't trip on them. Mirror jasper-aec-reconcile."""
    proc, _ = _run_guardian(tmp_path, stash_contents=None)
    assert "event=wifi_guardian.absent" in proc.stderr
    assert "event=wifi_guardian.absent" not in proc.stdout


# ----- --reason passes through -----


def test_guardian_accepts_reason_argument(tmp_path):
    """systemd unit + udev rule pass --reason; ensure the script
    accepts it and includes it in the log() prefix (mirrors
    aec-reconcile, where the prefix is only on the human-readable
    `log()` lines, not on the structured `event=` lines).

    Use a stash scenario so the script reaches the `log()` call that
    summarises probe state — the no-stash path exits before it."""
    proc, _ = _run_guardian(
        tmp_path,
        _stash(ssid="Home"),
        nmcli_env={
            "JASPER_NMCLI_ACTIVE": "Home:802-11-wireless\n",
            "JASPER_NMCLI_PROFILE_DETAILS": "802-11-wireless.ssid:Home\n",
            "JASPER_NMCLI_ALL_PROFILES": "Home\n",
        },
        args=["--reason", "systemd"],
    )
    assert proc.returncode == 0
    assert "jasper-wifi-guardian[systemd]:" in proc.stderr
