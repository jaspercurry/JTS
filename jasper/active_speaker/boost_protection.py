# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Durable excess-boost findings, addressed by the compiled graph (AGENTS §2)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from jasper.atomic_io import atomic_write_text

from .bundles import sessions_dir
from .candidate_bank import CandidateBankRefusal

BOOST_OVER_DECLARED_BOUND = "boost_over_declared_bound"


def config_graph_fingerprint(profile: Mapping[str, Any] | None) -> str:
    return str(((profile or {}).get("config") or {}).get("sha256") or "")[:16]


def boost_finding_path(graph_fingerprint: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{16}", graph_fingerprint) is None:
        raise CandidateBankRefusal("graph_fingerprint_required", "The compiled graph has no valid fingerprint.")
    # Sibling of the retained sessions: eviction must not erase a driver stop.
    return sessions_dir().parent / "candidate_graph_findings" / f"{graph_fingerprint}.json"


def read_boost_finding(graph_fingerprint: str) -> dict[str, Any] | None:
    path = boost_finding_path(graph_fingerprint)
    try:
        with path.open(encoding="utf-8") as stream:
            record = json.loads(stream.read(4096))
        if not isinstance(record, dict) or (
            record.get("graph_fingerprint") != graph_fingerprint
            or record.get("code") != BOOST_OVER_DECLARED_BOUND
        ):
            raise ValueError("invalid boost finding")
        return record
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise CandidateBankRefusal("boost_finding_unreadable", "The graph's boost finding cannot be read.") from exc


def record_boost_finding(graph_fingerprint: str, *, candidate_fingerprint: str, round_id: str) -> None:
    if read_boost_finding(graph_fingerprint) is not None:
        return
    record = {
        "code": BOOST_OVER_DECLARED_BOUND,
        "graph_fingerprint": graph_fingerprint,
        "candidate_fingerprint": candidate_fingerprint,
        "round_id": round_id,
    }
    atomic_write_text(boost_finding_path(graph_fingerprint), json.dumps(record) + "\n", mode=0o640, durable=True)
