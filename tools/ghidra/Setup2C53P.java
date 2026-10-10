// Ghidra PRE-analysis setup for the FNIRSI 2C53P APP image (raw binary at 0xE000).
// Run as -preScript with analyzeHeadless (see tools/ghidra/import.sh).
//   - adds RAM (0x20000000, RW init data from rw_init_0x20000000.bin), peripherals, SDRAM
//   - forces Thumb mode, creates vector-table handlers as functions
// Labels/comments live in Labels2C53P.java (post-script) so they survive analysis.
//@category 2C53P
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.lang.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.mem.*;
import ghidra.program.model.symbol.*;
import java.io.File;
import java.math.BigInteger;
import java.nio.file.Files;

public class Setup2C53P extends GhidraScript {

    Address a(long x) { return toAddr(x); }

    void block(String name, long start, long size, boolean init, boolean w, boolean x, boolean vol)
            throws Exception {
        Memory mem = currentProgram.getMemory();
        if (mem.getBlock(a(start)) != null) return;
        MemoryBlock b = init
            ? mem.createInitializedBlock(name, a(start), size, (byte) 0, monitor, false)
            : mem.createUninitializedBlock(name, a(start), size, false);
        b.setRead(true); b.setWrite(w); b.setExecute(x); b.setVolatile(vol);
    }

    @Override
    public void run() throws Exception {
        String root = System.getenv().getOrDefault("FNIRSI_ROOT", "/Users/c/2C53P_analysis");
        Memory mem = currentProgram.getMemory();
        mem.getBlocks()[0].setName("APP_FLASH");

        // --- memory map ---
        block("SRAM", 0x20000000L, 0x10000, true, true, true, false);
        byte[] rw = Files.readAllBytes(new File(root, "rw_init_0x20000000.bin").toPath());
        mem.setBytes(a(0x20000000L), rw);
        block("PERIPH", 0x40000000L, 0x100000, false, true, false, true);
        block("SCS", 0xE000E000L, 0x1000, false, true, false, true);
        block("SDRAM", 0x80000000L, 0x1000000, false, true, false, false); // waveform FB at 0x80700000

        // --- analysis options: find Thumb functions the entry-point walk misses ---
        setAnalysisOption(currentProgram, "ARM Aggressive Instruction Finder", "true");

        // --- Thumb everywhere (skip if instructions already exist, e.g. on -process) ---
        Register tm = currentProgram.getRegister("TMode");
        MemoryBlock f = mem.getBlock("APP_FLASH");
        if (tm != null && currentProgram.getListing().getInstructions(f.getStart(), true).hasNext() == false) {
            currentProgram.getProgramContext().setValue(tm, f.getStart(), f.getEnd(), BigInteger.ONE);
        }

        // --- vector table (106 entries incl. SP) ---
        Address vt = a(0xE000);
        for (int i = 0; i < 106; i++) {
            Address p = vt.add(i * 4);
            if (getDataAt(p) == null) createDWord(p);
            long v = mem.getInt(p) & 0xFFFFFFFFL;
            if (i == 0) { setEOLComment(p, "initial SP"); continue; }
            if (v == 0) continue;
            Address h = a(v & ~1L);
            disassemble(h);
            if (getFunctionAt(h) == null) createFunction(h, null);
            setEOLComment(p, i < 16 ? "exception " + i : "IRQ " + (i - 16));
        }
        println("Setup2C53P done");
    }
}
