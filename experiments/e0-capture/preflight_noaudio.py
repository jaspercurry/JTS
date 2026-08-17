#!/usr/bin/env python3
"""E0 no-audio pre-flight: validate session-start + spec fetch + MAC verify
WITHOUT posting begin_capture/armed (so the Pi plays NO audio), then recover
the session volume so nothing is left dangling.

Run with the e0venv:  e0venv/bin/python preflight_noaudio.py --host jts3.local
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib3
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import e0_capture as e0  # noqa: E402

urllib3.disable_warnings()  # self-signed cert on jts3.local


def recover_volume(host: str) -> tuple[int, str]:
    """POST the sanctioned volume-recovery endpoint (CSRF double-submit)."""
    base = f"https://{host}"
    s = requests.Session()
    hdr = {"Host": host}
    s.get(f"{base}/correction/", headers=hdr, verify=False, timeout=15)
    csrf = s.cookies.get(e0.CSRF_COOKIE_NAME) or ""
    r = s.post(
        f"{base}/correction/crossover/recover-volume",
        headers={**hdr, "Content-Type": "application/json", "X-CSRF-Token": csrf},
        data="{}",
        verify=False,
        timeout=15,
    )
    return r.status_code, (r.text[:300] if r.text else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="jts3.local")
    # Explicit so a preflight mints the SAME tier the run will use. Without
    # it this silently inherited e0_capture's default when the tier became
    # selectable (issue #2636).
    ap.add_argument("--tier", choices=e0.TIERS, default=e0.TIER_REMOTE)
    args = ap.parse_args()

    print(f"== 1. start session on {args.host} (tier={args.tier}; opens volume, NO audio yet) ==")
    tap_link = e0.start_session(args.host, tier=args.tier)
    print(f"   tap_link OK: {tap_link[:60]}...")

    handle = e0.parse_tap_link(tap_link)
    key = e0.crypto.content_key_from_b64url(handle.content_key_b64)
    print(f"   fragment parsed: session_id={handle.session_id[:12]}... "
          f"key={len(key)}B mac={'present' if handle.spec_mac else 'MISSING'}")

    print("== 2. fetch spec from relay + verify MAC ==")
    client = e0.RelayPhoneClient(
        base_url="https://relay.jasper.tech",
        session_id=handle.session_id,
        upload_token=handle.upload_token,
        content_key=key,
    )
    spec_text = client.fetch_spec_text()
    if handle.spec_mac:
        e0.integ.verify_capture_spec_mac(key, handle.session_id, spec_text, handle.spec_mac)
        print("   spec MAC VERIFIED")
    spec = json.loads(spec_text)

    print("== 3. the capture plan the Pi built ==")
    plan = spec.get("capture_plan") or {}
    print(f"   capture_target={plan.get('capture_target')}  "
          f"progress_poll_ms={spec.get('progress_poll_ms')}")
    for e in plan.get("entries") or []:
        print(f"   entry index={e.get('index')} kind={e.get('kind_label')!r} "
              f"duration_ms={e.get('duration_ms')} screen={e.get('screen')}")
    hint = (spec.get("default_setup") or {}).get("calibration")
    print(f"   calibration hint: {json.dumps(hint) if hint else '(none)'}")

    print("== 4. recover session volume (leave the box clean) ==")
    code, body = recover_volume(args.host)
    print(f"   recover-volume -> HTTP {code} {body[:120]}")

    print("\nPRE-FLIGHT OK — non-audio path validated, no tones played.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
