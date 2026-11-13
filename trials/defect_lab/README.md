# defect_lab

A small Windows program with one defect class per function, for the open-ended trial scenarios
("`lab_core.dll` is my module; what can I improve?"). `lab_core.dll` is the user's module;
`lab_vendor.dll` stands in for third-party code it calls; `lab_busy.exe` is background load. The
source is the repository's own and is distributed under the repository's license.

## Building

Needs clang, lld-link and llvm-dlltool from LLVM. No C runtime and no Windows SDK: the images
are freestanding C, and kernel32 comes from `kernel32.def`, as for the timer fixture.

```console
python trials/defect_lab/build.py <OUT_DIR>
```

`tests/test_defect_lab.py` builds the images and checks the class code: the functions exist,
`lab_lock` holds `lock cmpxchg` and `pause`, `lab_chain` holds `imul` and `div`,
`lab_efficient` holds `mulps`/`addps`, and `lab_branch` keeps a conditional jump with no
`cmov`. The recipe does not make the images reproducible byte for byte, so the test checks
content, not bytes.

## Running

```console
defect_lab.exe --class <class> [--seconds S] [--load N] [--control]
```

- **`--class`:** memory, lock, branch, os, vendor, wait, chain or efficient.
- **`--load N`:** starts N `lab_busy.exe` processes first.
- **`--control`:** also runs the class's control variant and prints nanoseconds per iteration for both, to check that the class reproduces its defect.

| Class | Code in `lab_core.dll` | Control variant |
|---|---|---|
| memory | random loads over 256 MiB | the same loads over 4 KiB |
| lock | two threads on a `lock cmpxchg` spin lock with `pause` | one thread |
| branch | a conditional jump on random data | the same jump, alternating |
| os | `HeapAlloc`/`HeapFree` per iteration (time in `ntdll.dll`) | no call |
| vendor | a call into `lab_vendor.dll`'s hot export | no call |
| wait | `WaitForSingleObject` with a 5 ms timeout between short bursts of work | the work without the wait |
| chain | a serial `imul`/`div` chain with a divisor known only at run time | four independent chains |
| efficient | a vectorized multiply-add over L1-resident arrays | the same (no defect) |

Measured on the development host (Intel CPU, Windows 11), one second per variant, in ns per
iteration (class, control):
- memory 15 vs 1, lock 34 vs 7, branch 4 vs 1, os 54 vs 1, vendor 2,222 vs 1, chain 10 vs 7.
- wait: 15.6 ms per iteration against 0.03 ms; the 5 ms wait rounds up to the 15.6 ms timer tick.
- efficient: below 1 ns in both.

## Captures

The scenarios' captures are recorded on the owner's reference machine, not committed from a
developer host. `record.py` runs each class beside 24 stressors (`busy_01.exe` ...
`busy_24.exe`, copies of `lab_busy.exe`) and records a 3 s system-wide capture per mode the CPU
allows (timer, IBS, PMC), plus plain IBS and PMC captures whose `keys.txt` lists the
`session.uprof` key names. With `--anonymize <OUT_DIR>` it runs `trials/anonymize_capture.py` on
each: only defect_lab and the stressors remain, host paths are rewritten, HostInfo is emptied,
and `review.txt` dumps every table for a person to read. Nothing is committed before that
review.

```console
python trials/defect_lab/build.py <BIN_DIR>
python trials/defect_lab/record.py <BIN_DIR> <RAW_DIR> --anonymize <OUT_DIR>
```

Each class is slower than its control. chain's control gains only 1.4x because independent
divides share the divider. Not verified: the same ratios on an AMD host.
