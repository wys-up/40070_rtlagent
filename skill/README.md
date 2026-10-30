# RTL 技能包

技能按题面与上一轮工具反馈路由；题面最多两条，再补工具反馈，总计最多三条。
`agent/skills.py` 对 `Log signatures` 进行大小写不敏感的完整单词或短语匹配，
短语允许换行，具体短语优先于单词，按短语命中数与签名长度排序；
通用时序规则的宽泛匹配优先级较低。组合任务不注入通用时序技能。

| 技能 | 适用范围 |
| --- | --- |
| `rtl-module-interface-contract` | 模块名、端口、方向、位宽和接口修复 |
| `rtl-sequential-timing` | 采样边沿、非阻塞赋值、输出周期、复位和重叠 |
| `rtl-arithmetic-widths` | 算术位宽、符号扩展、进位、截断和边界值 |
| `rtl-specification-tables` | 真值表、卡诺图的实际标签、变量顺序和有效格 |
| `rtl-fsm-output-timing` | Moore/Mealy 输出周期、转移表与单一过程归属 |
| `rtl-branch-predictor` | 按题面分析预测/训练索引、观察周期与并发优先级 |
| `rtl-serial-receiver` | 串行帧采样、位序、完成信号与错误恢复 |
| `rtl-shift-register-pattern` | 移位方向、拓扑、抽头编号和序列重叠 |

每条技能位于 `<skill-name>/SKILL.md`。front matter 包含 `name` 和
`description`；正文使用英文，并提供 `Trigger`、`Log signatures` 和适用边界。
该组织方式参考 AMD Ross AI Assistant 的技能格式；运行时不依赖该项目。

技能提供推导与检查步骤，不保存固定题目的成品设计或固定模式检测答案。
主系统提示与调用方控制最终输出格式。领域规则的通过率收益须由同版本消融验证，
技能路由或单元测试通过不等同于此类收益。
