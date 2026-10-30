---
name: rtl-signed-arithmetic
description: Design and review RTL with signed two's-complement arithmetic, explicit widening, overflow detection, saturation, and negative constants.
---

<!--
Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: MIT
-->

# Agent Skill: RTL Signed Arithmetic

## Skill Metadata
- **Name:** `rtl_signed_arithmetic`
- **Description:** Design and review RTL with signed two's-complement arithmetic, explicit widening, overflow detection, saturation, and negative constants.
- **Trigger:** Use this skill whenever the specification mentions signed values, two's-complement, arithmetic overflow, saturation, clamping, or a signed accumulator.
- **Log signatures:** `signed`, `two's-complement`, `saturating`, `saturation`, `clamp`, `overflow`

## Rules

1. Mark signed signals explicitly. Do not rely on a prompt saying "interpret as
   signed" while declaring the RTL ports or temporaries unsigned.
2. Widen operands before adding. Use a signed temporary wide enough for the full
   mathematical sum and sign-extend both operands before the addition. Assigning
   an already-truncated expression to a wider temporary cannot recover lost
   carry or sign bits.
3. Compare the widened signed sum against signed limits before narrowing it.
   Place unary minus before a sized literal, for example `-10'sd100` and
   `-8'sd100`. The spelling `8'sd-100` is invalid Verilog/SystemVerilog.
   Do not mix unsigned literals into signed comparisons.
4. Preserve a negative saturated result as the correct two's-complement bit
   pattern. For an 8-bit result, `-100` is `8'h9c`, not an unsigned expression
   that may be evaluated at an unintended width and truncated.
5. Test below-limit clamp, exact negative limit, exact positive limit,
   above-limit clamp, reset priority, and disabled hold.

## Common Failure

This is wrong even though it looks like an 8-bit encoding of `-100`:

```verilog
value <= 8'd100 + 8'd200;
```

The expression is unsigned and may be truncated to `8'h2c` (`44`). Prefer a
signed literal or a correctly typed signed temporary:

```verilog
value <= -8'sd100;
```

## Output Format

Return one complete synthesizable module with the exact requested port names,
directions, and widths. Do not add a second module or explanatory prose.
