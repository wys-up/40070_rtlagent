# 开源 agent 实现借鉴记录

本记录对应本地已下载源码的只读审查，2026-10-30。最终包中的实现面向比赛的
公开题面、接口、独立 checker 和 Vivado 流程；借鉴的是校验边界与反馈方式。

## 可追溯来源

| 项目 / revision | 源码位置 | 借鉴的边界 |
| --- | --- | --- |
| Pi / `74f46864025c68504cd10c89eeb626b7e3d64cea` | `packages/agent/src/agent-loop.ts:735–784`、`packages/ai/src/utils/validation.ts:317–352` | 工具输入先校验，通过后传递已校验参数，失败转成明确错误供修正 |
| OpenCode / `c7a59b7703db31ebf459fa3e37ffbeb6e1cfa257` | `packages/core/src/tool/runtime.ts:64–101` | schema 校验与执行分离，返回有界、具体的字段问题 |
| Hermes / `92663ae8054ae81f207f46d83512d3f879dc698d` | `agent/turn_tool_validation.py:178–228` | 截断 JSON 不进入执行，解析错误以具体反馈进入有界恢复 |

这些框架没有在此处实现 RTL checker reviewer；本仓库将相同边界思想应用到
`agent/verification.py::review_selftest` 和现有的独立重建循环，没有引入其运行时依赖。

## 对应的共性问题

开发中一次只读抽样分析发现：11 次已完成的完整重写式 checker 复核共耗时
363.61 秒，单次 22.18–48.38 秒；其中 3 次将可解析的生成结果改成缺少 `check()`
的无效结果，发生于 Prob038 第一次复核、Prob027 第二与第三次复核。
来源为 r11 broad/new20 的 `selftest-response-*.txt`、`selftest-review-*.txt` 和 JSONL。
这是对复核器的诊断，不是题目通过率或全题集统计。

## 本包实现

1. 生成器继续输出完整 checker；reviewer 只看原规格、接口、契约及该 checker，
   返回严格的 `{"approved": boolean, "issues": [string]}`，不获取 DUT 实现。
2. 批准必须为真正的 JSON 布尔值、空 issues，且无额外字段；拒绝须有 1–4 个
   非空问题，每个至多 500 字符。未知字段、冲突 verdict、解析错误及 length 截断均不批准。
3. 批准后冻结原 checker。控制器记录复核前后相同的 checker 哈希，若复核改变产物则拒绝。
   拒绝时由现有独立生成器携原 checker 和具体问题重新生成，再审核；未批准不运行。
4. 复核输出上限独立设为 512 token，完整 checker 仍为 2048；每次调用继续共享既有墙钟预算。
5. 批准不等于 RTL 通过。后续仍须完成 Vivado 仿真，具有非零比较、无 unknown oracle、
   无失配且有完整通过标志；官方 judge 继续独立运行。

当前只确认本地协议、冻结、拒绝重建和防止未审核执行的回归通过；真实模型速度、
checker 有效率与官方通过率应在对应提交的服务器批次中验证。缩小输出上限本身不是收益证据。

本轮没有增加插件系统、凭据管理、外部检索、多模型路由等与 RTL 比赛无关的功能。
独立 stub 的 checker 预编译是后续候选方向，其工具成本和收益尚未实测，未纳入本次实现。
