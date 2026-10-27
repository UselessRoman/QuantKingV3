# Stage 5 文档入口

Stage 5 用一个 `使用中` 模型、一个策略和一个本地模拟账本，形成人工确认的周度调仓、成交导入
与对账闭环。阶段完成即达到首次可监督使用里程碑。阶段状态只看 [路线图](../ROADMAP.md)。

## 文档职责

- [任务索引](TASKS.md)：S5-01～S5-03 的顺序和共同边界。
- [S5-01 任务卡](S5-01_TASK.md)：账户快照、正式预测、风险与调仓计划。
- [S5-02 任务卡](S5-02_TASK.md)：批准/导出、成交导入、策略账与对账。
- [S5-03 任务卡](S5-03_TASK.md)：调仓账户工作区、完整 E2E 与首次闭环。
- [验收矩阵](ACCEPTANCE.md)：首次可监督使用门禁。

## 阶段边界

- 只有 `使用中` 模型能对显式最新研究版本生成正式 `PredictionSet` 和计划。
- 模型分数、`TargetPortfolio`、风险决定、`RebalancePlan`、用户导入记录和确认 `Fill` 分层保存。
- 首版只支持一个策略账、一个规范账户快照 CSV、一个规范成交 CSV 和人工录入。
- 精确匹配失败只生成待确认项，不建设自动猜测、多券商解析或账户聚合。
- 计划是人工交易意图，系统只导出文件，不连接券商、不提交或撤销订单。
- Web 只交付一个调仓/账户工作区，复用现有任务和日志页面。

## 主链

```text
使用中模型 + 显式研究版本
  → 正式 PredictionSet
  → TargetPortfolio + RiskDecision
  → RebalancePlan
  → 用户批准并手工交易
  → 导入/录入 ImportedBrokerRecord
  → 用户确认 Fill
  → StrategyLedger + Reconciliation
```
