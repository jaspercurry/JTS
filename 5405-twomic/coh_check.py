# Upper bound on a gain+delay null: 1-|coh|^2 between front-alone and rear-alone at one mic.
import csv, sys, numpy as np, glob, os
def load(p):
    f,m,ph=[],[],[]
    for r in csv.DictReader(open(p)):
        f.append(float(r["frequency_hz"])); m.append(float(r["magnitude_db"])); ph.append(float(r["phase_deg"]))
    f=np.array(f); return f, 10**(np.array(m)/20)*np.exp(1j*np.deg2rad(np.array(ph)))
root=sys.argv[1]
for mic in ("side","main"):
    for pose in sorted(os.listdir(f"{root}/{mic}")):
        d=f"{root}/{mic}/{pose}"
        f,F=load(d+"/H_front.csv"); _,R=load(d+"/H_rear.csv")
        out=[]
        for lo,hi in ((100,350),(100,200),(200,350)):
            k=(f>=lo)&(f<=hi); w=1.0/f[k]   # log-grid weighting
            best=(9,0,0)
            for tau in np.arange(-3e-3,3e-3,0.02e-3):
                Rt=R[k]*np.exp(-2j*np.pi*f[k]*tau)
                g=np.sum(w*F[k]*np.conj(Rt))/np.sum(w*np.abs(Rt)**2)   # least-squares complex gain
                res=np.sum(w*np.abs(F[k]-g*Rt)**2)/np.sum(w*np.abs(F[k])**2)
                if res<best[0]: best=(res,tau,g)
            res,tau,g=best
            out.append(f"{lo}-{hi}: null {10*np.log10(res):6.1f} dB  tau {tau*1e3:+.2f} ms  gain {20*np.log10(abs(g)):+.1f} dB  ang {np.degrees(np.angle(g)):+4.0f}")
        print(mic,pose[:8]," | ".join(out))
