# Stage 1 验收矩阵

状态：Stage 0 已完成；Stage 1 已获实施授权并进行中，但尚未完成。S1-01 已于
2026-07-16 在提交 `7e4e5b38b7fc91edaf8727ed55b1e26435373aea` 完成主验收，证据见
[S1-01 主验收证据](S1-01_ACCEPTANCE.md)。
S1-02 已于 2026-07-18 在提交 `79f9fa3e682e212cf74e7886a04c6fb52158f302`
完成主验收，证据见 [S1-02 主验收证据](S1-02_ACCEPTANCE.md)。S1-03-R1 修复中/待主验收。
S1-04 已于 2026-07-22 在提交 `2f99491e9dc410acf85ca81c131a77cae08d0cc3`
完成主验收，证据见 [S1-04 主验收证据](S1-04_ACCEPTANCE.md)。
S1-05-R1 修复已提交，等待主验收；S1-06～S1-08 未开始、未获授权。

阶段状态只能由实际验收证据驱动。文档存在、代码提交或单次演示均不能单独证明阶段完成。

## Stage 1 完成条件（源自 [实施计划](../IMPLEMENTATION_PLAN.md)）

- 可构建并查询一个全量或明确范围的 Canonical `DatasetVersion`。
- 重复任务幂等；失败批次不污染已发布版本。
- XTQuant 与备用候选的小样本差异报告可复现。
- 全量历史证券，包括退市/ST/停牌数据，进入底层范围或有明确缺口报告。
- 数据质量失败会阻止发布并在 Web 显示。

以上条件需在 S1-02～S1-08 全部完成后方可整体验收。Stage 1 整体验收尚未进行。

## S1-01 验收标准

S1-01 是 Stage 1 的合同与基础设施基线，本身不发布数据版本。验收标准：

| 验收项 | 状态 | 证据 |
|---|---|---|
| 框架无关数据领域合同与直接构造状态一致性 | 已主验收 | `src/quantking/data/models.py`；`tests/data/test_state_machines.py` |
| 严格 Canonical Arrow 合同、排序、OHLC/交易状态与显式空结果 | 已主验收 | `src/quantking/data/adapters/canonical_schema.py`；攻击回归测试 |
| DatasetVersion 覆盖、父版本与输入批次 FK 血缘 | 已主验收 | `20260716_0002`、`20260716_0003`；仓储集成测试 |
| QualityRun/QualityResult/DifferenceReport 完整仓储和查询 | 已主验收 | `tests/integration/data/test_data_repository.py` |
| SourceOverride 三方绑定、同数据集直接子版本与不可绕过审计事务 | 已主验收 | `20260716_0003`；数据库触发器、事务回滚测试 |
| 终态不可变与失败后相同参数新 batch ID 重试 | 已主验收 | 状态/仓储/迁移集成测试 |
| Stage 0 完整快照合同、维护期写边界、run ID 终身占用和迁移证据 | 已主验收 | `tests/integration/data/test_safe_migration.py` 攻击回归 |
| Alembic 新库 head 与 Stage 0→`20260716_0003` 升级 | 已主验收 | `tests/integration/data/test_migrations_stage1.py` |
| `scripts/quality.ps1` 退出码 0 | 已主验收 | 详见 [S1-01 主验收证据](S1-01_ACCEPTANCE.md) |
| 默认测试不访问网络/QMT/正式根 | 已主验收 | 测试必须只使用 `tmp_path` 和合成数据 |

S1-01 主验收 Agent 复验后，状态被标记为已通过；离线定向测试与 `scripts/quality.ps1` 仍须
保持退出码 0，但已不再是验收判据。S1-01 通过不代表 Stage 1 完成；其余任务仍按主验收
顺序推进。

## S1-02 验收标准

| 验收项 | 状态 | 证据 |
|---|---|---|
| Raw 安全组件、受管理路径、不可变批次、manifest/SHA/COMPLETE 与失败证据 | 已主验收 | `raw_store.py`；路径与完整性回归 |
| 六类 Canonical 严格 schema、全局主键/排序、年月分区与合法空版本 | 已主验收 | `canonical_warehouse.py`；六类参数化发布/回读 |
| `.incomplete` 到 final 原子提升、冲突幂等与并发单一结果 | 已主验收 | 发布集成测试 |
| 统一 published-directory verifier：规范 JSON、自身 SHA、COMPLETE、文件集合、完整 Arrow schema、行数和分区唯一性 | 已主验收 | `verifier.py`；篡改与重解析点回归 |
| 唯一应用级协调服务写入实际 manifest/覆盖凭证并短事务发布 | 已主验收 | `publish_coordinator.py`；中断恢复测试 |
| SQLite 与 manifest 全凭证精确绑定；只有显式 PUBLISHED version_id 可查询 | 已主验收 | `query.py`；凭证漂移测试 |
| 合法空版本和省略排序键的字段投影返回精确 PyArrow schema | 已主验收 | 查询合同回归 |
| `scripts/quality.ps1` 退出码 0，且默认测试不访问外部系统 | 已主验收 | [S1-02 主验收证据](S1-02_ACCEPTANCE.md) |

S1-02 完成只证明 Warehouse、发布和查询基础可用于后续任务，不代表 Stage 1
整体完成，也不自动授权 S1-03。

## S1-03 验收标准

S1-03-R1 修复中/待主验收。当前离线门禁不得写成已经通过主验收：

| 验收项 | 状态 | 证据 |
|---|---|---|
| XTQuant 依赖只存在于适配器边界，生产加载延迟且仅允许 `xtquant.xtdata` | 待主验收 | `sources/xtquant.py`；边界测试 |
| 六类合成响应映射到精确 Canonical schema、排序、主键和语义验证 | 待主验收 | `test_xtquant_mapping.py` |
| 无法证实的 point-in-time、单位、因子和历史状态能力安全失败 | 待主验收 | 能力矩阵；`test_xtquant_adapter.py` |
| 显式覆盖、回看、缺口与稳定分块的 provider-neutral 增量计划 | 待主验收 | `test_incremental_fetch_plan.py` |
| Raw manifest/SHA/COMPLETE、失败证据及 `IngestionBatch` 终态 | 待主验收 | `test_xtquant_ingestion.py` |
| Raw 请求信封、真实 row_count、落地后重建及 Gateway/Raw records 一致性 | 待主验收 | S1-03-R1 绑定与篡改回归 |
| chunk/plan 直接构造、缺失/重复/错序和请求 SHA 复验 | 待主验收 | S1-03-R1 计划攻击回归 |
| 合法空、意外空、部分响应与稳定脱敏错误严格区分 | 待主验收 | 适配器与集成回归 |
| 不创建或发布任何 Canonical `DatasetVersion` | 待主验收 | 集成测试查询版本表为零行 |
| 默认门禁无网络、QMT、正式根和真实 XTQuant 访问 | 待主验收 | 离线守卫；未运行 `real_xtquant` |

真实 XTQuant 样本、QMT 或网络能力没有在本任务中验证。五类未充分证实的数据能力仍是独立显式缺口，
不能由合成测试消除；是否阻断 S1-03 主验收由主验收 Agent 判定。

## S1-04 验收标准

S1-04 已完成主验收。真实供应商能力仍按单独授权边界管理：

| 验收项 | 状态 | 证据 |
|---|---|---|
| 单源质量规则执行器、`PASSED`/`FAILED`/`SKIPPED` 终态与质量门触发器 | 已主验收 | `data/quality/`；`20260716_0004`；质量规则与发布回归 |
| 跨源比较器、字段级阈值、确定性 `report_id` 与双方绑定 | 已主验收 | `data/comparison/`；跨源比较回归 |
| 差异报告双方 batches/versions/quality_runs 绑定与触发器增强 | 已主验收 | `20260716_0005`；差异报告与 verifier 回归 |
| 备用源候选评估：合成证据不得标 `PRODUCTION_READY`，阻塞原因必填 | 已主验收 | `data/assessment/`；候选评估回归 |
| 默认生产矩阵：XTQuant PRIMARY，三个 FALLBACK `BLOCKED_EXTERNAL_VALIDATION`，无自动切换 | 已主验收 | `default_source_matrix()`；`SOURCE_MATRIX.md` |
| SourceOverride 创建/审批/拒绝/应用失败路径与审计事务 | 已主验收 | `data/override/`；R4～R6 workflow 回归 |
| `apply_override` 完整工作流、精确范围、双 SHA、恢复和终态冻结 | 已主验收 | `20260721_0008`、`20260722_0009`；R5/R6 回归 |
| 真实备用源（BAOSTOCK/AKSHARE/TUSHARE）外部验证 | 未授权 | 须在 S1-07 或专项验收逐项授权 |
| 默认门禁无网络、QMT、正式根和真实供应商访问 | 已主验收 | 离线守卫；未运行 `real_*` |

合成测试不构成真实备用源能力证明；真实 XTQuant 样本验证仍须逐项授权；合成数据不能标生产可用。
S1-04 主验收证据见 [独立验收报告](S1-04_ACCEPTANCE.md)。S1-05-R1 修复已提交，
等待主验收；S1-06～S1-08 未开始、未获授权。真实 XTQuant 和备用源验证仍延期并阻断
Stage 1 最终验收。

## S1-05 验收标准

S1-05-R1 修复已提交，等待主验收：

| 验收项 | 状态 | 证据 |
|---|---|---|
| 强类型 EOD 请求、确定性排序/规范 JSON/SHA 与固定 `data.eod.v1` | 待主验收 | `data/eod/contracts.py`；合同测试 |
| 同 key 同 manifests 复用、不同 manifests 冲突及并发单行创建 | 待主验收 | EOD command 和 SQLite 并发测试 |
| Worker claim 竞争、事务外 handler、节流 heartbeat/progress、取消和启动恢复 | 待主验收 | jobs/EOD Worker 回归 |
| ingestion→BUILDING→quality→显式 publish→严格凭证复验 | 待主验收 | `data/adapters/eod.py`；Fake Gateway 集成测试 |
| 显式 base 复验、完整快照主键合并、直接父版本和 NO_CHANGE | 待主验收 | 两次连续 EOD、基表凭证和空计划回归 |
| 正式 CLI/Web runtime 组合、模块/console 入口和 SDK 延迟加载 | 待主验收 | subprocess 启动级测试；`runtime.py` |
| 五类未验证 XTQuant 能力在 Gateway 前稳定失败 | 待主验收 | 五类参数化能力预检测试 |
| CLI/API 共用命令服务；POST 202、幂等 409、乐观取消和 SSE/GET 无回归 | 待主验收 | CLI 与 Web API 测试 |
| EOD 成功/失败证据严格文件集、规范 JSON、SHA/COMPLETE、不可覆盖和脱敏 | 待主验收 | EOD evidence 攻击回归 |
| 深层版本/质量/Warehouse verifier、发布 checkpoint、故障恢复和去重重试 | 待主验收 | R1 故障注入和重新签名攻击回归 |
| 默认门禁无网络、QMT、正式根、旧项目、外部备份和真实 SDK 访问 | 待主验收 | 离线守卫和完整质量门 |

## S1-06～S1-08 验收

按主验收 Agent 下发的顺序依次为 EOD/Worker/CLI/API、
数据中心 Web、经授权真实样本与旧数据判定、真实或明确范围 Canonical 构建
及 Stage 1 最终验收。S1-06～S1-08 未开始、未获授权；完成后填写，详见 [任务卡](TASKS.md)。
