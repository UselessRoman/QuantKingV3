# QuantKing V3 总体架构

## 文档状态

本文描述已确认的目标架构。Stage 0 已于 2026-07-16 完成验收，Stage 1 已获实施授权并进行中，但尚未完成；实现状态以 [路线图](ROADMAP.md) 为准。架构决策以 [DECISIONS.md](DECISIONS.md) 为最高依据。

## 架构风格

QuantKing V3 是本地模块化单体：领域和应用逻辑位于一个 Python 包中，FastAPI、CLI、后台任务和 QMT 访问复用同一套应用服务。进程隔离用于稳定性和资源管理，不构成微服务。

```mermaid
flowchart LR
    UI["React Web"] --> APP["应用服务"]
    CLI["CLI"] --> APP
    WORKER["任务工作进程"] --> APP

    APP --> DATA["数据"]
    DATA --> RESEARCH["因子与模型"]
    RESEARCH --> PORTFOLIO["组合构建"]
    PORTFOLIO --> RISK["风险门禁"]
    RISK --> SIGNALS["信号与调仓"]
    SIGNALS --> BACKTEST["回测"]
    SIGNALS --> TRADING["交易与对账"]

    XT["XTQuant"] --> DATA
    XT --> TRADING
    QLIB["Qlib"] --> RESEARCH
    QLIB --> BACKTEST
    BT["Backtrader"] --> BACKTEST
```

## 进程

| 进程 | 职责 | 限制 |
|---|---|---|
| Web/API | 提供同源 API、静态前端、SSE 与输入校验 | 不执行长任务，不包含业务规则 |
| Worker | 串行或受控并发执行下载、因子、训练、回测、备份 | 每个任务必须持久化状态和日志 |
| QMT 访问 | 在受控进程中完成行情或账户只读调用 | 不暴露任何下单能力 |
| CLI | 诊断、维护、离线运行和验收 | 调用同一应用服务，不复制逻辑 |

Stage 0 可将 Worker 和 QMT 访问放在同一工作进程，后续仅在稳定性证据要求时拆分进程。

Web 与 Worker 之间不建立内存队列。Worker 将低频任务状态和进度写入 SQLite `JobEvent`；Web 按单调事件游标读取后向浏览器发送 SSE。完整 JSON Lines 日志由受控日志读取适配器按文件偏移尾随，不把每条日志复制进 SQLite。

## Python 模块

```text
src/quantking/
  common/       ID、时间、版本、错误、配置、日志基础
  data/         数据接入、标准化、质量、版本与发布
  research/     因子、标签、数据集、实验与模型
  portfolio/    分数到目标组合、换仓与资金分配
  risk/         风险规则、检查结果与覆盖审批
  backtest/     Qlib 快速回测和 Backtrader 正式回测
  signals/      目标组合、调仓计划和生命周期
  trading/      账户、委托、成交、策略账与对账
  jobs/         持久化任务、取消、恢复和进度
  web/          FastAPI 路由、API schema、前端托管
  cli/          命令行入口
```

复杂模块使用轻量内部结构：

```text
module/
  models.py        领域对象和数据契约
  ports.py         不稳定外部边界
  services.py      用例和流程编排
  repositories.py  持久化接口
  adapters/        外部 SDK、数据库或引擎实现
```

不为每个小函数机械套用层级。端口只用于 XTQuant、备用源、Qlib、Backtrader、数据库、文件系统和时钟等需要替换或隔离的边界。

## 依赖方向

- `portfolio` 接收标准预测分数，不导入 Qlib。
- `risk` 依赖稳定业务对象，不导入 Backtrader、XTQuant 或 FastAPI。
- `signals` 不直接查询券商；它基于账户快照和策略账生成计划。
- `trading` 只实现读取、匹配、确认和对账，不提供下单端口。
- `web`、`cli` 和 `jobs` 调用应用服务，不自行实现领域规则。
- 供应商 DataFrame、回调对象和异常不得越过适配器边界。
- 批量数据可使用 Pandas、Arrow 和 DuckDB；交易计划、审批、成交和审计使用类型明确的业务对象。

## 核心对象

| 对象 | 作用 |
|---|---|
| `DatasetVersion` | 标识一次通过质量门禁的数据发布 |
| `FactorDefinition` / `FactorSetVersion` | 定义因子语义和集合版本 |
| `ExperimentRun` | 绑定数据、股票池、标签、切分、参数和产物 |
| `ModelVersion` | 可审批、启用和停用的模型版本 |
| `PredictionSet` | `date × code → score` 的不可变输出 |
| `TargetPortfolio` | 某信号日的目标股票和权重 |
| `RiskAssessment` | 风险检查结果、级别和覆盖记录 |
| `RebalancePlan` | 当前组合到目标组合的版本化调仓意图 |
| `BrokerSnapshot` | QMT 账户资产、持仓、委托和成交快照 |
| `Fill` | 原始成交明细及其策略归属 |
| `ReconciliationIssue` | 计划、策略账与券商总账的差异 |
| `JobRun` | 长任务状态、输入、日志、产物和失败信息 |
| `JobEvent` | 可跨进程恢复的低频任务状态和进度事件 |

## 主链路

### 数据发布

```text
数据源 → Raw 批次 → 标准化 → 质量检查 → Canonical 临时版本
      → 清单与校验和 → 原子发布 DatasetVersion
```

### 研究与模型

```text
DatasetVersion + 股票池规则
    → Qlib 派生数据
    → 因子和标签
    → 时间切分
    → 线性/Ridge 与 LightGBM
    → 实验记录
    → 模型审批
    → PredictionSet
```

### 组合与回测

```text
PredictionSet → 动态股票池 → Top 30 基本等权
    → 5% 单股上限 + 3% 现金 + 换仓限制
    → RiskAssessment → TargetPortfolio
    ├─ QuantKing Qlib Strategy 适配器 → Qlib 快速回测
    └─ Backtrader 正式回测
```

Qlib Strategy 适配器按日期读取已经生成的 `TargetPortfolio`，不得从 `score` 再次选股或计算目标权重。Qlib 内置组合构造策略只可用于明确标记的外部基准实验，不得进入 QuantKing 双引擎校准或正式候选策略结论。

### 调仓与对账

```text
TargetPortfolio + 已确认策略账 + BrokerSnapshot
    → RebalancePlan → 用户批准 → QMT 人工下单
    → 只读同步成交 → 待确认匹配 → 用户确认
    → 策略账更新 → 总账/子账对账
```

## 物理存储

```text
E:\QuantKingV3Data\
  raw\          各来源不可变原始批次
  warehouse\    标准 Parquet 数据版本
  qlib\         Qlib 派生视图
  artifacts\    因子、模型、回测和正式报告
  logs\         运行和任务 JSONL 日志
  backups\      本地恢复快照
  quantking.db  元数据、任务、审计、账务和对账

F:\QuantKingV3Backups\
  ...           异盘灾难备份
```

SQLite 使用 WAL 模式、连接级 `busy_timeout`、短事务和针对锁冲突的有界指数退避；任何训练或回测不得在数据库事务中执行。心跳采用合并写入，完整日志不进入 SQLite。Parquet 发布使用临时目录和原子重命名。DuckDB 是查询引擎，不是业务状态数据库。

## 运行触发

首期日常 EOD 链路由用户从 Web 或 CLI 手动触发，两种入口调用同一应用服务。系统不建设内部定时调度器，也不依赖 Windows 任务计划；以后引入无人值守调度必须重新评审失败恢复、幂等和用户授权边界。

## 配置

- 仓库保存无机密默认配置和 schema。
- 本机路径、QMT 账号、券商费率和敏感配置放在不入 Git 的本地配置中。
- 影响研究、回测、风险或调仓的参数修改必须生成配置版本。
- 正式实验和计划引用配置版本，而不是隐式读取当前配置。

## 安全边界

- Web 只绑定 `127.0.0.1`，默认不启用 CORS 或远程访问。
- API 不接受任意 Python 表达式、任意模型路径或任意文件系统路径。
- 模型只从受管理产物目录加载，并验证清单和校验和。
- 日志掩码账户标识，不写密码、令牌或完整敏感配置。
- 任何自动下单入口都属于架构违规。

## 状态声明

以上除已验收的 Stage 0 工程基线外均为目标结构。当前只确认锁定环境、通用持久任务与日志、最小本机 Web 壳、备份恢复和迁移清单等 Stage 0 能力；Stage 1 S1-01 已于 2026-07-16 在提交 `7e4e5b38b7fc91edaf8727ed55b1e26435373aea` 完成主验收（数据合同、元数据持久化与安全迁移基础），S1-02（Warehouse、原子发布与显式版本查询）已获实施授权并进行中。Stage 1 其余业务能力（XTQuant 抓取、质量、跨源差异、EOD、Web 页面等）尚未实现。各模块状态以 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) 和 [Stage 0 最终验收报告](stage0/FINAL_ACCEPTANCE.md) 为准。
