# Raw woofer levels per third octave at each mic from the pair take (rear minus front, dB).
import csv, os, sys, numpy as np
def load(p):
    f,m=[],[]
    for r in csv.DictReader(open(p)): f.append(float(r["frequency_hz"])); m.append(float(r["magnitude_db"]))
    return np.array(f), np.array(m)
cs=[100,125,160,200,250,315,400,500]
print("rear-minus-front level, dB, third octaves:", cs)
for mic in ("side","main"):
    for pose in sorted(os.listdir(f"{sys.argv[1]}/{mic}")):
        d=f"{sys.argv[1]}/{mic}/{pose}"; f,F=load(d+"/H_front.csv"); _,R=load(d+"/H_rear.csv"); row=[]
        for c in cs:
            k=(f>=c/2**(1/6))&(f<c*2**(1/6)); e=lambda x:10*np.log10(np.mean(10**(x[k]/10)))
            row.append(f"{e(R)-e(F):+5.1f}")
        print(mic, pose[:8], " ".join(row))
