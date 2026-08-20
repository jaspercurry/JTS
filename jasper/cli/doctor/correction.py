# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — correction domain.

Re-homed verbatim from the original monolithic
``jasper/cli/doctor.py``; see ``jasper/cli/doctor/__init__.py``
for the package overview and ``_registry.py`` for how order is
preserved. No check logic changed in the split."""
from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import time
from datetime import datetime as _datetime, timezone
from pathlib import Path
from ._registry import doctor_check
from ._shared import CheckResult, _run
from ...active_speaker.seat_level_reference import (
    load_seat_level_reference,
    seat_level_reference_state_path,
    seat_level_reference_volume_db,
)
from ...active_speaker.session_volume_plan import MEASUREMENT_REFERENCE_VOLUME_DB
from ...web._systemd import DEFERRED_EXIT_LOG_PERIOD_SEC

def _correction_root() -> Path:
    return Path(
        os.environ.get("JASPER_CORRECTION_ROOT", "/var/lib/jasper/correction")
    )

@doctor_check(order=25, group="correction")
def check_correction_web_service() -> CheckResult:
    """Socket activation is the liveness contract for /correction/.

    The service itself is expected to be inactive after its idle
    timeout; the socket must remain active so nginx can spawn the
    wizard on demand.
    """
    socket_state = _run(
        ["systemctl", "is-active", "jasper-correction-web.socket"]
    ).stdout.strip()
    service_state = _run(
        ["systemctl", "is-active", "jasper-correction-web.service"]
    ).stdout.strip()
    if socket_state == "active":
        return CheckResult(
            "correction web", "ok",
            f"socket active; service={service_state or 'unknown'}",
        )
    if service_state == "active":
        return CheckResult(
            "correction web", "warn",
            "service active but socket inactive — current session may work, "
            "but /correction/ will not restart after idle exit",
        )
    return CheckResult(
        "correction web", "warn",
        f"socket={socket_state or 'unknown'}, service={service_state or 'unknown'}. "
        "Run `sudo systemctl enable --now jasper-correction-web.socket` "
        "or redeploy.",
    )

# One rendered line from _systemd.py's _log_deferred_exit: "systemd idle-exit
# deferred: 2 active requests/holds after 7530s idle, busy for 7530s
# (threshold 600s, holds: relay:level_ramp:room)" — optionally followed by the
# " — busy past ...LEAKED hold..." note, which is what pushes it to WARNING.
# Captures (busy_for_seconds, holds).
_DEFERRED_HOLD_RE = re.compile(
    r"idle-exit deferred: \d+ active requests/holds after [\d.]+s idle, "
    r"busy for ([\d.]+)s \(threshold [\d.]+s, holds: ([^)]+)\)"
)

_CORRECTION_WEB_UNIT = "jasper-correction-web.service"


@doctor_check(order=25.5, group="correction")
def check_correction_idle_exit_holds() -> CheckResult:
    """A leaked idle-exit hold must be visible here, not only in the journal.

    ``IdleShutdownTracker.hold()`` (jasper/web/_systemd.py, issue #1854/#1856)
    keeps correction-web's socket-activated process alive across background
    session work that generates no inbound HTTP traffic: a crossover-v2 cloud
    session, and — since #1860 — the two human-paced level-ramp kinds
    (``level_ramp:room``, ``level_ramp:crossover``). Past
    ``HOLD_LEAK_WARN_AFTER_SEC`` of unbroken busy time, ``_systemd.py`` itself
    escalates its rate-limited "idle-exit deferred" line from INFO to
    WARNING, because no legitimate session runs that long. This check reads
    that same escalation from the unit's journal so a stuck hold shows up
    for an operator running ``jasper-doctor``, not only for whoever happens
    to be tailing the log when the line fires (#1860).

    Read-only, and cannot fix anything: per
    ``correction_setup._run_async``'s fail-closed invariant, nothing may
    release a hold out from under a possibly-still-mutating measurement, so
    a leaked hold is a bug for its own layer, not something this check reaps.

    Scoped to ``jasper-correction-web.service`` — the only wizard that
    threads a real hold anywhere today; the other four socket-activated
    wizards (bluetooth/sources/chat/system setup) pass
    ``_systemd.no_hold`` at every call site and structurally cannot produce
    this line. Skips when the service is not currently running (nothing to
    leak) or when journalctl is unavailable (dev host).
    """
    label = "correction idle-exit holds"
    active = _run(["systemctl", "is-active", _CORRECTION_WEB_UNIT]).stdout.strip()
    if active != "active":
        return CheckResult(label, "ok", f"{_CORRECTION_WEB_UNIT} not running (skipped)")

    # The deferred-exit line repeats at most every DEFERRED_EXIT_LOG_PERIOD_SEC
    # while the condition holds; doubling it comfortably spans one repeat
    # without dragging in a long-resolved leak from hours ago.
    since = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=DEFERRED_EXIT_LOG_PERIOD_SEC * 2)
    )
    try:
        journal = _run(
            ["journalctl", "-u", _CORRECTION_WEB_UNIT, "-p", "warning",
             "--since", since.strftime("%Y-%m-%d %H:%M:%S UTC"),
             "--no-pager", "--output=cat"],
            timeout=5.0,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        return CheckResult(
            label, "ok",
            f"journalctl unavailable ({type(e).__name__}: {e}) — skipped",
        )
    if journal.returncode != 0:
        return CheckResult(
            label, "ok",
            f"could not read journal (rc={journal.returncode}: "
            f"{journal.stderr.strip()}) — skipped",
        )

    # journalctl returns oldest-first; keep the LAST match so a resolved
    # older leak can't outrank a currently-outstanding one (or vice versa).
    latest = None
    for line in journal.stdout.splitlines():
        if "idle-exit deferred" not in line:
            continue
        match = _DEFERRED_HOLD_RE.search(line)
        if match is not None:
            latest = match
    if latest is None:
        return CheckResult(label, "ok", "no long-outstanding holds observed")
    busy_for, holds = latest.group(1), latest.group(2)
    return CheckResult(
        label, "warn",
        f"busy {busy_for}s with holds outstanding ({holds}) — the wizard "
        f"cannot idle-exit; see `journalctl -u {_CORRECTION_WEB_UNIT} | grep "
        "'idle-exit deferred'`",
    )


_CORRECTION_WEB_SOCKET_UNIT = "jasper-correction-web.socket"

# systemd's own name for "I gave up starting the service this socket triggers",
# as it appears in `systemctl show <socket> --property=Result`. This check
# REPORTS it and does not gate on it — see the docstring for why that
# distinction is load-bearing.
_CORRECTION_WEB_START_LIMIT_RESULT = "service-start-limit-hit"


@doctor_check(order=25.2, group="correction")
def check_correction_web_start_limited() -> CheckResult:
    """A failed ``.socket`` is what a start-limited /correction/ looks like.

    ``jasper-correction-web.service`` sets ``StartLimitBurst=20`` over
    ``StartLimitIntervalSec=600`` with no ``StartLimitAction=`` (systemd's
    default there: stop retrying and leave the unit ``failed``). Repeated
    fail-closed startup refusals — e.g. CamillaDSP unreachable during
    protected-neutral program-graph recovery — can exhaust that burst.

    **What systemd does when they do — measured**, on lab Pi jts4
    (systemd 257 / Debian Trixie) with transient units mirroring
    ``deploy/jasper-correction-web.service`` / ``.socket``, by the PR #2216
    adversarial gate (cited, not re-measured here)::

        <unit>.service: Start request repeated too quickly.
        <unit>.service: Failed with result 'exit-code'.
        <unit>.socket:  Failed with result 'service-start-limit-hit'.

    The start-limit marker lands on the **socket**, which goes
    ``ActiveState=failed`` and unbinds its listener — nginx then gets
    ECONNREFUSED and ``/correction/`` is dead until an operator runs
    ``reset-failed``. The **service** keeps its last real failure cause
    (``exit-code``) and never reports ``start-limit-hit`` at all: the gate
    sampled it at ~3 Hz across the whole transition, for 30 s after, and via
    direct rapid ``systemctl start``, and saw that value zero times. An
    earlier revision of this check gated on the *service* being
    ``ActiveState=failed`` **and** ``Result=start-limit-hit``, so it printed
    ``ok`` over exactly the outage it was added for (#2134 → #2216 blocker 1).

    Predicate: the socket's ``ActiveState == "failed"``. That is the rule
    :func:`jasper.cli.doctor.resilience.check_service_runtime_state` already
    applies to the core daemons — reused rather than re-invented — and the
    gate confirmed it catches this state. ``Result`` is *reported*, never
    gated on: besides surviving a rename of the marker string, ``ActiveState``
    is strictly *broader* — a socket killed by ``trigger-limit-hit`` is just
    as unbound, and a ``Result``-gated check would print ``ok`` over that
    whole class. That narrowing is what produced the original blind spot.

    **Why the socket and not the service — measured**, by the #2216 delta
    re-review on jts4, capturing systemd's D-Bus ``PropertiesChanged`` signals
    (polling misses this: a first attempt at 190 samples over 22 restarts saw
    ``activating`` only, and that null was a sampling artefact). Across 17
    ordinary ``Restart=on-failure`` retries::

        service ActiveState: 99 "activating"  34 "failed"  2 "inactive"
        service SubState:    34 "failed-before-auto-restart"
        socket:              ActiveState=active  ActiveExitTimestampMonotonic=0

    So the service passes through ``ActiveState=failed`` **34 times in 20
    seconds** of routine retrying — reading it with ``ActiveState == "failed"``
    would false-``fail`` on every one. The socket has no such window:
    ``ActiveExitTimestampMonotonic=0`` means systemd never recorded it leaving
    ``active`` at any duration (confirmed twice). Restarts and idle-exits are
    the service's business; the socket only changes state when the listener
    genuinely goes away.

    Deliberately ``ok``: the socket listening while the service idles between
    sessions (the normal socket-activated lifecycle); a service crash-looping
    *inside* its ``Restart=on-failure`` budget, which the socket rides out
    still bound; and an absent/uninstalled unit, for which ``systemctl show``
    answers ``ActiveState=inactive`` / ``Result=success`` at rc=0 (measured by
    the gate on jts4) rather than erroring.

    Known boundary, stated rather than papered over: a service start-limited
    by direct ``systemctl start`` calls, before any connection has triggered
    the socket, leaves the socket still bound and this check ``ok``. The first
    inbound request propagates the marker to the socket, so the state
    converges after one connection.

    A read that fails — non-zero ``systemctl``, or output carrying no
    ``ActiveState`` — is a ``fail``, never an ``ok``: "systemd says healthy"
    and "I could not ask systemd" must not render identically (#2216
    should-fix 2). ``systemctl`` missing outright is a dev host rather than a
    speaker, and skips like every sibling.
    """
    label = "correction web start limit"
    try:
        show = _run(
            ["systemctl", "show", _CORRECTION_WEB_SOCKET_UNIT,
             "--property=ActiveState", "--property=Result"]
        )
    except FileNotFoundError:
        return CheckResult(
            label, "ok", "systemctl unavailable — skipped (not Linux?)"
        )
    state: dict[str, str] = {}
    for line in show.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            state[key] = value.strip()
    active = state.get("ActiveState", "")
    if show.returncode != 0 or not active:
        return CheckResult(
            label, "fail",
            f"could not read {_CORRECTION_WEB_SOCKET_UNIT} state from "
            f"systemctl (rc={show.returncode}, "
            f"stdout={show.stdout.strip()[:120]!r}, "
            f"stderr={show.stderr.strip()[:120]!r}) — a start-limited "
            "/correction/ is indistinguishable from a healthy one until "
            "this reads",
        )
    result = state.get("Result", "") or "unknown"
    if active == "failed":
        cause = (
            "its paired service exhausted StartLimitBurst"
            if result == _CORRECTION_WEB_START_LIMIT_RESULT
            else "systemd reported the socket failed"
        )
        return CheckResult(
            label, "fail",
            f"{_CORRECTION_WEB_SOCKET_UNIT} is failed (ActiveState=failed, "
            f"Result={result}) — {cause}. Its listener is unbound, so "
            "/correction/ refuses connections and nothing brings it back on "
            "its own. Run `sudo systemctl reset-failed "
            f"{_CORRECTION_WEB_SOCKET_UNIT} {_CORRECTION_WEB_UNIT} && "
            f"sudo systemctl start {_CORRECTION_WEB_SOCKET_UNIT}`.",
        )
    return CheckResult(
        label, "ok", f"{_CORRECTION_WEB_SOCKET_UNIT} ActiveState={active}"
    )


def _probe_https_status(
    host: str, port: int, path: str, *, timeout: float = 4.0
) -> "tuple[int, str]":
    """GET ``path`` over HTTPS without following redirects, returning
    ``(status, location_header)``.

    Certificate verification is intentionally disabled: the speaker's cert is
    issued by a private CA and this probe checks nginx *routing* (a 200 vs an
    HTTP downgrade redirect), not certificate validity. ``http.client`` is used
    rather than ``urllib`` precisely because it does not follow redirects — the
    redirect target is the signal we are looking for. Factored out so tests can
    stub it."""
    import http.client
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, (resp.getheader("Location", "") or "")
    finally:
        conn.close()

@doctor_check(order=26, group="correction")
def check_correction_https_assets() -> CheckResult:
    """nginx's 443 block must serve ``/assets/``, not redirect it to HTTP.

    ``/correction/`` is the one wizard served over HTTPS (getUserMedia needs a
    secure context). Its measurement UI links ``/assets/app.css`` and its ES
    module by absolute path; if the 443 server block does not serve
    ``/assets/`` itself, those subresources fall through to the HTTP-downgrade
    catch-all and browsers block them as mixed content — the page renders
    unstyled and its JS (mic capture, sweep) never runs.

    ``check_web_design_assets`` covers the files existing on disk; this covers
    them being *reachable over HTTPS*. Skips on a dev checkout (no web root) and
    when 443 is unreachable (nginx liveness has its own checks)."""
    web_root = Path(os.environ.get("JASPER_WEB_SHARE_DIR", "/usr/share/jasper-web"))
    if not (web_root / "assets" / "app.css").is_file():
        return CheckResult(
            "correction HTTPS assets", "ok", "not installed (skipped)"
        )
    try:
        status, location = _probe_https_status("127.0.0.1", 443, "/assets/app.css")
    except OSError:
        return CheckResult(
            "correction HTTPS assets", "ok",
            "nginx 443 not reachable (skipped; see nginx / correction-web checks)",
        )
    if status == 200:
        return CheckResult(
            "correction HTTPS assets", "ok",
            "https://127.0.0.1/assets/app.css → 200",
        )
    if status in (301, 302, 307, 308) and location.startswith("http://"):
        return CheckResult(
            "correction HTTPS assets", "warn",
            f"/assets over HTTPS → {status} → {location}: the /correction/ UI's "
            "CSS/JS will be mixed-content-blocked. Add an `/assets/` location to "
            "the nginx 443 server block and redeploy.",
        )
    return CheckResult(
        "correction HTTPS assets", "warn",
        f"https://127.0.0.1/assets/app.css → HTTP {status} (expected 200); redeploy.",
    )

@doctor_check(order=27, group="correction")
def check_correction_state_dirs() -> CheckResult:
    root = _correction_root()
    expected = [
        root,
        root / "sweeps",
        root / "captures",
        root / "sessions",
        root / "calibration_mics",
    ]
    missing = [str(p) for p in expected if not p.exists()]
    not_dirs = [str(p) for p in expected if p.exists() and not p.is_dir()]
    not_writable = [str(p) for p in expected if p.is_dir() and not os.access(p, os.W_OK)]
    if not_dirs:
        return CheckResult(
            "correction state dirs", "fail",
            "expected directories but found files: " + ", ".join(not_dirs),
        )
    if not_writable:
        return CheckResult(
            "correction state dirs", "fail",
            "not writable: " + ", ".join(not_writable),
        )
    if missing:
        return CheckResult(
            "correction state dirs", "warn",
            "missing: " + ", ".join(missing) + " — redeploy to create them",
        )
    return CheckResult("correction state dirs", "ok", str(root))

@doctor_check(order=27.5, group="correction")
def check_correction_uploaded_calibration_sign() -> CheckResult:
    """Advisory: uploaded mic calibrations still claiming the "correction"
    convention.

    A measurement mic's calibration file states the microphone's RESPONSE, and
    JTS negates it. Vendor-fetched records are repaired automatically on
    deploy (``migrate_stored_sign_conventions``), but an UPLOADED record
    carries the household's own declaration about a file JTS never saw —
    never ours to flip silently. Uploads made before 2026-07-27 took a
    wrong-way default from the ``/correction/`` card, so the household is the
    only one who can say which those are. This is the surface that tells them
    to look; it never fails the doctor.
    """
    from jasper.audio_measurement import calibration

    root = calibration.configured_calibration_root()
    flagged: list[str] = []
    unreadable = 0
    for path in sorted(root.glob("*/*/*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            unreadable += 1
            continue
        if not isinstance(data, dict):
            unreadable += 1
            continue
        if str(data.get("provider") or "") != "manual_upload":
            continue
        if str(data.get("sign_convention") or "correction") == "correction":
            flagged.append(str(data.get("label") or data.get("calibration_id") or "?"))
    if not flagged:
        detail = "no uploaded calibrations need review"
        if unreadable:
            detail += f" ({unreadable} unreadable record(s))"
        return CheckResult("uploaded calibration sign", "ok", detail)
    return CheckResult(
        "uploaded calibration sign", "warn",
        f"{len(flagged)} uploaded calibration(s) stored as a correction to add: "
        + ", ".join(flagged[:3])
        + (" …" if len(flagged) > 3 else "")
        + " — review at /correction/; files from the REW ecosystem "
        "(miniDSP, Dayton, Cross-Spectrum) are response curves",
    )

def _parse_camilla_statefile_config_path(path: Path) -> str | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    match = re.search(r"^\s*config_path:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip("'\"") or None

def _active_camilla_config_path() -> tuple[Path, str | None]:
    statefile = Path(
        os.environ.get(
            "JASPER_CAMILLA_STATEFILE",
            "/var/lib/camilladsp/outputd-statefile.yml",
        )
    )
    return statefile, _parse_camilla_statefile_config_path(statefile)

@doctor_check(order=29, group="correction")
def check_correction_current_config() -> CheckResult:
    from jasper.correction.status import describe_current_config

    statefile, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(
            "current correction", "warn",
            f"could not read config_path from {statefile}",
        )
    path = Path(config_path)
    if not path.exists():
        return CheckResult(
            "current correction", "fail",
            f"CamillaDSP statefile points at missing config {config_path}",
        )

    descriptor = describe_current_config(str(path), config_dir=path.parent)
    parsed = descriptor.get("current_correction")
    if not isinstance(parsed, dict):
        if descriptor.get("kind") == "base":
            return CheckResult("current correction", "ok", "flat base config")
        if descriptor.get("managed") is True:
            return CheckResult(
                "current correction", "ok",
                f"{descriptor.get('label', 'JTS-managed config')}: "
                f"{descriptor.get('message', 'No room correction is applied.')} "
                f"({config_path})",
            )
        return CheckResult(
            "current correction", "warn",
            f"custom/non-JTS config loaded: {config_path}; "
            f"{descriptor.get('message', 'JTS cannot classify this config.')}",
        )
    return CheckResult(
        "current correction", "ok",
        f"session={parsed['session_id']} peqs={parsed['peq_count']} "
        f"({config_path})",
    )

def _format_byte_count(value: object) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        size = 0.0
    units = ("B", "KiB", "MiB", "GiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"

def _correction_evidence_status(bundle: dict[str, object]) -> str:
    missing: list[str] = []
    if not bundle.get("has_artifact_manifest"):
        missing.append("manifest")
    if not bundle.get("has_runtime_integrity_json"):
        missing.append("runtime")
    if not bundle.get("has_acoustic_quality_json"):
        missing.append("acoustic")
    if missing:
        return "missing:" + ",".join(missing)
    artifact_count = bundle.get("artifact_count")
    if isinstance(artifact_count, int):
        return f"complete({artifact_count} artifacts)"
    return "complete"

@doctor_check(order=32, group="correction")
def check_correction_latest_bundle() -> CheckResult:
    from jasper.correction import bundles

    sessions_dir = Path(
        os.environ.get(
            "JASPER_CORRECTION_SESSIONS_DIR",
            str(_correction_root() / "sessions"),
        )
    )
    collection = bundles.summarize_bundle_collection(sessions_dir)
    latest = collection.get("latest_bundle")
    if latest is None:
        return CheckResult(
            "latest correction bundle", "ok",
            f"no bundles under {sessions_dir} yet",
        )
    bundle_dir = Path(str(latest["bundle_dir"]))
    issues = bundles.validate_bundle(bundle_dir)
    fail_issues = [i for i in issues if i.severity == "fail"]
    warn_issues = [i for i in issues if i.severity == "warn"]
    summary = (
        f"session={latest.get('session_id')} state={latest.get('state')} "
        f"schema={latest.get('bundle_schema_version')}"
    )
    collection_summary = (
        f"; bundles={collection.get('bundle_count', 0)} "
        f"storage={_format_byte_count(collection.get('total_bundle_size_bytes'))} "
        f"private_raw={collection.get('private_raw_audio_count', 0)}/"
        f"{_format_byte_count(collection.get('private_raw_audio_bytes'))} "
        f"evidence={_correction_evidence_status(latest)}"
    )
    if collection.get("old_private_raw_audio_count"):
        collection_summary += (
            "; old raw recordings present "
            f"({collection.get('old_private_raw_audio_count')} files)"
        )
    summary += collection_summary
    if fail_issues:
        return CheckResult(
            "latest correction bundle", "fail",
            summary + "; " + "; ".join(i.message for i in fail_issues[:3]),
        )
    if warn_issues:
        return CheckResult(
            "latest correction bundle", "warn",
            summary + "; " + "; ".join(i.message for i in warn_issues[:3]),
        )
    if not latest.get("mic_calibration"):
        return CheckResult(
            "latest correction bundle", "warn",
            summary + "; last completed measurement used no calibrated mic",
        )
    return CheckResult("latest correction bundle", "ok", summary)


@doctor_check(order=29.5, group="correction")
def check_correction_cert_hostname() -> CheckResult:
    """The /correction/ TLS cert's SAN must cover the name the LAN
    actually resolves for this speaker.

    install.sh issues the leaf cert for JASPER_HOSTNAME at deploy time;
    a later hostname change (operator rename, Avahi collision rename)
    leaves the SAN stale, and the one HTTPS wizard greets the household
    with a browser warning. The reconciler-observed effective name
    comes from /var/lib/jasper/identity.env; the fix is a redeploy
    (which regenerates the leaf cert) after converging the name. Skips
    when the cert or identity snapshot isn't present (dev checkout,
    pre-first-run)."""
    import subprocess

    from ... import identity_state

    label = "correction cert ↔ hostname"
    cert_path = Path("/etc/nginx/ssl/jts.local.crt")
    if not cert_path.is_file():
        return CheckResult(label, "ok", "cert not installed (skipped)")
    snap = identity_state.snapshot()
    if snap.get("status") == "absent":
        return CheckResult(label, "ok", "identity snapshot absent (skipped)")
    effective = snap.get("avahi_hostname", "")
    if not effective:
        return CheckResult(label, "ok", "no effective hostname recorded (skipped)")
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout",
             "-ext", "subjectAltName"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return CheckResult(label, "warn", f"could not read cert SAN: {e}")
    if proc.returncode != 0:
        return CheckResult(
            label, "warn",
            f"openssl exited {proc.returncode} reading {cert_path}",
        )
    san = proc.stdout.lower()
    if effective.lower() in san:
        return CheckResult(label, "ok", f"SAN covers {effective}")
    return CheckResult(
        label, "warn",
        f"cert SAN does not include the advertised name {effective} — "
        "https://" + effective + "/correction/ will show a browser "
        "warning. Redeploy (bash scripts/deploy-to-pi.sh) to regenerate "
        "the leaf cert after converging the hostname.",
    )


@doctor_check(order=32.5, group="correction")
def check_capture_relay() -> CheckResult:
    """Phone-mic capture relay (cloud transport for browser mic capture).

    Fresh installs seed JASPER_CAPTURE_RELAY_BASE=https://relay.jasper.tech so
    phones can use a publicly trusted HTTPS mic-capture page. If an operator
    explicitly sets the base to disabled/off/0/none, the speaker uses the on-Pi
    same-origin capture and this reports OK/skipped. When configured, confirm the
    AEAD decrypt
    dependency is importable and the relay's /healthz is reachable (a relay
    outage breaks NEW measurements only — existing corrections are unaffected).

    Also reports the relay's advertised capture-plan ceiling. The crossover-v2
    cloud session emits a plan larger than the pre-capacity Worker's frozen
    ceiling of 8, so a stale deployment refuses it — correctly and before any
    tone plays, but only at the moment a household taps Start. Surfacing the
    ceiling here turns that into something an operator finds during a
    diagnostic run instead. A reachable relay whose ceiling is too small is a
    WARN, not a fail: every measurement OTHER than the cloud still works, and
    the remedy is one Worker deploy."""
    label = "Phone-mic relay"
    try:
        from jasper.capture_relay import health
    except ImportError as e:
        return CheckResult(label, "fail", f"capture_relay subsystem import failed: {e}")

    base = health.relay_base_from_env()
    if not base:
        return CheckResult(
            label, "ok",
            "not configured/disabled (skipped — fresh installs default to "
            "https://relay.jasper.tech; set JASPER_CAPTURE_RELAY_BASE to route "
            "phone-mic capture through another cloud relay)",
        )
    reachable, detail = health.probe_relay_health(base)
    if not reachable:
        # Unreachable relay: the capability probe would only repeat the same
        # failure, so do not spend a second round-trip saying it twice.
        return CheckResult(
            label, "warn",
            f"{base} {detail} — new phone-mic measurements need the relay; "
            "existing applied corrections are unaffected",
        )
    from jasper.active_speaker.crossover_v2_flow import relay_plan_attempts_required

    # The SHARED producer, not local arithmetic: it counts R16's lateral walk
    # exactly when that walk is on, so this number and the runtime capacity
    # guard's move together. See its docstring for what reading the cloud
    # figure alone would have cost a Pi on an older Worker.
    needed = relay_plan_attempts_required()
    ceiling, capacity_detail = health.probe_relay_plan_capacity(base)
    if ceiling is not None and ceiling >= needed:
        return CheckResult(label, "ok", f"{base} {detail}, {capacity_detail}")
    return CheckResult(
        label, "warn",
        f"{base} {detail}, but {capacity_detail} — the crossover measurement "
        f"needs {needed}. Deploy the current relay Worker "
        "(cd relay && npx wrangler deploy); other phone-mic measurements are "
        "unaffected",
    )


@doctor_check(order=32.6, group="correction")
def check_crossover_v2_cloud_pipeline() -> CheckResult:
    """Flat-linearization plan PR-4: the last session's honest-instrument
    cloud verdict — per group, the spec pass/fail, the excluded-interval
    count, and whether the geometry locked.

    "Not yet run" (no household has ever closed a cloud group — a fresh
    install, or one still on a pre-PR-3b build) is a distinct, non-failing
    state, never misreported as a clean pass — mirrors every other
    honesty-instrument surface's own rule (crossover_v2_flow's own
    ``group_geometry``/``group_cloud_result``: "None means not yet run, never
    a clean verdict"). A closed group whose spec FAILED is a WARN, not a
    FAIL: an out-of-spec speaker is a measurement finding for the household to
    act on at ``/correction/``, not a broken daemon.

    Only ``PHASE_CLOUD_VERIFY``'s spec verdict gates the warn (BLOCKER review
    finding, 2026-07-27). ``PHASE_CLOUD_MEASURE`` is the PRE-APPLY cloud — the
    uncorrected baseline that EXISTS in order to be out of spec — so gating
    on "any phase" meant a perfectly corrected speaker warned FOREVER: the
    pre-apply grade never changes no matter how good the fix is (reviewer
    reproduced against the real S0 corpus through this exact check). VERIFY's
    grade is the post-apply, household-actionable one. MEASURE's own verdict
    is still reported in the detail text below — never hidden, it just does
    not drive the warn.
    """
    from jasper.active_speaker.crossover_v2_flow import PHASE_CLOUD_VERIFY
    from jasper.web.correction_crossover_v2 import crossover_v2_status_block

    label = "crossover v2 cloud pipeline"
    block = crossover_v2_status_block()
    cloud = (block or {}).get("cloud")
    if not isinstance(cloud, dict) or not cloud:
        return CheckResult(label, "ok", "no cloud-measurement session recorded yet")
    parts: list[str] = []
    any_fail = False
    for phase in sorted(cloud):
        entry = cloud[phase]
        if not isinstance(entry, dict):
            continue
        overall = entry.get("overall_passed")
        spec_text = "pass" if overall is True else "fail" if overall is False else "n/a"
        if phase == PHASE_CLOUD_VERIFY and overall is False:
            any_fail = True
        excluded = entry.get("excluded_interval_count")
        excluded_text = "n/a" if excluded is None else str(excluded)
        # PR-5: the worst deviation, from the same spec report ``overall``
        # above came from — "spec=fail" alone cannot tell an operator apart a
        # speaker 0.1 dB over its tolerance from one 9 dB over. Read, never
        # re-derived (``_compact_cloud_status``'s ``flatness`` is a verbatim
        # copy of ``spec_flatness_gauge``'s dict).
        flatness = entry.get("flatness")
        worst = flatness.get("max_db") if isinstance(flatness, dict) else None
        worst_text = (
            f" worst={worst:+.2f}dB" if isinstance(worst, (int, float)) else ""
        )
        parts.append(
            f"{phase}: spec={spec_text}{worst_text} "
            f"excluded_intervals={excluded_text} "
            f"geometry_locked={bool(entry.get('geometry_locked'))}"
        )
    if not parts:
        return CheckResult(label, "ok", "no closed cloud groups recorded yet")
    return CheckResult(label, "warn" if any_fail else "ok", "; ".join(parts))


@doctor_check(order=32.7, group="correction")
def check_crossover_v2_applied_is_graded() -> CheckResult:
    """A correction is on the speaker; was it ever graded after it landed?

    Linearization-integrity PR-L4 item 6 — the companion to
    :func:`check_crossover_v2_cloud_pipeline`, not a change to it. That check
    deliberately gates its warn on ``PHASE_CLOUD_VERIFY``'s spec verdict alone,
    which is right and is left alone; the hole it leaves is the case where that
    verdict does not EXIST. On 2026-07-27 the doctor printed a green tick over a
    speaker running an applied profile whose only post-apply grade was absent,
    because a check that only warns on a failing grade cannot warn about a
    missing one.

    Two distinct silences, reported distinctly:

    * ``applied`` with no post-apply cloud group and no VERIFY outcome — the
      correction was never checked at all;
    * ``applied`` with a VERIFY outcome of ``inconclusive`` — it was checked and
      the check could not decide.

    WARN, never FAIL, matching this module's severity doctrine: an ungraded
    correction is a measurement finding for the household to act on at
    ``/correction/``, not a broken daemon. Express-tier sessions legitimately
    omit the post-apply position group, so the VERIFY outcome is what carries
    them — which is why both are consulted and either one satisfies this check.

    **A failed mark-VERIFY reaches this line as ``GRADE_FAILED`` however the
    spatial group graded** (#2464, ruled 2026-08-19). No branch here changed
    for it: the producer's own precedence was masking the state, and an honest
    ``state`` routes the cell into the warn arm that already carried the right
    remediation. What DID change is that that arm now prints the outcome word
    too, because the producer reads the claims record as well as ``outcome``
    and the two may honestly disagree — a crossover-region claim that missed
    its tolerance caps a capture whose own outcome is ``pass``, and a line
    saying "came back failed" beside a `/state` reading ``verify_outcome=pass``
    would leave a support read resolving that gap by guessing.

    **Reads ``post_apply_grade``; does not re-derive it** (PR-L4 review S2).
    An earlier revision recomputed the verdict here from ``verify`` and the
    cloud block with a predicate that had already drifted from the producer's
    (``overall_passed is not None`` vs an ``isinstance`` bool check), which is
    the second-owner-of-one-decision shape this PR's own docstrings spend
    paragraphs forbidding elsewhere. ``crossover_v2_status_block`` computes the
    grade once; every surface — `/state`, the wizard, this check — reads that.

    **A grade that EXISTS is not a grade that PASSED, and a grade at the mark
    is not the grade a Full session promised** (R19; #2160, #2098). Both were
    printing as one green tick, because ``state`` answers only "was it
    checked" and ``GRADE_GRADED``/``GRADE_MARK_VERIFIED`` are honest answers
    to that question in every case above. Keying ``ok`` on ``state`` alone
    therefore reported a closed-and-FAILED spatial grade, and a Full session
    that never closed one, as the same clean result — measured on jts3
    2026-08-07, where this line printed ``applied and graded (state=graded,
    verify=pass)`` beside a cloud line reading ``spec=fail worst=-4.63dB``.
    The producer now carries the two facts that separate them (``spatial``,
    ``complete``, plus the failing gauge's own number); this reads them and
    still re-derives nothing.

    **The producer's result code rides every line that names an instrument,
    as a disclosure and never as a gate**: this check grades the CHECKING, so
    a ``keep_previous`` behind a clean complete grade stays ``ok`` and simply
    says which code the household's badge is reading — see ``verify_text``
    below for why keying severity on it would be the nanny failure.

    Still WARN, never FAIL, and never a revert: a failed spatial grade is a
    COMPLETED grade the household acts on at ``/correction/`` (#2160's ruling
    — grade and disclose, do not gate). An unknown ``spatial`` word from a
    later build falls through to a WARN that names the unreadable word, never
    the passing wording — the same direction ``state`` already takes on a
    word this build has never heard of, never a guess at what it means.
    """
    from jasper.web.correction_crossover_v2 import (
        GRADE_FAILED,
        GRADE_GRADED,
        GRADE_INCONCLUSIVE,
        GRADE_MARK_VERIFIED,
        GRADE_NOT_APPLIED,
        GRADE_SPATIAL_ABSENT,
        GRADE_SPATIAL_FAILED,
        GRADE_SPATIAL_PASSED,
        GRADE_SPATIAL_UNMEASURABLE,
        crossover_v2_status_block,
    )

    label = "crossover v2 applied profile graded"
    block = crossover_v2_status_block() or {}
    grade = block.get("post_apply_grade")
    grade = grade if isinstance(grade, dict) else {}
    # `.get` with a default rather than a lookup: a durable state written by a
    # future build could carry a state name this one has never heard of, and
    # inventing a warning about it would be worse than saying what it said.
    state = str(grade.get("state") or "")
    # Hoisted (#2464) so the fail/inconclusive arm below can print it too:
    # ``state`` and ``verify_outcome`` may legitimately disagree there, because
    # a failed crossover-region CLAIM caps the state on a capture whose own
    # outcome is ``pass``. The never-graded fallback below still prints no
    # outcome word: it is reached only when there is no grade to attribute.
    #
    # **``capture``** qualifies it at every site, not just that arm: without it
    # ``came back failed (verify=pass)`` reads as self-contradictory to an
    # operator who does not know the two name different instruments — the state
    # is the union, ``verify_outcome`` is capture and tracking health alone.
    # One local, so the word cannot be attached at one site and missed at
    # another (#2464 gate, nit 3).
    #
    # **``result`` is DISCLOSED here and never gates.** The producer's result
    # code is the household's quality verdict — is the correction any GOOD —
    # and it has its own owner (``_post_apply_grade``) and its own surface (the
    # done screen's badge, ``/state``). This check's question is the one in its
    # title: was the applied correction CHECKED. Those two honestly disagree —
    # a ``keep_previous`` sitting behind a clean, complete grade printed a green
    # line here beside a badge reading "Keep the previous sound.", with nothing
    # saying a result code existed at all — so the fix is legibility, not
    # severity. Keying a warn on the code would also fire on healthy speakers:
    # a session that publishes no authorized winner, or whose VERIFY records no
    # pass/fail ``absolute`` claim, reaches ``inconclusive`` with every
    # instrument that graded the CHECKING passing (a worked cell of both sits
    # in ``tests/test_doctor_correction.py``). Same posture as
    # ``check_crossover_v2_cloud_pipeline`` above, which reports MEASURE's
    # verdict without gating on it. Absent whenever the producer recorded no
    # result evidence (a pre-R18 durable state): nothing is printed, never a
    # fabricated code, matching the spatial number's own rule below.
    verify_text = (
        f"capture verify={grade.get('verify_outcome') or 'n/a'}"
        + (f", result={grade.get('outcome')}" if grade.get("outcome") else "")
    )
    if state == GRADE_NOT_APPLIED:
        return CheckResult(label, "ok", "no applied measured crossover")
    if state in {GRADE_GRADED, GRADE_MARK_VERIFIED}:
        spatial = str(grade.get("spatial") or "")
        # A non-empty word this build does not recognize — a later build's
        # vocabulary. Never the empty string, which means a durable state
        # written before ``spatial`` existed at all and keeps its pre-R19
        # fallthrough below. S1 (#2242 gate): this used to fall through to
        # the passing wording; now it WARNS and names the word, the same
        # direction ``state``'s own unrecognised-value fallback already
        # takes.
        if spatial and spatial not in {
            GRADE_SPATIAL_ABSENT,
            GRADE_SPATIAL_PASSED,
            GRADE_SPATIAL_FAILED,
            GRADE_SPATIAL_UNMEASURABLE,
        }:
            return CheckResult(
                label, "warn",
                f"applied and graded, but the spatial grade word {spatial!r} "
                "is not one this build recognizes — treating it as unproven "
                f"rather than guessing ({verify_text}); check for a "
                "jasper-doctor update, or re-measure at /correction/",
            )
        if spatial == GRADE_SPATIAL_FAILED:
            worst = grade.get("spatial_worst_db")
            at = grade.get("spatial_worst_hz")
            # The number rides the verdict from the same gauge the cloud line
            # prints, so "the grade failed" and "by how much" cannot drift.
            # Absent when the gauge recorded none — never a fabricated 0.
            worst_text = ""
            if isinstance(worst, (int, float)):
                where = f" @ {at:.0f}Hz" if isinstance(at, (int, float)) else ""
                worst_text = f" ({worst:+.2f}dB{where})"
            return CheckResult(
                label, "warn",
                f"applied and graded, and the spatial grade FAILED{worst_text} "
                f"— the tune stays on the speaker ({verify_text}); re-measure "
                "at /correction/ or undo to restore the previous sound",
            )
        if spatial == GRADE_SPATIAL_UNMEASURABLE:
            return CheckResult(
                label, "warn",
                f"applied; the post-apply group closed but its spatial grade "
                f"could not be measured ({verify_text}) — re-measure at "
                "/correction/ in a quieter room, or undo",
            )
        if grade.get("complete") is False:
            return CheckResult(
                label, "warn",
                f"applied and verified at the mark, but no "
                f"{block.get('tier') or 'this'}-tier spatial grade exists "
                f"for this session, so it is unproven away from the mark "
                f"({verify_text}) — finish the measurement at /correction/, "
                "or undo",
            )
        return CheckResult(
            label, "ok",
            f"applied and graded (state={state}, scope="
            f"{grade.get('scope') or 'n/a'}, {verify_text})",
        )
    if state in {GRADE_INCONCLUSIVE, GRADE_FAILED}:
        return CheckResult(
            label, "warn",
            f"applied but the post-apply check came back {state} "
            f"({verify_text}) — re-verify at /correction/ or undo to restore "
            "the previous sound",
        )
    return CheckResult(
        label, "warn",
        "applied but never graded: no post-apply check completed for this "
        "correction — re-verify at /correction/ to confirm it, or undo",
    )


def _classify_seat_level_reference(
    path: Path, *, now: float | None = None
) -> CheckResult:
    """Classify the banked seat-SPL measurement reference at ``path``.

    Split from the check so tests can point it at a tmp file. Granular on
    purpose: the runtime reader
    (``session_volume_plan.measurement_reference_volume_db``) deliberately
    collapses missing, unreadable and implausible into "use the codified -20 dB
    default" — the fail-safe direction, and the right one there. That silence is
    exactly why an operator needs a line here saying WHICH of those states the
    file is in, and what number the next measurement session will actually hold.
    """
    label = "seat-SPL measurement reference"
    now = time.time() if now is None else now
    if not path.exists():
        # A box that never ran `jasper-seat-level`. Not a warning: the session
        # falls back to the codified reference and measures exactly as before.
        return CheckResult(
            label, "ok",
            f"not measured — sessions use the codified "
            f"{MEASUREMENT_REFERENCE_VOLUME_DB:g} dB reference "
            "(run jasper-seat-level to measure this room)",
        )
    record = load_seat_level_reference(state_path=path)
    volume_db = seat_level_reference_volume_db(state_path=path)
    if record is None or volume_db is None:
        return CheckResult(
            label, "warn",
            f"present but unusable — sessions silently fall back to "
            f"{MEASUREMENT_REFERENCE_VOLUME_DB:g} dB. Re-run jasper-seat-level, "
            f"or delete {path}",
        )
    serial = (record.get("mic_sensitivity") or {}).get("serial")
    measured = record.get("measured_db_spl")
    detail = (
        f"{volume_db:.2f} dB"
        + (f" measured {float(measured):.1f} dB SPL" if measured is not None else "")
        + f" (mic {serial or 'serial unknown'})"
    )
    raw_ts = record.get("updated_at")
    try:
        stamped = _datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
        age_days = (
            _datetime.now(timezone.utc) - stamped
        ).total_seconds() / 86400.0
        detail += f", measured {age_days:.0f}d ago"
    except (TypeError, ValueError):
        detail += f", unparseable updated_at: {raw_ts!r}"
    return CheckResult(label, "ok", detail)


@doctor_check(order=32.55, group="correction")
def check_seat_level_reference() -> CheckResult:
    """Surface the measured seat-SPL reference the next session will hold.

    One line, because the number it reports is otherwise invisible: its only
    runtime reader falls back silently, so a stale or unusable reference changes
    how loud every measurement runs without ever announcing itself.
    """
    return _classify_seat_level_reference(seat_level_reference_state_path())
