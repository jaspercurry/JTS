"""Citi Bike (NYC + Jersey City + Hoboken) GBFS client.

Two public-CDN feeds, no API key (https://gbfs.citibikenyc.com/gbfs/gbfs.json):

  * station_information.json  — id/name/lat/lon/capacity. 1 h TTL.
  * station_status.json       — bikes/ebikes/docks/last_reported. 30 s TTL.

A module-private TTL cache fronts both feeds. On a fresh-fetch failure
with any cached entry present (even past its TTL) we serve the cached
copy and the caller can disclose staleness via
`StationStatus.last_reported_age_seconds`. A transient GBFS outage
therefore degrades the answer rather than silencing it.

This module is the GBFS data layer; the wizard provider at
`jasper.transit.providers.citibike` imports `fetch_feed` to power
"nearest station" lookups, and the voice tool (PR 2) constructs a
`CitiBikeClient` from the wizard-written env vars for live queries.

Everything here is sync. The voice tool wraps `CitiBikeClient.get_status`
in `asyncio.to_thread` so the realtime LLM session never blocks —
matches the subway pattern in `jasper.subway` rather than bus's
parallel-fan-out AsyncClient (we only hit two feeds, both cached,
so async would be unnecessary complexity).
"""
from __future__ import annotations

import logging
import threading
import time
import re
from dataclasses import dataclass

import httpx

from .transit.base import TransitError

logger = logging.getLogger(__name__)


GBFS_BASE = "https://gbfs.citibikenyc.com/gbfs/en"
STATION_INFO_URL = f"{GBFS_BASE}/station_information.json"
STATION_STATUS_URL = f"{GBFS_BASE}/station_status.json"

HTTP_TIMEOUT = 3.0
USER_AGENT = "jts-jasper/1.0"

# Station list rarely changes (Lyft retires/adds maybe a handful per
# month); 1 h is enough freshness without re-pulling 1500+ entries
# repeatedly. Live counts update server-side every ~60 s per the GBFS
# spec, so 30 s catches every other publish while still letting two
# tool calls 5 s apart share one fetch.
INFO_TTL_SECONDS = 3600.0
STATUS_TTL_SECONDS = 30.0


# --- GBFS fetch with TTL cache + stale-on-error -----------------------

@dataclass(frozen=True)
class _CacheEntry:
    timestamp: float          # monotonic clock
    data: dict


_FEED_CACHE: dict[str, _CacheEntry] = {}
_FEED_LOCK = threading.Lock()


def _classify_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, ValueError):
        return "parse_error"
    if isinstance(exc, httpx.HTTPError):
        return "network"
    return "network"


def _http_get_json(url: str, client: httpx.Client | None) -> dict:
    """Sync GET → parsed JSON. Builds a short-lived client when none
    is injected; closes it before returning. Test injection follows
    the same shape as `nyc_bus._NycBus._client`."""
    owns = client is None
    if owns:
        client = httpx.Client(timeout=HTTP_TIMEOUT)
    try:
        r = client.get(url, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        return r.json()
    finally:
        if owns:
            client.close()


def fetch_feed(
    url: str,
    ttl_seconds: float,
    *,
    client: httpx.Client | None = None,
) -> dict:
    """Fetch one GBFS feed with TTL caching. Serves stale on error.

    Within `ttl_seconds` of the last successful fetch, returns the
    cached copy without going to the network. After expiry (or on
    cache miss), fetches fresh; on fetch failure, returns the
    previous cached copy if one exists (logged at WARN with the
    cache age) and raises `TransitError` only when fetch fails AND
    no cached entry is available.

    `client` is a test-only injection seam — production callers leave
    it None so each call gets its own short-lived `httpx.Client`.
    """
    with _FEED_LOCK:
        entry = _FEED_CACHE.get(url)
        if entry is not None and (time.monotonic() - entry.timestamp) < ttl_seconds:
            return entry.data

    try:
        data = _http_get_json(url, client)
    except (httpx.HTTPError, ValueError) as exc:
        outcome = _classify_error(exc)
        # Re-read under the lock: another caller may have refreshed
        # the cache while we were on the network.
        with _FEED_LOCK:
            stale = _FEED_CACHE.get(url)
        if stale is not None:
            age = time.monotonic() - stale.timestamp
            logger.warning(
                "event=transit.citibike.fetch.stale url=%s outcome=%s "
                "age_seconds=%.0f err=%s",
                url, outcome, age, exc,
            )
            return stale.data
        logger.warning(
            "event=transit.citibike.fetch.error url=%s outcome=%s err=%s",
            url, outcome, exc,
        )
        raise TransitError(f"GBFS request failed: {exc}") from exc

    with _FEED_LOCK:
        _FEED_CACHE[url] = _CacheEntry(timestamp=time.monotonic(), data=data)
    logger.info("event=transit.citibike.fetch.ok url=%s", url)
    return data


def clear_cache() -> None:
    """Test helper. Drops every cached feed."""
    with _FEED_LOCK:
        _FEED_CACHE.clear()


# --- Saved-stations env parsing ---------------------------------------

def parse_saved_stations(raw: str) -> list[tuple[str, str]]:
    """Parse `JASPER_CITIBIKE_STATIONS` into a list of (station_id, label).

    Format mirrors `JASPER_BUS_STOPS`: pipe-separated id|label,
    comma-separated entries. Labels are optional (bare id falls back
    to using the id as the label). GBFS station IDs are UUIDs which
    don't contain `|` or `,`, so this encoding is safe; the wizard
    additionally sanitises labels on save.

    Whitespace is tolerated around any component. Malformed entries
    (empty token after stripping, empty id) are skipped silently —
    the wizard validates at save time, so anything reaching here is
    either wizard-written or operator-typed-by-hand.

    Examples::

        "abc-uuid|9 Av & 41 St,def-uuid|Atlantic Av"
        "abc-uuid"                  # bare id → label = id
        "abc-uuid|9 Av,def-uuid"    # mix is fine
    """
    out: list[tuple[str, str]] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        if "|" in token:
            sid, _, label = token.partition("|")
            sid = sid.strip()
            label = label.strip() or sid
        else:
            sid = token
            label = token
        if not sid:
            continue
        out.append((sid, label))
    return out


def format_saved_stations(saved: list[tuple[str, str]]) -> str:
    """Inverse of `parse_saved_stations` — used by the wizard's save
    handler. Round-trip safe for well-formed inputs."""
    return ",".join(f"{sid}|{label}" for sid, label in saved)


# --- Speech-friendly station name normalization ----------------------

# GBFS returns raw station names with USPS-style abbreviations:
#   "9 Av & 41 St"           — MTA convention, Citi Bike sometimes
#   "Broadway & W 41 St"     — Lyft mixed convention
#   "4 Ave & E 12 St"        — Citi Bike's usual "Ave" spelling
#   "St James Pl"            — "St" here means "Saint"
#
# The realtime LLM emits these verbatim and its TTS reads them
# letter-by-letter: "St" becomes "Street" (correct) but "41" stays
# cardinal — produces "41 Street" instead of "41st Street." Same
# issue for compass directions ("W" → "W" instead of "West") and
# Avenue/Drive/etc. This normalizer expands abbreviations and
# ordinalizes numbered streets before the label reaches the LLM.
# Applied at as_dict() time only — the wizard-saved label and
# the StationStatus dataclass field stay raw so they match what
# the user sees on the Citi Bike website / app.

# Street-suffix abbreviations. Word-bounded so "Avenue" doesn't
# match inside "Avalanche" or similar. Optional trailing period for
# operator-typed labels ("Av.").
_SUFFIX_MAP = [
    (re.compile(r"\bAv\b\.?"), "Avenue"),
    (re.compile(r"\bAve\b\.?"), "Avenue"),
    (re.compile(r"\bBlvd\b\.?"), "Boulevard"),
    (re.compile(r"\bBr\b\.?"), "Bridge"),
    (re.compile(r"\bCt\b\.?"), "Court"),
    (re.compile(r"\bDr\b\.?"), "Drive"),
    (re.compile(r"\bExt\b\.?"), "Extension"),
    (re.compile(r"\bHts\b\.?"), "Heights"),
    (re.compile(r"\bLn\b\.?"), "Lane"),
    (re.compile(r"\bPkwy\b\.?"), "Parkway"),
    (re.compile(r"\bPl\b\.?"), "Place"),
    (re.compile(r"\bPlz\b\.?"), "Plaza"),
    (re.compile(r"\bRd\b\.?"), "Road"),
    (re.compile(r"\bSq\b\.?"), "Square"),
    (re.compile(r"\bTer\b\.?"), "Terrace"),
    (re.compile(r"\bTpke\b\.?"), "Turnpike"),
]

# Compass-direction expansion. Any standalone W/E/N/S word EXCEPT
# when it's a Brooklyn lettered-avenue identifier ("Avenue W",
# "Ave W", "Av W" — Brooklyn really does have an Avenue W). Negative
# lookbehinds for the three Avenue-prefix forms cover that case.
# Otherwise W in "Union Sq W & 14 St" (Union Square West), in "W 35 St"
# (at start of string), and in "& W 4 St" (after an intersection
# separator) all expand correctly.
_DIR_MAP = {"W": "West", "E": "East", "N": "North", "S": "South"}
_DIR_PAT = re.compile(r"(?<!Avenue )(?<!Ave )(?<!Av )\b([WENS])\b\.?")

# "St" → "Saint" when followed by a capitalized proper name (St James,
# St Marks, St Nicholas). When NOT followed by a capital (after a
# number or lowercase word), it means "Street" and gets the suffix
# expansion below. Saint substitution must run BEFORE the Street
# suffix expansion, otherwise "St James Pl" gets corrupted into
# "Street James Place".
_SAINT_PAT = re.compile(r"\bSt\b\.?(?=\s+[A-Z])")
_STREET_PAT = re.compile(r"\bSt\b\.?")

# Street-suffix words that mean "this number should be ordinal".
# "127 Street" → "127th Street"; "5 Avenue" → "5th Avenue".
_ORDINAL_PAT = re.compile(
    r"\b(\d+)( (?:Avenue|Street|Place|Boulevard|Parkway|Square|Court|"
    r"Heights|Drive|Plaza|Turnpike|Road|Lane|Terrace|Bridge|Extension))",
)


def _ordinal(n: int) -> str:
    """Convert an integer to its English ordinal form.

    1 → '1st', 2 → '2nd', 11 → '11th', 21 → '21st', 101 → '101st'.
    Standard rules: 11–13 always take 'th'; otherwise the last digit
    picks st/nd/rd/th.
    """
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def normalize_station_name(name: str) -> str:
    """Expand GBFS station-name abbreviations for speech.

    Designed for the realtime LLM's TTS, which reads short
    abbreviations literally ("41 St" → "41 Street" not "41st Street").
    Order matters:

      1. Compass directions at start / after "& " (W → West)
      2. "&" → "and"
      3. "St" → "Saint" when followed by a capitalized name
      4. Street/Avenue/etc. suffix expansion
      5. Ordinalize numbers preceding a street-type word

    Examples::

        "9 Ave & 41 St"            → "9th Avenue and 41st Street"
        "Broadway & W 41 St"       → "Broadway and West 41st Street"
        "62 Dr & 110 St"           → "62nd Drive and 110th Street"
        "St James Pl"              → "Saint James Place"
        "Avenue W & E 6 St"        → "Avenue W and East 6th Street"
        "Linden St & Knickerbocker Ave"
            → "Linden Street and Knickerbocker Avenue"
    """
    s = _DIR_PAT.sub(lambda m: _DIR_MAP[m.group(1)], name)
    s = s.replace("&", "and")
    s = _SAINT_PAT.sub("Saint", s)
    for pat, repl in _SUFFIX_MAP:
        s = pat.sub(repl, s)
    s = _STREET_PAT.sub("Street", s)
    s = _ORDINAL_PAT.sub(
        lambda m: f"{_ordinal(int(m.group(1)))}{m.group(2)}", s,
    )
    return re.sub(r"\s+", " ", s).strip()


# --- Status query + runtime client ------------------------------------

@dataclass(frozen=True)
class StationStatus:
    """One saved station's live status.

    `classic_bikes = num_bikes_available - num_ebikes_available`. The
    GBFS spec defines `num_bikes_available` as the total including
    ebikes, so this subtraction is the standard derivation.

    `status` is one of:
      * "ok"      — `is_renting` and `is_installed` both 1
      * "offline" — station present in GBFS but not currently renting
      * "missing" — saved station_id no longer appears in GBFS at all
                    (Lyft retired it). counts will be zero.
    """
    station_id: str
    label: str
    classic_bikes: int
    ebikes: int
    docks: int
    status: str
    last_reported_age_seconds: int

    def as_dict(self, *, include_classic: bool = True) -> dict:
        """Serialize for the voice tool's LLM-visible response.

        `label` is normalized for speech via `normalize_station_name`
        — the realtime LLM's TTS reads abbreviations literally, so we
        expand them server-side rather than relying on the model to
        learn NYC street-name conventions. The dataclass field stays
        raw (matches what the user picked in the wizard, which itself
        matches the Citi Bike app / website).

        When the household is in `ebike_only` mode the caller passes
        `include_classic=False` and the classic count is omitted from
        the dict — better than zeroing it, since the LLM might
        otherwise verbalise "zero classic bikes" instead of just
        speaking the e-bike count."""
        d: dict[str, object] = {
            "label": normalize_station_name(self.label),
            "station_id": self.station_id,
            "ebikes": self.ebikes,
            "docks": self.docks,
            "status": self.status,
            "last_reported_age_seconds": self.last_reported_age_seconds,
        }
        if include_classic:
            d["classic_bikes"] = self.classic_bikes
        return d


def _station_status_from_feeds(
    station_id: str,
    label: str,
    info_by_id: dict[str, dict],
    status_by_id: dict[str, dict],
    *,
    now_epoch: float,
) -> StationStatus:
    """Build a StationStatus from already-indexed GBFS feed dicts."""
    if station_id not in info_by_id or station_id not in status_by_id:
        return StationStatus(
            station_id=station_id, label=label,
            classic_bikes=0, ebikes=0, docks=0,
            status="missing", last_reported_age_seconds=0,
        )
    st = status_by_id[station_id]
    bikes = int(st.get("num_bikes_available", 0) or 0)
    ebikes = int(st.get("num_ebikes_available", 0) or 0)
    docks = int(st.get("num_docks_available", 0) or 0)
    is_renting = bool(st.get("is_renting", 1))
    is_installed = bool(st.get("is_installed", 1))
    last_reported = int(st.get("last_reported", 0) or 0)
    age = max(0, int(now_epoch - last_reported)) if last_reported else 0
    return StationStatus(
        station_id=station_id, label=label,
        classic_bikes=max(0, bikes - ebikes),
        ebikes=ebikes,
        docks=docks,
        status="ok" if (is_renting and is_installed) else "offline",
        last_reported_age_seconds=age,
    )


class CitiBikeClient:
    """Runtime client for the Citi Bike voice tool.

    Holds the parsed saved-stations set plus the global e-bike-only
    flag. `get_status` returns a list of `StationStatus` for the
    saved stations (optionally filtered to those whose label contains
    a substring). The tool's async wrapper calls this via
    `asyncio.to_thread` — see `make_citibike_tools` in PR 2.

    `http` is a test-only injection seam for `httpx.MockTransport`-
    wired clients. Production callers leave it None and each
    `fetch_feed` call builds a short-lived client.
    """

    def __init__(
        self,
        saved_stations: list[tuple[str, str]] | None = None,
        *,
        ebike_only: bool = False,
        http: httpx.Client | None = None,
    ) -> None:
        self._saved: tuple[tuple[str, str], ...] = tuple(saved_stations or ())
        self._ebike_only = bool(ebike_only)
        self._http = http

    @property
    def enabled(self) -> bool:
        """True iff at least one station is saved. The tool factory
        gates on this — an empty saved set means no tool is registered."""
        return len(self._saved) > 0

    @property
    def ebike_only(self) -> bool:
        return self._ebike_only

    @property
    def saved_stations(self) -> tuple[tuple[str, str], ...]:
        return self._saved

    @property
    def saved_labels(self) -> tuple[str, ...]:
        return tuple(label for _, label in self._saved)

    def get_status(
        self, *, station_filter: str = "",
    ) -> list[StationStatus]:
        """Return live status for saved stations.

        `station_filter` is a case-insensitive substring match. Both
        the filter and each saved label are normalized via
        `normalize_station_name` before comparison — that lets the
        user (and the LLM) say either form and still match:

            saved "9 Ave & 41 St"      filter "9th avenue"      → match
            saved "9 Ave & 41 St"      filter "9 Ave"           → match
            saved "St James Pl"        filter "saint james"     → match

        We also fall back to raw-substring match so single-token
        filters like "9" or "41" still hit (those tokens survive
        normalization unchanged).

        Empty filter returns every saved station, in insertion order.
        No-match filter returns an empty list — the tool then
        surfaces an LLM-visible "no match" message.

        Per-station `status='missing'` (station retired from GBFS) is
        logged at WARN so the doctor / operator can spot stale config.
        """
        info = fetch_feed(STATION_INFO_URL, INFO_TTL_SECONDS, client=self._http)
        status = fetch_feed(STATION_STATUS_URL, STATUS_TTL_SECONDS, client=self._http)
        info_by_id = {
            s["station_id"]: s
            for s in (info.get("data") or {}).get("stations", [])
            if isinstance(s, dict) and "station_id" in s
        }
        status_by_id = {
            s["station_id"]: s
            for s in (status.get("data") or {}).get("stations", [])
            if isinstance(s, dict) and "station_id" in s
        }
        now = time.time()
        needle_raw = station_filter.strip().casefold()
        needle_norm = normalize_station_name(station_filter).casefold()
        out: list[StationStatus] = []
        for station_id, label in self._saved:
            if needle_raw:
                candidate_raw = label.casefold()
                candidate_norm = normalize_station_name(label).casefold()
                if (
                    needle_raw not in candidate_raw
                    and needle_norm not in candidate_norm
                ):
                    continue
            ss = _station_status_from_feeds(
                station_id, label, info_by_id, status_by_id, now_epoch=now,
            )
            if ss.status == "missing":
                logger.warning(
                    "event=transit.citibike.station_missing "
                    "station_id=%s label=%s",
                    station_id, label,
                )
            out.append(ss)
        logger.info(
            "event=transit.citibike.client.query "
            "filter=%r requested=%d returned=%d",
            station_filter, len(self._saved), len(out),
        )
        return out

    def resolve_label(self, station_id: str) -> str | None:
        """Look up a saved station's label by id (None if not saved)."""
        for sid, label in self._saved:
            if sid == station_id:
                return label
        return None
