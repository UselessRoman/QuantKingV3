# Stage 1：A 股日线数据仓

状态：S1-01～S1-06 已完成独立主验收；旧 S1-07 证据链路线已于 2026-08-08 退役并从当前实现删除。Stage 1 尚未完成，Stage 2 未开始。

## 当前技术路线

Stage 1 只解决个人量化项目真正需要的问题：取得日线、保留原始数据、核对多个上游、隔离冲突并发布可复现版本。

- 统一接入库：AKShare。
- A 源：东财，默认主来源。
- B 源：腾讯，常态交叉校验来源。
- C 源：新浪，仅在 A/B 缺失、失败或分歧时参与第三方判定。
- 首期数据：沪深 A 股未复权日线 OHLCV 和成交额；北交所、退市证券和复权信息按缺口报告管理，不冒充已覆盖。
- 每个 Raw 批次同时记录 `provider=AKSHARE` 和真实 `upstream=EASTMONEY|TENCENT|SINA`。
- 不连接 QMT，不需要券商客户端、账号或密码，不调用任何交易接口。
- 不使用 Ed25519、signer allowlist、claim nonce、授权信封、sealed evidence 或多版本证据合同。

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
- [S1-03](S1-03_ACCEPTANCE.md)：历史 XTQuant 离线适配经验；代码将在 AKShare 替换任务中退役，不再代表目标路线。
- [S1-04](S1-04_ACCEPTANCE.md)：质量规则、跨源差异和显式 override。
- [S1-05](S1-05_ACCEPTANCE.md)：EOD、Worker、CLI/API、失败隔离和恢复。
- [S1-06](S1-06_ACCEPTANCE.md)：数据中心 Web。

旧 S1-07 的验收与实现文档仍可从 Git 历史审计，但不再放在当前文档树中，也不得作为后续任务的前置条件。

## 接下来

唯一下一任务是 [S1-07K1：AKShare 三渠道适配与小样本实测](S1-07K1_TASK.md)。完成并独立验收 K1 后，才进入 K2 的三渠道仲裁与生产 EOD，最后由 S1-08 发布首个明确范围版本并完成 Stage 1 验收。

状态和交接入口：

- [任务卡](TASKS.md)
- [验收矩阵](ACCEPTANCE.md)
- [简明交接](HANDOFF.md)
- [Stage 1 收口基线](STAGE1_COMPLETION_HANDOFF.md)
- [数据源矩阵](SOURCE_MATRIX.md)
- [数据合同](DATA_CONTRACTS.md)
- [质量合同](QUALITY_CONTRACTS.md)
