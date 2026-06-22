# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — WS1 Phase 4 secret-compartment on-disk posture.

The *secret-side complement* to :mod:`~jasper.cli.doctor.privsep`. privsep verifies
a **one-sided** contract — each non-root daemon can READ the group-``jasper`` config
it needs — and deliberately excludes the secret compartments because a secret has a
**two-sided** contract that a "more-readable-is-fine" check cannot express:

1. **Availability.** The secret must be readable by the daemons IN its compartment.
   A ``0600``-root or wrong-group secret silently breaks the Gmail / Calendar /
   Spotify / Home-Assistant tools and looks *identical* to "not configured" — the
   #900/#901 fail-soft bug class (a caught ``OSError`` → benign default).
2. **Confidentiality** ("treat secrets as secrets"). The secret must NOT be readable
   *outside* its compartment. If it regresses to ``o+r`` (world) or group ``jasper``
   (back in the shared group every daemon holds), the Phase 4 compartmentalization
   silently dissolves — jasper-mux/-control/-input regain the LLM keys + identity-grade
   OAuth tokens. privsep's one-sided check **passes** an over-readable secret (more
   access satisfies "must be readable"), so it structurally cannot catch this. That is
   why this is a distinct check.

WS1 Phase 4a/4b moved the real secrets into two sibling compartments OUTSIDE the
``/var/lib/jasper`` StateDirectory (whose recursive chown would force them back to
group ``jasper``):

- ``/var/lib/jasper-secrets`` (group ``jasper-secrets``, members voice + web
  surfaces) — the 3 LLM API keys (``voice_keys.env``),
  ``google_credentials.env``, the Google OAuth token tree.
- ``/var/lib/jasper-intsecrets`` (group ``jasper-intsecrets``, members voice + control
  + mux + web surfaces) — ``home_assistant.env``, ``spotify_credentials.env``,
  the Spotify token cache.

Static tests (``test_secret_env_modes``, ``test_install_secrets_migration``,
``test_systemd_hardening``) pin the install/writer *code*; nothing checked the **live
box**. A manual ``chmod``, a half-applied migration, a backup restore, or a future
writer that forgets ``SECRET_ENV_MODE`` drifts on disk with no runtime signal — in
*either* direction. This module is that runtime signal.

**Why this can't use ``os.access``.** ``jasper-doctor`` runs as **root**, so
``os.access`` (the *caller's* permissions) reports every file readable. Each check
``stat()``s the path and reasons about the compartment members' and non-members'
*identities* (uid + group set, resolved from each unit's live ``systemctl show``),
exactly like :mod:`privsep`'s read-side core — which this module reuses
(``_unit_runtime_identity`` / ``_resolve_identity`` / ``_process_can_read``).

**Severity (confirmed with the owner): a deliberate divergence from the rest of the
doctor.** Every other permission check WARNs on drift. Here, *under*-permission
(availability — a member daemon can't read; a re-deploy heals it) WARNs, but
*over*-permission (a non-member daemon — or the world — CAN read; the compartment has
silently dissolved) **FAILs** — louder than WARN, because a confidentiality regression
is invisible everywhere else and re-tightens only on an explicit re-deploy.

**Reports are strictly secret-free** — owner / group / octal-mode / daemon-name only,
never a byte of the secret (mirrors ``check_control_token`` /
``check_grouping_household_credential``).
"""
from __future__ import annotations

import glob as _glob
import grp
import os
import stat as _stat
from dataclasses import dataclass, field

from . import privsep
from ._registry import doctor_check
from ._shared import CheckResult

# Reuse privsep's read primitive + runtime identity resolution rather than
# re-deriving them (the task's "reuse where it helps"). They are the single home
# for "could a process with this uid + group-set read this stat'd file?" and
# "what identity does this unit run as?".
_process_can_read = privsep._process_can_read
_describe = privsep._describe


@dataclass(frozen=True)
class SecretCompartment:
    """One Phase 4 secret compartment's on-disk contract.

    ``group`` is the dedicated compartment group; ``directory`` the sibling tree
    outside the StateDirectory. ``member_units`` are the non-root daemons that MUST
    be able to read the secrets (the availability side) — the drift test pins this
    against each unit's ``SupplementaryGroups=``. ``files`` is the canonical set of
    secret paths (concrete + globs) the compartment holds; absent ones are skipped
    (absent = "not configured", not the bug class).
    """

    group: str
    directory: str
    member_units: tuple[str, ...]
    files: tuple[str, ...] = field(default_factory=tuple)


# The universe of leak targets is the Tier-A non-root daemons privsep already
# models. A compartment's NON-members are this universe minus its members —
# exactly the daemons Phase 4 documents losing the secret (4a: mux/control/input;
# 4b: input). Units that intentionally share a compartment-holding Unix identity
# are members even when their own unit file does not repeat SupplementaryGroups:
# e.g. jasper-chat-web runs as the jasper-web user, and that user is in both
# compartment groups on disk. The recon-tier oneshots
# (privsep.OUT_OF_SCOPE_NONROOT_UNITS) are in no compartment group and run as
# jasper-recon, so they are not leak targets; world-exposure is caught by the
# o-bit test independently.
_UNIVERSE_UNITS: tuple[str, ...] = tuple(sorted(s.unit for s in privsep.MANIFEST))


COMPARTMENTS: tuple[SecretCompartment, ...] = (
    SecretCompartment(
        group="jasper-secrets",
        directory="/var/lib/jasper-secrets",
        member_units=("jasper-chat-web", "jasper-voice", "jasper-web"),
        files=(
            # The 3 LLM API keys split out of voice_provider.env (Phase 4a).
            "/var/lib/jasper-secrets/voice_keys.env",
            # Google OAuth client secret + the per-account refresh-token tree.
            "/var/lib/jasper-secrets/google_credentials.env",
            "/var/lib/jasper-secrets/google/accounts.json",
            "/var/lib/jasper-secrets/google/tokens/*.json",
        ),
    ),
    SecretCompartment(
        group="jasper-intsecrets",
        directory="/var/lib/jasper-intsecrets",
        member_units=(
            "jasper-chat-web",
            "jasper-control",
            "jasper-mux",
            "jasper-voice",
            "jasper-web",
        ),
        files=(
            "/var/lib/jasper-intsecrets/home_assistant.env",
            "/var/lib/jasper-intsecrets/spotify_credentials.env",
            # Legacy single-account Spotify cache + the multi-account tree.
            "/var/lib/jasper-intsecrets/.spotify-cache",
            "/var/lib/jasper-intsecrets/spotify/accounts.json",
            "/var/lib/jasper-intsecrets/spotify/caches/*.json",
        ),
    ),
)

_COMPARTMENT_BY_GROUP: dict[str, SecretCompartment] = {c.group: c for c in COMPARTMENTS}

# The setgid + owner-rwx + group-rwx + other-none mode every compartment dir uses.
_EXPECTED_DIR_MODE = 0o2770


@dataclass(frozen=True)
class _Identity:
    """A resolved daemon identity for the read tests."""

    uid: int
    gids: frozenset[int]
    user: str


def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in "*?[")


def _unique_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


# --------------------------------------------------------------------------- #
# Pure, hardware-free classifiers (unit-tested with tmp files + synthetic ids).
# --------------------------------------------------------------------------- #
def _file_over_exposed_to(
    st: os.stat_result, non_members: list[_Identity]
) -> list[str]:
    """Which NON-member daemons (or the world) can read this stat'd file.

    The confidentiality side: any reader outside the compartment means the Phase 4
    isolation has dissolved. Catches BOTH regressions in one POSIX evaluation —
    a broad group (e.g. ``jasper``, which every non-member holds, with the group-read
    bit) and a world-read bit (a non-member that shares no group falls through to the
    *other* bits). The ``world`` sentinel is added explicitly so the FAIL still fires
    on a host where the non-member daemons don't resolve (e.g. they aren't installed).
    """
    exposed: list[str] = []
    if st.st_mode & (_stat.S_IROTH | _stat.S_IWOTH):
        exposed.append("world")
    for nm in non_members:
        if _process_can_read(st, nm.uid, nm.gids):
            exposed.append(nm.user)
    return _unique_names(exposed)


def _file_unreadable_by(st: os.stat_result, members: list[_Identity]) -> list[str]:
    """Which member daemons CANNOT read this stat'd file (the availability side)."""
    return _unique_names([
        m.user for m in members if not _process_can_read(st, m.uid, m.gids)
    ])


def _classify_compartment(
    label: str,
    comp: SecretCompartment,
    members: list[_Identity],
    non_members: list[_Identity],
    *,
    stat_fn=os.stat,
    glob_fn=_glob.glob,
) -> CheckResult:
    """Core of a per-compartment check, identity + fs parameterized for unit tests.

    One ``CheckResult`` aggregates the dir + every present secret file (the "doctor
    checks stay flat" rule — one result per function, mirroring
    ``env._classify_state_group_write``). FAIL on any over-exposure (a non-member /
    the world can read), else WARN on any under-availability or dir drift, else OK.
    Absent dir → OK (compartment not present / nothing configured). Absent files are
    skipped (absent ≠ the present-but-drifted bug).
    """
    fails: list[str] = []  # over-exposure (confidentiality)
    warns: list[str] = []  # under-availability + dir posture drift

    # --- the compartment directory -----------------------------------------
    try:
        dir_st = stat_fn(comp.directory)
    except OSError:
        return CheckResult(
            label,
            "ok",
            f"{comp.directory} absent — compartment not present (nothing configured)",
        )

    try:
        dir_group = grp.getgrgid(dir_st.st_gid).gr_name
    except (KeyError, OSError):
        dir_group = str(dir_st.st_gid)
    dir_mode = dir_st.st_mode & 0o7777

    # A non-member that can TRAVERSE (execute) the dir, or any world bit, is the
    # over-exposure: combined with a file bit it reaches the secret. Reuse the
    # read primitive against the execute bits by testing each precedence tier.
    dir_world = bool(dir_st.st_mode & (_stat.S_IROTH | _stat.S_IWOTH | _stat.S_IXOTH))
    dir_exposed_to = _unique_names([
        nm.user for nm in non_members if _dir_traversable_by(dir_st, nm.uid, nm.gids)
    ])
    if dir_world or dir_exposed_to:
        who = ", ".join(dir_exposed_to + (["world"] if dir_world else []))
        fails.append(
            f"dir {comp.directory} ({dir_group} {oct(dir_mode)}) grants traverse to "
            f"{who} beyond `{comp.group}` — the compartment gate is open "
            f"(expected 2770 group `{comp.group}`); re-deploy to re-tighten"
        )
    elif dir_group != comp.group or dir_mode != _EXPECTED_DIR_MODE:
        warns.append(
            f"dir {comp.directory} is {dir_group} {oct(dir_mode)}, expected group "
            f"`{comp.group}` mode 2770 (setgid → new files inherit the compartment "
            "group); re-deploy to heal"
        )
    else:
        unreach = _unique_names([
            m.user for m in members if not _dir_traversable_by(dir_st, m.uid, m.gids)
        ])
        if unreach:
            warns.append(
                f"dir {comp.directory} ({dir_group} {oct(dir_mode)}) not traversable "
                f"by {', '.join(unreach)}; re-deploy to heal"
            )

    # --- each present secret file ------------------------------------------
    checked = 0
    for pattern in comp.files:
        matches = sorted(glob_fn(pattern)) if _is_glob(pattern) else [pattern]
        for match in matches:
            try:
                st = stat_fn(match)
            except OSError:
                continue  # absent → not configured, not the bug
            checked += 1
            exposed = _file_over_exposed_to(st, non_members)
            if exposed:
                fails.append(
                    f"{_describe(match, st)} grants read to {', '.join(exposed)} "
                    f"beyond `{comp.group}` — expected 0640 group `{comp.group}` "
                    "(no o+r, not the shared `jasper` group); re-deploy to re-tighten"
                )
                continue
            unreadable = _file_unreadable_by(st, members)
            if unreadable:
                warns.append(
                    f"{_describe(match, st)} not readable by {', '.join(unreadable)} "
                    f"— expected group `{comp.group}` 0640; re-deploy to heal"
                )

    if fails:
        shown = _truncate(fails)
        detail = "OVER-EXPOSED (Phase 4 isolation regressed): " + "; ".join(shown)
        if warns:
            detail += f" (+{len(warns)} availability warning(s))"
        return CheckResult(label, "fail", detail)
    if warns:
        return CheckResult(label, "warn", "; ".join(_truncate(warns)))
    member_names = ", ".join(_unique_names([m.user for m in members]))
    member_names = member_names or "no members resolved"
    return CheckResult(
        label,
        "ok",
        f"dir 2770 group `{comp.group}`, {checked} secret(s) readable only by "
        f"{member_names}",
    )


def _dir_traversable_by(st: os.stat_result, uid: int, gids: frozenset[int]) -> bool:
    """Could a process with ``uid`` + ``gids`` execute (enter) this directory?

    Mirrors ``privsep._process_can_read``'s owner/group/other precedence but on the
    execute bit — directory traverse, which (with a readable file inside) is what a
    non-member needs to reach a secret."""
    if st.st_uid == uid:
        return bool(st.st_mode & _stat.S_IXUSR)
    if st.st_gid in gids:
        return bool(st.st_mode & _stat.S_IXGRP)
    return bool(st.st_mode & _stat.S_IXOTH)


def _truncate(items: list[str], limit: int = 6) -> list[str]:
    shown = items[:limit]
    overflow = len(items) - len(shown)
    if overflow:
        shown = [*shown, f"(+{overflow} more)"]
    return shown


# --------------------------------------------------------------------------- #
# Runtime identity resolution (on-Pi; degrades to skip off the Pi).
# --------------------------------------------------------------------------- #
def _systemctl_available() -> bool:
    """True if ``systemctl show`` works on this host. ``_unit_runtime_identity``
    returns ``None`` only when the systemctl subprocess errors (dev / non-Linux
    host) — a real unit, even a not-found one, yields a ``LoadState`` field."""
    return privsep._unit_runtime_identity(_UNIVERSE_UNITS[0]) is not None


def _resolve_unit(unit: str) -> _Identity | None:
    """``_Identity`` for a running NON-root ``unit``, or ``None`` to skip it (not
    installed / runs as root / user unresolvable / a transient systemctl miss).

    A root unit resolves to ``None`` deliberately: as a member it reads everything
    (no availability concern — the streambox jasper-web case), and as a non-member it
    is root (trusted; root reading a secret is not the compartment leak we guard).
    """
    info = privsep._unit_runtime_identity(unit)
    if info is None:
        return None
    if info.get("LoadState", "") in ("not-found", "masked"):
        return None
    user = info.get("User", "").strip()
    if user in ("", "root"):
        return None
    resolved = privsep._resolve_identity(
        user,
        info.get("Group", "").strip() or "jasper",
        tuple(info.get("SupplementaryGroups", "").split()),
    )
    if resolved is None:
        return None
    uid, gids = resolved
    return _Identity(uid=uid, gids=frozenset(gids), user=user)


def _check_compartment(group: str) -> CheckResult:
    comp = _COMPARTMENT_BY_GROUP[group]
    label = f"secret compartment: {group}"
    if not _systemctl_available():
        return CheckResult(label, "ok", "systemctl unavailable — skipped (not Linux?)")
    member_units = set(comp.member_units)
    members: list[_Identity] = []
    non_members: list[_Identity] = []
    for unit in _UNIVERSE_UNITS:
        ident = _resolve_unit(unit)
        if ident is None:
            continue  # not installed / root / unresolvable → not a leak target
        (members if unit in member_units else non_members).append(ident)
    return _classify_compartment(label, comp, members, non_members)


@doctor_check(order=23.6, group="privsep")
def check_jasper_secrets_compartment() -> CheckResult:
    """The ``jasper-secrets`` compartment (LLM keys + Google) must be readable by
    voice+web ONLY. FAIL if a non-member (mux/control/input) or the world can read a
    secret (confidentiality regressed); WARN if a member can't (availability). Skips
    when the compartment is absent or systemctl is unavailable."""
    return _check_compartment("jasper-secrets")


@doctor_check(order=23.61, group="privsep")
def check_jasper_intsecrets_compartment() -> CheckResult:
    """The ``jasper-intsecrets`` compartment (Home Assistant + Spotify) must be
    readable by voice/control/mux/web ONLY. FAIL if jasper-input or the world can read
    a secret; WARN if a member can't. Skips when absent / systemctl unavailable."""
    return _check_compartment("jasper-intsecrets")
