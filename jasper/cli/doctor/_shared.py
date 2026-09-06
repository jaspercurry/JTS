# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared primitives for the jasper-doctor check package.

The base layer every per-domain check module imports from: the output
contract re-exported from :mod:`jasper.doctor_contract`, the
crash-isolation harness (so one crashing check cannot abort the run),
the subprocess/env-file wrappers, the ANSI colour constants, and the
helpers and ``REASON_*`` codes more than one domain uses — each declared
beside the helper it belongs to.

Not here: the daemon ``STATUS`` control-socket reader, which is
:mod:`jasper.route_latency.status_socket` reached through
:mod:`jasper.cli.doctor._evidence`, and the per-run evidence cache itself.

``_run`` is re-imported into the domain modules that call it, so a check
resolves it in its OWN namespace and a test patch must target that module,
not this one."""
from __future__ import annotations

import grp
import hashlib
import os
import re
import shlex
import stat as _stat
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Awaitable, Callable
from ...doctor_contract import (  # noqa: F401 — re-exported for the domain modules
    CHECK_STATUSES,
    CheckResult,
    REASON_CHECK_CRASHED,
    REASON_CHECK_TIMED_OUT,
    REASON_CONFIG_ERROR,
    REASON_DOCTOR_CRASHED,
    REASON_NOT_INSTALLED,
    check_row,
    summarize,
)
from ...install_profile import is_streambox_install_profile, read_install_profile
from ...secret_redaction import redact_secrets

GREEN = "\033[32m"

RED = "\033[31m"

YELLOW = "\033[33m"

BOLD = "\033[1m"

DIM = "\033[2m"

RESET = "\033[0m"

_CHIP_AEC_PASSIVE_REQUIRED_CHECKS = frozenset({
    "runtime_profile",
    "mic_detected",
    "runtime_env",
    "service_state",
    "dac_reference",
    "wake_legs",
    "outputd_reference_health",
    "bridge_counter_window",
    "chip_profile_readback",
    "chip_convergence",
})

# The row contract lives in `jasper.doctor_contract` (stdlib-only, so
# jasper-control can build contract rows without importing this package).
DoctorCheck = Callable[[], CheckResult] | tuple[str, Callable[[], CheckResult]]

_EXCEPTION_DETAIL_LIMIT = 240

def _exception_detail(exc: BaseException, *, literals: Iterable[str] = ()) -> str:
    """Redact + cap an exception's message for a doctor row.

    ``literals`` are secret values the caller holds (e.g. a probed
    credential) that may appear in the exception text in a shape
    ``redact_secrets``'s patterns don't recognise; empty values are
    skipped by ``redact_secrets`` itself.
    """
    message = redact_secrets(str(exc), literals=literals)
    if len(message) > _EXCEPTION_DETAIL_LIMIT:
        message = message[: _EXCEPTION_DETAIL_LIMIT - 3] + "..."
    if not message:
        return type(exc).__name__
    return f"{type(exc).__name__}: {message}"

def _crashed_check_result(name: str, exc: BaseException) -> CheckResult:
    return CheckResult(
        name,
        "fail",
        f"check crashed: {_exception_detail(exc)}",
        reason=REASON_CHECK_CRASHED,
    )

def _check_name(check: Callable[[], CheckResult]) -> str:
    name = getattr(check, "__name__", "doctor check")
    if name == "<lambda>":
        return "doctor check"
    if name.startswith("check_"):
        name = name[len("check_"):]
    return name.replace("_", " ")

def _normalize_doctor_check(
    entry: DoctorCheck,
) -> tuple[str, Callable[[], CheckResult]]:
    if isinstance(entry, tuple):
        return entry
    return _check_name(entry), entry

def _run_doctor_check(entry: DoctorCheck) -> CheckResult:
    name, check = _normalize_doctor_check(entry)
    try:
        return check()
    except Exception as e:  # noqa: BLE001
        return _crashed_check_result(name, e)

async def _run_async_doctor_check(
    name: str,
    check: Callable[[], Awaitable[CheckResult]],
) -> CheckResult:
    try:
        return await check()
    except Exception as e:  # noqa: BLE001
        return _crashed_check_result(name, e)

# systemd absent (a dev laptop, a container): nothing was observed, so the
# callers of the systemctl helpers report `skipped` with this.
REASON_SYSTEMCTL_UNAVAILABLE = "systemctl_unavailable"

# The saved output topology is torn/unreadable — both the audio-domain
# output-hardware-match check and the active-speaker runtime-graph check
# report this same reason for the same underlying evidence failure.
REASON_TOPOLOGY_UNREADABLE = "output_topology_unreadable"

# ADR-0217 streambox-awaiting-accessory state, shared by resilience.py and voice.py.
REASON_VOICE_UNIT_NOT_FULL_PROFILE = "voice_unit_not_full_profile"

def _run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

def _parse_systemd_environment(text: str) -> dict[str, str]:
    """Parse ``systemctl show -p Environment`` output into key/value pairs."""
    text = text.strip()
    if text.startswith("Environment="):
        text = text[len("Environment="):]
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    env: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        env[key] = value.strip().strip('"').strip("'")
    return env


def _camilla_block_field(text: str, block: str, key: str) -> str | None:
    """The FIRST value of ``key`` inside the top-level ``block:`` of a
    CamillaDSP config text (comment + surrounding quotes stripped), or None
    when block or key is absent.

    The value is ``""`` for a key whose value is a nested block (a mixer name
    like ``channel_select:``), so ``... is not None`` is the presence test.

    A deliberately fail-soft line scan that never raises, unlike
    ``yaml.safe_load`` on a malformed config — the doctor must stay total.
    Block-scoped, but not depth-scoped: use it only for keys unambiguous at
    any depth within their block. Depth-sensitive safety fields such as
    ``devices.volume_limit`` use
    :func:`jasper.camilla_config_contract.parse_camilla_devices_config`."""
    in_block = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not raw.startswith((" ", "\t")):
            in_block = stripped == f"{block}:"
            continue
        if not in_block:
            continue
        match = re.match(rf"^\s+{re.escape(key)}:\s*([^#]*)", raw)
        if match:
            return match.group(1).strip().strip("'\"")
    return None

def _group_writable_dir(
    st: os.stat_result, *, expected_group: str, require_setgid: bool = True
) -> tuple[bool, str]:
    """Whether a non-root process in group ``expected_group`` could create or
    replace entries in the directory behind stat result ``st``.

    Three conditions, not one:

    - group-owned by ``expected_group``;
    - the group WRITE *and* SEARCH/execute bits (``0o030``) — POSIX needs
      search on a directory, not just write, to add or remove an entry;
    - (when ``require_setgid``) the setgid bit, so a subdirectory a ROOT-run
      process later creates here inherits ``expected_group`` instead of
      landing group-root and locking the non-root arm out. Pass
      ``require_setgid=False`` for a dir whose every writer already declares
      its own ``Group=``, where inheritance is not how it gets its group.

    Returns ``(writable, resolved_group_name)``, the name falling back to the
    numeric gid when it has no group-database entry.

    ``os.access(path, os.W_OK)`` cannot answer this: the callers run as root,
    and ``os.access`` reports every path writable to the *caller* whatever
    its mode.
    """
    try:
        group_name = grp.getgrgid(st.st_gid).gr_name
    except (KeyError, OSError):
        group_name = str(st.st_gid)
    writable = group_name == expected_group and (st.st_mode & 0o030) == 0o030
    if require_setgid:
        writable = writable and bool(st.st_mode & _stat.S_ISGID)
    return writable, group_name

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()

def install_profile_is_streambox() -> bool:
    """True on the streambox tier. Fails toward False so an unparseable
    marker keeps the louder full-speaker check running rather than silently
    skipping it."""
    try:
        return is_streambox_install_profile(read_install_profile())
    except (TypeError, ValueError, OSError):
        return False


# The household's per-source intent file is unreadable or malformed, so
# nothing downstream of it can be judged.
REASON_SOURCE_INTENT_INVALID = "source_intent_invalid"

REASON_PARKED_BONDED_FOLLOWER = "parked_bonded_follower"


def _parked_as_bonded_follower() -> bool:
    """True when this speaker is an ACTIVE bonded multiroom FOLLOWER.

    The dumb-follower profile parks the renderer/source stack while bonded, so
    liveness checks must read that as ok rather than as a failure against
    intended state. Fail-open to NOT-parked: a broken read must never mask a
    real failure on a solo speaker."""
    try:
        from ...multiroom.effective_role import (
            effective_local_sources_park_reason,
        )
        from ._evidence import evidence

        cfg = evidence.grouping_config()
        return effective_local_sources_park_reason(cfg) is not None
    except Exception:  # noqa: BLE001 — fail-open
        return False


def _service_state_failure(
    label: str,
    unit: str,
    *,
    missing: str,
    not_enabled: str,
    inactive: str,
) -> CheckResult | None:
    """The systemd verdict for a MANDATORY audio-path unit: the actionable
    failure, or ``None`` when it is installed, enabled and active.

    One ladder for jasper-fanin, jasper-camilla and jasper-outputd — each
    passes its own three reason codes. All three units carry an ``[Install]``
    section, so anything other than ``enabled``/``enabled-runtime`` (including
    ``static``, ``disabled``, ``indirect``, ``masked``) means the unit will not
    come up on its own. `journalctl -u <unit>` is the next step for every
    caller, so the detail says so rather than repeating a per-unit sentence.
    Every fail arm is ``speaker_silent``: all three are mandatory audio-path units."""
    from ._evidence import evidence

    state = evidence.unit_state(unit)
    if state is None:
        return CheckResult(
            label, "skipped",
            "systemctl unavailable — skipped (not Linux?)",
            reason=REASON_SYSTEMCTL_UNAVAILABLE,
        )
    if state.get("load_state") == "not-found":
        return CheckResult(
            label, "fail",
            f"{unit} is not installed. Re-run install.sh.",
            reason=missing, speaker_silent=True,
        )
    enabled = state.get("unit_file_state")
    if enabled not in ("enabled", "enabled-runtime"):
        return CheckResult(
            label, "fail",
            f"{unit} is {enabled or 'unknown'}; it is mandatory. Run: "
            f"sudo systemctl enable --now {unit}",
            reason=not_enabled, speaker_silent=True,
        )
    active = state.get("active_state")
    if active != "active":
        return CheckResult(
            label, "fail",
            f"{unit} is enabled but state={active or 'unknown'}. "
            f"Check: journalctl -u {unit}",
            reason=inactive, speaker_silent=True,
        )
    return None


def _parked_follower_result(label: str) -> CheckResult | None:
    """The `ok` row a liveness check returns while this speaker is parked as a
    bonded follower, or None so the caller falls through to its real probe.
    The parked state is intended and was observed, so `ok` with a reason
    rather than a warn or a skip."""
    from ._evidence import evidence

    if not evidence.parked_bonded_follower():
        return None
    return CheckResult(
        label, "ok",
        "parked (bonded follower) — the dumb-follower profile stops this "
        "while paired; the pair leader owns playback + the mic",
        reason=REASON_PARKED_BONDED_FOLLOWER,
    )


# NO audio-path unit: `_service_state_failure` (jasper-fanin, jasper-camilla)
# and resilience.check_outputd_failure_reconcile_park (jasper-outputd, park
# record and all) own their runtime state, so one down unit is one fail row.
_RUNTIME_STATE_UNITS = (
    "nginx.service",
    "jasper-voice.service",
    "jasper-aec-bridge.service",
    "jasper-control.service",
    "jasper-input.service",
    # A .path unit fails on a bad spec; resilience.check_required_units_active
    # defers every non-`inactive` state to this row, so both must track it.
    "jasper-accessory-reconcile.path",
    "jasper-mux.service",
    "nqptp.service",
    "shairport-sync.service",
    "librespot.service",
    "bluealsa.service",
    "bluealsa-aplay.service",
    "bt-agent.service",
    # A failed coupling-reconcile pass parks the unit in `failed` with the
    # evidence only in `systemctl --failed` + the journal; tracking it here
    # makes that doctor-visible.
    "jasper-fanin-coupling-auto.service",
)

# Type=oneshot members of the tracked set: `activating` is their NORMAL
# in-flight state, not the stuck-start instability it signals on the
# long-running daemons above, so only a `failed` end-state flags them. A
# wedged oneshot is still caught — its TimeoutStartSec (120 s on
# jasper-fanin-coupling-auto) moves it to `failed`.
_ONESHOT_RUNTIME_STATE_UNITS = frozenset({
    "jasper-fanin-coupling-auto.service",
})

def _loopback_playback_active() -> bool:
    """True if any renderer is currently writing the music-chain loopback.

    Reads `/proc/asound/Loopback/pcm0p/sub*/status`: an open subdevice prints
    `state: …\\nowner_pid: …`, a closed one the single word `closed`. Only
    input lanes 0..4 count as "music active" — substream 7 is jasper-fanin's
    own summed output and may be open while every renderer is idle.

    Gates the AEC bridge FAIL: ref-silent windows only diagnose a broken
    reference chain while music is actually routed through the loopback.

    Blind spots — False means "no snd-aloop renderer lane is open", NOT
    "nothing is playing": USB Audio Input is DIRECT-captured by jasper-fanin
    from hw:UAC2Gadget, and a lane armed for ring ingress writes a
    per-renderer SHM ring instead of its aloop substream. A caller needing
    true output silence must consult those too.

    Delete once no renderer lane can use snd-aloop any more (#2285),
    together with its callers' music-active gates.
    """
    from ._evidence import evidence

    def first_line(text: str) -> str:
        return text.splitlines()[0].strip() if text else ""

    return any(
        index <= 4 and first_line(status) not in ("", "closed")
        for index, status in evidence.loopback_substreams().items()
    )
