# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The journal is redacted by construction (:mod:`jasper.logging_setup`).

These tests drive real records through the real root handler rather than
through caplog, whose own handler is not the one under test — and whose
presence on the root logger would make ``basicConfig`` a no-op, so each test
takes the root logger away from pytest for the duration.
"""
from __future__ import annotations

import ast
import io
import logging
from pathlib import Path

import pytest

import jasper.logging_setup as logging_setup
from jasper.logging_setup import configure_logging
from tests.conftest import bare_root_logger

_REPO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "template, secret",
    [
        ("provider ready OPENAI_API_KEY=%s loaded", "sk-live-abc123456789"),
        ("GET /v1 Authorization: Bearer %s", "eyJhbGciOiJIUzI1NiJ9.body.sig"),
        ('body={"access_token": "%s"}', "ya29.aBcDeFgHiJkLmNo"),
        ("google key %s in use", "AIzaSyD-9aBcDeFgHiJkLmNoPq"),
    ],
)
@pytest.mark.parametrize("lazy_arg", [False, True])
def test_a_secret_never_reaches_the_stream(capsys, template, secret, lazy_arg):
    """Both record shapes redact: a pre-built message, and ``msg % args``.

    The args path is its own case — the filter reads ``getMessage()``, so a
    secret passed as a lazy log argument is only covered because the
    interpolation happens before the patterns run.
    """
    with bare_root_logger():
        configure_logging()
        logger = logging.getLogger("jasper.x")
        if lazy_arg:
            logger.warning(template, secret)
        else:
            logger.warning(template % secret)

    err = capsys.readouterr().err
    assert secret not in err
    assert "<redacted>" in err


def test_a_traceback_is_redacted_too(capsys):
    """``logger.exception`` renders the exception's own text, which is where
    a provider's rejected-credential message lands."""
    with bare_root_logger():
        configure_logging()
        try:
            raise RuntimeError("upstream rejected token=abc123secretvalue")
        except RuntimeError:
            logging.getLogger("jasper.x").exception("provider call failed")

    err = capsys.readouterr().err
    assert "abc123secretvalue" not in err
    assert "RuntimeError" in err  # the traceback still arrives
    assert "<redacted>" in err


def test_a_scrub_failure_fails_closed_for_the_traceback_too(monkeypatch, capsys):
    """Clearing ``exc_text`` alone is not fail-closed: ``Formatter.format``
    re-derives it from an untouched ``exc_info``, so the placeholder path
    must clear ``exc_info`` too or the exception's own text — exactly where
    a provider's rejected credential lands — reaches the stream raw."""
    monkeypatch.setattr(
        logging_setup,
        "redact_secrets",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("redactor blew up")),
    )
    with bare_root_logger():
        configure_logging()
        try:
            raise RuntimeError("upstream rejected token=abc123secretvalue")
        except RuntimeError:
            logging.getLogger("jasper.x").exception("provider call failed")

    err = capsys.readouterr().err
    assert "could not be scrubbed" in err
    assert "abc123secretvalue" not in err
    assert "Traceback" not in err


def test_a_logger_outside_jasper_is_redacted_too(capsys):
    """The filter is on the handler, not on the ``jasper`` logger.

    An httpx/urllib3 record carrying a query-string key lands in the same
    journal and has to be scrubbed by the same pass; moving the filter onto
    the ``jasper`` logger would silently stop covering it.
    """
    with bare_root_logger():
        configure_logging()
        logging.getLogger("httpx").warning(
            "GET https://api.example.com/v1?key=abcdef0123456789 -> 200"
        )

    err = capsys.readouterr().err
    assert "abcdef0123456789" not in err
    assert "<redacted>" in err


def test_a_record_is_redacted_once(monkeypatch):
    """One pass per record, not one per filtered handler.

    A ``jasper.*`` record reaches both the journal handler and the flight
    recorder's ring. ``redact_secrets`` is the whole per-line cost of this
    PR on a Pi Zero 2 W, so the second filtered handler must be free.
    """
    passes = []
    real = logging_setup.redact_secrets
    monkeypatch.setattr(
        logging_setup,
        "redact_secrets",
        lambda text, *a, **kw: (passes.append(text), real(text, *a, **kw))[1],
    )
    with bare_root_logger():
        configure_logging()
        ring = logging.StreamHandler(io.StringIO())
        ring.addFilter(logging_setup.REDACTING_FILTER)
        logging.getLogger("jasper").addHandler(ring)
        logging.getLogger("jasper.x").warning("OPENAI_API_KEY=sk-live-abc123456789")

    assert len(passes) == 1


# ------------------------------------------------------------------- ratchet

# The parked tuning zone (#4193 lane brief): these keep their own
# `basicConfig` because no file in the measurement/tuning program is edited
# without an owner-ticked row, and `cli/measure.py` is frozen until #4138
# merges. Their journals are NOT redacted yet — a listed file is a known
# gap, not an endorsement.
# Removal condition: adopt each of these when the tuning zone reopens
# (#3769 wave 10) and #4138 has merged, emptying this set.
_ALLOWLIST = frozenset({
    "jasper/cli/active_speaker_emit_bench.py",
    "jasper/cli/angle_capture.py",
    "jasper/cli/audition.py",
    "jasper/cli/crossover_prescriber.py",
    "jasper/cli/measure.py",
    "jasper/cli/seat_level.py",
    "jasper/web/correction_setup.py",
})


# The stdlib calls that put a handler where a jasper record reaches it.
_BOOTSTRAP_CALLS = frozenset({"basicConfig", "dictConfig", "fileConfig"})


def _names_the_root_logger(node: ast.expr) -> bool:
    """``logging.root``, a bare ``logging.getLogger()``, or
    ``logging.getLogger("")``: an ``addHandler`` on any of these sits under
    every jasper logger, unfiltered."""
    if isinstance(node, ast.Attribute) and node.attr == "root":
        return True
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getLogger"
        and not node.keywords
    ):
        return False
    if not node.args:
        return True
    return (
        len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == ""
    )


def _installs_its_own_handler(tree: ast.AST) -> bool:
    """One walk: attribute calls are matched by name (so ``import logging as
    L`` is covered), bare calls against what a ``from logging`` import bound
    (so an alias is covered too), and an ``addHandler`` on a name bound
    earlier in the module to the root logger (so ``root =
    logging.getLogger(); root.addHandler(...)`` is covered like the inline
    spelling).

    Out of scope: a dynamic lookup (``getattr(logging, "basicConfig")()``)
    and a root logger reached through something other than a direct name
    binding — a function's return value, an attribute, a container element —
    which no shape-based scan resolves.
    """
    bootstrap_names: set[str] = set()
    root_names: set[str] = set()
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "logging":
                bootstrap_names |= {
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name in _BOOTSTRAP_CALLS
                }
        elif isinstance(node, ast.Assign) and _names_the_root_logger(node.value):
            root_names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called_names.add(func.id)
            elif isinstance(func, ast.Attribute) and (
                func.attr in _BOOTSTRAP_CALLS
                or (
                    func.attr == "addHandler"
                    and (
                        _names_the_root_logger(func.value)
                        or (
                            isinstance(func.value, ast.Name)
                            and func.value.id in root_names
                        )
                    )
                )
            ):
                return True
    return bool(bootstrap_names & called_names)


def test_configure_logging_is_the_only_logging_bootstrap():
    """No process may reach the journal around the redacting filter.

    Covers the four ways a module installs a handler for itself:
    ``basicConfig``, ``logging.config.dictConfig``/``fileConfig``, and an
    ``addHandler`` on the root logger, including one reached through a local
    name bound earlier in the module. See ``_installs_its_own_handler`` for
    what is still out of scope.

    Exact-match, so the allowlist cannot go stale either: a parked file that
    adopts must leave the set in the same commit. Remove this ratchet when
    ``logging.basicConfig`` stops being how the tree installs its journal
    handler (a systemd ``JournalHandler``, say): the scanned shape would no
    longer be the bypass.
    """
    offenders = {
        path.relative_to(_REPO).as_posix()
        for path in sorted((_REPO / "jasper").rglob("*.py"))
        if path.name != "logging_setup.py"
        and _installs_its_own_handler(ast.parse(path.read_text()))
    }
    assert offenders == _ALLOWLIST, (
        "A logging bootstrap outside jasper/logging_setup.py must match the "
        "parked tuning zone exactly.\n"
        f"  new bypass(es): {sorted(offenders - _ALLOWLIST) or 'none'}\n"
        f"  stale entr(ies): {sorted(_ALLOWLIST - offenders) or 'none'}\n"
        "Call jasper.logging_setup.configure_logging instead — it is what "
        "attaches the redacting filter to the journal handler — and drop the "
        "file from _ALLOWLIST in the same commit."
    )
