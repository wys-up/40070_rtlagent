---
name: rtl-serial-receiver
description: Implement cycle-accurate serial frame receivers with exact start, data, stop, output-pulse and error-recovery behavior.
---

# Serial frame receiver timing

- **Trigger:** serial receiver, UART receiver, start bit, stop bit, received byte or serial frame.
- **Log signatures:** `serial receiver`, `UART`, `start bit`, `stop bit`, `received byte`, `fsm_serial`

- Read start value, data count, terminal condition, reset and declared outputs from the CURRENT specification. Store received data only if it affects a declared output; apply the stated bit order when it does.
- The edge recognizing a start has already consumed it. The next edge consumes the first data sample. Count every data edge, including zeros, exactly once. A fixed data phase consumes the stated number of edges, independently of data values; only the following edge checks the terminal condition.
- Distinguish ON-TIME VALID COMPLETION from ERROR RECOVERY. When the task says an invalid terminal sample requires waiting for a delimiter before attempting the next frame, that frame has FAILED. Later finding the delimiter only restores readiness; it does NOT retroactively validate the failed frame or assert success. These histories need different future behavior, even when their current outputs are both zero. Use separate logical phases; never merge them into one terminal-wait phase.
- After valid completion or recovery, consume the very next sample. With no specified inter-frame gap, this sample may already be a new start. Do not spend it returning to an idle state. Verify back-to-back frames.
- A one-cycle completion indication is true only for the specified completion cycle, then clears. It must hold until the next sampling edge; do not gate it with a later live input unless a Mealy output is explicitly requested.
- Independently check a valid frame, invalid terminal sample followed by several invalid samples THEN a valid recovery delimiter, and a new valid frame immediately afterward. Observe the output on the recovery delimiter itself, not just on the earlier invalid sample. Use compressed repeat counts for repeated data samples; do not hand-count long input lists.
- Check reset during a partial frame, all-zero and mixed data, and one procedural owner per register. The specification controls synchronous/asynchronous reset and edge polarity.

## Behavioral Constraints

- Do not assume a conventional UART recovery policy when the task states another rule.
- Do not move registered outputs into combinational logic or add a pipeline stage to make the state diagram look simpler.
