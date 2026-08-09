# Stage 1 数据合同

状态：S1-01～S1-06 的历史验收继续有效；当前目标路线是 AKShare 三 upstream 的未复权日线。旧 XTQuant/S1-07 证据合同已退役。

## 通用不变量

- 时间使用 UTC 时间戳；交易日使用无时区 `date`。
- A 股代码使用 `000001.SZ`、`600000.SH` 形式。
- Canonical 主键稳定且唯一，输出按固定 sort key 排序。
- Raw 只追加；`PUBLISHED` DatasetVersion 不可原地修改。
- 失败批次不能改成成功，重试使用新 ID。
- 路径必须相对于受管数据根，manifest 使用规范 JSON 和 SHA-256。

## 日线核心合同

数据集：`daily_bar_unadjusted`

这是 Stage 1 生产 EOD 唯一允许的数据集。其他历史 schema 不进入默认采集、比较或发布分支。

主键：`(symbol, trade_date)`

| 字段 | 语义 |
|---|---|
| `symbol` | 规范证券代码 |
| `trade_date` | 交易日 |
| `open/high/low/close` | 未复权价格 |
| `volume` | 成交量；单位由 K1 实测确认后冻结 |
| `amount` | 成交额；单位由 K1 实测确认后冻结 |
| `provider` | 固定为 `AKSHARE` |
| `upstream` | `EASTMONEY`、`TENCENT` 或 `SINA` |
| `endpoint` | 实际 AKShare 函数名 |
| `library_version` | AKShare 锁定版本 |
| `fetched_at` | 采集时间 |
| `batch_id` | 原始批次 |

最终 resolution artifact 还必须包含：

- `selected_upstream`；
- `compared_upstreams`；
- `resolution_rule_version`；
- `resolution_reason`；
- 对应 DifferenceReport 或比较摘要标识。

允许在 resolution run 摘要记录全版本默认决定，只为例外行保存完整字段；DatasetVersion manifest 必须绑定摘要、例外 artifact 和 SHA。

## AKShare endpoint 合同

| upstream | endpoint | 首期参数 |
|---|---|---|
| EASTMONEY | `stock_zh_a_hist` | 日线、未复权、显式起止日期 |
| TENCENT | `stock_zh_a_hist_tx` | 日线、未复权、显式起止日期 |
| SINA | `stock_zh_a_daily` | 日线、未复权、显式起止日期 |

证券代码格式转换属于适配器内部。上游返回列名、类型和量额单位不得直接泄漏到领域层。K1 单位策略只依据唯一真实探针、官方文档和锁定的 AKShare 1.18.83 源码冻结；未知包版本和非白名单代码段保持 `UNIT_UNVERIFIED` 或稳定拒绝。

R1 中 `DailyBarRequest` 仍只表达目标窗口；上游窗口固定为目标开始日前推 15 个自然日、结束日不变。Raw response 和 manifest 必须同时绑定 target/upstream 起止日期与 `lookback_days=15`；完整上游 context 原样进入 Raw，Canonical 仅包含裁回目标窗口的行。

## Raw manifest

每个 upstream 请求独立保存：

- source_id、provider、upstream、endpoint；
- AKShare 版本、采集时间；
- 证券与请求日期范围；
- 请求参数的规范摘要；
- row_count、文件 SHA、schema 摘要；
- `COMPLETE` 或稳定失败分类。

不得把三个 upstream 合并成一个不可区分的 AKShare Raw 文件。

## 范围与缺口

首个版本允许限定为沪深当前上市证券和明确日期范围。以下范围在补足前不能宣称研究就绪：

- 北交所和退市证券覆盖；
- 复权因子和公司行动；
- 历史 ST、停牌、上市/退市状态；
- 基准指数和历史成分；
- point-in-time 动态股票池。

## EOD 与发布

Web 和 CLI 只负责创建同一个持久 EOD Job；Worker 执行采集、标准化、比较、质量和发布。

- 入队不触发网络。
- Worker 使用冻结命令和显式数据范围。
- 质量失败或无两源一致时不发布。
- 候选完整写入临时目录并验证 manifest/`COMPLETE` 后原子切换。
- 重复同一输入幂等；中断 Job 关闭后由新 Job 重试。相同输入指纹已有完整发布时复验 manifest 后复用，不维护独立 evidence/checkpoint 恢复层。

S1-05 的详细历史合同和证据见 [验收记录](S1-05_ACCEPTANCE.md)。

## 数据中心读合同

- 只接受显式 `PUBLISHED version_id`，拒绝 `latest`、任意路径和 SQL。
- 预览字段和过滤条件使用 allowlist，分页使用绑定查询指纹的 keyset cursor。
- DifferenceReport、批次、质量和版本血缘在展示前复验。
- API 不返回正式绝对路径、供应商原始 payload、异常栈或机密。

S1-06 的详细历史合同和证据见 [验收记录](S1-06_ACCEPTANCE.md)。

日常读取是否在 K2 后改为轻量检查属于独立 P1 评审；发布时完整验证在任何情况下都不能省略。

## 历史 S1-03

S1-03 的 XTQuant 离线合同曾完成主验收，但没有真实行情能力证明。它不再属于目标架构；K1 必须先用 AKShare gateway 替换正式 runtime，再删除 XTQuant 生产代码和专用测试。历史证据保留在
[S1-03 验收记录](S1-03_ACCEPTANCE.md) 和 Git 历史中。
