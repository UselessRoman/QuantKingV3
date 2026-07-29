# Stage 1 质量与跨源合同

状态：S1-04 已于 2026-07-22 在提交 `2f99491e9dc410acf85ca81c131a77cae08d0cc3`
完成主验收，证据见 [S1-04 主验收证据](S1-04_ACCEPTANCE.md)。本文件固化 Stage 1 单源质量规则、跨源差异比较和
显式 SourceOverride 的可执行合同。权威来源是：

- `src/quantking/data/quality/`（质量规则与执行器）
- `src/quantking/data/comparison/`（跨源比较器与阈值）
- `src/quantking/data/assessment/`（备用源候选评估与生产矩阵）
- `src/quantking/data/override/`（SourceOverride 应用服务）
- `src/quantking/data/models.py`（框架无关领域对象）
- 迁移 `20260716_0004`（质量门触发器）、`20260716_0005`（差异报告双方绑定）与
  `20260722_0009`（精确审批范围冻结）；当前唯一 head 为 `20260729_0012`
  （assessment 原子 seal、连接本地写门与历史行隔离）

文档不得替代源码与契约测试。合成数据不能标生产可用。

## 1. 单源质量规则（S1-04A）

### 质量运行生命周期

`QualityRun` 状态：`RUNNING` → `PASSED` / `FAILED` / `SKIPPED`。终态不可变；重试必须创建
新 `run_id`。`PASSED` 要求非空且每条 `QualityResult.passed=True`；`FAILED` 要求结构化失败
或至少一条失败结果；`SKIPPED` 不携带结果或失败。

### 规则版本与 schema 版本

- `rule_version`：稳定标识质量规则集（当前 `quality-rules.v1`）。
- `schema_version`：必须与 `DatasetVersion.schema_version` 精确一致。
- `content_sha256`：被检 Canonical 表的内容 SHA-256；与 manifest 凭证漂移即失败。

### 质量门触发器（迁移 `20260716_0004`）

`dataset_versions` 的 `PUBLISHED` 转换必须存在至少一条绑定到该 `version_id` 且状态为
`PASSED` 的 `quality_runs` 行；否则 SQLite 触发器 `dataset_versions_quality_gate`
`RAISE(ABORT)`。直接 SQL UPDATE 到 `PUBLISHED` 同样被拦截。

### 失败分类

`BatchFailureKind.QUALITY_FAILURE` 用于质量失败；`INTERNAL`/`PARSE` 用于适配器层。
质量失败必须阻止发布并保留脱敏证据；失败目录、日志和输入清单不可原地覆盖。

## 2. 跨源差异比较（S1-04B）

### 比较器合同

`CrossSourceComparator` 按 Canonical primary key 对齐两个 PyArrow 表，字段级阈值（非单一
百分比），稳定排序，不修改输入。输出 `DifferenceReport` 携带：

- 双方 `batch_id` / `version_id` / `content_sha256` / `quality_run_id`
- `sample_spec_sha256` / `threshold_rule_version` / `report_sha256`
- `field_differences` / `total_compared` / `total_differences`

### 阈值版本

`ThresholdSet.rule_version` 当前为 `comparison-thresholds.v1`。每个数值字段独立设置
`absolute` 和 `relative` 阈值；字符串、日期、布尔和状态字段使用精确相等。

### report_id 确定性

`report_id` 基于 request 的 `to_deterministic_dict()` + SHA-256 → UUID4（强制 version/variant
bits）；排除随机元数据 ID（`version_id`、`batch_id`、`quality_run_id`）和 `created_at`。
相同输入得到相同 `report_id`；相同 `report_id` 但不同输入必须 `IdempotencyConflictError`。

### 差异报告双方绑定（迁移 `20260716_0005`）

`difference_reports` 表新增 11 个 NOT NULL 列，含外键到 `ingestion_batches`、
`dataset_versions`、`quality_runs`。Repository `save_difference_report` 验证：

- 双方 batches 存在且 source 匹配 report 的 `primary_source`/`fallback_source`。
- 双方 batches 的 dataset 一致且与 report 的 version dataset 匹配。
- 双方 versions 存在且 dataset 匹配。
- 双方 quality_runs 存在且绑定到对应 `version_id`。

`source_overrides_validate_insert` 触发器增强：override 的 primary/fallback batches 必须与
`difference_report` 的 compared batches 一致。

## 3. 备用源候选评估（S1-04B）

### 证据类型

`EvidenceKind.SYNTHETIC`：合成固定样本证据，**永远不能**标记 `PRODUCTION_READY`。
`EvidenceKind.REAL`：经授权的真实外部供应商响应证据。

### 评估状态

`AssessmentStatus.UNVERIFIED`：未收集证据。
`AssessmentStatus.BLOCKED_EXTERNAL_VALIDATION`：合成证据存在，但须真实外部验证。
`AssessmentStatus.PRODUCTION_READY`：真实证据通过全部门禁。

`UNVERIFIED` 和 `BLOCKED_EXTERNAL_VALIDATION` 必须携带 `blocking_reason`；
`PRODUCTION_READY` 不得携带 `blocking_reason`。

### 评估记录字段

`SourceCandidateAssessment` 携带：source、dataset、evidence_kind、semantic_contract_verified、
schema_unit_verified、coverage_adequate、quality_passed、fixed_sample_differences_adequate、
stability_runs、status、blocking_reason、assessment_config_version、sample_spec_version、
rule_version、evidence_relative_path、evidence_sha256、created_at。

## 4. 显式 SourceOverride（S1-04C）

### 生命周期

`SourceOverrideStatus`：`PENDING` → `APPROVED` / `REJECTED` → `APPLIED`（仅 APPROVED）。
`APPLIED` 和 `REJECTED` 终态不可变。

### 创建验证

`field_replacements` 与 `fallback_only_keys` 是唯一权威审批范围，分别表示一个
`FIELD_REPLACEMENT` 单元和一个 `ROW_INSERT` 单元。每个单元按对应 dataset 的
`DatasetSchemaDescriptor` 验证：

- `FIELD_REPLACEMENT` 完整且仅包含该 dataset 的全部主键和一个合法非主键 `field`；
- `ROW_INSERT` 完整且仅包含该 dataset 的全部主键；
- 日期值必须位于该 dataset 的实际日期主键（例如 `trade_date` 或 `effective_from`），采用
  `YYYY-MM-DD`；
- 不接受重复单元、空值、非文本值、未知字段、缺失主键或多余字段。

`scope_codes`、`scope_dates`、`fields` 仅为兼容展示的确定性摘要，分别由全部精确单元的
非日期主键、实际日期主键和 `FIELD_REPLACEMENT.field` 去重排序投影得到。API 请求、
`SourceOverride.pending`、直接领域构造和 repository 保存都必须拒绝摘要与投影不完全相等的
输入；摘要不能扩张、缩小或重新解释审批范围。

`CreateOverrideRequest` 还验证非空 reason 和不同 primary/fallback sources。
`SourceOverrideService.create_override` 将精确单元绑定到已验证的 `DifferenceReport`，并通过
repository 验证双方 batches/sources/datasets 一致及单元确实存在于报告证据中。

规范化精确范围生成 `exact_scope_sha256`；由同一精确范围生成 manifest contribution 和
`contribution_scope_sha256`。application `input_fingerprint` 同时包含规范化精确范围、
`exact_scope_sha256`、report SHA 和质量请求，不能只依赖 override ID。

### 应用规则

`apply_override` 只能作用于 `APPROVED` 状态；非 APPROVED 必须在状态变更前失败。完整应用
流程（创建直接子 `DatasetVersion`、重跑质量门、原子发布、标记 APPLIED）须在同一审计事务
内完成，需要 CanonicalWarehouse、QualityEngine 和 PublishCoordinator。

### 数据库触发器

- `source_overrides_validate_insert`：PENDING 状态、双方 batches 不同、双方 sources/datasets
  与 difference_report 一致；两个精确范围 JSON 必须是类型正确的非空数组，且每个单元、
  摘要投影和两个范围 SHA 满足 dataset 合同。
- `source_overrides_pending_transition`：PENDING → APPROVED/REJECTED，必须携带 actor 和
  decision 时间；精确范围、摘要、范围 SHA 和全部既有决策事实不得同时变化。
- `source_overrides_approved_transition`：APPROVED → APPLIED，精确范围不得变化；必须绑定
  PUBLISHED 的同数据集直接子 version、对应 application、DifferenceReport SHA 和同一
  `contribution_scope_sha256`。manifest provenance 由应用服务重新读取并精确核对后才允许绑定。
- `source_overrides_no_update_terminal`：REJECTED/APPLIED 不可修改。
- `source_overrides_no_delete_decided`：APPROVED/REJECTED/APPLIED 不可删除。

`20260721_0008` 的 downgrade 在 SQLite batch 重建 `source_overrides` 后恢复 0007 的完整五个
触发器。`20260722_0009` 替换向前生效的触发器并支持
`0009 → 0008 → 0007 → 0008 → 0009` 恢复。

### 旧 0007 非终态升级策略

旧 0007 不保存精确差异单元，禁止从 `scope_codes × scope_dates × fields` 笛卡尔积猜测审批
内容。生产迁移必须经过 Stage 0 安全迁移入口：先对完整快照副本和隔离副本执行迁移；0009
检测到缺少精确单元的旧 override 时明确阻断，原数据库保持在 0007，提示先取消旧非终态
override，或另行依据受验证 DifferenceReport 证据执行确定性迁移。不得直接把旧 PENDING /
APPROVED override 转成空精确范围。

## 5. 不变量

- 备用源不得静默替换主源；冲突必须隔离、报告并通过显式发布决定处理。
- override 只能进入同数据集的新 `DatasetVersion`，不得原地修改父版本。
- 已批准计划、模型和正式产物不得原地修改，只能建立新版本或停用旧版本。
- 失败任务不得发布半成品；失败目录、日志和输入清单必须保留，修复后创建新任务。
- 合成数据不能标生产可用；`EvidenceKind.SYNTHETIC` 永远不能 `PRODUCTION_READY`。

## 6. S1-07A 真实证据门

S1-07A 新增版本化 `FixedSampleSpec`、`RealSourceValidationRun` 和
`VerifiedAssessmentEvidence`。assessment 持久化入口只接受 verifier 返回的强类型证据；
裸 assessment 摘要不能写入。真实证据目录逐项复验规范 JSON、自签名 manifest、
`COMPLETE`、精确文件集合、逐文件 SHA/大小/行数、请求与样本 SHA、版本凭证、Canonical
内容 SHA、质量结果及可选 DifferenceReport 血缘。

`PRODUCTION_READY` 现在对所有来源和六类数据集统一要求 REAL evidence、五项 gate 全部通过，
以及至少三个不同 `data_validation` run ID。此前仅凭 SDK 合同标记的 XTQuant
`exchange_calendar` 也回到 `BLOCKED_EXTERNAL_VALIDATION`，等待相同真实门禁。

固定样本 release `s1-07.fixed-sample.v2` 覆盖六类数据集、SH/SZ/BJ 和沪深基准，SHA-256 为
`03d3d6c1e898b0746b7c5ab3911917a600fa3975f802c1d414fd0b6196c80dfb`。v2 release 绑定不可变
v1 基础内容，避免相同 version 接受不同规范内容。事件目的枚举已固化，
但 IPO、退市、ST、停牌和公司行动的正式 case 必须先取得可复查权威依据及版本；本次离线
实施没有联网核验，因此没有凭记忆把事件证券或日期写入 v2。
