# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conventions guard: operational ``event=`` lines go through ``jasper.log_event``.

JTS logs operational events as ``event=<domain>.<action> k=v k=v`` lines so
``jasper-trace.sh`` / ``journalctl | grep event=`` show what happened, when, and
from which surface. Historically every call site hand-rolled that f-string, and
**none of them escaped field values** — an SSID, USB descriptor, Bluetooth
device name, HA error body, or free-text reason that contains a space, ``=``, or
a quote silently corrupted the logfmt parse for anything reading the journal as
key=val. :mod:`jasper.log_event` is the one place that renders the line (logfmt
by default, JSON under ``JASPER_LOG_JSON``), byte-identical for clean values and
properly quoted/escaped for dirty ones.

This guard makes the canonical emitter the enforced default: a NEW (or surviving
hand-written) ``logger.<level>("event=...")`` call fails CI. The whole codebase
was migrated to ``log_event(logger, "<name>", ...)`` in the same change that
added this test. There is no permanent exemption — a field whose name collides
with a reserved parameter (chiefly ``level``) rides log_event's ``fields=``
mapping — so the allowlist below holds only files an in-flight work-stream owns
(active zones), deferred to avoid churning a parallel session's edits. Each is
pinned by a staleness check so an entry can't outlive its migration.

Detection is AST-based and deliberately precise: it flags a ``Call`` whose
function is an attribute access ending in a logging-level method
(``debug``/``info``/``warning``/``warn``/``error``/``exception``/``critical``)
whose first positional argument — or the generic ``<logger>.log(LEVEL, …)``
form, whose *second* argument (after the level) — is a string (a plain ``str``
constant or an f-string) whose literal text starts with ``event=``. That keys on
the ``event=`` *prefix in a logging call*, so docstring ``journalctl | grep
event=`` examples, ``# event=...`` comments, ``log_event(logger,
"domain.action")`` calls (which pass the bare name, no ``event=`` prefix), and
the emitter's own ``logger.log(level, message)`` (a *variable* message) are all
correctly ignored.

The same walk collects the **vocabulary** the three name checks below hold. It
stays test-only: ``log_event`` does no membership check at runtime, so no daemon
pays for a ~1,150-name table on a 415 MB Pi (ADR-0226) and no logging call can
raise on a wake-blocking path.
"""
from __future__ import annotations

import ast
import functools
import re
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

from tests.event_vocabulary import (
    CONSUMED_ELSEWHERE,
    FLAT_EVENT_NAMES,
    PREFIX_OWNERS,
)

ROOT = Path(__file__).resolve().parents[1]
JASPER = ROOT / "jasper"

# Logging-level methods. `warn` is the deprecated alias of `warning`; included
# so a stray `logger.warn("event=...")` is still caught.
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical"}
)

# `event=<name>` inside a rendered line. The charset is the vocabulary's own, so
# `event="…"` (a keyword argument), `event=${VAR}` and `event=[A-Za-z0-9_.:-]+`
# (an awk pattern) capture the empty name and are dropped.
_EVENT_IN_TEXT = re.compile(r"event=([a-z0-9_.]*)")

# `domain.action`: lower snake segments, at least one dot.
_SHAPE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*(?:\.[a-z0-9]+(?:_[a-z0-9]+)*)+$")

# Files that READ event names back: a doctor hint naming what to grep, a
# `/state` sampler scanning the journal. deploy/bin/ is not among them — its
# `event=` lines are its own shell emissions, and a shell-emitted name is
# outside a Python AST collector's reach either way.
_READER_PATHS = (
    "jasper/cli/doctor",
    "jasper/control",
    "scripts/journal-review.sh",
    "scripts/fetch-pi-logs.sh",
)

# Active-zone files an in-flight work-stream owns (the active-crossover / sound
# UI, the LLM tool surfaces). They are intentionally NOT migrated here so this
# change doesn't churn files a parallel session is editing — they fold into
# log_event when that work lands. Each maps to {"*"} (any event in the file is
# exempt). `_ACTIVE_ZONE_PREFIXES` below bounds this list: a deferral can only
# be an active-zone path, never an arbitrary "skip the migration here." The
# staleness test fails if a listed file no longer has a hand-written event=
# line (so a finished migration must drop its entry).
DEFERRED_ACTIVE_ZONE: dict[str, set[str]] = {
    "jasper/active_speaker/camilla_yaml.py": {"*"},
    "jasper/active_speaker/playback.py": {"*"},
    "jasper/active_speaker/staging.py": {"*"},
    "jasper/active_speaker/commission_load.py": {"*"},
    "jasper/active_speaker/startup_load.py": {"*"},
    "jasper/output_topology.py": {"*"},
    "jasper/sound/camilla_yaml.py": {"*"},
    "jasper/tools/__init__.py": {"*"},
    "jasper/tools/audio.py": {"*"},
    "jasper/tools/bus.py": {"*"},
    "jasper/tools/citibike.py": {"*"},
    "jasper/tools/diagnostic.py": {"*"},
    "jasper/tools/home_assistant.py": {"*"},
    "jasper/tools/packs.py": {"*"},
}

# An active-zone deferral's path must start with one of these — the tripwire
# that keeps DEFERRED_ACTIVE_ZONE from becoming a dumping ground for
# "migration skipped here." (output_topology.py and sound/camilla_yaml.py are
# active-crossover-adjacent backend; listed explicitly above.)
_ACTIVE_ZONE_PREFIXES = (
    "jasper/active_speaker/",
    "jasper/tools/",
    "jasper/sound/",
    "jasper/output_topology.py",
)

# There is NO permanent exemption. A field whose name collides with a reserved
# parameter (chiefly `level`, the volume level) or isn't a valid identifier
# rides log_event's explicit `fields=` mapping (see jasper/log_event.py), so
# every event line can go through the canonical emitter. The allowlist is purely
# the active-zone deferrals above.
ALLOWLIST: dict[str, set[str]] = dict(DEFERRED_ACTIVE_ZONE)


class _Event(NamedTuple):
    """One emitted event name.

    ``partial`` marks a name an f-string placeholder completes
    (``f"multiroom.reconcile.{key}_env_failed"`` → ``multiroom.reconcile.``):
    the literal head is all a static reader can know.
    """

    name: str
    partial: bool
    path: str
    lineno: int


class _Scan(NamedTuple):
    """One file's AST pass.

    ``emit_linenos`` holds every line that emits an event, literal name or not,
    so the reader scan can tell a grep-for-this-name from an emission.
    """

    violations: tuple[tuple[int, str], ...]
    events: tuple[_Event, ...]
    emit_linenos: frozenset[int]
    lines: tuple[str, ...]


def _literal_head(arg: ast.expr) -> tuple[str, bool] | None:
    """``(leading literal text, truncated)`` of a string/f-string arg, else None.

    A plain ``ast.Constant`` str returns its whole value, untruncated. An
    f-string (``ast.JoinedStr``) returns the text of its first segment when that
    segment is a constant, marked truncated — enough to see an ``event=`` prefix
    and, where the name itself is interpolated, its literal head. Anything else
    (a name, a call, a ``%`` format) → None.
    """
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value, False
    if isinstance(arg, ast.JoinedStr) and arg.values:
        first = arg.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value, True
    return None


def _event_name(prefix: str) -> str:
    """Extract the event name from an ``event=<name> ...`` literal prefix."""
    after = prefix[len("event="):]
    return after.split()[0] if after.split() else ""


def _message_arg(node: ast.Call) -> ast.expr | None:
    """The message-string arg of a logging call, or None if not a logging call.

    Covers `<obj>.{debug,info,warning,…}("event=…")` (message is the 1st arg)
    and the generic `<obj>.log(LEVEL, "event=…")` (message is the 2nd arg, after
    the level). log_event's own internal `logger.log(level, message)` passes a
    *variable* message, so it never trips the literal-prefix check below.
    """
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr in _LOG_METHODS and node.args:
        return node.args[0]
    if func.attr == "log" and len(node.args) >= 2:
        return node.args[1]
    return None


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return path.name


def _named_event(arg: ast.expr, rel_path: str) -> list[_Event]:
    """The event an argument that IS the name carries (log_event's name, `event=`)."""
    head = _literal_head(arg)
    if head is None:
        return []
    return [_Event(head[0], head[1], rel_path, arg.lineno)]


def _rendered_events(arg: ast.expr, rel_path: str) -> list[_Event]:
    """The events an argument that RENDERS a whole line carries (`print`, `.write`)."""
    head = _literal_head(arg)
    if head is None:
        return []
    text, truncated = head
    return [
        _Event(
            match.group(1),
            truncated and match.end() == len(text),
            rel_path,
            arg.lineno,
        )
        for match in _EVENT_IN_TEXT.finditer(text)
    ]


def _scan(path: Path) -> _Scan:
    """Walk one file once: hand-written event= violations AND the vocabulary.

    Five emission forms reach the vocabulary: ``log_event(logger, "<name>")``;
    a literal ``event=``/``*_event=`` keyword argument to any call (the helpers
    that emit on their caller's behalf); ``print("event=…")``;
    ``<stream>.write("… event=… ")`` (the flight recorder's dumps); and the
    hand-written ``logger.<level>("event=…")`` lines the allowlist still defers.
    A name passed as a Name or attribute instead of a literal (a module constant
    such as ``EVENT_FIT_FAILED_JOURNAL_DROPPED``) is not collected: the
    vocabulary is the set of *literal* names.
    """
    rel_path = _rel(path)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: list[tuple[int, str]] = []
    events: list[_Event] = []
    emit_linenos: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = (
            func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        )
        if called == "log_event" and len(node.args) >= 2:
            emit_linenos.update({node.lineno, node.args[1].lineno})
            events += _named_event(node.args[1], rel_path)
        for keyword in node.keywords:
            if keyword.arg and (
                keyword.arg == "event" or keyword.arg.endswith("_event")
            ):
                emit_linenos.update({node.lineno, keyword.value.lineno})
                events += _named_event(keyword.value, rel_path)
        message = _message_arg(node)
        if message is not None:
            head = _literal_head(message)
            if head is not None and head[0].startswith("event="):
                violations.append((node.lineno, _event_name(head[0])))
                emit_linenos.update({node.lineno, message.lineno})
                events += _rendered_events(message, rel_path)
        if called in {"print", "write"} and node.args:
            head = _literal_head(node.args[0])
            if head is not None and "event=" in head[0]:
                emit_linenos.update({node.lineno, node.args[0].lineno})
                events += _rendered_events(node.args[0], rel_path)
    lines = tuple(source.splitlines())
    return _Scan(tuple(violations), tuple(events), frozenset(emit_linenos), lines)


@functools.lru_cache(maxsize=1)
def _tree_scan() -> dict[str, _Scan]:
    """Every ``jasper/`` module, scanned once per session."""
    return {_rel(path): _scan(path) for path in sorted(JASPER.rglob("*.py"))}


def _violations_in(path: Path) -> list[tuple[int, str]]:
    """(lineno, event_name) for each hand-written event= logger call in path."""
    scan = _tree_scan().get(_rel(path)) or _scan(path)
    return list(scan.violations)


def _all_violations() -> dict[str, list[tuple[int, str]]]:
    return {
        rel_path: list(scan.violations)
        for rel_path, scan in _tree_scan().items()
        if scan.violations
    }


def _events() -> tuple[_Event, ...]:
    return tuple(event for scan in _tree_scan().values() for event in scan.events)


@functools.lru_cache(maxsize=1)
def _consumed() -> dict[str, tuple[str, ...]]:
    """``{event name: where a reader names it}``, minus the readers' own emissions."""
    found: dict[str, list[str]] = defaultdict(list)
    for entry in _READER_PATHS:
        root = ROOT / entry
        for file in sorted(root.rglob("*.py")) if root.is_dir() else [root]:
            rel_path = _rel(file)
            scan = _tree_scan().get(rel_path)
            emitted_at = scan.emit_linenos if scan else frozenset()
            # The .sh readers are the only ones the tree scan has not read.
            lines = (
                scan.lines
                if scan
                else tuple(file.read_text(encoding="utf-8").splitlines())
            )
            for lineno, line in enumerate(lines, 1):
                if lineno in emitted_at:
                    continue
                for match in _EVENT_IN_TEXT.finditer(line):
                    # A trailing dot is prose ("… see event=fanin.ring.opened.")
                    # or a glob stem ("event=multiroom.reconcile.*"); either way
                    # the reference is to that family.
                    name = match.group(1).rstrip(".")
                    if name:
                        found[name].append(f"{rel_path}:{lineno}")
    return {name: tuple(where) for name, where in found.items()}


def _is_allowed(rel_path: str, event_name: str) -> bool:
    exempt = ALLOWLIST.get(rel_path)
    if exempt is None:
        return False
    return "*" in exempt or event_name in exempt


def test_no_unmigrated_event_logger_calls():
    """Every operational event= line must go through jasper.log_event.log_event."""
    offending: list[str] = []
    for rel_path, hits in _all_violations().items():
        for lineno, name in hits:
            if not _is_allowed(rel_path, name):
                offending.append(f"{rel_path}:{lineno}  event={name or '<dynamic>'}")
    assert not offending, (
        "Hand-written `event=` logger call(s) found — use "
        "`log_event(logger, \"<domain.action>\", k=v, ...)` from "
        "jasper.log_event instead (it escapes untrusted field values). "
        "If a site genuinely cannot migrate, add it to ALLOWLIST in this "
        "test with a reason:\n  " + "\n  ".join(offending)
    )


def test_allowlist_is_not_stale():
    """Each ALLOWLIST entry must still have a matching hand-written event= line.

    Prevents the allowlist from outliving its reason: once a file (or its
    specific exempt event) is migrated, its entry must be removed.
    """
    violations = _all_violations()
    stale: list[str] = []
    for rel_path, exempt in ALLOWLIST.items():
        hits = violations.get(rel_path, [])
        if "*" in exempt:
            if not hits:
                stale.append(
                    f"{rel_path}: allowlisted '*' but no hand-written event= "
                    "logger line remains — remove this entry"
                )
            continue
        present = {name for _, name in hits}
        for name in exempt:
            if name not in present:
                stale.append(
                    f"{rel_path}: allowlisted event '{name}' is gone "
                    "(migrated?) — remove it from ALLOWLIST"
                )
    assert not stale, "Stale ALLOWLIST entries:\n  " + "\n  ".join(stale)


def test_deferred_entries_are_active_zone_only():
    """Tripwire: a deferral can only be an active-zone path, never an arbitrary
    "migration skipped here." Every other event line — including one with a
    field whose name collides with a reserved param — must use log_event
    (passing the colliding field via `fields=`)."""
    misplaced = [
        path
        for path in DEFERRED_ACTIVE_ZONE
        if not path.startswith(_ACTIVE_ZONE_PREFIXES)
    ]
    assert not misplaced, (
        "DEFERRED_ACTIVE_ZONE may only hold active-zone paths "
        f"(prefixes {_ACTIVE_ZONE_PREFIXES}); these are not: {misplaced}. "
        "A non-active-zone file should be migrated to log_event, not deferred."
    )


def test_sound_setup_migration_has_no_exemption_or_backdoor_prefix():
    """The completed sound-page migration stays enforced without a re-deferral."""
    rel_path = "jasper/web/sound_setup.py"

    assert _violations_in(ROOT / rel_path) == []
    assert rel_path not in DEFERRED_ACTIVE_ZONE
    assert rel_path not in ALLOWLIST
    assert not any(rel_path.startswith(prefix) for prefix in _ACTIVE_ZONE_PREFIXES)


def test_event_names_are_domain_action():
    """Every emitted name is `domain.action`, bar the frozen flat set.

    A partial name is checked with a placeholder segment appended, so
    `multiroom.reconcile.` passes and `Ramp.Locked.` would not.
    """
    offending = sorted(
        f"{event.path}:{event.lineno}  {event.name}"
        for event in _events()
        if not _SHAPE.match(f"{event.name}x" if event.partial else event.name)
        and event.name not in FLAT_EVENT_NAMES
    )
    assert not offending, (
        "Event name(s) outside the `domain.action` vocabulary (lower snake "
        "segments, at least one dot):\n  " + "\n  ".join(offending)
    )


def test_flat_event_names_only_shrink():
    """FLAT_EVENT_NAMES is worked off, never added to: every entry is still a
    flat name that some site still emits."""
    emitted = {event.name for event in _events() if not event.partial}
    stale = sorted(name for name in FLAT_EVENT_NAMES if name not in emitted)
    dotted = sorted(name for name in FLAT_EVENT_NAMES if "." in name)
    assert not stale, (
        "FLAT_EVENT_NAMES entries nothing emits any more (renamed? deleted?) — "
        "drop them, the list only shrinks:\n  " + "\n  ".join(stale)
    )
    assert not dotted, (
        "FLAT_EVENT_NAMES holds flat names only; these conform already and "
        f"must be dropped: {dotted}"
    )


def test_event_prefixes_stay_within_their_recorded_packages():
    """A top-level prefix may not reach a package PREFIX_OWNERS does not record,
    and a recorded package that stopped emitting it must be dropped."""
    owners: dict[str, set[str]] = defaultdict(set)
    for event in _events():
        if "." in event.name or event.partial:
            # jasper/<pkg>/… is owned by <pkg>; a top-level module by `jasper`.
            parts = event.path.split("/")
            owners[event.name.split(".")[0]].add(
                parts[1] if len(parts) > 2 else "jasper"
            )
    widened = sorted(
        f"{prefix}: now {sorted(packages)}, "
        f"recorded {list(PREFIX_OWNERS.get(prefix, ()))}"
        for prefix, packages in owners.items()
        if len(packages) > 1 and not packages <= set(PREFIX_OWNERS.get(prefix, ()))
    )
    stale = sorted(
        f"{prefix}: recorded {list(recorded)}, no longer emitted from "
        f"{sorted(set(recorded) - owners.get(prefix, set()))}"
        for prefix, recorded in PREFIX_OWNERS.items()
        if not set(recorded) <= owners.get(prefix, set())
    )
    assert not widened, (
        "An event prefix reached a new package — give the family one owner, or "
        "record the spread in PREFIX_OWNERS:\n  " + "\n  ".join(widened)
    )
    assert not stale, (
        "PREFIX_OWNERS is stale — trim these entries:\n  " + "\n  ".join(stale)
    )


def test_consumed_event_names_are_emitted():
    """Every name a reader greps for is still emitted, so a producer rename that
    breaks a doctor hint or /state's journal scan fails here first."""
    emitted = {event.name for event in _events() if not event.partial}
    families = {event.name for event in _events() if event.partial}

    def is_emitted(name: str) -> bool:
        return (
            name in emitted
            or any(other.startswith(f"{name}.") for other in emitted)
            or any(family.startswith(name) for family in families)
        )

    missing = sorted(
        f"{name}  (read at {', '.join(where)})"
        for name, where in _consumed().items()
        if not is_emitted(name) and name not in CONSUMED_ELSEWHERE
    )
    resolved = sorted(name for name in CONSUMED_ELSEWHERE if is_emitted(name))
    unread = sorted(name for name in CONSUMED_ELSEWHERE if name not in _consumed())
    assert not missing, (
        "A reader names an event nothing in jasper/ emits — restore the name or "
        "fix the reader:\n  " + "\n  ".join(missing)
    )
    assert not resolved and not unread, (
        "CONSUMED_ELSEWHERE is stale — drop entries now emitted from jasper/ "
        f"{resolved} or no longer read {unread}"
    )


def test_detector_catches_both_logging_forms(tmp_path):
    """The detector flags `logger.<level>("event=…")` AND the generic
    `logger.log(LEVEL, "event=…")` form, while ignoring a variable message
    (the emitter's own internal call) and a canonical `log_event(…)` call."""
    src = (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        'logger.info("event=demo.level_method k=v")\n'
        'logger.log(logging.WARNING, "event=demo.log_form k=v")\n'
        "logger.log(logging.INFO, _rendered)\n"  # variable message -> ignored
        'log_event(logger, "demo.canonical")\n'  # bare name, no prefix -> ignored
    )
    snippet = tmp_path / "snippet.py"
    snippet.write_text(src)
    names = sorted(name for _, name in _violations_in(snippet))
    assert names == ["demo.level_method", "demo.log_form"]


def test_collector_sees_every_emission_form(tmp_path):
    """All five emission forms reach the vocabulary, and a name an f-string
    completes is collected as the literal head it can be known by."""
    src = (
        'log_event(logger, "demo.canonical")\n'
        'log_event(logger, f"demo.family.{suffix}")\n'
        'helper(unit, refusal_event="demo.kwarg")\n'
        'print(f"event=demo.printed path={path}")\n'
        'print(f"event=demo.split_{suffix}")\n'
        'stream.write(f"flightrec event=demo.written n={count}\\n")\n'
        'logger.warning("event=demo.raw k=v")\n'
        'logger.info("nothing to see here")\n'
    )
    snippet = tmp_path / "snippet.py"
    snippet.write_text(src)
    assert sorted((e.name, e.partial) for e in _scan(snippet).events) == [
        ("demo.canonical", False),
        ("demo.family.", True),
        ("demo.kwarg", False),
        ("demo.printed", False),
        ("demo.raw", False),
        ("demo.split_", True),
        ("demo.written", False),
    ]
