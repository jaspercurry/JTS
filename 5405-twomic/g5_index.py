#!/usr/bin/env python3
"""Add L1's T1/T2/T3 to the search fingerprint index, if they are missing."""
import json
from pathlib import Path

SP = Path(__file__).resolve().parent
ADD = {"4b0374d1": "search/L1/doc-T1.json",
       "3c0c60db": "search/L1/doc-T2.json",
       "37419158": "search/L1/doc-T3.json"}
path = SP / "search" / "fp-index.json"
index = json.loads(path.read_text())
new = {k: v for k, v in ADD.items() if k not in index}
for key, value in new.items():
    assert (SP / value).exists(), value
index.update(new)
path.write_text(json.dumps(dict(sorted(index.items())), indent=1) + "\n")
print(f"added {sorted(new)}; index now holds {len(index)} documents")
