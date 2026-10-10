#!/usr/bin/env python3
"""Exact peripheral register addresses accessed (base reg tracked per function, dropped on
reassignment). Usage: periph_regs.py <app.bin> [lo hi]  (default 0x40000000..0x40100000)"""
import struct, sys, collections
from capstone import *
from capstone.arm import *
BASE=0xE000
d=open(sys.argv[1],'rb').read()
lo=int(sys.argv[2],16) if len(sys.argv)>2 else 0x40000000
hi=int(sys.argv[3],16) if len(sys.argv)>3 else 0x40100000
md=Cs(CS_ARCH_ARM,CS_MODE_THUMB);md.detail=True
regs=collections.Counter(); sites=collections.defaultdict(set)
off=0
while off<len(d):
    n=0; movw={}; bases={}
    for i in md.disasm(d[off:],BASE+off):
        n+=i.size; m=i.mnemonic; ops=i.operands
        if m.startswith(('pop','bx')) and 'pc' in i.op_str or m=='bx': bases={}; movw={}
        # memory access using a tracked base
        if m.startswith(('ldr','str')) and len(ops)>1 and ops[1].type==ARM_OP_MEM:
            mm=ops[1].mem
            if mm.base in bases and mm.index==0:
                a=bases[mm.base]+mm.disp; regs[a]+=1; sites[a].add(i.address)
        # register writes: update/drop tracking
        if ops and ops[0].type==ARM_OP_REG and not m.startswith(('str','cmp','cmn','tst','teq','push','vstr','b','it','stm')):
            r=ops[0].reg
            if m.startswith('movw'): movw[r]=ops[1].imm; bases.pop(r,None)
            elif m.startswith('movt') and r in movw:
                v=(ops[1].imm<<16)|movw[r]
                if lo<=v<hi: bases[r]=v
                else: bases.pop(r,None)
            elif m.startswith('ldr') and len(ops)>1 and ops[1].type==ARM_OP_MEM and ops[1].mem.base==ARM_REG_PC:
                la=((i.address+4)&~3)+ops[1].mem.disp; o=la-BASE
                v=struct.unpack_from('<I',d,o)[0] if 0<=o<len(d)-3 else 0
                if lo<=v<hi: bases[r]=v
                else: bases.pop(r,None)
            elif m.startswith(('add','sub')) and len(ops)==3 and ops[1].type==ARM_OP_REG and ops[1].reg in bases and ops[2].type==ARM_OP_IMM:
                bases[r]=bases[ops[1].reg]+(ops[2].imm if m.startswith('add') else -ops[2].imm)
            else: bases.pop(r,None); movw.pop(r,None)
    off+=n if n else 2
for a,c in sorted(regs.items()):
    print(f'{a:08x} x{c:<3d} sites={len(sites[a]):3d}  first={min(sites[a]):#x}')
