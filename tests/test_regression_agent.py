import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import regression_agent as evaluation


class RegressionHarnessTests(unittest.TestCase):
    def test_stages_only_public_inputs_and_keeps_judge_timing_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            agent = root / "agent"
            task = root / "task"
            out = root / "out"
            agent.mkdir()
            task.mkdir()
            (agent / "run.sh").touch()
            for name in ("prompt.txt", "interface.txt", "ref.sv", "tb.sv"):
                (task / name).write_text(name)
            judge = root / "judge.py"
            judge.touch()

            def run(command, cwd, log, timeout):
                if "run.sh" in command[1]:
                    output = Path(command[-1])
                    (output / "solution.v").write_text("module TopModule; endmodule")
                    (output / "trace.jsonl").write_text(json.dumps({
                        "tool": "llm", "purpose": "rtl", "elapsed_s": 2.5}) + "\n")
                    return dict(rc=8, wall_s=4, error=None)
                verdict = Path(command[command.index("--json") + 1])
                verdict.write_text(json.dumps({"level": 3}))
                return dict(rc=0, wall_s=1.25, error=None)

            with patch.object(evaluation, "run_logged", side_effect=run):
                result = evaluation.evaluate_task(agent, task, judge, out, 935, 330)

            self.assertEqual(sorted(p.name for p in (out / "input").iterdir()),
                             ["interface.txt", "prompt.txt"])
            self.assertEqual(result["agent"]["rc"], 8)
            self.assertEqual(result["judge"]["level"], 3)
            self.assertEqual(result["judge_process"]["wall_s"], 1.25)
            self.assertEqual(result["trace"]["llm_wall_s"], 2.5)

    def test_trace_summary_reports_malformed_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace.jsonl"
            trace.write_text('{"tool":"agent","event":"start"}\nnot-json\n')
            summary = evaluation.summarize_trace(trace)
            self.assertEqual(summary["events"], 1)
            self.assertEqual(len(summary["errors"]), 1)

    def test_source_hashes_ignore_python_bytecode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "agent/__pycache__").mkdir(parents=True)
            (root / "agent/main.py").write_text("source")
            (root / "agent/__pycache__/main.pyc").write_bytes(b"bytecode")
            hashes = evaluation.source_hashes(root)
            self.assertEqual(set(hashes), {"agent/main.py"})


if __name__ == "__main__":
    unittest.main()
