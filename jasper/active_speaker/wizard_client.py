# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from .capture_status import SESSION_ENDED_STATUSES
from .movers import MOVER_CONFIRMED
from .poll_backoff import next_poll_s

#: Page that mints the CSRF cookie + meta token pair, and this client's default. A
#: caller POSTing to a DIFFERENT wizard daemon passes that daemon's own page as
#: ``csrf_page_path``.
CSRF_PAGE_PATH = "/sound/speaker/crossover/"
STATUS_PATH = "/sound/speaker/crossover/status"

SESSION_PATH = "/sound/speaker/crossover/v2/session"
APPLY_PATH = "/sound/speaker/crossover/v2/apply"
CAPTURE_CANCEL_PATH = "/sound/speaker/crossover/capture-cancel"

REASON_NO_FINGERPRINT = "no_fingerprint_named"
REASON_NOT_APPLIED = "apply_not_applied"
REASON_ANSWER_LOST = "answer_lost"
REASON_WAIT_TIMEOUT = "wait_timeout"

_CSRF_META_RE = re.compile(r'<meta name="jts-csrf" content="([^"]+)"')


class WizardClient:

    def __init__(
        self,
        *,
        host_header: str | None = None,
        base_url: str = "http://127.0.0.1",
        timeout_s: float = 30.0,
        opener: Any | None = None,
        csrf_page_path: str = CSRF_PAGE_PATH,
    ) -> None:
        """``host_header=None`` sends no explicit Host header: urllib derives
        it from ``base_url`` instead, and the management-host guard accepts
        loopback IPs (``jasper/net/http_security.py``)."""
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

    def open(self, path: str, *, data: bytes | None = None,
             headers: Mapping[str, str] | None = None) -> tuple[int, str]:
        request = urllib.request.Request(
            self._base + path,
            data=data,
            headers={
                **({"Host": self._host} if self._host else {}),
                **(headers or {}),
            },
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
        """The page's token, minted once and reused -- but only once MINTED. A failed mint leaves
        the slot empty so the next POST that needs one retries, instead of caching an
        empty token forever.
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

    def get_json(self, path: str) -> tuple[int, Any]:
        status, body = self.open(path)
        return status, _as_json(body)

    def post_json(self, path: str, payload: Mapping[str, Any]) -> tuple[int, Any]:
        status, body = self.post(path, payload)
        return status, _as_json(body)

    def status_envelope(self) -> tuple[int, dict[str, Any]]:
        """The status GET's code, and the v2 block under it. The CODE separates "nothing known yet"
        from "nothing is answering".
        """
        status, payload = self.get_json(STATUS_PATH)
        block = payload.get("crossover_v2") if isinstance(payload, Mapping) else None
        answer = dict(block) if isinstance(block, Mapping) else {}
        capture = payload.get("capture") if isinstance(payload, Mapping) else None
        if isinstance(capture, Mapping) and str(capture.get("kind") or "").startswith("crossover_v2:"):
            answer["capture"] = dict(capture)
        return status, answer

    def v2_block(self) -> dict[str, Any]:
        """``status["crossover_v2"]`` -- phase, candidate, failure, session id. ``{}`` when
        unreadable or absent; every caller treats that as "nothing known yet", never a
        verdict.
        """
        status, block = self.status_envelope()
        return block if status == 200 else {}

    def open_session(self, plan: Mapping[str, Any]) -> tuple[int, Any]:
        return self.post_json(SESSION_PATH, {"plan": dict(plan)})

    def run_status(self, run_id: str) -> tuple[int, dict[str, Any]]:
        http, block = self.status_envelope()
        capture = block.get("capture") or {}
        if http != 200:
            return http, {"code": REASON_ANSWER_LOST}
        live_id = capture.get("session_id")
        if live_id and live_id != run_id:
            return 409, {"code": "run_not_current", "run_id": run_id, "current_run_id": live_id}
        if not live_id:
            capture = {"status": "starting"}
        progress = capture.get("run") or {}
        return http, {"run_id": run_id, **progress,
                      "status": capture.get("status"),
                      "result": progress.get("status"),
                      "pending": capture.get("position_pending") or capture.get("join"),
                      "current": capture.get("position_current"),
                      "code": progress.get("fault") or capture.get("code"),
                      "faults": progress.get("faults", [])}

    def placed(self, run_id: str, pose: int | None = None) -> tuple[int, Any]:
        from .crossover_v2.position_gate import POSITION_READY_ENDPOINT  # lazy: placement-only measurement imports
        from .crossover_v2.refusal_copy import REASON_WALK_MOVER_MISMATCH  # lazy: placement-only measurement imports

        http, status = self.run_status(run_id)
        if http != 200:
            return http, status
        pending = status.get("pending")
        if not pending:
            return 409, {"code": "position_not_pending", "run_id": run_id}
        if pending.get("mover") != MOVER_CONFIRMED:
            return 409, {"code": REASON_WALK_MOVER_MISMATCH, "run_id": run_id}
        if pose is not None and pose != (status.get("pose") or 1):
            return 409, {"code": "position_mismatch", "run_id": run_id}
        return self.post_json(POSITION_READY_ENDPOINT, {"index": pending["index"],
                              "attempt": pending["attempt"], "run_id": run_id})

    def stop(self, run_id: str) -> tuple[int, Any]:
        from .crossover_v2.refusal_copy import REASON_USER_STOPPED  # lazy: stop-only measurement imports

        http, status = self.run_status(run_id)
        if http != 200:
            return http, status
        if status.get("status") in SESSION_ENDED_STATUSES:
            return 409, {"code": "run_not_live", "run_id": run_id}
        return self.post_json(CAPTURE_CANCEL_PATH, {"reason": REASON_USER_STOPPED})

    def apply(self, expected_fingerprint: str) -> tuple[int, Any]:
        """Post the banked identity; the daemon owns admission."""
        return self.post_json(
            APPLY_PATH, {"expected_candidate_fingerprint": expected_fingerprint}
        )


def _as_json(body: str) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return body


def error_of(payload: Any) -> str | dict[str, Any]:
    """Keep a structured refusal intact; bound legacy prose to one line."""
    if isinstance(payload, Mapping):
        issues = payload.get("issues")
        first = next((row for row in issues if isinstance(row, Mapping)), {}) if isinstance(issues, list) else {}
        issue = payload.get("issue")
        issue = issue if isinstance(issue, Mapping) else {}
        code = payload.get("code") or issue.get("code") or issue.get("id") or first.get("code")
        message = payload.get("error") or issue.get("message") or first.get("message") or payload.get("message")
        if code or "next_action" in payload:
            return {**({"code": code} if code else {}),
                    **({"next_action": payload["next_action"]} if "next_action" in payload else {}),
                    **({"error": message} if message else {})}
        return str(message or payload.get("status") or payload)[:200]
    return str(payload)[:200]


def apply_by_fingerprint(
    client: WizardClient, expected_fingerprint: str
) -> dict[str, Any]:
    named = (expected_fingerprint or "").strip()
    if not named:
        return _blocked(REASON_NO_FINGERPRINT, named, "")
    http, payload = client.apply(named)
    outcome = str(payload.get("status") or "") if isinstance(payload, Mapping) else ""
    applied = http == 200 and outcome == "applied"
    lost = http == 0
    error = error_of(payload)
    code = error.get("code") if isinstance(error, Mapping) else None
    return {
        "status": "applied" if applied else "blocked",
        "refused_by": "" if applied or lost else "wizard",
        "reason": (
            "" if applied else REASON_ANSWER_LOST if lost else code or REASON_NOT_APPLIED
        ),
        "expected_candidate_fingerprint": named,
        "candidate_fingerprint": named,
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
    run_id: str,
    timeout_s: float,
    poll_s: float = 5.0,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    deadline = now() + timeout_s
    previous: dict[str, Any] | None = None
    interval_s = poll_s
    while True:
        http, result = client.run_status(run_id)
        if http != 200:
            return {**result, "status": "lost" if http == 0 else "failed",
                    "reason": result.get("code") or REASON_ANSWER_LOST}
        if on_progress and result != previous:
            on_progress(result)
        if result["status"] in SESSION_ENDED_STATUSES:
            return {**result, "status": "terminal"}
        if now() >= deadline:
            return {**result, "status": "timed_out", "reason": REASON_WAIT_TIMEOUT}
        interval_s = next_poll_s(interval_s, changed=result != previous, initial_s=poll_s)
        previous = result
        sleep(min(interval_s, max(0, deadline - now())))
