---
name: rtl-wide-reduction
description: Implement wide population counts and reductions with correct accumulator sizing and legal bounded indexing.
---

# Agent Skill: RTL Wide Reduction

## Skill Metadata
- **Name:** `rtl_wide_reduction`
- **Description:** Implement wide population counts and reductions with correct accumulator sizing and legal bounded indexing.
- **Trigger:** A combinational task counts asserted bits or reduces a wide vector, or Vivado reports an out-of-range select or invalid generate-loop index.
- **Log signatures:** `10-3705`, `out of bounds`, `invalid context for genvar`, `not a genvar`, `population count`, `popcount`

## Rules

1. For an N-bit population count, represent every value from zero through N.
   The required result width is `ceil(log2(N+1))`, not `ceil(log2(N))`.
2. Count exactly the declared indices `0` through `N-1`. Never use a padded
   chunk expression that references `vector[N]`, even if a conditional is meant
   to suppress it.
3. Size each intermediate sum for its own maximum before adding it to a wider
   accumulator. A later cast cannot recover a carry already truncated away.
4. Prefer a simple synthesizable combinational loop over a hand-built adder tree
   when the tree introduces width or remainder-boundary risk.

## Steps

1. Read the actual input range and compute N from the inclusive bounds.
2. Choose an accumulator that can represent N; use an integer loop index for a
   procedural loop, or a correctly declared `genvar` only for elaboration-time
   generate constructs.
3. Iterate with the strict bound `i < N`, adding one input bit per iteration.
4. Check zero, all ones, every edge bit, and mixed patterns that place set bits
   in the final partial group.
5. Treat every `VRFC 10-3705` warning as a failed candidate even if compilation
   exits successfully.

## Evidence

In the targeted 191-bit population-count variant, five attempts failed to reach
compilation. Vivado 2026.1 reported out-of-range references to `in[191]` and
partial-sum bit `[3]`, followed by invalid `genvar` usage. The interface and
functional stages were not reached.

## Behavioral Constraints

- Do not use `genvar` in a procedural `always` block or procedural `for` loop.
- Do not assume the vector width is divisible by the reduction chunk size.
- Do not declare a fixed-width partial sum without checking its maximum value.
- Do not claim a functional pass from compilation alone.
