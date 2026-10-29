# 40070 RTL Agent

这是比赛提交方案包根目录，包含模型声明、智能体、技能包、推理服务、基线入口和设计报告。

主要入口：

- `serve/start.sh`：启动本地推理服务
- `run.sh <input_dir> <output_dir>`：运行智能体并生成 `solution.v`
- `run_baseline.sh <input_dir> <output_dir>`：运行原版赛事基线
- `REPORT.md`：设计报告
- `model/MODEL.md`：模型、权重和推理配置声明

正式运行前需在断网环境中准备好模型权重、Python 依赖、Vivado 2026.1 和授权配置。
900 秒时限及兜底策略见 `docs/DEADLINE_FALLBACK.md`。
