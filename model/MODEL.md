# MODEL.md

本文件记录封存方案实际使用的本地模型和推理配置。内容与
`manifest.json`、`REPORT.md` 保持一致。

## 1. 模型

| 项 | 内容 |
| --- | --- |
| 模型名称 | `Qwen/Qwen2.5-Coder-32B-Instruct-GGUF` |
| 来源 | https://huggingface.co/Qwen/Qwen2.5-Coder-32B-Instruct-GGUF |
| 版本号 / SHA | `6ad0cdf`（文件页面提交） |
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
| 单次输出上限 | 16384 |
| RoPE 外推方式 | 无 |
| 思考设置 | 关闭（`enable_thinking: false`） |

方案和 `run_baseline.sh` 通过同一 manifest 声明使用这些配置。

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
