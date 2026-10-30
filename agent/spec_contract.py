"""Conservative, evidence-backed checks; uncertain prose stays advisory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re

from .tools import module_ports, ports_from_prompt, strip_comments


@dataclass
class Contract:
    top: str
    ports: list[tuple[str, int, str]]
    reset_kind: str | None
    reset_polarity: str | None
    reset_evidence: list[str]
    combinational_only: bool
    scope_evidence: list[str]
    allow_latches: bool

    def to_dict(self) -> dict:
        return asdict(self)

    def instructions(self) -> str:
        rules = [
            "Derive a cycle/transition table before writing RTL. Treat uncertain "
            "details as uncertain; do not invent ports or additional latency.",
            "Keep Moore outputs as current-state decodes, Mealy outputs dependent "
            "on current inputs, and registered outputs only where specified.",
            "Derive bit indices, shift direction, carries, priorities and boundary "
            "values explicitly. Check truth tables before simplifying them.",
            "Reset only the signals the specification resets. History sampling "
            "may need to continue during reset. Preserve direction/overlap history.",
            "Use continuous assignments for simple combinational wiring. Explicit "
            "constant power-up values and intentional latches are allowed when "
            "the task requires them.",
        ]
        if self.reset_kind:
            rules.append(f"Explicit reset contract: {self.reset_kind}. Evidence: "
                         + " | ".join(self.reset_evidence))
        if self.reset_polarity:
            rules.append("Explicit reset polarity: " + self.reset_polarity + ".")
        if self.combinational_only:
            rules.append("Implement ONLY combinational logic, including when an "
                         "unused clock port exists. Evidence: "
                         + " | ".join(self.scope_evidence))
        return "\n".join("- " + rule for rule in rules)


def extract_contract(prompt: str, interface: str, top: str) -> Contract:
    _, ports = module_ports(interface, top)
    if not ports:
        _, ports = ports_from_prompt(prompt)
    # Limit inference to sentences containing explicit language, not code headers.
    prose = re.sub(r"\s+", " ", prompt)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", prose) if s.strip()]
    sync = [s for s in sentences if re.search(
        r"\bsynchronous\b|\bsynchronously\b", s, re.I)]
    async_ = [s for s in sentences if re.search(
        r"\basynchronous\b|\basynchronously\b", s, re.I)]
    sync = [s for s in sync if re.search(r"reset|clear", s, re.I)]
    async_ = [s for s in async_ if re.search(r"reset|clear", s, re.I)]
    kind = ("synchronous" if sync and not async_ else
            "asynchronous" if async_ and not sync else None)
    reset_sentences = [s for s in sentences if re.search(r"reset|clear", s, re.I)]
    high = any(re.search(r"active[ -]high", s, re.I) for s in reset_sentences)
    low = any(re.search(r"active[ -]low", s, re.I) for s in reset_sentences)
    polarity = "high" if high and not low else "low" if low and not high else None
    scope = [s for s in sentences if re.search(
        r"(?:implement|write|design).{0,100}(?:only the combinational|"
        r"combinational (?:logic|portion|part))|"
        r"(?:only|just).{0,30}combinational|"
        r"(?:purely|entirely) combinational|"
        r"implement.{0,40}output logic and next.?state logic", s, re.I)]
    return Contract(top, ports, kind, polarity, (sync + async_)[:4], bool(scope), scope[:3],
                    bool(re.search(r"\blatch(?:es)?\b|transparent|waveforms?", prompt, re.I)))


def check_semantics(source: str, contract: Contract) -> tuple[int, str]:
    """Only reject syntactically visible violations of explicit contracts."""
    source = strip_comments(source)
    events = re.findall(
        r"\b(?:always|always_ff)\s*@\s*(?:\(([^)]*)\)|(\w+))", source)
    problems = []
    timing_inputs = {name for direction, _, name in contract.ports
                     if direction == "input" and re.search(
                         r"(?:^|_)(?:clk|clock|a?reset|rst|clear)(?:_?n|_|$)",
                         name, re.I)}
    for parenthesized, bare in events:
        event = parenthesized or bare
        if (not timing_inputs
                and re.search(r"\b(?:posedge|negedge)\b", event)):
            problems.append(
                f"timed process @({event}) has no declared clock or reset input; "
                "do not invent edge-triggered state")
        if contract.combinational_only and re.search(r"\b(?:posedge|negedge)\b", event):
            problems.append(
                f"combinational-only scope contains clocked process @({event}). "
                "Present state is already supplied as an INPUT (y/state). Use "
                "assign or always @* for next-state/output equations; no internal "
                "state storage. Leave the declared clock port unused.")
        for edge, signal in re.findall(r"\b(posedge|negedge)\s+(\w+)", event):
            # Name alone never establishes polarity or the reset value.
            if re.fullmatch(r"(?:a?reset\w*|rst\w*|clear\w*)", signal, re.I):
                if contract.reset_kind == "synchronous":
                    problems.append(f"synchronous reset must not appear as {edge} {signal} "
                                    "in an event list")
                if contract.reset_kind == "asynchronous" and contract.reset_polarity:
                    want = "posedge" if contract.reset_polarity == "high" else "negedge"
                    if edge != want:
                        problems.append(f"active-{contract.reset_polarity} asynchronous "
                                        f"reset {signal} requires {want}, not {edge}")
    return (1, "\n".join(problems)) if problems else (0, "explicit semantic checks passed")


def repair_synchronous_events(source: str, contract: Contract) -> tuple[str, list[str]]:
    """Remove only a reset event contradicted by an explicit synchronous contract."""
    if contract.reset_kind != "synchronous":
        return source, []
    changes = []
    input_names = {n for d, _, n in contract.ports if d == "input"}

    def fix(match):
        terms = re.split(r"\s*(?:\bor\b|,)\s*", match.group(1))
        parsed = [re.fullmatch(r"\s*(posedge|negedge)\s+(\w+)\s*", t) for t in terms]
        if not all(parsed):
            return match.group()
        reset_terms = [i for i, term in enumerate(parsed)
                       if term.group(2) in input_names and re.fullmatch(
                           r"(?:a?reset\w*|rst\w*|clear\w*)", term.group(2), re.I)]
        kept = [t for i, t in enumerate(terms) if i not in reset_terms]
        if not reset_terms or not kept:
            return match.group()
        changes.append("explicit synchronous reset: @(" + match.group(1)
                       + ") -> @(" + " or ".join(kept) + ")")
        return "@(" + " or ".join(kept) + ")"

    # Comments retain their original spelling and are not candidate event lists.
    chunks = re.split(r"(/\*.*?\*/|//[^\n]*)", source, flags=re.S)
    for index in range(0, len(chunks), 2):
        chunks[index] = re.sub(r"@\s*\(([^)]*)\)", fix, chunks[index])
    return "".join(chunks), changes
