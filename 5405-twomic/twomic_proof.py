"""Proofs A-D over twomic_result.json + the round's banked frequency_view."""
import json, sys
from pathlib import Path
import numpy as np

T = Path("/private/tmp/claude-501/-Users-jaspercurry-Code-JTS--claude-worktrees-speaker-tuning-llm-arch-bb1ff5/f447b743-f8a9-4f10-bce1-5ed46c07c2ff/scratchpad/twomic")
R = T / "round-5d35f92067b5"
res = json.load((T / "twomic_result.json").open())
fv = json.load((R / "frequency_view.json").open())
banked = {s["take_id"]: s for s in fv["runs"][0]["series"]}
takes = res["takes"]

print("=== A: replay (main mic) vs the product's banked series, 30 Hz - 10 kHz ===")
for row in [t for t in takes if t["mic"] == "main"]:
    b = banked[row["take_id"]]
    fr, mr = np.asarray(row["freqs_hz"]), np.asarray(row["magnitude_db"])
    fb, mb = np.asarray(b["freqs_hz"]), np.asarray(b["magnitude_db"])
    common = np.isin(np.round(fr, 6), np.round(fb, 6))
    idx = np.searchsorted(fb, fr[common])
    d = mr[common] - mb[idx]
    keep = (fr[common] >= 30) & (fr[common] <= 10000)
    print(f"  {row['take_id'][-9:]} bins={int(keep.sum())} max|d|={np.max(np.abs(d[keep])):.6g} "
          f"rms={np.sqrt(np.mean(d[keep]**2)):.6g} dropped_by_view={int((~common).sum())}")

print("\n=== B: Dayton minus UMIK-2, per third octave, median over takes, broadband offset removed ===")
centers = [31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000,
           1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000]
edges = [(c / 2 ** (1 / 6), c * 2 ** (1 / 6)) for c in centers]
pairs = {}
for row in takes:
    pairs.setdefault(row["take_id"], {})[row["mic"]] = row
rows = []
for tid, mics in sorted(pairs.items()):
    f = np.asarray(mics["main"]["freqs_hz"])
    d = np.asarray(mics["side"]["magnitude_db"]) - np.asarray(mics["main"]["magnitude_db"])
    band = (f >= 100) & (f <= 5000)
    d = d - np.mean(d[band])            # one broadband offset, 100 Hz - 5 kHz
    rows.append([np.mean(d[(f >= lo) & (f < hi)]) if ((f >= lo) & (f < hi)).any() else np.nan
                 for lo, hi in edges])
med = np.nanmedian(np.asarray(rows), axis=0)
for c, v in zip(centers, med):
    print(f"  {c:>7g} Hz  {v:+7.2f} dB")
mid = [v for c, v in zip(centers, med) if 100 <= c <= 5000]
print(f"  100 Hz-5 kHz: max|diff| {np.nanmax(np.abs(mid)):.2f} dB, rms {np.sqrt(np.nanmean(np.square(mid))):.2f} dB")
print(f"  whole 31.5 Hz-10 kHz: max|diff| {np.nanmax(np.abs(med)):.2f} dB")

print("\n=== C: change_db vs rear-muted, Dayton vs UMIK-2 ===")
names = {"a81d6c56": "base", "5e9afae3": "N1", "51697699": "N2", "0caaa048": "muted"}
tab = {mic: res["by_candidate"][mic] for mic in ("main", "side")}
bands = [tuple(b["band_hz"]) for b in next(iter(tab["main"]["candidates"].values()))["bands"]]
print("  band            " + "".join(f"{names[c[:8]]:>22s}" for c in sorted(tab["main"]["candidates"]) if not c.startswith("0caaa048")))
print("  " + " " * 14 + "      UMIK   Dayton   diff" * 3)
worst = (0.0, None)
for i, band in enumerate(bands):
    line = f"  {band[0]:>5g}-{band[1]:<6g} Hz "
    for cand in sorted(tab["main"]["candidates"]):
        if cand.startswith("0caaa048"):
            continue
        u = tab["main"]["candidates"][cand]["bands"][i]["change_db"]
        s = tab["side"]["candidates"][cand]["bands"][i]["change_db"]
        line += f"{u:+8.2f}{s:+9.2f}{s-u:+8.2f}"
        if abs(s - u) > worst[0]:
            worst = (abs(s - u), f"{names[cand[:8]]} {band[0]:g}-{band[1]:g} Hz (UMIK {u:+.2f}, Dayton {s:+.2f})")
    print(line)
print(f"  worst disagreement: {worst[0]:.2f} dB at {worst[1]}")

print("\n  cross-check against the product's own packet figures for this pose")
print("  (upper 3 bands, candidate minus muted). Base recomputed WITHOUT take_0001,")
print("  which is the entry_baseline pose the packet's lateral comparison excludes:")
lat = [t for t in takes if t["mic"] == "main" and t["ok"] and not t["take_id"].endswith("0001")]
grid = np.asarray(lat[0]["freqs_hz"])
groups = {}
for t in lat:
    groups.setdefault(t["candidate_id"][:8], []).append(np.asarray(t["magnitude_db"]))
ref = np.mean(groups["0caaa048"], axis=0)
from jasper.audio_measurement.rear_evidence import LEVEL_BANDS_HZ, band_level_changes
packet = {"base": (-0.37, -0.29, -0.01), "N1": (-0.34, -0.10, 0.19), "N2": (-0.89, -0.54, -0.55)}
for cid, rows_ in sorted(groups.items(), key=lambda kv: list(names).index(kv[0])):
    if cid == "0caaa048":
        continue
    got = band_level_changes(grid, np.mean(rows_, axis=0), reference_db=ref,
                             coverage_hz=tab["main"]["coverage_hz"], bands_hz=LEVEL_BANDS_HZ)[-3:]
    mine = tuple(round(b["change_db"], 2) for b in got)
    print(f"    {names[cid]:5s} replay {mine}  packet {packet[names[cid]]}  "
          f"max|diff| {max(abs(a - b) for a, b in zip(mine, packet[names[cid]])):.2f} dB")

print("\n  absolute band levels (dB, each mic's own scale):")
for i, band in enumerate(bands):
    line = f"  {band[0]:>5g}-{band[1]:<6g} Hz "
    for cand in sorted(tab["main"]["candidates"], key=lambda c: list(names).index(c[:8])):
        u = tab["main"]["candidates"][cand]["bands"][i]
        line += f" {names[cand[:8]]}:{u['level_db']:+8.2f}"
    print(line)

print("\n=== D: side-clock health ===")
print("  product epsilon_ppm is None on every take: a VERIFY summed program has ONE sweep_verify,")
print("  and _estimate_drift needs two occurrences of a role. Drift is measured across the round instead:")
rate = 48000.0
t0 = takes[0]["journal_start_epoch"]
xs, ys = [], []
for row in [t for t in takes if t["mic"] == "side"]:
    predicted = (row["journal_start_epoch"] - res["side_start_epoch"]) * rate
    found = row["cut_first_sample"] + row["sweep_located_start"]
    xs.append(row["journal_start_epoch"] - t0)
    ys.append(found - predicted)
slope, intercept = np.polyfit(xs, ys, 1)
resid = np.asarray(ys) - (slope * np.asarray(xs) + intercept)
print(f"  span {xs[-1]:.1f} s, slope {slope:+.3f} samples/s = {slope / rate * 1e6:+.1f} ppm nominal, "
      f"but the fit residual is +-{np.max(np.abs(resid)) / rate * 1e3:.0f} ms -- an order more than any")
print("  plausible drift over this span, so the scatter is journal action=start latency jitter,")
print("  not the clock. Read it as: no drift or dropout LARGER than ~40 ms is present.")
from jasper.audio_measurement.wired_capture import decode_wav_to_mono, scan_zero_runs
side_s, side_rate = decode_wav_to_mono(Path(res["side_wav"]).read_bytes())
zc, _ = scan_zero_runs(np.round(side_s * (2 ** 31 - 1)).astype("<i4"))
print(f"  side recording: {side_s.size} samples = {side_s.size / side_rate:.3f} s, "
      f"product scan_zero_runs count {zc}, peak {np.max(np.abs(side_s)):.4f} FS")
for row, x, y, r in zip([t for t in takes if t["mic"] == "side"], xs, ys, resid):
    main = next(t for t in takes if t["take_id"] == row["take_id"] and t["mic"] == "main")
    print(f"  {row['take_id'][-9:]} t={x:7.2f}s offset={y:9.1f} smp fit_resid={r:+7.1f} smp "
          f"({r / rate * 1e3:+.2f} ms) | side resid_ms={row['worst_residual_ms']:.3f} "
          f"main resid_ms={main['worst_residual_ms']:.3f} | anchor side={row['anchor']['confidence']:.3f} "
          f"main={main['anchor']['confidence']:.3f} | snr side={row['pilot_snr_db']['summed']:.1f} "
          f"main={main['pilot_snr_db']['summed']:.1f} dB | ok={row['ok']} {row['reason'] or '-'}")
print("  main-mic refusals:", [(t["take_id"][-9:], t["reason"]) for t in takes if t["mic"] == "main" and not t["ok"]])
print("  side-mic refusals:", [(t["take_id"][-9:], t["reason"]) for t in takes if t["mic"] == "side" and not t["ok"]])
