"""Submission endpoint.

A thin HTTP shell around run.sh / run_baseline.sh. The agent itself does not
change -- it still reads a directory and writes a file.

The protocol is asynchronous. POST /v1/solve queues the task and returns a
job id at once; the organisers then poll GET /v1/jobs/<id> until it reports
"done". A synchronous request that stayed open for the whole task would not
survive the platform: its front door cuts any response that takes longer than
60 seconds (HTTP 504), while the service carries on solving. Every request
here returns in well under a second.

One worker solves one task at a time, in arrival order.

    export FPGACHINA_TOKEN=<token issued by the organisers>
    export MODEL_NAME=qwen2.5-coder-32b-instruct-q4_k_m.gguf
    uvicorn serve.serve_api:app --host 127.0.0.1 --port 7860

Bind 127.0.0.1, not 0.0.0.0. Radeon Cloud's rc-tunnel only picks up HTTP
services bound to the loopback interface; a service on 0.0.0.0 is never
exposed, and from the organisers' side that is indistinguishable from a
crash. Expose it with `rc-tunnel expose --port 7860`; only one port per
notebook can be exposed, so leave the model service's 8000 internal.

The public app URL carries no platform-level auth, so the token check below is
not optional -- without it anyone who learns the URL can submit tasks.

See docs/API_CONTRACT.md for the full contract.
"""

from __future__ import annotations

import ctypes
import csv
import io
import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

app = FastAPI(title="rtlagent2026 submission endpoint")

TOKEN = os.environ.get("FPGACHINA_TOKEN", "")
MODEL_NAME = os.environ.get("MODEL_NAME", os.environ.get("LLM_MODEL", "unknown"))
TRACK = "rtl"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _become_subreaper() -> bool:
    """Adopt our own orphans instead of leaving them to PID 1.

    On Radeon Cloud, PID 1 in the instance is JupyterLab, and it never reaps
    the orphans it inherits. Every process of a killed task whose parent died
    first ends up there as a zombie: it holds no CPU or memory, but it keeps
    its PID forever, and enough of them exhaust the container's PID limit --
    after which no new process can start and every later task fails.

    As a child subreaper (prctl PR_SET_CHILD_SUBREAPER), this process becomes
    the new parent of any orphaned descendant, and _run_task reaps them.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(36, 1, 0, 0, 0) == 0     # 36 = PR_SET_CHILD_SUBREAPER
    except (OSError, AttributeError, TypeError):
        return False


SUBREAPER = _become_subreaper()


class SolveRequest(BaseModel):
    task_id: str = Field(min_length=1)
    nonce: str = Field(min_length=1)
    mode: str = Field(default="agent", pattern=r"^(agent|baseline)$")
    prompt: str = Field(min_length=1)
    interface: str = ""
    deadline_s: int = Field(default=900, ge=1, le=3600)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, _exc: RequestValidationError):
    # Keep malformed external requests in the API's 4xx class instead of
    # exposing framework-specific validation details.
    return JSONResponse(status_code=400, content={"detail": "invalid request"})


def _check_token(authorization: str | None) -> None:
    if not TOKEN:
        raise HTTPException(status_code=500, detail="FPGACHINA_TOKEN not set")
    if authorization is None or not _constant_time_equal(
        authorization, f"Bearer {TOKEN}"
    ):
        raise HTTPException(status_code=401, detail="bad token")


def _constant_time_equal(left: str, right: str) -> bool:
    """Compare bearer values without making token length observable."""
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@app.get("/v1/health")
def health(authorization: str | None = Header(default=None)):
    _check_token(authorization)
    model_ready = _WORKER.is_alive() and all(
        os.path.isfile(os.path.join(ROOT, name))
        for name in ("run.sh", "run_baseline.sh", "baseline.py")
    ) and _model_ready()
    return {
        "ready": model_ready,
        "track": TRACK,          # mismatched track => the organisers refuse to submit
        "model": MODEL_NAME,
        "vram_gb": _vram_gb(),
    }


@app.post("/v1/solve", status_code=202)
def solve(req: SolveRequest, authorization: str | None = Header(default=None)):
    _check_token(authorization)
    with _LOCK:
        # The same nonce twice is a client retry, not a new task.
        if req.nonce in _BY_NONCE:
            return _public(_JOBS[_BY_NONCE[req.nonce]])
        job = {"job_id": uuid.uuid4().hex, "task_id": req.task_id, "status": "queued",
               "req": req, "submitted": time.monotonic()}
        _JOBS[job["job_id"]] = job
        if req.nonce:
            _BY_NONCE[req.nonce] = job["job_id"]
    _QUEUE.put(job["job_id"])
    return _public(job)


@app.get("/v1/jobs/{job_id}")
def job_status(job_id: str, authorization: str | None = Header(default=None)):
    _check_token(authorization)
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job_id")
        _expire_queued(job)
        return _public(job)


_JOBS: dict[str, dict] = {}
_BY_NONCE: dict[str, str] = {}
_LOCK = threading.Lock()
_QUEUE: queue.Queue[str] = queue.Queue()


def _public(job: dict) -> dict:
    out = {k: job[k] for k in ("job_id", "task_id", "status")}
    if job["status"] == "done":
        out.update(solution=job["solution"],   # empty string means "could not solve it"
                   trace=job["trace"], elapsed_s=job["elapsed_s"])
    return out


def _expire_queued(job: dict) -> None:
    if job["status"] == "queued" and _remaining(job["req"], job["submitted"]) <= 20:
        job.update(
            status="done", solution="",
            trace=_service_trace("deadline", "insufficient budget while queued",
                                 job["submitted"]),
            elapsed_s=round(time.monotonic() - job["submitted"], 1),
        )


def _worker() -> None:
    while True:
        job_id = _QUEUE.get()
        solution = ""
        trace = ""
        try:
            with _LOCK:
                job = _JOBS[job_id]
                _expire_queued(job)
                if job["status"] == "done":
                    solution = job["solution"]
                    trace = job["trace"]
                else:
                    job["status"] = "running"
            if job["status"] != "done":
                solution, trace = _solve(job["req"], job["submitted"])
        except Exception as exc:               # never leave a job stuck in "running"
            trace = _service_trace(
                "exception",
                f"{type(exc).__name__}: {exc}",
                job["submitted"],
            )
        finally:
            with _LOCK:
                job.update(
                    status="done",
                    solution=solution,
                    trace=trace or _service_trace(
                        "empty_trace",
                        "solver returned no trace",
                        job["submitted"],
                    ),
                    elapsed_s=round(time.monotonic() - job["submitted"], 1),
                )
            _QUEUE.task_done()


def _solve(req: SolveRequest, submitted: float) -> tuple[str, str]:
    script = "run.sh" if req.mode == "agent" else "run_baseline.sh"
    # The organisers' clock starts at submission, so time spent queued counts.
    left = _remaining(req, submitted)
    if left <= 20:
        return "", _service_trace("deadline", "job expired before execution", submitted)

    scratch = _scratch_dir()
    with tempfile.TemporaryDirectory(dir=scratch) as work:
        in_dir = os.path.join(work, "in")
        out_dir = os.path.join(work, "out")
        os.makedirs(in_dir)
        os.makedirs(out_dir)

        with open(os.path.join(in_dir, "prompt.txt"), "w", encoding="utf-8") as fh:
            fh.write(req.prompt)
        with open(os.path.join(in_dir, "interface.txt"), "w", encoding="utf-8") as fh:
            fh.write(req.interface)

        left = _remaining(req, submitted)
        if left <= 20:
            return "", _service_trace("deadline", "budget spent preparing input", submitted)
        env = dict(os.environ, AGENT_DEADLINE_S=str(left),
                   AGENT_DEADLINE_MONOTONIC=str(submitted + req.deadline_s))
        failure_trace = ""
        try:
            rc = _run_task(
                [os.path.join(ROOT, script), in_dir, out_dir],
                env=env,
                timeout_s=left - 20,
            )
        except Exception as exc:
            rc = 1
            failure_trace = _service_trace(
                "runner_error", f"{type(exc).__name__}: {exc}", submitted)

        solution = _read(os.path.join(out_dir, "solution.v"))
        trace = _read(os.path.join(out_dir, "trace.jsonl"))
        if trace and not trace.endswith("\n"):
            # A force kill can interrupt the last JSONL write.
            trace = trace.rsplit("\n", 1)[0] + "\n" if "\n" in trace else ""
        if failure_trace:
            trace += failure_trace
        elif rc != 0 or not trace:
            trace += _service_trace("runner_exit", f"runner exit code: {rc}", submitted)
        return solution, trace


def _remaining(req: SolveRequest, submitted: float) -> float:
    return req.deadline_s - (time.monotonic() - submitted)


def _scratch_dir() -> str:
    path = os.environ.get("AGENT_TMP") or os.environ.get("EDA_TMP") or "/var/tmp/agent"
    os.makedirs(path, exist_ok=True)
    return path


def _service_trace(event: str, detail: str, submitted: float) -> str:
    return json.dumps(
        {
            "ts": round(time.time(), 3),
            "tool": "service",
            "event": event,
            "rc": 1,
            "excerpt": detail[:500],
            "elapsed_s": round(time.monotonic() - submitted, 3),
        },
        ensure_ascii=False,
    ) + "\n"


def _model_ready() -> bool:
    """Return whether the configured model endpoint is reachable.

    Mock mode is intentionally considered ready for local contract tests. For
    an OpenAI-compatible backend, a short /models request catches the common
    failure mode where the HTTP shell is alive but inference is not.
    """
    backend = os.environ.get("LLM_BACKEND", "").strip().lower()
    if backend == "mock":
        return True
    if backend != "openai":
        return False

    base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    if not base_url:
        return False
    try:
        import requests

        headers = {}
        api_key = os.environ.get("LLM_API_KEY", "")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = requests.get(
            f"{base_url}/models",
            headers=headers,
            timeout=min(float(os.environ.get("HEALTH_TIMEOUT_S", "3")), 5.0),
        )
        if response.status_code != 200:
            return False
        body = response.json()
        models = body.get("data", [])
        if not isinstance(models, list) or not models:
            return False
        configured = os.environ.get("LLM_MODEL") or MODEL_NAME
        return any(
            isinstance(item, dict) and item.get("id") == configured
            for item in models
        )
    except (OSError, ValueError, TypeError, requests.RequestException):
        return False


_WORKER = threading.Thread(target=_worker, daemon=True)
_WORKER.start()


def _run_task(cmd: list[str], env: dict, timeout_s: float, grace_s: float = 5) -> int:
    """Run one task in its own process group and leave nothing behind.

    run.sh starts the agent, which starts Vivado / xsim. Killing run.sh alone
    (what subprocess.run(timeout=...) does) orphans all of those; they keep the
    GPU and the CPU busy while the next task runs. So run.sh gets a process
    group of its own, and the whole group is signalled: SIGTERM at the
    deadline, SIGKILL after grace_s. The group is swept once more after a
    normal exit, for anything the agent left running in the background.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _signal_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                pass
    finally:
        _signal_group(proc.pid, signal.SIGKILL)
        proc.wait()
        _reap_group(proc.pid)
    return 124 if timed_out else proc.returncode


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _reap_group(pgid: int, timeout_s: float = 3) -> None:
    """Reap the orphans of one task that were handed to us as subreaper.

    Only children in the task's process group are touched, so the exit status
    of any other subprocess (rocm-smi in /v1/health, say) is never stolen.
    SIGKILL is asynchronous, hence the short polling loop.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        left = [pid for pid, pg in _children() if pg == pgid]
        for pid in left:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if not left or time.monotonic() > deadline:
            return
        time.sleep(0.1)


def _children() -> list[tuple[int, int]]:
    """(pid, pgid) of every direct child of this process, zombies included."""
    me, out = os.getpid(), []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as fh:
                stat = fh.read()
        except OSError:
            continue
        # The command name is in parentheses and may contain spaces.
        fields = stat[stat.rindex(")") + 2:].split()
        if int(fields[1]) == me:                   # state ppid pgrp ...
            out.append((int(entry), int(fields[2])))
    return out


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _vram_gb() -> float:
    """Best-effort VRAM reading. Reported in health, not used for scoring."""
    try:
        command = os.environ.get("ROCM_SMI", "rocm-smi")
        if not os.path.isabs(command) and not shutil.which(command):
            fallback = "/opt/python/bin/rocm-smi"
            if os.path.isfile(fallback):
                command = fallback
        out = subprocess.run(
            [command, "--showmeminfo", "vram", "--csv"],
            capture_output=True, text=True,
            timeout=min(float(os.environ.get("VRAM_TIMEOUT_S", "3")), 5.0),
        ).stdout
        used = 0
        for row in csv.DictReader(io.StringIO(out)):
            for key, value in row.items():
                if key and "used" in key.lower() and "vram" in key.lower():
                    used += int(value.strip())
        return round(used / (1024 ** 3), 1)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    return 0.0
