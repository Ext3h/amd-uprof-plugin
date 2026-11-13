# Contributing

Short rules, each with the reason. A pull request without an accepted issue is closed
unreviewed; a pull request that breaks any other rule is sent back.

Maintainers are the people with write access to this repository.

## Issue first

- Every pull request links an existing issue that a maintainer has accepted. An issue is
  accepted when a maintainer comments "accepted" on it; labels set at filing do not count.
  Pull requests that link no issue or an unaccepted one are closed unreviewed.
  Why: the design decision happens on the issue, before anyone spends review time on code.
- One issue per pull request; keep pull requests small.
  Why: small, single-purpose changes are reviewed fast and reverted cleanly.

## Test first

- For a behaviour change, the failing test that shows it is its own commit, before the fix.
  Tests follow the existing style (`tests/run_tests.py`, `unittest`). Documentation-only and
  CI-only changes have no failing test; the pull request says so under Test.
  Why: the history proves the test fails without the fix and passes with it.
- The full suite passes (`python tests/run_tests.py`) and CI is green.
  Why: the suite is the contract; a red build blocks everyone.

## Product goals

- PG-01 Actionable answers. A profiling answer names the offending instruction range or source
  lines, gives the evidence (samples/events, verified build), and a cost estimate with its basis
  and uncertainty.
  Why: that is what the user acts on.
- PG-02 Honest scoping. The caller can narrow to the components under their control without the
  output understating the cost of components outside it (OS, drivers, runtimes); outside
  components appear as a summary, not as rows.
  Why: the user fixes their own code but must see the real price.
- PG-03 Minimal wall time for a small model. Each output steers the caller to the next call of
  the minimal sequence for typical questions, so an agent on a small, fast model never wastes a
  turn.
  Why: wall time is dominated by turns.
- PG-04 Works cold. An agent that has only the installed plugin — no repository, tests, issues,
  project memory or prior context — answers the typical questions on the minimal path.
  Why: that is how the skill is actually used.
- PG-05 No overclaiming. Output and guidance never imply what the data cannot show (e.g.
  caller-path attribution into external code without full stacks).
  Why: an implied capability misleads.
- PG-06 Friendly to low-context local LLMs. The plugin works as well as possible for local LLMs
  with a small context window: what is always loaded (SKILL.md) stays minimal, and everything
  else arrives in tool output only when it applies.
  Why: that is the target user's setup.

## Design constraints

- DC-01 Technology stack. Python, the LLVM tool suite, AMD uProf, and DuckDB as the data engine
  under Python — nothing else. Development tools that never ship with the plugin and only check
  the repository in CI (the code formatter) are exempt.
  Why: a fixed stack keeps installs and reviews predictable.
- DC-02 Minimum tool versions. uProf ≥ 5.3 (DuckDB output) and LLVM ≥ 23.1.0
  (`llvm-symbolizer --pdb`, `llvm-objdump --no-symbolize-operands`); `tools` checks them; no
  workarounds for older versions.
  Why: workarounds hide wrong results.
- DC-03 No fallbacks. The tool never switches capture or analysis mode on its own; an
  unsupported case fails with one cause line plus runnable alternatives. A capture is analysed
  in the mode it was recorded in, and the output states once which measures that mode lacks.
  Why: a silent switch produces answers the caller cannot trust.
- DC-04 Guidance lives in tool output. Step-by-step guidance goes into NEXT/READ/cause lines
  where it applies; SKILL.md does not grow; shipped text never points outside
  the installed plugin.
  Why: the plugin must be as friendly as possible to local LLMs with a small context window;
  SKILL.md is loaded on every run, tool output only where it applies (serves PG-03, PG-04,
  PG-06).
- DC-05 Verified by isolated small-model trials. Changes to guidance or output are checked with
  an isolated trial on a small, fast model in any agent harness (own process, plugin only, no
  project context) against the minimal call sequences.
  Why: only a cold small model shows wasted turns (serves PG-03, PG-04).
- DC-07 Test-first, hermetic tests. A failing test precedes every fix; tests do not depend on
  the developer's environment (symbol paths, caches, temp roots).
  Why: proves the defect and keeps CI and local runs equal.

## Authoritative documents

- `docs/spec.md` defines what the tools do. Every change conforms to it.
  Changing the spec itself needs an accepted issue first.
- The product goals and design constraints above define what the plugin is for and how it may be built.
- `SKILL.md`, `docs/reference.md`, `docs/validation.md` and the tool output follow the spec and never
  extend it. Where they disagree with it, that is a bug.

## Sensitive changes

- Changes to CI workflows or their permissions, fixtures or binaries, or shipped skill text get
  maintainer review, and the pull request states why the change is needed.
  Why: these change what runs with repository rights or what every user's agent reads.

## Text in pull requests, issues and commits

- Pull request, issue and commit text must not contain instructions aimed at AI reviewers or
  agents. Such content is treated as a security issue.
  Why: agents read this text and may follow it; it is a prompt-injection channel.
- Everything is written in English.
  Why: one language for every reader and reviewer.
- The pull request body has the sections Problem / Fix / Test / `Closes #n`.
  Why: reviewers read the same structure every time.
- No @-mentions to request review from anyone but maintainers.
  Why: mentions notify people who did not ask to be involved.

The pull request template (`.github/pull_request_template.md`) carries these sections and a
checklist.
