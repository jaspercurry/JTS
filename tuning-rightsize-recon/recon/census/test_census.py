#!/usr/bin/env python3
"""Table 10: test census for the tuning scope.

Over the files listed in scope_tests.txt:
  - number of files, total lines
  - number of test functions (def test_* at any nesting, sync or async)
  - number of parametrized test functions (decorated with
    @pytest.mark.parametrize / @parametrize, any dotted-attr ending in
    "parametrize")
  - number of test functions whose body contains an `assert` comparing
    against a string literal containing a space (a likely prose/text pin) —
    detected as `assert <expr> == "...(space)..."` or `"...(space)..." ==
    <expr>` or `assertEqual` calls with one arg a string containing a space
  - number of test functions that read source files: calls to
    inspect.getsource(...), `.read_text()` where the receiver looks like a
    path ending in .py, or ast.parse(...) applied to something that looks
    like repo source (heuristic: ast.parse call present in the function, or
    a .py path literal passed to read_text/open)

Usage: python3 test_census.py <scope_tests.txt> > table_10.md
"""
import ast
import sys
from pathlib import Path

REPO = Path("/home/user/JTS")


def is_parametrize_decorator(dec) -> bool:
    # forms: @pytest.mark.parametrize(...), @parametrize(...), @some.thing.parametrize(...)
    node = dec
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr == "parametrize"
    if isinstance(node, ast.Name):
        return node.id == "parametrize"
    return False


def literal_str_with_space(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and " " in node.value


def has_prose_pin_assert(func_node) -> bool:
    for node in ast.walk(func_node):
        if isinstance(node, ast.Assert):
            test = node.test
            if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq,)):
                left = test.left
                right = test.comparators[0]
                if literal_str_with_space(left) or literal_str_with_space(right):
                    return True
        if isinstance(node, ast.Call):
            fname = None
            if isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            elif isinstance(node.func, ast.Name):
                fname = node.func.id
            if fname in ("assertEqual", "assertIn", "assertMultiLineEqual"):
                for arg in node.args:
                    if literal_str_with_space(arg):
                        return True
    return False


def reads_source(func_node) -> bool:
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            fname = None
            if isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            elif isinstance(node.func, ast.Name):
                fname = node.func.id
            if fname == "getsource":
                return True
            if fname == "parse" and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == "ast":
                    return True
            if fname == "read_text":
                # heuristic: any .py string literal appears anywhere among this
                # call's ancestry args, OR the immediate receiver chain mentions .py
                src_text = ast.dump(node)
                if ".py" in src_text:
                    return True
    return False


def walk_functions(tree):
    """Yield (name, node, is_test) for every function/async function def,
    including nested (e.g. inside a test class)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def main():
    list_path = Path(sys.argv[1])
    files = [line.strip() for line in list_path.read_text().splitlines() if line.strip()]

    total_lines = 0
    total_test_funcs = 0
    total_parametrized = 0
    total_prose_pin = 0
    total_reads_source = 0
    parse_errors = []

    per_file_rows = []

    for rel in files:
        path = REPO / rel
        try:
            src = path.read_text(errors="replace")
        except Exception:
            continue
        lines = src.splitlines()
        total_lines += len(lines)
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as e:
            parse_errors.append((rel, str(e)))
            continue

        file_test_funcs = 0
        file_parametrized = 0
        file_prose_pin = 0
        file_reads_source = 0

        for func in walk_functions(tree):
            if not func.name.startswith("test_") and func.name != "test":
                continue
            file_test_funcs += 1
            is_param = any(is_parametrize_decorator(d) for d in func.decorator_list)
            if is_param:
                file_parametrized += 1
            if has_prose_pin_assert(func):
                file_prose_pin += 1
            if reads_source(func):
                file_reads_source += 1

        total_test_funcs += file_test_funcs
        total_parametrized += file_parametrized
        total_prose_pin += file_prose_pin
        total_reads_source += file_reads_source

        per_file_rows.append((rel, len(lines), file_test_funcs, file_parametrized, file_prose_pin, file_reads_source))

    per_file_rows.sort(key=lambda r: -r[1])

    print(f"### Table 10 — test census for tuning-scope tests\n")
    print(f"- Files: {len(files)}")
    print(f"- Total lines: {total_lines}")
    print(f"- Test functions (def test_*): {total_test_funcs}")
    pct = round(100.0 * total_parametrized / total_test_funcs, 1) if total_test_funcs else 0.0
    print(f"- Parametrized test functions: {total_parametrized} ({pct}%)")
    print(f"- Tests asserting equality against a string literal containing a space (likely prose pin): {total_prose_pin}")
    print(f"- Tests that read source files (inspect.getsource / ast.parse / .py read_text): {total_reads_source}")
    if parse_errors:
        print(f"- Files with parse errors (excluded from function-level counts): {len(parse_errors)}")
        for f, e in parse_errors:
            print(f"  - {f}: {e}")

    print("\n#### Per-file breakdown (top 40 by line count)\n")
    print("| file | lines | test funcs | parametrized | prose-pin asserts | reads source |")
    print("|---|---:|---:|---:|---:|---:|")
    for rel, n, tf, pf, pp, rs in per_file_rows[:40]:
        print(f"| {rel} | {n} | {tf} | {pf} | {pp} | {rs} |")


if __name__ == "__main__":
    main()
