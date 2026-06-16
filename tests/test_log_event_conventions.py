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
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JASPER = ROOT / "jasper"

# Logging-level methods. `warn` is the deprecated alias of `warning`; included
# so a stray `logger.warn("event=...")` is still caught.
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical"}
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
    "jasper/active_speaker/startup_load.py": {"*"},
    "jasper/web/sound_setup.py": {"*"},
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
    "jasper/web/sound_setup.py",
    "jasper/sound/",
    "jasper/output_topology.py",
)

# There is NO permanent exemption. A field whose name collides with a reserved
# parameter (chiefly `level`, the volume level) or isn't a valid identifier
# rides log_event's explicit `fields=` mapping (see jasper/log_event.py), so
# every event line can go through the canonical emitter. The allowlist is purely
# the active-zone deferrals above.
ALLOWLIST: dict[str, set[str]] = dict(DEFERRED_ACTIVE_ZONE)


def _literal_prefix(arg: ast.expr) -> str | None:
    """Literal leading text of a string/f-string arg, else None.

    A plain ``ast.Constant`` str returns its value. An f-string
    (``ast.JoinedStr``) returns the text of its first segment when that segment
    is a constant — enough to see an ``event=`` prefix, since the event name is
    always a literal in this codebase. Anything else (a name, a call) → None.
    """
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr) and arg.values:
        first = arg.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
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


def _violations_in(path: Path) -> list[tuple[int, str]]:
    """(lineno, event_name) for each hand-written event= logger call in path."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        arg = _message_arg(node)
        if arg is None:
            continue
        prefix = _literal_prefix(arg)
        if prefix is None or not prefix.startswith("event="):
            continue
        found.append((node.lineno, _event_name(prefix)))
    return found


def _all_violations() -> dict[str, list[tuple[int, str]]]:
    out: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(JASPER.rglob("*.py")):
        hits = _violations_in(path)
        if hits:
            out[str(path.relative_to(ROOT))] = hits
    return out


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
