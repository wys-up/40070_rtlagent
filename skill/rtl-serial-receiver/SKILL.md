---
name: rtl-serial-receiver
description: Implement cycle-accurate serial frame receivers with exact start, data, stop, output-pulse and error-recovery behavior.
---

# Serial frame receiver timing

- **Trigger:** serial receiver, UART receiver, start bit, stop bit, received byte or serial frame.
- **Log signatures:** `serial receiver`, `UART`, `start bit`, `stop bit`, `received byte`, `fsm_serial`

- Make a cycle table for start, each data bit, and stop. Count and sample every data bit, including the terminal bit (bit 7 in an eight-bit frame), before testing STOP.
- Frame progress depends on the number of sampling edges, never on whether a data bit is 0 or 1. Exercise all-zero bytes and nonuniform bytes to expose conditional counter updates.
- For LSB-first reception, the first sampled data bit becomes output bit 0. Derive whether the shift expression should shift right or left using a mixed-bit sample, not a repeating or symmetric byte.
- Update the received byte and pulse `done` on the exact specified edge. If `done` is a one-cycle pulse, default it low on other active edges unless the specification says otherwise.
- On an invalid stop bit, follow the specified recovery rule. If the receiver must wait in STOP for a high bit, hold there; do not return to IDLE, emit `done`, or interpret the low stop sample as a new start.
- Keep each register under one procedural owner. A single clocked process with nonblocking assignments is often the clearest form, but the specification controls the required structure and edge.
- Verify back-to-back frames, invalid stop followed by recovery, reset during a partial frame, and a byte that distinguishes bit order.

## Behavioral Constraints

- Do not assume a conventional UART recovery policy when the task states another rule.
- Do not move registered outputs into combinational logic or add a pipeline stage to make the state diagram look simpler.
