# Stage 1 数据合同

状态：S1-01-R1 主验收未通过；S1-01-R2 修复中、待主验收。Stage 1 已获实施授权并进行中，但尚未完成。本文描述目标合同，不代表功能已通过主验收。

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
