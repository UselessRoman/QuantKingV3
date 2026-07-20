# Stage 1 / S1-03 离线验收证据

## 结论与范围

- 验收日期：2026-07-19（离线/防御性合同确认）。
- 结论：S1-03 XTQuant 主数据适配器与增量抓取的离线/防御性合同已确认验收；真实 XTQuant
  样本、QMT 或网络能力没有在本任务中验证，仍须在 S1-07 或专项验收中按项单独授权后复验。
- 主验收状态：S1-03-R1 修复中/待主验收；离线测试通过不等于主验收通过。
- 用户已例外授权 S1-04 离线推进，但 S1-03 真实能力验证仍须逐项授权。
- S1-04 离线实现已提交、待主验收。

## 账号与凭据边界

- XTQuant/QMT 账号、密码或凭据不得进入 Agent、Codex 或 QuantKing 仓库；任何真实连接
  须由用户在本机直接操作，或经主验收 Agent 单独授权并在仓库外完成。
- 本任务的离线测试只使用 pytest 临时目录、Fake Gateway 和合成响应；不加载 XTQuant、
  不连接 QMT、不访问网络、不读取真实行情或账户。

## 离线/防御性合同验收

| 验收项 | 状态 | 证据 |
|---|---|---|
| XTQuant 依赖只在适配器边界，生产加载延迟且仅允许 `xtquant.xtdata` | 已验收 | `sources/xtquant.py`；边界测试 |
| 六类合成响应映射到精确 Canonical schema、排序、主键和语义验证 | 已验收 | `test_xtquant_mapping.py` |
| 无法证实的 point-in-time、单位、因子和历史状态能力安全失败 | 已验收 | 能力矩阵；`test_xtquant_adapter.py` |
| 显式覆盖、回看、缺口与稳定分块的 provider-neutral 增量计划 | 已验收 | `test_incremental_fetch_plan.py` |
| Raw manifest/SHA/COMPLETE、失败证据及 `IngestionBatch` 终态 | 已验收 | `test_xtquant_ingestion.py` |
| Raw 请求信封、真实 row_count、落地后重建及 Gateway/Raw records 一致性 | 已验收 | S1-03-R1 绑定与篡改回归 |
| chunk/plan 直接构造、缺失/重复/错序和请求 SHA 复验 | 已验收 | S1-03-R1 计划攻击回归 |
| 合法空、意外空、部分响应与稳定脱敏错误严格区分 | 已验收 | 适配器与集成回归 |
| 不创建或发布任何 Canonical `DatasetVersion` | 已验收 | 集成测试查询版本表为零行 |
| 默认门禁无网络、QMT、正式根和真实 XTQuant 访问 | 已验收 | 离线守卫；未运行 `real_xtquant` |

## 真实能力缺口（待 S1-07 或专项验收）

五类未充分证实的 XTQuant 数据能力仍是独立显式缺口，不能由合成测试消除：

- `security_master` point-in-time 有效区间。
- `daily_bar_unadjusted` `pre_close` 与股/人民币元单位语义。
- `adjustment_factor` 返回列、公式、基准日与算法语义。
- `trading_status` 历史官方停牌/ST/*ST/退市整理期及涨跌停价。
- `benchmark_index_daily` 指数昨收及成交量/成交额的单位和可空语义。

仅 `exchange_calendar` 的 `get_trading_dates` 合同在当前证据下可用。能力不可用时返回
稳定、脱敏的 `CAPABILITY_UNAVAILABLE`/`RESPONSE_CONTRACT`，映射为
`BatchFailureKind.INTERNAL`/`BatchFailureKind.PARSE`，不得以常量或当前状态构造历史值。

## 外部访问与副作用

- 未访问网络、QMT、XTQuant 或备用数据源。
- 未读写 `E:\QuantKingV3Data`、`E:\QuantKing` 或 `F:\QuantKingV3Backups`。
- 数据测试只使用 pytest 临时目录和合成数据。
- 完整质量门仅在授权后读取既有离线工具链缓存；未下载依赖。
