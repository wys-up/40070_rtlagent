---
name: rtl-arithmetic-widths
description: Size RTL arithmetic, counters, signed operations and reductions without changing specified interface widths or overflow behavior.
---

# RTL arithmetic widths and signedness

- **Trigger:** population count, arithmetic width, signed comparison, overflow, truncation, arithmetic shift or carry behavior.
- **Log signatures:** `popcount`, `population count`, `bit count`, `odd-width`, `odd width`, `255-bit`, `truncation`, `overflow`, `signed`, `Synth 8-3295`, `VRFC 10-3705`

- Treat a prose width as a bit count: an N-bit vector is `[N-1:0]`; loops visit indices `0` through `N-1`. For odd widths such as 255, never access bit 255.
- A population count over N bits must represent 0 through N, requiring `$clog2(N+1)` bits. Give intermediate accumulators enough width before adding; a wider final result cannot restore a carry already discarded.
- Preserve the specified output width and overflow/truncation behavior. Do not widen ports or replace wraparound with saturation to remove a warning.
- `$signed` changes interpretation, not width. Sign-extend operands before arithmetic when required. For arithmetic right shift, ensure the left operand is signed, for example `$signed(value) >>> amount`.
- Fully define intermediate vectors. Check concatenation widths by adding each slice and padding width; do not depend on implicit extension to repair a malformed expression.
- Check zero, all ones, maximum representable count, carry/borrow, most-negative signed input, and shift distances at and beyond the operand width when the specification defines them.

## Behavioral Constraints

- Do not infer saturation, signedness, or overflow behavior from coding conventions; use the task specification.
- Do not change the public interface to simplify internal arithmetic.
