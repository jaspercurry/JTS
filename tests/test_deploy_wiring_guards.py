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

# Referenced deploy paths that legitimately may not exist in the tree:
# the install script guards them with an existence check and no-ops.
# Stale entries (no longer referenced, or now committed) fail.
_OPTIONAL_INSTALL_REFS: dict[str, str] = {}


def _install_refs() -> set[str]:
    refs: set[str] = set()
    for script in _INSTALL_SCRIPTS:
        refs.update(_INSTALL_REF_RE.findall(script.read_text()))
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
_SOCKET_ONLY_PORTS = {
    8776: "peering_setup backend kept for rooms_setup helpers; "
          "nginx 301s /peers/ to /rooms/ (see nginx-jasper.conf)",
}

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
