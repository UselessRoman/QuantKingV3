# Stage 1 验收矩阵

状态：Stage 0 已完成；Stage 1 已获实施授权并进行中，但尚未完成。S1-01 已于
2026-07-16 在提交 `7e4e5b38b7fc91edaf8727ed55b1e26435373aea` 完成主验收，证据见
[S1-01 主验收证据](S1-01_ACCEPTANCE.md)。
S1-02 已于 2026-07-18 在提交 `79f9fa3e682e212cf74e7886a04c6fb52158f302`
完成主验收，证据见 [S1-02 主验收证据](S1-02_ACCEPTANCE.md)。S1-03～S1-08
未开始、未获授权。

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

## S1-03～S1-08 验收

按主验收 Agent 下发的顺序依次为 XTQuant、质量/跨源/override、EOD/Worker/CLI/API、
数据中心 Web、经授权真实样本与旧数据判定、真实或明确范围 Canonical 构建
及 Stage 1 最终验收。待逐项授权和完成后填写，详见 [任务卡](TASKS.md)。
