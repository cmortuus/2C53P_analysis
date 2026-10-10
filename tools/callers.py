#!/usr/bin/env python3
"""List BL/B.W call sites targeting given addresses. Usage: callers.py <app.bin> <addr>..."""
import sys
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from capstone.arm import ARM_OP_IMM
BASE = 0xE000
d = open(sys.argv[1], 'rb').read()
T = {int(a, 16) & ~1 for a in sys.argv[2:]}
md = Cs(CS_ARCH_ARM, CS_MODE_THUMB); md.detail = True
off = 0
while off < len(d):
    n = 0
    for i in md.disasm(d[off:], BASE + off):
        n += i.size
        if i.mnemonic in ('bl', 'blx', 'b.w', 'b') and i.operands and i.operands[0].type == ARM_OP_IMM \
                and (i.operands[0].imm & ~1) in T:
            print(f'{i.address:08x}: {i.mnemonic} 0x{i.operands[0].imm:x}')
    off += n if n else 2
