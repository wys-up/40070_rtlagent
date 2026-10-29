# RTL track 设计报告

| 项目 | 内容 |
| --- | --- |
| Track | RTL |
| 版本日期 | 2026-10-26 |

## 1. 方案概述

本方案在断网环境中使用本地开源模型完成单题 RTL 生成，并把接口、语义、编译、综合
和自我验证结果纳入多轮修复。方案优先保证接口和可综合性，同时通过独立检查器争取
把 L1 提升到 L2/L3。单题默认最多五轮，方案和基线使用同一模型、同一上下文和同一
推理服务。

## 2. 总体架构

```text
prompt.txt
    |
    v
规格/接口提取 -> 技能注入 -> 本地 llama.cpp -> 代码抽取
    ^                                      |
    |                                      v
    +-- 失败摘要 <--- 接口/语义/lint/synth/self-test
                                           |
                                           v
                                  solution.v + trace.jsonl
```

`serve/start.sh` 是唯一启动常驻推理服务的位置；`run.sh` 只处理单题。工具链反馈经过
摘要后回灌模型，候选按接口、明确语义、编译、综合和自检证据排序，坏候选不会覆盖已有
的更好候选。

### 2.1 设计依据

接口错误会直接导致 L0，因而先做低成本接口检查；随后才调用 Vivado 编译、综合和
仿真。参考测试台不下发，自我验证控制器只使用 prompt/interface，生成独立 testbench
并做逐拍比较，不读取候选 RTL、官方 `ref.sv` 或官方测试台。

## 3. 模型与推理栈

### 3.1 最终模型

| 项目 | 内容 |
| --- | --- |
| 模型 | `Qwen/Qwen2.5-Coder-32B-Instruct-GGUF` |
| 来源与版本 | Hugging Face，文件页面提交 `6ad0cdf` |
| 文件 | `qwen2.5-coder-32b-instruct-q4_k_m.gguf` |
| SHA-256 | `4d64b316b5e6319d9613e0d97935d9ebd631fc7e334da400d00085eca749d085` |
| 量化 | GGUF Q4_K_M |
| 参数量 | 32.76B |
| 实测显存 | 25.1 GiB，单卡 |
| `max_model_len` | 24576 |
| 单次输出上限 | 16384 |
| RoPE | 无外推 |

### 3.2 模型选择

实际使用的是验证实例中已有的 Qwen2.5-Coder-32B-Instruct GGUF。未选择未量化 32B
权重，因为约 64 GB 的 FP16 权重不满足单卡 32 GB 约束；没有继续保留模板中的
Qwen3-8B 声明，因为它与实际验证服务不一致。

### 3.3 微调与辅助模型

未微调。无辅助模型。

### 3.4 推理栈

使用镜像已有的 `llama.cpp 0.4.0-dev`，commit `f114f91`。关键参数为
`-ngl 999 -c 24576 -np 1 -sm none -mg 0 --jinja`，服务监听 `127.0.0.1:8000`，
单卡单并发。验证实例当前服务由 `/workspace/rtlagent-config/start_model.sh` 启动；
提交目录中的 `serve/vllm.sh` 复用同一命令配置，文件名只是既有目录契约的兼容名称。

## 4. 智能体设计

### 4.1 控制流

`agent/main.py::solve_agent` 读取题面，提取模块接口与复位/时序约束，组装系统提示、
技能和上一轮错误摘要，调用本地模型，抽取 Verilog，再执行接口、语义、lint、综合和
自我验证。通过条件要求有非零比较、无失配、无 unknown expected/mask、无超时；否则
继续修复或返回证据最好的候选。

### 4.2 重试与回退

默认最多五轮。每轮按剩余墙钟预算收紧模型与工具超时；源码去重、错误指纹规范化和
从原规格重新生成用于避免在坏候选上反复局部修补。新候选证据更差时保留旧候选。

### 4.3 上下文管理

模型完整回复、候选、检查器和工具日志均落盘；回灌模型的工具日志由
`tools.py::summarize_log()` 保留错误、综合完成等关键行并限制长度，避免把数千行
Vivado 日志直接占满上下文。

### 4.4 自我验证

控制器根据题面构造 DUT 例化、比较任务、计数器和 watchdog。对可识别的 population
count、符号扩展、K-map、组合状态表、Moore 表、sticky 下降沿捕获和波形表使用程序化
检查器；其它题目由同一模型生成检查器后再复核。模型检查器错误与 DUT 功能错误分开
记录，不能仅凭编译或综合通过接受候选。

## 5. 技能包

技能包括 `rtl-module-interface-contract` 和 `rtl-sequential-timing`，另有
`skill/README.md`。前者约束模块名、端口名、方向、位宽、头部语法和测试台内容；
后者覆盖同步/异步复位、非阻塞赋值、状态更新和时序语义。根据真实失败样本增加了
非连续复位措辞路由、组合 next-state 题不注入时序模板，以及有意 latch/规定初值
的例外。

危险警告由工具链拦截，包括越界、VRFC 10-3705、Synth 8-6858/6859、多驱动和非预期
锁存；题面明确要求 latch 时继续进入功能检查。开发回归中 Prob030、133、153、155
的危险警告均被新 gate 阻断。

## 6. 实测结果

### 6.1 环境与范围

远程实例为 AMD Radeon Pro W7900D 48 GiB，Vivado 2026.1，目标器件
`xczu3eg-sbva484-1-e`，5 ns。服务器完整 unittest 为 46 项，其中包含 7 类 Vivado
oracle 集成测试；日志在 `outputs/agent-fix-20261026/unittest.log`。

以下是 7 道指定开发回归，不是 VerilogEval v2 全量统计，也不是 pass@k：

| 级别 | 方案题数 |
| --- | ---: |
| L0 | 0 |
| L1 | 1 |
| L2 | 0 |
| L3 | 6 |

指定样本题集得分为 `(6 + 0.2) / 7 = 0.8857`。该数字只描述上述 7 道回归，不能外推
为全量 145/156 题得分；基线未在同一批次完成，故不填写增益。

| 题目 | 原始等级 | 修复后 | 官方失配/样本 |
| --- | --- | --- | --- |
| Prob030_popcount255 | L1 | L3 | 0/215 |
| Prob042_vector4 | L0 | L3 | 0/100 |
| Prob066_edgecapture | L1 | L3 | 0/266 |
| Prob107_fsm1s | L1 | L3 | 0/230 |
| Prob122_kmap4 | L1 | L3 | 0/232 |
| Prob134_2014_q3c | L1 | L3 | 0/200 |
| Prob145_circuit8 | L1 | L1 | 176/240 |

### 6.2 代价

没有完成全题集墙钟汇总。已确认单题模型请求和 Vivado 工具均受剩余预算约束，服务
启动只在 `serve/start.sh` 中等待就绪；正式评测应按同一 `run.sh`/`run_baseline.sh`
口径重新记录平均和最长时间。

## 7. 复现说明

### 7.1 构建

```bash
docker build --build-arg BASE_IMAGE=<announced-official-image> -t rtlagent .
```

模型文件不进 Git。按本报告的仓库、revision、文件名和 SHA-256 在断网前准备权重，
并挂载到 `/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf`。

### 7.2 启动

```bash
export MODEL_FILE=/models/qwen2.5-coder-32b-instruct-q4_k_m.gguf
./serve/start.sh
export LLM_BACKEND=openai
export LLM_BASE_URL=http://127.0.0.1:8000/v1
export LLM_MODEL=qwen2.5-coder-32b-instruct-q4_k_m.gguf
export LLM_API_KEY=dummy
```

### 7.3 单题与基线

```bash
./run.sh <input_dir> <output_dir>
./run_baseline.sh <input_dir> <output_dir>
```

两者都访问同一个本地 OpenAI 兼容服务，使用 manifest 声明的 `enable_thinking=false`
和 24576/16384 上下文配置；基线只发一次模型请求，不调用 agent 工具。

### 7.4 自测

```bash
source /workspace/rtlagent-config/agent.env
bash selftest/run_selftest.sh --check
bash selftest/run_selftest.sh --reference
python3 -m unittest discover -s example/tests -v
```

已完成 `--check` 所需工具核查和 reference 链路核查；完整全量方案/基线评测未完成。

### 7.5 断网依赖

模型权重、llama.cpp、Python 依赖和 Vivado 必须在断网前准备好；服务只监听
`127.0.0.1`，临时目录使用 `/var/tmp`，不在启动时下载或联网。Vivado 授权变量由
环境配置提供。

## 8. 失败分析

### 8.1 仍未解决的类型

| 类型 | 卡级 | 原因 |
| --- | --- | --- |
| 透明锁存/波形表题 | L1/L2 | 模型仍会把透明 latch 误写成边沿触发或组合逻辑，checker 能发现反例但不能替模型完成正确改写 |
| 复杂时序语义题 | L1 | 同步复位、采样历史和非阻塞赋值的周期边界容易被模型错解 |
| 题面信息复杂或非标准格式 | L0/L1 | 受限规格解析器无法构造可靠 checker 时只能退回编译/综合证据 |

### 8.2 无效尝试

| 做法 | 结果 | 原因 |
| --- | --- | --- |
| 只增加重试轮数 | 墙钟增加，功能错误基本不变 | 没有新的功能 oracle，模型会重复错误解释 |
| 综合通过即接受 | 会误收 Prob030 等功能错误 | L3 只说明可综合，不能证明逐拍行为正确 |
| 让 checker 读取候选 RTL 或官方参考 | 被禁止且会产生泄漏 | 正式自检只从 prompt/interface 构造 |

### 8.3 边界

程序化 checker 只覆盖声明的规格格式，同一模型生成的通用 checker 不是形式化证明。
当前结果也未覆盖全部失败样本、92 个旧 L3 样本的重新生成或每题五次采样。模型更换、
上下文缩短、单卡显存超过 32 GB 或目标环境缺少离线依赖时，运行结果不可直接复现。

## 9. 大模型协作记录

开发阶段使用 Codex（GPT-5 系列，Codex desktop）和远程终端工具协助阅读仓库、定位
失败、修改 agent/skill、核对模型服务配置和执行测试。运行时方案只使用本地
Qwen2.5-Coder-32B-Instruct GGUF，不使用商用 API 或外部检索。

来自模型的有效建议包括：按接口/编译/综合/自检证据排序候选；把模型检查器错误与
DUT 错误分开；对综合日志做关键行摘要；用单卡、固定上下文和基线共用服务；把
`start.sh` 作为唯一常驻进程入口。队伍自己的判断是保留现有 controller、技能边界、
题面独立性和官方 judge 不变，并拒绝把未完成的全量结果写成成绩。

错误或不适用的建议也被实测纠正：模板中的 Qwen3-8B/vLLM/32768 配置与服务器实际
llama.cpp、GGUF 文件和 24576 上下文不一致；初期“综合通过即可接受”会放过
Prob030 功能错误；Prob145 的波形 checker 能拒绝错误候选但不能自动生成正确 latch。
这些问题通过 `curl /v1/models`、模型 SHA-256、`start_model.sh status`、Vivado
2026.1 真实仿真/综合、46 项 unittest 和 7 道官方 judge 回归发现。

## 10. 版本与复核边界

远程实例当前提交包含本报告对应的代码与声明；模型文件不提交到 Git。Gitee 推送曾因
TLS/SSH 公钥问题失败，最终封存前仍需在可访问的 Gitee 或 GitHub 私有仓库完成推送
并邀请赛事方协作者。
