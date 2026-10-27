# Stage 4 文档入口

Stage 4 把 Stage 3 已批准候选模型的分数转换为唯一 `TargetPortfolio`，用 Qlib 和 Backtrader
消费同一组合与成本规则，并在正式验收后启用模型。阶段状态只看 [路线图](../ROADMAP.md)。

## 文档职责

- [任务索引](TASKS.md)：S4-01～S4-03 的顺序和共同边界。
- [S4-01 任务卡](S4-01_TASK.md)：组合、交易规则、成本与风险内核。
- [S4-02 任务卡](S4-02_TASK.md)：Qlib/Backtrader 适配与双引擎校准。
- [S4-03 任务卡](S4-03_TASK.md)：正式回测、最小页面与模型启用。
- [验收矩阵](ACCEPTANCE.md)：阶段门禁与证据要求。

## 阶段边界

- 模型只提供 `PredictionSet`；组合服务是 Top 30、权重和换仓规则的唯一实现。
- Qlib、Backtrader 和后续调仓计划只消费同一不可变 `TargetPortfolio`。
- 只支持冻结研究窗口涉及的日频 A 股规则、一个成本配置和一个组合规则版本。
- 风险服务首版是纯函数规则集合，不建设规则 DSL、通用策略引擎或账户风控平台。
- Backtrader 是正式验收口径；Qlib 只表达共同规则和快速比较，不能为对齐而弱化正式口径。
- Stage 4 可把正式验收通过的候选模型设为 `使用中`，但不生成真实调仓计划或券商订单。

## 主链

```text
已批准 ModelVersion + PredictionSet
  → PortfolioRuleVersion
  → TargetPortfolio
  → RiskDecision
  → Qlib / Backtrader
  → BacktestRun
  → 用户启用模型
```
