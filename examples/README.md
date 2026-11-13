# Trial scenarios

Recorded profiling questions with their input, the expected answer and reference metrics, so a
fresh agent can be given the same prompt and its answer compared. This folder is outside the
plugin package (`plugin/`): it is not installed with the skill.

| Scenario | Capture | Reproducible here |
|---|---|---|
| [`timer-heap-churn/`](timer-heap-churn/README.md) | timer sampling (`--config tbp`), committed fixture | yes; `tests/test_examples.py` checks it against the current tools |
| [`ibs-trial-9/`](ibs-trial-9/README.md) | IBS OP, cycle-controlled | no: the capture is not available and IBS needs an AMD Zen host with privileges |

Each scenario directory holds the prompt, the input or its provenance, the expected answer, the
reference metrics and the skill version the answer was recorded with.
