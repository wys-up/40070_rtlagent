"""Specification-only reference model with a controller-owned stimulus schedule."""

import re
import time
from pathlib import Path

from .llm import extract_code
from .tools import module_ports, strip_comments, source_safety_warnings
from .verification import SelfTest
from .skills import load_skills, select
from .spec_contract import check_semantics
from .fsm import graph_interface, generate_graph, UnsupportedGraph


def build_reference(code, contract, prompt):
    """Accept one isolated model, never executable verdict or driver code."""
    # Namespace isolation is mechanical; it does not derive or repair behavior.
    modules = re.findall(r"\bmodule\s+([A-Za-z_]\w*)", strip_comments(code))
    if len(modules) == 1 and modules[0] != "SpecReference":
        code = re.sub(r"(\bmodule\s+)" + re.escape(modules[0]) + r"\b",
                      r"\1SpecReference", code, count=1)
    # The unary minus precedes the sized literal in legal Verilog syntax.
    code = re.sub(r"\b(\d+\s*'[sS][dD])\s*-\s*(\d+)", r"-\1\2", code)
    inspected = strip_comments(code)
    name, ports = module_ports(code, "SpecReference")
    expected = [(d, w, n) for d, w, n in contract.ports]
    if (name != "SpecReference" or ports != expected
            or len(re.findall(r"\bmodule\b", inspected)) != 1):
        raise ValueError("reference must be one SpecReference with exact contract ports")
    rc, semantic_error = check_semantics(code, contract)
    if rc:
        raise ValueError("reference violates the explicit timing contract: " + semantic_error)
    hazards = source_safety_warnings(code, contract.ports)
    if hazards:
        raise ValueError("reference source hazard: " + "; ".join(hazards))
    forbidden = re.search(
        r"\b(?:dut|agent_\w*|ref_\w*|force|release|bind|fork|join|final)\b"
        r"|\$(?!signed\b|unsigned\b|clog2\b|bits\b)[A-Za-z_]\w*|`|#",
        inspected)
    if forbidden or any(d == "inout" for d, _, _ in ports):
        raise ValueError("reference has unsupported driver, hierarchy or system control")
    # Hierarchical reads can expose another module's state. Decimal constants
    # are irrelevant here: real-valued RTL is unsupported by the tool contract.
    if re.search(r"\b[A-Za-z_]\w*\s*\.\s*[A-Za-z_]", inspected):
        raise ValueError("reference cannot read hierarchical state")
    clocks = sorted(set(re.findall(
        r"\b(?:posedge|negedge)\s+([A-Za-z_]\w*)", inspected)))
    inputs = {n: w for d, w, n in ports if d == "input"}
    resets = [n for n, w in inputs.items() if w == 1 and re.fullmatch(
        r"a?reset\w*|rst\w*|clear\w*", n, re.I)]
    clocks = [n for n in clocks if n not in resets]
    declared_clocks = [n for n, w in inputs.items()
                       if w == 1 and re.fullmatch(r"clk|clock", n, re.I)]
    if not clocks and declared_clocks and not contract.combinational_only:
        clocks = declared_clocks
    if len(clocks) > 1 or any(inputs.get(n) != 1 for n in clocks):
        raise ValueError("automatic driver requires at most one scalar clock")
    clk = clocks[0] if clocks else None
    if clk and contract.combinational_only:
        raise ValueError("reference added a clocked process to combinational scope")
    if clk and resets and not contract.reset_polarity:
        raise ValueError("reset polarity is not established; use explicit checker")
    data = [(w, n) for d, w, n in ports
            if d == "input" and n != clk and n not in resets]
    width = sum(w for w, _ in data)
    exhaustive = not clk and width <= 10
    cycles = (1 << width) if exhaustive else 512
    inactive = 0
    if clk and re.search(rf"\bnegedge\s+{re.escape(clk)}\b", inspected):
        inactive = 1
    active_reset = 0 if contract.reset_polarity == "low" else 1
    declarations = "integer vector_index, bit_index, word_index, seed;\n"
    outputs = [(w, n) for d, w, n in ports if d == "output"]
    declarations += "\n".join(f"reg [{w-1}:0] care_{n};" for w, n in outputs)
    declarations += "\n" + "\n".join(f"integer seen_{n};" for _, n in outputs)

    def checks(phase):
        lines = []
        for w, n in outputs:
            # Unspecified power-up/don't-care bits are not assertions. Each
            # output must nevertheless acquire known values and be exercised.
            observed = f"ref_{n}" if w == 1 else f"ref_{n}[bit_index]"
            lines += [f"care_{n}=0;",
                      f"for(bit_index=0;bit_index<{w};bit_index=bit_index+1)",
                      f"  if({observed} === 1'b0 || "
                      f"{observed} === 1'b1) care_{n}[bit_index]=1;",
                      f"if(care_{n} != 0) begin",
                      f"  seen_{n}=seen_{n}+1;",
                      f'  check({n},ref_{n},care_{n},'
                      f'$sformatf("{phase} vector=%0d output={n}",vector_index));',
                      "end"]
        return "\n".join(lines)

    body = ["seed=37;"]
    body += [f"seen_{n}=0;" for _, n in outputs]
    if clk:
        body.append(f"{clk}={inactive};")
    for reset in resets:
        body.append(f"{reset}={active_reset};")
    body.append("#2;")
    if clk:
        body += [f"repeat(2) begin {clk}={1-inactive}; #1; "
                 f"{clk}={inactive}; #1; end"]
    for reset in resets:
        body.append(f"{reset}={1-active_reset};")
    body += [f"for(vector_index=0;vector_index<{cycles};vector_index=vector_index+1) begin"]
    offset = 0
    for w, n in data:
        if exhaustive:
            body.append(f"{n}=vector_index >> {offset};")
        else:
            target = n if w == 1 else f"{n}[word_index]"
            body += [f"for(word_index=0;word_index<{w};word_index=word_index+1)",
                     f"  {target}=$random(seed);"]
            # Walking-one/zero patterns catch index and endpoint mistakes even
            # when randomized vectors rarely activate an isolated control bit.
            body += [f"if(vector_index < {min(w, 128)}) {n}='0;",
                     f"if(vector_index < {min(w, 128)}) "
                     + (f"{n}=1'b1;" if w == 1 else f"{n}[vector_index % {w}]=1'b1;"),
                     f"if(vector_index == 128) {n}='0;",
                     f"if(vector_index == 129) {n}='1;"]
        offset += w
    if clk:
        for reset in resets:
            body.append(f"{reset}=(vector_index % 97 == 96) ? "
                        f"{active_reset} : {1-active_reset};")
        body += ["#1; agent_record_sample();", checks("pre-edge"),
                 f"{clk}={1-inactive}; #1;", checks("post-edge"),
                 f"{clk}={inactive}; #1;", checks("opposite-edge")]
    else:
        body += ["#1; agent_record_sample();", checks("combinational")]
    body.append("end")
    for _, n in outputs:
        body.append(f'if(seen_{n} == 0) check({n},ref_{n},'
                    f"'1,\"reference output never became known\");")
    return SelfTest(declarations, "\n".join(body),
                    f"Controller-owned {'exhaustive' if exhaustive else 'directed/random'} "
                    f"{cycles}-vector schedule; all observable outputs; "
                    "pre/post-edge and reset where applicable", reference=code)


def generate_reference(llm, prompt, interface, contract, timeout_s,
                       previous_error="", previous_response="", record=lambda *_: None,
                       record_plan=lambda *_: None):
    if graph_interface(prompt, contract):
        started = time.monotonic()
        try:
            raw, compiled = generate_graph(
                llm, prompt, contract, "SpecReference", timeout_s, 2304,
                feedback=previous_error, record=record, record_plan=record_plan,
                previous_graph=previous_response)
        except UnsupportedGraph:
            timeout_s -= time.monotonic()-started
            if timeout_s < 5:
                raise ValueError("insufficient budget for the general reference fallback")
        else:
            test = build_reference(extract_code(compiled.text), contract, prompt)
            test.derivation = raw.text
            return test
    messages = [{"role": "system", "content": """Write an independent SPECIFICATION
REFERENCE module named SpecReference with the exact supplied port names/order,
directions and widths. You cannot see candidate RTL. Return one complete Verilog
code block and nothing else. This is a mathematical behavioral reference, not an
optimized implementation: use simple loops, widened signed arithmetic, explicit
state transitions and direct equations. No testbench, delays, system control,
hierarchy or extra modules. Only the specified observable outputs matter.
Derive behavior solely from the specification. Follow actual table labels, not
an assumed ordering. Keep edge counts, output cycles, reset priorities, history
and back-to-back transitions exact. Do not invent an extra idle/latency cycle.
Every clock edge consumes one sample regardless of its value. Preserve state
needed for overlapping events and concurrent independent register updates.
When an input sample causes a transition, that sample has ALREADY been consumed;
do not add a second state/edge to consume the same delimiter or event again.
Write state names for the next unconsumed sample. Successful completion and
resynchronization after an invalid event are different paths unless the spec
says otherwise. Do not store data that cannot affect any declared output.
Reset only specified state; initial values only when explicitly specified.
The controller supplies clocks, resets, exhaustive small combinational inputs,
directed and random inputs and comparisons; you provide ONLY reference RTL.
Use ordinary integer/reg/always/assign, minimal code, no explanatory comments.
Keep the complete answer below 1800 tokens whenever feasible."""},
                {"role": "user", "content": prompt + "\nInterface:\n" + interface
                 + "\nContract:\n" + str(contract.to_dict())}]
    skills = select(load_skills(str(Path(__file__).resolve().parents[1] / "skill")), prompt)
    if skills:
        messages[0]["content"] += ("\nSpecification reasoning rules (the reference "
                                   "module is still named SpecReference):\n"
                                   + "\n".join(s.render() for s in skills[:2]))
    if previous_error:
        if previous_response:
            messages.append({"role": "assistant", "content": previous_response})
        messages.append({"role": "user", "content":
                         "Correct the independent reference, not the specification. "
                         "Return a shorter complete SpecReference. Feedback:\n"
                         + previous_error[:1800]})
    messages.append({"role": "user", "content":
                     "The original task's module name is irrelevant to this "
                     "isolated reference. Name it SpecReference. Use legal negative "
                     "literal syntax -W'sdN, never W'sd-N. Return complete RTL only."})
    started = time.monotonic()
    result = llm.chat(messages, timeout_s=timeout_s, max_tokens=2304)
    record(result, time.monotonic()-started)
    if result.finish_reason == "length":
        raise ValueError("reference generation was truncated")
    code = extract_code(result.text)
    return build_reference(code, contract, prompt)


def validate_review_witness(test, contract, witness, timeout_s, record=lambda *_: None):
    """Replay a reviewer's allegation against the reference, never the candidate."""
    from .tools import RtlToolchain
    if (not isinstance(witness, dict)
            or set(witness) != {"inputs", "output", "expected"}
            or not isinstance(witness["inputs"], list)
            or not 1 <= len(witness["inputs"]) <= 64):
        raise ValueError("counterexample needs inputs (1..64 step objects), "
                         "output name and integer expected value")
    inputs = {n: w for d, w, n in contract.ports if d == "input"}
    outputs = {n: w for d, w, n in contract.ports if d == "output"}
    out, expected = witness["output"], witness["expected"]
    if (not isinstance(out, str) or out not in outputs
            or type(expected) is not int or not 0 <= expected < (1 << outputs[out])):
        raise ValueError("counterexample needs a real output and in-range expected value")
    resets = [n for n, w in inputs.items() if w == 1 and re.fullmatch(
        r"a?reset\w*|rst\w*|clear\w*", n, re.I)]
    edges = re.findall(r"\b(posedge|negedge)\s+(\w+)", strip_comments(test.reference))
    clock_edges = [(edge, name) for edge, name in edges if name not in resets]
    clk = clock_edges[0][1] if clock_edges else None
    inactive = int(bool(clock_edges and clock_edges[0][0] == "negedge"))
    active_reset = int(contract.reset_polarity != "low")
    body = []
    if clk:
        body.append(f"{clk}={inactive};")
    for reset in resets:
        body.append(f"{reset}={active_reset};")
    body.append("#1;")
    if clk:
        body.append(f"{clk}={1-inactive}; #1; {clk}={inactive}; #1;")
    for reset in resets:
        body.append(f"{reset}={1-active_reset};")
    expanded = []
    for step in witness["inputs"]:
        if isinstance(step, dict) and set(step) == {"values", "repeat"}:
            if (type(step["repeat"]) is not int or not 1 <= step["repeat"] <= 64
                    or not isinstance(step["values"], dict)):
                raise ValueError("witness repeat needs 1..64 samples and input values")
            expanded.extend([step["values"]] * step["repeat"])
        else:
            expanded.append(step)
        if len(expanded) > 64:
            raise ValueError("expanded witness exceeds 64 samples")
    for vector in expanded:
        if not isinstance(vector, dict) or not vector or clk in vector:
            raise ValueError("each witness step supplies real non-clock inputs")
        for name, value in vector.items():
            if (name not in inputs or type(value) is not int
                    or not 0 <= value < (1 << inputs[name])):
                raise ValueError("witness input must be a real port with in-range value")
            body.append(f"{name}={inputs[name]}'h{value:x};")
        body.append("#1;")
        if clk:
            body.append(f"{clk}={1-inactive}; #1; {clk}={inactive}; #1;")
    width = outputs[out]
    body.append(f"check({out},{width}'h{expected:x},"
                f"{width}'h{(1<<width)-1:x},\"review witness\");")
    checker = SelfTest("", "\n".join(body), "Reviewer counterexample replay")
    reference_dut = re.sub(r"\bmodule\s+SpecReference\b",
                           "module " + contract.top, test.reference, count=1)
    started = time.monotonic()
    rc, log = RtlToolchain().simulate(reference_dut, checker.render(contract), timeout_s)
    record(rc, log, time.monotonic()-started)
    if rc == 0:
        raise ValueError("review counterexample disproved by simulation: reference "
                         "produced the alleged correct expected value. Correct the "
                         "review of the UNCHANGED reference; do not invent another "
                         "requirement. Approve if no actual defect can be demonstrated.\n"
                         + log[:1000])
    if rc != 1:
        raise ValueError("review counterexample could not be validated: " + log[:1000])
    return log
