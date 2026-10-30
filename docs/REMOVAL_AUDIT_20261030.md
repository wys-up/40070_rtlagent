# 手写算法检查器与实现提示清理（2026-10-30）

## 原因与边界

根据用户要求删除运行时中针对具体电路类型的手写算法模型和答案式提示。
本地赛事材料 `SCORING.md` §2.10 禁止预置答案，§2.11 以设计是否由模型实质生成为界。
本次清理用于减少这类歧义，不替赛事方作最终合规裁定，不宣称清理提高通过率。

## 已删除

- `agent/oracles.py`：population count、符号扩展、sticky 下降沿捕获、Galois LFSR、
  固定端口/7 位历史/128 项 PHT 的 gshare 算法参考模型。
- 波形中的锁存器/下降沿寄存器拟合与具体实现提示；不再从波形推断实现结构。
- `agent/main.py`：将这些推断作为首轮 Specification observations 注入模型的路径。
- 系统提示与 branch-predictor / shift-register 技能中的固定恢复表达式、递推式和
  五位 LFSR 完整逐位方程。技能保留按本次规格分析位宽、观察周期、并发优先级的步骤。
- 已删除算法模型对应的 Vivado 正确/错误电路夹具，以及要求固定公式存在的旧断言。
  替换为确认电路文字规格不命中预置算法、波形只复现显式值的回归。

## 保留

- 当前输入题面显式给出的 K-map 单元、组合状态表、完整 Moore 转移表和波形样本的
  检查。预期值来自当前表格，状态机检查按当前转移表解释，不保存特定题目的转移答案。
- 接口、语法、综合、时限、输出保留、工具日志和严格 checker 协议。
- 非表格题由模型从本次公开 prompt/interface 生成 checker，独立复核后仿真。
- 通用验证框架与显式表格的测试夹具仅放在 tests；Dockerfile 不复制 tests 到运行镜像。
- 旧提交和带版本/哈希的历史验证资料原样留作追溯；不重写 Git 历史，不把旧成绩改称新成绩。

## 验证

- Windows 项目虚拟环境：`python -m unittest discover -s tests -v`，78 项、2.625 秒，OK；
  10 项因 Vivado / Linux 环境跳过，服务器单独验证。日志位于忽略目录
  `outputs/removal-audit-20261030-windows.log`，SHA256 `6721feee0b10a16df37af21e479ab5dd197fb85ede02625eb1caf3b0fe905716`。
- 两条技能的官方 quick_validate 尚待在有 PyYAML 的环境执行；项目虚拟环境缺少该验证工具依赖，未向运行依赖添加 PyYAML。
- 验证至少覆盖删除后的 fallback、checker 拒绝/独立重建、表格/波形解释和输出协议。
- 清理后完整题库通过率尚未得出；历史开发批次不可复用为新版本成绩。
