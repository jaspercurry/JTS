import json,sys,collections,os
g=json.load(open('/tmp/claude-0/-home-user-JTS/cf938fc0-997a-5915-a0d9-0d3bfa95c9c0/scratchpad/bottomup/graph.json'))
print(type(g), list(g.keys())[:5] if isinstance(g,dict) else len(g))
