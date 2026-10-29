#!/usr/bin/env bash
# serve/run_api.sh -- keep the submission API (serve_api.py) up.
#
#   ./serve/run_api.sh start     # start in the background, detached from this terminal
#   ./serve/run_api.sh stop      # stop the watchdog and the API
#   ./serve/run_api.sh status
#   ./serve/run_api.sh check     # require auth, ready=true, track=rtl
#   ./serve/run_api.sh expose    # check first, then use the platform tunnel
#   ./serve/run_api.sh tunnel-status
#
# Why it exists: Radeon Cloud's rc-tunnel probes 127.0.0.1:7860 every 10 s and
# revokes the tunnel after about a minute of failed probes. The public URL is
# then gone for good, and a restarted service does not get it back. So the API
# must never be down for long: this script restarts it within a couple of
# seconds whenever it exits, and runs under setsid so closing the terminal or
# notebook that started it does not take it down.
#
# Start serve/start.sh (the inference service) first, then this, then
# rc-tunnel expose. FPGACHINA_TOKEN and MODEL_NAME must be set in the
# environment, e.g. by `source /workspace/env.sh`.
#
# Do not use `set -u` here, see docs/API_CONTRACT.md section 10.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${API_PORT:-7860}"
STATE="${AGENT_TMP:-/tmp}/run_api"
PIDFILE="$STATE/run_api.pid"
LOG="$STATE/serve_api.log"
START_TIMEOUT_S="${API_START_TIMEOUT_S:-30}"
TUNNEL="${RC_TUNNEL:-$HOME/.local/bin/rc-tunnel}"
export NO_PROXY="127.0.0.1,localhost,::1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="127.0.0.1,localhost,::1${no_proxy:+,$no_proxy}"
mkdir -p "$STATE"

running() {
    local pid stat
    [ -f "$PIDFILE" ] || return 1
    pid="$(cat "$PIDFILE")"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    stat="$(ps -o stat= -p "$pid" 2>/dev/null)"
    [ -n "$stat" ] && [[ ! "$stat" =~ ^[[:space:]]*Z ]]
}

api_up() {
    local body
    body="$(curl -sf --max-time 10 --noproxy '*' \
        -H "Authorization: Bearer $FPGACHINA_TOKEN" \
        "http://127.0.0.1:$PORT/v1/health")" || return 1
    printf '%s' "$body" | python3 -c '
import json, sys
try:
    body = json.load(sys.stdin)
    valid = isinstance(body, dict) and body.get("ready") is True and body.get("track") == "rtl"
except (ValueError, TypeError):
    valid = False
sys.exit(0 if valid else 1)
'
}

check_api() {
    local code
    [ -n "$FPGACHINA_TOKEN" ] || {
        echo "run_api.sh: FPGACHINA_TOKEN is not set; source /workspace/env.sh" >&2
        return 1
    }
    code="$(curl -s --max-time 10 --noproxy '*' -o /dev/null -w '%{http_code}' \
        "http://127.0.0.1:$PORT/v1/health")" || {
        echo "run_api.sh: API is unreachable on 127.0.0.1:$PORT" >&2
        return 1
    }
    [ "$code" = 401 ] || {
        echo "run_api.sh: unauthenticated health must return 401, got $code" >&2
        return 1
    }
    api_up || {
        echo "run_api.sh: authenticated health failed; check token, model readiness and track=rtl" >&2
        return 1
    }
    echo "run_api.sh: health OK (unauthenticated=401, authenticated=200, ready=true, track=rtl)"
}

platform_command() (
    # SSH sessions may omit variables provided to the notebook's PID 1.
    eval "$(python3 - <<'PY'
import os, shlex
from pathlib import Path
keys = {"FRP_BROKER_URL", "FRP_BROKER_TLS_SERVER_NAME", "RC_TUNNEL_CA_FILE"}
try:
    fields = Path("/proc/1/environ").read_bytes().split(b"\0")
except OSError:
    fields = []
for field in fields:
    key, sep, value = field.partition(b"=")
    key = key.decode(errors="replace")
    if sep and key in keys and not os.environ.get(key):
        print(f"export {key}={shlex.quote(os.fsdecode(value))}")
PY
)" || exit 1
    export TMPDIR="${TMPDIR:-/var/tmp}"
    env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        -u all_proxy -u ALL_PROXY "$@"
)

case "${1:-}" in
start)
    [ -n "$FPGACHINA_TOKEN" ] || { echo "run_api.sh: FPGACHINA_TOKEN is not set" >&2; exit 1; }
    if running && check_api; then
        echo "run_api.sh: already running (pid $(cat "$PIDFILE")), log $LOG"
        exit 0
    fi
    python3 -c 'import uvicorn, fastapi, pydantic, requests' || {
        echo "run_api.sh: missing Python dependencies; source /workspace/rtlagent-config/agent.env" >&2
        exit 1
    }
    launcher=""
    if ! running; then
        setsid bash "$HERE/run_api.sh" _loop </dev/null >>"$LOG" 2>&1 &
        launcher=$!
    fi
    deadline=$((SECONDS + START_TIMEOUT_S))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if running && check_api >/dev/null 2>&1; then
            echo "run_api.sh: started (pid $(cat "$PIDFILE")), log $LOG"
            exit 0
        fi
        if [ -n "$launcher" ] && ! kill -0 "$launcher" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    echo "run_api.sh: API not ready after ${START_TIMEOUT_S}s, see $LOG" >&2
    check_api >&2 || true
    if [ -n "$launcher" ]; then
        kill "$launcher" 2>/dev/null || true
        wait "$launcher" 2>/dev/null || true
    fi
    tail -n 20 "$LOG" >&2
    exit 1
    ;;
_loop)
    echo $$ > "$PIDFILE"
    trap 'kill "$child" 2>/dev/null; wait "$child" 2>/dev/null; rm -f "$PIDFILE"; exit 0' TERM INT
    cd "$HERE/.."
    while :; do
        echo "$(date '+%F %T') run_api.sh: starting serve_api on 127.0.0.1:$PORT"
        python3 -m uvicorn serve.serve_api:app --host 127.0.0.1 --port "$PORT" &
        child=$!
        wait "$child"
        rc=$?
        echo "$(date '+%F %T') run_api.sh: serve_api exited (rc=$rc), restarting in 2 s"
        sleep 2
    done
    ;;
stop)
    if running; then
        kill "$(cat "$PIDFILE")"
        for _ in 1 2 3 4 5 6 7 8 9 10; do running || break; sleep 1; done
        echo "run_api.sh: stopped"
    else
        echo "run_api.sh: not running"
    fi
    ;;
status)
    if running; then
        echo "run_api.sh: running (pid $(cat "$PIDFILE")), log $LOG"
        check_api || exit 1
    else echo "run_api.sh: not running"; exit 1; fi
    ;;
check)
    check_api || exit 1
    ;;
expose)
    check_api || exit 1
    if [ ! -x "$TUNNEL" ]; then
        if [ "$TUNNEL" != "$HOME/.local/bin/rc-tunnel" ] ||
                [ ! -x /var/run/secrets/frp-self-service/install ]; then
            echo "run_api.sh: platform tunnel installer unavailable" >&2
            exit 1
        fi
        platform_command /var/run/secrets/frp-self-service/install || exit 1
    fi
    [ -x "$TUNNEL" ] || { echo "run_api.sh: rc-tunnel installation failed" >&2; exit 1; }
    check_api || exit 1
    platform_command "$TUNNEL" expose --port "$PORT" || exit 1
    sleep 15
    platform_command "$TUNNEL" status
    ;;
tunnel-status)
    [ -x "$TUNNEL" ] || { echo "run_api.sh: rc-tunnel is not installed" >&2; exit 1; }
    platform_command "$TUNNEL" status
    ;;
*)
    echo "usage: $0 start|stop|status|check|expose|tunnel-status" >&2
    exit 2
    ;;
esac
