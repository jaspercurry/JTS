import ast, pathlib, json

root = pathlib.Path("/home/user/JTS/.claude/worktrees/agent-a9d6e06ba55554569")
tests = sorted((root / "tests").glob("*.py"))


class Finder(ast.NodeVisitor):
    def __init__(self, src):
        self.src = src
        self.hits = []

    def visit_Call(self, node):
        seg = ast.get_source_segment(self.src, node) or ""
        f = node.func
        name = ""
        if isinstance(f, ast.Attribute):
            name = f.attr
        elif isinstance(f, ast.Name):
            name = f.id
        if name in ("getsource", "getsourcelines", "getsourcefile"):
            self.hits.append(("getsource", seg[:140]))
        elif name in ("read_text", "read_bytes"):
            self.hits.append(("read", seg[:180]))
        elif name in ("rglob", "glob"):
            self.hits.append(("glob", seg[:180]))
        elif (
            name == "parse"
            and isinstance(f, ast.Attribute)
            and isinstance(f.value, ast.Name)
            and f.value.id == "ast"
        ):
            self.hits.append(("astparse", seg[:180]))
        self.generic_visit(node)


out = {}
for t in tests:
    src = t.read_text()
    if not any(
        k in src for k in ("getsource", "read_text", "read_bytes", "ast.parse", "rglob")
    ):
        continue
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fnd = Finder(src)
            for st in node.body:
                fnd.visit(st)
            if fnd.hits:
                out.setdefault(str(t.relative_to(root)), []).append(
                    {"func": node.name, "line": node.lineno, "hits": fnd.hits}
                )
json.dump(out, open(str(root / ".pinwork/raw.json"), "w"), indent=1)
print(len(out), "files", sum(len(v) for v in out.values()), "funcs")
