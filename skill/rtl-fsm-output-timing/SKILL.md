---
name: rtl-fsm-output-timing
description: Preserve Moore and Mealy output cycles, state transitions, and single-owner rules in RTL state machines.
---

# FSM output timing

- **Trigger:** Moore/Mealy outputs, FSM state transitions, registered status signals or a cycle mismatch around a state change.
- **Log signatures:** `Moore`, `Mealy`, `state machine`, `finite-state`, `FSM`, `state transition`, `output cycle`

- Write a transition/output table from the specification before choosing an encoding. Record the state before the edge, sampled inputs, next state, and output visible after the edge.
- A Moore output is usually decoded from the registered current state. Use next-state decode only when the required observable cycle says the output changes with that transition; do not add an output register that adds a cycle.
- A Mealy output depends on the specified current state and current inputs. Register it only when the task explicitly requires registered output timing.
- Give each state-holding register and output one procedural owner. In a clocked process, later nonblocking assignments to the same variable can override earlier ones on the same edge.
- Preserve reset coverage and priority for each register independently. Do not clear state or outputs that the specification says must hold or continue sampling.
- For overlapping sequences, transition to the longest suffix that is also a prefix when required; do not force a return to IDLE unless specified.

## Behavioral Constraints

- “After the clock edge” describes an observation time; it does not by itself request another pipeline register.
- Keep combinational next-state/output tasks combinational even when the interface contains a clock.
- Do not change state transitions while repairing a declaration or compile error.
