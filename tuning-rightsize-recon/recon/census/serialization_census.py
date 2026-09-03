#!/usr/bin/env python3
"""Table 6: serialization census.

- methods named to_dict/from_dict/to_json/from_json/as_dict/to_record/
  from_record/to_mapping/from_mapping
- functions named serialize*/deserialize*/validate_*record*
- per-file counts
- how many are defined inside an @dataclass-decorated class
- how many dataclasses have to_dict but no from_dict

Usage: python3 serialization_census.py <scope_files.txt> > table_6.md
"""
import ast
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path("/home/user/JTS")

METHOD_NAMES = {
    "to_dict", "from_dict", "to_json", "from_json", "as_dict",
    "to_record", "from_record", "to_mapping", "from_mapping",
}
FUNC_PATTERNS = [re.compile(r"^serialize"), re.compile(r"^deserialize"), re.compile(r"^validate_.*record.*")]


def func_matches(name):
    return any(p.match(name) for p in FUNC_PATTERNS)


def is_dataclass(cls_node) -> bool:
    for dec in cls_node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "dataclass":
            return True
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == "dataclass":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "dataclass":
            return True
    return False


def main():
    list_path = Path(sys.argv[1])
    files = [line.strip() for line in list_path.read_text().splitlines() if line.strip()]

    per_file_method = defaultdict(int)
    per_file_func = defaultdict(int)
    total_method = 0
    total_func = 0
    method_on_dataclass = 0
    method_not_on_dataclass = 0
    name_counts = defaultdict(int)

    dataclasses_with_to_dict = set()
    dataclasses_with_from_dict = set()
    all_dataclasses = set()

    for rel in files:
        path = REPO / rel
        src = path.read_text(errors="replace")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                dc = is_dataclass(node)
                cls_key = (rel, node.name)
                if dc:
                    all_dataclasses.add(cls_key)
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name in METHOD_NAMES:
                            per_file_method[rel] += 1
                            total_method += 1
                            name_counts[item.name] += 1
                            if dc:
                                method_on_dataclass += 1
                                if item.name == "to_dict":
                                    dataclasses_with_to_dict.add(cls_key)
                                if item.name == "from_dict":
                                    dataclasses_with_from_dict.add(cls_key)
                            else:
                                method_not_on_dataclass += 1

    # separate pass for top-level-ish serialize*/deserialize*/validate_*record* funcs
    # (counted regardless of nesting depth, but not the dict/json methods above)
    for rel in files:
        path = REPO / rel
        src = path.read_text(errors="replace")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and func_matches(node.name):
                per_file_func[rel] += 1
                total_func += 1
                name_counts[node.name] += 1

    print("### Table 6a — serialization method/function counts per file\n")
    files_any = sorted(
        set(per_file_method) | set(per_file_func),
        key=lambda f: -(per_file_method.get(f, 0) + per_file_func.get(f, 0)),
    )
    print("| file | to/from_dict-ish methods | serialize/deserialize/validate_*record* funcs |")
    print("|---|---:|---:|")
    for f in files_any:
        print(f"| {f} | {per_file_method.get(f,0)} | {per_file_func.get(f,0)} |")
    print(f"| **TOTAL** | {total_method} | {total_func} |")

    print("\n### Table 6b — breakdown by exact name\n")
    print("| name | count |")
    print("|---|---:|")
    for name in sorted(name_counts, key=lambda k: -name_counts[k]):
        print(f"| `{name}` | {name_counts[name]} |")

    print("\n### Table 6c — dataclass serialization coverage\n")
    print(f"- Total classes decorated `@dataclass` in scope: {len(all_dataclasses)}")
    print(f"- Methods (to_dict/from_dict/...) defined on a `@dataclass` class: {method_on_dataclass}")
    print(f"- Methods defined on a non-dataclass class: {method_not_on_dataclass}")
    print(f"- Dataclasses with `to_dict`: {len(dataclasses_with_to_dict)}")
    print(f"- Dataclasses with `from_dict`: {len(dataclasses_with_from_dict)}")
    missing_from = dataclasses_with_to_dict - dataclasses_with_from_dict
    print(f"- Dataclasses with `to_dict` but NO `from_dict`: {len(missing_from)}\n")
    if missing_from:
        print("| file | class |")
        print("|---|---|")
        for f, name in sorted(missing_from):
            print(f"| {f} | {name} |")


if __name__ == "__main__":
    main()
