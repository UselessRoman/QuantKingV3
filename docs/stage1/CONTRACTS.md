# Stage 1 当前合同

本文是 Stage 1 当前数据、质量、来源和发布合同的唯一入口。发生冲突时，仍以
`docs/DECISIONS.md`、`docs/IMPLEMENTATION_PLAN.md` 和 `docs/TECHNICAL_STANDARDS.md`
为准。S1-01～S1-06 的验收报告只证明历史交付范围，K1/K2 已接受的简化合同覆盖其中
冲突的 XTQuant、assessment、`SourceOverride` 和旧 EOD evidence 描述。

## 范围

- 唯一生产数据集：`daily_bar_unadjusted`，schema 为 `daily_bar_unadjusted.v1`。
- 主键：`(symbol, trade_date)`，证券代码采用 `000001.SZ`、`600000.SH` 形式。
- 核心字段：`symbol, trade_date, open, high, low, close, pre_close, volume, amount`。
- `volume` 的 Canonical 单位为股，`amount` 单位为人民币元。
- Raw 只追加；Canonical、Resolution 和 Warehouse 每次产生新版本；已发布版本不可原地修改。
- 北交所、退市证券、复权、公司行动、历史 ST/停牌状态、基准指数和 point-in-time
  动态股票池不属于 Stage 1 已覆盖能力，必须作为缺口报告保留。

## 固定来源

| 角色 | provider | upstream | AKShare 1.18.83 endpoint | 调用时机 |
|---|---|---|---|---|
| A | AKSHARE | EASTMONEY | `stock_zh_a_hist` | 每次 |
| B | AKSHARE | TENCENT | `stock_zh_a_hist_tx` | 每次 |
| C | AKSHARE | SINA | `stock_zh_a_daily` | 仅 A/B 异常 |

每个 Raw 批次必须分别记录 provider、upstream、endpoint、AKShare 版本、采集时间、目标范围、
实际上游范围、请求参数、row count、内容 SHA 和 Job。不得把三个 upstream 合并为一个来源，
也不得恢复 `PRIMARY/FALLBACK` 审批、source×dataset 能力矩阵或静默替换。

冻结单位策略为 `akshare-1.18.83-returned-units.v1`：东财成交量为手，新浪为股；腾讯
`000xxx.SZ` 为手，其余已验证沪深白名单代码段为股；三者成交额均为元。Raw 数值原样保存，
Canonical 标准化层才把手乘 100。未知 AKShare 版本或代码段稳定拒绝，不猜单位。

目标窗口开始日前固定增加 15 个自然日 lookback；在完整上游序列中从前一日 close 派生
`pre_close` 后再裁回目标窗口。Raw manifest 同时绑定 target/upstream 窗口和
`lookback_days=15`。空结果、网络失败、限流、schema 漂移和合法非交易日必须分开建模。

## 质量与仲裁

每个 upstream 的 Canonical 候选先独立检查：

- schema、类型、主键唯一和固定排序；
- `low <= open/close <= high`；
- 价格、成交量、成交额非负；
- 请求范围、日期覆盖、空结果语义和内容 SHA；
- 单源 `QualityRun` 必须为 `PASSED` 才能参与比较。

跨源阈值固定为 `comparison-thresholds.daily-bar-unadjusted.k1.v1`；OHLC、`pre_close`、
成交量和成交额的 absolute/relative 容差均为 0。

1. A/B 都通过且逐行一致时选择 A，C 零调用。
2. A/B 任一调用失败、质量失败、缺行或字段分歧时，只为受影响证券/日期范围调用 C。
3. AB、AC、BC 任意两源一致即可形成决定；在一致集合内按 A、B、C 选择。
4. 无两源一致时写冲突并阻止发布；不得制造不存在的交易日或伪造缺失行。
5. 合法三方空结果可以形成零行共识；调用/质量失败不能冒充合法空。

不可变 `ResolutionRun`/`ResolutionDecision` 使用版本级默认摘要加行级例外和冲突，绑定
Job、输入指纹、全部实际 Raw batch/content SHA、DifferenceReport、规则版本和 C 请求范围。
SQLite 只保存摘要、相对路径、SHA 和计数；行级 artifact 使用规范 JSON Lines。

## 发布、恢复与查询

最终 `DatasetVersion` 只有一个候选，不发布中间单源版本。发布顺序为：

```text
Raw → 单源质量 → Resolution/Difference → 最终质量
    → manifest + COMPLETE → 原子发布 PUBLISHED
```

Warehouse manifest 必须绑定实际 Raw 批次、resolution artifact 和每份 DifferenceReport 的
ID、路径与 manifest SHA。质量失败、冲突、artifact 不完整或中断均不得发布半成品。

相同输入指纹只复用通过 Raw、Difference、Resolution、Warehouse manifest、`COMPLETE` 和
数据库交叉验证的 `PUBLISHED` 版本。中断 Job 标记 `INTERRUPTED`；重试创建新 Job 并保留
`retry_of_job_id`，不恢复旧 success/failure evidence、forensic 双终态或多层 checkpoint。

DataCenter 只接受显式 `version_id`，不接受 `latest`、任意路径或 SQL。版本、批次、质量、
DifferenceReport、resolution 例外/冲突和预览均使用有界分页；浏览器不接收绝对路径、
provider payload、异常栈或完整全市场 DataFrame。

## 阶段边界

Stage 1 只证明一个明确范围的真实未复权日线版本可取得、核对、发布和查询，不证明数据已
满足研究用复权、point-in-time 股票池或无幸存者偏差要求。S1-08 完成前 Stage 1 仍未完成；
Stage 2 不得提前开始。
