"""Development-only regressions; this harness, not the agent, may use official TBs.

Run from repository root after sourcing the local model/Vivado environment:
python3 -m example.tests.regression_agent --root /workspace/rtlagent2026 --real
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from example.agent.main import infer_top
from example.agent.spec_contract import extract_contract, check_semantics
from example.agent.tools import RtlToolchain, check_interface
from example.agent.verification import SelfTest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--skip-tools", action="store_true",
                        help="reuse an earlier real-tool gate validation")
    parser.add_argument("--tasks", nargs="+", default=[
        "Prob030_popcount255", "Prob042_vector4", "Prob066_edgecapture",
        "Prob107_fsm1s", "Prob122_kmap4", "Prob134_2014_q3c", "Prob145_circuit8"])
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    root = args.root
    hashes = {}
    for path in sorted((root / "example/agent").rglob("*")):
        if path.is_file() and path.suffix in (".py", ".md"):
            hashes[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    (args.out / "source-hashes.json").write_text(json.dumps(hashes, indent=2))
    tasks = root / "tasks/veval_v2_clean/tasks"
    old = root / "outputs/realmodel-20261026-050723/agent"
    static = []
    for task in sorted(tasks.iterdir()):
        solution = old / task.name / "s0/solution.v"
        if not solution.exists():
            continue
        prompt = (task / "prompt.txt").read_text()
        source = solution.read_text()
        contract = extract_contract(prompt, "", infer_top("", prompt))
        rc, msg = check_interface(source, "", prompt)
        semantic_rc, semantic = check_semantics(source, contract)
        static.append(dict(task=task.name, interface_rc=rc, interface=msg,
                           semantics_rc=semantic_rc, semantics=semantic,
                           contract=contract.to_dict()))
    (args.out / "static.json").write_text(json.dumps(static, indent=2))
    print("STATIC", json.dumps({k: [s["task"] for s in static if s[k]]
                               for k in ("interface_rc", "semantics_rc")}), flush=True)
    rtl = RtlToolchain(str(args.out / "tool-checks"))
    tool_results = []
    for index, task in enumerate([] if args.skip_tools else [
            "Prob030_popcount255", "Prob133_2014_q3fsm",
            "Prob153_gshare", "Prob155_lemmings4"], 1):
        rtl.round = index
        source = (old / task / "s0/solution.v").read_text()
        rc, log = (rtl.lint(source, "TopModule", 90) if index == 1
                   else rtl.synth(source, "TopModule", 150))
        tool_results.append(dict(task=task, rc=rc, log=log))
        print("WARNING_GATE", task, rc, log[:400], flush=True)
    # Exercise the controller-owned comparison/verdict with the actual simulator.
    contract = extract_contract("", "module TopModule(input a, output q);", "TopModule")
    test = SelfTest("", '#1; a=1; #1; check(q,1,1,"a=1");', "both logic values")
    for label, expression in ([] if args.skip_tools else (("bad", "~a"), ("good", "a"))):
        rtl.round += 1
        rc, log = rtl.simulate(
            f"module TopModule(input a,output q);assign q={expression};endmodule",
            test.render(contract), 90)
        tool_results.append(dict(task="simulation_" + label, rc=rc, log=log))
        print("SIMULATOR", label, rc, log[-500:], flush=True)
    (args.out / "tool-results.json").write_text(json.dumps(tool_results, indent=2))
    if not args.real:
        return
    results = []
    for name in args.tasks:
        out = args.out / "real" / name
        out.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        with (out / "runner.log").open("w") as log:
            proc = subprocess.run(
                ["bash", str(root / "example/run.sh"), str(tasks / name), str(out)],
                cwd=root, stdout=log, stderr=subprocess.STDOUT, timeout=920)
        judge = subprocess.run(
            [sys.executable, str(root / "selftest/judge.py"),
             "--task", str(tasks / name), "--solution", str(out / "solution.v"),
             "--outdir", str(out / "judge"), "--json", str(out / "verdict.json"),
             "--timeout", "330"], cwd=root, capture_output=True, text=True,
            timeout=400)
        (out / "judge-runner.log").write_text(judge.stdout + judge.stderr)
        try:
            verdict = json.loads((out / "verdict.json").read_text())
        except (ValueError, OSError):
            verdict = {"error": judge.stdout[-2000:] + judge.stderr[-2000:]}
        events = [json.loads(line) for line in (out / "trace.jsonl").read_text().splitlines()]
        item = dict(task=name, agent_rc=proc.returncode, judge=verdict,
                    wall_s=round(time.monotonic() - start, 2),
                    accepted=any(e.get("event") == "accept" for e in events),
                    rounds=max([e.get("round", 0) for e in events]),
                    simulations=[e for e in events if e.get("tool") == "simulate"])
        results.append(item)
        (args.out / "real-results.json").write_text(json.dumps(results, indent=2))
        print("REAL", json.dumps(item), flush=True)


if __name__ == "__main__":
    main()
