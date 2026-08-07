# QuantKing V3

QuantKing V3 是一个运行在个人 Windows 电脑上的 A 股日频量化研究、组合回测、调仓决策与人工交易登记系统。它面向单用户，覆盖“数据更新 → 因子与模型 → 组合与回测 → 调仓计划 → 人工下单 → 成交同步与对账 → Web 复盘”的闭环。

> 当前状态：架构设计已经确认，Stage 0 已于 2026-07-16 完成验收；Stage 1 已获实施授权并进行中，但尚未完成。个别 Stage 1 子任务可以拥有明确、有限的独立主验收；它们不代表整个 Stage 1 或后续目标能力已经实现或验收。

## 产品边界

- 市场：沪深京 A 股；保存包括退市、ST、停牌在内的全量历史证券数据。
- 频率：首期仅支持日频研究，周度调仓；5 个交易日收益是主标签，20 个交易日收益用于对照。
- 数据：XTQuant 是主数据源；免费备用源必须通过质量实测后启用，且不得静默覆盖主源。
- 研究：Qlib 用于因子、数据集、模型和实验工作流；项目自己拥有数据、因子、标签和版本语义。
- 模型：先建立线性/Ridge 与 LightGBM 基线，不在首期建设深度学习、强化学习或自动模型搜索。
- 回测：Qlib 用于快速研究，Backtrader 用于正式事件驱动验收。
- 交易：系统生成调仓计划，用户在 QMT 客户端人工下单；系统只读同步账户并由用户确认成交归属。
- 界面：本机中文 Web，只监听 `127.0.0.1`。

## 目标闭环

```text
XTQuant/备用源
    → 原始数据与质量校验
    → 标准 Parquet 数据版本
    → Qlib 因子、标签和模型
    → 预测分数
    → Top 30 目标组合
    → 风险门禁
    → 调仓计划
    ├─ Qlib 快速回测
    ├─ Backtrader 正式回测
    └─ QMT 人工执行、成交确认与对账
```

## 技术基线

- Python 3.12、`uv`、`pyproject.toml` 与锁文件。
- Parquet 保存批量数据，DuckDB 查询，SQLite 保存元数据、任务、审计和交易账务。
- SQLAlchemy 2 与 Alembic 管理 SQLite 结构。
- FastAPI + React + TypeScript + Vite + Apache ECharts。
- 本地持久化任务队列、独立工作进程、SSE 进度和结构化日志。
- 代码目录：`E:\QuantKingV3`。
- 运行数据：`E:\QuantKingV3Data`。
- 异盘备份：`F:\QuantKingV3Backups`。

## 文档导航

| 文档 | 用途 |
|---|---|
| [架构决策](docs/DECISIONS.md) | 已确认且会约束实现的产品与技术决策 |
| [总体架构](docs/ARCHITECTURE.md) | 进程、模块、依赖方向和主链路 |
| [数据架构](docs/DATA_ARCHITECTURE.md) | 数据源、分层、版本、质量和发布规则 |
| [研究与回测](docs/RESEARCH_AND_BACKTEST.md) | 因子、标签、模型、组合与双层回测 |
| [交易与风险](docs/TRADING_AND_RISK.md) | 调仓、账户、成交、对账、风险和模拟验收 |
| [Web 与运维](docs/WEB_AND_OPERATIONS.md) | 页面、任务、日志、配置、备份和恢复 |
| [技术标准](docs/TECHNICAL_STANDARDS.md) | 代码、契约、测试和质量门禁 |
| [实施计划](docs/IMPLEMENTATION_PLAN.md) | Stage 0～Stage 7 范围、依赖和完成条件 |
| [路线图](docs/ROADMAP.md) | 当前状态和阶段概览 |

## 非目标

- 自动向券商下单。
- 分钟、Tick、盘口或高频策略。
- 融资融券、卖空或日内回转策略。
- 多用户、云端服务、微服务或公网 API。
- 首期财务报表、估值、行业与历史指数成分因子；这些能力位于 Stage 7。

## 下一步

[Stage 0](docs/IMPLEMENTATION_PLAN.md#stage-0工程与可恢复基线) 已完成，证据见 [Stage 0 最终验收报告](docs/stage0/FINAL_ACCEPTANCE.md)。Stage 1 日线数据仓与数据质量已获实施授权并进行中，但尚未完成；S1-07C1A-R3 已主验收的只是 [纯离线 REAL 控制链合同](docs/stage1/S1-07C1A-R3_ACCEPTANCE.md)，不构成真实 XTQuant 数据能力或真实运行证明。真实网络、真实 XTQuant、运行数据根、旧数据迁移和 F 盘写入仍须逐项授权；C1B、C2、S1-07D、S1-08 与 Stage 2 未开始、未获授权。
