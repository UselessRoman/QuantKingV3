# Stage 1 质量与跨源合同

## 单源质量

每个 upstream 的 Canonical 候选先独立执行：

- schema/类型和主键唯一；
- 固定排序；
- `low <= open/close <= high`；
- 价格、成交量和成交额非负；
- 请求范围、日期覆盖和新鲜度；
- 合法空、上游空、超时、限流、解析失败和 schema 漂移的稳定分类。

QualityRun 终态为 `PASSED`、`FAILED` 或 `SKIPPED`。只有 `PASSED` 可进入跨源比较。

## 跨源比较

比较前必须统一证券代码、交易日、字段和单位。DifferenceReport 至少绑定：

- 两侧 upstream、batch_id 和内容 SHA；
- 数据集与比较范围；
- 字段级阈值版本；
- 缺行、额外行和字段差异；
- 创建时间和稳定 report_id。

价格、成交量和成交额使用独立阈值。阈值由 K1 的真实样本确认口径后，在 K2 以版本化配置冻结。

## A/B/C 规则

1. A=东财、B=腾讯常态采集。
2. A/B 均通过单源质量并在阈值内一致：选择 A。
3. 任一失败、缺行或分歧：调用 C=新浪。
4. 任意两源一致：在一致集合内按 A、B、C 优先级选择。
5. 无两源一致：`CONFLICT_QUARANTINED`，不发布。

优先级不能替代一致性门。C 不能在未记录原因时静默接管。

## 显式选源

每个 resolved 行记录：

- 选择来源；
- 参与判断的来源；
- 一致或失败原因；
- 阈值/规则版本；
- DifferenceReport 标识。

若现有 `SourceOverride` 用于人工修正，必须产生新 DatasetVersion，不得修改旧版本。

## 失败纪律

- 质量失败、解析失败和无共识都阻止发布。
- 失败输入、摘要和日志保留；修复后创建新任务。
- 合成测试只能证明代码合同，不能把 upstream 标为真实可用。
- 不需要签名、nonce、claim、seal 或授权信封；Git、manifest、SHA、任务日志和 DatasetVersion 血缘足以满足个人项目审计。
