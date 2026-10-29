# Build on the official base image. It carries ROCm, Vivado and a
# working inference stack for the target architecture; rebuilding that yourself
# is a large amount of work for no score.
#
# Two variants are planned, gfx1100 (validation window, W7900) and gfx1201
# (finals, R9700). Pin the one you are targeting -- prebuilt kernels and
# wheels are not interchangeable between the two generations.
#
# The official image tag is supplied by the evaluator/team after announcement:
#
#   docker build --build-arg BASE_IMAGE=<announced name> -t myteam-agent .
#
# Keeping this as a required build argument prevents a stale placeholder tag
# from being mistaken for a reproducible base image.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

# --- everything must be baked in: the sandbox has no network ----------------

WORKDIR /opt/agent

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY agent/  agent/
COPY skill/  skill/
COPY serve/  serve/
COPY model/  model/
COPY run.sh run_baseline.sh baseline.py manifest.json REPORT.md ./
RUN chmod +x run.sh run_baseline.sh serve/vllm.sh serve/start.sh serve/run_api.sh

# Model weights are intentionally not stored in git because the GGUF is about
# 18.5 GiB. Put the pinned file from model/MODEL.md on local disk before the
# network goes away and mount it at /models. `huggingface-cli download` at
# startup fails in the sandbox.
#
VOLUME ["/models"]

ENV LLM_BACKEND=openai \
    LLM_BASE_URL=http://localhost:8000/v1 \
    LLM_MODEL=qwen2.5-coder-32b-instruct-q4_k_m.gguf \
    MODEL_NAME=qwen2.5-coder-32b-instruct-q4_k_m.gguf \
    LLM_API_KEY=dummy \
    MODEL_FILE=/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf \
    MODEL_CONTEXT=24576 \
    MODEL_GPU_LAYERS=999 \
    MODEL_PARALLEL=1 \
    AGENT_TMP=/var/tmp/agent \
    EDA_TMP=/var/tmp/eda \
    RTL_PART=xczu3eg-sbva484-1-e

# Do NOT set PATH for the toolchain here. The base image already exports it,
# along with XILINX_VIVADO / XILINX_VITIS. The tools actually live under
# /tools/Xilinx/2026.1/, and the layout is version-first in 2026.1 but
# tool-first in 2024.2 and earlier -- which is why CONTRACT.md section 1
# says to read the variables instead of hardcoding a path.

# Container metadata only. On Radeon Cloud both services bind 127.0.0.1 and
# just 7860 is published, via rc-tunnel. See docs/API_CONTRACT.md section 7.
EXPOSE 7860 8000

# Not an entrypoint on purpose. Evaluation is by HTTP submission: the team
# runs serve/start.sh, then serve/serve_api.py. In the container-rebuild
# fallback the organisers run serve/start.sh once and then call run.sh /
# run_baseline.sh per task directly. See docs/API_CONTRACT.md section 8.
CMD ["/bin/bash"]
