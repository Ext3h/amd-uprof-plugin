# Capture kit (temporary branch)

**Temporary:** this branch, `capture-kit`, exists so captures can be recorded on another Windows
machine. It is not reviewed and not test-complete; the proper version follows in a pull request.
Delete the branch when the captures are handed over.

## What it records

Per defect class (memory, lock, branch, os, vendor, wait, chain, efficient), `defect_lab.exe`
runs one class while 15 stressor processes (`busy_01.exe` ... `busy_15.exe`, copies of
`lab_busy.exe`) load the machine. A 2 s system-wide capture is recorded in each mode the CPU
allows:

- `timer`: `AMDuProfCLI collect -e event=timer,interval=4` (any x86 CPU)
- `ibs`: `uprof.py collect --event ibs-op --sample-interval 16000000` (AMD CPU with IBS; VBS/Hyper-V must be off, see below)
- `pmc`: `uprof.py collect --event cycles --sample-interval 4ms` (core PMC sampling)
- `timer-cg`, `ibs-cg`: the timer or IBS event with `AMDuProfCLI collect --call-graph-mode fpo --call-graph-type user --call-graph-depth 64` (user-mode call stacks, at most 64 frames, unwind information where frame pointers are missing), for the caller paths of #187. AMDuProfCLI on Windows records no call stacks system-wide (`-a` prints `Callstack sampling in System-wide profiling is not supported on Windows`), so these modes profile only a defect_lab that AMDuProfCLI launches; the stressors still load the machine but are not in the capture. Each also writes `notes.txt` with the call graph's cost: defect_lab's ns per iteration in a run without profiling and in a run during a discarded call-graph collection.

The coarse intervals and short runs keep each capture at about 1–2 MB with its dump (estimated),
with a few hundred samples in the class's own module. `--seconds`, `--ibs-interval`,
`--timer-ms`, `--pmc-interval` and `--stressors` change them.

Two plain captures without workload (`plain.ibs`, `plain.pmc`) record only the `session.uprof`
key names, for #237.

## Steps

1. **Install:** Python 3.9 or newer, `python -m pip install duckdb==2.0.0`, LLVM (clang and lld-link; the Windows installer from the LLVM releases), and AMD uProf 5.3 with `AMDuProfCLI` on PATH.
2. **Get the branch:** `git fetch origin capture-kit`, then `git checkout capture-kit`.
3. **Build the workload:** `python trials\defect_lab\build.py C:\lab\bin`
4. **Check what the CPU can record:**
   - IBS needs VBS and Hyper-V off. If `collect` prints `SAMPLER: falling back to cycle sampling ... VBS is running`, the `ibs` captures are cycle captures. Either turn VBS off (as administrator: `bcdedit /set hypervisorlaunchtype off`, disable Memory integrity, reboot) or leave `ibs` out of `--modes`.
   - Kernel samples need an elevated prompt; user-mode samples do not.
   - Call stacks (`timer-cg`, `ibs-cg`) need an elevated prompt and uProf's capture driver installed. `record.py` stops with a cause line when a call-graph mode is asked for without elevation; leave both out of `--modes` to record without them.
   - **Not verified:** the per-process call-graph command has not been run yet. `record.py` passes AMDuProfCLI's console output through, so its warnings appear in the run's log. If an elevated run still shows `false` or `callstack.db` stays empty, report it with the capture.
5. **Record and anonymize, about 15 minutes without the call-graph modes, longer with them:**
   `python trials\defect_lab\record.py C:\lab\bin C:\lab\raw --anonymize C:\lab\out`
   - A mode the machine cannot record is printed as `skipped <class> <mode>: <cause>`.
   - Every capture ends with `anonymize <class>.<mode>: exit 0`. An exit 1 means `review.txt` still names this machine, the user or a profile path: do not hand that folder over.
6. **Review:** read `C:\lab\out\<class>.<mode>\review.txt`. It holds every table of the filtered capture as text and, after `== distinct strings`, every path and name in it. Only `defect_lab.exe`, `busy_NN.exe`, `lab_core.dll`, `lab_vendor.dll` under `c:\capture\`, Windows system modules, function names and timestamps should appear.
   - Each folder also has `images\`: the `defect_lab.exe`, `lab_core.dll` and `lab_vendor.dll` the capture was recorded with, and their PDBs. The anonymizer bundles an image only if its size and checksum match the capture's module, and a PDB only if its GUID and age match the image. `review.txt` lists each file's debug directory and strings under `== bundled images/<name>`; only bare PDB names and `C:\Program Files\LLVM\` toolchain paths should appear there. A host path in them fails the run with exit 1.
   - With `images\`, line and instruction views of the lab's modules should work from the folder alone (not verified end to end yet): `profile ... --by line --function <name> --module c:\capture\lab_core.dll --image-path <folder>\images`. System DLLs (ntdll and the like) are not bundled, so their instruction view is limited to what the host running `profile` has.
7. **Hand over:**
   - `C:\lab\out\` with each folder's `cpu.db`, `session.uprof`, `review.txt` and `images\`, plus the filtered `IbsOpDump.csv` for an IBS capture, `callstack.db` where uProf wrote one, and `notes.txt` for a call-graph capture. `profile` needs that dump and cannot regenerate it without the raw files;
   - the `*.keys.txt` files from `C:\lab\raw\`: key names and value types only, no values.

   Do not hand over `C:\lab\raw\` itself: the raw and translated captures name the machine, the user and every running process.

## Files

| File | Role |
|---|---|
| `trials/defect_lab/*.c`, `lab.h`, `kernel32.def`, `build.py` | the workload and its build (freestanding C, no C runtime; sources compiled under relative names, so no host path reaches the PDBs; frame pointers and unwind tables (`.pdata`) so uProf can walk call stacks through the lab's frames) |
| `trials/defect_lab/record.py` | starts the stressors and the class, records each mode, translates, writes `keys.txt`, anonymizes |
| `trials/anonymize_capture.py` | keeps the named processes, deletes the rest, rewrites host paths, empties HostInfo, bundles the matching images and PDBs (`--bundle`), writes `review.txt` and checks it |
