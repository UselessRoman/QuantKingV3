# Stage 1 当前合同

本文是 Stage 1 当前接受的数据、质量、来源和发布合同入口。发生冲突时，仍以
`docs/DECISIONS.md`、`docs/IMPLEMENTATION_PLAN.md` 和 `docs/TECHNICAL_STANDARDS.md`
为准。S1-01～S1-06 的验收报告只证明历史交付范围，K1/K2 已接受的简化合同覆盖其中
冲突的 XTQuant、assessment、`SourceOverride` 和旧 EOD evidence 描述。
[S1-07K3](S1-07K3_TASK.md) 只记录 BaoStock 与旧数据受管迁移的历史离线基线；未完成的真实
实测和旧根迁移不得由合成证据替代。S1-07K4 已把四角色运行时重构为 ADR-021 三角色路线并
修复缺行仲裁、zero-row 原因码、共享 `ThresholdSet`、Canonical `pre_close` 恒派生和版本化
交易日历质量门（见 [S1-07K4 实施记录](S1-07K4_IMPLEMENTATION.md)）。K4C 第一次代表性真实
三源实测（2026-08-15）当时经独立验收不通过（S1-07K4C-R1，历史证据
[evidence/k4c_representative_probe_2026-08-15.json](evidence/k4c_representative_probe_2026-08-15.json)
保留）；R1 修复、R2/R3 真实实测与 R4 证据链修复依次交付后，**K4C-R4 已于 2026-08-16 独立
验收通过，K4 阈值/单位/路由合同据此接受重新冻结（ADR-024）**。
K4D 评估已完成并收口（ADR-027）：只读 inventory 已于 2026-08-16 获授权执行一次并成功
（5,208 文件、零读取错误、Stage 0 基线差异 0/0，ADR-025）；两次真实 pilot attempt 分别于
2026-08-16/08-17 获授权执行并失败即停（ADR-026，数据根零写入、授权已消费）；旧 XTQuant
迁移因复权语义不可信被用户决定**拒绝**，第 3 次 pilot、全量诊断与 full 均不执行，
`XTQUANT_LEGACY` 不进入发布链。[S1-08R2](S1-08R2_TASK.md)（发布引擎离线收口）已于
2026-08-17 独立只读验收通过（[验收记录](S1-08R2_ACCEPTANCE.md)）；
[S1-08R3](S1-08R3_TASK.md) 已于 2026-08-18 通过独立终验（
[最终验收记录](S1-08_ACCEPTANCE.md)），S1-08 与 Stage 1 完成。

## 范围

- 唯一生产数据集：`daily_bar_unadjusted`，schema 为 `daily_bar_unadjusted.v1`。
- Canonical 物理主键：`(code, trade_date)`，证券代码采用 `000001.SZ`、`600000.SH` 形式；
  任务/配置中的 `symbol` 只表示逻辑证券 scope。
- 核心字段：`code, trade_date, open, high, low, close, pre_close, volume, amount`。
- `volume` 的 Canonical 单位为股，`amount` 单位为人民币元。
- Raw 只追加；Canonical、Resolution 和 Warehouse 每次产生新版本；已发布版本不可原地修改。
- 北交所、退市证券、复权、公司行动、历史 ST/停牌状态、基准指数和 point-in-time
  动态股票池不属于 Stage 1 已覆盖能力，必须作为缺口报告保留。

## 目标来源合同与当前实现阻断

| 角色 | provider / upstream | 接口 | 调用时机 |
|---|---|---|---|
| A | BAOSTOCK / BAOSTOCK | `query_history_k_data_plus` | 正常 EOD 每次 |
| B | AKSHARE / TENCENT | `stock_zh_a_hist_tx` | 正常 EOD 每次 |
| C | AKSHARE / SINA | `stock_zh_a_daily` | A/B 异常时，仅受影响范围 |
| X | XTQUANT_LEGACY / XTQUANT | 旧导出文件（只读评估对象） | 迁移已拒绝（ADR-027），不进入发布链 |

上表由 ADR-021 冻结。S1-07K4 已把来源 registry、路由、预算、artifact/verifier、DataCenter、
CLI/Web 投影和测试重构为三角色：正常任务只计划 A/B；A/B 一致时 C 的 planned/actual 都为 0，
C 只按受影响证券/日期调用；AKShare 每次调用经受控子进程硬超时（60 秒），BaoStock 会话硬
超时 20 秒，应用层零重试，预算不足在首次调用前拒绝。东财和 FTShare 已退出正式来源集合；
东财 runtime/registry/预算和新 Job 指纹分支已删除，其历史 Raw、migration 和证据仍可读。
K4 路由冻结为 `source-resolution.daily-bar-unadjusted.k4.v1` + `BAO-TX-SINA.k4.v1`，
预算冻结为端点 12/4/0、HTTP 上界 20、BaoStock 协议消息上界 12。K4C 第一次代表性实测
（2026-08-15）经独立验收不通过（S1-07K4C-R1）；K4C-R2（36/36）、K4C-R3 Phase B（9/9）
与 K4C-R4（证据链修复，ADR-023）依次交付后，**K4C-R4 已于 2026-08-16 独立验收通过，
`comparison-thresholds.daily-bar-unadjusted.k4.v1`（OHLC/`pre_close` 零容差、量额绝对
容差 50 股/50 元、相对 0）已按用户决定接受重新冻结（ADR-024）**。单位与路由版本继续
保持冻结。历史证据文件中生成于待重新验收时期的
`thresholds_pending_reacceptance: true` 字段为字节冻结事实，不随规则状态改写。

每个 Raw/导入批次必须分别记录 provider、upstream、endpoint/导入格式、库版本、采集或导入
时间、目标范围、实际范围、请求参数或源文件 SHA、row count、内容 SHA 和 Job。不得把两个
AKShare upstream 合并为一个来源，也不得恢复 `PRIMARY/FALLBACK` 审批、source×dataset
能力矩阵或静默替换。BaoStock 包版本、返回单位和不复权语义由 K3 实测后冻结；未知版本或
字段合同稳定拒绝。

当前已验证单位证据版本为 `akshare-1.18.83-returned-units.v1`：新浪成交量为股；腾讯
`000xxx.SZ` 为手，其余已验证沪深白名单代码段为股；三者成交额均为元。Raw 数值原样保存，
Canonical 标准化层才把手乘 100。未知 AKShare 版本或代码段稳定拒绝，不猜单位。

东财 `eastmoney-ipv4-only.v1` 只属于已退役 ADR-019 的历史证据；S1-07K4 必须从正式 runtime、
registry 和新 Job 指纹删除，不能迁移到腾讯或新浪。

目标窗口开始日前固定增加 15 个自然日 lookback；所有服务源都在完整未复权上游序列中从
前一行 `close` 派生 Canonical `pre_close` 后再裁回目标窗口。供应商直出的参考昨收只保留
在 Raw，不得成为按列是否存在而切换的 Canonical 分支。Raw manifest 同时绑定 target/upstream 窗口和
`lookback_days=15`。空结果、网络失败、限流、schema 漂移和合法非交易日必须分开建模。

旧 XTQuant 文件曾按"先逐文件复制为受管不可变导入对象、源根前后只读指纹一致、转换层
明确记录原字段/单位/复权和日期语义、未确认或未复核保留为迁移缺口"的合同评估；两次
真实 pilot 证明其复权语义无法可靠满足 `daily_bar_unadjusted` 契约，迁移已按 ADR-027
拒绝：`XTQUANT_LEGACY` 不进入正式 Raw、staging、Canonical、Warehouse 或发布候选，
该导入合同不再有活跃执行对象（离线框架与历史证据保留为审计输入）。

## 质量与仲裁

每个来源的 Canonical 候选先独立检查：

- schema、类型、主键唯一和固定排序；
- `low <= open/close <= high`；
- 价格、成交量、成交额非负；
- 请求范围、带版本/SHA 的交易日历成员关系、逐交易所/证券预期交易日缺口、新鲜度、空结果
  语义、上游 schema 漂移和内容 SHA；
- 单源 `QualityRun` 必须为 `PASSED` 才能参与比较。

全局 `coverage_window` 只能验证边界，不能替代逐证券覆盖。停牌、未上市或无交易等例外必须
形成显式缺口/空结果决定；发布范围内不得存在未解释的共享漏日。阻断失败必须保留受影响计数、
分区/证券摘要和有界行级证据路径，不能只保留固定失败文案。

历史阈值 `comparison-thresholds.daily-bar-unadjusted.k1.v1` 对全部字段零容差，只绑定首次
失败 Job，不得修改，且继续可复验。第一次 K4C 实测（2026-06-11..2026-08-09，6 只代表性
证券 × BaoStock/腾讯/新浪）的 36 次调用全部成功、OHLC/`pre_close` 三源一致、量额差异为
腾讯 100 股/100 元舍入；但独立验收不通过（代表性覆盖未验证、attempt b 无证据、50 极值
不可复算）。**K4C-R4 已于 2026-08-16 独立验收通过，
`comparison-thresholds.daily-bar-unadjusted.k4.v1`（OHLC/`pre_close` 零容差、量额绝对
容差 50、相对 0）已按用户决定接受重新冻结（ADR-024）**：规则值不变、状态由待重新验收
转为已接受，验收记录为 `k4_threshold_acceptance()`（含四条 SHA 绑定证据引用）。容差使用
十进制定点比较、记录实际差值和阈值，并覆盖边界正负测试，不得通过修改 Raw 或 Canonical
舍入来制造一致。任何更大容差都必须来自新的代表性证据并通过离线 verifier 与独立验收。

1. EOD 的 A/B 都通过且一致时选择 A，C 零调用。
2. A/B 任一调用失败、质量失败、缺行或字段分歧时，只为受影响证券/日期范围调用 C。
3. 任意两个独立来源一致即可形成决定；在一致集合内按 A、B、C 选择。
4. 历史导入先比较 X 与 A；一致时可选择 X。分歧时按 B、C 仲裁，未获两源支持的
   X 行不得发布。
5. 无两源一致时写冲突并阻止发布；不得制造不存在的交易日或伪造缺失行。
6. 两个来源都实际持有同一主键行时才能参与字段一致比较；若任一来源持有行，其他两个来源
   的共同缺行不构成该行共识，恰好一个来源持有行时必须继续取证或隔离。
7. 经版本化交易日历确认的多方整范围合法空结果可以形成零行结果，但必须使用独立原因码；
   调用/质量失败或逐行缺失不能冒充合法空或 `AB/AC/..._AGREE_*_SELECTED`。

DifferenceReport 与 Resolution 必须调用同一版本化 `ThresholdSet` 和 Decimal/定点比较函数
（`thresholds_for` 注册表与 `rows_within_threshold` 是唯一实现）。阈值版本同时进入比较、
决定、artifact、输入指纹和 verifier；不得一层按容差判断、另一层按 float 原值 `==`。
激活新版本时必须原子更新版本白名单/registry，而不是保留只接受 K1 的散落硬编码。

仲裁逐行只允许实际持有行的来源参与值一致：共同缺行不构成值共识；恰好一个实际来源持有行时
该行以 `SINGLE_SOURCE_ROW_ISOLATED` 隔离；两个实际行分歧时写 `NO_TWO_SOURCE_CONSENSUS`；
两者都是 conflict 并阻止发布。经版本化交易日历确认的多方合法整范围空结果产生
`ZERO_ROW_LEGAL_EMPTY_CONSENSUS` 的 run 级原因码，绝不使用任何 `*_AGREE_*_SELECTED`。
Canonical `pre_close` 对全部服务源统一由完整未复权序列的前一行 `close` 派生，vendor 参考
昨收只保留 Raw；`pre_close_consistency`、带版本/SHA 绑定的 `calendar_membership`、
逐证券 `expected_trading_days`（含冻结的上市/停牌例外）与 `freshness` 规则进入 K4 质量门
（`quality-rules.daily-bar-unadjusted.k4.v1`）。

不可变 `ResolutionRun`/`ResolutionDecision` 使用版本级默认摘要加行级例外和冲突，绑定
Job、输入指纹、全部实际 Raw/import batch/content SHA、DifferenceReport、规则版本和 C
请求范围。
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

合法空版本的重复发布必须收敛到同一 manifest；Web/Worker 或多 Worker 边界下，同一
`version_id` 必须通过进程间单发布者约束或等价锁/协调机制幂等收敛。SQLite 短写事务对
`SQLITE_BUSY/SQLITE_LOCKED` 执行有界退避和抖动；受管导入硬中断残留必须可由显式恢复流程
判定并重跑，不得永久占用内容幂等键。增量基覆盖按真实连续段/显式缺口表达，不能把每证券
`min..max` 自动展开成连续已覆盖区间。

DataCenter 只接受显式 `version_id`，不接受 `latest`、任意路径或 SQL。版本、批次、质量、
DifferenceReport、resolution 例外/冲突和预览均使用有界分页；浏览器不接收绝对路径、
provider payload、异常栈或完整全市场 DataFrame。R2 的证券集合 SHA、逐证券 coverage、分块、
调用预算和血缘边界由冻结 manifest 派生，不得继续绑定首次两证券常量；数据中心展示实际
来源健康、新鲜度、迁移/缺口计数和脱敏失败证据。查询期深层 SHA 复验的性能架构仍按 ADR-018
另行评审，但当前 R2 明确范围必须通过有界查询烟测。

## 个人本地信任模型与正式入口边界（ADR-028 / S1-08R3F）

- QuantKing V3 是单用户本机个人软件：本地用户与其启动的开发 Agent 属同一管理边界；
  授权机制只做防误执行、防范围扩大、防同任务重放与可审计留痕，不防御本机管理员
  或恶意本地进程，不引入 PKI/签名服务/HSM/密钥管理或多层 grant 链。
- 内容 SHA（envelope/plan/manifest）是完整性与漂移检测，不是身份认证。
- E1 正式入口 token 为 `<marker>:<authorization_id>:<envelope_sha256>`：token 第三段
  必须等于重算的 envelope 内容 SHA；入口同时校验写入面模板全等、日历来源白名单、
  声明数据根与 attempts root 边界，全部先于 runtime 构造与 provider 调用。
- C1 日历引导 plan 必须通过强类型全字段校验（拒绝未知字段），token 同样绑定
  `<plan_sha256>`。
- 未来 R3C 类探针必须先消费简单一次性 attempt ledger（绑定 marker/授权 ID/plan
  SHA/幂等键/开始时间），同 ID 重放与 FAILED 复用即拒；旧 R3C 证据字节不变。
- 正式发布版本的逐键证明：每个 `(code, trade_date)` 必须有 A/B 两个规范化实际来源
  行、K4 阈值重算通过（OHLC/`pre_close` 零容差、量额绝对 50/相对 0）、DifferenceReport
  可由 A/B 值重算、PUBLISHED 行逐字段等于 selected source、零 `XTQUANT_LEGACY`
  血缘；证据只需保存与该发布直接相关的 artifact SHA 与逐键证明，不保存全根
  manifest。
- `E:\QuantKingV3Data` 是公开声明的标准本地数据根：允许出现在内部任务、授权、
  证据与验收文档中；Web、DataCenter API 与最终用户响应仍不得返回本机绝对路径。

## 阶段边界

Stage 1 只证明一个明确范围的真实未复权日线版本可取得、核对、发布和查询，不证明数据已
满足研究用复权、point-in-time 股票池或无幸存者偏差要求。Stage 1 已完成，Stage 2 前置已
满足；但 Stage 2 实施仍未获得用户单独授权、尚未开始。
