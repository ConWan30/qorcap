"""Append eye-verified real clock glyphs to a Sunday Night pack: python tools/add_real_clock_glyphs.py PACK SOURCE FRAME:MMSS..."""
import sys
from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'sidecar'))
import cv2,numpy as np,qorcap_snp as S
from qorcap_mnp import norm_vec
pack=sys.argv[1]; src_name=sys.argv[2]; items=sys.argv[3:]
d=dict(np.load(pack)); L=list(map(str,d['clock_labels'])); V=list(d['clock_vecs'].reshape(len(L),-1))
src={str(k):str(v) for k,v in zip(d['source_keys'],d['source_vals'])}; add={}
for it in items:
  f,text=it.rsplit(':',1)
  gs=S.clock_glyphs(S.hsv_of(cv2.imread(f)))
  if len(gs)!=len(text) or any(g is None for g in gs): print('skip',it,len(gs)); continue
  for g,ch in zip(gs,text):
    v=norm_vec(g,S.CLOCK_SIZE)
    if v is not None: L.append(ch);V.append(v);add[ch]=add.get(ch,0)+1
for ch,n in add.items(): k=f'clock:{ch}'; src[k]=f"{src.get(k,'real')}+{src_name}:{n}"
d['clock_labels']=np.array(L,dtype='<U2'); d['clock_vecs']=np.array(V,np.float32)
ks=sorted(src); d['source_keys']=np.array(ks); d['source_vals']=np.array([src[k] for k in ks])
np.savez_compressed(pack,**d); print('added',add)
