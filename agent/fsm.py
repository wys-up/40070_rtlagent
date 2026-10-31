"""Compile independently derived small Moore transition graphs, not circuit templates."""

import itertools
import json
import re
import time
from pathlib import Path

from .llm import LLMResult, strip_think
from .skills import load_skills, select


class UnsupportedGraph(ValueError):
    pass


def _registered_graph(graph, inputs, outputs):
    """Convert edge emissions to a reachable Moore product, without protocol rules."""
    if (set(graph) != {"reset_state", "reset_outputs", "states"}
            or not isinstance(graph["states"], list)
            or not 1 <= len(graph["states"]) <= 64):
        raise ValueError("registered graph needs reset_state, reset_outputs and states")

    def valid_outputs(values):
        return (isinstance(values, dict) and set(values) == set(outputs)
                and all(type(v) is int and v in (0, 1) for v in values.values()))

    if not valid_outputs(graph["reset_outputs"]):
        raise ValueError("reset_outputs must define every real output as 0 or 1")
    states = {}
    patterns = {"".join(bits) for bits in itertools.product("01", repeat=len(inputs))}
    for state in graph["states"]:
        if (not isinstance(state, dict)
                or set(state) not in ({"name", "next"}, {"name", "next", "repeat"})
                or not isinstance(state["name"], str)
                or not re.fullmatch(r"[A-Za-z_]\w{0,31}", state["name"])
                or state["name"] in states
                or not isinstance(state["next"], dict)
                or set(state["next"]) != patterns):
            raise ValueError("each phase needs a unique name and every input VALUE key")
        repeat = state.get("repeat", {"count": 1, "outputs": graph["reset_outputs"]})
        if (not isinstance(repeat, dict) or set(repeat) != {"count", "outputs"}
                or type(repeat["count"]) is not int or not 1 <= repeat["count"] <= 64
                or not valid_outputs(repeat["outputs"])):
            raise ValueError("repeat needs count 1..64 and all outputs during repetition")
        for transition in state["next"].values():
            if (not isinstance(transition, dict)
                    or set(transition) != {"state", "outputs"}
                    or not isinstance(transition["state"], str)
                    or not valid_outputs(transition["outputs"])):
                raise ValueError("each next entry needs target state and outputs AFTER this edge")
        states[state["name"]] = state, repeat
    if (not isinstance(graph["reset_state"], str) or graph["reset_state"] not in states
            or any(t["state"] not in states for state, _ in states.values()
                   for t in state["next"].values())):
        raise ValueError("reset and all transitions must name declared phases")
    initial = (graph["reset_state"], 0,
               tuple(graph["reset_outputs"][n] for n in outputs))
    pending, indices, compiled = [initial], {initial: 0}, []
    for phase, offset, values in pending:
        state, repeat = states[phase]
        targets = {}
        for pattern in sorted(patterns):
            if offset+1 < repeat["count"]:
                target = phase, offset+1, tuple(repeat["outputs"][n] for n in outputs)
            else:
                transition = state["next"][pattern]
                target = (transition["state"], 0,
                          tuple(transition["outputs"][n] for n in outputs))
            if target not in indices:
                if len(pending) >= 64:
                    raise ValueError("registered graph expands beyond 64 reachable states")
                indices[target] = len(pending)
                pending.append(target)
            targets[pattern] = "G" + str(indices[target])
        compiled.append(dict(name="G" + str(len(compiled)),
                             outputs=dict(zip(outputs, values)), next=targets))
    unused = set(states) - {phase for phase, _, _ in pending}
    if unused:
        raise ValueError("unreachable phases: " + ", ".join(sorted(unused)))
    return dict(reset_state="G0", states=compiled)


def graph_interface(prompt, contract):
    if (contract.combinational_only or not contract.reset_kind
            or not contract.reset_polarity or not re.search(
                r"\bFSM\b|finite.state machine|\bMoore\b", prompt, re.I)):
        return None
    if any(d == "inout" for d, _, _ in contract.ports):
        return None
    inputs = [(w, n) for d, w, n in contract.ports if d == "input"]
    outputs = [(w, n) for d, w, n in contract.ports if d == "output"]
    clocks = [n for w, n in inputs if w == 1 and re.fullmatch(r"clk|clock", n, re.I)]
    resets = [n for w, n in inputs if w == 1 and re.fullmatch(r"a?reset\w*|rst\w*", n, re.I)]
    data = [(w, n) for w, n in inputs if n not in clocks + resets]
    if (len(clocks) != 1 or len(resets) != 1 or not outputs
            or any(w != 1 for w, _ in data + outputs)
            or not 1 <= len(data) <= 4):
        return None
    return clocks[0], resets[0], [n for _, n in data], [n for _, n in outputs]


def compile_graph(text, prompt, contract, top):
    interface = graph_interface(prompt, contract)
    if not interface:
        raise ValueError("task is outside the bounded scalar Moore graph subset")
    clk, reset, inputs, outputs = interface
    text = strip_think(text).strip()
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    if len(blocks) > 1:
        valid = set()
        for block in blocks[:4]:
            try:
                valid.add(compile_graph(block, prompt, contract, top))
            except (ValueError, TypeError):
                continue
        if len(blocks) <= 4 and len(valid) == 1:
            return valid.pop()
        raise ValueError("response needs one unique structurally valid graph")
    graph = json.loads(blocks[0] if blocks else text)
    if graph == {"unsupported": True}:
        raise UnsupportedGraph("specification requires behavior outside a scalar Moore graph")
    if isinstance(graph, dict) and "reasoning" in graph:
        if not isinstance(graph["reasoning"], str):
            raise ValueError("graph reasoning must be explanatory text")
        graph = {k: v for k, v in graph.items() if k != "reasoning"}
    if isinstance(graph, dict) and "reset_outputs" in graph:
        return compile_graph(json.dumps(_registered_graph(graph, inputs, outputs)),
                             prompt, contract, top)
    if (not isinstance(graph, dict) or set(graph) != {"reset_state", "states"}
            or not isinstance(graph["states"], list)
            or not 1 <= len(graph["states"]) <= 64):
        raise ValueError("graph requires reset_state and 1..64 complete states")
    states = graph["states"]
    names = [s.get("name") for s in states if isinstance(s, dict)]
    if (len(names) != len(states) or any(not isinstance(n, str) or not re.fullmatch(
            r"[A-Za-z_]\w{0,31}", n) for n in names) or len(set(names)) != len(names)
            or graph["reset_state"] not in names):
        raise ValueError("graph needs unique state names and a declared reset_state")
    patterns = {"".join(bits) for bits in itertools.product("01", repeat=len(inputs))}
    for state in states:
        if (isinstance(state, dict) and isinstance(state.get("next"), dict)
                and set(state["next"]) != patterns):
            raise ValueError("next keys must be INPUT VALUES "
                             + json.dumps(sorted(patterns))
                             + ", never input port names; received "
                             + json.dumps(sorted(state["next"])))
        if (set(state) not in ({"name", "outputs", "next"},
                               {"name", "outputs", "next", "repeat"})
                or type(state.get("repeat", 1)) is not int
                or not 1 <= state.get("repeat", 1) <= 64
                or not isinstance(state["outputs"], dict)
                or set(state["outputs"]) != set(outputs)
                or any(type(v) is not int or v not in (0, 1)
                       for v in state["outputs"].values())
                or not isinstance(state["next"], dict)
                or set(state["next"]) != patterns
                or any(not isinstance(n, str) or n not in names
                       for n in state["next"].values())):
            raise ValueError("each state needs all real outputs and every input pattern")
    reachable = {graph["reset_state"]}
    while True:
        updated = reachable | {target for state in states if state["name"] in reachable
                               for target in state["next"].values()}
        if updated == reachable:
            break
        reachable = updated
    if reachable != set(names):
        raise ValueError("unreachable states from reset: "
                         + ", ".join(sorted(set(names)-reachable))
                         + "; rederive transitions or remove unused states")
    expanded = []
    for state in states:
        count = state.get("repeat", 1)
        for offset in range(count):
            targets = {p: ((state["name"], offset+1) if offset+1 < count
                           else (state["next"][p], 0)) for p in patterns}
            expanded.append(((state["name"], offset), state["outputs"], targets))
    if len(expanded) > 64:
        raise ValueError("expanded repeat graph exceeds 64 states")
    indices = {name: index for index, (name, _, _) in enumerate(expanded)}
    reset_index = indices[(graph["reset_state"], 0)]
    width = max(1, (len(expanded)-1).bit_length())
    declarations = [f"  {d}{' reg' if d == 'output' else ''} {n}"
                    for d, _, n in contract.ports]
    clock_name = rf"(?:{re.escape(clk)}|clock)"
    falling = rf"(?:negative[- ]edge|falling[- ]edge|negedge)"
    edge = "negedge" if re.search(
        rf"\b{falling}\s+(?:of\s+(?:the\s+)?)?{clock_name}\b|"
        rf"\b{clock_name}\s+(?:is\s+)?(?:sampled\s+on\s+)?(?:the\s+)?{falling}\b",
        prompt, re.I) else "posedge"
    reset_condition = reset if contract.reset_polarity == "high" else "!" + reset
    events = edge + " " + clk
    if contract.reset_kind == "asynchronous":
        events += " or " + ("posedge" if contract.reset_polarity == "high" else "negedge") + " " + reset
    code = [f"module {top}(\n" + ",\n".join(declarations) + ");",
            f"reg [{width-1}:0] state;",
            f"always @({events}) begin",
            f"  if({reset_condition}) state <= {width}'d{reset_index};",
            "  else case(state)"]
    for index, (_, _, targets) in enumerate(expanded):
        code += [f"    {width}'d{index}: case({{{','.join(inputs)}}})"]
        code += [f"      {len(inputs)}'b{pattern}: state <= {width}'d{indices[target]};"
                 for pattern, target in sorted(targets.items())]
        code += [f"      default: state <= {width}'d{index};", "    endcase"]
    code += [f"    default: state <= {width}'d{reset_index};",
             "  endcase", "end", "always @* begin", "  case(state)"]
    for index, (_, values, _) in enumerate(expanded):
        assignments = " ".join(f"{n}=1'b{values[n]};" for n in outputs)
        code.append(f"    {width}'d{index}: begin {assignments} end")
    code += ["    default: begin " + " ".join(f"{n}=1'b0;" for n in outputs) + " end",
             "  endcase", "end", "endmodule"]
    return "\n".join(code) + "\n"


def generate_graph(llm, prompt, contract, top, timeout_s, max_tokens,
                   feedback="", record=lambda *_: None, record_plan=lambda *_: None,
                   previous_graph=""):
    clk, reset, inputs, outputs = graph_interface(prompt, contract)
    patterns = ["".join(bits) for bits in itertools.product("01", repeat=len(inputs))]
    started = time.monotonic()
    plan_messages = [
        {"role": "system", "content": """Analyze this synchronous specification
before any implementation. Return a concise table of phases, no RTL or JSON.
For each phase give its entry event, NUMBER of additional sampling edges,
input conditions, observable outputs, and next phase. Copy each finite repeat
count literally from the specification; do not manually enumerate repeated rows.
Separate waiting for a required event, successful completion, and recovery from
an invalid event. Explain how the immediately following sample is consumed.
Use only original requirements. Identify distinct histories needing states.
Histories with different future output behavior need distinct phases even when
their current outputs are identical.
Do not add validation cycles or delays absent from the specification.
A Moore state records history AFTER a sampling edge; next consumes the next
input. Current outputs must not depend on an input that has not been sampled.
Keep the derivation short and complete, within 400 tokens."""},
        {"role": "user", "content": prompt}]
    skills = select(load_skills(str(Path(__file__).resolve().parents[1] / "skill")), prompt)
    if skills:
        plan_messages[0]["content"] += (
            "\nRelevant reasoning rules; original requirements control. Return "
            "only the phase table, not RTL:\n"
            + "\n".join(s.render() for s in skills[:2]))
    plan = llm.chat(plan_messages,
        timeout_s=min(60, timeout_s * 0.4), max_tokens=640)
    record_plan(plan, time.monotonic()-started)
    if plan.finish_reason == "length":
        raise ValueError("FSM sampling derivation was truncated")
    timeout_s -= time.monotonic()-started
    if timeout_s < 10:
        raise ValueError("insufficient budget to compile the derived state graph")
    system = """Translate the ORIGINAL specification and phase table into a
complete synchronous transition graph. Return JSON only, with reset_state,
reset_outputs (all actual output names with 0/1 values), and states.
Each state is {"name":"phase_name","next":{INPUT_VALUE:
{"state":"target_phase","outputs":{ACTUAL_OUTPUT:0}}}}.
Each next map has EVERY input bit VALUE pattern in the supplied input order,
not input port names. Each transition outputs object assigns EVERY real output.
These outputs are the registered values AFTER consuming THIS input edge.
They hold until the next sampling edge. The compiler owns clock/reset wiring.
The target phase is ready for the NEXT input sample, not the one just consumed.
Do not add validation/idle samples absent from the original requirements.

For an input-independent phase lasting N additional sampling edges AFTER entry,
add "repeat":{"count":N,"outputs":{ACTUAL_OUTPUT:0}} to that ONE phase.
The compiler expands the exact count. Intermediate edges use repeat.outputs.
On the Nth edge, next selects the target and output values using that input.
The entry edge is NOT part of the repeat count. Do not unroll repeated phases.

Waiting for an event and recovery after an invalid event are distinct phases
when future output behavior differs. A recovery event is not a successful
completion unless the original specification explicitly says so. Do not lose
the next input after success. Every declared phase must be reachable from reset.
No unobservable data fields, RTL, testbench, comments or prose. At most 64
expanded states. Use only original requirements. If this bounded registered
output representation cannot express the task, return {"unsupported":true}."""
    if skills:
        system += ("\nRelevant specification reasoning rules; keep graph JSON:\n"
                   + "\n".join(s.render() for s in skills[:2]))
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": prompt + "\nInput pattern order: "
                 + ",".join(inputs) + "\nOutputs: " + ",".join(outputs)
                 + "\nClock: " + clk + "\nReset: " + reset
                 + "\nEVERY next object must have exactly these literal keys: "
                 + json.dumps(patterns)
                 + ". These are input bit VALUES, NOT port names."
                 + "\nA state object has this exact shape (replace state/output "
                   "values using the specification): "
                 + json.dumps(dict(name="STATE_NAME", next={
                     p: dict(state="TARGET_STATE_NAME", outputs={n: 0 for n in outputs})
                     for p in patterns}))
                 + "\nDerive the reset state and all transitions from this specification."},
                {"role": "assistant", "content": plan.text},
                {"role": "user", "content":
                 "Use the phase table to derive a complete graph. Recheck it against "
                 "the ORIGINAL requirements. Each transition consumes one new sample "
                 "and specifies all outputs AFTER that sample. "
                 "Return only the required JSON now."}]
    if feedback:
        if previous_graph:
            messages.append({"role": "assistant", "content": previous_graph})
        messages.append({"role": "user", "content":
                         "Repair your preceding graph using the ORIGINAL specification "
                         "and the first concrete counterexample. Trace which phase "
                         "consumes that input before editing. A repeated data phase's "
                         "last input is still data; a following condition consumes a "
                         "SEPARATE next input. Preserve behavior already correct; "
                         "do not shift a repeat count to conceal a missing phase.\n"
                         + feedback[:3500]})
    started = time.monotonic()
    result = llm.chat(messages, timeout_s=timeout_s,
                      max_tokens=min(max_tokens, max(256, int(timeout_s * 14))))
    record(result, time.monotonic()-started)
    if result.finish_reason == "length":
        raise ValueError("transition graph was truncated")
    code = compile_graph(result.text, prompt, contract, top)
    return result, LLMResult("```verilog\n" + code + "```", result.tokens_in,
                            result.tokens_out, result.finish_reason)
