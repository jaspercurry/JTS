"""The one Avahi ``*.service`` renderer for JTS.

Avahi (the system mDNS-SD daemon installed on Pi OS by default) is the
only mDNS responder on the host. Several JTS subsystems advertise a
service by rendering a static template (with ``__FOO__`` placeholders,
kept outside ``/etc/avahi/services`` so Avahi doesn't try to parse the
placeholder as XML) into ``/etc/avahi/services/<name>.service`` and
nudging Avahi to reload:

  - ``jasper/control_advert.py`` renders ``_jasper-control._tcp`` with
    the speaker's user-facing display name (a free-form, XML-escaped
    value).
  - ``jasper/peering/avahi.py`` renders ``_jasper-peer._udp`` with the
    peer id / room / primary metadata (mDNS-safe values).

Both grew their own copy of the same render+guard+atomic-write+reload
body. This module is the single extracted implementation; the two
callers route through ``render_service`` and the shared ``reload_avahi``.

``render_service`` is FAIL-SOFT and NEVER raises into the caller. The
callers run on hot paths (/speaker save, /peers save, deploy/install.sh)
that must not break because mDNS could not be re-rendered. Every handled
failure — missing/unreadable template, a stray ``__FOO__`` placeholder,
a write failure — logs and returns ``False``; the backstop is the next
daemon restart re-rendering the file. The render is idempotent (a
byte-stable render skips the write+reload, so a long-lived advert like
``_jasper-control._tcp`` never tears down and re-adds its service-group)
and atomic (tmp + ``os.chmod(0o644)`` + ``os.replace``).

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
log / reload only on an actual on-disk change can read it directly off
the result instead of bracketing the call with two reads of the output
file to diff before/after. The reload is internal and already fires only
on a write — ``render_service`` is the single owner of "did the bytes
change."
"""
from __future__ import annotations

import enum
import logging
import os
import re
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

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
                        was atomic-written (and reloaded if ``reload``).
      - ``UNCHANGED`` — the render matched disk byte-for-byte; nothing was
                        written and nothing reloaded (the idempotent path).
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
    reload: bool = True,
) -> RenderResult:
    """Render an Avahi ``*.service`` template and atomic-write it.

    Reads ``template_path``, replaces each ``token`` in ``substitutions``
    with its value (XML-escaped first when ``escape`` is True), refuses
    to install a half-rendered file (any leftover ``__FOO__``), and
    atomic-writes the result to ``out_path`` (mode 0644). When ``reload``
    is True and a write happened, nudges avahi-daemon to reload.

    ``substitutions`` keys are the FULL placeholder tokens including the
    ``__..__`` markers, e.g. ``{"__SPEAKER_NAME__": "Kitchen"}``.

    Returns a :class:`RenderResult`:

      - ``WROTE``     — the file was atomic-written (bytes changed); a
                        reload fired when ``reload`` is True.
      - ``UNCHANGED`` — the render matched disk, so nothing was written
                        and nothing reloaded (the idempotent path — a
                        long-lived advert never tears down + re-adds its
                        service-group on a byte-stable render).
      - ``FAILED``    — a handled failure (missing/unreadable template,
                        stray placeholder, write failure); nothing
                        written, nothing reloaded.

    NEVER raises — callers degrade gracefully; the backstop is the next
    daemon restart re-rendering. The reload fires ONLY on ``WROTE``, so a
    caller can drive its own reload off the result without re-reading the
    output file to detect whether a write happened.
    """
    try:
        text = Path(template_path).read_text()
    except FileNotFoundError:
        logger.warning(
            "event=avahi_service.template_missing path=%s — advert disabled; "
            "re-run deploy/install.sh to install it.",
            template_path,
        )
        return RenderResult.FAILED
    except OSError as e:
        logger.warning(
            "event=avahi_service.template_unreadable path=%s error=%s",
            template_path, e,
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
        logger.error(
            "event=avahi_service.stray_placeholder path=%s placeholder=%r — "
            "refusing to install. Add the substitution in the caller.",
            out_path, stray.group(0),
        )
        return RenderResult.FAILED

    # Idempotence: if the render matches what's on disk, skip write+reload.
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
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        tmp = out_path + ".tmp"
        with open(tmp, "w") as f:
            f.write(rendered)
        os.chmod(tmp, 0o644)
        os.replace(tmp, out_path)
    except OSError as e:
        logger.error("event=avahi_service.write_failed path=%s error=%s", out_path, e)
        return RenderResult.FAILED

    logger.info("event=avahi_service.installed path=%s", out_path)
    if reload:
        reload_avahi()
    return RenderResult.WROTE


def reload_avahi() -> None:
    """Best-effort reload of avahi-daemon (the shared reload).

    inotify usually catches changes on its own but an explicit reload is
    deterministic and fast (<100 ms). Same pattern as deploy/install.sh's
    install_avahi_jasper_control. Fail-soft — never raises.
    """
    try:
        subprocess.run(
            ["systemctl", "reload", "avahi-daemon"],
            check=False, timeout=4,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("event=avahi_service.reload_failed error=%s", e)
