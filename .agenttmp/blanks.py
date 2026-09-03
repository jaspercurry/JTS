#!/usr/bin/env python3
"""Report runs of 3+ blank lines, and trailing-whitespace lines, per file."""
import sys
from pathlib import Path

for arg in sys.argv[1:]:
    p = Path(arg)
    lines = p.read_text(encoding="utf-8").splitlines()
    run = 0
    start = 0
    for i, line in enumerate(lines, 1):
        if line.strip() == "":
            if run == 0:
                start = i
            run += 1
        else:
            if run >= 3:
                print(f"{p.name}: {run} blank lines at {start}-{start + run - 1}")
            run = 0
    if run >= 3:
        print(f"{p.name}: {run} trailing blank lines at {start}")
    for i, line in enumerate(lines, 1):
        if line != line.rstrip():
            print(f"{p.name}:{i}: trailing whitespace")
