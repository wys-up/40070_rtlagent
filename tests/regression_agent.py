"""Sequential development evaluation; official fixtures stay outside the agent.

python3 tests/regression_agent.py --task-root /data/tasks --judge /data/selftest/judge.py \
    --out outputs/batch --tasks Prob030_popcount255 Prob086_lfsr5

Agent wall time includes its model and tools, but excludes the separate judge.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def run_logged(command, cwd, log_path, timeout):
    started = time.monotonic()
    error = None
    rc = None
    with log_path.open("w", encoding="utf-8") as log:
        try:
            proc = subprocess.Popen(command, cwd=cwd, stdout=log,
                                    stderr=subprocess.STDOUT,
                                    start_new_session=os.name == "posix")
            try:
                rc = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                error = f"timeout after {timeout}s"
                if os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
                rc = proc.wait()
        except OSError as exc:
            error = str(exc)
    return dict(rc=rc, wall_s=round(time.monotonic() - started, 3), error=error)


def summarize_trace(path):
    events, errors = [], []
    try:
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event is not an object")
                events.append(event)
            except ValueError as exc:
                errors.append(f"line {line_no}: {exc}")
    except OSError as exc:
        errors.append(str(exc))
    calls = [e for e in events if e.get("tool") == "llm" and "elapsed_s" in e]
    start = next((e for e in events if e.get("event") == "start"
                  and e.get("tool") == "agent"), {})
    return dict(accepted=any(e.get("event") == "accept" for e in events),
                rounds=max((e.get("round", 0) for e in events), default=0),
                model=start.get("llm"), errors=errors, events=len(events),
                llm_calls=len(calls),
                llm_wall_s=round(sum(e["elapsed_s"] for e in calls), 3),
                llm_purposes=dict(Counter(e.get("purpose", "rtl") for e in calls)),
                checker_invalid=sum(e.get("tool") == "selftest" and
                                    e.get("event") == "invalid" for e in events),
                simulations=[{k: e[k] for k in ("round", "rc", "excerpt") if k in e}
                             for e in events if e.get("tool") == "simulate"])


def source_hashes(root):
    paths = [p for directory in ("agent", "serve", "skill", "tests", "docs", "model")
             for p in (root / directory).rglob("*") if p.is_file()]
    paths += [root / name for name in ("run.sh", "run_baseline.sh", "baseline.py",
              "Dockerfile", "requirements.txt", "manifest.json", "REPORT.md", "README.md")]
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths) if p.is_file() and "__pycache__" not in p.parts
            and p.suffix not in (".pyc", ".pyo")}


def evaluate_task(agent_root, task, judge, out, agent_timeout, judge_timeout):
    out.mkdir()
    inputs = out / "input"
    inputs.mkdir()
    # Official task directories may contain ref.sv/tb.sv. Stage ONLY public inputs.
    for name in ("prompt.txt", "interface.txt"):
        if (task / name).is_file():
            shutil.copyfile(task / name, inputs / name)
    output = out / "agent"
    output.mkdir()
    started = time.monotonic()
    agent = run_logged(["bash", str(agent_root / "run.sh"), str(inputs), str(output)],
                       agent_root, out / "agent-runner.log", agent_timeout)
    solution = output / "solution.v"
    verdict_path = out / "verdict.json"
    if solution.is_file() and solution.stat().st_size:
        judgement = run_logged([sys.executable, str(judge), "--task", str(task),
            "--solution", str(solution), "--outdir", str(out / "judge"),
            "--json", str(verdict_path), "--timeout", str(judge_timeout)],
            judge.parent, out / "judge-runner.log", judge_timeout + 30)
        try:
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            verdict = dict(error=str(exc))
    else:
        judgement = dict(rc=None, wall_s=0, error="missing or empty solution; judge not run")
        verdict = dict(error=judgement["error"])
    return dict(task=task.name, agent=agent, judge_process=judgement, judge=verdict,
                total_wall_s=round(time.monotonic() - started, 3),
                solution_sha256=hashlib.sha256(solution.read_bytes()).hexdigest()
                    if solution.is_file() else None,
                trace=summarize_trace(output / "trace.jsonl"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--task-root", type=Path, required=True)
    parser.add_argument("--judge", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tasks", nargs="+", required=True)
    parser.add_argument("--source-revision", default="unrecorded")
    parser.add_argument("--agent-timeout", type=int, default=935)
    parser.add_argument("--judge-timeout", type=int, default=330)
    args = parser.parse_args()
    root, tasks, judge, out = (p.resolve() for p in
                              (args.agent_root, args.task_root, args.judge, args.out))
    if (len(set(args.tasks)) != len(args.tasks)
            or any(Path(n).name != n or n in (".", "..") for n in args.tasks)):
        parser.error("task names must be unique direct child directory names")
    if not (root / "run.sh").is_file() or not judge.is_file():
        parser.error("agent run.sh and judge.py must exist")
    for name in args.tasks:
        if not (tasks / name / "prompt.txt").is_file():
            parser.error(f"missing prompt.txt for {name}")
    # Refuse reused result directories: stale verdicts must never appear as new passes.
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / "metadata.json", dict(
        started_utc=datetime.now(timezone.utc).isoformat(), source_revision=args.source_revision,
        agent_root=str(root), task_root=str(tasks), judge=str(judge), tasks=args.tasks,
        judge_sha256=hashlib.sha256(judge.read_bytes()).hexdigest(),
        source_sha256=source_hashes(root),
        limits=dict(agent_s=args.agent_timeout, judge_s=args.judge_timeout),
        timing="sequential; agent includes model/tools; judge measured separately",
        environment={k: v for k, v in os.environ.items()
                     if k.startswith("AGENT_") or k in ("LLM_BACKEND", "LLM_MODEL")}))
    results = []
    for name in args.tasks:
        print("START", name, flush=True)
        result = evaluate_task(root, tasks / name, judge, out / name,
                               args.agent_timeout, args.judge_timeout)
        results.append(result)
        write_json(out / "results.json", results)
        print("RESULT", json.dumps({k: result[k] for k in
              ("task", "agent", "judge_process", "judge")}), flush=True)


if __name__ == "__main__":
    main()
