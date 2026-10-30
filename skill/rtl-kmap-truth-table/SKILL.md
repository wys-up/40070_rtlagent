---
name: rtl-kmap-truth-table
description: Translate Karnaugh maps and truth tables using their declared variable order, labels, and don't-care cells.
---

# Agent Skill: RTL K-map Truth Table

## Skill Metadata
- **Name:** `rtl_kmap_truth_table`
- **Description:** Translate Karnaugh maps and truth tables using their declared variable order, labels, and don't-care cells.
- **Trigger:** The prompt contains a Karnaugh map, explicit truth table, row/column labels, or the self-test reports a map-cell mismatch.
- **Log signatures:** `K-map`, `Karnaugh`

## Rules

1. Treat the printed row and column labels as the source of truth. Do not assume
   Gray-code ordering unless the table explicitly uses that ordering.
2. Map each cell's row and column bits to named variables before deriving logic.
   Respect the variable order in each header.
3. Preserve `x`, `d`, or dash cells as don't-cares; do not invent expected values
   for them in a checker.
4. Validate the complete specified table, not only the simplified expression's
   visually adjacent groups.
5. For a non-Gray or otherwise unusual ordering, prefer a direct table-driven
   implementation first:

   ```verilog
   always @* begin
     case ({row_variables_in_printed_order,
            column_variables_in_printed_order})
       // one branch per defined cell
       default: out = 1'b0;
     endcase
   end
   ```

   Do not minimize the expression until every branch has been checked against
   the printed row and column labels. A compact expression is optional; correct
   coordinate mapping is mandatory.

## Steps

1. Transcribe every defined cell into a variable assignment and expected output.
2. Independently test the candidate against each defined row.
3. Write a normalized mapping list such as
   `a=0,b=1,c=1,d=0 -> out=...` before simplifying.
4. On a mismatch, use the reported row and column labels to identify the
   assignment; repair the mapping or expression without altering the checker.
5. Simplify only after the full mapping is verified.

## Behavioral Constraints

- Do not reorder columns to a preferred Gray sequence.
- Do not treat don't-care entries as required zeroes or ones.
- Do not rewrite expected test values to agree with the candidate RTL.
