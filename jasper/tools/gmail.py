"""Gmail voice tools — read-only views of a household member's Gmail.

Backed by `jasper.google_creds.GoogleClients`. Two tools:

- `gmail_unread_summary(limit=5, account="")` — top-N unread inbox
  messages with from/subject/date/snippet. The model uses these to
  answer 'any new emails' / 'who emailed me'.
- `gmail_read_thread(thread_id, account="")` — full read of one
  thread, body decoded from MIME parts. The model uses this when
  the user says 'read me the second one' after a summary.

Voice-friendliness:
- Dates are formatted relatively ('9:30 AM today', 'yesterday at
  3:14 PM', 'Tuesday at 2 PM', 'March 5'). The model reads what's
  given, so doing the math here keeps replies natural.
- Body text is stripped (HTML tags and entities collapsed) and
  truncated. Voice TTS at ~3 words/sec means even 4000 chars is a
  ~14-minute readout — realistically the model summarises anyway,
  but the cap protects against pathological all-image newsletters.
- All errors are returned as `{ok: false, error: ...}` per the
  no-silent-failure contract; the model speaks the error.
"""
from __future__ import annotations

import asyncio
import base64
import html
import logging
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any

from . import tool

if TYPE_CHECKING:
    from ..google_creds import GoogleClients

logger = logging.getLogger(__name__)


# Cap on per-call list size and per-message body length. The model
# condenses anyway; sending more just costs tokens and TTFA.
_DEFAULT_UNREAD = 5
_MAX_UNREAD = 20
_MAX_THREAD_MESSAGES = 10
_MAX_BODY_CHARS = 4000

# Gmail search query for the unread summary. Restricting to inbox
# filters out unread items the user has already triaged into folders;
# the explicit `-category:promotions -category:social` filters
# Gmail's auto-categorised noise so 'any new emails?' returns
# meaningful items rather than a daily-deals inbox tour.
_UNREAD_QUERY = "is:unread in:inbox -category:promotions -category:social"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


# ----------------------------------------------------------------------
# Date / body formatting helpers.
# ----------------------------------------------------------------------


def _parse_rfc2822_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        # Some senders ship naive timestamps. Treat as UTC — better
        # than crashing on the astimezone() call below.
        from datetime import timezone
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_relative_date(dt: datetime, *, now: datetime | None = None) -> str:
    """Render a datetime as a phrase the model can read aloud:
    today/yesterday at HH:MM, weekday at HH:MM, or 'Mon DD'."""
    local = dt.astimezone()
    if now is None:
        now = datetime.now().astimezone()
    today = now.date()
    target = local.date()
    delta_days = (today - target).days
    clock = local.strftime("%-I:%M %p").lstrip("0")
    if delta_days == 0:
        return f"{clock} today"
    if delta_days == 1:
        return f"yesterday at {clock}"
    # Within the past week: weekday name reads better than a date.
    if 0 < delta_days < 7:
        return f"{local.strftime('%A')} at {clock}"
    # Older this calendar year: drop the year for brevity.
    if local.year == now.year:
        return local.strftime("%B %-d").replace("  ", " ")
    return local.strftime("%B %-d, %Y")


def _strip_html(text: str) -> str:
    """Crude HTML→text for voice readout. The model doesn't need
    pretty formatting; it just needs the words. Drops tags, unescapes
    entities, collapses runs of blank lines."""
    if not text:
        return ""
    text = _HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text


def _decode_part(b64url: str) -> str:
    if not b64url:
        return ""
    # Gmail uses URL-safe base64 without padding. The "===" suffix is
    # always-safe overpadding that base64 ignores past the actual end.
    try:
        return base64.urlsafe_b64decode(b64url + "===").decode(
            "utf-8", errors="replace",
        )
    except Exception:  # noqa: BLE001
        return ""


def _decode_body(payload: dict) -> str:
    """Walk a Gmail message payload tree and return the best plaintext
    we can find. Prefers text/plain parts; falls back to text/html
    with tags stripped. Returns an empty string if no decodable body
    exists (rare — at minimum Gmail synthesises one)."""
    plain: list[str] = []
    html_parts: list[str] = []

    def walk(node: dict | None) -> None:
        if not node:
            return
        mime = (node.get("mimeType") or "").lower()
        body = node.get("body") or {}
        data = body.get("data")
        if data:
            decoded = _decode_part(data)
            if decoded:
                if mime == "text/plain":
                    plain.append(decoded)
                elif mime == "text/html":
                    html_parts.append(decoded)
        for child in (node.get("parts") or []):
            walk(child)

    walk(payload or {})
    if plain:
        out = "\n\n".join(plain).strip()
    elif html_parts:
        out = _strip_html("\n\n".join(html_parts)).strip()
    else:
        out = ""
    if len(out) > _MAX_BODY_CHARS:
        out = out[:_MAX_BODY_CHARS].rstrip() + "…"
    return out


def _header(headers: list[dict], name: str) -> str:
    """Case-insensitive single-value header lookup. Gmail returns
    headers as a list of {name, value} dicts; multiple Received:/etc.
    are common but we only call this for From/Subject/Date so first-
    wins is fine."""
    target = name.lower()
    for h in headers or []:
        if (h.get("name") or "").lower() == target:
            return (h.get("value") or "").strip()
    return ""


# ----------------------------------------------------------------------
# Error helpers (mirror calendar.py shape so the model gets consistent
# responses across both tool families).
# ----------------------------------------------------------------------


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
    logger.warning(
        "gmail API error for %s: %s", account_name, exc, exc_info=True,
    )
    return {
        "ok": False,
        "error": "Couldn't reach Gmail just now. Try again in a moment.",
    }


# ----------------------------------------------------------------------
# Sync API wrappers — invoked through asyncio.to_thread so the voice
# loop's event loop isn't blocked on networking.
# ----------------------------------------------------------------------


def _list_unread_sync(service, *, max_results: int) -> list[dict]:
    resp = service.users().messages().list(
        userId="me", q=_UNREAD_QUERY, maxResults=max_results,
    ).execute()
    return list(resp.get("messages") or [])


def _get_message_metadata_sync(service, msg_id: str) -> dict:
    return service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()


def _get_thread_sync(service, thread_id: str) -> dict:
    return service.users().threads().get(
        userId="me", id=thread_id, format="full",
    ).execute()


# ----------------------------------------------------------------------
# Tool factory.
# ----------------------------------------------------------------------


def make_gmail_tools(clients: "GoogleClients | None"):
    if clients is None:
        return []

    @tool()
    async def gmail_unread_summary(limit: int = _DEFAULT_UNREAD, account: str = "") -> dict:
        """Return the top-N unread inbox messages for a household
        member's Gmail account, filtered to skip Gmail's promotions
        and social categories.

        Use for "any new emails?", "what's in my inbox?", "did
        Brittany email me?". When the user names a household member,
        pass that name as `account`; otherwise omit.

        Each entry has from, subject, date (formatted relative to
        now: 'today at 9:30 AM', 'yesterday at 3 PM', 'Monday at
        2:14 PM'), and a snippet. `limit` defaults to 5 (max 20);
        `account` defaults to the primary registered account.

        Voice answer style: 'You have N unread: <sender> about
        <subject>, <sender> about <subject>…' — scannable; the user
        can follow up for details. Use the formatted `date` field
        verbatim; don't reformat ISO timestamps. If `count` is 0:
        'No new emails.'

        On error returns {ok: false, error: ...}; speak the error
        verbatim — it tells the user how to fix the access issue.
        """
        canonical = clients.resolve_account(account)
        if canonical is None:
            return _no_account_error(clients, account)
        try:
            n = int(limit)
        except (TypeError, ValueError):
            return {"ok": False, "error": "limit must be a whole number."}
        if n < 1:
            n = 1
        elif n > _MAX_UNREAD:
            n = _MAX_UNREAD
        service = clients.build_gmail(canonical)
        if service is None:
            return _no_credentials_error(canonical)
        try:
            stub_list = await asyncio.to_thread(
                _list_unread_sync, service, max_results=n,
            )
        except Exception as e:  # noqa: BLE001
            return _api_error(canonical, e)
        if not stub_list:
            return {
                "ok": True,
                "account": canonical,
                "count": 0,
                "messages": [],
            }
        # Fetch metadata for each id concurrently — Google's per-call
        # latency is 100-300 ms, so 5 sequential = up to 1.5 s, while
        # parallel keeps it close to a single call.
        async def _fetch(stub: dict) -> dict | None:
            msg_id = stub.get("id") or ""
            if not msg_id:
                return None
            try:
                return await asyncio.to_thread(
                    _get_message_metadata_sync, service, msg_id,
                )
            except Exception as e:  # noqa: BLE001
                logger.info("gmail metadata fetch failed for %s: %s", msg_id, e)
                return None

        msgs = await asyncio.gather(*(_fetch(s) for s in stub_list))
        out: list[dict[str, Any]] = []
        for m in msgs:
            if m is None:
                continue
            headers = (m.get("payload") or {}).get("headers") or []
            sender = _header(headers, "From")
            subject = _header(headers, "Subject") or "(no subject)"
            raw_date = _header(headers, "Date")
            entry: dict[str, Any] = {
                "id": m.get("id") or "",
                "thread_id": m.get("threadId") or "",
                "from": sender,
                "subject": subject.strip(),
            }
            dt = _parse_rfc2822_date(raw_date)
            if dt is not None:
                entry["date"] = _format_relative_date(dt)
                entry["date_iso"] = dt.isoformat()
            snippet = (m.get("snippet") or "").strip()
            if snippet:
                entry["snippet"] = snippet
            out.append(entry)
        return {
            "ok": True,
            "account": canonical,
            "count": len(out),
            "messages": out,
        }

    @tool()
    async def gmail_read_thread(thread_id: str, account: str = "") -> dict:
        """Return the full body text of a Gmail thread, with one
        entry per message (oldest first).

        Use when the user says "read me the first one", "open that
        email", "what does it say" after a summary. `thread_id` is
        the value from a prior `gmail_unread_summary` response;
        don't fabricate one. `account` defaults to the primary
        registered account.

        Returns subject and a list of messages each with from, date,
        and body (text/plain when available, otherwise HTML stripped
        to text).

        Voice answer style: lead with the subject and sender ('From
        <sender>, subject <subject>:'), then read the body. For
        multi-message threads (oldest first), name the sender for
        each. Body is already cleaned of HTML; read it as-is, but
        skip signature blocks if the message ends with one.

        On error returns {ok: false, error: ...}; speak the error
        verbatim.
        """
        canonical = clients.resolve_account(account)
        if canonical is None:
            return _no_account_error(clients, account)
        if not thread_id or not isinstance(thread_id, str):
            return {"ok": False, "error": "thread_id is required."}
        service = clients.build_gmail(canonical)
        if service is None:
            return _no_credentials_error(canonical)
        try:
            thread = await asyncio.to_thread(
                _get_thread_sync, service, thread_id,
            )
        except Exception as e:  # noqa: BLE001
            return _api_error(canonical, e)
        messages = (thread.get("messages") or [])[:_MAX_THREAD_MESSAGES]
        out_messages: list[dict[str, Any]] = []
        thread_subject = ""
        for m in messages:
            payload = m.get("payload") or {}
            headers = payload.get("headers") or []
            subject = _header(headers, "Subject")
            if subject and not thread_subject:
                # First message's subject — usually the canonical
                # thread subject without "Re: " prefixes.
                thread_subject = subject
            raw_date = _header(headers, "Date")
            entry: dict[str, Any] = {
                "from": _header(headers, "From"),
                "subject": subject,
                "body": _decode_body(payload),
            }
            dt = _parse_rfc2822_date(raw_date)
            if dt is not None:
                entry["date"] = _format_relative_date(dt)
                entry["date_iso"] = dt.isoformat()
            out_messages.append(entry)
        return {
            "ok": True,
            "account": canonical,
            "thread_id": thread_id,
            "subject": thread_subject,
            "message_count": len(out_messages),
            "messages": out_messages,
        }

    return [gmail_unread_summary, gmail_read_thread]


__all__ = ["make_gmail_tools"]
