"""Calendar voice tools — read-only views of a household member's
Google Calendar.

Backed by `jasper.google_creds.GoogleClients`; each tool resolves the
model's `account` arg ("" → default registered account, or a named
member like "brittany"), refreshes that account's OAuth token, builds
a `calendar` v3 service, and returns a flat dict the model can read
aloud.

Errors are returned as structured `{ok: false, error: ...}` dicts so
the model speaks the reason verbatim — no silent failures. Rules in
voice_daemon.SYSTEM_INSTRUCTION tell the model when to ask for
disambiguation vs. just speak the result.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from . import tool

if TYPE_CHECKING:
    from ..google_creds import GoogleClients

logger = logging.getLogger(__name__)


# Cap on items per response. Voice output is slow (~3 words/second) and
# the model condenses anyway; sending more events just costs latency
# and tokens. 20 covers a packed work day with margin.
_MAX_EVENTS = 20


def _format_clock_time(dt: datetime) -> str:
    """Render a datetime in 12-hour wall-clock form ('9:30 AM') in the
    Pi's local timezone. The model speaks this verbatim, so we want
    the natural-language form Siri/Alexa use, not ISO."""
    local = dt.astimezone()
    # %-I is GNU strftime for "no leading zero on hour"; safe on Linux,
    # which is the only target. macOS dev would need %#I.
    return local.strftime("%-I:%M %p").lstrip("0")


def _parse_event_dt(start_or_end: dict) -> tuple[datetime | None, bool]:
    """Pull a datetime out of a Google Calendar event start/end block.

    Returns (datetime, all_day). For all-day events Google sends
    ``{"date": "2026-05-09"}`` and we parse it as midnight in the
    Pi's local timezone — the model will use the all_day flag to
    word the response correctly ("Picnic — all day Saturday")."""
    raw = start_or_end or {}
    iso_dt = raw.get("dateTime")
    if iso_dt:
        try:
            return datetime.fromisoformat(iso_dt), False
        except ValueError:
            return None, False
    iso_date = raw.get("date")
    if iso_date:
        try:
            d = date.fromisoformat(iso_date)
            return datetime.combine(d, time.min, tzinfo=datetime.now().astimezone().tzinfo), True
        except ValueError:
            return None, True
    return None, False


def _serialise_event(item: dict) -> dict:
    """Tool-response shape for one event. Includes both raw ISO and
    pre-formatted clock times so the model doesn't need to parse
    timezones."""
    start_dt, start_all_day = _parse_event_dt(item.get("start") or {})
    end_dt, _end_all_day = _parse_event_dt(item.get("end") or {})
    out: dict[str, Any] = {
        "summary": (item.get("summary") or "(no title)").strip(),
        "all_day": start_all_day,
    }
    if start_dt is not None:
        out["start_iso"] = start_dt.isoformat()
        out["start"] = _format_clock_time(start_dt) if not start_all_day else "all day"
    if end_dt is not None:
        out["end_iso"] = end_dt.isoformat()
        if not start_all_day:
            out["end"] = _format_clock_time(end_dt)
    location = (item.get("location") or "").strip()
    if location:
        out["location"] = location
    # Google sends conferenceData when the event is a Hangouts/Meet
    # call. Surface a hint so the model can mention "video call" when
    # relevant without having to introspect the full block.
    if item.get("conferenceData"):
        out["video_call"] = True
    return out


def _no_account_error(clients: "GoogleClients", attempted: str) -> dict:
    available = clients.list_account_names()
    if not available:
        return {
            "ok": False,
            "error": (
                "No Google accounts linked to this speaker yet. "
                "Visit jts.local/google to add one."
            ),
        }
    name_list = ", ".join(available)
    if attempted:
        return {
            "ok": False,
            "error": (
                f"No Google account named '{attempted}' on this speaker. "
                f"Available: {name_list}."
            ),
        }
    return {
        "ok": False,
        "error": (
            f"Could not pick a default Google account. "
            f"Try naming one: {name_list}."
        ),
    }


def _no_credentials_error(account_name: str) -> dict:
    return {
        "ok": False,
        "error": (
            f"Google access for {account_name} can't be refreshed. "
            f"Re-link at jts.local/google."
        ),
    }


def _api_error(account_name: str, exc: Exception) -> dict:
    """Generic fallback for a googleapiclient HttpError or transport
    failure. Logged with the full traceback for debugging; the model
    speaks the short version."""
    logger.warning(
        "calendar API error for %s: %s", account_name, exc, exc_info=True,
    )
    return {
        "ok": False,
        "error": "Couldn't reach Google Calendar just now. Try again in a moment.",
    }


def _list_events_sync(service, *, time_min: datetime, time_max: datetime) -> list[dict]:
    """Blocking call into the discovery client. Run in a thread via
    asyncio.to_thread so the voice loop's event loop isn't blocked."""
    resp = service.events().list(
        calendarId="primary",
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=_MAX_EVENTS,
    ).execute()
    return list(resp.get("items") or [])


def make_calendar_tools(clients: "GoogleClients | None"):
    """Build the calendar voice tools. Returns an empty list if the
    daemon doesn't have Google clients configured (no CLIENT_ID/SECRET
    or no accounts) — caller `_build_registry` checks this so the
    tools never appear to the model when they couldn't function."""
    if clients is None:
        return []

    @tool()
    async def calendar_today_summary(account: str = "") -> dict:
        """Return today's calendar events for a household member's
        Google account. `account` is the member's name as configured
        at jts.local/google (e.g. 'jasper', 'brittany'); empty string
        uses the default account. Responses include start/end clock
        times in the speaker's local timezone, location, and an
        all_day flag. Use this for 'what's on my calendar today',
        'what's Brittany doing today', 'do I have anything this
        afternoon'."""
        canonical = clients.resolve_account(account)
        if canonical is None:
            return _no_account_error(clients, account)
        service = clients.build_calendar(canonical)
        if service is None:
            return _no_credentials_error(canonical)
        now = datetime.now().astimezone()
        # End-of-day in local time so events that started yesterday
        # but extend into today still surface (Google's timeMin filter
        # is on event END, not START — events crossing the boundary
        # will be returned).
        end_of_day = datetime.combine(
            now.date(), time(23, 59, 59), tzinfo=now.tzinfo,
        )
        try:
            items = await asyncio.to_thread(
                _list_events_sync,
                service, time_min=now, time_max=end_of_day,
            )
        except Exception as e:  # noqa: BLE001
            return _api_error(canonical, e)
        events = [_serialise_event(it) for it in items]
        return {
            "ok": True,
            "account": canonical,
            "scope": "today",
            "count": len(events),
            "events": events,
        }

    @tool()
    async def calendar_upcoming(hours: int = 24, account: str = "") -> dict:
        """Return calendar events starting within the next `hours`
        hours. `hours` defaults to 24 (the next day); pass 4 for
        'what's coming up this afternoon', 168 for 'what's on this
        week'. `account` is the member's name; empty string uses the
        default. Use for 'what's next', 'what's coming up', 'do I
        have anything in the next two hours'."""
        canonical = clients.resolve_account(account)
        if canonical is None:
            return _no_account_error(clients, account)
        try:
            window_hours = int(hours)
        except (TypeError, ValueError):
            return {"ok": False, "error": "hours must be a whole number."}
        if window_hours <= 0:
            return {"ok": False, "error": "hours must be positive."}
        # Cap the window so a runaway hours=10000 doesn't make a
        # multi-megabyte response (Google enforces 250-event maxResults
        # but we cap earlier for predictable latency).
        window_hours = min(window_hours, 24 * 30)
        service = clients.build_calendar(canonical)
        if service is None:
            return _no_credentials_error(canonical)
        now = datetime.now().astimezone()
        cutoff = now + timedelta(hours=window_hours)
        try:
            items = await asyncio.to_thread(
                _list_events_sync,
                service, time_min=now, time_max=cutoff,
            )
        except Exception as e:  # noqa: BLE001
            return _api_error(canonical, e)
        events = [_serialise_event(it) for it in items]
        return {
            "ok": True,
            "account": canonical,
            "scope": f"next_{window_hours}h",
            "count": len(events),
            "events": events,
        }

    return [calendar_today_summary, calendar_upcoming]


__all__ = ["make_calendar_tools"]
