#!/usr/bin/env python3
"""Build markdown tables 1 (per-file + per-package) and 2 (files>1500 lines with
largest class/function) from metrics.json + scope_files.txt.

Usage: python3 report_tables.py > tables_1_2.md
"""
import ast
import json
import sys
from pathlib import Path

REPO = Path("/home/user/JTS")
S = Path(__file__).parent


def package_of(rel: str) -> str:
    if rel.startswith("jasper/active_speaker/"):
        return "jasper/active_speaker"
    if rel.startswith("jasper/audio_measurement/"):
        return "jasper/audio_measurement"
    if rel.startswith("jasper/correction/"):
        return "jasper/correction"
    if rel.startswith("jasper/attribution/"):
        return "jasper/attribution"
    if rel.startswith("jasper/calibration_agent/"):
        return "jasper/calibration_agent"
    if rel.startswith("jasper/web/"):
        return "jasper/web"
    if rel.startswith("jasper/cli/"):
        return "jasper/cli"
    if rel.startswith("experiments/usb-turntable/"):
        return "experiments/usb-turntable"
    return "other"


def node_line_count(node) -> int:
    end = getattr(node, "end_lineno", None)
    if end is None:
        return 0
    return end - node.lineno + 1


def largest_class_and_func(path: Path):
    src = path.read_text(errors="replace")
    tree = ast.parse(src, filename=str(path))
    largest_class = None  # (name, lines)
    largest_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            n = node_line_count(node)
            if largest_class is None or n > largest_class[1]:
                largest_class = (node.name, n)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n = node_line_count(node)
            if largest_func is None or n > largest_func[1]:
                largest_func = (node.name, n)
    return largest_class, largest_func


def main():
    metrics = json.loads((S / "metrics.json").read_text())
    metrics = [m for m in metrics if "error" not in m]
    metrics.sort(key=lambda m: -m["total"])

    print("### Table 1a — per-file line census (sorted by total lines desc)\n")
    print("| file | total | code | docstring | comment | blank | prose % |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for m in metrics:
        print(f"| {m['file']} | {m['total']} | {m['code']} | {m['docstring']} | {m['comment']} | {m['blank']} | {m['prose_pct']} |")

    print("\n### Table 1b — per-package totals\n")
    pkg_totals = {}
    for m in metrics:
        pkg = package_of(m["file"])
        t = pkg_totals.setdefault(pkg, {"files": 0, "total": 0, "code": 0, "docstring": 0, "comment": 0, "blank": 0})
        t["files"] += 1
        t["total"] += m["total"]
        t["code"] += m["code"]
        t["docstring"] += m["docstring"]
        t["comment"] += m["comment"]
        t["blank"] += m["blank"]
    print("| package | files | total | code | docstring | comment | blank | prose % |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    grand = {"files": 0, "total": 0, "code": 0, "docstring": 0, "comment": 0, "blank": 0}
    for pkg in sorted(pkg_totals, key=lambda p: -pkg_totals[p]["total"]):
        t = pkg_totals[pkg]
        prose = round(100.0 * (t["docstring"] + t["comment"]) / t["total"], 2) if t["total"] else 0.0
        print(f"| {pkg} | {t['files']} | {t['total']} | {t['code']} | {t['docstring']} | {t['comment']} | {t['blank']} | {prose} |")
        for k in grand:
            grand[k] += t[k]
    prose_all = round(100.0 * (grand["docstring"] + grand["comment"]) / grand["total"], 2) if grand["total"] else 0.0
    print(f"| **TOTAL** | {grand['files']} | {grand['total']} | {grand['code']} | {grand['docstring']} | {grand['comment']} | {grand['blank']} | {prose_all} |")

    print("\n### Table 2 — files > 1,500 lines: largest class / largest function\n")
    big = [m for m in metrics if m["total"] > 1500]
    print(f"({len(big)} files)\n")
    print("| file | total lines | largest class (lines) | largest function (lines) |")
    print("|---|---:|---|---|")
    for m in big:
        path = REPO / m["file"]
        cls, func = largest_class_and_func(path)
        cls_s = f"{cls[0]} ({cls[1]})" if cls else "—"
        func_s = f"{func[0]} ({func[1]})" if func else "—"
        print(f"| {m['file']} | {m['total']} | {cls_s} | {func_s} |")


if __name__ == "__main__":
    main()
