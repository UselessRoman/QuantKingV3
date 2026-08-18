# 架构决策记录

状态：已确认。本文记录本轮逐项讨论后接受的决策。改变任意决策时，必须说明原因、数据迁移、兼容性和验收影响；不得通过局部实现绕过。

## ADR-001：产品定位

**状态：已接受**

QuantKing V3 是单用户、本机运行的沪深京 A 股日频量化研究和人工调仓系统。首期采用周度调仓，不建设自动下单、分钟/Tick、高频、卖空、多用户、云端或微服务。

## ADR-002：模块化单体

**状态：已接受**

采用模块化单体和外部适配器。Web 与工作进程可以作为独立 Windows 进程运行，但共享一套应用服务和本地存储，不拆成远程服务。

结果：外部 SDK 只能位于适配器；不引入 Redis、Celery 或远程消息队列。

## ADR-003：代码与运行数据隔离

**状态：已接受**

- 代码：`E:\QuantKingV3`。
- 运行数据：`E:\QuantKingV3Data`。
- 异盘备份：`F:\QuantKingV3Backups`。

Parquet 保存批量数据，DuckDB 用于查询，SQLite 保存元数据、任务、审计、策略账和对账状态。

## ADR-004：数据源与仲裁策略

**状态：已接受（2026-08-14 修订；现行角色由 ADR-021 冻结）**

Stage 1 使用 BaoStock 独立服务和 AKShare 两个明确 upstream。常态采集 BaoStock(A) 与
AKShare/腾讯(B)；任一调用失败、缺行、质量失败或字段分歧时，按受影响证券/日期调用
AKShare/新浪(C)。至少两个独立来源在冻结的字段、
单位和精度规则内一致，才可形成发布候选；不以简单多数绕过单位、复权或缺行冲突。

“一致”只指两个来源都实际持有同一 `(code, trade_date)` 行且字段在同一版本化阈值内一致。
若任一来源持有行，另外两个来源的共同缺行不得形成该行的值共识，也不得选择一个不存在的
来源行；它必须触发继续取证、显式缺口决定或冲突隔离。仅当冻结请求范围经版本化交易日历
确认无交易时，多个来源的合法整范围空结果才可形成零行结果，且必须使用区别于价格行一致的
原因码和证据。该段是对原有“缺行冲突不得绕过”的澄清，不改变两源共识原则。

结果：BaoStock 与两个 AKShare upstream 分别进入 Raw，`ResolutionDecision` 按证券/交易日
记录选择来源、参与比较来源、规则版本和原因。BaoStock 是常态供数来源，但不是可绕过复核
直接发布的权威源；腾讯是常态互验源，新浪只作异常仲裁且不得用于全市场常态抓取。没有
两源一致时隔离。XTQuant/QMT 不再是生产依赖；Tushare 和 JQData 不进入 Stage 1。

首次 AKShare 三源正式 Job 的 OHLC/`pre_close` 一致，而量额存在最大 50 股/50 元级舍入差。
因此容差必须由 S1-07K3 的 BaoStock、旧 XTQuant 和 AKShare 代表性样本实测后版本化冻结；
不得修改既有 Raw、把历史失败改写为成功，或仅凭两只证券永久设定全市场阈值。
S1-07K4C 第一次实测（2026-08-15，6 只代表性证券 × 三源）经独立验收不通过（S1-07K4C-R1：
代表性覆盖未实际验证、attempt b 无证据、50 极值不可离线复算），候选版本
`comparison-thresholds.daily-bar-unadjusted.k4.v1`（OHLC/`pre_close` 零容差、量额绝对容差
50 股/50 元、相对 0）保持代码但处于待重新验收状态。K4C-R2（36/36、三 objective 未观测）
与 K4C-R3 Phase B（9/9）真实证据、K4C-R4 证据链修复（ADR-023）依次交付后，独立验收于
2026-08-16 通过 K4C-R4 并接受重新冻结该版本（见 ADR-024）。历史 k1 版本保持不可变且继续
可复验。

东财 B 和四角色路由只保留为已提交历史基线；ADR-021 已用腾讯 B、 新浪 C 三角色路线取代。
S1-08R3 开始前必须由 S1-07K4 同步更新来源合同、实现和真实验收证据。

## ADR-005：数据范围与分期

**状态：已接受（2026-08-08 简化）**

Stage 1 首先发布明确范围的沪深 A 股未复权日线 OHLCV/成交额版本。北交所、退市证券、复权信息和历史交易状态必须作为显式缺口报告，不冒充已覆盖；在 Stage 2 用于研究前必须补足并验收 point-in-time 与复权语义。

Stage 1 生产 EOD 运行时只编排 `daily_bar_unadjusted`。其他数据集可以保留框架无关 schema 作为后续合同，但不得进入默认 EOD 枚举、来源矩阵、能力状态机或发布流程。

财务报表、估值、行业和历史指数成分位于 Stage 7。指数成分延后不影响首期保存基准指数行情。

## ADR-006：Qlib 的定位

**状态：已接受**

Qlib 用于数据集、表达式、因子处理、模型训练、预测和实验辅助；QuantKing 自己拥有标准数据、因子注册、标签、股票池、时间切分和版本语义。

结果：Qlib 数据格式是派生视图，不是主数据仓。已发布 Qlib 目录不可原地更新；只有父版本兼容且变化可安全追加时，才允许在新临时目录中基于父版本增量构建，否则执行全量重建。增量结果必须通过清单校验，并定期与全量重建结果校准。

## ADR-007：因子、标签与模型基线

**状态：已接受**

- 10～20 个可人工核对的量价基础因子作为审计集。
- Qlib Alpha158 作为并行基准，不默认成为生产因子集。
- 与实际执行区间对齐的未来 5 个交易日收益为主标签，未来 20 个交易日收益为对照。主标签原则上从 T+1 执行参考价开始，到下一周度调仓执行参考价结束；精确交易日下标和不可成交语义在 Stage 2 固化。
- 先使用线性/Ridge 与 LightGBM；复杂神经网络、强化学习和自动搜索不在首期。
- 训练、验证和测试按时间严格分割，使用滚动或走步验证。
- 首期不自动重训或自动切换模型。每月进行模型健康复核；性能衰减或有效期告警只提示用户，由用户手动创建新实验并重新完成验证、批准和启用。

## ADR-008：动态股票池

**状态：已接受**

每个历史日期使用当时可知的动态股票池。首期至少排除未上市满 120 个交易日、当日停牌、ST/`*ST`、退市整理期和流动性过低证券；阈值通过实验确定。底层数据仍全量保留。

## ADR-009：双层回测

**状态：已接受**

Qlib 用于快速研究回测，Backtrader 用于正式事件驱动验收。两者共享 QuantKing 组合服务输出的目标组合、交易规则和成本假设。Qlib 回测必须使用 QuantKing 自定义 Strategy 适配器，把按日期生成的 `TargetPortfolio` 作为唯一目标权重来源；正式回测和双引擎校准禁止使用 Qlib 内置组合构造策略重新选股或计算权重。

结果：正式结论以 Backtrader 验收口径为准。双引擎校准分为零费用、同价、完全成交的基础场景，以及两个引擎共同支持的真实费用、整手、停牌和方向性涨跌停场景；引擎不共同支持的精细规则由共享规则内核测试并记录差异。

## ADR-010：首个组合和执行时序

**状态：已接受**

- 每周调仓。
- 选择模型分数最高的 30 只股票，基本等权。
- 单股目标权重不超过 5%，保留 3% 现金缓冲。
- 使用换仓缓冲和可配置换手限制。
- T 日收盘后生成信号，默认 T+1 开盘执行。
- 不使用杠杆、融资融券或卖空。

具体换手、流动性和回撤阈值在模拟账户阶段结合资金规模和券商费率确定。

## ADR-011：交易、账户与对账

**状态：已接受（2026-08-08 修订）**

系统不提交券商订单，也不集成 QMT 或其他券商 SDK。用户在券商客户端人工下单；未来账户、持仓和成交通过用户确认的文件导入或人工录入进入待确认区，经确认后才写入策略账。

数据模型可保留多账户、多策略和成交归属能力，但首期只验收一个模拟账本和一个策略；不要求启动券商客户端或配置账号密码。

## ADR-012：风险门禁与模型审批

**状态：已接受**

建立独立风险服务，供回测、模拟和未来实盘计划共用。规则分为 `INFO`、`WARNING`、`BLOCK`；只有可覆盖规则允许填写原因后继续，数据泄漏、资金不足、非法数量等硬阻断不得覆盖。

模型生命周期为草稿、已训练、已验证、已批准、使用中、已停用。只有使用中的模型能产生正式计划。

## ADR-013：Web 与后台任务

**状态：已接受**

采用 FastAPI + React + TypeScript + Vite + Apache ECharts。生产时由 FastAPI 提供同源 API 和已构建静态前端，只监听 `127.0.0.1`。

长任务使用 SQLite 持久化队列和独立本地工作进程；不使用 Redis/Celery。Worker 把低频、可恢复的进度事件写入 SQLite `JobEvent`，Web 按单调事件游标读取并通过 SSE 推送；完整任务日志仍保存在 JSON Lines 中，不逐条写入 SQLite。参数可在 Web 修改，但每次修改产生新版本。

首期 EOD 流程由用户从 Web 或 CLI 手动触发同一个应用服务，不建设内部调度器，也不依赖 Windows 任务计划。

## ADR-014：Python 与依赖

**状态：已接受**

统一使用 Python 3.12；使用 `uv`、`pyproject.toml` 和锁文件管理解释器与依赖。BaoStock、
AKShare 和 Qlib 使用明确锁定版本，不跟随开发分支。

## ADR-015：日志与审计

**状态：已接受**

运行日志和任务日志使用结构化 JSON Lines，并在 Web 实时查看；SQLite 只保存低频任务事件、日志索引、摘要和业务审计。普通日志默认保留 90 天，审计记录、失败验收报告和正式产物不自动删除。

## ADR-016：原子发布、备份与恢复

**状态：已接受**

数据、因子、模型和回测均通过“临时目录 → 完整计算 → 质量检查 → 清单 → 原子发布”。失败产物保留且不得改成成功。

备份分为 `E:\QuantKingV3Data\backups` 本地恢复快照与 `F:\QuantKingV3Backups` 异盘灾难备份，并定期执行恢复演练。Warehouse 异盘备份采用分区清单和 SHA-256 驱动的增量复制，只传输缺失或变化对象；备份发布必须有完成标记和实际恢复验证，复制过程不得镜像删除历史或失败产物。具体工具由 Stage 0 的 Windows/F 盘实测确定。

## ADR-017：旧项目迁移

**状态：已接受（2026-08-12 修订）**

旧项目 `E:\QuantKing` 不整体搬迁。候选内容按 `REUSE / ADAPT / REWRITE / DISCARD / DATA_ONLY` 审计，并只在目标模块所属阶段迁移。任何代码复用都必须重新验证依赖、数据口径、前视风险、测试和新模块边界。

Stage 0 清单中的 `data/kline/1d/*.parquet` 是 Stage 1 明确指定的 `DATA_ONLY` 候选。迁移必须
从旧根只读，以独立导入批次复制到 `E:\QuantKingV3Data` 的受管 Raw/staging；先记录源文件
相对路径、大小、mtime、SHA-256、字段/单位/复权语义和覆盖，再转换为 Canonical 候选。
旧 XTQuant 只能标识为 `provider=XTQUANT_LEGACY` 的历史来源，不能冒充当前 XTQuant 能力，
也不能因文件存在而直接发布。至少与 BaoStock 或 AKShare 一个独立来源复核；分歧行进入
Difference/Resolution，无法形成两源一致时隔离。禁止修改旧项目、整体复制代码或启动 QMT。

## ADR-018：Stage 1 控制面简化

**状态：已接受（2026-08-08）**

Stage 1 以尽快交付真实、明确范围的未复权日线版本为复杂度边界，不建设通用数据平台或机构级证据系统：

- K1 删除 XTQuant/QMT 生产路线、旧 `DataSource` 候选集合、`SourceCandidateAssessment` 和
  24 单元 source×dataset 矩阵；K3 历史基线曾扩展到东财/腾讯/新浪，ADR-021 的现行固定来源
  为 `BAOSTOCK`、`AKSHARE/TENCENT|SINA` 和仅迁移使用的 `XTQUANT_LEGACY`。
- K1 的生产运行时只支持 `daily_bar_unadjusted`；其他五类历史 schema 不得驱动 EOD 分支。
- K2 最初使用 `ResolutionDecision` 表达 A/B/C 正常仲裁；K3 扩展到固定 A/B/C/D，但不改变
  该领域合同。`SourceOverride` 的申请、批准、claim、attempt 和 child-version 状态机保持
  退役。人工改判如未来确有需求，必须另立任务和新 DatasetVersion。
- K2 在改造生产 EOD 时把恢复事实收敛为冻结 `JobRun`、Raw manifest、已发布 DatasetVersion manifest 和 JSON Lines 日志。中断任务标记 `INTERRUPTED`；重试创建新 Job，并以输入指纹复用已经完整发布的版本。不得新增独立的 success/failure evidence、forensic success、authoritative failure 或多层 checkpoint 合同。
- 已验收的 S1-01～S1-06 代码在 K1/K2 替换前可作为过渡实现存在，但历史验收只证明当时范围，不得覆盖本 ADR 的目标边界。

DataCenter 日常读侧是否从深层复验收敛为“发布时完整验证 + 显式完整性检查”，在 K2 完成后单独评审；当前不作为 K1/K2 阻断项。ADR-009 双回测和 ADR-012 模型生命周期分别留到 Stage 4、Stage 3 开始时复核，本次不提前修改。

## ADR-019：东财适配器显式 IPv4 解析策略（S1-07K3B）

**状态：已退役（2026-08-14；历史诊断证据保留）**

2026-08-13 真实探针诊断确认：东财 IPv6 CDN（`push2hisipv6.trafficmanager.cn`）TCP 可连
但 HTTP 数据被断，urllib3 优先 IPv6 导致 `RemoteDisconnected`；同一出口的 IPv4 路径曾返回
完整数据。因此 AKShare/东财适配器在每次 vendor 调用期间把地址解析限制为 AF_INET
（策略版本 `eastmoney-ipv4-only.v1`），并写入 SupplierResponse provider_payload 的
`resolution_policy_version`；腾讯/新浪不受影响。

这是显式的按上游地址族策略，不是 IP 规避、UA 伪造或静默换源；当网络或 CDN 路径修复后，
应通过新证据和新决策撤销。同日后观测到东财对本出口 IP 的临时限流（同一 curl 请求先 200
后 56，python-requests 与 .NET TLS 栈同断），该现象属于服务端侧状态，代码不处理。

证据：`docs/stage1/evidence/k3b_eastmoney_ipv6_root_cause_2026-08-13.json`、
`docs/stage1/evidence/k3b_eastmoney_throttling_2026-08-13.json`。

## ADR-020：拒绝 FTShare 作为 Stage 1 常态 B 源

**状态：已接受（2026-08-14）**

FTShare 不进入 QuantKing V3 的依赖、来源 registry、生产 EOD 路由或发布血缘。R3 证明固定
SDK 在省略 `adjust_kind` 时可以返回单证券日线，但后续资格验收的双证券 batch 返回标识集合
不符合冻结合同并按失败即停规则停止；更关键的是，官方资料没有提供股票 K 线底层供应商
独立性、行情数据使用许可、返回单位、修订政策、调用配额或 SLA，`0.1.1` 也没有可由 Python
包索引解析的正式发行物。SDK 的 MIT 许可证不得替代行情数据许可。

R1～R3 与资格验收事实作为拒绝证据保留，临时候选探针代码不合入生产代码。不得因单证券
接口曾成功而将 FTShare 静默映射为任何既有 upstream。后续来源角色由 ADR-021 取代本段
“重新待选”状态；FTShare 仍保持拒绝。

证据：`docs/stage1/S1-07K3_FTSHARE_VALIDATION.md`、
`docs/stage1/evidence/k3_ftshare_qualification_attempt_2026-08-14.json`。

## ADR-021：Stage 1 三角色来源路线

**状态：已接受（2026-08-14）**

Stage 1 正式目标路线固定为：`A=BAOSTOCK/BAOSTOCK` 常态供数、
`B=AKSHARE/TENCENT` 常态互验、`C=AKSHARE/SINA` 异常仲裁。正常任务每次调用 A/B；A/B 任一
失败、缺行、质量失败或字段分歧时，只对受影响证券/日期调用 C。至少两个实际持有同一行的
来源在冻结规则内一致才允许发布；无两源一致即隔离。优先级为 A、B、C。

东财因网络/限流和可用性证据不足退出正式来源集合，FTShare 按 ADR-020 拒绝。两者的历史 Raw、
失败记录、适配器诊断和验收证据保留，但不得产生新的正式调用或进入新 Job 指纹。已提交的
A/B/C/D 实现是迁移前基线，必须由 S1-07K4 删除东财 runtime/registry/预算分支并将腾讯、 新浪
重排为 B/C；历史 migrations 和不可变证据不得改写。

腾讯与新浪虽然都通过 AKShare 客户端接入，仍必须按不同 upstream、endpoint、Raw、质量运行和
错误状态分别记录。AKShare 客户端相同不等于上游相同；但生产验收仍须以代表性真实探针确认
两者没有静默共用同一返回、字段单位和修订语义已冻结，并为每次单调用提供硬超时。旧 XTQuant
仍仅是 `DATA_ONLY` 历史候选 X，不计入日常 A/B/C 路由。

## ADR-022：K4C-R3 缺失场景改用上市前合法整范围空（用户接受的合同修订）

**状态：已接受（2026-08-16；用户明确表示本次决定只批准合同修订，不构成 R3 真实网络授权）**

原 Phase A 样本合同里的临时 `partial-missing`（`vendor_partial_missing`）样本无前缀、无法
合法化，予以替换：R3 的 `missing_or_empty_upstream` 样本改用**上市前合法整范围空**合同
`legal_full_range_empty_pre_listing`：

- 样本：688825.SH，`listing_date=2026-07-27`，`target_window=2026-07-24..2026-07-24`；
  上游请求窗最晚不超过 2026-07-24（请求范围不得跨上市日）。
- `expected_roles` A/B/C 全部 `EMPTY`；解释必须是 `legal_pre_listing_empty`，永不声称
  `vendor_partial_missing`；成功原因码 `LEGAL_EMPTY_PRE_LISTING_OBSERVED`。
- 成功条件：三次调用全部 SUCCEEDED+complete+`upstream_row_count=0`；零 raw 行、零
  canonical 行；请求合法且窗口完全在上市日前；`K4ExpectedSessionResolver` 判定无预期
  交易会话；独立合法空分类；composite 零行原因码 `ZERO_ROW_LEGAL_EMPTY_CONSENSUS`。
- 禁止：error→empty、无效参数、窗口跨 2026-07-27、预期交易日缺行、raw 有行而
  canonical/comparison 空、缺上市/日历参考、与停牌重复计数、声称 `vendor_partial_missing`。

离线仲裁测试（两源持有→只比较持有、单源持有→隔离、共同缺行→无共识）保持不变。反向测试
覆盖：错误/缺失上市日期或参考被拒、窗口跨上市日被拒、交易日节假日不得冒充上市前空、
任一来源有行即失败、任一来源 FAILED/PARTIAL 即失败、verifier 拒绝 raw 有行而 comparison
空、合法三源空只产生零行原因码、停牌与上市前空不重复计数。

三份独立参考文件 SHA 冻结（`docs/stage1/evidence/k4c_r3_references/`）：
停牌 `73e9cbec…`、低流动性 `060aac79…`、上市前空 `4ebd0410…`；R3 零网络 dry-run
`docs/stage1/evidence/k4c_r3_probe_dry_run_2026-08-16.json`（SHA `174c9546…`，
plan_sha256 `38c88f40…`）。R3 Phase B 真实网络执行（预算 9 次调用）仍需单独授权。

**执行结果（2026-08-16，三次一次性授权全部消费完毕）**：第一次尝试
`K4C-R3-4f9c2e7a1b08` 在第 1/9 次失败即停（BaoStock 对停牌日返回 OHLC 填充、
volume/amount 为空的占位行，冻结语义报 `BAOSTOCK_SCHEMA_DRIFT`）；经一次有界诊断
（1 次 BaoStock 调用，证据 `k4c_r3_diagnostic_baostock_688237_2026-08-16.json`，SHA
`c31febc8…`）确认占位行形态后修复（K4C-R3-R1：适配器与 raw 重建跳过 `tradestatus=0`
且 volume/amount 为空的停牌占位行，不产生 bar、不喂 `pre_close` 推导，含反向测试）；
第二次尝试 `K4C-R3-7c51a3d9e2b4` 前 6 次调用全部成功、第 7 次因执行器未把冻结日历的
合法空分类传入归一化而失败即停；修复后**第三次尝试 `K4C-R3-2b8e5f14a6c9` 9/9 成功**
（低流动性 688237.SH/2026-07-15 三源金额 ≤2.5e8 观测成立；688825.SH 三源零行+
日历证明 `LEGAL_EMPTY_PRE_LISTING_OBSERVED` 成立；停牌在 R3 本地因无横截面反例
如实未观测）。composite evidence（`k4c_composite_evidence_2026-08-16.json`，SHA
`910010da…`）经合并行再观测以 R2 行作反例观测停牌：**8/8 objective 成立、
`representative_evidence_sufficient=true`、`threshold_reacceptance_eligible=true`**，
离线 verifier 复算通过。R3 evidence SHA `1632ee8b…`、manifest SHA `2b2f919b…`。
k4 阈值重验收仍待独立验收。

## ADR-023：K4C-R4 证据链验证与停牌来源闭链修复（离线验收修复层）

**状态：已接受（2026-08-16；R4 为离线证据链修复，不构成真实网络授权或验收通过）**

独立验收发现三个阻断并据此离线修复：

1. `K4CVerifier.verify` 现在验证 evidence 全部绑定字段：`schema_version`、identity
   （authorization_id/attempt_id）、marker、plan_sha256、budget 必须从 attempt header
   与冻结 verifier 合同重算；`artifact_manifest_sha256` 直接对 manifest 文件计算；
   `artifacts_root` 规范化后必须精确指向被验证 attempt 目录；retries/fail-fast/
   数据根写入/failure 与冻结合同及 journal FAILED 事实一致；`sufficiency_reasons`
   与重算逐项一致；R3 `planned_objectives` 必须等于冻结样本序列；R3 `calendar_proofs`
   必须等于冻结日历常数导出的规范证明；顶层字段白名单之外的一律拒绝。稳定错误码：
   `EVIDENCE_SCHEMA_MISMATCH`、`EVIDENCE_HEADER_BINDING_MISMATCH:<field>`、
   `EVIDENCE_CONTRACT_BINDING_MISMATCH:<field>`、`EVIDENCE_MANIFEST_SHA_MISMATCH`、
   `EVIDENCE_ARTIFACT_ROOT_MISMATCH`、`EVIDENCE_SUMMARY_MISMATCH:<field>`、
   `EVIDENCE_PLANNED_OBJECTIVES_MISMATCH`、`EVIDENCE_CALENDAR_PROOF_MISMATCH`、
   `EVIDENCE_UNKNOWN_FIELD:<name>`。
2. `K4CCompositeVerifier` 增加 schema 支持：历史 v1 保持可复验；新 v2
   （`s1-07k4c-composite-evidence.v2`）要求 composite 自带的 `r2_component`/
   `r3_component` 与冻结 `*_component_reference().to_dict()` 完全一致（任一字段变化、
   缺失或类型错误 → `COMPOSITE_COMPONENT_REFERENCE_MISMATCH:R2/R3`）、
   `verification_contract_version` 冻结、`sufficiency_reasons` 与重算一致、schema 错误
   → `COMPOSITE_SCHEMA_MISMATCH`；这些失败不得被 structural 过滤忽略。
3. 停牌来源闭链（R4 provenance hardening）：新增官方上交所公告参考
   `docs/stage1/evidence/k4c_r4_references/suspension_688237.SH_2026-07-16_sse.json`
   （证券 688237.SH、公告编号 2026-031、停牌起始 2026-07-16、来源类型
   `exchange_announcement`、`provenance_hardening_only`、URL
   `https://static.sse.com.cn/…/688237_20260716_Y6W3.pdf`；未下载远端 PDF、未伪造远端
   SHA），本地 JSON SHA-256 冻结并由 v2 composite 精确绑定。停牌 objective 只有同时
   满足——样本即 688237.SH/2026-07-16、A/B/C canonical/comparison 均无该日、R2 同日
   横截面反例由 A/B/C 三源共同持有、官方参考通过 SHA/证券/日期/公告编号/来源类型检查、
   BaoStock raw `tradestatus=0` 且 volume/amount 为空——才成立；任一缺失返回
   `SUSPENSION_NOT_OBSERVED` 或具体稳定来源错误码（`SUSPENSION_REFERENCE_MISSING`、
   `SUSPENSION_RAW_FACT_MISSING`、`SUSPENSION_RAW_STATUS_MISMATCH` 等）。

R4 v2 composite（`k4c_r4_composite_evidence_2026-08-16.json`，SHA `87b04863…`）重算
248 行、8/8 objective、0/50 覆盖并离线验证通过，是当前复验候选；旧 v1 composite 保留为
历史产物并通过字节保护。**2026-08-16 独立验收已通过 K4C-R4 并接受重新冻结
`comparison-thresholds.daily-bar-unadjusted.k4.v1`（见 ADR-024）；K4C 的 K4E 独立
提交与 S1-08 仍待后续。**

## ADR-024：接受重新冻结 comparison-thresholds.daily-bar-unadjusted.k4.v1

**状态：已接受（2026-08-16，K4C-R4 独立验收通过后的用户决定）**

独立验收通过 K4C-R4（证据链验证与停牌来源闭链修复，ADR-023），用户据此接受重新冻结
阈值规则 `comparison-thresholds.daily-bar-unadjusted.k4.v1`：

- OHLC/`pre_close`：绝对容差 0、相对容差 0（零容差）。
- `volume`/`amount`：绝对容差 50、相对容差 0。

规则值本身自 K4 候选起未变，变化的是状态：从“待重新验收”转为**已接受重新冻结**。冻结的
验收记录为 `k4_threshold_acceptance()`（`K4ThresholdAcceptanceRecord`：规则版本、验收日
2026-08-16、验收方、各字段容差与四条 SHA 绑定证据引用）。历史 evidence 文件（R2/R3/v1/
v2 composite）是在“待重新验收”状态下生成的字节冻结产物，其中
`thresholds_pending_reacceptance: true` 保持原样并可字节一致复验，不得改写；该字段的
历史语义与规则当前已接受状态以代码注释和本 ADR 明确区分。历史 k1 与 k3-candidate 规则
继续可复验。阈值规则接入 EOD 指纹的方式不变（唯一注册表 `thresholds_for`）。

## ADR-025：K4C 状态收口与 K4D inventory 零读取 dry-run 冻结（S1-07K4D-I0）

**状态：已接受（2026-08-16；本 ADR 不构成读取 `E:\QuantKing`、写入 `E:\QuantKingV3Data`、
访问网络或执行 K4D pilot/full 的授权）**

1. **K4C 状态收口**：全部“当前状态”文档统一为——K4C-R4 已于 2026-08-16 独立验收通过，
   `comparison-thresholds.daily-bar-unadjusted.k4.v1` 已按 ADR-024 接受重新冻结。历史
   R1/R2/R3 的失败、阻断与 pending 字段是当时的事实，保持字节冻结并带时间限定叙述；
   旧文档中“样本选择 BLOCKED”“真实复测待授权”“阈值重新验收待独立验收”“下一步是
   K4C-R3 网络执行”等表述已从当前状态移除，仅保留为带时间限定的历史描述。
2. **inventory dry-run 冻结**：新增 `quantking-legacy-import inventory --dry-run`
   （合同 `legacy-inventory-dry-run.k4d.v1`，`LegacyInventoryDryRun` 纯域对象）。dry-run
   构建计划时零文件系统访问：不调用 discover/inventory/os.walk，不对旧根
   exists/is_dir/resolve/stat/open/rglob，不创建 staging，不读 Parquet；real marker
   （`S1-07K4D-INVENTORY-REAL-READONLY-V1`）下同样零访问。计划含
   `reads_source=false`、`writes_source=false`、`writes_staging=false`、
   `publishes_warehouse=false`、`service_network_invocation_upper_bound=0`、
   `application_retries=0`、`requires_new_authorization_for_execution=true` 与稳定
   `plan_sha256`。冻结证据
   `docs/stage1/evidence/k4d_inventory_dry_run_2026-08-16.json`（文件 SHA-256
   `46272de4…`、plan_sha256 `f4fee828…`），已在 evidence README 登记。
3. **Stage 0 基线语义**：5,208 文件 / 527,708,270 字节只作差异比较
   （`stage0_baseline_role=difference-comparison-only`），不是通过条件，也不因差异自动失败。
4. **实际 inventory 执行合同（已就绪、未执行）**：CLI `inventory`（无 `--dry-run`）执行
   `closed_scan`：discover → 候选 size/mtime 快照 → 逐文件 inventory（读取前后 stat 比对 +
   前后双 SHA）→ 再 discover → 再快照；候选新增、删除或元数据变化时以
   `LegacyCandidateSetChanged` 判定整个 inventory 失败，不产生可供 pilot 使用的成功清单。
   输出保持 `legacy-inventory.k3c.v1`（pilot 可直接消费，K3C 历史清单 payload 继续可读）。
   inventory 不写 staging、不复制文件、不做 Canonical 转换、不调用 A/B/C。
5. **授权边界不变**：K4D inventory、pilot、full 是三次独立授权，必须分别申请；真实
   inventory 授权须引用 dry-run 的 plan SHA 与
   `S1-07K4D-INVENTORY-REAL-READONLY-V1`。

**执行结果（2026-08-16，用户一次性授权已消费）**：真实只读 inventory 按上述合同执行一次
并成功（closed scan 197.1s、零重试、零 staging 写入、零网络）：5,208 文件 /
527,708,270 字节，与 Stage 0 基线差异 0/0；16,786,177 可读行、零读取错误、零重复键、
单一 Parquet schema（`code`/`date` 为 string，date=`yyyymmdd`，OHLC/volume/amount 为
double，无 `pre_close` 列）；date 覆盖 19901219..20260603，code 覆盖
000001.SZ..689009.SH。证据 `docs/stage1/evidence/k4d_inventory_2026-08-16.json`
（文件 SHA-256 `140978ea…`、source_fingerprint `af42dd43…`）冻结并有字节保护测试。
该授权不扩展到 pilot/full：二者仍须分别授权，pilot 前须另行确认 `date` 字符串日期与
`pre_close` 缺列的显式语义映射（`LegacyImportSemantics.confirmed` 仍要求逐字段确认）。

## ADR-026：K4D pilot 离线合同、执行器与零外部 I/O dry-run（S1-07K4D-P1）

**状态：已接受（2026-08-16；本 ADR 只冻结 pilot 离线合同与 dry-run，不构成真实 pilot
授权：不读取旧根、不调用 A/B/C、不写 `E:\QuantKingV3Data`、不访问 F 盘）**

1. **K4D 语义合同 `legacy-import-semantics.k4d.v1`**（`LegacyK4DSemantics`，纯域对象）：
   针对真实旧 schema（`code`/`date` string（yyyyMMdd）、OHLC/volume/amount double、无
   `pre_close`、无 suspension 字段、全字段零 null）的显式无猜测映射——`trade_date -> date`、
   `date_format=yyyyMMdd`（严格八位、无 locale）、
   `pre_close_policy=derive_from_previous_unadjusted_close`（按 code+date 排序由前一条
   未复权 close 派生；每证券首行 Raw 保留、Canonical 排除并计数，不得用 open/当日
   close/供应商昨收填补）、
   `suspension_policy=derive_from_price_equals_pre_close_and_zero_volume_amount`（零量且
   OHLC 全等于派生 pre_close 才是停牌占位；零量但价格变化、单项为零或字段异常隔离；
   缺失交易日保持缺行不合成）、`missing_suspension_row_policy=
   preserve_as_absent_do_not_synthesize`、`null_policy=reject_required_field_nulls`、
   `timezone=Asia/Shanghai`、预期 `unadjusted=true`、volume=share、amount=cny_yuan。
   状态字段区分 `HYPOTHESIS`/`CONFIRMED`；**离线 dry-run 只能冻结 HYPOTHESIS**。K3C
   historical `LegacyImportSemantics`（源端已有 pre_close/suspension 的假设）保持不变，
   历史 payload 与测试继续可验证。
2. **单位/未复权确认规则**（`confirm_k4d_units`/`prove_k4d_unadjusted`，均要求 K4
   阈值版本）：volume 候选尺度 {1,100}、amount 候选尺度 {1,1000}；只有 factor=1 在 K4
   阈值内对每个持有行的服务源稳定成立且无其他尺度同时成立、共同持有样本 ≥20、volume
   有限非负且积分、amount 有限非负，才确认 share/cny_yuan；否则返回稳定失败原因
   （`UNIT_SCALE_FACTOR_ONE_NOT_SATISFIED`/`UNIT_SCALE_AMBIGUOUS`/
   `UNIT_SAMPLE_INSUFFICIENT` 等）。未复权证明必须观察到一个边界：服务 Raw 参考昨收与
   完整未复权序列派生 pre_close 相对差 ≥0.5%，且 X 的 close 与未复权服务行情在 K4 内
   一致；无边界时如实返回 `SEMANTICS_UNADJUSTED_NOT_PROVEN`，普通日一致不冒充证明。
3. **A/B/C 调用与预算合同**（冻结在 plan 中）：固定窗口 `2025-05-01..2026-06-03`；
   6 证券 × A/B/C = 18 次 endpoint（各 6）；腾讯 HTTP 上界 18、新浪 12、HTTP 总 30、
   BaoStock 协议消息 18；BaoStock 会话 20s、AKShare 受控子进程 60s；应用层零重试、
   任一失败即停；仅使用冻结 K4 adapter/单位策略/未复权参数；禁止东财、FTShare、
   XTQuant runtime。
4. **K4 阈值绑定修复**：`LegacyRecheckEngine` 构造器改为显式必传 `ThresholdSet`
   （禁止 silent default），K4D 路径必须绑定
   `comparison-thresholds.daily-bar-unadjusted.k4.v1`（OHLC/pre_close 0/0、volume/amount
   50/0）；四类行分类（`X_A_CONSENSUS`/`SERVICE_CONSENSUS_REPLACED_X`/`QUARANTINED`/
   `X_ONLY_UNVERIFIED`）对全部 Canonical 行守恒；pilot 永不发布 Warehouse。
5. **Pilot staging 合同**（`LegacyPilotExecutor`）：一次性 attempt 守卫（attempt 目录
   存在性 + 排他 header + idempotency key）→ 只读读取六个 inventory 文件（前后
   size/mtime/SHA 校验）→ 每证券一次 fetcher 调用（A/B/C）→ 语义确认（单位+未复权）
   **成功后**才写 staging → 复制六文件（独立物理副本、禁 hardlink、复制前后校验）→
   写 Canonical candidate、语义确认、A/B/C service 表+vendor 参考昨收、recheck artifact、
   manifest、COMPLETE。batch 身份绑定 plan SHA、inventory SHA/fingerprint、六文件清单、
   语义确认 SHA、service evidence SHA、K4 阈值版本、adapter/pyarrow 版本；失败保留
   attempt failure.json 与 `.failed` 批次，不得改成成功；`verify_legacy_pilot_batch`
   从 raw 完整离线重建 candidate/确认/recheck/manifest 并校验文件集合。真实 marker 下
   Canonical 行数必须等于冻结预期 26,194。不创建 Warehouse DatasetVersion、不切 current
   pointer。
6. **Pilot dry-run**（`quantking-legacy-import pilot --dry-run --inventory-file <冻结
   inventory> --plan-file <输出> --marker real`）：纯计划构建，零旧根/网络/staging/F 盘
   I/O；从冻结 inventory 精确重算并绑定六文件（000001.SZ/000858.SZ/300750.SZ/600000.SH/
   688237.SH/688981.SH；合计 6 文件、878,893 字节、26,200 Raw 行、预期 Canonical 26,194
   行=26,200−6 首行）；包含全部预算/超时/语义 HYPOTHESIS/K4 阈值版本与稳定
   `plan_sha256`；已存在 plan 文件时按字节幂等。冻结证据
   `docs/stage1/evidence/k4d_pilot_dry_run_2026-08-16.json`（文件 SHA-256
   `c77b2d8e…`、plan_sha256 `76e928aa…`）。
7. **真实执行停止纪律**：真实执行入口是 env 守卫的一次性执行器
   （`tests/compatibility/test_k4d_pilot_real.py`，marker `real_k4d_pilot`，默认跳过），
   需要 plan 文件 + 新 authorization_id + marker `S1-07K4D-PILOT-REAL-MIGRATION-V1` +
   固定 attempt 目录 + idempotency key（如 `k4d-pilot-20260816-v1`）；同一
   authorization_id 只能启动一次；失败保留证据并停止，不得重跑/换证券/换窗口/扩预算。
   **真实 pilot 尚未执行，须用户单独授权。**

**执行结果（2026-08-16，用户一次性授权已消费；第一次 attempt 失败即停）**：按 plan SHA
`76e928aa…` 与 authorization_id `K4D-PILOT-30fb6c198a0e` 执行（idempotency key
`k4d-pilot-20260816-v1`）。第 6 次 endpoint 调用（000858.SZ × 新浪）抛
`SourceAdapterError`，fail-fast 即时中止：`E:\QuantKingV3Data` **零写入**（无
`legacy-pilot-imports`/`legacy-pilot-idempotency`），attempt 目录仅保留
attempt-header.json（STARTED）与 failure.json（`PILOT_SERVICE_FETCH_FAILED:000858.SZ`）。
调用计数由 fetcher 顺序推断为 5 成功 + 1 失败 + 12 未调用；**无逐调用 journal、
failure.json 未保留原始稳定错误码**——两个证据链缺陷已识别，重试前须离线修复：
(a) 执行器/真实执行器增加逐调用 journal（K4C `invocations.jsonl` 模式）；
(b) failure 证据解包并记录 `SourceAdapterError` 的稳定码与脱敏原因链。修复后按 K4C-R3
先例以新 authorization_id 另行授权重试；本授权不得重放，失败证据不得改写。

**证据链修复（2026-08-17，纯离线，用户已授权）**：两个缺陷已按合同修复并通过定向测试。
(a) **逐调用 journal**：执行器在 attempt 目录写 `invocations.jsonl`
（schema `legacy-pilot-invocation.k4d.v1`），每条记录一行 canonical JSON、append +
flush + fsync、单调 `sequence`；执行器对每次 fetcher 调用记录 `scope=SYMBOL`
SUCCESS/FAILED（SUCCESS 含各源行数），真实执行器 fetcher 对每次 endpoint 调用记录
`scope=ENDPOINT` SUCCESS/FAILED（SUCCESS 含行数、raw payload SHA、库版本）。journal
写失败按 `PILOT_JOURNAL_WRITE_FAILED` fail-closed，但在 fetch 失败路径中被抑制，
不得掩盖原始失败。
(b) **稳定错误码解包**：`unpack_source_error` 把 `SourceAdapterError` 解包为稳定、
脱敏的五元组（`code`/`network_reason`/`failure_kind`/`retryable`/`message`，全部来自
adapter 边界合同，不含第三方原始文本）；非 `SourceAdapterError` 一律 `null`，不记录
不稳定 repr。`PILOT_SERVICE_FETCH_FAILED:{symbol}` 的 failure.json 现在包含 `symbol`
与 `source_error` 五元组，journal 的 SYMBOL/ENDPOINT FAILED 记录同样携带该五元组。
旧 failure.json（仅四字段）仍是合法历史形态；第一次 attempt 的冻结证据目录字节不变，
由测试 `test_first_real_pilot_attempt_failure_evidence_is_frozen_as_failed` 持续守卫。
合成测试新增：成功 journal 六条 SYMBOL SUCCESS、`SourceAdapterError` 稳定码解包进
failure.json 与 journal、逐端点 journal（含 ENDPOINT FAILED 携带稳定码）。重试真实
pilot 须以新 authorization_id 另行授权。

**第 2 次 attempt 执行结果（2026-08-17，用户一次性授权已消费；失败即停）**：按同一
plan SHA `76e928aa…` 与新 authorization_id `K4D-PILOT-d0280d367480`（idempotency
key `k4d-pilot-20260817-v1`）执行（72.6s）。**18/18 A/B/C endpoint 调用全部成功**
（修复后的 invocations.jsonl 24 条全 SUCCESS 留证：三源各 symbol 窗口行数完全一致，
263×4+258+257=1,567 行）。失败发生在第 3 步纯转换后的冻结守卫：
`PILOT_CANONICAL_COUNT_UNEXPECTED`——真实旧六文件按 K4D 语义转换得 Canonical
17,585 行 ≠ 冻结预期 26,194（该预期假设零隔离：26,200−6 首行）。`E:\QuantKingV3Data`
零写入（守卫位于语义确认与批次写入之前）；attempt 目录冻结 attempt-header.json +
failure.json + invocations.jsonl。

**诊断结论（纯离线，`k4d_pilot_attempt2_diagnostic_2026-08-17.json`，SHA
`b75f27b4…`）**：隔离 8,609 行 = `NEGATIVE_VALUE:open` 8,503 + `NEGATIVE_VALUE:low`
105 + `ZERO_VOLUME_PRICE_MOVEMENT` 1；守恒 17,585+6+8,609=26,200 成立；停牌占位
188。负 OHLC 集中于 000001.SZ/000858.SZ/600000.SH 的 1991–2008 早期历史（300750.SZ
仅 IPO 日单行且按首行排除；688xxx 零隔离），形态包括恒定负 OHLC 零量占位与**伴随
真实成交的负价格**（000858.SZ 1998-04 open=-30.27、量 341,630、额 18.4 亿）——与
旧 XTQuant 导出早期历史为**前复权数据**的假设一致：`NEGATIVE_VALUE` 隔离规则按设计
正确拦截，fail-closed 守卫按设计正确触发，被证伪的是冻结预期值（假设零隔离）而非
执行器或语义规则。**待用户决策**：(a) 接受真实隔离分布并修订 pilot 合同冻结预期
（Canonical 17,585 + 隔离 8,609/原因分布）后以新 authorization_id 重试；或 (b) 先
扩大诊断面（如全量 5,208 文件的负值分布离线扫描）再定合同。本次授权已消费，不得
重放；两次失败证据均不得改写。（该"待用户决策"已由 ADR-027 作出最终处置：拒绝迁移，
两项备选均不执行。）

## ADR-027：拒绝旧 XTQuant DATA_ONLY 日线迁移（S1-07K4D-CLOSEOUT）

**状态：已接受（2026-08-17）**

1. **候选地位**：ADR-017 把旧项目内容定义为 `REUSE/ADAPT/REWRITE/DISCARD/DATA_ONLY`
   候选分类，不保证任何候选最终可迁移。旧 XTQuant 日线自始只是 `DATA_ONLY` 候选，
   不是生产依赖；XTQuant/QMT 运行路线已退役（ADR-021 路线下正式来源为 BaoStock(A)、
   腾讯(B)、新浪(C)）。
2. **评估事实**：K4D 只读 inventory 成功（ADR-025：5,208 文件/527,708,270 字节全部
   可读、零读取错误、Stage 0 基线差异 0/0），但两次真实 pilot 均未形成可接受迁移
   结果（ADR-026）：第 1 次 attempt 服务调用失败即停；第 2 次 attempt 18/18 调用
   成功，但六文件 26,200 行中 8,609 行（约 32.9%）因负 OHLC 等违反未复权 Canonical
   契约被隔离（守恒 17,585+6+8,609=26,200），冻结守卫 `PILOT_CANONICAL_COUNT_UNEXPECTED`
   正确触发。两次 attempt 均保持 FAILED 历史状态，staging 零写入。
3. **拒绝理由**：(a) 32.9% 的隔离比例证明这是系统性语义问题，不是少量偶发脏行；
   (b) 负 OHLC 只是疑似前复权数据的显性症状，剩余正数行不能仅因数值为正被证明为
   未复权；(c) 把 pilot 预期机械改为 17,585/8,609 只适配六个样本，不能证明全部
   5,208 文件的语义；(d) 全量负值扫描只能量化显性负值，不能识别全部正数前复权行，
   也不能恢复可靠的未复权价格；(e) 对单用户个人量化项目，继续建设旧数据复权识别
   与修复流程的收益不足以覆盖复杂度与数据风险。
4. **用户决定（2026-08-17）**：停止继续挽救。不执行第 3 次 pilot、不执行 5,208
   文件全量负值/复权诊断、不执行 K4D full；旧 XTQuant 日线从 `DATA_ONLY` 待迁移
   候选调整为"评估完成、迁移拒绝"，K4D 决策收口完成。
5. **处置边界**：原始旧文件保持不变，不删除、不覆盖；inventory evidence、两次失败
   attempt 目录与第 2 次诊断 JSON 字节冻结不变；`XTQUANT_LEGACY` 数据不进入正式
   Raw、staging、Canonical、Warehouse 或任何发布候选。本决定不影响 BaoStock/腾讯/
   新浪正式数据路线，也不修改 K4 0/50 阈值、`NEGATIVE_VALUE`/未复权证明/隔离/
   fail-closed 规则与既有 legacy importer 离线审计框架。
6. **重启条件**：未来若要重新研究旧数据，必须作为新的独立任务重新立项和授权，
   不得重放任何历史 authorization_id。
7. **对后续阶段的影响**：旧 XTQuant full 不是 K4E、S1-08R2/R3 或 Stage 1 完成的
   前置条件；Stage 1 对旧迁移的完成条件由"成功迁移并复核"改由"可复验的阻断/拒绝
   结论"满足（本决定即该结论）。下一步是 K4E 独立验收与提交。

## ADR-028：个人本地信任模型与适度授权控制（S1-08R3F-LITE）

**状态：已接受（2026-08-18）**

1. **产品边界**：QuantKing V3 是单用户、本机运行的个人量化软件。本地用户与被
   用户启动的开发 Agent 属于同一管理边界。
2. **授权机制的目标**：防止误执行、防止范围意外扩大、防止同一任务意外重放、
   保留可审计事实。授权机制不以防御本机管理员或恶意本地进程为目标。
3. **内容 SHA 的角色**：envelope/plan/manifest 的内容 SHA 用于批准内容的完整性
   与漂移检测，不构成身份认证，不要求 PKI、数字签名服务、HSM、密钥管理系统或
   多层 authorization request/grant/envelope 签名链，也不为每次普通只读访问建设
   复杂授权中心。
4. **历史产物有效性**：E1/C1 各次 attempt header 已证明实际执行使用了批准的
   envelope/plan SHA（代码树与 git HEAD 亦经复验），因此正式入口此前存在的
   健壮性缺陷（token 未绑定内容、C1 plan 只校验 schema version）不自动使历史
   正式产物失效；本 ADR 的修复面向未来执行。
5. **R3C 流程偏差定性**：旧 R3C 探针第 1 次运行已进入生产 `_reuse_published`
   路径并创建审计 Job；第 2 次执行零网络调用、零新版本、零新增业务产物。重复
   执行属于流程偏差，不要求为了修复流程偏差再创建一个真实审计 Job；旧证据字节
   保持不变。
6. **未来探针防重放**：未来真实 R3C 或类似探针使用简单的一次性 attempt ledger
   （绑定 marker、authorization ID、plan SHA、idempotency key 与开始时间），相同
   authorization ID 已存在即拒绝，FAILED attempt 不可复用；不绑定脚本 SHA、不建
   设新的授权框架。
7. **Stage 1 终验要求不变**：Stage 1 最终验收仍必须对正式发布行逐键证明两个
   实际来源（A/B 规范化实际行、阈值重算、selected source 与 PUBLISHED 行相等）；
   证据只需保存与 E1 直接相关的 artifact SHA 与逐键证明，不要求保存整个数据根
   的完整文件 manifest。
8. **数据根路径披露边界**：`E:\QuantKingV3Data` 是项目公开声明的标准本地数据根，
   不是密钥或隐私；允许绝对数据根出现在内部任务、授权、证据与验收文档中。Web、
   DataCenter API 与最终用户响应仍不得返回本机绝对路径。
9. **不降低数据要求**：本 ADR 不降低行情质量、双来源共识、发布原子性与不可覆盖
   要求；`comparison-thresholds.daily-bar-unadjusted.k4.v1`（OHLC/`pre_close`
   零容差、量额绝对 50/相对 0）与其他冻结规则继续有效。
