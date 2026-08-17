#!/usr/bin/env python3
"""Scoped crossover journey reset between E0 runs, so each run starts from a
clean, un-rebound conductor state (keeps topology/design-draft/baseline).
POST /correction/crossover/reset with CSRF double-submit.

Run:  e0venv/bin/python reset_run.py --host jts3.local
"""
from __future__ import annotations

import argparse
import json
import sys

import requests
import urllib3

urllib3.disable_warnings()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="jts3.local")
    args = ap.parse_args()
    base = f"https://{args.host}"
    s = requests.Session()
    hdr = {"Host": args.host}
    s.get(f"{base}/correction/", headers=hdr, verify=False, timeout=15)
    csrf = s.cookies.get("jts_csrf") or ""
    r = s.post(
        f"{base}/correction/crossover/reset",
        headers={**hdr, "Content-Type": "application/json", "X-CSRF-Token": csrf},
        data="{}",
        verify=False,
        timeout=20,
    )
    body = None
    try:
        body = r.json()
    except ValueError:
        body = r.text[:300]
    print(f"reset -> HTTP {r.status_code}: {json.dumps(body) if isinstance(body, dict) else body}")
    return 0 if r.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
