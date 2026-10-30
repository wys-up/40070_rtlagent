# 最终仓库集成与服务器验证（2026-10-30）

## 范围与复核口径

- 最终仓库为 `40070_rtlagent`；此前 `rtl_api_agent`、对照项目的成绩不是本提交包的成绩。
- 在新建目录运行本包；`/workspace/rtlagent2026` 的题库及开发 judge 只读，不修改对照项目。
- `tests/regression_agent.py` 仅复制 `prompt.txt` 和可选 `interface.txt` 给 agent。参考实现与官方测试台不进入 agent 输入或模型上下文。
- agent 结束后单独启动 judge。`agent.wall_s` 包含模型、EDA、自检及修复；`judge_process.wall_s` 单列。
- 本记录以官方 L3 为“通过”。agent 的 `accept` 只表示独立自检通过；官方通过也可能来自未完成自检的保留候选。
- 各批顺序执行，不并行投题。预算以各批 metadata 为准，不冒充完整官方 HTTP 900 秒端到端测试。
- [机器可读记录](validation_20261030.json) 保存 metadata、逐文件源码哈希、原结果文件哈希、官方判定、输出哈希、调用及自检摘要。完整 RTL、JSONL、候选和工具日志保存在对应服务器目录。

## 本轮提交与证据

| 提交 | 改动 | 实测边界 |
| --- | --- | --- |
| `2c0b262` | 从明确题面提取右移 Galois LFSR 位宽、抽头、种子、边沿及复位，构造独立 recurrence checker | 不匹配题号或读取候选；不支持的规格退回通用 checker；模型仍未修好真实 LFSR |
| `3232bf5` | 顺序评测器隔离公开输入、记录源码哈希、拆分 agent/judge 时间 | 不替代完整官方投题链路 |
| `9099804` | 提示逐寄存器分析同拍控制、优先级、首个失配和 tap 编号 | 三题复测仍 L1，没有证明通过率提升 |
| `253777f` | checker 输出上限从 4096 降到 2048 token | 串行题仍两次 ReadTimeout，不能称超时已解决 |
| `926fde3` | 程序化反例附带参考旧状态与同拍输入 | 提高可诊断性，不等于 RTL 修复成功 |
| `3369812` | 修复 gshare testbench 相邻格式字符串的 Vivado 编译错误 | Vivado 变异测试通过；真实模型 gshare 仍 L1 |

技能移植来源与适用边界详见 `REPORT.md` 第 5 节。领域技能先于通用时序技能，每轮最多注入两条；不引入比赛无关的 agent 功能。

## 环境及单测

源码快照 `3369812` 经 `git archive` 分发至 `/workspace/40070_rtlagent_validation_20261030-r11`。
环境为 Vivado 2026.1、本地 Qwen2.5-Coder-32B-Instruct Q4_K_M GGUF，单并发模型服务。

| 检查 | 结果 |
| --- | --- |
| Linux：`python3 -m unittest discover -s tests -v` | 76 项全部通过，149.603 秒，无跳过；包含 9 项真实 Vivado/xsim 集成测试 |
| Windows：同一命令 | Ran 67，OK，skipped=10；缺少 Linux Bash、信号/进程组及 Vivado 的条目跳过，不能替代服务器测试 |
| Linux 日志 | `/workspace/40070_rtlagent_validation_20261030-r11/outputs/unittest.log` |

旧 46 项单测和七题历史记录不是当前快照成绩；本包当前证据为上述 76 项及下列同版本样本。

## r11：同版本已完成 12 道题

源码 revision 均为 `3369812`。重点批次为 `outputs/r11-focused-20261030`，扩展批次为 `outputs/r11-broad-20261030`，相对于上述 r11 服务器目录。

| 题目 | 官方等级 | 失配/样本 | agent 秒 | judge 秒 | agent 自检接受 |
| --- | --- | --- | ---: | ---: | --- |
| Prob086_lfsr5 | L1 | 4280/4443 | 207.275 | 10.032 | 否 |
| Prob137_fsm_serial | L1 | 88/905 | 170.113 | 10.030 | 否；checker 两次 ReadTimeout |
| Prob153_gshare | L1 | 507/1083 | 326.870 | 10.183 | 否 |
| Prob001_zero | L3 | 0/20 | 64.438 | 28.607 | 是 |
| Prob018_mux256to1 | L3 | 0/2000 | 144.322 | 29.579 | 否；checker 编译失败后重建/复核超时 |
| Prob030_popcount255 | L1 | 202/215 | 353.001 | 10.434 | 否 |
| Prob038_count15 | L3 | 0/421 | 150.274 | 43.247 | 否；checker 构造/编译失败且重试预算耗尽 |
| Prob042_vector4 | L3 | 0/100 | 32.233 | 28.357 | 是 |
| Prob066_edgecapture | L1 | 93/266 | 132.885 | 9.929 | 否 |
| Prob107_fsm1s | L3 | 0/230 | 48.463 | 43.094 | 是 |
| Prob122_kmap4 | L3 | 0/232 | 32.797 | 28.464 | 是 |
| Prob145_circuit8 | L1 | 182/240 | 152.941 | 10.031 | 否 |

合计 6 道 L3、6 道 L1；扩展九题为 6 道 L3、3 道 L1。选定开发样本不是全题集准确率或 pass@k，没有同批基线则不报告增益。
尤其不能把旧版本 Prob030、Prob066 的历史 L3 算作本批通过；本批已出现重新生成不稳定。

### 预算与时间解释

重点三题设置 `AGENT_DEADLINE_S=600`、`AGENT_GENERATION_S=240`，外层 agent 635 秒、judge 120 秒。
扩展九题未覆盖 deadline，agent 默认 900 秒，但外层开发脚本仍为 635 秒；本批均正常退出且早于外层上限。
这没有验证完整 900 秒边界；后续题目如命中外层上限，须记录为开发预算截断，不能等同正式默认配置。

正式接口时限依据是 `docs/API_CONTRACT.md` 中的 `deadline_s=900`，不是 90 秒。
`trace.llm_calls` 与 `trace.llm_wall_s` 只累计成功返回并记录耗时的模型调用，不包含 ReadTimeout 等失败等待；总时间应以 `agent.wall_s` 为准。

## 历史批次对照

| 批次/源码 | 题目 | 官方等级 | 失配/样本 | agent 秒 | judge 秒 |
| --- | --- | --- | --- | ---: | ---: |
| r7 / `3232bf5` | Prob030_popcount255 | L3 | 0/215 | 42.103 | 33.068 |
| r7 / `3232bf5` | Prob086_lfsr5 | L1 | 4280/4443 | 134.116 | 10.081 |
| r7 / `3232bf5` | Prob107_fsm1s | L3 | 0/230 | 46.211 | 42.732 |
| r7 / `3232bf5` | Prob137_fsm_serial | L1 | 92/905 | 135.824 | 10.036 |
| r7 / `3232bf5` | Prob153_gshare | L1 | 544/1083 | 328.047 | 10.030 |
| r8 / `9099804` | Prob086_lfsr5 | L1 | 4280/4443 | 320.984 | 10.429 |
| r8 / `9099804` | Prob137_fsm_serial | L1 | 92/905 | 165.270 | 10.082 |
| r8 / `9099804` | Prob153_gshare | L1 | 577/1083 | 327.562 | 10.186 |

r7 的 deadline/单次生成/checker 单步为 360/180/45 秒，外层 395 秒。
r8 为 600/240/60 秒，外层 635 秒。judge 均 120 秒。
预算和随机生成输出不同，不能据此得出单变量速度或改进幅度结论。

服务器历史目录分别为：
- `/workspace/40070_rtlagent_validation_20261030-r7/outputs/r7-real-20261030`
- `/workspace/40070_rtlagent_validation_20261030-r8/outputs/r8-focused-20261030`

## 共性问题及后续方向

1. **诊断不等于修复**：LFSR tap 映射、gshare 预测周期与并发更新、sticky capture 历史采样和 latch 波形可被 checker 拒绝，但五轮模型修复仍未通过官方功能验证。
2. **生成稳定性**：Prob030 出现越界、漏计高位和 X 输出；保留可综合候选也不能确保功能正确。旧版本一次通过不代表稳定通过。
3. **checker 构造可靠性**：串行、mux、计数器题出现超时、缺失 `check`、不合法 helper 声明。保留候选可以官方通过，但不能记作 agent 已自行验证。
4. **预算**：2048 token checker 上限未消除超时；盲目加轮次或延长等待没有充分收益证据。
5. **官方边界**：不改官方 judge，不从官方参考生成 oracle，不以编译/综合替代功能判定，不为单题写固定答案。

继续扩大未测题型，按共同失败分类修复；新题与复测分别记录。声称改动提升通过率前，须在同题、同模型、同预算及独立官方 judge 下复测。

## 后续 checker 控制器修正

根据 mux、计数器等不同任务暴露的 checker 构造错误，做以下通用改动：

- 控制器在初始化输入后，用独立命名块承载 stimulus，允许刺激开头声明局部变量；此前输入赋值会让紧随其后的声明成为不合法的“语句后声明”。
- 解析阶段提前拒绝模块作用域中非法的 `local/protected/virtual task/function`，不盲目改写其语义，也不把坏 checker 运行当作 DUT 失败。
- checker 提示补充 module-scoped helper 的合法语法及独立延时语句的分号规则。
- 增加真实 Vivado 回归：带局部循环变量的相同 checker 应接受 AND DUT，拒绝 OR 变异；另有解析层正反例确保合法 automatic helper 和标签字符串不被误拒。

Windows 本地为 Ran 69、OK、skipped=10。服务器 20 道新题继续使用固定的 `3369812` 快照；以上改动的 Linux/Vivado 验证须在该批完成后另记，不冒充已有题目通过率提升。
