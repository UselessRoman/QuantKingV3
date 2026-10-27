# Stage 1 文档索引

Stage 1 已完成。本目录是冻结合同、任务记录、验收报告和不可变证据的历史索引，不承担项目当前
状态播报；当前状态见 [路线图](../ROADMAP.md)。

## 当前有效内容

- [冻结合同](CONTRACTS.md)：交付后仍有效的数据、来源、质量、发布和运行边界。
- [全阶段最终验收](S1-08_ACCEPTANCE.md)：Stage 1 最终结论。
- [项目级数据架构](../DATA_ARCHITECTURE.md)：跨阶段稳定数据链与研究前置。

## 任务与验收记录

| 范围 | 任务/实施 | 验收 |
|---|---|---|
| S1-01～S1-06 基础数据链 | [总任务](../stage1/ACCEPTANCE.md) | [S1-01](S1-01_ACCEPTANCE.md) · [S1-02](S1-02_ACCEPTANCE.md) · [S1-03](S1-03_ACCEPTANCE.md) · [S1-04](S1-04_ACCEPTANCE.md) · [S1-05](S1-05_ACCEPTANCE.md) · [S1-06](S1-06_ACCEPTANCE.md) |
| S1-07 数据源与仲裁收口 | [K3 任务](S1-07K3_TASK.md) · [K4 任务](S1-07K4_TASK.md) · [K4 实施](S1-07K4_IMPLEMENTATION.md) | [FTShare 资格结论](S1-07K3_FTSHARE_VALIDATION.md) · [K4 验收](S1-07K4_ACCEPTANCE.md) |
| S1-08 发布链收口 | [总任务](S1-08_TASK.md) · [R2 任务](S1-08R2_TASK.md) · [R2 实施](S1-08R2_IMPLEMENTATION.md) · [R3 任务](S1-08R3_TASK.md) · [R3 实施](S1-08R3_IMPLEMENTATION.md) | [R2 验收](S1-08R2_ACCEPTANCE.md) · [最终验收](S1-08_ACCEPTANCE.md) |

上述文件描述其发生时的计划、失败、修复和授权状态。诸如“待授权”“下一步”“阻断中”的语句
只能按文档日期理解，不能覆盖路线图、决策登记簿或当前冻结合同。

## 证据

[evidence/README.md](evidence/README.md) 是证据清单。JSON、JSONL、attempt 目录、协议字符串和
内容 SHA 属于可复验历史，不因当前代码命名整理而改写。

历史真实网络授权均已消费，不可重放。任何新网络、运行数据根、旧项目或外部写入都必须按新任务
单独授权。
