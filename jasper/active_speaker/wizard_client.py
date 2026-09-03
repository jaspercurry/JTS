# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The correction wizard's transport, and the two round verbs built on it.

One HTTP client for every caller of the crossover wizard (arm walk, jasper-basic-profile, jasper-round, scripts/run-crossover-round.py), so the Host-header and CSRF rules have one owner instead of drifting copies. ``apply_by_fingerprint`` and ``wait_for_round`` sit on top for the same reason -- the apply guard and bounded wait are BEHAVIOUR the laptop runner and the on-box CLI must agree about exactly. Neither sequences anything; the wizard's own artifact-dependency refusals do that.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

#: Page that mints the CSRF cookie + meta token pair, and this client's default. A caller POSTing to a DIFFERENT wizard daemon passes that daemon's own page as ``csrf_page_path``.
CSRF_PAGE_PATH = "/sound/crossover/"
STATUS_PATH = "/correction/crossover/status"

#: The two stage-opening POSTs and the apply; pinned against ``correction_setup``'s ``_POST_ROUTES`` by ``tests/test_cli_round.py``.
SESSION_PATH = "/correction/crossover/v2/session"
VERIFY_PATH = "/correction/crossover/v2/verify"
APPLY_PATH = "/correction/crossover/v2/apply"

#: Verify open's discriminator, restated (not imported, to stay numpy-free on a 1 GB speaker) from ``correction_crossover_v2``; pinned by ``tests/test_cli_round.py``.
STAGE_KEY = "stage"
STAGE_MEASURE = "measure"
STAGE_POST_APPLY = "post_apply"

#: Measurement tiers a measuring stage open may name. Restated, not imported from :mod:`crossover_v2_flow` (pulls numpy at import time); pinned equal to ``crossover_v2_flow.TIERS`` by ``tests/test_cli_round.py``.
TIERS = ("express", "full", "remote")

#: Phases a stage is still WORKING in: every capture phase plus ``closing`` and ``applying`` (session mid-flight, not a stopping point). Restated, not derived from ``.crossover_v2.journey`` (numpy import cost); pinned against it by ``tests/test_cli_round.py``.
RUNNING_PHASES = frozenset(
    {
        "check",
        "measure",
        "lateral",
        "cloud_measure",
        "entry_baseline",
        "verify",
        "cloud_verify",
        "closing",
        "applying",
    }
)

#: Why a round verb refused, as a slug a script can branch on. First four are this client's own pre-flight refusals (nothing sent); last three are the wizard's answer, the answer that never arrived, and the clock's.
REASON_NO_FINGERPRINT = "no_fingerprint_named"
REASON_NO_V2_STATE = "no_v2_state"
REASON_NO_CANDIDATE = "no_candidate_published"
REASON_FINGERPRINT_MISMATCH = "fingerprint_mismatch"
REASON_NOT_APPLIED = "apply_not_applied"
REASON_SESSION_FAILED = "session_failed"
REASON_ANSWER_LOST = "answer_lost"
REASON_WAIT_TIMEOUT = "wait_timeout"

_CSRF_META_RE = re.compile(r'<meta name="jts-csrf" content="([^"]+)"')


class WizardClient:
    """The correction wizard over HTTP: the speaker's Host, and CSRF handled.

    The transport every caller of that wizard needs, so none owns a second copy: an explicit ``Host:`` header (the management-host guard rejects ``127.0.0.1`` and any OTHER speaker's name), and the double-submit CSRF pair on every mutating POST (cookie from the jar, token from ``<meta name="jts-csrf">``) -- ``csrf_page_path`` names which of nginx's several wizard daemons mints it.

    :meth:`open`/:meth:`post` hand back TEXT, not parsed bodies; :meth:`get_json`/:meth:`post_json` are the round's JSON half, and :meth:`v2_block`, :meth:`open_session`, :meth:`apply` are the three verbs a round is made of.
    """

    def __init__(
        self,
        *,
        host_header: str,
        base_url: str = "http://127.0.0.1",
        timeout_s: float = 30.0,
        opener: Any | None = None,
        csrf_page_path: str = CSRF_PAGE_PATH,
    ) -> None:
        self._host = host_header
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s
        self._csrf_page = csrf_page_path
        if opener is None:
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar)
            )
        self._opener = opener
        self._csrf: str | None = None

    # -- transport ---------------------------------------------------------- #

    def open(self, path: str, *, data: bytes | None = None,
             headers: Mapping[str, str] | None = None) -> tuple[int, str]:
        request = urllib.request.Request(
            self._base + path,
            data=data,
            headers={"Host": self._host, **(headers or {})},
            method="POST" if data is not None else "GET",
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                return int(response.status), response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return int(exc.code), exc.read().decode("utf-8", "replace")
        except (OSError, ValueError) as exc:
            return 0, f"{type(exc).__name__}: {exc}"

    def _csrf_token(self) -> str:
        """The page's token, minted once and reused -- but only once MINTED. A failed mint leaves the slot empty so the next POST that needs one retries, instead of caching an empty token forever."""
        if self._csrf:
            return self._csrf
        _, body = self.open(self._csrf_page)
        match = _CSRF_META_RE.search(body)
        self._csrf = match.group(1) if match else None
        return self._csrf or ""

    def post(self, path: str, payload: Mapping[str, Any]) -> tuple[int, str]:
        """One JSON POST, carrying the double-submit pair."""
        return self.open(
            path,
            data=json.dumps(dict(payload)).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": self._csrf_token(),
            },
        )

    # -- the round's three verbs -------------------------------------------- #

    def get_json(self, path: str) -> tuple[int, Any]:
        status, body = self.open(path)
        return status, _as_json(body)

    def post_json(self, path: str, payload: Mapping[str, Any]) -> tuple[int, Any]:
        status, body = self.post(path, payload)
        return status, _as_json(body)

    def status_envelope(self) -> tuple[int, dict[str, Any]]:
        """The status GET's code, and the v2 block under it. The CODE separates "nothing known yet" from "nothing is answering"."""
        status, payload = self.get_json(STATUS_PATH)
        block = payload.get("crossover_v2") if isinstance(payload, Mapping) else None
        return status, dict(block) if isinstance(block, Mapping) else {}

    def v2_block(self) -> dict[str, Any]:
        """``status["crossover_v2"]`` -- phase, candidate, failure, session id. ``{}`` when unreadable or absent; every caller treats that as "nothing known yet", never a verdict."""
        status, block = self.status_envelope()
        return block if status == 200 else {}

    def open_session(
        self, tier: str, *, stage: str = STAGE_MEASURE
    ) -> tuple[int, Any]:
        """Open the measuring session, or the post-apply verify. ``tier`` is ignored for verify: stage 2 reads the instrument the MEASURING session already recorded."""
        if stage == STAGE_POST_APPLY:
            return self.post_json(VERIFY_PATH, {STAGE_KEY: STAGE_POST_APPLY})
        return self.post_json(SESSION_PATH, {"tier": tier})

    def apply(self, expected_fingerprint: str) -> tuple[int, Any]:
        """The bare POST. The gate is :func:`apply_by_fingerprint`, not this. No inline ``candidate`` override sent -- the host reopens the artifact from the recorded evidence bundle."""
        return self.post_json(
            APPLY_PATH, {"expected_candidate_fingerprint": expected_fingerprint}
        )


def _as_json(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return body


def error_of(payload: Any) -> str:
    """The wizard's own words for a refusal, bounded to one readable line."""
    if isinstance(payload, Mapping):
        return str(payload.get("error") or payload.get("status") or payload)[:200]
    return str(payload)[:200]


def _live_fingerprint(block: Mapping[str, Any]) -> str:
    candidate = block.get("candidate")
    return (
        str(candidate.get("fingerprint") or "")
        if isinstance(candidate, Mapping)
        else ""
    )


def apply_by_fingerprint(
    client: WizardClient, expected_fingerprint: str
) -> dict[str, Any]:
    """The gate, then the apply. A mismatch POSTs nothing at all.

    The endpoint runs the same comparison server-side, so this is not a second opinion -- what it adds is that nothing is SENT on a mismatch. An EMPTY argument is refused first and separately, since an absent live candidate also reads as ``""`` and would otherwise compare equal.

    Success is HTTP 200 **and** ``status == "applied"``: ``apply_failed`` is always 200 with an unchanged graph, ``blocked`` is the one status that moves the code off 200, and a refusal is a 400 with no ``status``. A LOST answer (http 0) is neither and carries no ``refused_by`` -- nothing refused, a connection just dropped.
    """
    named = (expected_fingerprint or "").strip()
    if not named:
        return _blocked(REASON_NO_FINGERPRINT, named, "")
    block = client.v2_block()
    live = _live_fingerprint(block)
    if live != named:
        # Empty block vs published-but-different candidate: same refusal,
        # different diagnoses (the first is usually an unreadable envelope).
        return _blocked(
            REASON_FINGERPRINT_MISMATCH
            if live
            else REASON_NO_CANDIDATE if block else REASON_NO_V2_STATE,
            named,
            live,
        )
    http, payload = client.apply(named)
    outcome = str(payload.get("status") or "") if isinstance(payload, Mapping) else ""
    applied = http == 200 and outcome == "applied"
    lost = http == 0
    return {
        "status": "applied" if applied else "blocked",
        "refused_by": "" if applied or lost else "wizard",
        "reason": (
            "" if applied else REASON_ANSWER_LOST if lost else REASON_NOT_APPLIED
        ),
        "expected_candidate_fingerprint": named,
        "candidate_fingerprint": live,
        "http": http,
        "outcome": outcome,
        "payload": payload,
    }


def _blocked(reason: str, named: str, live: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "refused_by": "client",
        "reason": reason,
        "expected_candidate_fingerprint": named,
        "candidate_fingerprint": live,
        "http": 0,
        "outcome": "",
        "payload": None,
    }


def wait_for_round(
    client: WizardClient,
    *,
    prior_session_id: str = "",
    timeout_s: float,
    poll_s: float,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll until this session's phases are all accepted, or it fails.

    Terminal requires BOTH: the session id must have MOVED off ``prior_session_id`` (else a previous round's terminal phase reads as this round's completion at the moment the poll started), and the phase must leave :data:`RUNNING_PHASES`. A caller with no prior id (a separate invocation) passes ``""`` and waits on whatever session is current. A status read that is not answered ends the wait at once, rather than burning the full timeout indistinguishable from a slow round.
    """
    deadline = now() + timeout_s
    while True:
        http, block = client.status_envelope()
        if http != 200:
            return {"status": "lost", "reason": REASON_ANSWER_LOST,
                    "phase": "", "session_id": "",
                    "candidate_fingerprint": "", "failure": None}
        phase = str(block.get("phase") or "")
        session_id = str(block.get("session_id") or "")
        failure = block.get("failure")
        if failure:
            return {"status": "failed", "reason": REASON_SESSION_FAILED,
                    "phase": phase, "session_id": session_id,
                    "candidate_fingerprint": _live_fingerprint(block),
                    "failure": failure}
        if (
            session_id
            and session_id != prior_session_id
            and phase not in RUNNING_PHASES
        ):
            return {"status": "terminal", "reason": "", "phase": phase,
                    "session_id": session_id,
                    "candidate_fingerprint": _live_fingerprint(block),
                    "failure": None}
        if now() >= deadline:
            return {"status": "timed_out", "reason": REASON_WAIT_TIMEOUT,
                    "phase": phase, "session_id": session_id,
                    "candidate_fingerprint": "", "failure": None}
        sleep(poll_s)
