# Stage 1 任务卡

状态：Stage 0 已完成；Stage 1 已获实施授权并进行中，但尚未完成。S1-01 已实现并离线测试通过；S1-02～S1-08 未开始。

每个任务必须声明所属 Stage、范围、输入、测试和验收证据。前一任务的完成条件未满足前，不得开始后一任务。

## S1-01：数据合同、元数据持久化与安全迁移基础

- **状态**：已实现，离线测试通过，等待主验收。
- **范围**：
  - 建立框架无关的数据领域合同（`DataSource`、`DatasetKind`、`IngestionBatch`、`DatasetVersion`、`QualityRun`/`QualityResult`、`DifferenceReport`、`SourceOverride`、manifest 文件项、覆盖摘要和失败信息）。
  - 固化三类状态机（批次、版本、override）的不可变语义。
  - 固化六类 Canonical 可执行 schema 与 PyArrow 映射和验证。
  - 建立 Stage 1 元数据持久化（ORM、Alembic revision、仓储、不可变触发器）。
  - 实现既有 SQLite 的安全迁移入口（快照、隔离验证、失败保留）。
- **输入**：Stage 0 基线提交 `ab1c9f9`。
- **测试**：领域对象与状态机单元测试；六类 schema 字段/类型/单位/主键/排序/null/边界样本测试；ORM 与领域分离测试；Alembic 新库 head 测试；Stage 0 revision → Stage 1 revision 升级测试；安全快照、隔离迁移与失败保留测试；published/approved 不可变测试；SourceOverride 非法转换与审计测试；架构边界测试；Markdown 链接测试。
- **验收证据**：`tests/data/`、`tests/integration/data/`、`tests/docs/` 全部离线通过；`scripts/quality.ps1` 退出码 0；未访问网络、QMT、`E:\QuantKingV3Data`、`E:\QuantKing` 或 `F:\QuantKingV3Backups`。
- **禁止项**：不实现 Raw 写入、Parquet 分区写入或 DatasetVersion 发布；不实现 DuckDB 查询；不实现任何数据源适配器；不实现质量规则执行器、跨源比较或 override 补洞；不实现 EOD、Worker handler、CLI/API 或 Web 页面；不实现 Qlib、因子、模型、组合、风险、回测或交易；不添加调度器或自动发布；不读取旧项目内容。

## S1-02：Raw 落地、Canonical 标准化与批次清单

- **状态**：未开始，未获实施授权。
- **范围**：实现 Raw 不可变落地、供应商字段到 Canonical 的标准化、批次清单与 SHA-256。
- **依赖**：S1-01 完成。
- **禁止项**：不实现数据源抓取（S1-03）；不发布 DatasetVersion（S1-06）。

## S1-03：XTQuant 主数据适配器与增量抓取

- **状态**：未开始，未获实施授权。
- **范围**：实现 XTQuant 主数据适配器、增量抓取与供应商异常到稳定错误转换。
- **依赖**：S1-02 完成；真实 XTQuant 访问须单独授权。
- **禁止项**：不实现备用源适配器（S1-05）；不下单。

## S1-04：质量规则执行器与单源门禁

- **状态**：未开始，未获实施授权。
- **范围**：实现单源质量规则执行器、规则版本、结果与发布阻断。
- **依赖**：S1-02 完成。
- **禁止项**：不实现跨源比较（S1-05）。

## S1-05：备用源差异报告与 SourceOverride

- **状态**：未开始，未获实施授权。
- **范围**：对 BaoStock、AKShare、可选 Tushare 做固定样本实测；实现跨源差异报告与显式 SourceOverride 审批与应用。
- **依赖**：S1-03、S1-04 完成；真实网络访问须单独授权。
- **禁止项**：备用源不静默替换主源；override 只能进入新 DatasetVersion。

## S1-06：DatasetVersion 原子发布与幂等

- **状态**：未开始，未获实施授权。
- **范围**：实现临时目录 → 质量 → 清单 → 原子发布；重复任务幂等；失败批次不污染已发布版本。
- **依赖**：S1-04、S1-05 完成。
- **禁止项**：不原地修改已发布版本；失败产物不得改成成功。

## S1-07：EOD 应用服务、CLI/API 与数据中心页面

- **状态**：未开始，未获实施授权。
- **范围**：实现 Web/CLI 手动触发的同一 EOD 应用服务和幂等入口；数据中心页面展示来源、版本、覆盖、质量、失败和差异。
- **依赖**：S1-06 完成。
- **禁止项**：不建设内部调度器或 Windows 任务计划；不实现自动切源或自动发布。

## S1-08：旧项目 DATA_ONLY 数据迁移

- **状态**：未开始，未获实施授权。
- **范围**：审计旧项目数据，只迁移通过 schema、覆盖和校验和验证的 `DATA_ONLY` 内容。
- **依赖**：S1-06 完成；读取 `E:\QuantKing` 须单独授权。
- **禁止项**：不整体复制旧目录；不迁移未通过校验的数据。

## Stage 2 及以后

Stage 2 及以后尚未授权。任何 Stage 2 开发、Qlib 数据导出或因子实现仍须按任务另行取得明确授权。
