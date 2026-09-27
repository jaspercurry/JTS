import json
from jasper.biquad import PeqFilter
from jasper.active_speaker.profile import ActiveSpeakerPreset
from jasper.active_speaker.camilla_yaml import emit_active_speaker_baseline_config as emit
from jasper.active_speaker.branch_chain import rear_branch_sum_headroom_db
from tests.test_rear_output_foundation import _rear_pair, _rear_document
from probe import charges

PCM = "hw:Test"
d = json.load(open("tests/fixtures/crossover_v2_incident_20260810/candidate_fit.json"))
preset2 = ActiveSpeakerPreset.from_mapping(d["source_preset"])
lin2 = {r: v["filters"] for r, v in d["linearization"].items()}
corr2 = {r: {"gain_db": g} for r, g in d["role_attenuations_db"].items()}

def show(name, text, **kw):
    print(f"{name:60s}", json.dumps({k: v for k, v in charges(text, **kw).items() if k in ("old_db","new_db","delta_db","peak_db","peak_output","peak_hz")}))

show("incident 2-way (lin boosts +4.79@347, +3.45@849)", emit(preset2, playback_device=PCM, corrections=corr2, linearization=lin2))
show("  + room cut -3 dB @347 Hz Q2", emit(preset2, playback_device=PCM, corrections=corr2, linearization=lin2, room_peqs=[PeqFilter(347.0, 2.0, -3.0)]))
show("  + room boost +3 dB @50 Hz Q2", emit(preset2, playback_device=PCM, corrections=corr2, linearization=lin2, room_peqs=[PeqFilter(50.0, 2.0, 3.0)]))
show("  + room boost +3@50 and +2@3k", emit(preset2, playback_device=PCM, corrections=corr2, linearization=lin2, room_peqs=[PeqFilter(50.0, 2.0, 3.0), PeqFilter(3000.0, 2.0, 2.0)]))
show("room only +3@50 Q2 (no lin), trims 0", emit(preset2, playback_device=PCM, room_peqs=[PeqFilter(50.0, 2.0, 3.0)]))
show("room only +3@50 Q2, woofer trim -3", emit(preset2, playback_device=PCM, corrections={"woofer": {"gain_db": -3.0}}, room_peqs=[PeqFilter(50.0, 2.0, 3.0)]))
show("room only +3@50 +2@3k, trims 0", emit(preset2, playback_device=PCM, room_peqs=[PeqFilter(50.0, 2.0, 3.0), PeqFilter(3000.0, 2.0, 2.0)]))
show("room only +2@3k, tweeter trim -13", emit(preset2, playback_device=PCM, corrections={"tweeter": {"gain_db": -13.0}}, room_peqs=[PeqFilter(3000.0, 2.0, 2.0)]))

pc, _ = _rear_pair("mono")
doc = _rear_document()
print("rear stage charge today:", round(rear_branch_sum_headroom_db(doc), 4))
show("cardioid seed, no lin", emit(pc, playback_device=PCM, rear_calibration=doc))
wl = {"woofer": [{"biquad_type": "Peaking", "freq": 100.0, "q": 1.0, "gain": 4.0}]}
show("cardioid seed + woofer +4@100", emit(pc, playback_device=PCM, rear_calibration=doc, linearization=wl))
show("cardioid seed + woofer +4@100, woofer trim -3", emit(pc, playback_device=PCM, rear_calibration=doc, linearization=wl, corrections={"woofer": {"gain_db": -3.0}}))
fb = {**doc, "front": {**doc["front"], "gain_db": -2.0, "filters": [{"type": "Biquad", "parameters": {"type": "Peaking", "freq": 60.0, "q": 1.0, "gain": 3.0}}]}}
print("rear stage charge today (front +3@60, gain -2):", round(rear_branch_sum_headroom_db(fb), 4))
show("cardioid front +3@60 g-2 + woofer +4@100", emit(pc, playback_device=PCM, rear_calibration=fb, linearization=wl))
