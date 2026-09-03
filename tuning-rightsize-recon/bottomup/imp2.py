import json,collections,sys
g=json.load(open('/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/bottomup/graph.json'))
edges=g['edges']; lines=g['lines']
rev=collections.defaultdict(set)
for a,bs in edges.items():
    for b in bs: rev[b].add(a)
tools=sys.argv[1:]
for t in tools:
    m='jasper.cli.'+t
    print(f"=== {m} ({lines.get(m)}) ===")
    for d in sorted(edges.get(m,[])):
        if d.startswith('jasper.cli') or d.count('.')<2 and False: pass
        imps=sorted(rev[d]-{m})
        nontest=[i for i in imps]
        print(f"  {lines.get(d,0):6} {d:60} importers={len(nontest)} {'' if len(nontest)>3 else nontest}")
