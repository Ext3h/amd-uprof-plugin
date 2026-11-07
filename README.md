# amd-uprof plugin

Claude Code plugin packaging of the `amd-uprof` skill: capture AMD uProf cycle or IBS OP
samples, symbolize them with llvm-symbolizer, and query the capture in DuckDB.

    claude plugin marketplace add D:\Projects\amd-uprof-plugin
    claude plugin install amd-uprof@amd-uprof-dev

The skill lives in `skills/amd-uprof/`; `SKILL.md` is the entry point and `tools/uprof.py` the
command-line surface. Validate a change with `claude plugin validate .`.
