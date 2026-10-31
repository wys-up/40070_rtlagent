# 部署与投题服务

推理服务监听 `127.0.0.1:8000`，投题接口监听 `127.0.0.1:7860`。
仅通过平台隧道暴露投题接口。运行环境需已有 llama.cpp、Vivado 2026.1、
模型权重、Python 依赖和有效 EDA 授权。

## 启动

在服务终端设置环境变量；`FPGACHINA_TOKEN` 由平台发放，须从仓库外的安全配置加载。

```bash
export LLM_BACKEND=openai
export LLM_BASE_URL=http://127.0.0.1:8000/v1
export LLM_MODEL=qwen2.5-coder-32b-instruct-q4_k_m.gguf
export MODEL_NAME="$LLM_MODEL"
export MODEL_FILE=/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf
export LLM_API_KEY=dummy
export AGENT_TMP=/var/tmp/agent
export EDA_TMP=/var/tmp/eda

bash serve/start.sh
bash serve/run_api.sh start
bash serve/run_api.sh check
```

`serve/vllm.sh` 实际启动 llama.cpp；兼容文件名不表示使用 vLLM。
默认配置为 context 24576、单并发槽位、单卡运行，详见
[模型声明](../model/MODEL.md)。模型端点不启用 `MODEL_API_KEY`，以兼容原版基线客户端；
对外投题接口仍必须使用 `FPGACHINA_TOKEN` 鉴权。

## 接口与隧道

```bash
curl --fail -H "Authorization: Bearer $FPGACHINA_TOKEN" \
  http://127.0.0.1:7860/v1/health
bash serve/run_api.sh tunnel-status
bash serve/run_api.sh expose
```

健康检查应返回 `ready:true`、`track:"rtl"` 和实际模型名。`check` 同时验证
未认证请求被拒绝。`expose` 依赖平台的 `rc-tunnel`；已有隧道时先检查状态，
避免产生新网址。隧道失效后需重新暴露并向赛事方更新网址。

```bash
curl --fail -X POST -H "Authorization: Bearer $FPGACHINA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"task_id":"t1","nonce":"n1","mode":"agent","prompt":"...","interface":"","deadline_s":900}' \
  http://127.0.0.1:7860/v1/solve
```

接口返回 `job_id` 后，使用相同鉴权查询 `GET /v1/jobs/<job_id>`，直到 `status` 为
`done`。单 worker 顺序处理任务；重复 nonce 复用已有任务。基线请求使用
`mode:"baseline"`，访问相同的模型服务。

## 日志与运行约束

- `serve/run_api.sh status` 查看状态，`stop` 停止投题服务；日志位于
  `${AGENT_TMP:-/tmp}/run_api/serve_api.log`。
- 常驻进程由 `start.sh` 独立启动；`run.sh` 仅处理单题。单题进程组终止不应结束模型。
- 临时 EDA 目录使用本地盘；完成后清理，避免在网络存储中保留 Vivado 中间文件。
- 900 秒包含排队和取回结果；工作预算与进程回收见
  [时限说明](https://github.com/wys-up/40070_rtlagent/blob/40590a5e903a5a4590ab2f2ea651011830bd0223/docs/DEADLINE_FALLBACK.md)。
- 接口任务表保存在内存。服务重启会丢失已有 job，守护脚本重启不能恢复在途评分任务。
- Docker 构建方式与单题入口见[设计报告](../REPORT.md)。
