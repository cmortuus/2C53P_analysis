#!/usr/bin/env python3
"""Find code references (ADR / LDR-literal / literal-pool word / MOVW+MOVT) to
given addresses in the 2C53P app image. Linear Thumb sweep; good enough to
locate callers without a full Ghidra analysis.

Usage: xref.py <app.bin> <addr> [<addr> ...]
"""
import struct, sys
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from capstone.arm import ARM_OP_IMM, ARM_OP_MEM, ARM_REG_PC

BASE = 0xE000

def sweep(d):
    md = Cs(CS_ARCH_ARM, CS_MODE_THUMB); md.detail = True
    off = 0
    while off < len(d):
        n = 0
        for ins in md.disasm(d[off:], BASE + off):
            n += ins.size; yield ins
        off += n if n else 2

def main():
    d = open(sys.argv[1], 'rb').read()
    targets = {int(a, 16) for a in sys.argv[2:]}
    # raw literal-pool words
    for o in range(0, len(d) - 3, 2):
        w = struct.unpack_from('<I', d, o)[0]
        if w in targets: print(f'word  @0x{BASE+o:08x} = 0x{w:08x}')
    movw = {}
    for ins in sweep(d):
        m = ins.mnemonic
        if m.startswith('adr'):
            t = (ins.address + 4) & ~3
            t += ins.operands[1].imm if ins.operands[1].type == ARM_OP_IMM else 0
            t = ins.operands[1].imm if ins.operands[1].imm > 0x1000 else t
            if t in targets: print(f'adr   @0x{ins.address:08x}  {m} {ins.op_str}')
        elif m.startswith('ldr') and len(ins.operands) > 1 and ins.operands[1].type == ARM_OP_MEM \
                and ins.operands[1].mem.base == ARM_REG_PC:
            la = ((ins.address + 4) & ~3) + ins.operands[1].mem.disp
            o = la - BASE
            if 0 <= o < len(d) - 3:
                w = struct.unpack_from('<I', d, o)[0]
                if w in targets: print(f'ldr   @0x{ins.address:08x}  {m} {ins.op_str}  ; [0x{la:x}]=0x{w:x}')
        elif m.startswith('movw'):
            movw[ins.operands[0].reg] = ins.operands[1].imm
        elif m.startswith('movt'):
            r = ins.operands[0].reg
            if r in movw:
                v = (ins.operands[1].imm << 16) | movw[r]
                if v in targets: print(f'movt  @0x{ins.address:08x}  -> 0x{v:08x}')
        elif m in ('add', 'add.w', 'addw') and 'pc' in ins.op_str:
            pass

if __name__ == '__main__':
    main()
