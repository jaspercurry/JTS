"""Conventions guard: a wizard handler that restarts a daemon must emit `event=`.

A web-wizard POST handler that triggers a daemon restart is, by construction,
applying a config change — and every config change should leave a stable
`event=<wizard>.<action>` audit line, so `jasper-trace.sh` / `journalctl | grep
event=` show what changed, when, and from which device. This test enforces that
convention so a NEW wizard (a future DAC / mic / LLM-provider setup page) can't
silently ship a restart-without-audit handler — the gap that #561/#572/#574
closed by hand across the existing wizards, and that this guard itself caught in
`voice_setup` (`_handle_save`/`_handle_clear`/`_handle_spend_cap` restarted
jasper-voice with no audit line).

Detection is AST-based and deliberately COARSE: a `_handle_*` / `_post_*` method
that calls a known restart helper must also contain an audit-line call somewhere
in its body — either a hand-written `logger.{info,warning,debug}("event=...")`
or a call to the canonical emitter `log_event(logger, "<domain.action>", ...)`
(the `event=` prefix is added by the emitter, so the audited token is the 2nd
positional string arg, not a literal starting with "event="). It does NOT verify
the event name matches the action (not statically knowable) — only that the
handler audits *something*. The real bug is a restart with NO audit line at all.
A handler that legitimately restarts but isn't an audit-worthy config change
(content/preference, not connection identity) goes in `DELIBERATELY_UNLOGGED`
with a reason.

Intentional caveats (documented so they aren't mistaken for bugs):
  - Scope is `_handle_*`/`_post_*` methods. A wizard that restarts from a do_GET
    branch (e.g. an OAuth `/callback`) or a shared private helper (e.g. spotify's
    `_exchange_and_finish`) is out of scope — the dominant pattern is the named
    handler, and call-graph analysis would add fragility for little gain. Those
    paths ARE logged today; they're just not guarded here.
  - "Contains an `event=` line" is coarse: a handler whose only `event=` line is
    an incidental side-effect log passes. Accepted false-negative — the guard
    targets the zero-audit case.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "jasper" / "web"

# A call to any of these inside a handler means "this handler restarts a daemon."
RESTART_HELPERS = {
    "restart_voice_daemon",
    "_restart_voice_daemon",
    "_restart_spotify_consumers",
    "_restart_jasper_control",
    "restart_systemd_units",
    "_restart_units",
    "_restart_shairport",
}

# (file stem, method): restarts a daemon but is deliberately NOT audited, with
# the reason. These are content/preference changes, not connection-identity
# config — distinct from the credentials / link / unlink / default / save
# lifecycle that IS audited. The staleness test below enforces that each entry
# still exists, still restarts, and still has no audit line (so the allowlist
# can't silently rot).
DELIBERATELY_UNLOGGED: dict[tuple[str, str], str] = {
    ("spotify_setup", "_handle_playlist_add"):
        "saved-playlist content, not connection identity; frequent → journal noise",
    ("spotify_setup", "_handle_playlist_remove"):
        "saved-playlist content, not connection identity; frequent → journal noise",
}


def _calls_restart(fn: ast.FunctionDef) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name in RESTART_HELPERS:
                return True
    return False


def _emits_event_log(fn: ast.FunctionDef) -> bool:
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        # Hand-written audit line: logger.{info,warning,debug}("event=...").
        if (isinstance(f, ast.Attribute) and f.attr in ("info", "warning", "debug")):
            for arg in n.args:
                if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        and arg.value.startswith("event=")):
                    return True
        # Canonical emitter: log_event(logger, "<domain.action>", ...). The
        # `event=` prefix is added by the emitter, so the audited token here is
        # the 2nd positional arg (the event name string), not a literal that
        # starts with "event=". Accept both a bare `log_event(...)` and an
        # attribute form (e.g. `mod.log_event(...)`).
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name == "log_event" and len(n.args) >= 2:
            event_name = n.args[1]
            if (isinstance(event_name, ast.Constant)
                    and isinstance(event_name.value, str)):
                return True
    return False


def _restarting_handlers() -> "list[tuple[str, ast.FunctionDef]]":
    """Every `_handle_*`/`_post_*` method in a wizard that calls a restart helper."""
    out: list[tuple[str, ast.FunctionDef]] = []
    for path in sorted(WEB_DIR.glob("*_setup.py")):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if (isinstance(fn, ast.FunctionDef)
                    and (fn.name.startswith("_handle_")
                         or fn.name.startswith("_post_"))
                    and _calls_restart(fn)):
                out.append((path.stem, fn))
    return out


def test_restarting_wizard_handlers_emit_event_log():
    violations = []
    for stem, fn in _restarting_handlers():
        if (stem, fn.name) in DELIBERATELY_UNLOGGED:
            continue
        if not _emits_event_log(fn):
            violations.append(f"{stem}.{fn.name} (line {fn.lineno})")
    assert not violations, (
        "These wizard handlers restart a daemon but emit no `event=` audit log:\n  "
        + "\n  ".join(violations)
        + "\n\nA handler that restarts a daemon is applying a config change — add\n"
        '  log_event(logger, "<wizard>.<action>", client=self.address_string())\n'
        "after the restart (action + client IP only — never secrets, coordinates, "
        "or account names). If the handler is content/preference rather than "
        "connection config, add it to DELIBERATELY_UNLOGGED with a reason."
    )


def test_deliberately_unlogged_allowlist_is_not_stale():
    by_key = {(stem, fn.name): fn for stem, fn in _restarting_handlers()}
    for key, reason in DELIBERATELY_UNLOGGED.items():
        assert reason.strip(), f"{key} needs a documented reason"
        assert key in by_key, (
            f"{key[0]}.{key[1]} is in DELIBERATELY_UNLOGGED but is no longer a "
            "restart-calling handler (renamed/removed, or no longer restarts) — "
            "remove the stale allowlist entry."
        )
        assert not _emits_event_log(by_key[key]), (
            f"{key[0]}.{key[1]} now emits an event= log — remove it from "
            "DELIBERATELY_UNLOGGED (it's audited; no allowlist needed)."
        )
