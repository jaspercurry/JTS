import json, sys, numpy as np, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker
SP=sys.argv[1]
d=json.load(open(f"{SP}/search/H4/result.json"))["by_pose"]
names={"0caaa048":("rear muted","#777777","--",1.6),"5e9afae3":("N1 (start of day)","#1f77b4","-",1.6),
       "2fc52a19":("ident-C","#ff7f0e","-",1.6),"711b458f":("S1","#2ca02c","-",1.6),"654e057b":("d-0.12 (pick)","#d62728","-",2.6)}
poses=[("az+20.00_el+0.00_d+1.00","+20"),("az+0.00_el+0.00_d+1.00","0"),("az-20.00_el+0.00_d+1.00","-20")]
rear_label={"+20":"160°","0":"180°","-20":"200°"}
def curves(mic,pose):
    out={}
    for fp,c in d[mic][pose]["candidates"].items():
        f=np.array(c["freqs_hz"]); m=np.array(c["magnitude_db"],float); k=(f>=20)&(f<=500)
        out[fp[:8]]=(f[k],m[k])
    return out
def style(ax,title,ylabel):
    ax.set_xscale("log"); ax.set_xlim(20,500); ax.set_xticks([20,30,50,80,100,125,160,200,250,315,400,500])
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter()); ax.get_xaxis().set_minor_formatter(matplotlib.ticker.NullFormatter()); ax.tick_params(labelsize=8)
    ax.grid(True,which="both",alpha=0.3); ax.set_title(title,fontsize=10); ax.set_ylabel(ylabel,fontsize=9)
    ax.axvspan(100,350,color="#ffe9a8",alpha=0.35,lw=0)
for kind in ("absolute","change"):
    fig,axes=plt.subplots(2,3,figsize=(17,9),sharex=True)
    for row,mic in enumerate(("main","side")):
        for col,(pose,tag) in enumerate(poses):
            ax=axes[row][col]; cs=curves(mic,pose); ref=cs["0caaa048"][1]
            for key,(lab,colr,ls,lw) in names.items():
                if key not in cs: continue
                f,m=cs[key]
                if kind=="change":
                    if key=="0caaa048": continue
                    m=m-ref
                ax.plot(f,m,color=colr,ls=ls,lw=lw,label=lab)
            where=(f"FRONT mic, {tag}° (UMIK-2, 0.61 m)" if mic=="main" else f"BEHIND the box, {rear_label[tag]} (Dayton, 0.61 m)")
            style(ax,where,"level, dB" if kind=="absolute" else "change vs rear muted, dB")
            if kind=="change":
                ax.axhline(0,color="#777777",ls="--",lw=1.2); ax.set_ylim(-22,8)
            if row==1: ax.set_xlabel("Hz",fontsize=9)
    axes[0][0].legend(fontsize=9,loc="lower right" if kind=="absolute" else "lower left")
    t=("Frequency response 20–500 Hz, all room sound in (ungated), summary round 8ae2ac84b867" if kind=="absolute"
       else "Change against rear muted, 20–500 Hz (ungated) — below 0 = quieter; yellow = 100–350 Hz target band")
    fig.suptitle(t+"\njts3 mid-room on a mini fridge; levels of the two mics are not comparable with each other, shapes are",fontsize=11)
    fig.tight_layout(rect=(0,0,1,0.94)); out=f"{SP}/graphs/h4-{kind}-20-500Hz.png"; fig.savefig(out,dpi=110); print(out)
