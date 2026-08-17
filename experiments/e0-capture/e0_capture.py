#!/usr/bin/env python3
"""E0: a standalone "phone" for the JTS crossover-measurement v2 conductor.

Stands in for the browser capture page (capture-page/js/main.js) so a
measurement mic (UMIK-2) plugged into this Mac can drive the REAL Pi-side
conductor flow (CHECK -> MEASURE -> VERIFY) without a phone/browser.

Protocol details, file:line citations, and known simplifications are in
PROTOCOL.md (same directory). Read that first if something here looks
surprising.

HARD BOUNDARY: this file's --start-session/--tap-link path talks to a live
Pi and the live relay (relay.jasper.tech) and will make the Pi play audio.
It must only be invoked by a human hardware operator coordinating a live
run -- never automatically. --selftest never touches the network.

Usage:
    e0_capture.py --selftest
    e0_capture.py --tap-link 'https://capture.jasper.tech/#s=...&u=...&k=...&a=...' \\
        --placement tweeter --run 1
    e0_capture.py --start-session --tier remote --host jts3.local \\
        --placement tweeter --run 1

Revived 2026-08-16 from captures/xover-e0-2026-07-21/drive-tooling (issue
#2636), which is preserved unmodified as the July session artifact. Two
changes: the capture_relay import path is now self-locating instead of
hardcoded to a deleted worktree, and the session mint sends an explicit
`{"tier": ...}` so it can open the REMOTE tier (the July build could only
ever mint FULL). --tap-link, the conductor's usual path, is unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import types
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# --------------------------------------------------------------------------
# Load the repo's capture_relay crypto/integrity modules VERBATIM, by file
# path. They are stdlib + `cryptography` only (no numpy), so this avoids
# pulling in numpy via `jasper/__init__`. See PROTOCOL.md #9.
# --------------------------------------------------------------------------

# Self-locating: this file lives at <repo>/captures/<session-dir>/e0_capture.py,
# so parents[2] is the repo root. The July original hardcoded an absolute path
# to a throwaway agent worktree, which was later deleted and broke import
# (issue #2636). Deriving the path from __file__ cannot rot that way.
# `E0_CAPTURE_RELAY_DIR` overrides it when reading from another checkout.
WT = Path(
    os.environ.get("E0_CAPTURE_RELAY_DIR")
    or Path(__file__).resolve().parents[2] / "jasper" / "capture_relay"
)


def _load_repo_module(name: str) -> types.ModuleType:
    path = WT / f"{name}.py"
    if not path.is_file():
        raise SystemExit(
            f"capture_relay module not found: {path}\n"
            "Expected this script to live at <repo>/captures/<dir>/e0_capture.py.\n"
            "Set E0_CAPTURE_RELAY_DIR to a checkout's jasper/capture_relay to override."
        )
    spec = importlib.util.spec_from_file_location(f"cr_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


crypto = _load_repo_module("crypto")
integ = _load_repo_module("integrity")


# --------------------------------------------------------------------------
# Constants (PROTOCOL.md sections cited inline)
# --------------------------------------------------------------------------

# Mirrors capture-page/version.json at origin/main 40d117229 (2026-08-16).
# `validate_capture_page` (jasper/capture_relay/session.py:812-825) checks
# schema_version == 1, that BOTH the spec's protocol and ours are members of
# the supported list, that every member is a non-bool int, and that the build
# matches [0-9]{8}\.[0-9]+ -- shape and membership only, never an exact build
# string, so this does not have to track the Cloudflare Pages deploy.
#
# Refreshed from the July original's [1, 2, 3] / "20260720.1": protocols 1
# and 2 were DELETED, not carried (jasper/capture_relay/spec.py:50-58, owner
# ruling 2026-07-27). [1, 2, 3] still passed the membership check, so this is
# honesty rather than a repair -- but advertising support for protocols that
# no longer exist is exactly the claim a future tightening would refuse.
DEFAULT_CAPTURE_PAGE_IDENTITY = {
    "schema_version": 1,
    "capture_protocol_version": 3,
    "supported_capture_protocol_versions": [3],
    "capture_page_build": "20260815.5",
}

REQUIRED_SAMPLE_RATE_HZ = 48000  # jasper/capture_relay/spec.py:79
REQUIRED_CHANNELS = 1  # jasper/capture_relay/spec.py:80
REQUIRED_BITS = 16  # measurement-audio.js float32ToWavBlob, PROTOCOL.md #10

# Commission tiers, mirroring TIERS / DEFAULT_TIER in
# jasper/active_speaker/crossover_v2_flow.py:1081-1096. Held here only so a
# typo fails on this Mac instead of after a round-trip to the Pi; the server
# re-validates through `normalize_tier` (:1326) and refuses an unknown one
# before any relay registration.
TIER_FULL = "full"
TIER_EXPRESS = "express"
TIER_REMOTE = "remote"
TIERS = (TIER_FULL, TIER_EXPRESS, TIER_REMOTE)

CSRF_COOKIE_NAME = "jts_csrf"  # jasper/web/_common.py:109
CAPTURE_DEFERRED_RETRY_POLL_S = 1.5  # main.js:1397 (CAPTURE_DEFERRED_RETRY_POLL_MS)
DEFAULT_PROGRESS_POLL_S = 0.25  # main.js progress_poll_ms fallback (250ms)

_log_file = None  # set by main() when --log is passed


def _log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, file=sys.stderr, flush=True)
    if _log_file is not None:
        _log_file.write(line + "\n")
        _log_file.flush()


# --------------------------------------------------------------------------
# Fragment parsing (mirrors capture-page/js/fragment.js:23-44)
# --------------------------------------------------------------------------


class FragmentError(ValueError):
    pass


@dataclass(frozen=True)
class CaptureHandle:
    session_id: str
    upload_token: str
    content_key_b64: str
    spec_mac: str


def parse_tap_link(tap_link: str) -> CaptureHandle:
    """Parse a `https://host/#s=..&u=..&k=..&a=..` tap link (or bare fragment)."""
    fragment = tap_link.split("#", 1)[1] if "#" in tap_link else tap_link
    params = urllib.parse.parse_qs(fragment, keep_blank_values=True)

    def _one(key: str) -> str:
        values = params.get(key) or [""]
        return values[0]

    session_id = _one("s")
    upload_token = _one("u")
    content_key_b64 = _one("k")
    spec_mac = _one("a")
    if not session_id or not upload_token or not content_key_b64:
        raise FragmentError(
            "This measurement link is incomplete or expired. "
            "Start again from the speaker."
        )
    return CaptureHandle(
        session_id=session_id,
        upload_token=upload_token,
        content_key_b64=content_key_b64,
        spec_mac=spec_mac,
    )


# --------------------------------------------------------------------------
# Relay phone client (mirrors capture-page/js/relay-client.js)
# --------------------------------------------------------------------------


class RelayError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class RelayPhoneClient:
    """The "phone" side of the relay contract. See PROTOCOL.md #4, #6."""

    def __init__(
        self,
        *,
        base_url: str,
        session_id: str,
        upload_token: str,
        content_key: bytes,
        capture_page_identity: dict[str, Any] | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id = session_id
        self.upload_token = upload_token
        self.content_key = content_key
        self.capture_page_identity = capture_page_identity or DEFAULT_CAPTURE_PAGE_IDENTITY
        self.timeout_s = timeout_s
        self._sequence = 0
        self._http = requests.Session()

    def _url(self, suffix: str) -> str:
        return f"{self.base_url}/sessions/{urllib.parse.quote(self.session_id, safe='')}{suffix}"

    def _auth_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.upload_token}"}
        if extra:
            headers.update(extra)
        return headers

    def _raise_for_status(self, resp: requests.Response) -> None:
        if resp.ok:
            return
        try:
            body = resp.json()
        except ValueError:
            body = None
        message = (body or {}).get("error") if isinstance(body, dict) else None
        raise RelayError(message or f"relay {resp.status_code}", resp.status_code, body)

    def fetch_spec_text(self) -> str:
        """GET /spec -- relay-client.js:122-129."""
        resp = self._http.get(self._url("/spec"), headers=self._auth_headers(), timeout=self.timeout_s)
        self._raise_for_status(resp)
        return resp.text

    def post_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """POST /event, always authenticated -- PROTOCOL.md #6.

        Merges `capture_page` identity into the payload BEFORE authenticating
        it, exactly like relay-client.js:140-151.
        """
        payload = {**event, "capture_page": self.capture_page_identity}
        self._sequence += 1
        body = integ.authenticated_phone_event(
            self.content_key, self.session_id, payload, sequence=self._sequence
        )
        resp = self._http.post(
            self._url("/event"),
            headers=self._auth_headers({"Content-Type": "application/json"}),
            data=json.dumps(body),
            timeout=self.timeout_s,
        )
        self._raise_for_status(resp)
        return resp.json()

    def fetch_phone_status(self) -> dict[str, Any]:
        """GET /phone-status -- relay-client.js:162-173. Returns {host_event:{...}}."""
        resp = self._http.get(
            self._url("/phone-status"), headers=self._auth_headers(), timeout=self.timeout_s
        )
        self._raise_for_status(resp)
        return resp.json()

    def put_blob(self, blob: bytes, plaintext_len: int, sha256_hex: str, capture_index: int) -> dict[str, Any]:
        """PUT /blob?index=N -- relay-client.js:175-198."""
        path = f"/blob?index={capture_index}" if capture_index >= 0 else "/blob"
        resp = self._http.put(
            self._url(path),
            headers=self._auth_headers(
                {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(blob)),
                    "X-Plaintext-Length": str(plaintext_len),
                    "X-Plaintext-Sha256": sha256_hex,
                }
            ),
            data=blob,
            timeout=self.timeout_s,
        )
        self._raise_for_status(resp)
        return resp.json()


# --------------------------------------------------------------------------
# Encryption (mirrors capture-page/js/crypto.js:57-70; PROTOCOL.md #9)
# --------------------------------------------------------------------------


def encrypt_wav(content_key: bytes, wav_bytes: bytes) -> tuple[bytes, int, str]:
    iv = os.urandom(12)
    ciphertext = AESGCM(content_key).encrypt(iv, wav_bytes, None)
    blob = iv + ciphertext
    return blob, len(wav_bytes), hashlib.sha256(wav_bytes).hexdigest()


# --------------------------------------------------------------------------
# Session start over HTTPS to the Pi (PROTOCOL.md #2)
# --------------------------------------------------------------------------


def session_start_body(tier: str) -> str:
    """The JSON body for POST /correction/crossover/v2/session.

    `prepare_v2_session` reads `raw.get("tier")` and resolves it through
    `resolve_plan_shape` (jasper/web/correction_crossover_v2.py:7351); an
    absent tier means DEFAULT_TIER == "full"
    (jasper/active_speaker/crossover_v2_flow.py:1096). The remote tier -- the
    externally-positioned walk this tool drives -- is reachable ONLY by
    posting it explicitly, because the wizard's chooser never offers it
    (crossover_v2_flow.py:1089-1093). The July original hardcoded "{}" and so
    could only ever mint a FULL-tier session.
    """
    return json.dumps({"tier": tier}) if tier else "{}"


def start_session(
    host: str, *, tier: str = TIER_REMOTE, verify_tls: bool = False, timeout_s: float = 15.0
) -> str:
    """GET /correction/ for CSRF, POST /correction/crossover/v2/session.

    Returns the tap_link. NEVER call this from an automated/background
    context -- it makes the Pi start a live measurement session (it does
    not itself play audio, but the resulting session does once a phone
    arms it).
    """
    base = f"https://{host}"
    session = requests.Session()
    headers = {"Host": host}

    get_resp = session.get(f"{base}/correction/", headers=headers, verify=verify_tls, timeout=timeout_s)
    get_resp.raise_for_status()
    csrf_token = session.cookies.get(CSRF_COOKIE_NAME)
    if not csrf_token:
        raise RuntimeError(
            f"GET /correction/ did not set the {CSRF_COOKIE_NAME} cookie -- "
            "cannot authenticate the session-start POST"
        )

    body = session_start_body(tier)
    _log(f"POST /correction/crossover/v2/session body={body}")
    post_resp = session.post(
        f"{base}/correction/crossover/v2/session",
        headers={**headers, "Content-Type": "application/json", "X-CSRF-Token": csrf_token},
        data=body,
        verify=verify_tls,
        timeout=timeout_s,
    )
    if not post_resp.ok:
        try:
            detail = post_resp.json()
        except ValueError:
            detail = post_resp.text
        raise RuntimeError(f"session-start failed ({post_resp.status_code}): {detail}")
    payload = post_resp.json()
    tap_link = ((payload or {}).get("relay") or {}).get("tap_link")
    if not tap_link:
        raise RuntimeError(f"session-start response had no relay.tap_link: {payload!r}")
    return tap_link


def reset_journey(host: str, *, verify_tls: bool = False, timeout_s: float = 20.0) -> None:
    """POST /correction/crossover/reset (scoped) so each E0 run starts from a
    clean, un-rebound conductor state. Keeps topology/design_draft/baseline;
    clears the journey (measurements/preview/staged config). NO audio."""
    base = f"https://{host}"
    s = requests.Session()
    hdr = {"Host": host}
    s.get(f"{base}/correction/", headers=hdr, verify=verify_tls, timeout=timeout_s)
    csrf = s.cookies.get(CSRF_COOKIE_NAME) or ""
    r = s.post(
        f"{base}/correction/crossover/reset",
        headers={**hdr, "Content-Type": "application/json", "X-CSRF-Token": csrf},
        data="{}",
        verify=verify_tls,
        timeout=timeout_s,
    )
    r.raise_for_status()
    _log("journey reset (scoped): topology/baseline kept, run state cleared")


def cancel_relay_capture(host: str, *, verify_tls: bool = False, timeout_s: float = 20.0) -> None:
    """POST /correction/crossover/relay-cancel: stop any in-progress relay
    capture so a fresh session isn't blocked by 'already in progress'
    (gracefully no-ops when nothing is active). NO audio."""
    base = f"https://{host}"
    s = requests.Session()
    hdr = {"Host": host}
    s.get(f"{base}/correction/", headers=hdr, verify=verify_tls, timeout=timeout_s)
    csrf = s.cookies.get(CSRF_COOKIE_NAME) or ""
    try:
        r = s.post(
            f"{base}/correction/crossover/relay-cancel",
            headers={**hdr, "Content-Type": "application/json", "X-CSRF-Token": csrf},
            data="{}",
            verify=verify_tls,
            timeout=timeout_s,
        )
        _log(f"relay-cancel -> HTTP {r.status_code}")
    except requests.RequestException as exc:
        _log(f"relay-cancel best-effort failed (continuing): {exc}")


def start_session_with_retry(
    host: str, *, tier: str = TIER_REMOTE, attempts: int = 5, backoff_s: float = 6.0
) -> str:
    """start_session, retrying a transient relay register 500 (observed under
    rapid session churn). The register step plays NO audio, so retry is free."""
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return start_session(host, tier=tier)
        except Exception as exc:  # noqa: BLE001 -- retry any session-start failure
            msg = str(exc)
            transient = "register failed: 500" in msg or "500" in msg
            _log(f"session-start attempt {i}/{attempts} failed: {msg[:120]}")
            last = exc
            if not transient or i == attempts:
                raise
            time.sleep(backoff_s)
    assert last is not None
    raise last


# --------------------------------------------------------------------------
# Spec parsing helpers (raw dict access -- PROTOCOL.md #5; no need to load
# jasper.capture_relay.spec's typed dataclasses for this)
# --------------------------------------------------------------------------


def poll_interval_s(spec: dict[str, Any]) -> float:
    raw = spec.get("progress_poll_ms")
    try:
        ms = float(raw) if raw else 250.0
    except (TypeError, ValueError):
        ms = 250.0
    return max(0.1, min(1.0, ms / 1000.0))


def entry_for_wire_index(spec: dict[str, Any], wire_index: int) -> dict[str, Any] | None:
    """1-based wire index -> 0-based capture_plan.entries[] lookup. PROTOCOL.md #5."""
    plan = spec.get("capture_plan") or {}
    for entry in plan.get("entries") or []:
        if int(entry.get("index", -1)) == wire_index - 1:
            return entry
    return None


def setup_wire_payload(spec: dict[str, Any]) -> dict[str, Any]:
    """Mirrors setupWirePayload()+applyDefaultCalibrationHintSilently. PROTOCOL.md #11.

    Re-checked against origin/main 40d117229 on 2026-08-16. The calibration
    sub-object below is byte-identical to what the live page now builds
    (capture-page/js/main.js:925-933, gated by validDefaultSetupHint at
    :902-908 on the same mode/resolvable/calibration_id triple).

    ONE deliberate divergence: `setupWirePayload()` (main.js:154-157) returns
    the whole `setupState`, which also carries `total_positions: 5`
    (main.js:104-107) -- a room-sweep default that means nothing for a
    remote-tier crossover walk. It is omitted here because no consumer reads
    it: the inbound event is extracted with no schema
    (jasper/capture_relay/session.py:744) and every reader takes only
    `setup["calibration"]` (correction_setup.py:3582, 3703, 3729;
    correction_crossover_v2.py:3872). Sending a fabricated 5 would be less
    honest than sending nothing.

    The page's other two `setupWirePayload()` branches are unreachable here:
    `{binding: ...}` needs a `setup_binding_id` (setup-store.js:38-45) and the
    v2 flow never sets one, and `null` needs `kind == "room_sweep"`
    (main.js:4638), while this flow is `crossover_sweep`.
    """
    hint = (spec.get("default_setup") or {}).get("calibration")
    if (
        isinstance(hint, dict)
        and hint.get("mode") in ("serial", "upload")
        and hint.get("resolvable") is True
        and hint.get("calibration_id")
    ):
        return {
            "calibration": {
                "mode": "stored",
                "calibration_id": str(hint["calibration_id"]),
                "model": str(hint.get("model") or ""),
            }
        }
    return {"calibration": {"mode": "none"}}


def acknowledgement_payload(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Mirrors acceptedAcknowledgement (render.js:162-174). PROTOCOL.md #11."""
    ack = spec.get("acknowledgement")
    if not isinstance(ack, dict):
        return None
    return {
        "schema_version": 1,
        "id": str(ack.get("id") or ""),
        "binding_id": str(ack.get("binding_id") or ""),
        "accepted": True,
    }


# --------------------------------------------------------------------------
# WAV recording via sox (PROTOCOL.md #10)
# --------------------------------------------------------------------------


def record_wav(path: Path, *, seconds: float, mic: str, rate: int = REQUIRED_SAMPLE_RATE_HZ) -> None:
    """Blocking sox record -- used by the self-test's optional hardware smoke
    check (test_e0_capture.py) and any ad-hoc/manual recording. The live plan
    loop (drive_one_capture) does NOT call this: it needs a non-blocking
    subprocess.Popen so it can post the `armed` event while sox is already
    recording, matching the page's recorder.start()-before-armed ordering.
    """
    cmd = [
        "sox",
        "-t", "coreaudio", mic,
        "-r", str(rate),
        "-b", str(REQUIRED_BITS),
        "-c", str(REQUIRED_CHANNELS),
        "-e", "signed-integer",
        str(path),
        "trim", "0", f"{seconds:.3f}",
    ]
    _log(f"sox recording {seconds:.2f}s -> {path.name} (mic={mic!r})")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 15.0)
    if proc.returncode != 0:
        raise RuntimeError(f"sox failed ({proc.returncode}): {proc.stderr.strip()}")


def wav_format_ok(path: Path, *, expect_rate: int = REQUIRED_SAMPLE_RATE_HZ) -> tuple[bool, str]:
    """Best-effort format check via the stdlib `wave` module (no numpy needed)."""
    import wave

    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
    ok = rate == expect_rate and channels == REQUIRED_CHANNELS and sampwidth == REQUIRED_BITS // 8
    detail = f"rate={rate} channels={channels} sampwidth_bytes={sampwidth}"
    return ok, detail


# --------------------------------------------------------------------------
# The plan-driving loop (mirrors runPlanCapture/beginAndAwaitAuthorization/
# waitForCaptureAuthorized/waitForCaptureResult, main.js:1738-2159).
# PROTOCOL.md #7, #8.
# --------------------------------------------------------------------------


@dataclass
class PhaseOutcome:
    index: int
    attempt: int
    phase_label: str
    accepted: bool
    code: str
    error: str
    attempts_used: int
    wav_path: str | None
    duration_s: float
    terminal: bool
    terminal_reason: str


class SessionDead(RuntimeError):
    pass


def _wait_for_authorization(
    client: RelayPhoneClient, spec: dict[str, Any], index: int, attempt: int, *, deadline_s: float = 20.0
) -> dict[str, Any]:
    """Poll /phone-status until capture_authorized/refused/deferred/exhausted.

    Mirrors waitForCaptureAuthorized (main.js:1738-1822).
    """
    poll_s = poll_interval_s(spec)
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        status = client.fetch_phone_status()
        event = status.get("host_event") or {}
        phase = event.get("phase")
        if phase == "capture_incompatible":
            raise SessionDead(event.get("error") or "capture page is incompatible with this speaker")
        if phase == "capture_authorized" and int(event.get("index", -1)) == index and int(
            event.get("attempt", -1)
        ) == attempt:
            return {"authorized": True}
        if phase == "capture_refused":
            return {"refused": True, "code": event.get("code", ""), "error": event.get("error", "")}
        if phase == "capture_set_exhausted":
            return {"session_over": True, "reason": event.get("reason") or event.get("error", "")}
        if (
            phase == "capture_deferred"
            and int(event.get("index", -1)) == index
            and int(event.get("attempt", -1)) == attempt
        ):
            return {"deferred": True, "code": event.get("code", ""), "reason": event.get("error", "")}
        time.sleep(poll_s)
    raise TimeoutError("no admission response before the timeout")


def _wait_for_sweep_progress(client: RelayPhoneClient, spec: dict[str, Any], *, deadline_s: float) -> None:
    """Best-effort progress print while the local recording runs. Informational
    only -- it does not control when the recording stops (PROTOCOL.md #12.1).
    """
    poll_s = poll_interval_s(spec)
    deadline = time.monotonic() + deadline_s
    last_phase = None
    while time.monotonic() < deadline:
        try:
            status = client.fetch_phone_status()
        except RelayError:
            return
        phase = (status.get("host_event") or {}).get("phase")
        if phase and phase != last_phase:
            last_phase = phase
            _log(f"  host phase: {phase}")
            if phase in ("sweep_complete", "sweep_cancelled", "sweep_failed"):
                return
        time.sleep(poll_s)


def _wait_for_result(
    client: RelayPhoneClient, spec: dict[str, Any], index: int, attempt: int, *, deadline_s: float
) -> dict[str, Any]:
    """Poll /phone-status for capture_result/set_complete/set_exhausted.

    Mirrors waitForCaptureResult (main.js:1830-1892).
    """
    poll_s = poll_interval_s(spec)
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        status = client.fetch_phone_status()
        event = status.get("host_event") or {}
        phase = event.get("phase")
        if phase == "capture_incompatible":
            raise SessionDead(event.get("error") or "capture page is incompatible with this speaker")
        if phase == "capture_set_complete":
            return {"set_complete": True, "accepted": int(event.get("accepted") or 0)}
        if phase == "capture_set_exhausted":
            return {
                "set_exhausted": True,
                "accepted": int(event.get("accepted") or 0),
                "attempts": int(event.get("attempts") or attempt),
            }
        if (
            phase == "capture_result"
            and int(event.get("index", -1)) == index
            and int(event.get("attempt", -1)) == attempt
        ):
            return {"accepted": event.get("accepted") is True, "error": event.get("error", "")}
        time.sleep(poll_s)
    raise TimeoutError("no result response before the timeout")


def drive_one_capture(
    client: RelayPhoneClient,
    spec: dict[str, Any],
    *,
    index: int,
    attempt: int,
    target: int,
    mic: str,
    record_margin_s: float,
    outdir: Path,
    placement: str,
    run: int,
) -> PhaseOutcome:
    entry = entry_for_wire_index(spec, index)
    kind_label = (entry or {}).get("kind_label") or f"index{index}"
    setup = setup_wire_payload(spec)
    ack = acknowledgement_payload(spec)

    # --- admission loop (deferred = re-post identical begin_capture) ---
    while True:
        _log(f"begin_capture index={index} attempt={attempt} ({kind_label})")
        event: dict[str, Any] = {"begin_capture": {"index": index, "attempt": attempt}, "setup": setup}
        client.post_event(event)
        admission = _wait_for_authorization(client, spec, index, attempt)
        if admission.get("refused"):
            return PhaseOutcome(
                index=index, attempt=attempt, phase_label=kind_label, accepted=False,
                code=admission.get("code", ""), error=admission.get("error", ""),
                attempts_used=attempt, wav_path=None, duration_s=0.0,
                terminal=True, terminal_reason="capture_refused",
            )
        if admission.get("session_over"):
            return PhaseOutcome(
                index=index, attempt=attempt, phase_label=kind_label, accepted=False,
                code="", error=admission.get("reason", ""), attempts_used=attempt,
                wav_path=None, duration_s=0.0, terminal=True, terminal_reason="capture_set_exhausted",
            )
        if admission.get("deferred"):
            _log(f"  deferred: {admission.get('code')} -- {admission.get('reason')}")
            time.sleep(CAPTURE_DEFERRED_RETRY_POLL_S)
            continue
        break  # authorized

    # --- record ---
    entry_duration_s = (entry or {}).get("duration_ms", spec.get("duration_ms", 20000)) / 1000.0
    spec_duration_s = spec.get("duration_ms", 20000) / 1000.0
    post_roll_s = spec.get("post_roll_ms", 0) / 1000.0
    # Record each phase for ITS OWN planned duration + margin, not the larger
    # spec-level window — the old max(..., spec_duration_s) over-recorded ~37s
    # every phase and pushed the cumulative session past the relay TTL right at
    # VERIFY (the last phase, after apply). entry_duration covers the phase's
    # sweep+gaps; spec_duration_s kept only as a floor for a malformed entry.
    record_s = max(entry_duration_s, 8.0) + post_roll_s + record_margin_s

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    wav_path = outdir / f"{ts}_{placement}_run{run}_{kind_label}.wav"
    outdir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [
            "sox", "-t", "coreaudio", mic,
            "-r", str(REQUIRED_SAMPLE_RATE_HZ), "-b", str(REQUIRED_BITS),
            "-c", str(REQUIRED_CHANNELS), "-e", "signed-integer",
            str(wav_path), "trim", "0", f"{record_s:.3f}",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(0.15)  # let the recording pipeline actually start before "armed"

    armed_event = {
        "armed": True,
        "degraded": False,
        "device": {"label": mic},
        "begin_capture": {"index": index, "attempt": attempt},
        "setup": setup,
    }
    if ack is not None:
        armed_event["acknowledgement"] = ack
    client.post_event(armed_event)
    _log(f"  armed, recording {record_s:.2f}s")

    _wait_for_sweep_progress(client, spec, deadline_s=record_s + 2.0)
    try:
        _, stderr = proc.communicate(timeout=record_s + 10.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"sox recording failed ({proc.returncode}): {stderr.strip()}")

    wav_bytes = wav_path.read_bytes()
    blob, plaintext_len, sha256_hex = encrypt_wav(client.content_key, wav_bytes)
    client.put_blob(blob, plaintext_len, sha256_hex, attempt - 1)
    _log(f"  uploaded blob index={attempt - 1} ({len(wav_bytes)} bytes plaintext)")

    # MEASURE's 5-fc sweep took ~59 s on jts3 2026-08-16; the recording length cannot predict analysis time.
    result_deadline = max(120.0, spec.get("duration_ms", 20000) / 1000.0)
    verdict = _wait_for_result(client, spec, index, attempt, deadline_s=result_deadline)

    if verdict.get("set_complete"):
        return PhaseOutcome(
            index=index, attempt=attempt, phase_label=kind_label, accepted=True, code="",
            error="", attempts_used=attempt, wav_path=str(wav_path), duration_s=record_s,
            terminal=True, terminal_reason="capture_set_complete",
        )
    if verdict.get("set_exhausted"):
        return PhaseOutcome(
            index=index, attempt=attempt, phase_label=kind_label, accepted=False, code="",
            error="attempt budget exhausted", attempts_used=verdict.get("attempts", attempt),
            wav_path=str(wav_path), duration_s=record_s, terminal=True,
            terminal_reason="capture_set_exhausted",
        )
    accepted = bool(verdict.get("accepted"))
    terminal = accepted and index >= target
    return PhaseOutcome(
        index=index, attempt=attempt, phase_label=kind_label, accepted=accepted, code="",
        error=verdict.get("error", ""), attempts_used=attempt, wav_path=str(wav_path),
        duration_s=record_s, terminal=terminal,
        terminal_reason="capture_set_complete" if terminal else "",
    )


def drive_session(
    *,
    tap_link: str,
    mic: str,
    record_margin_s: float,
    outdir: Path,
    placement: str,
    run: int,
) -> dict[str, Any]:
    handle = parse_tap_link(tap_link)
    content_key = crypto.content_key_from_b64url(handle.content_key_b64)

    # Relay base is the tap_link's own origin (capture.jasper.tech normally
    # redirects here; the phone talks to the RELAY, not the capture-page
    # origin -- PROTOCOL.md #1, #4). RELAY_BASE is a fixed deploy constant
    # in the real page (capture-page/js/config.js:10); mirror that here
    # rather than trying to derive it from the tap_link's origin, which is
    # the capture PAGE's origin, not the relay's.
    relay_base = "https://relay.jasper.tech"

    client = RelayPhoneClient(
        base_url=relay_base,
        session_id=handle.session_id,
        upload_token=handle.upload_token,
        content_key=content_key,
    )

    spec_text = client.fetch_spec_text()
    if handle.spec_mac:
        integ.verify_capture_spec_mac(content_key, handle.session_id, spec_text, handle.spec_mac)
    spec = json.loads(spec_text)

    plan = spec.get("capture_plan") or {}
    target = max(1, int(plan.get("capture_target", 1)))

    outcomes: list[PhaseOutcome] = []
    index, attempt = 1, 1
    while True:
        outcome = drive_one_capture(
            client, spec, index=index, attempt=attempt, target=target, mic=mic,
            record_margin_s=record_margin_s, outdir=outdir, placement=placement, run=run,
        )
        outcomes.append(outcome)
        if outcome.terminal:
            break
        if outcome.accepted:
            index += 1
            attempt += 1
        else:
            attempt += 1

    summary = {
        "tap_link": tap_link,
        "placement": placement,
        "run": run,
        "capture_target": target,
        "outcome": outcomes[-1].terminal_reason if outcomes else "no_captures",
        "phases": [
            {
                "phase": o.phase_label,
                "index": o.index,
                "accepted": o.accepted,
                "code": o.code,
                "error": o.error,
                "attempts": o.attempts_used,
                "wav_path": o.wav_path,
                "duration_s": round(o.duration_s, 2),
            }
            for o in outcomes
        ],
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary_path = outdir / f"{ts}_{placement}_run{run}_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    _log(f"summary written to {summary_path}")
    return summary


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--selftest", action="store_true", help="run offline self-tests and exit")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--start-session", action="store_true", help="POST /correction/crossover/v2/session")
    mode.add_argument("--tap-link", help="use a pre-minted tap link instead of starting a session")
    p.add_argument("--host", default="jts3.local", help="Pi hostname (default: jts3.local)")
    p.add_argument(
        "--tier",
        choices=TIERS,
        default=TIER_REMOTE,
        help="commission tier to mint with --start-session (default: remote -- the "
        "externally-positioned walk this tool drives). Ignored with --tap-link, "
        "where the tier was fixed when the conductor minted the session.",
    )
    p.add_argument("--placement", default="placement", help="label for output filenames")
    p.add_argument("--run", type=int, default=1, help="run number for output filenames")
    p.add_argument("--outdir", default=None, help="output dir (default: <scratchpad>/e0-corpus/<placement>)")
    p.add_argument("--mic", default="UMIK-2", help="sox -t coreaudio device name")
    p.add_argument("--record-margin-sec", type=float, default=2.0, help="extra recording tail/head margin")
    p.add_argument("--reset-first", action="store_true", help="scoped /crossover/reset before start-session (avoids session rebind)")
    p.add_argument("--log", default=None, help="also write logs to this file")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.log:
        global _log_file  # noqa: PLW0603
        _log_file = open(args.log, "a")  # noqa: SIM115 -- lives for process lifetime

    if args.selftest:
        import test_e0_capture

        return 0 if test_e0_capture.run_all() else 1

    if not args.start_session and not args.tap_link:
        print("Nothing to do: pass --selftest, --start-session, or --tap-link.", file=sys.stderr)
        return 2

    outdir = Path(args.outdir) if args.outdir else Path(__file__).parent / "e0-corpus" / args.placement

    try:
        if args.start_session:
            if args.reset_first:
                cancel_relay_capture(args.host)
                time.sleep(1.0)
                reset_journey(args.host)
                time.sleep(1.5)
            _log(f"starting session on {args.host} (tier={args.tier})")
            tap_link = start_session_with_retry(args.host, tier=args.tier)
            _log(f"tap_link: {tap_link}")
        else:
            tap_link = args.tap_link
            _log("using a pre-minted tap link (--tier not consulted)")

        summary = drive_session(
            tap_link=tap_link,
            mic=args.mic,
            record_margin_s=args.record_margin_sec,
            outdir=outdir,
            placement=args.placement,
            run=args.run,
        )
        print(json.dumps(summary, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 -- top-level CLI: report where it broke
        _log(f"FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
