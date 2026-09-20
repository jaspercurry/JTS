import json, sys, numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt, matplotlib.ticker
SP=sys.argv[1]; d=json.load(open(SP+"/search/H4/result.json"))["by_pose"]
names={"5e9afae3":("N1 (start of day)","#1f77b4",1.6),"2fc52a19":("ident-C","#ff7f0e",2.6),"711b458f":("S1","#2ca02c",1.6),"654e057b":("d-0.12","#d62728",2.6)}
cs=[63,80,100,125,160,200,250,315,400,500]
def band(f,m,c):
    k=(f>=c/2**(1/6))&(f<c*2**(1/6)); return 10*np.log10(np.mean(10**(m[k]/10)))
def change(mic,poses,fp,c):
    out=[]
    for p in poses:
        cand=d[mic][p]["candidates"]; ref=[v for k,v in cand.items() if k.startswith("0caaa048")][0]; x=[v for k,v in cand.items() if k.startswith(fp)][0]
        f=np.array(x["freqs_hz"]); out.append(band(f,np.array(x["magnitude_db"],float),c)-band(f,np.array(ref["magnitude_db"],float),c))
    return float(np.mean(out))
allp=list(d["main"]); pm20=[p for p in allp if "az+0.00" not in p]
fig,axes=plt.subplots(1,3,figsize=(17,5.2),sharex=True)
titles=["FRONT change vs rear muted (mean of 3 front positions)\nbelow 0 = the speaker got quieter IN FRONT","BEHIND change vs rear muted (mean of 160° and 200°)\nbelow 0 = quieter behind","FRONT-TO-BACK GAIN = front change minus behind change\nhigher = more cardioid (a common EQ cannot change this)"]
for fp,(lab,col,lw) in names.items():
    fr=np.array([change("main",allp,fp,c) for c in cs]); bk=np.array([change("side",pm20,fp,c) for c in cs])
    for ax,y in zip(axes,(fr,bk,fr-bk)): ax.plot(cs,y,marker="o",color=col,lw=lw,label=lab)
for ax,t in zip(axes,titles):
    ax.set_xscale("log"); ax.set_xticks(cs); ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter()); ax.get_xaxis().set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.axhline(0,color="#777777",ls="--",lw=1.2); ax.grid(True,alpha=0.3); ax.set_title(t,fontsize=10); ax.set_xlabel("third octave, Hz"); ax.set_ylabel("dB"); ax.axvspan(100,350,color="#ffe9a8",alpha=0.35,lw=0)
axes[0].legend(fontsize=9,loc="lower right")
fig.suptitle("Per third octave, all room sound in (ungated), summary round 8ae2ac84b867 — yellow = 100–350 Hz target band",fontsize=11)
fig.tight_layout(rect=(0,0,1,0.93)); out=SP+"/graphs/h4-front-back-gain.png"; fig.savefig(out,dpi=110); print(out)
