# Trials

Checks that the skill's guidance and tool output lead an agent on a small, fast model along the
minimal call sequence to an actionable answer. A trial gives a fresh agent only the user's
question, a capture and the installed plugin, then compares its calls and answer with the
catalogue. This folder is outside the plugin package (`plugin/`): it is not installed with the
skill.

| File | Role |
|---|---|
| [`scenarios.md`](scenarios.md) | the catalogue: per scenario the question, the session, the correct answer and the minimal calls |
| [`build_captures.py`](build_captures.py) | writes the synthetic sessions the scenarios run on |

`tests/test_trials.py` runs in CI: every catalogue call parses with the current CLI, every
session it names is one the builder writes, and every image a built session records passes the
tools' build-identity checks. The trial harness and the observer rubric are tracked in #254.

## Building the captures

Needs what the tools need: Python 3.9 or newer with the `duckdb` 2.x package.

```console
python trials/build_captures.py <OUT_DIR>
python trials/build_captures.py <OUT_DIR> attached pdb_far
```

`<OUT_DIR>` must not exist. It receives one directory per session and `bins/` with the images
those sessions record. The images are copies of committed test fixtures, and the PE CheckSum and
SizeOfImage recorded in `cpu.db` are read from those copies, so a rebuilt fixture never leaves a
stale identity behind. The other modules (`amdvlk64.dll`, `ntdll.dll`, `node.exe`) are the
repository's own `tests/fixtures/fixture.exe` under those names, never a copied vendor or OS
binary.

Module paths in a session are the absolute paths of the images under `<OUT_DIR>`, so build the
captures on the host that analyses them and do not move the folder afterwards.

## Running a scenario by hand

Build the session named in the scenario, replace the placeholders in its question with the real
paths (`<CAPTURE>` is `<OUT_DIR>/<session>`; for SCN-04 `<PDB_DIR>` is `<OUT_DIR>/bins/pdbs`;
for SCN-18 `<IMAGE_DIR>` is `<OUT_DIR>/bins/timer`), and give the question to an agent that has
only the plugin installed. Compare its `uprof.py` calls with the scenario's minimal calls and its
answer with the scenario's answer.
