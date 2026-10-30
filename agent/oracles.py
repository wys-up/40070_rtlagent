"""Small independent oracles for unambiguous specification formats.

Matchers use task content and port shapes, never task IDs, candidate RTL or
reference files. An incomplete parse returns None instead of guessing.
"""

import re

from .spec_contract import Contract
from .verification import SelfTest


def _check(name: str, value: int, width: int, label: str) -> str:
    return f'check({name}, {width}\'d{value}, {width}\'h{(1 << width) - 1:x}, "{label}");'


def from_spec(prompt: str, contract: Contract) -> SelfTest | None:
    galois = _galois_lfsr(prompt, contract)
    if galois:
        return galois
    gshare = _gshare(prompt, contract)
    if gshare:
        return gshare
    ports = contract.ports
    inputs = [(w, n) for d, w, n in ports if d == "input"]
    outputs = [(w, n) for d, w, n in ports if d == "output"]
    if not ports or any(w <= 0 for _, w, _ in ports):
        return None
    if len(inputs) == len(outputs) == 1:
        iw, inp = inputs[0]
        ow, out = outputs[0]
        if "population count" in prompt.lower() and iw <= 1024 and ow <= 32:
            return SelfTest("integer i, j, expected, seed;\n", f"""
seed=42;
for(i=0;i<{iw + 66};i=i+1) begin
  {inp}='0;
  if(i==1) {inp}='1;
  else if(i>=2 && i<{iw + 2}) {inp}[i-2]=1'b1;
  else if(i>={iw + 2})
    for(j=0;j<{iw};j=j+1) {inp}[j]=$random(seed);
  expected=0;
  for(j=0;j<{iw};j=j+1) expected=expected+{inp}[j];
  #1; check({out},expected,{ow}'h{(1 << ow) - 1:x},
             $sformatf("population-count vector=%0h", {inp}));
end
""", "Independent bit sum; zero, all ones, every singleton, 64 mixed vectors")
        if re.search(r"sign.extend", prompt, re.I) and 1 <= iw <= 12 and iw <= ow <= 64:
            return SelfTest(f"integer i;\nreg [{ow-1}:0] expected;\n", f"""
for(i=0;i<{1 << iw};i=i+1) begin
  {inp}=i;
  expected=i;
  if({inp}[{iw-1}]) expected=expected | ({ow}'h{(1 << ow) - 1:x} << {iw});
  #1; check({out},expected,{ow}'h{(1 << ow) - 1:x},
             $sformatf("sign-extension input=%0h", {inp}));
end
""", f"Exhaustive {1 << iw} signed input patterns")
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
                hints = []
                clocks = [n for _, n in inputs if n.lower() in ("clk", "clock")]
                for clock in clocks:
                    for _, name in outputs:
                        changes = [(a, b) for a, b in zip(samples, samples[1:])
                                   if a[1][name] in ("0", "1")
                                   and b[1][name] in ("0", "1")
                                   and a[1][name] != b[1][name]]
                        stable = [(a, b) for a, b in changes
                                  if a[1][clock] == b[1][clock]]
                        if stable:
                            hints.append(f"{name} changes while {clock} stays "
                                         f"{stable[0][0][1][clock]}; edge-only "
                                         "storage cannot implement this waveform. "
                                         "Consider level-sensitive/combinational behavior.")
                        if changes and all(a[1][clock] == "1" and b[1][clock] == "0"
                                           for a, b in changes):
                            hints.append(f"All defined {name} transitions occur on falling "
                                         f"edges of {clock}; check negedge sampling.")
                        for _, source in inputs:
                            if source == clock:
                                continue
                            if _fits_waveform(samples, name, source, clock, "latch_high"):
                                hints.append(f"Every defined sample is consistent with "
                                             f"{name} as a HIGH-transparent latch of {source}: "
                                             f"always @* if ({clock}) {name} = {source}; "
                                             "no else, intentional hold. Do not replace it "
                                             "with a posedge flip-flop.")
                        for _, source in outputs + inputs:
                            if source in (clock, name):
                                continue
                            if _fits_waveform(samples, name, source, clock, "negedge"):
                                hints.append(f"Every defined sample is consistent with "
                                             f"{name} sampling {source} on negedge {clock}.")
                return SelfTest("", "\n".join(body),
                                "Every defined output in the supplied waveform",
                                " ".join(hints))
    return (_state_table(prompt, contract) or _moore_arrows(prompt, contract)
            or _edge_capture(prompt, contract))


def _galois_lfsr(prompt: str, contract: Contract) -> SelfTest | None:
    """Recognize only explicit right-shifting XOR Galois specifications.

    Topology, taps, seed, edge and reset must all be supplied. The reference
    recurrence is derived from prose, never a candidate or a task identifier.
    """
    text = re.sub(r"\s+", " ", prompt).lower()
    if (not re.search(r"galois lfsr", text)
            or not re.search(r"shifts? right|right.shift", text)
            or not re.search(r"xor(?:ed)?", text)
            or re.search(r"\bxnor\b|shifts? left|left.shift|negative edge|falling edge", text)
            or not re.search(r"positive edge|rising edge|posedge", text)
            or contract.reset_kind != "synchronous"
            or contract.reset_polarity != "high"):
        return None
    ports = {n: (d, w) for d, w, n in contract.ports}
    if (set(ports) != {"clk", "reset", "q"}
            or ports["clk"] != ("input", 1)
            or ports["reset"] != ("input", 1)
            or ports["q"][0] != "output"):
        return None
    width = ports["q"][1]
    taps = re.findall(r"\btaps? (?:at (?:bit )?positions? )?"
                      r"([0-9]+(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+)[0-9]+)*)"
                      r"\s*[.;]", text)
    seeds = re.findall(r"reset (?:the )?lfsr output to ([0-9]+)\s*[.;]", text)
    sizes = re.findall(r"(?:implement (?:a|an) |a )(\d+)-bit.{0,30}galois lfsr", text)
    if (not 2 <= width <= 64 or len(taps) != 1 or len(seeds) != 1
            or len(sizes) != 1 or int(sizes[0]) != width):
        return None
    positions = [int(p) for p in re.findall(r"\d+", taps[0])]
    seed = int(seeds[0])
    if (len(set(positions)) != len(positions)
            or not all(1 <= p <= width for p in positions)
            or width not in positions or not 0 < seed < (1 << width)):
        return None
    tap_mask = sum(1 << (p - 1) for p in positions)
    mask = (1 << width) - 1
    return SelfTest(f"""
integer i;
reg [{width-1}:0] expected;
reg [{width-1}:0] old_state;
task sample_edge;
begin
  #1;
  old_state=expected;
  if(reset) expected={width}'d{seed};
  else if(expected[0]) expected=(expected >> 1) ^ {width}'h{tap_mask:x};
  else expected=expected >> 1;
  clk=1; #1;
  check(q,expected,{width}'h{mask:x},$sformatf(
        "Galois old_state=%0h feedback=%0b taps=%0h cycle=%0d reset=%0b",
        old_state,old_state[0],{width}'h{tap_mask:x},i,reset));
  clk=0; #1;
end
endtask
""", f"""
clk=0; reset=1; i=0;
sample_edge;
reset=0;
for(i=0;i<{min((1 << width) + 2, 512)};i=i+1) sample_edge;
// Reset off-edge must hold; the following edge must restore the explicit seed.
reset=1; #1;
check(q,expected,{width}'h{mask:x},"synchronous reset off-edge holds");
sample_edge;
reset=0;
for(i=0;i<{min((1 << width) + 2, 512)};i=i+1) sample_edge;
""", "Explicit XOR Galois recurrence, every state for small registers, seed, synchronous reset and restart",
        f"Right-shifting Galois: shift the old state, then XOR old q[0] into "
        f"one-based tap bits p-1; mask={width}'h{tap_mask:x}. "
        "Do not replace distributed feedback with Fibonacci feedback.")


def _gshare(prompt: str, contract: Contract) -> SelfTest | None:
    normalized = re.sub(r"\s+", " ", prompt).lower()
    required = ("gshare branch predictor", "train_mispredicted",
                "prediction sees the pht state before training",
                "training takes precedence")
    if not all(phrase in normalized for phrase in required):
        return None
    expected = {
        "clk": ("input", 1), "areset": ("input", 1),
        "predict_valid": ("input", 1), "predict_pc": ("input", 7),
        "predict_taken": ("output", 1), "predict_history": ("output", 7),
        "train_valid": ("input", 1), "train_taken": ("input", 1),
        "train_mispredicted": ("input", 1), "train_history": ("input", 7),
        "train_pc": ("input", 7),
    }
    actual = {name: (direction, width) for direction, width, name in contract.ports}
    if (actual != expected or contract.reset_kind != "asynchronous"
            or contract.reset_polarity != "high"):
        return None
    return SelfTest("""
integer i,j;
reg [6:0] ref_history;
reg [1:0] ref_pht [0:127];
reg expected_taken;
task step;
  reg [6:0] lookup_index, update_index, history_before;
  begin
    #1;
    history_before = ref_history;
    expected_taken = 1'b0;
    if (predict_valid) begin
      lookup_index = predict_pc ^ ref_history;
      expected_taken = ref_pht[lookup_index][1];
      check(predict_taken, expected_taken, 1'b1,
            $sformatf("prediction history_before=%0h pc=%0h index=%0d",
                      history_before,predict_pc,lookup_index));
      check(predict_history, ref_history, 7'h7f,
            $sformatf("history before=%0h predict=%0b train=%0b mispred=%0b taken=%0b train_history=%0h train_pc=%0h",
                      history_before,predict_valid,train_valid,
                      train_mispredicted,train_taken,train_history,train_pc));
    end
    clk=1'b1; #1;
    if (train_valid) begin
      update_index = train_pc ^ train_history;
      if (train_taken) begin
        if (ref_pht[update_index] != 2'b11)
          ref_pht[update_index] = ref_pht[update_index] + 1'b1;
      end else if (ref_pht[update_index] != 2'b00) begin
        ref_pht[update_index] = ref_pht[update_index] - 1'b1;
      end
    end
    if (train_valid && train_mispredicted)
      ref_history = {train_history[5:0], train_taken};
    else if (predict_valid)
      ref_history = {history_before[5:0], expected_taken};
    clk=1'b0; #1;
  end
endtask
""", """
clk=0; areset=1; predict_valid=0; predict_pc=0;
train_valid=0; train_taken=0; train_mispredicted=0;
train_history=0; train_pc=0;
#2; areset=0; #1;
// Force every table entry to a known saturation endpoint; do not assume reset encoding.
for(i=0;i<128;i=i+1) begin
  train_pc=i; train_history=0; train_taken=1; train_valid=1;
  for(j=0;j<4;j=j+1) begin #1; clk=1; #1; clk=0; #1; end
  ref_pht[i]=2'b11;
end
// Establish a known global history through the specified recovery interface.
train_valid=1; train_mispredicted=1; train_taken=0;
train_history=0; train_pc=0; #1; clk=1; #1; clk=0; #1;
ref_history=0; train_valid=0; train_mispredicted=0; predict_valid=1;
for(i=0;i<128;i=i+1) begin predict_pc=i ^ ref_history; step(); end
predict_valid=0; train_taken=0; train_valid=1;
for(i=0;i<128;i=i+1) begin
  train_pc=i; train_history=0;
  for(j=0;j<4;j=j+1) begin #1; clk=1; #1; clk=0; #1; end
  ref_pht[i]=2'b00;
end
train_valid=0; predict_valid=1;
for(i=0;i<128;i=i+1) begin predict_pc=i ^ ref_history; step(); end
// The lookup on the second update must still see the old weak-not-taken counter.
train_valid=1; train_taken=1; train_mispredicted=0;
train_history=0; train_pc=7'd17; predict_pc=7'd17 ^ ref_history; step();
predict_pc=7'd17 ^ ref_history; step();
train_valid=0; predict_pc=7'd17 ^ ref_history; step();
// Ordinary training must not block a concurrent prediction history update.
train_valid=1; train_taken=1; train_mispredicted=0; train_pc=7'd6; train_history=0;
predict_pc=7'd20 ^ ref_history; step();
train_valid=0; predict_pc=7'd20 ^ ref_history; step();
// A misprediction recovery overrides the younger same-cycle prediction update.
train_valid=1; train_taken=1; train_mispredicted=1;
train_history=7'h35; train_pc=7'd9 ^ 7'h35; predict_pc=7'd4 ^ ref_history; step();
train_valid=0; predict_pc=7'd4 ^ ref_history; step();
""", "All 128 PHT entries checked at both saturation endpoints; pre-edge lookup, current history snapshot, concurrent training/prediction and misprediction recovery",
        "The reset counter encoding is not assumed; repeated training establishes known table endpoints.")


def _fits_waveform(samples, output, source, clock, kind):
    """Evaluate a primitive hypothesis against all defined rows, not a suffix."""
    observed = None
    checked = updates = holds = 0
    for index, (_, row) in enumerate(samples):
        active = (row[clock] == "1" if kind == "latch_high" else
                  index > 0 and samples[index-1][1][clock] == "1" and row[clock] == "0")
        if active:
            observed = row[source] if row[source] in ("0", "1") else None
            updates += 1
        else:
            holds += 1
        if row[output] in ("0", "1"):
            if observed != row[output]:
                return False
            checked += 1
    return checked >= 3 and updates >= 2 and holds >= 2


def _edge_capture(prompt: str, contract: Contract) -> SelfTest | None:
    normalized = re.sub(r"\s+", " ", prompt).lower()
    if (not re.search(r"capture.{0,70}changes from 1.{0,45}to 0", normalized)
            or not re.search(r"remain 1 until", normalized)
            or contract.reset_kind != "synchronous" or contract.reset_polarity != "high"):
        return None
    ports = {n: (d, w) for d, w, n in contract.ports}
    if (set(ports) != {"clk", "reset", "in", "out"}
            or ports["clk"] != ("input", 1) or ports["reset"] != ("input", 1)
            or ports["in"][0] != "input" or ports["out"][0] != "output"
            or ports["in"][1] != ports["out"][1]):
        return None
    width = ports["in"][1]
    return SelfTest(f"""
integer i,j,seed;
reg [{width-1}:0] previous, expected;
task sample_edge;
begin
  #2;
  if(reset) expected='0;
  else expected=expected | (previous & ~in);
  previous=in;
  clk=1; #1;
  check(out,expected,{width}'h{(1 << width)-1:x},
        $sformatf("capture input=%0h reset=%0b previous=%0h",in,reset,previous));
  clk=0; #1;
end
endtask
""", f"""
clk=0; in='0; reset=1; expected='0; previous='0; seed=42;
sample_edge;
in='1; sample_edge;
reset=0; in='0; sample_edge;
reset=1; in='0; sample_edge;
reset=0;
for(i=0;i<80;i=i+1) begin
  for(j=0;j<{width};j=j+1) in[j]=$random(seed);
  reset=(i%11==0);
  sample_edge;
end
""", "Sticky falling-edge capture, reset spanning edges, continued input history, mixed vectors",
        "History is sampled every clock; synchronous reset clears captured output only.")


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
