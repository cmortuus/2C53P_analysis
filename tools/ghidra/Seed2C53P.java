// Seed Thumb function starts in undefined flash regions by prologue pattern, then
// let analysis follow. Conservative: only `push {..,lr}` (B5xx) / `push.w {..,lr}`
// (2D E9 xx 4x) at even addresses that are undefined and not fall-through from code.
// Run: import.sh -seed   (runs this as preScript with analysis, then re-applies labels)
//@category 2C53P
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.mem.*;

public class Seed2C53P extends GhidraScript {
    @Override
    public void run() throws Exception {
        MemoryBlock fl = currentProgram.getMemory().getBlock("APP_FLASH");
        Listing L = currentProgram.getListing();
        long end = Math.min(fl.getEnd().getOffset(), 0x58000L); // below string/asset region
        int seeded = 0;
        for (long x = fl.getStart().getOffset(); x < end; x += 2) {
            Address p = toAddr(x);
            if (L.getCodeUnitAt(p) instanceof Instruction) continue;
            Data d = L.getDataAt(p); if (d != null && d.isDefined()) continue;
            int b0 = fl.getByte(p) & 0xFF, b1 = fl.getByte(p.add(1)) & 0xFF;
            boolean pushT1 = b1 == 0xB5;                                   // push {.., lr}
            boolean pushT2 = b0 == 0x2D && b1 == 0xE9 && (fl.getByte(p.add(3)) & 0x40) != 0; // push.w {.., lr}
            if (!pushT1 && !pushT2) continue;
            // previous unit must not fall through into us
            Instruction prev = L.getInstructionBefore(p);
            if (prev != null && prev.getMaxAddress().add(1).equals(p) && prev.getFlowType().hasFallthrough()) continue;
            disassemble(p);
            if (L.getInstructionAt(p) != null && createFunction(p, null) != null) seeded++;
        }
        println("Seed2C53P seeded " + seeded + " functions");
    }
}
