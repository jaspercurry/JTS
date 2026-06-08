"""Render and install the Avahi service file for `_jasper-control._tcp`.

Avahi (the system mDNS-SD daemon installed on Pi OS by default) is the
only mDNS responder on the host. This module renders the always-on
`_jasper-control._tcp` advert from a template, substituting the
speaker's user-facing display name (jasper/speaker_name.py) into a
`name=` TXT record so the /rooms/ directory shows the same friendly
name peers see on Spotify / AirPlay / Bluetooth / USB.

The render+guard+atomic-write body is the shared implementation in
jasper/avahi_service.py (`render_service`); this module owns only the
control-advert specifics layered on top, which mirror
jasper/peering/avahi.py's shape with three deliberate differences:

  1. The advert is ALWAYS on. `_jasper-control._tcp` is the control-
     plane service the rotary dial discovers via
     ``MDNS.queryService("jasper-control", "tcp")`` (see
     deploy/avahi/jasper-control.service for the dial contract). There
     is no `uninstall()` — the rendered file must always exist or dial
     discovery degrades to the dial's compile-time JASPER_HOST.

  2. The substituted value is a FREE-FORM user name, so it is
     XML-escaped before substitution. Avahi parses each *.service file
     as a single <service-group>; one malformed character (`&`, `<`,
     `>`) would make Avahi reject the whole group, taking
     `_jasper-control._tcp` offline and breaking the dial. The escaping
     is done by ``render_service(..., escape=True)``. peering/avahi.py's
     values are constrained (UUID / controlled room/primary), so the
     escape is byte-identical there; the name is not.

  3. The change to the service file vs. the historical static one is
     purely additive — same <service>/<type>/<port>, plus the one
     `name=` TXT record — so the dial (which keys off type + address)
     is unaffected.

The template at `/etc/jasper/avahi-templates/jasper-control.service` is
installed by `deploy/install.sh`. It lives OUTSIDE /etc/avahi/services
so Avahi doesn't try to parse its `__SPEAKER_NAME__` placeholder as XML.
At runtime, this module substitutes the escaped name and atomic-writes
the rendered file to `/etc/avahi/services/jasper-control.service`. Avahi
auto-reloads via inotify (with a deterministic systemctl reload as the
fallback).

Every failure is fail-soft: a missing/unreadable template, a stray
placeholder, a write failure, or a reload failure logs and returns
``False`` (or is swallowed at DEBUG). ``render_control_advert`` NEVER
raises into the caller — /speaker save and deploy/install.sh must not
break because mDNS could not be re-rendered. The backstop is the
restart of jasper-control, which re-renders on the next boot.
"""
from __future__ import annotations

import logging
import os

from . import avahi_service
from .avahi_service import RenderResult

logger = logging.getLogger(__name__)


# Where install.sh drops the template. Lives outside /etc/avahi/services
# so Avahi doesn't try to parse it as-is (the placeholder isn't a valid
# XML value). Owned by root, mode 0644. Mirrors the peering template dir.
CONTROL_AVAHI_TEMPLATE = "/etc/jasper/avahi-templates/jasper-control.service"

# Where the rendered file goes for Avahi to pick up.
CONTROL_AVAHI_SERVICE = "/etc/avahi/services/jasper-control.service"


def _resolve_name(name: str | None) -> str:
    """Resolve the name to advertise, never raising.

    ``None`` reads the canonical speaker name (env-first, then
    /var/lib/jasper/speaker_name.env, then the built-in "JTS" default).
    An empty/blank value falls back to the hostname so the `name=` TXT
    record is never empty and the service always advertises something
    addressable.
    """
    resolved = name
    if resolved is None:
        try:
            # Route the name through the ONE identity reader so the advert
            # agrees with /rooms and the rest of the speaker on "who is
            # this speaker". identity.read_identity().name IS
            # speaker_name.runtime_name() today (with the same "JTS"
            # default), so this is identity-sourced without changing the
            # happy-path value. read_identity() is itself TOTAL; the
            # except stays as defense-in-depth so a read can never break
            # advertising.
            from .identity import read_identity

            resolved = read_identity().name
        except Exception as e:  # noqa: BLE001 — never let a read break advertising
            logger.warning(
                "event=control_advert.name_read result=failed error=%s", e,
            )
            resolved = ""
    resolved = (resolved or "").strip()
    if not resolved:
        resolved = os.environ.get("JASPER_HOSTNAME", "jts.local").strip() or "jts.local"
    return resolved


def render_control_advert(
    name: str | None = None,
    *,
    template: str = CONTROL_AVAHI_TEMPLATE,
    out: str = CONTROL_AVAHI_SERVICE,
    reload: bool = True,
) -> bool:
    """Render the always-on `_jasper-control._tcp` advert with the
    speaker's friendly name and atomic-write it into /etc/avahi/services/.

    ``name`` defaults to the canonical speaker name; an empty/unset name
    falls back to the hostname so the advert is never name-less. The name
    is XML-escaped before substitution.

    Returns True if the file was written (or was already up-to-date),
    False on any handled failure (missing/unreadable template, stray
    placeholder, write failure). NEVER raises — the caller (/speaker save,
    deploy/install.sh) degrades gracefully; the backstop is the next
    jasper-control restart re-rendering this file.

    The render/guard/atomic-write/reload body is delegated to the shared
    ``jasper.avahi_service.render_service``, which OWNS the reload: we pass
    ``reload=reload`` and it reloads avahi-daemon only when it actually wrote
    the file (``RenderResult.WROTE``). The name is a free-form user value, so
    we substitute with ``escape=True`` (load-bearing: an unescaped
    `&`/`<`/`>` would make Avahi drop the whole service-group and break the
    dial). The dial-safety property is unchanged — a byte-stable re-render
    returns ``RenderResult.UNCHANGED`` and skips both the write and the
    reload (a needless write+reload tears down and re-adds the service-group,
    opening a discovery gap) — but because ``render_service`` reports
    WROTE-vs-UNCHANGED-vs-FAILED directly, this no longer reads the rendered
    file before/after to detect whether a write happened.
    """
    resolved = _resolve_name(name)
    substitutions = {"__SPEAKER_NAME__": resolved}

    r = avahi_service.render_service(
        template,
        out,
        substitutions,
        escape=True,
        reload=reload,
    )
    if r is RenderResult.WROTE:
        logger.info("event=control_advert.installed path=%s name=%r", out, resolved)
    return r is not RenderResult.FAILED
