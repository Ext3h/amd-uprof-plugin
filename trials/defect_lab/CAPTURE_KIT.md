# Capture kit (temporary branch)

**Temporary:** this branch, `capture-kit`, exists so captures can be recorded on another Windows
machine. It is not reviewed and not test-complete; the proper version follows in a pull request.
Delete the branch when the captures are handed over.

## What it records

Per defect class (memory, lock, branch, os, vendor, wait, chain, efficient), `defect_lab.exe`
runs one class while 24 stressor processes (`busy_01.exe` ... `busy_24.exe`, copies of
`lab_busy.exe`) load the machine. A 3 s system-wide capture is recorded in each mode the CPU
allows:

- `timer`: `AMDuProfCLI collect -e event=timer,interval=10` (any x86 CPU)
- `ibs`: `uprof.py collect --event ibs-op` (AMD CPU with IBS; VBS/Hyper-V must be off, see below)
- `pmc`: `uprof.py collect --event cycles` (core PMC sampling)

Two plain captures without workload (`plain.ibs`, `plain.pmc`) record only the `session.uprof`
key names, for #237.

## Steps

1. **Install:** Python 3.9 or newer, `python -m pip install duckdb==2.0.0`, LLVM (clang and lld-link; the Windows installer from the LLVM releases), and AMD uProf 5.3 with `AMDuProfCLI` on PATH.
2. **Get the branch:** `git fetch origin capture-kit`, then `git checkout capture-kit`.
3. **Build the workload:** `python trials\defect_lab\build.py C:\lab\bin`
4. **Check what the CPU can record:**
   - IBS needs VBS and Hyper-V off. If `collect` prints `SAMPLER: falling back to cycle sampling ... VBS is running`, the `ibs` captures are cycle captures. Either turn VBS off (as administrator: `bcdedit /set hypervisorlaunchtype off`, disable Memory integrity, reboot) or leave `ibs` out of `--modes`.
   - Kernel samples need an elevated prompt; user-mode samples do not.
5. **Record and anonymize, about 15 minutes:**
   `python trials\defect_lab\record.py C:\lab\bin C:\lab\raw --anonymize C:\lab\out`
   - A mode the machine cannot record is printed as `skipped <class> <mode>: <cause>`.
   - Every capture ends with `anonymize <class>.<mode>: exit 0`. An exit 1 means `review.txt` still names this machine, the user or a profile path: do not hand that folder over.
6. **Review:** read `C:\lab\out\<class>.<mode>\review.txt`. It holds every table of the filtered capture as text and, after `== distinct strings`, every path and name in it. Only `defect_lab.exe`, `busy_NN.exe`, `lab_core.dll`, `lab_vendor.dll` under `c:\capture\`, Windows system modules, function names and timestamps should appear.
7. **Hand over:**
   - `C:\lab\out\` with each folder's `cpu.db`, `session.uprof` and `review.txt`;
   - the `*.keys.txt` files from `C:\lab\raw\`: key names and value types only, no values.

   Do not hand over `C:\lab\raw\` itself: the raw and translated captures name the machine, the user and every running process.

## Files

| File | Role |
|---|---|
| `trials/defect_lab/*.c`, `lab.h`, `kernel32.def`, `build.py` | the workload and its build (freestanding C, no C runtime) |
| `trials/defect_lab/record.py` | starts the stressors and the class, records each mode, translates, writes `keys.txt`, anonymizes |
| `trials/anonymize_capture.py` | keeps the named processes, deletes the rest, rewrites host paths, empties HostInfo, writes `review.txt` and checks it |
