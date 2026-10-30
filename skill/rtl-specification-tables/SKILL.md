---
name: rtl-specification-tables
description: Interpret truth tables and Karnaugh maps using their literal labels, variable order and care masks.
---

# Literal specification tables

- **Trigger:** a truth table, Karnaugh map, labeled row/column grid or explicit care mask.
- **Log signatures:** `Karnaugh map`, `Karnaugh`, `K-map`, `Kmap`, `truth table`, `column labels`, `row labels`, `care mask`

- Read the input-variable names attached to each axis and the labels printed beside every row and column. Map each cell to those exact input bits.
- Never assume binary or Gray ordering from a familiar drawing. A reordered column changes its position, not the binary value of its printed label.
- Distinguish variable order from port declaration order. Write down the complete assignment for a representative cell before deriving an expression.
- Preserve all specified zero and one cells. Treat x or a dash as a don't-care only when the specification identifies it as such; never infer missing values.
- Check the resulting expression against every specified cell in the current table before simplifying it. Prefer a direct expression or complete case table when a simplification is uncertain.
- For state-transition tables, distinguish present-state inputs, sampled controls, next-state outputs and current-state outputs. Do not introduce registers into a combinational table task.

## Behavioral Constraints

- Derive values only from the current specification, without remembering a named exercise or supplying a fixed circuit.
- Keep the public interface and the caller's RTL output format.
