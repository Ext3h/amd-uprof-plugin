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

## Design constraints

See issue #251; the list is being finalized.

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
