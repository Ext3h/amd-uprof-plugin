# Observer rubric

How to judge a trial run, separately from the run itself. The observer reads the run log
(`<RUN_DIR>/runlog.md`), each trial's neutral transcript (`<RUN_DIR>/out/<SCN>/transcript.jsonl`,
schema in `runners/__init__.py`) and `scenarios.md`. It never talks to a running trial. Read the
transcripts with a script: they can be large.

## What the skill must deliver

- **Actionable:** the offending instruction range or source lines, with definitive proof and a
  well-founded estimate of what the hotspot costs (the answer contract in `scenarios.md`).
- **Scoped:** the user's own components may be drilled; components outside their control keep
  their full share in one summary line, neither understated nor listed in detail.
- **Fast:** minimal wall time; no call that the outputs could have made unnecessary; as little
  context as the minimal calls need. The limit in `scenarios.md` is a defect ceiling, not a
  target: a run below it can still waste context.
- **Honest about gaps:** no caller-path attribution into outside code (#187); no claim the
  capture cannot support.

## Per trial

| Item | Record |
|---|---|
| Outcome | `correct` / `partial` / `wrong` / `gave up`, against the scenario's answer; name what is missing or wrong |
| Calls | the `uprof.py` calls made versus the scenario's minimal calls; list every extra call |
| Extra-call cause | for each extra call: (a) misleading or missing tool output (NEXT, READ or a cause line); (b) SKILL.md or reference guidance; (c) a model error unrelated to the guidance; (d) the environment |
| Issue | for each (a)/(b) cause: the existing issue it maps to, or `new` |
| Overclaims | any claim beyond the evidence: cost overstated, caller paths, wrong module ownership, a share compared across captures |
| Cost | wall time and, if the runner reported them, tokens or price |
| Context growth | the calls and outputs that grew the context beyond what the minimal calls need (extra calls, re-runs, oversized outputs), each with the same causes as extra calls |
| Context | the run log's final-turn context and its verdict; a run passes only with outcome `correct` and context below the limit in `scenarios.md` (Pass criteria); `not measured` cannot pass |

Count a call as extra when the scenario's answer was reachable without it. Reading SKILL.md and
writing the answer are not calls. A retry after the agent's own syntax error is cause (c).

## Summary

One table over all trials (scenario, outcome, context, pass, calls made / minimal, extra calls
by cause), then
the top five causes of extra calls ranked by calls lost across trials, each with its issue.
