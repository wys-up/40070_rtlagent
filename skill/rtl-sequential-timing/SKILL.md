---
name: rtl-sequential-timing
description: Design and review clocked RTL whose behavior depends on sampled inputs, registered outputs, reset timing, state transitions, or overlapping sequences.
---

<!--
Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: MIT
-->

# RTL sequential timing

- **Trigger:** clock edges, sampled inputs, registered outputs, reset, FSMs, counters, pipelines or overlapping occurrences.
- **Log signatures:** `Mismatches:`, `synchronous reset`, `asynchronous reset`, `registered output`, `sampled on the positive edge`, `sampled on the negative edge`, `overlapping`, `four most recent input bits`

## Temporal contract

- Extract the sampling edge, output observation time, reset polarity and type, reset targets, update priority, and overlap behavior from the current specification.
- Build a cycle table containing pre-edge state, sampled input, next state and post-edge outputs. Include a boundary case and a near miss.
- Nonblocking right-hand sides use pre-edge values. For each output, determine whether the specification requires old state, the current sample, or both; derive the expression accordingly.
- Distinguish Moore, Mealy and explicitly registered outputs. An observation after an edge does not itself request an additional register.
- For combinational next-state/output tasks, derive equations from the supplied state input without introducing state storage.

## Reset and overlap

- Use the reset edge, polarity, values and priority explicitly required by the specification.
- Check reset coverage for each register separately. History may need to continue sampling while another register resets.
- Derive any overlap transition from the current target sequence and its suffix/prefix relationships. Do not assume either overlapping or non-overlapping operation.
- Preserve specified initialization, hold behavior and error recovery.

## Verification procedure

1. Mark the specification's cycle boundaries and latency statements.
2. Derive expected transitions independently of the candidate implementation.
3. Exercise reset assertion/release, first valid input, terminal count or match, near misses, and concurrent control events when applicable.
4. Compare the first mismatch with the cycle table before changing the design.
5. Preserve the interface and unaffected behavior during a timing repair.

## Output format

Follow the caller's output contract. For RTL generation, return only the complete synthesizable design in the requested code block.
