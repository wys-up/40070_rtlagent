---
name: rtl-sequential-timing
description: Design and review clocked RTL whose behavior depends on sampled inputs, registered outputs, reset timing, state transitions, or overlapping sequences.
---

<!--
Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: MIT
-->

# Agent Skill: RTL Sequential Timing

## Skill Metadata
- **Name:** `rtl_sequential_timing`
- **Description:** Design and review clocked RTL whose behavior depends on sampled inputs, registered outputs, reset timing, state transitions, or overlapping sequences.
- **Trigger:** Use this skill whenever the specification mentions a clock edge, sampled input, registered output, synchronous or asynchronous reset, a sequence detector, an FSM, a counter, a pipeline cycle, or overlapping occurrences.
- **Log signatures:** `Mismatches:`, `synchronous reset`, `asynchronous reset`, `registered output`, `sampled on the positive edge`, `sampled on the negative edge`, `overlapping`, `four most recent input bits`

## System Prompt

You are reviewing a clocked RTL design against a cycle-accurate specification. Treat
the phrase "after that clock edge" as an observable post-edge value, and treat the
input sampled at that edge as part of the current transition. Do not rely on
simulation intuition that hides nonblocking-assignment semantics.

## Rules

### Rule 1 — Write the temporal contract before choosing the structure

Extract these facts from the prompt:

1. Which edge samples each input.
2. Whether each output is combinational or registered.
3. Whether an output reflects the current input on the same edge or one edge later.
4. Reset polarity, synchronous/asynchronous behavior, reset values, and priority.
5. Whether occurrences overlap and which suffix must be retained after a match.

For a registered output, make a small cycle table for at least one match and one
near miss. The value shown after edge `n` must be the value the testbench observes
during cycle `n`, not the value intended for edge `n+1`.

### Rule 2 — Remember what nonblocking assignments read

In a clocked block using `<=`, every right-hand side reads the pre-edge state.
This is correct and intentional. Therefore this pattern checks the old window:

```verilog
state <= next_state;
hit   <= (state == TARGET);
```

Before emitting code, annotate each registered output assignment as depending on
`old_state`, `current_input`, or both. If the specification says the input sampled
on this edge completes the event, that output must depend on both. A comparison
against only the old state is one cycle late.

For a shift-register pattern detector, if the prompt says the current sampled bit
is included in the window and the output fires on the detecting edge, the
comparison must use the prospective window. For example, with `sr` storing the
previous four bits and `in` sampled now:

```verilog
// Required for same-edge detection:
detected <= ({sr[2:0], in} == 4'b1101);

// Incorrect: compares the previous window and pulses one cycle late:
detected <= (sr == 4'b1101);
```

Compute and compare the prospective state explicitly:

```verilog
state <= {state[W-2:0], in};
hit   <= ({state[W-2:0], in} == TARGET);
```

Alternatively use named next-state signals, but ensure the next-state logic is
defined from both the old state and current inputs before the sequential block.
Never add a blocking assignment inside a clocked block merely to make a value
appear current.

### Rule 3 — Match output style to the specification

- A Moore output is normally a combinational decode of the CURRENT state
  register. Registering that decode from the old state adds an unwanted cycle.
- A Mealy output depends on current state AND current inputs unless explicit
  output registration is requested.
- "After the clock edge" describes an observation time; it alone does not
  require another output register. Use an extra register only when the stated
  output contract calls for one.
- For an explicitly registered event pulse, derive the detecting transition:
  `hit_next = (state == PREFIX_BEFORE_FINAL) && in`.
- If the task asks only for next-state/output combinational equations, do not
  implement state storage, even when the interface includes a clock.

### Rule 4 — Implement reset exactly as stated

Use a synchronous reset only in the clocked body:

```verilog
always @(posedge clk) begin
    if (reset) begin
        state <= RESET_STATE;
        out   <= RESET_OUT;
    end else begin
        ...
    end
end
```

Use an asynchronous reset only when the prompt explicitly requires it:

```verilog
always @(posedge clk or posedge reset) begin
    if (reset) ...
    else       ...
end
```

Apply reset priority to the signals actually covered by the reset contract.
Do not automatically reset every history register: an edge-capture circuit may
need to sample its previous input during reset while reset only clears the
sticky captured output. Preserve explicit power-up values when specified.

### Rule 5 — Preserve the correct suffix for overlaps

For a shift-register detector, compare the post-edge window, including the current
input. For an FSM, transition to the longest suffix that is also a prefix of the
pattern, rather than always returning to idle. For example, after recognizing a
pattern whose suffix can begin another occurrence, retain that suffix.

Always reason through an overlapping vector such as `1101101` when the target is
`1101`; it must produce two pulses.

### Rule 6 — Prefer explicit next-state reasoning over cleverness

For short fixed-width patterns, either of these forms is acceptable:

```verilog
// Shift-register form: current input participates in the comparison.
always @(posedge clk) begin
    if (reset) begin
        sr        <= '0;
        detected  <= 1'b0;
    end else begin
        sr        <= {sr[W-2:0], in};
        detected  <= ({sr[W-2:0], in} == TARGET);
    end
end
```

```verilog
// FSM form: output is asserted on the transition consuming the final bit.
always @(posedge clk) begin
    if (reset) begin
        state    <= IDLE;
        detected <= 1'b0;
    end else begin
        detected <= (state == PREFIX_BEFORE_FINAL) && in;
        state    <= state_next;
    end
end
```

Adapt widths, reset values, and pattern polarity to the prompt. Do not copy these
templates when the output is explicitly combinational or the reset is different.

## Steps

1. Mark every sentence that specifies a cycle boundary, edge, reset, or latency.
2. Write the first expected output cycle for a short stimulus by hand.
3. Define either `next_state` or a post-edge observation expression.
4. For each registered output, label its RHS dependencies. Confirm that an event
   completed by the current sample does not depend only on old state.
5. For shift-register detectors with same-edge output semantics, inspect the
   exact compare expression and confirm it includes the current input.
6. Test a positive match, a near miss, an overlapping match, reset during a partial
   match, and a match immediately after reset.
7. Check reset targets individually; preserve specified hold behavior, continuing
   history sampling, and explicit initial values. Do not invent resets.
8. Only after the timing table is consistent, emit the complete synthesizable module.

## Evidence

On Vivado 2026.1 for part `xczu3eg-sbva484-1-e`, the sequence detector task
`ex02_detect_1101` exposed this exact failure. The implementation compared the old
shift register after executing a nonblocking update:

```verilog
shift_reg <= {shift_reg[2:0], in};
if (shift_reg == 4'b1101)
    detected_reg <= 1'b1;
```

It compiled and synthesized but produced `4` mismatches in `226` samples and was
graded L1. Comparing `{shift_reg[2:0], in}` instead produced `0` mismatches in
`226` samples and reached L3, including the overlapping sequence, synchronous
reset, near misses, and random tail.

The boundary task `boundary_detect1011` reproduced the same defect with a
different pattern: `1011` had to be detected on the edge that samples its final
bit, and `1011011` had to produce two overlapping pulses. Comparing the old
four-bit register instead of `{history[2:0], in}` produced five mismatches. The
reliable repair is to derive both `history_next` and the registered pulse from
the same prospective window, then retain enough suffix history for overlaps.

## Output Format

Return one complete Verilog/SystemVerilog module in the format requested by the
task. Do not return a testbench, timing delays, or an explanation outside the
required code block.

## Behavioral Constraints

- Do not infer asynchronous reset from a generic word such as "reset"; use the
  exact reset wording in the prompt.
- Do not use blocking assignments to simulate current-state updates inside a
  clocked process.
- Do not compare only the old state when the specification includes the input
  sampled on the current edge.
- For same-edge shift-register detection, reject `detected <= (sr == PATTERN)` if
  `sr` contains only samples from before the current edge; compare the prospective
  window that appends the current input.
- Do not add a one-cycle pipeline unless the prompt explicitly requests it.
- Do not discard overlap information by unconditionally returning an FSM to idle.
- Do not change the module name, ports, widths, or output registration style while
  fixing a timing issue.
