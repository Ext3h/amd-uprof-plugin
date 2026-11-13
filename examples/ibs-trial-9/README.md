# Scenario: IBS trial 9, vendor driver versus workload (not yet reproducible)

**Status: not reproducible.** The capture exists only in the session where the trial ran; it is
not in this repository, and an IBS capture needs an AMD Zen host with IBS available and an
elevated capture. There is no check for this scenario. Record the capture or its provenance
here when one is available.

Recorded from issue #37; skill 2.1.0.

## Input

An IBS OP, cycle-controlled capture translated with `translate --ascii ibsop-event-dump`:
954 samples, `storage_test.exe` attached (PID 3112), `amdvlk64.dll` without a PDB.

## Prompt

> Where did the CPU time go? Give the module breakdown, say whether the cost of the hottest
> third-party module can be changed by the caller, and name the hot instruction or RVA if
> symbols are missing.

## Reference run with skill 2.1.0 (historical path)

The issue recorded 5 tool calls: Read `SKILL.md`, `profile --by process`, `profile --by
module`, `profile --by function`, and the hand-back. That is 3 `profile` calls under the
counting used in `timer-heap-churn/` (loading the skill and the hand-back are not counted). The
run took 2 min 11 s and read the skill first. It is a record of that run, not the current
minimal path: since #116 the skill picks the first call from the question, and this capture is
attached, so the process ranking would be skipped.

## Expected answer and acceptance

- `amdvlk64.dll`: 51.78% `sample_share`. It is a system-flagged vendor driver and the dominant
  dependency outside the workload; the caller cannot change its cost directly.
- Strict third-party total (`isSystemModule=false`): 9.01%, of which `storage_test` (the
  workload itself) is 8.70%; the caller can change that cost.
- Outside the workload: 91.30% (100% − 8.70%), of which 90.99% is system-flagged
  (`amdvlk64.dll` alone 51.78%) and 0.31% is other non-system modules. These two figures are
  derived from the numbers above, not read from the capture, which is not available.
- Hot RVA in `amdvlk64.dll`: 0x4E0FBA with 12 samples. The largest derived function is
  `pdata_0x2EC1C30` with 65 samples.
- Global `HOT_RVA`: `ntdll` `RtlQueryPerformanceCounter` 0x8B28A, 22 of 25 samples.
- User mode only.
- The rows are reported as derived or unnamed, never as unresolved.
