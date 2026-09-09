# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one logging bootstrap — and the one place the journal is redacted.

Every process that installs a journal handler goes through
:func:`configure_logging` and carries :class:`RedactingFilter`
(non-negotiable 3; ADR-0243 owns what "credential-shaped" means),
except the parked bootstraps whose journals are not redacted —
``tests/test_logging_setup.py`` holds that exhaustive list and fails
on any addition to it.
"""
from __future__ import annotations

import logging

from .secret_redaction import redact_secrets

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Marks a record this filter has already redacted, so a record reaching
# two filtered handlers (the journal handler and the flight recorder's
# ring) costs one redaction pass rather than two.
_REDACTED_ATTR = "_jasper_redacted"

_EXC_FORMATTER = logging.Formatter()


class RedactingFilter(logging.Filter):
    """Redact a record in place before any handler formats it.

    Attached to *handlers*, not loggers: records from every ``jasper.*``
    logger propagate to the root handler, and only a handler filter sees
    them all. The record is mutated, so every other handler sharing it —
    a second journal handler, the flight recorder's ring — is redacted too,
    and a formatter overriding ``formatException`` is never consulted
    because ``exc_text`` is already set by the time it formats.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _REDACTED_ATTR, False):
            return True
        setattr(record, _REDACTED_ATTR, True)
        try:
            self._scrub(record)
        except Exception as exc:  # noqa: BLE001
            # Handler.handle does not guard Filterer.filter, so anything
            # raised here surfaces at the log call site. Fail closed: an
            # unscrubbable record loses its content, never its redaction.
            record.msg = (
                "<redacted: log record could not be scrubbed "
                f"({type(exc).__name__})>"
            )
            record.args = ()
            # Formatter.format re-derives exc_text from exc_info when the
            # former is empty, so clearing exc_text alone still lets the
            # untouched traceback (or an overridden formatException) reach
            # the stream raw. exc_info must go too.
            record.exc_info = record.exc_text = record.stack_info = None
        return True

    @staticmethod
    def _scrub(record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception as exc:  # noqa: BLE001
            # A %-format mismatch is reported by Handler.handleError today
            # and must not become an exception raised at the log call site.
            flattened = (
                f"{record.msg!r} % {record.args!r} "
                f"(unformattable: {type(exc).__name__})"
            )
            record.msg, record.args = redact_secrets(flattened), ()
        else:
            redacted = redact_secrets(message)
            if redacted != message:
                # The pre-redaction msg is NOT kept anywhere on the record:
                # the leak case is exactly the case that would retain it.
                record.msg, record.args = redacted, ()
        if record.exc_info:
            # Formatter.format reuses a non-empty exc_text, so pre-formatting
            # the traceback here is what redacts it.
            record.exc_text = redact_secrets(
                record.exc_text or _EXC_FORMATTER.formatException(record.exc_info)
            )
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)


REDACTING_FILTER = RedactingFilter()


def configure_logging(
    *, level: int | str = logging.INFO, fmt: str = LOG_FORMAT,
) -> None:
    """Install this process's journal handler, redacting everything on it."""

    root = logging.getLogger()
    logging.basicConfig(level=level, format=fmt)
    # basicConfig no-ops entirely when root already carries a handler, so
    # neither the level nor the filter may be left to it: one foreign handler
    # would otherwise take this process's whole journal out of redaction with
    # no signal. setLevel and addFilter are both idempotent.
    root.setLevel(level)
    for handler in root.handlers:
        handler.addFilter(REDACTING_FILTER)


def configure_verbose_logging(*, verbose: bool) -> None:
    """Use DEBUG for ``--verbose`` and WARNING otherwise."""

    configure_logging(level=logging.DEBUG if verbose else logging.WARNING)
