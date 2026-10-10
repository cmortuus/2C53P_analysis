#!/usr/bin/env python3
"""Function-level diff of two 2C53P APP images. Functions = Thumb push{..lr} prologues.
Each function is hashed over its instructions with branch targets / PC-literal offsets /
movw-movt immediates masked, so relocation-only differences are ignored.
Usage: fndiff.py OLD.bin NEW.bin [--show]"""
import sys, hashlib
from capstone import *
from capstone.arm import *
BASE=0xE000
def funcs(path):
    d=open(path,'rb').read()
    md=Cs(CS_ARCH_ARM,CS_MODE_THUMB); md.detail=True
    ins=[]; off=0
    while off<len(d):
        n=0
        for i in md.disasm(d[off:],BASE+off): n+=i.size; ins.append(i)
        off+=n if n else 2
    out={}; cur=None; body=[]
    def flush():
        if cur is not None and body: out[cur]=(hashlib.md5(b''.join(body)).hexdigest(), len(body))
    for i in ins:
        m=i.mnemonic
        if m in('push','push.w') and 'lr' in i.op_str: flush(); cur=i.address; body=[]
        if cur is None: continue
        if m.startswith(('b','cb')) or m in('bl','blx') or m.startswith(('movw','movt','adr')) or (m.startswith('ldr') and '[pc' in i.op_str) or m.startswith('vldr'):
            body.append((m+' '+i.op_str.split('#')[0]).encode())
        else: body.append((m+' '+i.op_str).encode())
    flush(); return out,d
old,_=funcs(sys.argv[1]); new,_=funcs(sys.argv[2])
oh={h:a for a,(h,n) in old.items()}; nh={h:a for a,(h,n) in new.items()}
same=sum(1 for a,(h,n) in new.items() if h in oh)
print(f'old funcs={len(old)} new funcs={len(new)} identical(masked)={same} changed/new={len(new)-same}')
if '--show' in sys.argv:
    for a,(h,n) in sorted(new.items()):
        if h not in oh and n>=8: print(f'  changed/new @{a:#x} ({n} insns)')
