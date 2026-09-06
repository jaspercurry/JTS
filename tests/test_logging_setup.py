# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The journal is redacted by construction.

``configure_logging`` is the one bootstrap every process under ``jasper/``
calls, and the filter it attaches is what keeps a credential out of the
journal (non-negotiable 3). These tests drive real records through the real
root handler rather than through caplog, whose own handler is not the one
under test — and whose presence on the root logger would make
``basicConfig`` a no-op, so each test takes the root logger away from
pytest for the duration and restores it afterwards.
"""
from __future__ import annotations

import ast
import contextlib
import io
import logging
from pathlib import Path

import pytest

import jasper.logging_setup as logging_setup
from jasper.logging_setup import configure_logging

_REPO = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def _process_start_root():
    """A root logger as a fresh interpreter has it: no handlers."""
    root, jasper = logging.getLogger(), logging.getLogger("jasper")
    saved = (root.handlers[:], root.level, jasper.handlers[:], jasper.level)
    root.handlers[:], jasper.handlers[:] = [], []
    jasper.setLevel(logging.NOTSET)
    try:
        yield
    finally:
        root.handlers[:], root.level, jasper.handlers[:], jasper.level = saved


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
    with _process_start_root():
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
    with _process_start_root():
        configure_logging()
        try:
            raise RuntimeError("upstream rejected token=abc123secretvalue")
        except RuntimeError:
            logging.getLogger("jasper.x").exception("provider call failed")

    err = capsys.readouterr().err
    assert "abc123secretvalue" not in err
    assert "RuntimeError" in err  # the traceback still arrives
    assert "<redacted>" in err


def test_a_logger_outside_jasper_is_redacted_too(capsys):
    """The filter is on the handler, not on the ``jasper`` logger.

    An httpx/urllib3 record carrying a query-string key lands in the same
    journal and has to be scrubbed by the same pass; moving the filter onto
    the ``jasper`` logger would silently stop covering it.
    """
    with _process_start_root():
        configure_logging()
        logging.getLogger("httpx").warning(
            "GET https://api.example.com/v1?key=abcdef0123456789 -> 200"
        )

    err = capsys.readouterr().err
    assert "abcdef0123456789" not in err
    assert "<redacted>" in err


def test_a_record_is_redacted_once(capsys, monkeypatch):
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
    with _process_start_root():
        configure_logging()
        ring = logging.StreamHandler(io.StringIO())
        ring.addFilter(logging_setup.REDACTING_FILTER)
        logging.getLogger("jasper").addHandler(ring)
        logging.getLogger("jasper.x").warning("OPENAI_API_KEY=sk-live-abc123456789")

    assert len(passes) == 1
    assert "sk-live-abc123456789" not in capsys.readouterr().err


# ------------------------------------------------------------------- ratchet

# A module named here bootstraps logging without the filter and publishes
# its journal unredacted, so an entry is a reviewed decision, not a
# convenience. Empty is the intended steady state.
_ALLOWLIST: frozenset[str] = frozenset()


def _calls_basic_config(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "basicConfig"
        for node in ast.walk(tree)
    )


def test_configure_logging_is_the_only_logging_bootstrap():
    """No process may reach the journal around the redacting filter.

    Remove this ratchet when ``logging.basicConfig`` stops being how the
    tree installs its journal handler (a systemd ``JournalHandler``, say):
    the scanned shape would no longer be the bypass.
    """
    offenders = {
        path.relative_to(_REPO).as_posix()
        for path in sorted((_REPO / "jasper").rglob("*.py"))
        if path.name != "logging_setup.py"
        and _calls_basic_config(ast.parse(path.read_text()))
    }
    assert not offenders - _ALLOWLIST, (
        "logging.basicConfig outside jasper/logging_setup.py:\n  "
        + "\n  ".join(sorted(offenders - _ALLOWLIST))
        + "\nCall jasper.logging_setup.configure_logging instead — it is what "
        "attaches the redacting filter to the journal handler."
    )
