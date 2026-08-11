# Stage 1：A 股日线数据仓

状态：S1-01～S1-06 已主验收；K1 已提交 `f98f1d9`，K2 已提交 `0ebf84b`，两者均通过
独立主验收。S1-08 任务已设计但未授权执行，因此 Stage 1 尚未完成，Stage 2 未开始。

## 当前主链

```text
东财 A + 腾讯 B → 独立 Raw/质量 → 零容差比较
                           ├─ 一致：选择 A
                           └─ 异常：仅受影响范围调用新浪 C
                                      ├─ 任意两源一致：显式 ResolutionDecision
                                      └─ 无两源一致：隔离，不发布
```

唯一生产数据集是沪深 A 股未复权日线 `daily_bar_unadjusted.v1`。系统不连接 QMT、券商或
交易 SDK，不保留 source assessment、`SourceOverride` 或平行 EOD evidence 控制面。

## 当前文档

1. [当前合同](CONTRACTS.md)：数据、来源、质量、仲裁、发布和查询的唯一现行合同。
2. [S1-08 任务卡](S1-08_TASK.md)：下一步固定范围、网络预算、正式写入和验收指令。
3. [验收记录](ACCEPTANCE.md)：S1-01～K2 的结论与 Stage 1 最终完成条件。
4. `S1-01_ACCEPTANCE.md`～`S1-06_ACCEPTANCE.md`：已完成任务的详细历史证据。

已验收 K1/K2 的任务卡、实现交接以及重复的数据/质量/来源/收口说明已从当前文档树移除；
其内容由提交、验收记录和 Git 历史保留。历史验收中与 ADR-018 冲突的 XTQuant、override
或 evidence 措辞不再是现行合同。

## 下一步

必须先由用户按 [S1-08 任务卡](S1-08_TASK.md) 明确授权网络范围和
`E:\QuantKingV3Data` 写入。不得重跑 K1 探针、清理既有验收证据、扩大范围或自动开始
Stage 2。
