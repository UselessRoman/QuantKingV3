# Stage 2 文档入口

Stage 2 以首次可用优先，建设一个有界、point-in-time、可复算并能被 Qlib 稳定消费的研究数据
版本。阶段是否已获授权及当前进度只看 [路线图](../ROADMAP.md)。

## 文档职责

- [任务索引](TASKS.md)：四个任务的顺序、共同边界和后移项。
- [S2-01 任务卡](S2-01_TASK.md)：首次范围、来源资格与固定样本。
- [S2-01 实际验收](S2-01_ACCEPTANCE.md)：本地交付、检查结果与来源资格阻断。
- [S2-02 任务卡](S2-02_TASK.md)：point-in-time 研究输入。
- [S2-03 任务卡](S2-03_TASK.md)：动态股票池、周度主标签与 10 个基础因子。
- [S2-04 任务卡](S2-04_TASK.md)：Qlib 全量视图、最小检查页与 Stage 2 终验。
- [验收矩阵](ACCEPTANCE.md)：阶段门禁与证据要求。
- [研究与回测规范](../RESEARCH_AND_BACKTEST.md)：跨阶段研究语义、因子、标签、模型和回测原则。
- [实施计划](../IMPLEMENTATION_PLAN.md)：各 Stage 的顺序和完成定义。

## 阶段边界

- 只读取显式 `PUBLISHED` Stage 1 父版本，不读取可变路径或隐式 `latest`。
- `daily_bar_unadjusted` 保持不可变；复权和研究状态只在带父血缘的新研究版本中表达。
- 首次正式证券与日期范围必须明确且有界；未覆盖范围如实列出，不宣称全市场或全部历史。
- 日线继续执行 A/B/C 共识；生命周期、状态和复权按 ADR-029 的字段级来源政策验收。
- 当前列表、未来公司行动或供应商后续修订不得改变已发布历史结果。
- 状态缺失、来源冲突、复权不明或父版本覆盖不足必须隔离或先补发新父版本。
- 逻辑产物保留独立版本 ID，但共同复用现有 Job、manifest、原子发布和日志实现。
- 本阶段不训练模型、不接入 Alpha158、不建设 Qlib 增量构建、不建设完整因子诊断平台，也不
  扩展到 Stage 7 数据域。

## 主链

```text
Stage 1 DatasetVersion
  → ResearchInputVersion
  → UniverseVersion + 主 LabelVersion + 10 因子 FactorSetVersion
  → QlibViewVersion（全量重建）
  → 最小研究检查页与正式验收
```

任务细节只写在各任务卡；任务索引不复制实施细节，验收矩阵不记录执行历史。
