# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — correction domain."""
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
from ._shared import CheckResult, _group_writable_dir, _run
from ...active_speaker.environment import (
    camilla_statefile_path,
    read_camilla_statefile_config_path,
)
from ...active_speaker.seat_level_reference import (
    load_seat_level_reference,
    seat_level_reference_state_path,
    seat_level_reference_volume_db,
)
from ...active_speaker.session_volume_plan import (
    DEFAULT_SESSION_VOLUME_STATE_PATH,
    MAX_WALL_CLOCK_CEILING_S,
    MEASUREMENT_REFERENCE_VOLUME_DB,
    SessionVolumePlan,
)
from ...control.measurement_hold import MEASUREMENT_HOLD_TTL_SEC
from ...web._systemd import DEFERRED_EXIT_LOG_PERIOD_SEC

# Closed vocabulary for this module's `CheckResult.reason`: one snake_case
# constant per distinct outcome branch below. Every `warn`/`fail` carries one;
# an `ok` carries one only where the ok itself is a fact a consumer branches on
# (not-applicable, skipped, an informational sub-state). `detail` stays the
# human sentence and is free to reword; tests pin `status` and `reason`
# (ADR-0228 rule 3).

REASON_WEB_SOCKET_INACTIVE = "correction_web_socket_inactive"
REASON_WEB_INACTIVE = "correction_web_inactive"

REASON_IDLE_HOLDS_SERVICE_INACTIVE = "idle_holds_service_inactive"
REASON_IDLE_HOLDS_JOURNAL_UNAVAILABLE = "idle_holds_journal_unavailable"
REASON_IDLE_HOLDS_JOURNAL_UNREADABLE = "idle_holds_journal_unreadable"
REASON_IDLE_HOLDS_NONE = "idle_holds_none"
REASON_IDLE_HOLD_LEAKED = "idle_hold_leaked"

REASON_HTTPS_ASSETS_NOT_INSTALLED = "https_assets_not_installed"
REASON_HTTPS_ASSETS_UNREACHABLE = "https_assets_unreachable"
REASON_HTTPS_ASSETS_HTTP_REDIRECT = "https_assets_http_redirect"
REASON_HTTPS_ASSETS_UNEXPECTED_STATUS = "https_assets_unexpected_status"

REASON_STATE_DIRS_NOT_DIRECTORIES = "state_dirs_not_directories"
REASON_STATE_DIRS_NOT_WRITABLE = "state_dirs_not_writable"
REASON_STATE_DIRS_MISSING = "state_dirs_missing"

REASON_UPLOADED_CALIBRATION_SIGN_REVIEW = "uploaded_calibration_sign_review"

REASON_CURRENT_CONFIG_FLAT_BASE = "current_config_flat_base"
REASON_CURRENT_CONFIG_MANAGED = "current_config_managed"
REASON_CURRENT_CONFIG_UNCLASSIFIED = "current_config_unclassified"
REASON_CURRENT_CONFIG_ROOM_CORRECTION = "current_config_room_correction"

REASON_LATEST_BUNDLE_NONE = "latest_bundle_none"
REASON_LATEST_BUNDLE_INVALID = "latest_bundle_invalid"
REASON_LATEST_BUNDLE_ISSUES = "latest_bundle_issues"
REASON_LATEST_BUNDLE_UNCALIBRATED_MIC = "latest_bundle_uncalibrated_mic"

REASON_CERT_NOT_INSTALLED = "cert_not_installed"
REASON_CERT_IDENTITY_ABSENT = "cert_identity_absent"
REASON_CERT_HOSTNAME_UNKNOWN = "cert_hostname_unknown"
REASON_CERT_SAN_UNREADABLE = "cert_san_unreadable"
REASON_CERT_SAN_MISMATCH = "cert_san_mismatch"

REASON_CLOUD_NOT_RUN = "cloud_pipeline_not_run"
REASON_CLOUD_NO_CLOSED_GROUPS = "cloud_pipeline_no_closed_groups"
REASON_CLOUD_VERIFY_SPEC_FAILED = "cloud_verify_spec_failed"

REASON_APPLIED_GRADE_NOT_APPLIED = "applied_grade_not_applied"
REASON_APPLIED_GRADE_SPATIAL_UNRECOGNIZED = "applied_grade_spatial_unrecognized"
REASON_APPLIED_GRADE_SPATIAL_FAILED = "applied_grade_spatial_failed"
REASON_APPLIED_GRADE_SPATIAL_UNMEASURABLE = "applied_grade_spatial_unmeasurable"
REASON_APPLIED_GRADE_MARK_ONLY = "applied_grade_mark_only"
REASON_APPLIED_GRADE_VERIFY_FAILED = "applied_grade_verify_failed"
REASON_APPLIED_GRADE_VERIFY_INCONCLUSIVE = "applied_grade_verify_inconclusive"
REASON_APPLIED_GRADE_NEVER_GRADED = "applied_grade_never_graded"

REASON_SEAT_LEVEL_NOT_MEASURED = "seat_level_not_measured"
REASON_SEAT_LEVEL_UNUSABLE = "seat_level_unusable"
REASON_SEAT_LEVEL_TIMESTAMP_UNREADABLE = "seat_level_timestamp_unreadable"

REASON_MEASUREMENT_HOLD_CONTROL_UNREACHABLE = "measurement_hold_control_unreachable"
REASON_MEASUREMENT_HOLD_ACTIVE = "measurement_hold_active"
REASON_MEASUREMENT_HOLD_AGE_UNREADABLE = "measurement_hold_age_unreadable"
REASON_MEASUREMENT_HOLD_STUCK = "measurement_hold_stuck"

REASON_SESSION_VOLUME_UNRESOLVED = "session_volume_unresolved"
REASON_SESSION_VOLUME_STALE_ACTIVE = "session_volume_stale_active"
REASON_SESSION_VOLUME_ACTIVE_NO_OWNER = "session_volume_active_no_owner"


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
            reason=REASON_WEB_SOCKET_INACTIVE,
        )
    return CheckResult(
        "correction web", "warn",
        f"socket={socket_state or 'unknown'}, service={service_state or 'unknown'}. "
        "Run `sudo systemctl enable --now jasper-correction-web.socket` "
        "or redeploy.",
        reason=REASON_WEB_INACTIVE,
    )

# One rendered line from _systemd.py's _log_deferred_exit: "systemd idle-exit
# deferred: 2 active requests/holds after 7530s idle, busy for 7530s
# (threshold 600s, holds: capture:crossover_v2:session)" — optionally followed
# by the " — busy past ...LEAKED hold..." note, which pushes it to WARNING.
# Captures (busy_for_seconds, holds).
_DEFERRED_HOLD_RE = re.compile(
    r"idle-exit deferred: \d+ active requests/holds after [\d.]+s idle, "
    r"busy for ([\d.]+)s \(threshold [\d.]+s, holds: ([^)]+)\)"
)

_CORRECTION_WEB_UNIT = "jasper-correction-web.service"


def _latest_deferred_hold(journal_text: str) -> tuple[str, str] | None:
    """``(busy_for_seconds, holds)`` from the LAST deferred-exit line, if any.

    journalctl returns oldest-first, so the last match is the current one: a
    resolved older leak must not outrank an outstanding newer one.
    """
    latest: tuple[str, str] | None = None
    for line in journal_text.splitlines():
        if "idle-exit deferred" not in line:
            continue
        match = _DEFERRED_HOLD_RE.search(line)
        if match is not None:
            latest = (match.group(1), match.group(2))
    return latest


@doctor_check(order=25.5, group="correction")
def check_correction_idle_exit_holds() -> CheckResult:
    """A leaked idle-exit hold must be visible here, not only in the journal.

    ``_systemd.py`` escalates its "idle-exit deferred" line to WARNING past
    ``HOLD_LEAK_WARN_AFTER_SEC``; this reads that escalation back. Read-only:
    nothing may release a hold out from under a possibly-still-mutating
    measurement (``correction_setup._run_async``'s fail-closed invariant).

    Scoped to ``jasper-correction-web.service``, the only wizard that threads a
    real hold — the others pass ``_systemd.no_hold`` at every call site. See
    issues #1854/#1856/#1860.
    """
    label = "correction idle-exit holds"
    active = _run(["systemctl", "is-active", _CORRECTION_WEB_UNIT]).stdout.strip()
    if active != "active":
        return CheckResult(
            label, "skipped", f"{_CORRECTION_WEB_UNIT} not running",
            reason=REASON_IDLE_HOLDS_SERVICE_INACTIVE,
        )

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
            label, "skipped",
            f"journalctl unavailable ({type(e).__name__}: {e})",
            reason=REASON_IDLE_HOLDS_JOURNAL_UNAVAILABLE,
        )
    if journal.returncode != 0:
        return CheckResult(
            label, "skipped",
            f"could not read journal (rc={journal.returncode}: "
            f"{journal.stderr.strip()})",
            reason=REASON_IDLE_HOLDS_JOURNAL_UNREADABLE,
        )

    latest = _latest_deferred_hold(journal.stdout)
    if latest is None:
        return CheckResult(
            label, "ok", "no long-outstanding holds observed",
            reason=REASON_IDLE_HOLDS_NONE,
        )
    busy_for, holds = latest
    return CheckResult(
        label, "warn",
        f"busy {busy_for}s with holds outstanding ({holds}) — the wizard "
        f"cannot idle-exit; see `journalctl -u {_CORRECTION_WEB_UNIT} | grep "
        "'idle-exit deferred'`",
        reason=REASON_IDLE_HOLD_LEAKED,
    )


def _probe_https_status(
    host: str, port: int, path: str, *, timeout: float = 4.0
) -> "tuple[int, str]":
    """GET ``path`` over HTTPS without following redirects, returning
    ``(status, location_header)``.

    Verification is off on purpose: the cert is from a private CA and this
    probe checks nginx *routing*, not validity. ``http.client`` rather than
    ``urllib`` because it does not follow redirects — the redirect target is
    the signal."""
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
    secure context) and links its CSS/ES module by absolute path, so a 443
    block that does not serve ``/assets/`` leaves them mixed-content-blocked.
    ``check_web_design_assets`` covers the files existing on disk; this covers
    them being *reachable over HTTPS*."""
    web_root = Path(os.environ.get("JASPER_WEB_SHARE_DIR", "/usr/share/jasper-web"))
    if not (web_root / "assets" / "app.css").is_file():
        return CheckResult(
            "correction HTTPS assets", "skipped", "web root not installed",
            reason=REASON_HTTPS_ASSETS_NOT_INSTALLED,
        )
    try:
        status, location = _probe_https_status("127.0.0.1", 443, "/assets/app.css")
    except OSError:
        return CheckResult(
            "correction HTTPS assets", "skipped",
            "nginx 443 not reachable (see the nginx / correction-web checks)",
            reason=REASON_HTTPS_ASSETS_UNREACHABLE,
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
            reason=REASON_HTTPS_ASSETS_HTTP_REDIRECT,
        )
    return CheckResult(
        "correction HTTPS assets", "warn",
        f"https://127.0.0.1/assets/app.css → HTTP {status} (expected 200); redeploy.",
        reason=REASON_HTTPS_ASSETS_UNEXPECTED_STATUS,
    )

# jasper-correction-web and jasper-web (WS1 drop) both run as this user; see
# privsep.MANIFEST. Named here so a writability failure tells the operator
# WHO is locked out, not just that the doctor's own check succeeded.
_CORRECTION_SERVICE_USER = "jasper-web"


def _not_writable_by_group(
    paths: list[Path], *, expected_group: str = "jasper"
) -> list[str]:
    """Which of ``paths`` (already confirmed to be existing directories) a
    non-root, ``expected_group``-member service user could NOT write into.

    ``os.access(p, os.W_OK)`` cannot answer this: ``jasper-doctor`` runs as
    root, and ``os.access`` reports every path writable to the *caller* — root
    can write regardless of a directory's actual mode — so a root:root 0700
    directory reads as "ok" while the dropped ``jasper-web`` writer is locked
    out. The predicate itself is ``_shared._group_writable_dir``; see its
    docstring for the write+search+setgid reasoning."""
    not_writable: list[str] = []
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        writable, _ = _group_writable_dir(st, expected_group=expected_group)
        if not writable:
            not_writable.append(str(p))
    return not_writable


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
    not_writable = _not_writable_by_group([p for p in expected if p.is_dir()])
    if not_dirs:
        return CheckResult(
            "correction state dirs", "fail",
            "expected directories but found files: " + ", ".join(not_dirs),
            reason=REASON_STATE_DIRS_NOT_DIRECTORIES,
        )
    if not_writable:
        return CheckResult(
            "correction state dirs", "fail",
            f"not writable by {_CORRECTION_SERVICE_USER}: " + ", ".join(not_writable),
            reason=REASON_STATE_DIRS_NOT_WRITABLE,
        )
    if missing:
        return CheckResult(
            "correction state dirs", "warn",
            "missing: " + ", ".join(missing) + " — redeploy to create them",
            reason=REASON_STATE_DIRS_MISSING,
        )
    return CheckResult("correction state dirs", "ok", str(root))

@doctor_check(order=27.5, group="correction")
def check_correction_uploaded_calibration_sign() -> CheckResult:
    """Advisory: uploaded mic calibrations still claiming the "correction"
    convention.

    A calibration file states the microphone's RESPONSE and JTS negates it.
    Vendor records are repaired on deploy (``migrate_stored_sign_conventions``);
    an UPLOADED record carries the household's own declaration about a file JTS
    never saw, so it is surfaced for review and never flipped silently.
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
        reason=REASON_UPLOADED_CALIBRATION_SIGN_REVIEW,
    )

# The three ways `_active_camilla_config_path` below leaves a caller with no
# config to read. Homed here, beside the reader, and imported by every doctor
# module that calls it (ADR-0228 rule 1).
REASON_CAMILLA_STATEFILE_UNREADABLE = "camilla_statefile_unreadable"
REASON_CAMILLA_CONFIG_MISSING = "camilla_config_missing"
REASON_CAMILLA_CONFIG_UNREADABLE = "camilla_config_unreadable"


def _active_camilla_config_path() -> tuple[Path, str | None]:
    """Which statefile this box means, and the config it names (or ``None``).

    Both halves come from ``active_speaker.environment``, the one owner of the
    ``JASPER_CAMILLA_STATEFILE`` override and the ``config_path`` parse. The
    path is returned too so callers can name it when the parse fails.
    """

    statefile = camilla_statefile_path()
    return statefile, read_camilla_statefile_config_path(statefile)

@doctor_check(order=29, group="correction")
def check_correction_current_config() -> CheckResult:
    from jasper.correction.status import describe_current_config

    statefile, config_path = _active_camilla_config_path()
    if config_path is None:
        return CheckResult(
            "current correction", "warn",
            f"could not read config_path from {statefile}",
            reason=REASON_CAMILLA_STATEFILE_UNREADABLE,
        )
    path = Path(config_path)
    if not path.exists():
        return CheckResult(
            "current correction", "fail",
            f"CamillaDSP statefile points at missing config {config_path}",
            reason=REASON_CAMILLA_CONFIG_MISSING,
        )

    descriptor = describe_current_config(str(path), config_dir=path.parent)
    parsed = descriptor.get("current_correction")
    if not isinstance(parsed, dict):
        if descriptor.get("kind") == "base":
            return CheckResult(
                "current correction", "ok", "flat base config",
                reason=REASON_CURRENT_CONFIG_FLAT_BASE,
            )
        if descriptor.get("managed") is True:
            return CheckResult(
                "current correction", "ok",
                f"{descriptor.get('label', 'JTS-managed config')}: "
                f"{descriptor.get('message', 'No room correction is applied.')} "
                f"({config_path})",
                reason=REASON_CURRENT_CONFIG_MANAGED,
            )
        return CheckResult(
            "current correction", "warn",
            f"custom/non-JTS config loaded: {config_path}; "
            f"{descriptor.get('message', 'JTS cannot classify this config.')}",
            reason=REASON_CURRENT_CONFIG_UNCLASSIFIED,
        )
    return CheckResult(
        "current correction", "ok",
        f"session={parsed['session_id']} peqs={parsed['peq_count']} "
        f"({config_path})",
        reason=REASON_CURRENT_CONFIG_ROOM_CORRECTION,
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
            reason=REASON_LATEST_BUNDLE_NONE,
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
            reason=REASON_LATEST_BUNDLE_INVALID,
        )
    if warn_issues:
        return CheckResult(
            "latest correction bundle", "warn",
            summary + "; " + "; ".join(i.message for i in warn_issues[:3]),
            reason=REASON_LATEST_BUNDLE_ISSUES,
        )
    if not latest.get("mic_calibration"):
        return CheckResult(
            "latest correction bundle", "warn",
            summary + "; last completed measurement used no calibrated mic — "
            "calibrate one under Microphone on /correction/ (enter the "
            "serial number and Fetch calibration)",
            reason=REASON_LATEST_BUNDLE_UNCALIBRATED_MIC,
        )
    return CheckResult("latest correction bundle", "ok", summary)


@doctor_check(order=29.5, group="correction")
def check_correction_cert_hostname() -> CheckResult:
    """The /correction/ TLS cert's SAN must cover the name the LAN actually
    resolves for this speaker.

    install.sh issues the leaf cert for JASPER_HOSTNAME at deploy time, so a
    later rename (operator or Avahi collision) leaves the SAN stale and the one
    HTTPS wizard shows a browser warning. The effective name comes from
    /var/lib/jasper/identity.env; the fix is a redeploy."""
    import subprocess

    from ... import identity_state

    label = "correction cert ↔ hostname"
    cert_path = Path("/etc/nginx/ssl/jts.local.crt")
    if not cert_path.is_file():
        return CheckResult(
            label, "skipped", "cert not installed",
            reason=REASON_CERT_NOT_INSTALLED,
        )
    snap = identity_state.snapshot()
    if snap.get("status") == "absent":
        return CheckResult(
            label, "skipped", "identity snapshot absent",
            reason=REASON_CERT_IDENTITY_ABSENT,
        )
    effective = snap.get("avahi_hostname", "")
    if not effective:
        return CheckResult(
            label, "skipped", "no effective hostname recorded",
            reason=REASON_CERT_HOSTNAME_UNKNOWN,
        )
    try:
        proc = subprocess.run(
            ["openssl", "x509", "-in", str(cert_path), "-noout",
             "-ext", "subjectAltName"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return CheckResult(
            label, "warn", f"could not read cert SAN: {e}",
            reason=REASON_CERT_SAN_UNREADABLE,
        )
    if proc.returncode != 0:
        return CheckResult(
            label, "warn",
            f"openssl exited {proc.returncode} reading {cert_path}",
            reason=REASON_CERT_SAN_UNREADABLE,
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
        reason=REASON_CERT_SAN_MISMATCH,
    )


@doctor_check(order=32.6, group="correction")
def check_crossover_v2_cloud_pipeline() -> CheckResult:
    """The last session's honest-instrument cloud verdict — per group, the spec
    pass/fail, the excluded-interval count, and whether the geometry locked.

    Only ``PHASE_CLOUD_VERIFY``'s spec verdict gates the warn.
    ``PHASE_CLOUD_MEASURE`` is the PRE-APPLY cloud — the uncorrected baseline
    that exists in order to be out of spec — so gating on any phase warns
    forever on a perfectly corrected speaker. MEASURE's verdict is still
    reported, it just does not drive the status. A failed spec is a WARN: an
    out-of-spec speaker is a measurement finding, not a broken daemon.
    """
    from jasper.active_speaker.crossover_v2.journey import PHASE_CLOUD_VERIFY
    from jasper.web.correction_crossover_v2_status import crossover_v2_status_block

    label = "crossover v2 cloud pipeline"
    block = crossover_v2_status_block()
    cloud = (block or {}).get("cloud")
    if not isinstance(cloud, dict) or not cloud:
        return CheckResult(
            label, "ok", "no cloud-measurement session recorded yet",
            reason=REASON_CLOUD_NOT_RUN,
        )
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
        # The worst deviation, read from the same spec report ``overall`` came
        # from, never re-derived: "spec=fail" alone cannot tell a speaker 0.1 dB
        # over its tolerance from one 9 dB over.
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
        return CheckResult(
            label, "ok", "no closed cloud groups recorded yet",
            reason=REASON_CLOUD_NO_CLOSED_GROUPS,
        )
    if any_fail:
        return CheckResult(
            label, "warn", "; ".join(parts),
            reason=REASON_CLOUD_VERIFY_SPEC_FAILED,
        )
    return CheckResult(label, "ok", "; ".join(parts))


@doctor_check(order=32.7, group="correction")
def check_crossover_v2_applied_is_graded() -> CheckResult:
    """A correction is on the speaker; was it ever graded after it landed?

    The companion to :func:`check_crossover_v2_cloud_pipeline`, which warns
    only on a FAILING post-apply grade and so cannot see a missing one.

    Reads ``post_apply_grade`` and re-derives nothing: the grade has one owner
    (``crossover_v2_status_block``), and every surface — `/state`, the wizard,
    this check — reads it. Express-tier sessions omit the post-apply position
    group, so the VERIFY outcome carries them and either half satisfies this.

    WARN, never FAIL, and never a revert: an ungraded or failed correction is a
    measurement finding the household acts on at ``/correction/`` (#2160 —
    grade and disclose, do not gate). The producer's ``result`` code is
    disclosed beside the grade and never gates it: this check grades the
    CHECKING, and healthy sessions legitimately reach ``inconclusive``. An
    unrecognised ``spatial`` word warns and names the word rather than guessing
    at it (#2242). See #2098, #2160, #2464.
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
    )
    from jasper.web.correction_crossover_v2_status import crossover_v2_status_block

    label = "crossover v2 applied profile graded"
    block = crossover_v2_status_block() or {}
    grade = block.get("post_apply_grade")
    grade = grade if isinstance(grade, dict) else {}
    # `.get` with a default rather than a lookup: a durable state written by a
    # future build could carry a state name this one has never heard of, and
    # inventing a warning about it would be worse than saying what it said.
    state = str(grade.get("state") or "")
    # ``capture`` qualifies the outcome at every site: ``state`` is the union of
    # the instruments, ``verify_outcome`` is capture and tracking health alone,
    # and the two may honestly disagree (a failed crossover-region claim caps a
    # capture whose own outcome is ``pass``). ``result`` is absent whenever the
    # producer recorded no result evidence — never a fabricated code.
    verify_text = (
        f"capture verify={grade.get('verify_outcome') or 'n/a'}"
        + (f", result={grade.get('outcome')}" if grade.get("outcome") else "")
    )
    if state == GRADE_NOT_APPLIED:
        return CheckResult(
            label, "ok", "no applied measured crossover",
            reason=REASON_APPLIED_GRADE_NOT_APPLIED,
        )
    if state in {GRADE_GRADED, GRADE_MARK_VERIFIED}:
        spatial = str(grade.get("spatial") or "")
        # A non-empty word this build does not recognize is a later build's
        # vocabulary. The empty string is a durable state written before
        # ``spatial`` existed and keeps the fallthrough below.
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
                reason=REASON_APPLIED_GRADE_SPATIAL_UNRECOGNIZED,
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
                f"applied and graded, and the spatial grade missed the "
                f"target{worst_text} — the tune stays on the speaker "
                f"({verify_text}); re-measure at /correction/ or undo to "
                "restore the previous sound",
                reason=REASON_APPLIED_GRADE_SPATIAL_FAILED,
            )
        if spatial == GRADE_SPATIAL_UNMEASURABLE:
            return CheckResult(
                label, "warn",
                f"applied; the post-apply group closed but its spatial grade "
                f"could not be measured ({verify_text}) — re-measure at "
                "/correction/ in a quieter room, or undo",
                reason=REASON_APPLIED_GRADE_SPATIAL_UNMEASURABLE,
            )
        if grade.get("complete") is False:
            return CheckResult(
                label, "warn",
                f"applied and verified at the mark, but no "
                f"{block.get('tier') or 'this'}-tier spatial grade exists "
                f"for this session, so it is unproven away from the mark "
                f"({verify_text}) — finish the measurement at /correction/, "
                "or undo",
                reason=REASON_APPLIED_GRADE_MARK_ONLY,
            )
        return CheckResult(
            label, "ok",
            f"applied and graded (state={state}, scope="
            f"{grade.get('scope') or 'n/a'}, {verify_text})",
        )
    if state in {GRADE_INCONCLUSIVE, GRADE_FAILED}:
        detail = (
            f"applied but the post-apply check came back {state} "
            f"({verify_text}) — re-verify at /correction/ or undo to restore "
            "the previous sound"
        )
        if state == GRADE_INCONCLUSIVE:
            return CheckResult(
                label, "warn", detail,
                reason=REASON_APPLIED_GRADE_VERIFY_INCONCLUSIVE,
            )
        return CheckResult(
            label, "warn", detail,
            reason=REASON_APPLIED_GRADE_VERIFY_FAILED,
        )
    return CheckResult(
        label, "warn",
        "applied but never graded: no post-apply check completed for this "
        "correction — re-verify at /correction/ to confirm it, or undo",
        reason=REASON_APPLIED_GRADE_NEVER_GRADED,
    )


def _classify_seat_level_reference(
    path: Path, *, now: float | None = None
) -> CheckResult:
    """Classify the banked seat-SPL measurement reference at ``path``.

    Granular on purpose: the runtime reader
    (``session_volume_plan.measurement_reference_volume_db``) collapses
    missing, unreadable and implausible into the codified default, so this is
    the only surface that says which state the file is in and what number the
    next session will hold.
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
            reason=REASON_SEAT_LEVEL_NOT_MEASURED,
        )
    record = load_seat_level_reference(state_path=path)
    volume_db = seat_level_reference_volume_db(state_path=path)
    if record is None or volume_db is None:
        return CheckResult(
            label, "warn",
            f"present but unusable — sessions silently fall back to "
            f"{MEASUREMENT_REFERENCE_VOLUME_DB:g} dB. Re-run jasper-seat-level, "
            f"or delete {path}",
            reason=REASON_SEAT_LEVEL_UNUSABLE,
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
        return CheckResult(
            label, "ok", detail + f", unparseable updated_at: {raw_ts!r}",
            reason=REASON_SEAT_LEVEL_TIMESTAMP_UNREADABLE,
        )
    return CheckResult(label, "ok", detail)


@doctor_check(order=32.55, group="correction")
def check_seat_level_reference() -> CheckResult:
    """Surface the measured seat-SPL reference the next session will hold."""
    return _classify_seat_level_reference(seat_level_reference_state_path())


@doctor_check(order=25.6, group="correction")
def check_measurement_hold() -> CheckResult:
    """A measurement hold that never lapses must be visible here.

    A crashed holder recovers on ``MEASUREMENT_HOLD_TTL_SEC`` with no operator
    step; what TTLs cannot catch is a holder that is alive and renewing but
    whose session never ends (a measurement page left open, a wedged CLI) —
    that reads as a permanently declined host slider with no visible cause.

    The threshold is ``session_volume_plan.MAX_WALL_CLOCK_CEILING_S``, derived
    so it cannot drift from the cap the flow itself enforces. Reads
    ``held_for_s``, NOT ``expires_in_s``: the latter resets on every renewal.
    """
    label = "measurement hold"
    from ...control import client as control

    try:
        hold = control.get_measurement()
    except (control.ControlError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return CheckResult(
            label, "skipped",
            f"jasper-control unreachable ({type(e).__name__}: {e})",
            reason=REASON_MEASUREMENT_HOLD_CONTROL_UNREACHABLE,
        )
    if not hold.get("active"):
        return CheckResult(label, "ok", "no measurement in progress")
    owner = str(hold.get("owner") or "unknown")
    mode = str(hold.get("mode") or "unknown")
    held_for = hold.get("held_for_s")
    try:
        held_for_s = float(held_for)
    except (TypeError, ValueError):
        return CheckResult(
            label, "warn",
            f"held by {owner} (mode={mode}) but held_for_s is unreadable "
            f"({held_for!r}) — a stuck hold could not be ruled out",
            reason=REASON_MEASUREMENT_HOLD_AGE_UNREADABLE,
        )
    detail = f"held by {owner} (mode={mode}) for {held_for_s:.0f}s"
    if held_for_s > MAX_WALL_CLOCK_CEILING_S:
        return CheckResult(
            label, "warn",
            f"{detail}, past the {MAX_WALL_CLOCK_CEILING_S:.0f}s ceiling any "
            "single measurement session may run. Host-slider volume changes "
            "are being declined. Close the measurement page (or stop the CLI) "
            "that owns it; the hold then lapses within "
            f"{MEASUREMENT_HOLD_TTL_SEC:.0f}s.",
            reason=REASON_MEASUREMENT_HOLD_STUCK,
        )
    return CheckResult(label, "ok", detail, reason=REASON_MEASUREMENT_HOLD_ACTIVE)


@doctor_check(order=32.56, group="correction")
def check_session_volume_unresolved() -> CheckResult:
    """An unresolved measurement volume must not be silent.

    ``SessionVolumePlan.needs_recovery`` is the durable "a measurement left the
    speaker's volume in a state that must be drained" latch; without this line
    only the ``/correction/crossover/`` recovery screen knows about it.

    Two shapes refuse a new session and need different words: a latched
    ``unresolved`` state, and a durably ``active`` state with no live owner.
    The second self-heals — past its wall-clock ceiling the flow's open path
    force-drains it (``reconcile_session_volume_for_new_session``) — so it is
    reported as a crash remnant rather than as something to act on.
    """
    label = "session measurement volume"
    plan = SessionVolumePlan(state_path=DEFAULT_SESSION_VOLUME_STATE_PATH)
    if not plan.needs_recovery:
        return CheckResult(label, "ok", "no unresolved measurement volume")
    if plan.unresolved_volume_safety is not None:
        safety = str(
            plan.unresolved_volume_safety.get("reason")
            or "session_volume_restore_unconfirmed"
        )
        return CheckResult(
            label, "warn",
            f"a previous measurement left the volume unresolved ({safety}); "
            "the next session will refuse to start. Recover it at "
            "http://jts.local/correction/crossover/",
            reason=REASON_SESSION_VOLUME_UNRESOLVED,
        )
    if plan.stale_active():
        return CheckResult(
            label, "ok",
            "a crashed session's volume state is still on disk but is past its "
            "wall-clock ceiling; the next session force-drains it",
            reason=REASON_SESSION_VOLUME_STALE_ACTIVE,
        )
    return CheckResult(
        label, "warn",
        "a measurement session's volume state is durably active with no live "
        "owner in this process; if no measurement is running, recover it at "
        "http://jts.local/correction/crossover/",
        reason=REASON_SESSION_VOLUME_ACTIVE_NO_OWNER,
    )
