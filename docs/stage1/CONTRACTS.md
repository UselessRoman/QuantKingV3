# Stage 1 冻结合同

本文是 Stage 1 交付后仍有效的数据、来源、质量和发布合同。历史任务、尝试与授权不在此重复，
统一由 [Stage 1 历史索引](README.md) 指向实施、验收和证据记录。

## 数据范围

- 生产数据集：`daily_bar_unadjusted`，schema `daily_bar_unadjusted.v1`。
- 物理主键：`(code, trade_date)`；代码格式如 `000001.SZ`、`600000.SH`。
- 核心字段：`code, trade_date, open, high, low, close, pre_close, volume, amount`。
- Canonical 成交量单位为股，成交额单位为人民币元。
- Raw 只追加；Canonical、Resolution 和 Warehouse 版本不可原地修改。
- Stage 1 不声明覆盖北交所、历史退市证券、复权、公司行动、历史 ST/停牌状态、基准指数或
  point-in-time 动态股票池。

## 生产来源

| 角色 | provider / upstream | 接口 | 调用 |
|---|---|---|---|
| A | BAOSTOCK / BAOSTOCK | `query_history_k_data_plus` | 每次正常任务 |
| B | AKSHARE / TENCENT | `stock_zh_a_hist_tx` | 每次正常任务 |
| C | AKSHARE / SINA | `stock_zh_a_daily` | A/B 异常的受影响范围 |

正式来源集合只有 A/B/C。东财、FTShare 与 XTQuant/QMT 已退役或拒绝；历史文件和代码仅用于
审计兼容，不得进入新 Job 的数据来源、指纹、Raw、Canonical、Warehouse 或发布候选。两个
AKShare upstream 必须分别记录，禁止合并或静默替换。

每个 Raw 批次记录 provider、upstream、endpoint、库版本、采集时间、目标与实际范围、请求
参数、row count、内容 SHA 和 Job。未知 AKShare 版本或未验证代码段稳定拒绝，不猜测单位。

## 标准化合同

- 目标窗口开始日前增加 15 个自然日 lookback，在完整未复权序列中派生 `pre_close`，再裁回目标窗口。
- BaoStock 和新浪成交量按股解释；腾讯仅按已冻结版本与代码段单位策略转换。
- Raw 数值保持原样；单位转换只发生在 Canonical。
- 空结果、网络失败、限流、schema 漂移和合法非交易日分别建模。

## 质量与仲裁

来源候选必须先通过 schema、类型、唯一性、排序、OHLC 关系、非负数值、范围、交易日历、逐证券
覆盖、新鲜度和空结果检查，才可参加比较。

当前阈值协议字符串保持为 `comparison-thresholds.daily-bar-unadjusted.k4.v1`，这是冻结的历史
兼容标识，不代表当前模块或功能名。阈值为：

| 字段 | 绝对容差 | 相对容差 |
|---|---:|---:|
| open/high/low/close/pre_close | 0 | 0 |
| volume/amount | 50 | 0 |

仲裁顺序：

1. A/B 合格且一致时选择 A，不调用 C。
2. A/B 任一失败、缺行或分歧时，只对受影响范围调用 C。
3. 只有实际持有同一主键行的来源才能形成值共识；至少两源一致才可选择。
4. 一致集合按 A、B、C 排序；单源行或无两源共识均隔离并阻止发布。
5. 绑定交易日历确认的整范围合法空可产生零行结果，但使用独立原因码。

DifferenceReport、Resolution、输入指纹和 verifier 必须绑定同一阈值版本并调用同一 Decimal/定点
比较实现。任何新阈值都必须建立新版本和可复算证据，不能修改既有协议值或证据文件。

## 发布、查询与恢复

- 只有质量通过且满足两源共识的候选可进入发布。
- 发布范围、预算、日历、来源、阈值和血缘由内容 SHA 绑定的 manifest 驱动。
- 发布过程使用临时目录、完整写入、校验、manifest、COMPLETE 与原子切换。
- 失败或中断不产生 `PUBLISHED` 半成品；重试创建新 Job。
- 相同输入指纹已有完整 `PUBLISHED` 版本时，复验后幂等复用。
- 查询和 DataCenter 只读取显式版本；逐证券 coverage、实际选源和隔离原因必须可审计。
- 发布 verifier 必须只读、失败关闭，并拒绝 XTQuant/QMT 血缘。

## 运行边界

- 业务、研究、组合和 Web 层不得直接导入数据供应商 SDK。
- 单个上游调用受端点预算、总预算和硬超时限制；应用层不自动重试。
- 新的真实网络、运行数据根读取/写入、外部备份或证据重放必须按任务单独授权。
- Stage 1 已结束的一次性正式发布和日历引导入口不属于日常 CLI。

## 后续阶段

Stage 2 只能读取显式 Stage 1 父版本，补足 point-in-time 生命周期、状态和复权合同；不得把
研究派生值写回 `daily_bar_unadjusted`。详见 [Stage 2 入口](../stage2/README.md)。
