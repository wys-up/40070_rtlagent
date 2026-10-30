---
name: rtl-branch-predictor
description: Implement gshare history indexing, recovery priority, PHT initialization and saturating-counter timing.
---

# Gshare branch predictor semantics

- **Trigger:** gshare, branch predictor, global history, prediction/training, pattern history table or misprediction recovery.
- **Log signatures:** `gshare`, `branch predictor`, `predict_history`, `train_history`, `PHT`, `train_mispredicted`, `saturating counter`

- Derive prediction and training indices separately from the specified PC and history values. `predict_history` or `train_history` may represent pre-edge snapshots; do not silently replace them with the live history register.
- Apply misprediction recovery only under the specified valid/recovery condition and give it the stated priority over prediction updates. A correctly predicted training event must not restore stale history unless the specification says so.
- Derive the recovered history bit order explicitly. For a 7-bit history, `{train_history[5:0], train_taken}` is one possible specified next value, not a universal convention.
- Use the specified counter encoding, reset value, increment/decrement direction, and saturation limits. Check prediction behavior on the same edge as a PHT write using nonblocking old-state semantics.
- Reset every entry in a packed or unpacked multi-dimensional PHT. A narrow scalar assignment such as `pht <= 128'b0` does not cover a table wider than 128 bits; use a matching aggregate or a loop that visits every entry.

## Behavioral Constraints

- Do not infer history width, index XOR order, counter reset state, or recovery priority from a familiar gshare implementation.
- Keep the reset sensitivity (synchronous/asynchronous) and all externally visible latency exactly as specified.
