# RTL track 设计报告

| 项目 | 内容 |
| --- | --- |
| Track | RTL |
| 版本日期 | 2026-10-30 |

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
count、符号扩展、K-map、组合状态表、Moore 表、sticky 下降沿捕获、明确规格的右移
Galois LFSR、gshare 和波形表使用程序化
检查器；其它题目由同一模型生成检查器后再复核。模型检查器错误与 DUT 功能错误分开
记录，不能仅凭编译或综合通过接受候选。

控制器用独立 stimulus 块支持开头的局部变量声明，并在复核前拒绝模块作用域非法的
class helper 限定符；保留原有 checker 独立性、unknown 检查和完成判定。这类框架
健壮性修正不等于真实题目通过率提升，须按源码版本另行验证。

通用 checker 的独立复核只返回严格批准/问题清单，批准后冻结原 checker 并记录哈希；
拒绝或响应无效时进入独立重建，不执行未经批准的 checker。复核不再重写整个测试台，
输出单独上限为 512 token。借鉴 Pi、OpenCode、Hermes 的校验边界及错误反馈设计，
精确源码版本、适用范围及待验收益记录在 `docs/OPEN_SOURCE_PATTERNS.md`。

生成器恢复时将前一 checker 作为 assistant 消息，再跟随明确的 user 校验反馈；
检查必须直接放在 stimulus，coverage 只作说明。保持最多三次独立构造，拒绝生成截断，
不以执行说明字段或放宽验收条件来掩盖协议错误。

## 5. 技能包

技能包包含接口契约、通用时序，以及算术位宽、FSM 输出周期、gshare 分支预测、串行
接收器和移位/反馈寄存器等七条技能。`agent/skills.py` 根据显式 log signatures
路由，精确的领域技能优先于通用时序技能，每轮仍最多注入两条。新增技能明确列出适用
条件和禁止泛化的行为边界，避免将特定 FSM 编码、错误 stop 恢复或 LFSR 拓扑套到
不相干题目。

这些领域规则来自此前独立开发包 `rtl_api_agent` 的可追溯开发回归：奇数位宽和
LFSR 方程（`e5818e6`、`bdfe193`），FSM 输出周期与 PHT 全表初始化
（`7a310c1`、`d7ab83f`），编译修复保持行为（`d2463b4`），以及串行 stop-bit
恢复（`3af331d`）。这些是提示/技能的设计依据，不是本提交包对相同题目的成绩声明；
源仓库的题目结果不能代替本包复测。目标包中的路由契约测试覆盖五类专用技能，并
检查对应任务优先选择专用技能。

危险警告由工具链拦截，包括越界、VRFC 10-3705、Synth 8-6858/6859、多驱动和非预期
锁存；题面明确要求 latch 时继续进入功能检查。开发回归中 Prob030、133、153、155
的危险警告均被新 gate 阻断。

## 6. 实测结果

### 6.1 当前最终包的验证

最终包源码快照 `3369812` 在服务器隔离目录
`/workspace/40070_rtlagent_validation_20261030-r11` 完成 76 项 unittest，全部通过，
无跳过，耗时 149.603 秒；其中包括 9 项真实 Vivado/xsim 集成测试。Windows 同一
测试命令为 Ran 67、OK、skipped=10，因 Linux 能力和 Vivado 缺失而跳过的条目不能
替代服务器验证。

该快照先完成重点三题与扩展九题，6 道 L3、6 道 L1；随后追加 20 道不同题，13 道
L3、7 道 L1。合计 32 道不同题的顺序模型生成及独立官方 judge：19 道 L3、13 道 L1。
没有全题集或同批基线，不能把这个样本比例当成全量通过率、
pass@k 或改进增益。Prob030、066 的旧版本历史通过不能算作当前通过。

逐题失配数、源码与输出哈希、agent/judge 分开耗时、checker 超时和预算覆盖记录见
`docs/VALIDATION_20261030.md` 及 `docs/validation_20261030.json`。官方通过与 agent
自检接受分开统计：例如 Prob018、038 官方 L3，但 checker 未完成有效判定，agent
只返回了可综合的保留候选。LFSR、串行及 gshare 的新增诊断没有使本批失败题通过。

后续 `e3138e6` 在 r12 隔离目录完成 79 项 Linux 单测，全部通过且无跳过，含
10 项真实 Vivado；另外四道首次测试的题为半加器、双边沿检测、FSM2、K-map3，
2 道 L3、2 道 L1。至此跨版本去重覆盖 36 道题；这些结果不能合并为最新代码的
通过率。短复核、遮蔽检查及生成器恢复的对照与十二道新题测试按其固定源码另记。

### 6.2 历史开发回归（不并入当前成绩）

既有验证记录对应 AMD Radeon Pro W7900D 48 GiB，Vivado 2026.1，目标器件
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

### 6.3 代价与时间边界

没有完成全题集墙钟汇总。上述 32 题的 agent 耗时为 32.233–353.001 秒，不包含后续
官方 judge；题目逐个执行，不能将 agent 与 judge 合计时间称作 agent 时间。
重点批覆盖 deadline=600，扩展批为 agent 默认 900 秒、开发外层上限 635 秒；本批
均早于外层上限正常退出。这不是正式 HTTP 900 秒链路或边界验收，详情见验证文档。
正式评测仍需按相同 `run.sh`/`run_baseline.sh` 口径完成全量与基线对比。

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
python3 -m unittest discover -s tests -v
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
| 综合通过即接受 | 会误收 Prob030 等功能错误 | 综合本身不能证明逐拍正确；官方 L3 还要求功能仿真通过 |
| 让 checker 读取候选 RTL 或官方参考 | 被禁止且会产生泄漏 | 正式自检只从 prompt/interface 构造 |

### 8.3 边界

程序化 checker 只覆盖声明的规格格式，同一模型生成的通用 checker 不是形式化证明。
当前结果也未覆盖全部失败样本、92 个旧 L3 样本的重新生成或每题五次采样。模型更换、
上下文缩短、单卡显存超过 32 GB 或目标环境缺少离线依赖时，运行结果不可直接复现。

## 9. 大模型协作记录

开发阶段使用 Codex desktop 和远程终端工具协助阅读仓库、定位
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

当前最终包以第 6.1 节和验证文档中明确标识的源码快照为准；第 6.2 节与旧文档中的
历史项目结果不代替本包复测。修改按逻辑分组提交，测试摘要随文档入库，模型权重及
EDA 中间产物不提交到 Git。本 fork 的 GitHub origin 与最终赛事提交渠道应分别核验；
赛事协作者邀请、正式封存和全量 agent/baseline 对比未在本轮完成。
