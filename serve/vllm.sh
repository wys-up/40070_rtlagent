#!/usr/bin/env bash
# Start the local offline inference service.
#
# The verified runtime uses the image-provided llama.cpp server. The filename
# is kept for compatibility with serve/start.sh and the submission layout.
# Do not start this script from run.sh: serve/start.sh is the only owner of
# resident processes.

set -euo pipefail

MODEL_SERVER="${MODEL_SERVER:-/opt/llamacpp/llama-server}"
MODEL_FILE="${MODEL_FILE:-${MODEL:-}}"
if [ -z "$MODEL_FILE" ]; then
    MODEL_FILE=/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf
    if [ ! -f "$MODEL_FILE" ] && [ -f /workspace/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf ]; then
        MODEL_FILE=/workspace/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf
    fi
fi
MODEL_HOST="${MODEL_HOST:-127.0.0.1}"
MODEL_PORT="${MODEL_PORT:-8000}"
MODEL_CONTEXT="${MODEL_CONTEXT:-24576}"
MODEL_GPU_LAYERS="${MODEL_GPU_LAYERS:-999}"
MODEL_PARALLEL="${MODEL_PARALLEL:-1}"
MODEL_ALIAS="${MODEL_ALIAS:-$(basename "$MODEL_FILE")}"

if [ ! -x "$MODEL_SERVER" ]; then
    echo "serve/vllm.sh: llama-server not executable: $MODEL_SERVER" >&2
    exit 1
fi
if [ ! -f "$MODEL_FILE" ]; then
    echo "serve/vllm.sh: model file not found: $MODEL_FILE" >&2
    echo "serve/vllm.sh: set MODEL_FILE to an existing local GGUF file" >&2
    exit 1
fi

export ROCBLAS_TENSILE_LIBPATH="${ROCBLAS_TENSILE_LIBPATH:-/opt/llamacpp/rocblas/library}"
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}"

args=(
    -m "$MODEL_FILE"
    --host "$MODEL_HOST"
    --port "$MODEL_PORT"
    -ngl "$MODEL_GPU_LAYERS"
    -c "$MODEL_CONTEXT"
    -np "$MODEL_PARALLEL"
    -sm none
    -mg 0
    --jinja
    --alias "$MODEL_ALIAS"
)

if [ -n "${MODEL_API_KEY:-}" ]; then
    args+=(--api-key "$MODEL_API_KEY")
fi
if [ -n "${MODEL_EXTRA_ARGS:-}" ]; then
    # Allow image/runtime-specific flags without changing the verified default.
    # shellcheck disable=SC2206
    args+=($MODEL_EXTRA_ARGS)
fi

exec "$MODEL_SERVER" "${args[@]}"
