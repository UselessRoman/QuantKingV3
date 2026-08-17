# Stage 1：A 股日线数据仓

状态：S1-01～S1-06 已主验收；K1 已提交 `f98f1d9`，K2 已提交 `0ebf84b`。S1-08 首次
AKShare 三源正式 Job 因量额零容差产生 80 个冲突并阻止发布。2026-08-12 已接受 BaoStock
常态供数、AKShare 三源复核/仲裁和旧 XTQuant 日线 `DATA_ONLY` 导入的新路线。K3A～K3D
离线实现已完成并提交；代表性四源实测三度在东财断连即停，根因定位为东财 IPv6 CDN 数据
断流（已退役 ADR-019），东财随后限流；FTShare 候选按 ADR-020 拒绝。ADR-021 确定
`BaoStock(A)+腾讯(B)+新浪(C)` 三角色路线。

S1-07K4 离线实现已完成：四角色运行时（东财 B、腾讯 C、新浪 D）已删除，三角色
`source-resolution.daily-bar-unadjusted.k4.v1` / `BAO-TX-SINA.k4.v1` 成为唯一新 Job 路线；
缺行仲裁、zero-row 原因码、共享 `ThresholdSet`、Canonical `pre_close` 恒派生、版本化交易日历
质量门和 K4D 三阶段授权/inventory/recheck 框架均已实现。K4C 第一次代表性真实三源实测曾于
2026-08-15 经独立验收不通过（S1-07K4C-R1，当时的历史事实）；此后 R1 修复、K4C-R2 真实实测
（36/36 成功、sufficient=false 如实记录）、K4C-R3（ADR-022 + Phase B 9/9）与 K4C-R4
（ADR-023）依次交付，**K4C-R4 已于 2026-08-16 独立验收通过**，
`comparison-thresholds.daily-bar-unadjusted.k4.v1` 已按用户决定接受重新冻结（ADR-024；
详见 [S1-07K4 实施记录](S1-07K4_IMPLEMENTATION.md)）。K4D inventory dry-run 已冻结，且
真实只读 inventory 已于 2026-08-16 获一次性授权执行成功（5,208 文件全部可读、零读取错误、
与 Stage 0 基线差异 0/0）；**K4D 真实 pilot 两次 attempt 分别于 2026-08-16/08-17 获授权
执行并失败即停（ADR-026；数据根零写入、授权已消费）**；**旧 XTQuant 日线迁移已按用户
决定拒绝并完成 K4D 决策收口（ADR-027）：第 3 次 pilot、全量诊断与 full 均不执行，
`XTQUANT_LEGACY` 不进入发布链**。**S1-07K4 已于 2026-08-17 经 K4E 独立验收通过并形成
单一 K4 提交（[验收记录](S1-07K4_ACCEPTANCE.md)）**；下一步是 S1-08R2。Stage 1
尚未完成，Stage 2 未开始。
2026-08-14
两份独立数据评审确认的发布前代码缺口已分别并入 S1-07K4 与 S1-08R2，不能由后续真实运行代替修复。

## 当前主链

```text
BaoStock A + 腾讯 B → 独立 Raw/质量 → 版本化比较
                              ├─ 一致：选择 A
                              └─ 异常：受影响范围调用新浪 C
旧 XTQuant X → DATA_ONLY 候选评估（inventory 成功 + 两次 pilot 失败）→ 迁移已拒绝（ADR-027）
任意路径无两个独立来源一致 → 隔离，不发布
```

唯一生产数据集是沪深 A 股未复权日线 `daily_bar_unadjusted.v1`。系统不连接 QMT、券商或
交易 SDK，不保留 source assessment、`SourceOverride` 或平行 EOD evidence 控制面。

## 当前文档

1. [当前合同](CONTRACTS.md)：数据、来源、质量、仲裁、发布和查询的唯一现行合同。
2. [S1-07K4 任务卡](S1-07K4_TASK.md)：三角色、仲裁/质量、代表性实测和旧日线迁移。
3. [S1-07K4 实施记录](S1-07K4_IMPLEMENTATION.md)：K4A/K4B 离线实现与 K4C/K4D 授权边界。
4. [S1-08R2 任务卡](S1-08R2_TASK.md)：发布引擎、存储恢复、manifest 和读侧离线收口。
5. [S1-08R3 任务卡](S1-08R3_TASK.md)：唯一正式 Job 与 Stage 1 最终验收。
6. [历史任务索引](S1-08_TASK.md)与 [K3 历史记录](S1-07K3_TASK.md)：只保存已执行事实。
7. [S1-07K4 验收记录](S1-07K4_ACCEPTANCE.md)：K4E 独立验收的不可变结论。
8. [验收记录](ACCEPTANCE.md)：历史结论与 Stage 1 最终完成条件。

已验收 K1/K2 的任务卡、实现交接以及重复的数据/质量/来源/收口说明已从当前文档树移除；
其内容由提交、验收记录和 Git 历史保留。历史验收中与 ADR-018 冲突的 XTQuant、override
或 evidence 措辞不再是现行合同。

## 下一步

按唯一顺序收口：

1. S1-07K4：**已完成——2026-08-17 K4E 独立验收通过并形成单一 K4 提交**
   （K4A/K4B 离线实现、K4C-R4 独立验收与 ADR-024 阈值冻结、K4D inventory 成功 +
   两次 pilot 失败即停 + ADR-027 迁移拒绝收口；详见
   [S1-07K4 验收记录](S1-07K4_ACCEPTANCE.md)）。
2. S1-08R2（下一步）：SQLite/幂等/恢复、manifest 参数化、verifier/DataCenter 和
   独立提交。
3. S1-08R3：冻结范围，另行授权唯一正式 Job，独立复验、双幂等、完整门禁和最终提交。

真实网络、旧根读取和正式根写入仍分别另行授权；不得清理失败证据或自动开始 Stage 2。
