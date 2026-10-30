# 开源实现参考

以下源码只读用于研究校验与错误恢复边界，未引入对应框架的运行依赖。

| 项目 / revision | 源码位置 | 参考机制 |
| --- | --- | --- |
| Pi / `74f46864025c68504cd10c89eeb626b7e3d64cea` | `packages/agent/src/agent-loop.ts:735–784`、`packages/ai/src/utils/validation.ts:317–352` | 先校验工具输入，通过后执行；错误作为具体反馈 |
| OpenCode / `c7a59b7703db31ebf459fa3e37ffbeb6e1cfa257` | `packages/core/src/tool/runtime.ts:64–101` | schema 校验与执行分离 |
| Hermes / `92663ae8054ae81f207f46d83512d3f879dc698d` | `agent/turn_tool_validation.py:178–228` | 拒绝截断 JSON；保留原请求及随后校验反馈 |

## 实现映射

- `agent/verification.py` 的 checker 生成与复核只接收公开规格、接口和 checker，
  不接收候选 RTL 或官方参考。复核返回严格批准/问题清单，不重写已冻结的 checker。
- 批准前后记录同一哈希；拒绝时携原响应和具体问题独立重建，未经批准不执行。
- 生成器最多三次构造；完整 checker 与复核分别最多 2048 / 512 输出 token。
  解析错误、协议冲突和 length 截断均拒绝。
- stimulus 作用域禁止遮蔽 DUT 端口及不支持的声明类型；coverage 仅为说明字段。
  检查直接放在 stimulus，期望值未知或零比较不能形成通过。
- 程序化路径仅转换本次题面显式表格与波形，不包含预置电路算法。

上述边界由协议、冻结、拒绝重建和 Vivado 集成回归验证。
开源框架本身未实现本方案的 RTL checker；这里参考的是通用设计机制。
真实题目效果见对应固定版本的验证记录，不能从输出上限或协议测试推断通过率收益。
