// Recheck Headless evidence exporter, schema/script version 1.0.
// Decompiler output is tool-estimated code, never original source.
// @category Recheck
import ghidra.app.util.headless.HeadlessScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.framework.Application;
import ghidra.program.model.block.*;
import ghidra.program.model.listing.*;
import com.google.gson.GsonBuilder;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class RecheckExport extends HeadlessScript {
    private static final int MAX_FUNCTIONS = 10000;
    private static final int MAX_BLOCKS = 10000;
    private static final int MAX_INSTRUCTIONS = 100000;
    private static final int MAX_DECOMPILE_FUNCTIONS = 100;
    private static final int MAX_DECOMPILE_CHARS = 32768;
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 2) throw new IllegalArgumentException("output.json completion.marker required");
        Map<String,Object> root = new LinkedHashMap<>();
        root.put("schema_version", "1.0");
        root.put("script_version", "1.0");
        root.put("tool", "Ghidra Headless");
        root.put("tool_version", Application.getApplicationVersion());
        root.put("sha256", currentProgram.getExecutableSHA256());
        root.put("language", currentProgram.getLanguageID().toString());
        root.put("compiler", currentProgram.getCompilerSpec().getCompilerSpecID().toString());
        root.put("executable_format", currentProgram.getExecutableFormat());
        root.put("analysis_timed_out", analysisTimeoutOccurred());
        root.put("decompilation_provenance", "Ghidra decompiler estimated code; not original source");
        root.put("decompile_policy", "First 100 discovered functions; 10s/function; 32768 characters/function");
        List<Map<String,Object>> functions = new ArrayList<>();
        FunctionIterator it = currentProgram.getFunctionManager().getFunctions(true);
        DecompInterface decompiler = new DecompInterface();
        decompiler.openProgram(currentProgram);
        int total = currentProgram.getFunctionManager().getFunctionCount();
        long decompileDeadline = System.currentTimeMillis() + 60000;
        root.put("total_functions", total);
        root.put("functions_truncated", total > MAX_FUNCTIONS);
        try {
            while (it.hasNext() && functions.size() < MAX_FUNCTIONS && !monitor.isCancelled()) {
                Function f = it.next();
                Map<String,Object> item = new LinkedHashMap<>();
                item.put("id", f.getEntryPoint().toString());
                item.put("address", f.getEntryPoint().toString());
                item.put("name", f.getName());
                item.put("signature", f.getPrototypeString(false,false));
                item.put("external", f.isExternal());
                item.put("thunk", f.isThunk());
                item.put("size", f.getBody().getNumAddresses());
                List<String> mnemonics = new ArrayList<>();
                InstructionIterator instructions = currentProgram.getListing().getInstructions(f.getBody(), true);
                while (instructions.hasNext() && mnemonics.size() < MAX_INSTRUCTIONS) {
                    Instruction instruction = instructions.next();
                    // Address/operand-independent mnemonic sequence.
                    mnemonics.add(instruction.getMnemonicString().toLowerCase(Locale.ROOT));
                }
                item.put("mnemonics", mnemonics);
                item.put("instructions_truncated", instructions.hasNext());
                item.put("instruction_count", mnemonics.size());
                List<Map<String,Object>> blocks = new ArrayList<>();
                List<List<String>> edges = new ArrayList<>();
                CodeBlockIterator blockIterator = new BasicBlockModel(currentProgram).getCodeBlocksContaining(f.getBody(), monitor);
                while (blockIterator.hasNext() && blocks.size() < MAX_BLOCKS) {
                    CodeBlock block = blockIterator.next();
                    Map<String,Object> node = new LinkedHashMap<>();
                    String start = block.getFirstStartAddress().toString();
                    node.put("id", start);
                    node.put("start", block.getMinAddress().toString());
                    node.put("end", block.getMaxAddress().toString());
                    node.put("size", block.getNumAddresses());
                    blocks.add(node);
                    CodeBlockReferenceIterator references = block.getDestinations(monitor);
                    while (references.hasNext() && edges.size() < MAX_BLOCKS * 8) {
                        CodeBlockReference reference = references.next();
                        edges.add(Arrays.asList(start, reference.getDestinationAddress().toString()));
                    }
                }
                item.put("blocks", blocks);
                item.put("edges", edges);
                item.put("block_count", blocks.size());
                item.put("edge_count", edges.size());
                item.put("cfg_truncated", blockIterator.hasNext() || edges.size() >= MAX_BLOCKS * 8);
                List<String> calls = new ArrayList<>();
                for (Function called : f.getCalledFunctions(monitor)) calls.add(called.getEntryPoint().toString());
                Collections.sort(calls);
                item.put("calls", calls);
                item.put("decompile_status", "not_selected");
                if (functions.size() < MAX_DECOMPILE_FUNCTIONS && !f.isExternal() && System.currentTimeMillis() < decompileDeadline) {
                    DecompileResults d = decompiler.decompileFunction(f, 10, monitor);
                    if (d.decompileCompleted() && d.getDecompiledFunction() != null) {
                        String code = d.getDecompiledFunction().getC();
                        item.put("decompile", code.substring(0, Math.min(MAX_DECOMPILE_CHARS, code.length())));
                        item.put("decompile_truncated", code.length() > MAX_DECOMPILE_CHARS);
                        item.put("decompile_status", "success");
                    } else {
                        item.put("decompile_status", "failed");
                        item.put("decompile_error", d.getErrorMessage());
                    }
                }
                functions.add(item);
            }
        } finally {
            decompiler.dispose();
        }
        root.put("functions", functions);
        root.put("complete", !monitor.isCancelled());
        Path output = Paths.get(args[0]);
        Path temporary = Paths.get(args[0] + ".tmp");
        Files.writeString(temporary, new GsonBuilder().setPrettyPrinting().create().toJson(root), StandardCharsets.UTF_8);
        Files.move(temporary, output, StandardCopyOption.REPLACE_EXISTING);
        if (!monitor.isCancelled()) Files.writeString(Paths.get(args[1]), "RECHECK_COMPLETE_1", StandardCharsets.UTF_8);
        println("RECHECK_EXPORT functions=" + functions.size() + " timedOut=" + analysisTimeoutOccurred());
    }
}
