# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Deploy-domain wiring guards — pin the install/systemd/nginx promises.

Five structural invariants in the deploy/ tree that were previously
prose-only (AGENTS.md, unit-file comments, PR #118 post-mortem) and
that, when violated, fail where no reviewer is looking — silently on
the Pi (1-4) or as a whole red lane on the laptop (5):

1. **Orphan-artifact guard (two-sided).** Every shipped systemd unit,
   drop-in, udev rule, and helper script under deploy/ must be
   referenced by an install step (`${REPO_DIR}/deploy/...` in
   deploy/install.sh or deploy/lib/install/*.sh) — a unit file with no
   install step never reaches a Pi and "works" only in the repo.
   Reverse side: every `${REPO_DIR}/deploy/...` reference must resolve
   to a real file, so a renamed source can't leave a stale install line
   that breaks the next deploy at install time.

2. **Wizard-env precedence guard.** The documented "wizard file wins"
   rule (comments in jasper-voice.service): in any unit that sources both
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

5. **Shell-dialect portability.** deploy/ shell targets the Pi, but the
   hardware-free suites execute it on the developer's laptop, where macOS
   supplies BSD sed and bash 3.2. GNU-only and bash-4-only spellings turn
   every local lane red regardless of the diff under test.

6. **Install-window gate.** Every unit an install can have started
   behind its back is gated on the in-progress marker, or is named in an
   allowlist with the reason it is safe on a half-synced /opt/jasper
   (issue #4123).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ._shell_corpus import shell_files
from .systemd_unit_helpers import value_for, values_for

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


def _shipped_unit_name(wants_target: str) -> str:
    """udev instantiates a template with the event's %k; the file ships as @."""
    return wants_target.replace("@%k.service", "@.service")


def test_udev_systemd_wants_units_are_shipped():
    missing = []
    for rules in sorted(_DEPLOY.glob("udev/*.rules")):
        for unit in _WANTS_RE.findall(rules.read_text()):
            if not (_DEPLOY / "systemd" / _shipped_unit_name(unit)).is_file():
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


# ----------------------------------------------------------------------
# 6 — deploy-to-pi.sh post-install verification wiring (Workstream B)
# ----------------------------------------------------------------------
#
# The transactional-update fix rests on deploy-to-pi.sh actually invoking
# three pieces after install.sh: surface collateral OOM kills, gate on the
# build manifest having advanced to the deployed SHA, and surface runtime
# health. These guard against a refactor silently dropping any of them —
# the failure mode would be a green deploy that hides exactly what
# ADR-0172, ADR-0173 and ADR-0174 close. The behavior of the helpers
# themselves is pinned in test_deploy_oom_collateral.py and
# test_lib_deploy_direction.py.


def test_deploy_production_oom_is_gated_after_end_state_evidence():
    """A production-daemon OOM during deploy is SURFACED loudly and then
    fails verification after the end-state gates have run. That keeps the
    transcript useful while preventing a deploy that killed a live service
    from looking merge-clean just because systemd restarted it."""
    text = _DEPLOY_TO_PI.read_text()
    assert "report_oom_collateral" in text  # surfacing happens
    success_path = text[text.index("Build manifest now on Pi"):]
    assert "verify_manifest_advanced" in success_path
    assert "gate_core_health" in success_path
    assert 'if [[ "$OOM_PRODUCTION_HIT" == "1" ]]' in success_path
    assert "DEPLOY VERIFICATION FAILED: a live production daemon was" in success_path
    assert success_path.index("gate_core_health") < success_path.index(
        'if [[ "$OOM_PRODUCTION_HIT" == "1" ]]'
    )


# ----------------------------------------------------------------------
# 7 — shell-dialect portability (docstring invariant 5)
# ----------------------------------------------------------------------

# Two spellings each turned every macOS lane red regardless of the diff
# under test: GNU-only `sed -i` (issue #3021) and bash-4 builtins (#3015,
# reached once a test started executing deploy/bin helpers). Each has a
# replacement that behaves identically under GNU sed / bash 5 on the Pi, so
# the ban costs nothing there. Drop this guard if no lane ever executes
# deploy/ shell outside the Pi's dialect again.
_NON_PORTABLE_SPELLINGS = (
    (
        "GNU-only in-place sed — BSD sed reads `-i`'s backup suffix as the "
        "NEXT ARGUMENT, so it parses the sed program as the suffix",
        # Scan sed's own argument run only (stop at a pipe/`;`/`&`), so a
        # `grep -i` further down the line is not sed's; `-i` anywhere in that
        # run counts, quoted-empty suffix (`-i''`) included, because the shell
        # strips the quotes before sed ever sees them. `-i.bak` is the one
        # accepted spelling.
        re.compile(r"\bsed\b[^\n|;&]*?\s(?:-i(?=['\"]|[\s\\]|$)|--in-place)"),
        "sed_inplace FILE EXPRESSION... from deploy/lib/jasper-sed-inplace.sh "
        "— but only install-time shell can source it (install.sh reads it "
        "from the deploy checkout; unlike its deploy/lib siblings it is NOT "
        "installed to /usr/local/lib/jasper/). A deploy/bin script that runs "
        "on the Pi must inline `sed -i.bak` + `rm -f` instead",
    ),
    (
        "bash-4-only builtin — macOS /bin/bash is 3.2",
        re.compile(
            r"\b(?:mapfile|readarray)\b"
            r"|\b(?:declare|local|typeset)\s+-[A-Za-z]*A"
        ),
        "a `while IFS= read -r` loop, or parallel indexed arrays with a "
        "linear scan (deploy/bin/jasper-headphone-monitor)",
    ),
)


@pytest.mark.parametrize(
    ("reason", "pattern", "remedy"),
    _NON_PORTABLE_SPELLINGS,
    ids=("gnu-only-sed-inplace", "bash-4-only-builtins"),
)
def test_deploy_shell_stays_in_the_portable_dialect(reason, pattern, remedy):
    sources = shell_files("deploy")
    assert len(sources) >= 25, (
        f"shell-file selection collapsed to {len(sources)} files — the scan, "
        "not the tree, is what broke."
    )

    offenders = []
    for path in sources:
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            if line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(_REPO)}:{lineno}")
    assert not offenders, f"{reason}: {offenders}. Use {remedy}."


_IDENTITY_ONESHOT = "jasper-identity-reconcile.service"
_SYSTEMD = _REPO / "deploy" / "systemd"


@pytest.mark.parametrize(
    "reader", ["jasper-voice.service", "jasper-control.service"],
)
def test_start_time_hostname_readers_order_behind_the_identity_oneshot(reader):
    """These two resolve the speaker's name ONCE at start and keep it — voice
    through Config.from_env, control through the management-host allowlist's
    env arm. Before= alone orders only units already in the transaction, so
    each reader must also pull the oneshot in."""
    assert _IDENTITY_ONESHOT in values_for(
        (_SYSTEMD / reader).read_text(encoding="utf-8"), "Wants",
    )
    assert reader in values_for(
        (_SYSTEMD / _IDENTITY_ONESHOT).read_text(encoding="utf-8"), "Before",
    )


# ----------------------------------------------------------------------
# 6 — asynchronously activated units are gated on the install marker
# ----------------------------------------------------------------------

# Remove when the installer stops mutating /opt/jasper in place.
_INSTALL_MARKER_GATE = "!/run/jasper-install/in_progress"

# Async-activated units that deliberately carry NO gate, with the reason each
# is safe enough on a half-synced tree. Explicit rather than derived from "does
# the ExecStart name /opt/jasper", because an ExecStart can be a bash wrapper
# that execs venv Python (jasper-wifi-recover does exactly that) — the
# derivation would silently under-report. Stale entries fail: an ungated unit
# that stops being async-activated has to leave this list too.
_UNGATED_ASYNC_UNITS = {
    "jasper-identity-reconcile.service": "pure bash; no /opt/jasper reference",
    "jasper-dongle-recover.service": "its two reconciler legs are gated, but it also "
                                     "starts jasper-camilla and jasper-outputd — "
                                     "pre-existing mid-install exposure #4123 does "
                                     "not address",
    "jasper-wifi-recover.service": "bash recovery that must keep running during a "
                                   "long install; its Python branch self-gates on "
                                   "the marker instead",
    "jasper-journal-review.service": "pure bash + journalctl/awk; read-only, "
                                     "writes only its own state file, always exits 0",
}


def _async_activated_units() -> set[str]:
    """Units something other than the installer can start mid-install.

    udev SYSTEMD_WANTS targets and every timer's service. Path-activated
    services are excluded: a Condition there leaves the level-triggered .path
    re-triggering until TriggerLimitBurst fails it, so the installer stops
    those units instead.
    """
    units: set[str] = set()
    for rules in sorted(_DEPLOY.glob("udev/*.rules")):
        units.update(
            _shipped_unit_name(u)
            for u in _WANTS_RE.findall(rules.read_text(encoding="utf-8"))
        )
    for timer in sorted(_DEPLOY.glob("systemd/*.timer")):
        unit = value_for(timer.read_text(encoding="utf-8"), "Unit")
        units.add(unit or f"{timer.stem}.service")
    for watcher in sorted(_DEPLOY.glob("systemd/*.path")):
        unit = value_for(watcher.read_text(encoding="utf-8"), "Unit")
        units.discard(unit or f"{watcher.stem}.service")
    return units


def test_async_activated_units_are_gated_on_the_install_marker():
    """A new udev rule or timer added without the gate is the recurrence risk
    this pin exists for — not the units that carry it today."""
    async_units = _async_activated_units()
    # The #4123 unit reaches the set only through the udev chain, so its
    # absence means the derivation broke rather than the units regressing.
    assert "jasper-audio-hardware-reconcile.service" in async_units
    assert _UNGATED_ASYNC_UNITS.keys() <= async_units
    gated = {
        unit.name
        for unit in sorted(_DEPLOY.glob("systemd/*.service"))
        if unit.is_file()
        and _INSTALL_MARKER_GATE
        in values_for(unit.read_text(encoding="utf-8"), "ConditionPathExists")
    }
    assert gated == async_units - _UNGATED_ASYNC_UNITS.keys()
