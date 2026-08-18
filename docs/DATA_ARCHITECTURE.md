# 数据架构与质量规范

## Stage 1 目标

发布可查询、可追溯的 A 股未复权日线版本。首个版本允许明确限定为沪深当前上市证券和指定日期范围；北交所、退市证券、复权、历史 ST/停牌状态必须列为缺口，不能伪装成已覆盖。

核心字段：

`code, trade_date, open, high, low, close, pre_close, volume, amount`

来源字段：

`provider, upstream, endpoint, library_version, fetched_at, batch_id`

## 数据源

| 角色 | provider | upstream | 接口/输入 |
|---|---|---|---|
| A 常态供数 | BAOSTOCK | BAOSTOCK | `query_history_k_data_plus` |
| B 常态互验 | AKSHARE | TENCENT | `stock_zh_a_hist_tx` |
| C 异常仲裁 | AKSHARE | SINA | `stock_zh_a_daily` |
| X 历史候选 | XTQUANT_LEGACY | XTQUANT | `DATA_ONLY` 候选：评估完成、迁移已拒绝（ADR-027） |

必须记录实际 provider/upstream；BaoStock 服务故障与 AKShare 的网页/API/库变化分别建模。
两个 AKShare upstream 不得合并，旧 XTQuant 也不得冒充当前可调用服务。来源合同保持固定且
最小，不恢复候选审批、`PRODUCTION_READY` 或 source×dataset 能力矩阵。

上表是 ADR-021 接受的合同。S1-07K4 已完成 registry、路由、预算、artifact、verifier 和
DataCenter 的三角色迁移与离线验收。K4C 第一次代表性真实三源实测曾于 2026-08-15 经独立
验收不通过（S1-07K4C-R1，当时的历史事实）；此后 R1 修复、K4C-R2 真实实测、K4C-R3
（ADR-022）与 K4C-R4（ADR-023）依次交付，**K4C-R4 已于 2026-08-16 独立验收通过**，
`comparison-thresholds.daily-bar-unadjusted.k4.v1` 已按用户决定接受重新冻结（ADR-024，
取代此前“待重新验收”状态；见 [S1-07K4 实施记录](stage1/S1-07K4_IMPLEMENTATION.md)）。
K4D 评估已完成并收口（ADR-027）：只读 inventory 已于 2026-08-16 获授权执行一次并成功
（5,208 文件、零读取错误、Stage 0 基线差异 0/0，ADR-025）；两次真实 pilot attempt 分别于
2026-08-16/08-17 获授权执行并失败即停（ADR-026，数据根零写入、授权已消费）；旧 XTQuant
迁移因复权语义不可信被用户决定拒绝，第 3 次 pilot、全量诊断与 full 均不执行，
`XTQUANT_LEGACY` 不进入正式 Raw、Canonical、Warehouse 或发布候选。**S1-07K4 已于
2026-08-17 经 K4E 独立验收通过并形成单一 K4 提交**
（[S1-07K4 验收记录](stage1/S1-07K4_ACCEPTANCE.md)），该验收不构成新的
真实运行授权。**S1-08R2（发布引擎离线收口）已于 2026-08-17 独立只读验收通过**
（[S1-08R2 验收记录](stage1/S1-08R2_ACCEPTANCE.md)）：发布范围/预算/血缘由冻结
release manifest（`s1-08r2-release-manifest.v1` + 独立 `RELEASE_SAFETY_CAPS`）驱动，
SQLite 锁重试、合法空版本幂等、跨进程单发布者、硬中断恢复、逐证券真实 coverage 与
只读 verifier 均有合成回归；R2 零真实网络、零正式数据根写入。**S1-08R3 已由单独任务
执行并完成开发侧收口：C1-R4 发布唯一 PUBLISHED `exchange_calendar` 版本，E1 唯一
正式 Job 于 2026-08-17 SUCCEEDED，发布首个 PUBLISHED `daily_bar_unadjusted` 版本
（82 行/2 证券，授权一次消费）；R3C 双幂等探针与 E1R 结果证据只读提取于 2026-08-18
均成功（新基线 `299ccba9…`）；仅剩 R3D 独立终验。**东财和 FTShare 不得进入正式来源。

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
- Canonical 物理主键至少为 `(code, trade_date)`，排序确定；任务/配置中的 `symbol` 只表示
  逻辑证券 scope，不得与物理列名混用。
- 已发布版本不可原地修改；新数据或修订生成新版本。
- 失败或未达成两源一致的行进入隔离，不进入发布候选。
- Stage 1 运行时只处理 `daily_bar_unadjusted`；其他数据集 schema 不进入抓取、比较或发布枚举。

## 单源检查

- schema 和类型。
- 主键唯一、排序稳定。
- `low <= open/close <= high`。
- 价格非负，volume/amount 非负。
- 日期范围、交易日和空结果语义。
- 按交易所/证券的预期交易日覆盖、未解释缺口、新鲜度和上游 schema 漂移。

Stage 1 发布门必须消费带版本和内容 SHA 的交易日历。`coverage_window` 的全局最小/最大日期
只能作为边界检查，不能替代逐证券预期交易日检查；停牌、未上市或整个请求范围无交易等例外
必须形成显式缺口/空结果决定和证据。

## 多源仲裁

1. 每次正常任务采集 BaoStock A 和腾讯 B。
2. A/B 都成功并在字段阈值内一致时选择 A。
3. A/B 任一失败、缺行、质量失败或分歧时，对受影响范围采集新浪 C。
4. 至少两源一致才可选择；从一致集合按 A、B、C 优先级选择。
5. 历史导入的 X 先与 A 比较，分歧时按 B/C 仲裁。
6. 无两源一致时隔离并报告。

只有两个来源都持有同一主键行时才能比较字段并形成值共识。若任一来源持有行，其他来源的
共同缺行不构成该行共识；恰好一个来源持有行时必须继续取证或隔离。经交易日历确认的多源
整范围合法空结果可以形成零行结果，但使用独立原因码，不能伪装成 `AB/AC/..._AGREE_*_SELECTED`。

比较器与仲裁器必须复用同一版本化 Decimal/定点阈值实现；阈值版本进入 DifferenceReport、
Resolution artifact、输入指纹和 verifier，不能出现报告判为容差内一致而仲裁仍按 float
原值判为冲突的双重口径。

允许逐证券/交易日选源。每个决定至少记录 `selected_upstream`、参与比较的来源、规则版本、决定原因和差异报告标识；它属于正常 `ResolutionDecision`，不是 `SourceOverride` 或静默补洞。

K2 可以在版本级记录共同默认决定，并只为例外证券/交易日保存行级决定，避免为完全一致的普通行重复存储相同审计文本。

BaoStock 的价格、`pre_close`、成交量、成交额、复权和停牌语义必须由 K3 实测；新量额
容差也由代表性样本形成版本化合同。不得在未实测时猜测或覆盖历史零容差失败证据。
旧 XTQuant 的对应语义评估未通过（两次 pilot 失败，ADR-026/027），其数据不进入发布链，
不再列为待实测对象。
Canonical `pre_close` 对所有服务源统一由完整未复权序列的前一行 `close` 派生；供应商直出的
参考昨收只保留在 Raw，不能在某个适配器存在该列时静默切换 Canonical 语义。

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

Stage 2 已规划采用最小研究派生层，详见 [Stage 2 当前入口](stage2/README.md)：

- Stage 1 的 `DatasetVersion` 保持不可变，研究层只以显式父版本读取，不把复权值写回
  `daily_bar_unadjusted`。
- 除复用 Stage 1 未复权日线合同外，只新增 `security_lifecycle`、`security_status_daily`、
  `adjustment_factor_daily` 三类必要合同，并发布带父血缘的 `ResearchInputVersion`；不建设
  通用证券主数据或公司行动平台。
- 复权表达必须 point-in-time 且追加稳定。未来才生效的公司行动不能改变此前的研究收益或
  特征；供应商历史修订必须创建新版本，不能覆盖旧研究输入。
- 股票池、标签和因子都读取同一研究输入与交易日历。历史退市证券不能因当前上市列表而消失，
  状态缺口、单源冲突或复权不明的范围必须隔离。
- 初始市场边界建议为沪深 A 股；S2-01 先检查父版本是否覆盖声明窗口内全部曾上市证券。若
  历史退市证券缺失，S2-02 只复用 Stage 1 已验收的 A/B/C 日线管线发布覆盖扩展新版本，通过
  后再进入研究；北交所在取得同等质量数据前保持显式缺口并禁止出现在已覆盖声明中。
