# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one Avahi ``*.service`` renderer for JTS.

Avahi (the system mDNS-SD daemon installed on Pi OS by default) is the
only mDNS responder on the host. Several JTS subsystems advertise a
service by rendering a static template (with ``__FOO__`` placeholders,
kept outside ``/etc/avahi/services`` so Avahi doesn't try to parse the
placeholder as XML) into ``/etc/avahi/services/<name>.service``, which
Avahi picks up on its own via inotify:

  - ``jasper/net/control_advert.py`` renders ``_jasper-control._tcp`` with
    the speaker's user-facing display name (a free-form, XML-escaped
    value).
  - ``jasper/peering/avahi.py`` renders ``_jasper-peer._udp`` with the
    peer id / room / primary metadata (mDNS-safe values).

Both grew their own copy of the same render+guard+atomic-write body. This
module is the single extracted implementation; the two callers route
through ``render_service``.

``render_service`` is FAIL-SOFT and NEVER raises into the caller. The
callers run on hot paths (/speaker save, /rooms peering save, deploy/install.sh)
that must not break because mDNS could not be re-rendered. Every handled
failure — missing/unreadable template, a stray ``__FOO__`` placeholder,
a write failure — logs and returns ``RenderResult.FAILED``. Retry belongs to
each caller; for the control advert, the retry opportunities are a later
/speaker apply or deploy/install render, not a jasper-control restart. The
render is idempotent (a byte-stable render skips the write, so a
long-lived advert like
``_jasper-control._tcp`` never tears down and re-adds its service-group)
and atomic through :func:`jasper.atomic_io.atomic_write_text`.

The two callers differ only in whether the substituted values need
XML-escaping, which is the ``escape`` knob:

  - ``escape=True`` (control advert): a free-form name with ``&``, ``<``,
    or ``>`` would make Avahi reject the entire ``<service-group>`` and
    drop the service, so each value is run through
    ``xml.sax.saxutils.escape`` before substitution. Load-bearing, not
    cosmetic.
  - The peering values are already mDNS-safe (UUID / constrained
    room / ``0``|``1``), so escaping them is byte-identical — they pass
    ``escape=True`` too without changing output.

``substitutions`` keys are the FULL tokens including the ``__..__``
markers, e.g. ``{"__SPEAKER_NAME__": name}``.

``render_service`` returns a 3-state ``RenderResult`` (``WROTE`` /
``UNCHANGED`` / ``FAILED``) rather than a lossy bool. The distinction
the bool couldn't carry is WROTE-vs-UNCHANGED: a caller that wants to
log only on an actual on-disk change can read it directly off the result
instead of bracketing the call with two reads of the output file to diff
before/after.
"""

from __future__ import annotations

import enum
import logging
import re
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from jasper.atomic_io import atomic_write_text
from jasper.log_event import log_event

# Detector for any unresolved __FOO__ placeholder. Catches template
# drift (a new token added to a template without a matching key in the
# caller's ``substitutions`` dict).
_PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")

logger = logging.getLogger(__name__)


class RenderResult(enum.Enum):
    """Outcome of ``render_service`` — replaces the lossy bool.

    The bool collapsed "wrote the file" and "already up-to-date" into a
    single ``True``, forcing a caller that only wanted to act on a real
    change to re-read the output file before and after to diff it. These
    three states make that distinction first-class:

      - ``WROTE``     — the rendered bytes differed from disk; the file
                        was atomic-written.
      - ``UNCHANGED`` — the render matched disk byte-for-byte; nothing was
                        written (the idempotent path).
      - ``FAILED``    — a handled failure (missing/unreadable template,
                        stray placeholder, write OSError); nothing written.

    Truthiness is intentionally NOT overloaded — callers compare against
    members explicitly (``r is RenderResult.FAILED`` / ``is
    RenderResult.WROTE``) so the success/skip/fail trichotomy can't be
    accidentally flattened back into a bool.
    """

    WROTE = "wrote"
    UNCHANGED = "unchanged"
    FAILED = "failed"


def render_service(
    template_path: str,
    out_path: str,
    substitutions: dict[str, str],
    *,
    escape: bool = True,
) -> RenderResult:
    """Render an Avahi ``*.service`` template and atomic-write it.

    Reads ``template_path``, replaces each ``token`` in ``substitutions``
    with its value (XML-escaped first when ``escape`` is True), refuses
    to install a half-rendered file (any leftover ``__FOO__``), and
    atomic-writes the result to ``out_path`` (mode 0644). Avahi picks up
    the change on its own via inotify.

    ``substitutions`` keys are the FULL placeholder tokens including the
    ``__..__`` markers, e.g. ``{"__SPEAKER_NAME__": "Kitchen"}``.

    Returns a :class:`RenderResult`:

      - ``WROTE``     — the file was atomic-written (bytes changed).
      - ``UNCHANGED`` — the render matched disk, so nothing was written
                        (the idempotent path — a long-lived advert never
                        tears down + re-adds its service-group on a
                        byte-stable render).
      - ``FAILED``    — a handled failure (missing/unreadable template,
                        stray placeholder, write failure); nothing
                        written.

    NEVER raises — callers degrade gracefully and own their retry policy. For
    the control advert, a later /speaker apply or deploy/install render retries
    a failure; jasper-control startup does not render it.
    """
    try:
        text = Path(template_path).read_text()
    except FileNotFoundError:
        log_event(
            logger,
            "avahi_service.template_missing",
            path=template_path,
            note="advert disabled; re-run deploy/install.sh to install it.",
            level=logging.WARNING,
        )
        return RenderResult.FAILED
    except OSError as e:
        log_event(
            logger,
            "avahi_service.template_unreadable",
            path=template_path,
            error=e,
            level=logging.WARNING,
        )
        return RenderResult.FAILED

    rendered = text
    for token, value in substitutions.items():
        rendered = rendered.replace(token, xml_escape(value) if escape else value)

    # Refuse to install a half-rendered file. Catches a template edit
    # that introduces a new placeholder the caller doesn't substitute,
    # rather than letting Avahi reject the XML and take the whole
    # service-group offline.
    stray = _PLACEHOLDER_RE.search(rendered)
    if stray:
        log_event(
            logger,
            "avahi_service.stray_placeholder",
            path=out_path,
            placeholder=repr(stray.group(0)),
            note="refusing to install. Add the substitution in the caller.",
            level=logging.ERROR,
        )
        return RenderResult.FAILED

    # Idempotence: if the render matches what's on disk, skip the write.
    # Critical for long-lived adverts — a byte-stable render never tears
    # down and re-adds the service-group, so browsers never see a gap.
    try:
        if Path(out_path).read_text() == rendered:
            return RenderResult.UNCHANGED
    except FileNotFoundError:
        pass
    except OSError:
        pass

    try:
        atomic_write_text(out_path, rendered, mode=0o644)
    except OSError as e:
        log_event(
            logger,
            "avahi_service.write_failed",
            path=out_path,
            error=e,
            level=logging.ERROR,
        )
        return RenderResult.FAILED

    log_event(logger, "avahi_service.installed", path=out_path)
    return RenderResult.WROTE
