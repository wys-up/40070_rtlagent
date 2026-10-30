"""Build an independent, bounded self-check from the specification only.

The controller owns the instance, comparison task, counters and verdict. A model
self-test is useful evidence, never a substitute for an independent official TB.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time

from .llm import LLM, strip_think
from .spec_contract import Contract

SELFTEST_MAX_TOKENS = 2048
REVIEW_MAX_TOKENS = 512
from .tools import strip_comments


@dataclass
class SelfTest:
    declarations: str
    stimulus: str
    coverage: str
    uncertainties: str = ""

    def render(self, contract: Contract) -> str:
        width = max([1024] + [w for _, w, _ in contract.ports])
        ports = []
        init = []
        for direction, bits, name in contract.ports:
            if not 0 < bits <= 8192:
                raise ValueError("unresolved or unsupported port width for self-test")
            rng = "" if bits == 1 else f"[{bits - 1}:0] "
            if direction == "inout":
                raise ValueError("inout self-test requires an explicit bus driver")
            ports.append(f"  {'reg' if direction == 'input' else 'wire'} {rng}{name};")
            if direction == "input":
                init.append(f"    {name} = '0;")
        connections = ", ".join(f".{n}({n})" for _, _, n in contract.ports)
        return f"""`timescale 1ns/1ps
module agent_tb;
{chr(10).join(ports)}
  {contract.top} dut ({connections});
  integer agent_checks = 0;
  integer agent_failures = 0;
  task check;
    input [{width - 1}:0] actual, expected, mask;
    input string label;
    begin
      if ((^mask === 1'bx) || (^(expected & mask) === 1'bx)) begin
        $display("AGENT_CHECKER_ERROR unknown expected/mask %s", label);
        $fatal(1, "invalid oracle");
      end
      if (mask == 0) begin
        $display("AGENT_CHECKER_ERROR empty mask %s", label);
        $fatal(1, "empty comparison");
      end
      agent_checks = agent_checks + 1;
      if ((actual & mask) !== (expected & mask)) begin
        agent_failures = agent_failures + 1;
        $display("AGENT_MISMATCH t=%0t case=%s actual=%0h expected=%0h mask=%0h",
                 $time, label, actual, expected, mask);
        if (agent_failures >= 8) $fatal(1, "too many mismatches");
      end
    end
  endtask
{self.declarations}
  initial begin
{chr(10).join(init)}
    begin : agent_stimulus
{self.stimulus}
    end
    if (agent_checks == 0) begin
      $display("AGENT_CHECKER_ERROR no comparisons executed");
      $fatal(1, "empty self-test");
    end
    if (agent_failures != 0) $fatal(1, "functional mismatches");
    $display("AGENT_SELFTEST_PASS checks=%0d", agent_checks);
    $finish;
  end
  initial begin
    #1000000;
    $display("AGENT_CHECKER_ERROR simulation watchdog expired");
    $fatal(1, "self-test timeout");
  end
endmodule
"""


def _stimulus_declarations(text: str):
    """Read names from ordinary variable declarations, not initializer arguments."""
    inspected = re.sub(r'"(?:\\.|[^"\\])*"', '""', strip_comments(text))
    # Keep procedural declarations in a small, auditable subset. Type aliases,
    # enum constants and implicit foreach indices need a full SV scope parser;
    # reject them instead of missing a shadowed DUT port.
    unsupported = re.search(
        r"\b(?:typedef|enum|struct|union|foreach|parameter|localparam|let|var|type)\b",
        inspected)
    if unsupported:
        raise ValueError("unsupported declaration or loop in stimulus: "
                         + unsupported.group() + "; use ordinary variables and for loops, "
                         "and put helper types/constants in declarations")
    for declaration in re.finditer(
            r"\b(?:reg|wire|logic|bit|byte|shortint|int|longint|integer|time|real|realtime"
            r"|shortreal|string|chandle|event)"
            r"\b\s*([^;]+);",
            inspected, re.S):
        declarators = declaration.group(1).lstrip()
        # Packed widths may themselves contain selects, e.g. $bits(ref[0]).
        # Skip balanced dimensions rather than losing the declaration name.
        while declarators:
            qualifier = re.match(r"(?:signed|unsigned)\b\s*", declarators)
            if qualifier:
                declarators = declarators[qualifier.end():].lstrip()
            elif declarators.startswith("["):
                depth = 0
                for end, char in enumerate(declarators):
                    depth += (char == "[") - (char == "]")
                    if depth == 0:
                        declarators = declarators[end + 1:].lstrip()
                        break
                else:
                    raise ValueError("unsupported declaration or loop: unclosed packed width")
            else:
                break
        depth, start = 0, 0
        parts = []
        for index, char in enumerate(declarators):
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append(declarators[start:index])
                start = index + 1
        parts.append(declarators[start:])
        for part in parts:
            name = re.match(r"\s*([A-Za-z_]\w*)", part)
            if name:
                yield name.group(1)


def parse_selftest(text: str, contract: Contract) -> SelfTest:
    text = strip_think(text).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)```\s*", text, re.S)
    data = json.loads(fenced.group(1) if fenced else text)
    if not isinstance(data, dict):
        raise ValueError("self-test response must be a JSON object")
    for field in ("declarations", "stimulus", "coverage"):
        if not isinstance(data.get(field), str):
            raise ValueError(f"self-test field {field} must be a string")
    test = SelfTest(data["declarations"], data["stimulus"], data["coverage"],
                    str(data.get("uncertainties", "")))
    code = test.declarations + "\n" + test.stimulus
    # The model can supply a reference model and stimuli, not verdict machinery.
    forbidden = (r"\bagent_\w*\b|\bdut\b|\b(?:module|endmodule|initial|always"
                 r"|always_ff|always_comb|final|fork|join|force|release|bind)\b"
                 r"|\$(?!signed\b|unsigned\b|clog2\b|bits\b|random\b|urandom\b"
                 r"|urandom_range\b|sformatf\b)[A-Za-z_]\w*|`")
    inspected = re.sub(r'"(?:\\.|[^"\\])*"', '""', strip_comments(code))
    if "\\" in inspected:
        raise ValueError("unsupported declaration or loop: escaped identifiers")
    match = re.search(forbidden, inspected)
    if match:
        raise ValueError("self-test contains forbidden control or DUT introspection: "
                         + match.group(0))
    unsupported_type = re.search(
        r"\b(?:typedef|enum|struct|union|class|endclass|nettype)\b", inspected)
    if unsupported_type:
        raise ValueError("unsupported declaration or loop in checker: "
                         + unsupported_type.group() + "; use reg/integer/localparam "
                         "reference models without custom types")
    # Class-only qualifiers are invalid on module-scope helper routines. Reject
    # them before spending another model call on review or launching Vivado.
    if re.search(r"\b(?:local|protected|virtual)\s+"
                 r"(?:(?:static|automatic)\s+)?(?:task|function)\b", inspected):
        raise ValueError("self-test helpers are module scoped; use task/function "
                         "without local, protected or virtual qualifiers")
    # Models often echo supplied ports. Drop equivalent standalone declarations;
    # reject width changes. Never normalize declarations inside helper routines.
    protected = [(m.start(), m.end()) for m in re.finditer(
        r"\bfunction\b.*?\bendfunction\b|\btask\b.*?\bendtask\b",
        test.declarations, re.S)]
    port_widths = {n: w for _, w, n in contract.ports}
    shadowed = port_widths.keys() & set(_stimulus_declarations(test.stimulus))
    if shadowed:
        raise ValueError("stimulus local declaration shadows a DUT port: "
                         + ", ".join(sorted(shadowed)))
    edits = []
    for match in re.finditer(r"\b(?:reg|wire|logic)\s+"
                             r"(\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*)?"
                             r"([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*;",
                             test.declarations):
        if any(start <= match.start() < end for start, end in protected):
            continue
        names = [n.strip() for n in match.group(4).split(",")]
        width = abs(int(match.group(2)) - int(match.group(3))) + 1 if match.group(1) else 1
        duplicate = [n for n in names if n in port_widths]
        for name in duplicate:
            if width != port_widths[name]:
                raise ValueError(f"duplicate port '{name}' is {width} bits; controller "
                                 f"already declares {port_widths[name]} bits")
        if duplicate:
            kept = [n for n in names if n not in duplicate]
            prefix = match.group()[:match.start(4) - match.start()]
            edits.append((match.start(), match.end(),
                          prefix + ", ".join(kept) + ";" if kept else ""))
    for start, end, replacement in reversed(edits):
        test.declarations = test.declarations[:start] + replacement + test.declarations[end:]
    if re.search(r"\b(?:task|function)\b[^;]*\bcheck\b", inspected):
        raise ValueError("self-test must not redefine check")
    if not re.search(r"\bcheck\s*\(", test.stimulus):
        raise ValueError("self-test has no check calls in stimulus; put executable "
                         "check(actual_output, expected, nonzero_mask, label) calls "
                         "directly in stimulus after driving and settling inputs, "
                         "not in coverage or helper declarations")
    actuals = re.findall(r"\bcheck\s*\(([^,;]*)", strip_comments(test.stimulus))
    outputs = [n for d, _, n in contract.ports if d == "output"]
    if not any(re.search(rf"\b{re.escape(n)}\b", actual)
               for n in outputs for actual in actuals):
        raise ValueError("check actual argument must observe a DUT output")
    test.render(contract)  # Validate supported ports before launching tools.
    return test


def generate_selftest(llm: LLM, prompt: str, interface: str, contract: Contract,
                      timeout_s: float, previous_error: str = "",
                      previous_response: str = "",
                      record=lambda result, elapsed: None) -> SelfTest:
    if not contract.ports or not any(d == "output" for d, _, _ in contract.ports):
        raise ValueError("no reliable output interface for independent self-test")
    messages = [
        {"role": "system", "content": """You write an independent SystemVerilog
reference checker from a specification. You will NOT see the DUT implementation.
Return ONLY a JSON object with string fields declarations, stimulus, coverage,
uncertainties. The controller supplies module agent_tb, input regs initialized to
zero, output wires, the DUT instance, a watchdog and a comparison task:
  check(actual, expected, mask, "case description / cycle / input");
All vector arguments are zero-extended to at least 1024 bits. Use explicitly sized
masks for the output bits being compared. Each call must check at least one bit.
declarations: reference variables, localparams and helper tasks/functions only.
Use built-in reg/integer types and localparams, not typedef/enum/struct/classes.
Helpers are module scoped, not class methods: use task automatic tick; begin
#1; end endtask. Never use local/protected/virtual task or function qualifiers.
End a standalone delay with a semicolon: #1; before end/endtask/endfunction.
NEVER redeclare the input/output ports; their exact names and widths are in the
parsed contract. Declare all helper variables here, not halfway through a block.
stimulus: statements INSIDE an existing initial begin/end; no initial/module.
Put every check(...) call directly in stimulus, interleaved with input driving
and settling. Helpers may compute references or tick clocks, not call check().
coverage: plain-language summary only; code placed here is NEVER executed.
Local ordinary variables must not shadow DUT ports. Put constants in declarations;
use explicit for loops, not foreach or custom types.
Use # delays and explicit clock assignments; do not generate an always clock.
Drive away from the sampling edge, sample after #1 for settled NBA values.
Never call $display/$finish/$fatal/$stop or access dut or agent_* names.
$sformatf for a check label is allowed. Use localparam integer, not INTEGER.
Use an integer cycle counter or a static string for labels, not $time/$realtime.
Only check() establishes pass/fail. Expected values MUST follow the spec, not a
guessed circuit. Independently enumerate small truth tables/K-maps using their
actual row/column labels and care masks. For sequential designs use a reference
transition table and cycle count. Cover reset off-edge/on-edge and release,
priorities, boundary counts, asymmetric shifts, overlaps, back-to-back frames
and error recovery as relevant. Do not assume all history is cleared on reset.
For serial frames, count EVERY data sampling edge, including zero-valued bits;
test all-zero bytes, mixed bytes and malformed stops. Do not infer a done/output
cycle that the prose does not establish. For feedback registers, first identify
Galois versus Fibonacci, shift direction, XOR versus XNOR and tap numbering;
derive the reference next-state equations from those explicit requirements.
Do not compare unspecified initial states or don't-care bits. If an oracle cannot
be derived reliably, put the reason in uncertainties and do not invent expected
values. Keep the test bounded to at most 2048 cycles, avoid huge unrolled code.
Use ordinary Verilog syntax (integer, reg, explicit widths) in the body.
CRITICAL: A synchronous reset must be asserted THROUGH an active clock edge.
The sequence "#1 reset=1; #1 reset=0;" without a clock edge does NOT reset a DUT.
Initialize a synchronous design by holding reset through at least one active
edge, allow #1 for NBA updates, then release away from the edge. Later reset
tests must obey the same rule. An off-edge reset assertion must leave a
synchronous output unchanged until the next active edge. Never expect power-up
values before a specified initialization or reset. For asynchronous reset,
also test the off-edge effect explicitly.
"""},
        {"role": "user", "content": "Specification:\n" + prompt
         + "\nInterface:\n" + interface
         + "\nParsed contract:\n" + json.dumps(contract.to_dict(), ensure_ascii=False)},
    ]
    if previous_error:
        if previous_response:
            messages.append({"role": "assistant", "content": previous_response})
        messages.append({"role": "user", "content":
                         "The previous checker was rejected. Correct the checker, "
                         "not the specification. Return the complete corrected JSON "
                         "with declarations, stimulus, coverage and uncertainties. "
                         "Put executable check(...) calls directly in stimulus; "
                         "coverage is prose only. Do not repeat the reported error.\n"
                         "Validation feedback:\n" + previous_error})
    started = time.monotonic()
    result = llm.chat(messages, timeout_s=timeout_s,
                      max_tokens=SELFTEST_MAX_TOKENS)
    record(result, time.monotonic() - started)
    if result.finish_reason == "length":
        raise ValueError("checker generation was truncated; shorten the checker "
                         "using bounded loops and return complete JSON")
    return parse_selftest(result.text, contract)


def review_selftest(llm: LLM, prompt: str, interface: str, contract: Contract,
                    test: SelfTest, timeout_s: float,
                    record=lambda result, elapsed: None) -> SelfTest:
    """Approve a frozen specification-only oracle or return reasons to rebuild it.

    Reviewing must not rewrite the payload: full rewrites can silently remove
    checks and consume the budget meant for RTL repair. A rejection still goes
    through the existing independent generator/review loop, never simulation.
    """
    messages = [
        {"role": "system", "content": """Independently audit a proposed SystemVerilog
checker against the ORIGINAL specification. No DUT implementation is provided.
Return ONLY JSON with exactly two fields: approved (boolean), issues (array of
short strings). Do NOT rewrite the checker or provide RTL/testbench code.
Approve only if the EXECUTED comparisons, not just coverage prose, derive from
the specification and provide useful functional coverage. Check initialization,
synchronous reset spanning an active edge, off-edge synchronous reset holding,
asynchronous reset timing, old/new-state observation, output cycles, priorities,
bit order, widths, care masks, helper syntax, and unknown initial states.
For multi-input designs require mixed/nonuniform values and relevant boundaries;
do not approve a checker that only checks zeros/ones or invents missing behavior.
Declarations are module scoped; stimulus runs in an initialized named block.
The controller provides input regs, output wires, DUT instance, watchdog and
check(actual, expected, mask, label). No initial/always, DUT internals, verdict
machinery or input/output redeclarations are allowed in the proposed fields.
If correct: {"approved":true,"issues":[]}.
Otherwise return approved=false with up to four concise, actionable issues,
each at most 500 characters.
An uncertain oracle must be rejected. Never adapt expectations to a DUT."""},
        {"role": "user", "content": "Specification:\n" + prompt
         + "\nInterface:\n" + interface
         + "\nParsed contract:\n" + json.dumps(contract.to_dict(), ensure_ascii=False)
         + "\nProposed checker:\n" + json.dumps(test.__dict__, ensure_ascii=False)},
    ]
    started = time.monotonic()
    result = llm.chat(messages, timeout_s=timeout_s, max_tokens=REVIEW_MAX_TOKENS)
    record(result, time.monotonic() - started)
    if result.finish_reason == "length":
        raise ValueError("checker review was truncated; no approval")
    text = strip_think(result.text).strip()
    fenced = re.fullmatch(r"```(?:json)?\s*\n(.*?)```\s*", text, re.S)
    verdict = json.loads(fenced.group(1) if fenced else text)
    if (not isinstance(verdict, dict) or set(verdict) != {"approved", "issues"}
            or type(verdict["approved"]) is not bool
            or not isinstance(verdict["issues"], list)
            or len(verdict["issues"]) > 4
            or any(not isinstance(issue, str) or not issue.strip() or len(issue) > 500
                   for issue in verdict["issues"])):
        raise ValueError("checker review must return approved boolean and up to four "
                         "nonempty issues of at most 500 characters")
    issues = verdict["issues"]
    if verdict["approved"] and not issues:
        return test
    if not verdict["approved"] and issues:
        raise ValueError("checker review rejected: " + "; ".join(issues)[:1500])
    raise ValueError("checker review approval and issues are inconsistent")
