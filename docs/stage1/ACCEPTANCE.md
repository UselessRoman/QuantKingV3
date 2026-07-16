# Stage 1 验收矩阵

状态：Stage 0 已完成；Stage 1 已获实施授权并进行中，但尚未完成。S1-01-R1 主验收未通过；S1-01-R2 修复中、待主验收；S1-02～S1-08 尚未开始。

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
| 框架无关数据领域合同与直接构造状态一致性 | R1 主验收未通过；R2 待主验收 | `src/quantking/data/models.py`；`tests/data/test_state_machines.py` |
| 严格 Canonical Arrow 合同、排序、OHLC/交易状态与显式空结果 | R1 主验收未通过；R2 待主验收 | `src/quantking/data/adapters/canonical_schema.py`；攻击回归测试 |
| DatasetVersion 覆盖、父版本与输入批次 FK 血缘 | R1 主验收未通过；R2 待主验收 | `20260716_0002`、`20260716_0003`；仓储集成测试 |
| QualityRun/QualityResult/DifferenceReport 完整仓储和查询 | R1 主验收未通过；R2 待主验收 | `tests/integration/data/test_data_repository.py` |
| SourceOverride 三方绑定、同数据集直接子版本与不可绕过审计事务 | R2 修复中、待主验收 | `20260716_0003`；数据库触发器、事务回滚测试 |
| 终态不可变与失败后相同参数新 batch ID 重试 | R1 主验收未通过；R2 待主验收 | 状态/仓储/迁移集成测试 |
| Stage 0 完整快照合同、维护期写边界、run ID 终身占用和迁移证据 | R2 修复中、待主验收 | `tests/integration/data/test_safe_migration.py` 攻击回归 |
| Alembic 新库 head 与 Stage 0→`20260716_0003` 升级 | R2 修复中、待主验收 | `tests/integration/data/test_migrations_stage1.py` |
| `scripts/quality.ps1` 退出码 0 | 待本修复运行及主验收复跑 | 离线结果不得描述成主验收通过 |
| 默认测试不访问网络/QMT/正式根 | 待本修复复核 | 测试必须只使用 `tmp_path` 和合成数据 |

S1-01-R1 主验收已经给出未通过结论。S1-01-R2 修复完成后仍须由主验收 Agent 复跑并逐项核对；
本地定向测试或 `scripts/quality.ps1` 通过均不得写成主验收通过，也不得据此开始 S1-02。

## S1-02～S1-08 验收

按主验收 Agent 下发的顺序依次为 Warehouse/发布/查询、XTQuant、质量/跨源/override、
EOD/Worker/CLI/API、数据中心 Web、经授权真实样本与旧数据判定、真实或明确范围 Canonical 构建
及 Stage 1 最终验收。待逐项授权和完成后填写，详见 [任务卡](TASKS.md)。
