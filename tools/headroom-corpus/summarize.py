import collections
import json
import sys

rows = [json.loads(line) for line in open(sys.argv[1])]
errors = [r for r in rows if "error" in r]
ok = [r for r in rows if "error" not in r]
by_sha = {}
tests_by_sha = collections.defaultdict(set)
for r in ok:
    by_sha.setdefault(r["sha"], r)
    tests_by_sha[r["sha"]].add(r["test"].split("::")[0].replace("tests/", ""))
print(f"emits={len(rows)} ok={len(ok)} errors={len(errors)} distinct_graphs={len(by_sha)}")
for e in collections.Counter(r["error"][:120] for r in errors).most_common(8):
    print("  ERR", e)
changed = [r for r in by_sha.values() if abs(r["delta_db"]) > 0.005]
charged = [r for r in by_sha.values() if r["old_db"] > 0.005 or r["new_db"] > 0.005]
print(f"graphs with any charge (old or new) = {len(charged)}; changed = {len(changed)}")
worst_old = max(r["old_graph_charged_peak_db"] for r in by_sha.values())
print(f"max charged peak of TODAY's graphs under the one function: {worst_old:.4f} dB")


def kind(r):
    parts = [f"{r['way']}-way/{r['outputs']}out"]
    if r["rear"]:
        parts.append("rear")
    if r["sub"]:
        parts.append("sub")
    if r["room_boost_db"] > 0:
        parts.append("room+")
    elif r["room_n"]:
        parts.append("room-cut")
    if r["lin_term_db"] > 0:
        parts.append("lin+")
    if any((v or 0) < 0 for v in r["trims"].values()):
        parts.append("trims")
    if r["output_trim_db"]:
        parts.append("outtrim")
    if r["protection"]:
        parts.append("prot")
    if r["blend_n"]:
        parts.append("blend")
    return " ".join(parts)


groups = collections.defaultdict(list)
for r in changed:
    groups[kind(r)].append(r)
for k, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    deltas = sorted(r["delta_db"] for r in rs)
    files = sorted(set().union(*(tests_by_sha[r["sha"]] for r in rs)))
    print(f"{k:45s} n={len(rs):3d} delta min/med/max = {deltas[0]:+.3f}/{deltas[len(deltas)//2]:+.3f}/{deltas[-1]:+.3f}  files={files[:6]}{'…' if len(files) > 6 else ''}")
print("--- examples")
for r in sorted(changed, key=lambda r: r["delta_db"])[:6] + sorted(changed, key=lambda r: r["delta_db"])[-6:]:
    print(json.dumps({k: r[k] for k in ("old_db", "new_db", "delta_db", "peak_db", "peak_output", "peak_hz", "room_boost_db", "lin_term_db", "rear_term_db", "trims", "way", "rear")}), sorted(tests_by_sha[r["sha"]])[:2])
