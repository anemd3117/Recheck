# Deterministic comparison corpus

The six C programs accept bounded values and avoid signed overflow in the registered cases.
`oracle.json` freezes literal expected UTF-8 stdout, stderr and exit codes before execution or AI review.
`MUTANT=1` selects one documented semantic change; `ALTERNATE=1` selects an equivalent implementation.
The evaluation builds O0, O2, alternate, mutant and stripped variants. The identical comparison runs the O0 binary again.

Run from the source root:

```powershell
.venv\Scripts\python scripts\evaluate_accuracy.py --output verification\recheck-next-20261116\accuracy
```

ELF compilation uses a local Docker compiler image; target execution uses Recheck's `ManagedExecutionAdapter` and the packaged immutable Linux execution image. PE x64 compilation uses installed MinGW when available; a PE build is not a claim of Windows execution support. The harness records missing tools and images as blockers.

Left-side observations are collected once per case and reused across comparison types, explicitly recorded in the receipt. Right-side identical binaries are executed separately. Accuracy rows retain every registered case; blocked and interrupted cases do not enter the measured denominator. This harness tests the real adapter and comparison function; it does not claim GUI or whole-engine workflow coverage.

Mutations: arithmetic adds one only at zero; branch changes <=100 to <100; loop changes inclusive accumulation to exclusive; strings counts empty input as one byte; statistics adds one to sum when the two smallest values repeat; io omits the argument delta. UTF-8 string length is byte length, not Unicode character count.

`recovery.c` only exercises bounded-container cleanup for time limit, output limit and a paused child. It never changes system settings or user files.

`many_functions.cpp` supplies 129 noinline C++ template instantiations for the decompilation cap. Its defined output is the bounded integer input plus 8256; invalid input prints `invalid integer` to stderr and exits 2. The harness compiles base and stripped ELF/PE variants as static-analysis stress artifacts. These extra artifacts are outside the six-program dynamic accuracy denominator.

The oracle has 71 registered inputs, including invalid inputs and UTF-8 byte cases. Across the five comparison types it defines 355 pair verdicts, with exactly 19 expected differences in the mutant cases. Each baseline observation is reused across the five comparisons; the identical right-hand observation is a fresh target execution. A full sweep collects 426 separate target executions.
