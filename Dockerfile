# BASE_IMAGE must identify the announced official Vivado/ROCm image.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

WORKDIR /opt/agent

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY agent/  agent/
COPY skill/  skill/
COPY serve/  serve/
COPY model/  model/
COPY run.sh run_baseline.sh baseline.py manifest.json REPORT.md ./
RUN chmod +x run.sh run_baseline.sh serve/vllm.sh serve/start.sh serve/run_api.sh

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

EXPOSE 7860 8000

CMD ["/bin/bash"]
