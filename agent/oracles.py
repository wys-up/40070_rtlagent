"""Literal checks from tables explicitly supplied in the current specification.

Only K-map cells, state/transition tables and waveform samples are replayed.
No circuit-family algorithm, inferred implementation or stored answer is used.
An incomplete parse returns None so the model must construct a checker.
"""

import re

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
    # Four-variable maps retain the actual column order, including non-Gray maps.
    header = re.search(
        r"(?m)^\s*([A-Za-z]{2})\s*\n\s*([A-Za-z]{2})\s+"
        r"([01]{2}(?:\s+[01]{2}){3})\s*$", prompt)
    rows = re.findall(r"(?m)^\s*([01]{2})\s*\|\s*([01xXdD-])\s*"
                      r"\|\s*([01xXdD-])\s*\|\s*([01xXdD-])\s*"
                      r"\|\s*([01xXdD-])\s*\|?\s*$", prompt)
    if header and len(rows) == 4 and len(outputs) == 1 and outputs[0][0] == 1:
        cols, rowvars = header.group(1), header.group(2)
        if len(set(cols + rowvars)) == 4 and set(cols + rowvars) == {
                n for w, n in inputs if w == 1} and len(inputs) == 4:
            columns = header.group(3).split()
            if len(set(columns)) == 4 and len({row[0] for row in rows}) == 4:
                statements = []
                for row in rows:
                    for col, value in zip(columns, row[1:]):
                        if value not in ("0", "1"):
                            continue
                        values = dict(zip(cols + rowvars, col + row[0]))
                        statements.append("; ".join(f"{n}=1'b{values[n]}" for _, n in inputs)
                                          + "; #1; " + _check(outputs[0][1], int(value), 1,
                                                              f"K-map col={col} row={row[0]}"))
                if statements:
                    return SelfTest("", "\n".join(statements), "All specified K-map cells")
    # A waveform table supplies expected values directly, without circuit inference.
    if all(w == 1 for _, w, _ in ports):
        for line in prompt.splitlines():
            labels = line.split()
            if labels and labels[0].lower() == "time" and set(labels[1:]) == {n for _, _, n in ports}:
                samples = []
                for candidate in prompt.splitlines():
                    match = re.fullmatch(r"\s*(\d+)ns\s+([01xX](?:\s+[01xX])+)\s*", candidate)
                    if match and len(match.group(2).split()) == len(labels) - 1:
                        samples.append((int(match.group(1)), dict(zip(
                            labels[1:], match.group(2).split()))))
                if len(samples) < 3 or any(b[0] <= a[0] for a, b in zip(samples, samples[1:])):
                    continue
                body, now = [], 0
                for timestamp, values in samples:
                    if timestamp < now:
                        return None
                    body.append(f"#{timestamp-now};")
                    for _, name in inputs:
                        body.append(f"{name}=1'b{values[name]};")
                    body.append("#1;")
                    now = timestamp + 1
                    for width, name in outputs:
                        if values[name] in ("0", "1"):
                            body.append(_check(name, int(values[name]), width,
                                               f"waveform {timestamp}ns {name}"))
                return SelfTest("", "\n".join(body),
                                "Every defined output in the supplied waveform")
    return _state_table(prompt, contract) or _moore_arrows(prompt, contract)


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
    edges = re.findall(r"\b(\w+)\s*\(\s*(\w+)\s*=\s*([01])\s*\)\s*"
                       r"--\s*(\w+)\s*=\s*([01])\s*-->\s*(\w+)", prompt)
    reset_state = re.search(r"reset state is\s+(\w+)", prompt, re.I)
    if not edges or not reset_state or not contract.reset_kind or not contract.reset_polarity:
        return None
    clocks = [n for d, w, n in contract.ports if d == "input" and w == 1
              and n.lower() in ("clk", "clock")]
    resets = [n for d, w, n in contract.ports if d == "input" and w == 1
              and re.fullmatch(r"a?reset\w*|rst\w*", n, re.I)]
    outputs = [n for d, _, n in contract.ports if d == "output"]
    names = sorted({edge[0] for edge in edges})
    if (len(clocks) != 1 or len(resets) != 1 or len(outputs) != 1
            or reset_state.group(1) not in names or len(names) > 32):
        return None
    clk, reset, out = clocks[0], resets[0], outputs[0]
    input_names = {edge[3] for edge in edges}
    if len(input_names) != 1 or {edge[1] for edge in edges} != {out}:
        return None
    inp = next(iter(input_names))
    if set(n for d, w, n in contract.ports if d == "input") != {clk, reset, inp}:
        return None
    transition = {(src, value): dst for src, _, _, _, value, dst in edges}
    levels = {src: value for src, _, value, _, _, _ in edges}
    if len(transition) != 2 * len(names) or any(dst not in names for dst in transition.values()):
        return None
    reset_index = names.index(reset_state.group(1))
    high = 1 if contract.reset_polarity == "high" else 0
    active_clock = 0 if re.search(r"negative edge|falling edge", prompt, re.I) else 1
    case = []
    decode = []
    for index, name in enumerate(names):
        zero, one = (names.index(transition[(name, str(x))]) for x in (0, 1))
        case.append(f"{index}: ref_state={inp} ? {one} : {zero};")
        decode.append(f"{index}: expected={levels[name]};")
    body = f"""
{clk}={1-active_clock}; {reset}={high}; ref_state={reset_index};
#2; {clk}={active_clock}; #1;
check({out},1'b{levels[reset_state.group(1)]},1'b1,"reset edge");
{clk}={1-active_clock}; #1; {reset}={1-high}; seed=42;
for(i=0;i<128;i=i+1) begin
  {inp}=$random(seed); #2;
  case(ref_state)
    {chr(10).join(decode)}
  endcase
  check({out},expected,1'b1,$sformatf("Moore pre-edge state=%0d input=%0b",ref_state,{inp}));
  case(ref_state)
    {chr(10).join(case)}
  endcase
  {clk}={active_clock}; #1;
  case(ref_state)
    {chr(10).join(decode)}
  endcase
  check({out},expected,1'b1,$sformatf("Moore post-edge state=%0d input=%0b",ref_state,{inp}));
  {clk}={1-active_clock}; #1;
end
"""
    if contract.reset_kind == "synchronous":
        body += f"""
{reset}={high}; #1;
check({out},expected,1'b1,"synchronous reset off-edge holds");
{clk}={active_clock}; #1;
check({out},1'b{levels[reset_state.group(1)]},1'b1,"synchronous reset on-edge");
"""
    return SelfTest("integer ref_state, expected, i, seed;", body,
                    "Complete Moore transition table; pre/post-edge decode and reset")
