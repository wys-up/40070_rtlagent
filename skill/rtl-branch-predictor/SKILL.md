---
name: rtl-branch-predictor
description: Implement gshare history indexing, recovery priority, PHT initialization and saturating-counter timing.
---

# Gshare branch predictor semantics

- **Trigger:** gshare, branch predictor, global history, prediction/training, pattern history table or misprediction recovery.
- **Log signatures:** `gshare`, `branch predictor`, `predict_history`, `train_history`, `PHT`, `train_mispredicted`, `saturating counter`

- Derive prediction and training indices independently from the current specification. Identify which values are snapshots and which are live state.
- Build a cycle table for requests, responses and state writes. Establish response latency and whether a simultaneous read sees old or updated state from the specified behavior.
- Record validity conditions and priority for every concurrent history update, training event and recovery event. Do not infer priority from a familiar predictor.
- Derive the counter update direction, encoding, reset value and saturation endpoints from the specification.
- Derive recovered history width, bit order and inclusion of the resolved outcome from the specification; do not reuse a fixed concatenation.
- Check that table initialization covers the specified entries and element width.

## Behavioral Constraints

- Do not infer history width, index XOR order, counter reset state, or recovery priority from a familiar gshare implementation.
- Keep the reset sensitivity (synchronous/asynchronous) and all externally visible latency exactly as specified.
