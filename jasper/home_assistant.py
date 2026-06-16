"""Home Assistant conversation-API client.

Thin async wrapper around HA's `POST /api/conversation/process` endpoint.
HA owns NLU + entity resolution + automation dispatch; this module is a
relay that hands the user's utterance over and speaks whatever text HA
returns. See `docs/HANDOFF-homeassistant.md` for the architecture choice
(why conversation API, not MCP) and the full setup walkthrough.

Endpoint reference:
  POST {url}/api/conversation/process
  Authorization: Bearer <long-lived-access-token>
  Content-Type: application/json
  Body: {"text": "...", "language": "en", "agent_id"?: "...",
         "conversation_id"?: "..."}

Response shape (exhaustive enums, confirmed against
homeassistant/helpers/intent.py on the dev branch):

  {
    "response": {
      "response_type": "action_done" | "query_answer" | "error",
      "speech": {"plain": {"speech": "<text>"}}  # or "ssml"
      "data": {
        "code"?: "no_intent_match" | "no_valid_targets" |
                 "failed_to_handle" | "unknown",
        "success"?: [{...}],
        "failed"?: [{...}],
      },
      "language": "en",
    },
    "conversation_id": "01JR1HZQS3JVV5CSDMDT7CTX7D",
    "continue_conversation": false,
  }

Footgun: do NOT POST to `/api/services/conversation/process`. That
endpoint returns no response body (HA core issues #93754, #104122 —
still live in 2026). The purpose-built REST endpoint is the one used
here.

`no_valid_targets` is NOT a hard error. In multi-satellite homes,
another device may have answered; speak the returned text regardless
as long as it's non-empty.

Bug-of-the-future: `agent_id` is functional but undocumented in HA's
REST API surface. A future schema-tightening could break us. The
regression test asserts the field is accepted; if HA ever 4xxs on it,
we'll know.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jasper.log_event import log_event

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


# Env var names — single source of truth for the JASPER_HA_* keys.
# Imported by jasper.config, jasper.web.home_assistant_setup, and
# jasper.control.server. Anywhere else hardcoding these strings is a
# refactoring footgun.
ENV_URL = "JASPER_HA_URL"
ENV_TOKEN = "JASPER_HA_TOKEN"
ENV_AGENT_ID = "JASPER_HA_AGENT_ID"
ENV_VERIFY_SSL = "JASPER_HA_VERIFY_SSL"
ENV_RECENT_URLS = "JASPER_HA_RECENT_URLS"

# State-file path the wizard writes and the daemons source via systemd
# EnvironmentFile=. Used directly by read_ha_env_file() + the wizard.
HA_ENV_FILE = "/var/lib/jasper/home_assistant.env"

# Endpoint paths. Joined with the configured base URL.
CONVERSATION_PATH = "/api/conversation/process"
HEALTH_PATH = "/api/"
CONFIG_PATH = "/api/config"
STATES_PATH = "/api/states"

# httpx is imported lazily (inside the timeout helpers below and the
# methods that actually perform I/O), never at module level: this module
# is on `jasper.config`'s import chain for the JASPER_HA_* env-var names
# alone, so a top-level `import httpx` made every process that loads
# config — socket-activated wizards, jasper-doctor, tests in minimal
# envs — pay httpx's import cost (and hard-require it) without ever
# talking to HA. Mirrors the lazy-import pattern in
# jasper/transit/providers/.

# Split timeouts. Connect failures should fail FAST (HA down → model
# speaks "I can't reach Home Assistant"); read failures should be
# patient because LLM-backed HA agents (OpenAI Conversation, Anthropic,
# Google Generative AI inside HA) legitimately take 30-60s for a
# tool-using turn. 90s total is generous but not unbounded. The raw
# seconds are plain floats (not httpx.Timeout objects) so consumers
# like jasper.tools.home_assistant can derive their own budgets from
# the same numbers without importing httpx.
DEFAULT_READ_TIMEOUT_SEC = 90.0
DEFAULT_CONNECT_TIMEOUT_SEC = 3.0

# Cheap health-check timeout — used by the wizard validation cascade.
# Short because GET /api/ should return in <100ms on a healthy HA.
HEALTH_READ_TIMEOUT_SEC = 5.0


def _default_timeout() -> "httpx.Timeout":
    import httpx
    return httpx.Timeout(
        timeout=DEFAULT_READ_TIMEOUT_SEC, connect=DEFAULT_CONNECT_TIMEOUT_SEC,
    )


def _health_timeout() -> "httpx.Timeout":
    import httpx
    return httpx.Timeout(
        timeout=HEALTH_READ_TIMEOUT_SEC, connect=DEFAULT_CONNECT_TIMEOUT_SEC,
    )

# Conversation-ID idle reuse window. HA's empirical TTL is ~5 minutes;
# we use 4 minutes with a safety margin. After this window, drop the
# cached ID and let HA mint a fresh one on the next call.
CONVERSATION_ID_TTL_SEC = 240.0

# Outcome buckets — used in structured log lines so dashboards can
# slice ha.call by category. Six in total, mirroring dubot's split:
#   network       — connection refused, DNS failure, connector error
#   timeout       — explicit asyncio/httpx timeout
#   auth          — 401 from HA (token revoked / invalid)
#   agent_error   — 5xx from HA (broken conversation entity, etc.)
#   intent_miss   — 200 with response_type=error
#   parse_error   — 200 but unexpected body shape
#   ok            — everything else
OUTCOME_OK = "ok"
OUTCOME_NETWORK = "network"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_AUTH = "auth"
OUTCOME_AGENT_ERROR = "agent_error"
OUTCOME_INTENT_MISS = "intent_miss"
OUTCOME_PARSE_ERROR = "parse_error"


@dataclass(frozen=True)
class HAResponse:
    """The parsed result of one `process()` call.

    `success` is true when HA gave us non-empty text to speak, regardless
    of `response_type`. An `action_done` with no speech is still a
    semantic failure; an `error/no_valid_targets` with text ("Sorry, I
    couldn't find that") is still speakable. The voice model speaks
    `speech` verbatim either way.
    """
    speech: str
    success: bool
    response_type: str                  # "action_done" | "query_answer" | "error" | ""
    error_code: str | None              # "no_intent_match" etc., None when not an error
    outcome: str                        # one of OUTCOME_* above
    conversation_id: str | None         # what HA returned (may differ from what we sent)
    continue_conversation: bool         # hint only; HA's heuristic is known-flaky
    targets_success: list[dict[str, Any]] = field(default_factory=list)
    targets_failed: list[dict[str, Any]] = field(default_factory=list)
    latency_ms: int = 0
    error_detail: str = ""              # short human-readable detail for logging

    def as_tool_result(self) -> dict[str, Any]:
        """Shape returned to the voice model. Keep keys minimal — the
        model speaks `spoken_response` or `error_detail` and uses
        `success` to decide tone."""
        return {
            "spoken_response": self.speech,
            "success": self.success,
            "response_type": self.response_type,
            "error_code": self.error_code,
            "error_detail": self.error_detail if not self.success else "",
        }


class HAClient:
    """Persistent async client for one HA instance.

    Owns a long-lived `httpx.AsyncClient` (don't instantiate a fresh
    one per call — TCP+TLS rebuild per turn is the dominant prior-art
    anti-pattern in surveyed Python HA integrations). Reuses HTTP
    keep-alive across calls.

    `conversation_id` lifecycle is opaque to callers: the client
    decides when to send one based on the prior response's
    `continue_conversation` field + a 4-minute idle TTL. Reset
    happens automatically on `continue_conversation=False` or after
    `CONVERSATION_ID_TTL_SEC` of no calls. HA may rotate the ID
    silently; we treat the returned ID as canonical.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        agent_id: str | None = None,
        language: str = "en",
        verify_ssl: bool = True,
        timeout: httpx.Timeout | None = None,
        http: httpx.AsyncClient | None = None,
        clock=None,
    ) -> None:
        # Normalize: strip trailing slash and any trailing /api so callers
        # can paste any of: http://homeassistant.local:8123,
        # http://homeassistant.local:8123/, http://homeassistant.local:8123/api,
        # http://homeassistant.local:8123/api/. Endpoints below assume
        # we hold the bare base URL.
        self._url = url.rstrip("/").removesuffix("/api").rstrip("/")
        self._token = token
        self._agent_id = agent_id.strip() if agent_id else ""
        self._language = language.strip() or "en"
        self._verify_ssl = verify_ssl
        self._timeout = timeout or _default_timeout()
        self._http: httpx.AsyncClient | None = http
        self._owns_http = http is None
        self._clock = clock or time.monotonic

        # conversation_id state. _conv_id_until is a monotonic deadline;
        # when now > deadline, drop the cached ID.
        self._conv_id: str | None = None
        self._conv_id_until: float = 0.0

    @property
    def url(self) -> str:
        return self._url

    @property
    def agent_id(self) -> str | None:
        return self._agent_id or None

    @property
    def conversation_id(self) -> str | None:
        """Current cached conversation_id — useful for the `/state`
        aggregator and `/system/` dashboard card. May be None if no
        call has happened yet, or if the TTL has expired."""
        if self._conv_id and self._clock() < self._conv_id_until:
            return self._conv_id
        return None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            import httpx  # lazy — see module-level comment
            self._http = httpx.AsyncClient(
                timeout=self._timeout,
                verify=self._verify_ssl,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _headers(self) -> dict[str, str]:
        # Used when a caller passed in its own AsyncClient (tests). The
        # owned client carries the auth header on the session.
        return {"Authorization": f"Bearer {self._token}"}

    def _build_body(self, query: str) -> dict[str, Any]:
        body: dict[str, Any] = {"text": query, "language": self._language}
        if self._agent_id:
            body["agent_id"] = self._agent_id
        # Reuse conversation_id only within the idle TTL window. HA mints
        # a fresh one if we omit, which is what we want after expiry.
        if self._conv_id and self._clock() < self._conv_id_until:
            body["conversation_id"] = self._conv_id
        return body

    async def process(self, query: str) -> HAResponse:
        """Send a natural-language query to HA and return the parsed
        response. Single try; no retry — `/api/conversation/process` is
        not idempotent (a retried 'turn off the lights' could double-fire
        a script). Six-bucket error categorization via `HAResponse.outcome`.
        """
        import httpx  # lazy — see module-level comment

        query = (query or "").strip()
        if not query:
            return self._error(OUTCOME_PARSE_ERROR, "empty query", started=0.0)

        body = self._build_body(query)
        started = self._clock()
        client = await self._client()
        try:
            resp = await client.post(
                self._url + CONVERSATION_PATH,
                json=body,
                headers=self._headers(),
            )
        except httpx.TimeoutException as e:
            log_event(
                logger,
                "ha.call",
                outcome="timeout",
                query_len=len(query),
                detail=repr(str(e)),
                level=logging.WARNING,
            )
            return self._error(OUTCOME_TIMEOUT, "HA did not respond in time", started)
        except httpx.HTTPError as e:
            # ConnectError, NetworkError, ConnectTimeout-as-network, etc.
            log_event(
                logger,
                "ha.call",
                outcome="network",
                query_len=len(query),
                detail=repr(str(e)),
                level=logging.WARNING,
            )
            return self._error(OUTCOME_NETWORK, str(e)[:200], started)

        latency_ms = int((self._clock() - started) * 1000)

        if resp.status_code == 401:
            log_event(
                logger,
                "ha.call",
                outcome="auth",
                status=401,
                latency_ms=latency_ms,
                level=logging.WARNING,
            )
            return self._error(
                OUTCOME_AUTH,
                "Home Assistant token rejected — reconnect at the speaker's setup page",
                started, latency_ms,
            )
        if resp.status_code >= 500:
            text = (resp.text or "")[:200]
            log_event(
                logger,
                "ha.call",
                outcome="agent_error",
                status=resp.status_code,
                latency_ms=latency_ms,
                body=text,
                level=logging.WARNING,
            )
            return self._error(
                OUTCOME_AGENT_ERROR,
                f"Home Assistant returned an internal error ({resp.status_code})",
                started, latency_ms,
            )
        if resp.status_code != 200:
            text = (resp.text or "")[:200]
            log_event(
                logger,
                "ha.call",
                outcome="parse_error",
                status=resp.status_code,
                latency_ms=latency_ms,
                body=text,
                level=logging.WARNING,
            )
            return self._error(
                OUTCOME_PARSE_ERROR,
                f"unexpected status {resp.status_code}",
                started, latency_ms,
            )

        try:
            data = resp.json()
        except ValueError as e:
            log_event(
                logger,
                "ha.call",
                outcome="parse_error",
                detail="json_decode",
                err=repr(str(e)),
                level=logging.WARNING,
            )
            return self._error(OUTCOME_PARSE_ERROR, "could not decode HA response", started, latency_ms)

        return self._parse(data, latency_ms)

    def _parse(self, data: dict[str, Any], latency_ms: int) -> HAResponse:
        response = data.get("response") or {}
        rtype = str(response.get("response_type") or "")
        speech_obj = response.get("speech") or {}
        # Prefer plain over ssml; both have the same {"speech": "..."} shape.
        plain = speech_obj.get("plain") or {}
        ssml = speech_obj.get("ssml") or {}
        speech_text = str(plain.get("speech") or ssml.get("speech") or "").strip()

        data_block = response.get("data") or {}
        error_code = data_block.get("code")
        targets_success = list(data_block.get("success") or [])
        targets_failed = list(data_block.get("failed") or [])

        conv_id = data.get("conversation_id")
        continue_conv = bool(data.get("continue_conversation"))

        # Update the cached conversation_id from HA's response. HA may
        # rotate it silently; treat what comes back as canonical. Reset
        # the cache when HA signals the conversation is done.
        if continue_conv and conv_id:
            self._conv_id = conv_id
            self._conv_id_until = self._clock() + CONVERSATION_ID_TTL_SEC
        else:
            self._conv_id = None
            self._conv_id_until = 0.0

        # Outcome bucket (for log slicing) is orthogonal to `success`
        # (for the model's behaviour). HA may return response_type=error
        # WITH a useful speech string ("I couldn't find a device called
        # 'living room TV' in the bedroom") — that text is worth speaking
        # verbatim, and `success` reflects "did HA give us text to
        # speak". The outcome bucket still flags intent_miss vs ok so
        # `jasper-trace.sh | grep ha\\.call` can slice for forensics.
        if rtype == "error":
            outcome = OUTCOME_INTENT_MISS
        elif speech_text:
            outcome = OUTCOME_OK
        else:
            outcome = OUTCOME_PARSE_ERROR
        success = bool(speech_text)

        log_event(
            logger,
            "ha.call",
            **{
                "outcome": outcome,
                "response_type": rtype or "-",
                "error_code": error_code or "-",
                "speech_len": len(speech_text),
                "latency_ms": latency_ms,
                "conv_id": (conv_id or "-")[:12],
                "continue": continue_conv,
                "targets_success": len(targets_success),
                "targets_failed": len(targets_failed),
            },
        )

        return HAResponse(
            speech=speech_text,
            success=success,
            response_type=rtype,
            error_code=error_code,
            outcome=outcome,
            conversation_id=conv_id,
            continue_conversation=continue_conv,
            targets_success=targets_success,
            targets_failed=targets_failed,
            latency_ms=latency_ms,
            error_detail="" if success else (
                "Home Assistant said it couldn't find anything matching that"
                if error_code == "no_intent_match" else
                "Home Assistant couldn't handle that request"
                if error_code in ("failed_to_handle", "unknown") else
                "Home Assistant returned no response"
            ),
        )

    def _error(
        self,
        outcome: str,
        detail: str,
        started: float,
        latency_ms: int | None = None,
    ) -> HAResponse:
        if latency_ms is None:
            latency_ms = int((self._clock() - started) * 1000) if started else 0
        # User-facing text for each outcome. Provider-agnostic per
        # CLAUDE.md — no mention of Gemini/OpenAI/Grok.
        speech_text = {
            OUTCOME_NETWORK: "I can't reach Home Assistant right now.",
            OUTCOME_TIMEOUT: "Home Assistant didn't respond in time.",
            OUTCOME_AUTH: "I'm not authorized to control Home Assistant. "
                          "Please reconnect at the speaker's setup page.",
            OUTCOME_AGENT_ERROR: "Home Assistant had an internal error.",
            OUTCOME_PARSE_ERROR: "Home Assistant returned a response I couldn't understand.",
        }.get(outcome, "Something went wrong with Home Assistant.")
        return HAResponse(
            speech=speech_text,
            success=False,
            response_type="",
            error_code=None,
            outcome=outcome,
            conversation_id=None,
            continue_conversation=False,
            latency_ms=latency_ms,
            error_detail=detail,
        )

    # ---- Helpers used by the wizard (PR 2) and the doctor ------------------

    async def healthcheck(self) -> bool:
        """Cheap probe of `GET /api/` — returns True if HA responds 200
        with the expected body. Used by the wizard's verify step and
        `jasper-doctor` (skip-if-not-configured). Does NOT touch the
        conversation endpoint — that would cost money on LLM-backed
        HA agents."""
        import httpx  # lazy — see module-level comment

        client = await self._client()
        try:
            resp = await client.get(
                self._url + HEALTH_PATH,
                headers=self._headers(),
                timeout=_health_timeout(),
            )
        except httpx.HTTPError as e:
            logger.debug("ha healthcheck: %r", e)
            return False
        if resp.status_code != 200:
            return False
        try:
            return resp.json().get("message") == "API running."
        except ValueError:
            return False

    async def config(self) -> dict[str, Any] | None:
        """GET /api/config — used by the wizard to display location_name +
        version after a successful connect. Returns None on any error."""
        import httpx  # lazy — see module-level comment

        client = await self._client()
        try:
            resp = await client.get(
                self._url + CONFIG_PATH,
                headers=self._headers(),
                timeout=_health_timeout(),
            )
            if resp.status_code != 200:
                return None
            return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("ha config: %r", e)
            return None

    async def list_agents(self) -> list[dict[str, str]]:
        """Enumerate HA conversation agents via `GET /api/states` filtered
        to entity_id starting with `conversation.`. REST-only (avoids the
        WebSocket auth dance that `conversation/agent/list` would
        require). Returns a list of {"entity_id", "name"} dicts."""
        import httpx  # lazy — see module-level comment

        client = await self._client()
        try:
            resp = await client.get(
                self._url + STATES_PATH,
                headers=self._headers(),
                timeout=_health_timeout(),
            )
            if resp.status_code != 200:
                return []
            states = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("ha list_agents: %r", e)
            return []

        agents: list[dict[str, str]] = []
        for s in states or []:
            entity_id = str(s.get("entity_id") or "")
            if not entity_id.startswith("conversation."):
                continue
            attrs = s.get("attributes") or {}
            name = str(attrs.get("friendly_name") or entity_id.split(".", 1)[-1])
            agents.append({"entity_id": entity_id, "name": name})
        return agents


def build_ha_client(cfg) -> HAClient | None:
    """Construct an HAClient from a `Config` instance, or return None when
    HA is not configured. Mirrors the gating pattern of the bus/subway
    factories — None means the model never sees the tool."""
    if not cfg.ha_enabled:
        return None
    return HAClient(
        url=cfg.ha_url,
        token=cfg.ha_token,
        agent_id=cfg.ha_agent_id or None,
        verify_ssl=bool(getattr(cfg, "ha_verify_ssl", True)),
    )


# ---- probe_status: cached one-shot probe ----------------------------------
#
# probe_status is consumed by jasper-control's /state aggregator,
# /system/snapshot, and jasper-doctor. The dashboard polls
# /system/snapshot every 5 seconds while it's open, which means without
# caching, an unreachable HA would block each poll for up to 5 seconds
# (_health_timeout) — making the dashboard unusable when HA is down AND
# burning ~12 wasted RPM against a dead URL. We cache the probe result
# with a TTL of PROBE_CACHE_TTL_SEC. Doctor passes force=True to bypass
# the cache so its output reflects ground truth at invocation time.
#
# The cache is module-global and process-local. That's fine because:
#  - jasper-control is a single process; one cache per process.
#  - jasper-voice also calls probe_status indirectly (via HAClient) for
#    its own purposes, but probe_status is only invoked from the
#    control daemon — voice owns its own HAClient lifecycle separately.
#
# Cache key is (url, token). When the wizard updates the env, the key
# changes and the next probe is uncached.

# Cache for probe_status. Aligned to how often HA reachability actually
# changes in a household (reboots, network blips): 15 s is plenty fresh
# for a dashboard polled every 5 s, and limits a dead HA to one probe
# per 15 s rather than one per 5.
PROBE_CACHE_TTL_SEC = 15.0

# (deadline_monotonic, url, token, verify_ssl, result_dict). None = no
# cached value. verify_ssl is in the key because toggling the wizard's
# self-signed checkbox can change reachability without changing url or
# token — without it in the key, a "broken with strict TLS" cache entry
# would shadow a "working with relaxed TLS" probe for up to 15 s after
# the user fixes the config.
_probe_cache: tuple[float, str, str, bool, dict[str, Any]] | None = None

# Last (configured, connected) tuple — used to log state transitions.
# Module-global so a single jasper-control process sees the same
# transition history across /state and /system/snapshot calls.
_last_state: tuple[bool, bool] | None = None


def _reset_cache_for_tests() -> None:
    """Wipe module state. Tests should call this between scenarios so
    cached responses don't leak across them."""
    global _probe_cache, _last_state
    _probe_cache = None
    _last_state = None


async def probe_status(
    url: str, token: str, *, force: bool = False, verify_ssl: bool = True,
) -> dict[str, Any]:
    """One-shot reachability + version probe of an HA instance.

    Used by jasper-control's /state aggregator, the /system/ dashboard
    card, and jasper-doctor — none of which need the full HAClient
    lifecycle (no conversation_id, no per-call structured logging).
    Returns a dict the caller ships directly as JSON.

    Results are cached process-globally for PROBE_CACHE_TTL_SEC seconds
    keyed by (url, token). Pass `force=True` to bypass the cache when
    fresh ground truth matters (jasper-doctor does this).

    Logs `event=ha.unreachable` and `event=ha.reachable` on state
    transitions — one log line per change, not per call.

    Result shape:
      {
        "configured":   bool,           # url AND token both present
        "connected":    bool,           # GET /api/ returned 200 + sigil
        "url":          str,            # what we probed (normalized)
        "instance_name": str | None,    # from /api/config.location_name
        "version":      str | None,     # from /api/config.version
        "error":        str | None,     # short human-readable detail
      }
    """
    global _probe_cache, _last_state

    now = time.monotonic()
    if not force and _probe_cache is not None:
        deadline, cached_url, cached_token, cached_verify, cached_result = _probe_cache
        if (
            now < deadline
            and cached_url == url
            and cached_token == token
            and cached_verify == verify_ssl
        ):
            return cached_result

    result = await _probe_uncached(url, token, verify_ssl=verify_ssl)

    if not force:
        _probe_cache = (now + PROBE_CACHE_TTL_SEC, url, token, verify_ssl, result)

    # Emit one log line per (configured, connected) state transition.
    # Avoids per-poll noise — the dashboard polls every 5 s, doctor runs
    # ad-hoc; we only want to know "when did HA go down" / "when did
    # it come back". Logged after the cache write so reads during a
    # transition still see the new result.
    new_state = (bool(result.get("configured")), bool(result.get("connected")))
    if _last_state is None:
        # First-ever probe: log the initial state for forensics, but only
        # when configured (no point logging "unconfigured, untouched").
        if new_state[0]:
            if new_state[1]:
                log_event(
                    logger,
                    "ha.reachable",
                    url=result.get("url") or url,
                    instance=result.get("instance_name") or "?",
                    version=result.get("version") or "?",
                )
            else:
                log_event(
                    logger,
                    "ha.unreachable",
                    url=result.get("url") or url,
                    error=result.get("error") or "unknown",
                    level=logging.WARNING,
                )
    elif new_state != _last_state:
        if new_state[0] and new_state[1]:
            log_event(
                logger,
                "ha.reachable",
                url=result.get("url") or url,
                instance=result.get("instance_name") or "?",
                version=result.get("version") or "?",
            )
        elif new_state[0] and not new_state[1]:
            log_event(
                logger,
                "ha.unreachable",
                url=result.get("url") or url,
                error=result.get("error") or "unknown",
                level=logging.WARNING,
            )
        elif not new_state[0]:
            log_event(logger, "ha.unconfigured")
    _last_state = new_state

    return result


def read_ha_env_file(path: str = HA_ENV_FILE) -> dict[str, str]:
    """Parse the wizard-written env file into a dict. Returns {} if the
    file is missing or unreadable.

    The wizard writes this file (mode 0640 group jasper — WS1 Phase 3b-2)
    on every save/disconnect.
    Systemd-managed daemons that source it via `EnvironmentFile=` only
    see updates across process restarts — so any consumer that needs
    to reflect wizard changes immediately (jasper-control's /state
    aggregator and /system/snapshot endpoints) must read this file
    directly rather than `os.environ`. See `probe_status_from_env`.
    """
    out: dict[str, str] = {}
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug("ha: read_ha_env_file(%s): %r", path, e)
    return out


async def probe_status_from_env(
    *, env_file_path: str = HA_ENV_FILE, force: bool = False,
) -> dict[str, Any]:
    """Probe HA reachability using the URL/token/verify_ssl values from
    the wizard-written env file directly (not `os.environ`).

    Used by jasper-control's `/state` aggregator and `/system/snapshot`
    section. Critical that this reads the file fresh on each call —
    jasper-control's `os.environ` is a snapshot from process start, so
    it wouldn't reflect a wizard save until jasper-control restarts.
    The wizard only restarts jasper-voice (the consumer that owns the
    HAClient), not jasper-control. Reading the file every call is the
    cheap fix.

    Cache TTL still applies (jasper-control polls every 5 s, cache
    holds for 15 s) so this stays sub-millisecond on the hot path.
    """
    state = read_ha_env_file(env_file_path)
    return await probe_status(
        state.get(ENV_URL, "").strip(),
        state.get(ENV_TOKEN, "").strip(),
        force=force,
        verify_ssl=state.get(ENV_VERIFY_SSL, "1").strip() not in ("0", "false", "no"),
    )


async def _probe_uncached(
    url: str, token: str, *, verify_ssl: bool = True,
) -> dict[str, Any]:
    """The real probe. Separate from probe_status() so the cache wrapper
    stays thin and the inner logic stays testable in isolation."""
    if not url or not token:
        return {
            "configured": False, "connected": False, "url": url,
            "instance_name": None, "version": None,
            "error": None,
        }
    client = HAClient(url=url, token=token, verify_ssl=verify_ssl)
    try:
        if not await client.healthcheck():
            return {
                "configured": True, "connected": False, "url": client.url,
                "instance_name": None, "version": None,
                "error": "Couldn't reach Home Assistant — check the URL and token.",
            }
        cfg = await client.config()
        return {
            "configured": True, "connected": True, "url": client.url,
            "instance_name": (cfg or {}).get("location_name") or "Home Assistant",
            "version": (cfg or {}).get("version"),
            "error": None,
        }
    finally:
        await client.aclose()
