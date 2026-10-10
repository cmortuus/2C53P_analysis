// Decompile functions to C. Usage: import.sh -decomp <addr> [<addr>...]  (hex, runtime addrs)
// Output goes to ghidra/decomp/<name>_<addr>.c and is echoed to the log.
//@category 2C53P
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.*;
import ghidra.program.model.listing.*;
import java.io.File;
import java.nio.file.Files;

public class Decomp2C53P extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        DecompInterface di = new DecompInterface();
        di.toggleCCode(true); di.setSimplificationStyle("decompile");
        di.openProgram(currentProgram);
        File out = new File(System.getenv().getOrDefault("FNIRSI_ROOT", "."), "ghidra/decomp");
        out.mkdirs();
        for (String s : args) {
            long x = Long.parseLong(s.replaceFirst("^0x", ""), 16) & ~1L;
            Function f = getFunctionContaining(toAddr(x));
            if (f == null) { println("DECOMP " + s + ": no function"); continue; }
            DecompileResults r = di.decompileFunction(f, 120, monitor);
            String c = r.getDecompiledFunction() == null ? "// decompile failed: " + r.getErrorMessage()
                                                          : r.getDecompiledFunction().getC();
            File fo = new File(out, f.getName() + "_" + Long.toHexString(f.getEntryPoint().getOffset()) + ".c");
            Files.writeString(fo.toPath(), c);
            println("DECOMP " + f.getName() + " @" + f.getEntryPoint() + " -> " + fo);
        }
        di.dispose();
    }
}
