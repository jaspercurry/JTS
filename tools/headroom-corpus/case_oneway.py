import sys, json
from tests.crossover_v2_fixtures import _one_way_preset
from jasper.active_speaker.camilla_yaml import emit_active_speaker_baseline_config
from probe import charges
preset = _one_way_preset()
print("way", preset.way_count, [ (o.index, o.driver_role, o.output_variant) for o in preset.channel_map.outputs])
lin = {"full_range": [{"biquad_type": "Peaking", "freq": 1000.0, "q": 1.0, "gain": 4.0}]}
for trim in (0.0, -2.0, -6.0):
    y = emit_active_speaker_baseline_config(preset, playback_device="hw:Test", corrections={"full_range": {"gain_db": trim}}, linearization=lin)
    print(trim, json.dumps(charges(y)))
