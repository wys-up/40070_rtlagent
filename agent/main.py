"""Budgeted RTL repair with interface, semantic, synthesis and functional checks.

Self-tests are generated from the specification without candidate RTL or hidden
fixtures. Candidates and full diagnostics are retained beside trace.jsonl.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import sys
import time

from .llm import LLM, LLMError, extract_code
from .skills import load_skills, select
from .tools import (RtlToolchain, check_interface, source_safety_warnings,
                    signed_arithmetic_warnings)
from .tools import stop_active_tools, repair_output_declarations
from .spec_contract import extract_contract, check_semantics, repair_synchronous_events
from .verification import (CheckerReviewRejected, generate_selftest,
                           review_selftest)
from .oracles import from_spec

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROMPT_DIR = os.path.join(HERE, "prompts")
SKILL_DIR = os.path.join(ROOT, "skill")

MAX_ROUNDS = int(os.environ.get("AGENT_MAX_ROUNDS", "5"))
DEADLINE_S = float(os.environ.get("AGENT_DEADLINE_S", "900"))
# 为写出结果预留的余量。
RESERVE_S = float(os.environ.get("AGENT_RESERVE_S", "30"))
GENERATION_S = float(os.environ.get("AGENT_GENERATION_S", "240"))
CHECKER_S = float(os.environ.get("AGENT_CHECKER_GENERATION_S",
                               os.environ.get("AGENT_CHECKER_S", "150")))
CHECKER_REVIEW_S = float(os.environ.get("AGENT_CHECKER_REVIEW_S", "45"))
CHECKER_TOTAL_S = float(os.environ.get("AGENT_CHECKER_TOTAL_S", "240"))
CHECKER_SIM_RESERVE_S = 30
VALIDATION_RESERVE_S = float(os.environ.get("AGENT_VALIDATION_RESERVE_S", "180"))


class BudgetExpired(Exception):
    """Stop work while the retained candidate can still be returned."""


class RepeatedCheckerError(ValueError):
    """An already rejected response cannot justify another review or simulation."""


class Trace:
    """Append-only record of every model and tool call.

    评分所需，非可选项。
    """

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "w", encoding="utf-8")

    def write(self, **fields) -> None:
        fields.setdefault("ts", round(time.time(), 3))
        self._fh.write(json.dumps(fields, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass


def read_prompt_file(name: str) -> str:
    with open(os.path.join(PROMPT_DIR, name), encoding="utf-8") as fh:
        return fh.read().strip()


def read_task(input_dir: str) -> tuple[str, str]:
    def _read(fname: str) -> str:
        path = os.path.join(input_dir, fname)
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()

    return _read("prompt.txt"), _read("interface.txt")


def build_task_block(prompt: str, interface: str) -> str:
    return (
        "## 题目\n\n" + (prompt or "(empty)") +
        "\n\n## 模块接口（模块名与端口不得改动）\n\n```verilog\n"
        + (interface or "(empty)") + "\n```\n"
    )


def _with_note(note: str, log: str) -> str:
    """把接口提示并进工具日志，一起喂给下一轮。

    题面推出来的端口清单只作提示、不拦截（见 tools.check_interface），但提示
    必须同时传递到下一轮上下文，不能只记录在 trace 中。
    """
    return f"{note}\n\n{log}" if note else log


# 基线由仓库根目录中保持原样的 baseline.py 实现。


def save_solution(path: str, code: str) -> None:
    """Write the solution file atomically, so a kill mid-write cannot leave half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(code)
    os.replace(tmp, path)


def error_fingerprint(log: str) -> str:
    primary = next((line for line in log.splitlines() if line.strip()), "")
    primary = re.sub(r"\[(?:/|[A-Za-z]:\\)[^\]\n]+\]", "[source]", primary)
    return re.sub(r"\b\d+\b|0x[0-9a-f]+", "#", primary, flags=re.I)


def solve_agent(llm: LLM, trace: Trace, prompt: str, interface: str, top: str,
                save=lambda code: None, deadline_at: float | None = None) -> str:
    if deadline_at is None:
        deadline_at = time.monotonic() + max(0, DEADLINE_S - max(RESERVE_S, 30))
    artifact_dir = os.path.join(os.path.dirname(trace.path), "checks")
    rtl = RtlToolchain(artifact_dir)
    trace.write(tool="env", vivado_available=rtl.available, reason=rtl.reason or None)

    skills = load_skills(SKILL_DIR)
    trace.write(tool="skills", loaded=[s.name for s in skills])

    system = read_prompt_file("system.md")
    repair_tpl = read_prompt_file("repair.md")

    contract = extract_contract(prompt, interface, top)
    trace.write(tool="contract", **contract.to_dict())
    rtl.record("contract.json", json.dumps(contract.to_dict(), ensure_ascii=False, indent=2))
    best = ""
    best_rank = (-1, -1, -1, -1, -1)
    current = ""
    last_log = ""
    seen: dict[str, str] = {}
    failures: dict[str, int] = {}
    selftest = from_spec(prompt, contract)
    checker_attempts = 1 if selftest else 0
    checker_error = ""
    checker_response = json.dumps(selftest.__dict__) if selftest else ""
    invalid_checkers: set[str] = set()
    if selftest:
        rtl.record("selftest.json", checker_response)
        trace.write(tool="selftest", event="generated", source="spec_parser",
                    coverage=selftest.coverage, uncertainties=selftest.uncertainties)
    restart_next = False
    duplicate_counts: dict[str, int] = {}
    task_skills = select(skills, prompt)

    def remaining() -> float:
        return max(0.0, deadline_at - time.monotonic())

    def retain(code: str, rank: tuple[int, ...], rnd: int) -> None:
        nonlocal best, best_rank
        if rank > best_rank:
            best, best_rank = code, rank
            save(code)
            trace.write(tool="candidate", event="retain", round=rnd, rank=rank,
                        sha256=hashlib.sha256(code.encode()).hexdigest())

    def failed(digest: str, log: str) -> str:
        nonlocal restart_next
        fingerprint = error_fingerprint(log)
        failures[fingerprint] = failures.get(fingerprint, 0) + 1
        if failures[fingerprint] >= 2:
            restart_next = True
            log += ("\nRepeated failure: derive the original transition/truth table "
                    "again and switch RTL structure (assign, explicit bit equations, "
                    "or a complete next-state table). Do not patch the checker.")
        seen[digest] = log
        if "10-3705" in log:
            log += ("\nUse actual input indices 0..width-1 with a width-safe "
                    "accumulator, or explicitly zero-pad the vector before pairing. "
                    "Do not reference a nonexistent bit even inside a conditional.")
        return log

    def record_checker(result, elapsed) -> None:
        nonlocal checker_response
        checker_response = result.text
        rtl.record(f"selftest-response-{checker_attempts}.txt", result.text)
        trace.write(tool="llm", purpose="selftest", attempt=checker_attempts,
                    tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                    elapsed_s=round(elapsed, 2), finish_reason=result.finish_reason)
        if checker_fingerprint(checker_response) in invalid_checkers:
            raise RepeatedCheckerError(
                "repeated invalid checker; derive a different compact "
                "reference and stimulus from the original specification")

    def checker_fingerprint(response: str) -> str:
        try:
            canonical = json.dumps(json.loads(response), sort_keys=True)
        except ValueError:
            canonical = response.strip()
        return hashlib.sha256(canonical.encode()).hexdigest()

    def record_review(result, elapsed) -> None:
        suffix = "" if review_attempt == 1 else f"-retry-{review_attempt}"
        rtl.record(f"selftest-review-{checker_attempts}{suffix}.txt", result.text)
        trace.write(tool="llm", purpose="selftest_review", attempt=checker_attempts,
                    review_attempt=review_attempt,
                    tokens_in=result.tokens_in, tokens_out=result.tokens_out,
                    elapsed_s=round(elapsed, 2), finish_reason=result.finish_reason)

    for rnd in range(1, MAX_ROUNDS + 1):
        rtl.round = rnd
        if remaining() <= 0:
            trace.write(tool="budget", event="stop", round=rnd, reason="deadline")
            break
        if best and remaining() < 60:
            trace.write(tool="budget", event="stop", round=rnd,
                        reason="retain candidate; insufficient repair budget")
            break

        messages = [{"role": "system", "content": system + "\n\n"
                     + contract.instructions()}]

        # Skills can be relevant before any tool has run.  Matching the task
        # statement here lets timing/reset skills shape the first RTL draft;
        # log-only skills still activate on later repair rounds.
        chosen = list(task_skills)
        for skill in select(skills, last_log):
            if skill not in chosen:
                chosen.append(skill)
            if len(chosen) >= 3:
                break
        if contract.combinational_only:
            chosen = [s for s in chosen if s.name != "rtl-sequential-timing"]
        if chosen:
            messages.append({
                "role": "system",
                "content": "以下技能与当前任务相关。使用其检查规则；最终输出格式始终"
                           "遵循主系统提示，只输出完整 RTL 代码块，不输出技能的分析表格：\n\n"
                           + "\n\n".join(s.render() for s in chosen)
                           + "\n\n再次确认：只输出一个完整的 Verilog 代码块。",
            })
            trace.write(tool="skills", event="inject", round=rnd,
                        selected=[s.name for s in chosen])

        user = build_task_block(prompt, interface)
        if last_log:
            if restart_next:
                trace.write(tool="agent", event="restart_from_spec", round=rnd)
                user += ("\n\nPrevious structure failed repeatedly. Derive a NEW "
                         "implementation from the original specification; the old "
                         "RTL is deliberately omitted. Prefer a simple combinational "
                         "loop for a population count, continuous equations for "
                         "combinational logic, or a complete transition table for "
                         "an FSM. Do not recreate the previous structure.\n"
                         "Failure to resolve:\n" + last_log)
                restart_next = False
            else:
                user += "\n\n" + repair_tpl.replace("{{LOG}}", last_log) \
                                           .replace("{{CODE}}", current)

        messages.append({"role": "user", "content": user})

        t0 = time.monotonic()
        left = remaining()
        generation_s = min(
            GENERATION_S, max(0, left - min(VALIDATION_RESERVE_S, left * 0.35)))
        if generation_s <= 0:
            break
        trace.write(tool="llm", round=rnd, event="start",
                    timeout_s=round(generation_s, 3))
        try:
            result = llm.chat(messages, timeout_s=generation_s)
        except LLMError as exc:
            trace.write(tool="llm", round=rnd, event="error", rc=1,
                        elapsed_s=round(time.monotonic() - t0, 2),
                        excerpt=str(exc)[:500])
            if best:
                break
            continue
        trace.write(
            tool="llm", round=rnd,
            tokens_in=result.tokens_in, tokens_out=result.tokens_out,
            elapsed_s=round(time.monotonic() - t0, 2),
            finish_reason=result.finish_reason,
        )
        rtl.record("model-response.txt", result.text)

        code = extract_code(result.text)
        if not code:
            trace.write(tool="extract", round=rnd, rc=1,
                        excerpt="no code block in reply")
            last_log = "上一轮回复中没有找到代码块。只输出一个 ```verilog 代码块，不要加解释。"
            continue

        code, event_repairs = repair_synchronous_events(code, contract)
        if event_repairs:
            trace.write(tool="repair_reset_events", round=rnd, changes=event_repairs)
        current = code
        rtl.record("candidate.sv", code)
        digest = hashlib.sha256(re.sub(r"\s+", "", code).encode()).hexdigest()
        if digest in seen:
            duplicate_counts[digest] = duplicate_counts.get(digest, 0) + 1
            if duplicate_counts[digest] >= 2:
                restart_next = True
            last_log = ("Repeated identical candidate; do not repeat it. Switch "
                        "implementation structure (continuous assign for simple "
                        "logic, explicit bit equations for one-hot next-state).\n"
                        + seen[digest])
            trace.write(tool="candidate", event="duplicate", round=rnd, sha256=digest)
            continue

        # --- cheapest check first: no tools needed, and it catches the L0 class
        rc, msg = check_interface(code, interface, prompt)
        trace.write(tool="check_interface", round=rnd, rc=rc, excerpt=msg[:500])
        if rc != 0:
            last_log = failed(digest, msg)
            continue
        # Prompt-derived direction/width differences remain advisory, because
        # some tasks contain typos. Missing/extra ports and wrong tops are fatal.
        interface_note = msg if msg.startswith("⚠") else ""

        retain(code, (1, 0, 0, 0, 0), rnd)
        semantic_rc, semantic_log = check_semantics(code, contract)
        trace.write(tool="check_semantics", round=rnd, rc=semantic_rc,
                    excerpt=semantic_log)
        if semantic_rc:
            last_log = failed(digest, semantic_log)
            continue
        safety = source_safety_warnings(code, contract.ports)
        if re.search(r"\bsigned\s+8-bit\s+saturating\s+accumulator\b",
                     prompt, re.I):
            safety.extend(signed_arithmetic_warnings(code))
            safety = list(dict.fromkeys(safety))
        trace.write(tool="check_source_safety", round=rnd,
                    rc=1 if safety else 0, warnings=safety)
        if safety:
            last_log = failed(digest, "source safety preflight:\n- "
                                  + "\n- ".join(safety))
            continue
        retain(code, (1, 1, 0, 0, 0), rnd)
        if remaining() <= 0:
            trace.write(tool="budget", event="stop", round=rnd, reason="deadline")
            break

        rc, log = rtl.lint(code, top, timeout_s=min(remaining(), 120))
        trace.write(tool="lint", round=rnd, rc=rc, excerpt=log[:2000])
        if rc > 0 and remaining() > 1:
            repaired, changes = repair_output_declarations(code, log, top)
            if changes and repaired != code:
                rtl.record("candidate-before-type-repair.sv", code)
                code = current = repaired
                rtl.record("candidate.sv", code)
                trace.write(tool="repair_declarations", round=rnd, changes=changes)
                rc, log = rtl.lint(code, top, timeout_s=min(remaining(), 120))
                trace.write(tool="lint", round=rnd, event="after_type_repair",
                            rc=rc, excerpt=log[:2000])
        if rc > 0:
            last_log = failed(digest, _with_note(interface_note, log))
            continue
        if rc < 0:
            # No toolchain. We cannot verify further, so do not burn rounds
            # pretending we can.
            trace.write(tool="agent", event="stop", round=rnd,
                        reason="no verification available")
            break

        retain(code, (1, 1, 1, 0, 0), rnd)
        if remaining() <= 0:
            break

        rc, log = rtl.synth(code, top, timeout_s=min(remaining(), 180),
                            allow_latches=contract.allow_latches)
        trace.write(tool="synth", round=rnd, rc=rc, excerpt=log[:2000])
        if rc < 0:
            break
        if rc:
            last_log = failed(digest, _with_note(interface_note, log))
            continue
        retain(code, (1, 1, 1, 1, 0), rnd)
        if (os.environ.get("AGENT_SELFTEST", "1") == "0"
                or getattr(llm, "backend", "") == "mock"
                or not rtl.simulation_available):
            trace.write(tool="agent", event="stop", round=rnd,
                        reason="compile/synthesis only; functional verification unavailable")
            break

        # A broken checker is repaired independently of the RTL. The generator
        # never receives candidate code, official tests or reference solutions.
        sim_rc, sim_log = 2, checker_error
        # Keep time for simulation and a subsequent RTL repair, rather than
        # letting checker construction consume all remaining work time.
        repair_reserve = min(VALIDATION_RESERVE_S, remaining() * 0.35)
        checker_deadline = min(deadline_at - repair_reserve,
                               time.monotonic() + CHECKER_TOTAL_S)
        trace.write(tool="selftest", event="budget", round=rnd,
                    generation_s=CHECKER_S, review_s=CHECKER_REVIEW_S,
                    phase_s=round(max(0, checker_deadline - time.monotonic()), 3),
                    repair_reserve_s=round(repair_reserve, 3))
        while min(remaining(), checker_deadline - time.monotonic()) > 5:
            if selftest is None:
                if checker_attempts >= 3:
                    break
                request_budget = min(
                    remaining(), CHECKER_S,
                    checker_deadline - time.monotonic()
                    - CHECKER_REVIEW_S - CHECKER_SIM_RESERVE_S)
                if request_budget < 30:
                    checker_error = "insufficient budget to generate, review and simulate"
                    break
                checker_attempts += 1
                request_started = time.monotonic()
                previous_response, checker_response = checker_response, ""
                try:
                    trace.write(tool="llm", purpose="selftest", event="start",
                                attempt=checker_attempts,
                                timeout_s=round(request_budget, 3))
                    selftest = generate_selftest(
                        llm, prompt, interface, contract,
                        timeout_s=request_budget,
                        previous_error=checker_error,
                        previous_response=previous_response,
                        record=record_checker)
                except (LLMError, ValueError) as exc:
                    if isinstance(exc, LLMError):
                        trace.write(tool="llm", purpose="selftest", event="error",
                                    attempt=checker_attempts, rc=1,
                                    elapsed_s=round(time.monotonic() - request_started, 2),
                                    excerpt=str(exc)[:500])
                    if checker_response:
                        invalid_checkers.add(checker_fingerprint(checker_response))
                    selftest = None
                    checker_error = str(exc)
                    trace.write(tool="selftest", event="invalid",
                                attempt=checker_attempts, excerpt=checker_error)
                    if isinstance(exc, RepeatedCheckerError):
                        break
                    continue
                frozen_checker = json.dumps(selftest.__dict__, sort_keys=True)
                checker_sha256 = hashlib.sha256(frozen_checker.encode()).hexdigest()
                approved = False
                review_error = ""
                for review_attempt in range(1, 3):
                    review_budget = min(
                        remaining(), CHECKER_REVIEW_S,
                        checker_deadline - time.monotonic() - CHECKER_SIM_RESERVE_S)
                    if review_budget < 5:
                        checker_error = "checker budget exhausted before review"
                        break
                    request_started = time.monotonic()
                    trace.write(tool="llm", purpose="selftest_review", event="start",
                                attempt=checker_attempts, review_attempt=review_attempt,
                                timeout_s=round(review_budget, 3),
                                checker_sha256=checker_sha256)
                    try:
                        reviewed = review_selftest(
                            llm, prompt, interface, contract, selftest,
                            timeout_s=review_budget, record=record_review,
                            previous_error=review_error)
                        if json.dumps(reviewed.__dict__, sort_keys=True) != frozen_checker:
                            raise CheckerReviewRejected(
                                "checker review changed the frozen payload")
                    except (LLMError, ValueError) as exc:
                        if isinstance(exc, LLMError):
                            trace.write(
                                tool="llm", purpose="selftest_review", event="error",
                                attempt=checker_attempts, review_attempt=review_attempt,
                                rc=1, elapsed_s=round(
                                    time.monotonic() - request_started, 2),
                                excerpt=str(exc)[:500])
                        checker_error = review_error = str(exc)
                        trace.write(tool="selftest", event="invalid",
                                    attempt=checker_attempts, phase="review",
                                    review_attempt=review_attempt, excerpt=checker_error)
                        if isinstance(exc, CheckerReviewRejected):
                            if checker_response:
                                invalid_checkers.add(checker_fingerprint(checker_response))
                            selftest = None
                            break
                        continue
                    approved = True
                    trace.write(tool="selftest", event="review_approved",
                                attempt=checker_attempts, review_attempt=review_attempt,
                                checker_sha256=checker_sha256)
                    rtl.record("selftest.json", frozen_checker)
                    trace.write(tool="selftest", event="generated",
                                attempt=checker_attempts, coverage=selftest.coverage,
                                uncertainties=selftest.uncertainties)
                    break
                if not approved:
                    if selftest is None:
                        continue
                    # Transport/protocol failures do not justify regenerating
                    # an unchanged checker or executing it without approval.
                    selftest = None
                    break
            sim_rc, sim_log = rtl.simulate(
                code, selftest.render(contract), timeout_s=min(
                    remaining(), 120, max(0, checker_deadline - time.monotonic())))
            trace.write(tool="simulate", round=rnd, rc=sim_rc, excerpt=sim_log[:3000])
            if sim_rc != 2:
                break
            checker_error = sim_log
            if checker_response:
                invalid_checkers.add(checker_fingerprint(checker_response))
            selftest = None
        if sim_rc == 0:
            retain(code, (1, 1, 1, 1, 1), rnd)
            trace.write(tool="agent", event="accept", round=rnd,
                        verification="spec_selftest_and_synthesis",
                        limitation="generated checker does not prove official-test correctness")
            return best
        if sim_rc != 1:
            trace.write(tool="agent", event="stop", round=rnd,
                        reason="no valid independent functional verdict",
                        excerpt=(checker_error or sim_log)[:1500])
            break
        if selftest and selftest.uncertainties:
            sim_log += "\nSpecification/checker interpretation: " + selftest.uncertainties
        last_log = failed(digest, _with_note(interface_note, sim_log))

    trace.write(tool="agent", event="return_best", rank=best_rank,
                functional_verified=best_rank[-1] == 1)
    return best


def main(argv: list[str] | None = None) -> int:
    started = time.monotonic()
    deadline = min(started + DEADLINE_S,
                   float(os.environ.get("AGENT_DEADLINE_MONOTONIC", "inf")))
    deadline_at = deadline - max(RESERVE_S, 30)
    ap = argparse.ArgumentParser(description="minimal RTL agent")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--top", default=os.environ.get("RTL_TOP", ""),
                    help="top module name; inferred from interface.txt or the prompt, default TopModule")
    args = ap.parse_args(argv)
    if os.name == "posix":
        def interrupted(signum, frame):
            raise BudgetExpired(f"signal {signum}")
        signal.signal(signal.SIGTERM, interrupted)

    os.makedirs(args.output, exist_ok=True)
    sol_path = os.path.join(args.output, "solution.v")
    trace = Trace(os.path.join(args.output, "trace.jsonl"))

    code = ""
    try:
        save_solution(sol_path, "")
        if os.name == "posix":
            signal.signal(signal.SIGALRM, interrupted)
            signal.setitimer(signal.ITIMER_REAL, max(0.001, deadline_at - time.monotonic()))
        prompt, interface = read_task(args.input)
        llm = LLM()
        trace.write(tool="agent", event="start", mode="agent",
                    llm=llm.describe(), deadline_s=DEADLINE_S)

        top = args.top or infer_top(interface, prompt)
        code = solve_agent(llm, trace, prompt, interface, top,
                           save=lambda c: save_solution(sol_path, c),
                           deadline_at=deadline_at)
    except BudgetExpired as exc:
        trace.write(tool="budget", event="stop", reason=str(exc))
    except Exception as exc:  # noqa: BLE001 -- never fail loudly, see run.sh
        trace.write(tool="agent", event="error", excerpt=f"{type(exc).__name__}: {exc}"[:500])
        print(f"agent: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        if os.name == "posix":
            signal.setitimer(signal.ITIMER_REAL, 0)
        stop_active_tools()
        # An empty result must not overwrite a candidate saved during the rounds.
        if code or not os.path.exists(sol_path):
            save_solution(sol_path, code or "")
        trace.write(tool="agent", event="done", bytes=os.path.getsize(sol_path))
        trace.close()

    return 0


def infer_top(interface: str, prompt: str = "") -> str:
    """Best-effort top module name from the declared interface."""
    m = re.search(r"\bmodule\s+(\w+)", interface or "")
    if m:
        return m.group(1)
    m = re.search(r"\bmodule\s+named\s+(\w+)", prompt)
    return m.group(1) if m else "TopModule"


if __name__ == "__main__":
    raise SystemExit(main())
