# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks — privilege-separation read access.

The jasper-* service daemons run non-root under primary group ``jasper``. A
config or state file written ``0600`` root-only is then unreadable by the
owning daemon — and because those reads are fail-soft (a caught ``OSError``
mapped to a benign default), a permission failure looks *identical* to "not
configured / healthy". This module verifies, per daemon, that each file its
code reads at runtime really is readable by that daemon's uid + group set,
and WARNs with the file and mode instead of letting it degrade silently.

``os.access`` cannot answer that: jasper-doctor runs as root, and
``os.access`` tests the *caller's* permissions. Each check ``stat()``s the
path and reasons about the daemon's own identity (uid, primary gid,
supplementary gids resolved from the unit's live ``systemctl show``
directives) against the file's owner / group / mode bits.

Scope is coarse and canonical: :data:`MANIFEST` lists the load-bearing reads
per daemon — the security gates, the SSOT files each daemon re-reads fresh,
and the CamillaDSP configs — under the group-``jasper`` trees
``/var/lib/jasper`` and ``/var/lib/camilladsp``. It is not an exhaustive read
trace, and it excludes the secret compartments (see
:mod:`~jasper.cli.doctor.secret_compartments`).
``tests/test_doctor_privsep.py`` drift-pins it against the systemd units so it
cannot fall behind a unit edit or a new non-root daemon.

The ``household_secret`` file gets its own check
(:func:`check_household_secret_readable`) because present-but-unreadable
there means the device-to-device ``/grouping/set`` gate has silently
fail-safe-OPENED, which ``grouping.check_grouping_household_credential``
cannot see: it keys on ``is_paired()``, which collapses absent and unreadable
into the same ``""``.
"""
from __future__ import annotations

import glob as _glob
import grp
import os
import pwd
import stat as _stat
from dataclasses import dataclass, field

from ...accessories.mic_env import DEFAULT_ACCESSORY_MIC_ENV_FILE
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import REASON_SYSTEMCTL_UNAVAILABLE, CheckResult

# Machine-stable codes naming which branch of a privsep check produced a
# result (AGENTS.md: tests pin status + reason, never detail prose).
REASON_UNIT_NOT_INSTALLED = "daemon_reads_unit_not_installed"
REASON_UNIT_RUNS_AS_ROOT = "daemon_reads_unit_runs_as_root"
REASON_USER_NOT_RESOLVABLE = "daemon_reads_user_not_resolvable"
REASON_NO_DECLARED_INPUTS = "daemon_reads_no_declared_inputs"

REASON_NO_INPUTS_PRESENT = "daemon_reads_no_inputs_present"
REASON_INPUTS_UNREADABLE = "daemon_reads_unreadable"
REASON_INPUTS_READABLE = "daemon_reads_readable"

REASON_HOUSEHOLD_SECRET_ABSENT = "household_secret_absent"
REASON_HOUSEHOLD_SECRET_READABLE = "household_secret_readable"
REASON_HOUSEHOLD_SECRET_UNREADABLE = "household_secret_unreadable"


@dataclass(frozen=True)
class DaemonReadSpec:
    """One non-root daemon's declared runtime read-set + expected identity.

    ``unit`` carries no ``.service`` suffix. ``unit_file`` is the
    repo-relative path to the canonical *non-root* unit, which the drift test
    pins ``user`` / ``group`` / ``supplementary_groups`` against. ``paths``
    are concrete paths or globs the daemon's own code opens for reading.
    """

    unit: str
    unit_file: str
    user: str
    group: str
    supplementary_groups: tuple[str, ...]
    paths: tuple[str, ...] = field(default_factory=tuple)


# Coarse + canonical, group-`jasper` trees only (see module docstring).
# household_secret is deliberately absent: check_household_secret_readable
# below owns it, and listing it twice would double-report.
MANIFEST: tuple[DaemonReadSpec, ...] = (
    DaemonReadSpec(
        unit="jasper-control",
        unit_file="deploy/systemd/jasper-control.service",
        user="jasper-control",
        group="jasper",
        supplementary_groups=(
            "systemd-journal",
            "jasper-intsecrets",
            # Read access to /dev/shm/jts-ring/grouping.ring's shared header
            # for /state's grouping `ring` block. Not in `paths` below: that
            # set is scoped to the group-`jasper` state trees.
            "jts-ring",
        ),
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
            # Statefile -> active CamillaDSP config, read for the
            # bonded-leader producer-liveness signal.
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
            # /dev/shm/jts-ring write access for the wizard-spawned
            # correction-lane aplay children, exercised only while this box's
            # correction lane is armed.
            "jts-ring",
        ),
        paths=(
            # EQ editor + the sound config family.
            "/var/lib/camilladsp/configs/*.yml",
            # Wizard SSOT / status files re-read fresh on page render.
            "/var/lib/jasper/voice_provider.env",
            "/var/lib/jasper/wake_model.env",
            "/var/lib/jasper/transit.env",
            # weather_setup._load_state opens this on every /weather/ render,
            # same shape as transit.env above.
            "/var/lib/jasper/weather.env",
            "/var/lib/jasper/speaker_name.env",
            "/var/lib/jasper/tool_state.env",
            # jasper-web hosts the bluetooth engine (web/bluetooth_setup.py ->
            # bluetooth.engine.RoleStore.load), which reads bt_roles.json;
            # jasper-control does NOT read it.
            "/var/lib/jasper/bt_roles.json",
            # /sound/ wizard reads the active profile + global settings.
            "/var/lib/jasper/sound_profile.json",
            "/var/lib/jasper/sound_settings.json",
            # The two stores the /sound/ design page renders from. The
            # crossover-accept seam writes both from the ROOT correction-web
            # process, so an unreadable one here renders the page empty against
            # a store the API reports "unreadable" at revision 0.
            "/var/lib/jasper/active_speaker_design_draft.json",
            "/var/lib/jasper/active_speaker_crossover_preview.json",
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
        unit="jasper-correction-web",
        unit_file="deploy/jasper-correction-web.service",
        user="jasper-web",
        group="jasper",
        supplementary_groups=("audio", "jts-ring"),
        paths=(
            # The graphs /correction/ validates, applies, and rolls back.
            "/var/lib/camilladsp/configs/*.yml",
            # Written by whichever commissioning arm measured first — /sound/
            # as jasper-web, or this unit. An unreadable one reads as "no
            # measurements" and silently discards the household's captures.
            "/var/lib/jasper/active_speaker_measurements.json",
            "/var/lib/jasper/active_speaker_design_draft.json",
            "/var/lib/jasper/active_speaker_crossover_preview.json",
            # The tuning surface reads the provider file fresh for its spend
            # settings and sums BOTH ledgers for the household cap, so an
            # unreadable one under-counts spend against a paid API.
            "/var/lib/jasper/voice_provider.env",
            "/var/lib/jasper/usage.db",
            "/var/lib/jasper/usage-tuning.db",
        ),
    ),
    DaemonReadSpec(
        unit="jasper-bluetooth-web",
        unit_file="deploy/jasper-bluetooth-web.service",
        user="jasper-web",
        group="jasper",
        supplementary_groups=("bluetooth",),
        paths=(
            # bt_roles.json from its other reader. RoleStore.set() LOADS
            # before it writes, so unreadable here does not degrade — it
            # republishes an empty map and forgets every device's handler.
            "/var/lib/jasper/bt_roles.json",
            # The shared source-intent SSOT the Bluetooth power switch and
            # /sources/ both drive.
            "/var/lib/jasper/source_intent.env",
        ),
    ),
    DaemonReadSpec(
        unit="jasper-system-web",
        unit_file="deploy/jasper-system-web.service",
        user="jasper-web",
        group="jasper",
        supplementary_groups=(),
        paths=(
            # The dashboard writes nothing and proxies the rest to
            # jasper-control. Its one on-disk read is the token canonical_page()
            # embeds — and _stored_token() fails safe to gate-OFF on EACCES.
            "/var/lib/jasper/control_token",
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
    # jasper-input's one on-disk read is the accessory reconciler's published
    # mic sources, which decide whether this process also runs an accessory mic
    # adapter task (ADR-0225); an unreadable file costs the box its remote
    # microphone. The adapter's 'bluetooth' grant is absent here because the
    # unit does not declare it either — _resolve_identity picks it up from the
    # user's own group memberships.
    DaemonReadSpec(
        unit="jasper-input",
        unit_file="deploy/systemd/jasper-input.service",
        user="jasper-input",
        group="jasper",
        supplementary_groups=("input",),
        paths=(DEFAULT_ACCESSORY_MIC_ENV_FILE,),
    ),
    DaemonReadSpec(
        unit="jasper-usbmic",
        unit_file="deploy/systemd/jasper-usbmic.service",
        user="jasper-usbmic",
        group="jasper",
        supplementary_groups=("audio",),
        paths=(
            # Explicit export intent is the relay's only persisted policy
            # input. Assistant pause state deliberately does not gate the Mac
            # microphone export.
            "/var/lib/jasper/usb_mic.env",
        ),
    ),
)

_SPEC_BY_UNIT: dict[str, DaemonReadSpec] = {s.unit: s for s in MANIFEST}

# Non-root jasper-* units deliberately OUT of scope: they run as
# `jasper-recon` (short-lived oneshots/monitors owning their own writes), not
# the service daemons in MANIFEST. The drift test requires every
# `User=jasper-*` unit to appear in MANIFEST or here, so a new non-root daemon
# cannot be added without a scope decision.
OUT_OF_SCOPE_NONROOT_UNITS: frozenset[str] = frozenset(
    {"jasper-dac-init", "jasper-headphone-monitor", "jasper-usbsink"}
)


def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in "*?[")


def _process_can_read(st: os.stat_result, uid: int, gids: frozenset[int]) -> bool:
    """Could a process with ``uid`` and supplementary group set ``gids`` read
    the ``stat``'d file? POSIX owner/group/other precedence; uid 0 is never
    one of these daemons, so no CAP_DAC_READ_SEARCH special case."""
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
    """Core of the per-daemon read check, path + identity parameterized.

    Absent paths are skipped — absent means "not configured yet", while the
    bug class is present-but-unreadable. Returns WARN naming each unreadable
    file + its mode, else OK.
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
        return CheckResult(
            label, "skipped", f"no declared inputs present yet ({user})",
            reason=REASON_NO_INPUTS_PRESENT,
        )
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
            reason=REASON_INPUTS_UNREADABLE,
        )
    return CheckResult(
        label, "ok", f"{checked} input(s) readable by {user}",
        reason=REASON_INPUTS_READABLE,
    )


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
            reason=REASON_HOUSEHOLD_SECRET_READABLE,
        )
    return CheckResult(
        label,
        "warn",
        f"present but UNREADABLE by {user} ({_describe('household_secret', st)}) — "
        "the device-to-device /grouping/set auth gate has silently fail-safe-OPENED "
        "(household_credential.verify treats an unreadable secret as 'not paired' "
        "and accepts any X-JTS-Household). Expected group `jasper`-readable (0640); "
        "re-deploy to heal.",
        reason=REASON_HOUSEHOLD_SECRET_UNREADABLE,
    )


# Every User/Group/SupplementaryGroups lookup below batches over this SAME
# tuple, so `evidence.unit_property`'s memoization makes it one
# `systemctl show` per property for the whole run, however many daemons ask.
_MANIFEST_UNITS: tuple[str, ...] = tuple(s.unit for s in MANIFEST)
_MANIFEST_UNIT_NAMES: tuple[str, ...] = tuple(f"{u}.service" for u in _MANIFEST_UNITS)


def _manifest_unit_property_map(prop: str) -> dict[str, str] | None:
    """{unit: value} for one property outside SHOW_PROPERTIES (User, Group,
    SupplementaryGroups), read once per run for the whole manifest. None when
    systemctl is unavailable or the reply shape is wrong."""
    values = evidence.unit_property(prop, _MANIFEST_UNIT_NAMES)
    if values is None:
        return None
    return dict(zip(_MANIFEST_UNITS, values))


def _unit_runtime_identity(unit: str) -> dict[str, str] | None:
    """``LoadState`` / ``User`` / ``Group`` / ``SupplementaryGroups`` for
    ``unit``, or ``None`` when systemctl is unavailable (dev / non-Linux
    host) so callers can fall through to a skipped-ok path.

    Reads the *runtime* identity, not the manifest's, so a unit that runs as
    root self-skips and any live unit edit is honoured."""
    state = evidence.unit_state(f"{unit}.service")
    if state is None:
        return None
    fields: dict[str, str] = {"LoadState": state.get("load_state") or ""}
    for prop in ("User", "Group", "SupplementaryGroups"):
        prop_map = _manifest_unit_property_map(prop)
        if prop_map is None:
            return None
        fields[prop] = prop_map.get(unit, "")
    return fields


def _resolve_identity(
    user: str, group: str, supplementary_groups: tuple[str, ...]
) -> tuple[int, frozenset[int]] | None:
    """Resolve ``user`` to ``(uid, {gids})`` — the full group set a process
    started as ``user`` with primary ``group`` + ``supplementary_groups``
    would hold, or ``None`` if the user does not exist on this host.

    The gid set unions the user's group-database memberships with the unit's
    declared groups: a safe superset, so a correctly group-readable file is
    never falsely flagged unreadable."""
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
        return CheckResult(
            label, "skipped", "systemctl unavailable — skipped (not Linux?)",
            reason=REASON_SYSTEMCTL_UNAVAILABLE,
        )
    if info.get("LoadState", "") in ("not-found", "masked"):
        return CheckResult(
            label, "skipped", f"{unit} not installed",
            reason=REASON_UNIT_NOT_INSTALLED,
        )
    user = info.get("User", "").strip()
    if user in ("", "root"):
        return CheckResult(
            label, "skipped", f"{unit} runs as root (reads all inputs; n/a)",
            reason=REASON_UNIT_RUNS_AS_ROOT,
        )
    resolved = _resolve_identity(
        user,
        info.get("Group", "").strip() or "jasper",
        tuple(info.get("SupplementaryGroups", "").split()),
    )
    if resolved is None:
        return CheckResult(
            label, "skipped", f"user {user!r} not resolvable — skipped",
            reason=REASON_USER_NOT_RESOLVABLE,
        )
    uid, gids = resolved
    return uid, gids, user


def _check_daemon(unit: str) -> CheckResult:
    spec = _SPEC_BY_UNIT[unit]
    label = f"daemon reads: {unit}"
    if not spec.paths:
        # Still honour "not installed" so a streambox shows the skip; a
        # read-less daemon needs no identity resolution beyond that.
        info = _unit_runtime_identity(unit)
        if info is not None and info.get("LoadState", "") in ("not-found", "masked"):
            return CheckResult(
                label, "skipped", f"{unit} not installed",
                reason=REASON_UNIT_NOT_INSTALLED,
            )
        return CheckResult(
            label, "skipped", f"{unit} declares no on-disk inputs",
            reason=REASON_NO_DECLARED_INPUTS,
        )
    resolved = _resolve_runtime(unit, label)
    if isinstance(resolved, CheckResult):
        return resolved
    uid, gids, user = resolved
    return _classify_readable_inputs(label, spec.paths, uid, gids, user)


@doctor_check(order=23.55, group="privsep")
def check_control_readable_inputs() -> CheckResult:
    """jasper-control must be able to read its runtime inputs: the CamillaDSP
    configs, the SSOT files it re-reads fresh, and the CSRF token."""
    return _check_daemon("jasper-control")


@doctor_check(order=23.56, group="privsep")
def check_web_readable_inputs() -> CheckResult:
    """jasper-web must be able to read the camilla configs + wizard SSOT/status
    files it renders. Skips on streambox, where jasper-web runs as root."""
    return _check_daemon("jasper-web")


@doctor_check(order=23.565, group="privsep")
def check_chat_web_readable_inputs() -> CheckResult:
    """jasper-chat-web must be able to read the conversation-history settings
    and SQLite store it renders and mutates."""
    return _check_daemon("jasper-chat-web")


@doctor_check(order=23.566, group="privsep")
def check_correction_web_readable_inputs() -> CheckResult:
    """jasper-correction-web must be able to read the graphs it applies, the
    commissioning stores /sound/ shares with it, and both spend ledgers."""
    return _check_daemon("jasper-correction-web")


@doctor_check(order=23.567, group="privsep")
def check_bluetooth_web_readable_inputs() -> CheckResult:
    """jasper-bluetooth-web must be able to read the device role map (an
    unreadable one is republished EMPTY) and the shared source intent."""
    return _check_daemon("jasper-bluetooth-web")


@doctor_check(order=23.568, group="privsep")
def check_system_web_readable_inputs() -> CheckResult:
    """jasper-system-web must be able to read the control token it embeds; the
    reader fails safe to gate-OFF, so unreadable = the CSRF gate silently off."""
    return _check_daemon("jasper-system-web")


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
    """The accessory bridge process must be able to read the published accessory
    mic sources — that file is what decides whether it runs the mic adapter."""
    return _check_daemon("jasper-input")


@doctor_check(order=23.592, group="privsep")
def check_usbmic_readable_inputs() -> CheckResult:
    """The USB mic relay must be able to read its explicit export intent."""
    return _check_daemon("jasper-usbmic")


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
        return CheckResult(
            label, "skipped", "absent — this speaker is not paired",
            reason=REASON_HOUSEHOLD_SECRET_ABSENT,
        )
    resolved = _resolve_runtime("jasper-control", label)
    if isinstance(resolved, CheckResult):
        return resolved
    uid, gids, user = resolved
    return _household_secret_verdict(st, uid, gids, user)
