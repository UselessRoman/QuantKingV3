# S1-01 主验收证据

- 任务：S1-01 数据合同、元数据持久化与安全迁移基础。
- 主验收提交：`7e4e5b38b7fc91edaf8727ed55b1e26435373aea`。
- 主验收日期：2026-07-16。
- 后续任务：S1-02 已获实施授权；S1-03～S1-08 尚未开始。

## 主验收 Agent 结论

S1-01 主验收 Agent 复验后逐项核对完毕，确认 S1-01 主验收通过。本文件固化当时提供的离线证据
以便 Stage 1 后续任务与最终验收回溯；任何描述“已通过”的措辞必须以本文件为准。

## 离线证据

- R2 定向测试：`pytest tests/data tests/integration/data -q -p no:cacheprovider` 报告
  54 passed。
- Stage 1 范围测试：相同命令扩展至 Stage 1 触达模块，147 passed。
- `scripts/quality.ps1`：`360 passed, 3 deselected`，无失败、无网络/QMT/正式根访问。
- Alembic head：`20260716_0003`，新库从空文件直接 upgrade 至 head 与 Stage 0 既有库
  升级至 `20260716_0003` 均通过 `tests/integration/data/test_migrations_stage1.py` 验证。
- 无外部访问：测试仅使用 `tmp_path` 与合成数据，未访问 `E:\QuantKingV3Data`、
  `E:\QuantKing` 或 `F:\QuantKingV3Backups`；亦未读取 XTQuant/QMT/真实网络。

## 任务范围确认

S1-01 提供：

- 严格 Canonical Arrow 合同、显式空结果分类与全局排序/主键校验。
- DatasetVersion 状态机、父版本与输入批次 FK 血缘。
- QualityRun/QualityResult/DifferenceReport 完整仓储与查询。
- SourceOverride 三方绑定、同数据集直接子版本与不可绕过审计事务。
- 终态不可变与失败后必须使用新 batch ID 重试。
- Stage 0 完整快照合同、维护期写边界、run ID 终身占用和迁移证据。
- Alembic 新库 head 与 Stage 0→`20260716_0003` 升级。

## 重要边界

S1-01 主验收通过**不**等同于 Stage 1 整体验收通过。后续任务仍按主验收顺序推进：

- S1-02：Warehouse、原子发布与显式版本查询。
- S1-03：XTQuant 主数据适配器与增量抓取。
- S1-04：质量、跨源差异与 SourceOverride 应用。
- S1-05：EOD、Worker、CLI 与 API。
- S1-06：数据中心 Web。
- S1-07：经授权真实样本与旧数据 `DATA_ONLY` 判定。
- S1-08：真实或明确范围 Canonical 构建及 Stage 1 最终验收。

## 重新运行证据

任何后续任务开始前或完成后均可重新运行以下命令以确认 S1-01 离线门禁未被破坏：

```powershell
.venv\Scripts\python.exe -m pytest tests\data tests\integration\data -q -p no:cacheprovider
.\scripts\quality.ps1
```

任何测试失败或外部访问证据均意味着 S1-01 必须重新接受主验收。
