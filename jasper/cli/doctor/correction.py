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
# (threshold 600s, holds: capture:crossover_v2:session)" — optionally followed by the
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
    session work that generates no inbound HTTP traffic: a crossover-v2
    measurement session. Past
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
    directory (the fresh-install bug this guards) read as "ok" while the
    dropped ``jasper-web`` writer was locked out.

    Delegates the actual predicate to ``_shared._group_writable_dir`` (shared
    with ``audio._camilla_configs_writable_result`` and
    ``env.check_state_dir`` — this used to be a third near-copy of the same
    stat-and-compare logic); see its docstring for the write+search+setgid
    reasoning. ``privsep`` / ``secret_compartments`` document and solve the
    identical root-caller blind spot on the read side."""
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
        )
    if not_writable:
        return CheckResult(
            "correction state dirs", "fail",
            f"not writable by {_CORRECTION_SERVICE_USER}: " + ", ".join(not_writable),
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

def _active_camilla_config_path() -> tuple[Path, str | None]:
    """Which statefile this box means, and the config it names (or ``None``).

    Both halves come from ``active_speaker.environment`` — the one owner of the
    ``JASPER_CAMILLA_STATEFILE`` override, the shipped default, and the
    ``config_path`` parse. The pair is what the callers need: the path is named
    in the warn detail even when the parse fails.
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
            summary + "; last completed measurement used no calibrated mic — "
            "calibrate one under Microphone on /correction/ (enter the "
            "serial number and Fetch calibration)",
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

    # The SHARED producer, not local arithmetic: it counts R16's lateral walk,
    # which an operator's staged angle walk can add to any session, so this
    # number and the runtime capacity guard's move together. See its docstring
    # for what reading the cloud figure alone would have cost a Pi on an older
    # Worker.
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
    from jasper.active_speaker.crossover_v2.journey import PHASE_CLOUD_VERIFY
    from jasper.web.correction_crossover_v2_status import crossover_v2_status_block

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
                f"applied and graded, and the spatial grade missed the "
                f"target{worst_text} — the tune stays on the speaker "
                f"({verify_text}); re-measure at /correction/ or undo to "
                "restore the previous sound",
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


@doctor_check(order=25.6, group="correction")
def check_measurement_hold() -> CheckResult:
    """A measurement hold that never lapses must be visible here.

    ``jasper.control.measurement_hold`` is jasper-control's self-expiring copy
    of "a measurement is live" — while it is up, source-observed volume writes
    (a host moving its USB slider) are declined so they cannot walk the fader a
    sweep is holding, and every other measurement is refused. It renews every
    ``coordinator.MEASUREMENT_LEASE_REFRESH_SEC`` and lapses on its own
    ``MEASUREMENT_HOLD_TTL_SEC`` after that, so a crashed holder recovers with
    no operator step and this check has nothing to reap.

    What it CAN catch is the shape TTLs cannot: a holder that is alive and
    renewing, but whose session will never end — a browser tab left open on a
    measurement page, a wedged CLI. That reads as a permanently declined host
    slider with no visible cause, so it is warned about rather than left to be
    discovered by ear.

    The threshold is ``session_volume_plan.MAX_WALL_CLOCK_CEILING_S``: the hard
    cap the volume plan puts on ANY single measurement session's wall clock. A
    hold outliving the longest session the flow will ever permit is implausible
    by construction, so the number is derived rather than guessed, and it
    cannot drift from the ceiling it is reasoning about.

    Reads ``held_for_s``, NOT ``expires_in_s``: the latter resets on every
    renewal, so a hold stuck for an hour looks two minutes old through it.
    """
    label = "measurement hold"
    from ...control import client as control

    try:
        hold = control.get_measurement()
    except (control.ControlError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return CheckResult(
            label, "ok",
            f"jasper-control unreachable ({type(e).__name__}: {e}) — skipped",
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
        )
    return CheckResult(label, "ok", detail)


@doctor_check(order=32.56, group="correction")
def check_session_volume_unresolved() -> CheckResult:
    """An unresolved measurement volume must not be silent.

    ``SessionVolumePlan.needs_recovery`` is the durable "a measurement left the
    speaker's volume in a state that must be drained" latch. Until now nothing
    in ``jasper/cli/doctor/`` read it, so a speaker could sit holding a
    measurement volume — quiet, or louder than the household set — with every
    doctor check green and only the ``/correction/crossover/`` recovery screen
    aware of it.

    Two shapes refuse a new session and are reported separately because they
    need different words: a latched ``unresolved`` state, and a durably
    ``active`` state with no live owner. The second one **self-heals**: past its
    own wall-clock ceiling the flow's open path force-drains it
    (``reconcile_session_volume_for_new_session``), so a stale-active state is
    reported as an ordinary crash remnant rather than as something to act on.
    """
    label = "session measurement volume"
    plan = SessionVolumePlan(state_path=DEFAULT_SESSION_VOLUME_STATE_PATH)
    if not plan.needs_recovery:
        return CheckResult(label, "ok", "no unresolved measurement volume")
    if plan.unresolved_volume_safety is not None:
        reason = str(
            plan.unresolved_volume_safety.get("reason")
            or "session_volume_restore_unconfirmed"
        )
        return CheckResult(
            label, "warn",
            f"a previous measurement left the volume unresolved ({reason}); "
            "the next session will refuse to start. Recover it at "
            "http://jts.local/correction/crossover/",
        )
    if plan.stale_active():
        return CheckResult(
            label, "ok",
            "a crashed session's volume state is still on disk but is past its "
            "wall-clock ceiling; the next session force-drains it",
        )
    return CheckResult(
        label, "warn",
        "a measurement session's volume state is durably active with no live "
        "owner in this process; if no measurement is running, recover it at "
        "http://jts.local/correction/crossover/",
    )
