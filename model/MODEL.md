# MODEL.md

本文件记录封存方案实际使用的本地模型和推理配置。内容与
`manifest.json`、`REPORT.md` 保持一致。

## 1. 模型

| 项 | 内容 |
| --- | --- |
| 模型名称 | `Qwen/Qwen2.5-Coder-32B-Instruct-GGUF` |
| 来源 | https://huggingface.co/Qwen/Qwen2.5-Coder-32B-Instruct-GGUF |
| 版本号 / SHA | `6ad0cdf97c9a3cfd154faf15a973c93044ba5c7e` |
| 权重文件 | `qwen2.5-coder-32b-instruct-q4_k_m.gguf` |
| 权重校验和 | `sha256: 4d64b316b5e6319d9613e0d97935d9ebd631fc7e334da400d00085eca749d085` |
| 参数量 | 32.76B |
| 许可证 | Apache-2.0 |

该模型为可离线部署的开源权重，运行时不访问外部 API。

## 2. 量化与显存

| 项 | 内容 |
| --- | --- |
| 量化方式 | GGUF Q4_K_M |
| 量化工具与版本 | 使用发布仓库提供的 GGUF 文件，未在本方案中重新量化 |
| **实测显存占用** | `25.1 GiB`，单卡加载后稳态 |
| 测量方式 | `start_model.sh status` 的单卡增量，配合 `/sys/class/drm` VRAM 统计 |
| 测量硬件 | AMD Radeon Pro W7900D，48 GiB 验证卡 |

该配置在验证实例上低于比赛要求的单卡 32 GB 上限；目标环境仍须重新实测。

## 3. 上下文配置

| 项 | 值 |
| --- | --- |
| `max_model_len` | 24576 |
| 基线单次输出上限 | 16384 token，赛事方固定，不修改 |
| Agent 客户端输出上限 | `LLM_MAX_TOKENS=16384`，各阶段请求另有限额，见下表 |
| RoPE 外推方式 | 无 |
| 思考设置 | 关闭（`enable_thinking: false`） |

方案和 `run_baseline.sh` 使用同一推理服务、模型、量化、上下文及
`manifest.json` 中的思考设置。采样和阶段预算属于 agent 配置，不改动基线。

### 3.1 采样与阶段预算

| 项 | 当前默认配置 |
| --- | --- |
| 基线采样 | temperature 0.0，top_p 1.0；单次请求，不重试、不调用工具 |
| Agent 采样 | `LLM_TEMPERATURE=0.2`；未显式传 top_p，采用服务默认值 |
| Agent RTL / 状态图 | `AGENT_RTL_MAX_TOKENS=3072`；同时按剩余生成秒数乘 14 收紧，最低请求预算 256 token |
| 规格阶段分析 | 640 token |
| 独立参考 RTL | 2304 token |
| 通用自检生成器 | 2048 token；当前默认主路径使用独立参考 RTL 生成器 |
| 自检复核 | 512 token |
| 反例期望值复核 | 256 token |
| Agent 单次请求硬上限 | 上述阶段限额与 `LLM_MAX_TOKENS` 取较小值 |
| 基线请求超时 | 840 秒，赛事方固定 |
| Agent 客户端请求超时 | `LLM_TIMEOUT_S` 默认 600 秒，实际与各阶段剩余时间取较小值 |
| 单题 / 修复轮数 | 900 秒 / 最多 5 轮，预留至少 30 秒收尾 |
| RTL 生成阶段 | 最多 240 秒；规格分析最多 60 秒且不超过该次生成预算的 40% |
| 自检生成 / 复核 | 默认 150 / 45 秒；每轮自检共享最多 240 秒，另为仿真和 RTL 修复预留时间 |

模型原生生成 RTL，或实质生成完整状态图后由通用编译器转换为 RTL。
题面表格、转移和波形解析仅用于规格约束与独立检查，不直接生成提交候选；
模型请求失败时不以确定性脚本答案替代。

## 4. 推理栈

| 项 | 内容 |
| --- | --- |
| 后端 | llama.cpp (`/opt/llamacpp/llama-server`) |
| 版本 | `0.4.0-dev`，commit `f114f91` |
| 关键启动参数 | `-ngl 999 -c 24576 -np 1 -sm none -mg 0 --jinja --host 127.0.0.1 --port 8000` |
| 并发策略 | 单卡单实例，单并发槽位 |

`serve/vllm.sh` 是为既有提交目录保留的兼容文件名，实际执行的是
`llama-server`。默认权重路径为
`/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf`，可通过 `MODEL_FILE` 覆盖。

## 5. 微调

否。本方案未训练或修改模型权重。

## 6. 辅助模型

无。

## 7. 声明

- [x] 上述模型为可本地部署的开源权重，非商用 API
- [x] 实测显存占用（含辅助模型）不超过 32 GB，单卡运行
- [x] `run_baseline.sh` 使用与 `run.sh` 完全相同的模型、量化方式与上下文配置
- [x] 未微调，无额外权重
- [x] 本文件内容与 `manifest.json`、`REPORT.md` 一致
