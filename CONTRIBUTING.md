# Contributing

Short rules, each with the reason. A pull request that breaks one of them is closed or sent back.

## Issue first

- Every pull request links an existing issue that a maintainer has accepted (by label or
  comment). Pull requests without one are closed unreviewed.
  Why: the design decision happens on the issue, before anyone spends review time on code.
- One issue per pull request; keep pull requests small.
  Why: small, single-purpose changes are reviewed fast and reverted cleanly.

## Test first

- The failing test that shows the defect is its own commit, before the fix. Tests follow the
  existing style (`tests/run_tests.py`, `unittest`).
  Why: the history proves the test fails without the fix and passes with it.
- The full suite passes (`python tests/run_tests.py`) and CI is green.
  Why: the suite is the contract; a red build blocks everyone.

## Design constraints

See issue #251; the list is being finalized.

## Sensitive changes

- Changes to CI workflows, fixtures, or shipped skill text get maintainer review, and the pull
  request states why the change is needed.
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
