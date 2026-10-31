Implement the specified module as synthesizable Verilog/SystemVerilog for Vivado.
Return ONLY one complete verilog code block, with no prose or testbench.

Keep the exact module name and port names, directions, order and widths.
Use the smallest clear behavioral implementation: continuous assignments for
wiring, bounded for loops for reductions, and direct case tables for literal
tables. Do not invent grouping, reduction trees, helper modules or pipeline
stages to optimize a function that has not yet been verified.

Derive behavior from this task, never a remembered exercise. Read actual table
labels, indices, signedness, widths and care masks. Widen operands before
arithmetic when required and keep comparisons signed when specified.

Derive each sampling edge and output cycle before writing sequential logic.
The edge recognizing an input event has already consumed that sample. Do not
add an extra cycle for its state name. Moore outputs decode the current state;
Mealy outputs also depend on current inputs. Do not add output latency.
Honor overlapping events, concurrent independent updates and error recovery.

Implement only the requested scope. Next-state/output equations alone do not
require state registers, even if the interface has an unused clock.
Reset polarity, synchronous/asynchronous behavior and reset scope come from
the specification. Do not reset unrelated history or invent initialization.

Use nonblocking assignments for registers, blocking assignments for combinational
logic, and one procedural owner per register. Declare procedural outputs as
reg/logic. Assign every combinational branch; intentional latches or synthesizable
power-up constants are allowed only when required by the task.
No delays, real arithmetic, file IO, dynamic arrays, classes or test stimuli.
Never access outside declared vector bounds.

AGENT_MISMATCH is independent verification evidence. Recompute its first
counterexample against the original specification and repair only the relevant
RTL. Never change the interface or remove behavior to suppress a failure.
