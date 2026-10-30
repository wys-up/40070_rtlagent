"""Local RTL checks without official fixtures.

Lint and synthesis reject specific dangerous diagnostics even on exit code zero.
Simulation requires a completed controller-owned verdict with positive checks.
Full logs survive scratch cleanup in the output checks directory.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time

PART = os.environ.get("RTL_PART", "xczu3eg-sbva484-1-e")


class RtlToolchain:
    def __init__(self, artifact_dir: str | None = None) -> None:
        self.xvlog = shutil.which("xvlog")
        self.xelab = shutil.which("xelab")
        self.xsim = shutil.which("xsim")
        self.vivado = shutil.which("vivado")
        self.artifact_dir = artifact_dir
        self.round = 0
        self.simulation_attempt = 0
        self.lint_attempt = 0
        self.reason = ""
        missing = [n for n, p in (("xvlog", self.xvlog), ("xelab", self.xelab),
                                  ("vivado", self.vivado)) if not p]
        if missing:
            self.reason = (f"not on PATH: {', '.join(missing)}; "
                           "source your Vivado settings64.sh")

    @property
    def available(self) -> bool:
        return not self.reason

    @property
    def simulation_available(self) -> bool:
        return bool(self.xvlog and self.xelab and self.xsim)

    def record(self, name: str, text: str) -> None:
        if self.artifact_dir:
            path = os.path.join(self.artifact_dir, f"round-{self.round:02d}")
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, name), "w", encoding="utf-8") as fh:
                fh.write(text)

    # ------------------------------------------------------------------ lint

    def lint(self, source: str, top: str, timeout_s: float = 300.0) -> tuple[int, str]:
        """Analyze and elaborate the DUT on its own.

        rc =  0  the module analyzes and elaborates
        rc =  1  the tools ran and rejected it
        rc = -1  the tools could not be run
        """
        if not self.available:
            return -1, f"vivado unavailable: {self.reason}"

        work = tempfile.mkdtemp(prefix="agent_lint_", dir=_scratch_dir())
        self.lint_attempt += 1
        attempt = self.lint_attempt
        started = time.monotonic()
        log = ""
        try:
            src = os.path.join(work, "solution.sv")
            with open(src, "w") as fh:
                fh.write(source)

            rc, out = _run([self.xvlog or "xvlog", "--sv", "solution.sv"], work, timeout_s)
            log = out
            if rc != 0:
                return 1, summarize_log(out)
            if dangerous_warnings(out):
                return 1, summarize_log(out)

            rc, out2 = _run([self.xelab or "xelab", top, "-s", "dutsim"], work,
                            timeout_s - (time.monotonic() - started))
            log += "\n" + out2
            return (0 if rc == 0 and not dangerous_warnings(log) else 1), summarize_log(log)
        finally:
            self.record("lint.log", log)
            self.record(f"lint-{attempt}.log", log)
            if os.environ.get("AGENT_KEEP_WORK") != "1":
                shutil.rmtree(work, ignore_errors=True)

    # ----------------------------------------------------------------- synth

    def synth(self, source: str, top: str, timeout_s: float = 900.0,
              allow_latches: bool = False) -> tuple[int, str]:
        if not self.available:
            return -1, f"vivado unavailable: {self.reason}"

        work = tempfile.mkdtemp(prefix="agent_synth_", dir=_scratch_dir())
        out = ""
        try:
            with open(os.path.join(work, "solution.sv"), "w") as fh:
                fh.write(source)
            with open(os.path.join(work, "synth.tcl"), "w") as fh:
                fh.write(
                    textwrap.dedent(
                        f"""\
                        read_verilog -sv solution.sv
                        synth_design -top {top} -part {PART} -mode out_of_context
                        """
                    )
                )
            rc, out = _run(
                [self.vivado or "vivado", "-mode", "batch", "-source", "synth.tcl",
                 "-nojournal", "-log", "synth.log"],
                work, timeout_s,
            )
            ok = (rc == 0 and "Synthesis finished" in out
                  and not dangerous_warnings(out, allow_latches))
            return (0 if ok else 1), summarize_log(out)
        finally:
            self.record("synth.log", out)
            if os.environ.get("AGENT_KEEP_WORK") != "1":
                shutil.rmtree(work, ignore_errors=True)

    def simulate(self, source: str, testbench: str,
                 timeout_s: float = 120.0) -> tuple[int, str]:
        """rc=2 means a broken checker/tool run; rc=1 is an observed mismatch."""
        if not self.simulation_available:
            return -1, "xvlog/xelab/xsim unavailable for functional self-test"
        work = tempfile.mkdtemp(prefix="agent_sim_", dir=_scratch_dir())
        self.simulation_attempt += 1
        attempt = self.simulation_attempt
        started = time.monotonic()
        log = ""
        try:
            for name, text in (("solution.sv", source), ("selftest_tb.sv", testbench)):
                with open(os.path.join(work, name), "w", encoding="utf-8") as fh:
                    fh.write(text)
            commands = [
                [self.xvlog, "--sv", "solution.sv", "selftest_tb.sv"],
                [self.xelab, "agent_tb", "-s", "agent_selftest",
                 "-timescale", "1ns/1ps"],
                [self.xsim, "agent_selftest", "-runall"],
            ]
            for index, command in enumerate(commands):
                rc, out = _run(command, work, timeout_s - (time.monotonic() - started))
                log += "\n" + out
                if index < 2 and (rc != 0 or dangerous_warnings(out)):
                    return 2, summarize_log(log)
                if index == 2:
                    if "AGENT_CHECKER_ERROR" in out:
                        return 2, summarize_log(log)
                    if "AGENT_MISMATCH" in out:
                        return 1, summarize_log(log)
                    passed = re.search(r"AGENT_SELFTEST_PASS checks=([1-9]\d*)", out)
                    if rc == 0 and passed:
                        return 0, summarize_log(log)
                    return 2, "self-test has no valid completed verdict\n" + summarize_log(log)
            return 2, "self-test did not execute"
        finally:
            self.record("simulation.log", log)
            self.record("selftest_tb.sv", testbench)
            self.record(f"simulation-{attempt}.log", log)
            self.record(f"selftest_tb-{attempt}.sv", testbench)
            if os.environ.get("AGENT_KEEP_WORK") != "1":
                shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------- interface checking

_MODULE_RE = re.compile(r"module\s+(\w+)\s*(?:#\s*\([^)]*\)\s*)?\((.*?)\)\s*;", re.DOTALL)


# Prose port lists commonly use this form:
#
#     I would like you to implement a module named TopModule with the following
#     interface. All input and output ports are one bit unless otherwise specified.
#
#      - input  clk
#      - input  d   (8 bits)
#      - output q   (8 bits)
#
# Widths may be omitted, followed by optional whitespace and commas. The parser
# accepts both English and Chinese width units used by repository prompts.
_PROMPT_PORT_RE = re.compile(
    r"^\s*-\s*(input|output|inout)\s+([A-Za-z_]\w*)\s*,?"
    r"\s*(?:\(\s*(\d+)\s*(?:bits?|位)\s*\))?\s*$",
    re.MULTILINE)
_PROMPT_VECTOR_PORT_RE = re.compile(
    r"^\s*-\s*(input|output|inout)\s+"
    r"\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*([A-Za-z_]\w*)\s*,?\s*$",
    re.MULTILINE)
_INLINE_INTERFACE_PORT_RE = re.compile(
    r"\b(input|output|inout)\s+"
    r"(?:\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*)?([A-Za-z_]\w*)\b",
    re.I)


def ports_from_prompt(prompt: str) -> tuple[str | None, list[tuple[str, int, str]]]:
    """从题面还原模块名与端口表。

    When the structured interface is absent, the prompt may still carry the
    module name and port list. Parsing that fallback keeps interface checking
    useful without treating arbitrary prose as authoritative.
    """
    text = prompt or ""
    ports = [(d, int(w) if w else 1, n)
             for d, n, w in _PROMPT_PORT_RE.findall(text)]
    listed = re.findall(r"^\s*-\s*(?:input|output|inout)\b.*$", text, re.M)
    if len(ports) != len(listed):
        parsed_lines = []
        for line in listed:
            scalar = _PROMPT_PORT_RE.fullmatch(line)
            vector = _PROMPT_VECTOR_PORT_RE.fullmatch(line)
            if scalar:
                d, name, width = scalar.groups()
                parsed_lines.append((d, int(width) if width else 1, name))
            elif vector:
                d, hi, lo, name = vector.groups()
                parsed_lines.append((d, abs(int(hi) - int(lo)) + 1, name))
        if len(parsed_lines) == len(listed):
            ports = parsed_lines
        else:
            # A partially parsed prose list cannot justify rejecting extra
            # ports.
            return None, []
    if not ports:
        # Only trust an ANSI declaration when the surrounding prose labels it
        # as the interface. A code block may otherwise be example data.
        if not re.search(r"(?:following|exact)\s+interface|interface\s*:",
                         text, re.I):
            return None, []
        module_match = _MODULE_RE.search(text)
        if module_match:
            return module_ports(module_match.group(0))
        inline = re.search(r"interface\s*:\s*(.*?)(?:\.\s|\n|$)",
                           text, re.I | re.S)
        if not inline:
            return None, []
        inline_ports = _INLINE_INTERFACE_PORT_RE.findall(inline.group(1))
        if not inline_ports:
            return None, []
        return (
            "TopModule",
            [(d.lower(), abs(int(hi) - int(lo)) + 1 if hi else 1, name)
             for d, hi, lo, name in inline_ports],
        )
    m = re.search(r"module\s+named\s+(\w+)", text)
    return (m.group(1) if m else "TopModule"), ports


def check_interface(source: str, interface: str,
                    prompt: str = "") -> tuple[int, str]:
    """Compare the generated module header against the declared one.

    Deterministic, no tools, no model. Returns (0, "ok") or (1, <diagnosis>).

    When `interface` is empty, the prompt port list is used as a fallback.

    Compares the module name and the ordered list of (direction, width, name).
    Whitespace, comments, and `wire`/`reg`/`logic` keywords are ignored -- those
    do not affect elaboration. Everything that does affect it is compared.
    """
    want_name, want_ports = _ports(interface)
    # A structured interface is authoritative. Prompt-derived direction and
    # width differences remain advisory because prose can be ambiguous, while
    # missing/extra ports and wrong module names remain fatal.
    advisory = False
    if want_name is None:
        want_name, want_ports = ports_from_prompt(prompt)
        advisory = want_name is not None
    got_name, got_ports = module_ports(source, want_name)

    if want_name is None:
        return 0, "接口既不在 interface 里，也没能从题面解析出端口表，本次跳过检查"
    if got_name is None:
        return 1, "no module declaration found in the generated code"

    problems = []
    hard_problems = []
    if got_name != want_name:
        hard_problems.append(f"module name is '{got_name}', must be '{want_name}'")

    want_map = {n: (d, w) for d, w, n in want_ports}
    got_map = {n: (d, w) for d, w, n in got_ports}

    for name in want_map:
        if name not in got_map:
            hard_problems.append(f"missing port '{name}'")
    for name in got_map:
        if name not in want_map:
            hard_problems.append(f"extra port '{name}' not in the declared interface")
    for name in want_map.keys() & got_map.keys():
        wd, ww = want_map[name]
        gd, gw = got_map[name]
        if wd != gd:
            problems.append(f"port '{name}' is {gd}, must be {wd}")
        if ww and gw and ww != gw:
            problems.append(f"port '{name}' is {gw} bits, must be {ww}")

    if hard_problems:
        return 1, "interface mismatch: " + "; ".join(hard_problems + problems)
    if not problems:
        return 0, ("interface matches the declaration" if not advisory
                   else "interface matches the port list read from the prompt")
    if advisory:
        # Prompt-derived direction and width differences are reported for
        # diagnostics but do not reject an otherwise compatible interface.
        return 0, ("⚠ 与题面端口清单不符（题面为散文，仅供参考，未拦截）: "
                   + "; ".join(problems))
    return 1, "interface mismatch: " + "; ".join(problems)


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def module_ports(text: str, top: str | None = None) -> tuple[str | None, list[tuple[str, int, str]]]:
    """Read simple ANSI/non-ANSI ports; unresolved parameter widths are unknown (0)."""
    stripped = strip_comments(text)
    modules = list(_MODULE_RE.finditer(stripped))
    m = next((m for m in modules if m.group(1) == top), None)
    if m is None and modules:
        m = modules[0]
    if not m:
        return None, []

    ports: list[tuple[str, int, str]] = []
    direction = None
    width = 1
    header = m.group(2)
    if not re.search(r"\b(input|output|inout)\b", header):
        end = stripped.find("endmodule", m.end())
        body = stripped[m.end():end if end >= 0 else None]
        declared = {}
        for decl in re.findall(r"\b(?:input|output|inout)\b[^;]*;", body):
            _, parsed = module_ports("module ports(" + decl.rstrip(";") + ");")
            declared.update({n: (d, w, n) for d, w, n in parsed})
        for name in header.split(","):
            name = name.strip()
            if name not in declared:
                return m.group(1), []
            ports.append(declared[name])
        return m.group(1), ports
    for decl in header.split(","):
        decl = decl.strip()
        if not decl:
            continue
        d = re.match(r"\b(input|output|inout)\b", decl)
        if d:
            direction = d.group(1)
            width = 1
        if direction is None:
            continue          # ANSI list with a leading type-only entry
        rng = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", decl)
        if rng:
            width = abs(int(rng.group(1)) - int(rng.group(2))) + 1
        elif "[" in decl:
            width = 0
        name = re.sub(r"\[[^\]]*\]", " ", decl).split()[-1]
        ports.append((direction, width, name))
    return m.group(1), ports


def source_safety_warnings(source: str,
                           ports: list[tuple[str, int, str]]) -> list[str]:
    """Catch deterministic RTL hazards before spending a Vivado invocation.

    This is intentionally narrow. It only diagnoses literal indices on ports
    whose widths are known from the contract and `genvar` used in a procedural
    loop. The compiler remains authoritative for all other syntax and semantics.
    """
    text = strip_comments(source)
    widths = {name: width for _, width, name in ports if width > 0}
    warnings: list[str] = []
    for name, width in widths.items():
        scalar = re.compile(rf"\b{re.escape(name)}\s*\[\s*(\d+)\s*\]")
        for match in scalar.finditer(text):
            index = int(match.group(1))
            if index >= width:
                warnings.append(
                    f"literal index {index} is out of bounds for port "
                    f"'{name}' with width {width} (valid indices 0..{width - 1})")
        vector = re.compile(
            rf"\b{re.escape(name)}\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]")
        for match in vector.finditer(text):
            high, low = (int(value) for value in match.groups())
            if max(high, low) >= width:
                warnings.append(
                    f"literal range [{high}:{low}] is out of bounds for port "
                    f"'{name}' with width {width} (valid indices 0..{width - 1})")
    if re.search(r"\balways(?:_ff|_comb)?\b[\s\S]{0,120}?\bbegin\b"
                 r"(?:(?!\bend\b)[\s\S]){0,800}"
                 r"\bfor\s*\(\s*genvar\b", text, re.I):
        warnings.append(
            "genvar appears in a procedural loop; use an integer loop variable "
            "inside always, or move a generate-for to module scope")
    return list(dict.fromkeys(warnings))


def signed_arithmetic_warnings(source: str) -> list[str]:
    """Catch common signed saturation encodings before tool execution."""
    text = strip_comments(source)
    warnings: list[str] = []
    if re.search(r"\b\d+\s*'[sS][dD]\s*-\s*\d+\b", text):
        warnings.append(
            "invalid signed literal spelling: put unary minus before the "
            "literal, for example -8'sd100, not 8'sd-100")
    if (re.search(r"\b(?:sum|total|result|next|new_value)\b[\s\S]{0,100}"
                  r"<\s*-\s*\d+", text, re.I)
            and re.search(r"\b\d+\s*'[dD]\s*\d+\s*\+\s*"
                          r"\d+\s*'[dD]\s*\d+", text)):
        warnings.append(
            "negative saturation branch uses unsigned sized addition; encode "
            "the limit as a signed literal such as -8'sd100 or 8'h9c")
    return list(dict.fromkeys(warnings))


def _ports(text: str) -> tuple[str | None, list[tuple[str, int, str]]]:
    return module_ports(text)


def repair_output_declarations(source: str, log: str, top: str) -> tuple[str, list[str]]:
    """Compiler-directed type repair only; never rewrite expressions or timing."""
    targets = set(re.findall(r"procedural assignment to a non-register (\w+)", log))
    targets.update(re.findall(r"redeclaration of ANSI port '(\w+)'", log))
    header = next((m for m in _MODULE_RE.finditer(source) if m.group(1) == top), None)
    if not header or not targets:
        return source, []
    text = header.group(2)
    changes = []
    # Limit this mechanical repair to numeric scalar/vector ANSI outputs.
    for target in sorted(targets):
        pattern = re.compile(r"\boutput\s+(?:(?:wire|reg|logic)\s+)?"
                             r"(?:\[\s*\d+\s*:\s*\d+\s*\]\s*)?"
                             r"[A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*(?=\s*(?:,|$))")
        group = next((m for m in pattern.finditer(text)
                      if re.search(rf"\b{re.escape(target)}\b", m.group())), None)
        if group is None:
            continue
        # A separate reg declaration is removed only if it has no initializer
        # and exactly the same width as the ANSI declaration.
        body_start = header.end()
        body_end = source.find("endmodule", body_start)
        body = source[body_start:body_end if body_end >= 0 else None]
        redecl = re.search(rf"(?m)^\s*reg\s*(\[\s*\d+\s*:\s*\d+\s*\])?\s+"
                           rf"{re.escape(target)}\s*;\s*$", body)
        if "redeclaration of ANSI port" in log and target not in set(re.findall(
                r"procedural assignment to a non-register (\w+)", log)):
            if redecl is None:
                continue
            rng = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", group.group())
            got_rng = re.search(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", redecl.group())
            if (rng.groups() if rng else None) != (got_rng.groups() if got_rng else None):
                continue
        replacement = re.sub(r"^output\s+(?:(?:wire|reg|logic)\s+)?",
                             "output reg ", group.group())
        text = text[:group.start()] + replacement + text[group.end():]
        changes.append(f"ANSI output {target}: procedural variable declaration in header")
        if redecl:
            offset = body_start + redecl.start()
            source = source[:offset] + "\n" + source[body_start + redecl.end():]
    if changes:
        source = source[:header.start(2)] + text + source[header.end(2):]
    return source, changes


# --------------------------------------------------------------------- utils


_ACTIVE_TOOLS: set[subprocess.Popen] = set()


def _stop_tool(proc: subprocess.Popen) -> None:
    if os.name == "posix":
        # Tools inherit the task group, so the evaluator's group kill reaches
        # them too. For a single tool timeout, stop only this process tree.
        descendants = []
        pending = [proc.pid]
        while pending:
            pid = pending.pop()
            try:
                with open(f"/proc/{pid}/task/{pid}/children") as fh:
                    children = [int(child) for child in fh.read().split()]
            except OSError:
                children = []
            descendants.extend(children)
            pending.extend(children)
        for pid in reversed(descendants):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc.poll() is None:
            proc.kill()
    elif proc.poll() is None:
        proc.kill()


def stop_active_tools() -> None:
    """Stop tools before returning the retained solution on cancellation."""
    for proc in list(_ACTIVE_TOOLS):
        _stop_tool(proc)
        proc.wait(timeout=5)


def _run(cmd: list[str], cwd: str, timeout_s: float) -> tuple[int, str]:
    if timeout_s <= 0:
        return 124, "[agent] verification budget exhausted"
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace",
        )
        _ACTIVE_TOOLS.add(proc)
        try:
            out, _ = proc.communicate(timeout=timeout_s)
            return proc.returncode, out
        except subprocess.TimeoutExpired:
            _stop_tool(proc)
            out, _ = proc.communicate()
            return 124, out + f"\n[agent] {cmd[0]} timed out after {timeout_s:.1f}s"
        finally:
            if proc.poll() is None:
                _stop_tool(proc)
                proc.wait(timeout=5)
            _ACTIVE_TOOLS.discard(proc)
    except OSError as exc:
        return -1, f"[agent] could not launch {cmd[0]}: {exc}"


def _scratch_dir() -> str:
    """Local disk, never network storage.

    Vivado produces large numbers of small files. On the NFS-backed /workspace of
    the cloud platform that is pathologically slow. This is not an optimisation.

    Section 9 of docs/API_CONTRACT.md names `/tmp/eda` and the base image exports it
    as `EDA_TMP`. Honour it -- running run.sh directly (rather than through
    serve_api.py, which passes `AGENT_TMP`) otherwise lands in `/tmp`, which is
    local disk too but not the directory the platform cleans up for you.

    `TRACK_TMP` is the old name of `EDA_TMP`, kept as a fallback.
    """
    path = (os.environ.get("AGENT_TMP")
            or os.environ.get("EDA_TMP")
            or os.environ.get("TRACK_TMP")
            or tempfile.gettempdir())
    os.makedirs(path, exist_ok=True)
    return path


_DANGEROUS = re.compile(
    r"VRFC 10-3705|Synth 8-(?:6858|6859)|"
    r"(?:out of (?:bounds|range)|multi[- ]?driven|multiple (?:constant )?drivers|"
    r"driven by multiple|conflicting drivers)", re.I)
_LATCH = re.compile(r"(?:inferr?ed|inferring).{0,80}\blatch|"
                    r"\blatch.{0,80}(?:inferr?ed|inferring)", re.I)


def dangerous_warnings(log: str, allow_latches: bool = False) -> list[str]:
    return [line for line in log.splitlines()
            if _DANGEROUS.search(line) or (not allow_latches and _LATCH.search(line))]


_INTERESTING = (
    "ERROR:", "CRITICAL WARNING:", "error:",
    "Synthesis finished", "Synthesis failed", "Mismatches:",
    "AGENT_", "[agent]",
)


def summarize_log(log: str, budget: int = 2000) -> str:
    """Compress a tool log to something a model can actually read.

    A Vivado synthesis log runs to thousands of lines. Feeding it back whole eats
    the context window and buries the one line that matters. Keep the lines that
    carry a verdict, drop the rest, and mark the truncation honestly.
    """
    lines = [ln.rstrip() for ln in log.splitlines()]
    # Causal errors lead the excerpt so repeated warnings cannot hide the
    # failure the model needs to repair.
    failures = [ln for ln in lines if re.search(
        r"\b(?:ERROR|FATAL):|AGENT_MISMATCH|AGENT_CHECKER_ERROR|timed? out|timeout",
        ln, re.I)]
    warnings = dangerous_warnings(log)
    critical = ([ln for ln in warnings if _DANGEROUS.search(ln)]
                + [ln for ln in warnings if not _DANGEROUS.search(ln)])
    verdicts = [ln for ln in lines if re.search(
        r"Synthesis finished|Synthesis failed|Mismatches:|AGENT_SELFTEST_PASS|AGENT_",
        ln, re.I)]
    interesting = [ln for ln in lines if any(k in ln for k in _INTERESTING)]
    keep = []
    seen = set()
    for line in failures + critical + verdicts + interesting:
        if line and line not in seen:
            keep.append(line)
            seen.add(line)
    if not keep:
        keep = lines[-40:]

    text = "\n".join(keep)
    if len(text) > budget:
        text = text[:budget] + f"\n... [truncated, {len(text) - budget} more chars]"
    return text
