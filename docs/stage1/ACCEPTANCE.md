# Stage 1 验收矩阵

状态：Stage 0 已完成；Stage 1 已获实施授权并进行中，但尚未完成。S1-01 已于
2026-07-16 在提交 `7e4e5b38b7fc91edaf8727ed55b1e26435373aea` 完成主验收，证据见
[S1-01 主验收证据](S1-01_ACCEPTANCE.md)。
S1-02 已于 2026-07-18 在提交 `79f9fa3e682e212cf74e7886a04c6fb52158f302`
完成主验收，证据见 [S1-02 主验收证据](S1-02_ACCEPTANCE.md)。S1-03-R1 修复中/待主验收。
S1-04 已于 2026-07-22 在提交 `2f99491e9dc410acf85ca81c131a77cae08d0cc3`
完成主验收，证据见 [S1-04 主验收证据](S1-04_ACCEPTANCE.md)。
S1-05 已于 2026-07-27 在提交 `e580d6963e01b1301dc26f29ed57a0b5b4770a0f`
完成主验收，证据见 [S1-05 主验收证据](S1-05_ACCEPTANCE.md)。
S1-06 已于 2026-07-28 在提交 `413805074b3a82c2d2a090d2b73c854a238e2e19`
完成主验收，证据见 [S1-06 主验收证据](S1-06_ACCEPTANCE.md)。
S1-07A 已于 2026-07-29 在提交 `37512be531e15bbd5a698e6a227b0753781ef5e6`
完成主验收。该验收仅覆盖离线验证框架及其信任边界；24 个 source×dataset 单元仍为
BLOCKED_EXTERNAL_VALIDATION，真实运行数为 0。
S1-07B-FINAL-R1 修复已提交、等待主验收：权威核心样本 v4、首个 AUTHORITY_APPROVED
case（603377.SH ST）、reference bundle v3 证据链和按 case 放行运行时已定版；
17 个 purpose 仍是覆盖 backlog，不是 S1-07B 全局阻断门。
S1-07C/D、S1-08 尚未开始、未获授权。

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
S1-04 主验收证据见 [独立验收报告](S1-04_ACCEPTANCE.md)。S1-05 已完成主验收，
证据见 [S1-05 主验收证据](S1-05_ACCEPTANCE.md)。S1-06 已完成主验收，证据见
[S1-06 主验收证据](S1-06_ACCEPTANCE.md)；
S1-07A 已完成主验收；S1-07B-FINAL-R1 修复已提交、等待主验收。首个 ST case 已获得有限范围的
权威材料批准；其余 purpose 继续作为覆盖 backlog。S1-07C/D、S1-08 尚未开始、未获授权。
真实 XTQuant 和备用源验证仍阻断 Stage 1 最终验收。

## S1-05 验收标准

S1-05 已完成主验收：

| 验收项 | 状态 | 证据 |
|---|---|---|
| 强类型 EOD 请求、确定性排序/规范 JSON/SHA 与固定 `data.eod.v1` | 已主验收 | `data/eod/contracts.py`；合同测试 |
| 同 key 同 manifests 复用、不同 manifests 冲突及并发单行创建 | 已主验收 | EOD command 和 SQLite 并发测试 |
| Worker claim 竞争、事务外 handler、节流 heartbeat/progress、取消和启动恢复 | 已主验收 | jobs/EOD Worker 回归 |
| ingestion→BUILDING→quality→显式 publish→严格凭证复验 | 已主验收 | `data/adapters/eod.py`；Fake Gateway 集成测试 |
| 显式 base 复验、完整快照主键合并、直接父版本和 NO_CHANGE | 已主验收 | 两次连续 EOD、基表凭证和空计划回归 |
| CLI/API 精确 base resolver、租约恢复与 frozen-command 交叉绑定 | 已主验收 | 正式 runtime、真实 SQLite 并发和重新签名攻击回归 |
| 正式 CLI/Web runtime 组合、模块/console 入口和 SDK 延迟加载 | 已主验收 | subprocess 启动级测试；`runtime.py` |
| 五类未验证 XTQuant 能力在 Gateway 前稳定失败 | 已主验收 | 五类参数化能力预检测试 |
| CLI/API 共用命令服务；POST 202、幂等 409、乐观取消和 SSE/GET 无回归 | 已主验收 | CLI 与 Web API 测试 |
| EOD 成功/失败证据严格文件集、规范 JSON、SHA/COMPLETE、不可覆盖和脱敏 | 已主验收 | EOD evidence 攻击回归 |
| 深层版本/质量/Warehouse verifier、发布 checkpoint、故障恢复和去重重试 | 已主验收 | R1 故障注入和重新签名攻击回归 |
| JobRun 三类 manifest 终态绑定、全生命周期续租和最终提交故障去重恢复 | 已主验收 | R3 success/failure 攻击、租约竞争和 provisional recovery 回归 |
| 恢复证据幂等协调、failure completed/checkpoint 深层复验及终态前停止续租 | 已主验收 | R4 lineage/checkpoint 攻击、重复恢复和正式 Worker 入口回归 |
| 状态感知 completed verifier、no-change 历史 parent/input batch 保真及故障去重恢复 | 已主验收 | R5 root/child base、混合结果、父血缘攻击和正式 Worker 恢复回归 |
| 默认门禁无网络、QMT、正式根、旧项目、外部备份和真实 SDK 访问 | 已主验收 | 离线守卫和完整质量门 |

主验收的独立执行记录见 [S1-05 主验收证据](S1-05_ACCEPTANCE.md)。

## S1-06 验收标准

S1-06 已完成主验收：

| 验收项 | 状态 | 证据 |
|---|---|---|
| provider-neutral 数据中心查询服务与稳定、严格、可分页的只读 API | 已主验收 | 应用服务、ports、API 合同测试 |
| 来源能力、批次、版本、覆盖、质量、失败、差异、override 和 EOD Job 可追溯展示 | 已主验收 | API/React 集成测试 |
| 数据预览只接受显式 `PUBLISHED version_id`、allowlist 字段和有界过滤 | 已主验收 | 查询合同与篡改回归 |
| DifferenceReport 行级证据在展示前经严格 verifier 复验 | 已主验收 | verifier/API 攻击回归 |
| EOD 表单冻结精确 base version；入队保持 202 异步、幂等和乐观取消合同 | 已主验收 | Web/API 单测与 E2E |
| 加载、空、失败、阻断、分页、SSE 重连及响应式界面状态完整 | 已主验收 | Vitest 与 Playwright |
| API/UI 不泄露绝对路径、供应商 payload、请求机密、异常栈或任意 SQL 能力 | 已主验收 | 安全响应回归 |
| 默认测试无网络、QMT、真实 SDK、正式数据根、旧项目或外部备份访问 | 已主验收 | 离线守卫和完整质量门 |
| R1 精确报告/Override 血缘、查询指纹 Cursor、持久 assessment 证据和真实应用查询链 E2E | 已主验收 | R1 攻击回归、Vitest、真实临时 SQLite/Warehouse Playwright |
| R2 Override 双向状态矩阵与统一失败视图 SQL 精确总数 | 已主验收 | 状态图攻击回归、0/101/150 与混合实体聚合/分页回归 |

主验收的独立执行记录见 [S1-06 主验收证据](S1-06_ACCEPTANCE.md)。

## S1-07A/B 与后续任务

S1-07A 离线验证框架已完成主验收，证据见
[S1-07A 主验收证据](S1-07A_ACCEPTANCE.md)。S1-07B-FINAL-R1 修复已提交、等待主验收：
权威核心样本 v4、首个 AUTHORITY_APPROVED case（603377.SH ST）、reference bundle v3
和按 case 放行运行时已定版。17 个 purpose 仍是覆盖 backlog，不是 S1-07B 全局阻断门。
证据见 [S1-07B 最终实现交接](S1-07B_ACCEPTANCE.md)。
S1-07C/D、S1-08 尚未开始、未获授权，详见 [任务卡](TASKS.md)。
