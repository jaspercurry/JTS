"""Apply hand-made prose edits: JSON list of {"range": [start, end], "text": [lines...]}."""
import json, sys
from pathlib import Path

path, spec = sys.argv[1], sys.argv[2]
edits = json.loads(Path(spec).read_text())
lines = Path(path).read_text().splitlines(keepends=True)
edits.sort(key=lambda e: e["range"][0], reverse=True)
seen = set()
for e in edits:
    s, t = e["range"]
    assert s not in seen, f"duplicate edit at {s}"
    seen.add(s)
    new = [x + "\n" for x in e.get("text", [])]
    lines[s-1:t] = new
Path(path).write_text("".join(lines))
print(f"applied {len(edits)} edits to {path}")
