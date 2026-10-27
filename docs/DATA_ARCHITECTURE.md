# 数据架构与质量规范

本文只描述长期有效的数据边界。阶段进度见 [路线图](ROADMAP.md)，Stage 1 的执行过程与证据见
[Stage 1 历史索引](stage1/README.md)。

## 数据链

```text
Raw → Canonical → Resolution → Warehouse → Research
```

- **Raw**：按实际 provider/upstream 分开保存原始响应，只追加不覆盖。
- **Canonical**：统一代码、字段、单位和日期；不改变来源事实。
- **Resolution**：保存逐证券、逐交易日的候选比较、选源决定和隔离原因。
- **Warehouse**：只暴露通过质量门并原子发布的不可变数据版本。
- **Research**：以显式父版本派生复权、股票池、因子和标签，不回写上游版本。

## 当前日线合同

生产数据集为 `daily_bar_unadjusted`，schema 为 `daily_bar_unadjusted.v1`，主键为
`(code, trade_date)`。核心字段为：

`code, trade_date, open, high, low, close, pre_close, volume, amount`

Canonical 成交量单位为股，成交额单位为人民币元。`pre_close` 统一由完整未复权序列的前一行
`close` 派生；供应商参考昨收仅保留在 Raw。

该 `pre_close` 不保证等于除权日交易规则参考价。研究连续价、未复权成交/估值价和交易规则
参考价的交接由 S2-01/02 冻结，Stage 4 验证使用；不能改写既有日线字段补偿缺失语义。

## 来源与仲裁

| 角色 | provider / upstream | 接口 | 调用时机 |
|---|---|---|---|
| A | BAOSTOCK / BAOSTOCK | `query_history_k_data_plus` | 正常任务 |
| B | AKSHARE / TENCENT | `stock_zh_a_hist_tx` | 正常任务 |
| C | AKSHARE / SINA | `stock_zh_a_daily` | A/B 异常范围 |

东财、FTShare、XTQuant/QMT 均不属于当前来源集合。历史 XTQuant 文件仅是冻结审计输入，不能
进入 Raw、Canonical、Warehouse 或发布候选。

仲裁规则：

1. 正常任务采集 A 和 B；两者质量合格且在冻结阈值内一致时选择 A，C 不调用。
2. A/B 任一调用失败、质量失败、缺行或分歧时，只为受影响范围采集 C。
3. 至少两个独立来源持有同一主键行且一致才可选择；一致集合内按 A、B、C 排序。
4. 恰好一个来源持有行，或不存在两源共识时，隔离并阻止发布。
5. 经绑定交易日历确认的整范围合法空结果使用独立原因码，不伪装成值共识。

允许逐证券、逐交易日选源，但每个决定必须记录参与来源、实际选源、规则版本、原因与差异报告。
禁止静默替换或把腾讯和新浪合并成一个 AKShare 来源。

## 质量门

单源候选至少检查：

- schema、类型、主键唯一与确定排序；
- `low <= open/close <= high`；
- 价格、成交量和成交额非负；
- 请求范围、交易日历成员关系、新鲜度、空结果和上游 schema 漂移；
- 按交易所和证券计算的预期交易日覆盖及显式例外。

全局日期最小值/最大值只能做边界检查，不能替代逐证券覆盖。比较器与仲裁器必须复用同一套
Decimal/定点阈值实现；阈值版本进入差异报告、选源 artifact、输入指纹和 verifier。当前接受
的冻结值为价格及 `pre_close` 零容差，成交量/成交额绝对容差 50、相对容差 0。

## 批次、版本与发布

- 每次请求有明确批次和 manifest，记录来源、端点、版本、范围、参数、行数、内容 SHA 与 Job。
- Raw 只追加；Canonical、Resolution、数据集版本和研究派生均不可原地修改。
- 失败任务不得发布半成品；失败目录、日志和输入清单保留。
- 发布采用“临时写入 → 校验 → manifest → COMPLETE → 原子切换”。
- 查询、研究、回测和 Web 只消费显式 `PUBLISHED DatasetVersion`，不读取可变 `latest` 路径。
- 相同输入指纹已有完整发布版本时，复验 manifest 后复用；中断任务标记 `INTERRUPTED`，重试建立新 Job。

## 研究层前置

Stage 1 日线版本不自动证明研究数据已满足 point-in-time 要求。进入收益、因子和动态股票池前，
必须补足并验收历史上市状态、退市、ST、停牌和复权语义；供应商历史修订必须产生新版本。
研究合同见 [研究与回测](RESEARCH_AND_BACKTEST.md)，实施拆分见 [Stage 2](stage2/README.md)。

Stage 2 同时交接复权相关的最小公司行动事实及执行缺口，不从累计复权因子反推现金/股数事件。
研究必要字段不明仍阻断研究发布；仅执行字段缺失可以显式记录，但阻断后续受影响的正式回测或
账户处理。未知覆盖不能表示无事件，事实和缺口复用同一研究 manifest，不建设独立发布控制面。
