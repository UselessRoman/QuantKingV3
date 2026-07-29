# Stage 1 任务卡

状态：Stage 0 已完成；Stage 1 已获实施授权并进行中，但尚未完成。S1-01 已于
2026-07-16 在提交 `7e4e5b38b7fc91edaf8727ed55b1e26435373aea` 完成主验收，证据见
[S1-01 主验收证据](S1-01_ACCEPTANCE.md)。
S1-02 已于 2026-07-18 在提交 `79f9fa3e682e212cf74e7886a04c6fb52158f302`
完成主验收，证据见 [S1-02 主验收证据](S1-02_ACCEPTANCE.md)。S1-03-R1 修复中/待主验收。
S1-04 已于 2026-07-22 在提交 `2f99491e9dc410acf85ca81c131a77cae08d0cc3`
完成主验收，证据见 [S1-04 主验收证据](S1-04_ACCEPTANCE.md)。
S1-05 已于 2026-07-27 在提交 `e580d6963e01b1301dc26f29ed57a0b5b4770a0f`
完成主验收，证据见 [S1-05 主验收证据](S1-05_ACCEPTANCE.md)。
S1-06 已于 2026-07-28 在提交 `413805074b3a82c2d2a090d2b73c854a238e2e19`
完成主验收，证据见 [S1-06 主验收证据](S1-06_ACCEPTANCE.md)。
S1-07A-R2 修复已提交、等待主验收；24 个单元仍为
BLOCKED_EXTERNAL_VALIDATION；S1-07B/C/D、S1-08 未开始、未获授权。

真实 XTQuant 和备用源验证仍延期并阻断 Stage 1 最终验收。

每个任务必须声明所属 Stage、范围、输入、测试和验收证据。前一任务的完成条件未满足前不得
开始后一任务。离线质量门通过不等于主验收通过。

## S1-01：数据合同、元数据持久化与安全迁移基础

- **状态**：已于 2026-07-16 在提交 `7e4e5b38b7fc91edaf8727ed55b1e26435373aea` 完成主验收。证据见 [S1-01 主验收证据](S1-01_ACCEPTANCE.md)。
- **范围**：严格 Canonical 合同和空结果分类；DatasetVersion 覆盖与可约束血缘；QualityRun/QualityResult/DifferenceReport 仓储；旧数据 `LEGACY_DATA_ONLY` 来源；绑定差异报告与双方批次的 SourceOverride；不可绕过审计的短事务；终态不可变与新 batch ID 重试；使用 Stage 0 完整快照合同、维护期写边界和不可篡改证据的安全迁移。
- **输入**：S1-01-R1 提交 `64490e1bc15657e0980308f2fdec51a309be4f8e`；原主实现提交 `98a8440dc2b860f4dd46ae592c94ea832f89e811` 保持不变。
- **测试**：失败 run ID 重放/修正后重试和证据不可变攻击；失败证据字段、真实 UTC 时间、脱敏消息和非覆盖写入；override 无直接父版本、跨数据集、审批前版本、正确直接子版本及事务回滚；新库 head 与 Stage 0→`20260716_0003` 升级。
- **验收纪律**：离线测试与 `scripts/quality.ps1` 通过只能证明该任务的离线门禁；S1-01 主验收已确认通过。
- **禁止项**：不实施 Warehouse 发布/查询、数据抓取、质量执行器、EOD、Worker handler、CLI/API、业务 Web、真实访问或旧数据读取；不得开始 S1-02 之外的 Stage 1 任务。

## S1-02：Warehouse、原子发布与查询

- **状态**：已于 2026-07-18 完成主验收。原实现提交
  `3235aba1fe758394bcee39f03945f5cd2ab4aa83`、R1 提交
  `d3d8b17f66d8f0e9e8bb9faaa801b7d1b7d7fb47` 均保留未改写；主验收补强提交为
  `79f9fa3e682e212cf74e7886a04c6fb52158f302`。证据见
  [S1-02 主验收证据](S1-02_ACCEPTANCE.md)。
- **范围**：Raw/Canonical 受管理落地、批次清单和 SHA-256、Canonical Warehouse 分区、临时目录到 DatasetVersion 的原子发布、幂等发布以及 DuckDB 显式版本查询。
- **R1 修复重点**：关闭全部路径边界（source/dataset/batch_id/version_id 严格组件、拒绝 symlink/junction 根与中间路径）；建立唯一应用级 Canonical 发布协调服务；统一严格 published-directory verifier；对齐 manifest 与 DatasetVersion 凭证；修复查询合同（空版本、字段投影、错误映射）；保留脱敏失败证据，删除 `shutil.rmtree` 恢复路径。
- **依赖**：S1-01 主验收通过并获得 S1-02 单独授权。
- **禁止项**：不实现 XTQuant 抓取（S1-03），不提前实现质量和跨源业务（S1-04）。

## S1-03：XTQuant 主数据适配器与增量抓取

- **状态**：S1-03-R1 修复中/待主验收；不得写成已主验收。
- **范围**：六类 Stage 1 数据的窄只读源端口、合成供应商响应标准化、确定性增量计划、Raw 批次凭证、`IngestionBatch` 终态和稳定错误转换。只生成通过 S1-01 schema 验证的 Canonical 候选表，不发布 `DatasetVersion`。
- **R1 修复重点**：以规范 Raw 响应信封绑定请求 SHA、完整 chunk、dataset、供应商合同、标准化 records 和原始 provider payload；落地复验后只从 Raw 重建候选并拒绝任何漂移。直接构造的 chunk/plan 必须重建生成式白名单合同；空供应商响应默认失败；所有 RUNNING 后异常必须收敛为终态；loader 失败必须恢复导入状态。
- **能力边界**：对本机自然可访问的 XTQuant 公共源码仅作了只读签名核对。交易日历可以由已验证的 `get_trading_dates` 合同提供；其余五类的 point-in-time、单位、因子公式或历史交易状态语义未获充分证明，仍是独立主验收阻断，生产 Gateway 必须返回稳定能力不可用错误。
- **依赖**：S1-02 完成；真实 XTQuant、网络或 QMT 访问须逐次单独授权。
- **测试**：默认测试只使用 `tmp_path`、Fake Gateway 和合成响应；不加载 XTQuant，不运行 `real_xtquant`，不访问正式数据根。
- **禁止项**：不实现备用源质量决策，不调用发布协调器，不创建 `PUBLISHED DatasetVersion`，不调用下单或撤单接口。

## S1-04：质量、跨源差异与 SourceOverride

- **状态**：已于 2026-07-22 在提交 `2f99491e9dc410acf85ca81c131a77cae08d0cc3`
  完成主验收，证据见 [S1-04 主验收证据](S1-04_ACCEPTANCE.md)；真实备用源验证仍须逐项授权。
- **范围**：单源质量规则执行、跨源固定样本比较、差异报告、备用源候选判定和显式
  SourceOverride 审批/应用；质量失败阻止发布。
- **子任务**：
  - S1-04A：质量门（`quality` 包、`20260716_0004` 触发器）。
  - S1-04B：跨源比较器与差异报告双方绑定（`comparison`、`assessment` 包，`20260716_0005`）。
  - S1-04C：SourceOverride 应用服务（`override` 包）。
- **依赖**：S1-02、S1-03 完成；备用源真实网络实测须单独授权。
- **禁止项**：备用源不得静默替换主源；override 只能进入同数据集的新 DatasetVersion。
- **真实 XTQuant 样本验证仍须逐项授权；合成数据不能标生产可用。**

## S1-05：EOD、Worker、CLI 与 API

- **状态**：已于 2026-07-27 在提交
  `e580d6963e01b1301dc26f29ed57a0b5b4770a0f` 完成主验收。证据见
  [S1-05 主验收证据](S1-05_ACCEPTANCE.md)。真实网络、QMT 和正式数据根仍未授权。
- **授权**：本实现仅使用已获离线实施授权的范围；该授权不包含任何真实外部访问。
- **范围**：Web/CLI 手动触发的同一 EOD 应用服务、持久 Worker handler、幂等命令和 API；
  串联已完成的 Warehouse、XTQuant 与质量能力。
- **实现**：固定 `data.eod.v1` 任务从持久 request/config/version manifests 重建请求，
  逐数据集执行能力预检、增量 ingestion、BUILDING version、QualityRunner、显式质量后发布
  和发布凭证复验；成功或失败均保留不可覆盖 EOD 证据和脱敏 JSONL/JobEvent。
- **R2 闭环**：公共 CLI/API 只接受精确 `dataset=data_version:...` 基版本；入队 resolver
  复验 PUBLISHED/Warehouse/Quality 后冻结覆盖。通用 runtime 不恢复任务，只有显式 Worker
  按过期 heartbeat 租约条件恢复。Worker 与 evidence 共用严格 frozen-command parser，
  lookback 按实际 FetchChunk 权威切片替换，post-success 故障同时保留 forensic success
  与权威 failure evidence。
- **R3 闭环**：权威终态 verifier 以共享 frozen-command parser 将 success/failure
  证据精确绑定 JobRun 的 input/config/version manifests，并反查 DatasetVersion、
  QualityRun 和 Warehouse 凭证。Worker 在完整 handler 生命周期持续续租；最终成功提交
  故障或进程退出经显式 stale recovery 生成 INTERRUPTED 证据，新幂等键重试严格复用
  checkpoint 对应的唯一已发布版本，不重复抓取、质量运行或发布。
- **R4 闭环**：显式 stale recovery 对已有 `FINAL_SUCCESS_COMMIT_FAILED` 证据先执行
  JobRun、冻结命令、完整发布血缘和 checkpoint 深层复验，完全一致时只做幂等 artifact
  绑定，不重写不可变字节。failure 权威 verifier 逐项反查 DatasetVersion、batch、
  QualityRun、Warehouse 和 checkpoint；Worker 停止并 join 续租线程后才提交终态，
  已丢失租约时保持可恢复状态而不宣称成功。
- **R5 闭环**：handler 与终态 evidence 复用同一状态感知 completed-result verifier；
  `PUBLISHED_NEW` 绑定冻结 base 与本次 batch，`REUSED_UNCHANGED` 保留 base 的真实历史
  parent 和 input batches，使最终提交故障、重复 stale recovery 与新幂等键重试不重复发布。
- **R1 完整性**：增量配置冻结并严格复验显式 base version；新候选按 Canonical 主键合并
  完整基表后执行全表质量和发布，直接父版本固定为 base。全覆盖请求返回
  `REUSED_UNCHANGED`，不会创建空 batch/version；新快照返回 `PUBLISHED_NEW`。
- **R1 恢复**：发布后立即保存不可变数据集 checkpoint；严格 success/failure verifier
  同时校验业务语义、仓库、版本和质量凭证，发布后故障可发现已有版本且重试不重复发布。
- **入口**：CLI 只提供显式入队、按 `job_id` 执行 Worker 和状态查询；API 提供
  `POST /api/data/eod-runs`、`GET /api/jobs/{job_id}` 和
  `POST /api/jobs/{job_id}/cancel`。
- **正式组合**：`quantking-data`、`python -m quantking.cli.eod` 和
  `quantking.web.main:create_runtime_app` 共用类型化本地 runtime；SDK、数据库和数据根
  只来自受控设置，import、`--help` 和 API 入队不会加载 XTQuant SDK。
- **依赖**：S1-04 完成。
- **禁止项**：不建设内部调度器、Windows 任务计划或自动切源；发布意图必须显式为
  `PUBLISH_AFTER_QUALITY`，不会因质量通过而隐式发布。

## S1-06：数据中心 Web

- **状态**：已于 2026-07-28 在提交
  `413805074b3a82c2d2a090d2b73c854a238e2e19` 完成主验收。证据见
  [S1-06 主验收证据](S1-06_ACCEPTANCE.md)。真实网络、QMT、真实供应商和正式数据根仍未授权。
- **范围**：展示来源、批次、版本、覆盖、质量、失败、差异、override 和 EOD 任务状态的数据中心
  页面及关键端到端测试。
- **实现**：应用层查询服务通过窄 port 组合数据库级 keyset 仓储、严格 Warehouse 与
  DifferenceReport verifier、来源矩阵和既有 Job/EOD 服务；FastAPI 只做校验、映射和
  稳定错误转换。React 使用显式 version ID，EOD 冻结空 root mapping 或完整精确 mapping。
- **R1 修复**：报告读侧精确绑定 SQLite/artifact/双方 PUBLISHED version/PASSED quality；
  Override 显示 FAILED attempt 并严格核对 active binding、child 与 manifest provenance；
  Cursor 绑定资源、全部过滤、投影和排序版本；来源证据读取最新持久 assessment；前端增加
  过滤、专用血缘详情和独立详情分页；Playwright 使用真实临时应用查询链。
- **R2 修复**：Override application/attempt/active binding 与 override 状态执行双向矩阵
  验证，保留 `APPROVED + PUBLISHED` 恢复窗口但拒绝 `APPROVED + APPLIED`；总览失败数
  使用与统一失败列表相同状态集合的数据库聚合计数，不加载或截断失败对象。
- **验证**：仅使用临时 SQLite/Warehouse、合成证据、Fake Gateway 和 loopback；
  Playwright 不启动 Worker，也不加载 XTQuant 或访问外部网络。
- **依赖**：S1-05 完成。
- **禁止项**：页面不得绕过应用服务、审计或版本选择合同；不得接受任意 SQL、路径或
  `latest` 别名，不得同步运行 Worker，也不得直接读取 SQLite、Parquet 或日志文件。

## S1-07：经授权真实样本与旧数据 DATA_ONLY 判定

- **状态**：S1-07A-R2 修复已提交、等待主验收；24 个单元仍为
  BLOCKED_EXTERNAL_VALIDATION；S1-07B/C/D、S1-08 未开始、未获授权。
- **范围**：在逐项授权下运行真实 XTQuant/备用源固定样本；只读审计旧项目候选数据并作
  `DATA_ONLY`/拒绝判定，记录 schema、覆盖、校验和、来源和缺口证据。
- **依赖**：S1-06 完成；真实网络、QMT、正式数据根和 `E:\QuantKing` 读取分别授权。
- **禁止项**：不整体复制旧目录，不把旧数据伪装成 XTQuant，不迁移未通过判定的数据。

## S1-08：真实或明确范围 Canonical 构建及 Stage 1 最终验收

- **状态**：未开始，未获实施授权。
- **范围**：基于经授权真实输入构建全量或明确范围的 Canonical DatasetVersion，验证发布、查询、
  覆盖、质量、差异、失败隔离与 Web 证据，并执行 Stage 1 最终验收。
- **依赖**：S1-02～S1-07 全部完成且逐项验收。
- **禁止项**：范围不明确、质量失败或证据不完整时不得宣称 Stage 1 完成；不得开始 Stage 2。

## Stage 2 及以后

Stage 2 及以后尚未授权。任何 Qlib 数据导出、因子、模型、组合、回测或交易实现均不得提前进行。
