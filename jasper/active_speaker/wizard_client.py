# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The correction wizard's transport, and the two round verbs built on it.

One HTTP client for every caller of the crossover wizard -- the arm walk on the
speaker (:mod:`jasper.active_speaker.arm_walk`), ``jasper-basic-profile``,
``jasper-round`` and the laptop's ``scripts/run-crossover-round.py``. It lives
here rather than in any one of them because a second copy of the Host-header
and CSRF rules would drift from the first, silently, and be discovered as a
403 mid-round.

Two round verbs sit on top of it, for the same reason: the apply guard and the
bounded wait are BEHAVIOUR the laptop runner and the on-box CLI must agree
about exactly. ``apply_by_fingerprint`` is the gate that compares the
operator's fingerprint against the live candidate before anything is sent;
``wait_for_round`` is the bounded poll that says when a stage has stopped.
Neither sequences anything -- the wizard's own artifact-dependency refusals do
that.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

#: The page that mints the correction backend's CSRF cookie + meta token pair,
#: and this client's default. A caller POSTing to a DIFFERENT wizard daemon
#: passes that daemon's own page as ``csrf_page_path`` -- see the constructor.
CSRF_PAGE_PATH = "/sound/crossover/"
STATUS_PATH = "/correction/crossover/status"

#: The two stage-opening POSTs and the apply. Members of ``correction_setup``'s
#: ``_POST_ROUTES`` under the wizard's ``/correction`` prefix -- pinned against
#: that frozenset by ``tests/test_cli_round.py`` so a renamed route fails here
#: instead of 404ing mid-round.
SESSION_PATH = "/correction/crossover/v2/session"
VERIFY_PATH = "/correction/crossover/v2/verify"
APPLY_PATH = "/correction/crossover/v2/apply"

#: The verify open's discriminator, in the wizard host's own words
#: (``correction_crossover_v2.VERIFY_STAGE_KEY`` / ``VERIFY_STAGE_POST_APPLY``,
#: pinned against them by ``tests/test_cli_round.py``). Restated rather than
#: imported because that module's import pulls the whole web layer, which the
#: arm walk would then pay for on a 1 GB speaker to read two strings.
STAGE_KEY = "stage"
STAGE_MEASURE = "measure"
STAGE_POST_APPLY = "post_apply"

#: The phases a stage is still WORKING in -- every capture phase, plus the two
#: control-page phases that are a session mid-flight rather than a stopping
#: point: ``closing`` (the measuring session's own tail while the fit runs)
#: and ``applying`` (the machine-paced window between MEASURE-accepted and
#: apply-observed) are exactly the moments a poller must keep waiting
#: through, and treating either as terminal banks a round mid-flight.
#: Restated rather than derived from :mod:`.crossover_v2.journey` because
#: importing that package pulls its ``contracts`` re-export, and with it
#: numpy, onto a round CLI's import path for the sake of reading phase
#: strings. Pinned against ``journey`` -- the complement (``{review, done}``)
#: and ``CAPTURE_PHASES`` as a subset both -- by ``tests/test_cli_round.py``.
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

#: Why a round verb refused, or could not say, as a slug a script can branch
#: on. The first four are this client's own pre-flight refusals -- nothing was
#: sent; the last three are the wizard's answer, the answer that never arrived,
#: and the clock's.
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

    The transport every caller of that wizard needs and none of them should own
    a second copy of. Two rules live here:

    * **An explicit** ``Host:`` **header**, the shape the deploy's
      management-surface probe and ``jasper-doctor``'s
      ``check_management_surface`` already use. It is required rather than
      stylistic: the management-host guard rejects a request whose Host is
      ``127.0.0.1``, and it equally rejects one carrying a DIFFERENT speaker's
      name -- so this header is the caller's claim about which speaker it
      believes it is talking to.
    * **The double-submit CSRF pair** on every mutating POST -- the cookie from
      the jar and the token from the page's ``<meta name="jts-csrf">`` --
      exactly as a browser's ``fetch()`` does. ``csrf_page_path`` names which
      page mints it, because nginx fronts SEVERAL wizard daemons on one host
      (``/sound/crossover/`` is jasper-correction-web, ``/sound/setup/`` is
      jasper-web) and a caller should mint from the daemon it is about to POST
      to rather than rely on the two keeping one token scheme.

    :meth:`open` and :meth:`post` hand back TEXT, not parsed bodies: the
    position gate builds a typed Poll out of one
    (:class:`~jasper.active_speaker.arm_walk.LoopbackSession`) and a round
    wants JSON, and a client that guessed would make one of them unwrap the
    guess. :meth:`get_json` / :meth:`post_json` are the round's half of that,
    and :meth:`v2_block`, :meth:`open_session` and :meth:`apply` are the three
    verbs a round is made of.
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
        """The page's token, minted once and reused -- but only once MINTED.

        A failed first mint (the wizard still starting, a blipped connection)
        used to cache ``""`` forever, so every later POST in the run carried an
        empty token and was refused 403 with nothing saying why. Only a real
        token is cached; a failure leaves the slot empty and the next POST that
        needs one tries again.
        """
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
        """The status GET's code, and the v2 block under it.

        The CODE is what separates "nothing known yet" from "nothing is
        answering": a poller that cannot see it waits its whole timeout out
        against a wizard that is down or on another host.
        """
        status, payload = self.get_json(STATUS_PATH)
        block = payload.get("crossover_v2") if isinstance(payload, Mapping) else None
        return status, dict(block) if isinstance(block, Mapping) else {}

    def v2_block(self) -> dict[str, Any]:
        """``status["crossover_v2"]`` -- phase, candidate, failure, session id.

        ``{}`` when the envelope is unreadable or carries no v2 block, which
        every caller treats as "nothing known yet" rather than as a verdict.
        """
        status, block = self.status_envelope()
        return block if status == 200 else {}

    def open_session(
        self, tier: str, *, stage: str = STAGE_MEASURE
    ) -> tuple[int, Any]:
        """Open the measuring session, or the post-apply verify.

        The verify body carries no tier and this one is ignored for it: stage 2
        reads the instrument the MEASURING session recorded in durable state,
        so the household's one choice at the tier chooser governs both stages.
        A tier posted here would be a second, later answer to a settled
        question.
        """
        if stage == STAGE_POST_APPLY:
            return self.post_json(VERIFY_PATH, {STAGE_KEY: STAGE_POST_APPLY})
        return self.post_json(SESSION_PATH, {"tier": tier})

    def apply(self, expected_fingerprint: str) -> tuple[int, Any]:
        """The bare POST. The gate is :func:`apply_by_fingerprint`, not this.

        The endpoint also takes an inline ``candidate`` override, which nothing
        here sends: the host reopens the artifact from the evidence bundle
        recorded at publish, so the fingerprint is the whole request.
        """
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

    **The endpoint runs the same comparison server-side** and would refuse the
    same request, so this is not a second opinion about the candidate. What it
    adds is that nothing is SENT: a mistyped or stale fingerprint ends at the
    caller rather than becoming a state-changing request against a speaker, and
    both values ride the receipt where an operator can read them instead of
    arriving as one sentence inside a 400. An EMPTY argument is refused first
    and separately, because an absent live candidate also reads as ``""`` and
    would otherwise compare equal and POST.

    Success is HTTP 200 **and** ``status == "applied"``, because neither half
    decides alone (the route's own dispatch): ``apply_failed`` is always 200
    with a graph that did not change, ``blocked`` is the one status that moves
    the code off 200, and a refusal is a 400 carrying no ``status`` at all.

    A LOST answer (http 0) is neither: the route has no try/except around its
    own answer, so a connection dropped after the graph loaded looks exactly
    like one dropped before it. It carries no ``refused_by`` because nothing
    refused -- reporting the wizard as the refuser there would be a claim about
    a speaker nobody heard from.
    """
    named = (expected_fingerprint or "").strip()
    if not named:
        return _blocked(REASON_NO_FINGERPRINT, named, "")
    block = client.v2_block()
    live = _live_fingerprint(block)
    if live != named:
        # An empty block and a published-but-different candidate are the same
        # refusal and DIFFERENT diagnoses: the first is usually an unreadable
        # envelope (wrong host, wizard down), and reporting it as "no candidate
        # yet" would send an operator to measure again over a broken link.
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

    Two conditions, and the first one is why a sleep was never enough: the
    session id must have MOVED off ``prior_session_id``, or a previous round's
    terminal phase would read as this round's completion the moment the poll
    started. Then the phase must leave :data:`RUNNING_PHASES`, which is the
    flow's own answer to "is every phase this session ran accepted".

    A caller that never saw the prior id (a separate invocation, which is what
    ``jasper-round wait`` is) passes ``""`` and waits on whatever session the
    wizard is currently publishing.

    A status read that is not answered ends the wait at once. Polling a dead
    daemon, or a wrong ``--hostname``, is indistinguishable from a slow round
    if the code is thrown away -- and waiting the full timeout out to then
    report "still going" sends an operator back to the speaker to watch a round
    that was never running.
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
