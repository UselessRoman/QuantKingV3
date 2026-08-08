# 数据架构与质量规范

## Stage 1 目标

发布可查询、可追溯的 A 股未复权日线版本。首个版本允许明确限定为沪深当前上市证券和指定日期范围；北交所、退市证券、复权、历史 ST/停牌状态必须列为缺口，不能伪装成已覆盖。

核心字段：

`symbol, trade_date, open, high, low, close, volume, amount`

来源字段：

`provider, upstream, endpoint, library_version, fetched_at, batch_id`

## 数据源

| 角色 | provider | upstream | 接口 |
|---|---|---|---|
| A 主来源 | AKSHARE | EASTMONEY | `stock_zh_a_hist` |
| B 常态互验 | AKSHARE | TENCENT | `stock_zh_a_hist_tx` |
| C 异常第三源 | AKSHARE | SINA | `stock_zh_a_daily` |

AKShare 是统一依赖，不等于三个来源相同。必须记录实际 upstream；上游网页或接口变化属于可观测故障。

## 分层

```text
Raw              各 upstream 原始响应，独立、只追加
    ↓
Canonical        统一代码、字段、单位和日期
    ↓
Resolved         多源比较后的选源结果与原因
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

## 单源检查

- schema 和类型。
- 主键唯一、排序稳定。
- `low <= open/close <= high`。
- 价格非负，volume/amount 非负。
- 日期范围、交易日和空结果语义。
- 覆盖、新鲜度和上游 schema 漂移。

## 三源仲裁

1. 每次正常任务采集 A 和 B。
2. A/B 都成功并在字段阈值内一致时选择 A。
3. A/B 任一失败、缺行、质量失败或分歧时采集 C。
4. 至少两源一致才可选择；从一致集合按 A、B、C 优先级选择。
5. 无两源一致时隔离并报告。

允许逐证券/交易日选源，但每行必须有 `selected_upstream`、参与比较的来源、规则版本和决定原因；这不是静默补洞。

价格精度、成交量和成交额单位及容差先由 K1 真实小样本确认，再由 K2 冻结。不得在未实测时猜测。

## 发布

```text
临时目录 → 完整写入 → 质量/一致性检查 → manifest → COMPLETE → 原子切换
```

失败目录保留且不能改成成功。研究、回测和 Web 只能消费显式 `PUBLISHED DatasetVersion`，不能读取“latest”路径。

## 后续研究边界

Stage 2 在计算收益、因子和动态股票池前，必须补足并验收复权、历史上市状态、ST、停牌及其他 point-in-time 语义。Stage 1 的价格版本不自动证明这些研究条件已经满足。
