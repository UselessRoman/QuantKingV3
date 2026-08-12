# 数据架构与质量规范

## Stage 1 目标

发布可查询、可追溯的 A 股未复权日线版本。首个版本允许明确限定为沪深当前上市证券和指定日期范围；北交所、退市证券、复权、历史 ST/停牌状态必须列为缺口，不能伪装成已覆盖。

核心字段：

`symbol, trade_date, open, high, low, close, volume, amount`

来源字段：

`provider, upstream, endpoint, library_version, fetched_at, batch_id`

## 数据源

| 角色 | provider | upstream | 接口/输入 |
|---|---|---|---|
| A 常态供数 | BAOSTOCK | BAOSTOCK | `query_history_k_data_plus` |
| B 常态互验 | AKSHARE | EASTMONEY | `stock_zh_a_hist` |
| C 异常仲裁 | AKSHARE | TENCENT | `stock_zh_a_hist_tx` |
| D 最后仲裁 | AKSHARE | SINA | `stock_zh_a_daily` |
| X 历史候选 | XTQUANT_LEGACY | XTQUANT | 受管 `DATA_ONLY` 导入对象 |

必须记录实际 provider/upstream；BaoStock 服务故障与 AKShare 的网页/API/库变化分别建模。
三个 AKShare upstream 不得合并，旧 XTQuant 也不得冒充当前可调用服务。来源合同保持固定且
最小，不恢复候选审批、`PRODUCTION_READY` 或 source×dataset 能力矩阵。

## 分层

```text
Raw              各 upstream 原始响应，独立、只追加
    ↓
Canonical        统一代码、字段、单位和日期
    ↓
Resolved         `ResolutionDecision` 与日线候选；不是人工 override
    ↓
Warehouse        已发布 DatasetVersion
    ↓
Research         复权、股票池、因子、标签和 Qlib 派生视图
```

## 批次和版本

- 每次 upstream 请求对应明确批次和 manifest。
- Raw 记录请求范围、endpoint、AKShare 版本、采集时间、row_count 和 SHA。
- Canonical 主键至少为 `(symbol, trade_date)`，排序确定。
- 已发布版本不可原地修改；新数据或修订生成新版本。
- 失败或未达成两源一致的行进入隔离，不进入发布候选。
- Stage 1 运行时只处理 `daily_bar_unadjusted`；其他数据集 schema 不进入抓取、比较或发布枚举。

## 单源检查

- schema 和类型。
- 主键唯一、排序稳定。
- `low <= open/close <= high`。
- 价格非负，volume/amount 非负。
- 日期范围、交易日和空结果语义。
- 覆盖、新鲜度和上游 schema 漂移。

## 多源仲裁

1. 每次正常任务采集 BaoStock A 和东财 B。
2. A/B 都成功并在字段阈值内一致时选择 A。
3. A/B 任一失败、缺行、质量失败或分歧时，对受影响范围采集腾讯 C。
4. 仍无两源一致时才对剩余范围采集新浪 D。
5. 至少两源一致才可选择；从一致集合按 A、B、C、D 优先级选择。
6. 历史导入的 X 先与 A 比较，分歧时按 B/C/D 逐级仲裁。
7. 无两源一致时隔离并报告。

允许逐证券/交易日选源。每个决定至少记录 `selected_upstream`、参与比较的来源、规则版本、决定原因和差异报告标识；它属于正常 `ResolutionDecision`，不是 `SourceOverride` 或静默补洞。

K2 可以在版本级记录共同默认决定，并只为例外证券/交易日保存行级决定，避免为完全一致的普通行重复存储相同审计文本。

BaoStock 与旧 XTQuant 的价格、`pre_close`、成交量、成交额、复权和停牌语义必须由 K3 实测；
新量额容差也由代表性样本形成版本化合同。不得在未实测时猜测或覆盖历史零容差失败证据。

## 发布

```text
临时目录 → 完整写入 → 质量/一致性检查 → manifest → COMPLETE → 原子切换
```

失败目录保留且不能改成成功。研究、回测和 Web 只能消费显式 `PUBLISHED DatasetVersion`，不能读取“latest”路径。

## EOD 恢复

Stage 1 的权威恢复事实仅包括冻结 `JobRun`、各 upstream Raw manifest、最终 DatasetVersion manifest 和 JSON Lines 日志：

1. 进程中断后把旧 Job 标记为 `INTERRUPTED`，不在原 Job 中继续执行。
2. 用户重试时创建新 Job。
3. 相同输入指纹若已有完整 `PUBLISHED DatasetVersion`，复验发布 manifest 后直接复用。
4. 否则从最近的安全业务边界重新执行，不增加独立 success/failure evidence 或多层 checkpoint 状态。

## 后续研究边界

Stage 2 在计算收益、因子和动态股票池前，必须补足并验收复权、历史上市状态、ST、停牌及其他 point-in-time 语义。Stage 1 的价格版本不自动证明这些研究条件已经满足。
