# Stage 1：A 股日线数据仓

状态：S1-01～S1-06 已主验收；K1 已提交 `f98f1d9`，K2 已提交 `0ebf84b`。S1-08 首次
AKShare 三源正式 Job 因量额零容差产生 80 个冲突并阻止发布。2026-08-12 已接受 BaoStock
常态供数、AKShare 三源复核/仲裁和旧 XTQuant 日线 `DATA_ONLY` 导入的新路线。K3A～K3D
离线实现已完成并提交；2026-08-13 代表性四源实测三度在东财断连即停，最小连通性探针证明
仅东财 HTTP 层被断（腾讯/新浪正常），根因定位为东财 IPv6 CDN 数据断流并按 ADR-019 把
东财适配器钉死 IPv4，随后东财对本出口 IP 限流；旧根实际迁移未授权，因此 Stage 1 尚未完成，
Stage 2 未开始。

## 当前主链

```text
BaoStock A + 东财 B → 独立 Raw/质量 → 版本化比较
                              ├─ 一致：选择 A
                              └─ 异常：受影响范围调用腾讯 C
                                         └─ 仍无共识才调用新浪 D
旧 XTQuant X → 受管 DATA_ONLY 导入 → 先与 BaoStock 复核 → 同一 ResolutionDecision
任意路径无两个独立来源一致 → 隔离，不发布
```

唯一生产数据集是沪深 A 股未复权日线 `daily_bar_unadjusted.v1`。系统不连接 QMT、券商或
交易 SDK，不保留 source assessment、`SourceOverride` 或平行 EOD evidence 控制面。

## 当前文档

1. [当前合同](CONTRACTS.md)：数据、来源、质量、仲裁、发布和查询的唯一现行合同。
2. [S1-07K3 任务卡](S1-07K3_TASK.md)：BaoStock、四源仲裁、比较重基线和旧日线迁移。
3. [S1-08 任务卡](S1-08_TASK.md)：首次失败事实、K3 前置和后续正式发布/终验。
4. [验收记录](ACCEPTANCE.md)：S1-01～K2 的结论与 Stage 1 最终完成条件。
5. `S1-01_ACCEPTANCE.md`～`S1-06_ACCEPTANCE.md`：已完成任务的详细历史证据。

已验收 K1/K2 的任务卡、实现交接以及重复的数据/质量/来源/收口说明已从当前文档树移除；
其内容由提交、验收记录和 Git 历史保留。历史验收中与 ADR-018 冲突的 XTQuant、override
或 evidence 措辞不再是现行合同。

## 下一步

先冷却东财对本出口 IP 的限流窗口，再完成 K3 的代表性四源实测（每次真实执行需另行授权，
失败即停、零重试）；随后完成旧根实际 inventory/pilot/full 迁移。当前零容差继续生效，
不得用离线合成证据激活 K3 候选阈值。真实网络、旧根读取和正式根写入分别另行授权。
K3 独立验收提交后才能执行 S1-08R2；不得清理首次失败证据或自动开始 Stage 2。
