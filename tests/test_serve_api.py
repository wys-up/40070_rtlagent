import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from serve import serve_api


class ServeApiTests(unittest.TestCase):
    def setUp(self):
        self.token = serve_api.TOKEN
        serve_api.TOKEN = "test-token"
        serve_api._JOBS.clear()
        serve_api._BY_NONCE.clear()

    def tearDown(self):
        serve_api.TOKEN = self.token

    def request(self, **overrides):
        data = {
            "task_id": "task-1",
            "nonce": "nonce-1",
            "mode": "agent",
            "prompt": "implement TopModule",
            "interface": "",
            "deadline_s": 60,
        }
        data.update(overrides)
        return serve_api.SolveRequest(**data)

    def test_request_model_rejects_invalid_mode_and_missing_nonce(self):
        with self.assertRaises(ValidationError):
            self.request(mode="hls")
        with self.assertRaises(ValidationError):
            self.request(nonce="")

    def test_auth_and_health(self):
        with patch.object(serve_api, "_model_ready", return_value=True), \
                patch.object(serve_api, "_vram_gb", return_value=12.5):
            health = serve_api.health("Bearer test-token")
        self.assertEqual(health, {
            "ready": True,
            "track": "rtl",
            "model": serve_api.MODEL_NAME,
            "vram_gb": 12.5,
        })
        with self.assertRaises(HTTPException):
            serve_api.health("Bearer wrong-token")

    def test_same_nonce_returns_same_queued_job(self):
        with patch.object(serve_api._QUEUE, "put") as put:
            first = serve_api.solve(self.request(), "Bearer test-token")
            second = serve_api.solve(self.request(prompt="changed"), "Bearer test-token")
        self.assertEqual(first, second)
        self.assertEqual(put.call_count, 1)
        self.assertEqual(first["status"], "queued")

    def test_expired_job_does_not_start_runner(self):
        req = self.request(deadline_s=1)
        trace = serve_api._solve(req, time.monotonic() - 2)
        self.assertEqual(trace[0], "")
        event = json.loads(trace[1])
        self.assertEqual(event["tool"], "service")
        self.assertEqual(event["event"], "deadline")

    def test_service_trace_is_jsonl(self):
        line = serve_api._service_trace("exception", "runner failed", time.monotonic())
        event = json.loads(line)
        self.assertEqual(event["tool"], "service")
        self.assertEqual(event["excerpt"], "runner failed")

    def test_openai_health_requires_reachable_model(self):
        with patch.dict(
            "os.environ",
            {
                "LLM_BACKEND": "openai",
                "LLM_BASE_URL": "http://127.0.0.1:1/v1",
                "LLM_MODEL": "test-model",
            },
            clear=False,
        ):
            self.assertFalse(serve_api._model_ready())

    def test_health_does_not_silently_default_to_mock(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(serve_api._model_ready())

    def test_health_requires_live_worker_and_packaged_baseline(self):
        with patch.object(serve_api._WORKER, "is_alive", return_value=False):
            self.assertFalse(serve_api.health("Bearer test-token")["ready"])
        with patch.object(serve_api.os.path, "isfile", return_value=False):
            self.assertFalse(serve_api.health("Bearer test-token")["ready"])

    def test_queued_expiry_is_visible_without_waiting_for_worker(self):
        with patch.object(serve_api._QUEUE, "put"):
            result = serve_api.solve(self.request(), "Bearer test-token")
        job = serve_api._JOBS[result["job_id"]]
        job["submitted"] -= 50
        with patch.object(serve_api, "_solve") as runner:
            public = serve_api.job_status(result["job_id"], "Bearer test-token")
            repeat = serve_api.solve(self.request(), "Bearer test-token")
        self.assertEqual(public["status"], "done")
        self.assertEqual(public["solution"], "")
        self.assertEqual(public, repeat)
        runner.assert_not_called()

    def test_wall_clock_changes_do_not_change_budget(self):
        with patch.object(serve_api.time, "monotonic", return_value=125), \
                patch.object(serve_api.time, "time", return_value=99999999):
            self.assertEqual(serve_api._remaining(self.request(), 100), 35)

    def test_solver_receives_queue_adjusted_deadline_and_returns_saved_candidate(self):
        observed = {}

        def runner(cmd, env, timeout_s):
            observed.update(env=env, timeout=timeout_s)
            out_dir = Path(cmd[-1])
            (out_dir / "solution.v").write_text("module TopModule; endmodule\n")
            (out_dir / "trace.jsonl").write_text('{"tool":"llm"}\n{"partial"')
            return 124

        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {"AGENT_TMP": tmp}), \
                patch.object(serve_api, "_run_task", side_effect=runner), \
                patch.object(serve_api.time, "monotonic", return_value=125):
            solution, trace = serve_api._solve(self.request(deadline_s=900), 100)
        self.assertEqual(observed["env"]["AGENT_DEADLINE_S"], "875")
        self.assertEqual(observed["env"]["AGENT_DEADLINE_MONOTONIC"], "1000")
        self.assertEqual(observed["timeout"], 855)
        self.assertIn("endmodule", solution)
        events = [json.loads(line) for line in trace.splitlines()]
        self.assertEqual(events[-1]["event"], "runner_exit")

    def test_vram_is_measured_in_bytes_instead_of_utilization_percent(self):
        result = subprocess.CompletedProcess([], 0, stdout=(
            "device,VRAM Total Memory (B),VRAM Total Used Memory (B)\n"
            "card0,34359738368,1073741824\n"))
        with patch.object(serve_api.subprocess, "run", return_value=result):
            self.assertEqual(serve_api._vram_gb(), 1.0)


@unittest.skipUnless(os.name == "posix", "requires Linux process groups")
class TaskProcessTests(unittest.TestCase):
    def test_real_http_worker_returns_prompt_only_solution_and_deduplicates_nonce(self):
        source = """
import socket,sys,uvicorn
sock=socket.socket()
sock.bind(('127.0.0.1',0))
open(sys.argv[1],'w').write(str(sock.getsockname()[1]))
server=uvicorn.Server(uvicorn.Config('serve.serve_api:app', log_level='error'))
server.run(sockets=[sock])
"""
        with tempfile.TemporaryDirectory() as tmp:
            portfile = Path(tmp) / "port"
            env = dict(os.environ, LLM_BACKEND="mock", FPGACHINA_TOKEN="http-test",
                       AGENT_TMP=tmp, RTL_PART="test-part")
            proc = subprocess.Popen(
                [sys.executable, "-c", source, str(portfile)],
                cwd=serve_api.ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                limit = time.monotonic() + 15
                while not portfile.exists() and time.monotonic() < limit:
                    time.sleep(0.05)
                self.assertTrue(portfile.exists())
                base = f"http://127.0.0.1:{portfile.read_text()}"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                def request(path, data=None, authenticated=True):
                    headers = {"Content-Type": "application/json"}
                    if authenticated:
                        headers["Authorization"] = "Bearer http-test"
                    req = urllib.request.Request(
                        base + path, headers=headers,
                        data=json.dumps(data).encode() if data is not None else None)
                    with opener.open(req, timeout=3) as response:
                        return response.status, json.load(response)

                while time.monotonic() < limit:
                    try:
                        _, health = request("/v1/health")
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.05)
                else:
                    self.fail("HTTP service did not start")
                self.assertTrue(health["ready"])
                with self.assertRaises(urllib.error.HTTPError) as denied:
                    request("/v1/health", authenticated=False)
                self.assertEqual(denied.exception.code, 401)
                payload = {
                    "task_id": "http-task", "nonce": "http-nonce", "mode": "agent",
                    "prompt": "module named TopModule\n - input a\n - output q\n",
                    "interface": "", "deadline_s": 60,
                }
                status, first = request("/v1/solve", payload)
                self.assertEqual(status, 202)
                _, duplicate = request("/v1/solve", payload)
                self.assertEqual(first["job_id"], duplicate["job_id"])
                while time.monotonic() < limit:
                    _, job = request("/v1/jobs/" + first["job_id"])
                    if job["status"] == "done":
                        break
                    time.sleep(0.1)
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["task_id"], payload["task_id"])
                self.assertIn("module TopModule", job["solution"])
                self.assertIn("endmodule", job["solution"])
                self.assertTrue([json.loads(line) for line in job["trace"].splitlines()])
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

    def test_timeout_kills_and_reaps_grandchild_in_task_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "child.pid"
            source = (
                "import subprocess,sys,time,signal\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "p=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                " time.sleep(60)'])\n"
                "open(sys.argv[1],'w').write(str(p.pid))\n"
                "time.sleep(60)\n"
            )
            started = time.monotonic()
            rc = serve_api._run_task(
                [sys.executable, "-c", source, str(pidfile)],
                dict(os.environ), timeout_s=0.8, grace_s=0.2)
            self.assertEqual(rc, 124)
            self.assertLess(time.monotonic() - started, 5)
            if serve_api.SUBREAPER:
                self.assertFalse(Path(f"/proc/{pidfile.read_text()}").exists())


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "requires Linux Bash")
class ApiLauncherTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.launcher = Path(__file__).resolve().parents[1] / "serve" / "run_api.sh"
        self.env = dict(
            os.environ,
            PATH=f"{self.root}:{os.environ['PATH']}",
            AGENT_TMP=str(self.root),
            FPGACHINA_TOKEN="test-token",
            CHECK_BODY='{"ready":true,"track":"rtl"}',
            CHECK_HTTP="401",
            RC_TUNNEL=str(self.root / "tunnel"),
            TUNNEL_CALLS=str(self.root / "calls"),
        )
        self.executable("curl", """#!/usr/bin/env bash
case "$*" in
  *Authorization*) printf '%s' "$CHECK_BODY" ;;
  *) printf '%s' "$CHECK_HTTP" ;;
esac
""")
        self.executable("tunnel", """#!/usr/bin/env bash
[ -z "$http_proxy$https_proxy$HTTP_PROXY$HTTPS_PROXY$all_proxy$ALL_PROXY" ] || exit 42
printf '%s\\n' "$*" >> "$TUNNEL_CALLS"
""")
        self.executable("sleep", "#!/usr/bin/env bash\nexit 0\n")

    def executable(self, name, source):
        path = self.root / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)

    def run_launcher(self, command):
        return subprocess.run(
            ["bash", str(self.launcher), command],
            env=self.env, capture_output=True, text=True, timeout=15,
        )

    def test_check_requires_authentication_and_ready_rtl(self):
        self.assertEqual(self.run_launcher("check").returncode, 0)
        for body in ('{"ready":false,"track":"rtl"}',
                     '{"ready":true,"track":"hls"}', 'invalid json'):
            with self.subTest(body=body):
                self.env["CHECK_BODY"] = body
                result = self.run_launcher("check")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("test-token", result.stdout + result.stderr)

    def test_check_rejects_unprotected_service_and_missing_token(self):
        self.env["CHECK_HTTP"] = "200"
        self.assertNotEqual(self.run_launcher("check").returncode, 0)
        self.env["FPGACHINA_TOKEN"] = ""
        self.assertNotEqual(self.run_launcher("check").returncode, 0)

    def test_expose_never_calls_tunnel_when_model_is_not_ready(self):
        self.env["CHECK_BODY"] = '{"ready":false,"track":"rtl"}'
        self.assertNotEqual(self.run_launcher("expose").returncode, 0)
        self.assertFalse((self.root / "calls").exists())

    def test_expose_checks_health_and_removes_proxy_variables(self):
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                    "all_proxy", "ALL_PROXY"):
            self.env[key] = "http://127.0.0.1:1"
        result = self.run_launcher("expose")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "calls").read_text().splitlines(),
                         ["expose --port 7860", "status"])

    def test_tunnel_status_does_not_create_a_new_public_url(self):
        self.env["HTTP_PROXY"] = "http://127.0.0.1:1"
        result = self.run_launcher("tunnel-status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["status"])


if __name__ == "__main__":
    unittest.main()
