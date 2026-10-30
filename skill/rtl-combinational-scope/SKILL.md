---
name: rtl-combinational-scope
description: Distinguish module-wide combinational requirements from a single combinational output or logic cone in a sequential design.
---

# Agent Skill: RTL Combinational Scope

## Skill Metadata
- **Name:** `rtl_combinational_scope`
- **Description:** Distinguish module-wide combinational requirements from a single combinational output or logic cone in a sequential design.
- **Trigger:** A prompt calls the whole module, circuit, or design combinational, or says that a particular output or portion is combinational alongside clocked state.
- **Log signatures:** `combinational-only scope contains clocked process`, `purely combinational module`

## Rules

1. Identify the grammatical subject of every combinational statement. "Implement a
   purely combinational module" constrains the whole module. "The output `hit` is
   purely combinational" constrains only `hit` and its logic cone.
2. Keep clocked state when the specification also requires counters, registered
   values, sampled inputs, or state transitions. A combinational output may decode
   that state without changing how the state is stored.
3. Treat clock ports as ordinary inputs unless the prompt assigns them a timing
   role. Do not add a clocked process merely because a clock port exists.
4. When the prose scope is ambiguous, preserve explicitly described sequential
   behavior and record the ambiguity. Do not let one local adjective erase a
   separate clock/reset contract.

## Steps

1. Build a signal table with each output's source: combinational input decode,
   current-state decode, or registered value.
2. Extract clock and reset behavior independently from combinational-scope wording.
3. Compare the planned processes with those assignments before emitting RTL.
4. If the tool flags clocked logic as forbidden, re-check whether the contract
   explicitly constrains the entire module or only a named output.

## Behavioral Constraints

- Do not implement state storage for a module explicitly specified as wholly
  combinational.
- Do not remove required counters, registers, or reset behavior because a
  different output is described as combinational.
- Do not infer timing semantics from port names alone.
