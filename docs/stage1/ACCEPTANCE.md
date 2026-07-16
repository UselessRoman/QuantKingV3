# Stage 1 验收矩阵

状态：Stage 0 已完成；Stage 1 已获实施授权并进行中，但尚未完成。S1-01 已实现并离线测试通过，等待主验收；S1-02～S1-08 验收待对应任务完成后进行。

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
| 框架无关数据领域合同与状态机 | 已实现 | `src/quantking/data/models.py`；`tests/data/test_state_machines.py` |
| 六类 Canonical 可执行 schema 与 PyArrow 验证 | 已实现 | `src/quantking/data/schemas.py`、`src/quantking/data/adapters/canonical_schema.py`；`tests/data/adapters/test_canonical_schema.py` |
| Stage 1 元数据持久化（ORM、Alembic revision、仓储、不可变触发器） | 已实现 | `src/quantking/data/adapters/persistence/`；`migrations/versions/20260716_0001_stage1_data_metadata.py`；`tests/integration/data/test_data_repository.py`、`tests/integration/data/test_migrations_stage1.py` |
| 既有 SQLite 安全迁移入口（快照、隔离验证、失败保留） | 已实现 | `src/quantking/data/adapters/persistence/maintenance.py`；`tests/integration/data/test_safe_migration.py` |
| published/approved 数据不可变（DB 层 + 服务层） | 已实现 | 迁移触发器 + 仓储守卫；不可变测试 |
| SourceOverride 非法转换与审计 | 已实现 | 状态机 + 仓储审计测试 |
| ORM 与领域对象分离 | 已实现 | `tests/integration/data/test_data_repository.py::TestOrmDomainSeparation` |
| Alembic 新库 head 与 Stage 0→Stage 1 升级 | 已实现 | `tests/integration/data/test_migrations_stage1.py` |
| 架构边界测试 | 已实现 | `scripts/check_architecture_boundaries.py` 退出码 0 |
| Markdown 链接测试 | 已实现 | `scripts/check_markdown_links.py` + `tests/docs/test_stage1_status.py` |
| `scripts/quality.ps1` 退出码 0 | 待主验收复跑 | 离线开发环境已通过 |
| 默认测试不访问网络/QMT/正式根 | 已验证 | `tests/conftest.py` 守卫；新测试仅用 `tmp_path` |

S1-01 验收由主验收 Agent 复跑 `scripts/quality.ps1` 并核对授权边界后确认。本表在主验收通过前不得标记为“已验收”。

## S1-02～S1-08 验收

待对应任务完成后填写。各任务验收标准见 [任务卡](TASKS.md)。
