# QuantKing V3 路线图

## 状态说明

| 阶段 | 状态 | 说明 |
|---|---|---|
| 设计阶段 | 已完成 | 产品、数据、研究、回测、交易、Web、风险、日志、备份和实施顺序已确认 |
| Stage 0 | 已完成 | 2026-07-16 完成工程、兼容性、任务、日志、Web、备份恢复、旧项目清单和全量质量门验收 |
| Stage 1 | 进行中 | 日线数据仓、备用源实测和数据质量；S1-05 已于提交 `e580d6963e01b1301dc26f29ed57a0b5b4770a0f` 完成主验收，S1-06 已于提交 `413805074b3a82c2d2a090d2b73c854a238e2e19` 完成主验收。S1-07A 已于 2026-07-29 在提交 `37512be531e15bbd5a698e6a227b0753781ef5e6` 完成主验收；S1-07B 已于 2026-08-04 在提交 `d60559601efd61de15b41ffa86b4f2f730a69d5e` 完成主验收。S1-07C0A、C0B-R1、C1A-R3、C1B-R1 与 C1C0-R3 已完成纯离线控制链/运行就绪化独立主验收；历史 C1B 结论为 **“S1-07C1B-R1 已完成独立主验收（纯离线运行就绪化）；真实运行数为 0。”** C1C0-R3 接受提交为 `85d5f44a0564a487540ba072f6ba75c4dce8ee34`，**“S1-07C1C0-R3 已完成独立主验收（纯离线 XTData ST observation 运行就绪化）；真实运行数为 0。”** Native XtData 官方页面尚未证明 `xtdata.get_his_st_data` 存在；真实 MiniQMT/XTQuant 调用数为 0，observation 始终为 `BLOCKED/CONTRACT_INCOMPLETE`，不证明 `READY` 或生产 capability。17 个 purpose 仍是覆盖 backlog，24 个 source×dataset 单元仍为 BLOCKED_EXTERNAL_VALIDATION；**“S1-07C1C1-P0 已完成独立主验收（纯离线正式 file-only 操作入口与逐次授权交接）；C1C1 真实运行数为 0，仍未授权、未开始。”** 证据见 [P0 独立主验收记录](stage1/S1-07C1C1-P0_ACCEPTANCE.md)。后续唯一候选是用户基于具体 review 的单次明确授权；C2、S1-07D、S1-08 和 Stage 2 均未开始、未获授权 |
| Stage 2 | 未开始 | Qlib、因子、标签和动态股票池 |
| Stage 3 | 未开始 | 线性/Ridge、LightGBM 和模型审批 |
| Stage 4 | 未开始 | Top 30 组合、风险和双层回测 |
| Stage 5 | 未开始 | 调仓计划、QMT 模拟账户、成交和对账 |
| Stage 6 | 未开始 | 至少 4 次周度模拟调仓和系统验收 |
| Stage 7 | 未开始 | 财务、估值、行业和历史指数成分 |

状态必须由实际验收证据驱动。文档存在、代码提交或单次演示均不能单独证明阶段完成。

除已主验收的 C0A/C0B/C1A-R3 离线合同、C1B-R1 和 C1C0-R3 纯离线运行就绪化外，**“P0 纯离线
操作入口完成，C1C1 真实运行仍未开始、未授权。”** C2、S1-07D、S1-08 和 Stage 2 尚未开始、未获授权。

## 近期路径

```text
Stage 0 可恢复工程基线
    → Stage 1 可信日线数据版本
    → Stage 2 可核对因子和标签
    → Stage 3 可复现模型
    → Stage 4 统一组合与正式回测
    → Stage 5 模拟账户交易闭环
    → Stage 6 连续模拟验收
```

Stage 7 是首期闭环之后的数据扩展，不应提前混入 Stage 1～Stage 6。

## 首期完成定义

Stage 6 完成后，首期系统应能够：

- 从 XTQuant 更新并发布通过质量门禁的日线数据版本。
- 计算基础审计因子和 Alpha158，训练线性/Ridge 与 LightGBM。
- 生成周度 Top 30 目标组合和版本化调仓计划。
- 使用 Qlib 快速评价并由 Backtrader 正式验收。
- 在 Web 审批计划、查看任务与日志。
- 用户在 QMT 模拟账户人工下单后，同步、确认成交并完成对账。
- 通过风险、故障恢复和异盘备份验收。

即使首期完成，系统仍不自动下单；进入真实账户需要用户单独决策和授权。

## 文档关系

- 具体任务范围和完成条件：[实施计划](IMPLEMENTATION_PLAN.md)。
- 已确认决策：[架构决策](DECISIONS.md)。
- 代码和质量要求：[技术标准](TECHNICAL_STANDARDS.md)。
- 总体模块和流程：[总体架构](ARCHITECTURE.md)。
