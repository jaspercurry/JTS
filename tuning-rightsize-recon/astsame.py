"""Compare code (docstrings stripped, comments ignored) of a worktree file vs origin/main."""
import ast,subprocess,sys
def strip(src):
    t=ast.parse(src)
    for n in ast.walk(t):
        if isinstance(n,(ast.Module,ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and n.body and isinstance(n.body[0],ast.Expr) and isinstance(getattr(n.body[0],'value',None),ast.Constant) and isinstance(n.body[0].value.value,str):
            n.body[0].value.value='<doc>'
    return ast.dump(t)
wt=sys.argv[1]; bad=0
files=subprocess.run(['git','-C',wt,'diff','--name-only','origin/main'],capture_output=True,text=True).stdout.split()
for f in files:
    if not f.endswith('.py'): print('  non-py',f); continue
    try: a=strip(open(f'{wt}/{f}').read())
    except SyntaxError as e: print('  SYNTAX',f,e); bad+=1; continue
    b=strip(subprocess.run(['git','-C',wt,'show',f'origin/main:{f}'],capture_output=True,text=True).stdout)
    if a!=b: print('  CODE DIFFERS',f); bad+=1
print(f'  {len(files)} files, {bad} not code-identical')
