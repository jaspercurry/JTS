# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Select banked pair evidence without coupling path readers to rear analysis."""
from functools import lru_cache
from pathlib import Path
from typing import Any

from jasper.active_speaker.candidate_bank import _directories
from jasper.active_speaker.measurement_programs import PURPOSE_REAR
from jasper.json_fields import parse_utc_iso
from .rear_views import front_on_axis, pair_takes
from .room_selection import purpose_take_records
from .round_inputs import _read_json_mapping, round_inputs


@lru_cache(maxsize=32)
def _front_pair_round(directory: Path) -> bool:
    takes = pair_takes(record for _, record in purpose_take_records(
        round_inputs(directory).session_dir, purpose=PURPOSE_REAR))
    return any(front_on_axis(take.pose_key, take.pose_kind) for take in takes)


def newest_rear_pair_round(root: Path | None = None, *, limit: int = 32) -> dict[str, Any] | None:
    """Newest front pair in a bounded bank window, independent of tune identity."""
    from jasper.active_speaker.round_bank import DEFAULT_CAMPAIGN_ROOT  # lazy: commissioning import cost

    recent = sorted(((path.stat().st_mtime, path) for path in _directories(
        root if root is not None else DEFAULT_CAMPAIGN_ROOT)), reverse=True)[:max(0, limit)]
    banked = []
    for modified, path in recent:
        provenance = _read_json_mapping(path / "provenance.json")
        if (path / "bundle").is_dir() and provenance is not None:
            at = provenance.get("banked_at_utc")
            banked.append((parse_utc_iso(str(at or "")) or modified, path, at))
    for _, path, at in sorted(banked, reverse=True):
        try:
            if _front_pair_round(path):
                return {"round_dir": path, "round_id": path.name, "banked_at": at}
        except (OSError, ValueError):
            continue
    return None
