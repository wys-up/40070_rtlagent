---
name: rtl-shift-register-pattern
description: Preserve sequence detector overlap and output timing, and derive LFSR feedback from the specified topology and bit numbering.
---

# Sequence detection and LFSR semantics

- **Trigger:** sequence detector, overlapping pattern, shift register, LFSR, feedback tap, shift direction or seed.
- **Log signatures:** `sequence detector`, `overlapping`, `shift register`, `LFSR`, `lfsr`, `feedback`, `tap`, `taps`

- For a same-edge sequence match, compare the prospective window that includes the input sampled on the current edge. Comparing only old history adds a cycle. Preserve a suffix/prefix overlap only when required by the task.
- Do not use a detector's `{history, input}` template for an LFSR, CRC or scrambler. First classify the specified topology as Fibonacci or Galois, shift direction, numbered taps, XOR/XNOR polarity and seed; then write every next-state bit equation.
- For a right-shifting Galois register, begin with `feedback = old_state[0]` and `next_state = old_state >> 1`; each one-based tap position `p` XORs feedback into bit `p-1` if the specified polarity is XOR. This distributed feedback is not a Fibonacci XOR reduction.
- As a consistency check, a five-bit right-shifting Galois XOR register with tap positions 5 and 3 has `next_q[4]=q[0]`, `next_q[3]=q[4]`, `next_q[2]=q[3]^q[0]`, `next_q[1]=q[2]`, and `next_q[0]=q[1]`.
- Respect the specified reset/load/enable priority. A zero seed may be intentional; do not add automatic recovery from the all-zero state unless required.

## Behavioral Constraints

- Do not infer tap numbering from a familiar polynomial or silently convert between Fibonacci and Galois forms.
- Do not add a valid-history counter, reset, or overlap behavior unless the task requires it.
