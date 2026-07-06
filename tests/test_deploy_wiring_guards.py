# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deploy-domain wiring guards — pin the install/systemd/nginx promises.

Four structural invariants in the deploy/ tree that were previously
prose-only (AGENTS.md, unit-file comments, PR #118 post-mortem) and
that fail silently on the Pi when violated:

1. **Orphan-artifact guard (two-sided).** Every shipped systemd unit,
   drop-in, udev rule, and helper script under deploy/ must be
   referenced by an install step (`${REPO_DIR}/deploy/...` in
   deploy/install.sh or deploy/lib/install/*.sh) — a unit file with no
   install step never reaches a Pi and "works" only in the repo.
   Reverse side: every `${REPO_DIR}/deploy/...` reference must resolve
   to a real file, so a renamed source can't leave a stale install line
   that breaks the next deploy at install time.

2. **Wizard-env precedence guard.** The documented "wizard file wins"
   rule (AGENTS.md "Voice provider switching", comments in
   jasper-voice.service): in any unit that sources both
   /etc/jasper/jasper.env and a wizard-owned /var/lib/jasper/*.env,
   the wizard file's EnvironmentFile= line must come AFTER jasper.env.
   systemd applies later files over earlier ones; a misordered line
   silently makes stale operator values beat the wizard.

3. **udev → unit chain guard.** Every ENV{SYSTEMD_WANTS} target in
   deploy/udev/*.rules must be a unit that ships in deploy/systemd/.
   A typo'd or renamed unit makes the hotplug self-heal path a no-op
   with zero log evidence (udev just drops unknown wants).

4. **Wizard-socket ↔ nginx parity (two-sided allowlist).** Every
   ListenStream port in the wizard sockets (deploy/*.socket) must have
   an nginx proxy_pass upstream, and every 127.0.0.1 proxy_pass port
   must be socket-backed — the PR #118 bug class (wizard 502s because
   one side of the port contract moved without the other). Intentional
   one-sided ports live in explicit allowlists that fail when stale,
   so the lists only shrink.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO / "deploy"
_DEPLOY_TO_PI = _REPO / "scripts" / "deploy-to-pi.sh"

_INSTALL_SCRIPTS = [_DEPLOY / "install.sh", *sorted((_DEPLOY / "lib" / "install").glob("*.sh"))]


# ----------------------------------------------------------------------
# 1 — orphan-artifact guard (shipped deploy file ↔ install step)
# ----------------------------------------------------------------------

# deploy/ subtrees whose files are install-owned artifacts: each file
# must be staged onto the Pi by an install step. Docs-only or laptop-side
# content (e.g. deploy/provenance.toml is read by CI, not installed)
# stays out of scope.
_SHIPPED_GLOBS = (
    "systemd/**/*",
    "udev/*.rules",
    "bin/*",
    "usbsink/*",
    "*.service",
    "*.socket",
)

# Shipped files intentionally NOT installed by install.sh. Empty today;
# an entry here must carry a reason. Stale entries fail (two-sided).
_NOT_INSTALLED_ALLOWLIST: dict[str, str] = {}

_INSTALL_REF_RE = re.compile(r'\$\{REPO_DIR\}"?/(deploy/[^"\'\s)]+)')

# The install-rows-table idiom: a bash array of `"<octal-mode> deploy/<src>
# <dest>"` rows consumed by a loop that stages each source via
# `install -m "${mode}" "${REPO_DIR}/${src}" "${dst}"` (e.g.
# JASPER_CORE_AUDIO_GRAPH_INSTALL_ROWS in deploy/lib/install/systemd-units.sh).
# The `deploy/...` source is a bare middle token there — `${REPO_DIR}/` is only
# prepended at the call site — so _INSTALL_REF_RE (which needs a literal
# `${REPO_DIR}/deploy/...`) misses it, and a file installed ONLY through such a
# table would look orphaned. This second matcher recognizes that row shape so the
# source counts as installed. Anchored on the leading octal mode inside a quoted
# row to avoid matching arbitrary `deploy/...` mentions in prose/comments.
_INSTALL_ROW_REF_RE = re.compile(r'"[0-7]{3,4}\s+(deploy/[^"\'\s]+)\s')


# Referenced deploy paths that legitimately may not exist in the tree:
# the install script guards them with an existence check and no-ops.
# Stale entries (no longer referenced, or now committed) fail.
_OPTIONAL_INSTALL_REFS: dict[str, str] = {}


def _install_refs() -> set[str]:
    refs: set[str] = set()
    for script in _INSTALL_SCRIPTS:
        text = script.read_text()
        refs.update(_INSTALL_REF_RE.findall(text))
        refs.update(_INSTALL_ROW_REF_RE.findall(text))
    return refs


def _shipped_files() -> list[Path]:
    files: list[Path] = []
    for pattern in _SHIPPED_GLOBS:
        files.extend(p for p in _DEPLOY.glob(pattern) if p.is_file())
    return sorted(set(files))


def test_every_shipped_deploy_artifact_has_an_install_step():
    refs = _install_refs()

    # Expand each reference into the set of repo files it stages.
    covered: set[Path] = set()
    for ref in refs:
        rel = ref.rstrip("/")
        if "*" in rel:
            covered.update(p for p in _REPO.glob(rel) if p.is_file())
        else:
            path = _REPO / rel
            if path.is_dir():
                covered.update(p for p in path.rglob("*") if p.is_file())
            else:
                covered.add(path)

    missing = [
        str(p.relative_to(_REPO))
        for p in _shipped_files()
        if p not in covered and str(p.relative_to(_REPO)) not in _NOT_INSTALLED_ALLOWLIST
    ]
    assert not missing, (
        "Shipped deploy artifacts with no ${REPO_DIR}/deploy/... install "
        f"reference in install.sh / deploy/lib/install/*.sh: {missing}. "
        "A unit/rule/script that install.sh never stages silently never "
        "reaches the Pi. Add the install step, or allowlist with a reason "
        "in _NOT_INSTALLED_ALLOWLIST."
    )

    stale_allowlist = [
        rel for rel in _NOT_INSTALLED_ALLOWLIST
        if not (_REPO / rel).is_file() or (_REPO / rel) in covered
    ]
    assert not stale_allowlist, (
        f"Stale _NOT_INSTALLED_ALLOWLIST entries (file gone or now installed): "
        f"{stale_allowlist} — remove them so the list only shrinks."
    )


def test_every_install_deploy_reference_resolves():
    refs = _install_refs()
    broken = []
    for ref in sorted(refs):
        rel = ref.rstrip("/")
        if rel in _OPTIONAL_INSTALL_REFS:
            continue
        if "*" in rel:
            if not list(_REPO.glob(rel)):
                broken.append(ref)
        elif not (_REPO / rel).exists():
            broken.append(ref)
    assert not broken, (
        f"install scripts reference deploy sources that do not exist: {broken}. "
        "A renamed/deleted source file left a stale install line — this "
        "breaks `bash install.sh` on the next deploy. (Genuinely optional, "
        "existence-guarded sources go in _OPTIONAL_INSTALL_REFS.)"
    )

    stale_optional = [
        rel for rel in _OPTIONAL_INSTALL_REFS
        if rel not in refs or (_REPO / rel).exists()
    ]
    assert not stale_optional, (
        f"Stale _OPTIONAL_INSTALL_REFS entries (no longer referenced, or now "
        f"committed to the tree): {stale_optional} — remove them."
    )


# ----------------------------------------------------------------------
# 2 — wizard-env precedence ("wizard file wins")
# ----------------------------------------------------------------------

_ENV_FILE_RE = re.compile(r"^EnvironmentFile=-?(\S+)", re.MULTILINE)


def _unit_files() -> list[Path]:
    units = [p for p in _DEPLOY.glob("systemd/**/*") if p.is_file()]
    units += list(_DEPLOY.glob("*.service")) + list(_DEPLOY.glob("*.socket"))
    return sorted(set(units))


def test_wizard_env_files_load_after_jasper_env():
    """In every unit, /var/lib/jasper/*.env must come after jasper.env.

    systemd applies EnvironmentFile= directives in order, later wins.
    The wizard-owned /var/lib/jasper files are documented to override
    operator-managed /etc/jasper/jasper.env; sourcing them first would
    silently invert that (the stale-provider bug class).
    """
    violations = []
    for unit in _unit_files():
        paths = _ENV_FILE_RE.findall(unit.read_text())
        if "/etc/jasper/jasper.env" not in paths:
            continue
        base_idx = paths.index("/etc/jasper/jasper.env")
        for idx, path in enumerate(paths):
            if path.startswith("/var/lib/jasper/") and idx < base_idx:
                violations.append(f"{unit.relative_to(_REPO)}: {path} sourced before jasper.env")
    assert not violations, (
        "Wizard env files sourced BEFORE /etc/jasper/jasper.env — operator "
        f"values would override the wizard's: {violations}"
    )


# ----------------------------------------------------------------------
# 3 — udev SYSTEMD_WANTS targets must be shipped units
# ----------------------------------------------------------------------

_WANTS_RE = re.compile(r'SYSTEMD_WANTS\}\+="([^"]+)"')


def test_udev_systemd_wants_units_are_shipped():
    missing = []
    for rules in sorted(_DEPLOY.glob("udev/*.rules")):
        for unit in _WANTS_RE.findall(rules.read_text()):
            if not (_DEPLOY / "systemd" / unit).is_file():
                missing.append(f"{rules.relative_to(_REPO)} -> {unit}")
    assert not missing, (
        "udev rules request units that don't ship in deploy/systemd/: "
        f"{missing}. udev silently drops unknown SYSTEMD_WANTS targets, "
        "so the hotplug self-heal path becomes a no-op."
    )


# ----------------------------------------------------------------------
# 4 — wizard-socket ListenStream ↔ nginx proxy_pass parity
# ----------------------------------------------------------------------

# Socket-backed ports with deliberately no nginx route. Stale entries fail.
_SOCKET_ONLY_PORTS: dict[int, str] = {}

# nginx 127.0.0.1 upstreams deliberately not socket-activated. Stale entries fail.
_NGINX_ONLY_PORTS = {
    8780: "jasper-control — always-on daemon (jasper-control.service), "
          "not a socket-activated wizard",
}

_LISTEN_RE = re.compile(r"^ListenStream=127\.0\.0\.1:(\d+)", re.MULTILINE)
_PROXY_RE = re.compile(r"proxy_pass\s+http://127\.0\.0\.1:(\d+)")


def test_wizard_socket_ports_match_nginx_upstreams():
    """The PR #118 bug class: a wizard port live on one side only.

    Every ListenStream in the wizard sockets (deploy/*.socket) needs an
    nginx proxy_pass or it's an unreachable backend; every 127.0.0.1
    proxy_pass needs a socket (or jasper-control) behind it or the
    route 502s. Both directions enforced; intentional exceptions live
    in the allowlists above and fail when they go stale.
    """
    socket_ports: dict[int, str] = {}
    for sock in sorted(_DEPLOY.glob("*.socket")):
        for port in _LISTEN_RE.findall(sock.read_text()):
            socket_ports[int(port)] = sock.name
    nginx_ports = {int(p) for p in _PROXY_RE.findall((_DEPLOY / "nginx-jasper.conf").read_text())}

    unrouted = {
        port: socket_ports[port]
        for port in socket_ports
        if port not in nginx_ports and port not in _SOCKET_ONLY_PORTS
    }
    assert not unrouted, (
        f"ListenStream ports with no nginx proxy_pass: {unrouted} — the "
        "wizard is unreachable through jts.local. Add the nginx location "
        "or allowlist in _SOCKET_ONLY_PORTS with a reason."
    )

    unbacked = sorted(
        port for port in nginx_ports
        if port not in socket_ports and port not in _NGINX_ONLY_PORTS
    )
    assert not unbacked, (
        f"nginx proxy_pass ports with no ListenStream in deploy/*.socket: "
        f"{unbacked} — that route 502s (PR #118). Add the ListenStream or "
        "allowlist in _NGINX_ONLY_PORTS with a reason."
    )

    stale = sorted(
        [p for p in _SOCKET_ONLY_PORTS if p not in socket_ports or p in nginx_ports]
        + [p for p in _NGINX_ONLY_PORTS if p not in nginx_ports or p in socket_ports]
    )
    assert not stale, (
        f"Stale parity-allowlist entries: {stale} — the exception no longer "
        "exists; remove it so the allowlists only shrink."
    )


# ----------------------------------------------------------------------
# 5 — recoverable service policy
# ----------------------------------------------------------------------

_RECOVERABLE_ALWAYS_ON_UNITS = (
    _DEPLOY / "systemd" / "nginx.service.d" / "jts-recovery.conf",
    _DEPLOY / "systemd" / "bluealsa-aplay.service.d" / "jts-restart.conf",
    _DEPLOY / "systemd" / "bluealsa.service.d" / "jts-restart.conf",
    _DEPLOY / "systemd" / "bt-agent.service",
    _DEPLOY / "systemd" / "librespot.service",
    _DEPLOY / "systemd" / "nqptp.service",
    _DEPLOY / "systemd" / "shairport-sync.service",
    _DEPLOY / "systemd" / "jasper-mux.service",
)

_RECOVERABLE_SOCKET_WEB_UNITS = (
    _DEPLOY / "jasper-web.service",
    _DEPLOY / "jasper-web-streambox.service",
    _DEPLOY / "jasper-bluetooth-web.service",
    _DEPLOY / "jasper-correction-web.service",
    _DEPLOY / "jasper-dial-web.service",
    _DEPLOY / "jasper-system-web.service",
    _DEPLOY / "jasper-chat-web.service",
)


def test_recoverable_always_on_services_restart_with_generous_limit():
    """Transient OOM/update pressure should not park safe services forever."""
    for path in _RECOVERABLE_ALWAYS_ON_UNITS:
        text = path.read_text(encoding="utf-8")
        assert "Restart=always" in text, path
        assert "RestartSec=5" in text, path
        assert "StartLimitIntervalSec=600" in text, path
        assert "StartLimitBurst=20" in text, path


def test_socket_web_services_have_generous_start_limit():
    """Socket web daemons exit on idle, but should retry through OOM bursts."""
    for path in _RECOVERABLE_SOCKET_WEB_UNITS:
        text = path.read_text(encoding="utf-8")
        assert "Restart=on-failure" in text, path
        assert "StartLimitIntervalSec=600" in text, path
        assert "StartLimitBurst=20" in text, path


def test_package_owned_recovery_dropins_are_installed():
    install_text = "\n".join(
        p.read_text(encoding="utf-8") for p in _INSTALL_SCRIPTS
    )
    for rel in (
        "deploy/systemd/nginx.service.d/jts-recovery.conf",
        "deploy/systemd/bluealsa-aplay.service.d/jts-restart.conf",
    ):
        assert rel in install_text


def test_legacy_ad_hoc_recovery_window_dropins_are_removed():
    install_text = "\n".join(
        p.read_text(encoding="utf-8") for p in _INSTALL_SCRIPTS
    )
    assert "cleanup_legacy_recovery_window_dropins" in install_text
    assert "jts-recovery-window.conf" in install_text
    for unit in (
        "librespot",
        "nqptp",
        "shairport-sync",
        "bt-agent",
        "jasper-mux",
        "jasper-web",
        "jasper-bluetooth-web",
        "jasper-correction-web",
        "jasper-dial-web",
        "jasper-system-web",
        "jasper-chat-web",
    ):
        assert unit in install_text


# ----------------------------------------------------------------------
# 6 — deploy-to-pi.sh post-install verification wiring (Workstream B)
# ----------------------------------------------------------------------
#
# The transactional-update fix rests on deploy-to-pi.sh actually invoking
# three pieces after install.sh: surface collateral OOM kills, gate on the
# build manifest having advanced to the deployed SHA, and surface runtime
# health. These guard against a refactor silently dropping any of them —
# the failure mode would be a green deploy that hides exactly the problems
# (#2/#4/#5/#7) this work closed. The behavior of the helpers themselves is
# pinned in test_deploy_oom_collateral.py and test_lib_deploy_direction.py.


def test_deploy_captures_install_rc_so_collateral_is_always_surfaced():
    """install.sh must run with its exit code captured (not under bare
    set -e), so report_oom_collateral runs even when the build failed —
    otherwise an OOM-killed build would abort the deploy before surfacing
    the collateral (problem #5)."""
    text = _DEPLOY_TO_PI.read_text()
    assert re.search(
        r'run_remote_sudo "\$\{install_env\} bash[^\n]*"\s*\|\|\s*install_rc=\$\?',
        text,
    ), "install.sh invocation must capture its exit code with || install_rc=$?"
    assert "report_oom_collateral" in text


def test_deploy_defines_and_calls_post_install_verification():
    """The three post-install verification helpers must be both defined
    and called."""
    text = _DEPLOY_TO_PI.read_text()
    for fn in (
        "report_oom_collateral",
        "verify_manifest_advanced",
        "surface_system_health",
    ):
        assert f"{fn}() {{" in text, f"{fn} is not defined in deploy-to-pi.sh"
        # Called at least once in addition to its definition.
        assert text.count(fn) >= 2, f"{fn} is defined but never called"


def test_deploy_captures_pi_clock_for_oom_window():
    """The OOM scan bounds its kernel-log window to the Pi's clock at
    install start — captured before the install run."""
    text = _DEPLOY_TO_PI.read_text()
    assert "DEPLOY_START_EPOCH=" in text
    assert "date +%s" in text
    # The capture must precede the install invocation it bounds.
    assert text.index("DEPLOY_START_EPOCH=\"$(ssh_remote") < text.index(
        "|| install_rc=$?"
    )


def test_deploy_manifest_gate_checks_verified_status_and_sha():
    """verify_manifest_advanced must confirm BOTH the deployed full SHA and
    the JASPER_INSTALL_STATUS=ok marker — proving the install ran to
    completion, not just that some manifest exists (problem #4)."""
    text = _DEPLOY_TO_PI.read_text()
    start = text.index("verify_manifest_advanced() {")
    body = text[start: text.index("\n}", start)]
    assert "build_manifest_value" in body
    assert "JASPER_GIT_SHA_FULL" in body
    assert "JASPER_INSTALL_STATUS" in body
    assert 'installed_status" == "ok"' in body
    assert "exit 1" in body  # a non-advanced manifest fails the deploy


def test_deploy_production_oom_is_gated_after_end_state_evidence():
    """A production-daemon OOM during deploy is SURFACED loudly and then
    fails verification after the end-state gates have run. That keeps the
    transcript useful while preventing a deploy that killed a live service
    from looking merge-clean just because systemd restarted it."""
    text = _DEPLOY_TO_PI.read_text()
    assert "report_oom_collateral" in text  # surfacing happens
    success_path = text[text.index("Build manifest now on Pi"):]
    assert "verify_manifest_advanced" in success_path
    assert "surface_system_health" in success_path
    assert 'if [[ "$OOM_PRODUCTION_HIT" == "1" ]]' in success_path
    assert "DEPLOY VERIFICATION FAILED: a live production daemon was" in success_path
    assert success_path.index("surface_system_health") < success_path.index(
        'if [[ "$OOM_PRODUCTION_HIT" == "1" ]]'
    )


def test_deploy_post_health_uses_lightweight_probe_on_low_memory_hosts():
    """The post-deploy doctor runs after install.sh has removed temporary
    build swap. On a 1 GB Pi we use a cheap deploy-health probe instead of
    importing the full doctor graph beside freshly restarted services."""
    text = _DEPLOY_TO_PI.read_text()
    start = text.index("surface_system_health() {")
    body = text[start: text.index("\n}", start)]
    assert "MemTotal" in body
    assert "1200000" in body
    assert "jasper-deploy-health" in body
    assert "/opt/jasper/.venv/bin/jasper-doctor" in body


def test_deploy_verification_skipped_cleanly_under_interactive_sudo():
    """The manifest read + doctor capture corrupt under `ssh -tt`, so they
    must be guarded by the same passwordless-sudo gate as the identity and
    direction guards — skipping with a notice rather than mis-verifying."""
    text = _DEPLOY_TO_PI.read_text()
    # The verify+surface calls live in the else-branch of a SUDO_INTERACTIVE
    # check that prints a skip notice in the then-branch.
    assert re.search(
        r'if \[\[ "\$SUDO_INTERACTIVE" == "1" \]\]; then[\s\S]*?'
        r'manifest \+ health checks skipped[\s\S]*?else[\s\S]*?'
        r'verify_manifest_advanced[\s\S]*?surface_system_health[\s\S]*?fi',
        text,
    )
