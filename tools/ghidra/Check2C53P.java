// Print key symbols to confirm Setup2C53P applied. Run: import.sh -check
//@category 2C53P
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.*;

public class Check2C53P extends GhidraScript {
    @Override
    public void run() throws Exception {
        println("CHECK functions=" + currentProgram.getFunctionManager().getFunctionCount()
              + " blocks=" + currentProgram.getMemory().getBlocks().length);
        for (long x : new long[]{0x3F6C8, 0x3C510, 0x37084, 0x37D08, 0x418A4, 0xE1BC}) {
            Function f = getFunctionAt(toAddr(x));
            println("CHECK " + Long.toHexString(x) + " -> " + (f == null ? "NO FUNCTION" : f.getName()));
        }
        for (long x : new long[]{0x200019A9L, 0x20000128L, 0x80700000L}) {
            StringBuilder sb = new StringBuilder();
            for (Symbol s : currentProgram.getSymbolTable().getSymbols(toAddr(x)))
                sb.append(s.getName()).append(s.isPrimary() ? "*" : "").append(' ');
            println("CHECK " + Long.toHexString(x) + " -> " + (sb.length() == 0 ? "NO SYMBOL" : sb));
        }
        ghidra.program.model.mem.MemoryBlock fl = currentProgram.getMemory().getBlock("APP_FLASH");
        long code = 0, data = 0, undef = 0;
        for (Address p = fl.getStart(); p.compareTo(fl.getEnd()) < 0; ) {
            CodeUnit cu = currentProgram.getListing().getCodeUnitAt(p);
            if (cu == null) { undef++; p = p.add(1); continue; }
            if (cu instanceof Instruction) code += cu.getLength(); else if (((Data) cu).isDefined()) data += cu.getLength(); else undef += cu.getLength();
            p = cu.getMaxAddress().add(1);
        }
        println("CHECK coverage code=" + code + " data=" + data + " undef=" + undef + " of " + fl.getSize());
        ghidra.framework.options.Options o = currentProgram.getOptions("Analyzers");
        for (String n : o.getOptionNames())
            if (n.contains("Aggressive") || n.contains("Thumb") || n.contains("Function Start"))
                println("CHECK opt " + n + " = " + o.getValueAsString(n));
    }
}
