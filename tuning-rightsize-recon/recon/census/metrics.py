#!/usr/bin/env python3
"""Per-file line-category census (total/code/docstring/comment/blank) via ast+tokenize.

Usage: python3 metrics.py <file_list.txt> > out.json
Each line of <file_list.txt> is a repo-relative .py path.

Line classification (each physical line gets exactly one bucket):
  - blank: line is empty/whitespace only
  - comment: line's only tokens (ignoring NL/INDENT/DEDENT) are COMMENT
  - docstring: physical line falls inside a STRING token that ast identifies
    as a docstring expression (module/class/function/method first statement,
    bare `expr` statement whose value is a Str/Constant-str)
  - code: everything else (includes lines that are string literals used as
    values, not docstrings, and lines mixing code + trailing comment)
"""
import ast
import json
import sys
import tokenize
from pathlib import Path

REPO = Path("/home/user/JTS")


def docstring_line_ranges(tree, src_lines):
    """Return set of 1-indexed physical line numbers that are inside a docstring
    expression (module/class/def first-statement bare string constant)."""
    ranges = set()

    def consider_body(body):
        if not body:
            return
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            start = first.value.lineno
            end = getattr(first.value, "end_lineno", start)
            for ln in range(start, end + 1):
                ranges.add(ln)

    consider_body(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            consider_body(node.body)
    return ranges


def analyze_file(path: Path):
    src = path.read_text(errors="replace")
    lines = src.splitlines()
    total = len(lines)
    blank = sum(1 for l in lines if l.strip() == "")

    comment_lines = set()
    string_token_lines = set()  # any line touched by a STRING token
    try:
        tokens = list(tokenize.generate_tokens(iter(src.splitlines(keepends=True)).__next__))
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                comment_lines.add(tok.start[0])
            elif tok.type == tokenize.STRING:
                for ln in range(tok.start[0], tok.end[0] + 1):
                    string_token_lines.add(ln)
    except tokenize.TokenizeError:
        pass
    except Exception:
        pass

    docstring_lines = set()
    parse_error = None
    try:
        tree = ast.parse(src, filename=str(path))
        docstring_lines = docstring_line_ranges(tree, lines)
    except SyntaxError as e:
        parse_error = str(e)

    # A line counted as "comment" only if it has no code on it besides the comment
    # (i.e. a full-line comment, not code+trailing "# comment").
    full_comment_lines = set()
    trailing_comment_lines = set()
    try:
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                ln = tok.start[0]
                col = tok.start[1]
                line_text = lines[ln - 1] if ln - 1 < len(lines) else ""
                prefix = line_text[:col]
                if prefix.strip() == "":
                    full_comment_lines.add(ln)
                else:
                    trailing_comment_lines.add(ln)
    except NameError:
        pass

    docstring_only_lines = set()
    for ln in docstring_lines:
        idx = ln - 1
        if 0 <= idx < len(lines) and lines[idx].strip() != "":
            docstring_only_lines.add(ln)

    counted_blank = 0
    counted_comment = 0
    counted_docstring = 0
    counted_code = 0
    for i in range(1, total + 1):
        idx = i - 1
        text = lines[idx]
        if text.strip() == "":
            counted_blank += 1
        elif i in full_comment_lines:
            counted_comment += 1
        elif i in docstring_only_lines:
            counted_docstring += 1
        else:
            counted_code += 1

    prose_pct = 0.0
    if total > 0:
        prose_pct = round(100.0 * (counted_comment + counted_docstring) / total, 2)

    return {
        "file": str(path.relative_to(REPO)),
        "total": total,
        "code": counted_code,
        "docstring": counted_docstring,
        "comment": counted_comment,
        "blank": counted_blank,
        "prose_pct": prose_pct,
        "parse_error": parse_error,
    }


def main():
    list_path = Path(sys.argv[1])
    files = [REPO / line.strip() for line in list_path.read_text().splitlines() if line.strip()]
    results = []
    for f in files:
        try:
            results.append(analyze_file(f))
        except Exception as e:
            results.append({"file": str(f.relative_to(REPO)), "error": str(e)})
    json.dump(results, sys.stdout, indent=1)


if __name__ == "__main__":
    main()
