"""pytest plugin (investigation only): log today's and the proposed charge for
every baseline graph the test corpus emits."""
from __future__ import annotations

import hashlib
import json
import os
import sys

_OUT = os.environ.get("HEADROOM_PROBE_OUT", "/dev/null")
_orig = None
_seen: set[str] = set()


def _measure(args, kwargs, text):
    from jasper.biquad import total_positive_boost_db
    from jasper.active_speaker.camilla_yaml import _branch_context, linearization_headroom_db
    from jasper.active_speaker.branch_chain import rear_branch_sum_headroom_db
    from probe import charges

    preset = args[0] if args else kwargs["preset"]
    corrections = kwargs.get("corrections") or {}
    lin = kwargs.get("linearization") or {}
    room = tuple(kwargs.get("room_peqs") or ())
    rear = kwargs.get("rear_calibration")
    trim_out = float(kwargs.get("output_trim_db") or 0.0)
    rec = charges(text, output_trim_db=trim_out, baseline_headroom_db=float(kwargs.get("baseline_headroom_db") or 0.0))
    rec.update({
        "sha": hashlib.sha256(text.encode()).hexdigest()[:12],
        "way": preset.way_count,
        "outputs": len(preset.channel_map.outputs),
        "sub": preset.local_subwoofer is not None,
        "rear": bool(rear),
        "room_boost_db": round(total_positive_boost_db(room), 3),
        "room_n": len(room),
        "lin_term_db": round(linearization_headroom_db(lin, branch_context=_branch_context(preset, corrections)), 4),
        "rear_term_db": round(rear_branch_sum_headroom_db(rear), 4) if rear and rear.get("case") == "electrical_dsp" else 0.0,
        "trims": {k: v.get("gain_db") for k, v in corrections.items() if isinstance(v, dict)},
        "output_trim_db": trim_out,
        "blend_n": len(kwargs.get("blend_correction") or ()),
        "pref_n": len(kwargs.get("preference_filters") or ()),
        "protection": kwargs.get("protection_sections_by_role") is not None,
        "bass": bool(kwargs.get("bass_extension")),
    })
    return rec


def _wrapper(*args, **kwargs):
    text = _orig(*args, **kwargs)
    try:
        rec = _measure(args, kwargs, text)
    except Exception as exc:  # noqa: BLE001 - a probe must never disturb a test
        rec = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    rec["test"] = os.environ.get("PYTEST_CURRENT_TEST", "").split(" ")[0]
    with open(_OUT, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return text


def _sweep():
    for name, mod in list(sys.modules.items()):
        if name in _seen or mod is None or name == __name__:
            continue
        _seen.add(name)
        d = getattr(mod, "__dict__", None)
        if not isinstance(d, dict):
            continue
        for key, value in list(d.items()):
            if value is _orig:
                d[key] = _wrapper


def pytest_configure(config):
    global _orig
    from jasper.active_speaker.camilla_yaml import emit_baseline
    _orig = emit_baseline.emit_active_speaker_baseline_config
    import functools
    functools.update_wrapper(_wrapper, _orig)
    _sweep()


def pytest_collection_finish(session):
    _sweep()


def pytest_runtest_setup(item):
    _sweep()


def pytest_runtest_call(item):
    _sweep()
