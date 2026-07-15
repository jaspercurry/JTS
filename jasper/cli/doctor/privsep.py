# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — privilege-separation read access.

WS1 dropped jasper-control/-web/-chat-web/-mux/-voice/-input/-wiim-remote-mic
to non-root (each runs as its own ``jasper-<role>`` user with primary group
``jasper``). A config or
state file written ``0600`` root-only is then **unreadable** by the owning
daemon — and because every one of these reads is fail-soft (a caught
``OSError`` mapped to a benign default), a permission failure looks *identical*
to "not configured / healthy". The daemon keeps running with blank state and
nothing surfaces.

That bug class has already bitten twice:

- **#900** — ``grouping_leader.yml`` (and the sound configs) were left ``0600``
  by ``emit_sound_config``'s hand-rolled tempfile writer. jasper-control reads
  the active CamillaDSP config off-disk (``active_leader_pipe_path``) for the
  bonded-leader producer-liveness signal; the unreadable file resolved to
  ``""`` and ``/state`` reported a *bonded leader* "degraded — the stream is
  silent" while audio was flowing.
- **#901** — ``bt_roles.json`` was published ``0600`` by ``RoleStore._write``.
  No active denial (only jasper-web reads it), but latent the moment any
  non-root ``jasper``-group daemon needs it.

Nothing structural stopped the *next* writer from re-introducing it. This
module is that structural guard: for every non-root daemon it verifies that
each file the daemon's own code reads at runtime is actually readable by that
daemon's uid + group set, failing **loud** (WARN, with the exact file + mode)
at ``jasper-doctor`` / install time instead of silently degrading.

**Why this can't use ``os.access``.** ``jasper-doctor`` runs as **root**, so
``os.access`` (which tests the *caller's* permissions) would report every file
readable. Instead each check ``stat()``s the path and reasons about the
*daemon's* identity (uid, primary gid, supplementary gids — resolved from the
unit's live ``systemctl show`` directives) against the file's owner / group /
mode bits. This mirrors ``env.check_state_dir_group_writable`` (the *write*-side
sibling at order 23.5); this module is the *read* complement.

**Scope (confirmed with the owner): coarse + canonical, group-``jasper`` trees
only.** :data:`MANIFEST` lists the load-bearing reads per daemon — the security
gates, the SSOT files each daemon re-reads *fresh* (it is not restarted on a
wizard save), and the CamillaDSP configs of the #900/#901 family — under
``/var/lib/jasper`` and ``/var/lib/camilladsp`` (group ``jasper``). It is *not*
an exhaustive read trace, and it deliberately excludes the secret compartments
(``jasper-secrets`` / ``jasper-intsecrets``), whose own group ownership is a
separate concern. The manifest is drift-pinned against the systemd units by
``tests/test_doctor_privsep_manifest.py`` so it cannot fall behind a unit edit
or a newly-added non-root daemon.

The ``household_secret`` file gets a dedicated check
(:func:`check_household_secret_readable`): a *present-but-unreadable* secret
means the device-to-device ``/grouping/set`` auth gate has silently
fail-safe-**opened** (``household_credential.verify`` treats an unreadable
secret as "not paired" and accepts any caller). That degraded posture is
invisible to the existing ``grouping.check_grouping_household_credential``,
which keys on ``is_paired()`` — and ``is_paired()`` collapses *absent* and
*unreadable* into the same ``""``. This check is pure observability of the
ratified fail-safe-open behaviour (docs/HANDOFF-control-plane-auth.md); it does
not change ``household_credential.py``.
"""
from __future__ import annotations

import glob as _glob
import grp
import os
import pwd
import stat as _stat
import subprocess
from dataclasses import dataclass, field

from ._registry import doctor_check
from ._shared import CheckResult, _run


@dataclass(frozen=True)
class DaemonReadSpec:
    """One non-root daemon's declared runtime read-set + expected identity.

    ``unit`` is the systemd unit name (no ``.service``). ``unit_file`` is the
    repo-relative path to the canonical *non-root* unit, used by the drift test
    to pin ``user`` / ``group`` / ``supplementary_groups`` against the committed
    directives. ``paths`` is the COARSE canonical read-set — concrete paths or
    globs the daemon's own code opens for reading at runtime.
    """

    unit: str
    unit_file: str
    user: str
    group: str
    supplementary_groups: tuple[str, ...]
    paths: tuple[str, ...] = field(default_factory=tuple)


# COARSE + CANONICAL, group-`jasper` trees only. See module docstring for the
# scope decision. household_secret is intentionally NOT listed here — it has the
# dedicated check_household_secret_readable below (distinct fail-safe-open
# semantics), and listing it twice would double-report.
MANIFEST: tuple[DaemonReadSpec, ...] = (
    DaemonReadSpec(
        unit="jasper-control",
        unit_file="deploy/systemd/jasper-control.service",
        user="jasper-control",
        group="jasper",
        supplementary_groups=("systemd-journal", "jasper-intsecrets"),
        paths=(
            # Security gate: present-but-unreadable = CSRF gate silently off.
            "/var/lib/jasper/control_token",
            # SSOT files jasper-control re-reads FRESH on every /state / endpoint
            # call (it is not restarted on a wizard save).
            "/var/lib/jasper/grouping.env",
            "/var/lib/jasper/identity.env",
            "/var/lib/jasper/voice_provider.env",
            "/var/lib/jasper/speaker_name.env",
            "/var/lib/jasper/transit.env",
            "/var/lib/jasper/peering.env",
            "/var/lib/jasper/aec_mode.env",
            # /state's sound card: load_profile() + load_sound_settings() are
            # called fresh on every /state aggregation (control.state_aggregate),
            # so a 0600 regression silently degrades the dashboard sound card.
            "/var/lib/jasper/sound_profile.json",
            "/var/lib/jasper/sound_settings.json",
            # The #900 surface: statefile -> active CamillaDSP config, read for
            # the bonded-leader producer-liveness signal.
            "/var/lib/camilladsp/outputd-statefile.yml",
            "/var/lib/camilladsp/configs/*.yml",
        ),
    ),
    DaemonReadSpec(
        unit="jasper-web",
        # The full (non-root) unit. The streambox variant
        # (deploy/jasper-web-streambox.service) runs as root and self-skips at
        # runtime; the drift test pins against this non-root unit.
        unit_file="deploy/jasper-web.service",
        user="jasper-web",
        group="jasper",
        supplementary_groups=(
            "audio",
            "bluetooth",
            "systemd-journal",
            "jasper-secrets",
            "jasper-intsecrets",
        ),
        paths=(
            # EQ editor + the #900 sound config family.
            "/var/lib/camilladsp/configs/*.yml",
            # Wizard SSOT / status files re-read fresh on page render.
            "/var/lib/jasper/voice_provider.env",
            "/var/lib/jasper/wake_model.env",
            "/var/lib/jasper/transit.env",
            "/var/lib/jasper/speaker_name.env",
            "/var/lib/jasper/tool_state.env",
            # The #901 file: jasper-web hosts the bluetooth engine (web/
            # bluetooth_setup.py -> bluetooth.engine.RoleStore.load), which reads
            # bt_roles.json — jasper-control does NOT read it.
            "/var/lib/jasper/bt_roles.json",
            # /sound/ wizard reads the active profile + global settings.
            "/var/lib/jasper/sound_profile.json",
            "/var/lib/jasper/sound_settings.json",
        ),
    ),
    DaemonReadSpec(
        unit="jasper-chat-web",
        unit_file="deploy/jasper-chat-web.service",
        user="jasper-web",
        group="jasper",
        supplementary_groups=(),
        paths=(
            # /chat/ re-reads these fresh so the browser toggle takes effect
            # without restarting jasper-voice or jasper-chat-web.
            "/var/lib/jasper/conversation_history.env",
            "/var/lib/jasper/conversation_history.db",
        ),
    ),
    DaemonReadSpec(
        unit="jasper-mux",
        unit_file="deploy/systemd/jasper-mux.service",
        user="jasper-mux",
        group="jasper",
        supplementary_groups=("jasper-intsecrets",),
        paths=(
            "/var/lib/jasper/mux_mode.json",
            "/var/lib/jasper/speaker_volume.json",
        ),
    ),
    DaemonReadSpec(
        unit="jasper-voice",
        unit_file="deploy/systemd/jasper-voice.service",
        user="jasper-voice",
        group="jasper",
        supplementary_groups=("audio", "jasper-secrets", "jasper-intsecrets"),
        paths=(
            # Cross-daemon-written files jasper-voice reads fresh (the wizards
            # that write these run as jasper-web). voice's own usage.db /
            # wake-events.sqlite3 / conversation_history.db are WRITE-owned by
            # voice and covered by env.check_state_dir_group_writable — not
            # repeated here.
            "/var/lib/jasper/tool_state.env",
            "/var/lib/jasper/voice_provider.env",
            "/var/lib/jasper/mic_mute.env",
        ),
    ),
    # jasper-input reads NO on-disk config/state at runtime — it only watches
    # /dev/input/event* (kernel I/O, 'input' supplementary group) and calls
    # jasper-control over localhost HTTP. Kept here with an empty read-set so
    # the drift test pins its identity and a future on-disk read added to this
    # daemon is forced into the manifest rather than slipping in silently.
    DaemonReadSpec(
        unit="jasper-input",
        unit_file="deploy/systemd/jasper-input.service",
        user="jasper-input",
        group="jasper",
        supplementary_groups=("input",),
        paths=(),
    ),
    # jasper-wiim-remote-mic reads NO on-disk config/state at runtime. It listens
    # to BlueZ over system D-Bus ('bluetooth' supplementary group) and forwards
    # decoded PCM to the localhost manual-mic UDP source.
    DaemonReadSpec(
        unit="jasper-wiim-remote-mic",
        unit_file="deploy/systemd/jasper-wiim-remote-mic.service",
        user="jasper-input",
        group="jasper",
        supplementary_groups=("bluetooth",),
        paths=(),
    ),
)

_SPEC_BY_UNIT: dict[str, DaemonReadSpec] = {s.unit: s for s in MANIFEST}

# Non-root jasper-* units deliberately OUT of this check's scope. These run as
# `jasper-recon` (the reconciler tier — short-lived oneshots/monitors that own
# their own writes), not the Tier-A service daemons in MANIFEST above. The
# drift test enumerates every `User=jasper-*` unit and requires each to be
# either in MANIFEST or here, so a genuinely new non-root daemon can't be
# added without a conscious scope decision.
OUT_OF_SCOPE_NONROOT_UNITS: frozenset[str] = frozenset(
    {"jasper-dac-init", "jasper-headphone-monitor", "jasper-usbsink"}
)


# --------------------------------------------------------------------------- #
# Pure, hardware-free core (unit-tested directly with synthetic identities).
# --------------------------------------------------------------------------- #
def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in "*?[")


def _process_can_read(st: os.stat_result, uid: int, gids: frozenset[int]) -> bool:
    """Could a process with ``uid`` and supplementary group set ``gids`` read
    the ``stat``'d file? Standard POSIX owner/group/other precedence — owner
    bits win if the process owns the file, else group bits if it shares the
    file's group, else the other bits. (root/uid 0 is never one of our daemons,
    so no CAP_DAC_READ_SEARCH special-case is needed.)"""
    if st.st_uid == uid:
        return bool(st.st_mode & _stat.S_IRUSR)
    if st.st_gid in gids:
        return bool(st.st_mode & _stat.S_IRGRP)
    return bool(st.st_mode & _stat.S_IROTH)


def _describe(path: str, st: os.stat_result) -> str:
    """``path (owner:group 0NNN)`` for the WARN detail. Resolves owner/group
    names where possible, falling back to numeric ids."""
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except (KeyError, OSError):
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except (KeyError, OSError):
        group = str(st.st_gid)
    return f"{path} ({owner}:{group} {oct(st.st_mode & 0o777)})"


def _classify_readable_inputs(
    label: str,
    paths: tuple[str, ...],
    uid: int,
    gids: frozenset[int],
    user: str,
    *,
    stat_fn=os.stat,
    glob_fn=_glob.glob,
) -> CheckResult:
    """Core of the per-daemon read check, path + identity parameterized so it is
    unit-testable with tmp files and synthetic identities (mirrors
    ``env._classify_state_group_write``).

    Absent paths are skipped (absent = "not configured yet", not the bug class —
    the bug is *present-but-unreadable*). Globs expand via ``glob_fn``. Returns
    WARN naming each unreadable file + its mode, else OK.
    """
    unreadable: list[str] = []
    checked = 0
    for pattern in paths:
        matches = sorted(glob_fn(pattern)) if _is_glob(pattern) else [pattern]
        for match in matches:
            try:
                st = stat_fn(match)
            except OSError:
                continue  # absent / unstat-able → not the present-but-unreadable bug
            checked += 1
            if not _process_can_read(st, uid, gids):
                unreadable.append(_describe(match, st))
    if not checked:
        return CheckResult(label, "ok", f"no declared inputs present yet ({user})")
    if unreadable:
        shown = unreadable[:6]
        overflow = len(unreadable) - len(shown)
        if overflow:
            shown.append(f"(+{overflow} more)")
        return CheckResult(
            label,
            "warn",
            f"{user} cannot read: "
            + "; ".join(shown)
            + " — expected group `jasper`-readable (0640); the writer must emit "
            "mode 0o640 (atomic_write_text(mode=0o640)). Re-deploy after fixing.",
        )
    return CheckResult(label, "ok", f"{checked} input(s) readable by {user}")


def _household_secret_verdict(
    st: os.stat_result, uid: int, gids: frozenset[int], user: str
) -> CheckResult:
    """OK/WARN for a PRESENT household_secret, identity-parameterized for tests.
    Present-but-unreadable = the M2M ``/grouping/set`` gate is silently open."""
    label = "household secret readable"
    if _process_can_read(st, uid, gids):
        return CheckResult(
            label,
            "ok",
            f"present and readable by {user} — /grouping/set auth gate is enforced",
        )
    return CheckResult(
        label,
        "warn",
        f"present but UNREADABLE by {user} ({_describe('household_secret', st)}) — "
        "the device-to-device /grouping/set auth gate has silently fail-safe-OPENED "
        "(household_credential.verify treats an unreadable secret as 'not paired' "
        "and accepts any X-JTS-Household). Expected group `jasper`-readable (0640); "
        "re-deploy to heal.",
    )


# --------------------------------------------------------------------------- #
# Runtime identity resolution (on-Pi; degrades to skip off the Pi).
# --------------------------------------------------------------------------- #
def _unit_runtime_identity(unit: str) -> dict[str, str] | None:
    """``LoadState`` / ``User`` / ``Group`` / ``SupplementaryGroups`` from
    ``systemctl show`` for ``unit``, or ``None`` when systemctl is unavailable
    (dev / non-Linux host) so callers can fall through to a skipped-ok path.

    Reads the *runtime* identity, not the manifest's, so the streambox
    jasper-web (which runs as root) self-skips and any live unit edit is
    honoured."""
    try:
        proc = _run(
            [
                "systemctl",
                "show",
                "-p",
                "LoadState",
                "-p",
                "User",
                "-p",
                "Group",
                "-p",
                "SupplementaryGroups",
                f"{unit}.service",
            ]
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields or None


def _resolve_identity(
    user: str, group: str, supplementary_groups: tuple[str, ...]
) -> tuple[int, frozenset[int]] | None:
    """Resolve ``user`` to ``(uid, {gids})`` — the full group set a process
    started as ``user`` with primary ``group`` + ``supplementary_groups`` would
    hold. Returns ``None`` if the user does not exist on this host (dev box).

    The gid set is the union of the user's group-database memberships
    (``os.getgrouplist``) and the unit's declared primary + supplementary groups
    — a safe superset so a correctly group-readable file is never falsely
    flagged unreadable."""
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return None
    gids: set[int] = set()
    try:
        gids.update(os.getgrouplist(user, pw.pw_gid))
    except (KeyError, OSError):
        gids.add(pw.pw_gid)
    for name in (group, *supplementary_groups):
        if not name:
            continue
        try:
            gids.add(grp.getgrnam(name).gr_gid)
        except KeyError:
            pass
    return pw.pw_uid, frozenset(gids)


def _resolve_runtime(
    unit: str, label: str
) -> tuple[int, frozenset[int], str] | CheckResult:
    """Shared front half: returns ``(uid, gids, user)`` for a running non-root
    daemon, or an early-return ``CheckResult`` (skip-ok) when systemctl is
    unavailable, the unit isn't installed, it runs as root, or its user can't be
    resolved."""
    info = _unit_runtime_identity(unit)
    if info is None:
        return CheckResult(label, "ok", "systemctl unavailable — skipped (not Linux?)")
    if info.get("LoadState", "") in ("not-found", "masked"):
        return CheckResult(label, "ok", f"{unit} not installed (skipped)")
    user = info.get("User", "").strip()
    if user in ("", "root"):
        return CheckResult(label, "ok", f"{unit} runs as root (reads all inputs; n/a)")
    resolved = _resolve_identity(
        user,
        info.get("Group", "").strip() or "jasper",
        tuple(info.get("SupplementaryGroups", "").split()),
    )
    if resolved is None:
        return CheckResult(label, "ok", f"user {user!r} not resolvable — skipped")
    uid, gids = resolved
    return uid, gids, user


def _check_daemon(unit: str) -> CheckResult:
    spec = _SPEC_BY_UNIT[unit]
    label = f"daemon reads: {unit}"
    if not spec.paths:
        # Still honour "not installed" so a streambox shows the skip, but a
        # read-less daemon (jasper-input) never needs identity resolution.
        info = _unit_runtime_identity(unit)
        if info is not None and info.get("LoadState", "") in ("not-found", "masked"):
            return CheckResult(label, "ok", f"{unit} not installed (skipped)")
        return CheckResult(label, "ok", f"{unit} declares no on-disk inputs (n/a)")
    resolved = _resolve_runtime(unit, label)
    if isinstance(resolved, CheckResult):
        return resolved
    uid, gids, user = resolved
    return _classify_readable_inputs(label, spec.paths, uid, gids, user)


@doctor_check(order=23.55, group="privsep")
def check_control_readable_inputs() -> CheckResult:
    """jasper-control must be able to read its runtime inputs (the #900 surface
    + the SSOT files it re-reads fresh + the CSRF token). See module docstring."""
    return _check_daemon("jasper-control")


@doctor_check(order=23.56, group="privsep")
def check_web_readable_inputs() -> CheckResult:
    """jasper-web must be able to read the camilla configs + wizard SSOT/status
    files it renders (the #901 bt_roles.json surface). Skips on streambox, where
    jasper-web runs as root."""
    return _check_daemon("jasper-web")


@doctor_check(order=23.565, group="privsep")
def check_chat_web_readable_inputs() -> CheckResult:
    """jasper-chat-web must be able to read the conversation-history settings
    and SQLite store it renders and mutates."""
    return _check_daemon("jasper-chat-web")


@doctor_check(order=23.57, group="privsep")
def check_mux_readable_inputs() -> CheckResult:
    """jasper-mux must be able to read its source-policy state (manual-source pin
    + persisted volume)."""
    return _check_daemon("jasper-mux")


@doctor_check(order=23.58, group="privsep")
def check_voice_readable_inputs() -> CheckResult:
    """jasper-voice must be able to read the cross-daemon-written files it reads
    fresh (tool state, active provider, mic-mute privacy state)."""
    return _check_daemon("jasper-voice")


@doctor_check(order=23.59, group="privsep")
def check_input_readable_inputs() -> CheckResult:
    """jasper-input declares no on-disk reads; this is its drift-pin so a future
    file read is forced into the manifest."""
    return _check_daemon("jasper-input")


@doctor_check(order=23.595, group="privsep")
def check_household_secret_readable() -> CheckResult:
    """A PRESENT household_secret must be readable by jasper-control (the
    ``/grouping/set`` verify gate). Present-but-unreadable = the gate has
    silently fail-safe-OPENED; absent = this speaker is simply not paired (ok).

    Complements ``grouping.check_grouping_household_credential``, which keys on
    ``is_paired()`` and therefore cannot distinguish absent from unreadable."""
    from ...control import household_credential

    label = "household secret readable"
    path = household_credential.SECRET_FILE
    try:
        st = os.stat(path)
    except OSError:
        return CheckResult(label, "ok", "absent — this speaker is not paired (n/a)")
    resolved = _resolve_runtime("jasper-control", label)
    if isinstance(resolved, CheckResult):
        return resolved
    uid, gids, user = resolved
    return _household_secret_verdict(st, uid, gids, user)
