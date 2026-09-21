# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Select banked pair evidence without coupling path readers to rear analysis."""
from functools import lru_cache
from pathlib import Path
from typing import Any

from jasper.active_speaker.candidate_bank import _directories
from jasper.active_speaker.measurement_programs import POSE_KIND_BEARING, PURPOSE_REAR
from jasper.json_fields import parse_utc_iso
from .rear_views import front_on_axis, pair_diagnostic
from .room_selection import purpose_take_records
from .round_captures import RoundCapturesRefused, doc_pose_key
from .round_inputs import _read_json_mapping, round_inputs


# Pair at index 29 of 232 rounds: 128 gives margin at ~8 s cold (2.46 s/40 on Pi 5).
_ROUND_LIMIT = 128


@lru_cache(maxsize=_ROUND_LIMIT)
def _front_pair_round(directory: Path) -> bool:
    return any(
        front_on_axis(doc_pose_key(record), record.get("pose_kind") or POSE_KIND_BEARING)
        and pair_diagnostic(record) is not None
        for _, record in purpose_take_records(round_inputs(directory).session_dir, purpose=PURPOSE_REAR)
    )


def newest_rear_pair_round(root: Path | None = None, *, limit: int = _ROUND_LIMIT) -> dict[str, Any] | None:
    """Newest front pair in a bounded bank window, independent of tune identity."""
    from jasper.active_speaker.round_bank import DEFAULT_CAMPAIGN_ROOT  # lazy: commissioning import cost

    banked = []
    for path in _directories(root if root is not None else DEFAULT_CAMPAIGN_ROOT):
        try:
            if (path / "bundle").is_dir():
                provenance = _read_json_mapping(path / "provenance.json")
                if provenance is not None:
                    at = provenance.get("banked_at_utc")
                    banked.append((parse_utc_iso(str(at or "")) or path.stat().st_mtime, path, at))
        except (OSError, ValueError):
            continue
    for _, path, at in sorted(banked, reverse=True)[:max(0, limit)]:
        try:
            if _front_pair_round(path):
                return {"round_dir": path, "round_id": path.name, "banked_at": at}
        except (OSError, ValueError, RoundCapturesRefused):
            continue
    return None
