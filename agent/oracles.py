"""Literal checks from tables explicitly supplied in the current specification.

Only K-map cells, state/transition tables and waveform samples are replayed.
No circuit-family algorithm, inferred implementation or stored answer is used.
An incomplete parse returns None so the model must construct a checker.
"""

import json
import re

from .fsm import clock_edge, compile_graph, literal_moore_graph, literal_mealy_graph
from .spec_contract import Contract
from .verification import SelfTest


def _check(name: str, value: int, width: int, label: str) -> str:
    return f'check({name}, {width}\'d{value}, {width}\'h{(1 << width) - 1:x}, "{label}");'


def from_spec(prompt: str, contract: Contract) -> SelfTest | None:
    ports = contract.ports
    inputs = [(w, n) for d, w, n in ports if d == "input"]
    outputs = [(w, n) for d, w, n in ports if d == "output"]
    if not ports or any(w <= 0 for _, w, _ in ports):
        return None
    # Literal axes retain their printed order; no Gray-order inference.
    header = re.search(
        r"(?m)^\s*([A-Za-z]{1,4})\s*\n\s*([A-Za-z]{1,4})\s+"
        r"([01]+(?:\s+[01]+)+)\s*$", prompt)
    raw_rows = re.findall(r"(?m)^\s*([01]+)\s*\|\s*([01xXdD| \t-]+)\s*$", prompt)
    rows = [(label, *[v.strip() for v in cells.strip().strip("|").split("|")])
            for label, cells in raw_rows]
    if header and len(outputs) == 1 and outputs[0][0] == 1:
        cols, rowvars = header.group(1), header.group(2)
        if (len(set(cols + rowvars)) == len(cols + rowvars)
                and set(cols + rowvars) == {n for w, n in inputs if w == 1}
                and len(inputs) == len(cols + rowvars)):
            columns = header.group(3).split()
            if (len(columns) == len(set(columns)) == 1 << len(cols)
                    and all(len(c) == len(cols) for c in columns)
                    and len(rows) == len({row[0] for row in rows}) == 1 << len(rowvars)
                    and all(len(row[0]) == len(rowvars)
                            and len(row[1:]) == len(columns)
                            and all(v in "01xXdD-" and len(v) == 1 for v in row[1:])
                            for row in rows)):
                statements = []
                facts = []
                for row in rows:
                    for col, value in zip(columns, row[1:]):
                        if value not in ("0", "1"):
                            continue
                        values = dict(zip(cols + rowvars, col + row[0]))
                        facts.append(dict(inputs={n: int(v) for n, v in values.items()},
                                          outputs={outputs[0][1]: int(value)}))
                        statements.append("; ".join(f"{n}=1'b{values[n]}" for _, n in inputs)
                                          + "; #1; " + _check(outputs[0][1], int(value), 1,
                                                              f"K-map col={col} row={row[0]}"))
                if statements:
                    return SelfTest("", "\n".join(statements), "All specified K-map cells",
                                    facts=facts)
    return (_waveform(prompt, contract) or _onehot_arrows(prompt, contract)
            or _state_table(prompt, contract) or _moore_arrows(prompt, contract)
            or _mealy_arrows(prompt, contract))


def literal_implementation(test: SelfTest | None, contract: Contract,
                           prompt: str = "") -> str | None:
    """Compile complete literal observations, without minimizing or guessing."""
    if test and test.derivation:
        return compile_graph(test.derivation, prompt + "\nMoore", contract, contract.top)
    if test is None or not test.facts:
        return None
    inputs = [(w, n) for d, w, n in contract.ports if d == "input"]
    outputs = [(w, n) for d, w, n in contract.ports if d == "output"]
    if (not inputs or not outputs or any(w <= 0 for w, _ in inputs + outputs)
            or sum(w for w, _ in inputs) > 10):
        return None
    observations = {}
    for fact in test.facts:
        if (set(fact["inputs"]) != {n for _, n in inputs}
                or set(fact["outputs"]) != {n for _, n in outputs}):
            return None
        if any(type(fact[k][n]) is not int or not 0 <= fact[k][n] < 1 << w
               for k, ports in (("inputs", inputs), ("outputs", outputs))
               for w, n in ports):
            return None
        key = "".join(format(fact["inputs"][n], f"0{w}b") for w, n in inputs)
        value = "".join(format(fact["outputs"][n], f"0{w}b") for w, n in outputs)
        if key in observations and observations[key] != value:
            return None
        observations[key] = value
    declarations = [f"  {d}{' reg' if d == 'output' else ''} "
                    + (f"[{w-1}:0] " if w > 1 else "") + n
                    for d, w, n in contract.ports]
    input_width = sum(w for w, _ in inputs)
    output_width = sum(w for w, _ in outputs)
    lhs = "{" + ",".join(n for _, n in outputs) + "}"
    selector = "{" + ",".join(n for _, n in inputs) + "}"
    rows = [f"    {input_width}'b{k}: {lhs}={output_width}'b{v};"
            for k, v in sorted(observations.items())]
    return (f"module {contract.top}(\n" + ",\n".join(declarations)
            + ");\nalways @* begin\n  case(" + selector + ")\n"
            + "\n".join(rows) + f"\n    default: {lhs}='0;\n"
            "  endcase\nend\nendmodule\n")


def _mealy_arrows(prompt, contract):
    graph = literal_mealy_graph(prompt, contract)
    if graph is None:
        return None
    from .reference import build_reference
    test = build_reference(compile_graph(json.dumps(graph), prompt, contract,
                                        "SpecReference"), contract, prompt)
    test.derivation = json.dumps(graph)
    return test


def _waveform(prompt: str, contract: Contract) -> SelfTest | None:
    ports = contract.ports
    inputs = [(w, n) for d, w, n in ports if d == "input"]
    outputs = [(w, n) for d, w, n in ports if d == "output"]
    if any(d == "inout" for d, _, _ in ports):
        return None
    for line in prompt.splitlines():
        labels = line.split()
        if (not labels or labels[0].lower() != "time"
                or len(labels) != len(ports)+1
                or set(labels[1:]) != {n for _, _, n in ports}):
            continue
        samples = []
        for candidate in prompt.splitlines():
            match = re.fullmatch(
                r"\s*(\d+)ns\s+([0-9a-fA-FxX]+(?:\s+[0-9a-fA-FxX]+)+)\s*",
                candidate)
            if match and len(match.group(2).split()) == len(ports):
                samples.append((int(match.group(1)), dict(zip(
                    labels[1:], match.group(2).split()))))
        if len(samples) < 3 or any(b[0] <= a[0] for a, b in zip(samples, samples[1:])):
            return None
        # Scalars have an unambiguous radix. A vector column needs hexadecimal
        # evidence; do not guess whether an unlabelled numeric vector is binary.
        explicit_hex = bool(re.search(r"\bhex(?:adecimal)?\b", prompt, re.I))
        hex_columns = {n for _, _, n in ports if any(
            re.search(r"[a-fA-F]", row[n]) for _, row in samples)}
        for _, width, name in ports:
            if width > 1 and name not in hex_columns and any(
                    value not in ("0", "1", "x", "X")
                    for _, row in samples for value in (row[name],)):
                # Hexadecimal waveform convention applies to the table, not
                # just the one column containing a hexadecimal letter.
                if not hex_columns and not explicit_hex:
                    return None
        clocks = {n for w, n in inputs if w == 1 and re.fullmatch(r"clk|clock", n, re.I)}
        combinational = not clocks and bool(re.search(r"\bcombinational\b", prompt, re.I))
        body, facts, observations, now, checks = [], [], {}, 0, 0
        for timestamp, row in samples:
            if timestamp < now:
                return None
            values = {}
            for _, width, name in ports:
                raw = row[name]
                value = None if "x" in raw.lower() else int(raw, 16)
                if value is not None and value >= 1 << width:
                    return None
                values[name] = value
            body.append(f"#{timestamp-now};")
            for width, name in inputs:
                value = "'x" if values[name] is None else f"{width}'h{values[name]:x}"
                assignment = "<=" if clocks and name not in clocks else "="
                body.append(f"{name}{assignment}{value};")
            body.append("#1;")
            now = timestamp+1
            for width, name in outputs:
                if values[name] is not None:
                    checks += 1
                    body.append(_check(name, values[name], width,
                                       f"waveform {timestamp}ns {name}"))
            if all(value is not None for value in values.values()):
                key = tuple(values[n] for _, n in inputs)
                output = {n: values[n] for _, n in outputs}
                if combinational and key in observations and observations[key] != output:
                    return None
                observations[key] = output
                facts.append(dict(inputs={n: values[n] for _, n in inputs}, outputs=output))
        if not checks:
            return None
        total_bits = sum(w for w, _ in inputs)
        complete_comb = (combinational and total_bits <= 10
                         and len(observations) == 1 << total_bits)
        return SelfTest("", "\n".join(body),
                        "Every defined output in the supplied waveform",
                        facts=facts if complete_comb else [])
    return None


def _onehot_arrows(prompt: str, contract: Contract) -> SelfTest | None:
    """Expand incoming transition terms from an explicit one-hot assignment."""
    if not re.search(r"\bone[- ]hot\b", prompt, re.I):
        return None
    assignment = re.search(
        r"\bstate assignment\s+(\w+)\s*\[\s*(\d+)\s*:\s*0\s*\]\s*=\s*"
        r"((?:[01]+\s*\(\w+\)\s*[,]?\s*)+)", prompt, re.I)
    if not assignment:
        return None
    state, high, codes = assignment.groups()
    width = int(high)+1
    inputs = {n: w for d, w, n in contract.ports if d == "input"}
    outputs = {n: w for d, w, n in contract.ports if d == "output"}
    controls = [n for n in inputs if n != state]
    if (not 1 <= width <= 8 or inputs.get(state) != width
            or len(controls) != 1 or inputs[controls[0]] != 1
            or not outputs or any(w != 1 for w in outputs.values())
            or any(d == "inout" for d, _, _ in contract.ports)):
        return None
    entries = re.findall(r"([01]+)\s*\((\w+)\)", codes)
    encodings = {name: int(bits, 2) for bits, name in entries}
    if (len(encodings) != width or len(entries) != width
            or any(len(bits) != width for bits, _ in entries)
            or set(encodings.values()) != {1 << bit for bit in range(width)}):
        return None
    arrow_lines = [line.strip() for line in prompt.splitlines() if "--" in line]
    edges = [re.fullmatch(r"(\w+)\s*\(\s*[01]\s*\)\s*--\s*([01])\s*-->\s*(\w+)",
                          line) for line in arrow_lines]
    if not edges or not all(edges):
        return None
    transitions = {(m[1], int(m[2])): m[3] for m in edges}
    if (len(edges) != width*2 or len(transitions) != width*2
            or set(transitions) != {(name, value) for name in encodings for value in (0, 1)}
            or any(dst not in encodings for dst in transitions.values())):
        return None
    requested = re.findall(
        r"\boutput(?:\s+signal)?\s+(\w+)\s+should be the input of "
        r"state flip[- ]flop\s+" + re.escape(state) + r"\s*\[\s*(\d+)\s*\]",
        prompt, re.I)
    bits = {name: int(bit) for name, bit in requested}
    if (len(requested) != len(outputs) or set(bits) != set(outputs)
            or any(bit >= width for bit in bits.values())):
        return None
    control = controls[0]
    facts, body = [], []
    for value in range(1 << width):
        for condition in (0, 1):
            next_mask = 0
            for name, code in encodings.items():
                if value & code:
                    next_mask |= encodings[transitions[name, condition]]
            observed = {name: (next_mask >> bit) & 1 for name, bit in bits.items()}
            facts.append(dict(inputs={state: value, control: condition}, outputs=observed))
            body.append(f"{state}={width}'d{value}; {control}=1'b{condition}; #1;")
            body += [_check(name, expected, 1, "incoming one-hot transition terms")
                     for name, expected in observed.items()]
    return SelfTest("", "\n".join(body),
                    "Exhaustive incoming-edge equations from literal one-hot transitions",
                    facts=facts)


def _state_table(prompt: str, contract: Contract) -> SelfTest | None:
    if not contract.combinational_only:
        return None
    present = re.search(r"Present state input\s+(\w+)\[", prompt, re.I)
    next_bit = re.search(r"output\s+(\w+)\s+is\s+\w+\[(\d+)\]", prompt, re.I)
    stimulus_input = re.search(r"when\s+(\w+)=0", prompt, re.I)
    output = re.search(r"\|\s*Output\s+(\w+)", prompt)
    rows = re.findall(r"(?m)^\s*([01]+)\s*\|\s*([01]+)\s*,\s*([01]+)"
                      r"\s*\|\s*([01]+)\s*$", prompt)
    if not all((present, next_bit, stimulus_input, output, rows)):
        return None
    port_map = {n: (d, w) for d, w, n in contract.ports}
    state, inp = present.group(1), stimulus_input.group(1)
    nxt, bit, out = next_bit.group(1), int(next_bit.group(2)), output.group(1)
    if (port_map.get(state, (None,))[0] != "input" or
            port_map.get(inp) != ("input", 1) or
            port_map.get(nxt) != ("output", 1) or port_map.get(out) != ("output", 1)):
        return None
    width = port_map[state][1]
    if any(len(row[0]) != width or len(row[1]) != width or
           len(row[2]) != width or len(row[3]) != 1 for row in rows) or bit >= width:
        return None
    body = []
    for current, next0, next1, value in rows:
        for x, following in enumerate((next0, next1)):
            body.append(f"{state}={width}'b{current}; {inp}={x}; #1;")
            body.append(_check(nxt, (int(following, 2) >> bit) & 1, 1,
                               f"state={current} input={x} next-bit"))
            body.append(_check(out, int(value, 2), 1, f"state={current} output"))
    return SelfTest("", "\n".join(body), "Every specified present-state/input row before a clock edge")


def _moore_arrows(prompt: str, contract: Contract) -> SelfTest | None:
    graph = literal_moore_graph(prompt, contract)
    if graph is None:
        return None
    clocks = [n for d, w, n in contract.ports if d == "input" and w == 1
              and n.lower() in ("clk", "clock")]
    resets = [n for d, w, n in contract.ports if d == "input" and w == 1
              and re.fullmatch(r"a?reset\w*|rst\w*", n, re.I)]
    out = next(n for d, _, n in contract.ports if d == "output")
    clk, reset = clocks[0], resets[0]
    inputs = [n for d, _, n in contract.ports if d == "input" and n not in (clk, reset)]
    states = graph["states"]
    names = [row["name"] for row in states]
    reset_index = names.index(graph["reset_state"])
    reset_level = states[reset_index]["outputs"][out]
    high = 1 if contract.reset_polarity == "high" else 0
    active_clock = int(clock_edge(prompt, clk) == "posedge")
    case = []
    decode = []
    selector = "{" + ",".join(inputs) + "}"
    for index, state in enumerate(states):
        transitions = "\n".join(
            f"{len(inputs)}'b{pattern}: ref_state={names.index(target)};"
            for pattern, target in sorted(state["next"].items()))
        case.append(f"{index}: case({selector})\n{transitions}\nendcase")
        decode.append(f"{index}: expected={state['outputs'][out]};")
    drive = "\n".join(f"{name}=$random(seed);" for name in inputs)
    body = f"""
{clk}={1-active_clock}; {reset}={high}; ref_state={reset_index};
#2; {clk}={active_clock}; #1;
check({out},1'b{reset_level},1'b1,"reset edge");
{clk}={1-active_clock}; #1; {reset}={1-high}; seed=42;
for(i=0;i<128;i=i+1) begin
  {drive} #2;
  case(ref_state)
    {chr(10).join(decode)}
  endcase
  check({out},expected,1'b1,$sformatf("Moore pre-edge state=%0d",ref_state));
  case(ref_state)
    {chr(10).join(case)}
  endcase
  {clk}={active_clock}; #1;
  case(ref_state)
    {chr(10).join(decode)}
  endcase
  check({out},expected,1'b1,$sformatf("Moore post-edge state=%0d",ref_state));
  {clk}={1-active_clock}; #1;
end
"""
    # Reach each source state from reset, then exercise every input pattern.
    # Explicit off-edge checks must not rely on a random final state differing
    # from reset; otherwise asynchronous reset can masquerade as synchronous.
    paths = {reset_index: []}
    pending = [reset_index]
    for source in pending:
        for pattern, target_name in states[source]["next"].items():
            target = names.index(target_name)
            if target not in paths:
                paths[target] = paths[source] + [pattern]
                pending.append(target)
    directed = []
    for source, path in paths.items():
        for pattern in sorted(states[source]["next"]):
            directed += [
                f"{clk}={1-active_clock}; {reset}={high}; #1;",
                f"{clk}={active_clock}; #1; {clk}={1-active_clock}; #1;",
                f"{reset}={1-high}; #1;"]
            current = reset_index
            for vector in path + [pattern]:
                directed += [f"{name}=1'b{bit};" for name, bit in zip(inputs, vector)]
                directed += ["#1;", _check(out, states[current]["outputs"][out], 1,
                                          "directed Moore pre-edge")]
                current = names.index(states[current]["next"][vector])
                directed += [f"{clk}={active_clock}; #1;",
                             _check(out, states[current]["outputs"][out], 1,
                                    "directed Moore post-edge"),
                             f"{clk}={1-active_clock}; #1;"]
            off_edge = (states[current]["outputs"][out]
                        if contract.reset_kind == "synchronous" else reset_level)
            directed += [f"{reset}={high}; #1;",
                         _check(out, off_edge, 1, "directed reset off-edge"),
                         f"{clk}={active_clock}; #1;",
                         _check(out, reset_level, 1, "directed reset on-edge"),
                         f"{clk}={1-active_clock}; #1;",
                         f"expected={reset_level};"]
    body += "\n".join(directed) + "\n"
    if contract.reset_kind == "synchronous":
        body += f"""
{reset}={high}; #1;
check({out},expected,1'b1,"synchronous reset off-edge holds");
{clk}={active_clock}; #1;
check({out},1'b{reset_level},1'b1,"synchronous reset on-edge");
"""
    return SelfTest("integer ref_state, expected, i, seed;", body,
                    "Complete Moore transition table; pre/post-edge decode and reset",
                    derivation=json.dumps(graph))
