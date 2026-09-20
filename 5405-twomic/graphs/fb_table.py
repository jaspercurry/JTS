# Front change, behind change and front-to-back gain per third octave (ungated), vs rear muted, round H4.
import json, sys, numpy as np
d=json.load(open(sys.argv[1]+"/search/H4/result.json"))["by_pose"]
names={"5e9afae3":"N1","2fc52a19":"ident-C","711b458f":"S1","654e057b":"d-0.12"}
cs=[80,100,125,160,200,250,315,400,500]
def band(f,m,c):
    k=(f>=c/2**(1/6))&(f<c*2**(1/6)); return 10*np.log10(np.mean(10**(m[k]/10)))
def change(mic,poses,fp,c):
    out=[]
    for p in poses:
        cand=d[mic][p]["candidates"]; ref=[v for k,v in cand.items() if k.startswith("0caaa048")][0]; x=[v for k,v in cand.items() if k.startswith(fp)][0]
        f=np.array(x["freqs_hz"]); out.append(band(f,np.array(x["magnitude_db"],float),c)-band(f,np.array(ref["magnitude_db"],float),c))
    return float(np.mean(out))
allp=list(d["main"]); pm20=[p for p in allp if "az+0.00" not in p]
print("third octave:      "+"".join(f"{c:>7}" for c in cs))
for fp,n in names.items():
    fr=[change("main",allp,fp,c) for c in cs]; bk=[change("side",pm20,fp,c) for c in cs]
    print(f"{n:8} front    "+"".join(f"{v:7.1f}" for v in fr))
    print(f"{'':8} behind   "+"".join(f"{v:7.1f}" for v in bk))
    print(f"{'':8} F/B gain "+"".join(f"{a-b:7.1f}" for a,b in zip(fr,bk)))
