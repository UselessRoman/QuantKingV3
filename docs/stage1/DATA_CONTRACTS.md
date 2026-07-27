# Stage 1 数据合同

状态：S1-01 已于 2026-07-16 在提交 `7e4e5b38b7fc91edaf8727ed55b1e26435373aea`
完成主验收；S1-02（Warehouse、原子发布与显式版本查询）已于 2026-07-18 在提交
`79f9fa3e682e212cf74e7886a04c6fb52158f302` 完成主验收。Stage 1 已获实施授权并
进行中，但尚未完成；S1-03-R1 修复中/待主验收。S1-04 已于 2026-07-22 在提交
`2f99491e9dc410acf85ca81c131a77cae08d0cc3` 完成主验收，证据见
[S1-04 主验收证据](S1-04_ACCEPTANCE.md)。真实 XTQuant 样本验证仍须逐项授权，合成数据不能
标生产可用。S1-05-R4 修复已提交、等待主验收；S1-06～S1-08 未开始、未获授权。

本文件固化 Stage 1 六类 Canonical 数据集的可执行合同。权威来源是 `src/quantking/data/schemas.py`（框架无关描述符）和 `src/quantking/data/adapters/canonical_schema.py`（PyArrow 映射与验证）。文档不得替代 schema 与契约测试。

## 通用规则

- Canonical 证券代码使用 `000001.SZ`、`600000.SH`、`430047.BJ` 形式（6 位数字 + `.` + `SZ`/`SH`/`BJ`）。
- `volume` 标准单位为股（share），`amount` 标准单位为人民币元（cny_yuan）。
- 不允许 `NaN` 或无穷；缺失值使用显式 `null`。
- 正常交易行的价格必须为正，数量和金额不得为负。
- 停牌和合法无数据必须由 `trading_status` 或明确缺口记录解释。
- 对上市有效期内的开市日，必须有行情、明确交易状态或缺口记录之一；不允许用空表冒充成功。
- 输出必须按合同排序且主键唯一。
- schema 不得包含供应商专用字段名（如 `symbol`、`tradeStatus`、`qtq` 等）。
- 所有时间戳使用 UTC；交易日使用 `TradingDate`（交易所本地日期，非时间戳）。
- schema 版本随每个数据集描述符固定（如 `daily_bar_unadjusted.v1`）。

## 1. security_master（证券主数据）

| 字段 | 类型 | 单位 | 可空 | 主键 | 说明 |
|---|---|---|---|---|---|
| code | string | - | 否 | 是 | Canonical 代码 |
| exchange | string | - | 否 | 否 | 交易所 SZ/SH/BJ |
| name | string | - | 否 | 否 | 证券名称 |
| board | string | - | 否 | 否 | 板块：MAIN/STAR/GEM/BSE 等 |
| listing_date | date32 | - | 否 | 否 | 上市日期 |
| delisting_date | date32 | - | 是 | 否 | 退市日期；在市为 null |
| effective_from | date32 | - | 否 | 是 | 本记录生效起始日（point-in-time） |
| effective_to | date32 | - | 是 | 否 | 生效截止日；当前记录为 null |
| listing_status | string | - | 否 | 否 | LISTED/SUSPENDED/DELISTED 等 |

- 主键：`(code, effective_from)`
- 排序键：`(code, effective_from)`
- schema 版本：`security_master.v1`
- 必须保留历史状态，不只保存当前证券列表。

## 2. exchange_calendar（交易日历）

| 字段 | 类型 | 单位 | 可空 | 主键 | 说明 |
|---|---|---|---|---|---|
| exchange | string | - | 否 | 是 | 交易所 SZ/SH/BJ |
| trade_date | date32 | - | 否 | 是 | 日历日期 |
| is_open | bool | - | 否 | 否 | 当日是否开市 |

- 主键：`(exchange, trade_date)`
- 排序键：`(exchange, trade_date)`
- schema 版本：`exchange_calendar.v1`

## 3. daily_bar_unadjusted（未复权日线）

| 字段 | 类型 | 单位 | 可空 | 主键 | 说明 |
|---|---|---|---|---|---|
| code | string | - | 否 | 是 | Canonical 代码 |
| trade_date | date32 | - | 否 | 是 | 交易日 |
| open | float64 | cny_yuan | 否 | 否 | 开盘价（未复权，必须为正） |
| high | float64 | cny_yuan | 否 | 否 | 最高价（必须为正） |
| low | float64 | cny_yuan | 否 | 否 | 最低价（必须为正） |
| close | float64 | cny_yuan | 否 | 否 | 收盘价（必须为正） |
| pre_close | float64 | cny_yuan | 否 | 否 | 前收盘价（必须为正） |
| volume | int64 | share | 否 | 否 | 成交量（股，不得为负） |
| amount | float64 | cny_yuan | 否 | 否 | 成交额（元，不得为负） |

- 主键：`(code, trade_date)`
- 排序键：`(code, trade_date)`
- schema 版本：`daily_bar_unadjusted.v1`
- 未复权 OHLCV 永久保存；复权序列是派生数据，不得覆盖原值。

## 4. adjustment_factor（复权信息）

| 字段 | 类型 | 单位 | 可空 | 主键 | 说明 |
|---|---|---|---|---|---|
| code | string | - | 否 | 是 | Canonical 代码 |
| trade_date | date32 | - | 否 | 是 | 交易日 |
| price_multiplier | float64 | - | 否 | 否 | 价格复权因子（必须为正） |
| volume_multiplier | float64 | - | 否 | 否 | 成交量复权因子（必须为正） |
| normalization_date | date32 | - | 否 | 否 | 复权基准日 |
| algorithm_version | string | - | 否 | 否 | 复权算法版本标识（非供应商名） |

- 主键：`(code, trade_date)`
- 排序键：`(code, trade_date)`
- schema 版本：`adjustment_factor.v1`
- 公式：`p_adj = p_raw * price_multiplier`；`v_adj = v_raw * volume_multiplier`。
- 基准日语义：`normalization_date` 是其未复权价格作为复权参考的基准日；因子相对该日前后应用。
- **Stage 1 不生成或保存覆盖原值的复权 OHLCV。** 复权序列是 Stage 2 的派生视图。

## 5. trading_status（交易状态）

| 字段 | 类型 | 单位 | 可空 | 主键 | 说明 |
|---|---|---|---|---|---|
| code | string | - | 否 | 是 | Canonical 代码 |
| trade_date | date32 | - | 否 | 是 | 交易日 |
| is_trading | bool | - | 否 | 否 | 当日是否交易 |
| is_suspended | bool | - | 否 | 否 | 是否停牌 |
| is_st | bool | - | 否 | 否 | 是否 ST |
| is_star_st | bool | - | 否 | 否 | 是否 *ST |
| is_delisting_period | bool | - | 否 | 否 | 是否退市整理期 |
| limit_up_price | float64 | cny_yuan | 是 | 否 | 涨停价；不适用时为 null |
| limit_down_price | float64 | cny_yuan | 是 | 否 | 跌停价；不适用时为 null |
| status_reason | string | - | 是 | 否 | 状态原因；可空 |

- 主键：`(code, trade_date)`
- 排序键：`(code, trade_date)`
- schema 版本：`trading_status.v1`
- 涨跌停价和状态原因按合同允许为空。

## 6. benchmark_index_daily（基准指数日线）

| 字段 | 类型 | 单位 | 可空 | 主键 | 说明 |
|---|---|---|---|---|---|
| code | string | - | 否 | 是 | 指数 Canonical 代码 |
| trade_date | date32 | - | 否 | 是 | 交易日 |
| open | float64 | - | 否 | 否 | 指数开盘（必须为正） |
| high | float64 | - | 否 | 否 | 指数最高（必须为正） |
| low | float64 | - | 否 | 否 | 指数最低（必须为正） |
| close | float64 | - | 否 | 否 | 指数收盘（必须为正） |
| pre_close | float64 | - | 否 | 否 | 指数前收盘（必须为正） |
| volume | int64 | share | 是 | 否 | 成交量；部分指数无成交量 |
| amount | float64 | cny_yuan | 是 | 否 | 成交额；部分指数无成交额 |

- 主键：`(code, trade_date)`
- 排序键：`(code, trade_date)`
- schema 版本：`benchmark_index_daily.v1`
- 指数行情不等于历史指数成分（成分在 Stage 7）。
- `volume`/`amount` 可空：部分指数不提供成交量/成交额。

## 固定边界样本

PyArrow 验证器对以下样本的接受/拒绝行为由 `tests/data/adapters/test_canonical_schema.py` 固化：

| 样本 | 预期 |
|---|---|
| 正常交易行 | 接受 |
| 停牌行（is_trading=False, is_suspended=True, 涨跌停为 null） | 接受 |
| ST 行 | 接受 |
| *ST 行 | 接受 |
| 上市首日附近 | 接受 |
| 退市阶段 | 接受 |
| 复权事件 | 接受 |
| 合法空结果（零行表、schema 匹配、携带显式合法分类和非空原因） | 接受 |
| 意外空表或未携带合法分类/原因 | 拒绝，并建模为 `UNEXPECTED_EMPTY` |
| 网络失败、权限失败、解析失败 | 分别建模，禁止伪装成空表成功 |
| 非法空结果（缺列/类型错） | 拒绝 |
| Arrow 字段类型、字段顺序或 nullability 不精确匹配 | 拒绝 |
| 未按合同 `sort_key` 排序 | 拒绝 |
| `high`/`low` 与 OHLC 关系冲突 | 拒绝 |
| `exchange` 与代码后缀不一致 | 拒绝 |
| 交易、停牌、ST/*ST 或涨跌停状态互相矛盾 | 拒绝 |
| 重复主键 | 拒绝 |
| NaN / 无穷 | 拒绝 |
| 错误代码（非 `000001.SZ` 形式） | 拒绝 |
| 负成交量 / 负金额 | 拒绝 |
| 零或负价格（交易行） | 拒绝 |

## 框架无关性

- `src/quantking/data/schemas.py` 不导入 pyarrow、pandas、sqlalchemy 或任何供应商 SDK。
- PyArrow 映射只出现在 `src/quantking/data/adapters/canonical_schema.py`。
- 数据领域对象（`src/quantking/data/models.py`）不导入 sqlalchemy 或 alembic。
- ORM 行只位于 `src/quantking/data/adapters/persistence/`，不作为 API 或领域接口。

## S1-03 XTQuant 只读能力矩阵

S1-03 的生产适配器只允许延迟加载明确配置目录中的 `xtquant.xtdata`，不加载交易、账户或
下单模块。以下依据仅来自本机自然可访问的 XTQuant 公共 Python 源码/签名的只读核对，
没有导入 SDK、连接 QMT、访问网络或读取真实行情。真实 XTQuant 样本验证仍须逐项授权；
合成测试不构成真实 XTQuant 能力证明。

| Canonical 数据集 | 只读公共接口 | 供应商边界所需字段 | 字段/单位依据与确定性推导 | 当前能力与缺口 |
|---|---|---|---|---|
| `security_master` | `get_instrument_detail_list` | 代码、交易所、名称、板块、上市/退市日、有效区间、上市状态 | 公共源码只描述当前证券详情；Canonical 需要 point-in-time 有效区间 | 不可用：当前列表不能伪装为历史 point-in-time 主数据 |
| `exchange_calendar` | `get_trading_dates` | 市场、显式起止日、开市日毫秒时间戳 | 公共源码描述起止边界和开市日；闭市日可在显式区间内由开市日补集确定性推导 | 可用：仅此能力在当前证据下启用 |
| `daily_bar_unadjusted` | `get_market_data_ex` | 日期、OHLC、昨收、成交量、成交额 | 公共源码列出部分行情字段，但 `pre_close` 和股/手、人民币元等单位语义未验证 | 不可用：不得猜测单位或把其他复权口径冒充未复权 |
| `adjustment_factor` | `get_divid_factors` | 事件日、价量乘数、基准日、算法版本 | 公共源码未证明返回列、公式、基准日与算法语义 | 不可用：不得伪造因子公式或基准 |
| `trading_status` | `get_instrument_detail` | 历史官方停牌、ST、*ST、退市整理期及涨跌停价 | 公共源码只描述当前 `IsTrading`/`InstrumentStatus`；没有历史交易状态合同 | 不可用：不得由零成交量或当前状态推断历史官方状态 |
| `benchmark_index_daily` | `get_market_data_ex` | 日期、OHLC、昨收及可空成交量/成交额 | 公共源码没有证明指数昨收及成交量/成交额的单位和可空语义 | 不可用：单位语义未验证时不得补零或沿用股票口径 |

能力不可用、字段缺失或语义未验证时，对外返回稳定、脱敏的
`CAPABILITY_UNAVAILABLE`/`RESPONSE_CONTRACT`，并分别映射为现有
`BatchFailureKind.INTERNAL`/`BatchFailureKind.PARSE`；
不得以常量、当前状态或无关字段构造历史值。

## S1-03 合成供应商响应合同

Fake Gateway 和离线映射测试使用显式 `SupplierResponseContract`。调用方必须声明代码格式、
交易所映射、日期格式、成交量单位、成交额单位、未复权、point-in-time、官方交易状态及
因子公式是否已验证。只有声明与数据集合同完全一致时才进入转换：

- `SZ000001`、`SH600000`、`BJ430047` 确定性转换为
  `000001.SZ`、`600000.SH`、`430047.BJ`，并复验交易所与后缀。
- 供应商日期按显式格式解析为交易所本地 `date32`；批次时间统一使用 UTC。
- 成交量只接受已证明的“股”或明确的“100 股/手”并转换为股；成交额只接受人民币元。
- 未复权日线必须显式标记未复权；指数成交量、成交额只按 Canonical nullability 允许为空。
- 复权公式固定为
  `p_adj=p_raw*price_multiplier;v_adj=v_raw*volume_multiplier`，并要求基准日和
  `algorithm_version`。
- 交易状态必须来自官方状态合同，分别保留交易、停牌、ST、*ST 和退市整理期；不从零成交量推断。
- 原始记录以规范 JSON 保存到 Raw，Canonical 表不携带供应商专用字段。
- 结果使用精确 Arrow 字段顺序、类型和 nullability，按 `sort_key` 排序并复验全局主键；
  重复、矛盾、NaN、无穷、缺字段或错类型一律失败。

## S1-03 增量、Raw 与批次语义

`IncrementalFetchPlan` 显式记录 dataset、证券/交易所范围、包含边界的请求起止日、已知覆盖
区间、调用方给定回看天数、稳定分块、adapter version、请求摘要和新 `batch_id`。中间缺口
逐段补抓，回看窗口不会从最大日期推断并跳过缺口；核心逻辑不使用“今天”或 `latest`。
除 `batch_id` 外相同输入得到相同计划摘要，相同参数失败重试必须换新 `batch_id`。

编排先短事务写 `RUNNING IngestionBatch`，事务外调用 Gateway，将完整供应商 JSON 响应交给
现有 `RawBatchPublisher`，复验 `manifest.json`、逐文件 SHA-256 和 `COMPLETE` 后才标准化。
Raw 发布后标准化失败仍保留完整 Raw；payload 产生前失败则使用同一受管理路径防护写入
不可覆盖的 `.incomplete/failure.json`。不删除失败目录。

S1-03-R1 修复中/待主验收。R1 要求每个非空请求分块保存规范
`xtquant-raw-response.v1` 信封，信封同时绑定：

- 计划 `request_sha256` 和完整 chunk 序号、dataset、scope、起止日及包含边界；
- 经验证的供应商字段/单位合同；
- 标准化前 records、完整性标记和稳定 partial code；
- JSON 安全的原始 provider payload。

`RawManifestFile.row_count` 必须等于信封内 records 的真实数量。发布并通过统一 Raw verifier
后，编排器重新读取每个 Raw 文件，按计划顺序重建 `SupplierResponse`，复验 manifest 请求
参数、chunk 库存、row_count、Raw 与 Gateway records 全部一致，之后才生成 Canonical
候选。缺失、重复、错序、请求 SHA 漂移、计划外代码/交易所/日期或 Raw/records 漂移均失败。

`FetchChunk` 和 `IncrementalFetchPlan` 的直接构造与每个 Gateway/编排入口都执行同一生成式
复验；请求参数只允许固定白名单键和值格式。payload 前失败证据只保存 dataset、
adapter version 和 request SHA，不保存 scope、攻击输入或任意调用参数。

供应商空响应默认是 `UNEXPECTED_EMPTY`。只有调用方同时提供合法 `EmptyResultInfo` 及证据
原因时才能得到空候选；空交易日历响应不得自动扩展成区间全部闭市。Gateway 返回错误类型、
Raw/请求构造失败和标准化异常在 `RUNNING` 已持久化后都必须写入稳定终态，不得遗留
`RUNNING`。显式 SDK loader 加载失败或检测到交易模块时必须恢复进入 loader 前的模块状态。

批次终态如下：

- `SUCCEEDED`：Raw 凭证完整且完整 Canonical 候选表通过 S1-01 验证；合法空结果还必须携带
  `NO_TRADING_SESSION`、`OUTSIDE_LISTING_RANGE` 或 `EMPTY_REQUEST_UNIVERSE` 之一及原因。
- `PARTIAL`：供应商明确部分响应；保存已收到 Raw 和 `PARTIAL_SOURCE`，不返回完整候选。
  多分块抓取中后续分块失败时，同样保存此前响应和脱敏错误代码后进入 `PARTIAL`。
- `FAILED`：网络、权限、请求、解析、未验证能力、意外空、Raw 或内部失败；保存脱敏失败信息。

稳定源错误映射为 `NETWORK`、`PERMISSION`、`PARSE`、`UNEXPECTED_EMPTY`、
`PARTIAL_SOURCE` 或 `INTERNAL`，S1-03 不产生 `QUALITY`。终态 `batch_id` 不再调用供应商；
失败或部分响应不得变成成功，相同参数只允许使用新 `batch_id` 重试。S1-03 不调用
`CanonicalPublishCoordinator`，不创建或推进任何 `PUBLISHED DatasetVersion`。

## S1-05 EOD 请求、执行与证据合同

- 固定任务类型是 `data.eod.v1`；请求只接受明确起止交易日期、六类 allowlist 中非空且
  无重复的数据集，以及不可省略的 `PUBLISH_AFTER_QUALITY`。来源固定为 `XTQUANT`。
- `base_versions` 必须显式为空对象（根版本），或按请求数据集完整提供
  `dataset=data_version:...` 精确映射；禁止 `latest` 和任何隐式当前版本选择。入队 resolver
  复验 PUBLISHED/Warehouse/PASSED quality 后才冻结完整 base 凭证与真实 coverage。
- 应用服务将规范 request JSON、request SHA、冻结的 adapter/code/rule/schema 版本、
  scope、lookback、chunk 参数、显式 reference version 及可选 base version 凭证分别写入
  `JobRun` manifests。Worker 只能从持久 manifests 重建并复验，不使用进程内请求对象。
- base version 冻结 `version_id/dataset/schema_version/content_sha256/manifest_sha256/coverage`；
  Worker 使用显式 ID 复验 `PUBLISHED` 元数据、Warehouse 和 PASSED quality 后，从完整基表
  推导 known coverage。调用者不能单独声明 known coverage。
- `job_type + idempotency_key` 相同且三类 manifests 完全相同才复用原 `job_id`；任一请求、
  配置或版本凭证变化均返回稳定幂等冲突。失败或中断重试必须使用新 job/key，并产生新
  batch/version ID。
- 每个数据集依次执行能力预检、现有 XTQuant ingestion、按 Canonical 主键将本次完整响应
  按实际 `FetchChunk` 的 scope/date 权威切片从基表移除旧行、加入本次完整响应，再执行真实
  BUILDING version、全快照 QualityRunner 和 CanonicalPublishCoordinator。未抓取 scope
  与范围外历史保持不变，新版本直接父版本为 base。
  只有 ingestion `SUCCEEDED`、quality
  `PASSED` 且发布目录的 manifest/COMPLETE/content SHA/元数据全部复验一致才记为完成。
- 全部请求范围已由 base 覆盖时返回 `REUSED_UNCHANGED`，不创建空 batch/version；实际发布
  新快照返回 `PUBLISHED_NEW`。发布后 checkpoint 绑定 batch/version/quality/content/manifest
  凭证，故障证据保留已完成结果，严格复验通过的重试不会创建重复版本。
- 生产能力矩阵仍只有 `exchange_calendar` 可用；其余五类在 Gateway 调用前返回
  `CAPABILITY_UNAVAILABLE`。Fake Gateway 的六类合成覆盖不改变生产矩阵。
- 成功证据位于 `artifacts/eod/<job_id>/` 的严格
  `request.json/result.json/manifest.json/COMPLETE` 文件集；失败证据位于对应
  `.incomplete` 目录。证据只记录稳定 ID、状态、SHA 和脱敏错误，不记录供应商 payload、
  token、账号、密码或绝对路径，且终态不可覆盖。success verifier 还复验 request/result
  精确字段、数据集集合/顺序、ID namespace、状态字段组合及版本、质量、Warehouse 凭证；
  failure verifier 复验真实规范 UTC、终态、错误码、请求 SHA 和 completed results。
- Worker 和 verifier 共用同一个严格 frozen-command parser；深层验证还交叉绑定 batch 的
  dataset/source/adapter/request params 与 request/config/version manifests。成功证据在 Job
  提交 `SUCCEEDED` 前属于不可变 forensic 证据；若随后故障，原目录保留并另写权威 failure，
  终态 success verifier 同时要求 Job 状态与 artifact binding。
- Web、enqueue 和 status runtime 不做恢复。只有显式 Worker 启动可按持久 heartbeat 的明确
  租约阈值、row_version 和数据库条件更新，将过期 RUNNING 转为 INTERRUPTED；新鲜租约不变。
