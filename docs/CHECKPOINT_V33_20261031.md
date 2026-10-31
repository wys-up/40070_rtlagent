# v33 Published Checkpoint

Published and deployed at the owner's explicit request on 2026-10-31.
This is a diagnostic checkpoint, not a claim that all release gates passed.

## Changes

- Parse complete literal Moore and Mealy diagrams with correct control order,
  reset/clock handling, current-input Mealy outputs and one-hot encoding.
- Expand one-hot next-state equations using incoming-edge OR terms, including
  zero and multi-hot present-state values.
- Fix coincident waveform clock/data scheduling and comparison-history feedback.
- Support bounded vector waveform observations and reject ambiguous radix.
- Fall back to general RTL/reference generation after truncated graph derivation.
- Add independent recurrence checks, altered specifications and faulty RTL tests.

No exercise identifier, stored answer or task-specific protocol template is
used. Literal implementations still undergo compile, synthesis and simulation.
The official baseline and API service code are unchanged.

## Validation

The frozen source-v33 passed 142 Linux/Vivado unit tests. Local Windows tests
reported 128 tests, OK with 15 environment-dependent skips.
Twenty historical regression tasks produced 18/20 external L3 results.
The two remaining L1 tasks were Prob136_m2014_q6 and Prob140_fsm_hdlc.
Internal verification accepted only 14/20; reference reliability remains limited.

Fresh random seed: 7192980704623098516. Five tasks were frozen before reading
their prompts/results, excluding all previous sampled and development tasks.
Both solvers ran once per task against the same Qwen2.5-Coder-32B-Instruct
Q4_K_M model. Agent temperature was 0.2; official baseline retained temperature 0.
Solver time excludes separate external judging. No failures were replaced.

| Task | Agent Level / Seconds | Baseline Level / Seconds |
| --- | --- | --- |
| Prob131_mt2015_q4 | L1 / 112.402 | L0 / 16.204 |
| Prob017_mux2to1v | L3 / 49.249 | L3 / 3.420 |
| Prob007_wire | L3 / 37.024 | L3 / 1.818 |
| Prob130_circuit5 | L3 / 36.120 | L0 / 8.321 |
| Prob054_edgedetect | L3 / 76.817 | L1 / 4.089 |

Agent: 4/5 L3, mean 62.322 seconds, mean official coefficient 0.84.
Baseline: 2/5 L3, mean 6.770 seconds, mean official coefficient 0.44.
Agent was approximately 9.21 times slower on this sample.
Single-sample measurements do not establish universal performance or improved
model reasoning.

## Known Issues

- Literal waveform parsing can mistake a submodule waveform for top-level
  behavior when their port names match. Prob131_mt2015_q4 exposes this scope
  error; its returned RTL also misreads the submodule truth table.
- Some generated references invent state for combinational interfaces or
  disagree on protocol output timing. Review agreement is not ground truth.
- Simple tasks retain substantial reference/review overhead.
- Original complex-five regression remained 5/5 L3, but mean solver time rose
  from 96.025 to 143.249 seconds; this is not a universal speed improvement.
- Literal Mealy candidate and runtime reference share a generic compiler;
  independent unit-test recurrences and external judging are bounded evidence,
  not a formal proof.

## Evidence

Remote benchmark root:
`/workspace/operations/benchmarks/closure-20261031`.

Frozen code: `source-v33`; unit results: `unit-v33`.
Historical regression directories: `after-random2-v33`, `random-33`,
`development-33`, `fresh-random3-v33`.
Fresh comparison: `paired-random4-v33`, with seed/population/exclusions in
`random-selection-4.json`. Detailed prompts, returned RTL, traces and external
verdicts remain in those directories outside the submission repository.
