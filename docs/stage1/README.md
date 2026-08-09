# Stage 1：A 股日线数据仓

状态：S1-01～S1-06 已完成独立主验收；旧 S1-07 证据链路线已退役。S1-07K1-D1 两只东财证券各尝试一次、零重试，均为 52 upstream/41 Canonical、`complete=True`；结合 R1 腾讯/Sina 四组 complete，K1 功能验收通过。D1 后完整质量门 935 passed，全部固化门禁全绿。当前变更未提交且工作树不干净；K2 与 Stage 2 均未开始。

## 当前技术路线

Stage 1 只解决个人量化项目真正需要的问题：取得日线、保留原始数据、核对多个上游、隔离冲突并发布可复现版本。

- 统一接入库：AKShare。
- A 源：东财，默认主来源。
- B 源：腾讯，常态交叉校验来源。
- C 源：新浪，仅在 A/B 缺失、失败或分歧时参与第三方判定。
- 首期数据：沪深 A 股未复权日线 OHLCV 和成交额；北交所、退市证券和复权信息按缺口报告管理，不冒充已覆盖。
- Stage 1 生产 EOD 只编排 `daily_bar_unadjusted`；其他历史 schema 不进入默认运行时。
- 每个 Raw 批次同时记录 `provider=AKSHARE` 和真实 `upstream=EASTMONEY|TENCENT|SINA`。
- 不连接 QMT，不需要券商客户端、账号或密码，不调用任何交易接口。
- 不使用 Ed25519、signer allowlist、claim nonce、授权信封、sealed evidence 或多版本证据合同。
- 不保留 `SourceCandidateAssessment` 或 24 单元 source×dataset 矩阵；K2 用 `ResolutionDecision` 表达正常仲裁，不沿用 `SourceOverride` 审批状态机。

## 简化主链

```text
AKShare/东财(A) ─┐
                 ├─ 独立 Raw → 统一日线 schema → A/B 比较
AKShare/腾讯(B) ─┘                            │
                                              ├─ 一致：按优先级选源并发布候选
AKShare/新浪(C) ── 仅在 A/B 异常时采集 ────────┤
                                              └─ 无两源一致：隔离，不发布
```

允许按证券和交易日选择来源，但必须显式记录 `selected_upstream`、比较结果和原因；禁止无血缘的静默替换。

## 已保留成果

- [S1-01](S1-01_ACCEPTANCE.md)：数据合同、元数据与迁移基础。
- [S1-02](S1-02_ACCEPTANCE.md)：Raw/Canonical/Warehouse、原子发布和显式版本查询。
- [S1-03](S1-03_ACCEPTANCE.md)：历史 XTQuant 离线适配经验；生产代码已在 K1 离线实现中退役，不再代表目标路线。
- [S1-04](S1-04_ACCEPTANCE.md)：质量规则、跨源差异和显式 override。
- [S1-05](S1-05_ACCEPTANCE.md)：EOD、Worker、CLI/API、失败隔离和恢复。
- [S1-06](S1-06_ACCEPTANCE.md)：数据中心 Web。

旧 S1-07 的验收与实现文档仍可从 Git 历史审计，但不再放在当前文档树中，也不得作为后续任务的前置条件。

S1-01～S1-06 的验收文档是历史证据；其中出现的 XTQuant、备用源、`SourceOverride`、旧 EOD evidence 或深层恢复措辞只证明当时验收范围。与 [ADR-018](../DECISIONS.md#adr-018stage-1-控制面简化) 冲突时一律以当前决策和 K1/K2 任务卡为准。

## 接下来

当前停在 [S1-07K1](S1-07K1_TASK.md) 的 D1 功能验收通过交付点；精确结果见 [K1 实现交接](S1-07K1_IMPLEMENTATION_HANDOFF.md)。所有真实授权均已消耗，禁止重跑 marker；[S1-07K2](S1-07K2_TASK.md) 未自动获准，须等待用户明确授权。

状态和交接入口：

- [任务卡](TASKS.md)
- [验收矩阵](ACCEPTANCE.md)
- [简明交接](HANDOFF.md)
- [Stage 1 收口基线](STAGE1_COMPLETION_HANDOFF.md)
- [数据源矩阵](SOURCE_MATRIX.md)
- [数据合同](DATA_CONTRACTS.md)
- [质量合同](QUALITY_CONTRACTS.md)
