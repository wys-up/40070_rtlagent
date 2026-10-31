# Functional verification and repair

Status: published development checkpoint, snapshot `source-v26`.
The original five tasks passed L3, but the frozen random selection passed only
four of five. This checkpoint is not an approved production release.

## Changes

- Generate a specification-only behavioral reference in an isolated model call.
  The reference generator and reviewer never receive candidate RTL, official
  testbenches or reference solutions.
- Small scalar synchronous machines can be derived as complete transition graphs.
  A generic compiler owns state encoding, edge/reset wiring and output decode.
  Fixed-duration phases expand their supplied repeat count, and transition
  emissions compile to a reachable product of phase progress and registered
  output history. The compiler checks all input patterns and reachable phases.
  Multiple fenced drafts are accepted only when they yield exactly one unique
  structurally valid implementation; conflicting valid graphs remain invalid.
  Candidate repair graphs and reference graphs are generated independently;
  neither compiler contains protocol states, frame lengths or exercise answers.
  Unsupported machines fall back to the general RTL/reference path.
- The controller owns testbench instantiation, input generation, clock/reset
  driving, observation phases, output masks and verdicts. Small combinational
  interfaces are exhausted; larger/clocked interfaces receive deterministic
  directed and seeded random vectors.
- Reference reviewers must cite a literal specification requirement, identify
  the relevant reference statement or driver, and provide a concrete
  input sequence and expected observable output when rejecting. Allegations are
  replayed against the independent reference, without candidate RTL. A disproved
  allegation is a review protocol error, not grounds to rewrite the reference.
  A witnessed difference also requires a separate specification-only derivation
  of its expected output, without the reference or reviewer's proposed answer.
  Malformed or timed-out reviews do not approve a checker.
- Complete simulations report total comparisons and failures. Only measurements
  with the same frozen checker and comparison count can replace an equally
  ranked candidate on functional improvement. Aborted runs are not comparable.
- Repair requests receive the first counterexample and bounded input history.
  History records one driven vector per sampling edge, separately from the
  pre-edge/post-edge/opposite-edge comparisons of that vector.
  RTL generation has a separate output bound and can retry a failed request
  while preserving the last usable candidate.
- Reject direct comparisons between an unsigned vector and a negative literal,
  including in independent references. A signed right-hand assignment does not
  make an unsigned temporary signed. Bounds and names are not fixed constants.
- Literal table facts are expanded from the current prompt. Complete labeled
  maps can compile directly to case statements without logic minimization; their
  implementations still undergo interface, semantic, synthesis and simulation
  checks. Axis widths, label permutations, variable names and cells come from
  the current prompt, never a task identifier.
  No exercise name,
  known answer, fixed saturation limit, frame implementation or task-specific
  circuit template is used.

## Validation Policy

Run Linux/Vivado unit tests and all five previous development tasks first.
After development fixes, uniformly sample five tasks without replacement from
the canonical `tasks/veval_v2_clean/tasks` population, excluding the serial
development task. Freeze the seed, population and selection before reading
selected prompts or results. Never replace a failed selection.

Retain every attempt and its exact source hashes. Official fixtures remain
outside solver inputs. A full release requires external L3 for all selected
tasks. This checkpoint is published at the owner's explicit request with the
4/5 result preserved; production synchronization is deferred. Subsequent
changes require rerunning the frozen selection; this then constitutes regression
evidence, not a new unseen holdout result.

Generated reference agreement remains bounded evidence, not a proof of
correctness. A random sample cannot establish universal task generalization.

## Measured Checkpoint

Validation date: 2026-10-31. All rows below use the identical `source-v26`
runtime and tests. The model remained Qwen2.5-Coder-32B-Instruct-Q4_K_M at
temperature 0.2, one sample per task, with a 900-second agent deadline.
Solver time excludes external judging. Internal verification is recorded
separately from the official external level.

Linux/Vivado: `unit-26`, 127 tests passed in 182.284 seconds.

Original five: `development-26`, **5/5 external L3**.

| Task | Solver Seconds | External Level | Internally Verified |
| --- | ---: | ---: | --- |
| Prob137_fsm_serial | 158.278 | 3 | yes |
| sat_accum8 | 98.546 | 3 | no |
| byte_write_register | 85.355 | 3 | yes |
| boundary_popcount191 | 109.038 | 3 | no |
| boundary_kmap_non_gray | 28.908 | 3 | yes |

Mean solver time was 96.025 seconds, compared with 245.881 seconds and 3/5 L3
for the preceding `e4097df` production checkpoint on this group. The original
baseline achieved 2/5 L3 at 11.858 seconds mean. These single-sample results
show a measured accuracy/time tradeoff on this group, not a universal speedup.

Random five: `random-26`, **4/5 external L3**.
The selection was frozen at 2026-10-31T00:48:44.920255Z, using seed
`14448296925056204987`, uniformly without replacement from 144 canonical tasks.
The development serial task was excluded. No failed task was replaced.

| Task | Solver Seconds | External Level | Internally Verified |
| --- | ---: | ---: | --- |
| Prob098_circuit7 | 113.852 | 3 | no |
| Prob050_kmap1 | 28.559 | 3 | yes |
| Prob091_2012_q2b | 128.358 | 1 | yes |
| Prob095_review2015_fsmshift | 79.549 | 3 | yes |
| Prob043_vector5 | 92.945 | 3 | no |

Known limitations retained in this checkpoint:

- The one-hot next-state task failed external simulation with 138 mismatches
  despite internal approval. Candidate and generated reference shared an
  encoded-state case interpretation instead of incoming-edge Boolean equations.
  Independent model calls can still make correlated errors.
- Literal waveform replay can report a mismatch when data changes coincide with
  a clock edge, because its driver changes clock and data in the same active
  simulation region. The waveform task nevertheless passed external L3.
- A checker review can remain unresolved. Such a candidate is returned with
  internal verification marked false rather than a fabricated approval.

Subsequent waveform and one-hot experiments are excluded from this commit.
They are preserved separately as `source-v27` and `source-v28`; their results
must not be attributed to this checkpoint.

Full source hashes, task hashes, traces, judge results, frozen population and
all attempts are retained on the production host under:
`/workspace/operations/benchmarks/closure-20261031/`.
The decisive records are `unit-26`, `development-26`, `random-26`, and
`random-selection.json`. No production deployment was performed for this
checkpoint.
