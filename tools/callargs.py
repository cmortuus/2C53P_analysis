#!/usr/bin/env python3
"""For every BL to <target>, recover constant r0..r3 (movs/movw/movt/ldr-literal/add) set
in the preceding ~12 instructions. Usage: callargs.py <app.bin> <target> [nargs]"""
import sys, struct
from capstone import *
from capstone.arm import *
BASE=0xE000
d=open(sys.argv[1],'rb').read(); T=int(sys.argv[2],16)&~1; NA=int(sys.argv[3]) if len(sys.argv)>3 else 4
md=Cs(CS_ARCH_ARM,CS_MODE_THUMB); md.detail=True
ins=[]; off=0
while off<len(d):
    n=0
    for i in md.disasm(d[off:],BASE+off): n+=i.size; ins.append(i)
    off+=n if n else 2
R=[ARM_REG_R0,ARM_REG_R1,ARM_REG_R2,ARM_REG_R3]
for k,i in enumerate(ins):
    if i.mnemonic in('bl','blx','b.w') and i.operands and i.operands[0].type==ARM_OP_IMM and (i.operands[0].imm&~1)==T:
        vals={}
        for j in reversed(ins[max(0,k-14):k]):
            ops=j.operands; m=j.mnemonic
            if not ops or ops[0].type!=ARM_OP_REG or ops[0].reg not in R: 
                if m in('bl','blx'): break
                continue
            r=ops[0].reg
            if r in vals and vals[r] is not None: continue
            if m in('movs','mov','mov.w','movw') and ops[1].type==ARM_OP_IMM: vals[r]=(vals.get(r,0)&0xFFFF0000)|ops[1].imm if m=='movw' else ops[1].imm
            elif m=='movt' and ops[1].type==ARM_OP_IMM: vals[r]=(vals.get(r) or 0)|(ops[1].imm<<16); vals[r]=None if False else vals[r]; 
            elif m.startswith('ldr') and len(ops)>1 and ops[1].type==ARM_OP_MEM and ops[1].mem.base==ARM_REG_PC:
                la=((j.address+4)&~3)+ops[1].mem.disp; o=la-BASE
                vals[r]=struct.unpack_from('<I',d,o)[0] if 0<=o<len(d)-3 else None
            else: vals.setdefault(r,None)
        # movt before movw in reversed scan: fix by recomputing forward
        fw={}
        for j in ins[max(0,k-14):k]:
            ops=j.operands; m=j.mnemonic
            if not ops or ops[0].type!=ARM_OP_REG or ops[0].reg not in R: continue
            r=ops[0].reg
            if m in('movs','mov','mov.w') and ops[1].type==ARM_OP_IMM: fw[r]=ops[1].imm
            elif m=='movw': fw[r]=ops[1].imm
            elif m=='movt' and r in fw and fw[r] is not None: fw[r]|=ops[1].imm<<16
            elif m.startswith('ldr') and len(ops)>1 and ops[1].type==ARM_OP_MEM and ops[1].mem.base==ARM_REG_PC:
                la=((j.address+4)&~3)+ops[1].mem.disp; o=la-BASE
                fw[r]=struct.unpack_from('<I',d,o)[0] if 0<=o<len(d)-3 else None
            else: fw[r]=None
        print(f'{i.address:08x}: '+' '.join(f'r{n}={fw.get(R[n]):#x}' if isinstance(fw.get(R[n]),int) else f'r{n}=?' for n in range(NA)))
