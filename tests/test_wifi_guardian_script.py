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

import errno
import os
import subprocess
import time
import warnings
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-wifi-guardian"

# --- Spawn resilience under load -------------------------------------------
#
# These tests exec a real `bash` subprocess (which itself fork()s nmcli/awk/
# sed). In a full hardware-free suite run on a loaded machine the OS can
# momentarily refuse to start a child process: posix_spawn()/fork() returns
# a transient errno. The dominant one is EAGAIN — fork() hitting process/
# memory/scheduler pressure during a sustained-load window (e.g. two full
# suites back-to-back) — with EMFILE/ENFILE/ENOMEM as the rarer FD/memory
# variants (macOS's default 256 RLIMIT_NOFILE soft limit makes EMFILE a
# real, if secondary, risk for an FD-heavy suite). Because the failure is
# windowed, EVERY subprocess.run in that window fails at once: ~16 tests in
# this file (13) plus the sibling test_aec_reconcile.py (3) ERROR *together*
# under load while every one of them passes in isolation.
#
# Reproduced deterministically by exhausting FDs around the real helper
# (≤4 free FDs -> every scenario raises; ≥6 -> all pass); the natural
# trigger is the same spawn failure, just provoked by load rather than a
# squeeze. The handling below covers the whole transient-errno family, so
# it is correct regardless of which one fires first on a given box.
#
# Retrying the spawn is the documented handling of these "temporarily
# unavailable" errnos. It changes nothing about what the guardian script
# does or what we assert: the child either never started (transient errno,
# no side effects) or it started but its own fork() of a helper failed
# (recognisable kernel/shell text that never appears in a healthy run). A
# genuine hang still fails loudly via the subprocess timeout; a real
# non-zero exit or assertion mismatch is surfaced unchanged.
#
# The retry is NOT silent: each one emits a `_TransientSpawnRetryWarning`
# so a *persistently* degraded machine (real FD/process leak, starved CI)
# leaves a breadcrumb in pytest's warnings summary instead of the retry
# quietly papering over it — JTS's no-silent-failures rule, applied to the
# test harness. A momentary blip warns 1-2× and passes; a real problem
# warns on every attempt and then fails loudly via the bounded re-raise.
_TRANSIENT_SPAWN_ERRNOS = frozenset({
    errno.EAGAIN,   # 35: resource temporarily unavailable
    errno.EMFILE,   # 24: too many open files (this process)
    errno.ENFILE,   # 23: too many open files (system-wide)
    errno.ENOMEM,   # 12: cannot allocate memory
})

# Same exhaustion can hit the *child's* fork()s; bash then prints one of
# these and the script's output is garbage. These strings never occur in a
# healthy run, so treating them as retryable cannot mask a logic bug.
_TRANSIENT_OUTPUT_SIGNATURES = (
    "Resource temporarily unavailable",
    "Cannot allocate memory",
    "Too many open files",
    "fork: retry",
)

# Bounded — a persistently-exhausted machine still fails loudly rather than
# hanging. Total worst-case backoff ≈ 0.05*(1+..+6) ≈ 1.05 s.
_SPAWN_RETRIES = 6
# Generous: the fake nmcli returns instantly, so a real run is milliseconds.
# This only trips on a genuine hang, turning it into a loud failure instead
# of a wedged suite.
_SPAWN_TIMEOUT_S = 60


class _TransientSpawnRetryWarning(UserWarning):
    """Emitted once per transient-spawn retry in `_run_guardian`.

    Its purpose is observability, not control flow: a momentary blip warns
    1-2× and the test still passes, but a persistently-degraded machine
    (real FD/process leak, starved CI) warns on every attempt and the count
    shows up in pytest's warnings summary — so the retry can never silently
    paper over a real problem before the bounded re-raise fails it loudly.
    """


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


def _output_looks_transient(proc: subprocess.CompletedProcess[str]) -> bool:
    """True if the child's output bears an OS resource-exhaustion signature
    (its own fork() of nmcli/awk/sed failed under load). Such text never
    appears in a healthy run, so retrying on it can't hide a logic bug."""
    blob = (proc.stdout or "") + (proc.stderr or "")
    return any(sig in blob for sig in _TRANSIENT_OUTPUT_SIGNATURES)


def _warn_retry(attempt: int, reason: str) -> None:
    """Leave a breadcrumb for each transient-spawn retry (see
    `_TransientSpawnRetryWarning`)."""
    warnings.warn(
        f"jasper-wifi-guardian test: retrying bash spawn "
        f"(attempt {attempt + 1}/{_SPAWN_RETRIES + 1}) after transient "
        f"failure: {reason}",
        _TransientSpawnRetryWarning,
        stacklevel=2,
    )


def _run_guardian(
    tmp_path: Path,
    stash_contents: str | None,
    *,
    nmcli_env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the guardian script with a fake nmcli + stash file.

    Resilient to transient process-spawn exhaustion under load (see
    `_TRANSIENT_SPAWN_ERRNOS` above): each attempt builds a fresh fake
    nmcli + log dir so a retry never accumulates stale nmcli-log lines, and
    the spawn is retried only on transient OS resource errors — never on a
    real non-zero exit, a hang (which fails loudly via the timeout), or an
    assertion mismatch. Healthy runs take the first attempt and behave
    exactly as before.
    """
    last_exc: OSError | None = None
    for attempt in range(_SPAWN_RETRIES + 1):
        work = tmp_path / f"attempt{attempt}"
        work.mkdir()
        fake_nmcli, nmcli_log = _fake_nmcli(work)
        stash_path = work / "wifi_guardian.env"
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
        try:
            proc = subprocess.run(
                ["bash", str(SCRIPT), *(args or [])],
                check=False,
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=_SPAWN_TIMEOUT_S,
            )
        except OSError as exc:
            # The child never started (no side effects). Retry only if the
            # OS said "temporarily unavailable"; otherwise surface it.
            if exc.errno not in _TRANSIENT_SPAWN_ERRNOS:
                raise
            last_exc = exc
            _warn_retry(attempt, f"spawn OSError errno={exc.errno} ({exc.strerror})")
            time.sleep(0.05 * (attempt + 1))
            continue
        # The child started but may have hit the same exhaustion when
        # fork()ing a helper. Retry on that signature unless this was the
        # last attempt (then return it so the assertion fails loudly).
        if attempt < _SPAWN_RETRIES and _output_looks_transient(proc):
            _warn_retry(attempt, "child fork() failure signature in subprocess output")
            time.sleep(0.05 * (attempt + 1))
            continue
        return proc, nmcli_log
    # Every attempt raised a transient spawn error: fail loudly with it.
    assert last_exc is not None  # only reached via the except-continue path
    raise last_exc


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


def test_guardian_matches_netplan_profile_by_ssid(tmp_path):
    """Pi Imager/netplan profiles are not named after the SSID. The guardian
    must activate the profile whose 802-11-wireless.ssid matches the stash,
    not recreate a duplicate profile named like the SSID."""
    proc, log = _run_guardian(
        tmp_path,
        _stash(ssid="Home"),
        nmcli_env={
            "JASPER_NMCLI_ACTIVE": "",
            "JASPER_NMCLI_ALL_PROFILES": "netplan-wlan0-Home\nGuest\n",
            "JASPER_NMCLI_PROFILE_DETAILS": "802-11-wireless.ssid:Home\n",
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_guardian.activate" in proc.stderr
    assert "profile=netplan-wlan0-Home" in proc.stderr
    nm = _nmcli_log(log)
    assert "connection up netplan-wlan0-Home" in nm
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


# ----- Spawn resilience under load (the flaky-under-load regression) -----
#
# Repro of the original flake: in a full suite under sustained load (two
# runs back-to-back / busy machine) posix_spawn()/fork() intermittently
# fails with a transient errno (EAGAIN dominant; EMFILE/ENOMEM rarer) and
# every subprocess test in this file errors at once. These pin the
# harness's retry behaviour so the flake stays fixed across refactors —
# without weakening any assertion above. `time.sleep` is neutered in the
# retrying cases so they stay instant (the assertions are on attempt
# *count*, not on real backoff latency).


def test_run_guardian_retries_transient_spawn_oserror(tmp_path, monkeypatch):
    """The test's own subprocess.run can fail to spawn bash with a
    transient EAGAIN/EMFILE *before the child runs* — the documented
    cause of ~16 subprocess tests erroring together under load. The
    harness must retry and still get a correct result; the retried spawn
    has no side effects, so nothing the real tests assert is weakened."""
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)  # no real backoff
    real_run = subprocess.run
    calls = {"n": 0}

    def flaky_run(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise BlockingIOError(errno.EMFILE, "Too many open files")
        return real_run(*a, **k)

    monkeypatch.setattr(subprocess, "run", flaky_run)
    proc, _ = _run_guardian(tmp_path, stash_contents=None)
    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_guardian.absent" in proc.stderr
    assert calls["n"] == 4  # 3 transient spawn failures, then the real run


def test_run_guardian_retries_transient_child_fork_failure(tmp_path, monkeypatch):
    """The child bash can itself fail to fork() a helper (nmcli/awk/sed)
    under the same load, printing 'fork: Resource temporarily unavailable'
    and producing garbage output. The harness must treat that signature as
    transient and retry, not surface the garbage as a spurious failure."""
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)  # no real backoff
    real_run = subprocess.run
    calls = {"n": 0}

    def flaky_run(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            return subprocess.CompletedProcess(
                a[0] if a else k["args"], 1,
                stdout="",
                stderr="bash: fork: Resource temporarily unavailable\n",
            )
        return real_run(*a, **k)

    monkeypatch.setattr(subprocess, "run", flaky_run)
    proc, _ = _run_guardian(tmp_path, stash_contents=None)
    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_guardian.absent" in proc.stderr
    assert calls["n"] == 3  # 2 transient-output results, then the real run


def test_run_guardian_warns_on_each_transient_retry(tmp_path, monkeypatch):
    """Retries are NOT silent: each transient retry emits a
    `_TransientSpawnRetryWarning` so a persistently-degraded machine leaves
    a breadcrumb in pytest's warnings summary instead of the retry quietly
    masking it — JTS's no-silent-failures rule, applied to the test harness.
    One blip warns once and still passes; a real problem warns every time
    and then fails loudly via the bounded re-raise."""
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)  # no real backoff
    real_run = subprocess.run
    calls = {"n": 0}

    def flaky_run(*a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
        return real_run(*a, **k)

    monkeypatch.setattr(subprocess, "run", flaky_run)
    with pytest.warns(_TransientSpawnRetryWarning) as record:
        proc, _ = _run_guardian(tmp_path, stash_contents=None)
    assert proc.returncode == 0, proc.stderr
    assert "event=wifi_guardian.absent" in proc.stderr
    # One warning per retry (2 transient failures -> 2 warnings), and the
    # message carries the attempt counter so a tail of them is diagnosable.
    retries = [w for w in record if issubclass(w.category, _TransientSpawnRetryWarning)]
    assert len(retries) == 2
    assert "attempt 1/" in str(retries[0].message)


def test_run_guardian_does_not_retry_real_oserror(tmp_path, monkeypatch):
    """A non-transient OSError (e.g. ENOENT — bash genuinely missing) is a
    real bug, not load. It must surface on the first attempt, never be
    retried into a confusing multi-second hang."""
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(FileNotFoundError):
        _run_guardian(tmp_path, stash_contents=None)
    assert calls["n"] == 1  # surfaced immediately, no retry


def test_run_guardian_surfaces_persistent_spawn_failure(tmp_path, monkeypatch):
    """Retry is bounded: a machine that is *persistently* out of spawn
    resources fails loudly (re-raises) rather than looping forever or
    silently passing."""
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)  # no real backoff
    calls = {"n": 0}

    def always_eagain(*a, **k):
        calls["n"] += 1
        raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(subprocess, "run", always_eagain)
    with pytest.raises(BlockingIOError):
        _run_guardian(tmp_path, stash_contents=None)
    assert calls["n"] == _SPAWN_RETRIES + 1  # bounded attempts, then raise
