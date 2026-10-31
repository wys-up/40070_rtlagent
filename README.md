# RTL Agent

基于本地 Qwen2.5-Coder-32B-Instruct、llama.cpp 与 Vivado 2026.1 的 RTL 生成方案。
输入题面和模块接口，输出 `solution.v` 与逐调用 `trace.jsonl`。

## 运行入口

| 入口 | 用途 |
| --- | --- |
| `serve/start.sh` | 启动本地推理及临时目录清理服务 |
| `serve/run_api.sh` | 启动、检查和管理异步投题接口 |
| `run.sh <input_dir> <output_dir>` | 智能体单题生成 |
| `run_baseline.sh <input_dir> <output_dir>` | 赛事方原版基线 |

正式运行须准备官方基础镜像、模型权重、Vivado 授权和投题令牌。部署命令见
[服务说明](serve/README.md)，单题总墙钟预算为 900 秒。

## 文档

- [设计报告](REPORT.md)：架构、模型、技能、验证与失败分析。
- [模型配置](model/MODEL.md)：来源、revision、权重校验和与推理参数。
- [接口契约](docs/API_CONTRACT.md)：赛事接口参考；其中模型示例不是本方案配置。
- [常见问题](docs/FAQ.md)：参赛环境、判定、模型与评测规则。
- [提交说明](docs/SUBMISSION.md)：仓库提交、封存与复现要求。

开发验证记录、实现来源及已知问题保留在
[历史文档](https://github.com/wys-up/40070_rtlagent/tree/40590a5e903a5a4590ab2f2ea651011830bd0223/docs)。

当前程序化自检仅转换本次题面显式给出的表格和波形；其它规格由模型生成检查器后独立复核。
旧版题目成绩与当前全量评测分别记录，不合并计算通过率。
