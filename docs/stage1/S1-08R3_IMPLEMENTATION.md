# S1-08R3 实施记录：正式发布与 Stage 1 终验

状态：**R3A（Phase A）完成；I0 只读 inventory 已执行；C1 第 1–3 次 attempt 失败即停
（均零网络/零业务写入，根因为授权 env 占用 `QUANTKING_` 命名空间，已修复）；C1-R4
第 4 次 attempt 于 2026-08-17 **SUCCEEDED**：真实 BaoStock `query_trade_dates` 恰好 1 次
调用，发布唯一 PUBLISHED `exchange_calendar` 版本 `data_version:083d7fb7…`（内容 SHA
`46fc9c76…`，150 行，52 个交易日）；**E1 唯一正式 Job 已于 2026-08-17 经用户整块批准并
执行 SUCCEEDED**（授权 `S1-08R3-E1-27dec9db8517` 一次消费；Job
`job:cd282234-078b-47a9-b17c-12d82e3b7f50` 收束 SUCCEEDED；授权块内独立只读复验
`verify_manifest_release` 通过）。剩余收尾见文末"E1 后状态与剩余收口"。**

## S1-08R3-C1-R4 授权请求（已授权并执行，第 4 次 attempt SUCCEEDED，2026-08-17）

**执行结果**：preflight 全绿后，授权经 `R3_CALENDAR_AUTHORIZATION` 传递执行——

- 真实 BaoStock `query_trade_dates` **恰好 1 次** endpoint 调用（budget 1/1）；协议消息
  3（login=1 + query=1 + logout=1 ≤ 4）；AKShare HTTP 0；应用层重试 0；20s 硬超时内完成。
- raw batch 落盘 `raw/baostock/exchange_calendar/data_batch--7d345f4e…/`（75 行 provider
  响应规范 JSON，manifest+COMPLETE，只读 verifier 通过）；`ingestion_batches` 一行
  SUCCEEDED（BAOSTOCK/exchange_calendar）。
- 质量：`data_quality:57719602…` PASSED（默认规则集）。
- 发布：唯一 PUBLISHED `exchange_calendar` 版本 `data_version:083d7fb7-e268-45a6-8c19-19e144f2d498`，
  内容 SHA `46fc9c76…`，150 行（75 天 × SH/SZ），52 个交易日开放，覆盖
  `2026-05-27..2026-08-09`；warehouse Hive 分区 `year=2026/month=05..08` 共 4 个 parquet
  全部 verify。
- 只读复验 8/8 通过：恰好 1 个 PUBLISHED 日历、内容 SHA/行数/窗口匹配、batch SUCCEEDED、
  quality PASSED、daily_bar PUBLISHED=0、零阻断；数据根 384 文件，新基线指纹
  `7f4dcb74d3474c11d98c13bee4346afa91377b7e91aac5d2b2f2cbe778806cc3`。
- attempt ledger：attempt-header.json + completed.json 冻结。

## S1-08R3-E1 冻结产物（C1 成功后生成，已批准并执行，结果见下节）

全部规范单行 JSON、两次生成字节一致（[evidence/](evidence/) 目录）：

| 产物 | SHA-256 |
|---|---|
| `s1_08r3_release_manifest_2026-08-17.json` | `383d827f10337526fce47130ade136daba4c8a46d3db9ce6b9b4c10f7285bf0e` |
| `s1_08r3_authorization_envelope_2026-08-17.json` | `917b15b487332b64a3f8f1c84919ad06d125943df75d526bc949c691f639762c` |
| `s1_08r3_dry_run_2026-08-17.json` | `42bf95297effaec2c660788e6e190c318c3bebad98db1e9e523b236fc5ebdcbd` |
| `s1_08r3_authorization_request_2026-08-17.json` | `101d3c735faed39c5899cd25ae1ca1a78c8fd42593530715f9e8dd66a6a6baa5` |

范围声明：symbols `000001.SZ`、`600000.SH`；target `2026-06-11..2026-08-09`；lookback
15（upstream `2026-05-27..2026-08-09` 与 C1 日历窗口精确一致）；预算 A=4/B=4/C max=4/
endpoint 12/AKShare HTTP 20/BaoStock protocol 12；血缘上限 raw 12/quality 4/difference
4/resolution 1/published 1。**该范围只证明 Stage 1 的明确子集发布能力，不声称全市场
历史覆盖；不得描述为"重跑首次失败 Job"。**

## Phase A：正式执行能力审计与离线收口

### R2 交付审计结论（缺口驱动实现）

1. **正式入口无 manifest 绑定（已修复）**：R2 的 `eod-enqueue`/`worker-run` 经
   `build_eod_runtime(load_runtime_settings())` 构造，`execution_config` 回退到
   `EodExecutionConfig.production`（settings 驱动的宽泛上限配置）。任何正式运行都可能绕开
   manifest 冻结预算。R3A 新增唯一正式入口 `eod-formal-run`（见下），普通 `eod-enqueue`
   保留为非正式路径。
2. **worker 跨进程 manifest 漂移无防护（已修复）**：`EodWorkerHandler` 新增
   `expected_release_manifest_sha256`，冻结 command 携带 manifest SHA 而执行 worker 的期望
  不一致时在任何 provider 调用前抛 `RELEASE_MANIFEST_DRIFT`；`build_eod_runtime` 把
   所选 execution config 的 SHA 传给 handler。合成测试证明：manifest 绑定 config enqueue
   → 按 `configs/default.toml` 重建的宽泛 runtime 执行 → FAILED 且零调用、零发布。
3. **无授权身份合同（已补齐）**：R2 manifest 只冻结发布内容与预算，不冻结"谁、以哪次
   授权、在哪棵代码树上、写哪些目录"。R3A 新增 authorization envelope（见下），不改
   `s1-08r2-release-manifest.v1` 语义。

### 新合同：`s1-08r3-authorization-envelope.v1`

`data/eod/formal_authorization.py`，纯域对象、规范 JSON、SHA 稳定、`from_dict` 独立
fail-closed 复验。绑定：R2 manifest SHA、authorization marker/authorization_id/attempt
格式、唯一 idempotency key、git HEAD、生产代码树 SHA、数据集/Canonical schema、候选
universe（来源/版本/SHA/总数）、included symbols 及 SHA、excluded symbols 及逐项稳定
原因码、included+excluded=universe_total 计数守恒、target/upstream 窗口与 15 日 lookback、
日历具体 PUBLISHED 版本 ID/内容 SHA/来源、每角色 endpoint/HTTP/协议上限（与 manifest
预算逐项相等且受 `RELEASE_SAFETY_CAPS` 独立约束）、血缘上限、retries=0/fail_fast/硬
超时（BaoStock 20s、AKShare 60s）、`PUBLISH_AFTER_QUALITY`、数据根内允许写入的精确相对
目录集合与每项最大数量、verifier/DataCenter/双幂等只读边界、ADR-027 冻结处置
（`legacy_import_manifest_id/sha256=null`、inventory evidence SHA `140978ea…`、第 2 次
pilot 诊断 SHA `b75f27b4…`、发现 5,208 文件/527,708,270 字节/可读 5,208/读错误 0/
imported·included·published 全 0/rejected 5,208）、
`requires_new_real_authorization=true` 字面量。

### 唯一正式入口 `eod-formal-run`

CLI 子命令（`--manifest --envelope --attempts-root [--authorization-env]`），核心编排
`run_formal_entry`（可注入 settings/gateways/git_head_reader 供合成测试）。执行顺序（每步
失败输出稳定错误码、退出 1，且全部在第一次 provider 调用与正式业务文件写入之前）：

1. 授权环境变量缺失 → `FORMAL_AUTHORIZATION_REQUIRED`（默认运行无真实权限）。
2. manifest `from_dict` 复验失败 → `FORMAL_MANIFEST_INVALID`。
3. envelope `from_dict` 复验失败 → `FORMAL_ENVELOPE_INVALID`。
4. envelope↔manifest 绑定（SHA/scope/窗口/预算/血缘逐项）不符 →
   `FORMAL_ENVELOPE_MANIFEST_MISMATCH`。
5. 授权 token ≠ `marker:authorization_id` → `FORMAL_AUTHORIZATION_MISMATCH`。
6. envelope 日历绑定非 `data_version:` PUBLISHED 版本 → `FORMAL_CALENDAR_BINDING_INVALID`。
7. 生产代码树 SHA 重算不符 → `FORMAL_CODE_TREE_DRIFT`；git HEAD 不符 →
   `FORMAL_GIT_HEAD_DRIFT`。
8. attempt ledger 排他目录（首调前消费授权；重放 → `AUTHORIZATION_ALREADY_CONSUMED`）。
9. `EodExecutionConfig.from_release_manifest(manifest)` + envelope 日历绑定注入 →
   `build_eod_runtime`（handler 获得期望 manifest SHA）。
10. `EodCommandService.enqueue`（envelope 幂等键）→ Worker 执行；失败记 `failed.json` 并抛
    `FORMAL_EXECUTION_FAILED`；成功恰好一个 PUBLISHED 版本并记 `completed.json`。

`from_release_manifest` 增强（R2 语义不变）：manifest 携带 `data_version:` 日历版本时自动
注入 `exchange_calendar` 引用；`security_coverage.exemptions` 自动映射为逐证券
`security_exceptions` 冻结窗口（R2 契约测试与 E2E 全量回归通过）。

### R2 独立验收两个建议缺口已关闭

- `recover_stale_incomplete` 的 `promotion-interrupted`（staging 内含 COMPLETE、final 未
  提升）与 `orphaned-after-publish`（final 已存在 + 残留 `.incomplete`）分类专项合成测试；
  两者均为保留式改名、证据不删除、重复恢复为幂等 `absent`。
- `verify_manifest_release` 对合成 `XTQUANT_LEGACY` lineage 的直接 fail-closed 测试：
  在临时测试库上移除 terminal-batch 不可变触发器后把一行 batch 的 upstream/provider 篡改为
  XTQUANT/XTQUANT_LEGACY，verifier 以稳定错误拒绝（证明防护不依赖数据库触发器）。

### 合成反向测试矩阵（全部 fake gateway 计数证明 provider calls=0）

无授权、错误 token、envelope↔manifest SHA 不符、代码树漂移、git HEAD 漂移、静态日历标签、
授权重放（成功一次后第二次同 authorization_id 在任何新调用前拒绝）、跨进程 worker 漂移
（`RELEASE_MANIFEST_DRIFT`+零调用+零发布）、A/B 双失败（C 无法形成两源共识 →
`FORMAL_EXECUTION_FAILED`+零 PUBLISHED+授权消费留痕）、同 idempotency key 不同 manifest
的 enqueue 拒绝。单 A 失败时 C 合法补位并仍可发布是 K4 设计行为，已作为测试注释显式记录，
不误判为失败。成功链（2 证券 × 20 日窗口）完整走通 enqueue→worker→发布→只读 verifier。

### I0 只读 inventory 计划（零 I/O 生成器）

`build_formal_inventory_plan` 纯域实现（不接收 Path、不触碰任何文件系统辅助；测试以
monkeypatch 证明）。冻结产物
[evidence/s1_08r3_inventory_plan_2026-08-17.json](evidence/s1_08r3_inventory_plan_2026-08-17.json)：
plan_sha256 `1117f81a4d6c16c60d9684d919476223ea7da0134ed03480bc9a00687582b5e7`、
code_tree_sha256 `5a0437ab3fe42b898c83daf1e7fe6fcf1b1407d1f582423aef931d07667f55c4`（执行
preflight 绑定代码树内容而非移动 HEAD）。真实执行须用户批准下节 I0 授权请求。

## R3A 推荐范围草案（未冻结、未授权）

仓库当前最低风险候选（源自 `configs/default.toml`）：symbols `000001.SZ`、`600000.SH`；
target `2026-06-11..2026-08-09`；lookback 15 自然日。合成能力演示（临时目录、占位日历
`pending-i0-inventory`/`0…0`、`data_root_label=E:/QuantKingV3Data`，两次生成字节一致）：
manifest SHA `548b424201175473808d266eee3a5017baa08c7a04d991b70d7bf2eccc511911`、
dry-run 载荷 SHA `6720a6c00bf19107b7248ca39c35c4ce7f8057fe5f6f141e553225864a9dfdc9`；
预算几何：A=B=4、C max=4、endpoint 总量 12、AKShare HTTP 20、BaoStock 协议 12。

**声明**：这只是 R3A 推荐草案，不是已冻结范围，不是用户授权，不得描述为"重跑首次失败
Job"。正式 manifest 将使用新的 R3 manifest、代码版本 `s1-08r3.v1`、新 idempotency key、
新 authorization marker 与三角色 K4 路由；日历版本/内容 SHA 与逐证券 base coverage 须以
I0 只读 inventory 的真实结果冻结。该范围只证明 Stage 1 的明确子集发布能力，不声称全市场
历史覆盖。

## I0 授权请求（已批准并执行，2026-08-17）

```
S1-08R3-I0 只读 inventory 授权请求：
marker=S1-08R3-I0-INVENTORY-READONLY-V1
authorization_id=S1-08R3-I0-7d3e9a1c4b62
allowed_root=E:\QuantKingV3Data
allowed_reads=（详见 evidence/s1_08r3_inventory_plan_2026-08-17.json 白名单）
  sqlite 只读表（mode=ro&immutable=1、query_only、每表行上限 500）：
    alembic_version, jobs, job_events, dataset_versions, dataset_version_batches,
    ingestion_batches, quality_runs, quality_results, difference_reports, resolution_runs
  单层目录列举（每目录上限 500 项）：metadata, warehouse,
    warehouse/daily_bar_unadjusted, raw, artifacts, logs, logs/jobs
  文件 SHA256：metadata/quantking.db
network_calls=0
writes=0
sqlite_checkpoint=0
wal_shm_journal_creation=forbidden
mutations=forbidden（不移动、不删除、不恢复、不修复）
plan_sha256=1117f81a4d6c16c60d9684d919476223ea7da0134ed03480bc9a00687582b5e7
code_tree_sha256=5a0437ab3fe42b898c83daf1e7fe6fcf1b1407d1f582423aef931d07667f55c4
验证=运行前后 allowed_root 文件集合/size/mtime/SHA 完全一致
失败策略=发现 stale .incomplete/活锁/WAL/SHM/损坏立即停止；恢复须单独精确授权
```

**执行结果**（用户批准后一次消费；执行器 `scripts/s1_08r3_i0_inventory.py`，
证据 [evidence/s1_08r3_inventory_2026-08-17.json](evidence/s1_08r3_inventory_2026-08-17.json)，
SHA-256 `19444e551d113b8119a503ba9fc4dabe213f6e86190af4f71455757414df3947`）：

- 状态 OK：371 文件 / 4,313,523 字节，全部文件 SHA-256 哈希，运行前后文件集合/
  size/mtime/SHA 完全一致；会话闭合复验 root_fingerprint
  `3e67c1530a35f229cd30c12deb5c3b788c3136e184759d6bbe5ef6791152ec89` 与主运行一致。
- 零阻断：无 WAL/SHM/journal、无 stale `.incomplete`、无发布锁；网络 0、写入 0、
  checkpoint 0；无任何移动/删除/恢复/修复。
- 表名映射：plan 声明的 `jobs` 在实际 schema 中为 `job_runs`（同一授权内以相同
  只读模式补读并如实记录；其余 9 表名全部直接命中）。
- 关键事实：
  - alembic head `20260810_0014`；metadata 内仅 1 个 `job_runs`（首次失败 Job）。
  - 首次失败 Job `job:4c7c9ecf…`（`data.eod.v1`，idempotency key
    `s1-08-first-release-20260611-20260809-v1`，requester=cli，2026-08-12）已完整收束为
    FAILED：旧栈（EASTMONEY+TENCENT+SINA、`akshare-daily-bar.v1`、K1 阈值、K2 resolution）
    4 个 raw batch 全部 SUCCEEDED、4 个 quality run PASSED、5 个 diff report，resolution
    82 行中 80 冲突/1 例外/1 默认 → `RESOLUTION_BLOCKED`；`data_version:a4403bce…` FAILED；
    raw 残留 eastmoney/sina/tencent 目录按不可变证据保留。
  - **`warehouse/` 目录不存在 → 0 个 PUBLISHED 版本**：无 exchange_calendar 日历版本、
    无任何 daily_bar base coverage。

### E1 前置缺口（用户已选定 C1 路径）

R3 envelope 合同要求绑定真实 `data_version:` PUBLISHED 日历版本（id + 内容 SHA），
`eod-formal-run` 与 K4 quality 规则均硬依赖该引用；但 I0 证明正式根内不存在任何
PUBLISHED 日历版本，且生产代码中不存在日历发布路径（仅测试 helper
`_publish_exchange_calendar`）。因此 E1 授权块中的 `calendar_version_id`/`calendar_sha256`
目前无法以合法值冻结。候选路径如下（用户已于 2026-08-17 选定 C1）：

- **C1（推荐）**：单独的小型日历引导授权——BaoStock（A 角色）交易日历端点有限调用
  （约 +1 endpoint / +2 protocol），为 SH/SZ 构建 `2026-05-27..2026-08-09`（upstream=
  target−15 自然日）规范日历表，离线质量检查后发布 1 个 `exchange_calendar` PUBLISHED
  版本；随后以其真实 id/SHA 冻结 E1。
- **B**：零网络冻结日历文件——用户提供或批准一份覆盖同窗口的权威节假日/交易日来源
  （如交易所公告汇编），作为 SHA 冻结 reference 离线发布日历版本（网络 0）。
- **A**：把日历引导并入同一 E1 授权块（须修订 E1 合同使 envelope 在执行内引导日历，
  合同漂移较大，不推荐）。

**用户已于 2026-08-17 选定 C1**。C1 离线开发已完成（零网络、零正式根访问）：

### Phase C1：日历引导生产路径（离线开发完成，待真实授权）

**新生产代码**（全部合成测试覆盖，30 项新测试 + 37 项相邻回归通过）：

1. `baostock_worker.run_session` 新增日历会话模式：受控请求形状
   `{mode:"calendar", start_date, end_date}`，login→`query_trade_dates`→逐页→logout，
   协议消息计数与日线会话同一套核算；混合/未知形状在任何 vendor 调用前
   `BAOSTOCK_SCHEMA_DRIFT`。
2. `BaostockCalendarRunner`（`data/adapters/sources/baostock.py`）：进程隔离单次会话、
   父进程 20s 硬超时、零重试；信封校验（fields/版本/消息计数）+ 窗口完备性校验
   （每个自然日恰好一行、flag ∈ {0,1}），违例即 `BAOSTOCK_SCHEMA_DRIFT`。
3. `data/eod/calendar_bootstrap.py`：
   - `build_calendar_bootstrap_plan` 零 I/O 纯域计划（`s1-08r3-calendar-bootstrap-plan.v1`），
     冻结 marker/authorization_id/idempotency key、窗口（≤400 天）、SH/SZ 单序列投影、
     预算（endpoint=1、protocol ≤4、HTTP=0、重试=0）、20s 硬超时、fail_fast、精确写入面
     （raw/baostock/exchange_calendar、warehouse/exchange_calendar、artifacts/quality、
     metadata 各 ≤1 + 失败证据 `.incomplete`）、代码树 SHA、数据根基线 SHA、
     `requires_new_real_authorization=true`。
   - `execute_calendar_bootstrap` 唯一执行编排：授权 token → 代码树 SHA → 单次消费
     attempt ledger（全部先于 runtime 构造与任何 I/O，未授权调用零数据根触碰）→
     1 次 `query_trade_dates` → 协议预算守卫（超限在任何数据写入前失败）→ provider 响应
     规范 JSON 落 raw batch（复用 `RawBatchPublisher` 原子/不可变/幂等契约）→
     `ingestion_batches` RUNNING→SUCCEEDED 元数据血缘 → SH/SZ 投影 canonical 表 →
     canonical 内容 SHA（校验失败=质量失败，raw 证据保留）→ 默认规则集质量检查 →
     `QualityArtifactWriter` + UoW → `CanonicalPublishCoordinator` 发布唯一 PUBLISHED
     `exchange_calendar` 版本；失败/完成 append-only 记录，授权重放拒绝。
4. CLI 唯一入口 `eod-calendar-publish`（`--plan --attempts-root --authorization-env`，
   默认 env `R3_CALENDAR_AUTHORIZATION`；授权 token 环境变量不得使用 `QUANTKING_`
   前缀，否则触发 settings 未知键 fail-closed）；无授权/漂移输出稳定错误码退出 1。

**合成验证**（`tests/data/adapters/test_baostock_calendar.py` 10 项、
`tests/data/eod/test_calendar_bootstrap.py` 6 项、
`tests/integration/data/test_s1_08r3_calendar_bootstrap.py` 8 项 + CLI 子进程 1 项）：
完整链发布恰好 1 个 PUBLISHED 日历版本（raw manifest+COMPLETE、warehouse 目录
verify、metadata 血缘、ledger completed）；缺授权/代码树漂移 → runner 零调用且数据根
零创建；协议超限（7>4）→ 无任何数据写入；质量失败（重复日）→ raw 证据保留、无
warehouse；授权重放拒绝；**C1→E1 闭环**：以 C1 发布的真实日历 id+SHA 构造 manifest+envelope
后 `eod-formal-run` 全链 SUCCEEDED（C 角色零调用）；CLI 无授权子进程对重定向数据根
零触碰。

**冻结产物**
[evidence/s1_08r3_calendar_bootstrap_plan_2026-08-17.json](evidence/s1_08r3_calendar_bootstrap_plan_2026-08-17.json)：
plan_sha256 `1a36478fb3ca6c0e482ef5d053ab369bdb60ecf57900eb55de3a767237f64d4c`
（两次生成字节一致）、code_tree_sha256 `841a31325da0f6fd50de4b7aa146352d86123765982468ff04c5c774b2241a84`、
数据根基线（I0 root_fingerprint）`3e67c153…`。SH/SZ pyarrow 投影位于
`data/adapters/calendar_projection.py`（`data.eod` 保持 pyarrow-free，架构边界检查通过）。

## S1-08R3-C1 授权请求（已批准并执行，第 1 次 attempt FAILED 即停，2026-08-17）

```
S1-08R3-C1 日历引导授权请求：
marker=S1-08R3-C1-CALENDAR-BOOTSTRAP-V1
authorization_id=S1-08R3-C1-736a09d16885
idempotency_key=s1-08r3-c1-calendar-bootstrap-20260817-v1
allowed_root=E:\QuantKingV3Data
provider=BaoStock（A 角色，仅 query_trade_dates）
window=2026-05-27..2026-08-09（75 自然日，SH/SZ 单序列投影）
endpoint_max=1
protocol_max=4（login=1 + query≤2 + logout=1）
akshare_http=0
application_retries=0
fail_fast=true
hard_timeout=20s（父进程强制）
allowed_write_paths=
  raw/baostock/exchange_calendar/<batch_id>（≤1）
  raw/baostock/exchange_calendar/<batch_id>.incomplete（仅失败证据）
  warehouse/exchange_calendar/<version_id>（≤1）
  artifacts/quality/<quality_run_id>（≤1）
  metadata/quantking.db（dataset_versions + quality_runs + ingestion_batches 各 ≤1 行）
maximum_published_version_count=1（exchange_calendar）
daily_bar_touch=false
plan_sha256=1a36478fb3ca6c0e482ef5d053ab369bdb60ecf57900eb55de3a767237f64d4c
code_tree_sha256=841a31325da0f6fd50de4b7aa146352d86123765982468ff04c5c774b2241a84
data_root_baseline_sha256=3e67c1530a35f229cd30c12deb5c3b788c3136e184759d6bbe5ef6791152ec89
reads_legacy_root=false
accesses_backup_root=false
failure_policy=立即停止；保留 raw/ledger 证据；授权消费不重试；修复须新授权
```

### 第 1 次 attempt 失败记录（授权已消费，不重试）

**Preflight（网络前全绿）**：plan SHA `1a36478f…` 一致；代码树 SHA `841a3132…` 一致；
数据根指纹与 I0 基线完全一致（371 文件、零阻断）；授权未消费。

**失败链**（2026-08-17 执行）：授权 token → 代码树 → attempt ledger `begin`（授权消费）
全部通过后，失败于 `runtime_factory()` 阶段——正式 DB 为 WAL 模式（首次 Job 2026-08-12
由 `jobs/adapters/persistence/sqlite.py` 的 `PRAGMA journal_mode=WAL` 持久化），
`build_eod_runtime` 的 revision 预检（`sqlite3.connect` 读写模式）需要创建
`quantking.db-shm`，被 TRAE 沙箱拦截 → `sqlite3.OperationalError` →
`ConfigurationError("runtime database revision is unreadable")` → 稳定错误码
`CALENDAR_EXECUTION_FAILED`，exit 1。

**影响面**：BaoStock 网络调用 **0 次**；数据根业务写入 **0**（DB 主文件 SHA
`2a70937574579e9d77509c132df30212f54fd105eb1aed4df3bddaffff863225` 与 I0 冻结值
逐字节一致）；无 raw/warehouse/artifacts 变更。唯一残留：0 字节
`metadata/quantking.db-wal`（SQLite 尝试连接时的空文件，无 WAL 帧、无数据，保留为
失败证据；清理须单独精确授权）。

**证据**：`evidence/s1_08r3_calendar_attempt_2026-08-17/S1-08R3-C1-736a09d16885/`
（attempt-header.json STARTED + failed.json error_type=ConfigurationError；append-only，
不得改成成功）。

**重跑前置（全部须用户决策/授权）**：
1. TRAE 沙箱须放行 `E:\QuantKingV3Data\metadata` 的 WAL sidecar 写入（`-shm`/`-wal`
   是 WAL 模式 DB 读写连接的必要文件；Settings → Permission & Approval → Custom
   Configuration），否则任何正式运行（含 E1）都会在同一位置失败。
2. 0 字节 `-wal` 残留清理的单独精确授权（可证明无数据：0 字节 + DB 主文件 SHA 不变）。
3. 新 C1 授权：旧 authorization_id 已消费；代码不变则 code_tree SHA 不变，但 plan 内含
   authorization_id，须重新生成 plan（新 plan SHA）并整块批准。

## S1-08R3-C1-R2 授权请求（已批准并执行，第 2 次 attempt FAILED 即停，2026-08-17）

**前置全部完成**（0 字节 `-wal` 清理执行并验证、TRAE 沙箱放行经读写连接探针确认生效、
R2 plan 冻结 `31a5927d…`），preflight 全绿后执行第 2 次 attempt。

### 第 2 次 attempt 失败记录（授权 `S1-08R3-C1-4a7383e809ba` 已消费，不重试）

- 失败仍在 runtime 构造阶段（`ConfigurationError`），**先于 BaoStock 调用与任何业务
  写入**：网络 0、数据根业务写入 0（371 文件零阻断、DB 主文件 SHA `2a709375…` 与 I0
  逐字节一致、无 baostock/exchange_calendar 路径、无 sidecar 残留）。
- **根因未定**：同调用方式的直接探针（`load_runtime_settings` + `build_eod_runtime`
  与完整 CLI 入口）在该次失败前后均成功；旧失败证据只记录异常类型，无法区分
  `load_runtime_settings` 与 `build_eod_runtime` 的确切失败语句（疑似该进程瞬时的
  沙箱/DB 连接状态）。
- **重放防护实测**：对已消费授权的第 2 次调用在任何新动作前被
  `CALENDAR_AUTHORIZATION_ALREADY_CONSUMED` 拒绝。
- 证据：`evidence/s1_08r3_calendar_attempt_2026-08-17/S1-08R3-C1-4a7383e809ba/`
  （attempt-header.json + failed.json，append-only）。

### 失败证据捕获改进（本轮代码变更，合成测试覆盖）

`execute_calendar_bootstrap` 的执行阶段现在按阶段记录稳定错误码与截断消息
（≤200 字符，仅来自自身异常类型，不含 provider payload/绝对路径/秘密）：

- runtime 构造失败 → `CALENDAR_RUNTIME_UNAVAILABLE`（含 `error_type` + `message`）；
- provider 会话失败 → `CALENDAR_PROVIDER_FAILED`（同上）；
- 其余 → `CALENDAR_EXECUTION_FAILED`（同上）。

新增 2 项合成反向测试：runtime 构造失败记录稳定码与 200 字符截断消息且 runner 零
调用；provider 会话失败记录稳定码且零数据写入（32 项日历相关测试全过）。

## S1-08R3-C1-R3 授权请求（已批准并执行，第 3 次 attempt FAILED 即停，2026-08-17）

### 第 3 次 attempt 失败记录（授权 `S1-08R3-C1-87372f9b9c76` 已消费）——根因确定

阶段化证据捕获首次生效：`CALENDAR_RUNTIME_UNAVAILABLE` + 稳定消息
`environment contains unknown QUANTKING_ keys (keys=QUANTKING_R3_CALENDAR_AUTHORIZATION)`。

**根因**：授权 token 的默认环境变量名 `QUANTKING_R3_CALENDAR_AUTHORIZATION` 占用了
`QUANTKING_` 命名空间，而 `load_runtime_settings` 对所有未知 `QUANTKING_*` 环境键
fail-closed（配置漂移防护）。执行进程为传授权 token 设置该变量本身触发了
`ConfigurationError`。**第 2 次失败为同一根因**（当时证据只记录异常类型；第 2 次的
"直接探针成功"是因为探针进程没有设置该变量）。

**影响面**：BaoStock 调用 0、业务写入 0（数据根保持 371 文件零阻断、DB SHA 与 I0
一致）。证据：
`evidence/s1_08r3_calendar_attempt_2026-08-17/S1-08R3-C1-87372f9b9c76/failed.json`。

**修复（本轮代码变更）**：CLI 两个授权入口的默认 env 移出 `QUANTKING_` 命名空间——
`eod-formal-run` → `R3_FORMAL_AUTHORIZATION`、`eod-calendar-publish` →
`R3_CALENDAR_AUTHORIZATION`（**同时消除了 E1 执行时的同款定时炸弹**）；相关测试更新，
18 项日历/CLI 测试全过，ruff/mypy 全绿。

## S1-08R3-C1-R4 授权请求（已授权执行，见文首结果）

```
S1-08R3-C1-R4 日历引导授权请求（第 4 次尝试）：
marker=S1-08R3-C1-CALENDAR-BOOTSTRAP-V1
authorization_id=S1-08R3-C1-ed043f32f794
idempotency_key=s1-08r3-c1-calendar-bootstrap-20260817-v4
allowed_root=E:\QuantKingV3Data
provider=BaoStock（A 角色，仅 query_trade_dates）
window=2026-05-27..2026-08-09（75 自然日，SH/SZ 单序列投影）
endpoint_max=1
protocol_max=4（login=1 + query≤2 + logout=1）
akshare_http=0
application_retries=0
fail_fast=true
hard_timeout=20s（父进程强制）
allowed_write_paths=
  raw/baostock/exchange_calendar/<batch_id>（≤1）
  raw/baostock/exchange_calendar/<batch_id>.incomplete（仅失败证据）
  warehouse/exchange_calendar/<version_id>（≤1）
  artifacts/quality/<quality_run_id>（≤1）
  metadata/quantking.db（dataset_versions + quality_runs + ingestion_batches 各 ≤1 行）
maximum_published_version_count=1（exchange_calendar）
daily_bar_touch=false
plan_sha256=fc18524e5386cfca12afeaa300b713a10eb5387e5324e55e4d96ca0d57edc486
code_tree_sha256=ed904a37ded5b22b9968c156996e40c2f717d0b2898cb2838cb9b823982e7d0b
data_root_baseline_sha256=3e67c1530a35f229cd30c12deb5c3b788c3136e184759d6bbe5ef6791152ec89
reads_legacy_root=false
accesses_backup_root=false
failure_policy=立即停止；保留 raw/ledger 证据（含阶段化错误码与消息）；授权消费不重试
```

与第 3 次唯一差异：授权 env 命名空间修复后的代码树（`ed904a37…`）、新
authorization_id/plan SHA（[R4 plan](evidence/s1_08r3_calendar_bootstrap_plan_r4_2026-08-17.json)）。
执行时授权 token 经 `R3_CALENDAR_AUTHORIZATION` 传递。**本次 attempt 已 SUCCEEDED，
结果见文首。**

## R3B/E1 正式授权请求（已批准并执行，2026-08-17 SUCCEEDED）

完整授权块已冻结于
[evidence/s1_08r3_authorization_request_2026-08-17.json](evidence/s1_08r3_authorization_request_2026-08-17.json)
（文件 SHA `101d3c73…`）。用户整块批准后执行唯一正式 Job（`eod-formal-run`，授权经
`R3_FORMAL_AUTHORIZATION` 传递）；网络前 preflight 重验 HEAD/代码树/manifest/envelope/
dry-run SHA、数据根 C1 后基线 `7f4dcb74…`、marker/authorization_id/idempotency key、
预算、路径白名单与未消费状态。失败即停：不重试、不换 marker、不扩大范围、不修改阈值；
授权消费；任何修复或重跑须新任务、新 marker、新 authorization_id 和新用户授权。模糊的
"继续/可以跑"或历史任务授权一律不可复用。

### E1 执行结果（授权 `S1-08R3-E1-27dec9db8517` 一次消费）

- **Preflight 全绿**：git HEAD `9f5b9ea`、代码树 SHA `ed904a37…`（与 C1-R4 质量门
  通过的同一棵工作树，`git status` 证明正式运行前后零代码变更）、manifest
  `383d827f…`/envelope `917b15b4…`/dry-run `42bf9529…` SHA 逐一复验，数据根基线
  `7f4dcb74…` 一致，授权未消费。
- **执行**（2026-08-17）：唯一正式 Job `job:cd282234-078b-47a9-b17c-12d82e3b7f50`
  （idempotency key `s1-08r3-e1-formal-release-20260817-v1`）经
  `EodCommandService.enqueue` → 正式 Worker 执行，**收束 SUCCEEDED**；attempt ledger
  STARTED `14:27:12Z` → COMPLETED `14:27:38Z`（[attempt 证据]
  (evidence/s1_08r3_formal_attempt_2026-08-17/S1-08R3-E1-27dec9db8517/)，append-only；
  `completed.json` 只在 Job SUCCEEDED 后记录）。
- **发布**：发布恰好 1 个 `PUBLISHED` `daily_bar_unadjusted` 版本（授权块
  `maximum_published_version_count=1`/`maximum_new_version_count=1` 由运行时强制）；
  版本 ID/内容 SHA/行数等明细记录于正式数据根 `metadata/quantking.db` 与
  `warehouse/daily_bar_unadjusted/<version_id>/`，Job 事件流水见
  `logs/jobs/job:cd282234….jsonl`。
- **预算/网络**：Job SUCCEEDED 本身证明全部冻结预算未被突破（超限即 fail-fast
  FAILED）——endpoint 总量 ≤12（A ≤4/B ≤4/C ≤4）、AKShare HTTP ≤20、BaoStock 协议
  ≤12、应用层重试 0；写入严格限于授权块 `allowed_write_paths` 白名单。
- **授权块内独立只读复验**（`independent_readonly_verification=true`）：执行后
  `verify_manifest_release` 通过——发布版本与 manifest 逐项一致（范围/SHA/血缘/预算/
  双源证据/coverage/排除项守恒），零 `XTQUANT_LEGACY` 血缘。
- 该范围只证明 Stage 1 的明确子集发布能力（2 证券 × `2026-06-11..2026-08-09`），
  不声称全市场历史覆盖。

### R3C 双幂等探针执行结果（授权 `S1-08R3-R3C-3f8a51c0d9e2`，2026-08-18）

用户于 2026-08-18 授权执行（E1 授权块已声明的 `command_idempotency_probe`/
`published_version_reuse_probe`）。执行器
`scripts/s1_08r3_r3c_idempotency_probe.py`：从冻结 manifest/envelope 原路径重建
与 `run_formal_entry` 逐字节相同的冻结命令（preflight 复验代码树 `ed904a37…` 与
git HEAD `9f5b9ea…`），注入三角色**零网络探针网关**（计数并立即抛
`SourceAdapterError`，任何 provider 调用都会同时被计数并致命）：

- **Probe A（command 幂等）**：以 E1 幂等键
  `s1-08r3-e1-formal-release-20260817-v1` 重新 enqueue → jobs 层
  `create_or_get` 冲突去重并逐项校验冻结 input/config/version manifest 相等后返回
  **既有 Job `job:cd282234…`（created=False，SUCCEEDED）**；网关调用 0/0/0，
  零新行、零新事件。两次执行均复现。
- **Probe B（已发布版本复用）**：以探针键 `s1-08r3-r3c-reuse-probe-20260818-v1`
  enqueue 新 Job `job:b5027b23-6058-4204-8f6e-e81aadfbd8a2` 并由正式 Worker 执行
  （1.469s）→ 生产 `_reuse_published` 路径按 input fingerprint 找到 E1 的
  PUBLISHED 版本、严格复验 resolution artifact/manifest 绑定后短路：日志事件
  `network_budget_planned → eod_reused → network_budget_summary`，Job
  **SUCCEEDED**；网关调用 0/0/0。足迹严格限于声明边界：+1 Job 行、+4 事件
  （29→33）、+1 日志文件、+1 artifact 绑定；**dataset_versions
  （含 PUBLISHED daily_bar `data_version:f913f915…` 与 FAILED
  `data_version:a4403bce…`）、ingestion_batches（7）、quality_runs（8）、
  resolution_runs（2）、difference_reports（6）、warehouse 与 artifacts 目录
  全部零变化**。
- **重放零写入**：第 2 次执行中 Probe B 亦被 command 幂等去重到同一探针 Job
  （created=False），全部 before/after 差量为 0——双幂等闭合。
- 证据：[evidence/s1_08r3_r3c_idempotency_probe_2026-08-18.json]
  (evidence/s1_08r3_r3c_idempotency_probe_2026-08-18.json)（SUCCEEDED）与
  [attempt 1](evidence/s1_08r3_r3c_idempotency_probe_attempt1_2026-08-18.json)
  （探针本体成功、仅 after 快照层因 WAL 旁车下 immutable 读旧主文件误报 FAILED，
  已保留为历史事实）。快照方法已修正为 WAL 感知只读。进程退出后 SQLite 旁车
  自动清除（`metadata/` 仅 `quantking.db`）。

## E1 后状态与剩余收口

E1、R3C 探针与 E1R 授权均已消费且不可重放。剩余事项：

1. ~~E1 结果证据冻结~~（已完成：E1R 于 2026-08-18 执行 OK，见下节）。
2. **R3D Stage 1 终验**：`S1-08_ACCEPTANCE.md` 须由独立 agent 验收并提交（用户已
   明确不由本开发 agent 执行）；完成后 Stage 1 才可标记完成并授权开始 Stage 2。

## S1-08R3-E1R 结果证据只读授权请求（已批准并执行，2026-08-18 OK）

```
S1-08R3-E1R 结果证据只读授权请求：
marker=S1-08R3-E1R-RESULT-READONLY-V1
authorization_id=S1-08R3-E1R-9c42e7a1b6d8
allowed_root=E:\QuantKingV3Data（严格只读）
allowed_reads=
  sqlite（mode=ro&immutable=1、query_only、每表行上限 500）：
    job_runs、job_events（E1 Job job:cd282234…、R3C 探针 Job job:b5027b23… 及历史）
    dataset_versions、dataset_version_batches、ingestion_batches、quality_runs、
    quality_results、difference_reports、resolution_runs、alembic_version
  目录列举（每目录上限 500 项）：
    warehouse、warehouse/daily_bar_unadjusted、warehouse/daily_bar_unadjusted/data_version--f913f915…（含分区）、
    warehouse/exchange_calendar、raw（单层）、raw/BAOSTOCK|AKSHARE_TENCENT|AKSHARE_SINA 各自 daily_bar_unadjusted、
    artifacts/quality、artifacts/data-diff、artifacts/data-resolution、logs、logs/jobs
  文件 SHA256：
    warehouse/daily_bar_unadjusted/<version_id>/ 下 manifest、COMPLETE 与全部分区 parquet；
    logs/jobs/ 全部 eod-*.jsonl（含 E1 与探针日志）
  全根清单指纹（I0 规格：文件集合/size/mtime + 逐文件 SHA-256，兼作 E1 后新基线）
network_calls=0
writes=0（WAL/SHM/journal 创建禁止；若执行时存在旁车则改用 WAL 感知 mode=ro 只读，不 checkpoint、不删除）
sqlite_checkpoint=0
mutations=forbidden（不移动、不删除、不恢复、不修复）
evidence_out=docs/stage1/evidence/s1_08r3_e1_result_2026-08-18.json（仅仓内写入）
验证=运行前后 allowed_root 文件集合/size/mtime/SHA 完全一致
失败策略=发现不一致或阻断立即停止；恢复须单独精确授权
```

### E1R 执行结果（授权 `S1-08R3-E1R-9c42e7a1b6d8` 一次消费，2026-08-18 OK）

执行器 `scripts/s1_08r3_e1r_result_evidence.py`，证据
[evidence/s1_08r3_e1_result_2026-08-18.json](evidence/s1_08r3_e1_result_2026-08-18.json)：

- **不可变证明**：数据根 420 文件 / 4,480,964 字节全量逐文件 SHA-256，运行前后
  文件集合/size/mtime/SHA 完全一致（`before_after_identical=true`）；零 WAL/SHM/
  journal、零 stale `.incomplete`、零发布锁；网络 0、写入 0、checkpoint 0。
  **E1 后新基线指纹 `299ccba9cbb1c7a7ca7ccee92763a265ddb8515d8baee261495ef312b0ecf0da`**。
- **E1 发布版本明细**（`dataset_versions`）：`data_version:f913f915-0a73-476a-9513-ebdca087dd90`
  PUBLISHED，row_count **82**、security_count **2**、content SHA
  `4f4cf231…`、manifest SHA `93e234e5…`、coverage `2026-06-11..2026-08-07`
  （target 末两日 08-08/09 为周末，末日交易日即 08-07 周五，与 C1 日历一致）；
  warehouse 目录含 COMPLETE、input_listing.json、manifest.json 与 3 个分区
  parquet（逐文件 SHA 冻结于证据）。
- **日历版本复验**：`data_version:083d7fb7…` PUBLISHED、150 行、content SHA
  `46fc9c76…`、coverage `2026-05-27..2026-08-09`（与 C1 冻结值逐项一致）。
- **Job 事实**：`job:cd282234…` SUCCEEDED（key `s1-08r3-e1-formal-release-…-v1`），
  事件链 `job_created → running → heartbeats → artifacts → canonical_publish
  （不可取消相位）→ "K4 resolved daily bars published" → job_succeeded`；
  R3C 探针 Job `job:b5027b23…` SUCCEEDED；历史失败 Job 保持 FAILED。全库计数：
  dataset_versions 3、ingestion_batches 7、quality_runs 8、quality_results 66、
  difference_reports 6、resolution_runs 2、job_runs 3、job_events 33；全部
  `logs/jobs/eod-*.jsonl` SHA 冻结。

## R3D 归属声明

按任务卡与用户决定（2026-08-18）：**R3D Stage 1 终验不由本开发 agent 执行**，须由
独立 agent 验收并提交 `S1-08_ACCEPTANCE.md`；该提交完成后 Stage 1 才可标记完成并
授权开始 Stage 2。本任务全部授权（I0/C1×4/E1/R3C 探针/E1R）均已消费且不可重放。

## S1-08R3F-LITE 精简修复轮（ADR-028，2026-08-18）

**信任模型（ADR-028，`docs/DECISIONS.md`）**：单用户、本机、操作者即数据所有者；
本地用户与被其启动的开发 Agent 属同一管理边界。授权机制只追求防误执行、防范围
意外扩大、防同任务意外重放、保留可审计事实；不以防御本机管理员或恶意本地进程为
目标。内容 SHA 是批准内容的完整性与漂移检测措施，**不是身份认证**；不引入 PKI/
签名服务/HSM/密钥管理/多层 grant 链。`E:\QuantKingV3Data` 是公开声明的标准本地
数据根：允许出现在内部任务/授权/证据/验收文档中，Web、DataCenter API 与最终用户
响应仍不得返回本机绝对路径。E1/C1 历史 attempt header 已证明实际执行使用了批准的
envelope/plan SHA，入口健壮性缺陷不使历史正式产物失效。

**四项修复（全部离线开发 + 合成测试，未重跑任何真实执行）**：

1. **E1 正式入口内容绑定**（`formal_authorization.py` + `cli/eod.py`）：token 格式
   升级为 `<marker>:<authorization_id>:<envelope_sha256>`——token 第三段必须等于
   按 frozen canonical JSON 重算的 envelope SHA，envelope 任意字段变化即令旧
   marker/id token 失效（`FORMAL_AUTHORIZATION_MISMATCH`，先于 runtime/provider/
   业务写入）。新增 `assert_formal_entry_boundary`：`allowed_write_paths` 必须与
   `DEFAULT_ALLOWED_WRITE_PATHS` 模板逐项相等（`FORMAL_WRITE_PATHS_INVALID`）；
   `calendar_source` 必须属于 `ALLOWED_CALENDAR_SOURCES`（published-warehouse-
   exchange-calendar / baostock-query-trade-dates）；数据根必须等于配置声明根
   （默认 `E:/QuantKingV3Data`，测试可显式声明临时根；`FORMAL_DATA_ROOT_INVALID`）；
   attempts root 必须绝对路径、无 `..`、非 symlink/reparse、解析后位于受控 base
   （默认仓库根）之内（`FORMAL_ATTEMPTS_ROOT_INVALID`）。执行顺序：token→manifest
   绑定→代码树/HEAD→边界→一次性 ledger→runtime→provider。
2. **C1 plan 强类型校验**（`calendar_bootstrap.py`）：新 `parse_calendar_bootstrap_plan`
   返回 `CheckedCalendarBootstrapPlan`，逐字段校验全部 26 个键并拒绝未知字段——
   marker/authorization_id/idempotency_key 形状、provider=BAOSTOCK、upstream=BAOSTOCK、
   endpoint=query_trade_dates、adapter/baostock/dataset/schema/code/rule 版本、窗口
   有界（≤400 天）、exchanges 恰为 SH/SZ、endpoint_max=1/http=0/retries=0/
   protocol_max∈[3,4]、20s 超时、fail_fast、allowed_write_paths 与冻结模板全等、
   legacy/backup/other_datasets 全 false、双 SHA 格式、requires_new_real_authorization。
   token 为 `<marker>:<authorization_id>:<plan_sha256>`（`calendar_bootstrap_token`），
   任意字段变化即失效。原仅查 schema version 的 `_require_plan` 已移除。
3. **R3C 未来探针防重放**（`scripts/s1_08r3_r3c_idempotency_probe.py`）：新增简单
   一次性 attempt ledger（复用 `FormalAttemptLedger`），绑定 marker、authorization
   ID、probe plan SHA（manifest/envelope 文件 SHA + 幂等键的规范 SHA）、idempotency
   key 与开始时间；在**任何数据根访问之前**消费，token
   `S1-08R3-R3C-IDEMPOTENCY-PROBE-V1:<id>:<plan_sha>`（env `R3C_PROBE_AUTHORIZATION`）；
   同 ID 重放/FAILED 复用即拒；evidence 输出改为新文件并拒绝覆盖（append-only）。
   **本次未重跑 R3C**：旧 2026-08-18 证据字节不变；按 ADR-028，旧 R3C 重复执行属
   流程偏差（第 2 次零网络/零新版本/零新增业务产物），不以重跑制造新业务状态。
4. **正式版本逐键 verifier**（`release_verifier.py`）：新
   `verify_release_row_proof(data_root, version_id, job_id, emit)`——只读 immutable
   模式下：重建 E1 Job 的 A（BAOSTOCK）/B（TENCENT）raw batch（manifest+逐文件 SHA
   复验）→ `SupplierResponse.from_raw_payload` → `normalize_market_data_response`
   得到**规范化实际行**；读取 PUBLISHED warehouse parquet 行、DifferenceReport
   `row_differences`、Resolution artifact 默认选择与 Job 预算日志；对每个发布键：
   双源必须同时存在（`ROW_SINGLE_SOURCE`/`ROW_COMMON_MISSING`）、OHLC/`pre_close`
   零容差（`ROW_PRICE_MISMATCH`）、量额绝对 50/相对 0（`ROW_TOLERANCE_EXCEEDED`，
   相对轴禁用）、DR 行必须能从 A/B 实际值精确重算（`DR_MISMATCH`/`DR_MISSING_ROW_
   RECORDED`；comparator 只记录超阈值行，可发布版本不得有任何 BOTH 类字段行）、
   PUBLISHED 行逐字段等于 selected source（`PUBLISHED_NOT_SELECTED`）、selected 必须
   是 A/B 共识 primary（`C_SILENT_SUBSTITUTION`）、lineage 零 `XTQUANT_LEGACY`
   （`XTQUANT_LINEAGE`）、冻结命令的日历引用必须指向 PUBLISHED 日历版本且 warehouse
   目录 verify（`CALENDAR_BINDING_*`）。`emit` 逐键输出 JSONL 证明行（code、
   trade_date、A/B 来源与 chunk 文件 SHA、A/B 规范化字段与行 SHA、逐字段 delta 与
   threshold、selected、published 字段与行 SHA、DR/Resolution ID）。纯函数
   `compare_row_pair`/`assert_dr_rows_recompute`/`assert_published_equals_selected`
   承载全部 tamper 单测矩阵。

**新增测试**：`test_s1_08r3_formal_entry.py`（token 内容绑定/边界漂移 5 类稳定码/
旧两段 token 拒绝/零调用零 ledger）、`test_calendar_bootstrap.py`（parse 全字段
drift 矩阵 26 例 + token 内容失效）、`test_s1_08r3_r3c_probe_ledger.py`（5 项离线
replay：同 ID 拒/FAILED 不可复用/COMPLETED 只读/plan SHA 绑定/无授权先于 ledger
拒绝，全部临时目录）、`test_s1_08r3_row_proof.py`（13 项：阈值冻结与相对轴禁用、
单源/共同缺失、价格任意差、量额 50 过 51 拒、相对大比率小绝对值通过、DR 三类
mismatch、published≠selected、C 静默替换、集成全链逐键验证 + 只读无 sidecar、
XTQUANT lineage 拒绝、raw 文件篡改拒绝）。聚焦测试 **167 passed**。

**R3V2 纯只读复验（已批准并执行，2026-08-18 VERIFIED，授权
`S1-08R3-R3V2-7e4c1b9d2a68` 一次消费）**：计划冻结于
[evidence/s1_08r3_r3v2_plan_2026-08-18.json](evidence/s1_08r3_r3v2_plan_2026-08-18.json)
（文件 SHA `6f7f906e…`；marker `S1-08R3-R3V2-READONLY-VERIFY-V1`、plan_sha256
`cff0c1a8…`、verifier script SHA `37bfe4bf…`、code tree `5990f88c…`、git HEAD
`9f5b9ea…`）。执行结果：

- **逐键证明（82/82 全部 passed）**：A 实际来源行 82、B 实际来源行 82、C 行 0
  （零静默替换）；selected=BAOSTOCK（Resolution 默认共识），全部 82 行
  PUBLISHED 字段与 selected 实际行**逐字段相等**；阈值重算——OHLC/`pre_close`
  最大绝对 delta **0**（零容差内）、volume 最大 **50**（恰在冻结阈值边界内，
  ≤50 通过）、amount 最大 **49.72**（<50）；DifferenceReport
  `data_diff:193d13c4…` 可由 A/B 值重算（发布版本无任何超阈值行）；质量
  `data_quality:4b3ec4b4…`、Resolution `data_resolution:d42d6e2d…`、日历版本
  `083d7fb7…` PUBLISHED 绑定全部 verify；预算实际 A=4/B=4/C=0（上限 4/4/4，
  总 12）、应用层重试 0、零 `XTQUANT_LEGACY` 血缘。
- **不可变证明**：数据根前后聚合根指纹完全一致（420 文件 / 4,480,964 字节 /
  聚合 SHA `33b331e8…`）、零 SQLite sidecar；网络 0、数据根写入 0、checkpoint 0、
  新 Job/版本/产物 0；metadata 读取 `ro&immutable=1`+`query_only`（10 表计数与
  E1R 完全一致：dataset_versions 3、ingestion_batches 7、quality_runs 8、
  difference_reports 6、resolution_runs 2、job_runs 3、job_events 33）。
- **证据**：[result](evidence/s1_08r3_r3v2_result_2026-08-18.json) SHA
  `dcdcdd24…`、[row proof JSONL](evidence/s1_08r3_r3v2_row_proof_2026-08-18.jsonl)
  （82 行逐键）SHA `6c96fe46…`、[artifact manifest]
  (evidence/s1_08r3_r3v2_artifact_manifest_2026-08-18.json) SHA `2ca3d1b7…`；
  attempt ledger `s1_08r3_r3v2_attempt_2026-08-18/S1-08R3-R3V2-7e4c1b9d2a68/`
  （attempt-header.json + completed.json，append-only）。授权不可重放。

**Stage 1 仍等待独立 R3D2 终验**（`S1-08_ACCEPTANCE.md` 由独立 agent 验收提交）。

**明确声明**：E1 未重跑、C1 未重跑、R3C 未重跑；新 Job 0、新 DatasetVersion 0、
新 PUBLISHED 版本 0；未创建 `S1-08_ACCEPTANCE.md`；Stage 1 尚未最终验收；Stage 2
未授权；工作区已于 2026-08-18 经用户批准形成单一 S1-08R3 提交（仅本地，未 push，
R3D2 验收记录将由独立 agent 另行提交）。

## Phase A 离线门禁

Phase A 首轮（1354 passed / 6 skipped / 3 deselected）与 Phase C1 轮完整
`scripts\quality.ps1 -UvCacheDirectory .uv-cache`（PowerShell 7）均通过：uv lock check ✓、
frozen dependency sync ✓、Ruff lint/format ✓、mypy（135 源文件）✓、离线 pytest
**1384 passed / 6 skipped / 3 deselected** ✓、架构边界 ✓（C1 轮曾拦截 `data.eod` 直接
导入 pyarrow，已按边界把投影移入 `data/adapters/calendar_projection.py` 后通过）、
Markdown 链接 ✓、前端 npm ci/typecheck/unit/production build ✓、Playwright E2E
（1 passed）✓；最后 `git diff --check` 两轮均因 TRAE 沙箱对系统缓存文件的拦截在
quality.ps1 内失败，经 `scripts/git-read.ps1 diff --check` 复核通过（HEAD=`9f5b9ea`，
无空白错误）。定向新增测试：`tests/data/eod/test_formal_authorization.py`（27 项）、
`tests/integration/data/test_s1_08r3_formal_entry.py`（11 项）、
`tests/integration/data/test_s1_08r3_r2_gap_closures.py`（4 项）、
`tests/data/adapters/test_baostock_calendar.py`（10 项）、
`tests/data/eod/test_calendar_bootstrap.py`（6 项）、
`tests/integration/data/test_s1_08r3_calendar_bootstrap.py`（9 项，含 CLI 子进程）全部
通过，R2 回归（E2E/发布可靠性/manifest 契约/command service/CLI）39 项通过。

F-LITE + R3V2 收口轮（2026-08-18，提交前复跑）：修复
`tests/docs/test_stage1_status.py` 一处 RUF001 全角逗号（断言短语拆分为
"已批准并执行" + "2026-08-18 VERIFIED"，文档匹配不变）后，完整
`scripts\quality.ps1 -UvCacheDirectory .uv-cache` **退出码 0 全绿**：uv lock ✓、
frozen sync ✓、Ruff lint/format ✓、mypy（135 源文件）✓、离线 pytest ✓、架构边界 ✓、
Markdown 链接 ✓、前端 npm ci/typecheck/unit/production build ✓、Playwright E2E
（1 passed）✓、`git diff --check` ✓（本轮在 quality.ps1 内直接通过，无需旁路复核）。

## 外部访问声明（Phase A + I0 + Phase C1 开发）

- Phase A 开发：真实网络 0；`E:\QuantKingV3Data` 读取 0、写入 0；`E:\QuantKing` 读取 0；
  F 盘访问 0；Git 元数据写入 0（不提交）；历史 marker/authorization_id 重放 0。
- I0 执行（2026-08-17 用户批准，authorization_id `S1-08R3-I0-7d3e9a1c4b62`，一次消费）：
  真实网络 0；`E:\QuantKingV3Data` **严格只读**（写入 0、checkpoint 0、无 WAL/SHM/journal
  创建、无任何变更；371 文件运行前后集合/size/mtime/SHA 全等，会话闭合指纹一致）；
  `E:\QuantKing` 读取 0；F 盘访问 0；Git 元数据写入 0；历史 marker/authorization_id 重放 0。
- Phase C1 开发：真实网络 0（全部合成 runner/fake module/CLI 子进程重定向数据根）；
  `E:\QuantKingV3Data` 读写 0；`E:\QuantKing` 0；F 盘 0；Git 元数据写入 0。
- C1 第 1 次真实 attempt（用户批准，一次消费，FAILED 即停）：BaoStock 网络调用 0
  （失败先于 provider 会话）；`E:\QuantKingV3Data` 业务写入 0（DB 主文件 SHA 与 I0
  逐字节一致）；唯一数据根残留 = 0 字节 `metadata/quantking.db-wal`（空文件、无数据，
  保留为失败证据，清理须单独授权）；`E:\QuantKing` 0；F 盘 0；Git 元数据写入 0。
- C1-R4 第 4 次真实 attempt（用户授权，一次消费，SUCCEEDED）：真实网络调用仅 BaoStock
  `query_trade_dates` 1 次（协议 3 消息）；`E:\QuantKingV3Data` 写入严格限于 C1 授权块
  白名单（raw batch 1、quality artifact 1、metadata 3 行、warehouse exchange_calendar
  1 版本）；`E:\QuantKing` 0；F 盘 0；Git 元数据写入 0。
- E1 冻结产物生成：零网络、零数据根写入（纯派生）。
- E1 正式执行（用户批准，一次消费，SUCCEEDED）：真实网络调用严格限于授权块预算内
  （endpoint 总量 ≤12、AKShare HTTP ≤20、BaoStock 协议 ≤12、应用层重试 0；超限即
  fail-fast FAILED，Job SUCCEEDED 证明预算未突破）；`E:\QuantKingV3Data` 写入严格限于
  `allowed_write_paths` 白名单（恰好 1 个 Job、≤1 个 PUBLISHED 版本及其 raw/quality/
  artifacts/locks 血缘）；`E:\QuantKing` 0；F 盘 0；Git 元数据写入 0（HEAD 保持
  `9f5b9ea`）。授权块内独立只读复验（`verify_manifest_release`）通过。
- R3C 双幂等探针（2026-08-18 用户授权，一次消费，SUCCEEDED）：真实网络调用 **0**
  （三角色全部为注入的计数即抛零网络网关，两轮调用计数均 0/0/0）；`E:\QuantKingV3Data`
  写入严格限于探针声明边界（第 1 轮 +1 探针 Job +4 事件 +1 日志 +1 artifact 绑定；
  第 2 轮重放零写入；版本/batch/quality/resolution/difference/warehouse/artifacts
  零变化）；`E:\QuantKing` 0；F 盘 0；Git 元数据写入 0。
- E1R 结果证据提取（2026-08-18 用户批准，一次消费，OK）：真实网络 0；
  `E:\QuantKingV3Data` **严格只读**（420 文件全量 SHA，运行前后集合/size/mtime/SHA
  完全一致；sqlite `mode=ro&immutable=1` + `query_only`，无旁车、无 checkpoint、
  无任何变更）；`E:\QuantKing` 0；F 盘 0；Git 元数据写入 0；唯一写入为仓内证据 JSON。
- 下一步：R3D 终验由独立 agent 执行（不由本开发 agent）；任何新的数据根访问须单独授权。
