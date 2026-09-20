# Same bound as coh_check.py, after a common time window on both impulse responses (direct sound + T ms).
import csv, sys, numpy as np, os
BIN=1.46484375; N=32768; FS=48000
def load(p):
    f,m,ph=[],[],[]
    for r in csv.DictReader(open(p)):
        f.append(float(r["frequency_hz"])); m.append(float(r["magnitude_db"])); ph.append(float(r["phase_deg"]))
    H=np.zeros(N//2+1,complex); k=np.rint(np.array(f)/BIN).astype(int)
    H[k]=10**(np.array(m)/20)*np.exp(1j*np.deg2rad(np.array(ph))); return H
def bound(F,R,lo,hi):
    f=np.arange(N//2+1)*BIN; k=(f>=lo)&(f<=hi); w=1/f[k]; best=(9,0,0)
    for tau in np.arange(-2.5e-3,2.5e-3,0.02e-3):
        Rt=R[k]*np.exp(-2j*np.pi*f[k]*tau); g=np.sum(w*F[k]*np.conj(Rt))/np.sum(w*abs(Rt)**2)
        res=np.sum(w*abs(F[k]-g*Rt)**2)/np.sum(w*abs(F[k])**2)
        if res<best[0]: best=(res,tau,g)
    return best
root=sys.argv[1]
for mic in ("side","main"):
    for pose in sorted(os.listdir(f"{root}/{mic}")):
        d=f"{root}/{mic}/{pose}"; HF=load(d+"/H_front.csv"); HR=load(d+"/H_rear.csv")
        hf=np.fft.irfft(HF,N); hr=np.fft.irfft(HR,N)
        t0=min(np.argmax(abs(hf)),np.argmax(abs(hr)))   # earliest main arrival
        line=[]
        for T in (6,12,25,1000):
            w=np.zeros(N); a=t0-int(0.004*FS); b=t0+int(T*1e-3*FS)
            idx=np.arange(a,min(b,a+N-1))%N; L=len(idx); win=np.ones(L)
            nl=int(0.002*FS); win[:nl]=0.5-0.5*np.cos(np.pi*np.arange(nl)/nl)
            nr=max(1,L//3); win[-nr:]=0.5+0.5*np.cos(np.pi*np.arange(nr)/nr); w[idx]=win
            Fw=np.fft.rfft(hf*w); Rw=np.fft.rfft(hr*w)
            r1=bound(Fw,Rw,100,350); r2=bound(Fw,Rw,100,200); r3=bound(Fw,Rw,200,350)
            line.append(f"T={T:>4}ms: {10*np.log10(r1[0]):5.1f} (tau {r1[1]*1e3:+.2f} g {20*np.log10(abs(r1[2])):+.1f} ang {np.degrees(np.angle(r1[2])):+.0f}) lo {10*np.log10(r2[0]):5.1f} hi {10*np.log10(r3[0]):5.1f}")
        print(mic,pose[:8]); [print("    ",x) for x in line]
